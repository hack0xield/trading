"""Performance statistics and typed parameter handling."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta

import pytest

from backtester.core.types import BacktestResult, EquityPoint, ExitReason, Side, Trade
from backtester.metrics.stats import compute, monthly_returns
from backtester.utils.params import StrategyParams
from conftest import START


def trade(pnl: float, index: int = 0, side: Side = Side.BUY) -> Trade:
    entry = START + timedelta(days=index)
    return Trade(
        id=index, side=side, volume=0.1,
        entry_time=entry, entry_price=1800.0,
        exit_time=entry + timedelta(hours=4), exit_price=1800.0 + pnl,
        reason=ExitReason.TAKE_PROFIT if pnl > 0 else ExitReason.STOP_LOSS,
        gross_pnl=pnl, commission=0.0, swap=0.0,
        bars_held=4, mae=0.0, mfe=0.0, balance_after=0.0,
    )


def result(trades=None, equity_values=None) -> BacktestResult:
    equity_values = equity_values or [10_000.0]
    equity = [
        EquityPoint(time=START + timedelta(days=i), balance=v, equity=v)
        for i, v in enumerate(equity_values)
    ]
    return BacktestResult(
        strategy="t", symbol="XAUUSD", timeframe="M15",
        trades=trades or [], equity=equity,
        initial_balance=10_000.0, final_balance=equity_values[-1],
        bars_processed=len(equity_values),
        start=equity[0].time, end=equity[-1].time,
    )


class TestDrawdown:
    def test_drawdown_is_measured_from_the_peak(self):
        m = compute(result(equity_values=[10_000, 12_000, 9_000, 11_000]))
        assert m.max_drawdown == pytest.approx(3_000.0)
        assert m.max_drawdown_pct == pytest.approx(25.0)  # 3000 off a 12000 peak

    def test_a_curve_that_only_rises_has_no_drawdown(self):
        m = compute(result(equity_values=[10_000, 11_000, 12_000]))
        assert m.max_drawdown == 0.0
        assert m.max_drawdown_pct == 0.0

    def test_underwater_time_is_reported_in_days(self):
        m = compute(result(equity_values=[10_000, 12_000, 11_000, 11_500, 11_800]))
        assert m.max_drawdown_days == pytest.approx(3.0)

    def test_drawdown_uses_equity_not_balance(self):
        """An open loser counts against you before it is realised."""
        equity = [
            EquityPoint(time=START + timedelta(days=i), balance=10_000.0, equity=e)
            for i, e in enumerate([10_000.0, 8_000.0, 10_000.0])
        ]
        res = result()
        res.equity = equity
        m = compute(res)
        assert m.max_drawdown == pytest.approx(2_000.0)


class TestTradeStats:
    def test_win_rate_and_counts(self):
        m = compute(result(trades=[trade(100, 0), trade(-50, 1), trade(200, 2), trade(-25, 3)]))
        assert m.trades == 4
        assert m.wins == 2
        assert m.losses == 2
        assert m.win_rate_pct == pytest.approx(50.0)

    def test_profit_factor_is_gross_win_over_gross_loss(self):
        m = compute(result(trades=[trade(300, 0), trade(-100, 1)]))
        assert m.profit_factor == pytest.approx(3.0)

    def test_profit_factor_without_losers_is_infinite(self):
        m = compute(result(trades=[trade(100, 0), trade(50, 1)]))
        assert m.profit_factor == float("inf")

    def test_expectancy_is_mean_net_pnl(self):
        m = compute(result(trades=[trade(100, 0), trade(-50, 1)]))
        assert m.expectancy == pytest.approx(25.0)

    def test_streaks(self):
        pnls = [10, 20, 30, -10, -20, 40, -5, -5, -5, -5]
        m = compute(result(trades=[trade(p, i) for i, p in enumerate(pnls)]))
        assert m.max_consecutive_wins == 3
        assert m.max_consecutive_losses == 4

    def test_a_breakeven_trade_counts_as_a_loss(self):
        """Zero P&L after costs is not a win; classing it as one flatters the rate."""
        m = compute(result(trades=[trade(0, 0), trade(100, 1)]))
        assert m.wins == 1
        assert m.losses == 1

    def test_exit_reasons_are_tallied(self):
        m = compute(result(trades=[trade(100, 0), trade(-50, 1), trade(-50, 2)]))
        assert m.exits == {"TAKE_PROFIT": 1, "STOP_LOSS": 2}

    def test_no_trades_is_not_an_error(self):
        m = compute(result())
        assert m.trades == 0
        assert m.win_rate_pct == 0.0
        assert m.profit_factor == 0.0


class TestReturns:
    def test_return_percentage(self):
        m = compute(result(equity_values=[10_000, 11_500]))
        assert m.return_pct == pytest.approx(15.0)

    def test_a_flat_curve_has_no_sharpe(self):
        m = compute(result(equity_values=[10_000] * 30))
        assert m.sharpe == 0.0

    def test_a_rising_curve_has_positive_sharpe(self):
        m = compute(result(equity_values=[10_000 * (1.001**i) for i in range(200)]))
        assert m.sharpe > 0

    def test_monthly_returns_are_grouped_by_month(self):
        values = [10_000 + i * 10 for i in range(70)]
        res = result(equity_values=values)
        months = monthly_returns(res)
        assert len(months) >= 2
        assert all(isinstance(v, float) for v in months.values())


class TestParams:
    def test_strings_are_coerced_to_the_annotated_type(self):
        @dataclass
        class P(StrategyParams):
            volume: float = 0.1
            fast: int = 10
            flag: bool = False
            label: str = ""

        p = P.from_dict({"volume": "0.25", "fast": "30", "flag": "true", "label": "x"})
        assert p.volume == 0.25 and isinstance(p.volume, float)
        assert p.fast == 30 and isinstance(p.fast, int)
        assert p.flag is True
        assert p.label == "x"

    def test_an_int_written_as_a_float_string_still_works(self):
        """Sweeps and YAML both produce '20.0' where an int is wanted."""

        @dataclass
        class P(StrategyParams):
            fast: int = 10

        assert P.from_dict({"fast": "20.0"}).fast == 20

    def test_optional_ints_accept_none(self):
        @dataclass
        class P(StrategyParams):
            max_hold_bars: int | None = None

        assert P.from_dict({"max_hold_bars": None}).max_hold_bars is None
        assert P.from_dict({"max_hold_bars": "5"}).max_hold_bars == 5

    def test_a_misspelled_parameter_fails_loudly(self):
        """The whole point: a typo must not be silently ignored for a long run."""

        @dataclass
        class P(StrategyParams):
            stop_pct: float = 2.0

        with pytest.raises(ValueError, match="Unknown parameter"):
            P.from_dict({"stop_pcnt": 3.0})

    def test_an_unparseable_value_names_the_parameter(self):
        @dataclass
        class P(StrategyParams):
            stop_pct: float = 2.0

        with pytest.raises(ValueError, match="stop_pct"):
            P.from_dict({"stop_pct": "two percent"})

    def test_boolean_spellings(self):
        @dataclass
        class P(StrategyParams):
            flag: bool = False

        for text in ("true", "TRUE", "yes", "1", "on"):
            assert P.from_dict({"flag": text}).flag is True
        for text in ("false", "no", "0", "off"):
            assert P.from_dict({"flag": text}).flag is False
