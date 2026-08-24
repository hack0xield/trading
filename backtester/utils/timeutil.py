"""Time parsing and session-boundary helpers.

Two things bite everyone who backtests intraday strategies, and both are handled
here rather than in strategy code:

1. MT5 bar timestamps are *broker server* time (typically UTC+2/+3), not UTC.
   They are stored verbatim and labelled UTC; use `session_tz` to place the day
   boundary where you actually mean it.
2. "Day start" is not midnight UTC for most instruments. It is the first bar at
   or after a session open in some local timezone.
"""

from __future__ import annotations

import json
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

try:  # stdlib since 3.9; the Wine interpreter may lack tzdata
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover
    ZoneInfo = None  # type: ignore[assignment]

UTC = timezone.utc

_FORMATS = (
    "%Y-%m-%d %H:%M:%S",
    "%Y-%m-%dT%H:%M:%S",
    "%Y-%m-%d %H:%M",
    "%Y-%m-%d",
    "%Y/%m/%d",
    "%d.%m.%Y",
)


def parse_dt(value: str | datetime | date | None) -> datetime | None:
    """Parse the date formats a CLI user is likely to type. Result is UTC-aware."""
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return ensure_utc(value)
    if isinstance(value, date):
        return datetime(value.year, value.month, value.day, tzinfo=UTC)

    text = str(value).strip()
    try:
        return ensure_utc(datetime.fromisoformat(text.replace("Z", "+00:00")))
    except ValueError:
        pass
    for fmt in _FORMATS:
        try:
            return datetime.strptime(text, fmt).replace(tzinfo=UTC)
        except ValueError:
            continue
    raise ValueError(f"Cannot parse datetime {value!r}")


def ensure_utc(dt: datetime) -> datetime:
    """Attach UTC to a naive datetime, or convert an aware one."""
    if dt.tzinfo is None:
        return dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


def get_tz(name: str):
    """Resolve a timezone name, falling back to fixed offsets like 'UTC+3'.

    The fallback matters: a bare Wine Python install often has no tzdata, and a
    broker offset is usually all that is needed anyway.
    """
    if not name or name.upper() == "UTC":
        return UTC
    upper = name.upper()
    if upper.startswith("UTC") and len(upper) > 3:
        try:
            return timezone(timedelta(hours=float(upper[3:])))
        except ValueError:
            pass
    if ZoneInfo is None:
        raise ValueError(f"zoneinfo unavailable; use 'UTC+2' style offsets, got {name!r}")
    return ZoneInfo(name)


#: Written by `scripts/fetch_mt5.py` on every fetch. See `broker_offset`.
BROKER_CLOCK_PATH = Path(__file__).resolve().parents[2] / "configs" / "broker.json"


def broker_offset(path: Path | str | None = None, default: float | None = None) -> float | None:
    """Hours the broker's server clock runs ahead of UTC, or `default`.

    MT5 stamps every bar with the server's wall clock and labels it UTC, so a
    timestamp in `data/` is this many hours ahead of the moment it describes.
    Anything that reads an hour off a bar — a trading-session breakdown, a
    session filter — needs this to be right, and guessing it produces labels
    that are confidently wrong rather than obviously missing.

    **This is the offset as at the last fetch, not a constant.** Server clocks
    normally observe daylight saving — MetaQuotes-Demo runs EET/EEST, so UTC+2
    in winter and UTC+3 in summer — which means subtracting this number from a
    timestamp six months old is wrong by an hour.

    That seasonality is also why reading an hour off a bar rarely needs it: the
    broker clock shifts with New York, so the trading sessions themselves sit at
    stable *broker-local* hours and move around in UTC. Working in the bars'
    own stamps is the steady frame; this value is for provenance and for sanity
    checks, not for routine conversion.

    Returns `default` when no measurement exists: the file is written by a
    fetch, which needs the Wine terminal, so a checkout that has only ever
    read stored bars will not have one.
    """
    file = Path(path or BROKER_CLOCK_PATH)
    if not file.exists():
        return default
    try:
        with open(file, "r", encoding="utf-8") as fh:
            value = json.load(fh).get("utc_offset_hours")
    except (OSError, ValueError):
        return default
    return float(value) if isinstance(value, (int, float)) else default


def session_day(dt: datetime, tz=UTC, session_start_hour: int = 0) -> date:
    """The trading day a timestamp belongs to.

    With `session_start_hour=17` in New York time, bars from 17:00 Sunday
    onwards belong to Monday's session — the standard FX rollover convention.
    """
    local = dt.astimezone(tz)
    if session_start_hour:
        local = local - timedelta(hours=session_start_hour)
    return local.date()


def to_epoch(dt: datetime) -> int:
    return int(ensure_utc(dt).timestamp())


def from_epoch(seconds: float) -> datetime:
    return datetime.fromtimestamp(int(seconds), tz=UTC)


def nights_between(start: datetime, end: datetime) -> int:
    """Rollovers crossed, for swap charges. Counts UTC date changes."""
    days = (end.astimezone(UTC).date() - start.astimezone(UTC).date()).days
    return max(0, days)
