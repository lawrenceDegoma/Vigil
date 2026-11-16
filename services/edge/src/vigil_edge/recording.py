"""Event-only clip recording with a pre-event buffer (DESIGN §5.3).

The **backend** owns pixels: it keeps a rolling buffer of recent video and
writes a clip file on request. Two implementations:

* ``SoftwareRingBackend`` (here): JPEG-compressed ring buffer of frames,
  written with OpenCV. For video-file, synthetic, and USB-webcam sources.
* ``Picamera2CircularBackend`` (``sources/picamera2_source.py``): the Pi's
  hardware H.264 encoder into Picamera2's ``CircularOutput``. No CPU encoding.

The **ClipManager** owns policy: which state-machine outputs start a clip,
when it stops (``post_event_seconds`` after the last incident ends), splitting
very long episodes, integrity hashing, and indexing in the local store.
Normal operation never writes continuous footage; only event clips persist.

Threading: the capture loop only *starts* and *stops* clips. The slow part of
finalizing (backend post-processing such as an ffmpeg remux, SHA-256, store
insert) runs on one background worker, in order. Doing it inline stalled
analysis for 1.5-2.5 s per clip on the Pi 3 (benchmark 2026-11-16), which
during a long episode would recur at every clip split.
"""

from __future__ import annotations

import hashlib
import logging
import threading
import uuid
from collections import deque
from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Protocol

import cv2
import numpy as np

from vigil_edge.config import RecordingConfig
from vigil_edge.incidents import Output
from vigil_edge.models import Frame, Image, IncidentEvent, IncidentEventType, ShadowDetection
from vigil_edge.store import ClipRecord, LocalStore

log = logging.getLogger(__name__)

SHADOW_POST_SECONDS = 30.0


class ClipBackend(Protocol):
    def push(self, frame: Frame) -> None:
        """Offer a frame (software backends buffer it; hardware ones ignore it)."""

    def start_clip(self, path: Path, from_ts: float) -> None:
        """Begin writing to ``path``, including buffered video from ``from_ts`` on."""

    def stop_clip(self) -> Path | None:
        """Stop writing (fast; capture thread). Returns the raw file, or None if idle."""

    def finish(self, raw: Path) -> Path | None:
        """Post-process a stopped clip (may be slow; runs on a worker thread).

        Must not touch live capture state. Returns the final file, or None on failure.
        """

    def close(self) -> None:
        """Stop and finish any clip in progress (synchronously)."""


class SoftwareRingBackend:
    def __init__(
        self, buffer_seconds: float, fps: float, width: int = 640, jpeg_quality: int = 80
    ) -> None:
        self.buffer_seconds = buffer_seconds
        self.fps = fps
        self.width = width
        self._quality = [int(cv2.IMWRITE_JPEG_QUALITY), jpeg_quality]
        self._ring: deque[tuple[float, bytes]] = deque()
        self._writer: cv2.VideoWriter | None = None
        self._path: Path | None = None
        self._size: tuple[int, int] | None = None

    def _prepare(self, image: Image) -> Image:
        if image.ndim == 2:
            image = cv2.cvtColor(image, cv2.COLOR_GRAY2BGR)
        h, w = image.shape[:2]
        if w > self.width:
            image = cv2.resize(
                image,
                (self.width, round(h * self.width / w) // 2 * 2),
                interpolation=cv2.INTER_AREA,
            )
        if self._size is None:
            self._size = (image.shape[1], image.shape[0])
        elif (image.shape[1], image.shape[0]) != self._size:
            image = cv2.resize(image, self._size, interpolation=cv2.INTER_AREA)
        return image

    def push(self, frame: Frame) -> None:
        image = self._prepare(frame.image)
        if self._writer is not None:
            self._writer.write(image)
        ok, buf = cv2.imencode(".jpg", image, self._quality)
        if ok:
            self._ring.append((frame.timestamp, buf.tobytes()))
        horizon = frame.timestamp - self.buffer_seconds
        while self._ring and self._ring[0][0] < horizon:
            self._ring.popleft()

    @property
    def buffered_seconds(self) -> float:
        return self._ring[-1][0] - self._ring[0][0] if len(self._ring) > 1 else 0.0

    def start_clip(self, path: Path, from_ts: float) -> None:
        if self._writer is not None:
            raise RuntimeError("a clip is already being written")
        path.parent.mkdir(parents=True, exist_ok=True)
        size = self._size or (self.width, self.width * 9 // 16)
        writer = cv2.VideoWriter(str(path), cv2.VideoWriter.fourcc(*"mp4v"), self.fps, size)
        if not writer.isOpened():
            raise RuntimeError(f"could not open clip writer at {path}")
        for ts, jpg in self._ring:
            if ts >= from_ts:
                writer.write(cv2.imdecode(np.frombuffer(jpg, np.uint8), cv2.IMREAD_COLOR))
        self._writer, self._path = writer, path

    def stop_clip(self) -> Path | None:
        if self._writer is None:
            return None
        self._writer.release()
        path, self._writer, self._path = self._path, None, None
        return path

    def finish(self, raw: Path) -> Path | None:
        return raw if raw.is_file() else None

    def close(self) -> None:
        self.stop_clip()
        self._ring.clear()


@dataclass
class _ActiveClip:
    id: str
    path: Path
    kind: str  # incident | shadow
    start_ts: float
    incident_ids: list[str] = field(default_factory=list)
    open_incidents: set[str] = field(default_factory=set)
    stop_at: float | None = None


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


class ClipManager:
    def __init__(
        self,
        backend: ClipBackend,
        store: LocalStore,
        cfg: RecordingConfig,
        clips_dir: Path,
        stream_started_at: datetime,
        now_utc: Callable[[], datetime],
        id_factory: Callable[[], str] = lambda: str(uuid.uuid4()),
        background: bool = True,
    ) -> None:
        self.backend = backend
        self.store = store
        self.cfg = cfg
        self.clips_dir = clips_dir
        self.stream_started_at = stream_started_at
        self._now = now_utc
        self._new_id = id_factory
        self.active: _ActiveClip | None = None
        self.finalized: list[ClipRecord] = []
        self._lock = threading.Lock()
        self._pool = (
            ThreadPoolExecutor(max_workers=1, thread_name_prefix="clip-finalize")
            if background
            else None
        )
        self._pending: list[Future[None]] = []

    # -------------------------------------------------------------- policy

    def handle(self, output: Output, t: float) -> None:
        if not self.cfg.enabled:
            return
        if isinstance(output, IncidentEvent):
            inc = output.incident
            if output.event_type in (IncidentEventType.OPENED, IncidentEventType.UPDATED):
                if inc.ended_at is not None:
                    return
                clip = self.active or self._start(t, "incident", self.cfg.pre_event_seconds)
                clip.kind = "incident"
                clip.open_incidents.add(inc.id)
                if inc.id not in clip.incident_ids:
                    clip.incident_ids.append(inc.id)
                clip.stop_at = None
            elif output.event_type is IncidentEventType.ENDED and self.active is not None:
                self.active.open_incidents.discard(inc.id)
                if not self.active.open_incidents:
                    self.active.stop_at = t + self.cfg.post_event_seconds
        elif isinstance(output, ShadowDetection) and self.cfg.record_shadow:
            if self.active is None:
                clip = self._start(t, "shadow", self.cfg.pre_event_seconds)
                clip.stop_at = t + SHADOW_POST_SECONDS
            elif self.active.stop_at is not None:
                self.active.stop_at = max(self.active.stop_at, t + SHADOW_POST_SECONDS)

    def tick(self, t: float) -> None:
        clip = self.active
        if clip is None:
            return
        if clip.stop_at is not None and t >= clip.stop_at:
            self._finalize(t)
        elif t - clip.start_ts >= self.cfg.max_clip_seconds:
            carry = list(clip.open_incidents)
            self._finalize(t)
            if carry:  # long episode: continue seamlessly in a new clip
                nxt = self._start(t, "incident", 0.0)
                nxt.incident_ids = list(carry)
                nxt.open_incidents = set(carry)

    def close(self, t: float) -> None:
        """Finalize any open clip and wait for all pending finalization."""
        if self.active is not None:
            self._finalize(t)
        self.drain()
        if self._pool is not None:
            self._pool.shutdown(wait=True)
            self._pool = None

    def drain(self) -> None:
        """Block until every stopped clip has been finished and indexed."""
        while True:
            with self._lock:
                pending, self._pending = self._pending, []
            if not pending:
                return
            for fut in pending:
                fut.result()

    @property
    def pending_finalizations(self) -> int:
        with self._lock:
            return sum(not f.done() for f in self._pending)

    # ----------------------------------------------------------- mechanics

    def _start(self, t: float, kind: str, pre: float) -> _ActiveClip:
        clip_id = self._new_id()
        start_ts = max(0.0, t - pre)
        wall = self.stream_started_at + timedelta(seconds=start_ts)
        name = f"{wall:%Y%m%dT%H%M%SZ}-{kind}-{clip_id[:8]}.mp4"
        path = self.clips_dir / f"{wall:%Y-%m-%d}" / name
        self.backend.start_clip(path, start_ts)
        self.active = _ActiveClip(clip_id, path, kind, start_ts)
        log.info("clip %s started (%s, pre-event from %.1fs)", clip_id, kind, start_ts)
        return self.active

    def _finalize(self, t: float) -> None:
        clip = self.active
        assert clip is not None
        self.active = None
        raw = self.backend.stop_clip()  # fast: the encoder/writer stops on this thread
        if raw is None:
            log.error("clip %s produced no file", clip.id)
            return
        if self._pool is None:
            self._index(clip, raw, t)
            return
        fut = self._pool.submit(self._index, clip, raw, t)
        with self._lock:
            self._pending = [f for f in self._pending if not f.done()] + [fut]

    def _index(self, clip: _ActiveClip, raw: Path, t: float) -> None:
        try:
            path = self.backend.finish(raw)
            if path is None or not path.is_file():
                log.error("clip %s produced no file", clip.id)
                return
            created = self._now()
            record = ClipRecord(
                id=clip.id,
                path=str(path),
                incident_ids=tuple(clip.incident_ids),
                kind=clip.kind,
                started_at=self.stream_started_at + timedelta(seconds=clip.start_ts),
                ended_at=self.stream_started_at + timedelta(seconds=t),
                bytes=path.stat().st_size,
                sha256=sha256_file(path),
                created_at=created,
                expires_at=created + timedelta(days=self.cfg.retention_days),
            )
            self.store.add_clip(record)
            with self._lock:
                self.finalized.append(record)
            log.info("clip %s saved: %s (%d bytes)", clip.id, path.name, record.bytes)
        except Exception:  # a failed clip must never take down capture
            log.exception("clip %s finalization failed", clip.id)
