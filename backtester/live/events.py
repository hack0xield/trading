"""Trading events, and the listeners they are delivered to.

A listener is any callable taking an `Event`. It is told before an order goes
to the broker (`order_intent`, `exit_intent`) and again once the broker has
answered. A listener that raises is reported and skipped, never allowed to
stop the trading loop. An `error` the runner carries on through is delivered
once, not again while it keeps repeating.
"""

from __future__ import annotations

import json
import sys
import time as _time
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Callable

#: Process lifecycle.
STARTED = "started"
MODE = "mode"                        # shadow <-> live
STOPPED = "stopped"
ERROR = "error"
BAR_CLOSED = "bar_closed"

#: Entries.
ORDER_INTENT = "order_intent"        # about to be sent
ORDER_PLACED = "order_placed"        # a limit order now resting at the broker
ORDER_FILLED = "order_filled"        # a position opened
ORDER_REJECTED = "order_rejected"
ORDER_CANCELLED = "order_cancelled"

#: Open positions.
POSITION_MODIFIED = "position_modified"
EXIT_INTENT = "exit_intent"          # a strategy close about to be sent
POSITION_CLOSED = "position_closed"

SHADOW = "shadow"
LIVE = "live"


@dataclass(frozen=True, slots=True)
class Event:
    kind: str
    mode: str
    strategy: str
    symbol: str
    data: dict = field(default_factory=dict)
    time: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    def as_dict(self) -> dict:
        return {
            "time": self.time.isoformat(timespec="seconds"),
            "kind": self.kind,
            "mode": self.mode,
            "strategy": self.strategy,
            "symbol": self.symbol,
            **self.data,
        }


Listener = Callable[[Event], None]


class Notifier:
    """Fans events out to every registered listener."""

    #: Seconds before the same retrying error is delivered again.
    REPEAT_AFTER = 300.0

    def __init__(self, strategy: str, symbol: str, listeners: list[Listener] | None = None):
        self.strategy = strategy
        self.symbol = symbol
        self.mode = SHADOW
        self.listeners: list[Listener] = list(listeners or [])
        self._last_error: tuple[str, float] | None = None

    def add(self, listener: Listener) -> None:
        self.listeners.append(listener)

    def emit(self, kind: str, **data) -> Event | None:
        """Deliver an event; None when it repeats the retrying error just delivered."""
        if kind == ERROR and data.get("retrying"):
            now = _time.monotonic()
            message = str(data.get("error"))
            if self._last_error and self._last_error[0] == message \
                    and now - self._last_error[1] < self.REPEAT_AFTER:
                return None
            self._last_error = (message, now)
        event = Event(kind, self.mode, self.strategy, self.symbol, json_safe(data))
        for listener in self.listeners:
            try:
                listener(event)
            except Exception as exc:  # a broken listener must not stop trading
                print(f"[live] listener {listener!r} failed on {kind}: {exc}", file=sys.stderr)
        return event


def json_safe(value):
    """JSON-safe copy: datetimes and dates as ISO strings, enums as their values."""
    if isinstance(value, dict):
        return {k: json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [json_safe(v) for v in value]
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, Enum):
        return value.value
    return value


# ------------------------------------------------------------------ listeners

class ConsoleListener:
    """One line per event on stdout."""

    def __call__(self, event: Event) -> None:
        detail = " ".join(f"{k}={v}" for k, v in event.data.items() if v not in (None, ""))
        print(f"{event.time:%Y-%m-%d %H:%M:%S}  [{event.mode}] {event.kind:<18} {detail}",
              flush=True)


class JsonlListener:
    """Appends every event to a JSON-lines file."""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def __call__(self, event: Event) -> None:
        with open(self.path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(event.as_dict()) + "\n")

    def __repr__(self) -> str:
        return f"JsonlListener({self.path})"

