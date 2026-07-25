"""Engine loop: ordering of hooks, and the guarantee that fills cannot cheat."""

from __future__ import annotations

import pytest

from backtester.core.context import BarOpen, Context
from backtester.core.engine import Backtester, EngineConfig
from backtester.core.strategy import Strategy
from backtester.core.types import Bar, ExitReason
from backtester.utils.params import StrategyParams
from conftest import series


def run(strategy, bars, gold, config, **engine_kwargs):
    return Backtester(
        strategy=strategy,
        symbol="XAUUSD",
        timeframe="M15",
        instrument=gold,
        execution=config,
        engine=EngineConfig(**engine_kwargs),
    ).run(bars)


class BuyOnOpen(Strategy):
    name = "test_buy_on_open"
    params_class = StrategyParams

    def on_start(self, ctx):
        self.done = False

    def on_bar_open(self, ctx: Context, event: BarOpen):
        if not self.done:
            ctx.buy(0.1)
            self.done = True


class BuyOnClose(Strategy):
    name = "test_buy_on_close"
    params_class = StrategyParams

    def on_start(self, ctx):
        self.done = False

    def on_bar(self, ctx: Context, bar: Bar):
        if not self.done:
            ctx.buy(0.1)
            self.done = True


class TestFillTiming:
    """The core anti-lookahead property, stated as two tests."""

    def test_an_open_decision_fills_at_that_open(self, gold, config):
        bars = series([1800.0, 1900.0, 2000.0])
        result = run(BuyOnOpen(), bars, gold, config)
        assert result.trades[0].entry_price == pytest.approx(1800.0)

    def test_a_close_decision_fills_at_the_next_open(self, gold, config):
        """Deciding from a closed bar cannot buy at that bar's own price."""
        bars = series([1800.0, 1900.0, 2000.0])
        result = run(BuyOnClose(), bars, gold, config)
        assert result.trades[0].entry_price == pytest.approx(1900.0)

    def test_an_order_on_the_last_close_never_fills(self, gold, config):
        class BuyAtTheEnd(Strategy):
            name = "test_buy_at_end"

            def on_bar(self, ctx, bar):
                if len(ctx.history) == 3:
                    ctx.buy(0.1)

        result = run(BuyAtTheEnd(), series([1800.0, 1850.0, 1900.0]), gold, config)
        assert result.trades == []


class TestHistory:
    def test_history_only_reaches_the_current_bar(self, gold, config):
        seen = []

        class Recorder(Strategy):
            name = "test_recorder"

            def on_bar_open(self, ctx, event):
                # At the open, the bar in progress is not yet in history.
                seen.append(("open", len(ctx.history)))

            def on_bar(self, ctx, bar):
                seen.append(("close", len(ctx.history), ctx.history[-1].close))

        bars = series([1800.0, 1810.0, 1820.0])
        run(Recorder(), bars, gold, config)

        assert [s for s in seen if s[0] == "open"] == [("open", 0), ("open", 1), ("open", 2)]
        closes = [s[2] for s in seen if s[0] == "close"]
        assert closes == [1800.0, 1810.0, 1820.0]

    def test_history_helpers_return_the_last_n_closes(self, gold, config):
        captured = []

        class Peek(Strategy):
            name = "test_peek"

            def on_bar(self, ctx, bar):
                captured.append(ctx.history.closes(2))

        run(Peek(), series([1800.0, 1810.0, 1820.0]), gold, config)
        assert captured[-1] == [1810.0, 1820.0]


class TestLifecycle:
    def test_hooks_fire_in_order(self, gold, config):
        events = []

        class Tracker(Strategy):
            name = "test_tracker"

            def on_start(self, ctx):
                events.append("start")

            def on_bar_open(self, ctx, event):
                events.append("open")
                if len(ctx.history) == 1:
                    ctx.buy(0.1, tp_pct=1.0)

            def on_bar(self, ctx, bar):
                events.append("bar")

            def on_trade(self, ctx, trade):
                events.append("trade")

            def on_finish(self, ctx):
                events.append("finish")

        run(Tracker(), series([1800.0, 1800.0, 2000.0]), gold, config)
        assert events[0] == "start"
        assert events[-1] == "finish"
        assert "trade" in events
        # A trade closed inside a bar is reported before that bar's on_bar.
        assert events.index("trade") < len(events) - 1

    def test_warmup_suppresses_early_trading(self, gold, config):
        result = run(BuyOnOpen(), series([1800.0] * 10), gold, config, warmup_bars=5)
        assert result.trades[0].entry_time == series([1800.0] * 10)[5].time

    def test_open_position_is_closed_at_the_end(self, gold, config):
        result = run(BuyOnOpen(), series([1800.0, 1850.0, 1900.0]), gold, config)
        assert len(result.trades) == 1
        assert result.trades[0].reason is ExitReason.END_OF_DATA
        assert result.trades[0].exit_price == pytest.approx(1900.0)

    def test_close_at_end_can_be_disabled(self, gold, config):
        result = run(BuyOnOpen(), series([1800.0, 1850.0]), gold, config, close_at_end=False)
        assert result.trades == []

    def test_empty_input_is_rejected(self, gold, config):
        with pytest.raises(ValueError, match="No bars"):
            run(BuyOnOpen(), [], gold, config)


class TestResult:
    def test_equity_curve_covers_every_bar(self, gold, config):
        bars = series([1800.0] * 20)
        result = run(BuyOnOpen(), bars, gold, config)
        assert result.bars_processed == 20
        assert len(result.equity) >= 20
        assert result.equity[0].time == bars[0].time

    def test_pnl_reaches_the_final_balance(self, gold, config):
        result = run(BuyOnOpen(), series([1800.0, 1810.0]), gold, config)
        realised = sum(t.net_pnl for t in result.trades)
        assert result.final_balance == pytest.approx(result.initial_balance + realised)
