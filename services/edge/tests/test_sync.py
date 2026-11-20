"""Cloud sync: the outbox is only marked sent after the server confirms each row."""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from typing import Any

import pytest

from helpers import T0
from vigil_edge.clock import FakeClock
from vigil_edge.config import CloudConfig, ConfigError, config_from_dict
from vigil_edge.service import EdgeService
from vigil_edge.sources.synthetic import SyntheticSource, get_scenario
from vigil_edge.store import LocalStore
from vigil_edge.sync import CloudSync, SyncError

TOKEN = "vgl_" + "a" * 43
CFG = CloudConfig(enabled=True, endpoint="http://127.0.0.1:54321/functions/v1/")


class FakeServer:
    """Stands in for device-ingest. ``reply`` maps the request to (status, body)."""

    def __init__(self, reply: Any = None) -> None:
        self.requests: list[dict[str, Any]] = []
        self.reply = reply or self.accept_all
        self.lock = threading.Lock()

    @staticmethod
    def accept_all(msgs: list[dict[str, Any]]) -> tuple[int, bytes]:
        results = [{"index": i, "status": "accepted"} for i in range(len(msgs))]
        return 200, json.dumps({"results": results}).encode()

    def __call__(
        self, url: str, body: bytes, headers: dict[str, str], timeout: float
    ) -> tuple[int, bytes]:
        msgs = json.loads(body)["messages"]
        with self.lock:
            self.requests.append({"url": url, "headers": headers, "messages": msgs})
        out = self.reply(msgs)
        if isinstance(out, Exception):
            raise out
        status, raw = out
        return int(status), bytes(raw)


def store_with(tmp_path: Path, n_events: int = 2, n_heartbeats: int = 1) -> LocalStore:
    s = LocalStore(tmp_path / "v.db")
    for i in range(n_events):
        s.enqueue("incident_event", {"event_id": f"e{i}"}, T0, f"e{i}")
    for i in range(n_heartbeats):
        s.enqueue("heartbeat", {"n": i}, T0)
    return s


def test_accepted_and_duplicate_rows_are_marked_sent(tmp_path: Path) -> None:
    def reply(msgs: list[dict[str, Any]]) -> tuple[int, bytes]:
        statuses = ["accepted", "duplicate", "rejected"]
        results = [{"index": i, "status": statuses[i], "reason": "bad"} for i in range(len(msgs))]
        return 200, json.dumps({"results": results}).encode()

    store = store_with(tmp_path)
    server = FakeServer(reply)
    sync = CloudSync(store, CFG, TOKEN, FakeClock(T0), server)
    r = sync.sync_once()
    assert (r.status, r.sent, r.rejected) == ("ok", 2, 1)
    [left] = store.pending()
    assert left.kind == "heartbeat" and left.attempts == 1
    req = server.requests[0]
    assert req["url"] == "http://127.0.0.1:54321/functions/v1/device-ingest"
    assert req["headers"]["Authorization"] == f"Bearer {TOKEN}"
    assert [m["kind"] for m in req["messages"]] == ["incident_event", "incident_event", "heartbeat"]


def test_repeatedly_rejected_messages_are_parked_not_dropped(tmp_path: Path) -> None:
    def reject(msgs: list[dict[str, Any]]) -> tuple[int, bytes]:
        results = [{"index": i, "status": "rejected"} for i in range(len(msgs))]
        return 200, json.dumps({"results": results}).encode()

    store = store_with(tmp_path, n_events=1, n_heartbeats=0)
    sync = CloudSync(store, CloudConfig(**{**CFG.__dict__, "max_rejections": 3}), TOKEN,
                     FakeClock(T0), FakeServer(reject))  # fmt: skip
    for _ in range(3):
        assert sync.sync_once().status == "ok"
    assert sync.sync_once().status == "idle"  # parked
    assert store.queue_depth() == 1  # still visible as stuck
    store.enqueue("heartbeat", {"n": 9}, T0)
    assert [m.kind for m in store.pending(10, max_attempts=3)] == ["heartbeat"]


def test_outages_back_off_and_mark_nothing(tmp_path: Path) -> None:
    store = store_with(tmp_path)
    clock = FakeClock(T0)
    sync = CloudSync(store, CFG, TOKEN, clock, FakeServer(lambda m: SyncError("refused")))
    delays = []
    for _ in range(9):
        assert sync.sync_once().status == "error"
        delays.append(sync.next_attempt - clock.monotonic())
    assert delays[:4] == [5, 10, 20, 40]
    assert max(delays) == CFG.max_backoff_seconds
    assert store.queue_depth() == 3
    sync._transport = FakeServer()
    assert sync.sync_once().sent == 3
    assert sync.failures == 0 and sync.next_attempt == 0


@pytest.mark.parametrize(
    ("status", "body"),
    [(500, b"{}"), (200, b"not json"), (200, b'{"results": []}'), (502, b"")],
)
def test_bad_responses_mark_nothing(tmp_path: Path, status: int, body: bytes) -> None:
    store = store_with(tmp_path)
    sync = CloudSync(store, CFG, TOKEN, FakeClock(T0), FakeServer(lambda m: (status, body)))
    assert sync.sync_once().status == "error"
    assert store.queue_depth() == 3


def test_unauthorized_waits_the_maximum(tmp_path: Path) -> None:
    store = store_with(tmp_path)
    clock = FakeClock(T0)
    sync = CloudSync(store, CFG, TOKEN, clock, FakeServer(lambda m: (401, b"{}")))
    assert sync.sync_once().status == "unauthorized"
    assert sync.next_attempt - clock.monotonic() == CFG.max_backoff_seconds


def test_payload_too_large_halves_the_batch(tmp_path: Path) -> None:
    store = store_with(tmp_path, n_events=6, n_heartbeats=0)
    server = FakeServer(lambda m: (413, b"") if len(m) > 2 else FakeServer.accept_all(m))
    sync = CloudSync(store, CloudConfig(**{**CFG.__dict__, "batch_size": 8}), TOKEN,
                     FakeClock(T0), server)  # fmt: skip
    statuses = [sync.sync_once().status for _ in range(6)]
    assert sync.batch_size == 2
    assert statuses[:2] == ["error", "error"] and store.queue_depth() == 0


def test_wake_sends_immediately_and_stop_ends_the_thread(tmp_path: Path) -> None:
    store = store_with(tmp_path, n_events=0, n_heartbeats=0)
    server = FakeServer()
    cfg = CloudConfig(**{**CFG.__dict__, "interval_seconds": 30.0})
    sync = CloudSync(store, cfg, TOKEN, FakeClock(T0), server)
    stop = threading.Event()
    th = threading.Thread(target=sync.run, args=(stop,), daemon=True)
    th.start()
    time.sleep(0.05)
    store.enqueue("incident_event", {"event_id": "x"}, T0, "x")
    sync.wake.set()  # what the service does when it queues an incident event
    deadline = time.monotonic() + 2
    while store.queue_depth() and time.monotonic() < deadline:
        time.sleep(0.01)
    assert store.queue_depth() == 0, "woken sync did not wait for the 30 s interval"
    stop.set()
    sync.wake.set()
    th.join(timeout=2)
    assert not th.is_alive()


def test_config_requires_https_except_localhost() -> None:
    with pytest.raises(ConfigError, match="https"):
        CloudConfig(enabled=True, endpoint="http://example.com/functions/v1")
    CloudConfig(enabled=True, endpoint="https://abc.supabase.co/functions/v1")
    CloudConfig(enabled=False, endpoint="")  # disabled needs nothing


def test_service_delivers_incident_events_and_heartbeats(tmp_path: Path) -> None:
    cfg = config_from_dict(
        {
            "service": {"data_dir": str(tmp_path / "edge"), "device_id": "test-pi"},
            "recording": {"enabled": False},
            "cloud": {"enabled": True, "endpoint": "http://127.0.0.1:54321/functions/v1"},
        }
    )
    server = FakeServer()
    svc = EdgeService(
        cfg,
        lambda: SyntheticSource(get_scenario("convulsion"), fps=15, size=(320, 180)),
        cloud_token=TOKEN,
        cloud_transport=server,
    )
    svc.run()
    sent = [m for r in server.requests for m in r["messages"]]
    events = [m["payload"]["event_type"] for m in sent if m["kind"] == "incident_event"]
    assert events == ["incident_opened", "incident_ended"]
    assert any(m["kind"] == "heartbeat" for m in sent)
    store = LocalStore(svc.paths.db)
    try:
        assert store.queue_depth() == 0, "shutdown flush delivered everything"
    finally:
        store.close()


def test_service_without_token_keeps_queueing(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    cfg = config_from_dict(
        {
            "service": {"data_dir": str(tmp_path / "edge")},
            "recording": {"enabled": False},
            "cloud": {"enabled": True, "endpoint": "https://abc.supabase.co/functions/v1"},
        }
    )
    svc = EdgeService(cfg, lambda: SyntheticSource(get_scenario("rest"), fps=15, size=(320, 180)))
    assert svc.sync is None
    assert "VIGIL_DEVICE_TOKEN is not set" in caplog.text
