"""`vigil test-alert`: queues a contract-valid request for the running service."""

from __future__ import annotations

import json
import threading
import time
from datetime import UTC, datetime
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

from vigil_edge.cli import main
from vigil_edge.contracts import build_test_alert
from vigil_edge.store import LocalStore

CLOUD = [
    "--set",
    "cloud.enabled=true",
    "--set",
    "cloud.endpoint=https://abc.supabase.co/functions/v1",
]


def schema(repo_root: Path) -> Draft202012Validator:
    s = json.loads((repo_root / "packages/contracts/schemas/test-alert.v1.schema.json").read_text())
    return Draft202012Validator(s, format_checker=Draft202012Validator.FORMAT_CHECKER)


def test_message_matches_the_contract_and_example(repo_root: Path) -> None:
    msg = build_test_alert(
        "pi-livingroom", "all", "d0000000-0000-0000-0000-000000000001",
        datetime(2026, 11, 21, 20, 0, tzinfo=UTC),
    )  # fmt: skip
    schema(repo_root).validate(msg)
    example = json.loads((repo_root / "packages/contracts/examples/test_alert.json").read_text())
    assert msg == example
    with pytest.raises(ValueError):
        build_test_alert("pi", "everyone", "x", datetime.now(UTC))


def test_refuses_when_cloud_is_off(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["test-alert", "--data-dir", str(tmp_path)]) == 1
    assert "not enabled" in capsys.readouterr().out


def test_queues_and_reports_delivery(
    tmp_path: Path, repo_root: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    LocalStore(tmp_path / "vigil.db").close()  # an installed service has a database
    result: list[int] = []
    cmd = ["test-alert", "--data-dir", str(tmp_path), "--device-id", "pi-x", "--to", "admin",
           "--wait", "5", *CLOUD]  # fmt: skip
    th = threading.Thread(target=lambda: result.append(main(cmd)))
    th.start()
    store = LocalStore(tmp_path / "vigil.db")
    try:
        deadline = time.monotonic() + 3
        msgs = []
        while not msgs and time.monotonic() < deadline:
            msgs = [m for m in store.pending() if m.kind == "test_alert"]
            time.sleep(0.05)
        [m] = msgs
        schema(repo_root).validate(m.payload)
        assert m.payload["scope"] == "admin" and m.payload["device_id"] == "pi-x"
        store.mark_sent([m.seq], datetime.now(UTC))  # what CloudSync does on "accepted"
    finally:
        th.join(timeout=10)
        store.close()
    assert result == [0]
    assert "delivered to the cloud" in capsys.readouterr().out


def test_reports_when_not_delivered(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    LocalStore(tmp_path / "vigil.db").close()
    assert main(["test-alert", "--data-dir", str(tmp_path), "--wait", "0", *CLOUD]) == 2
    assert "stays queued" in capsys.readouterr().out
    store = LocalStore(tmp_path / "vigil.db")
    try:
        assert store.pending_count("test_alert") == 1
    finally:
        store.close()


def test_device_id_comes_from_the_running_service(tmp_path: Path) -> None:
    # `sudo -u vigil vigil test-alert` does not see VIGIL_DEVICE_ID from vigil.env.
    LocalStore(tmp_path / "vigil.db").close()
    (tmp_path / "state").mkdir()
    (tmp_path / "state" / "heartbeat.json").write_text(json.dumps({"device_id": "pi-livingroom"}))
    assert main(["test-alert", "--data-dir", str(tmp_path), "--wait", "0", *CLOUD]) == 2
    store = LocalStore(tmp_path / "vigil.db")
    try:
        [m] = [m for m in store.pending() if m.kind == "test_alert"]
        assert m.payload["device_id"] == "pi-livingroom"
    finally:
        store.close()
