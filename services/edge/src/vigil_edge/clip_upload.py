"""Upload incident clips to the backend (ADR-010 option b), in its own thread.

Only clips recorded for incidents are considered, and the server accepts them
only if one of their incidents was alerted, so shadow-mode clips and ordinary
footage never leave the device. A clip waits until every incident event in the
outbox has been delivered, so the server knows its incidents.

Flow per clip: optional split into parts no larger than ``max_upload_mb`` (ffmpeg
stream copy, no re-encoding) -> ``register`` -> PUT each part to its signed
upload URL -> ``complete`` (the server checks each part's size in storage).
Uploads never block alert delivery, which runs in the CloudSync thread.
"""

from __future__ import annotations

import json
import logging
import math
import shutil
import subprocess
import tempfile
import threading
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from vigil_edge import __version__
from vigil_edge.clock import Clock
from vigil_edge.config import CloudConfig
from vigil_edge.recording import sha256_file
from vigil_edge.store import ClipRecord, LocalStore, utc_iso
from vigil_edge.sync import SyncError, Transport, urllib_transport

log = logging.getLogger(__name__)

MB = 1024 * 1024
MAX_ATTEMPTS = 10
MAX_PARTS = 50  # server limit per clip

# (url, file, size, headers, timeout) -> HTTP status
Putter = Callable[[str, Path, int, dict[str, str], float], int]
# (clip file, segment seconds, output dir) -> part files in order
Splitter = Callable[[Path, float, Path], list[Path]]


def urllib_put(url: str, path: Path, size: int, headers: dict[str, str], timeout: float) -> int:
    import urllib.error
    import urllib.request

    with path.open("rb") as fh:
        req = urllib.request.Request(
            url, data=fh, method="PUT", headers={**headers, "Content-Length": str(size)}
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:  # https enforced by config
                return int(resp.status)
        except urllib.error.HTTPError as exc:
            return int(exc.code)
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise SyncError(f"{type(exc).__name__}: {getattr(exc, 'reason', exc)}") from None


def ffmpeg_split(src: Path, seconds: float, out_dir: Path) -> list[Path]:
    """Cut at keyframes into playable MP4 parts without re-encoding."""
    if shutil.which("ffmpeg") is None:
        raise RuntimeError("ffmpeg is required to split large clips")
    result = subprocess.run(
        [
            "ffmpeg", "-loglevel", "error", "-y", "-i", str(src), "-map", "0", "-c", "copy",
            "-f", "segment", "-segment_time", f"{seconds:.1f}", "-reset_timestamps", "1",
            "-segment_format", "mp4", "-segment_format_options", "movflags=+faststart",
            str(out_dir / "part-%03d.mp4"),
        ],
        capture_output=True, text=True, timeout=300, check=False,
    )  # fmt: skip
    if result.returncode != 0:
        raise RuntimeError(f"ffmpeg split failed: {result.stderr.strip()[:200]}")
    return sorted(out_dir.glob("part-*.mp4"))


@dataclass(frozen=True)
class Part:
    index: int
    path: Path
    bytes: int
    sha256: str


class ClipUploader:
    def __init__(
        self,
        store: LocalStore,
        cfg: CloudConfig,
        token: str,
        clock: Clock,
        transport: Transport = urllib_transport,
        put: Putter = urllib_put,
        splitter: Splitter = ffmpeg_split,
    ) -> None:
        self.store = store
        self.cfg = cfg
        self.clock = clock
        self._token = token
        self._transport = transport
        self._put = put
        self._split = splitter
        endpoint = cfg.endpoint.rstrip("/")
        self.url = endpoint + "/clip-upload"
        # Signed URLs come back relative to the project ("/storage/v1/...").
        self.base = endpoint.removesuffix("/functions/v1")
        self.max_bytes = int(cfg.max_upload_mb * MB)
        self.failures = 0
        self.next_attempt = 0.0

    # ------------------------------------------------------------------ http

    def _post(self, body: dict[str, Any]) -> tuple[int, dict[str, Any]]:
        headers = {
            "Authorization": f"Bearer {self._token}",
            "Content-Type": "application/json",
            "User-Agent": f"vigil-edge/{__version__}",
        }
        status, raw = self._transport(
            self.url, json.dumps(body).encode(), headers, self.cfg.timeout_seconds
        )
        try:
            data = json.loads(raw) if raw else {}
        except ValueError:
            data = {}
        return status, data if isinstance(data, dict) else {}

    # ----------------------------------------------------------------- parts

    def _parts(self, clip: ClipRecord, path: Path, workdir: Path) -> list[Part]:
        if clip.bytes <= self.max_bytes:
            return [Part(0, path, clip.bytes, clip.sha256)]
        duration = max(1.0, (clip.ended_at - clip.started_at).total_seconds())
        files = self._fit(path, duration, clip.bytes, workdir, depth=0)
        if len(files) > MAX_PARTS:
            raise ValueError(f"clip would need {len(files)} parts (max {MAX_PARTS})")
        return [Part(i, f, f.stat().st_size, sha256_file(f)) for i, f in enumerate(files)]

    def _fit(self, src: Path, duration: float, size: int, workdir: Path, depth: int) -> list[Path]:
        """Split ``src`` until every part fits. Only parts that are still too large are
        split again (encoders are bursty: the start of a clip often has more bits)."""
        if size <= self.max_bytes:
            return [src]
        if depth >= 4:
            raise ValueError("could not split clip into parts under the upload limit")
        seconds = max(1.0, math.floor(duration * self.max_bytes / size * 0.85))
        out_dir = workdir / f"split-{depth}-{len(list(workdir.iterdir()))}"
        out_dir.mkdir()
        files = self._split(src, seconds, out_dir)
        if len(files) < 2:
            raise ValueError("clip cannot be cut further (keyframes too far apart)")
        fitted: list[Path] = []
        for f in files:
            fitted += self._fit(f, seconds, f.stat().st_size, workdir, depth + 1)
        return fitted

    # ---------------------------------------------------------------- upload

    def _give_up_or_retry(self, clip: ClipRecord, why: str) -> str:
        n = self.store.bump_cloud_attempts(clip.id)
        if n >= MAX_ATTEMPTS:
            self.store.set_cloud_state(clip.id, f"skipped:{why}")
            log.error("clip %s: giving up after %d attempts (%s)", clip.id, n, why)
            return "skipped"
        self._backoff()
        return "retry"

    def _backoff(self) -> None:
        self.failures += 1
        delay = min(self.cfg.max_backoff_seconds, 30.0 * 2 ** (self.failures - 1))
        self.next_attempt = self.clock.monotonic() + delay

    def upload_next(self) -> str:
        """Handle one clip. Returns idle | waiting | uploaded | skipped | retry."""
        if self.store.pending_count("incident_event") > 0:
            return "waiting"  # deliver incident events first: the server must know them
        clips = self.store.clips_to_upload(1)
        if not clips:
            return "idle"
        clip = clips[0]
        path = Path(clip.path)
        if not path.is_file():
            self.store.set_cloud_state(clip.id, "skipped:missing")
            return "skipped"
        with tempfile.TemporaryDirectory(prefix="vigil-upload-", dir=path.parent) as tmp:
            try:
                parts = self._parts(clip, path, Path(tmp))
                return self._upload(clip, parts)
            except SyncError as exc:
                log.warning("clip %s upload interrupted (%s); will retry", clip.id, exc)
                return self._give_up_or_retry(clip, "network")
            except (OSError, RuntimeError, ValueError) as exc:
                log.error("clip %s cannot be prepared: %s", clip.id, exc)
                return self._give_up_or_retry(clip, "prepare")

    def _upload(self, clip: ClipRecord, parts: list[Part]) -> str:
        status, reg = self._post(
            {
                "action": "register",
                "clip": {
                    "id": clip.id,
                    "incident_ids": list(clip.incident_ids),
                    "started_at": utc_iso(clip.started_at),
                    "ended_at": utc_iso(clip.ended_at),
                    "bytes": clip.bytes,
                    "sha256": clip.sha256,
                    "parts": [
                        {"index": p.index, "bytes": p.bytes, "sha256": p.sha256} for p in parts
                    ],
                },
            }
        )
        outcome = reg.get("status")
        if status == 401:
            log.error("clip upload unauthorized: check VIGIL_DEVICE_TOKEN")
            self._backoff()
            return "retry"
        if status != 200:
            return self._give_up_or_retry(clip, f"http{status}")
        if outcome in ("disabled", "not_eligible"):
            state = "skipped:disabled" if outcome == "disabled" else "skipped:not_alerted"
            self.store.set_cloud_state(clip.id, state)
            log.info("clip %s stays on the device (%s)", clip.id, outcome)
            return "skipped"
        if outcome == "unknown_incident":
            return self._give_up_or_retry(clip, "unknown_incident")
        if outcome != "ok":
            log.error("clip %s rejected: %s", clip.id, reg.get("reason"))
            self.store.set_cloud_state(clip.id, "skipped:rejected")
            return "skipped"

        urls = {int(p["index"]): p.get("upload_url") for p in reg.get("parts", [])}
        for part in parts:
            url = urls.get(part.index)
            if url:
                code = self._put(
                    self.base + url,
                    part.path,
                    part.bytes,
                    {"Content-Type": "video/mp4", "x-upsert": "true"},
                    max(60.0, part.bytes / (256 * 1024)),  # allow ~2 Mbit/s uplinks
                )
                if code not in (200, 201):
                    return self._give_up_or_retry(clip, f"put{code}")
            status, done = self._post(
                {"action": "complete", "clip_id": clip.id, "part_index": part.index}
            )
            if status != 200 or done.get("status") != "ok":
                return self._give_up_or_retry(clip, str(done.get("status") or f"http{status}"))
        self.store.set_cloud_state(clip.id, "uploaded")
        self.failures = 0
        self.next_attempt = 0.0
        log.info("clip %s uploaded (%d part%s)", clip.id, len(parts), "s" * (len(parts) > 1))
        return "uploaded"

    # --------------------------------------------------------------- driving

    def run(self, stop: threading.Event, wake: threading.Event | None = None) -> None:
        idle_wait = max(self.cfg.interval_seconds, 15.0)
        while not stop.is_set():
            r = "retry"
            if self.clock.monotonic() >= self.next_attempt:
                try:
                    r = self.upload_next()
                except Exception:  # never let the uploader die
                    log.exception("clip uploader crashed; retrying later")
                    self._backoff()
            if r in ("uploaded", "skipped"):
                continue  # next clip right away
            delay = self.next_attempt - self.clock.monotonic()
            (wake or stop).wait(timeout=delay if delay > 0 else idle_wait)
            if wake is not None:
                wake.clear()
