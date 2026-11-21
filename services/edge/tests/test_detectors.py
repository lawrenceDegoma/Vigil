from __future__ import annotations

from dataclasses import replace

import pytest

from helpers import CONVULSING_WINDOW, QUIET_WINDOW, RETCHING_WINDOW
from vigil_edge.config import ConvulsionConfig, VigilConfig, VomitingConfig
from vigil_edge.detectors import ConvulsionDetector, VomitingDetector, build_detectors, ramp
from vigil_edge.models import DetectorMode


def test_ramp() -> None:
    assert ramp(0.0, 0.1, 0.3) == 0.0
    assert ramp(0.2, 0.1, 0.3) == pytest.approx(0.5)
    assert ramp(9.0, 0.1, 0.3) == 1.0


def test_convulsion_scores_sustained_rhythmic_motion() -> None:
    r = ConvulsionDetector(ConvulsionConfig()).evaluate(CONVULSING_WINDOW)
    assert r.active and r.score > 0.9 and not r.suppressed_by


def test_convulsion_ignores_quiet_window() -> None:
    r = ConvulsionDetector(ConvulsionConfig()).evaluate(QUIET_WINDOW)
    assert not r.active and r.score == 0.0


def test_rhythm_boosts_but_is_not_required() -> None:
    det = ConvulsionDetector(ConvulsionConfig())
    irregular = det.evaluate(replace(CONVULSING_WINDOW, periodicity=0.0))
    assert 0.0 < irregular.score < det.evaluate(CONVULSING_WINDOW).score
    assert irregular.score == pytest.approx(ConvulsionConfig().rhythm_floor)


@pytest.mark.parametrize(
    ("change", "reason"),
    [
        ({"travel": 0.3}, "locomotion"),
        ({"camera_motion": 0.4}, "camera_motion"),
        ({"lighting_change": 0.2}, "lighting_change"),
        ({"region_area": 0.95}, "scene_wide_motion"),
    ],
)
def test_convulsion_suppressions(change: dict[str, float], reason: str) -> None:
    r = ConvulsionDetector(ConvulsionConfig()).evaluate(replace(CONVULSING_WINDOW, **change))
    assert not r.active
    assert reason in r.suppressed_by


def test_off_mode_never_active() -> None:
    det = ConvulsionDetector(ConvulsionConfig(mode=DetectorMode.OFF))
    assert not det.evaluate(CONVULSING_WINDOW).active


def test_vomiting_scores_slow_heaving() -> None:
    r = VomitingDetector(VomitingConfig()).evaluate(RETCHING_WINDOW)
    assert r.active, r
    assert r.mode is DetectorMode.SHADOW


def test_vomiting_rejects_vigorous_motion() -> None:
    r = VomitingDetector(VomitingConfig()).evaluate(replace(CONVULSING_WINDOW, low_periodicity=0.5))
    assert not r.active and "vigorous_motion" in r.suppressed_by


def test_build_detectors_defaults() -> None:
    dets = {d.name: d for d in build_detectors(VigilConfig())}
    assert dets["convulsion"].mode is DetectorMode.ALERT
    assert dets["vomiting"].mode is DetectorMode.SHADOW


def test_clear_slow_rhythm_is_evidence_against_convulsion() -> None:
    """A person exercising (~1.5 Hz, very regular) must not look like paddling (~3 Hz)."""
    det = ConvulsionDetector(ConvulsionConfig())
    base = replace(CONVULSING_WINDOW, periodicity=0.5)
    paddling = replace(base, dominant_hz=3.2)
    pushups = replace(base, dominant_hz=1.5)
    irregular_slow = replace(base, dominant_hz=1.5, periodicity=0.15)  # no clear rhythm
    assert "slow_rhythm" not in det.score(paddling)[1]
    assert "slow_rhythm" in det.score(pushups)[1]
    assert "slow_rhythm" not in det.score(irregular_slow)[1]  # tonic/irregular phases kept
    off = ConvulsionDetector(ConvulsionConfig(slow_rhythm_hz=0.0))
    assert "slow_rhythm" not in off.score(pushups)[1]
