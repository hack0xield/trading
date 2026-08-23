"""True 50% Crossing -> 100% MZ, checked against `impl-spec/Backtest Strategy(crossing50).pdf`.

Section numbers are the specification's own. The load-bearing tests are the
ones pinning what the document is strict about: only True crossings trade (§1),
the target is the *far* boundary and not the near one (§2), the stop mirrors the
target distance about the price actually paid (§3), and a pair must sit under
one unchanged zone with no missed rollover day between them (§5).
"""

from __future__ import annotations

import csv
import json
from datetime import date, datetime, timedelta, timezone

import pytest

from backtester.core.engine import Backtester
from backtester.core.types import Bar, ExitReason, Side
from backtester.strategies import get_strategy
from backtester.strategies.margin_zones.crossing import CrossingTracker
from backtester.strategies.margin_zones.crossing50 import Crossing50Strategy
from backtester.strategies.margin_zones.margins import MarginZones
from backtester.strategies.margin_zones.rollover import RolloverTracker, rollover_points

UTC = timezone.utc
START = datetime(2024, 1, 1, tzinfo=UTC)

# 100 pips of FMZ at a pip_size of 1.0 is 100 price units, so a zone from a
# high at 2000 runs 1900 (FMZ) to 1890 (IMZ), its 50% MZ midpoint is 1895 and
# the E50 level — halfway from the extremum to that midpoint — is 1947.5.
ZONES = MarginZones(
    code="TEST", as_of=date(2023, 1, 1), maintenance=10_000.0, initial=11_000.0,
    pip_value=100.0, fmz=100.0, imz=110.0, mz=10.0,
)


def h4(index: int, price: float, high: float | None = None, low: float | None = None) -> Bar:
    return Bar(
        time=START + timedelta(hours=4 * index),
        open=price, high=price if high is None else high,
        low=price if low is None else low, close=price, volume=100, spread=0.0,
    )


def zones_for(_pivot):
    return ZONES


def stream(bars, **kwargs):
    tracker = CrossingTracker(zones_for, pip_size=1.0, deviation_abs=20.0, **kwargs)
    fired = [signal for bar in bars for signal in tracker.push(bar)]
    return tracker, fired


class TestRolloverTracker:
    """The streaming half of the signal has to be the batch function."""

    @pytest.mark.parametrize("hour", [0, 8, 21])
    def test_streaming_matches_the_batch_points(self, hour):
        bars = [h4(i, 1800 + (i % 11)) for i in range(120)]
        batch = rollover_points(bars, hour, "UTC")

        tracker = RolloverTracker(hour, "UTC")
        for bar in bars:
            tracker.push(bar)

        # The batch walks one day past the last settled break and emits a
        # provisional point from the final bar; the tracker waits for the
        # instant to actually pass. Everything before that must agree exactly.
        assert tracker.points == batch[: len(tracker.points)]
        assert 0 <= len(batch) - len(tracker.points) <= 1

    def test_a_point_is_the_close_before_the_break(self):
        bars = [h4(i, 1800 + i) for i in range(12)]
        tracker = RolloverTracker(0, "UTC")
        points = [p for bar in bars for p in tracker.push(bar)]

        # Bars are 4h from 2024-01-01 00:00, so the 20:00 bar (index 5) is the
        # last before the next midnight, and its close is the point's price.
        assert points[0].price == bars[5].close
        assert points[0].day == date(2024, 1, 2)

    def test_a_day_with_no_new_bar_produces_nothing(self):
        """Rather than repeating the previous day's price (§5.4)."""
        bars = [h4(0, 1800), h4(1, 1801), h4(30, 1802)]   # a long hole
        tracker = RolloverTracker(0, "UTC")
        points = [p for bar in bars for p in tracker.push(bar)]
        assert len({p.price for p in points}) == len(points)


def series(prices: list[float]) -> list[Bar]:
    """A confirmed high at 2000, then one rollover day per entry in `prices`.

    Six H4 bars make a day, so a price every sixth bar is a price every
    rollover, which is what the crossing rule consumes. The zone is the high's:
    below the extremum, E50 at 1947.5.

    The trailing day matters. A rollover point is the close of the last bar
    *before* the break, so it does not exist until a bar arrives on the far
    side of it — the last price block needs a day after it to settle.
    """
    bars = [h4(i, p) for i, p in enumerate([1900, 1960, 2000, 1975])]
    bars += [h4(4 + i, 1940) for i in range(20)]          # confirm the high,
                                                          # below E50 throughout
    start = len(bars)
    for k, price in enumerate(prices):
        bars += [h4(start + k * 6 + j, price) for j in range(6)]
    tail = len(bars)
    bars += [h4(tail + j, prices[-1]) for j in range(6)]  # settle the last point
    return bars


class TestSignal:
    """§1 — which crossings trade, and in which direction."""

    def test_a_true_crossing_from_a_high_is_a_short(self):
        """Zone below the extremum, rollover falls through E50 (1947.5)."""
        _, fired = stream(series([1955, 1940]))
        assert len(fired) == 1
        signal = fired[0]
        assert not signal.is_long
        assert signal.crossing.classification == "True"
        assert signal.crossing.direction == "down"

    def test_a_false_crossing_is_not_a_signal(self):
        """Same zone, rollover rises back through the level — away from it."""
        tracker, fired = stream(series([1940, 1955]))
        assert fired == []
        assert [c.classification for c in tracker.crossings] == ["False"]

    def test_a_price_sitting_on_the_level_is_not_a_crossing(self):
        """Validation rule 17: both sides must be strictly nonzero."""
        _, fired = stream(series([1947.5, 1940]))
        assert fired == []

    def test_the_entry_is_the_second_rollover_price(self):
        _, fired = stream(series([1955, 1940]))
        assert fired[0].entry == 1940
        assert fired[0].crossing.previous.price == 1955


class TestTargetAndStop:
    """§2 and §3."""

    def signal(self):
        return stream(series([1955, 1940]))[1][0]

    def test_the_target_is_the_far_boundary_not_the_near_one(self):
        """§2: 100% MZ is IMZ. FMZ (1900) would be the near edge."""
        signal = self.signal()
        assert signal.take_profit == pytest.approx(1890.0)
        assert signal.zone.fmz_price == pytest.approx(1900.0)

    def test_risk_equals_the_target_distance(self):
        signal = self.signal()
        assert signal.risk == pytest.approx(abs(signal.entry - signal.take_profit))

    def test_the_stop_mirrors_the_target_about_the_fill(self):
        """§3 measured from what was paid, not from the signal price."""
        signal = self.signal()                      # SHORT, entry 1940, TP 1890
        assert signal.stop_for(1940.0) == pytest.approx(1990.0)
        # Filled 5 away from the signal price: the stop moves with it, and the
        # ratio stays 1:1 rather than drifting.
        assert signal.stop_for(1935.0) == pytest.approx(1980.0)
        assert abs(1935.0 - signal.stop_for(1935.0)) == pytest.approx(
            abs(1935.0 - signal.take_profit)
        )


class TestPairingRules:
    """§5 — when two rollover points may not be paired at all."""

    def test_points_under_different_zones_do_not_pair(self):
        """§5: a new extremum resets the crossing state.

        The jump to 2100 confirms a low at 1940 and so replaces the zone. The
        points either side of that change — 1940 and 2100 — straddle the *new*
        zone's E50 by a wide margin, so pairing them would produce a crossing.
        They belong to different zones, so no event is created.
        """
        bars = [h4(i, p) for i, p in enumerate([1900, 1960, 2000, 1975])]
        bars += [h4(4 + i, 1940) for i in range(20)]
        start = len(bars)
        bars += [h4(start + j, 2100) for j in range(12)]      # two settled days
        tracker, fired = stream(bars)

        assert fired == []
        assert tracker.crossings == []
        # The pair really would have crossed, so the rule is what suppressed it.
        new_zone = tracker.zones[-1]
        assert new_zone.pivot.price == 1940 and new_zone.direction > 0
        assert 1940 < new_zone.e50_price < 2100
        assert {p.price for p in tracker.points} == {1940, 2100}

    def test_a_wide_gap_between_points_does_not_pair(self):
        """§5: no signal across a missed rollover day."""
        bars = [h4(i, p) for i, p in enumerate([1900, 1960, 2000, 1975])]
        bars += [h4(4 + i, 1940) for i in range(20)]
        start = len(bars)
        bars += [h4(start + j, 1960) for j in range(6)]
        bars += [h4(start + 200 + j, 1930) for j in range(6)]   # ~33 days later
        bars += [h4(start + 206 + j, 1930) for j in range(6)]
        _, fired = stream(bars, max_gap_days=3)
        assert fired == []

    def test_the_zone_activates_at_confirmation_not_at_the_extreme(self):
        """The whole reason this cannot reuse `rollover_crossings`."""
        bars = [h4(i, p) for i, p in enumerate([1900, 1960, 2000, 1975])]
        bars += [h4(4 + i, 1940) for i in range(20)]
        tracker, _ = stream(bars)
        zone = tracker.zones[-1]
        assert zone.pivot.price == 2000
        assert zone.pivot.confirm_index > zone.pivot.index


# ------------------------------------------------------------------ strategy

@pytest.fixture
def desk(tmp_path):
    contracts = tmp_path / "contracts"
    contracts.mkdir()
    (contracts / "TEST.json").write_text(json.dumps({
        "code": "TEST", "name": "Test future", "exchange": "TEST",
        "contract_size": 100.0, "base_currency": "XAU", "quote_currency": "USD",
        "tick_size": 1.0, "tick_value": 100.0, "pip_size": 1.0,
    }))
    log = tmp_path / "margins.csv"
    log.write_text("code,as_of,maintenance,initial,source,note\nTEST,2023-01-01,5000.0,,test,\n")
    return {
        "contract": "TEST", "contracts_dir": str(contracts),
        "margin_log": str(log), "deviation_pips": 20.0,
    }


def short_series() -> list[Bar]:
    """Confirms a high at 2000, crosses E50 downward, then reaches 100% MZ.

    A 5,000 maintenance over a 100 pip value is a 50-pip FMZ, so the zone runs
    1950 (FMZ) to 1945 (IMZ), its midpoint is 1947.5 and E50 — halfway from the
    extremum to that midpoint — is 1973.75. A rollover pair of 1980 -> 1970
    therefore crosses it downward, which is toward the zone and so True.

    The tail drifts rather than gapping: the order fills at the open after the
    signal, and a jump straight past 1945 would fill below the target and make
    the test about gap handling instead of about the rule.
    """
    bars = [h4(i, p) for i, p in enumerate([1900, 1960, 2000, 1975])]
    bars += [h4(4 + i, 1980) for i in range(20)]
    start = len(bars)
    for k, price in enumerate([1980, 1970]):
        bars += [h4(start + k * 6 + j, price) for j in range(6)]
    tail = len(bars)
    bars += [h4(tail + j, 1965) for j in range(6)]                       # the fill
    bars += [h4(tail + 6 + j, 1960, high=1966, low=1940) for j in range(6)]  # to TP
    return bars


def run(strategy, bars, gold, config, timeframe="H4"):
    return Backtester(
        strategy=strategy, symbol="XAUUSD", timeframe=timeframe,
        instrument=gold, execution=config,
    ).run(bars)


class TestStrategy:
    def test_it_is_registered(self):
        assert get_strategy("crossing50") is Crossing50Strategy

    def test_a_true_crossing_from_a_high_sells(self, desk, gold, config):
        result = run(Crossing50Strategy(**desk), short_series(), gold, config)
        assert len(result.trades) == 1
        assert result.trades[0].side is Side.SELL

    def test_the_entry_comes_after_the_signal(self, desk, gold, config):
        """A rollover price is a bar's close; the earliest honest fill is the
        next open."""
        strategy = Crossing50Strategy(**desk)
        result = run(strategy, short_series(), gold, config)
        signal = strategy.tracker.signals[0]
        assert result.trades[0].entry_time > signal.crossing.current.roll_time

    def test_the_stop_is_symmetric_about_the_actual_fill(self, desk, gold, config):
        strategy = Crossing50Strategy(**desk)
        stops = {}
        original = strategy.on_bar

        def spy(ctx, bar):
            original(ctx, bar)
            stops.update({p.id: (p.entry_price, p.sl, p.tp) for p in ctx.positions})

        strategy.on_bar = spy
        run(strategy, short_series(), gold, config)

        (entry, sl, tp), = stops.values()
        assert abs(entry - sl) == pytest.approx(abs(entry - tp))

    def test_trade_false_detects_without_trading(self, desk, gold, config):
        strategy = Crossing50Strategy(**desk, trade=False)
        result = run(strategy, short_series(), gold, config)
        assert result.trades == []
        assert len(strategy.tracker.signals) == 1

    def test_the_signal_table_carries_the_spec_fields(self, desk, gold, config, tmp_path):
        out = tmp_path / "signals.csv"
        strategy = Crossing50Strategy(**desk, signals_csv=str(out))
        result = run(strategy, short_series(), gold, config)

        (row,) = list(csv.DictReader(out.open()))
        for column in ("instrument", "zone_id", "extremum_type", "extremum_timestamp",
                       "signal_timestamp", "direction", "entry_price", "100pct_mz_price",
                       "take_profit", "stop_loss", "risk_distance", "exit_timestamp",
                       "exit_price", "result", "R_result"):
            assert row[column] != "", column
        assert row["direction"] == "SHORT"
        assert row["result"] in ("WIN", "LOSS")

    def test_it_is_published_as_a_run_artifact(self, desk, gold, config):
        strategy = Crossing50Strategy(**desk)
        result = run(strategy, short_series(), gold, config)
        assert "signals" in result.artifacts

    def test_a_contract_with_no_margin_reading_is_rejected(self, desk, gold, config, tmp_path):
        empty = tmp_path / "empty.csv"
        empty.write_text("code,as_of,maintenance,initial,source,note\n")
        with pytest.raises(ValueError, match="No margin readings"):
            run(Crossing50Strategy(**{**desk, "margin_log": str(empty)}),
                short_series(), gold, config)


class TestChartIdentity:
    """A backtest chart and an analysis chart draw the same layers.

    The heading is the only thing that distinguishes them, so it has to say
    which one you are looking at.
    """

    def payload(self, **over) -> dict:
        from backtester.strategies.margin_zones.report import build_payload
        from backtester.strategies.margin_zones.margins import load_spec

        bars = [h4(i, 1800 + i) for i in range(10)]
        return build_payload(
            symbol="EURUSD", timeframe="H4", bars=bars, pivots=[], envelopes=[],
            prov=None, spec=load_spec("6E"), deviation="2%", initial_ratio=1.1,
            **over,
        )

    def title_of(self, tmp_path, payload: dict) -> str:
        """The <title> `write_report` puts on the page."""
        import re

        from backtester.strategies.margin_zones.margins import load_spec
        from backtester.strategies.margin_zones.report import write_report

        write_report(tmp_path, payload, [], [], [], load_spec("6E"),
                     summary_name="zones.json")
        page = (tmp_path / "chart.html").read_text(encoding="utf-8")
        return re.search(r"<title>(.*?)</title>", page).group(1)

    def test_a_backtest_names_its_strategy(self, tmp_path):
        payload = self.payload(strategy="crossing50")
        assert payload["strategy"] == "crossing50"
        assert self.title_of(tmp_path, payload) == "EURUSD H4 — crossing50 backtest"

    def test_an_analysis_run_says_margin_zones(self, tmp_path):
        payload = self.payload()
        assert payload["strategy"] is None
        assert self.title_of(tmp_path, payload) == "EURUSD H4 — margin zones"
