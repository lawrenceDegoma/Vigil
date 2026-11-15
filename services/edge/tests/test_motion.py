from __future__ import annotations

import cv2
import numpy as np

from helpers import textured
from vigil_edge.config import MotionConfig
from vigil_edge.motion import MotionFeatureExtractor

H, W = 120, 160
FPS = 15.0


def extractor(**kw: object) -> MotionFeatureExtractor:
    return MotionFeatureExtractor(MotionConfig(**kw), np.full((H, W), 255, np.uint8), FPS)  # type: ignore[arg-type]


def shift(img: np.ndarray, dx: float, dy: float) -> np.ndarray:
    m = np.array([[1, 0, dx], [0, 1, dy]], np.float32)
    out: np.ndarray = cv2.warpAffine(img, m, (W, H), borderMode=cv2.BORDER_REFLECT)
    return out


def with_square(bg: np.ndarray, x: int, y: int, size: int = 20) -> np.ndarray:
    img = bg.copy()
    img[y : y + size, x : x + size] = 250
    img[y + 5 : y + 10, x + 5 : x + 15] = 10  # texture inside the square
    return img


def test_first_frame_returns_none() -> None:
    assert extractor().process(textured(), 0.0, 0) is None


def test_static_scene_has_no_motion() -> None:
    ex = extractor()
    bg = textured()
    ex.process(bg, 0.0, 0)
    f = ex.process(bg.copy(), 1 / FPS, 1)
    assert f is not None
    assert f.motion_fraction == 0.0
    assert f.centroid is None
    assert f.global_motion < 0.01


def test_moving_object_is_localized() -> None:
    ex = extractor()
    bg = textured()
    ex.process(with_square(bg, 60, 50), 0.0, 0)
    f = ex.process(with_square(bg, 66, 50), 1 / FPS, 1)
    assert f is not None
    assert f.motion_fraction > 0.005
    assert f.centroid is not None
    cx, cy = f.centroid
    assert 0.35 < cx < 0.6 and 0.35 < cy < 0.65
    assert f.flow_magnitude > 0
    assert f.flow_dx > 0  # moved right


def test_camera_motion_is_estimated() -> None:
    ex = extractor()
    bg = textured()
    ex.process(bg, 0.0, 0)
    f = ex.process(shift(bg, 4, 0), 1 / FPS, 1)
    assert f is not None
    # 4 px at 160 px width in 1/15 s = 0.375 frame-widths/s
    assert 0.25 < f.global_motion < 0.5


def test_stabilization_removes_camera_motion() -> None:
    bg = textured()
    plain, stab = extractor(), extractor(stabilize=True)
    for ex in (plain, stab):
        ex.process(bg, 0.0, 0)
    moved = shift(bg, 3, 2)
    f_plain = plain.process(moved, 1 / FPS, 1)
    f_stab = stab.process(moved, 1 / FPS, 1)
    assert f_plain is not None and f_stab is not None
    assert f_plain.motion_fraction > 0.05
    assert f_stab.motion_fraction < f_plain.motion_fraction * 0.2


def test_gain_compensation_ignores_global_brightness_change() -> None:
    ex = extractor()
    bg = textured()
    ex.process(bg, 0.0, 0)
    dimmer = cv2.convertScaleAbs(bg, alpha=0.8)
    f = ex.process(dimmer, 1 / FPS, 1)
    assert f is not None
    assert f.brightness_delta > 0.05
    assert f.motion_fraction < 0.02


def test_slow_channel_sees_slow_motion() -> None:
    ex = extractor(slow_lag_seconds=0.25)
    bg = textured()
    f = None
    for i in range(10):  # 1 px per frame: too little for consecutive differencing
        f = ex.process(with_square(bg, 60 + i, 50), i / FPS, i)
    assert f is not None
    assert f.slow_motion_fraction > f.motion_fraction


def test_roi_excludes_motion_outside() -> None:
    roi = np.zeros((H, W), np.uint8)
    roi[:, : W // 2] = 255  # left half only
    ex = MotionFeatureExtractor(MotionConfig(), roi, FPS)
    bg = textured()
    ex.process(with_square(bg, 110, 50), 0.0, 0)
    f = ex.process(with_square(bg, 118, 50), 1 / FPS, 1)
    assert f is not None
    assert f.motion_fraction == 0.0
