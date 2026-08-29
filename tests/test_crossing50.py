"""Provisional-candidate Margin Zone strategy.

Checked against `impl-spec/Provisional_ZigZag_MZ50_Strategy_Spec.md`. §15 lists
twelve minimum tests and §14 fourteen invariants; the class names below say
which is which.

The point of the whole design is that a zone anchored on a *moving* candidate
must never let a later anchor touch an earlier observation. Most of what
follows is that one property, approached from different directions.
"""

from __future__ import annotations

import csv
import json
from datetime import date, datetime, timedelta, timezone

import pytest

from backtester.core.engine import Backtester
from backtester.core.types import Bar, Side
from backtester.indicators.zigzag import Candidate, ZigZagTracker
from backtester.strategies import get_strategy
from backtester.strategies.margin_zones.crossing import CrossingTracker
from backtester.strategies.margin_zones.crossing50 import (
    CLOSE_ON_CANDIDATE_UPDATE,
    KEEP_OPEN,
    Crossing50Strategy,
)
from backtester.strategies.margin_zones.margins import MarginZones
from backtester.strategies.margin_zones.provisional import (
    INITIAL,
    STRICT_EXTENSION,
    ProvisionalZoneTracker,
)

UTC = timezone.utc
START = datetime(2024, 1, 1, tzinfo=UTC)

# 100 pips of FMZ and 110 of IMZ at a pip_size of 1.0, so from a high at 2000:
#   MZ0 1900, MZ100 1890, midpoint 1895, e50 = (2000 + 1895) / 2 = 1947.5
ZONES = MarginZones(code="TEST", as_of=date(2023, 1, 1), maintenance=10_000.0,
                    initial=11_000.0, pip_value=100.0, fmz=100.0, imz=110.0, mz=10.0)


def h4(i: int, price: float) -> Bar:
    return Bar(time=START + timedelta(hours=4 * i), open=price, high=price,
               low=price, close=price, volume=100, spread=0.0)


def zones_for(_candidate):
    return ZONES


def feed(prices, cls=ProvisionalZoneTracker, **kw):
    tracker = cls(zones_for, pip_size=1.0, deviation_abs=80.0, **kw)
    out = [tracker.push(h4(i, p)) for i, p in enumerate(prices)]
    return tracker, out


class TestCandidate:
    """§3.2, §3.3 — what the ZigZag is currently tracking, and what changes it."""

    def test_no_candidate_before_a_direction_exists(self):
        """§3.2: with direction 0 the tracker watches both ends; do not trade."""
        zz = ZigZagTracker(deviation_abs=80.0)
        zz.push(h4(0, 1800))
        assert zz.candidate is None

    def test_the_candidate_is_the_running_extreme(self):
        zz = ZigZagTracker(deviation_abs=80.0)
        for i, p in enumerate([1800, 1900, 1860, 2000]):
            zz.push(h4(i, p))
        assert zz.candidate.kind == "high"
        assert zz.candidate.price == 2000

    def test_a_strict_extension_is_an_update(self):
        a = Candidate(leg=1, kind="high", price=100.0, index=0, time=START)
        assert Candidate(leg=1, kind="high", price=101.0, index=1, time=START).extends(a)

    def test_an_equal_extreme_is_not_an_update(self):
        """§3.3: it may move the ZigZag index, but nothing about the level changed."""
        a = Candidate(leg=1, kind="high", price=100.0, index=0, time=START)
        assert not Candidate(leg=1, kind="high", price=100.0, index=9, time=START).extends(a)

    def test_a_different_leg_is_not_an_update(self):
        """§3.3: confirmation and the flip to the opposite search are not updates."""
        a = Candidate(leg=1, kind="high", price=100.0, index=0, time=START)
        assert not Candidate(leg=2, kind="low", price=90.0, index=5, time=START).extends(a)


class TestZoneVersions:
    """§3.4, §4 — one immutable zone per anchor, levels frozen at creation."""

    def test_levels_follow_the_kept_e50_formula(self):
        """§4 as amended: e50 is anchor ± (dFMZ + dIMZ) / 4, not the midpoint."""
        _, out = feed([1800, 1900, 1860, 2000, 1960])
        version = next(v for v in out if v is not None)
        assert version.kind == "high"
        anchor = version.anchor_price
        assert version.mz0 == pytest.approx(anchor - 100)
        assert version.mz100 == pytest.approx(anchor - 110)
        assert version.mz50_midpoint == pytest.approx(anchor - 105)
        assert version.e50 == pytest.approx(anchor - 52.5)

    def test_a_strict_extension_creates_a_new_version(self):
        tracker, _ = feed([1800, 1900, 1860, 2000, 2050, 2100])
        events = [v.event_type for v in tracker.versions]
        assert events[0] == INITIAL
        assert STRICT_EXTENSION in events
        assert [v.anchor_price for v in tracker.versions] == sorted(
            v.anchor_price for v in tracker.versions
        )

    def test_an_equal_high_creates_no_version(self):
        """§15.2, the second minimum test."""
        tracker, _ = feed([1800, 1900, 1860, 2000, 2000, 2000])
        anchors = [v.anchor_price for v in tracker.versions]
        assert anchors.count(2000) == 1

    def test_versions_are_immutable(self):
        """§14.4: a historical zone level never changes after creation."""
        tracker, _ = feed([1800, 1900, 1860, 2000, 2050])
        first = tracker.versions[0]
        with pytest.raises(Exception):
            first.anchor_price = 999.0

    def test_a_version_is_never_knowable_before_its_bar(self):
        """§14.1: valid_from >= known_time, and known_time is a bar close."""
        tracker, _ = feed([1800, 1900, 1860, 2000, 2050])
        for v in tracker.versions:
            assert v.known_time >= v.anchor_time

    def test_a_candidate_with_no_margin_reading_makes_no_zone(self):
        tracker = ProvisionalZoneTracker(lambda c: None, pip_size=1.0, deviation_abs=80.0)
        for i, p in enumerate([1800, 1900, 1860, 2000]):
            tracker.push(h4(i, p))
        assert tracker.versions == [] and tracker.uncovered


class TestCrossing:
    """§6 — when two rollover observations form a signal."""

    def series(self, tail: list[float], hold: float = 1960) -> list[float]:
        """A high candidate standing at 2000, then one price per rollover day.

        Six H4 bars make a day. e50 for a 2000 anchor is 1947.5, and the ZigZag
        threshold is 80, so the hold and the tail stay above 1920 — otherwise
        the candidate confirms, the leg flips, and the zone under test is gone
        before price reaches its level.
        """
        prices = [1800, 1900, 2000]
        prices += [hold] * 21
        for price in tail:
            prices += [price] * 6
        return prices + [tail[-1]] * 6 if tail else prices

    def run(self, tail, hold: float = 1960):
        tracker = CrossingTracker(zones_for, pip_size=1.0, deviation_abs=80.0)
        fired = []
        for i, p in enumerate(self.series(tail, hold)):
            fired += tracker.push(h4(i, p))
        return tracker, fired

    def test_a_downward_crossing_from_a_high_is_a_short(self):
        """§15.4."""
        _, fired = self.run([1960, 1940])
        assert len(fired) == 1
        assert not fired[0].is_long
        assert fired[0].take_profit() == pytest.approx(fired[0].zone.mz100)

    def test_a_crossing_back_out_is_ignored(self):
        """§6: only crossings toward the Margin Zone are signals.

        Held below e50 so the first pair cannot be an inward crossing; the only
        crossing available is the one back out toward the extremum.
        """
        _, fired = self.run([1960], hold=1940)
        assert fired == []

    def test_sitting_exactly_on_the_level_is_neutral(self):
        _, fired = self.run([1947.5, 1940])
        assert fired == []

    def test_observations_under_different_versions_do_not_pair(self):
        """§15.3 — the safeguard the whole versioning exists for."""
        tracker = CrossingTracker(zones_for, pip_size=1.0, deviation_abs=80.0)
        prices = [1800, 1900, 2000] + [1960] * 21
        prices += [1960] * 6          # one observation under the 2000 anchor
        prices += [2100] * 12         # a strict extension: new anchor, new level
        fired = []
        for i, p in enumerate(prices):
            fired += tracker.push(h4(i, p))
        assert fired == []

    def test_the_stop_mirrors_the_target_about_the_fill(self):
        """§7 and §14.7."""
        _, fired = self.run([1960, 1940])
        signal = fired[0]
        for fill in (1940.0, 1935.0, 1944.0):
            assert abs(fill - signal.stop_for(fill)) == pytest.approx(
                abs(fill - signal.take_profit())
            )


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
    log.write_text("code,as_of,maintenance,initial,source,note\nTEST,2023-01-01,5000.0,,t,\n")
    return {"contract": "TEST", "contracts_dir": str(contracts),
            "margin_log": str(log), "deviation_pips": 80.0}


def short_setup() -> list[Bar]:
    """A high candidate at 2000, a downward e50 crossing, then price to MZ100.

    A 5,000 maintenance over a 100 pip value gives dFMZ 50 and dIMZ 55, so from
    2000: MZ0 1950, MZ100 1945, midpoint 1947.5, e50 1973.75. The ZigZag
    threshold is 80 points, comfortably wider than the 26.25 from anchor to
    e50 — otherwise the candidate confirms before price can reach the level.
    """
    prices = [1800, 1900, 2000] + [1980] * 21
    for price in (1980, 1970):               # 1973.75 crossed downward
        prices += [price] * 6
    prices += [1970] * 6
    prices += [1944] * 12                    # reach MZ100 at 1945
    return [h4(i, p) for i, p in enumerate(prices)]


def run(strategy, bars, gold, config):
    return Backtester(strategy=strategy, symbol="XAUUSD", timeframe="H4",
                      instrument=gold, execution=config).run(bars)


class TestStrategy:
    def test_it_is_registered(self):
        assert get_strategy("crossing50") is Crossing50Strategy

    def test_a_downward_crossing_sells(self, desk, gold, config):
        result = run(Crossing50Strategy(**desk), short_setup(), gold, config)
        assert len(result.trades) == 1
        assert result.trades[0].side is Side.SELL

    def test_the_entry_is_after_the_signal_never_at_it(self, desk, gold, config):
        """§6: the first executable price *after* the observation, not backfilled."""
        strategy = Crossing50Strategy(**desk)
        result = run(strategy, short_setup(), gold, config)
        signal = strategy.tracker.signals[0]
        assert result.trades[0].entry_time > signal.signal_time

    def test_the_bracket_is_symmetric_about_the_actual_fill(self, desk, gold, config):
        """§14.7, measured on the filled trade rather than on the signal."""
        result = run(Crossing50Strategy(**desk), short_setup(), gold, config)
        trade = result.trades[0]
        assert abs(trade.tp - trade.entry_price) == pytest.approx(
            abs(trade.entry_price - trade.sl), abs=1e-6
        )

    def test_an_unknown_variant_is_rejected(self, desk, gold, config):
        with pytest.raises(ValueError, match="variant must be one of"):
            run(Crossing50Strategy(**desk, variant="MAYBE"), short_setup(), gold, config)

    def test_detect_only_places_no_orders(self, desk, gold, config):
        strategy = Crossing50Strategy(**desk, trade=False)
        result = run(strategy, short_setup(), gold, config)
        assert result.trades == []
        assert len(strategy.tracker.signals) == 1

    def test_the_three_records_are_published(self, desk, gold, config):
        """§12."""
        strategy = Crossing50Strategy(**desk)
        result = run(strategy, short_setup(), gold, config)
        assert {"zones", "signals", "cases"} <= set(result.artifacts)
        case = result.artifacts["cases"][0]
        for field in ("origin_zone_id", "origin_candidate_leg_id", "initial_tp",
                      "initial_sl", "exit_reason", "r_multiple", "ambiguous_tp_sl",
                      "candidate_updates_while_open"):
            assert field in case

    def test_a_trade_keeps_its_originating_zone(self, desk, gold, config):
        """§14.5: origin_zone_id never changes, even as later versions appear."""
        strategy = Crossing50Strategy(**desk)
        result = run(strategy, short_setup(), gold, config)
        case = result.artifacts["cases"][0]
        zone_ids = {z["zone_id"] for z in result.artifacts["zones"]}
        assert case["origin_zone_id"] in zone_ids


class TestVariants:
    """§9, §10 — the single intended behavioural difference."""

    def adverse_setup(self) -> list[Bar]:
        """Enter short, then let the originating high extend against the trade."""
        prices = [1800, 1900, 2000] + [1980] * 21
        for price in (1980, 1972):        # crosses e50 at 1973.75 downward
            prices += [price] * 6
        prices += [1973] * 6              # the fill, at 1973 -> risk 28, stop 2001
        # A strictly higher high than 2000, but inside the stop at 2001. The
        # window is narrow by construction: the stop sits at
        # anchor + (dIMZ - dFMZ) / 2, barely above the anchor itself, so an
        # extension large enough to matter usually takes the stop out first.
        prices += [2000.5] * 18
        return [h4(i, p) for i, p in enumerate(prices)]

    def test_keep_open_ignores_the_extension(self, desk, gold, config):
        """§15.6."""
        strategy = Crossing50Strategy(**desk, variant=KEEP_OPEN)
        result = run(strategy, self.adverse_setup(), gold, config)
        (case,) = result.artifacts["cases"]
        assert case["exit_reason"] != "candidate_update"
        assert int(case["candidate_updates_while_open"]) >= 1

    def test_close_on_update_exits(self, desk, gold, config):
        """§15.7."""
        strategy = Crossing50Strategy(**desk, variant=CLOSE_ON_CANDIDATE_UPDATE)
        result = run(strategy, self.adverse_setup(), gold, config)
        (case,) = result.artifacts["cases"]
        assert case["exit_reason"] == "candidate_update"

    def test_the_forced_exit_is_after_the_update_not_at_it(self, desk, gold, config):
        """§10: the first executable price strictly after the update became known."""
        strategy = Crossing50Strategy(**desk, variant=CLOSE_ON_CANDIDATE_UPDATE)
        result = run(strategy, self.adverse_setup(), gold, config)
        (case,) = result.artifacts["cases"]
        assert case["exit_time"] > case["first_candidate_update_time"]

    def test_variant_a_never_exits_on_an_update(self, desk, gold, config):
        """§14.10."""
        strategy = Crossing50Strategy(**desk, variant=KEEP_OPEN)
        result = run(strategy, self.adverse_setup(), gold, config)
        assert all(c["exit_reason"] != "candidate_update"
                   for c in result.artifacts["cases"])
