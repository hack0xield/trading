"""Storage interface for bar data.

Every backend implements the same four operations, so `--data csv://data/bars`
and `--data postgresql://localhost/trading` are interchangeable on any script.
`write` is an upsert keyed on (symbol, timeframe, time): re-fetching an
overlapping range is always safe and never duplicates bars, which matters
because MT5 hands back whole candles including the one still forming.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime

from ..core.types import Bar

BAR_COLUMNS = ("time", "open", "high", "low", "close", "volume", "spread")


@dataclass(slots=True)
class SeriesInfo:
    """What a store knows about one symbol/timeframe series."""

    symbol: str
    timeframe: str
    count: int
    first: datetime | None
    last: datetime | None

    def __str__(self) -> str:
        span = "-"
        if self.first and self.last:
            span = f"{self.first:%Y-%m-%d} .. {self.last:%Y-%m-%d}"
        return f"{self.symbol:10} {self.timeframe:4} {self.count:>9,} bars  {span}"


class BarStore(ABC):
    """Persistent store of OHLC series."""

    name = "base"

    @abstractmethod
    def write(self, symbol: str, timeframe: str, bars: list[Bar]) -> int:
        """Upsert bars. Returns the number written."""

    @abstractmethod
    def read(
        self,
        symbol: str,
        timeframe: str,
        start: datetime | None = None,
        end: datetime | None = None,
    ) -> list[Bar]:
        """Read a series, ascending by time. `start`/`end` are inclusive."""

    @abstractmethod
    def list_series(self) -> list[SeriesInfo]:
        """Everything held by this store."""

    @abstractmethod
    def delete(self, symbol: str, timeframe: str) -> None:
        """Drop a series."""

    def info(self, symbol: str, timeframe: str) -> SeriesInfo | None:
        for series in self.list_series():
            if series.symbol == symbol.upper() and series.timeframe == timeframe.upper():
                return series
        return None

    def __str__(self) -> str:  # pragma: no cover - display only
        return f"{type(self).__name__}({getattr(self, 'location', '')})"


def dedupe(bars: list[Bar]) -> list[Bar]:
    """Sort by time and keep the last bar seen for each timestamp.

    Last-wins because a re-fetch returns the more complete version of a candle
    that was still forming when it was first stored.
    """
    merged: dict[datetime, Bar] = {}
    for bar in bars:
        merged[bar.time] = bar
    return [merged[key] for key in sorted(merged)]


def in_range(bars: list[Bar], start: datetime | None, end: datetime | None) -> list[Bar]:
    if start is None and end is None:
        return bars
    return [
        b
        for b in bars
        if (start is None or b.time >= start) and (end is None or b.time <= end)
    ]
