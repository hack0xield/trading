"""Loading bars for a run, with the sanity checks worth doing every time.

Bad data produces confident, wrong backtests. `load_bars` refuses a series that
is empty or out of order, and reports gaps and zero-range bars rather than
silently trading through them.
"""

from __future__ import annotations

from datetime import datetime

from ..core.types import Bar
from ..utils import timeframes
from ..utils.timeutil import parse_dt
from .base import BarStore
from .registry import store_from_uri


def load_bars(
    symbol: str,
    timeframe: str,
    data: str | BarStore | None = None,
    start: str | datetime | None = None,
    end: str | datetime | None = None,
    validate: bool = True,
) -> list[Bar]:
    store = store_from_uri(data)
    bars = store.read(symbol, timeframe, parse_dt(start), parse_dt(end))
    if not bars:
        available = ", ".join(str(s) for s in store.list_series()) or "(store is empty)"
        raise ValueError(
            f"No {symbol} {timeframe} bars in {store}"
            f"{' for the requested range' if start or end else ''}.\n"
            f"Available:\n  {available}\n"
            f"Fetch some first: scripts/fetch-mt5.sh --symbol {symbol} --timeframe {timeframe}"
        )
    if validate:
        problems = validate_bars(bars, timeframe)
        errors = [p for p in problems if p.startswith("ERROR")]
        if errors:
            raise ValueError("Bad bar data:\n  " + "\n  ".join(errors))
    return bars


def validate_bars(bars: list[Bar], timeframe: str | None = None) -> list[str]:
    """Structural checks. ERROR entries make a run meaningless; WARN ones do not."""
    problems: list[str] = []
    if not bars:
        return ["ERROR: no bars"]

    previous = None
    bad_ohlc = 0
    flat = 0
    for bar in bars:
        if previous is not None and bar.time <= previous.time:
            problems.append(f"ERROR: bars out of order or duplicated at {bar.time}")
            break
        if not (bar.low <= bar.open <= bar.high and bar.low <= bar.close <= bar.high):
            bad_ohlc += 1
        if bar.high == bar.low:
            flat += 1
        previous = bar

    if bad_ohlc:
        problems.append(f"ERROR: {bad_ohlc:,} bars where open/close fall outside high/low")
    if flat:
        problems.append(f"WARN: {flat:,} bars with zero range (illiquid or synthetic)")

    if timeframe:
        step = timeframes.duration(timeframe).total_seconds()
        # Weekends make gaps normal in FX; only flag the unusually large ones.
        big = sum(
            1
            for a, b in zip(bars, bars[1:])
            if (b.time - a.time).total_seconds() > step * 3 + 3 * 86400
        )
        if big:
            problems.append(f"WARN: {big:,} gaps longer than a weekend")

    return problems


def resample(bars: list[Bar], target: str) -> list[Bar]:
    """Aggregate to a coarser timeframe (M1 -> M15, H1 -> D1, ...).

    Handy for running the same strategy across timeframes off one stored series,
    and for checking how much a result depends on intrabar resolution.
    """
    seconds = timeframes.duration(target).total_seconds()
    if not bars:
        return []

    out: list[Bar] = []
    bucket: list[Bar] = []
    bucket_key = None

    for bar in bars:
        key = int(bar.time.timestamp() // seconds)
        if bucket_key is None:
            bucket_key = key
        if key != bucket_key:
            out.append(_merge(bucket))
            bucket = []
            bucket_key = key
        bucket.append(bar)
    if bucket:
        out.append(_merge(bucket))
    return out


def _merge(bucket: list[Bar]) -> Bar:
    return Bar(
        time=bucket[0].time,
        open=bucket[0].open,
        high=max(b.high for b in bucket),
        low=min(b.low for b in bucket),
        close=bucket[-1].close,
        volume=sum(b.volume for b in bucket),
        spread=sum(b.spread for b in bucket) / len(bucket),
    )
