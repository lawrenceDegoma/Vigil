from __future__ import annotations

import numpy as np
import pytest

from vigil_edge.config import PreprocessConfig
from vigil_edge.preprocess import Preprocessor


def test_resizes_to_width_keeping_aspect() -> None:
    p = Preprocessor(PreprocessConfig(width=320), (1280, 720))
    assert p.size == (320, 180)
    gray = p(np.zeros((720, 1280, 3), np.uint8))
    assert gray.shape == (180, 320)
    assert gray.dtype == np.uint8


def test_never_upscales() -> None:
    p = Preprocessor(PreprocessConfig(width=320), (160, 120))
    assert p.size == (160, 120)


def test_roi_mask_covers_polygon_only() -> None:
    roi = ((0.0, 0.5), (1.0, 0.5), (1.0, 1.0), (0.0, 1.0))  # bottom half
    p = Preprocessor(PreprocessConfig(width=200, roi=roi), (200, 100))
    assert p.roi_mask[10, 100] == 0
    assert p.roi_mask[90, 100] == 255
    assert 0.45 < p.roi_pixels / (200 * 100) < 0.56


def test_accepts_greyscale_input() -> None:
    p = Preprocessor(PreprocessConfig(width=64, blur_kernel=0), (64, 48))
    img = np.full((48, 64), 77, np.uint8)
    assert (p(img) == 77).all()


def test_invalid_size() -> None:
    with pytest.raises(ValueError):
        Preprocessor(PreprocessConfig(), (0, 10))
