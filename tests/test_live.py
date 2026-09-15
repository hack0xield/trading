"""Trading live the way the backtest trades: `backtester/live/`.

The central property is the first class: stepped bar by bar against a terminal
that resolves stops, targets and limits by the backtest's own rules, the live
runner takes exactly the trades the backtest takes.
"""

from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import sys
import time
from datetime import date, datetime, timezone
from pathlib import Path

import pytest

from backtester.core.engine import Backtester
from backtester.core.types import Bar, ExitReason, OrderRequest, Side
from backtester.live import (
    AccountChanged,
    BrokerUnavailable,
    LiveRunner,
    MarginWatch,
    Notifier,
    StateFile,
)
from backtester.live.events import (
    BAR_CLOSED,
    ERROR,
    EXIT_INTENT,
    LIVE,
    MODE,
    ORDER_CANCELLED,
    ORDER_FILLED,
    ORDER_INTENT,
    ORDER_PLACED,
    ORDER_REJECTED,
    POSITION_CLOSED,
    SHADOW,
    STOPPED,
)
from backtester.strategies.margin_zones.mz50 import MZ50Strategy
from fake_mt5 import FakeMT5, Tape
from test_mz50 import START, bars_for, desk  # noqa: F401  (desk is a fixture)

MAGIC = 5050
PARENT = [1900, 1730, 1730]

#: The bars before the first signal, where the replay holds nothing.
QUIET = 20


def wicks(after: int, price: float, high: float, count: int = 12):
    from test_mz50 import TestChain

    return TestChain().wicks(after, price, high, count)


class Recorder:
    """A listener remembering every event, and how many requests preceded it."""

    def __init__(self, mt5):
        self.mt5 = mt5
        self.events = []

    def __call__(self, event):
        self.events.append((event, len(self.mt5.requests)))

    def kinds(self):
        return [e.kind for e, _ in self.events]

    def of(self, kind):
        return [e for e, _ in self.events if e.kind == kind]


ROOT = Path(__file__).resolve().parents[1]


def make(desk, gold, config, bars, closed=QUIET, paper=False, state=None, **params):
    mt5 = FakeMT5(gold, config)
    tape = Tape(mt5, bars, closed)
    notify = Notifier("mz50", "XAUUSD")
    recorder = Recorder(mt5)
    notify.add(recorder)
    state_file = None
    if state is not None:
        state_file = StateFile(state, notify, pid=4242, strategy="mz50", symbol="XAUUSD",
                               timeframe="H4", config="configs/test.yaml", magic=MAGIC,
                               paper=paper)
    runner = LiveRunner(
        mt5=mt5, feed=tape, strategy=MZ50Strategy(**{**desk, **params}), symbol="XAUUSD",
        timeframe="H4", start=START, magic=MAGIC, notify=notify, instrument=gold,
        execution=config, poll_seconds=0, paper=paper, state=state_file, log=lambda _: None,
    )
    return runner, tape, mt5, recorder


def read_state(path):
    return json.loads(Path(path).read_text())


def entry_index(desk, gold, config, bars, **params):
    """The bar the backtest's first trade entered on."""
    _, result = backtest(desk, gold, config, bars, **params)
    return next(i for i, b in enumerate(bars) if b.time == result.trades[0].entry_time)


def closed_when_sent(desk, gold, config, bars, wanted, **params):
    """How many bars had closed when the first request `wanted` picks was sent."""
    runner, tape, mt5, _ = make(desk, gold, config, bars, **params)
    runner.warm_up()
    while not tape.done:
        tape.advance()
        runner.tick()
        if any(wanted(r) for r in mt5.requests):
            return tape.closed
    raise AssertionError("no such request")


def is_entry(request):
    return request["action"] == FakeMT5.TRADE_ACTION_DEAL and "position" not in request


def is_close(request):
    return request["action"] == FakeMT5.TRADE_ACTION_DEAL and "position" in request


def is_limit(request):
    return request["action"] == FakeMT5.TRADE_ACTION_PENDING


def short_order(tag="t"):
    return OrderRequest(side=Side.SELL, volume=0.1, sl_price=2100.0, tp_price=1500.0, tag=tag)


def entries_sent(mt5):
    return [r for r in mt5.requests if is_entry(r)]


def play(runner, tape, until=None):
    """Advance the tape bar by bar, polling once per bar."""
    while not tape.done and (until is None or tape.closed < until):
        tape.advance()
        runner.tick()


def backtest(desk, gold, config, bars, **params):
    strategy = MZ50Strategy(**{**desk, **params})
    result = Backtester(strategy=strategy, symbol="XAUUSD", timeframe="H4",
                        instrument=gold, execution=config).run(bars)
    return strategy, result


def fingerprint(trades):
    return [(t.side, t.entry_time, t.entry_price, t.sl, t.tp, t.exit_time, t.exit_price, t.reason)
            for t in trades if t.reason is not ExitReason.END_OF_DATA]


def entries(trades):
    return [(t.side, t.entry_time, t.entry_price) for t in trades]


def records(strategy):
    """What the strategy wrote down, less trade ids and what only an end of data writes."""
    out = strategy.artifacts()
    return {
        name: [{k: v for k, v in row.items() if k not in ("parent_trade_id", "trade_id")}
               for row in out.get(name, []) if not row.get("event", "").startswith("end_of_data")]
        for name in ("crossings", "signals", "chain", "warnings")
    }


def scenario(name, desk, gold, config):
    tail, extra, params = SCENARIOS[name]
    bars = bars_for(tail)
    if extra == "wicks":
        bars += wicks(len(bars), 1400.0, 1480.0)
    return bars, params


SCENARIOS = {
    "take_profit": (PARENT + [1499, 1499], (), {}),
    "stop_loss": (PARENT + [2100, 2100], (), {}),
    "two_close_exit": (PARENT + [1800, 1800, 1800], (), {}),
    "chain_daily_cross": (
        PARENT + [1499, 1499, 1470, 1470, 1200, 1200], (), {"trend_follow": True},
    ),
    "chain_limit_fill": (PARENT + [1400], "wicks", {"trend_follow": True}),
    "chain_limit_void": (PARENT + [1400, 1200, 1200], (), {"trend_follow": True}),
    "superseded_chain": (PARENT + [1499, 2050, 2050, 1780, 1780], (), {"trend_follow": True}),
}


class TestMirrorsTheBacktest:
    @pytest.mark.parametrize("name", sorted(SCENARIOS))
    def test_the_same_trades_at_the_same_prices(self, name, desk, gold, config):
        bars, params = scenario(name, desk, gold, config)
        _, result = backtest(desk, gold, config, bars, **params)
        runner, tape, _, _ = make(desk, gold, config, bars, **params)

        runner.warm_up()
        assert runner.live is not None
        play(runner, tape)

        expected = fingerprint(result.trades)
        assert expected, "the scenario should trade"
        assert fingerprint(runner.live.trades) == expected
        held = runner.live.trades + runner.live.positions
        assert entries(held) == entries(result.trades)

    @pytest.mark.parametrize("name", sorted(SCENARIOS))
    def test_the_same_records(self, name, desk, gold, config):
        """Crossings, signals, chain steps and warnings, all but the trade ids."""
        bars, params = scenario(name, desk, gold, config)
        strategy, _ = backtest(desk, gold, config, bars, **params)
        runner, tape, _, _ = make(desk, gold, config, bars, **params)
        runner.warm_up()
        play(runner, tape)
        assert records(runner.strategy) == records(strategy)

    def test_the_stop_is_measured_from_the_fill_before_the_entry_bar_plays(
        self, desk, gold, config
    ):
        """The entry bar opens below the signal close and reaches the re-measured stop."""
        bars, _ = scenario("take_profit", desk, gold, config)
        _, result = backtest(desk, gold, config, bars)
        i = next(i for i, b in enumerate(bars) if b.time == result.trades[0].entry_time)
        bars[i] = Bar(time=bars[i].time, open=1725.0, high=2005.0, low=1725.0, close=1730.0)
        _, result = backtest(desk, gold, config, bars)
        assert result.trades[0].reason is ExitReason.STOP_LOSS

        runner, tape, _, _ = make(desk, gold, config, bars)
        runner.warm_up()
        play(runner, tape)
        assert fingerprint(runner.live.trades) == fingerprint(result.trades)

    def test_orders_carry_the_magic_number_and_the_tag(self, desk, gold, config):
        runner, tape, mt5, _ = make(desk, gold, config, bars_for(PARENT + [1499, 1499]))
        runner.warm_up()
        play(runner, tape)
        (entry, *_) = [r for r in mt5.requests if r["action"] == mt5.TRADE_ACTION_DEAL]
        assert entry["magic"] == MAGIC
        assert entry["comment"].startswith("mz50 z")
        assert entry["sl"] > 0 and entry["tp"] > 0


class TestEvents:
    def test_the_intent_is_told_before_the_order_is_sent(self, desk, gold, config):
        runner, tape, mt5, recorder = make(desk, gold, config, bars_for(PARENT + [1499, 1499]))
        runner.warm_up()
        play(runner, tape)
        intent = recorder.events[recorder.kinds().index(ORDER_INTENT)]
        filled = recorder.events[recorder.kinds().index(ORDER_FILLED)]
        assert intent[1] == 0                      # nothing sent yet
        assert filled[1] == 1
        assert intent[0].data["side"] == "SELL"
        assert filled[0].data["ticket"] in {d.position_id for d in mt5.deals}

    def test_a_close_is_told_with_its_reason(self, desk, gold, config):
        runner, tape, _, recorder = make(desk, gold, config, bars_for(PARENT + [1800, 1800, 1800]))
        runner.warm_up()
        play(runner, tape)
        assert EXIT_INTENT in recorder.kinds()
        (closed,) = recorder.of(POSITION_CLOSED)
        assert closed.data["reason"] == "STRATEGY"

    def test_a_server_stop_is_told_as_it_is_seen_and_reaches_the_strategy_at_the_close(
        self, desk, gold, config
    ):
        runner, tape, mt5, recorder = make(desk, gold, config, bars_for(PARENT + [2100, 2100]))
        runner.warm_up()
        while not mt5.positions:
            tape.advance()
            runner.tick()
        mt5.walk(_through(tape, 2200.0))              # the server stops it mid-bar
        runner.tick()                                 # a poll inside the bar
        assert [e.data["reason"] for e in recorder.of(POSITION_CLOSED)] == ["STOP_LOSS"]
        assert runner.strategy.artifacts().get("cases") is None
        tape.closed += 1
        mt5.open_bar(tape.bars[tape.closed])
        runner.tick()
        assert runner.strategy.artifacts()["cases"][0]["exit_reason"] == "stop_loss"

    def test_a_broken_listener_does_not_stop_trading(self, desk, gold, config):
        runner, tape, _, _ = make(desk, gold, config, bars_for(PARENT + [1499, 1499]))

        def broken(event):
            raise RuntimeError("listener down")

        runner.notify.listeners.insert(0, broken)
        runner.warm_up()
        play(runner, tape)
        assert runner.live.trades

    def test_every_closed_bar_is_reported(self, desk, gold, config):
        bars = bars_for(PARENT)
        runner, tape, _, recorder = make(desk, gold, config, bars)
        runner.warm_up()
        play(runner, tape)
        assert len(recorder.of(BAR_CLOSED)) == len(bars) - QUIET - 1


class TestHandover:
    def test_a_flat_replay_trades_live_from_the_start(self, desk, gold, config):
        runner, _, _, recorder = make(desk, gold, config, bars_for(PARENT))
        runner.warm_up()
        assert runner.live is not None
        assert recorder.of(MODE)[0].mode == LIVE

    def test_a_position_the_account_lacks_is_not_opened_late(self, desk, gold, config):
        bars = bars_for(PARENT + [1499, 1499])
        _, result = backtest(desk, gold, config, bars)
        entry = next(i for i, b in enumerate(bars) if b.time == result.trades[0].entry_time)
        runner, tape, mt5, recorder = make(desk, gold, config, bars, closed=entry + 2)

        runner.warm_up()
        assert runner.live is None
        assert runner.backtest.broker.positions
        play(runner, tape)
        assert not [r for r in mt5.requests if r["action"] == mt5.TRADE_ACTION_DEAL]
        assert recorder.of(POSITION_CLOSED)[0].mode == SHADOW
        assert runner.live is not None                 # handed over once the replay was flat

    def test_a_signal_on_the_last_replayed_bar_is_not_chased(self, desk, gold, config):
        bars = bars_for(PARENT + [1499, 1499])
        _, result = backtest(desk, gold, config, bars)
        entry = next(i for i, b in enumerate(bars) if b.time == result.trades[0].entry_time)
        runner, _, mt5, _ = make(desk, gold, config, bars, closed=entry)
        runner.warm_up()
        assert runner.backtest.broker.pending
        assert runner.live is None
        assert mt5.requests == []

    def test_the_accounts_matching_position_is_adopted(self, desk, gold, config):
        bars = bars_for(PARENT + [1499, 1499])
        _, result = backtest(desk, gold, config, bars)
        trade = result.trades[0]
        entry = next(i for i, b in enumerate(bars) if b.time == trade.entry_time)
        runner, tape, mt5, _ = make(desk, gold, config, bars, closed=entry + 2)
        row = mt5.hold(trade.side, trade.entry_price, trade.sl, trade.tp, MAGIC, trade.tag)

        runner.warm_up()
        assert runner.live is not None
        assert [p.id for p in runner.live.positions] == [row.ticket]
        play(runner, tape)
        assert fingerprint(runner.live.trades)[0][5:] == fingerprint(result.trades)[0][5:]

    def test_a_leftover_of_ours_is_closed_and_anyone_elses_left_alone(self, desk, gold, config):
        runner, _, mt5, _ = make(desk, gold, config, bars_for(PARENT))
        ours = mt5.hold(Side.BUY, 1900.0, 1000.0, 2500.0, MAGIC, "mz50 z99 old")
        theirs = mt5.hold(Side.BUY, 1900.0, 1000.0, 2500.0, 0, "manual")
        runner.warm_up()
        assert ours.ticket not in mt5.positions
        assert theirs.ticket in mt5.positions
        assert runner.live.trades == []              # not the strategy's trade

    def test_paper_sends_nothing(self, desk, gold, config):
        runner, tape, mt5, recorder = make(desk, gold, config, bars_for(PARENT + [1499, 1499]),
                                           paper=True)
        runner.warm_up()
        play(runner, tape)
        assert mt5.requests == []
        assert runner.live is None
        assert {e.mode for e, _ in recorder.events} == {SHADOW}
        assert ORDER_FILLED in recorder.kinds() and POSITION_CLOSED in recorder.kinds()


class TestBroker:
    def test_a_rejected_order_leaves_the_strategy_flat(self, desk, gold, config):
        runner, tape, mt5, recorder = make(desk, gold, config, bars_for(PARENT + [1499, 1499]))
        runner.warm_up()
        mt5.reject_next = True
        play(runner, tape)
        assert recorder.of(ORDER_REJECTED)
        assert runner.live.trades == []
        assert runner.strategy._pending is None

    def test_a_stop_already_passed_closes_instead_of_moving(self, desk, gold, config):
        runner, tape, mt5, _ = make(desk, gold, config, bars_for(PARENT + [1499, 1499]))
        runner.warm_up()
        while not runner.live.positions:
            tape.advance()
            runner.tick()
        position = runner.live.positions[0]
        runner.live.modify(position, sl=mt5.ask - 1.0)   # a short's stop below the market
        assert runner.live.positions == []
        assert runner.live.trades[0].reason is ExitReason.STOP_LOSS

    def test_a_void_level_reached_removes_the_resting_order(self, desk, gold, config):
        bars = bars_for(PARENT + [1400, 1200, 1200])
        runner, tape, mt5, recorder = make(desk, gold, config, bars, trend_follow=True)
        runner.warm_up()
        play(runner, tape)
        assert [r for r in mt5.requests if r["action"] == mt5.TRADE_ACTION_PENDING]
        assert mt5.orders == {}
        assert [e.data["reason"] for e in recorder.of(ORDER_CANCELLED)] == ["void level reached"]

    def test_a_terminal_away_at_the_close_processes_the_bar_once_it_returns(
        self, desk, gold, config
    ):
        runner, tape, mt5, recorder = make(desk, gold, config, bars_for(PARENT))
        runner.warm_up()
        tape.advance()
        mt5.online = False
        with pytest.raises(BrokerUnavailable):
            runner.tick()
        assert len(runner.bars) == QUIET
        mt5.online = True
        runner.tick()
        runner.tick()
        assert len(recorder.of(BAR_CLOSED)) == 1

    def test_the_stop_file_ends_the_run(self, desk, gold, config, tmp_path):
        runner, _, _, recorder = make(desk, gold, config, bars_for(PARENT))
        runner.stop_file = tmp_path / "stop"
        runner.notify.add(lambda e: runner.stop_file.touch() if e.kind == MODE else None)
        runner.run()
        assert recorder.kinds()[-1] == STOPPED
        assert not runner.stop_file.exists()


def _through(tape, high):
    """The forming bar, reaching up to `high`."""
    bar = tape.bars[tape.closed]
    return Bar(time=bar.time, open=bar.open, high=high, low=bar.low, close=bar.close,
               volume=bar.volume, spread=bar.spread)


class TestAccountGuard:
    def test_a_login_switch_blocks_the_send_and_rejects_nothing(self, desk, gold, config):
        runner, _, mt5, recorder = make(desk, gold, config, bars_for(PARENT))
        runner.warm_up()
        live = runner.live
        live.submit(short_order())
        mt5.login = 2
        live.execute_pending()

        assert mt5.requests == []
        assert len(live.pending) == 1
        assert not recorder.of(ORDER_REJECTED) and not recorder.of(ORDER_INTENT)
        (error,) = recorder.of(ERROR)
        assert (error.data["expected_login"], error.data["actual_login"]) == (1, 2)
        assert error.data["retrying"] is True

        mt5.login = 1
        live.execute_pending()
        assert len(entries_sent(mt5)) == 1
        assert live.positions and not live.pending

    def test_moves_and_closes_wait_for_the_account_and_then_go_through(self, desk, gold, config):
        runner, _, mt5, recorder = make(desk, gold, config, bars_for(PARENT))
        runner.warm_up()
        live = runner.live
        live.submit(short_order())
        live.execute_pending()
        position = live.positions[0]

        mt5.login = 2
        live.modify(position, sl=2050.0)
        assert position.sl == 2100.0
        mt5.login = 1
        live.retry()
        assert position.sl == 2050.0
        assert mt5.positions[position.id].sl == 2050.0

        mt5.login = 2
        assert live.close_position(position) is None
        assert position.id in mt5.positions
        mt5.login = 1
        live.retry()
        assert position.id not in mt5.positions
        assert [t.reason for t in live.trades] == [ExitReason.STRATEGY]
        assert recorder.kinds().count(EXIT_INTENT) == 1
        assert not recorder.of(ORDER_REJECTED)

    def test_the_runner_waits_out_a_switch_and_still_trades_as_the_backtest(
        self, desk, gold, config
    ):
        bars = bars_for(PARENT + [1499, 1499])
        _, result = backtest(desk, gold, config, bars)
        runner, tape, mt5, recorder = make(desk, gold, config, bars)
        runner.warm_up()
        play(runner, tape, until=entry_index(desk, gold, config, bars) - 1)

        tape.advance()
        mt5.login = 2
        with pytest.raises(AccountChanged):
            runner.tick()
        assert mt5.requests == []
        mt5.login = 1
        runner.tick()
        play(runner, tape)
        assert not recorder.of(ORDER_REJECTED)
        assert fingerprint(runner.live.trades) == fingerprint(result.trades)


class TestUnansweredSends:
    def test_one_that_reached_the_broker_is_adopted_not_sent_again(self, desk, gold, config):
        bars = bars_for(PARENT + [1499, 1499])
        _, result = backtest(desk, gold, config, bars)
        runner, tape, mt5, recorder = make(desk, gold, config, bars)
        runner.warm_up()
        mt5.silence = ["lost"]
        play(runner, tape)

        assert len(entries_sent(mt5)) == 1
        assert recorder.kinds().count(ORDER_FILLED) == 1
        assert not recorder.of(ORDER_REJECTED)
        assert fingerprint(runner.live.trades) == fingerprint(result.trades)

    def test_one_that_shows_up_late_is_found_before_it_is_sent_again(self, desk, gold, config):
        runner, _, mt5, recorder = make(desk, gold, config, bars_for(PARENT))
        runner.warm_up()
        live = runner.live
        live.submit(short_order("late"))
        mt5.silence = ["late"]
        live.execute_pending()
        assert live.pending and not live.positions

        mt5.catch_up()
        live.execute_pending()
        assert len(entries_sent(mt5)) == 1
        assert [p.id for p in live.positions] == list(mt5.positions)
        assert recorder.kinds().count(ORDER_FILLED) == 1

    def test_one_that_never_arrived_is_sent_again_within_the_bar(self, desk, gold, config):
        bars = bars_for(PARENT + [1499, 1499])
        _, result = backtest(desk, gold, config, bars)
        runner, tape, mt5, recorder = make(desk, gold, config, bars)
        runner.warm_up()
        play(runner, tape, until=entry_index(desk, gold, config, bars) - 1)

        mt5.silence = ["unsent"]
        tape.advance()
        runner.tick()                                  # the open: no answer, nothing there
        assert len(entries_sent(mt5)) == 1 and not runner.live.positions
        runner.tick()                                  # a poll inside the same bar
        assert len(entries_sent(mt5)) == 2 and runner.live.positions
        assert recorder.kinds().count(ORDER_INTENT) == 1
        play(runner, tape)
        assert not recorder.of(ORDER_REJECTED)
        assert fingerprint(runner.live.trades) == fingerprint(result.trades)

    def test_one_still_unanswered_when_the_bar_ends_is_rejected(self, desk, gold, config):
        bars = bars_for(PARENT + [1499, 1499])
        runner, tape, mt5, recorder = make(desk, gold, config, bars)
        runner.warm_up()
        play(runner, tape, until=entry_index(desk, gold, config, bars) - 1)

        mt5.silence = ["unsent", "unsent"]
        tape.advance()
        runner.tick()
        runner.tick()
        assert not recorder.of(ORDER_REJECTED)
        tape.advance()
        runner.tick()
        (rejected,) = recorder.of(ORDER_REJECTED)
        assert rejected.data["reason"].startswith("not sent before its bar ended")
        assert mt5.positions == {} and runner.live.positions == []

    def test_a_limit_that_reached_the_broker_rests(self, desk, gold, config):
        bars = bars_for(PARENT + [1400, 1400])
        closed = closed_when_sent(desk, gold, config, bars, is_limit, trend_follow=True)
        runner, tape, mt5, recorder = make(desk, gold, config, bars, trend_follow=True)
        runner.warm_up()
        play(runner, tape, until=closed - 1)
        mt5.silence = ["lost"]
        tape.advance()
        runner.tick()

        assert len([r for r in mt5.requests if is_limit(r)]) == 1
        (placed,) = recorder.of(ORDER_PLACED)
        assert placed.data["ticket"] in mt5.orders
        assert [row["ticket"] for row in runner.live.resting_rows()] == [placed.data["ticket"]]

    @pytest.mark.parametrize("silence, sends", [("lost", 1), ("unsent", 2)])
    def test_a_close_is_booked_once_the_broker_no_longer_holds_it(
        self, silence, sends, desk, gold, config
    ):
        bars = bars_for(PARENT + [1800, 1800, 1800])
        _, result = backtest(desk, gold, config, bars)
        closed = closed_when_sent(desk, gold, config, bars, is_close)
        runner, tape, mt5, recorder = make(desk, gold, config, bars)
        runner.warm_up()
        play(runner, tape, until=closed - 1)

        mt5.silence = [silence]
        tape.advance()
        runner.tick()                                  # the open: the close gets no answer
        runner.tick()                                  # the next poll
        play(runner, tape)
        assert len([r for r in mt5.requests if is_close(r)]) == sends
        assert recorder.kinds().count(EXIT_INTENT) == 1
        assert not recorder.of(ORDER_REJECTED)
        assert fingerprint(runner.live.trades) == fingerprint(result.trades)


class TestUntracked:
    def test_a_position_under_our_magic_nothing_tracks_is_reported_once(
        self, desk, gold, config
    ):
        runner, tape, mt5, recorder = make(desk, gold, config, bars_for(PARENT))
        runner.warm_up()
        stray = mt5.hold(Side.BUY, 1900.0, 1000.0, 2500.0, MAGIC, "stray")
        tape.advance()
        for _ in range(3):
            runner.tick()

        reports = [e for e in recorder.of(ERROR) if e.data.get("ticket") == stray.ticket]
        assert len(reports) == 1
        assert "does not track" in reports[0].data["error"]
        assert stray.ticket in mt5.positions               # reported, not touched


class TestState:
    def test_shadow_shows_the_replays_book_and_says_so(self, desk, gold, config, tmp_path):
        bars = bars_for(PARENT + [1499, 1499])
        closed = entry_index(desk, gold, config, bars) + 2
        runner, _, _, _ = make(desk, gold, config, bars, closed=closed,
                               state=tmp_path / "state.json")
        runner.warm_up()
        state = read_state(tmp_path / "state.json")

        assert state["running"] is True and state["stopped_at"] is None
        assert (state["mode"], state["source"], state["paper"]) == ("shadow", "replay", False)
        assert (state["strategy"], state["symbol"], state["timeframe"]) == ("mz50", "XAUUSD", "H4")
        assert (state["config"], state["magic"], state["pid"]) == ("configs/test.yaml", MAGIC, 4242)
        assert state["account"] == {"login": 1, "server": "Fake", "trade_mode": "demo"}
        (position,) = state["positions"]
        assert position["ticket"] == runner.backtest.broker.positions[0].id
        assert position["side"] == "SELL" and isinstance(position["profit"], float)
        assert state["last_closed_bar"] == bars[closed - 1].time.isoformat()
        assert state["forming_bar"] == bars[closed].time.isoformat()
        assert state["balance"] == 10_000.0 and state["last_error"] is None
        assert state["margin"]["contract"] == "TEST" and state["margin"]["stale"] is True

    def test_live_shows_the_accounts_positions(self, desk, gold, config, tmp_path):
        runner, tape, mt5, _ = make(desk, gold, config, bars_for(PARENT + [1499, 1499]),
                                    state=tmp_path / "state.json")
        runner.warm_up()
        while not runner.live.positions:
            tape.advance()
            runner.tick()
        runner.tick()                                  # a poll reads the floating profit
        runner.write_state()
        state = read_state(tmp_path / "state.json")

        assert (state["mode"], state["source"]) == ("live", "account")
        (position,) = state["positions"]
        assert position["ticket"] in mt5.positions
        assert position["sl"] and position["tp"] and position["profit"] == 0.0
        assert state["queued_orders"] == [] and state["resting_orders"] == []

    def test_stopping_withdraws_resting_orders_and_writes_the_final_state(
        self, desk, gold, config, tmp_path
    ):
        bars = bars_for(PARENT + [1400, 1400])
        runner, tape, mt5, recorder = make(desk, gold, config, bars, trend_follow=True,
                                           state=tmp_path / "state.json")
        runner.warm_up()
        while not mt5.orders:
            tape.advance()
            runner.tick()
        runner.write_state()
        assert read_state(tmp_path / "state.json")["resting_orders"][0]["void"] is not None

        runner.shutdown()
        assert mt5.orders == {}
        assert recorder.of(ORDER_CANCELLED)[-1].data["reason"] == "runner stopped"
        assert recorder.kinds()[-1] == STOPPED
        state = read_state(tmp_path / "state.json")
        assert state["running"] is False and state["stopped_at"] and state["failure"] is None
        assert state["resting_orders"] == []

    def test_the_last_error_is_kept(self, desk, gold, config, tmp_path):
        runner, _, mt5, _ = make(desk, gold, config, bars_for(PARENT),
                                 state=tmp_path / "state.json")
        runner.warm_up()
        mt5.login = 7
        runner.live.submit(short_order())
        runner.live.execute_pending()
        runner.write_state()
        state = read_state(tmp_path / "state.json")
        assert "account 7" in state["last_error"]["error"]
        assert state["queued_orders"][0]["problem"]


class TestMargin:
    def test_a_reading_past_the_threshold_is_stale_until_a_newer_one_arrives(self, desk):
        watch = MarginWatch(MZ50Strategy(**desk), stale_after_days=30)
        status = watch.status(date(2023, 3, 1))
        assert (status["as_of"], status["age_days"], status["stale"]) == ("2023-01-01", 59, True)
        assert status["maintenance"] == 50_000.0 and status["stale_after_days"] == 30

        with open(desk["margin_log"], "a") as fh:
            fh.write("TEST,2023-02-20,52000.0,,t,\n")
        status = watch.status(date(2023, 3, 1))
        assert (status["as_of"], status["age_days"], status["stale"]) == ("2023-02-20", 9, False)

    def test_a_strategy_without_a_margin_log_has_no_block(self):
        from backtester.strategies import get_strategy

        assert MarginWatch(get_strategy("day_open")()).status() is None

    def test_the_runner_clears_the_warning_without_a_restart(self, desk, gold, config, tmp_path):
        runner, tape, _, recorder = make(desk, gold, config, bars_for(PARENT),
                                         state=tmp_path / "state.json")
        runner.warm_up()
        assert recorder.of("started")[0].data["margin"]["stale"] is True
        today = datetime.now(timezone.utc).date().isoformat()
        with open(desk["margin_log"], "a") as fh:
            fh.write(f"TEST,{today},52000.0,,t,\n")
        tape.advance()
        runner.tick()
        runner.write_state()
        margin = read_state(tmp_path / "state.json")["margin"]
        assert (margin["as_of"], margin["stale"]) == (today, False)


class TestStartupFailures:
    def test_a_failure_in_the_replay_leaves_an_error_and_a_stopped_state(
        self, desk, gold, config, tmp_path
    ):
        runner, _, _, recorder = make(desk, gold, config, bars_for(PARENT), closed=0,
                                      state=tmp_path / "state.json")
        with pytest.raises(ValueError, match="No closed"):
            runner.run()
        (error,) = recorder.of(ERROR)
        assert error.data["retrying"] is False and "No closed" in error.data["error"]
        state = read_state(tmp_path / "state.json")
        assert state["running"] is False and "No closed" in state["failure"]

    def run_script(self, tmp_path, monkeypatch, mt5, params, probe=None):
        import scripts.run_live as run_live

        monkeypatch.setitem(sys.modules, "MetaTrader5", mt5)
        monkeypatch.setattr(run_live, "LIVE_DIR", tmp_path / "runs-live")
        monkeypatch.setattr(run_live, "connect", lambda _: None)
        monkeypatch.setattr(run_live, "probe_terminal", probe or _probe_answers)
        config = tmp_path / "run.json"
        config.write_text(json.dumps({"strategy": "mz50", "symbol": "XAUUSD", "timeframe": "H4",
                                      "start": "2024-01-01", "params": params}))
        code = run_live.main(["--config", str(config), "--quiet"])
        session = tmp_path / "runs-live" / f"mz50_XAUUSD_{run_live.default_magic('mz50', 'XAUUSD')}"
        return code, config, session

    def test_a_refused_account_is_written_to_the_session(
        self, desk, gold, config, tmp_path, monkeypatch, capsys
    ):
        mt5 = FakeMT5(gold, config)
        mt5.trade_mode = mt5.ACCOUNT_TRADE_MODE_REAL
        code, path, session = self.run_script(tmp_path, monkeypatch, mt5, desk)

        assert code == 1
        lines = (session / "events.jsonl").read_text().splitlines()
        (event,) = [json.loads(line) for line in lines]
        assert event["kind"] == "error" and event["retrying"] is False
        assert "real-money" in event["error"]
        state = read_state(session / "state.json")
        assert state["running"] is False and "real-money" in state["failure"]
        assert (state["config"], state["strategy"], state["paper"]) == (str(path), "mz50", False)
        assert "real-money" in capsys.readouterr().err

    def test_algo_trading_off_is_written_to_the_session(
        self, desk, gold, config, tmp_path, monkeypatch
    ):
        mt5 = FakeMT5(gold, config)
        mt5.algo_trading = False
        code, _, session = self.run_script(tmp_path, monkeypatch, mt5, desk)
        assert code == 1
        assert "Algo Trading is off" in read_state(session / "state.json")["failure"]

    def test_a_terminal_that_never_answers_is_reported(
        self, desk, gold, config, tmp_path, monkeypatch
    ):
        import scripts.run_live as run_live

        monkeypatch.setattr(run_live, "PROBE", "import time; time.sleep(30)")
        with pytest.raises(run_live.TerminalTimeout, match="did not answer"):
            run_live.probe_terminal(0.5)

        def hung(seconds):
            raise run_live.TerminalTimeout("the MT5 terminal did not answer a login within 1 s")

        code, _, session = self.run_script(tmp_path, monkeypatch, FakeMT5(gold, config), desk,
                                           probe=hung)
        assert code == 1
        state = read_state(session / "state.json")
        assert state["running"] is False and "did not answer" in state["failure"]

    def test_a_failed_probe_says_why(self, monkeypatch):
        import scripts.run_live as run_live

        monkeypatch.setattr(run_live, "PROBE", "raise SystemExit('MT5 initialize failed: nope')")
        with pytest.raises(SystemExit, match="initialize failed: nope"):
            run_live.probe_terminal(10)

    def test_the_session_exists_before_the_terminal_is_touched(
        self, desk, gold, config, tmp_path, monkeypatch
    ):
        import scripts.run_live as run_live

        seen = {}

        def connect(_):
            (session,) = (tmp_path / "runs-live").iterdir()
            seen.update(read_state(session / "state.json"))
            raise SystemExit("stop here")

        monkeypatch.setitem(sys.modules, "MetaTrader5", FakeMT5(gold, config))
        monkeypatch.setattr(run_live, "LIVE_DIR", tmp_path / "runs-live")
        monkeypatch.setattr(run_live, "probe_terminal", _probe_answers)
        monkeypatch.setattr(run_live, "connect", connect)
        path = tmp_path / "run.json"
        path.write_text(json.dumps({"strategy": "mz50", "symbol": "XAUUSD", "timeframe": "H4",
                                    "start": "2024-01-01", "params": desk}))
        assert run_live.main(["--config", str(path), "--quiet"]) == 1
        assert seen["running"] is True and seen["updated_at"] and seen["mode"] is None

    def test_an_unreadable_config_fails_before_any_session(self, tmp_path, monkeypatch, capsys):
        import scripts.run_live as run_live

        monkeypatch.setattr(run_live, "LIVE_DIR", tmp_path / "runs-live")
        assert run_live.main(["--config", str(tmp_path / "missing.yaml")]) == 1
        assert "missing.yaml" in capsys.readouterr().err
        assert not (tmp_path / "runs-live").exists()


FAKE_WINE = """#!/usr/bin/env python3
import os, subprocess, sys, time
args = sys.argv[1:]
if args[0] != "--child":
    sys.exit(subprocess.Popen([sys.executable, __file__, "--child", *args]).wait())
stop = args[args.index("--stop-file") + 1]
with open(os.environ["FAKE_RUNNER_LOG"], "a") as fh:
    fh.write(f"runner {os.getpid()}\\n")
while not (os.path.exists(stop) and not os.environ.get("IGNORE_STOP")):
    time.sleep(0.05)
with open(os.environ["FAKE_RUNNER_LOG"], "a") as fh:
    fh.write("stopped cleanly\\n")
"""


@pytest.mark.skipif(not (shutil.which("bash") and shutil.which("pkill")),
                    reason="needs bash and pkill")
class TestWrapper:
    """`run-live.sh` under a stand-in for Wine: a launcher and the runner it starts."""

    def start(self, tmp_path, **env):
        repo, bin_dir = tmp_path / "repo", tmp_path / "bin"
        (repo / "scripts").mkdir(parents=True)
        bin_dir.mkdir()
        shutil.copy(ROOT / "scripts" / "run-live.sh", repo / "scripts" / "run-live.sh")
        wine = bin_dir / "wine"
        wine.write_text(FAKE_WINE)
        wine.chmod(0o755)
        log = tmp_path / "runner.log"
        environment = {**os.environ, "PATH": f"{bin_dir}:{os.environ['PATH']}",
                       "FAKE_RUNNER_LOG": str(log), **env}
        wrapper = subprocess.Popen(["bash", str(repo / "scripts" / "run-live.sh"), "-c", "x.yaml"],
                                   env=environment, start_new_session=True)
        runner = int(wait_for(lambda: _runner_pid(log)))
        return wrapper, runner, log, repo, wine, environment

    def test_sigterm_to_the_wrapper_alone_stops_the_runner_cleanly(self, tmp_path):
        wrapper, _, log, repo, _, _ = self.start(tmp_path)
        wrapper.send_signal(signal.SIGTERM)
        assert wrapper.wait(timeout=10) == 0
        assert "stopped cleanly" in log.read_text()
        assert not list((repo / "runs-live").glob("stop-*"))

    def test_a_runner_that_will_not_stop_is_killed_and_no_other_runner_is(self, tmp_path):
        wrapper, runner, _, repo, wine, environment = self.start(
            tmp_path, IGNORE_STOP="1", RUN_LIVE_GRACE="1",
        )
        other = subprocess.Popen(
            [sys.executable, str(wine), "--child", "C:\\Python311\\python.exe",
             "scripts/run_live.py", "--stop-file", f"runs-live/stop-{wrapper.pid}0"],
            cwd=repo, env={**environment, "FAKE_RUNNER_LOG": str(tmp_path / "other.log")},
        )
        try:
            wait_for(lambda: (tmp_path / "other.log").exists())
            wrapper.send_signal(signal.SIGTERM)
            assert wrapper.wait(timeout=10) == 1
            assert wait_for(lambda: not _alive(runner))
            assert other.poll() is None
        finally:
            other.kill()
            other.wait()


def _probe_answers(seconds):
    """A terminal that answers the login probe."""


def wait_for(check, timeout: float = 10.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = check()
        if value:
            return value
        time.sleep(0.05)
    raise AssertionError("timed out")


def _runner_pid(log: Path):
    if not log.exists():
        return None
    lines = [line for line in log.read_text().splitlines() if line.startswith("runner ")]
    return lines[0].split()[1] if lines else None


def _alive(pid: int) -> bool:
    try:
        with open(f"/proc/{pid}/stat") as fh:
            return fh.read().split(") ")[1][0] != "Z"
    except FileNotFoundError:
        return False
