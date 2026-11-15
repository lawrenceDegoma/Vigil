"""Frame preprocessing: optional crop, downscale, greyscale, blur, region-of-interest mask."""

from __future__ import annotations

import cv2
import numpy as np

from vigil_edge.config import PreprocessConfig
from vigil_edge.models import Image


class Preprocessor:
    """Converts source frames into small blurred greyscale analysis frames.

    The analysis size is fixed at construction from the source frame size so
    every downstream stage sees consistent dimensions.
    """

    def __init__(self, cfg: PreprocessConfig, frame_size: tuple[int, int]) -> None:
        src_w, src_h = frame_size
        if src_w <= 0 or src_h <= 0:
            raise ValueError(f"invalid frame size {frame_size}")
        self._crop: tuple[int, int, int, int] | None = None
        roi = cfg.roi
        if cfg.crop:
            cx, cy, cw, ch = cfg.crop
            x0, y0 = round(cx * src_w), round(cy * src_h)
            x1, y1 = min(src_w, round((cx + cw) * src_w)), min(src_h, round((cy + ch) * src_h))
            self._crop = (x0, y0, x1, y1)
            src_w, src_h = x1 - x0, y1 - y0
            # ROI is specified in full-frame coordinates; express it inside the crop.
            roi = tuple(((x - cx) / cw, (y - cy) / ch) for x, y in roi)
            roi = tuple((min(1.0, max(0.0, x)), min(1.0, max(0.0, y))) for x, y in roi)
        width = min(cfg.width, src_w)  # never upscale
        height = max(2, round(src_h * width / src_w))
        self.size: tuple[int, int] = (width, height)
        self._blur = cfg.blur_kernel if cfg.blur_kernel > 1 else 0
        self.roi_mask: Image = self._build_mask(roi, self.size)
        self.roi_pixels = int(np.count_nonzero(self.roi_mask))
        if self.roi_pixels == 0:
            raise ValueError("region of interest covers no pixels")

    @staticmethod
    def _build_mask(roi: tuple[tuple[float, float], ...], size: tuple[int, int]) -> Image:
        w, h = size
        if not roi:
            return np.full((h, w), 255, dtype=np.uint8)
        mask = np.zeros((h, w), dtype=np.uint8)
        pts = np.array([[round(x * (w - 1)), round(y * (h - 1))] for x, y in roi], dtype=np.int32)
        cv2.fillPoly(mask, [pts], 255)
        return mask

    def resize(self, image: Image) -> Image:
        if self._crop is not None:
            x0, y0, x1, y1 = self._crop
            image = image[y0:y1, x0:x1]
        if (image.shape[1], image.shape[0]) == self.size:
            return image
        out: Image = cv2.resize(image, self.size, interpolation=cv2.INTER_AREA)
        return out

    def __call__(self, image: Image) -> Image:
        small = self.resize(image)
        gray: Image = cv2.cvtColor(small, cv2.COLOR_BGR2GRAY) if small.ndim == 3 else small
        if self._blur:
            gray = cv2.GaussianBlur(gray, (self._blur, self._blur), 0)
        return gray
