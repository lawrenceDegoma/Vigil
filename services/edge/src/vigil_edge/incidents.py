"""Incident state machine (DESIGN §5.2, ADR-009).

Turns per-window detector results into incident lifecycle events.

Per detector:

* **Candidate.** While no incident is open for a detector, consecutive active
  windows build a candidate. Pauses up to ``gap_tolerance_seconds`` are
  tolerated. A candidate is *confirmed* once it has lasted ``onset_seconds``
  (measured from its estimated onset) with at least ``min_active_ratio`` of
  its windows active.
* **Shadow mode.** Confirmation emits a ``ShadowDetection`` only.
* **Alert mode.** Confirmation opens an incident, or resumes the previous one
  if the new onset falls within the merge window (``merge_gap_seconds`` for
  urgent signals; the whole early-warning window for warning signals, so a
  run of vomiting produces one early warning rather than many).
* **Open incident.** Every active window extends it. After
  ``end_quiet_seconds`` without activity it ends (``ended_at`` = last active).

The top-level state is derived each update:
``URGENT`` if any urgent incident is open, else ``RECOVERY_OBSERVATION``
within ``recovery_observation_seconds`` of an urgent incident ending, else
``EARLY_WARNING`` within ``early_warning_window_seconds`` of warning-signal
activity, else ``MONITORING``. An urgent incident clears any pending early
warning so recovery returns to plain monitoring.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace

from vigil_edge.config import IncidentConfig
from vigil_edge.detectors.base import Detector
from vigil_edge.models import (
    DetectionResult,
    DetectorMode,
    Incident,
    IncidentEvent,
    IncidentEventType,
    MonitorState,
    Severity,
    ShadowDetection,
    StateChange,
    WindowFeatures,
)
from vigil_edge.profiles import Profile

Output = IncidentEvent | StateChange | ShadowDetection


def _uuid() -> str:
    return str(uuid.uuid4())


@dataclass(slots=True)
class _Candidate:
    onset: float
    last_active: float
    windows: int = 0
    active_windows: int = 0
    strong_windows: int = 0
    peak: float = 0.0
    confirmed: bool = False


@dataclass(slots=True)
class _Track:
    detector: Detector
    severity: Severity
    candidate: _Candidate | None = None
    open: Incident | None = None
    last: Incident | None = None  # most recent incident (open or ended)
    history: list[Incident] = field(default_factory=list)


class IncidentStateMachine:
    def __init__(
        self,
        detectors: Sequence[Detector],
        profile: Profile,
        cfg: IncidentConfig,
        window_seconds: float,
        rule_version: str,
        id_factory: Callable[[], str] = _uuid,
    ) -> None:
        self.cfg = cfg
        self.window_seconds = window_seconds
        self.rule_version = rule_version
        self._new_id = id_factory
        self._tracks = {
            d.name: _Track(d, profile.severity_for(d.signal_type))
            for d in detectors
            if d.mode is not DetectorMode.OFF
        }
        self.state = MonitorState.MONITORING
        self._recovery_until = float("-inf")
        self._warning_until = float("-inf")
        self._last_t = 0.0

    # ----------------------------------------------------------------- public

    @property
    def incidents(self) -> list[Incident]:
        out = [i for t in self._tracks.values() for i in t.history]
        return sorted(out, key=lambda i: i.opened_at)

    def update(
        self, t: float, results: Sequence[DetectionResult], window: WindowFeatures | None = None
    ) -> list[Output]:
        self._last_t = t
        outputs: list[Output] = []
        for r in results:
            track = self._tracks.get(r.detector)
            if track is not None:
                outputs.extend(self._update_track(track, t, r, window))
        outputs.extend(self._end_quiet_incidents(t))
        outputs.extend(self._refresh_state(t))
        return outputs

    def finish(self, t: float | None = None) -> list[Output]:
        """Close any open incidents at end of stream."""
        t = self._last_t if t is None else t
        outputs: list[Output] = []
        for track in self._tracks.values():
            if track.open is not None:
                outputs.append(self._end(track, t, reason="stream_end"))
        outputs.extend(self._refresh_state(t))
        return outputs

    # --------------------------------------------------------------- internals

    def _update_track(
        self, track: _Track, t: float, r: DetectionResult, window: WindowFeatures | None
    ) -> list[Output]:
        det = track.detector
        if track.open is not None:
            if r.active:
                track.open.last_active_at = t
                track.open.peak_score = max(track.open.peak_score, r.score)
                if track.severity is Severity.WARNING:
                    self._warning_until = t + self.cfg.early_warning_window_seconds
            return []

        cand = track.candidate
        if r.active:
            if cand is None:
                # Onset estimate: the active portion of the first active window.
                onset = t - max(r.coverage, 0.1) * self.window_seconds
                if window is not None:  # never before the window (or stream) began
                    onset = max(onset, window.t_start)
                cand = track.candidate = _Candidate(onset=onset, last_active=t)
            cand.last_active = t
            cand.active_windows += 1
            cand.strong_windows += int(r.strong)
            cand.peak = max(cand.peak, r.score)
        if cand is None:
            return []
        cand.windows += 1
        if not r.active and t - cand.last_active > self.cfg.gap_tolerance_seconds:
            track.candidate = None
            return []
        if cand.confirmed or not r.active:
            # Only an active window can confirm: a trailing pause must not count
            # towards the onset duration.
            return []
        lasted = t - cand.onset
        ratio = cand.active_windows / cand.windows
        strong_share = cand.strong_windows / cand.active_windows
        needed = (
            det.onset_seconds if strong_share >= det.strong_min_share else det.weak_onset_seconds
        )
        if lasted < needed or ratio < det.min_active_ratio:
            return []

        cand.confirmed = True
        features = {"score": r.score, **r.components}
        if window is not None:
            features.update(window.as_dict())
        if det.mode is DetectorMode.SHADOW:
            return [ShadowDetection(t, det.name, det.signal_type, cand.onset, cand.peak)]
        outputs = self._open_or_resume(track, t, cand, features)
        track.candidate = None
        return outputs

    def _merge_window(self, severity: Severity) -> float:
        if severity is Severity.WARNING:
            return max(self.cfg.merge_gap_seconds, self.cfg.early_warning_window_seconds)
        return self.cfg.merge_gap_seconds

    def _open_or_resume(
        self, track: _Track, t: float, cand: _Candidate, features: dict[str, float]
    ) -> list[Output]:
        prev = track.last
        if (
            prev is not None
            and prev.ended_at is not None
            and cand.onset - prev.ended_at <= self._merge_window(track.severity)
        ):
            prev.ended_at = None
            prev.last_active_at = t
            prev.peak_score = max(prev.peak_score, cand.peak)
            track.open = prev
            if track.severity is Severity.WARNING:
                self._warning_until = t + self.cfg.early_warning_window_seconds
            return [self._event(IncidentEventType.UPDATED, t, prev, {**features, "resumed": 1.0})]

        follows: str | None = None
        if track.severity is Severity.URGENT:
            if t < self._warning_until:
                follows = self._latest_id(Severity.WARNING)
            elif t < self._recovery_until:
                follows = self._latest_id(Severity.URGENT)
        incident = Incident(
            id=self._new_id(),
            signal_type=track.detector.signal_type,
            severity=track.severity,
            started_at=cand.onset,
            opened_at=t,
            last_active_at=t,
            peak_score=cand.peak,
            follows_incident_id=follows,
            rule_version=self.rule_version,
        )
        track.open = track.last = incident
        track.history.append(incident)
        if track.severity is Severity.WARNING:
            self._warning_until = t + self.cfg.early_warning_window_seconds
        else:
            self._warning_until = float("-inf")
        return [self._event(IncidentEventType.OPENED, t, incident, features)]

    def _latest_id(self, severity: Severity) -> str | None:
        candidates = [
            tr.last for tr in self._tracks.values() if tr.severity is severity and tr.last
        ]
        if not candidates:
            return None
        return max(candidates, key=lambda i: i.opened_at).id

    def _end_quiet_incidents(self, t: float) -> list[Output]:
        outputs: list[Output] = []
        for track in self._tracks.values():
            inc = track.open
            if inc is not None and t - inc.last_active_at >= self.cfg.end_quiet_seconds:
                outputs.append(self._end(track, t, reason="quiet"))
        return outputs

    def _end(self, track: _Track, t: float, reason: str) -> IncidentEvent:
        inc = track.open
        assert inc is not None
        inc.ended_at = inc.last_active_at
        track.open = None
        if track.severity is Severity.URGENT:
            self._recovery_until = t + self.cfg.recovery_observation_seconds
        return self._event(IncidentEventType.ENDED, t, inc, {"end_reason": reason})

    def _event(
        self, kind: IncidentEventType, t: float, inc: Incident, features: Mapping[str, object]
    ) -> IncidentEvent:
        return IncidentEvent(self._new_id(), kind, t, replace(inc), dict(features))

    def _refresh_state(self, t: float) -> list[Output]:
        urgent_open = any(
            tr.open is not None for tr in self._tracks.values() if tr.severity is Severity.URGENT
        )
        if urgent_open:
            new, reason = MonitorState.URGENT, "urgent incident open"
        elif t < self._recovery_until:
            new, reason = MonitorState.RECOVERY_OBSERVATION, "urgent incident ended"
        elif t < self._warning_until:
            new, reason = MonitorState.EARLY_WARNING, "warning signal observed"
        else:
            new, reason = MonitorState.MONITORING, "observation windows elapsed"
        if new is self.state:
            return []
        change = StateChange(t, self.state, new, reason)
        self.state = new
        return [change]
