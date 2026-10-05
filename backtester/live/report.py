"""The session's report: the backtest's run files and page, kept current.

`runs-live/<session>/report/` holds what `run_backtest.py --save` writes for a
run, over everything replayed and stepped so far: summary.json, trades.csv,
equity.csv, the strategy's tables, the casebook and chart.html. Beside them,
`live.json` is the session's layer — account, open positions and orders, the
strategy's status, the journal and the runner's events — which the page also
carries inline. `pulse.json`, rewritten on every poll, lets the page tell a
runner that is alive from one that is stuck, and reload when a newer report
is written.

Each file is written to a staging directory and moved into place on its own,
so a reader never sees half of one.
"""

from __future__ import annotations

import json
import os
import shutil
from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable

from ..core.strategy import Strategy
from ..core.types import Bar, BacktestResult
from ..utils import timeframes
from ..utils.timeutil import broker_offset
from .events import BAR_CLOSED, json_safe
from .state import utc_now

#: Newest entries of each journal carried by the page.
JOURNAL_SIZE = 200

#: How far back into events.jsonl the page's event list reads.
EVENTS_TAIL_BYTES = 256 * 1024

STAGING = ".staging"
PULSE = "pulse.json"


class LiveReport:
    def __init__(
        self,
        directory: str | Path,
        execution: dict,
        events_path: str | Path | None = None,
        chart_timeframe: str | None = None,
        log: Callable[[str], None] = print,
    ):
        self.directory = Path(directory)
        self.execution = execution
        self.events_path = Path(events_path) if events_path else None
        self.chart_timeframe = chart_timeframe
        self.log = log
        self.generated: datetime | None = None
        self.report_bar: datetime | None = None
        self.error: str | None = None

    def write(
        self,
        result: BacktestResult,
        strategy: Strategy,
        bars: list[Bar],
        snapshot: dict,
        running: bool,
    ) -> Path:
        """Write the whole report for the bars processed so far. Returns chart.html."""
        from ..data.results import write_run
        from ..metrics import casebook, compute

        staging = self.directory / STAGING
        shutil.rmtree(staging, ignore_errors=True)
        staging.mkdir(parents=True)

        write_run(staging, result, compute(result).to_dict(), self.execution)
        casebook.write(staging, result)

        generated = utc_now()
        live = json_safe(self.layer(result, strategy, bars, snapshot, running, generated))
        (staging / "live.json").write_text(json.dumps(live, indent=2) + "\n", encoding="utf-8")

        timeframe = self.chart_timeframe or result.timeframe
        page = strategy.chart(staging, "", timeframe, live=live)
        if page is None:
            from scripts.plot_run import render

            page = render(staging, "", timeframe, bars=bars, live=live)

        self._publish(staging)
        self.generated, self.report_bar = generated, bars[-1].time
        if self.error is not None:
            self.log("report written again")
        self.error = None
        return self.directory / Path(page).name

    def failed(self, exc: Exception) -> None:
        """Remember why the report could not be written; said once until it changes."""
        message = f"{type(exc).__name__}: {exc}"
        if message != self.error:
            self.log(f"report not written: {message}")
        self.error = message

    def pulse(self, **fields) -> None:
        """Rewrite pulse.json: the heartbeat and the report the page should be showing."""
        data = {
            "updated_at": utc_now(),
            **fields,
            "report_generated": self.generated,
            "report_bar": self.report_bar,
            "report_error": self.error,
        }
        self.directory.mkdir(parents=True, exist_ok=True)
        _replace_text(self.directory / PULSE, json.dumps(json_safe(data), indent=2) + "\n")

    # --------------------------------------------------------------- the layer

    def layer(
        self,
        result: BacktestResult,
        strategy: Strategy,
        bars: list[Bar],
        snapshot: dict,
        running: bool,
        generated: datetime,
    ) -> dict:
        """What the page shows on top of the backtest's own chart."""
        last = bars[-1].time
        journal = (result.artifacts or {}).get("journal", [])
        return {
            "generated": generated,
            "running": running,
            "strategy": result.strategy,
            "symbol": result.symbol,
            "timeframe": result.timeframe,
            "mode": snapshot.get("mode"),
            "paper": snapshot.get("paper"),
            "replay_from": bars[0].time,
            "last_closed_bar": last,
            "forming_bar": snapshot.get("forming_bar"),
            "bar_close_utc": bar_close_utc(last, result.timeframe),
            "account": snapshot.get("session"),
            "broker_account": {"balance": snapshot.get("balance"),
                               "equity": snapshot.get("equity")},
            "positions": snapshot.get("positions", []),
            "resting_orders": snapshot.get("resting_orders", []),
            "queued_orders": snapshot.get("queued_orders", []),
            "status": strategy.status(),
            "journal": journal[-JOURNAL_SIZE:],
            "events": self._events(),
            "margin": snapshot.get("margin"),
        }

    def _events(self) -> list[dict]:
        """The newest runner events from events.jsonl, every bar_closed left out."""
        if self.events_path is None or not self.events_path.exists():
            return []
        with open(self.events_path, "rb") as fh:
            fh.seek(0, os.SEEK_END)
            size = fh.tell()
            fh.seek(max(0, size - EVENTS_TAIL_BYTES))
            lines = fh.read().decode("utf-8", errors="replace").splitlines()
        if size > EVENTS_TAIL_BYTES:
            lines = lines[1:]                  # the first may be cut
        events = []
        for line in lines:
            try:
                event = json.loads(line)
            except ValueError:
                continue
            if event.get("kind") != BAR_CLOSED:
                events.append(event)
        return events[-JOURNAL_SIZE:]

    # ------------------------------------------------------------- publishing

    def _publish(self, staging: Path) -> None:
        """Move every staged file into the report, and drop files no longer written."""
        fresh = {f.name for f in staging.iterdir() if f.is_file()}
        for name in sorted(fresh):
            os.replace(staging / name, self.directory / name)
        for old in self.directory.iterdir():
            if old.is_file() and old.name not in fresh and old.name != PULSE:
                old.unlink(missing_ok=True)
        shutil.rmtree(staging, ignore_errors=True)


def bar_close_utc(bar_open: datetime, timeframe: str) -> datetime | None:
    """When a bar stamped on the broker's clock closed, in UTC; None without the offset."""
    offset = broker_offset(default=None)
    if offset is None:
        return None
    return bar_open + timeframes.duration(timeframe) - timedelta(hours=offset)


def _replace_text(path: Path, text: str) -> None:
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(text, encoding="utf-8")
    os.replace(temporary, path)
