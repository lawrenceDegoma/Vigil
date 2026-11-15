"""Minimal sd_notify(3) client (no dependency): READY, WATCHDOG, STATUS, STOPPING."""

from __future__ import annotations

import os
import socket


def notify(message: str) -> bool:
    """Send a notification to systemd if running under a Type=notify unit."""
    addr = os.environ.get("NOTIFY_SOCKET")
    if not addr:
        return False
    if addr.startswith("@"):
        addr = "\0" + addr[1:]  # abstract namespace
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as sock:
            sock.connect(addr)
            sock.sendall(message.encode())
        return True
    except OSError:
        return False


def watchdog_enabled() -> bool:
    return bool(os.environ.get("WATCHDOG_USEC")) and bool(os.environ.get("NOTIFY_SOCKET"))
