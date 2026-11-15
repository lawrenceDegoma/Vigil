"""Component health tracking and heartbeat construction (DESIGN §8).

The monitor is fed by the capture loop (frames, windows, errors) and read by
the housekeeping thread. Health is component-based: a reachable device with a
stalled camera or detector reports ``ok: false`` for that component, so the
cloud never shows "online" without fresh proof of capture and analysis.
"""

from __future__ import annotations

import shutil
import threading
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from vigil_edge import __version__, system
from vigil_edge.clock import Clock
from vigil_edge.store import utc_iso

HEARTBEAT_SCHEMA = "heartbeat.v1"


@dataclass
class _Component:
    last_mono: float | None = None
    last_utc: datetime | None = None
    count: int = 0
    error: str | None = None


class HealthMonitor:
    def __init__(
        self,
        clock: Clock,
        camera_stall_seconds: float,
        detector_stall_seconds: float,
        rule_version: str,
    ) -> None:
        self.clock = clock
        self.camera_stall = camera_stall_seconds
        self.detector_stall = detector_stall_seconds
        self.rule_version = rule_version
        self._lock = threading.Lock()
        self._camera = _Component()
        self._detector = _Component()
        self._fps_window: list[float] = []
        self._proc_frames = 0
        self._proc_seconds = 0.0
        self.dropped_frames = 0
        self.maintenance = False
        self.storage_warnings: list[str] = []
        self._cpu = system.CpuMeter()

    # ------------------------------------------------------------- feeding

    def frame(self, processing_seconds: float, dropped: int = 0) -> None:
        now = self.clock.monotonic()
        with self._lock:
            c = self._camera
            c.last_mono, c.last_utc, c.error = now, self.clock.now_utc(), None
            c.count += 1
            self._fps_window.append(now)
            cutoff = now - 10.0
            while self._fps_window and self._fps_window[0] < cutoff:
                self._fps_window.pop(0)
            self._proc_frames += 1
            self._proc_seconds += processing_seconds
            self.dropped_frames += dropped

    def window(self) -> None:
        with self._lock:
            d = self._detector
            d.last_mono, d.last_utc, d.error = self.clock.monotonic(), self.clock.now_utc(), None
            d.count += 1

    def camera_error(self, message: str) -> None:
        with self._lock:
            self._camera.error = message[:200]

    def detector_error(self, message: str) -> None:
        with self._lock:
            self._detector.error = message[:200]

    # ------------------------------------------------------------- reading

    def _fresh(self, c: _Component, limit: float, now: float) -> bool:
        return c.error is None and c.last_mono is not None and now - c.last_mono <= limit

    def camera_ok(self) -> bool:
        with self._lock:
            return self._fresh(self._camera, self.camera_stall, self.clock.monotonic())

    def detector_ok(self) -> bool:
        with self._lock:
            return self._fresh(self._detector, self.detector_stall, self.clock.monotonic())

    def healthy(self) -> bool:
        """Both capture and analysis are demonstrably running (watchdog gate)."""
        return self.camera_ok() and self.detector_ok()

    def heartbeat(
        self, device_id: str, data_dir: Path, clips_bytes: int, queue_depth: int
    ) -> dict[str, Any]:
        now = self.clock.monotonic()
        usage = shutil.disk_usage(data_dir)
        with self._lock:
            cam, det = self._camera, self._detector
            span = (
                (self._fps_window[-1] - self._fps_window[0]) if len(self._fps_window) > 1 else 0.0
            )
            fps = (len(self._fps_window) - 1) / span if span > 0 else 0.0
            proc_fps = self._proc_frames / self._proc_seconds if self._proc_seconds else 0.0
            self._proc_frames, self._proc_seconds = 0, 0.0
            msg: dict[str, Any] = {
                "schema_version": HEARTBEAT_SCHEMA,
                "device_id": device_id,
                "sent_at": utc_iso(self.clock.now_utc()),
                "software_version": __version__,
                "maintenance": self.maintenance,
                "camera": {
                    "ok": self._fresh(cam, self.camera_stall, now),
                    "last_frame_at": utc_iso(cam.last_utc) if cam.last_utc else None,
                    "fps": round(fps, 2),
                    "dropped_frames": self.dropped_frames,
                    "error": cam.error,
                },
                "detector": {
                    "ok": self._fresh(det, self.detector_stall, now),
                    "last_window_at": utc_iso(det.last_utc) if det.last_utc else None,
                    "processing_fps": round(proc_fps, 1),
                    "rule_version": self.rule_version,
                    "error": det.error,
                },
                "storage": {
                    "free_bytes": int(usage.free),
                    "total_bytes": int(usage.total),
                    "recordings_bytes": int(clips_bytes),
                },
                "queue_depth": int(queue_depth),
                "temperature_c": system.cpu_temperature_c(),
                "cpu_percent": self._cpu.percent(),
                "memory_used_percent": system.memory_used_percent(),
            }
        return msg
