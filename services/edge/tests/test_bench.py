from __future__ import annotations

from vigil_edge.bench import bench_offline
from vigil_edge.config import VigilConfig


def test_offline_bench_reports_per_variant() -> None:
    report = bench_offline(
        VigilConfig(),
        seconds=6.0,
        variants=[
            {"name": "default"},
            {"name": "160 no flow", "preprocess": {"width": 160}, "motion": {"flow": False}},
        ],
    )
    assert report["kind"] == "offline"
    assert report["input"]["analysis_size"] == [320, 180]
    assert [r["variant"] for r in report["results"]] == ["default", "160 no flow"]
    for r in report["results"]:
        assert r["frames"] == 90
        assert r["ms_per_frame"]["p50"] > 0
        assert set(r["stage_ms_per_frame"]) >= {"preprocess", "motion", "window"}
