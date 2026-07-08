from __future__ import annotations

from datetime import datetime
from typing import Literal, Protocol, runtime_checkable

from radar.models import RawPost

SearchWindow = Literal["hour", "day", "week", "month", "year", "all"]


@runtime_checkable
class Source(Protocol):
    """Every platform collector implements this shape (structural typing --
    no shared base class needed, so a future X source can sit behind a
    feature flag without touching this interface).
    """

    name: str

    def search_top(self, query: str, window: SearchWindow, limit: int = 50) -> list[RawPost]:
        """Most-engaged recent posts matching `query` -- the triage pass."""
        ...

    def search_recent(self, query: str, since: datetime, limit: int = 50) -> list[RawPost]:
        """Newest posts matching `query`, created at/after `since` -- the early-warning pass."""
        ...
