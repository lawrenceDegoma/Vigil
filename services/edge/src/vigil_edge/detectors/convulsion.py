"""Convulsion-like motion detector (experimental, rule-based).

Looks for windows with *persistent*, *vigorous*, preferably *rhythmic* motion
that stays *in place*. Hard suppressions reject windows dominated by camera
movement, lighting changes, locomotion, or scene-wide change.

Score = persistence × intensity × rhythm_term, where
rhythm_term = rhythm_floor + (1 - rhythm_floor) × rhythm. Rhythm raises the
score but is not strictly required, because tonic or irregular clonic phases
may not be cleanly periodic. Sustained duration is enforced downstream by the
incident state machine (``onset_seconds``), which is what separates brief
scratching or shaking-off from a sustained episode.

These rules are hypotheses to be evaluated on labeled footage; they are not
medically validated.
"""

from __future__ import annotations

from vigil_edge.config import ConvulsionConfig
from vigil_edge.detectors.base import Detector, ramp
from vigil_edge.models import SignalType, WindowFeatures


class ConvulsionDetector(Detector):
    name = "convulsion"
    signal_type = SignalType.CONVULSION

    def __init__(self, cfg: ConvulsionConfig) -> None:
        super().__init__(cfg.mode, cfg.threshold, cfg.onset_seconds, cfg.min_active_ratio)
        self.cfg = cfg

    def score(self, w: WindowFeatures) -> tuple[float, tuple[str, ...], dict[str, float]]:
        c = self.cfg
        suppressed: list[str] = []
        if w.camera_motion > c.max_camera_motion:
            suppressed.append("camera_motion")
        if w.lighting_change > c.max_lighting_change:
            suppressed.append("lighting_change")
        if w.travel > c.max_travel:
            suppressed.append("locomotion")
        if w.region_area > c.max_region_area:
            suppressed.append("scene_wide_motion")
        if (
            c.slow_rhythm_hz > 0
            and w.periodicity >= c.slow_rhythm_min_periodicity
            and 0 < w.dominant_hz < c.slow_rhythm_hz
        ):
            suppressed.append("slow_rhythm")  # clearly rhythmic but too slow (e.g. exercise)

        persistence = ramp(w.active_ratio, *c.active_ratio_ramp)
        intensity = max(ramp(w.activity, *c.activity_ramp), ramp(w.flow_magnitude, *c.flow_ramp))
        rhythm = ramp(w.periodicity, *c.periodicity_ramp)
        rhythm_term = c.rhythm_floor + (1.0 - c.rhythm_floor) * rhythm
        score = persistence * intensity * rhythm_term
        return (
            score,
            tuple(suppressed),
            {
                "persistence": persistence,
                "intensity": intensity,
                "rhythm": rhythm,
            },
        )
