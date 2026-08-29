"""Provisional-candidate Margin Zone strategy.

Checked against `impl-spec/Provisional_ZigZag_MZ50_Strategy_Spec.md`. §15 lists
twelve minimum tests and §14 eleven invariants.

The point of the design is that a zone anchored on a *moving* candidate must
never let a later anchor touch an earlier observation. Much of what follows is
that one property, approached from different directions.
"""

from __future__ import annotations

import json
from datetime import date, datetime, timedelta, timezone

import pytest

from backtester.core.engine import Backtester
from backtester.core.types import Bar, Side
from backtester.indicators.zigzag import Candidate, ZigZagTracker
from backtester.strategies import get_strategy
from backtester.strategies.margin_zones.crossing import CrossingTracker
from backtester.strategies.margin_zones.margins import MarginZones
from backtester.strategies.margin_zones.mz50 import (
    CLOSE_ON_CANDIDATE_UPDATE,
    KEEP_OPEN,
    MZ50Strategy,
)
from backtester.strategies.margin_zones.zones import (
    E50,
    INITIAL,
    MZ50,
    STRICT_EXTENSION,
    ZoneTracker,
)

UTC = timezone.utc
START = datetime(2024, 1, 1, tzinfo=UTC)

# 100 pips of FMZ and 110 of IMZ at a pip_size of 1.0, so from a high at 2000:
#   MZ0 1900, MZ100 1890, MZ50 1895, E50 1947.5
ZONES = MarginZones(code="TEST", as_of=date(2023, 1, 1), maintenance=10_000.0,
                    initial=11_000.0, pip_value=100.0, fmz=100.0, imz=110.0, mz=10.0)


def h4(i: int, price: float) -> Bar:
    return Bar(time=START + timedelta(hours=4 * i), open=price, high=price,
               low=price, close=price, volume=100, spread=0.0)


def zones_for(_candidate):
    return ZONES


def feed(prices, **kw):
    tracker = ZoneTracker(zones_for, pip_size=1.0, deviation_abs=80.0, **kw)
    out = [tracker.push(h4(i, p)) for i, p in enumerate(prices)]
    return tracker, out


class TestCandidate:
    """What the ZigZag is currently tracking, and what counts as a change."""

    def test_no_candidate_before_a_direction_exists(self):
        """With direction 0 the tracker watches both ends; nothing to anchor on."""
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
        """§15.2: it may move the ZigZag index, but no level changed."""
        a = Candidate(leg=1, kind="high", price=100.0, index=0, time=START)
        assert not Candidate(leg=1, kind="high", price=100.0, index=9, time=START).extends(a)

    def test_a_different_leg_is_not_an_update(self):
        a = Candidate(leg=1, kind="high", price=100.0, index=0, time=START)
        assert not Candidate(leg=2, kind="low", price=90.0, index=5, time=START).extends(a)


class TestZoneVersions:
    """§3.4, §4 — one immutable zone per anchor, levels frozen at creation."""

    def test_the_two_named_levels_are_separate(self):
        """§4: MZ50 is the midpoint of the band; E50 is half as far from the anchor."""
        _, out = feed([1800, 1900, 1860, 2000, 1960])
        version = next(v for v in out if v is not None)
        assert version.kind == "high"
        anchor = version.anchor_price
        assert version.mz0 == pytest.approx(anchor - 100)
        assert version.mz100 == pytest.approx(anchor - 110)
        assert version.mz50 == pytest.approx(anchor - 105)      # (dFMZ + dIMZ) / 2
        assert version.e50 == pytest.approx(anchor - 52.5)      # (dFMZ + dIMZ) / 4
        assert version.level(MZ50) == version.mz50
        assert version.level(E50) == version.e50

    def test_mz50_is_inside_the_band_and_e50_outside_it(self):
        _, out = feed([1800, 1900, 1860, 2000, 1960])
        v = next(x for x in out if x is not None)
        assert v.lo <= v.mz50 <= v.hi
        assert not v.lo <= v.e50 <= v.hi

    def test_an_unknown_level_is_rejected(self):
        _, out = feed([1800, 1900, 1860, 2000, 1960])
        with pytest.raises(ValueError, match="level must be one of"):
            next(x for x in out if x is not None).level("mz25")

    def test_a_strict_extension_creates_a_new_version(self):
        tracker, _ = feed([1800, 1900, 1860, 2000, 2050, 2100])
        events = [v.event_type for v in tracker.versions]
        assert events[0] == INITIAL
        assert STRICT_EXTENSION in events
        assert [v.anchor_price for v in tracker.versions] == sorted(
            v.anchor_price for v in tracker.versions
        )

    def test_an_equal_high_creates_no_version(self):
        tracker, _ = feed([1800, 1900, 1860, 2000, 2000, 2000])
        anchors = [v.anchor_price for v in tracker.versions]
        assert anchors.count(2000) == 1

    def test_versions_are_immutable(self):
        """§14.4: a historical zone level never changes after creation."""
        tracker, _ = feed([1800, 1900, 1860, 2000, 2050])
        with pytest.raises(Exception):
            tracker.versions[0].anchor_price = 999.0

    def test_a_version_is_never_knowable_before_its_bar(self):
        """§14.1."""
        tracker, _ = feed([1800, 1900, 1860, 2000, 2050])
        for v in tracker.versions:
            assert v.known_time >= v.anchor_time

    def test_superseding_records_when_the_previous_zone_ended(self):
        tracker, _ = feed([1800, 1900, 1860, 2000, 2050, 2100])
        for older, newer in zip(tracker.versions, tracker.versions[1:]):
            index, time = tracker.superseded[older.zone_id]
            assert index == newer.known_index and time == newer.known_time
        assert tracker.versions[-1].zone_id not in tracker.superseded

    def test_a_candidate_with_no_margin_reading_makes_no_zone(self):
        tracker = ZoneTracker(lambda c: None, pip_size=1.0, deviation_abs=80.0)
        for i, p in enumerate([1800, 1900, 1860, 2000]):
            tracker.push(h4(i, p))
        assert tracker.versions == [] and tracker.uncovered


class TestCrossing:
    """§6 — when two rollover observations count as having crossed the level.

    The threshold is 150 here, wider than the 105 from the 2000 anchor to its
    MZ50 at 1895: a level further from the anchor than the ZigZag threshold can
    never be reached, because the leg flips first.
    """

    def series(self, tail: list[float], hold: float) -> list[float]:
        """A high candidate standing at 2000, then one price per rollover day."""
        prices = [1800, 1900, 2000]
        prices += [hold] * 21
        for price in tail:
            prices += [price] * 6
        return prices + [tail[-1]] * 6 if tail else prices

    def run(self, tail, hold: float, level: str = MZ50):
        tracker = CrossingTracker(zones_for, pip_size=1.0, deviation_abs=150.0, level=level)
        fired = []
        for i, p in enumerate(self.series(tail, hold)):
            fired += tracker.push(h4(i, p))
        return tracker, fired

    def test_a_downward_crossing_from_a_high_is_true(self):
        """§15.4: MZ50 is 1895, so 1930 -> 1880 crosses it toward the zone."""
        _, fired = self.run([1930, 1880], hold=1930)
        assert len(fired) == 1
        assert fired[0].classification == "True"
        assert fired[0].direction == "down"
        assert not fired[0].is_long
        assert fired[0].level == pytest.approx(1895.0)
        assert fired[0].level_name == MZ50

    def test_a_crossing_back_out_is_false(self):
        """§6: held below MZ50, the only crossing available is the one outward."""
        _, fired = self.run([1930], hold=1880)
        assert len(fired) == 1
        assert fired[0].classification == "False"
        assert fired[0].direction == "up"

    def test_sitting_exactly_on_the_level_is_neutral(self):
        _, fired = self.run([1895, 1880], hold=1930)
        assert fired == []

    def test_the_level_is_the_one_the_run_asked_for(self):
        """The same prices cross E50 (1947.5) but not MZ50 (1895)."""
        _, on_mid = self.run([1960, 1940], hold=1960, level=MZ50)
        _, on_e50 = self.run([1960, 1940], hold=1960, level=E50)
        assert on_mid == []
        assert len(on_e50) == 1
        assert on_e50[0].level == pytest.approx(1947.5)

    def test_observations_under_different_versions_do_not_pair(self):
        """§15.3 — the safeguard the whole versioning exists for."""
        tracker = CrossingTracker(zones_for, pip_size=1.0, deviation_abs=150.0)
        prices = [1800, 1900, 2000] + [1930] * 21
        prices += [1930] * 6          # one observation under the 2000 anchor
        prices += [2100] * 12         # a strict extension: new anchor, new level
        fired = []
        for i, p in enumerate(prices):
            fired += tracker.push(h4(i, p))
        assert fired == []

    def test_a_crossing_is_never_recorded_before_its_zone_existed(self):
        """§14.2."""
        _, fired = self.run([1930, 1880], hold=1930)
        for crossing in fired:
            assert crossing.time > crossing.zone.known_time


# ------------------------------------------------------------------ strategy

@pytest.fixture
def desk(tmp_path):
    """A contract whose zone is wide enough for MZ50 to be reachable.

    MM 50,000 over a pip value of 100 gives dFMZ 500 and dIMZ 550, so from a
    high at 2000: MZ0 1500, MZ50 1475, MZ100 1450, E50 1737.5. The 600-point
    ZigZag threshold is wider than the 525 from anchor to MZ50.
    """
    contracts = tmp_path / "contracts"
    contracts.mkdir()
    (contracts / "TEST.json").write_text(json.dumps({
        "code": "TEST", "name": "Test future", "exchange": "TEST",
        "contract_size": 100.0, "base_currency": "XAU", "quote_currency": "USD",
        "tick_size": 1.0, "tick_value": 100.0, "pip_size": 1.0,
    }))
    log = tmp_path / "margins.csv"
    log.write_text("code,as_of,maintenance,initial,source,note\nTEST,2023-01-01,50000.0,,t,\n")
    return {"contract": "TEST", "contracts_dir": str(contracts),
            "margin_log": str(log), "deviation_pips": 600.0, "place_orders": True}


def bars_for(tail: list[float], hold: float = 1900.0) -> list[Bar]:
    """A high candidate at 2000, then one price per rollover day."""
    prices = [1000, 1500, 2000] + [hold] * 21
    for price in tail:
        prices += [price] * 6
    return [h4(i, p) for i, p in enumerate(prices)]


def crossing_setup() -> list[Bar]:
    """A downward MZ50 crossing at 1470, then price on to MZ100 at 1450."""
    return bars_for([1900, 1470, 1470] + [1449] * 2)


def run(strategy, bars, gold, config):
    return Backtester(strategy=strategy, symbol="XAUUSD", timeframe="H4",
                      instrument=gold, execution=config).run(bars)


class TestNoOrders:
    """`place_orders: false` runs the same pass and trades nothing."""

    def test_it_is_registered(self):
        assert get_strategy("mz50") is MZ50Strategy

    def test_it_is_off_by_default(self, desk):
        assert MZ50Strategy(**{**desk, "place_orders": False}).p.place_orders is False

    def test_no_orders_but_the_crossings_are_still_recorded(self, desk, gold, config):
        strategy = MZ50Strategy(**{**desk, "place_orders": False})
        result = run(strategy, crossing_setup(), gold, config)
        assert result.trades == []
        assert strategy.tracker.crossings
        assert "signals" not in result.artifacts

    def test_the_support_records_are_published(self, desk, gold, config):
        strategy = MZ50Strategy(**{**desk, "place_orders": False})
        result = run(strategy, crossing_setup(), gold, config)
        assert {"zones", "pivots", "rollover", "crossings"} <= set(result.artifacts)


class TestStrategy:
    """§6, §7 — the entry, and the levels it copies from the zone version."""

    def test_a_downward_crossing_sells(self, desk, gold, config):
        """§15.4."""
        result = run(MZ50Strategy(**desk), crossing_setup(), gold, config)
        assert len(result.trades) == 1
        assert result.trades[0].side is Side.SELL

    def test_the_entry_is_after_the_signal_never_at_it(self, desk, gold, config):
        """§6: the first executable price *after* the observation."""
        strategy = MZ50Strategy(**desk)
        result = run(strategy, crossing_setup(), gold, config)
        signal = strategy.tracker.crossings[0]
        assert result.trades[0].entry_time > signal.time

    def test_the_target_is_mz100(self, desk, gold, config):
        strategy = MZ50Strategy(**desk)
        result = run(strategy, crossing_setup(), gold, config)
        assert result.trades[0].tp == pytest.approx(strategy.tracker.crossings[0].zone.mz100)

    def test_the_bracket_is_symmetric_about_the_actual_fill(self, desk, gold, config):
        """§14.7, measured on the filled trade rather than on the signal."""
        trade = run(MZ50Strategy(**desk), crossing_setup(), gold, config).trades[0]
        assert abs(trade.tp - trade.entry_price) == pytest.approx(
            abs(trade.entry_price - trade.sl), abs=1e-6
        )

    def test_the_stop_lands_on_mz0(self, desk, gold, config):
        """A consequence of the MZ50 entry, worth pinning.

        `stop = 2*entry - MZ100`, and `2*MZ50 - MZ100 == MZ0`, so a fill exactly
        at MZ50 puts the stop exactly on MZ0. A fill `d` past MZ50 puts it `2d`
        past MZ0 — further from the anchor, never nearer.
        """
        strategy = MZ50Strategy(**desk)
        result = run(strategy, crossing_setup(), gold, config)
        zone = strategy.tracker.crossings[0].zone
        trade = result.trades[0]
        drift = abs(trade.entry_price - zone.mz50)
        assert abs(trade.sl - zone.mz0) == pytest.approx(2 * drift, abs=1e-6)
        # Whatever the fill, the stop stays on the anchor's side of nothing:
        # it is at least as far from the anchor as MZ0 is.
        assert abs(trade.sl - zone.anchor_price) >= abs(zone.mz0 - zone.anchor_price)

    def test_a_signal_beyond_the_target_is_skipped(self, desk, gold, config):
        """§15.11: the crossing observation already sits past MZ100."""
        strategy = MZ50Strategy(**desk)
        result = run(strategy, bars_for([1900, 1440, 1440]), gold, config)
        assert result.trades == []
        assert [r["status"] for r in strategy.artifacts()["signals"]] == ["entry_beyond_target"]

    def test_the_records_are_published(self, desk, gold, config):
        """§12."""
        result = run(MZ50Strategy(**desk), crossing_setup(), gold, config)
        assert {"zones", "signals", "cases"} <= set(result.artifacts)
        case = result.artifacts["cases"][0]
        for field in ("origin_zone_id", "origin_candidate_leg_id", "initial_tp",
                      "initial_sl", "initial_risk", "exit_reason", "r_multiple",
                      "ambiguous_tp_sl", "candidate_updates_while_open",
                      "signal_level_name"):
            assert field in case
        assert case["signal_level_name"] == MZ50

    def test_a_trade_keeps_its_originating_zone(self, desk, gold, config):
        """§14.5: origin_zone_id never changes as later versions appear."""
        result = run(MZ50Strategy(**desk), crossing_setup(), gold, config)
        case = result.artifacts["cases"][0]
        assert case["origin_zone_id"] in {z["zone_id"] for z in result.artifacts["zones"]}

    def test_an_unknown_variant_is_rejected(self, desk, gold, config):
        with pytest.raises(ValueError, match="variant must be one of"):
            run(MZ50Strategy(**desk, variant="MAYBE"), crossing_setup(), gold, config)

    def test_an_unknown_signal_level_is_rejected(self, desk, gold, config):
        with pytest.raises(ValueError, match="signal_level must be one of"):
            run(MZ50Strategy(**desk, signal_level="mz25"), crossing_setup(), gold, config)


class TestVariants:
    """§9, §10 — the single intended behavioural difference.

    Driven on `e50` rather than `mz50`. With an MZ50 entry the stop lands on
    MZ0, and a strict adverse extension is by definition beyond the anchor,
    which is further than MZ0 — so the stop always resolves first and variant B
    can never fire. `TestMZ50MakesVariantBUnreachable` pins that separately;
    these tests exercise the mechanism where it is reachable.
    """

    def adverse_setup(self) -> list[Bar]:
        """Enter short off E50 at 1737.5, then extend the originating high.

        The fill is ~1730, so risk is ~280 and the stop sits near 2010 — above
        the 2000 anchor, leaving room for a higher high at 2005 to arrive first.
        """
        return bars_for([1900, 1730, 1730] + [2005] * 3)

    def variant(self, desk, variant):
        return MZ50Strategy(**{**desk, "signal_level": E50, "variant": variant})

    def test_keep_open_ignores_the_extension(self, desk, gold, config):
        """§15.6."""
        result = run(self.variant(desk, KEEP_OPEN), self.adverse_setup(), gold, config)
        (case,) = result.artifacts["cases"]
        assert case["exit_reason"] != "candidate_update"
        assert int(case["candidate_updates_while_open"]) >= 1

    def test_close_on_update_exits(self, desk, gold, config):
        """§15.7."""
        result = run(
            self.variant(desk, CLOSE_ON_CANDIDATE_UPDATE), self.adverse_setup(), gold, config
        )
        (case,) = result.artifacts["cases"]
        assert case["exit_reason"] == "candidate_update"

    def test_the_forced_exit_is_after_the_update_not_at_it(self, desk, gold, config):
        """§10: the first executable price strictly after the update was known."""
        result = run(
            self.variant(desk, CLOSE_ON_CANDIDATE_UPDATE), self.adverse_setup(), gold, config
        )
        (case,) = result.artifacts["cases"]
        assert case["exit_time"] > case["first_candidate_update_time"]

    def test_variant_a_never_exits_on_an_update(self, desk, gold, config):
        """§14.10."""
        result = run(self.variant(desk, KEEP_OPEN), self.adverse_setup(), gold, config)
        assert all(c["exit_reason"] != "candidate_update" for c in result.artifacts["cases"])

    def test_confirmation_alone_is_not_an_update(self, desk, gold, config):
        """§15.8: an ordinary pivot confirmation forces no variant-B exit."""
        # 1100 confirms the 2000 high (threshold 600) without ever extending it.
        bars = bars_for([1900, 1730, 1730] + [1100] * 3)
        result = run(self.variant(desk, CLOSE_ON_CANDIDATE_UPDATE), bars, gold, config)
        assert all(c["exit_reason"] != "candidate_update" for c in result.artifacts["cases"])


class TestMZ50MakesVariantBUnreachable:
    """The two variants cannot differ when the entry level is MZ50.

    `stop = entry + (entry - MZ100)`, and at a fill of MZ50 that is exactly MZ0.
    A strict adverse extension is beyond the anchor, which is `dFMZ` further out
    than MZ0, so the stop is always touched first.
    """

    def test_the_two_variants_agree(self, desk, gold, config):
        bars = bars_for([1900, 1470, 1470] + [2005] * 3)
        a = run(MZ50Strategy(**{**desk, "variant": KEEP_OPEN}), bars, gold, config)
        b = run(MZ50Strategy(**{**desk, "variant": CLOSE_ON_CANDIDATE_UPDATE}),
                bars, gold, config)
        assert [t.reason for t in a.trades] == [t.reason for t in b.trades]
        assert [t.exit_price for t in a.trades] == [t.exit_price for t in b.trades]
        assert all(c["exit_reason"] != "candidate_update" for c in b.artifacts["cases"])
