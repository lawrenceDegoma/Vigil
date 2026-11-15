"""Pipeline orchestration: source → preprocess → motion → window → detectors → incidents.

The same ``Pipeline`` object is driven frame-by-frame by offline replay
(``run_pipeline``) and, from Phase 2, by the live capture loop. Observers
receive every intermediate product for logging, overlays, and plots.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Protocol

from vigil_edge.config import VigilConfig
from vigil_edge.detectors import Detector, build_detectors
from vigil_edge.incidents import IncidentStateMachine, Output
from vigil_edge.models import (
    DetectionResult,
    Frame,
    FrameFeatures,
    Image,
    Incident,
    IncidentEvent,
    MonitorState,
    ShadowDetection,
    StateChange,
    WindowFeatures,
)
from vigil_edge.motion import MotionDebug, MotionFeatureExtractor
from vigil_edge.preprocess import Preprocessor
from vigil_edge.profiles import Profile, get_profile
from vigil_edge.sources.base import FrameSource
from vigil_edge.windowing import WindowAnalyzer


@dataclass(frozen=True, slots=True)
class WindowRecord:
    features: WindowFeatures
    results: tuple[DetectionResult, ...]
    state: MonitorState

    def result(self, detector: str) -> DetectionResult | None:
        return next((r for r in self.results if r.detector == detector), None)


@dataclass(frozen=True, slots=True)
class FrameContext:
    frame: Frame
    gray: Image
    features: FrameFeatures | None
    debug: MotionDebug | None
    state: MonitorState
    last_window: WindowRecord | None


class PipelineObserver(Protocol):
    def on_frame(self, ctx: FrameContext) -> None: ...

    def on_window(self, record: WindowRecord) -> None: ...

    def on_output(self, output: Output) -> None: ...

    def close(self) -> None: ...


class NullObserver:
    """Convenience base: override only the hooks you need."""

    def on_frame(self, ctx: FrameContext) -> None:
        pass

    def on_window(self, record: WindowRecord) -> None:
        pass

    def on_output(self, output: Output) -> None:
        pass

    def close(self) -> None:
        pass


STAGES = ("preprocess", "motion", "window", "detect", "incident", "observers")


@dataclass
class PipelineStats:
    frames: int = 0
    windows: int = 0
    first_ts: float | None = None
    last_ts: float = 0.0
    stage_seconds: dict[str, float] = field(default_factory=lambda: dict.fromkeys(STAGES, 0.0))
    wall_seconds: float = 0.0

    @property
    def media_seconds(self) -> float:
        return 0.0 if self.first_ts is None else self.last_ts - self.first_ts

    @property
    def processing_fps(self) -> float:
        busy = sum(self.stage_seconds.values()) - self.stage_seconds["observers"]
        return self.frames / busy if busy > 0 else 0.0

    @property
    def realtime_factor(self) -> float:
        """Media seconds processed per second of analysis compute (>1 = faster than live)."""
        busy = sum(self.stage_seconds.values()) - self.stage_seconds["observers"]
        return self.media_seconds / busy if busy > 0 else 0.0


@dataclass
class PipelineResult:
    config: VigilConfig
    stats: PipelineStats
    frame_features: list[FrameFeatures]
    windows: list[WindowRecord]
    outputs: list[Output]
    incidents: list[Incident]

    @property
    def events(self) -> list[IncidentEvent]:
        return [o for o in self.outputs if isinstance(o, IncidentEvent)]

    @property
    def shadow_detections(self) -> list[ShadowDetection]:
        return [o for o in self.outputs if isinstance(o, ShadowDetection)]

    @property
    def state_changes(self) -> list[StateChange]:
        return [o for o in self.outputs if isinstance(o, StateChange)]


class Pipeline:
    def __init__(
        self,
        config: VigilConfig,
        frame_size: tuple[int, int],
        nominal_fps: float,
        *,
        observers: Sequence[PipelineObserver] = (),
        profile: Profile | None = None,
        detectors: Sequence[Detector] | None = None,
        id_factory: Callable[[], str] | None = None,
        keep_history: bool = True,
    ) -> None:
        self.config = config
        self.profile = profile or get_profile(config.subject.profile)
        self.preprocess = Preprocessor(config.preprocess, frame_size)
        self.motion = MotionFeatureExtractor(config.motion, self.preprocess.roi_mask, nominal_fps)
        self.windows = WindowAnalyzer(config.window)
        self.detectors = list(detectors) if detectors is not None else build_detectors(config)
        kwargs = {"id_factory": id_factory} if id_factory else {}
        self.incidents = IncidentStateMachine(
            self.detectors,
            self.profile,
            config.incident,
            config.window.seconds,
            config.rule_version,
            **kwargs,
        )
        self.observers = list(observers)
        self.stats = PipelineStats()
        self._keep = keep_history
        self.frame_features: list[FrameFeatures] = []
        self.window_records: list[WindowRecord] = []
        self.outputs: list[Output] = []
        self.last_window: WindowRecord | None = None

    def process(self, frame: Frame) -> list[Output]:
        st = self.stats.stage_seconds
        clock = time.perf_counter
        t0 = clock()
        gray = self.preprocess(frame.image)
        t1 = clock()
        feats = self.motion.process(gray, frame.timestamp, frame.index)
        t2 = clock()
        st["preprocess"] += t1 - t0
        st["motion"] += t2 - t1

        self.stats.frames += 1
        if self.stats.first_ts is None:
            self.stats.first_ts = frame.timestamp
        self.stats.last_ts = frame.timestamp

        outputs: list[Output] = []
        if feats is not None:
            if self._keep:
                self.frame_features.append(feats)
            t3 = clock()
            window = self.windows.push(feats)
            t4 = clock()
            st["window"] += t4 - t3
            if window is not None:
                outputs = self._handle_window(window)

        t5 = clock()
        ctx = FrameContext(
            frame, gray, feats, self.motion.last_debug, self.incidents.state, self.last_window
        )
        for obs in self.observers:
            obs.on_frame(ctx)
        st["observers"] += clock() - t5
        return outputs

    def _handle_window(self, window: WindowFeatures) -> list[Output]:
        st = self.stats.stage_seconds
        clock = time.perf_counter
        t0 = clock()
        results = tuple(d.evaluate(window) for d in self.detectors)
        t1 = clock()
        outputs = self.incidents.update(window.t_end, results, window)
        t2 = clock()
        st["detect"] += t1 - t0
        st["incident"] += t2 - t1
        record = WindowRecord(window, results, self.incidents.state)
        self.last_window = record
        self.stats.windows += 1
        if self._keep:
            self.window_records.append(record)
        self._emit(outputs)
        t3 = clock()
        for obs in self.observers:
            obs.on_window(record)
        st["observers"] += clock() - t3
        return outputs

    def _emit(self, outputs: list[Output]) -> None:
        self.outputs.extend(outputs)
        for out in outputs:
            for obs in self.observers:
                obs.on_output(out)

    def finish(self) -> PipelineResult:
        self._emit(self.incidents.finish(self.stats.last_ts))
        for obs in self.observers:
            obs.close()
        return PipelineResult(
            config=self.config,
            stats=self.stats,
            frame_features=self.frame_features,
            windows=self.window_records,
            outputs=self.outputs,
            incidents=self.incidents.incidents,
        )


def run_pipeline(
    source: FrameSource,
    config: VigilConfig,
    *,
    observers: Sequence[PipelineObserver] = (),
    id_factory: Callable[[], str] | None = None,
    max_frames: int | None = None,
    progress: Callable[[Frame], None] | None = None,
) -> PipelineResult:
    """Process a finite source end-to-end and return everything it produced."""
    pipeline = Pipeline(
        config, source.frame_size, source.fps, observers=observers, id_factory=id_factory
    )
    start = time.perf_counter()
    try:
        for frame in source.frames():
            pipeline.process(frame)
            if progress is not None:
                progress(frame)
            if max_frames is not None and pipeline.stats.frames >= max_frames:
                break
    finally:
        source.close()
    result = pipeline.finish()
    result.stats.wall_seconds = time.perf_counter() - start
    return result
