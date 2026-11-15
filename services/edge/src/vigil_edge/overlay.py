"""Debug overlay video: what the pipeline sees, frame by frame.

Shows the analysis-resolution frame with the motion mask tinted, the dominant
moving region, current state, and detector score bars. Written locally only;
overlay videos contain footage and must be treated as private.
"""

from __future__ import annotations

from pathlib import Path

import cv2
import numpy as np

from vigil_edge.models import Image, MonitorState
from vigil_edge.pipeline import FrameContext, NullObserver

_STATE_COLOURS = {
    MonitorState.MONITORING: (140, 140, 140),
    MonitorState.EARLY_WARNING: (0, 190, 255),
    MonitorState.URGENT: (40, 40, 230),
    MonitorState.RECOVERY_OBSERVATION: (230, 160, 60),
}


class OverlayVideoWriter(NullObserver):
    def __init__(self, path: Path, fps: float, width: int = 640) -> None:
        self.path = path
        self._fps = fps
        self._width = width
        self._writer: cv2.VideoWriter | None = None
        self._size: tuple[int, int] | None = None

    def _ensure(self, h: int, w: int) -> None:
        if self._writer is not None:
            return
        out_h = round(h * self._width / w)
        self._size = (self._width, out_h + 70)
        fourcc = cv2.VideoWriter.fourcc(*"mp4v")
        self._writer = cv2.VideoWriter(str(self.path), fourcc, self._fps, self._size)
        if not self._writer.isOpened():
            raise RuntimeError(f"could not open overlay writer at {self.path}")

    def on_frame(self, ctx: FrameContext) -> None:
        gray = ctx.gray
        h, w = gray.shape
        self._ensure(h, w)
        assert self._writer is not None and self._size is not None
        img: Image = cv2.cvtColor(gray, cv2.COLOR_GRAY2BGR)
        if ctx.debug is not None:
            tint = np.zeros_like(img)
            tint[ctx.debug.mask > 0] = (0, 0, 255)
            img = cv2.addWeighted(img, 1.0, tint, 0.6, 0)
        scale = self._width / w
        img = cv2.resize(img, (self._width, round(h * scale)), interpolation=cv2.INTER_NEAREST)
        f = ctx.features
        if f is not None and f.region is not None:
            x, y, rw, rh = f.region
            ih, iw = img.shape[:2]
            cv2.rectangle(
                img,
                (int(x * iw), int(y * ih)),
                (int((x + rw) * iw), int((y + rh) * ih)),
                (0, 255, 0),
                1,
            )
        colour = _STATE_COLOURS[ctx.state]
        if ctx.state is not MonitorState.MONITORING:
            cv2.rectangle(img, (0, 0), (img.shape[1] - 1, img.shape[0] - 1), colour, 4)

        panel = np.full((70, self._width, 3), 18, np.uint8)
        t = ctx.frame.timestamp
        m, s = divmod(t, 60)
        cv2.putText(
            panel,
            f"{int(m)}:{s:04.1f}  {ctx.state.value}",
            (8, 20),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.5,
            colour,
            1,
            cv2.LINE_AA,
        )
        if f is not None:
            cv2.putText(
                panel,
                f"motion {f.motion_fraction:.3f}  flow {f.flow_magnitude:.2f}  "
                f"cam {f.global_motion:.2f}",
                (8, 40),
                cv2.FONT_HERSHEY_SIMPLEX,
                0.4,
                (200, 200, 200),
                1,
                cv2.LINE_AA,
            )
        if ctx.last_window is not None:
            x0 = self._width - 250
            for i, r in enumerate(ctx.last_window.results):
                y = 14 + i * 22
                bar = int(150 * r.score)
                c = (60, 60, 230) if r.active else (120, 120, 120)
                if r.suppressed_by:
                    c = (90, 90, 90)
                cv2.rectangle(panel, (x0 + 90, y - 9), (x0 + 90 + bar, y + 3), c, -1)
                cv2.rectangle(panel, (x0 + 90, y - 9), (x0 + 240, y + 3), (70, 70, 70), 1)
                label = r.detector + ("*" if r.mode.value == "shadow" else "")
                cv2.putText(
                    panel,
                    label,
                    (x0, y + 2),
                    cv2.FONT_HERSHEY_SIMPLEX,
                    0.4,
                    (200, 200, 200),
                    1,
                    cv2.LINE_AA,
                )
            fw = ctx.last_window.features
            cv2.putText(
                panel,
                f"rhythm {fw.periodicity:.2f}@{fw.dominant_hz:.1f}Hz  travel {fw.travel:.2f}",
                (8, 60),
                cv2.FONT_HERSHEY_SIMPLEX,
                0.4,
                (200, 200, 200),
                1,
                cv2.LINE_AA,
            )
        self._writer.write(np.vstack([img, panel]))

    def close(self) -> None:
        if self._writer is not None:
            self._writer.release()
            self._writer = None
