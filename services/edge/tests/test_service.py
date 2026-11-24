from __future__ import annotations

import json
from collections.abc import Iterator
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest
from jsonschema import Draft202012Validator

from helpers import T0, clip
from vigil_edge import service as service_mod
from vigil_edge.clock import FakeClock
from vigil_edge.config import VigilConfig, config_from_dict
from vigil_edge.models import Frame
from vigil_edge.service import EdgeService
from vigil_edge.sources.base import FrameSource, SourceError
from vigil_edge.sources.synthetic import SyntheticSource, get_scenario
from vigil_edge.store import LocalStore


def cfg(tmp_path: Path, **recording: Any) -> VigilConfig:
    return config_from_dict(
        {
            "service": {"data_dir": str(tmp_path / "edge"), "device_id": "test-pi"},
            "recording": {"pre_event_seconds": 5.0, "post_event_seconds": 10.0, **recording},
        }
    )


def synth(name: str) -> SyntheticSource:
    return SyntheticSource(get_scenario(name), fps=15, size=(320, 180))


def validator(repo_root: Path, name: str) -> Draft202012Validator:
    schema = json.loads((repo_root / f"packages/contracts/schemas/{name}").read_text())
    return Draft202012Validator(schema, format_checker=Draft202012Validator.FORMAT_CHECKER)


def outbox(svc: EdgeService) -> list[dict[str, Any]]:
    store = LocalStore(svc.paths.db)
    try:
        return [{"kind": m.kind, **m.payload} for m in store.pending(1000)]
    finally:
        store.close()


def test_end_to_end_incident_clip_and_messages(tmp_path: Path, repo_root: Path) -> None:
    svc = EdgeService(cfg(tmp_path), lambda: synth("convulsion"))
    svc.run(housekeeping_thread=False)
    msgs = outbox(svc)
    events = [m for m in msgs if m["kind"] == "incident_event"]
    assert [e["event_type"] for e in events] == ["incident_opened", "incident_ended"]
    ev_validator = validator(repo_root, "incident-event.v1.schema.json")
    for e in events:
        ev_validator.validate({k: v for k, v in e.items() if k != "kind"})
        assert e["device_id"] == "test-pi"
    hb_validator = validator(repo_root, "heartbeat.v1.schema.json")
    heartbeats = [m for m in msgs if m["kind"] == "heartbeat"]
    assert len(heartbeats) >= 2  # startup + shutdown
    for hb in heartbeats:
        hb_validator.validate({k: v for k, v in hb.items() if k != "kind"})
    assert not heartbeats[0]["camera"]["ok"]  # startup: no frames yet → not claimed ok
    final = json.loads(svc.paths.heartbeat_file.read_text())
    hb_validator.validate(final)
    store = LocalStore(svc.paths.db)
    [rec] = store.list_clips()
    assert Path(rec.path).is_file() and rec.kind == "incident"
    assert rec.incident_ids == (events[0]["incident_id"],)


def test_maintenance_mode_exports_nothing(tmp_path: Path) -> None:
    c = cfg(tmp_path)
    flag = Path(c.service.data_dir) / "state" / "maintenance"
    flag.parent.mkdir(parents=True)
    flag.write_text("on")
    svc = EdgeService(c, lambda: synth("convulsion"))
    svc.run(housekeeping_thread=False)
    msgs = outbox(svc)
    assert not [m for m in msgs if m["kind"] == "incident_event"]
    assert all(m["maintenance"] for m in msgs if m["kind"] == "heartbeat")
    assert LocalStore(svc.paths.db).list_clips() == []


def test_camera_unavailable_retries_with_backoff(tmp_path: Path) -> None:
    attempts = {"n": 0}
    slept: list[float] = []

    def factory() -> FrameSource:
        attempts["n"] += 1
        if attempts["n"] <= 3:
            raise SourceError("no cameras available")
        return synth("rest")

    svc = EdgeService(cfg(tmp_path), factory, sleep=slept.append)
    svc.run(housekeeping_thread=False)
    assert slept == [1.0, 2.0, 4.0]
    assert svc.sessions == 4
    assert svc.health.camera_ok()  # recovered: frames flowed after the errors


class FailingLive(FrameSource):
    """Live source that delivers part of a scenario, then the camera 'disconnects'."""

    def __init__(self, inner: FrameSource, fail_at: float) -> None:
        self.inner, self.fail_at = inner, fail_at

    @property
    def fps(self) -> float:
        return self.inner.fps

    @property
    def frame_size(self) -> tuple[int, int]:
        return self.inner.frame_size

    @property
    def is_live(self) -> bool:
        return True

    def frames(self) -> Iterator[Frame]:
        for f in self.inner.frames():
            if f.timestamp >= self.fail_at:
                raise SourceError("camera frontend has timed out")
            yield f


def test_camera_failure_mid_incident_closes_and_reconnects(tmp_path: Path) -> None:
    # Episode 20-80 s; with the 25 s onset (ADR-023) the incident opens at ~45 s.
    sources = iter([FailingLive(synth("convulsion"), fail_at=60.0), synth("rest")])
    slept: list[float] = []
    svc = EdgeService(cfg(tmp_path), lambda: next(sources), sleep=slept.append)
    svc.run(housekeeping_thread=False)
    events = [m for m in outbox(svc) if m["kind"] == "incident_event"]
    assert [e["event_type"] for e in events] == ["incident_opened", "incident_ended"]
    assert events[1]["features"]["end_reason"] == "stream_end"
    assert svc.sessions == 2 and slept == [1.0]
    assert len(LocalStore(svc.paths.db).list_clips()) == 1  # partial clip still saved


def test_startup_housekeeping_applies_retention(tmp_path: Path) -> None:
    c = cfg(tmp_path)
    clips_dir = Path(c.service.data_dir) / "clips"
    clips_dir.mkdir(parents=True)
    old = clips_dir / "old.mp4"
    old.write_bytes(b"x")
    store = LocalStore(Path(c.service.data_dir) / "vigil.db")
    store.add_clip(clip(1, path=str(old), expires_at=T0 - timedelta(days=1)))
    store.close()
    svc = EdgeService(c, lambda: synth("rest"))
    svc.run(housekeeping_thread=False)
    assert not old.exists()


def test_watchdog_fed_only_while_capture_progresses(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(service_mod.systemd, "watchdog_enabled", lambda: True)
    sent: list[str] = []
    clock = FakeClock()
    svc = EdgeService(
        cfg(tmp_path), lambda: synth("rest"), clock=clock, notify=lambda m: sent.append(m) or True
    )
    svc.housekeeping_tick()
    assert "WATCHDOG=1" in sent
    sent.clear()
    clock.advance(60)  # capture loop made no progress for a minute
    svc.housekeeping_tick()
    assert "WATCHDOG=1" not in sent
    svc.store.close()


def test_watchdog_fed_during_intentional_backoff(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(service_mod.systemd, "watchdog_enabled", lambda: True)
    sent: list[str] = []
    clock = FakeClock()
    svc = EdgeService(
        cfg(tmp_path),
        lambda: synth("rest"),
        clock=clock,
        sleep=lambda s: None,
        notify=lambda m: sent.append(m) or True,
    )
    svc._wait(60.0)  # e.g. camera reconnect backoff
    clock.advance(45)
    svc.housekeeping_tick()
    assert "WATCHDOG=1" in sent
    svc.store.close()
