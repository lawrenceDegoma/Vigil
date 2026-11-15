"""Frame source interface. Every acquisition backend implements this."""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Iterator
from types import TracebackType

from vigil_edge.models import Frame


class SourceError(RuntimeError):
    """Raised when a source cannot be opened or fails irrecoverably."""


class FrameSource(ABC):
    """Yields frames with monotonically increasing stream-offset timestamps.

    Sources deliver frames at (approximately) ``fps``. Live sources may drop
    frames; consumers must use ``Frame.timestamp`` rather than assume spacing.
    """

    @property
    @abstractmethod
    def fps(self) -> float:
        """Nominal delivery rate after any subsampling."""

    @property
    @abstractmethod
    def frame_size(self) -> tuple[int, int]:
        """(width, height) of delivered images in pixels."""

    @property
    def is_live(self) -> bool:
        return False

    @abstractmethod
    def frames(self) -> Iterator[Frame]:
        """Iterate frames until the source is exhausted or closed."""

    def close(self) -> None:  # noqa: B027 - optional hook
        """Release underlying resources. Safe to call more than once."""

    def __enter__(self) -> FrameSource:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()
