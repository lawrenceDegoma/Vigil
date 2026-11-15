"""Performance benchmarks (``vigil bench``), primarily for the Raspberry Pi 3 B+.

* **Offline** (any machine): pre-renders synthetic frames at the camera's
  lores size and greyscale (exactly what Picamera2 delivers), then times the
  analysis pipeline per stage across settings variants. Rendering and decode
  are excluded, so this isolates analysis cost.
* **Camera** (Pi only): runs the real camera with the hardware encoder and
  circular buffer active, analyses live frames, writes one test clip
  mid-run, and samples CPU, memory, temperature, and throttling.

Reports are JSON and say exactly what was measured on which host; never
compare numbers across hosts without saying so.
"""

from __future__ import annotations

import contextlib
import logging
import platform
import statistics
import time
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import cv2

from vigil_edge import __version__, system
from vigil_edge.config import VigilConfig
from vigil_edge.models import Frame, Image
from vigil_edge.pipeline import STAGES, Pipeline
from vigil_edge.sources.synthetic import SyntheticSource, get_scenario

log = logging.getLogger(__name__)

DEFAULT_VARIANTS: list[dict[str, Any]] = [
    {"name": "default (320px, flow on)"},
    {"name": "320px, flow off", "motion": {"flow": False}},
    {"name": "240px, flow on", "preprocess": {"width": 240}, "motion": {"flow_width": 120}},
    {"name": "160px, flow on", "preprocess": {"width": 160}, "motion": {"flow_width": 80}},
    {"name": "160px, flow off", "preprocess": {"width": 160}, "motion": {"flow": False}},
]


def _cpu_model() -> str | None:
    try:
        for line in Path("/proc/cpuinfo").read_text().splitlines():
            key, _, value = line.partition(":")
            if key.strip() in ("model name", "Model"):
                return value.strip()
    except OSError:
        pass
    if platform.system() == "Darwin":
        import subprocess

        out = subprocess.run(
            ["sysctl", "-n", "machdep.cpu.brand_string"],
            capture_output=True,
            text=True,
            check=False,
        ).stdout.strip()
        return out or None
    return None


def host_info() -> dict[str, Any]:
    model = None
    with contextlib.suppress(OSError):
        model = Path("/proc/device-tree/model").read_text().strip("\x00\n ")
    return {
        "cpu": _cpu_model(),
        "machine": platform.machine(),
        "system": platform.system(),
        "release": platform.release(),
        "python": platform.python_version(),
        "processor": platform.processor() or None,
        "model": model,
        "opencv": cv2.__version__,
        "cv_threads": cv2.getNumThreads(),
    }


def _variant(cfg: VigilConfig, v: dict[str, Any]) -> VigilConfig:
    sections = {}
    for section in ("preprocess", "motion"):
        if section in v:
            sections[section] = replace(getattr(cfg, section), **v[section])
    return replace(cfg, **sections)


def _summ(values: list[float]) -> dict[str, float]:
    if not values:
        return {}
    ordered = sorted(values)
    return {
        "mean": round(statistics.fmean(values), 3),
        "p50": round(ordered[len(ordered) // 2], 3),
        "p95": round(ordered[min(len(ordered) - 1, int(len(ordered) * 0.95))], 3),
        "max": round(ordered[-1], 3),
    }


def prerender(
    seconds: float, fps: float, size: tuple[int, int], scenario: str = "mixed"
) -> list[Frame]:
    """Synthetic frames as greyscale at ``size`` (mimics the Picamera2 lores Y plane)."""
    src = SyntheticSource(get_scenario(scenario), fps=fps, size=(640, 360))
    out: list[Frame] = []
    for f in src.frames():
        if f.timestamp >= seconds:
            break
        gray: Image = cv2.cvtColor(
            cv2.resize(f.image, size, interpolation=cv2.INTER_AREA), cv2.COLOR_BGR2GRAY
        )
        out.append(Frame(f.index, f.timestamp, gray))
    return out


def bench_offline(
    cfg: VigilConfig, seconds: float = 60.0, variants: list[dict[str, Any]] | None = None
) -> dict[str, Any]:
    fps = cfg.camera.fps
    size = cfg.camera.analysis_size
    frames = prerender(seconds, fps, size)
    results = []
    for v in variants or DEFAULT_VARIANTS:
        vcfg = _variant(cfg, v)
        warm = Pipeline(vcfg, size, fps, keep_history=False)
        for f in frames[:30]:  # warm caches / OpenCV allocations before timing
            warm.process(f)
        pipe = Pipeline(vcfg, size, fps, keep_history=False)
        per_frame: list[float] = []
        cpu = system.CpuMeter()
        t_start = time.perf_counter()
        for f in frames:
            t0 = time.perf_counter()
            pipe.process(f)
            per_frame.append((time.perf_counter() - t0) * 1000)
        wall = time.perf_counter() - t_start
        st = pipe.stats
        budget_ms = 1000.0 / fps
        results.append(
            {
                "variant": v["name"],
                "frames": st.frames,
                "ms_per_frame": _summ(per_frame),
                "stage_ms_per_frame": {
                    k: round(1000 * st.stage_seconds[k] / max(st.frames, 1), 3)
                    for k in STAGES
                    if k != "observers"
                },
                "max_analysis_fps": round(st.frames / wall, 1),
                "cpu_percent_at_target_fps": round(
                    100 * statistics.fmean(per_frame) / budget_ms, 1
                ),
                "process_cpu_percent_during_run": cpu.percent(),
            }
        )
    return {
        "kind": "offline",
        "measured_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "vigil_edge_version": __version__,
        "host": host_info(),
        "input": {
            "frames": len(frames),
            "analysis_size": list(size),
            "fps": fps,
            "note": "synthetic greyscale frames, pre-rendered; decode/render excluded",
        },
        "peak_rss_mb": round(system.peak_rss_mb(), 1),
        "results": results,
    }


def bench_camera(
    cfg: VigilConfig,
    seconds: float = 120.0,
    clip_dir: Path | None = None,
) -> dict[str, Any]:  # pragma: no cover - requires Raspberry Pi hardware
    from vigil_edge.sources.picamera2_source import Picamera2Source

    src = Picamera2Source(cfg.camera, cfg.recording.pre_event_seconds, record=True)
    backend = src.clip_backend()
    pipe = Pipeline(cfg, src.frame_size, src.fps, keep_history=False)
    cpu = system.CpuMeter()
    per_frame: list[float] = []
    samples: list[dict[str, Any]] = []
    clip_path = (clip_dir or Path("data/bench")) / "bench-clip.mp4"
    clip_state = "pending"
    clip_file: Path | None = None
    start = time.monotonic()
    next_sample = start + 5
    try:
        for f in src.frames():
            t0 = time.perf_counter()
            pipe.process(f)
            per_frame.append((time.perf_counter() - t0) * 1000)
            now = time.monotonic()
            if clip_state == "pending" and now - start > seconds / 3:
                backend.start_clip(clip_path, 0.0)
                clip_state = "recording"
            elif clip_state == "recording" and now - start > seconds / 3 + 10:
                clip_file = backend.stop_clip()
                clip_state = "done"
            if now >= next_sample:
                next_sample = now + 5
                samples.append(
                    {
                        "t": round(now - start, 1),
                        "cpu_percent": cpu.percent(),
                        "temperature_c": system.cpu_temperature_c(),
                        "memory_used_percent": system.memory_used_percent(),
                    }
                )
            if now - start >= seconds:
                break
    finally:
        if clip_state == "recording":
            clip_file = backend.stop_clip()
        src.close()
    elapsed = time.monotonic() - start
    temps = [s["temperature_c"] for s in samples if s["temperature_c"] is not None]
    return {
        "kind": "camera",
        "measured_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "vigil_edge_version": __version__,
        "host": host_info(),
        "camera": {
            "main_size": list(cfg.camera.main_size),
            "analysis_size": list(cfg.camera.analysis_size),
            "fps_requested": cfg.camera.fps,
            "bitrate": cfg.camera.bitrate,
        },
        "seconds": round(elapsed, 1),
        "frames_analysed": pipe.stats.frames,
        "delivered_fps": round(pipe.stats.frames / elapsed, 2),
        "dropped_frames": src.dropped_frames,
        "ms_per_frame": _summ(per_frame),
        "stage_ms_per_frame": {
            k: round(1000 * pipe.stats.stage_seconds[k] / max(pipe.stats.frames, 1), 3)
            for k in STAGES
        },
        "cpu_percent": _summ([s["cpu_percent"] for s in samples]),
        "temperature_c": {"max": max(temps)} if temps else None,
        "throttled": system.pi_throttled(),
        "peak_rss_mb": round(system.peak_rss_mb(), 1),
        "test_clip": {
            "path": str(clip_file) if clip_file else None,
            "bytes": clip_file.stat().st_size if clip_file and clip_file.exists() else None,
        },
        "samples": samples,
    }
