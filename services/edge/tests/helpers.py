"""Shared builders for tests."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import Any

import numpy as np

from vigil_edge.models import (
    DetectionResult,
    DetectorMode,
    FrameFeatures,
    SignalType,
    WindowFeatures,
)
from vigil_edge.store import ClipRecord

QUIET_WINDOW = WindowFeatures(
    t_start=0.0,
    t_end=5.0,
    n_frames=75,
    sample_rate=15.0,
    activity=0.0,
    active_ratio=0.0,
    slow_activity=0.0,
    slow_active_ratio=0.0,
    flow_magnitude=0.0,
    periodicity=0.0,
    dominant_hz=0.0,
    low_periodicity=0.0,
    low_dominant_hz=0.0,
    reversal_rate=0.0,
    travel=0.0,
    region_area=0.0,
    camera_motion=0.0,
    lighting_change=0.0,
)

CONVULSING_WINDOW = replace(
    QUIET_WINDOW,
    activity=0.02,
    active_ratio=1.0,
    slow_activity=0.02,
    slow_active_ratio=1.0,
    flow_magnitude=0.25,
    periodicity=0.7,
    dominant_hz=3.0,
    travel=0.01,
    region_area=0.08,
)

RETCHING_WINDOW = replace(
    QUIET_WINDOW,
    activity=0.0002,
    active_ratio=0.1,
    slow_activity=0.004,
    slow_active_ratio=0.5,
    flow_magnitude=0.02,
    low_periodicity=0.45,
    low_dominant_hz=0.8,
    travel=0.01,
    region_area=0.05,
)


def window(**kw: Any) -> WindowFeatures:
    return replace(QUIET_WINDOW, **kw)


def frame_features(t: float, **kw: Any) -> FrameFeatures:
    base: dict[str, Any] = dict(
        index=int(t * 15),
        timestamp=t,
        motion_fraction=0.0,
        slow_motion_fraction=0.0,
        flow_dx=0.0,
        flow_dy=0.0,
        flow_magnitude=0.0,
        global_motion=0.0,
        brightness=0.5,
        brightness_delta=0.0,
        centroid=None,
        region=None,
        region_area=0.0,
    )
    base.update(kw)
    return FrameFeatures(**base)


def result(
    detector: str = "convulsion",
    active: bool = True,
    score: float | None = None,
    mode: DetectorMode = DetectorMode.ALERT,
) -> DetectionResult:
    signal = SignalType.CONVULSION if detector == "convulsion" else SignalType.VOMITING
    return DetectionResult(
        detector,
        signal,
        mode,
        score if score is not None else (0.9 if active else 0.0),
        active,
        coverage=0.1,
    )


def textured(h: int = 120, w: int = 160, seed: int = 0) -> np.ndarray:
    import cv2

    rng = np.random.default_rng(seed)
    img = rng.integers(40, 200, (h // 4, w // 4)).astype(np.uint8)
    out: np.ndarray = cv2.resize(img, (w, h), interpolation=cv2.INTER_CUBIC)
    return out


T0 = datetime(2026, 11, 15, 12, 0, tzinfo=UTC)


def clip(i: int, **kw: object) -> ClipRecord:
    base: dict[str, object] = dict(
        id=f"c{i}",
        path=f"/x/c{i}.mp4",
        incident_ids=(f"i{i}",),
        kind="incident",
        started_at=T0,
        ended_at=T0 + timedelta(seconds=90),
        bytes=1000 * i,
        sha256="ab",
        created_at=T0 + timedelta(minutes=i),
        expires_at=T0 + timedelta(days=30),
    )
    base.update(kw)
    return ClipRecord(**base)  # type: ignore[arg-type]
