"""Timeframe names, kept independent of the MetaTrader5 package.

The MT5 module only imports under Wine, but the engine, the stores and the CLI
all need to reason about timeframes on Linux. So the canonical table here maps
names to durations, and the MT5 constants are looked up lazily only inside the
fetcher that actually talks to the terminal.
"""

from __future__ import annotations

from datetime import timedelta

# name -> length in seconds
TIMEFRAME_SECONDS: dict[str, int] = {
    "M1": 60,
    "M2": 120,
    "M3": 180,
    "M4": 240,
    "M5": 300,
    "M6": 360,
    "M10": 600,
    "M12": 720,
    "M15": 900,
    "M20": 1200,
    "M30": 1800,
    "H1": 3600,
    "H2": 7200,
    "H3": 10800,
    "H4": 14400,
    "H6": 21600,
    "H8": 28800,
    "H12": 43200,
    "D1": 86400,
    "W1": 604800,
    "MN1": 2592000,
}


def normalize(timeframe: str) -> str:
    tf = str(timeframe).strip().upper()
    if tf not in TIMEFRAME_SECONDS:
        raise ValueError(
            f"Unknown timeframe {timeframe!r}. Valid: {', '.join(TIMEFRAME_SECONDS)}"
        )
    return tf


def duration(timeframe: str) -> timedelta:
    return timedelta(seconds=TIMEFRAME_SECONDS[normalize(timeframe)])


def bars_per_day(timeframe: str) -> float:
    return 86400 / TIMEFRAME_SECONDS[normalize(timeframe)]


def is_intraday(timeframe: str) -> bool:
    return TIMEFRAME_SECONDS[normalize(timeframe)] < 86400
