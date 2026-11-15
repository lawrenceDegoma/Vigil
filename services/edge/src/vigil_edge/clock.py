"""Injectable clocks so time-dependent logic is testable without sleeping."""

from __future__ import annotations

import time
from datetime import UTC, datetime, timedelta
from typing import Protocol


class Clock(Protocol):
    def monotonic(self) -> float: ...

    def now_utc(self) -> datetime: ...


class SystemClock:
    def monotonic(self) -> float:
        return time.monotonic()

    def now_utc(self) -> datetime:
        return datetime.now(UTC)


class FakeClock:
    """Manually advanced clock for tests and deterministic replay."""

    def __init__(self, start_utc: datetime | None = None, start_monotonic: float = 0.0) -> None:
        self._mono = start_monotonic
        self._utc = start_utc or datetime(2026, 1, 1, tzinfo=UTC)

    def monotonic(self) -> float:
        return self._mono

    def now_utc(self) -> datetime:
        return self._utc

    def advance(self, seconds: float) -> None:
        if seconds < 0:
            raise ValueError("cannot move a clock backwards")
        self._mono += seconds
        self._utc += timedelta(seconds=seconds)
