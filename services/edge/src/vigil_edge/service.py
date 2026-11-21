"""The long-running edge service (``vigil run``).

Structure:

* **Capture loop** (main thread): open the source, push frames through the
  pipeline, route state-machine outputs to the clip manager and the durable
  outbox. Camera failures end the *session*; the loop reconnects with
  exponential backoff. Open incidents are closed with ``stream_end`` when a
  session ends, so state never silently persists across a camera outage.
* **Cloud sync thread** (optional, ``[cloud]``): delivers the outbox to the
  backend (``sync.py``). Incident events wake it immediately; detection never
  waits on the network.
* **Housekeeping thread**: heartbeats (local file + outbox), retention, the
  maintenance flag, outbox bounds, and the systemd watchdog. It runs even
  when the capture loop is blocked, so a stalled camera is *reported*, and
  the watchdog is only fed while the capture loop is making progress, so a
  hung camera stack gets the process restarted by systemd.

Maintenance mode (``<data_dir>/state/maintenance`` exists): analysis keeps
running and is logged, but no incidents are exported and no clips recorded;
heartbeats say ``maintenance: true``.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from vigil_edge import systemd
from vigil_edge.clip_upload import ClipUploader, Putter, urllib_put
from vigil_edge.clock import Clock, SystemClock
from vigil_edge.config import VigilConfig
from vigil_edge.contracts import incident_event_message
from vigil_edge.health import HealthMonitor
from vigil_edge.incidents import Output
from vigil_edge.models import IncidentEvent, ShadowDetection, StateChange
from vigil_edge.outputs import output_to_dict
from vigil_edge.pipeline import Pipeline
from vigil_edge.recording import ClipBackend, ClipManager, SoftwareRingBackend
from vigil_edge.retention import GB, RetentionManager, RetentionPolicy
from vigil_edge.sources.base import FrameSource, SourceError
from vigil_edge.store import LocalStore, utc_iso
from vigil_edge.sync import CloudSync, Transport, urllib_transport

log = logging.getLogger(__name__)

SourceFactory = Callable[[], FrameSource]
BackendFactory = Callable[[FrameSource, VigilConfig], ClipBackend]


@dataclass(frozen=True)
class ServicePaths:
    data_dir: Path

    @property
    def clips_dir(self) -> Path:
        return self.data_dir / "clips"

    @property
    def state_dir(self) -> Path:
        return self.data_dir / "state"

    @property
    def db(self) -> Path:
        return self.data_dir / "vigil.db"

    @property
    def maintenance_flag(self) -> Path:
        return self.state_dir / "maintenance"

    @property
    def heartbeat_file(self) -> Path:
        return self.state_dir / "heartbeat.json"


def default_backend(source: FrameSource, cfg: VigilConfig) -> ClipBackend:
    make = getattr(source, "clip_backend", None)
    if callable(make):  # Picamera2 hardware encoder
        backend: ClipBackend = make()
        return backend
    r = cfg.recording
    return SoftwareRingBackend(
        r.pre_event_seconds, source.fps, r.software_width, r.software_jpeg_quality
    )


def write_json_atomic(path: Path, data: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2))
    os.replace(tmp, path)


class EdgeService:
    def __init__(
        self,
        config: VigilConfig,
        source_factory: SourceFactory,
        *,
        clock: Clock | None = None,
        backend_factory: BackendFactory = default_backend,
        sleep: Callable[[float], None] = time.sleep,
        notify: Callable[[str], bool] = systemd.notify,
        cloud_token: str | None = None,
        cloud_transport: Transport = urllib_transport,
        cloud_put: Putter = urllib_put,
    ) -> None:
        self.cfg = config
        self.svc = config.service
        self.paths = ServicePaths(Path(self.svc.data_dir))
        for d in (self.paths.clips_dir, self.paths.state_dir):
            d.mkdir(parents=True, exist_ok=True)
        self.clock: Clock = clock or SystemClock()
        self.source_factory = source_factory
        self.backend_factory = backend_factory
        self._sleep = sleep
        self._notify = notify
        self.store = LocalStore(self.paths.db)
        window_stall = max(3 * config.window.step_seconds + config.window.seconds, 10.0)
        self.health = HealthMonitor(
            self.clock, self.svc.camera_stall_seconds, window_stall, config.rule_version
        )
        self.retention = RetentionManager(
            self.store,
            self.paths.clips_dir,
            RetentionPolicy(
                int(config.recording.disk_quota_gb * GB), int(config.recording.min_free_gb * GB)
            ),
        )
        self.stop_event = threading.Event()
        self._progress = self.clock.monotonic()
        self._idle_until = 0.0
        self._next_heartbeat = 0.0
        self._next_housekeeping = 0.0
        self.sessions = 0
        self.exported_events = 0
        self.sync: CloudSync | None = None
        self.uploader: ClipUploader | None = None
        self._uploader_wake = threading.Event()
        if config.cloud.enabled:
            if cloud_token:
                self.sync = CloudSync(
                    self.store, config.cloud, cloud_token, self.clock, cloud_transport
                )
                if config.cloud.upload_clips and config.recording.enabled:
                    self.uploader = ClipUploader(
                        self.store,
                        config.cloud,
                        cloud_token,
                        self.clock,
                        cloud_transport,
                        cloud_put,
                    )
            else:
                log.error("cloud.enabled but VIGIL_DEVICE_TOKEN is not set; cloud sync disabled")

    # ---------------------------------------------------------------- loop

    def run(self, max_sessions: int | None = None, housekeeping_thread: bool = True) -> None:
        self.housekeeping_tick(force=True)  # maintenance flag, retention, first heartbeat
        self._notify("READY=1")
        hk: threading.Thread | None = None
        if housekeeping_thread:
            hk = threading.Thread(target=self._housekeeping_loop, name="housekeeping", daemon=True)
            hk.start()
        sync_thread: threading.Thread | None = None
        if self.sync is not None and housekeeping_thread:
            sync_thread = threading.Thread(
                target=self.sync.run, args=(self.stop_event,), name="cloud-sync", daemon=True
            )
            sync_thread.start()
        upload_thread: threading.Thread | None = None
        if self.uploader is not None and housekeeping_thread:
            upload_thread = threading.Thread(
                target=self.uploader.run,
                args=(self.stop_event, self._uploader_wake),
                name="clip-upload",
                daemon=True,
            )
            upload_thread.start()
        backoff = self.svc.reconnect_min_seconds
        try:
            while not self.stop_event.is_set():
                if max_sessions is not None and self.sessions >= max_sessions:
                    break
                self._progress = self.clock.monotonic()
                try:
                    source = self.source_factory()
                except SourceError as exc:
                    self.health.camera_error(str(exc))
                    log.error("camera unavailable: %s (retry in %.0fs)", exc, backoff)
                    self.sessions += 1
                    self._wait(backoff)
                    backoff = min(backoff * 2, self.svc.reconnect_max_seconds)
                    continue
                self.sessions += 1
                ended_cleanly = False
                try:
                    self._run_session(source)
                    ended_cleanly = True
                except SourceError as exc:
                    self.health.camera_error(str(exc))
                    log.error("camera session ended: %s", exc)
                except Exception as exc:
                    self.health.detector_error(f"{type(exc).__name__}: {exc}")
                    log.exception("analysis session crashed; restarting session")
                finally:
                    source.close()
                if ended_cleanly and not source.is_live:
                    log.info("finite source exhausted; stopping")
                    break
                if ended_cleanly:
                    backoff = self.svc.reconnect_min_seconds
                if not self.stop_event.is_set():
                    self._wait(backoff)
                    backoff = min(backoff * 2, self.svc.reconnect_max_seconds)
        finally:
            self.stop_event.set()
            self._notify("STOPPING=1")
            if hk is not None:
                hk.join(timeout=5)
            if self.sync is not None:
                self.sync.wake.set()
                if sync_thread is not None:
                    sync_thread.join(timeout=5)
            if upload_thread is not None:
                self._uploader_wake.set()
                upload_thread.join(timeout=5)  # an unfinished upload resumes next start
            self.housekeeping_tick(force=True)
            if self.sync is not None:
                try:
                    self.sync.flush()  # best effort: final events + heartbeat
                except Exception:
                    log.exception("final cloud flush failed")
            self.store.close()

    def _wait(self, seconds: float) -> None:
        # An intentional backoff is progress: don't let the watchdog restart us for it.
        self._idle_until = self.clock.monotonic() + seconds + 1.0
        if self._sleep is time.sleep:
            self.stop_event.wait(seconds)  # wakes early on shutdown
        else:
            self._sleep(seconds)

    def _run_session(self, source: FrameSource) -> None:
        cfg = self.cfg
        started = self.clock.now_utc()
        pipeline = Pipeline(cfg, source.frame_size, source.fps, keep_history=False)
        backend = self.backend_factory(source, cfg) if cfg.recording.enabled else None
        clips = (
            ClipManager(
                backend,
                self.store,
                cfg.recording,
                self.paths.clips_dir,
                started,
                self.clock.now_utc,
            )
            if backend is not None
            else None
        )
        log.info(
            "session %d started: %dx%d @ %.1f fps, rules %s",
            self.sessions,
            *source.frame_size,
            source.fps,
            cfg.rule_version,
        )
        last_t = 0.0
        dropped_seen = 0
        windows_seen = 0
        try:
            for frame in source.frames():
                if self.stop_event.is_set():
                    break
                t0 = time.perf_counter()
                if backend is not None:
                    backend.push(frame)
                outputs = pipeline.process(frame)
                dropped = int(getattr(source, "dropped_frames", 0))
                self.health.frame(time.perf_counter() - t0, dropped - dropped_seen)
                dropped_seen = dropped
                if pipeline.stats.windows != windows_seen:
                    windows_seen = pipeline.stats.windows
                    self.health.window()
                self._progress = self.clock.monotonic()
                last_t = frame.timestamp
                for out in outputs:
                    self._handle(out, started, clips, frame.timestamp)
                if clips is not None:
                    clips.tick(frame.timestamp)
        finally:
            for out in pipeline.incidents.finish(last_t):  # close incidents: stream_end
                self._handle(out, started, clips, last_t)
            if clips is not None:
                clips.close(last_t)
            if backend is not None:
                backend.close()

    def _handle(self, out: Output, started: datetime, clips: ClipManager | None, t: float) -> None:
        record = output_to_dict(out)
        maintenance = self.health.maintenance
        if isinstance(out, StateChange):
            log.info("state %s -> %s (%s)", out.previous.value, out.current.value, out.reason)
            return
        if maintenance:
            log.info("maintenance mode, not exported: %s", record["kind"])
            return
        if isinstance(out, IncidentEvent):
            msg = incident_event_message(out, self.svc.device_id, started)
            if self.store.enqueue("incident_event", msg, self.clock.now_utc(), out.event_id):
                self.exported_events += 1
                if self.sync is not None:
                    self.sync.wake.set()  # alerts go out now, not at the next poll
            log.warning(
                "%s %s/%s peak=%.2f",
                record["kind"],
                record["signal_type"],
                record["severity"],
                record["peak_score"],
            )
        elif isinstance(out, ShadowDetection):
            log.info(
                "shadow detection (experimental, not alerted): %s peak=%.2f",
                out.signal_type.value,
                out.peak_score,
            )
        if clips is not None:
            clips.handle(out, t)

    # --------------------------------------------------------- housekeeping

    def _housekeeping_loop(self) -> None:
        while not self.stop_event.wait(1.0):
            try:
                self.housekeeping_tick()
            except Exception:  # never let housekeeping die silently
                log.exception("housekeeping failed")

    def housekeeping_tick(self, force: bool = False) -> None:
        now = self.clock.monotonic()
        self.health.maintenance = self.paths.maintenance_flag.exists()
        watchdog_window = max(self.svc.camera_stall_seconds * 3, 15.0)
        alive = now - self._progress < watchdog_window or now <= self._idle_until
        if systemd.watchdog_enabled() and alive:
            self._notify("WATCHDOG=1")
        if force or now >= self._next_housekeeping:
            self._next_housekeeping = now + self.svc.housekeeping_seconds
            plan = self.retention.run(self.clock.now_utc())
            self.health.storage_warnings = plan.warnings
        if force or now >= self._next_heartbeat:
            self._next_heartbeat = now + self.svc.heartbeat_seconds
            self.emit_heartbeat()

    def emit_heartbeat(self) -> dict[str, object]:
        hb = self.health.heartbeat(
            self.svc.device_id,
            self.paths.data_dir,
            self.store.clips_bytes(),
            self.store.queue_depth(),
        )
        if self.health.storage_warnings:
            hb["storage"]["warnings"] = list(self.health.storage_warnings)
        write_json_atomic(self.paths.heartbeat_file, hb)
        self.store.enqueue("heartbeat", hb, self.clock.now_utc())
        self.store.trim_outbox(self.svc.outbox_max_rows)
        cam, det = hb["camera"], hb["detector"]
        self._notify(
            f"STATUS=camera {'ok' if cam['ok'] else 'FAIL'} {cam['fps']}fps, "
            f"detector {'ok' if det['ok'] else 'FAIL'}"
            f"{', MAINTENANCE' if hb['maintenance'] else ''}"
        )
        log.info(
            "heartbeat: camera=%s detector=%s fps=%s queue=%s at %s",
            cam["ok"],
            det["ok"],
            cam["fps"],
            hb["queue_depth"],
            utc_iso(self.clock.now_utc()),
        )
        return hb
