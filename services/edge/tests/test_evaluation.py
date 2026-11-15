from __future__ import annotations

import json
from pathlib import Path

import pytest

from vigil_edge.evaluation import Detection, evaluate, labels_to_json, load_labels
from vigil_edge.models import Label, SignalType

C = SignalType.CONVULSION


def det(onset: float, confirmed: float, end: float, sig: SignalType = C) -> Detection:
    return Detection(sig, onset, confirmed, end, shadow=False)


def test_hit_miss_and_false_alert() -> None:
    labels = [Label(C, 20, 80), Label(C, 300, 360)]
    dets = [det(19, 30, 81), det(150, 160, 170)]
    m = evaluate(dets, labels)[C]
    assert (m.labeled_events, m.detected_events, m.false_alerts) == (2, 1, 1)
    assert m.recall == 0.5
    assert m.latencies == [10]


def test_two_detections_for_one_event_count_once() -> None:
    m = evaluate([det(20, 31, 50), det(55, 66, 80)], [Label(C, 20, 80)])[C]
    assert m.detected_events == 1 and m.false_alerts == 0
    assert m.latencies == [11]


def test_tolerance_after_label_end() -> None:
    labels = [Label(C, 20, 30)]
    assert evaluate([det(35, 45, 50)], labels, tolerance=10)[C].detected_events == 1
    assert evaluate([det(45, 55, 60)], labels, tolerance=10)[C].false_alerts == 1


def test_signal_types_are_separate() -> None:
    m = evaluate([det(20, 30, 40, SignalType.VOMITING)], [Label(C, 20, 40)])
    assert m[C].detected_events == 0
    assert m[SignalType.VOMITING].false_alerts == 1


def test_as_dict_reports_rate() -> None:
    m = evaluate([det(5, 15, 20)], [])[C]
    d = m.as_dict(hours=2.0)
    assert d["false_alerts_per_hour"] == 0.5
    assert d["recall"] is None


def test_label_file_roundtrip(tmp_path: Path) -> None:
    labels = [Label(C, 1.5, 9.0, "episode")]
    p = tmp_path / "x.labels.json"
    p.write_text(json.dumps(labels_to_json(labels)))
    assert load_labels(p) == labels


def test_label_file_validation(tmp_path: Path) -> None:
    p = tmp_path / "bad.labels.json"
    p.write_text(json.dumps({"events": [{"signal_type": "convulsion", "start": 5, "end": 1}]}))
    with pytest.raises(ValueError, match="ends before"):
        load_labels(p)
    p.write_text(json.dumps({"events": [{"signal_type": "sneeze", "start": 0, "end": 1}]}))
    with pytest.raises(ValueError, match="invalid event"):
        load_labels(p)


def test_latency_clamped_for_ongoing_detection() -> None:
    m = evaluate([det(10, 20, 100)], [Label(C, 50, 60)])[C]
    assert m.latencies == [0.0]
