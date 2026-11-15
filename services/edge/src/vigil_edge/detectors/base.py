"""Detector interface and shared scoring helpers."""

from __future__ import annotations

from abc import ABC, abstractmethod

from vigil_edge.models import DetectionResult, DetectorMode, SignalType, WindowFeatures


def ramp(value: float, lo: float, hi: float) -> float:
    """Linear 0..1 ramp: ``lo`` → 0, ``hi`` → 1, clamped."""
    if hi <= lo:
        return 1.0 if value >= hi else 0.0
    return min(1.0, max(0.0, (value - lo) / (hi - lo)))


class Detector(ABC):
    """Scores analysis windows for one signal type.

    Detectors are stateless per window; temporal persistence is the incident
    state machine's job. Scores are uncalibrated and must not be presented as
    probabilities.
    """

    name: str
    signal_type: SignalType

    def __init__(
        self, mode: DetectorMode, threshold: float, onset_seconds: float, min_active_ratio: float
    ) -> None:
        self.mode = mode
        self.threshold = threshold
        self.onset_seconds = onset_seconds
        self.min_active_ratio = min_active_ratio

    @abstractmethod
    def score(self, window: WindowFeatures) -> tuple[float, tuple[str, ...], dict[str, float]]:
        """Return (score, suppression reasons, component scores)."""

    def evaluate(self, window: WindowFeatures) -> DetectionResult:
        score, suppressed, components = self.score(window)
        active = self.mode is not DetectorMode.OFF and not suppressed and score >= self.threshold
        return DetectionResult(
            detector=self.name,
            signal_type=self.signal_type,
            mode=self.mode,
            score=round(score, 4),
            active=active,
            suppressed_by=suppressed,
            components={k: round(v, 4) for k, v in components.items()},
        )
