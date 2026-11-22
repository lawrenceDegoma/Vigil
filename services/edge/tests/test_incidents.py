from __future__ import annotations

import itertools
from dataclasses import replace

import pytest

from helpers import result, window
from vigil_edge.config import ConvulsionConfig, IncidentConfig, VomitingConfig
from vigil_edge.detectors import ConvulsionDetector, VomitingDetector
from vigil_edge.incidents import IncidentStateMachine, Output
from vigil_edge.models import (
    DetectorMode,
    IncidentEvent,
    IncidentEventType,
    MonitorState,
    Severity,
    ShadowDetection,
    StateChange,
)
from vigil_edge.profiles import get_profile

STEP = 0.5
# Onset estimate = t - coverage * window_seconds = t - 0.5 s (helpers.result coverage=0.1).
FIRST_WINDOW = window(active_ratio=0.1)


def make_fsm(
    vomiting_mode: DetectorMode = DetectorMode.ALERT, **incident: float
) -> IncidentStateMachine:
    counter = itertools.count(1)
    return IncidentStateMachine(
        [
            ConvulsionDetector(ConvulsionConfig()),
            VomitingDetector(VomitingConfig(mode=vomiting_mode)),
        ],
        get_profile("dog"),
        IncidentConfig(**incident),
        window_seconds=5.0,
        rule_version="test",
        id_factory=lambda: f"id-{next(counter)}",
    )


def drive(
    fsm: IncidentStateMachine,
    until: float,
    conv: list[tuple[float, float]] = (),  # type: ignore[assignment]
    vom: list[tuple[float, float]] = (),
    start: float = 0.0,
) -> list[Output]:  # type: ignore[assignment]
    """Feed windows every STEP seconds; signals are active within the given spans."""
    out: list[Output] = []
    t = start
    while t <= until + 1e-9:
        c = any(a <= t < b for a, b in conv)
        v = any(a <= t < b for a, b in vom)
        out += fsm.update(t, [result("convulsion", c), result("vomiting", v)], FIRST_WINDOW)
        t = round(t + STEP, 6)
    return out


def events(outs: list[Output], kind: IncidentEventType | None = None) -> list[IncidentEvent]:
    return [
        o for o in outs if isinstance(o, IncidentEvent) and (kind is None or o.event_type is kind)
    ]


def states(outs: list[Output]) -> list[MonitorState]:
    return [o.current for o in outs if isinstance(o, StateChange)]


def test_short_burst_does_not_open_incident() -> None:
    fsm = make_fsm()
    outs = drive(fsm, 60, conv=[(10, 15)])  # 5 s < 10 s onset
    assert events(outs) == []
    assert fsm.state is MonitorState.MONITORING


def test_sustained_signal_opens_one_incident_after_onset() -> None:
    fsm = make_fsm()
    outs = drive(fsm, 50, conv=[(10, 40)])
    opened = events(outs, IncidentEventType.OPENED)
    assert len(opened) == 1
    inc = opened[0].incident
    assert inc.severity is Severity.URGENT
    assert inc.started_at == pytest.approx(9.5)
    assert inc.opened_at == pytest.approx(19.5)  # onset + 10 s
    assert MonitorState.URGENT in states(outs)


def test_short_gaps_tolerated_while_building() -> None:
    fsm = make_fsm()
    outs = drive(fsm, 40, conv=[(10, 14), (16, 30)])  # 2 s gap < 3 s tolerance
    assert len(events(outs, IncidentEventType.OPENED)) == 1
    assert events(outs, IncidentEventType.OPENED)[0].incident.started_at == pytest.approx(9.5)


def test_long_gap_resets_candidate() -> None:
    fsm = make_fsm()
    outs = drive(fsm, 40, conv=[(10, 17), (22, 29)])  # 5 s gap; each part < 10 s
    assert events(outs) == []


def test_pause_shorter_than_end_quiet_keeps_one_incident() -> None:
    fsm = make_fsm()
    outs = drive(fsm, 100, conv=[(10, 40), (45, 70)])  # 5 s pause < 8 s end_quiet
    assert len(events(outs, IncidentEventType.OPENED)) == 1
    ended = events(outs, IncidentEventType.ENDED)
    assert len(ended) == 1
    assert ended[0].incident.ended_at == pytest.approx(69.5)


def test_end_then_recovery_then_monitoring() -> None:
    fsm = make_fsm(recovery_observation_seconds=60)
    outs = drive(fsm, 200, conv=[(10, 40)])
    ended = events(outs, IncidentEventType.ENDED)
    assert len(ended) == 1
    assert ended[0].incident.ended_at == pytest.approx(39.5)
    assert ended[0].at == pytest.approx(47.5)  # 8 s quiet
    assert states(outs) == [
        MonitorState.URGENT,
        MonitorState.RECOVERY_OBSERVATION,
        MonitorState.MONITORING,
    ]


def test_resumption_within_merge_gap_continues_same_incident() -> None:
    fsm = make_fsm(merge_gap_seconds=30)
    outs = drive(fsm, 120, conv=[(10, 40), (55, 80)])  # restarts 15 s after ending
    opened = events(outs, IncidentEventType.OPENED)
    updated = events(outs, IncidentEventType.UPDATED)
    assert len(opened) == 1
    assert len(updated) == 1
    assert updated[0].incident.id == opened[0].incident.id
    assert updated[0].incident.ended_at is None
    assert updated[0].features["resumed"] == 1.0
    assert len(fsm.incidents) == 1


def test_repeat_episode_after_merge_gap_is_new_linked_incident() -> None:
    fsm = make_fsm(merge_gap_seconds=30, recovery_observation_seconds=120)
    outs = drive(fsm, 200, conv=[(10, 40), (90, 120)])
    opened = events(outs, IncidentEventType.OPENED)
    assert len(opened) == 2
    first, second = (e.incident for e in opened)
    assert second.follows_incident_id == first.id
    assert len(fsm.incidents) == 2


def test_vomiting_early_warning_then_escalation() -> None:
    fsm = make_fsm(recovery_observation_seconds=30)
    outs = drive(fsm, 200, vom=[(10, 20)], conv=[(60, 90)])
    opened = events(outs, IncidentEventType.OPENED)
    assert [e.incident.severity for e in opened] == [Severity.WARNING, Severity.URGENT]
    warning, urgent = (e.incident for e in opened)
    assert urgent.follows_incident_id == warning.id
    # Early warning → urgent → recovery → plain monitoring (warning cleared by escalation).
    assert states(outs) == [
        MonitorState.EARLY_WARNING,
        MonitorState.URGENT,
        MonitorState.RECOVERY_OBSERVATION,
        MonitorState.MONITORING,
    ]


def test_repeated_vomiting_within_warning_window_dedupes() -> None:
    fsm = make_fsm(early_warning_window_seconds=600)
    outs = drive(fsm, 400, vom=[(10, 20), (200, 210)])
    assert len(events(outs, IncidentEventType.OPENED)) == 1
    assert len(events(outs, IncidentEventType.UPDATED)) == 1
    assert fsm.state is MonitorState.EARLY_WARNING


def test_early_warning_expires() -> None:
    fsm = make_fsm(early_warning_window_seconds=60)
    outs = drive(fsm, 120, vom=[(10, 20)])
    assert states(outs) == [MonitorState.EARLY_WARNING, MonitorState.MONITORING]


def test_seizure_detection_does_not_depend_on_vomiting() -> None:
    fsm = make_fsm(vomiting_mode=DetectorMode.OFF)
    outs = drive(fsm, 60, conv=[(10, 40)])
    assert len(events(outs, IncidentEventType.OPENED)) == 1


def test_shadow_mode_never_opens_incidents() -> None:
    fsm = make_fsm(vomiting_mode=DetectorMode.SHADOW)
    outs = drive(fsm, 60, vom=[(10, 25)])
    shadows = [o for o in outs if isinstance(o, ShadowDetection)]
    assert len(shadows) == 1
    assert events(outs) == []
    assert fsm.state is MonitorState.MONITORING


def test_finish_closes_open_incident() -> None:
    fsm = make_fsm()
    outs = drive(fsm, 30, conv=[(10, 100)])
    outs += fsm.finish()
    ended = events(outs, IncidentEventType.ENDED)
    assert len(ended) == 1
    assert ended[0].features["end_reason"] == "stream_end"


def test_event_ids_are_unique_idempotency_keys() -> None:
    fsm = make_fsm()
    outs = drive(fsm, 200, conv=[(10, 40), (100, 130)])
    ids = [e.event_id for e in events(outs)]
    assert len(ids) == len(set(ids))


def test_events_carry_snapshots_not_live_objects() -> None:
    fsm = make_fsm()
    outs = drive(fsm, 60, conv=[(10, 40)])
    opened = events(outs, IncidentEventType.OPENED)[0]
    assert opened.incident.ended_at is None  # later end must not mutate the emitted event


def test_onset_estimate_never_precedes_window_start() -> None:
    fsm = make_fsm()
    full = window(t_start=0.0, t_end=5.0, active_ratio=1.0)
    outs: list[Output] = []
    t = 5.0
    while t <= 30:
        r = result("convulsion", True)
        outs += fsm.update(t, [replace(r, coverage=1.0)], replace(full, t_start=t - 5, t_end=t))
        t += STEP
    inc = events(outs, IncidentEventType.OPENED)[0].incident
    assert inc.started_at == pytest.approx(0.0)


def _drive_conv(
    fsm: IncidentStateMachine, until: float, span: tuple[float, float], strong
) -> list[Output]:  # type: ignore[no-untyped-def]
    out: list[Output] = []
    t, i = 0.0, 0
    while t <= until + 1e-9:
        active = span[0] <= t < span[1]
        out += fsm.update(
            t,
            [result("convulsion", active, strong=strong(i)), result("vomiting", False)],
            FIRST_WINDOW,
        )
        t, i = round(t + STEP, 6), i + 1
    return out


def test_single_region_rhythm_needs_the_longer_onset() -> None:
    """One spot moving (e.g. a scratching leg) must persist 20 s, not 10 s (ADR-022)."""
    short = _drive_conv(make_fsm(), 60, (10, 26), strong=lambda i: False)  # ~16 s
    assert events(short, IncidentEventType.OPENED) == []
    long = _drive_conv(make_fsm(), 80, (10, 45), strong=lambda i: False)
    [opened] = events(long, IncidentEventType.OPENED)
    assert opened.at - opened.incident.started_at >= 20.0


def test_multi_limb_rhythm_keeps_the_fast_onset() -> None:
    outs = _drive_conv(make_fsm(), 60, (10, 30), strong=lambda i: True)
    [opened] = events(outs, IncidentEventType.OPENED)
    assert 10.0 <= opened.at - opened.incident.started_at < 11.0


def test_intermittent_multi_limb_evidence_is_enough() -> None:
    # Real paddling shows the spread in most but not all windows; 1 in 3 suffices.
    outs = _drive_conv(make_fsm(), 60, (10, 30), strong=lambda i: i % 3 == 0)
    [opened] = events(outs, IncidentEventType.OPENED)
    assert opened.at - opened.incident.started_at < 11.0
