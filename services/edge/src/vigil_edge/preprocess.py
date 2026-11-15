"""Frame preprocessing: downscale, greyscale, blur, region-of-interest mask."""

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
        width = min(cfg.width, src_w)  # never upscale
        height = max(2, round(src_h * width / src_w))
        self.size: tuple[int, int] = (width, height)
        self._blur = cfg.blur_kernel if cfg.blur_kernel > 1 else 0
        self.roi_mask: Image = self._build_mask(cfg.roi, self.size)
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
