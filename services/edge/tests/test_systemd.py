from __future__ import annotations

import socket
import tempfile
from pathlib import Path

import pytest

from vigil_edge import systemd


def test_no_socket_is_noop(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("NOTIFY_SOCKET", raising=False)
    assert systemd.notify("READY=1") is False
    assert not systemd.watchdog_enabled()


def test_sends_datagram(monkeypatch: pytest.MonkeyPatch) -> None:
    # AF_UNIX paths are length-limited; use a short temp dir.
    with tempfile.TemporaryDirectory(dir="/tmp") as d:
        path = str(Path(d) / "n.sock")
        server = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
        server.bind(path)
        try:
            monkeypatch.setenv("NOTIFY_SOCKET", path)
            monkeypatch.setenv("WATCHDOG_USEC", "30000000")
            assert systemd.notify("WATCHDOG=1")
            assert server.recv(64) == b"WATCHDOG=1"
            assert systemd.watchdog_enabled()
        finally:
            server.close()
