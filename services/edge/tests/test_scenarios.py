"""End-to-end replay of synthetic scenarios through the full pipeline.

These check plumbing and gross discrimination on synthetic footage only;
they are regression tests, not evidence of real-world accuracy.
"""

from __future__ import annotations

import pytest

from vigil_edge.config import VigilConfig, config_from_dict
from vigil_edge.evaluation import detections_from_result, evaluate
from vigil_edge.models import IncidentEventType, SignalType
from vigil_edge.pipeline import PipelineResult, run_pipeline
from vigil_edge.sources.synthetic import SyntheticSource, get_scenario


def run(name: str, cfg: VigilConfig | None = None) -> PipelineResult:
    cfg = cfg or VigilConfig()
    return run_pipeline(SyntheticSource(get_scenario(name), fps=cfg.source.target_fps), cfg)


NEGATIVES = ["rest", "walk", "scratch", "shake_off", "lighting", "camera", "person"]


@pytest.mark.parametrize("name", NEGATIVES)
def test_normal_activity_raises_no_incident(name: str) -> None:
    res = run(name)
    assert res.incidents == []
    assert res.shadow_detections == []


def test_convulsion_detected_once_with_bounded_latency() -> None:
    res = run("convulsion")
    assert len(res.incidents) == 1
    inc = res.incidents[0]
    assert inc.signal_type is SignalType.CONVULSION
    m = evaluate(detections_from_result(res), get_scenario("convulsion").labels)
    conv = m[SignalType.CONVULSION]
    assert conv.recall == 1.0 and conv.false_alerts == 0
    # onset_seconds (25 s, ADR-023) plus a little window lag
    assert 24.0 <= conv.latencies[0] <= 28.0
    assert inc.ended_at is not None and 78 <= inc.ended_at <= 84


def test_pause_mid_episode_stays_one_incident() -> None:
    res = run("pause")
    assert len(get_scenario("pause").labels) == 1  # labeled as one episode
    assert len(res.incidents) == 1
    assert not [e for e in res.events if e.event_type is IncidentEventType.UPDATED]


@pytest.mark.slow
def test_cluster_produces_two_linked_incidents() -> None:
    res = run("cluster")
    assert len(res.incidents) == 2
    first, second = res.incidents
    assert second.follows_incident_id == first.id


def test_retching_is_shadow_only_by_default() -> None:
    res = run("retch")
    assert res.incidents == []
    assert len(res.shadow_detections) == 1
    assert res.shadow_detections[0].signal_type is SignalType.VOMITING


def test_retching_opens_warning_when_promoted_to_alert() -> None:
    cfg = config_from_dict({"detectors": {"vomiting": {"mode": "alert"}}})
    res = run("retch", cfg)
    assert [i.signal_type for i in res.incidents] == [SignalType.VOMITING]


@pytest.mark.slow
def test_mixed_day_only_flags_the_convulsion() -> None:
    res = run("mixed")
    m = evaluate(detections_from_result(res), get_scenario("mixed").labels)
    assert m[SignalType.CONVULSION].recall == 1.0
    assert sum(x.false_alerts for x in m.values()) == 0


def test_handheld_needs_stabilization_profile(repo_root) -> None:  # type: ignore[no-untyped-def]
    from vigil_edge.config import load_config

    res = run("handheld", load_config(repo_root / "config" / "handheld.toml"))
    m = evaluate(detections_from_result(res), get_scenario("handheld").labels)
    assert m[SignalType.CONVULSION].recall == 1.0
    assert sum(x.false_alerts for x in m.values()) == 0


def test_stats_are_reported() -> None:
    res = run("rest")
    assert res.stats.frames == 900
    assert res.stats.processing_fps > 0
    assert res.stats.media_seconds == pytest.approx(59.93, abs=0.1)
