"""Provisional-candidate Margin Zone strategy.

Checked against `impl-spec-old/Provisional_ZigZag_MZ50_Strategy_Spec.md`. §15 lists
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
    FAR,
    KEEP_OPEN,
    NEAR,
    MZ50Strategy,
)
from backtester.strategies.margin_zones.zones import INITIAL, STRICT_EXTENSION, ZoneTracker

UTC = timezone.utc
START = datetime(2024, 1, 1, tzinfo=UTC)

# 100 pips of FMZ and 110 of IMZ at a pip_size of 1.0, so from a high at 2000:
#   MZ0 1900, MZ100 1890, MZ50 1895, E50 1947.5 — E50 is the signal level.
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

    def test_mz50_is_inside_the_band_and_e50_outside_it(self):
        _, out = feed([1800, 1900, 1860, 2000, 1960])
        v = next(x for x in out if x is not None)
        assert v.lo <= v.mz50 <= v.hi
        assert not v.lo <= v.e50 <= v.hi

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
    """§6 — when two rollover observations count as having crossed E50.

    The threshold is 150 here, wider than the 52.5 from the 2000 anchor to its
    E50 at 1947.5: a level further from the anchor than the ZigZag threshold can
    never be reached, because the leg flips first.
    """

    def series(self, tail: list[float], hold: float) -> list[float]:
        """A high candidate standing at 2000, then one price per rollover day."""
        prices = [1800, 1900, 2000]
        prices += [hold] * 21
        for price in tail:
            prices += [price] * 6
        return prices + [tail[-1]] * 6 if tail else prices

    def run(self, tail, hold: float):
        tracker = CrossingTracker(zones_for, pip_size=1.0, deviation_abs=150.0)
        fired = []
        for i, p in enumerate(self.series(tail, hold)):
            fired += tracker.push(h4(i, p))
        return tracker, fired

    def test_a_downward_crossing_from_a_high_is_true(self):
        """§15.4: E50 is 1947.5, so 1960 -> 1940 crosses it toward the zone."""
        _, fired = self.run([1960, 1940], hold=1960)
        assert len(fired) == 1
        assert fired[0].classification == "True"
        assert fired[0].direction == "down"
        assert not fired[0].is_long
        assert fired[0].level == pytest.approx(1947.5)

    def test_a_crossing_back_out_is_false(self):
        """§6: held below E50, the only crossing available is the one outward."""
        _, fired = self.run([1960], hold=1940)
        assert len(fired) == 1
        assert fired[0].classification == "False"
        assert fired[0].direction == "up"

    def test_sitting_exactly_on_the_level_is_neutral(self):
        _, fired = self.run([1947.5, 1940], hold=1960)
        assert fired == []

    def test_the_level_is_e50_and_not_the_bands_midpoint(self):
        """A move to 1940 crosses E50 (1947.5); MZ50 (1895) is untouched."""
        _, fired = self.run([1960, 1940], hold=1960)
        (crossing,) = fired
        assert crossing.level == pytest.approx(crossing.zone.e50)
        assert crossing.level != pytest.approx(crossing.zone.mz50)

    def test_observations_under_different_versions_do_not_pair(self):
        """§15.3 — the safeguard the whole versioning exists for."""
        tracker = CrossingTracker(zones_for, pip_size=1.0, deviation_abs=150.0)
        prices = [1800, 1900, 2000] + [1960] * 21
        prices += [1960] * 6          # one observation under the 2000 anchor
        prices += [2100] * 12         # a strict extension: new anchor, new level
        fired = []
        for i, p in enumerate(prices):
            fired += tracker.push(h4(i, p))
        assert fired == []

    def test_a_reset_baseline_stops_the_next_point_pairing_backwards(self):
        """§9/§15.10: after an exit, a fresh pair is required."""
        series = self.series([1960, 1940], hold=1960)
        # Bar 30 files the 1960 observation and bar 36 the 1940 one; a reset
        # between them is what an exit does, and the pair must not survive it.
        assert self.run([1960, 1940], hold=1960)[1], "the pair forms without a reset"

        tracker = CrossingTracker(zones_for, pip_size=1.0, deviation_abs=150.0)
        fired = []
        for i, price in enumerate(series):
            if i == 33:
                tracker.reset_baseline()
            fired += tracker.push(h4(i, price))
        assert fired == []

    def test_a_crossing_is_never_recorded_before_its_zone_existed(self):
        """§14.2."""
        _, fired = self.run([1960, 1940], hold=1960)
        for crossing in fired:
            assert crossing.time > crossing.zone.known_time


# ------------------------------------------------------------------ strategy

@pytest.fixture
def desk(tmp_path):
    """A contract whose zone leaves room between E50 and the ZigZag threshold.

    MM 50,000 over a pip value of 100 gives dFMZ 500 and dIMZ 550, so from a
    high at 2000: MZ0 1500, MZ50 1475, MZ100 1450, E50 1737.5. The 600-point
    ZigZag threshold is wider than the 262.5 from anchor to E50, so price can
    reach the signal level before the leg flips.
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
    """A downward E50 crossing at 1730, then price on to MZ100 at 1450."""
    return bars_for([1900, 1730, 1730] + [1449] * 2)


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

    def test_the_target_is_the_near_boundary(self, desk, gold, config):
        """§6: TP is MZ0, not the far boundary."""
        strategy = MZ50Strategy(**desk)
        result = run(strategy, crossing_setup(), gold, config)
        zone = strategy.tracker.crossings[0].zone
        assert result.trades[0].tp == pytest.approx(zone.mz0)
        assert result.trades[0].tp != pytest.approx(zone.mz100)

    def test_run_a_aims_at_the_far_boundary_instead(self, desk, gold, config):
        """§12 run A keeps the old target."""
        strategy = MZ50Strategy(**{**desk, "take_profit": FAR})
        result = run(strategy, crossing_setup(), gold, config)
        assert result.trades[0].tp == pytest.approx(strategy.tracker.crossings[0].zone.mz100)

    def test_the_stop_keeps_the_distance_to_the_far_boundary(self, desk, gold, config):
        """§6: `SL = 2 * entry - origin_MZ100`, whatever the target is."""
        strategy = MZ50Strategy(**desk)
        trade = run(strategy, crossing_setup(), gold, config).trades[0]
        zone = strategy.tracker.crossings[0].zone
        assert trade.sl == pytest.approx(2 * trade.entry_price - zone.mz100, abs=1e-6)

    def test_moving_the_target_does_not_move_the_stop(self, desk, gold, config):
        """§13.2: the near target changes TP alone."""
        near = run(MZ50Strategy(**desk), crossing_setup(), gold, config).trades[0]
        far = run(MZ50Strategy(**{**desk, "take_profit": FAR}),
                  crossing_setup(), gold, config).trades[0]
        assert near.entry_price == far.entry_price
        assert near.sl == far.sl
        assert near.tp != far.tp

    def test_the_planned_reward_to_risk_is_below_one(self, desk, gold, config):
        """§6: intended, and not to be restored by moving the stop."""
        trade = run(MZ50Strategy(**desk), crossing_setup(), gold, config).trades[0]
        assert 0 < trade.planned_rr < 1

    def test_the_stop_sits_just_past_the_anchor(self, desk, gold, config):
        """`stop = 2*entry - MZ100`, and at a fill of E50 that is
        `anchor + (dIMZ - dFMZ) / 2` — barely beyond the anchor itself."""
        strategy = MZ50Strategy(**desk)
        result = run(strategy, crossing_setup(), gold, config)
        zone = strategy.tracker.crossings[0].zone
        trade = result.trades[0]
        drift = abs(trade.entry_price - zone.e50)
        expected = zone.anchor_price + (zone.d_imz - zone.d_fmz) / 2
        assert abs(trade.sl - expected) == pytest.approx(2 * drift, abs=1e-6)

    def test_a_signal_beyond_the_target_is_skipped(self, desk, gold, config):
        """§15.11: the crossing observation already sits past MZ100."""
        strategy = MZ50Strategy(**desk)
        result = run(strategy, bars_for([1900, 1449, 1449]), gold, config)
        assert result.trades == []
        assert [r["status"] for r in strategy.artifacts()["signals"]] == ["entry_beyond_target"]

    def test_the_records_are_published(self, desk, gold, config):
        """§12."""
        result = run(MZ50Strategy(**desk), crossing_setup(), gold, config)
        assert {"zones", "signals", "cases"} <= set(result.artifacts)
        case = result.artifacts["cases"][0]
        for field in ("origin_zone_id", "origin_candidate_leg_id", "initial_tp",
                      "initial_sl", "initial_risk", "exit_reason", "r_multiple",
                      "ambiguous_tp_sl", "candidate_updates_while_open", "e50",
                      "initial_reward", "initial_rr", "origin_mz0", "origin_mz50",
                      "origin_mz100", "warning_count", "warning_reset_count",
                      "confirming_close_time", "pending_exit_at_end",
                      "execution_mode"):
            assert field in case

    def test_a_trade_keeps_its_originating_zone(self, desk, gold, config):
        """§14.5: origin_zone_id never changes as later versions appear."""
        result = run(MZ50Strategy(**desk), crossing_setup(), gold, config)
        case = result.artifacts["cases"][0]
        assert case["origin_zone_id"] in {z["zone_id"] for z in result.artifacts["zones"]}

    def test_an_unknown_variant_is_rejected(self, desk, gold, config):
        with pytest.raises(ValueError, match="variant must be one of"):
            run(MZ50Strategy(**desk, variant="MAYBE"), crossing_setup(), gold, config)



class TestTwoCloseExit:
    """§7 — leaving on two consecutive daily closes back past the trade's E50.

    The trade enters short at ~1730 off an E50 of 1737.5, so a daily close above
    1737.5 is adverse and one below it is on the zone's side. 1800 stays well
    under the 2000 anchor, so it warns without extending the candidate.
    """

    def case(self, desk, gold, config, tail, **params):
        strategy = MZ50Strategy(**{**desk, **params})
        result = run(strategy, bars_for([1900, 1730, 1730] + tail), gold, config)
        return strategy, result

    def test_two_adverse_closes_confirm_the_exit(self, desk, gold, config):
        """§15.6."""
        strategy, result = self.case(desk, gold, config, [1800, 1800, 1800])
        (case,) = result.artifacts["cases"]
        assert case["exit_reason"] == "e50_two_close_return"
        assert case["warning_count"] == 1
        assert case["confirming_close_time"] > case["confirmed_warning_time"]

    def test_the_exit_is_after_the_confirming_close_never_at_it(self, desk, gold, config):
        """§7: the first executable price once the second close is known."""
        _, result = self.case(desk, gold, config, [1800, 1800, 1800])
        (case,) = result.artifacts["cases"]
        assert case["exit_time"] > case["confirming_close_time"]
        assert case["execution_mode"] == "next_open"

    def test_one_adverse_close_only_warns(self, desk, gold, config):
        """§15.6: the first is a warning, nothing more."""
        _, result = self.case(desk, gold, config, [1800, 1700, 1700])
        (case,) = result.artifacts["cases"]
        assert case["exit_reason"] != "e50_two_close_return"
        assert case["warning_count"] == 1 and case["warning_reset_count"] == 1

    def test_a_close_on_the_zone_side_clears_the_warning(self, desk, gold, config):
        """§15.7."""
        strategy, result = self.case(desk, gold, config, [1800, 1700, 1800, 1700])
        (case,) = result.artifacts["cases"]
        assert case["exit_reason"] != "e50_two_close_return"
        events = [w["event"] for w in strategy.artifacts()["warnings"]]
        assert events.count("warning") == 2
        assert "reset_zone_side" in events

    def test_a_close_exactly_on_e50_clears_the_warning(self, desk, gold, config):
        """§15.7: equality is neutral, and it resets."""
        strategy, result = self.case(desk, gold, config, [1800, 1737.5, 1800, 1700])
        (case,) = result.artifacts["cases"]
        assert case["exit_reason"] != "e50_two_close_return"
        assert "reset_on_level" in [w["event"] for w in strategy.artifacts()["warnings"]]

    def test_the_warnings_come_only_after_the_fill(self, desk, gold, config):
        """§7: the signal's own close cannot be the first warning."""
        strategy, result = self.case(desk, gold, config, [1800, 1800, 1800])
        (case,) = result.artifacts["cases"]
        for warning in strategy.artifacts()["warnings"]:
            assert warning["close_time"] > case["entry_time"]

    def test_switching_it_off_leaves_the_trade_alone(self, desk, gold, config):
        """§12 runs A and B carry no early exit."""
        _, result = self.case(desk, gold, config, [1800, 1800, 1800], two_close_exit=False)
        (case,) = result.artifacts["cases"]
        assert case["exit_reason"] != "e50_two_close_return"
        assert case["warning_count"] == 0

    def test_the_target_closes_the_trade_before_any_warning(self, desk, gold, config):
        """§15.11: TP reached first wins, and no second exit is created."""
        _, result = self.case(desk, gold, config, [1499, 1800, 1800, 1800])
        (case,) = result.artifacts["cases"]
        assert case["exit_reason"] == "take_profit"
        assert case["warning_count"] == 0
        assert len(result.trades) >= 1

    def test_the_warning_log_is_published(self, desk, gold, config):
        """§11: every warning, reset and confirmation, not just the last pair."""
        strategy, _ = self.case(desk, gold, config, [1800, 1700, 1800, 1800])
        rows = strategy.artifacts()["warnings"]
        assert rows and {"event", "session_day", "close_price", "origin_e50"} <= set(rows[0])


class TestVariants:
    """§9, §10 — the single intended behavioural difference."""

    def adverse_setup(self) -> list[Bar]:
        """Enter short off E50 at 1737.5, then extend the originating high.

        The fill is ~1730, so risk is ~280 and the stop sits near 2010 — above
        the 2000 anchor, leaving room for a higher high at 2005 to arrive first.
        """
        return bars_for([1900, 1730, 1730] + [2005] * 3)

    def variant(self, desk, variant):
        return MZ50Strategy(**{**desk, "variant": variant})

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


class TestChart:
    """The run draws the margin-zone chart from its own forward pass."""

    def payload(self, desk, gold, config, tmp_path) -> dict:
        import re

        from backtester.data.results import save_result
        from backtester.metrics import compute

        strategy = MZ50Strategy(**desk)
        result = run(strategy, crossing_setup(), gold, config)
        directory = save_result(result, compute(result).to_dict(), root=tmp_path)
        assert strategy.chart(directory, "parquet://data/bars", "H4") is not None
        page = (directory / "chart.html").read_text(encoding="utf-8")
        return json.loads(re.search(r'(\{"symbol".*?\})\s*;', page, re.S).group(1))

    def test_the_layers_are_present(self, desk, gold, config, tmp_path):
        payload = self.payload(desk, gold, config, tmp_path)
        assert payload["pivots"], "the ZigZag went missing"
        assert payload["zones"], "the margin zones went missing"
        assert payload["rollover"], "the rollover observations went missing"
        assert payload["crossings"], "the crossings went missing"
        assert payload["trades"], "the orders went missing"

    def test_the_title_names_everything_a_run_can_differ_by(
        self, desk, gold, config, tmp_path
    ):
        """The §12 comparison runs must not share a heading."""
        titles = {
            "A": self.payload({**desk, "take_profit": FAR, "two_close_exit": False},
                              gold, config, tmp_path)["strategy"],
            "B": self.payload({**desk, "two_close_exit": False},
                              gold, config, tmp_path)["strategy"],
            "C": self.payload(desk, gold, config, tmp_path)["strategy"],
            "variant": self.payload({**desk, "variant": CLOSE_ON_CANDIDATE_UPDATE},
                                    gold, config, tmp_path)["strategy"],
        }
        assert titles["A"] == "mz50 [TP mz100, KEEP_OPEN]"
        assert titles["B"] == "mz50 [TP mz0, KEEP_OPEN]"
        assert titles["C"] == "mz50 [TP mz0, two-close exit, KEEP_OPEN]"
        assert len(set(titles.values())) == len(titles)

    def test_a_drawing_run_says_it_placed_no_orders(self, desk, gold, config, tmp_path):
        payload = self.payload({**desk, "place_orders": False}, gold, config, tmp_path)
        assert payload["strategy"] == "mz50 [no orders]"

    def test_the_crossings_carry_e50_not_the_bands_midpoint(
        self, desk, gold, config, tmp_path
    ):
        payload = self.payload(desk, gold, config, tmp_path)
        assert payload["crossings"]
        for crossing in payload["crossings"]:
            zone = next(z for z in payload["zones"] if z["id"] == crossing["z"])
            assert crossing["e50"] == pytest.approx(zone["e50"])
            assert crossing["e50"] != pytest.approx(zone["mid"])

    def test_a_zone_spans_from_when_it_became_knowable(self, desk, gold, config, tmp_path):
        """i0 is the bar that created the version, never the bar holding the anchor."""
        payload = self.payload(desk, gold, config, tmp_path)
        for zone in payload["zones"]:
            assert zone["i0"] >= zone["ai"]
            assert zone["i1"] >= zone["i0"]

    def test_the_candidate_still_in_progress_is_drawn(self, desk, gold, config, tmp_path):
        payload = self.payload(desk, gold, config, tmp_path)
        assert payload["cand"] is not None
        assert payload["cand"]["confirmAt"] is not None


class TestCandidateFan(TestChart):
    """Each candidate is joined back to the pivot that opened its leg."""

    def test_every_zone_names_its_opening_pivot(self, desk, gold, config, tmp_path):
        payload = self.payload(desk, gold, config, tmp_path)
        drawn = [z for z in payload["zones"] if z["pi"] is not None]
        assert drawn, "no candidate had a pivot to join back to"
        for zone in drawn:
            assert 0 <= zone["pi"] < len(payload["pivots"])

    def test_the_opening_pivot_is_the_opposite_kind(self, desk, gold, config, tmp_path):
        """Confirming a high starts a low-candidate leg, and the reverse."""
        payload = self.payload(desk, gold, config, tmp_path)
        for zone in payload["zones"]:
            if zone["pi"] is None:
                continue
            assert payload["pivots"][zone["pi"]]["kind"] != zone["kind"]

    def test_the_line_never_predates_its_pivots_confirmation(
        self, desk, gold, config, tmp_path
    ):
        """The fan is drawable at the anchor's own bar — it reads no future."""
        payload = self.payload(desk, gold, config, tmp_path)
        for zone in payload["zones"]:
            if zone["pi"] is None:
                continue
            assert payload["pivots"][zone["pi"]]["ci"] <= zone["i0"]


@pytest.mark.skipif(__import__("shutil").which("node") is None, reason="needs node")
class TestChartRenders:
    """The page's script runs to completion under a stub DOM.

    A throw anywhere in it leaves the page blank — no chart, no tables, and no
    error visible without opening a console — so it is checked headlessly.
    """

    def render(self, desk, gold, config, tmp_path):
        import subprocess

        from backtester.data.results import save_result
        from backtester.metrics import compute

        strategy = MZ50Strategy(**desk)
        result = run(strategy, crossing_setup(), gold, config)
        directory = save_result(result, compute(result).to_dict(), root=tmp_path)
        page = strategy.chart(directory, "parquet://data/bars", "H4")
        return subprocess.run(
            ["node", "tests/render_check.js", str(page)],
            capture_output=True, text=True,
        )

    def test_the_page_runs_and_draws(self, desk, gold, config, tmp_path):
        out = self.render(desk, gold, config, tmp_path)
        assert out.returncode == 0, out.stdout + out.stderr
        assert "OK: script ran to completion" in out.stdout
        assert "zone rows   : 0" not in out.stdout
        assert "pivot rows  : 0" not in out.stdout
        assert "order elements drawn  : 0" not in out.stdout

    def test_hovering_fills_the_docked_readout(self, desk, gold, config, tmp_path):
        """The hover detail lives in its own block, not in a floating tooltip."""
        out = self.render(desk, gold, config, tmp_path)
        assert "hover fired: yes" in out.stdout
        assert "readout blocks: 0" not in out.stdout

    def test_the_title_says_what_the_run_was(self, desk, gold, config, tmp_path):
        out = self.render(desk, gold, config, tmp_path)
        title = next(
            line.split(":", 1)[1].strip()
            for line in out.stdout.splitlines() if line.strip().startswith("title")
        )
        assert title.startswith("XAUUSD H4 — mz50 [TP mz0, two-close exit, KEEP_OPEN],")
        assert "zone versions" in title and "orders" in title



class TestChain:
    """§14 — continuing from a take-profit, anchored on that trade's own E50.

    The parent is a SHORT off the 2000 anchor: E50 1737.5, MZ0 1500, MZ100 1450.
    Its child therefore anchors at 1737.5 with the same inherited distances:
    MZ0 1237.5, MZ50 1212.5, MZ100 1187.5, E50 1475.
    """

    PARENT = [1900, 1730, 1730]

    def chain(self, desk, gold, config, tail, extra=(), **params):
        strategy = MZ50Strategy(**{**desk, "trend_follow": True, **params})
        bars = bars_for(self.PARENT + tail) + list(extra)
        result = run(strategy, bars, gold, config)
        return strategy, result

    def wicks(self, after: int, price: float, high: float, count: int = 12) -> list[Bar]:
        """Bars that open away from `high` but reach it, so a limit fills at its
        own level rather than at a better open."""
        return [
            Bar(time=START + timedelta(hours=4 * (after + k)), open=price, high=high,
                low=price, close=price, volume=100, spread=0.0)
            for k in range(count)
        ]

    def events(self, strategy):
        return [r["event"] for r in strategy.artifacts().get("chain", [])]

    # ------------------------------------------------------------- creation

    def test_a_take_profit_creates_one_child_from_the_parents_e50(
        self, desk, gold, config
    ):
        """§14.7.1: the anchor is origin_E50, not the TP or the exit price."""
        strategy, result = self.chain(desk, gold, config, [1499, 1499])
        (row,) = [r for r in strategy.artifacts()["chain"] if r["event"].startswith("created")]
        parent = result.artifacts["cases"][0]
        assert parent["exit_reason"] == "take_profit"
        assert row["anchor_price"] == pytest.approx(float(parent["e50"]))
        assert row["anchor_price"] != pytest.approx(float(parent["exit_price"]))
        assert row["chain_depth"] == 1

    def test_the_child_inherits_direction_and_distances(self, desk, gold, config):
        """§14.7.2: levels recomputed from the new anchor, distances frozen."""
        strategy, result = self.chain(desk, gold, config, [1499, 1499])
        (row,) = [r for r in strategy.artifacts()["chain"] if r["event"].startswith("created")]
        parent = result.artifacts["cases"][0]
        d_fmz = float(parent["origin_anchor_price"]) - float(parent["origin_mz0"])
        d_imz = float(parent["origin_anchor_price"]) - float(parent["origin_mz100"])
        assert row["direction"] == parent["direction"]
        assert row["anchor_price"] - row["mz0"] == pytest.approx(d_fmz)
        assert row["anchor_price"] - row["mz100"] == pytest.approx(d_imz)
        mz50 = (row["mz0"] + row["mz100"]) / 2
        assert row["e50"] == pytest.approx((row["anchor_price"] + mz50) / 2)

    def test_nothing_is_created_without_the_flag(self, desk, gold, config):
        strategy, result = self.chain(desk, gold, config, [1499, 1499], trend_follow=False)
        assert "chain" not in strategy.artifacts()
        assert all(c["zone_source"] == "zigzag" for c in result.artifacts["cases"])

    def test_a_stop_loss_starts_no_chain(self, desk, gold, config):
        """§14.7.4: only a take-profit continues."""
        strategy, result = self.chain(desk, gold, config, [2100, 2100])
        assert result.artifacts["cases"][0]["exit_reason"] != "take_profit"
        assert not [e for e in self.events(strategy) if e.startswith("created")]

    def test_an_early_exit_starts_no_chain(self, desk, gold, config):
        """§14.7.4: a profitable two-close exit is still not a take-profit."""
        strategy, result = self.chain(desk, gold, config, [1800, 1800, 1800])
        assert result.artifacts["cases"][0]["exit_reason"] == "e50_two_close_return"
        assert not [e for e in self.events(strategy) if e.startswith("created")]

    # ---------------------------------------------------------- entry modes

    def test_price_on_the_anchor_side_waits_for_a_daily_cross(self, desk, gold, config):
        """§14.2 row 2: 1499 is above the child's E50 of 1475."""
        strategy, _ = self.chain(desk, gold, config, [1499, 1499])
        assert "created_chain_daily_cross" in self.events(strategy)

    def test_price_inside_the_band_rests_a_limit_at_e50(self, desk, gold, config):
        """§14.2 row 1: 1400 sits between the child's MZ0 and its E50."""
        strategy, _ = self.chain(desk, gold, config, [1400, 1400])
        assert "created_chain_limit_retest" in self.events(strategy)

    def test_price_already_at_the_child_target_ends_the_branch(self, desk, gold, config):
        """§14.2 row 3: 1200 is past the child's MZ0 of 1237.5."""
        strategy, _ = self.chain(desk, gold, config, [1200, 1200])
        assert "chain_target_already_reached" in self.events(strategy)
        assert not [e for e in self.events(strategy) if e.startswith("created")]

    # --------------------------------------------------------- limit entries

    def test_the_limit_fills_on_a_retest_of_the_child_e50(self, desk, gold, config):
        """§14.3, and §14.7.8: the child then trades its own levels."""
        base = len(bars_for(self.PARENT + [1400]))
        strategy, result = self.chain(
            desk, gold, config, [1400], extra=self.wicks(base, 1400.0, 1480.0)
        )
        assert "limit_filled" in self.events(strategy)
        child = [c for c in result.artifacts["cases"] if c["zone_source"] == "chain"]
        assert len(child) == 1
        case = child[0]
        assert case["entry_mode"] == "chain_limit_retest"
        assert float(case["entry_price"]) == pytest.approx(1475.0)
        assert float(case["initial_tp"]) == pytest.approx(1237.5)     # the child's MZ0
        assert float(case["initial_sl"]) == pytest.approx(2 * 1475.0 - 1187.5)
        assert case["chain_depth"] == 1

    def test_the_child_target_before_a_fill_voids_the_order(self, desk, gold, config):
        """§14.3: reaching MZ0 first cancels the branch without a trade."""
        strategy, result = self.chain(desk, gold, config, [1400, 1200, 1200])
        assert "chain_target_reached_without_entry" in self.events(strategy)
        assert "limit_filled" not in self.events(strategy)
        assert all(c["zone_source"] == "zigzag" for c in result.artifacts["cases"])

    def test_the_limit_never_fills_before_it_was_placed(self, desk, gold, config):
        """The parent's own TP bar dipped through the child's E50; it must not count."""
        strategy, result = self.chain(desk, gold, config, [1400, 1400])
        chain_cases = [c for c in result.artifacts["cases"] if c["zone_source"] == "chain"]
        assert chain_cases == []

    # ---------------------------------------------------------- arbitration

    def test_an_ordinary_signal_supersedes_a_pending_step(self, desk, gold, config):
        """§14.4.3: a fresh admissible ZigZag signal cancels the waiting child.

        2050 extends the candidate into a new zone version whose E50 is 1787.5,
        and 2050 -> 1780 crosses it while the child is still waiting on 1475.
        """
        strategy, result = self.chain(desk, gold, config, [1499, 2050, 2050, 1780, 1780])
        events = self.events(strategy)
        assert events.index("superseded_by_zigzag_signal") > events.index(
            "created_chain_daily_cross"
        )
        assert all(c["zone_source"] == "zigzag" for c in result.artifacts["cases"])
        assert len(result.artifacts["cases"]) == 2

    def test_the_end_of_data_cancels_a_resting_order(self, desk, gold, config):
        """§14.7.11: cancelled, never turned into a trade."""
        strategy, result = self.chain(desk, gold, config, [1400, 1400])
        assert "end_of_data_cancel" in self.events(strategy)
        assert all(c["zone_source"] == "zigzag" for c in result.artifacts["cases"])

