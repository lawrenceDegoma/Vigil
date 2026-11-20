from __future__ import annotations

import cv2
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


def test_crop_gives_more_pixels_to_region() -> None:
    cfg = PreprocessConfig(width=320, blur_kernel=0, crop=(0.25, 0.5, 0.5, 0.5))
    p = Preprocessor(cfg, (1280, 720))
    assert p.size == (320, 180)  # 640x360 crop scaled to 320
    img = np.zeros((720, 1280, 3), np.uint8)
    img[360:, 320:960] = 200  # exactly the crop area
    assert (p(img) == 200).all()


def test_roi_is_full_frame_coordinates_with_crop() -> None:
    roi = ((0.5, 0.5), (1.0, 0.5), (1.0, 1.0), (0.5, 1.0))  # bottom-right quarter
    p = Preprocessor(PreprocessConfig(width=200, roi=roi, crop=(0.0, 0.5, 1.0, 0.5)), (400, 200))
    assert p.size == (200, 50)
    assert p.roi_mask[25, 20] == 0  # left half of the crop
    assert p.roi_mask[25, 180] == 255


def test_invalid_crop_rejected() -> None:
    from vigil_edge.config import ConfigError

    with pytest.raises(ConfigError):
        PreprocessConfig(crop=(0.5, 0.5, 0.8, 0.2))


def test_small_downscale_uses_bilinear_large_uses_area() -> None:
    rng = np.random.default_rng(1)
    img = cv2.GaussianBlur(rng.integers(0, 255, (360, 640), dtype=np.uint8), (9, 9), 0)
    crop = (0.14, 0.28, 0.66, 0.57)  # owner deployment: 422x205 crop -> 384 px
    p = Preprocessor(PreprocessConfig(width=384, crop=crop, blur_kernel=0), (640, 360))
    out = p(img)
    region = img[101:306, 90:512]
    assert out.shape == (187, 384)
    linear = cv2.resize(region, (384, 187), interpolation=cv2.INTER_LINEAR)
    area = cv2.resize(region, (384, 187), interpolation=cv2.INTER_AREA)
    assert np.array_equal(out, linear)
    assert np.abs(out.astype(int) - area.astype(int)).mean() < 2.0  # same picture
    big = Preprocessor(PreprocessConfig(width=320, blur_kernel=0), (1280, 720))
    assert np.array_equal(big(cv2.resize(img, (1280, 720))),
                          cv2.resize(cv2.resize(img, (1280, 720)), (320, 180),
                                     interpolation=cv2.INTER_AREA))  # fmt: skip
