"""Clock adapters. Domain code never calls these directly; the app layer injects time."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone


class SystemClock:
    def now(self) -> datetime:
        return datetime.now(timezone.utc)


class FixedClock:
    """Deterministic clock for tests."""

    def __init__(self, now: datetime) -> None:
        if now.tzinfo is None:
            now = now.replace(tzinfo=timezone.utc)
        self._now = now

    def now(self) -> datetime:
        return self._now

    def advance(self, **kwargs: float) -> None:
        self._now = self._now + timedelta(**kwargs)
