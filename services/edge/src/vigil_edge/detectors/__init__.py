"""Pluggable window detectors. Each scores one signal type."""

from __future__ import annotations

from vigil_edge.config import VigilConfig
from vigil_edge.detectors.base import Detector, ramp
from vigil_edge.detectors.convulsion import ConvulsionDetector
from vigil_edge.detectors.vomiting import VomitingDetector

__all__ = ["ConvulsionDetector", "Detector", "VomitingDetector", "build_detectors", "ramp"]


def build_detectors(config: VigilConfig) -> list[Detector]:
    """Instantiate every configured detector (including ``off`` ones, for logging)."""
    return [
        ConvulsionDetector(config.detectors.convulsion),
        VomitingDetector(config.detectors.vomiting),
    ]
