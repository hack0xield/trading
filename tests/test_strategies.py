"""The mock strategy, checked against the brief it was written from.

    1) On Day start = BUY X
    2) Set stop 2%
    3) Set Take 2%
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from backtester.core.engine import Backtester, EngineConfig
from backtester.core.types import Bar, ExitReason, Side
from backtester.strategies import available, get_strategy
from backtester.strategies.day_open import DayOpenStrategy

UTC = timezone.utc


def hourly(start: datetime, prices: list[float], drift: float = 0.0) -> list[Bar]:
    """One H1 bar per price, each with a small symmetric range."""
    return [
        Bar(
            time=start + timedelta(hours=i),
            open=price,
            high=price * (1 + abs(drift)) + 0.5,
            low=price * (1 - abs(drift)) - 0.5,
            close=price,
            volume=100,
            spread=0.0,
        )
        for i, price in enumerate(prices)
    ]


def explicit(index: int, open_: float, high: float, low: float, close: float) -> Bar:
    """An H1 bar with every price stated, for exact stop/target arithmetic."""
    return Bar(
        time=datetime(2024, 1, 1, tzinfo=UTC) + timedelta(hours=index),
        open=open_, high=high, low=low, close=close, volume=100, spread=0.0,
    )


def run(strategy, bars, gold, config, **kwargs):
    return Backtester(
        strategy=strategy,
        symbol="XAUUSD",
        timeframe="H1",
        instrument=gold,
        execution=config,
        engine=EngineConfig(**kwargs),
    ).run(bars)


class TestEntryTiming:
    def test_it_buys_at_the_first_bar_of_each_day(self, gold, config):
        bars = hourly(datetime(2024, 1, 1, tzinfo=UTC), [1800.0] * 72)
        result = run(
            DayOpenStrategy(volume=0.1, stop_pct=2, take_pct=2, close_at_day_end=True),
            bars, gold, config,
        )
        entries = [t.entry_time for t in result.trades]
        assert [e.hour for e in entries] == [0, 0, 0]
        assert [e.day for e in entries] == [1, 2, 3]

    def test_the_entry_price_is_the_day_open_not_a_later_bar(self, gold, config):
        prices = [1800.0] + [1850.0] * 23
        bars = hourly(datetime(2024, 1, 1, tzinfo=UTC), prices)
        result = run(DayOpenStrategy(volume=0.1), bars, gold, config)
        assert result.trades[0].entry_price == pytest.approx(1800.0)

    def test_session_start_hour_moves_the_boundary(self, gold, config):
        bars = hourly(datetime(2024, 1, 1, tzinfo=UTC), [1800.0] * 72)
        result = run(
            DayOpenStrategy(volume=0.1, session_start_hour=8, close_at_day_end=True),
            bars, gold, config,
        )
        # The very first bar always opens a session; every later one is 08:00.
        hours = [t.entry_time.hour for t in result.trades]
        assert hours[0] == 0
        assert set(hours[1:]) == {8}

    def test_a_timezone_offset_moves_the_boundary(self, gold, config):
        bars = hourly(datetime(2024, 1, 1, tzinfo=UTC), [1800.0] * 72)
        result = run(
            DayOpenStrategy(volume=0.1, session_tz="UTC+3", close_at_day_end=True),
            bars, gold, config,
        )
        # Midnight in UTC+3 is 21:00 the previous day in UTC.
        hours = [t.entry_time.hour for t in result.trades]
        assert hours[0] == 0
        assert set(hours[1:]) == {21}

    def test_weekday_filter_skips_the_weekend(self, gold, config):
        # 2024-01-05 is a Friday, so this window covers Sat and Sun.
        bars = hourly(datetime(2024, 1, 5, tzinfo=UTC), [1800.0] * 96)
        result = run(
            DayOpenStrategy(volume=0.1, weekdays="0,1,2,3,4", close_at_day_end=True),
            bars, gold, config,
        )
        assert all(t.entry_time.weekday() < 5 for t in result.trades)


class TestBrackets:
    def test_stop_and_target_sit_two_percent_from_the_fill(self, gold, config):
        captured = {}
        strategy = DayOpenStrategy(volume=0.1, stop_pct=2, take_pct=2)
        original = strategy.on_bar_open

        def spy(ctx, event):
            original(ctx, event)
            if ctx.positions and "sl" not in captured:
                captured["sl"] = ctx.positions[0].sl
                captured["tp"] = ctx.positions[0].tp

        strategy.on_bar_open = spy
        run(strategy, hourly(datetime(2024, 1, 1, tzinfo=UTC), [1800.0] * 48), gold, config)

        assert captured["sl"] == pytest.approx(1764.0)
        assert captured["tp"] == pytest.approx(1836.0)

    def test_a_two_percent_rally_takes_profit(self, gold, config):
        # Entry on bar 0 at 1800; bar 1 trades up through 1836 without gapping.
        bars = [
            explicit(0, 1800.0, 1801.0, 1799.0, 1800.0),
            explicit(1, 1800.0, 1840.0, 1800.0, 1838.0),
        ]
        result = run(DayOpenStrategy(volume=0.1, stop_pct=2, take_pct=2), bars, gold, config)
        assert result.trades[0].reason is ExitReason.TAKE_PROFIT
        assert result.trades[0].exit_price == pytest.approx(1836.0)
        assert result.trades[0].net_pnl == pytest.approx(360.0)  # $36 on 10 oz

    def test_a_two_percent_selloff_stops_out(self, gold, config):
        bars = [
            explicit(0, 1800.0, 1801.0, 1799.0, 1800.0),
            explicit(1, 1800.0, 1800.0, 1760.0, 1765.0),
        ]
        result = run(DayOpenStrategy(volume=0.1, stop_pct=2, take_pct=2), bars, gold, config)
        assert result.trades[0].reason is ExitReason.STOP_LOSS
        assert result.trades[0].exit_price == pytest.approx(1764.0)
        assert result.trades[0].net_pnl == pytest.approx(-360.0)

    def test_a_gap_below_the_stop_costs_more_than_the_stop(self, gold, config):
        """The overnight risk a percentage stop does not actually protect against."""
        bars = [
            explicit(0, 1800.0, 1801.0, 1799.0, 1800.0),
            explicit(1, 1746.0, 1750.0, 1740.0, 1746.0),  # gapped straight through 1764
        ]
        result = run(DayOpenStrategy(volume=0.1, stop_pct=2, take_pct=2), bars, gold, config)
        assert result.trades[0].reason is ExitReason.STOP_LOSS
        assert result.trades[0].exit_price == pytest.approx(1746.0)
        assert result.trades[0].net_pnl == pytest.approx(-540.0)

    def test_a_gap_above_the_target_books_only_the_target(self, gold, config):
        """No windfall from positive slippage the broker never promised."""
        bars = [
            explicit(0, 1800.0, 1801.0, 1799.0, 1800.0),
            explicit(1, 1854.0, 1860.0, 1850.0, 1854.0),  # gapped past 1836
        ]
        result = run(DayOpenStrategy(volume=0.1, stop_pct=2, take_pct=2), bars, gold, config)
        assert result.trades[0].reason is ExitReason.TAKE_PROFIT
        assert result.trades[0].exit_price == pytest.approx(1836.0)
        assert result.trades[0].net_pnl == pytest.approx(360.0)

    def test_direction_sell_inverts_everything(self, gold, config):
        prices = [1800.0] + [1800.0 * 0.97] * 10
        result = run(
            DayOpenStrategy(volume=0.1, direction="SELL", stop_pct=2, take_pct=2),
            hourly(datetime(2024, 1, 1, tzinfo=UTC), prices), gold, config,
        )
        assert result.trades[0].side is Side.SELL
        assert result.trades[0].reason is ExitReason.TAKE_PROFIT


class TestPositionManagement:
    def test_one_position_blocks_a_second_entry(self, gold, config):
        """Default behaviour: a day-2 signal is skipped while day 1 is still open."""
        bars = hourly(datetime(2024, 1, 1, tzinfo=UTC), [1800.0] * 72)
        result = run(
            DayOpenStrategy(volume=0.1, one_position=True, close_at_day_end=False),
            bars, gold, config,
        )
        assert len(result.trades) == 1

    def test_close_at_day_end_flattens_the_boundary(self, gold, config):
        bars = hourly(datetime(2024, 1, 1, tzinfo=UTC), [1800.0] * 72)
        result = run(
            DayOpenStrategy(volume=0.1, close_at_day_end=True), bars, gold, config
        )
        assert len(result.trades) == 3
        assert result.trades[0].reason is ExitReason.SESSION_END

    def test_stacking_is_possible_when_both_flags_are_off(self, gold, config):
        bars = hourly(datetime(2024, 1, 1, tzinfo=UTC), [1800.0] * 72)
        result = run(
            DayOpenStrategy(volume=0.1, one_position=False, close_at_day_end=False),
            bars, gold, config,
        )
        assert len(result.trades) == 3
        assert all(t.reason is ExitReason.END_OF_DATA for t in result.trades)

    def test_max_hold_bars_is_a_time_stop(self, gold, config):
        bars = hourly(datetime(2024, 1, 1, tzinfo=UTC), [1800.0] * 72)
        result = run(
            DayOpenStrategy(volume=0.1, max_hold_bars=5, close_at_day_end=False),
            bars, gold, config,
        )
        assert result.trades[0].reason is ExitReason.SESSION_END
        assert result.trades[0].bars_held == 5


class TestValidationAndRegistry:
    def test_a_non_positive_stop_is_rejected(self, gold, config):
        with pytest.raises(ValueError, match="must both be > 0"):
            run(
                DayOpenStrategy(volume=0.1, stop_pct=0),
                hourly(datetime(2024, 1, 1, tzinfo=UTC), [1800.0] * 5), gold, config,
            )

    def test_strategies_are_registered_by_name(self):
        assert "day_open" in available()
        assert "sma_cross" in available()
        assert get_strategy("day_open") is DayOpenStrategy

    def test_an_unknown_name_lists_what_exists(self):
        with pytest.raises(KeyError, match="Available"):
            get_strategy("no_such_strategy")


class TestSmaCross:
    def test_it_trades_a_crossover(self, gold, config):
        # Down then up: the fast average crosses the slow one on the way back.
        prices = [1800.0 - i for i in range(40)] + [1760.0 + i * 3 for i in range(40)]
        from backtester.strategies.sma_cross import SmaCrossStrategy

        result = run(
            SmaCrossStrategy(volume=0.1, fast=5, slow=20, stop_pct=5, take_pct=5),
            hourly(datetime(2024, 1, 1, tzinfo=UTC), prices), gold, config, warmup_bars=20,
        )
        assert result.trades

    def test_fast_must_be_shorter_than_slow(self, gold, config):
        from backtester.strategies.sma_cross import SmaCrossStrategy

        with pytest.raises(ValueError, match="must be shorter"):
            run(
                SmaCrossStrategy(fast=50, slow=20),
                hourly(datetime(2024, 1, 1, tzinfo=UTC), [1800.0] * 5), gold, config,
            )
