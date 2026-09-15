"""Trading events, and the listeners they are delivered to.

A listener is any callable taking an `Event`. It is told before an order goes
to the broker (`order_intent`, `exit_intent`) and again once the broker has
answered. A listener that raises is reported and skipped, never allowed to
stop the trading loop.
"""

from __future__ import annotations

import json
import queue
import sys
import threading
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timezone
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

    def __init__(self, strategy: str, symbol: str, listeners: list[Listener] | None = None):
        self.strategy = strategy
        self.symbol = symbol
        self.mode = SHADOW
        self.listeners: list[Listener] = list(listeners or [])

    def add(self, listener: Listener) -> None:
        self.listeners.append(listener)

    def emit(self, kind: str, **data) -> Event:
        event = Event(kind, self.mode, self.strategy, self.symbol, _plain(data))
        for listener in self.listeners:
            try:
                listener(event)
            except Exception as exc:  # a broken listener must not stop trading
                print(f"[live] listener {listener!r} failed on {kind}: {exc}", file=sys.stderr)
        return event


def _plain(value):
    """JSON-safe copy: datetimes as ISO strings, enums as their values."""
    if isinstance(value, dict):
        return {k: _plain(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain(v) for v in value]
    if isinstance(value, datetime):
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


class WebhookListener:
    """POSTs each event as JSON from a background thread.

    The thread keeps a slow or dead endpoint from delaying an order.
    """

    def __init__(self, url: str, kinds: set[str] | None = None, timeout: float = 10.0):
        self.url = url
        self.kinds = kinds
        self.timeout = timeout
        self._queue: queue.Queue = queue.Queue()
        threading.Thread(target=self._drain, name="webhook", daemon=True).start()

    def __call__(self, event: Event) -> None:
        if self.kinds is None or event.kind in self.kinds:
            self._queue.put(event.as_dict())

    def _drain(self) -> None:
        while True:
            payload = self._queue.get()
            request = urllib.request.Request(
                self.url, data=json.dumps(payload).encode(),
                headers={"content-type": "application/json"}, method="POST",
            )
            try:
                urllib.request.urlopen(request, timeout=self.timeout).close()
            except Exception as exc:
                print(f"[live] webhook {self.url} failed: {exc}", file=sys.stderr)

    def __repr__(self) -> str:
        return f"WebhookListener({self.url})"
