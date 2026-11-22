"""Per-frame motion features.

Pipeline per frame (all on small greyscale images):

1. Estimate global (camera) motion with sparse Lucas-Kanade tracking and a
   RANSAC similarity transform. For fixed cameras this is reported so camera
   bumps can be suppressed; with ``stabilize`` the previous frame is warped
   onto the current one first (handheld footage).
2. Gain-compensated frame differencing → thresholded motion mask inside the
   ROI. Gain compensation removes most auto-exposure / gradual lighting change.
3. Dominant moving region via connected components on a dilated mask. This
   is "where the activity is", not object recognition (ADR-006).
4. A second, *lagged* difference against a frame ``slow_lag_seconds`` back
   picks up slow movements (heaving, slow rolling) that barely change between
   consecutive frames. Not camera-compensated; with ``stabilize`` it falls
   back to the consecutive-frame value.
5. Optional dense Farneback optical flow at reduced resolution, averaged over
   moving pixels, giving a signed direction signal for oscillation analysis.

All outputs are resolution-independent: fractions of the frame, or frame
widths per second.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass

import cv2
import numpy as np
import numpy.typing as npt

from vigil_edge.config import MotionConfig
from vigil_edge.models import FrameFeatures, Image

FloatArray = npt.NDArray[np.float32]


@dataclass(frozen=True, slots=True)
class MotionDebug:
    """Intermediate products exposed for overlays and tests (not persisted)."""

    mask: Image  # motion mask at analysis resolution (0/255)
    transform: FloatArray | None  # 2x3 prev→cur camera transform at analysis resolution


class MotionFeatureExtractor:
    def __init__(self, cfg: MotionConfig, roi_mask: Image, nominal_fps: float) -> None:
        self.cfg = cfg
        self.roi_mask = roi_mask
        h, w = roi_mask.shape
        self.size = (w, h)
        self._frame_area = float(w * h)
        self._roi_bool = roi_mask > 0
        self._nominal_dt = 1.0 / max(nominal_fps, 1e-3)
        fw = min(cfg.flow_width, w)
        self.flow_size = (fw, max(2, round(h * fw / w)))
        self._flow_scale = fw / w
        k = max(3, (w // 40) | 1)
        self._dilate_kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (k, k))
        self._open_kernel = np.ones((3, 3), np.uint8)
        self._prev: Image | None = None
        self._prev_small: Image | None = None
        self._prev_ts = 0.0
        self._prev_brightness = 0.0
        self._history: deque[tuple[float, Image, float]] = deque()
        self.last_debug: MotionDebug | None = None

    def reset(self) -> None:
        self._prev = None
        self._prev_small = None
        self._history.clear()
        self.last_debug = None

    def _diff_mask(
        self, cur: Image, ref: Image, cur_b: float, ref_b: float, valid: npt.NDArray[np.bool_]
    ) -> Image:
        """Gain-compensated thresholded difference, cleaned with a 3x3 opening."""
        if ref_b > 0.02:
            ref = cv2.convertScaleAbs(ref, alpha=cur_b / ref_b)
        diff = cv2.absdiff(cur, ref)
        mask = np.where((diff > self.cfg.diff_threshold) & valid, 255, 0).astype(np.uint8)
        out: Image = cv2.morphologyEx(mask, cv2.MORPH_OPEN, self._open_kernel)
        return out

    def _slow_reference(self, timestamp: float) -> tuple[Image, float] | None:
        lag = self.cfg.slow_lag_seconds
        ref: tuple[Image, float] | None = None
        while self._history and self._history[0][0] <= timestamp - lag:
            _ts, img, b = self._history.popleft()
            ref = (img, b)
        if ref is not None:  # keep the reference for the next frame too
            self._history.appendleft((timestamp - lag, ref[0], ref[1]))
        return ref

    # ------------------------------------------------------------------ helpers

    def _estimate_transform(self, prev_small: Image, cur_small: Image) -> FloatArray | None:
        """Similarity transform prev→cur at flow resolution, or None if unreliable."""
        pts = cv2.goodFeaturesToTrack(
            prev_small, maxCorners=120, qualityLevel=0.01, minDistance=5, blockSize=5
        )
        if pts is None or len(pts) < 12:
            return None
        nxt, status, _err = cv2.calcOpticalFlowPyrLK(
            prev_small, cur_small, pts, None, winSize=(15, 15), maxLevel=2
        )
        if nxt is None or status is None:
            return None
        good = status.reshape(-1) == 1
        if int(good.sum()) < 10:
            return None
        matrix, inliers = cv2.estimateAffinePartial2D(
            pts[good], nxt[good], method=cv2.RANSAC, ransacReprojThreshold=1.0
        )
        if matrix is None or inliers is None or int(inliers.sum()) < 8:
            return None
        result: FloatArray = matrix.astype(np.float32)
        return result

    def _transform_displacement(self, matrix: FloatArray, size: tuple[int, int]) -> float:
        """Mean displacement (px) of a grid of points under ``matrix``."""
        w, h = size
        xs, ys = np.meshgrid(np.linspace(0, w - 1, 3), np.linspace(0, h - 1, 3))
        pts = np.stack([xs.ravel(), ys.ravel(), np.ones(9)], axis=1)
        moved = pts @ matrix.T.astype(np.float64)
        return float(np.mean(np.linalg.norm(moved - pts[:, :2], axis=1)))

    # ------------------------------------------------------------------ main

    def process(self, gray: Image, timestamp: float, index: int) -> FrameFeatures | None:
        """Return features for this frame, or ``None`` for the first frame."""
        cur_small: Image = (
            cv2.resize(gray, self.flow_size, interpolation=cv2.INTER_AREA)
            if self.flow_size != self.size
            else gray
        )
        brightness = float(cv2.mean(gray, mask=self.roi_mask)[0]) / 255.0

        prev, prev_small = self._prev, self._prev_small
        dt = timestamp - self._prev_ts
        if dt <= 0 or dt > 10 * self._nominal_dt:
            dt = self._nominal_dt
        prev_brightness = self._prev_brightness
        self._prev, self._prev_small = gray, cur_small
        self._prev_ts, self._prev_brightness = timestamp, brightness
        slow_ref = self._slow_reference(timestamp) if self.cfg.slow_lag_seconds > 0 else None
        if self.cfg.slow_lag_seconds > 0:
            self._history.append((timestamp, gray, brightness))
        if prev is None or prev_small is None:
            return None

        # 1. Camera motion.
        small_tf = self._estimate_transform(prev_small, cur_small)
        global_motion = 0.0
        full_tf: FloatArray | None = None
        if small_tf is not None:
            disp = self._transform_displacement(small_tf, self.flow_size)
            global_motion = disp / self.flow_size[0] / dt
            full_tf = small_tf.copy()
            full_tf[:, 2] /= self._flow_scale

        valid = self._roi_bool
        if self.cfg.stabilize and full_tf is not None:
            w, h = self.size
            prev = cv2.warpAffine(
                prev, full_tf, (w, h), flags=cv2.INTER_LINEAR, borderMode=cv2.BORDER_REPLICATE
            )
            prev_small = cv2.warpAffine(
                prev_small,
                small_tf,
                self.flow_size,
                flags=cv2.INTER_LINEAR,
                borderMode=cv2.BORDER_REPLICATE,
            )
            border = cv2.warpAffine(
                np.full((h, w), 255, np.uint8),
                full_tf,
                (w, h),
                flags=cv2.INTER_NEAREST,
                borderValue=0,
            )
            valid = valid & (cv2.erode(border, self._open_kernel) > 0)

        # 2. Gain-compensated differencing (consecutive and lagged).
        mask = self._diff_mask(gray, prev, brightness, prev_brightness, valid)
        valid_px = max(1, int(np.count_nonzero(valid)))
        motion_fraction = float(np.count_nonzero(mask)) / valid_px
        slow_fraction = motion_fraction
        if slow_ref is not None and not self.cfg.stabilize:
            slow_mask = self._diff_mask(gray, slow_ref[0], brightness, slow_ref[1], valid)
            slow_fraction = max(motion_fraction, float(np.count_nonzero(slow_mask)) / valid_px)

        # 3. Dominant moving region.
        centroid: tuple[float, float] | None = None
        region: tuple[float, float, float, float] | None = None
        region_area = 0.0
        if motion_fraction > 0:
            grown = cv2.dilate(mask, self._dilate_kernel, iterations=2)
            n, _labels, stats, cents = cv2.connectedComponentsWithStats(grown, connectivity=8)
            if n > 1:
                best = 1 + int(np.argmax(stats[1:, cv2.CC_STAT_AREA]))
                x, y, bw, bh, _area = (int(v) for v in stats[best])
                area = bw * bh / self._frame_area
                if area >= self.cfg.min_region_area:
                    w, h = self.size
                    region = (x / w, y / h, bw / w, bh / h)
                    region_area = area
                    centroid = (float(cents[best][0]) / w, float(cents[best][1]) / h)

        # 5. Dense optical flow over moving pixels.
        flow_dx = flow_dy = flow_mag = 0.0
        if self.cfg.flow and motion_fraction > 0:
            flow = cv2.calcOpticalFlowFarneback(
                prev_small,
                cur_small,
                None,
                pyr_scale=0.5,
                levels=2,
                winsize=9,
                iterations=2,
                poly_n=5,
                poly_sigma=1.1,
                flags=0,
            )
            small_mask = cv2.resize(mask, self.flow_size, interpolation=cv2.INTER_NEAREST) > 0
            if small_mask.any():
                vecs = flow[small_mask]
                scale = 1.0 / (self.flow_size[0] * dt)
                flow_dx = float(vecs[:, 0].mean()) * scale
                flow_dy = float(vecs[:, 1].mean()) * scale
                flow_mag = float(np.linalg.norm(vecs, axis=1).mean()) * scale

        # 6. Coarse per-cell motion (where in the frame things move), for the spatial
        #    extent of rhythmic motion: one leg vs several limbs (DETECTION §5.6).
        gx, gy = self.cfg.grid
        if motion_fraction > 0:
            # Nearest-neighbour resize to an exact multiple, then block means: much
            # cheaper on a Pi than INTER_AREA at a non-integer ratio.
            k = 4
            small = cv2.resize(mask, (gx * k, gy * k), interpolation=cv2.INTER_NEAREST)
            cells = small.reshape(gy, k, gx, k).mean(axis=(1, 3), dtype=np.float32) / 255.0
        else:
            cells = np.zeros((gy, gx), np.float32)

        self.last_debug = MotionDebug(mask=mask, transform=full_tf)
        return FrameFeatures(
            index=index,
            timestamp=timestamp,
            motion_fraction=motion_fraction,
            slow_motion_fraction=slow_fraction,
            flow_dx=flow_dx,
            flow_dy=flow_dy,
            flow_magnitude=flow_mag,
            global_motion=global_motion,
            brightness=brightness,
            brightness_delta=abs(brightness - prev_brightness),
            centroid=centroid,
            region=region,
            region_area=region_area,
            cells=cells,
            aspect=self.size[1] / self.size[0],
        )
