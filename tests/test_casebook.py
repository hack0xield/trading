"""The Backtest Database and its views.

Checked against `impl-spec-old/Claude Specification_ Backtest Data Collection and
Reporting.md` and against the reference export in `backtest_tables.7z`, whose
headline figures the aggregation has to reproduce exactly.
"""

from __future__ import annotations

import csv
from datetime import datetime, timedelta, timezone

import pytest

from backtester.core.types import BacktestResult, ExitReason, Side, Trade
from backtester.metrics import casebook

UTC = timezone.utc
START = datetime(2024, 1, 1, 12, tzinfo=UTC)   # a Monday


def trade(n: int, entry: float, exit_: float, sl: float, tp: float,
          side: Side = Side.BUY, hour: int | None = None, day: int = 1) -> Trade:
    when = START.replace(day=day, hour=START.hour if hour is None else hour)
    pnl = (exit_ - entry) * side.sign
    return Trade(
        id=n, side=side, volume=0.1, entry_time=when, entry_price=entry,
        exit_time=when + timedelta(hours=4), exit_price=exit_,
        reason=ExitReason.TAKE_PROFIT if pnl > 0 else ExitReason.STOP_LOSS,
        gross_pnl=pnl, commission=0.0, swap=0.0, bars_held=1, mae=0.0, mfe=0.0,
        balance_after=10_000.0, tag="", sl=sl, tp=tp,
    )


def run(trades: list[Trade], strategy: str = "mz50") -> BacktestResult:
    return BacktestResult(
        strategy=strategy, symbol="EURUSD", timeframe="H4", trades=trades,
        start=START, end=START + timedelta(days=30),
    )


class TestSessionWindows:
    """§3.4's five windows cover half the clock; the rest must go somewhere."""

    def test_the_gaps_are_filled_and_self_named(self):
        names = [w[0] for w in casebook.session_windows()]
        assert names == ["Asia", "06:00-09:00", "Frankfurt", "London", "Lunch",
                         "New York", "18:00-03:00"]

    def test_every_hour_of_the_day_lands_somewhere(self):
        windows = casebook.session_windows()
        labels = {casebook.session_for(h, windows) for h in range(24)}
        assert "Unclassified" not in labels
        assert len([h for h in range(24) if casebook.session_for(h, windows)]) == 24

    def test_the_named_windows_keep_the_specs_boundaries(self):
        windows = dict((n, (a, b)) for n, a, b in casebook.session_windows())
        for name, start, end in casebook.SPEC_SESSIONS:
            assert windows[name] == (start, end)

    def test_the_wrap_around_bucket_covers_midnight(self):
        windows = casebook.session_windows()
        for hour in (18, 21, 0, 2):
            assert casebook.session_for(hour, windows) == "18:00-03:00"

    def test_one_window_still_produces_a_full_day(self):
        windows = casebook.session_windows((("Asia", 3, 6),))
        assert {casebook.session_for(h, windows) for h in range(24)} == {"Asia", "06:00-03:00"}


class TestClassification:
    def test_a_flat_trade_is_break_even(self):
        """§2 has no definition, so this is the decided one: within 0.1R."""
        cases = casebook.build(run([trade(1, 1.10, 1.1000_5, sl=1.09, tp=1.12)]))
        assert cases[0].result == "BE"
        assert cases[0].result_pct == 0.0

    def test_a_winner_and_a_loser(self):
        cases = casebook.build(run([
            trade(1, 1.10, 1.12, sl=1.09, tp=1.12),          # +2R
            trade(2, 1.10, 1.09, sl=1.09, tp=1.12),          # -1R
        ]))
        assert [c.result for c in cases] == ["Win", "Lose"]
        assert cases[0].result_pct == pytest.approx(2.0)
        assert cases[1].result_pct == pytest.approx(-1.0)

    def test_rr_comes_from_the_bracket(self):
        (case,) = casebook.build(run([trade(1, 1.10, 1.12, sl=1.09, tp=1.12)]))
        assert case.rr == pytest.approx(2.0)     # 200 pips reward / 100 risk
        assert case.risk == 1.0

    def test_the_threshold_is_configurable(self):
        trades = [trade(1, 1.10, 1.1050, sl=1.09, tp=1.12)]     # +0.5R
        assert casebook.build(run(trades))[0].result == "Win"
        assert casebook.build(run(trades), be_threshold=0.6)[0].result == "BE"

    def test_a_short_is_a_short(self):
        (case,) = casebook.build(run([
            trade(1, 1.10, 1.08, sl=1.11, tp=1.08, side=Side.SELL)
        ]))
        assert case.direction == "Short" and case.result == "Win"

    def test_classifiers_come_off_the_entry_timestamp(self):
        (case,) = casebook.build(run([trade(1, 1.10, 1.12, 1.09, 1.12, hour=16)]))
        assert case.weekday == "Monday"
        assert case.month == "2024 Jan"
        assert case.session == "New York"

    def test_models_carries_the_strategy(self):
        (case,) = casebook.build(run([trade(1, 1.10, 1.12, 1.09, 1.12)], "day_open"))
        assert case.model == "day_open"


class TestStatistics:
    def composition(self, wins: int, losses: int, be: int) -> list[casebook.Case]:
        trades = (
            [trade(i, 1.10, 1.12, 1.09, 1.12) for i in range(wins)]
            + [trade(100 + i, 1.10, 1.09, 1.09, 1.12) for i in range(losses)]
            + [trade(200 + i, 1.10, 1.1000_2, 1.09, 1.12) for i in range(be)]
        )
        return casebook.build(run(trades))

    def test_winrate_excludes_break_even(self):
        """§3.1. The reference book's exact composition and headline figure."""
        stats = casebook.total_statistics(self.composition(120, 32, 32))
        assert stats["trades"] == 184
        assert (stats["wins"], stats["losses"], stats["be"]) == (120, 32, 32)
        assert stats["winrate_pct"] == pytest.approx(78.95, abs=0.01)

    def test_counting_be_in_the_denominator_would_be_wrong(self):
        """The mistake this guards: 64.17% is what all-trades counting gives."""
        stats = casebook.total_statistics(self.composition(120, 32, 32))
        assert stats["winrate_pct"] != pytest.approx(120 / 184 * 100, abs=0.01)

    def test_a_run_with_no_decided_trades_does_not_divide_by_zero(self):
        assert casebook.total_statistics(self.composition(0, 0, 5))["winrate_pct"] == 0.0

    def test_gained_rr_sums_the_case_results(self):
        cases = self.composition(3, 2, 1)
        assert casebook.total_statistics(cases)["gained_rr"] == pytest.approx(3 * 2 - 2)


class TestViews:
    def cases(self) -> list[casebook.Case]:
        return casebook.build(run([
            trade(1, 1.10, 1.12, 1.09, 1.12, hour=16, day=1),
            trade(2, 1.10, 1.09, 1.09, 1.12, hour=4, day=2),
            trade(3, 1.10, 1.12, 1.09, 1.12, Side.SELL, hour=20, day=3),
        ]))

    def test_every_view_totals_the_database(self):
        """The whole point of §3: they are views, so they cannot disagree."""
        cases = self.cases()
        total = casebook.total_statistics(cases)["trades"]
        for name, rows in casebook.tables(cases).items():
            if name == "total_statistics":
                continue
            assert sum(r["trades"] for r in rows) == total, name

    def test_sessions_carry_their_hours(self):
        rows = {r["name"]: r for r in casebook.by_session(self.cases())}
        assert rows["New York"]["time"] == "14:00-18:00"
        assert rows["18:00-03:00"]["time"] == "18:00-03:00"

    def test_months_are_chronological_not_alphabetical(self):
        cases = casebook.build(run([
            trade(1, 1.10, 1.12, 1.09, 1.12, day=1),
            Trade(id=2, side=Side.BUY, volume=0.1,
                  entry_time=datetime(2024, 4, 1, 12, tzinfo=UTC), entry_price=1.10,
                  exit_time=datetime(2024, 4, 1, 16, tzinfo=UTC), exit_price=1.12,
                  reason=ExitReason.TAKE_PROFIT, gross_pnl=1.0, commission=0.0, swap=0.0,
                  bars_held=1, mae=0.0, mfe=0.0, balance_after=1.0, sl=1.09, tp=1.12),
        ]))
        # "2024 Apr" sorts before "2024 Jan" alphabetically; it must not here.
        assert [r["name"] for r in casebook.by_month(cases)] == ["2024 Jan", "2024 Apr"]


class TestOutput:
    def test_it_writes_the_database_the_views_and_the_report(self, tmp_path):
        result = run([trade(1, 1.10, 1.12, 1.09, 1.12), trade(2, 1.10, 1.09, 1.09, 1.12)])
        casebook.write(tmp_path, result)

        assert (tmp_path / "casebook.csv").exists()
        for view in ("total_statistics", "direction", "models", "sessions",
                     "weekdays", "months"):
            assert (tmp_path / f"casebook_{view}.csv").exists(), view
        assert "# Backtest Summary" in (tmp_path / "report.md").read_text()

    def test_the_database_carries_the_spec_columns(self, tmp_path):
        casebook.write(tmp_path, run([trade(1, 1.10, 1.12, 1.09, 1.12)]))
        (row,) = list(csv.DictReader((tmp_path / "casebook.csv").open()))
        for column in ("Case #", "Pair", "Date", "Weekday", "Direction", "Models",
                       "Sessions", "Months", "RR", "Result", "Risk", "Result Pct",
                       "Win?", "Lose?", "BE?", "BE Reason", "News Event", "Mistake",
                       "To Improve", "Needs validation"):
            assert column in row, column
        assert row["Win?"] == "Yes" and row["Lose?"] == "No" and row["BE?"] == "No"

    def test_review_columns_are_left_for_a_human(self, tmp_path):
        casebook.write(tmp_path, run([trade(1, 1.10, 1.12, 1.09, 1.12)]))
        (row,) = list(csv.DictReader((tmp_path / "casebook.csv").open()))
        assert all(row[c] == "" for c in
                   ("BE Reason", "News Event", "Mistake", "To Improve", "Needs validation"))

    def test_an_empty_run_writes_nothing(self, tmp_path):
        assert casebook.write(tmp_path, run([])) == []
        assert not (tmp_path / "casebook.csv").exists()
