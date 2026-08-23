"""The H4 Senior Extremum / 25% Control Zone pattern, checked against the PDF.

Section numbers below are the specification's own. The tests that matter most
are the ones pinning things the document is explicit about *not* wanting:
domination must be strict (§3.2), the tolerance must come from the pattern's
geometry and never from the market price (§5.2), the approach must be judged on
the candle range and not its close (§6), and nothing may be known before
`ConfirmationTime` (§3.4).
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest

from backtester.core.engine import Backtester
from backtester.core.types import Bar, ExitReason, Side
from backtester.indicators.zigzag import ZigZagTracker, zigzag
from backtester.strategies import get_strategy
from backtester.strategies.margin_zones.approach25 import Senior25Strategy
from backtester.strategies.margin_zones.margins import MarginZones
from backtester.strategies.margin_zones.senior import (
    SeniorApproachTracker,
    SeniorExtremum,
    first_approach,
    senior_extremums,
)

UTC = timezone.utc
START = datetime(2024, 1, 1, tzinfo=UTC)

# A round-numbers zone: 100 pips of FMZ at a pip_size of 1.0 is 100 price
# units, so Distance25 is 25 and the tolerance 2.5. Every expectation below is
# readable without a calculator.
ZONES = MarginZones(
    code="TEST",
    as_of=date(2023, 1, 1),
    maintenance=10_000.0,
    initial=11_000.0,
    pip_value=100.0,
    fmz=100.0,
    imz=110.0,
    mz=10.0,
)


def h4(index: int, high: float, low: float, close: float | None = None) -> Bar:
    return Bar(
        time=START + timedelta(hours=4 * index),
        open=(high + low) / 2 if close is None else close,
        high=high,
        low=low,
        close=(high + low) / 2 if close is None else close,
        volume=100,
        spread=0.0,
    )


def flat(prices: list[float]) -> list[Bar]:
    """One bar per price, with no range at all — a clean ZigZag input."""
    return [h4(i, p, p, p) for i, p in enumerate(prices)]


def walk(waypoints: list[float], step: float = 5.0, pad: int = 0) -> list[float]:
    """Prices marching between waypoints, so swings land exactly where stated.

    `pad` repeats the final price, which lets a test sit still for a known
    number of bars — the only way to age a pattern past `max_wait_bars`.
    """
    out = [float(waypoints[0])]
    for target in waypoints[1:]:
        current = out[-1]
        toward = 1.0 if target > current else -1.0
        while abs(target - current) > 1e-9:
            current += toward * step
            if (target - current) * toward < 0:
                current = float(target)
            out.append(round(current, 6))
    return out + [out[-1]] * pad


def zones_for(_pivot):
    return ZONES


# --------------------------------------------------------------------- zigzag

class TestZigZagTracker:
    """The streaming tracker has to be the batch indicator, or nothing else holds."""

    @pytest.mark.parametrize("deviation", [0.5, 1.0, 2.5])
    def test_streaming_matches_the_batch_indicator(self, deviation):
        prices = walk([1800, 1900, 1830, 1980, 1870, 1950, 1750, 1890, 1810], step=3.0)
        bars = flat(prices)

        tracker = ZigZagTracker(deviation_pct=deviation)
        for bar in bars:
            tracker.push(bar)

        assert tracker.pivots == zigzag(bars, deviation_pct=deviation)

    def test_a_pivot_is_returned_by_the_bar_that_confirmed_it(self):
        bars = flat(walk([1800, 1900, 1800]))
        tracker = ZigZagTracker(deviation_abs=20.0)
        emitted = {}
        for index, bar in enumerate(bars):
            for pivot in tracker.push(bar):
                emitted[pivot.kind] = index

        # The high sits at 1900 but is only knowable 20 points down from it.
        high = next(pivot for pivot in tracker.pivots if pivot.is_high)
        assert high.price == 1900
        assert emitted["high"] == high.confirm_index > high.index

    def test_it_rejects_a_deviation_that_cannot_confirm_anything(self):
        with pytest.raises(ValueError, match="positive deviation"):
            ZigZagTracker(deviation_pct=0)


# ------------------------------------------------------------------- geometry

def extremum(kind: str, price: float, **kwargs) -> SeniorExtremum:
    """A senior extremum with plausible neighbours, for level arithmetic."""
    from backtester.indicators.zigzag import Pivot

    beaten = price - 10 if kind == "high" else price + 10
    pivot = Pivot(10, START, price, kind, 12, START + timedelta(hours=48))
    side = Pivot(0, START, beaten, kind, 2, START)
    return SeniorExtremum(
        pivot=pivot, left=side, right=side, zones=ZONES, pip_size=1.0, **kwargs
    )


class TestLevel25:
    def test_a_maximum_puts_the_level_below_itself(self):
        """§4.2 and the §11 direction rule."""
        e = extremum("high", 2000.0)
        assert e.level25 == pytest.approx(1975.0)   # 2000 - 0.25 * 100
        assert e.level25 < e.price

    def test_a_minimum_puts_the_level_above_itself(self):
        e = extremum("low", 2000.0)
        assert e.level25 == pytest.approx(2025.0)
        assert e.level25 > e.price

    def test_the_level_sits_between_the_extremum_and_the_zone_boundary(self):
        """§11: a quarter of the way to FMZ, not a quarter of the FMZ-IMZ width."""
        e = extremum("high", 2000.0)
        assert e.zone_near == pytest.approx(1900.0)          # 2000 - FMZ
        assert e.price > e.level25 > e.zone_near
        # The wrong reading the spec calls out would land at 2000 - 0.25*(110-100).
        assert e.level25 != pytest.approx(1997.5)

    def test_the_tolerance_is_ten_percent_of_distance25(self):
        """§5.2 — equivalently 2.5% of FMZ."""
        e = extremum("high", 2000.0)
        assert e.distance25 == pytest.approx(25.0)
        assert e.tolerance == pytest.approx(2.5)
        assert e.tolerance == pytest.approx(0.10 * abs(e.price - e.level25))
        assert (e.lower, e.upper) == pytest.approx((1972.5, 1977.5))

    def test_the_tolerance_is_not_a_percentage_of_the_market_price(self):
        """The mistake §5.2 flags by name: `Tolerance = Level25 * 0.10`.

        At these prices that would be 197.5 — nearly two whole FMZs — so the
        two readings cannot be confused by accident once this is pinned.
        """
        e = extremum("high", 2000.0)
        assert e.tolerance == pytest.approx(2.5)
        assert e.tolerance != pytest.approx(e.level25 * 0.10)

    def test_the_tolerance_scales_with_fmz_not_with_price(self):
        cheap = extremum("high", 20.0)
        dear = extremum("high", 20_000.0)
        assert cheap.tolerance == pytest.approx(dear.tolerance)

    def test_an_approach_is_judged_on_the_candle_range(self):
        """§6: `CandleHigh >= LowerBound AND CandleLow <= UpperBound`."""
        e = extremum("high", 2000.0)                      # range [1972.5, 1977.5]
        pierced = h4(0, high=1974.0, low=1950.0, close=1950.0)  # closed well below
        assert e.intersects(pierced)
        assert not e.intersects(h4(0, high=1972.0, low=1950.0))
        assert not e.intersects(h4(0, high=1990.0, low=1978.0))


# -------------------------------------------------------------------- seniors

class TestSeniorIdentification:
    """§3.2/§3.3, via the batch scanner so the pivot structure is explicit."""

    def pivots(self, prices):
        return zigzag(flat(walk(prices, step=3.0)), deviation_abs=20.0)

    def test_a_dominating_high_is_senior(self):
        # rise, dip, HIGHER high, dip, lower high, fall to confirm
        found = senior_extremums(
            self.pivots([1800, 1900, 1850, 1980, 1900, 1940, 1850]), zones_for, 1.0
        )
        assert [(e.label, e.price) for e in found] == [("HIGH", 1980.0)]

    def test_a_dominating_low_is_senior(self):
        found = senior_extremums(
            self.pivots([1980, 1880, 1930, 1800, 1880, 1840, 1930]), zones_for, 1.0
        )
        assert [(e.label, e.price) for e in found] == [("LOW", 1800.0)]

    def test_a_high_beaten_on_the_right_is_not_senior(self):
        assert not senior_extremums(
            self.pivots([1800, 1900, 1850, 1940, 1900, 1980, 1850]), zones_for, 1.0
        )

    def test_a_high_beaten_on_the_left_is_not_senior(self):
        assert not senior_extremums(
            self.pivots([1800, 1980, 1900, 1940, 1880, 1920, 1850]), zones_for, 1.0
        )

    def test_the_neighbours_compared_are_the_same_type(self):
        """`H+1` is two pivots along, not the intervening low."""
        found = senior_extremums(
            self.pivots([1800, 1900, 1850, 1980, 1900, 1940, 1850]), zones_for, 1.0
        )
        assert found[0].left.kind == found[0].right.kind == "high"

    def test_a_pattern_without_a_margin_reading_is_skipped(self):
        assert not senior_extremums(
            self.pivots([1800, 1900, 1850, 1980, 1900, 1940, 1850]),
            lambda pivot: None,
            1.0,
        )


class TestConfirmation:
    """§3.4 — the no-lookahead rule, which is the whole difficulty of the pattern."""

    def bars(self):
        return flat(walk([1800, 1900, 1850, 1980, 1900, 1940, 1850], step=3.0))

    def stream(self, bars, **kwargs):
        tracker = SeniorApproachTracker(zones_for, pip_size=1.0, deviation_abs=20.0, **kwargs)
        for bar in bars:
            tracker.push(bar)
        return tracker

    def test_confirmation_is_the_right_neighbours_confirmation_not_its_extreme(self):
        tracker = self.stream(self.bars())
        senior = tracker.extremums[0]
        assert senior.confirm_time == senior.right.confirm_time
        assert senior.confirm_time > senior.right.time > senior.time

    def test_the_pattern_is_unknown_until_that_bar(self):
        bars = self.bars()
        senior = self.stream(bars).extremums[0]

        partial = SeniorApproachTracker(zones_for, pip_size=1.0, deviation_abs=20.0)
        for bar in bars[: senior.confirm_index]:
            partial.push(bar)
        assert partial.extremums == []

    def test_streaming_finds_what_the_batch_scan_finds(self):
        bars = self.bars()
        streamed = self.stream(bars).extremums
        batch = senior_extremums(zigzag(bars, deviation_abs=20.0), zones_for, 1.0)
        assert [e.price for e in streamed] == [e.price for e in batch]


class TestApproachEvent:
    def build(self, tail: list[float], pad: int = 0, **kwargs):
        """The senior-high fixture, then whatever `tail` does next.

        The fixture confirms one senior maximum at 1980, whose 25% level is
        1955 and whose approach range is therefore [1952.5, 1957.5].
        """
        prices = walk([1800, 1900, 1850, 1980, 1900, 1940, 1850], step=3.0)
        prices += walk([1850] + tail, step=3.0, pad=pad)[1:]
        bars = flat(prices)
        tracker = SeniorApproachTracker(zones_for, pip_size=1.0, deviation_abs=20.0, **kwargs)
        events = [event for bar in bars for event in tracker.push(bar)]
        return tracker, events

    def test_a_return_to_the_level_fires_once(self):
        """§7: the *first* approach. The tail leaves and comes back to 1955."""
        tracker, events = self.build([1955, 1900, 1955])
        fired = [event for event in events if event.extremum.price == 1980.0]

        assert len(fired) == 1
        assert fired[0].time > fired[0].extremum.confirm_time
        # Retired, so the second visit to the same level generates nothing.
        assert 1980.0 not in [pending.price for pending in tracker.pending]

    def test_the_event_carries_the_bar_that_triggered_it(self):
        _, events = self.build([1955])
        event = events[0]
        assert event.low <= event.extremum.upper
        assert event.high >= event.extremum.lower
        assert event.day == event.time.date()

    def test_a_level_never_reached_leaves_the_pattern_pending(self):
        tracker, events = self.build([1860])
        assert events == []
        assert [e.price for e in tracker.pending] == [1980.0]

    def test_crossings_before_confirmation_do_not_count(self):
        """§7. Price passes 1955 on the way down from 1980, long before H+1 exists."""
        tracker, events = self.build([1860])
        senior = tracker.pending[0]
        crossed_early = any(
            senior.intersects(bar)
            for bar in flat(walk([1800, 1900, 1850, 1980, 1900, 1940, 1850], step=3.0))[
                : senior.confirm_index
            ]
        )
        assert crossed_early          # it really did trade through the level
        assert events == []           # and it still did not fire

    def test_max_wait_bars_retires_a_stale_pattern(self):
        tracker, events = self.build([1860], pad=10, max_wait_bars=5)
        assert events == []
        assert [e.price for e in tracker.expired] == [1980.0]
        assert tracker.pending == []

    def test_first_approach_agrees_with_the_stream(self):
        prices = walk([1800, 1900, 1850, 1980, 1900, 1940, 1850], step=3.0)
        prices += walk([1850, 1955, 1900], step=3.0)[1:]
        bars = flat(prices)
        streamed = self.build([1955, 1900])[1][0]
        batch = first_approach(
            senior_extremums(zigzag(bars, deviation_abs=20.0), zones_for, 1.0)[0], bars
        )
        assert batch.time == streamed.time


# ------------------------------------------------------------------- strategy

@pytest.fixture
def desk(tmp_path):
    """A contract whose pip is one price unit, so the zone maths stays readable.

    contract_size * pip_size == tick_value * NP keeps `ContractSpec.problems`
    quiet; a 5,000 maintenance over a 100 pip value is a 50-point FMZ.
    """
    contracts = tmp_path / "contracts"
    contracts.mkdir()
    (contracts / "TEST.json").write_text(
        json.dumps(
            {
                "code": "TEST", "name": "Test future", "exchange": "TEST",
                "contract_size": 100.0, "base_currency": "XAU", "quote_currency": "USD",
                "tick_size": 1.0, "tick_value": 100.0, "pip_size": 1.0,
            }
        )
    )
    log = tmp_path / "margins.csv"
    log.write_text(
        "code,as_of,maintenance,initial,source,note\n"
        "TEST,2023-01-01,5000.0,,test,\n"
    )
    return {
        "contract": "TEST",
        "contracts_dir": str(contracts),
        "margin_log": str(log),
        "deviation_pips": 20.0,
    }


def senior_high_series() -> list[Bar]:
    """Confirms a senior high at 1980, then rallies back to its 25% level.

    FMZ is 50 points here, so Level25 = 1980 - 12.5 = 1967.5 and the approach
    range is [1966.25, 1968.75]. The tail returns to 1968 and then falls away.
    """
    prices = walk([1800, 1900, 1850, 1980, 1900, 1940, 1850], step=3.0)
    prices += walk([1850, 1968, 1900], step=3.0)[1:]
    return flat(prices)


def run(strategy, bars, gold, config, timeframe="H4"):
    return Backtester(
        strategy=strategy, symbol="XAUUSD", timeframe=timeframe,
        instrument=gold, execution=config,
    ).run(bars)


class TestStrategy:
    def test_it_is_registered(self):
        assert get_strategy("senior25") is Senior25Strategy

    def test_a_senior_high_approach_is_sold(self, desk, gold, config):
        result = run(Senior25Strategy(**desk), senior_high_series(), gold, config)
        assert len(result.trades) == 1
        assert result.trades[0].side is Side.SELL

    def test_the_entry_comes_after_the_approach_not_on_it(self, gold, config, desk):
        """The engine's rule, restated where it matters: detection on a closed
        bar can only be acted on at the next open."""
        strategy = Senior25Strategy(**desk)
        result = run(strategy, senior_high_series(), gold, config)
        event = strategy.tracker.events[0]
        assert result.trades[0].entry_time > event.time
        assert event.time >= event.extremum.confirm_time

    def test_the_stop_sits_beyond_the_extremum(self, desk, gold, config):
        strategy = Senior25Strategy(**desk, stop_buffer=0.10)
        stops: dict[int, float] = {}
        original = strategy.on_bar

        def spy(ctx, bar):
            original(ctx, bar)
            stops.update({p.id: p.sl for p in ctx.positions})

        strategy.on_bar = spy
        run(strategy, senior_high_series(), gold, config)
        # FMZ is 50 here, so Distance25 is 12.5 and the buffer 1.25.
        assert list(stops.values()) == [pytest.approx(1981.25)]

    def test_the_target_is_the_near_zone_boundary(self, desk, gold, config):
        strategy = Senior25Strategy(**desk, target_fraction=1.0)
        result = run(strategy, senior_high_series(), gold, config)
        assert result.trades[0].reason is ExitReason.TAKE_PROFIT
        assert result.trades[0].exit_price == pytest.approx(1930.0)  # 1980 - FMZ

    def test_bias_extremum_takes_the_other_side(self, desk, gold, config):
        strategy = Senior25Strategy(
            **desk, bias="extremum", stop_mode="distance", target_fraction=0.0
        )
        result = run(strategy, senior_high_series(), gold, config)
        assert result.trades[0].side is Side.BUY

    def test_bias_none_detects_without_trading(self, desk, gold, config):
        strategy = Senior25Strategy(**desk, bias="none")
        result = run(strategy, senior_high_series(), gold, config)
        assert result.trades == []
        assert len(strategy.tracker.events) == 1

    def test_trades_carry_the_tag_that_joins_them_to_the_event_table(self, desk, gold, config):
        strategy = Senior25Strategy(**desk)
        result = run(strategy, senior_high_series(), gold, config)
        assert result.trades[0].tag == "senior25 HIGH 2024-01-16 20:00"

    def test_the_event_table_holds_one_row_per_extremum(self, desk, gold, config, tmp_path):
        out = tmp_path / "events.csv"
        strategy = Senior25Strategy(**desk, events_csv=str(out))
        run(strategy, senior_high_series(), gold, config)

        import csv

        rows = list(csv.DictReader(out.open()))
        assert len(rows) == 1
        row = rows[0]
        assert row["extremum_type"] == "HIGH"
        assert float(row["extremum_price"]) == 1980.0
        assert float(row["level25"]) == pytest.approx(1967.5)
        assert float(row["tolerance"]) == pytest.approx(1.25)
        assert row["approached"] == "True" and row["traded"] == "True"
        # §8 asks for all of these by name.
        for column in ("instrument", "confirmation_time", "fmz_pips", "imz_pips",
                       "approach_day", "approach_bar_high", "approach_bar_low"):
            assert row[column] != ""

    def test_an_untouched_level_is_still_reported(self, desk, gold, config, tmp_path):
        out = tmp_path / "events.csv"
        prices = walk([1800, 1900, 1850, 1980, 1900, 1940, 1850], step=3.0)
        strategy = Senior25Strategy(**desk, events_csv=str(out))
        run(strategy, flat(prices), gold, config)

        import csv

        rows = list(csv.DictReader(out.open()))
        assert len(rows) == 1
        assert rows[0]["approached"] == "False"
        assert rows[0]["approach_time"] == ""


class TestStrategyValidation:
    def run_bars(self, strategy, gold, config):
        return run(strategy, flat(walk([1800, 1900], step=3.0)), gold, config)

    def test_an_unknown_bias_is_rejected(self, desk, gold, config):
        with pytest.raises(ValueError, match="bias must be one of"):
            self.run_bars(Senior25Strategy(**desk, bias="sideways"), gold, config)

    def test_a_stop_at_the_extremum_conflicts_with_bias_extremum(self, desk, gold, config):
        with pytest.raises(ValueError, match="puts the stop where"):
            self.run_bars(
                Senior25Strategy(**desk, bias="extremum", stop_mode="extremum"), gold, config
            )

    def test_a_target_short_of_the_entry_is_rejected(self, desk, gold, config):
        with pytest.raises(ValueError, match="must be beyond level_fraction"):
            self.run_bars(Senior25Strategy(**desk, target_fraction=0.2), gold, config)

    def test_a_contract_with_no_margin_reading_is_rejected(self, desk, gold, config, tmp_path):
        empty = tmp_path / "empty.csv"
        empty.write_text("code,as_of,maintenance,initial,source,note\n")
        with pytest.raises(ValueError, match="No margin readings"):
            self.run_bars(
                Senior25Strategy(**{**desk, "margin_log": str(empty)}), gold, config
            )

    def test_a_mismatched_timeframe_is_only_a_warning(self, desk, gold, config):
        strategy = Senior25Strategy(**desk)
        result = run(strategy, senior_high_series(), gold, config, timeframe="M15")
        assert any("running on M15" in line for line in result.logs)
        assert result.trades


# ------------------------------------------------------- run output and chart

class TestRunArtifacts:
    """What a run leaves behind, beyond its trades.

    A trade list cannot say why the strategy traded, or record the 10 senior
    extremums it found and never traded at all. `Strategy.artifacts` is the way
    that reaches the run directory, and `plot_run.py` draws §9 off it.
    """

    def test_the_events_table_is_published_as_an_artifact(self, desk, gold, config):
        strategy = Senior25Strategy(**desk)
        result = run(strategy, senior_high_series(), gold, config)

        assert "events" in result.artifacts
        assert result.artifacts["events"][0]["extremum_type"] == "HIGH"

    def test_saving_a_run_writes_it_beside_trades_csv(self, desk, gold, config, tmp_path):
        import csv

        from backtester.data.results import save_result
        from backtester.metrics import compute

        strategy = Senior25Strategy(**desk)
        result = run(strategy, senior_high_series(), gold, config)
        directory = save_result(result, compute(result).to_dict(), root=tmp_path)

        rows = list(csv.DictReader((directory / "events.csv").open()))
        assert len(rows) == 1
        assert float(rows[0]["level25"]) == pytest.approx(1967.5)

    def test_a_strategy_that_publishes_nothing_adds_no_files(self, gold, config, tmp_path):
        from backtester.data.results import save_result
        from backtester.metrics import compute
        from backtester.strategies.day_open import DayOpenStrategy

        result = run(DayOpenStrategy(volume=0.1), senior_high_series(), gold, config)
        directory = save_result(result, compute(result).to_dict(), root=tmp_path)

        assert result.artifacts == {}
        assert sorted(p.name for p in directory.iterdir()) == [
            "equity.csv", "run.log", "summary.json", "trades.csv",
        ]


@pytest.mark.skipif(shutil.which("node") is None, reason="needs node to execute the page")
class TestZonesChart:
    """The run's chart is the margin-zones chart with the strategy on it.

    Not a second, plainer chart drawn from the trade list: the zone picture is
    the analysis the strategy was built on, so the run gets that exact chart —
    same pivots, same envelopes, same colours — with the 25% levels, approach
    events and orders as layers on top.

    Its JS is one top-level script, so anything that throws in it renders a
    blank page: no chart, no tables, and no error unless you open a console.
    `tests/render_check.js` executes it against a stub DOM so that surfaces
    here instead.
    """

    CHECK = Path(__file__).resolve().parent / "render_check.js"

    def run_page(self, tmp_path, html: str, view_w: int = 900) -> str:
        page = tmp_path / "chart.html"
        page.write_text(html, encoding="utf-8")
        done = subprocess.run(
            ["node", str(self.CHECK), str(page)],
            capture_output=True, text=True, timeout=120,
            env={**os.environ, "VIEW_W": str(view_w)},
        )
        assert done.returncode == 0, done.stdout + done.stderr
        return done.stdout

    def chart_html(self, desk, gold, config, tmp_path, **params) -> str:
        from backtester.data.results import save_result
        from backtester.metrics import compute

        strategy = Senior25Strategy(**desk, **params)
        result = run(strategy, senior_high_series(), gold, config)
        directory = save_result(result, compute(result).to_dict(), root=tmp_path)
        assert strategy.chart(directory, "parquet://data/bars", "H4") is not None
        return (directory / "chart.html").read_text(encoding="utf-8")

    def test_the_run_renders_the_zones_chart(self, desk, gold, config, tmp_path):
        out = self.run_page(tmp_path, self.chart_html(desk, gold, config, tmp_path))
        assert "OK:" in out
        assert "margin zones" in out          # the zones page, not the generic one

    def test_levels_and_orders_are_drawn_on_it(self, desk, gold, config, tmp_path):
        """Wide viewport so the whole series is on screen at once."""
        out = self.run_page(
            tmp_path, self.chart_html(desk, gold, config, tmp_path), view_w=20_000
        )
        assert "pattern elements drawn: 0" not in out
        assert "order elements drawn  : 0" not in out

    def test_a_detection_only_run_still_charts(self, desk, gold, config, tmp_path):
        """`bias=none` places no orders; the zones and levels must still draw."""
        out = self.run_page(
            tmp_path,
            self.chart_html(desk, gold, config, tmp_path, bias="none"),
            view_w=20_000,
        )
        assert "OK:" in out
        assert "order elements drawn  : 0" in out
        assert "pattern elements drawn: 0" not in out


class TestZonesReportLayout:
    """A backtest directory holds the same files an analysis run does."""

    def write(self, desk, gold, config, tmp_path):
        from backtester.data.results import save_result
        from backtester.metrics import compute

        strategy = Senior25Strategy(**desk)
        result = run(strategy, senior_high_series(), gold, config)
        directory = save_result(result, compute(result).to_dict(), root=tmp_path)
        strategy.chart(directory, "parquet://data/bars", "H4")
        return directory

    def test_it_writes_the_zone_report_beside_the_backtest(self, desk, gold, config, tmp_path):
        directory = self.write(desk, gold, config, tmp_path)
        present = {p.name for p in directory.iterdir()}
        assert {"chart.html", "pivots.csv", "envelopes.csv"} <= present   # the zones report
        assert {"trades.csv", "equity.csv", "summary.json", "events.csv"} <= present

    def test_the_chart_uses_the_runs_own_zigzag(self, desk, gold, config, tmp_path):
        """No second ZigZag and no second margin read, so it cannot disagree."""
        import csv

        from backtester.data.results import save_result
        from backtester.metrics import compute

        strategy = Senior25Strategy(**desk)
        result = run(strategy, senior_high_series(), gold, config)
        directory = save_result(result, compute(result).to_dict(), root=tmp_path)
        strategy.chart(directory, "parquet://data/bars", "H4")

        drawn = list(csv.DictReader((directory / "pivots.csv").open()))
        assert len(drawn) == len(strategy.tracker.pivots)
        assert [float(r["price"]) for r in drawn] == [
            p.price for p in strategy.tracker.pivots
        ]

    def test_another_strategy_still_gets_the_generic_chart(self, gold, config):
        from backtester.strategies.day_open import DayOpenStrategy

        assert DayOpenStrategy(volume=0.1).chart(Path("."), "parquet://data/bars", "H4") is None


class TestChartSummaries:
    """The backtest's own record must survive the zone report being written."""

    def write(self, desk, gold, config, tmp_path, **params):
        from backtester.data.results import save_result
        from backtester.metrics import compute

        strategy = Senior25Strategy(**desk, **params)
        result = run(strategy, senior_high_series(), gold, config)
        directory = save_result(result, compute(result).to_dict(), root=tmp_path)
        strategy.chart(directory, "parquet://data/bars", "H4")
        return directory

    def test_summary_json_still_holds_the_backtest(self, desk, gold, config, tmp_path):
        """`write_report` writes a summary too; it must not land on this one."""
        directory = self.write(desk, gold, config, tmp_path)
        summary = json.loads((directory / "summary.json").read_text())

        assert summary["strategy"] == "senior25"
        assert "metrics" in summary and "params" in summary
        assert summary["metrics"]["trades"] == 1

    def test_the_zone_summary_lands_beside_it(self, desk, gold, config, tmp_path):
        directory = self.write(desk, gold, config, tmp_path)
        zones = json.loads((directory / "zones.json").read_text())

        assert "zigzag" in zones and "zones" in zones

    def test_the_chart_quotes_the_runs_own_metrics(self, desk, gold, config, tmp_path):
        """Read back from summary.json, never recomputed, so the chart and the
        printed report cannot disagree about the same run."""
        directory = self.write(desk, gold, config, tmp_path)
        page = (directory / "chart.html").read_text(encoding="utf-8")
        summary = json.loads((directory / "summary.json").read_text())

        payload = json.loads(re.search(r'const DATA\s*=\s*(\{.*?\});', page, re.S).group(1))
        assert payload["backtest"]["trades"] == summary["metrics"]["trades"]
        assert payload["backtest"]["net_profit"] == summary["metrics"]["net_profit"]

    def test_an_analysis_run_has_no_backtest_block(self, desk, gold, config, tmp_path):
        """`bias=none` places no orders, so the chart shows zone stats only."""
        directory = self.write(desk, gold, config, tmp_path, bias="none")
        page = (directory / "chart.html").read_text(encoding="utf-8")
        payload = json.loads(re.search(r'const DATA\s*=\s*(\{.*?\});', page, re.S).group(1))

        assert payload["trades"] == []
        assert payload["backtest"]["trades"] == 0
