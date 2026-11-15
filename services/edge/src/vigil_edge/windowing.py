"""Sliding-window temporal analysis of frame features.

The key measurements for convulsion-like motion are:

* **persistence** – what fraction of frames in the window show motion;
* **rhythm** – spectral concentration of the oscillation signal in a band;
* **in-place vs. travelling** – net displacement of the moving region.

Rhythm is measured on two signals and the stronger one is used:

1. the per-frame mean optical flow projected on its principal axis (captures
   back-and-forth shaking at its fundamental frequency), and
2. the detrended motion-fraction signal (captures pulsing activity even when
   opposing limb movements cancel in the mean flow). The slow band uses the
   lagged ("slow") motion signal instead.

Spectral concentration = power within one main-lobe of the strongest in-band
peak divided by total power above a near-DC cutoff. A pure sinusoid scores
close to 1, broadband noise scores low.
"""

from __future__ import annotations

from collections import deque

import numpy as np
import numpy.typing as npt

from vigil_edge.config import WindowConfig
from vigil_edge.models import FrameFeatures, WindowFeatures

FloatArray = npt.NDArray[np.float64]

_DC_CUTOFF_HZ = 0.2


def spectral_concentration(
    signal: FloatArray, sample_rate: float, band: tuple[float, float]
) -> tuple[float, float]:
    """Return (concentration 0..1, peak frequency Hz) of ``signal`` within ``band``."""
    n = signal.size
    if n < 8 or sample_rate <= 0:
        return 0.0, 0.0
    t = np.arange(n, dtype=np.float64)
    # Linear detrend removes drift (e.g. slow posture change) before the FFT.
    slope, intercept = np.polyfit(t, signal, 1)
    x = signal - (slope * t + intercept)
    if float(np.std(x)) < 1e-9:
        return 0.0, 0.0
    x = x * np.hanning(n)
    n_fft = 1 << int(np.ceil(np.log2(n * 4)))
    power = np.abs(np.fft.rfft(x, n_fft)) ** 2
    freqs = np.fft.rfftfreq(n_fft, d=1.0 / sample_rate)
    total = float(power[freqs >= _DC_CUTOFF_HZ].sum())
    in_band = (freqs >= band[0]) & (freqs <= band[1])
    if total <= 0 or not in_band.any():
        return 0.0, 0.0
    band_idx = np.flatnonzero(in_band)
    peak = int(band_idx[np.argmax(power[band_idx])])
    peak_hz = float(freqs[peak])
    half_lobe = 2.0 * sample_rate / n  # Hann main-lobe half-width in Hz
    lobe = np.abs(freqs - peak_hz) <= half_lobe
    lobe &= freqs >= _DC_CUTOFF_HZ
    return float(power[lobe].sum()) / total, peak_hz


def principal_axis_projection(dx: FloatArray, dy: FloatArray) -> FloatArray:
    """Project 2-D flow vectors onto their direction of greatest variance."""
    vecs = np.stack([dx, dy], axis=1)
    centred = vecs - vecs.mean(axis=0)
    cov = centred.T @ centred
    if float(np.trace(cov)) < 1e-12:
        return np.zeros(dx.size)
    _vals, eigvecs = np.linalg.eigh(cov)
    projected: FloatArray = centred @ eigvecs[:, -1]
    return projected


def reversal_rate(signal: FloatArray, duration: float) -> float:
    """Direction reversals per second, with a deadband against noise."""
    if signal.size < 3 or duration <= 0:
        return 0.0
    x = signal - signal.mean()
    deadband = 0.15 * float(np.std(x))
    signs = np.sign(np.where(np.abs(x) < deadband, 0.0, x))
    signs = signs[signs != 0]
    if signs.size < 2:
        return 0.0
    return float(np.count_nonzero(np.diff(signs))) / duration


class WindowAnalyzer:
    """Accumulates frame features and emits a ``WindowFeatures`` every step."""

    def __init__(self, cfg: WindowConfig) -> None:
        self.cfg = cfg
        self._buf: deque[FrameFeatures] = deque()
        self._last_emit: float | None = None

    def reset(self) -> None:
        self._buf.clear()
        self._last_emit = None

    def push(self, f: FrameFeatures) -> WindowFeatures | None:
        self._buf.append(f)
        horizon = f.timestamp - self.cfg.seconds
        while self._buf and self._buf[0].timestamp < horizon:
            self._buf.popleft()
        span = f.timestamp - self._buf[0].timestamp
        if span < self.cfg.seconds * 0.9:
            return None
        if self._last_emit is not None and f.timestamp - self._last_emit < self.cfg.step_seconds:
            return None
        self._last_emit = f.timestamp
        return self.compute(list(self._buf))

    def compute(self, frames: list[FrameFeatures]) -> WindowFeatures:
        cfg = self.cfg
        n = len(frames)
        t0, t1 = frames[0].timestamp, frames[-1].timestamp
        duration = max(t1 - t0, 1e-6)
        fs = (n - 1) / duration if n > 1 else 0.0

        motion = np.array([f.motion_fraction for f in frames])
        slow = np.array([f.slow_motion_fraction for f in frames])
        dx = np.array([f.flow_dx for f in frames])
        dy = np.array([f.flow_dy for f in frames])
        flow_mag = np.array([f.flow_magnitude for f in frames])

        axis_signal = principal_axis_projection(dx, dy)
        conc_flow, hz_flow = spectral_concentration(axis_signal, fs, cfg.band_hz)
        conc_motion, hz_motion = spectral_concentration(motion, fs, cfg.band_hz)
        if conc_flow >= conc_motion:
            periodicity, dominant = conc_flow, hz_flow
        else:
            periodicity, dominant = conc_motion, hz_motion

        low_flow, low_hz_flow = spectral_concentration(axis_signal, fs, cfg.low_band_hz)
        low_motion, low_hz_motion = spectral_concentration(slow, fs, cfg.low_band_hz)
        if low_flow >= low_motion:
            low_periodicity, low_dominant = low_flow, low_hz_flow
        else:
            low_periodicity, low_dominant = low_motion, low_hz_motion

        centroids = np.array([f.centroid for f in frames if f.centroid is not None])
        travel = 0.0
        if len(centroids) >= 6:
            third = len(centroids) // 3
            start = centroids[:third].mean(axis=0)
            end = centroids[-third:].mean(axis=0)
            travel = float(np.linalg.norm(end - start))

        areas = [f.region_area for f in frames if f.region is not None]
        global_motion = np.array([f.global_motion for f in frames])

        return WindowFeatures(
            t_start=t0,
            t_end=t1,
            n_frames=n,
            sample_rate=fs,
            activity=float(motion.mean()),
            active_ratio=float(np.mean(motion > cfg.activity_floor)),
            slow_activity=float(slow.mean()),
            slow_active_ratio=float(np.mean(slow > cfg.activity_floor)),
            flow_magnitude=float(flow_mag.mean()),
            periodicity=periodicity,
            dominant_hz=dominant,
            low_periodicity=low_periodicity,
            low_dominant_hz=low_dominant,
            reversal_rate=reversal_rate(axis_signal, duration),
            travel=travel,
            region_area=float(np.mean(areas)) if areas else 0.0,
            camera_motion=float(np.percentile(global_motion, 90)),
            lighting_change=float(max(f.brightness_delta for f in frames)),
        )
