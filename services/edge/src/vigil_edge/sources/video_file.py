"""Video file source (OpenCV) with deterministic frame-rate subsampling."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import cv2

from vigil_edge.models import Frame
from vigil_edge.sources.base import FrameSource, SourceError

_FALLBACK_FPS = 30.0


class VideoFileSource(FrameSource):
    """Reads a video file, emitting frames at no more than ``target_fps``.

    Timestamps are derived from the frame index and the container frame rate
    (``index / native_fps``), so they equal positions in the file and replay is
    fully deterministic. ``start``/``end`` restrict analysis to a segment.
    """

    def __init__(
        self,
        path: Path | str,
        target_fps: float | None = None,
        start: float = 0.0,
        end: float | None = None,
    ) -> None:
        self.path = Path(path)
        if not self.path.is_file():
            raise SourceError(f"video file not found: {self.path}")
        self._cap = cv2.VideoCapture(str(self.path))
        if not self._cap.isOpened():
            raise SourceError(f"could not open video: {self.path}")
        native = float(self._cap.get(cv2.CAP_PROP_FPS) or 0.0)
        self.native_fps = native if 0.0 < native <= 240.0 else _FALLBACK_FPS
        self.frame_count = int(self._cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
        self._size = (
            int(self._cap.get(cv2.CAP_PROP_FRAME_WIDTH)),
            int(self._cap.get(cv2.CAP_PROP_FRAME_HEIGHT)),
        )
        if self._size[0] <= 0 or self._size[1] <= 0:
            raise SourceError(f"video has no readable frames: {self.path}")
        if start < 0 or (end is not None and end <= start):
            raise SourceError("invalid start/end range")
        self._target = min(target_fps or self.native_fps, self.native_fps)
        self._start = start
        self._end = end

    @property
    def fps(self) -> float:
        return self._target

    @property
    def frame_size(self) -> tuple[int, int]:
        return self._size

    @property
    def duration(self) -> float:
        return self.frame_count / self.native_fps if self.frame_count else 0.0

    def frames(self) -> Iterator[Frame]:
        first = round(self._start * self.native_fps)
        if first:
            self._cap.set(cv2.CAP_PROP_POS_FRAMES, first)
        period = 1.0 / self._target
        next_emit = self._start
        src_index = first
        out_index = 0
        while True:
            ok, image = self._cap.read()
            if not ok or image is None:
                break
            ts = src_index / self.native_fps
            src_index += 1
            if self._end is not None and ts > self._end:
                break
            if ts + 1e-6 < next_emit:
                continue
            # Advance by whole periods so emitted timestamps stay on a fixed grid.
            next_emit += period * max(1, int((ts - next_emit) / period) + 1)
            yield Frame(out_index, ts, image)
            out_index += 1

    def close(self) -> None:
        self._cap.release()
