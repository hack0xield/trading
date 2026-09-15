"""`state.json`: the runner's latest snapshot, for anything reporting on it.

The file is replaced whole on every write, through a temporary file and a
rename, so a reader never sees half of one. `MarginWatch` supplies its
`margin` block.
"""

from __future__ import annotations

import json
import os
from datetime import date, datetime, timezone
from pathlib import Path

from .events import ERROR, Event, Notifier, json_safe

DEFAULT_STALE_AFTER_DAYS = 30


def utc_now() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)


class StateFile:
    def __init__(self, path: str | Path, notify: Notifier, **session):
        """`session` holds what never changes during a run: strategy, symbol,
        timeframe, config, magic, paper and pid."""
        self.path = Path(path)
        self.session = session
        self.started_at = utc_now()
        self.last_error: dict | None = None
        notify.add(self._remember)

    def _remember(self, event: Event) -> None:
        if event.kind == ERROR:
            self.last_error = {"time": event.time.isoformat(timespec="seconds"),
                               "error": event.data.get("error")}

    def write(self, running: bool, **snapshot) -> None:
        now = utc_now()
        data = {
            "updated_at": now,
            "running": running,
            "started_at": self.started_at,
            "stopped_at": None if running else now,
            "pid": None,
            "strategy": None,
            "symbol": None,
            "timeframe": None,
            "config": None,
            "magic": None,
            "mode": None,
            "paper": None,
            "account": None,
            "last_closed_bar": None,
            "forming_bar": None,
            "balance": None,
            "equity": None,
            "source": None,
            "positions": [],
            "resting_orders": [],
            "queued_orders": [],
            "margin": None,
            "last_error": self.last_error,
            "failure": None,
        }
        data.update(self.session)
        data.update(snapshot)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_name(f".{self.path.name}.tmp")
        temporary.write_text(json.dumps(json_safe(data), indent=2) + "\n", encoding="utf-8")
        os.replace(temporary, self.path)


class MarginWatch:
    """How old the newest margin reading behind a strategy is.

    Applies to a strategy whose parameters name a `contract` and a
    `margin_log`. The log is read again whenever the file or the date changes.
    """

    def __init__(self, strategy, stale_after_days: int = DEFAULT_STALE_AFTER_DAYS):
        params = strategy.p
        self.stale_after_days = int(stale_after_days)
        self.code = str(getattr(params, "contract", "") or "").upper()
        self.path: Path | None = None
        if self.code and hasattr(params, "margin_log"):
            from ..strategies.margin_zones.margins import MARGIN_LOG

            self.path = Path(params.margin_log or MARGIN_LOG)
        self._key = None
        self._status: dict | None = None

    def status(self, today: date | None = None) -> dict | None:
        """The `margin` block, or None for a strategy without a margin log."""
        if self.path is None:
            return None
        today = today or utc_now().date()
        try:
            stat = os.stat(self.path)
            key = (stat.st_mtime_ns, stat.st_size, today)
        except OSError:
            key = (None, None, today)
        if key != self._key:
            self._key = key
            self._status = self._read(today)
        return self._status

    def _read(self, today: date) -> dict:
        from ..strategies.margin_zones.margins import MarginLog

        status = {
            "contract": self.code, "as_of": None, "maintenance": None, "age_days": None,
            "stale": True, "stale_after_days": self.stale_after_days,
        }
        try:
            reading = MarginLog(self.path).latest(self.code)
        except (OSError, ValueError, KeyError) as exc:
            return {**status, "error": f"cannot read {self.path}: {exc}"}
        if reading is None:
            return status
        age = (today - reading.as_of).days
        return {**status, "as_of": reading.as_of.isoformat(), "maintenance": reading.maintenance,
                "age_days": age, "stale": age > self.stale_after_days}
