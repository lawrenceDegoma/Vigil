from __future__ import annotations

import json
from pathlib import Path

import pytest

from vigil_edge.cli import _parse_set, build_config, main


def test_parse_set_types() -> None:
    assert _parse_set("detectors.convulsion.threshold=0.6") == {
        "detectors": {"convulsion": {"threshold": 0.6}}
    }
    assert _parse_set("motion.stabilize=true") == {"motion": {"stabilize": True}}
    assert _parse_set("subject.name=Biscuit") == {"subject": {"name": "Biscuit"}}
    assert _parse_set("preprocess.roi=[[0,0],[1,0],[1,1]]")["preprocess"]["roi"] == [
        [0, 0],
        [1, 0],
        [1, 1],
    ]


def test_config_layering(repo_root: Path) -> None:
    cfg = build_config(
        [str(repo_root / "config/handheld.toml")], ["detectors.convulsion.threshold=0.7"]
    )
    assert cfg.motion.stabilize is True
    assert cfg.detectors.convulsion.threshold == 0.7
    assert cfg.detectors.convulsion.max_camera_motion == 1.5


def test_synth_then_analyze_file(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    video = tmp_path / "conv.mp4"
    assert main(["synth", "convulsion", "-o", str(video), "--fps", "15", "--width", "320"]) == 0
    assert (tmp_path / "conv.labels.json").is_file()
    out = tmp_path / "run"
    code = main(
        [
            "analyze",
            str(video),
            "-o",
            str(out),
            "-q",
            "--overlay",
            "--recorded-at",
            "2026-11-14T20:00:00+00:00",
        ]
    )
    assert code == 0
    text = capsys.readouterr().out
    assert "convulsion detected 1/1" in text
    assert "not diagnoses" in text
    summary = json.loads((out / "summary.json").read_text())
    assert len(summary["incidents"]) == 1
    assert summary["evaluation"][0]["recall"] == 1.0
    for name in ("events.jsonl", "windows.csv", "overlay.mp4", "incident_messages.jsonl"):
        assert (out / name).stat().st_size > 0


def test_analyze_synthetic_with_plot(tmp_path: Path) -> None:
    pytest.importorskip("matplotlib")
    out = tmp_path / "run"
    assert main(["analyze", "synthetic:retch", "-o", str(out), "-q", "--plot"]) == 0
    assert (out / "timeline.png").stat().st_size > 10_000


def test_evaluate_synthetic_subset(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    report = tmp_path / "report.json"
    assert main(["evaluate", "--synthetic", "convulsion", "walk", "--json-out", str(report)]) == 0
    data = json.loads(report.read_text())
    conv = next(t for t in data["totals"] if t["signal_type"] == "convulsion")
    assert conv["labeled_events"] == 1 and conv["detected_events"] == 1
    assert "not a real-world accuracy estimate" in capsys.readouterr().out


def test_errors_exit_2(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["analyze", str(tmp_path / "missing.mp4"), "--no-output"]) == 2
    assert main(["analyze", "synthetic:nope", "--no-output"]) == 2
    assert main(["config", "--set", "detectors.convulsion.treshold=1"]) == 2
    assert "error" in capsys.readouterr().err


def test_config_prints_rule_version(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["config"]) == 0
    assert json.loads(capsys.readouterr().out)["rule_version"].startswith("motion-rules-")
