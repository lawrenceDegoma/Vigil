"""In-process data model shared by all pipeline stages.

Everything here is plain, immutable data so stages can be tested independently.
Times are *stream offsets* in seconds (see docs/DECISIONS.md ADR-008).
"""

from __future__ import annotations

from dataclasses import dataclass, field, fields
from enum import StrEnum
from typing import Any

import numpy as np
import numpy.typing as npt

Image = npt.NDArray[np.uint8]


class SignalType(StrEnum):
    """Observable patterns a detector can score. These are not diagnoses."""

    CONVULSION = "convulsion"
    VOMITING = "vomiting"
    # Reserved for future profiles (e.g. monitoring an elderly person):
    FALL = "fall"
    PROLONGED_INACTIVITY = "prolonged_inactivity"


class Severity(StrEnum):
    WARNING = "warning"
    URGENT = "urgent"


class DetectorMode(StrEnum):
    OFF = "off"
    SHADOW = "shadow"  # scored and logged, never opens incidents
    ALERT = "alert"


class MonitorState(StrEnum):
    """Top-level incident state machine states (DESIGN §5.2).

    ``URGENT`` corresponds to "SuspectedSeizure" in the dog profile.
    """

    MONITORING = "monitoring"
    EARLY_WARNING = "early_warning"
    URGENT = "urgent"
    RECOVERY_OBSERVATION = "recovery_observation"


@dataclass(frozen=True, slots=True)
class Frame:
    index: int
    timestamp: float  # seconds since stream start
    image: Image  # BGR, uint8


@dataclass(frozen=True, slots=True)
class FrameFeatures:
    """Per-frame motion measurements, all resolution-independent."""

    index: int
    timestamp: float
    motion_fraction: float  # moving pixels / ROI pixels
    flow_dx: float  # mean residual flow of moving pixels, in frame-widths per second
    flow_dy: float
    flow_magnitude: float  # mean residual flow magnitude, frame-widths per second
    global_motion: float  # estimated camera motion, frame-widths per second
    brightness: float  # mean ROI intensity, 0..1
    brightness_delta: float  # absolute change vs previous frame, 0..1
    centroid: tuple[float, float] | None  # dominant moving region centre, normalized 0..1
    region: tuple[float, float, float, float] | None  # dominant region bbox (x, y, w, h), 0..1
    region_area: float  # bbox area / frame area


@dataclass(frozen=True, slots=True)
class WindowFeatures:
    """Temporal summary of a sliding window of frame features."""

    t_start: float
    t_end: float
    n_frames: int
    sample_rate: float
    activity: float  # mean motion_fraction
    active_ratio: float  # fraction of frames with motion above the activity floor
    flow_magnitude: float  # mean residual flow magnitude
    periodicity: float  # 0..1 spectral concentration in the analysis band
    dominant_hz: float  # frequency of the strongest in-band peak (0 if none)
    low_periodicity: float  # spectral concentration in the slow band (retching)
    low_dominant_hz: float
    reversal_rate: float  # direction reversals per second of the principal flow axis
    travel: float  # net displacement of the moving region across the window, 0..~1.4
    region_area: float  # mean dominant-region area
    camera_motion: float  # max global motion in window
    lighting_change: float  # max brightness delta in window

    def as_dict(self) -> dict[str, float]:
        return {f.name: float(getattr(self, f.name)) for f in fields(self)}


@dataclass(frozen=True, slots=True)
class DetectionResult:
    """A detector's verdict for one window."""

    detector: str
    signal_type: SignalType
    mode: DetectorMode
    score: float  # 0..1, uncalibrated
    active: bool  # score >= threshold and no suppression
    suppressed_by: tuple[str, ...] = ()
    components: dict[str, float] = field(default_factory=dict)


@dataclass(slots=True)
class Incident:
    """An episode tracked by the state machine. Mutable while open."""

    id: str
    signal_type: SignalType
    severity: Severity
    started_at: float  # estimated onset (stream offset)
    opened_at: float  # when the incident was confirmed (detection latency = opened_at - onset)
    last_active_at: float
    ended_at: float | None = None
    peak_score: float = 0.0
    follows_incident_id: str | None = None
    rule_version: str = ""

    @property
    def is_open(self) -> bool:
        return self.ended_at is None


class IncidentEventType(StrEnum):
    OPENED = "incident_opened"
    UPDATED = "incident_updated"
    ENDED = "incident_ended"


@dataclass(frozen=True, slots=True)
class IncidentEvent:
    """Lifecycle event emitted by the state machine (maps to incident-event.v1)."""

    event_id: str
    event_type: IncidentEventType
    at: float  # stream offset when the event was emitted
    incident: Incident  # snapshot copy
    features: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class StateChange:
    at: float
    previous: MonitorState
    current: MonitorState
    reason: str


@dataclass(frozen=True, slots=True)
class ShadowDetection:
    """A persistence-confirmed candidate from a detector running in shadow mode."""

    at: float
    detector: str
    signal_type: SignalType
    started_at: float
    peak_score: float
