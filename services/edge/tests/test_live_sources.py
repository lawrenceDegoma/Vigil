from __future__ import annotations

from vigil_edge.sources.live import LoopingSource, PacedSource
from vigil_edge.sources.synthetic import Scenario, Segment, SyntheticSource

SCENE = Scenario("t", "tiny", 1.0, (Segment(0, 1, "rest"),))


def tiny() -> SyntheticSource:
    return SyntheticSource(SCENE, fps=10, size=(64, 36))


def test_paced_source_sleeps_to_timestamps() -> None:
    now = [0.0]
    slept: list[float] = []

    def sleep(s: float) -> None:
        slept.append(round(s, 3))
        now[0] += s

    frames = list(PacedSource(tiny(), monotonic=lambda: now[0], sleep=sleep).frames())
    assert len(frames) == 10
    assert slept == [0.1] * 9
    assert now[0] == 0.9 or abs(now[0] - 0.9) < 1e-9


def test_looping_source_has_continuous_timestamps() -> None:
    frames = list(LoopingSource(tiny, max_loops=3).frames())
    ts = [round(f.timestamp, 3) for f in frames]
    assert len(frames) == 30
    assert ts[9:12] == [0.9, 1.0, 1.1]
    assert [f.index for f in frames] == list(range(30))
