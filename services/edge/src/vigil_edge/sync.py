"""Cloud sync: deliver the durable outbox to the backend's ``device-ingest`` function.

The outbox (``store.py``) is the source of truth; this module only moves rows
out of it. Rows are marked sent only after the server answers ``accepted`` or
``duplicate`` for them, so a crash, timeout or outage just means a retry, and
the server's idempotency (event_id) makes retries harmless.

* Network failures and 5xx: exponential backoff; nothing is marked.
* 401: the device token is wrong or revoked; long backoff, loud log.
* 413: the batch was too large; halve the batch size and retry.
* Per-message ``rejected``: the attempt counter goes up; after
  ``max_rejections`` the message is parked (skipped, still counted in
  ``queue_depth`` so the dashboard shows something is stuck). Rejections are a
  contract bug, not a transient condition, so retrying them forever would only
  block the queue.

Detection never waits on any of this: sync runs in its own thread.
"""

from __future__ import annotations

import json
import logging
import threading
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from vigil_edge import __version__
from vigil_edge.clock import Clock
from vigil_edge.config import CloudConfig
from vigil_edge.store import LocalStore

log = logging.getLogger(__name__)

# (url, body, headers, timeout) -> (status, response body). Raises SyncError on
# transport failure (DNS, refused, TLS, timeout).
Transport = Callable[[str, bytes, dict[str, str], float], tuple[int, bytes]]


class SyncError(Exception):
    pass


def urllib_transport(
    url: str, body: bytes, headers: dict[str, str], timeout: float
) -> tuple[int, bytes]:
    req = urllib.request.Request(url, data=body, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # https enforced by CloudConfig
            return int(resp.status), resp.read()
    except urllib.error.HTTPError as exc:
        return int(exc.code), exc.read()
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise SyncError(f"{type(exc).__name__}: {getattr(exc, 'reason', exc)}") from None


@dataclass(frozen=True)
class SyncResult:
    status: str  # idle | ok | error | unauthorized
    sent: int = 0
    rejected: int = 0
    attempted: int = 0


class CloudSync:
    def __init__(
        self,
        store: LocalStore,
        cfg: CloudConfig,
        token: str,
        clock: Clock,
        transport: Transport = urllib_transport,
    ) -> None:
        if not token:
            raise ValueError("cloud sync needs a device token (VIGIL_DEVICE_TOKEN)")
        self.store = store
        self.cfg = cfg
        self.clock = clock
        self.url = cfg.endpoint.rstrip("/") + "/device-ingest"
        self._token = token
        self._transport = transport
        self.batch_size = cfg.batch_size
        self.failures = 0
        self.next_attempt = 0.0  # monotonic
        self.last_success: float | None = None
        self.wake = threading.Event()

    # ------------------------------------------------------------- one round

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self._token}",
            "Content-Type": "application/json",
            "User-Agent": f"vigil-edge/{__version__}",
        }

    def _backoff(self, seconds: float | None = None) -> None:
        self.failures += 1
        delay = seconds or min(
            self.cfg.max_backoff_seconds, self.cfg.interval_seconds * 2 ** (self.failures - 1)
        )
        self.next_attempt = self.clock.monotonic() + delay

    def sync_once(self) -> SyncResult:
        msgs = self.store.pending(self.batch_size, max_attempts=self.cfg.max_rejections)
        if not msgs:
            return SyncResult("idle")
        payload = {"messages": [{"kind": m.kind, "payload": m.payload} for m in msgs]}
        body = json.dumps(payload, separators=(",", ":")).encode()
        try:
            status, raw = self._transport(self.url, body, self._headers(), self.cfg.timeout_seconds)
        except SyncError as exc:
            self._backoff()
            log.warning("cloud sync failed (%s); %d queued, retry later", exc, len(msgs))
            return SyncResult("error", attempted=len(msgs))
        if status == 401:
            self._backoff(self.cfg.max_backoff_seconds)
            log.error("cloud sync unauthorized: check VIGIL_DEVICE_TOKEN / device revocation")
            return SyncResult("unauthorized", attempted=len(msgs))
        if status == 413 and self.batch_size > 1:
            self.batch_size = max(1, self.batch_size // 2)
            log.warning("cloud sync batch too large; batch size now %d", self.batch_size)
            return SyncResult("error", attempted=len(msgs))
        results = self._parse(status, raw, len(msgs))
        if results is None:
            self._backoff()
            log.warning("cloud sync got HTTP %d; %d queued, retry later", status, len(msgs))
            return SyncResult("error", attempted=len(msgs))

        delivered: list[int] = []
        rejected: list[int] = []
        for r in results:
            m = msgs[r["index"]]
            if r["status"] in ("accepted", "duplicate"):
                delivered.append(m.seq)
            else:
                rejected.append(m.seq)
                log.error(
                    "cloud rejected %s #%d (attempt %d/%d): %s",
                    m.kind,
                    m.seq,
                    m.attempts + 1,
                    self.cfg.max_rejections,
                    str(r.get("reason", ""))[:200],
                )
        self.store.mark_sent(delivered, self.clock.now_utc())
        self.store.record_attempt(rejected)
        self.failures = 0
        self.next_attempt = 0.0
        self.last_success = self.clock.monotonic()
        return SyncResult("ok", sent=len(delivered), rejected=len(rejected), attempted=len(msgs))

    @staticmethod
    def _parse(status: int, raw: bytes, n: int) -> list[dict[str, Any]] | None:
        if status != 200:
            return None
        try:
            results = json.loads(raw)["results"]
        except (ValueError, KeyError, TypeError):
            return None
        if not isinstance(results, list) or len(results) != n:
            return None
        ok = all(
            isinstance(r, dict) and isinstance(r.get("index"), int) and 0 <= r["index"] < n
            for r in results
        )
        return results if ok else None

    # --------------------------------------------------------------- driving

    def run(self, stop: threading.Event) -> None:
        """Sync until ``stop`` is set.

        Rounds happen every ``interval_seconds``, or after the backoff delay
        following a failure. ``wake`` (set when an incident event is queued)
        forces an immediate round even during backoff: alerts are worth an
        extra attempt.
        """
        woken = True
        while not stop.is_set():
            if woken or self.clock.monotonic() >= self.next_attempt:
                try:
                    r = self.sync_once()
                    if r.status == "ok" and r.attempted >= self.batch_size:
                        continue  # more queued: keep draining
                except Exception:  # never let the sync thread die
                    log.exception("cloud sync crashed; retrying")
                    self._backoff()
            delay = self.next_attempt - self.clock.monotonic()
            timeout = delay if delay > 0 else self.cfg.interval_seconds
            woken = self.wake.wait(timeout=timeout)
            self.wake.clear()

    def flush(self, max_rounds: int = 5) -> None:
        """Best-effort delivery at shutdown (bounded; ignores backoff)."""
        for _ in range(max_rounds):
            r = self.sync_once()
            if r.status != "ok" or r.attempted < self.batch_size:
                return
