from __future__ import annotations

import threading
import time
from datetime import UTC, datetime
from pathlib import Path

import cv2
import numpy as np
import pytest

from vigil_edge.config import RecordingConfig
from vigil_edge.models import (
    Frame,
    Incident,
    IncidentEvent,
    IncidentEventType,
    Severity,
    ShadowDetection,
    SignalType,
)
from vigil_edge.recording import ClipManager, SoftwareRingBackend, sha256_file
from vigil_edge.store import ClipRecord, LocalStore

FPS = 10.0
START = datetime(2026, 11, 15, 20, 0, tzinfo=UTC)


def frame(i: int) -> Frame:
    img = np.full((90, 160, 3), (i * 3) % 255, np.uint8)
    return Frame(i, i / FPS, img)


def count_frames(path: Path) -> int:
    cap = cv2.VideoCapture(str(path))
    n = 0
    while cap.read()[0]:
        n += 1
    cap.release()
    return n


def test_ring_backend_includes_pre_event_frames(tmp_path: Path) -> None:
    be = SoftwareRingBackend(buffer_seconds=2.0, fps=FPS, width=160)
    for i in range(50):  # 5 s; ring keeps the last ~2 s
        be.push(frame(i))
    assert 1.8 <= be.buffered_seconds <= 2.0
    be.start_clip(tmp_path / "c.mp4", from_ts=3.5)  # 1.4 s of lead-in available
    for i in range(50, 60):
        be.push(frame(i))
    path = be.stop_clip()
    assert path is not None
    assert count_frames(path) == 15 + 10
    assert be.stop_clip() is None


def ev(
    kind: IncidentEventType, t: float, inc_id: str = "i1", ended: float | None = None
) -> IncidentEvent:
    inc = Incident(inc_id, SignalType.CONVULSION, Severity.URGENT, t - 10, t, t, ended)
    return IncidentEvent(f"e-{kind.value}-{t}", kind, t, inc)


class Rig:
    def __init__(self, tmp_path: Path, **cfg: object) -> None:
        self.backend = SoftwareRingBackend(30.0, FPS, 160)
        self.store = LocalStore(tmp_path / "v.db")
        n = iter(range(100))
        self.mgr = ClipManager(
            self.backend,
            self.store,
            RecordingConfig(**cfg),  # type: ignore[arg-type]
            tmp_path / "clips",
            START,
            lambda: START,
            id_factory=lambda: f"clip{next(n)}-xxxxxxxx",
        )
        self.t = 0

    def clips(self) -> list[ClipRecord]:
        self.mgr.drain()  # finalization runs on a worker thread
        return self.store.list_clips()

    def advance_to(self, seconds: float) -> None:
        while self.t / FPS < seconds:
            self.backend.push(frame(self.t))
            self.mgr.tick(self.t / FPS)
            self.t += 1


def test_incident_clip_lifecycle(tmp_path: Path) -> None:
    rig = Rig(tmp_path, pre_event_seconds=5.0, post_event_seconds=10.0)
    rig.advance_to(20)
    rig.mgr.handle(ev(IncidentEventType.OPENED, 20), 20)
    assert rig.mgr.active is not None and rig.mgr.active.start_ts == 15.0
    rig.advance_to(40)
    rig.mgr.handle(ev(IncidentEventType.ENDED, 40, ended=32), 40)
    rig.advance_to(49.9)
    assert rig.mgr.active is not None  # still in post-event tail
    rig.advance_to(51)
    assert rig.mgr.active is None
    [rec] = rig.clips()
    assert rec.incident_ids == ("i1",)
    assert (rec.ended_at - rec.started_at).total_seconds() == 35.0
    assert rec.sha256 == sha256_file(Path(rec.path))
    assert rec.bytes > 0
    assert count_frames(Path(rec.path)) >= 340


def test_resumed_incident_cancels_stop(tmp_path: Path) -> None:
    rig = Rig(tmp_path, pre_event_seconds=0.0, post_event_seconds=10.0)
    rig.advance_to(1)
    rig.mgr.handle(ev(IncidentEventType.OPENED, 1), 1)
    rig.advance_to(5)
    rig.mgr.handle(ev(IncidentEventType.ENDED, 5, ended=4), 5)
    rig.advance_to(8)
    rig.mgr.handle(ev(IncidentEventType.UPDATED, 8), 8)  # resumed
    rig.advance_to(30)
    assert rig.mgr.active is not None and rig.mgr.active.stop_at is None


def test_overlapping_incidents_share_one_clip(tmp_path: Path) -> None:
    rig = Rig(tmp_path, pre_event_seconds=0.0, post_event_seconds=5.0)
    rig.advance_to(1)
    rig.mgr.handle(ev(IncidentEventType.OPENED, 1, "a"), 1)
    rig.mgr.handle(ev(IncidentEventType.OPENED, 2, "b"), 2)
    rig.mgr.handle(ev(IncidentEventType.ENDED, 4, "a", ended=3), 4)
    rig.advance_to(12)
    assert rig.mgr.active is not None  # "b" still open
    rig.mgr.handle(ev(IncidentEventType.ENDED, 12, "b", ended=11), 12)
    rig.advance_to(18)
    [rec] = rig.clips()
    assert rec.incident_ids == ("a", "b")


def test_long_episode_is_split(tmp_path: Path) -> None:
    rig = Rig(tmp_path, pre_event_seconds=0.0, max_clip_seconds=30.0)
    rig.advance_to(1)
    rig.mgr.handle(ev(IncidentEventType.OPENED, 1), 1)
    rig.advance_to(70)
    assert len(rig.clips()) == 2
    assert rig.mgr.active is not None and rig.mgr.active.open_incidents == {"i1"}


def test_shadow_detection_gets_short_clip(tmp_path: Path) -> None:
    rig = Rig(tmp_path, pre_event_seconds=5.0)
    rig.advance_to(10)
    rig.mgr.handle(ShadowDetection(10, "vomiting", SignalType.VOMITING, 6, 0.6), 10)
    rig.advance_to(41)
    [rec] = rig.clips()
    assert rec.kind == "shadow"
    assert (rec.ended_at - rec.started_at).total_seconds() == 35.0


def test_disabled_recording_does_nothing(tmp_path: Path) -> None:
    rig = Rig(tmp_path, enabled=False)
    rig.mgr.handle(ev(IncidentEventType.OPENED, 1), 1)
    assert rig.mgr.active is None


def test_close_finalizes_open_clip(tmp_path: Path) -> None:
    rig = Rig(tmp_path, pre_event_seconds=0.0)
    rig.advance_to(1)
    rig.mgr.handle(ev(IncidentEventType.OPENED, 1), 1)
    rig.advance_to(5)
    rig.mgr.close(5)
    assert len(rig.clips()) == 1


class GatedBackend(SoftwareRingBackend):
    """finish() blocks until released, like a slow ffmpeg remux on a Pi."""

    def __init__(self, fail: bool = False) -> None:
        super().__init__(30.0, FPS, 160)
        self.release = threading.Event()
        self.fail = fail

    def finish(self, raw: Path) -> Path | None:
        assert self.release.wait(5)
        if self.fail:
            raise RuntimeError("remux exploded")
        return super().finish(raw)


def test_slow_finish_does_not_block_capture(tmp_path: Path) -> None:
    rig = Rig(tmp_path, pre_event_seconds=0.0, post_event_seconds=1.0)
    gated = GatedBackend()
    rig.backend = rig.mgr.backend = gated
    rig.advance_to(1)
    rig.mgr.handle(ev(IncidentEventType.OPENED, 1), 1)
    rig.advance_to(3)
    rig.mgr.handle(ev(IncidentEventType.ENDED, 3, ended=2), 3)
    t0 = time.perf_counter()
    rig.advance_to(6)  # passes stop_at: the clip stops while finish() is still blocked
    assert time.perf_counter() - t0 < 1.0
    assert rig.mgr.active is None
    assert rig.mgr.pending_finalizations == 1
    assert rig.store.list_clips() == []
    gated.release.set()
    [rec] = rig.clips()
    assert rec.sha256 == sha256_file(Path(rec.path))
    assert rig.mgr.pending_finalizations == 0


def test_failed_finish_is_logged_not_raised(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    rig = Rig(tmp_path, pre_event_seconds=0.0)
    gated = GatedBackend(fail=True)
    gated.release.set()
    rig.backend = rig.mgr.backend = gated
    rig.advance_to(1)
    rig.mgr.handle(ev(IncidentEventType.OPENED, 1), 1)
    rig.advance_to(2)
    rig.mgr.close(2)
    assert rig.store.list_clips() == []
    assert "finalization failed" in caplog.text


def test_close_waits_for_pending_finalization(tmp_path: Path) -> None:
    rig = Rig(tmp_path, pre_event_seconds=0.0)
    gated = GatedBackend()
    rig.backend = rig.mgr.backend = gated
    rig.advance_to(1)
    rig.mgr.handle(ev(IncidentEventType.OPENED, 1), 1)
    rig.advance_to(2)
    threading.Timer(0.2, gated.release.set).start()
    rig.mgr.close(2)
    assert len(rig.store.list_clips()) == 1
