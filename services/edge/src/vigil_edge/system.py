"""Host metrics with graceful fallbacks (Linux/Pi first; None elsewhere)."""

from __future__ import annotations

import os
import resource
import shutil
import subprocess
import sys
import time
from pathlib import Path

_THERMAL = Path("/sys/class/thermal/thermal_zone0/temp")
_MEMINFO = Path("/proc/meminfo")


def cpu_temperature_c() -> float | None:
    try:
        return round(int(_THERMAL.read_text().strip()) / 1000.0, 1)
    except (OSError, ValueError):
        return None


def memory_used_percent() -> float | None:
    try:
        info = {}
        for line in _MEMINFO.read_text().splitlines():
            key, _, rest = line.partition(":")
            info[key] = int(rest.split()[0])
        total, avail = info["MemTotal"], info["MemAvailable"]
        return round(100.0 * (total - avail) / total, 1)
    except (OSError, KeyError, ValueError, IndexError, ZeroDivisionError):
        return None


def peak_rss_mb() -> float:
    rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return rss / (1024 * 1024) if sys.platform == "darwin" else rss / 1024


def pi_throttled() -> str | None:
    """Raw ``vcgencmd get_throttled`` value on a Pi (e.g. '0x0'), else None.

    Non-zero bits indicate under-voltage or thermal throttling, now or since boot.
    """
    if shutil.which("vcgencmd") is None:
        return None
    try:
        out = subprocess.run(
            ["vcgencmd", "get_throttled"], capture_output=True, text=True, timeout=2, check=False
        ).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return None
    return out.partition("=")[2] or None


class CpuMeter:
    """Process CPU utilisation (% of one core) between successive calls."""

    def __init__(self) -> None:
        self._last = (time.monotonic(), self._cpu())

    @staticmethod
    def _cpu() -> float:
        t = os.times()
        return t.user + t.system

    def percent(self) -> float:
        now, cpu = time.monotonic(), self._cpu()
        wall = now - self._last[0]
        used = cpu - self._last[1]
        self._last = (now, cpu)
        return round(100.0 * used / wall, 1) if wall > 0 else 0.0
