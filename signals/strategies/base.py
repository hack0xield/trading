"""Base class for a signal strategy.

A strategy owns two things: how to turn stored bars into facts, and how to
phrase those facts. Everything else — scheduling, delivery, recipient routing,
failure handling — belongs to the runner and is identical for every strategy.

Parameters reuse `backtester.utils.params.StrategyParams`, so a job's `params:`
block is typed and a misspelled key is rejected when the config loads rather
than midway through a scheduled run.
"""

from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from backtester.core.types import Bar  # noqa: E402
from backtester.utils import timeframes  # noqa: E402
from backtester.utils.params import StrategyParams  # noqa: E402


class SignalStrategy:
    """Subclass this, set `name` and `params_class`, implement the two hooks."""

    name: str = "base"
    params_class: type[StrategyParams] = StrategyParams
    description: str = ""

    def __init__(self, params: dict | None = None):
        self.p = self.params_class.from_dict(params or {})

    def evaluate(self, job, config) -> dict:
        """Compute the current picture. Returns the facts `compose` renders."""
        raise NotImplementedError

    def compose(self, job, facts: dict) -> str:
        """Render the facts as a Telegram message (HTML parse mode)."""
        raise NotImplementedError

    def write_artifacts(self, job, config, facts: dict):
        """Optionally leave a report in runs/. Return the directory, or None.

        Strategies that have nothing worth saving can ignore this; the runner
        treats None as "nothing written".
        """
        return None

    @property
    def params(self) -> dict:
        return self.p.to_dict()


def bar_facts(bars: list[Bar], timeframe: str) -> dict:
    """Freshness and coverage, computed the same way for every strategy.

    Staleness is measured from the newest *bar*, never from whether a fetch
    reported success — a fetch can succeed and return nothing over a weekend,
    and a signal computed on week-old bars is worse than no signal.
    """
    last = bars[-1]
    age_h = (datetime.now(timezone.utc) - last.time).total_seconds() / 3600
    bar_h = timeframes.duration(timeframe).total_seconds() / 3600
    return {
        "bars": len(bars),
        "first_bar": bars[0].time,
        "last_bar": last.time,
        "last_close": last.close,
        "age_hours": age_h,
        # +72h of slack so a normal weekend closure is not reported as stale.
        "stale": age_h > bar_h * 2 + 72,
    }
