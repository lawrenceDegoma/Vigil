from __future__ import annotations

import math

import numpy as np
import pytest

from helpers import frame_features
from vigil_edge.config import WindowConfig
from vigil_edge.windowing import (
    WindowAnalyzer,
    principal_axis_projection,
    reversal_rate,
    spectral_concentration,
)

FS = 15.0


def test_pure_tone_is_concentrated_at_its_frequency() -> None:
    t = np.arange(75) / FS
    conc, hz = spectral_concentration(np.sin(2 * math.pi * 3.0 * t), FS, (1.0, 6.0))
    assert conc > 0.85
    assert hz == pytest.approx(3.0, abs=0.25)


def test_noise_is_not_concentrated() -> None:
    rng = np.random.default_rng(1)
    concs = [spectral_concentration(rng.normal(size=75), FS, (1.0, 6.0))[0] for _ in range(50)]
    assert float(np.median(concs)) < 0.3


def test_flat_signal_scores_zero() -> None:
    assert spectral_concentration(np.ones(75), FS, (1.0, 6.0)) == (0.0, 0.0)


def test_out_of_band_tone_scores_low_in_band() -> None:
    t = np.arange(75) / FS
    conc, _ = spectral_concentration(np.sin(2 * math.pi * 0.6 * t), FS, (1.5, 6.0))
    assert conc < 0.15


def test_principal_axis_recovers_diagonal_oscillation() -> None:
    t = np.arange(60) / FS
    s = np.sin(2 * math.pi * 2 * t)
    proj = principal_axis_projection(s * 0.6, s * 0.8)
    assert abs(np.corrcoef(proj, s)[0, 1]) > 0.99


def test_reversal_rate() -> None:
    t = np.arange(150) / FS  # 10 s of 2 Hz → ~4 reversals/s
    assert reversal_rate(np.sin(2 * math.pi * 2 * t), 10.0) == pytest.approx(4.0, abs=0.5)


def _feed(cfg: WindowConfig, gen: object, seconds: float) -> list:
    wa = WindowAnalyzer(cfg)
    out = []
    for i in range(int(seconds * FS)):
        t = i / FS
        w = wa.push(gen(t))  # type: ignore[operator]
        if w is not None:
            out.append(w)
    return out


def test_emits_after_full_window_then_every_step() -> None:
    cfg = WindowConfig(seconds=5.0, step_seconds=0.5)
    windows = _feed(cfg, lambda t: frame_features(t), 12.0)
    assert windows[0].t_end >= 4.5
    gaps = np.diff([w.t_end for w in windows])
    assert np.all(gaps >= 0.5 - 1e-9) and np.all(gaps < 0.6)


def test_shaking_in_place_window() -> None:
    def gen(t: float):  # type: ignore[no-untyped-def]
        v = math.sin(2 * math.pi * 3 * t)
        return frame_features(
            t,
            motion_fraction=0.02,
            slow_motion_fraction=0.02,
            flow_dx=v,
            flow_dy=0.2 * v,
            flow_magnitude=abs(v),
            centroid=(0.5, 0.6),
            region=(0.4, 0.5, 0.2, 0.2),
            region_area=0.04,
        )

    w = _feed(WindowConfig(), gen, 8.0)[-1]
    assert w.active_ratio == 1.0
    assert w.periodicity > 0.8
    assert w.dominant_hz == pytest.approx(3.0, abs=0.3)
    assert w.travel < 0.01
    assert w.region_area == pytest.approx(0.04)


def test_walking_window_has_travel() -> None:
    def gen(t: float):  # type: ignore[no-untyped-def]
        return frame_features(t, motion_fraction=0.01, centroid=(0.1 + 0.05 * t, 0.6))

    w = _feed(WindowConfig(), gen, 8.0)[-1]
    assert w.travel > 0.15


def test_lighting_and_camera_maxima() -> None:
    def gen(t: float):  # type: ignore[no-untyped-def]
        jump = 0.2 if 6.0 <= t < 6.1 else 0.0
        return frame_features(t, brightness_delta=jump, global_motion=0.3 if t > 4 else 0.0)

    w = _feed(WindowConfig(), gen, 8.0)[-1]
    assert w.lighting_change == pytest.approx(0.2)
    assert w.camera_motion == pytest.approx(0.3)
