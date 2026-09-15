"""Trading live the way the backtest trades: `backtester/live/`.

The central property is the first class: stepped bar by bar against a terminal
that resolves stops, targets and limits by the backtest's own rules, the live
runner takes exactly the trades the backtest takes.
"""

from __future__ import annotations

import pytest

from backtester.core.engine import Backtester
from backtester.core.types import Bar, ExitReason, Side
from backtester.live import BrokerUnavailable, LiveRunner, Notifier
from backtester.live.events import (
    BAR_CLOSED,
    EXIT_INTENT,
    LIVE,
    MODE,
    ORDER_CANCELLED,
    ORDER_FILLED,
    ORDER_INTENT,
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


def make(desk, gold, config, bars, closed=QUIET, paper=False, **params):
    mt5 = FakeMT5(gold, config)
    tape = Tape(mt5, bars, closed)
    notify = Notifier("mz50", "XAUUSD")
    recorder = Recorder(mt5)
    notify.add(recorder)
    runner = LiveRunner(
        mt5=mt5, feed=tape, strategy=MZ50Strategy(**{**desk, **params}), symbol="XAUUSD",
        timeframe="H4", start=START, magic=MAGIC, notify=notify, instrument=gold,
        execution=config, poll_seconds=0, paper=paper, log=lambda _: None,
    )
    return runner, tape, mt5, recorder


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
