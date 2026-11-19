"""Engine loop: ordering of hooks, and the guarantee that fills cannot cheat."""

from __future__ import annotations

import pytest

from backtester.core.context import BarOpen, Context
from backtester.core.engine import Backtester, EngineConfig
from backtester.core.strategy import Strategy
from backtester.core.types import Bar, ExitReason
from backtester.utils.params import StrategyParams
from conftest import bar, series


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


class TestLimitOrders:
    """A resting order fills at its own level, never worse."""

    def strategy(self, side: str, limit: float, **kw):
        class PlaceLimit(Strategy):
            name = "test_limit"
            params_class = StrategyParams

            def on_start(self, ctx):
                self.placed = False

            def on_bar(self, ctx: Context, bar: Bar):
                if not self.placed:
                    ctx.order(side, 0.1, limit=limit, **kw)
                    self.placed = True

        return PlaceLimit()

    def test_it_rests_until_the_market_reaches_it(self, gold, config):
        bars = [bar(0, 1800.0), bar(1, 1800.0), bar(2, 1800.0)]
        result = run(self.strategy("BUY", 1700.0), bars, gold, config)
        assert result.trades == []

    def test_a_buy_fills_at_the_limit_when_the_bar_trades_through(self, gold, config):
        bars = [bar(0, 1800.0), bar(1, 1800.0), bar(2, 1800.0, low=1650.0, close=1800.0)]
        result = run(self.strategy("BUY", 1700.0), bars, gold, config)
        (trade,) = result.trades
        assert trade.entry_price == pytest.approx(1700.0)
        assert trade.entry_time == bars[2].time

    def test_a_sell_fills_at_the_limit_when_the_bar_trades_through(self, gold, config):
        bars = [bar(0, 1800.0), bar(1, 1800.0), bar(2, 1800.0, high=1950.0, close=1800.0)]
        result = run(self.strategy("SELL", 1900.0), bars, gold, config)
        (trade,) = result.trades
        assert trade.entry_price == pytest.approx(1900.0)

    def test_a_bar_that_stops_short_does_not_fill_it(self, gold, config):
        bars = [bar(0, 1800.0), bar(1, 1800.0), bar(2, 1800.0, low=1701.0, close=1800.0)]
        result = run(self.strategy("BUY", 1700.0), bars, gold, config)
        assert result.trades == []

    def test_an_open_already_through_the_limit_fills_there_instead(self, gold, config):
        """Better than the level asked for, which is what a real fill does."""
        bars = [bar(0, 1800.0), bar(1, 1800.0), bar(2, 1650.0)]
        result = run(self.strategy("BUY", 1700.0), bars, gold, config)
        (trade,) = result.trades
        assert trade.entry_price == pytest.approx(1650.0)

    def test_the_bracket_starts_on_the_next_bar(self, gold, config):
        """The path after an intrabar fill is unknown, so the same bar's range
        cannot resolve the position it just opened."""
        bars = [
            bar(0, 1800.0), bar(1, 1800.0),
            bar(2, 1800.0, low=1650.0, high=1800.0, close=1800.0),  # fills at 1700
            bar(3, 1800.0, low=1600.0, high=1800.0, close=1800.0),  # takes the stop
        ]
        result = run(self.strategy("BUY", 1700.0, sl=1650.0), bars, gold, config)
        (trade,) = result.trades
        assert trade.reason is ExitReason.STOP_LOSS
        assert trade.exit_time == bars[3].time

    def test_cancelling_removes_it(self, gold, config):
        class PlaceThenCancel(Strategy):
            name = "test_limit_cancel"
            params_class = StrategyParams

            def on_bar(self, ctx: Context, bar: Bar):
                if len(ctx.history) == 1:
                    ctx.order("BUY", 0.1, limit=1700.0)
                if len(ctx.history) == 2:
                    assert ctx.cancel_pending() == 1

        bars = [bar(0, 1800.0), bar(1, 1800.0), bar(2, 1800.0, low=1650.0, close=1800.0)]
        assert run(PlaceThenCancel(), bars, gold, config).trades == []

    def test_a_market_order_beside_it_still_fills_at_the_open(self, gold, config):
        class Both(Strategy):
            name = "test_limit_and_market"
            params_class = StrategyParams

            def on_bar(self, ctx: Context, bar: Bar):
                if len(ctx.history) == 1:
                    ctx.order("BUY", 0.1, limit=1700.0)
                    ctx.order("SELL", 0.1)

        bars = [bar(0, 1800.0), bar(1, 1850.0), bar(2, 1800.0)]
        result = run(Both(), bars, gold, config)
        assert [t.side.value for t in result.trades] == ["SELL"]
        assert result.trades[0].entry_price == pytest.approx(1850.0)

    def test_an_invalidation_level_voids_it(self, gold, config):
        bars = [bar(0, 1800.0), bar(1, 1800.0), bar(2, 1800.0, low=1550.0, close=1800.0)]
        result = run(self.strategy("BUY", 1700.0, cancel_at=1900.0), bars, gold, config)
        assert result.trades and result.trades[0].entry_price == pytest.approx(1700.0)

        # The same bar reaching the invalidation instead
        bars = [bar(0, 1800.0), bar(1, 1800.0), bar(2, 1800.0, high=1950.0, close=1800.0)]
        assert run(self.strategy("BUY", 1700.0, cancel_at=1900.0), bars, gold, config).trades == []

    def test_a_bar_reaching_both_resolves_as_the_cancellation(self, gold, config):
        """The intrabar path is unknown, so the conservative reading wins."""
        bars = [
            bar(0, 1800.0), bar(1, 1800.0),
            bar(2, 1800.0, low=1650.0, high=1950.0, close=1800.0),
        ]
        result = run(self.strategy("BUY", 1700.0, cancel_at=1900.0), bars, gold, config)
        assert result.trades == []
