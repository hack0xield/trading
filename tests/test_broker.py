"""Broker simulation: fills, costs, and how stops resolve inside a bar."""

from __future__ import annotations

import pytest

from backtester.core.broker import ExecutionConfig, SimulatedBroker
from backtester.core.types import ExitReason, OrderRequest, Side
from conftest import START, bar


def buy(volume=0.1, **kwargs) -> OrderRequest:
    return OrderRequest(side=Side.BUY, volume=volume, **kwargs)


def sell(volume=0.1, **kwargs) -> OrderRequest:
    return OrderRequest(side=Side.SELL, volume=volume, **kwargs)


class TestFills:
    def test_market_buy_fills_at_the_offered_price(self, broker):
        broker.submit(buy())
        opened = broker.fill_pending(1800.0, START)
        assert len(opened) == 1
        assert opened[0].entry_price == 1800.0
        assert broker.pending == []

    def test_long_pays_the_spread_on_entry(self, gold):
        broker = SimulatedBroker(gold, ExecutionConfig(spread_points=25.0, apply_swap=False))
        broker.submit(buy())
        position = broker.fill_pending(1800.0, START)[0]
        assert position.entry_price == pytest.approx(1800.25)

    def test_short_enters_at_bid(self, gold):
        broker = SimulatedBroker(gold, ExecutionConfig(spread_points=25.0, apply_swap=False))
        broker.submit(sell())
        position = broker.fill_pending(1800.0, START)[0]
        assert position.entry_price == pytest.approx(1800.0)

    def test_slippage_works_against_both_sides(self, gold):
        broker = SimulatedBroker(
            gold, ExecutionConfig(spread_points=0.0, slippage_points=10.0, apply_swap=False)
        )
        broker.submit(buy())
        broker.submit(sell())
        long, short = broker.fill_pending(1800.0, START)
        assert long.entry_price == pytest.approx(1800.10)
        assert short.entry_price == pytest.approx(1799.90)

    def test_volume_snaps_to_the_broker_step(self, broker):
        broker.submit(buy(volume=0.117))
        assert broker.fill_pending(1800.0, START)[0].volume == pytest.approx(0.12)

    def test_order_beyond_free_margin_is_rejected(self, gold):
        broker = SimulatedBroker(
            gold, ExecutionConfig(initial_balance=1_000.0, leverage=100.0, apply_swap=False)
        )
        # 10 lots of gold at 1800 needs 10*100*1800/100 = 180,000 of margin.
        broker.submit(buy(volume=10.0))
        assert broker.fill_pending(1800.0, START) == []
        assert broker.rejected and "margin" in broker.rejected[0][1]


class TestPercentageLevels:
    def test_long_levels_are_resolved_from_the_fill(self, broker):
        broker.submit(buy(sl_pct=2.0, tp_pct=2.0))
        position = broker.fill_pending(1800.0, START)[0]
        assert position.sl == pytest.approx(1764.0)
        assert position.tp == pytest.approx(1836.0)

    def test_short_levels_invert(self, broker):
        broker.submit(sell(sl_pct=2.0, tp_pct=2.0))
        position = broker.fill_pending(1800.0, START)[0]
        assert position.sl == pytest.approx(1836.0)
        assert position.tp == pytest.approx(1764.0)

    def test_levels_follow_the_spread_not_the_signal_price(self, gold):
        """A 2% stop is 2% from where you actually got filled, not from the mid."""
        broker = SimulatedBroker(gold, ExecutionConfig(spread_points=100.0, apply_swap=False))
        broker.submit(buy(sl_pct=2.0))
        position = broker.fill_pending(1800.0, START)[0]
        assert position.entry_price == pytest.approx(1801.0)
        assert position.sl == pytest.approx(1801.0 * 0.98)

    def test_explicit_prices_win_over_percentages(self, broker):
        broker.submit(buy(sl_price=1750.0, sl_pct=2.0))
        assert broker.fill_pending(1800.0, START)[0].sl == pytest.approx(1750.0)


class TestExits:
    def test_stop_fires_when_the_low_reaches_it(self, broker):
        broker.submit(buy(sl_pct=2.0, tp_pct=2.0))
        broker.fill_pending(1800.0, START)
        closed = broker.process_bar(bar(1, open_=1800, high=1801, low=1760, close=1770))
        assert len(closed) == 1
        assert closed[0].reason is ExitReason.STOP_LOSS
        assert closed[0].exit_price == pytest.approx(1764.0)
        # 0.1 lot of gold = 10 oz, so a $36 move is $360.
        assert closed[0].net_pnl == pytest.approx(-360.0)

    def test_target_fires_when_the_high_reaches_it(self, broker):
        broker.submit(buy(sl_pct=2.0, tp_pct=2.0))
        broker.fill_pending(1800.0, START)
        closed = broker.process_bar(bar(1, open_=1800, high=1840, low=1799, close=1838))
        assert closed[0].reason is ExitReason.TAKE_PROFIT
        assert closed[0].net_pnl == pytest.approx(360.0)

    def test_a_gap_through_the_stop_fills_at_the_open(self, broker):
        """The whole point of modelling gaps: you do not get your stop price."""
        broker.submit(buy(sl_pct=2.0))
        broker.fill_pending(1800.0, START)
        closed = broker.process_bar(bar(1, open_=1700, high=1710, low=1690, close=1700))
        assert closed[0].exit_price == pytest.approx(1700.0)
        assert closed[0].net_pnl == pytest.approx(-1000.0)

    def test_untouched_position_stays_open(self, broker):
        broker.submit(buy(sl_pct=2.0, tp_pct=2.0))
        broker.fill_pending(1800.0, START)
        assert broker.process_bar(bar(1, open_=1800, high=1810, low=1790, close=1805)) == []
        assert len(broker.positions) == 1

    def test_short_exits_are_measured_at_the_ask(self, gold):
        """A short is closed by buying, so the spread has to be added to the bar."""
        broker = SimulatedBroker(
            gold, ExecutionConfig(spread_points=100.0, apply_swap=False, use_bar_spread=False)
        )
        broker.submit(sell(sl_pct=2.0))
        position = broker.fill_pending(1800.0, START)[0]
        assert position.sl == pytest.approx(1836.0)
        # High of 1835.5 is below the stop on the bid, but 1836.5 on the ask.
        closed = broker.process_bar(bar(1, open_=1800, high=1835.5, low=1799, close=1830))
        assert closed and closed[0].reason is ExitReason.STOP_LOSS


class TestIntrabarPolicy:
    """A bar containing both levels cannot say which was hit first."""

    def both_hit(self, gold, policy: str):
        broker = SimulatedBroker(
            gold, ExecutionConfig(spread_points=0.0, apply_swap=False, intrabar=policy)
        )
        broker.submit(buy(sl_pct=2.0, tp_pct=2.0))
        broker.fill_pending(1800.0, START)
        # An up bar whose range covers 1764 and 1836.
        return broker.process_bar(bar(1, open_=1800, high=1850, low=1750, close=1820))[0]

    def test_conservative_assumes_the_stop(self, gold):
        assert self.both_hit(gold, "conservative").reason is ExitReason.STOP_LOSS

    def test_optimistic_assumes_the_target(self, gold):
        assert self.both_hit(gold, "optimistic").reason is ExitReason.TAKE_PROFIT

    def test_ohlc_infers_from_the_bar_direction(self, gold):
        # Up bar -> assumed path O-H-L-C, so a long sees the target first.
        assert self.both_hit(gold, "ohlc").reason is ExitReason.TAKE_PROFIT

    def test_ohlc_on_a_down_bar_hits_the_stop(self, gold):
        broker = SimulatedBroker(
            gold, ExecutionConfig(spread_points=0.0, apply_swap=False, intrabar="ohlc")
        )
        broker.submit(buy(sl_pct=2.0, tp_pct=2.0))
        broker.fill_pending(1800.0, START)
        closed = broker.process_bar(bar(1, open_=1800, high=1850, low=1750, close=1780))
        assert closed[0].reason is ExitReason.STOP_LOSS

    def test_the_policy_actually_changes_the_answer(self, gold):
        """Guards the knob itself: if these ever agree, the test is worthless."""
        assert (
            self.both_hit(gold, "conservative").net_pnl < self.both_hit(gold, "optimistic").net_pnl
        )


class TestCosts:
    def test_commission_is_charged_round_turn(self, gold):
        broker = SimulatedBroker(
            gold, ExecutionConfig(spread_points=0.0, commission_per_lot=5.0, apply_swap=False)
        )
        broker.submit(buy(volume=1.0))
        broker.fill_pending(1800.0, START)
        trade = broker.close_all(1800.0, START)[0]
        assert trade.commission == pytest.approx(10.0)  # 5 per side
        assert trade.net_pnl == pytest.approx(-10.0)

    def test_swap_accrues_per_calendar_day(self, gold):
        gold.swap_long = -10.0  # points per lot per night
        broker = SimulatedBroker(gold, ExecutionConfig(spread_points=0.0, apply_swap=True))
        broker.submit(buy(volume=1.0))
        broker.fill_pending(1800.0, START)
        broker.process_bar(_daily(0))
        for day in range(1, 4):
            broker.process_bar(_daily(day))
        assert broker.positions[0].swap == pytest.approx(-30.0)  # 3 nights at -$10

    def test_no_swap_when_disabled(self, broker):
        broker.submit(buy(volume=1.0))
        broker.fill_pending(1800.0, START)
        for day in range(1, 4):
            broker.process_bar(_daily(day))
        assert broker.positions[0].swap == 0.0


class TestAccounting:
    def test_balance_moves_only_on_close(self, broker):
        broker.submit(buy())
        broker.fill_pending(1800.0, START)
        broker.process_bar(bar(1, open_=1800, high=1850, low=1800, close=1850))
        assert broker.balance == pytest.approx(10_000.0)
        assert broker.equity == pytest.approx(10_500.0)
        broker.close_all(1850.0, START)
        assert broker.balance == pytest.approx(10_500.0)

    def test_excursions_track_the_worst_and_best_of_the_bar(self, broker):
        broker.submit(buy())
        broker.fill_pending(1800.0, START)
        broker.process_bar(bar(1, open_=1800, high=1830, low=1770, close=1800))
        position = broker.positions[0]
        assert position.mfe == pytest.approx(30.0)
        assert position.mae == pytest.approx(-30.0)

    def test_finalize_closes_what_is_left(self, broker):
        broker.submit(buy())
        broker.fill_pending(1800.0, START)
        trades = broker.finalize(bar(5, open_=1810, close=1810))
        assert len(trades) == 1
        assert trades[0].reason is ExitReason.END_OF_DATA
        assert broker.positions == []


def _daily(day: int):
    """A bar on a distinct calendar date, for swap accrual."""
    from datetime import timedelta

    from backtester.core.types import Bar

    return Bar(
        time=START + timedelta(days=day),
        open=1800.0,
        high=1800.0,
        low=1800.0,
        close=1800.0,
        volume=1,
        spread=0.0,
    )
