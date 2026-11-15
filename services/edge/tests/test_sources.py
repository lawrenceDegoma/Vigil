from __future__ import annotations

from pathlib import Path

import cv2
import numpy as np
import pytest

from vigil_edge.sources.base import SourceError
from vigil_edge.sources.synthetic import PRESETS, Segment, SyntheticSource, get_scenario
from vigil_edge.sources.video_file import VideoFileSource


def write_video(path: Path, seconds: float = 4.0, fps: float = 30.0) -> None:
    w = cv2.VideoWriter(str(path), cv2.VideoWriter.fourcc(*"mp4v"), fps, (160, 120))
    assert w.isOpened()
    for i in range(int(seconds * fps)):
        img = np.full((120, 160, 3), (i * 2) % 255, np.uint8)
        w.write(img)
    w.release()


def test_video_file_subsamples_to_target_fps(tmp_path: Path) -> None:
    p = tmp_path / "clip.mp4"
    write_video(p, seconds=4.0, fps=30.0)
    src = VideoFileSource(p, target_fps=10.0)
    assert src.frame_size == (160, 120)
    assert src.native_fps == pytest.approx(30.0)
    frames = list(src.frames())
    src.close()
    assert len(frames) == pytest.approx(40, abs=1)
    ts = np.array([f.timestamp for f in frames])
    assert np.allclose(np.diff(ts), 0.1, atol=1e-6)
    assert [f.index for f in frames] == list(range(len(frames)))


def test_video_file_start_end(tmp_path: Path) -> None:
    p = tmp_path / "clip.mp4"
    write_video(p, seconds=4.0)
    with VideoFileSource(p, target_fps=30.0, start=1.0, end=2.0) as src:
        ts = [f.timestamp for f in src.frames()]
    assert ts[0] == pytest.approx(1.0, abs=0.04)
    assert ts[-1] <= 2.0


def test_video_file_errors(tmp_path: Path) -> None:
    with pytest.raises(SourceError, match="not found"):
        VideoFileSource(tmp_path / "missing.mp4")
    bad = tmp_path / "bad.mp4"
    bad.write_bytes(b"not a video")
    with pytest.raises(SourceError):
        VideoFileSource(bad)


def test_synthetic_is_deterministic() -> None:
    sc = get_scenario("convulsion")
    a = SyntheticSource(sc, fps=15, size=(160, 90), seed=3)
    b = SyntheticSource(sc, fps=15, size=(160, 90), seed=3)
    fa = [f.image for f, _ in zip(a.frames(), range(40), strict=False)]
    fb = [f.image for f, _ in zip(b.frames(), range(40), strict=False)]
    assert all(np.array_equal(x, y) for x, y in zip(fa, fb, strict=True))


def test_synthetic_frame_count_and_labels() -> None:
    sc = get_scenario("cluster")
    src = SyntheticSource(sc, fps=10, size=(160, 90))
    assert sum(1 for _ in src.frames()) == int(sc.duration * 10)
    assert [lb.signal_type.value for lb in sc.labels] == ["convulsion", "convulsion"]


@pytest.mark.parametrize("name", list(PRESETS))
def test_every_preset_renders(name: str) -> None:
    src = SyntheticSource(get_scenario(name), fps=5, size=(160, 90))
    frame = next(iter(src.frames()))
    assert frame.image.shape == (90, 160, 3)


def test_segment_validation() -> None:
    with pytest.raises(ValueError):
        Segment(5, 1, "rest")
    with pytest.raises(ValueError):
        Segment(0, 1, "dance")
