"""EXPERIMENTAL possible-vomiting (retching) detector.

Retching in dogs often shows as slow, repeated abdominal heaving while the
body stays mostly in place. This detector looks for moderate, persistent,
*slow-rhythmic* motion (``window.low_band_hz``) with little travel, using the
lagged ("slow") motion channel, which registers slow heaving that
consecutive-frame differencing misses.

Known confusions: panting, licking, eating/drinking, sniffing, breathing
heavily after exercise. It defaults to ``shadow`` mode (ADR-005) and must
never be required for seizure detection.
"""

from __future__ import annotations

from vigil_edge.config import VomitingConfig
from vigil_edge.detectors.base import Detector, ramp
from vigil_edge.models import SignalType, WindowFeatures


class VomitingDetector(Detector):
    name = "vomiting"
    signal_type = SignalType.VOMITING
    experimental = True

    def __init__(self, cfg: VomitingConfig) -> None:
        super().__init__(cfg.mode, cfg.threshold, cfg.onset_seconds, cfg.min_active_ratio)
        self.cfg = cfg

    def coverage(self, window: WindowFeatures) -> float:
        return window.slow_active_ratio

    def score(self, w: WindowFeatures) -> tuple[float, tuple[str, ...], dict[str, float]]:
        c = self.cfg
        suppressed: list[str] = []
        if w.camera_motion > c.max_camera_motion:
            suppressed.append("camera_motion")
        if w.lighting_change > c.max_lighting_change:
            suppressed.append("lighting_change")
        if w.travel > c.max_travel:
            suppressed.append("locomotion")
        if w.activity > c.max_activity or w.flow_magnitude > c.max_flow:
            suppressed.append("vigorous_motion")

        persistence = ramp(w.slow_active_ratio, *c.active_ratio_ramp)
        intensity = ramp(w.slow_activity, *c.activity_ramp)
        rhythm = ramp(w.low_periodicity, *c.low_periodicity_ramp)
        score = persistence * intensity * rhythm
        return (
            score,
            tuple(suppressed),
            {
                "persistence": persistence,
                "intensity": intensity,
                "rhythm": rhythm,
            },
        )
