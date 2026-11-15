"""Picamera2 adapter against a fake Picamera2 API (real hardware: docs/PI_SETUP.md)."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any, ClassVar

import numpy as np
import pytest

from vigil_edge.config import CameraConfig
from vigil_edge.sources.base import SourceError
from vigil_edge.sources.picamera2_source import Picamera2CircularBackend, Picamera2Source

W, H = 320, 180


class FakeRequest:
    def __init__(self, ts_ns: int, value: int) -> None:
        self.ts, self.value, self.released = ts_ns, value, False

    def make_array(self, stream: str) -> np.ndarray:
        assert stream == "lores"
        yuv = np.zeros((H * 3 // 2, W), np.uint8)
        yuv[:H] = self.value  # Y plane
        yuv[H:] = 128
        return yuv

    def get_metadata(self) -> dict[str, Any]:
        return {"SensorTimestamp": self.ts}

    def release(self) -> None:
        self.released = True


class FakeCam:
    instances: ClassVar[list[FakeCam]] = []

    def __init__(self) -> None:
        self.controls: dict[str, Any] = {}
        self.calls: list[str] = []
        self.timestamps = [0, 66_666_667, 133_333_333, 333_333_333, 400_000_000]  # gap: 2 dropped
        self.fail_after: int | None = None
        FakeCam.instances.append(self)

    def create_video_configuration(self, **kw: Any) -> dict[str, Any]:
        self.config = kw
        return kw

    def configure(self, cfg: Any) -> None:
        self.calls.append("configure")

    def start_recording(self, encoder: Any, output: Any) -> None:
        self.encoder, self.output = encoder, output
        self.calls.append("start_recording")

    def start(self) -> None:
        self.calls.append("start")

    def set_controls(self, c: dict[str, Any]) -> None:
        self.controls.update(c)

    def autofocus_cycle(self) -> bool:
        self.calls.append("autofocus_cycle")
        return True

    def capture_request(self) -> FakeRequest:
        n = sum(1 for c in self.calls if c == "capture")
        self.calls.append("capture")
        if (self.fail_after is not None and n >= self.fail_after) or n >= len(self.timestamps):
            raise RuntimeError("Camera frontend has timed out!")
        return FakeRequest(self.timestamps[n], 10 * n)

    def stop_recording(self) -> None:
        self.calls.append("stop_recording")

    def stop(self) -> None:
        self.calls.append("stop")

    def close(self) -> None:
        self.calls.append("close")


class FakeOutput:
    def __init__(self, buffersize: int) -> None:
        self.buffersize = buffersize
        self.fileoutput: str | None = None
        self.events: list[str] = []

    def start(self) -> None:
        assert self.fileoutput
        Path(self.fileoutput).write_bytes(b"\x00\x00\x00\x01fake-h264")
        self.events.append("start")

    def stop(self) -> None:
        self.events.append("stop")


def modules() -> SimpleNamespace:
    return SimpleNamespace(
        Picamera2=FakeCam,
        H264Encoder=lambda **kw: SimpleNamespace(**kw),
        CircularOutput=FakeOutput,
        Transform=lambda hflip, vflip: ("T", hflip, vflip),
        controls=SimpleNamespace(AfModeEnum=SimpleNamespace(Continuous="C", Manual="M")),
    )


def make(**kw: Any) -> Picamera2Source:
    FakeCam.instances.clear()
    return Picamera2Source(CameraConfig(**kw), pre_event_seconds=30, modules=modules)


def test_configures_dual_stream_with_hardware_recording() -> None:
    src = make(fps=15, main_size=(1280, 720), analysis_size=(W, H), vflip=True)
    cam = FakeCam.instances[0]
    assert cam.config["main"] == {"size": (1280, 720)}
    assert cam.config["lores"] == {"size": (W, H), "format": "YUV420"}
    assert cam.config["controls"]["FrameDurationLimits"] == (66666, 66666)
    assert cam.config["transform"] == ("T", 0, 1)
    assert "start_recording" in cam.calls and "autofocus_cycle" in cam.calls
    assert cam.output.buffersize == 450  # 30 s @ 15 fps
    assert cam.encoder.iperiod == 15 and cam.encoder.repeat is True
    assert src.frame_size == (W, H) and src.is_live


def test_frames_are_y_plane_with_sensor_timestamps_and_drop_count() -> None:
    src = make(fps=15, analysis_size=(W, H))
    it = src.frames()
    frames = [next(it) for _ in range(5)]
    assert frames[0].image.shape == (H, W)
    assert frames[1].image[0, 0] == 10
    assert [round(f.timestamp, 3) for f in frames] == [0.0, 0.067, 0.133, 0.333, 0.4]
    assert src.dropped_frames == 2
    with pytest.raises(SourceError, match="capture failed"):
        next(it)


@pytest.mark.parametrize(
    ("mode", "expected"),
    [
        ("continuous", {"AfMode": "C"}),
        ("manual", {"AfMode": "M", "LensPosition": 2.5}),
    ],
)
def test_focus_modes(mode: str, expected: dict[str, Any]) -> None:
    make(autofocus=mode, lens_position=2.5)
    assert FakeCam.instances[0].controls == expected


def test_start_failure_is_source_error() -> None:
    def broken() -> SimpleNamespace:
        m = modules()
        m.Picamera2 = lambda: (_ for _ in ()).throw(RuntimeError("no cameras available"))
        return m

    with pytest.raises(SourceError, match="no cameras"):
        Picamera2Source(CameraConfig(), 30, modules=broken)


def test_close_stops_camera() -> None:
    src = make()
    src.close()
    assert FakeCam.instances[0].calls[-2:] == ["stop_recording", "close"]


def test_circular_backend_writes_clip(tmp_path: Path) -> None:
    out = FakeOutput(450)
    be = Picamera2CircularBackend(out, fps=15, remux=False)
    be.start_clip(tmp_path / "d" / "clip.mp4", from_ts=0)
    path = be.stop_clip()
    assert path == tmp_path / "d" / "clip.h264"
    assert path.read_bytes().startswith(b"\x00\x00\x00\x01")
    assert out.events == ["start", "stop"]
    assert be.stop_clip() is None
