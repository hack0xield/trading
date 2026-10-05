"""A live session's account and report: `backtester/live/account.py`, `report.py`.

The report is the backtest's own run directory for the bars stepped so far, so
the central check is that it matches what `save_result` writes for a backtest
over the same bars.
"""

from __future__ import annotations

import csv
import json
import shutil
import subprocess
from datetime import datetime, timezone

import pytest

from backtester.core.engine import Backtester
from backtester.core.types import ExitReason
from backtester.data.results import save_result
from backtester.live import LiveReport, SessionStart
from backtester.metrics import compute
from backtester.strategies.margin_zones.mz50 import MZ50Strategy
from test_live import PARENT, backtest, entry_index, make, play, read_state, scenario
from test_mz50 import bars_for, desk  # noqa: F401  (desk is a fixture)

#: The page checks run its script under node.
needs_node = pytest.mark.skipif(shutil.which("node") is None, reason="needs node")

#: Tables the report and a backtest must agree on, row for row.
TABLES = ("trades", "crossings", "signals", "zones", "pivots", "warnings", "chain", "journal")


def rows(path):
    if not path.exists() or path.stat().st_size == 0:
        return []
    with open(path, newline="", encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


def settled(table, records):
    """Leave out what only an end of data writes, which a live session never reaches."""
    if table == "trades":
        return [r for r in records if r["reason"] != ExitReason.END_OF_DATA.value]
    return [r for r in records
            if not r.get("event", "").startswith("end_of_data")
            and "end of the data" not in r.get("detail", "")
            and "end of data" not in r.get("detail", "")]


def session(tmp_path, desk, gold, config, bars, paper=True, **params):
    start = SessionStart(tmp_path / "session" / "session.json", config.initial_balance)
    report = LiveReport(tmp_path / "session" / "report", config.__dict__,
                        events_path=tmp_path / "session" / "events.jsonl", log=lambda _: None)
    runner, tape, mt5, recorder = make(desk, gold, config, bars, paper=paper,
                                       state=tmp_path / "session" / "state.json",
                                       session=start, report=report, **params)
    return runner, tape, start, report


def report_dir(tmp_path):
    return tmp_path / "session" / "report"


def live_json(tmp_path):
    return json.loads((report_dir(tmp_path) / "live.json").read_text())


class TestSnapshot:
    def test_it_is_the_result_so_far_without_finishing(self, desk, gold, config):
        bars = bars_for(PARENT + [1499, 1499])
        strategy = MZ50Strategy(**desk)
        engine = Backtester(strategy, "XAUUSD", "H4", gold, config)
        engine.start(bars)
        while engine.processed < len(bars):
            engine.step()
        snap = engine.snapshot()
        assert snap.end == bars[-1].time and snap.bars_processed == len(bars)
        assert not any(t.reason is ExitReason.END_OF_DATA for t in snap.trades)
        assert snap.trades == engine.broker.trades and snap.trades is not engine.broker.trades
        engine.snapshot()                                   # asking twice changes nothing
        assert engine.snapshot().artifacts == snap.artifacts


class TestReportMirrorsTheBacktest:
    def test_the_same_files_as_a_saved_backtest_over_the_same_bars(
        self, desk, gold, config, tmp_path
    ):
        bars, params = scenario("chain_limit_fill", desk, gold, config)
        runner, tape, _, _ = session(tmp_path, desk, gold, config, bars, **params)
        runner.warm_up()
        play(runner, tape)
        runner.write_report()

        _, result = backtest(desk, gold, config, bars[:len(runner.bars)], **params)
        saved = save_result(result, compute(result).to_dict(), root=tmp_path / "bt")
        assert result.trades, "the scenario should trade"
        for table in TABLES:
            mine = settled(table, rows(report_dir(tmp_path) / f"{table}.csv"))
            theirs = settled(table, rows(saved / f"{table}.csv"))
            assert mine == theirs, table
        summary = json.loads((report_dir(tmp_path) / "summary.json").read_text())
        assert summary["period"]["bars"] == len(runner.bars)
        assert (report_dir(tmp_path) / "chart.html").exists()

    def test_the_journal_says_why(self, desk, gold, config):
        bars, params = scenario("two_close_exit", desk, gold, config)
        strategy, _ = backtest(desk, gold, config, bars, **params)
        events = [r["event"] for r in strategy.artifacts()["journal"]]
        for event in ("zone_created", "signal", "entered", "two_close", "exit_at_open", "closed"):
            assert event in events, event
        entered = next(r for r in strategy.artifacts()["journal"] if r["event"] == "entered")
        assert entered["detail"].startswith("SHORT 0.1 at") and "SL" in entered["detail"]


class TestSessionAccount:
    def test_opens_at_the_first_bar_seen_open_and_counts_only_from_there(
        self, desk, gold, config, tmp_path
    ):
        bars = bars_for(PARENT + [1499, 1499])
        closed = entry_index(desk, gold, config, bars) + 2   # the replay holds the trade
        runner, tape, start, _ = session(tmp_path, desk, gold, config, bars, closed=closed)
        runner.warm_up()
        assert start.live_from is None
        account = live_json(tmp_path)["account"]
        assert account["live_from"] is None and account["trades"] == 0

        play(runner, tape)
        runner.write_state()
        assert start.live_from == bars[closed + 1].time
        assert runner.backtest.broker.trades, "the replay's trade closed"
        state = read_state(tmp_path / "session" / "state.json")
        assert state["session"]["trades"] == 0             # entered before the session
        assert state["balance"] == config.initial_balance  # paper: the session's account

    def test_counts_what_the_strategy_trades_once_open(self, desk, gold, config, tmp_path):
        bars = bars_for(PARENT + [1499, 1499])
        runner, tape, start, _ = session(tmp_path, desk, gold, config, bars)
        runner.warm_up()
        play(runner, tape)
        runner.write_state()
        (trade,) = [t for t in runner.backtest.broker.trades if start.counts(t.entry_time)]
        state = read_state(tmp_path / "session" / "state.json")
        assert state["session"]["trades"] == 1
        assert state["session"]["realised"] == round(trade.net_pnl, 2)
        assert state["balance"] == round(config.initial_balance + trade.net_pnl, 2)

    def test_a_restart_carries_on_the_same_account_without_repeating_a_trade(
        self, desk, gold, config, tmp_path
    ):
        bars = bars_for(PARENT + [1499, 1499])
        first, tape, start, _ = session(tmp_path, desk, gold, config, bars)
        first.warm_up()
        play(first, tape)
        first.write_state()
        before = read_state(tmp_path / "session" / "state.json")["session"]

        # Stopped part-way through and started again at the end: the replay
        # catches up on the bars missed, and the account is the one it was.
        again, tape2, start2, _ = session(tmp_path, desk, gold, config, bars,
                                          closed=len(bars) - 1)
        assert start2.live_from == start.live_from
        again.warm_up()
        again.write_state()
        after = read_state(tmp_path / "session" / "state.json")["session"]
        assert after == before
        assert len(again.backtest.broker.trades) == len(first.backtest.broker.trades)

    def test_reset_opens_a_new_account(self, desk, gold, config, tmp_path):
        path = tmp_path / "session.json"
        start = SessionStart(path, 10_000.0)
        assert start.begin(datetime(2024, 1, 2, tzinfo=timezone.utc), datetime.now(timezone.utc))
        assert not start.begin(datetime(2024, 2, 1, tzinfo=timezone.utc), datetime.now(timezone.utc))
        assert SessionStart(path, 10_000.0).live_from == datetime(2024, 1, 2, tzinfo=timezone.utc)
        assert SessionStart(path, 10_000.0, reset=True).live_from is None
        assert not path.exists()


class TestLiveLayer:
    def test_status_and_open_book(self, desk, gold, config, tmp_path):
        bars = bars_for(PARENT + [1499, 1499])
        closed = entry_index(desk, gold, config, bars) + 1
        runner, tape, _, _ = session(tmp_path, desk, gold, config, bars, closed=closed)
        runner.warm_up()
        live = live_json(tmp_path)
        assert live["paper"] is True and live["mode"] == "shadow"
        assert live["status"]["phase"] == "in_position"
        assert live["status"]["zone"]["direction"] == "SHORT"
        (position,) = live["positions"]
        assert position["in_session"] is False and position["sl"] and position["tp"]
        assert live["last_closed_bar"] == bars[closed - 1].time.isoformat()
        assert [e["kind"] for e in live["events"]] == []   # nothing written to events.jsonl here

    def test_status_walks_through_the_phases(self, desk, gold, config):
        def phases(name):
            bars, params = scenario(name, desk, gold, config)
            strategy = MZ50Strategy(**{**desk, **params})
            engine = Backtester(strategy, "XAUUSD", "H4", gold, config)
            engine.start(bars)
            seen = []
            while engine.processed < len(bars):
                engine.step()
                phase = strategy.status()["phase"]
                if not seen or seen[-1] != phase:
                    seen.append(phase)
            return seen

        assert phases("take_profit")[:3] == ["waiting_signal", "entry_ordered", "in_position"]
        two_close = phases("two_close_exit")
        assert two_close.index("exit_warning") < two_close.index("exit_due")
        assert "limit_resting" in phases("chain_limit_fill")
        assert "chain_waiting_cross" in phases("chain_daily_cross")

    def test_pulse_names_the_report_the_page_should_show(self, desk, gold, config, tmp_path):
        runner, tape, _, report = session(tmp_path, desk, gold, config, bars_for(PARENT))
        runner.warm_up()
        tape.advance()
        runner.tick()
        runner.write_report()
        runner.write_state()
        pulse = json.loads((report_dir(tmp_path) / "pulse.json").read_text())
        live = live_json(tmp_path)
        assert pulse["running"] is True and pulse["report_error"] is None
        assert pulse["report_generated"] == live["generated"]
        assert pulse["report_bar"] == live["last_closed_bar"] == pulse["last_closed_bar"]

    def test_a_report_that_fails_never_stops_trading(self, desk, gold, config, tmp_path):
        bars = bars_for(PARENT + [1499, 1499])
        runner, tape, _, report = session(tmp_path, desk, gold, config, bars, paper=False)

        def broken(*args, **kwargs):
            raise RuntimeError("no chart today")

        runner.strategy.chart = broken
        runner.warm_up()
        play(runner, tape)
        runner.write_report()
        runner.write_state()
        assert runner.live.trades, "trading went on"
        assert "no chart today" in report.error
        pulse = json.loads((report_dir(tmp_path) / "pulse.json").read_text())
        assert "no chart today" in pulse["report_error"]

    @needs_node
    def test_the_page_runs_with_the_live_layer(self, desk, gold, config, tmp_path):
        bars = bars_for(PARENT + [1499, 1499])
        closed = entry_index(desk, gold, config, bars) + 1
        runner, tape, _, _ = session(tmp_path, desk, gold, config, bars, closed=closed)
        runner.warm_up()
        out = subprocess.run(["node", "tests/render_check.js",
                              str(report_dir(tmp_path) / "chart.html")],
                             capture_output=True, text=True)
        assert out.returncode == 0, out.stdout + out.stderr
        assert "OK: script ran to completion" in out.stdout
        assert "live blocks : 0" not in out.stdout
        assert "live logs   : 0" not in out.stdout

    @needs_node
    def test_the_generic_page_runs_with_the_live_layer(self, gold, config, tmp_path):
        from backtester.strategies.sma_cross import SmaCrossStrategy
        from conftest import series

        bars = series([1800 + 10 * (i % 7) for i in range(80)])
        start = SessionStart(tmp_path / "session.json", config.initial_balance)
        report = LiveReport(tmp_path / "report", config.__dict__, log=lambda _: None)
        strategy = SmaCrossStrategy(fast=2, slow=5, volume=0.1)
        engine = Backtester(strategy, "XAUUSD", "M15", gold, config)
        engine.start(bars)
        while engine.processed < len(bars):
            engine.step()
        start.begin(bars[40].time, datetime.now(timezone.utc))
        snapshot = {"mode": "shadow", "paper": True, "positions": [], "resting_orders": [],
                    "queued_orders": [], "forming_bar": None, "session": None}
        page = report.write(engine.snapshot(), strategy, bars, snapshot, running=True)
        out = subprocess.run(["node", "tests/render_check.js", str(page)],
                             capture_output=True, text=True)
        assert out.returncode == 0, out.stdout + out.stderr
        assert "live blocks : 0" not in out.stdout
