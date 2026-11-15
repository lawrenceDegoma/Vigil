from __future__ import annotations

import json
from pathlib import Path

from jsonschema import Draft202012Validator

from vigil_edge.clock import FakeClock
from vigil_edge.health import HealthMonitor


def monitor() -> tuple[HealthMonitor, FakeClock]:
    clock = FakeClock()
    return HealthMonitor(
        clock, camera_stall_seconds=5, detector_stall_seconds=10, rule_version="r1"
    ), clock


def test_unknown_until_first_frame() -> None:
    m, _ = monitor()
    assert not m.camera_ok() and not m.detector_ok() and not m.healthy()


def test_fresh_frames_and_windows_are_healthy() -> None:
    m, clock = monitor()
    m.frame(0.01)
    m.window()
    assert m.healthy()
    clock.advance(6)
    assert not m.camera_ok()  # stalled camera
    assert m.detector_ok()
    clock.advance(5)
    assert not m.detector_ok()


def test_errors_mark_component_unhealthy_until_next_frame() -> None:
    m, _ = monitor()
    m.frame(0.01)
    m.camera_error("disconnected")
    assert not m.camera_ok()
    m.frame(0.01)
    assert m.camera_ok()


def test_heartbeat_matches_schema(repo_root: Path, tmp_path: Path) -> None:
    m, clock = monitor()
    for _ in range(30):
        m.frame(0.02, dropped=0)
        clock.advance(1 / 15)
    m.frame(0.02, dropped=2)
    m.window()
    m.maintenance = True
    hb = m.heartbeat("pi-1", tmp_path, clips_bytes=123, queue_depth=4)
    schema = json.loads(
        (repo_root / "packages/contracts/schemas/heartbeat.v1.schema.json").read_text()
    )
    Draft202012Validator(schema, format_checker=Draft202012Validator.FORMAT_CHECKER).validate(hb)
    assert hb["camera"]["ok"] and hb["detector"]["ok"] and hb["maintenance"]
    assert 14.0 < hb["camera"]["fps"] < 16.0
    assert hb["camera"]["dropped_frames"] == 2
    assert hb["detector"]["processing_fps"] == 50.0
    assert hb["storage"]["recordings_bytes"] == 123
    assert "image" not in json.dumps(hb)
