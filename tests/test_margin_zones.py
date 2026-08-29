"""ZigZag pivots, the zones anchored on them, and the daily rollover stream."""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import pytest

from backtester.core.types import Bar
from backtester.indicators.zigzag import (
    Pivot, legs, pivots_known_by, provisional, swing_sizes, zigzag,
)
from backtester.strategies.margin_zones import (
    ContractSpec, MarginLog, MarginObservation, RolloverTracker, ZoneTracker,
    compute_zones, report_name, summarise, zone_spans,
)

UTC = timezone.utc
T0 = datetime(2024, 1, 1, tzinfo=UTC)


def ramp(prices: list[float], step_hours: int = 4) -> list[Bar]:
    """Flat bars at the given prices — high == low, so pivots land exactly."""
    return [
        Bar(time=T0 + timedelta(hours=i * step_hours), open=p, high=p, low=p, close=p, volume=1)
        for i, p in enumerate(prices)
    ]


def spec_6e() -> ContractSpec:
    return ContractSpec(
        code="6E", name="Euro FX futures", contract_size=125_000.0,
        base_currency="EUR", quote_currency="USD",
        tick_size=0.00005, tick_value=6.25, pip_size=0.0001,
    )


class TestZigZag:
    def test_finds_alternating_pivots(self):
        pivots = zigzag(ramp([100, 105, 110, 105, 100, 105, 110]), deviation_abs=5)
        assert [(p.kind, p.index, p.price) for p in pivots] == [
            ("low", 0, 100), ("high", 2, 110), ("low", 4, 100),
        ]

    def test_the_unfinished_swing_is_not_emitted(self):
        """The last leg is still moving, so its extreme could still change."""
        pivots = zigzag(ramp([100, 105, 110, 105, 100, 105, 110]), deviation_abs=5)
        assert all(p.index <= 4 for p in pivots)      # the 110 at index 6 is not a pivot
        assert pivots[-1].kind == "low"

    def test_a_pivot_is_confirmed_after_the_extreme_not_at_it(self):
        """The repaint property, stated as a test."""
        pivots = zigzag(ramp([100, 105, 110, 105, 100, 105, 110]), deviation_abs=5)
        for p in pivots:
            assert p.confirm_index > p.index
            assert p.confirm_time > p.time
        high = [p for p in pivots if p.is_high][0]
        assert high.index == 2 and high.confirm_index == 3
        assert high.lag_bars == 1

    def test_moves_below_the_threshold_are_ignored(self):
        # 2-point wiggles cannot confirm a 5-point reversal.
        pivots = zigzag(ramp([100, 102, 100, 102, 100, 102]), deviation_abs=5)
        assert pivots == []

    def test_a_larger_threshold_finds_fewer_pivots(self):
        prices = [100, 106, 100, 106, 100, 112, 100, 112]
        assert len(zigzag(ramp(prices), deviation_abs=5)) > len(
            zigzag(ramp(prices), deviation_abs=11)
        )

    def test_percentage_threshold(self):
        # 10% of 100 is 10, so a 5-point retrace must not confirm anything.
        assert zigzag(ramp([100, 105, 100, 105]), deviation_pct=10.0) == []
        assert zigzag(ramp([100, 115, 100, 115]), deviation_pct=10.0) != []

    def test_absolute_threshold_wins_over_percentage(self):
        bars = ramp([100, 105, 100, 105])
        assert zigzag(bars, deviation_pct=10.0, deviation_abs=4) != []

    def test_pivots_alternate_and_are_ordered(self):
        prices = [100, 110, 101, 121, 108, 130, 115, 140, 120, 150]
        pivots = zigzag(ramp(prices), deviation_abs=6)
        assert len(pivots) >= 4
        for a, b in legs(pivots):
            assert a.kind != b.kind
            assert a.index < b.index
            assert a.confirm_index <= b.confirm_index

    def test_highs_and_lows_use_bar_extremes_not_closes(self):
        bars = [
            Bar(time=T0 + timedelta(hours=4 * i), open=100, high=h, low=lo, close=100, volume=1)
            for i, (h, lo) in enumerate([(100, 100), (120, 99), (101, 100), (101, 80)])
        ]
        pivots = zigzag(bars, deviation_abs=10)
        assert [p.price for p in pivots if p.is_high] == [120]

    def test_empty_and_single_bar(self):
        assert zigzag([], deviation_abs=1) == []
        assert zigzag(ramp([100]), deviation_abs=1) == []

    def test_a_bad_threshold_is_rejected(self):
        with pytest.raises(ValueError, match="positive deviation"):
            zigzag(ramp([1, 2, 3]), deviation_pct=0)
        with pytest.raises(ValueError, match="deviation_abs must be"):
            zigzag(ramp([1, 2, 3]), deviation_abs=-1)

    def test_swing_sizes(self):
        pivots = zigzag(ramp([100, 105, 110, 105, 100, 105, 110]), deviation_abs=5)
        assert swing_sizes(pivots) == [10.0, 10.0]
        assert swing_sizes(pivots, as_pct=True) == pytest.approx([10.0, 9.0909], rel=1e-3)


class TestNoLookahead:
    def test_pivots_known_by_respects_confirmation(self):
        pivots = zigzag(ramp([100, 105, 110, 105, 100, 105, 110]), deviation_abs=5)
        high = [p for p in pivots if p.is_high][0]      # index 2, confirmed at 3
        assert high not in pivots_known_by(pivots, 2)   # not knowable on its own bar
        assert high in pivots_known_by(pivots, 3)

    def test_filtering_on_index_would_leak(self):
        """Why `confirm_index` exists: the naive filter hands over unconfirmed pivots."""
        pivots = zigzag(ramp([100, 105, 110, 105, 100, 105, 110]), deviation_abs=5)
        naive = [p for p in pivots if p.index <= 2]
        honest = pivots_known_by(pivots, 2)
        assert len(naive) > len(honest)


class TestProvisional:
    """The leg in progress — drawn, never traded, never a pivot."""

    def test_it_reports_the_extreme_since_the_last_pivot(self):
        # ...up to 110, down to 100, then only part-way back: the 100 is the
        # running low but has not been confirmed.
        bars = ramp([100, 105, 110, 105, 100, 102])
        pivots = zigzag(bars, deviation_abs=5)
        pv = provisional(bars, deviation_abs=5, pivots=pivots)
        assert pv is not None
        assert pv.kind == "low" and pv.price == 100

    def test_it_is_not_a_pivot_type(self):
        """Deliberately a different class, so it cannot be passed as confirmed."""
        bars = ramp([100, 105, 110, 105, 100, 102])
        pivots = zigzag(bars, deviation_abs=5)
        pv = provisional(bars, deviation_abs=5, pivots=pivots)
        assert not isinstance(pv, Pivot)
        assert pv not in pivots

    def test_confirm_at_is_the_price_that_would_promote_it(self):
        bars = ramp([100, 105, 110, 105, 100, 102])
        pv = provisional(bars, deviation_abs=5)
        assert pv.confirm_at == pytest.approx(105.0)   # low 100 + 5

    def test_reaching_confirm_at_turns_it_into_a_pivot(self):
        """The contract between the two functions, stated as a test."""
        bars = ramp([100, 105, 110, 105, 100, 102])
        pv = provisional(bars, deviation_abs=5)
        assert pv.price == 100

        extended = ramp([100, 105, 110, 105, 100, 102, pv.confirm_at])
        pivots = zigzag(extended, deviation_abs=5)
        assert pivots[-1].kind == "low" and pivots[-1].price == 100

    def test_it_tracks_a_high_after_a_low_pivot(self):
        bars = ramp([110, 105, 100, 105, 112, 110])
        pv = provisional(bars, deviation_abs=5)
        assert pv.kind == "high" and pv.price == 112

    def test_bars_since_counts_from_the_extreme(self):
        bars = ramp([100, 105, 110, 105, 100, 101, 102])
        pv = provisional(bars, deviation_abs=5)
        assert pv.bars_since == 2       # low at index 4, data ends at 6

    def test_none_when_there_are_no_pivots(self):
        assert provisional(ramp([100, 101, 100]), deviation_abs=50) is None
        assert provisional([], deviation_abs=1) is None



class TestZoneVersions:
    """Zones anchored on the ZigZag candidate, versioned as it moves."""

    @pytest.fixture
    def log(self, tmp_path) -> MarginLog:
        log = MarginLog(tmp_path / "m.csv")
        log.add(MarginObservation("6E", date(2023, 1, 1), 2900.0, source="t"))
        return log

    def track(self, log, bars, deviation_abs=0.01) -> ZoneTracker:
        spec = spec_6e()

        def zones_for(candidate):
            observation = log.latest("6E", on=candidate.time.date())
            return None if observation is None else compute_zones(spec, observation, 1.1)

        tracker = ZoneTracker(zones_for, spec.pip_size, None, deviation_abs)
        for bar in bars:
            tracker.push(bar)
        return tracker

    def test_a_low_anchors_its_zone_above(self, log):
        bars = ramp([1.12, 1.11, 1.10, 1.11, 1.12])
        low = [v for v in self.track(log, bars).versions if v.kind == "low"][0]

        assert low.direction == 1
        # MM=2900, PP=6.25, NP=2 -> FMZ 232 pips, IMZ 255.2 pips at 0.0001/pip.
        assert low.mz0 == pytest.approx(low.anchor_price + 0.0232)
        assert low.mz100 == pytest.approx(low.anchor_price + 0.02552)

    def test_a_high_anchors_its_zone_below(self, log):
        bars = ramp([1.10, 1.11, 1.12, 1.11, 1.10])
        high = [v for v in self.track(log, bars).versions if v.kind == "high"][0]

        assert high.direction == -1
        assert high.mz0 == pytest.approx(high.anchor_price - 0.0232)
        assert high.mz100 < high.mz0

    def test_e50_matches_the_spec_worked_example(self, log):
        # 50% MZ = 243.6 pips, E50 = 121.8 pips from the anchor.
        bars = ramp([1.12, 1.11, 1.10, 1.11, 1.12])
        low = [v for v in self.track(log, bars).versions if v.kind == "low"][0]
        assert low.e50 == pytest.approx(low.anchor_price + 0.01218)

    def test_e50_is_halfway_between_the_anchor_and_the_midpoint(self, log):
        bars = ramp([1.10, 1.11, 1.12, 1.11, 1.10, 1.11, 1.12])
        for version in self.track(log, bars).versions:
            assert version.e50 == pytest.approx((version.anchor_price + version.mz50) / 2)

    def test_e50_sits_outside_the_zone_nearer_the_anchor(self, log):
        bars = ramp([1.10, 1.11, 1.12, 1.11, 1.10])
        high = [v for v in self.track(log, bars).versions if v.kind == "high"][0]
        assert high.anchor_price - high.e50 < high.anchor_price - high.mz0

    def test_a_version_is_never_knowable_before_its_own_bar(self, log):
        bars = ramp([1.10, 1.11, 1.12, 1.11, 1.10, 1.11, 1.12])
        for version in self.track(log, bars).versions:
            assert version.known_index >= version.anchor_index
            assert version.known_time >= version.anchor_time

    def test_margin_is_read_as_it_stood_at_the_anchor(self, tmp_path):
        """A 2024 anchor must not be drawn with a 2025 margin."""
        log = MarginLog(tmp_path / "m.csv")
        log.add(MarginObservation("6E", date(2023, 1, 1), 2900.0, source="t"))
        log.add(MarginObservation("6E", date(2025, 1, 1), 5800.0, source="t"))

        bars = ramp([1.10, 1.11, 1.12, 1.11, 1.10, 1.11, 1.12])  # all in 2024
        versions = self.track(log, bars).versions
        assert versions
        assert {v.zones.maintenance for v in versions} == {2900.0}
        assert all(v.zones.as_of == date(2023, 1, 1) for v in versions)

    def test_a_wider_margin_widens_the_zone(self, tmp_path):
        bars = ramp([1.10, 1.11, 1.12, 1.11, 1.10, 1.11, 1.12])
        narrow = MarginLog(tmp_path / "narrow.csv")
        narrow.add(MarginObservation("6E", date(2023, 1, 1), 2900.0, source="t"))
        wide = MarginLog(tmp_path / "wide.csv")
        wide.add(MarginObservation("6E", date(2023, 1, 1), 5800.0, source="t"))

        a = self.track(narrow, bars).versions[0]
        b = self.track(wide, bars).versions[0]
        assert b.zones.fmz == pytest.approx(2 * a.zones.fmz)

    def test_a_candidate_with_no_margin_reading_makes_no_zone(self, tmp_path):
        log = MarginLog(tmp_path / "m.csv")
        log.add(MarginObservation("6E", date(2099, 1, 1), 2900.0, source="t"))
        bars = ramp([1.10, 1.11, 1.12, 1.11, 1.10, 1.11, 1.12])
        tracker = self.track(log, bars)
        assert tracker.versions == []
        assert tracker.uncovered

    def test_spans_run_from_known_to_superseded(self, log):
        bars = ramp([1.10, 1.11, 1.12, 1.11, 1.10, 1.11, 1.12])
        tracker = self.track(log, bars)
        spans = zone_spans(tracker.versions, tracker.superseded, len(bars) - 1)

        assert [s[1] for s in spans] == [v.known_index for v in tracker.versions]
        for (_, _, end), (nxt, start, _) in zip(spans, spans[1:]):
            assert end == start - 1
        assert spans[-1][2] == len(bars) - 1

    def test_summarise_counts_zones_reached_while_active(self, log):
        bars = ramp([1.10, 1.11, 1.12, 1.11, 1.10, 1.11, 1.12])
        tracker = self.track(log, bars)
        spans = zone_spans(tracker.versions, tracker.superseded, len(bars) - 1)
        stats = summarise(spans, bars)
        assert stats["zones"] == len(spans)
        assert stats["distinct_margins"] == 1
        assert stats["avg_fmz_pips"] == pytest.approx(232.0)

    def test_summarise_on_nothing(self):
        assert summarise([], []) == {"zones": 0}


class TestRolloverTracker:
    """One daily marker at the last price strictly before T_roll."""

    def m15(self, prices: list[float], start: datetime) -> list[Bar]:
        return [
            Bar(time=start + timedelta(minutes=15 * i), open=p, high=p, low=p, close=p, volume=1)
            for i, p in enumerate(prices)
        ]

    def points(self, bars, rollover_hour=0, rollover_tz="UTC"):
        tracker = RolloverTracker(rollover_hour, rollover_tz)
        for bar in bars:
            tracker.push(bar)
        return tracker.points

    def test_empty_bars(self):
        assert self.points([]) == []

    def test_one_point_per_day_using_the_last_bar_before_midnight(self):
        # 23:00 to 23:45 on day 1, then a bar on day 2 so day 2's own T_roll
        # settles -> the 23:45 close is its point.
        bars = self.m15([10, 11, 12, 13], start=datetime(2024, 1, 1, 23, 0, tzinfo=UTC))
        bars += self.m15([99], start=datetime(2024, 1, 2, 0, 30, tzinfo=UTC))
        points = self.points(bars)
        assert [p.day for p in points] == [date(2024, 1, 2)]
        assert points[0].price == 13
        assert points[0].roll_time == datetime(2024, 1, 2, 0, 0, tzinfo=UTC)
        assert points[0].bar_time == datetime(2024, 1, 1, 23, 45, tzinfo=UTC)

    def test_first_day_with_nothing_before_it_gets_no_point(self):
        bars = self.m15([10, 11, 12, 13], start=datetime(2024, 1, 1, 0, 0, tzinfo=UTC))
        bars += self.m15([99], start=datetime(2024, 1, 2, 0, 30, tzinfo=UTC))
        points = self.points(bars)
        assert [p.day for p in points] == [date(2024, 1, 2)]
        assert points[0].price == 13

    def test_the_bar_at_or_after_roll_time_is_never_used(self):
        # A bar exactly at midnight belongs to the new session.
        bars = self.m15([10, 11], start=datetime(2024, 1, 1, 23, 45, tzinfo=UTC))
        assert bars[1].time == datetime(2024, 1, 2, 0, 0, tzinfo=UTC)
        day2 = [p for p in self.points(bars) if p.day == date(2024, 1, 2)]
        assert day2 and day2[0].price == 10          # the 23:45 bar, not the 00:00 one

    def test_weekend_gap_collapses_into_one_point_not_two(self):
        # Friday bars, then nothing until Sunday night. Saturday's T_roll finds
        # Friday's last bar; Sunday's finds that same bar and must not repeat
        # it; Monday's finds fresh Sunday-night data.
        friday = self.m15([100, 101, 102, 103], start=datetime(2024, 1, 5, 23, 0, tzinfo=UTC))
        sunday = self.m15(
            [110, 111, 112, 113, 114, 115, 116, 117], start=datetime(2024, 1, 7, 22, 0, tzinfo=UTC)
        )
        monday = self.m15([120], start=datetime(2024, 1, 8, 0, 0, tzinfo=UTC))
        points = self.points(friday + sunday + monday)
        assert [p.day for p in points] == [date(2024, 1, 6), date(2024, 1, 8)]
        assert points[0].price == 103   # Saturday <- Friday 23:45
        assert points[1].price == 117   # Monday   <- Sunday 23:45, not Friday again

    def test_rollover_tz_shifts_which_bar_is_picked(self):
        # Break at 00:00 UTC+3 is 21:00 UTC the day before, so an earlier bar
        # becomes the price.
        bars = self.m15([5], start=datetime(2024, 1, 1, 20, 0, tzinfo=UTC))
        bars += self.m15([10, 11, 12, 13], start=datetime(2024, 1, 1, 22, 0, tzinfo=UTC))
        bars += self.m15([99], start=datetime(2024, 1, 2, 1, 0, tzinfo=UTC))
        assert self.points(bars)[0].price == 13                        # before 01-02 00:00 UTC
        assert self.points(bars, rollover_tz="UTC+3")[0].price == 5    # before 01-01 21:00 UTC


class TestReportName:
    """Timestamp-first, so runs sort chronologically and never collide."""

    def test_it_leads_with_a_utc_stamp_then_the_inputs(self):
        name = report_name("EURUSD", "H4", "6E", "2%", stamp=T0)
        assert name == "20240101-000000_zones_EURUSD_H4_6E_dev2pct"

    def test_two_runs_a_second_apart_do_not_collide(self):
        a = report_name("EURUSD", "H4", "6E", "2%", stamp=T0)
        b = report_name("EURUSD", "H4", "6E", "2%", stamp=T0 + timedelta(seconds=1))
        assert a != b
        assert sorted([b, a]) == [a, b]      # chronological by string sort

    def test_pips_thresholds_survive_the_slug(self):
        assert report_name("EURUSD", "H4", "6E", "250 pips", stamp=T0).endswith("dev250pips")
