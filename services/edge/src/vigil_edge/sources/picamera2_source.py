"""Raspberry Pi camera source (Picamera2/libcamera) + hardware clip backend.

Two streams from one camera configuration:

* ``main`` (e.g. 1280x720) goes to the VideoCore hardware H.264 encoder and a
  ``CircularOutput`` ring buffer holding ``pre_event_seconds`` of video. Clips
  are written by pointing that output at a file, which flushes the buffered
  lead-in first. The CPU never encodes video.
* ``lores`` (e.g. 320x180, YUV420) is read for analysis; its Y plane *is* the
  greyscale image the pipeline needs, so no colour conversion is required.

Picamera2 is imported lazily (it only exists on Raspberry Pi OS, installed via
apt), so this module is importable on any machine. Tests substitute fakes via
``load_modules``.

Requires a video-capable Pi (Pi 3/4 have the hardware encoder; a Pi 5 has
none and would need a software-encoder variant).
"""

from __future__ import annotations

import logging
import shutil
import subprocess
from collections.abc import Callable, Iterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import numpy as np

from vigil_edge.config import CameraConfig
from vigil_edge.models import Frame, Image
from vigil_edge.sources.base import FrameSource, SourceError

log = logging.getLogger(__name__)


def load_modules() -> SimpleNamespace:  # pragma: no cover - needs a Raspberry Pi
    try:
        from libcamera import Transform, controls
        from picamera2 import Picamera2
        from picamera2.encoders import H264Encoder
        from picamera2.outputs import CircularOutput
    except ImportError as exc:
        raise SourceError(
            "Picamera2 is not available. On Raspberry Pi OS install `python3-picamera2` "
            "and create the venv with --system-site-packages (see docs/PI_SETUP.md)."
        ) from exc
    return SimpleNamespace(
        Picamera2=Picamera2,
        H264Encoder=H264Encoder,
        CircularOutput=CircularOutput,
        Transform=Transform,
        controls=controls,
    )


class Picamera2CircularBackend:
    """Clip backend backed by Picamera2's CircularOutput (hardware H.264)."""

    def __init__(self, output: Any, fps: float, remux: bool = True) -> None:
        self._output = output
        self._fps = fps
        self._remux = remux and shutil.which("ffmpeg") is not None
        self._h264: Path | None = None

    def push(self, frame: Frame) -> None:  # buffering happens in the encoder pipeline
        pass

    def start_clip(self, path: Path, from_ts: float) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self._h264 = path.with_suffix(".h264")
        self._output.fileoutput = str(self._h264)
        self._output.start()

    def stop_clip(self) -> Path | None:
        if self._h264 is None:
            return None
        self._output.stop()
        h264, self._h264 = self._h264, None
        if not self._remux:
            return h264
        mp4 = h264.with_suffix(".mp4")
        # Input `-r` forces constant-rate timestamps. `-framerate` is overridden by the
        # stream's VUI timing, which the Pi encoder writes as 2x the frame rate: the
        # first Pi benchmark clip (2026-11-16) played at double speed because of it.
        result = subprocess.run(
            [
                "ffmpeg",
                "-loglevel",
                "error",
                "-y",
                "-r",
                f"{self._fps:g}",
                "-i",
                str(h264),
                "-c",
                "copy",
                str(mp4),
            ],
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
        )
        if result.returncode == 0 and mp4.is_file():
            h264.unlink(missing_ok=True)
            return mp4
        log.warning("remux failed, keeping raw H.264: %s", result.stderr.strip()[:200])
        return h264

    def close(self) -> None:
        if self._h264 is not None:
            self.stop_clip()


class Picamera2Source(FrameSource):
    def __init__(
        self,
        cfg: CameraConfig,
        pre_event_seconds: float,
        record: bool = True,
        modules: Callable[[], SimpleNamespace] = load_modules,
    ) -> None:
        self.cfg = cfg
        self._m = modules()
        self.dropped_frames = 0
        self._record = record
        w, h = cfg.analysis_size
        self._lores = (w, h)
        try:
            cam = self._m.Picamera2()
            frame_us = int(1_000_000 / cfg.fps)
            config = cam.create_video_configuration(
                main={"size": tuple(cfg.main_size)},
                lores={"size": (w, h), "format": "YUV420"},
                controls={"FrameDurationLimits": (frame_us, frame_us)},
                transform=self._m.Transform(hflip=int(cfg.hflip), vflip=int(cfg.vflip)),
            )
            cam.configure(config)
            self._cam = cam
            self.output: Any = None
            if record:
                encoder = self._m.H264Encoder(
                    bitrate=cfg.bitrate, repeat=True, iperiod=max(1, round(cfg.fps))
                )
                self.output = self._m.CircularOutput(
                    buffersize=max(1, round(cfg.fps * pre_event_seconds))
                )
                cam.start_recording(encoder, self.output)
            else:
                cam.start()
            self._focus()
        except SourceError:
            raise
        except Exception as exc:  # libcamera raises assorted runtime errors
            raise SourceError(f"camera failed to start: {exc}") from exc

    def _focus(self) -> None:
        af = self._m.controls.AfModeEnum
        try:
            if self.cfg.autofocus == "continuous":
                self._cam.set_controls({"AfMode": af.Continuous})
            elif self.cfg.autofocus == "manual":
                self._cam.set_controls(
                    {"AfMode": af.Manual, "LensPosition": self.cfg.lens_position}
                )
            else:
                ok = self._cam.autofocus_cycle()
                log.info("single autofocus cycle %s", "succeeded" if ok else "did not converge")
        except Exception as exc:  # cameras without AF reject these controls
            log.info("autofocus not applied: %s", exc)

    @property
    def fps(self) -> float:
        return self.cfg.fps

    @property
    def frame_size(self) -> tuple[int, int]:
        return self._lores

    @property
    def is_live(self) -> bool:
        return True

    def clip_backend(self) -> Picamera2CircularBackend:
        if self.output is None:
            raise SourceError("recording disabled; no circular output")
        return Picamera2CircularBackend(self.output, self.cfg.fps)

    def frames(self) -> Iterator[Frame]:
        w, h = self._lores
        period_ns = 1e9 / self.cfg.fps
        first: int | None = None
        last: int | None = None
        index = 0
        while True:
            try:
                request = self._cam.capture_request()
                try:
                    yuv = request.make_array("lores")
                    ts_ns = int(request.get_metadata()["SensorTimestamp"])
                finally:
                    request.release()
            except Exception as exc:
                raise SourceError(f"camera capture failed: {exc}") from exc
            if first is None:
                first = ts_ns
            if last is not None:
                gap = (ts_ns - last) / period_ns
                if gap > 1.5:
                    self.dropped_frames += round(gap) - 1
            last = ts_ns
            gray: Image = np.ascontiguousarray(yuv[:h, :w])
            yield Frame(index, (ts_ns - first) / 1e9, gray)
            index += 1

    def close(self) -> None:
        try:
            if self._record:
                self._cam.stop_recording()
            else:
                self._cam.stop()
        except Exception as exc:  # pragma: no cover - best effort
            log.debug("camera stop: %s", exc)
        try:
            self._cam.close()
        except Exception as exc:  # pragma: no cover
            log.debug("camera close: %s", exc)
