"""Event-level evaluation against labeled footage.

Label files are JSON::

    {
      "events": [
        {"signal_type": "convulsion", "start": 20.0, "end": 80.0, "note": "..."}
      ]
    }

An empty ``events`` list marks a negative clip (no events of any type).

Matching: a detection (incident or shadow detection) matches a label of the
same signal type when the detection's [onset estimate, end] interval overlaps
the label's [start, end + ``tolerance``]. Per label we report whether it was
detected and the **latency** from labeled onset to the first matching
detection's confirmation. Detections matching no label are **false alerts**.

These metrics describe performance on *this* footage only. They are not
estimates of real-world sensitivity or specificity.
"""

from __future__ import annotations

import json
import statistics
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from vigil_edge.models import Label, SignalType
from vigil_edge.pipeline import PipelineResult


def load_labels(path: Path) -> list[Label]:
    data = json.loads(path.read_text(encoding="utf-8"))
    events = data.get("events")
    if not isinstance(events, list):
        raise ValueError(f"{path}: expected an 'events' list")
    labels = []
    for i, e in enumerate(events):
        try:
            label = Label(
                SignalType(e["signal_type"]),
                float(e["start"]),
                float(e["end"]),
                str(e.get("note", "")),
            )
        except (KeyError, ValueError) as exc:
            raise ValueError(f"{path}: invalid event #{i}: {exc}") from None
        if label.end <= label.start:
            raise ValueError(f"{path}: event #{i} ends before it starts")
        labels.append(label)
    return labels


def labels_to_json(labels: list[Label]) -> dict[str, Any]:
    return {
        "events": [
            {"signal_type": lb.signal_type.value, "start": lb.start, "end": lb.end, "note": lb.note}
            for lb in labels
        ]
    }


@dataclass(frozen=True)
class Detection:
    signal_type: SignalType
    onset: float
    confirmed_at: float
    end: float
    shadow: bool


def detections_from_result(result: PipelineResult) -> list[Detection]:
    out = [
        Detection(
            i.signal_type,
            i.started_at,
            i.opened_at,
            i.ended_at if i.ended_at is not None else result.stats.last_ts,
            False,
        )
        for i in result.incidents
    ]
    out += [
        Detection(s.signal_type, s.started_at, s.at, s.at, True) for s in result.shadow_detections
    ]
    return out


@dataclass
class SignalMetrics:
    signal_type: SignalType
    labeled_events: int = 0
    detected_events: int = 0
    false_alerts: int = 0
    latencies: list[float] = field(default_factory=list)

    @property
    def recall(self) -> float | None:
        return self.detected_events / self.labeled_events if self.labeled_events else None

    def merge(self, other: SignalMetrics) -> None:
        self.labeled_events += other.labeled_events
        self.detected_events += other.detected_events
        self.false_alerts += other.false_alerts
        self.latencies += other.latencies

    def as_dict(self, hours: float) -> dict[str, Any]:
        lat = self.latencies
        return {
            "signal_type": self.signal_type.value,
            "labeled_events": self.labeled_events,
            "detected_events": self.detected_events,
            "recall": None if self.recall is None else round(self.recall, 3),
            "false_alerts": self.false_alerts,
            "false_alerts_per_hour": round(self.false_alerts / hours, 2) if hours > 0 else None,
            "latency_s": {
                "min": round(min(lat), 2),
                "median": round(statistics.median(lat), 2),
                "max": round(max(lat), 2),
            }
            if lat
            else None,
        }


def evaluate(
    detections: list[Detection],
    labels: list[Label],
    tolerance: float = 10.0,
    signal_types: tuple[SignalType, ...] = (SignalType.CONVULSION, SignalType.VOMITING),
) -> dict[SignalType, SignalMetrics]:
    metrics = {s: SignalMetrics(s) for s in signal_types}
    for sig in signal_types:
        m = metrics[sig]
        sig_labels = [lb for lb in labels if lb.signal_type is sig]
        sig_dets = [d for d in detections if d.signal_type is sig]
        matched: set[int] = set()
        m.labeled_events = len(sig_labels)
        for lb in sig_labels:
            hits = [
                (i, d)
                for i, d in enumerate(sig_dets)
                if d.onset <= lb.end + tolerance and d.end >= lb.start
            ]
            if hits:
                m.detected_events += 1
                matched.update(i for i, _ in hits)
                # Clamped at 0: a detection already open when the label starts
                # (e.g. an ongoing episode) has no additional delay.
                m.latencies.append(max(0.0, min(d.confirmed_at for _, d in hits) - lb.start))
        m.false_alerts = len(sig_dets) - len(matched)
    return metrics
