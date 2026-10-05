"""The session's account: what the strategy has made since the session began.

The replay that brings a strategy to the present trades history too, and none
of that belongs to the session. `session.json` in the session directory records
`live_from`, the open time of the first bar the session saw open; only entries
at or after it count. It is written once and kept across restarts, so a
restart carries on the same account.
"""

from __future__ import annotations

import json
import os
from datetime import datetime
from pathlib import Path

from ..utils.timeutil import parse_dt
from .events import json_safe


class SessionStart:
    """`session.json`: when the session's account opened, and with how much."""

    def __init__(self, path: str | Path, initial_balance: float, reset: bool = False):
        self.path = Path(path)
        self.initial_balance = float(initial_balance)
        self.live_from: datetime | None = None
        self.created_at: datetime | None = None
        if reset:
            self.path.unlink(missing_ok=True)
        elif self.path.exists():
            data = json.loads(self.path.read_text(encoding="utf-8"))
            self.live_from = parse_dt(data.get("live_from"))
            self.created_at = parse_dt(data.get("created_at"))
            self.initial_balance = float(data.get("initial_balance", self.initial_balance))

    def begin(self, bar_open: datetime, now: datetime) -> bool:
        """Open the account at this bar, unless it is already open. True if it opened."""
        if self.live_from is not None:
            return False
        self.live_from, self.created_at = bar_open, now
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_name(f".{self.path.name}.tmp")
        temporary.write_text(json.dumps(json_safe({
            "live_from": self.live_from,
            "initial_balance": self.initial_balance,
            "created_at": self.created_at,
        }), indent=2) + "\n", encoding="utf-8")
        os.replace(temporary, self.path)
        return True

    def counts(self, entry_time: datetime) -> bool:
        return self.live_from is not None and entry_time >= self.live_from


def session_account(start: SessionStart, trades: list, positions: list[dict]) -> dict:
    """Balance, equity and results of the trades entered since `live_from`.

    `positions` are state rows carrying `entry_time` and the floating `profit`
    (None while unknown, counted as nothing).
    """
    closed = [t for t in trades if start.counts(t.entry_time)]
    held = [p for p in positions if start.counts(_time(p["entry_time"]))]
    realised = sum(t.net_pnl for t in closed)
    floating = sum(p.get("profit") or 0.0 for p in held)
    wins = [t.net_pnl for t in closed if t.net_pnl > 0]
    losses = [t.net_pnl for t in closed if t.net_pnl <= 0]
    balance = start.initial_balance + realised
    return {
        "live_from": start.live_from,
        "initial_balance": round(start.initial_balance, 2),
        "balance": round(balance, 2),
        "equity": round(balance + floating, 2),
        "realised": round(realised, 2),
        "floating": round(floating, 2),
        "return_pct": round(realised / start.initial_balance * 100.0, 4)
        if start.initial_balance else 0.0,
        "trades": len(closed),
        "wins": len(wins),
        "losses": len(losses),
        "win_rate_pct": round(len(wins) / len(closed) * 100.0, 2) if closed else 0.0,
        "profit_factor": round(sum(wins) / -sum(losses), 4) if sum(losses) < 0 else None,
        "open_positions": len(held),
    }


def _time(value) -> datetime:
    return value if isinstance(value, datetime) else parse_dt(value)
