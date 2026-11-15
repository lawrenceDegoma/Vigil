"""Typed, strictly validated configuration (docs/DECISIONS.md ADR-004).

All thresholds are *experimental engineering defaults*, not medically validated
values. They exist to be tuned against labeled footage.
"""

from __future__ import annotations

import hashlib
import json
import tomllib
import types
from dataclasses import asdict, dataclass, field, fields, is_dataclass, replace
from enum import Enum
from pathlib import Path
from typing import Any, TypeVar, Union, get_args, get_origin, get_type_hints

from vigil_edge.models import DetectorMode

RULES_VERSION = "motion-rules-0.1.0"


class ConfigError(ValueError):
    pass


Range = tuple[float, float]


def _check_range(name: str, value: Range, lo: float = 0.0, hi: float | None = None) -> None:
    a, b = value
    if not (a < b):
        raise ConfigError(f"{name}: lower bound {a} must be < upper bound {b}")
    if a < lo or (hi is not None and b > hi):
        raise ConfigError(f"{name}: {value} outside allowed [{lo}, {hi}]")


@dataclass(frozen=True)
class SubjectConfig:
    name: str = "Subject"
    profile: str = "dog"


@dataclass(frozen=True)
class SourceConfig:
    target_fps: float = 15.0  # analysis rate; sources are subsampled to this

    def __post_init__(self) -> None:
        if not (1.0 <= self.target_fps <= 60.0):
            raise ConfigError("source.target_fps must be within [1, 60]")


@dataclass(frozen=True)
class PreprocessConfig:
    width: int = 320  # analysis width in pixels; height keeps aspect ratio
    blur_kernel: int = 5  # odd Gaussian kernel; 0/1 disables
    # Region of interest polygon in normalized coordinates [[x, y], ...]; empty = full frame.
    roi: tuple[tuple[float, float], ...] = ()

    def __post_init__(self) -> None:
        if not (64 <= self.width <= 1920):
            raise ConfigError("preprocess.width must be within [64, 1920]")
        if self.blur_kernel > 1 and self.blur_kernel % 2 == 0:
            raise ConfigError("preprocess.blur_kernel must be odd")
        if self.roi and len(self.roi) < 3:
            raise ConfigError("preprocess.roi needs at least 3 points")
        for x, y in self.roi:
            if not (0.0 <= x <= 1.0 and 0.0 <= y <= 1.0):
                raise ConfigError("preprocess.roi points must be normalized to [0, 1]")


@dataclass(frozen=True)
class MotionConfig:
    diff_threshold: int = 14  # grey-level change counted as motion (0..255)
    stabilize: bool = False  # compensate camera motion (handheld footage); off for fixed mounts
    flow: bool = True  # dense optical flow for direction/oscillation features
    flow_width: int = 160  # optical flow runs at this width to bound CPU cost
    min_region_area: float = 0.0004  # ignore moving blobs smaller than this fraction of frame
    # Second, lagged difference for slow movements (e.g. heaving) that move too
    # little between consecutive frames to register. 0 disables.
    slow_lag_seconds: float = 0.25

    def __post_init__(self) -> None:
        if not (0.0 <= self.slow_lag_seconds <= 2.0):
            raise ConfigError("motion.slow_lag_seconds must be within [0, 2]")
        if not (1 <= self.diff_threshold <= 255):
            raise ConfigError("motion.diff_threshold must be within [1, 255]")
        if not (32 <= self.flow_width <= 640):
            raise ConfigError("motion.flow_width must be within [32, 640]")


@dataclass(frozen=True)
class WindowConfig:
    seconds: float = 5.0  # sliding analysis window length
    step_seconds: float = 0.5  # how often a window is evaluated
    activity_floor: float = 0.004  # motion_fraction above which a frame counts as active
    band_hz: Range = (1.0, 6.0)  # rhythmic band for convulsion-like motion
    low_band_hz: Range = (0.4, 1.2)  # slow rhythmic band for retching-like motion

    def __post_init__(self) -> None:
        if not (1.0 <= self.seconds <= 30.0):
            raise ConfigError("window.seconds must be within [1, 30]")
        if not (0.05 <= self.step_seconds <= self.seconds):
            raise ConfigError("window.step_seconds must be within [0.05, window.seconds]")
        _check_range("window.band_hz", self.band_hz)
        _check_range("window.low_band_hz", self.low_band_hz)


@dataclass(frozen=True)
class ConvulsionConfig:
    """Sustained, rhythmic, in-place, whole-body motion. Experimental rule set."""

    mode: DetectorMode = DetectorMode.ALERT
    threshold: float = 0.5  # window score needed to count as active
    onset_seconds: float = 10.0  # persistence before an incident opens
    min_active_ratio: float = 0.6  # fraction of windows active during the onset period
    # Score ramps: (value giving 0, value giving 1)
    active_ratio_ramp: Range = (0.5, 0.9)
    activity_ramp: Range = (0.006, 0.03)
    flow_ramp: Range = (0.02, 0.12)  # residual flow magnitude, frame-widths/s
    periodicity_ramp: Range = (0.12, 0.4)
    rhythm_floor: float = 0.35  # score multiplier when no rhythm is found (irregular jerks)
    # Suppressions (each blocks the window outright)
    max_travel: float = 0.1  # walking/running moves the region across the frame
    max_camera_motion: float = 0.05  # frame-widths/s; a fixed camera should not move
    max_lighting_change: float = 0.06  # per-frame brightness jump
    max_region_area: float = 0.6  # motion over most of the frame = scene-wide change

    def __post_init__(self) -> None:
        _validate_detector("convulsion", self.threshold, self.onset_seconds, self.min_active_ratio)
        for name in ("active_ratio_ramp", "activity_ramp", "flow_ramp", "periodicity_ramp"):
            _check_range(f"detectors.convulsion.{name}", getattr(self, name))
        if not (0.0 <= self.rhythm_floor <= 1.0):
            raise ConfigError("detectors.convulsion.rhythm_floor must be within [0, 1]")


@dataclass(frozen=True)
class VomitingConfig:
    """EXPERIMENTAL: slow rhythmic heaving while stationary. Expected to be noisy."""

    mode: DetectorMode = DetectorMode.SHADOW
    threshold: float = 0.35
    onset_seconds: float = 4.0
    min_active_ratio: float = 0.6
    # Ramps apply to the lagged ("slow") motion channel. Heaving has still
    # phases between contractions, so the active ratio is naturally ~0.5.
    active_ratio_ramp: Range = (0.2, 0.5)
    activity_ramp: Range = (0.001, 0.004)
    low_periodicity_ramp: Range = (0.15, 0.4)
    max_activity: float = 0.08  # vigorous motion is something else (play, convulsion)
    max_flow: float = 0.08  # frame-widths/s; heaving is slow, shaking/walking is fast
    max_travel: float = 0.06
    max_camera_motion: float = 0.05
    max_lighting_change: float = 0.06

    def __post_init__(self) -> None:
        _validate_detector("vomiting", self.threshold, self.onset_seconds, self.min_active_ratio)
        for name in ("active_ratio_ramp", "activity_ramp", "low_periodicity_ramp"):
            _check_range(f"detectors.vomiting.{name}", getattr(self, name))


def _validate_detector(name: str, threshold: float, onset: float, ratio: float) -> None:
    if not (0.0 < threshold <= 1.0):
        raise ConfigError(f"detectors.{name}.threshold must be within (0, 1]")
    if not (0.0 <= onset <= 600.0):
        raise ConfigError(f"detectors.{name}.onset_seconds must be within [0, 600]")
    if not (0.0 < ratio <= 1.0):
        raise ConfigError(f"detectors.{name}.min_active_ratio must be within (0, 1]")


@dataclass(frozen=True)
class DetectorsConfig:
    convulsion: ConvulsionConfig = field(default_factory=ConvulsionConfig)
    vomiting: VomitingConfig = field(default_factory=VomitingConfig)


@dataclass(frozen=True)
class IncidentConfig:
    gap_tolerance_seconds: float = 3.0  # brief pauses that don't break a candidate
    end_quiet_seconds: float = 8.0  # inactivity before an episode is considered ended
    merge_gap_seconds: float = 30.0  # resumption within this continues the same incident
    recovery_observation_seconds: float = 120.0
    early_warning_window_seconds: float = 600.0

    def __post_init__(self) -> None:
        for f in fields(self):
            if getattr(self, f.name) < 0:
                raise ConfigError(f"incident.{f.name} must be >= 0")


@dataclass(frozen=True)
class RecordingConfig:
    """Used from Phase 2 (live circular buffer); defined now so configs are stable."""

    pre_event_seconds: float = 30.0
    post_event_seconds: float = 120.0
    retention_days: int = 30
    disk_quota_gb: float = 8.0
    min_free_gb: float = 2.0


@dataclass(frozen=True)
class VigilConfig:
    subject: SubjectConfig = field(default_factory=SubjectConfig)
    source: SourceConfig = field(default_factory=SourceConfig)
    preprocess: PreprocessConfig = field(default_factory=PreprocessConfig)
    motion: MotionConfig = field(default_factory=MotionConfig)
    window: WindowConfig = field(default_factory=WindowConfig)
    detectors: DetectorsConfig = field(default_factory=DetectorsConfig)
    incident: IncidentConfig = field(default_factory=IncidentConfig)
    recording: RecordingConfig = field(default_factory=RecordingConfig)

    def __post_init__(self) -> None:
        nyquist = self.source.target_fps / 2.0
        if self.window.band_hz[1] >= nyquist:
            raise ConfigError(
                f"window.band_hz upper bound {self.window.band_hz[1]} Hz must be below the "
                f"Nyquist limit {nyquist} Hz for source.target_fps={self.source.target_fps}"
            )
        if self.window.seconds * self.window.low_band_hz[0] < 1.5:
            raise ConfigError(
                "window.seconds is too short to resolve window.low_band_hz (need ≥1.5 cycles)"
            )

    @property
    def rule_version(self) -> str:
        """Rule-set version plus a hash of every detection-relevant setting."""
        relevant = {
            k: v for k, v in config_to_dict(self).items() if k not in ("subject", "recording")
        }
        digest = hashlib.sha256(json.dumps(relevant, sort_keys=True).encode()).hexdigest()
        return f"{RULES_VERSION}+{digest[:8]}"

    def with_overrides(self, **sections: Any) -> VigilConfig:
        """Return a copy with whole sections replaced, re-running validation."""
        return replace(self, **sections)


# --------------------------------------------------------------------------- loading

T = TypeVar("T")


def _coerce(tp: Any, value: Any, path: str) -> Any:
    origin = get_origin(tp)
    if origin in (Union, types.UnionType):
        args = [a for a in get_args(tp) if a is not type(None)]
        if value is None:
            return None
        return _coerce(args[0], value, path)
    if is_dataclass(tp) and isinstance(tp, type):
        if not isinstance(value, dict):
            raise ConfigError(f"{path}: expected a table")
        return _build(tp, value, path)
    if isinstance(tp, type) and issubclass(tp, Enum):
        try:
            return tp(value)
        except ValueError:
            allowed = ", ".join(str(m.value) for m in tp)
            raise ConfigError(f"{path}: {value!r} is not one of: {allowed}") from None
    if origin is tuple:
        targs = get_args(tp)
        if not isinstance(value, list | tuple):
            raise ConfigError(f"{path}: expected an array")
        if len(targs) == 2 and targs[1] is Ellipsis:
            return tuple(_coerce(targs[0], v, f"{path}[{i}]") for i, v in enumerate(value))
        if len(value) != len(targs):
            raise ConfigError(f"{path}: expected {len(targs)} values, got {len(value)}")
        pairs = enumerate(zip(targs, value, strict=True))
        return tuple(_coerce(a, v, f"{path}[{i}]") for i, (a, v) in pairs)
    if tp is bool:
        if not isinstance(value, bool):
            raise ConfigError(f"{path}: expected true/false")
        return value
    if tp is int:
        if isinstance(value, bool) or not isinstance(value, int):
            raise ConfigError(f"{path}: expected an integer")
        return value
    if tp is float:
        if isinstance(value, bool) or not isinstance(value, int | float):
            raise ConfigError(f"{path}: expected a number")
        return float(value)
    if tp is str:
        if not isinstance(value, str):
            raise ConfigError(f"{path}: expected a string")
        return value
    raise ConfigError(f"{path}: unsupported config type {tp!r}")


def _build(cls: type[T], data: dict[str, Any], path: str = "") -> T:
    hints = get_type_hints(cls)
    known = {f.name for f in fields(cls)}  # type: ignore[arg-type]
    unknown = set(data) - known
    if unknown:
        where = f" in [{path}]" if path else ""
        raise ConfigError(f"unknown config key(s){where}: {', '.join(sorted(unknown))}")
    kwargs = {k: _coerce(hints[k], v, f"{path}.{k}" if path else k) for k, v in data.items()}
    return cls(**kwargs)


def config_from_dict(data: dict[str, Any]) -> VigilConfig:
    return _build(VigilConfig, data)


def load_config(path: Path | str | None = None) -> VigilConfig:
    """Load a TOML config file; ``None`` returns the built-in defaults."""
    if path is None:
        return VigilConfig()
    p = Path(path)
    try:
        with p.open("rb") as fh:
            data = tomllib.load(fh)
    except FileNotFoundError:
        raise ConfigError(f"config file not found: {p}") from None
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"invalid TOML in {p}: {exc}") from None
    return config_from_dict(data)


def config_to_dict(config: VigilConfig) -> dict[str, Any]:
    def convert(obj: Any) -> Any:
        if isinstance(obj, Enum):
            return obj.value
        if isinstance(obj, dict):
            return {k: convert(v) for k, v in obj.items()}
        if isinstance(obj, list | tuple):
            return [convert(v) for v in obj]
        return obj

    result: dict[str, Any] = convert(asdict(config))
    return result
