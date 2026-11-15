"""Development live sources: USB/built-in webcam, real-time pacing, and looping.

These let the full service run on a Mac: a webcam behaves like the Pi
camera, and a recorded file can be replayed at real speed (optionally on
loop) for soak tests.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Iterator

import cv2

from vigil_edge.models import Frame
from vigil_edge.sources.base import FrameSource, SourceError


class OpenCVCameraSource(FrameSource):
    def __init__(
        self,
        device: int,
        fps: float,
        size: tuple[int, int] | None = None,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self._cap = cv2.VideoCapture(device)
        if not self._cap.isOpened():
            raise SourceError(f"could not open camera device {device}")
        if size:
            self._cap.set(cv2.CAP_PROP_FRAME_WIDTH, size[0])
            self._cap.set(cv2.CAP_PROP_FRAME_HEIGHT, size[1])
        ok, frame = self._cap.read()
        if not ok or frame is None:
            raise SourceError(f"camera device {device} returned no frames")
        self._first = frame
        self._size = (frame.shape[1], frame.shape[0])
        self._fps = fps
        self._mono = monotonic
        self.dropped_frames = 0

    @property
    def fps(self) -> float:
        return self._fps

    @property
    def frame_size(self) -> tuple[int, int]:
        return self._size

    @property
    def is_live(self) -> bool:
        return True

    def frames(self) -> Iterator[Frame]:
        start = self._mono()
        period = 1.0 / self._fps
        next_emit = 0.0
        index = 0
        image = self._first
        while True:
            ts = self._mono() - start
            if ts + 1e-3 >= next_emit:
                yield Frame(index, ts, image)
                index += 1
                next_emit += period * max(1, int((ts - next_emit) / period) + 1)
            ok, image = self._cap.read()
            if not ok or image is None:
                raise SourceError("camera stopped delivering frames")

    def close(self) -> None:
        self._cap.release()


class PacedSource(FrameSource):
    """Delivers an inner source's frames no faster than their timestamps (real time)."""

    def __init__(
        self,
        inner: FrameSource,
        speed: float = 1.0,
        monotonic: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.inner = inner
        self.speed = speed
        self._mono = monotonic
        self._sleep = sleep

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
        start: float | None = None
        first_ts = 0.0
        for frame in self.inner.frames():
            if start is None:
                start, first_ts = self._mono(), frame.timestamp
            due = start + (frame.timestamp - first_ts) / self.speed
            delay = due - self._mono()
            if delay > 0:
                self._sleep(delay)
            yield frame

    def close(self) -> None:
        self.inner.close()


class LoopingSource(FrameSource):
    """Replays sources from a factory forever with continuous timestamps (soak tests)."""

    def __init__(self, factory: Callable[[], FrameSource], max_loops: int | None = None) -> None:
        self._factory = factory
        self._max = max_loops
        probe = factory()
        self._fps, self._size = probe.fps, probe.frame_size
        probe.close()

    @property
    def fps(self) -> float:
        return self._fps

    @property
    def frame_size(self) -> tuple[int, int]:
        return self._size

    @property
    def is_live(self) -> bool:
        return True

    def frames(self) -> Iterator[Frame]:
        offset = 0.0
        index = 0
        loops = 0
        while self._max is None or loops < self._max:
            src = self._factory()
            last = 0.0
            try:
                for f in src.frames():
                    last = f.timestamp
                    yield Frame(index, offset + f.timestamp, f.image)
                    index += 1
            finally:
                src.close()
            offset += last + 1.0 / self._fps
            loops += 1
