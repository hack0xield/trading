"""ZigZag pivots and margin-zone envelopes."""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import pytest

from backtester.core.types import Bar
from backtester.indicators.zigzag import (
    Pivot, legs, pivots_known_by, provisional, swing_sizes, zigzag,
)
from backtester.strategies.margin_zones import (
    ContractSpec, Envelope, MarginLog, MarginObservation, RolloverPoint, build_envelopes,
    envelope_at, margin_coverage, rollover_crossings, rollover_points, summarise,
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


class TestEnvelopes:
    """FMZ/IMZ bands projected from pivots, per step 5 of the note."""

    @pytest.fixture
    def log(self, tmp_path) -> MarginLog:
        log = MarginLog(tmp_path / "m.csv")
        log.add(MarginObservation("6E", date(2023, 1, 1), 2900.0, source="t"))
        return log

    def test_band_is_projected_up_from_a_low(self, log):
        bars = ramp([1.10, 1.11, 1.12, 1.11, 1.10, 1.11, 1.12])
        pivots = zigzag(bars, deviation_abs=0.01)
        envelopes = build_envelopes(bars, pivots, spec_6e(), log)
        low = [e for e in envelopes if not e.pivot.is_high][0]

        assert low.direction == 1
        # FMZ 232 pips, IMZ 255.2 pips, at 0.0001 per pip.
        assert low.fmz_price == pytest.approx(low.pivot.price + 0.0232)
        assert low.imz_price == pytest.approx(low.pivot.price + 0.02552)
        assert low.width == pytest.approx(0.00232)

    def test_band_is_projected_down_from_a_high(self, log):
        bars = ramp([1.10, 1.11, 1.12, 1.11, 1.10, 1.11, 1.12])
        pivots = zigzag(bars, deviation_abs=0.01)
        envelopes = build_envelopes(bars, pivots, spec_6e(), log)
        high = [e for e in envelopes if e.pivot.is_high][0]

        assert high.direction == -1
        assert high.fmz_price == pytest.approx(high.pivot.price - 0.0232)
        assert high.imz_price < high.fmz_price

    def test_each_envelope_runs_to_the_next_pivot(self, log):
        bars = ramp([1.10, 1.11, 1.12, 1.11, 1.10, 1.11, 1.12])
        pivots = zigzag(bars, deviation_abs=0.01)
        envelopes = build_envelopes(bars, pivots, spec_6e(), log)
        for env, nxt in zip(envelopes, pivots[1:]):
            assert env.end_index == nxt.index
        assert envelopes[-1].end_index == len(bars) - 1

    def test_margin_is_read_as_it_stood_at_the_pivot(self, tmp_path):
        """A 2024 pivot must not be drawn with a 2025 margin."""
        log = MarginLog(tmp_path / "m.csv")
        log.add(MarginObservation("6E", date(2023, 1, 1), 2900.0, source="t"))
        log.add(MarginObservation("6E", date(2025, 1, 1), 5800.0, source="t"))

        bars = ramp([1.10, 1.11, 1.12, 1.11, 1.10, 1.11, 1.12])  # all in 2024
        pivots = zigzag(bars, deviation_abs=0.01)
        envelopes = build_envelopes(bars, pivots, spec_6e(), log)

        assert {e.maintenance for e in envelopes} == {2900.0}
        assert all(e.margin_as_of == date(2023, 1, 1) for e in envelopes)

    def test_a_margin_change_widens_later_envelopes(self, tmp_path):
        log = MarginLog(tmp_path / "m.csv")
        log.add(MarginObservation("6E", date(2023, 1, 1), 2900.0, source="t"))
        bars = ramp([1.10, 1.11, 1.12, 1.11, 1.10, 1.11, 1.12])
        narrow = build_envelopes(bars, zigzag(bars, deviation_abs=0.01), spec_6e(), log)

        log.add(MarginObservation("6E", date(2023, 6, 1), 5800.0, source="t"))
        wide = build_envelopes(bars, zigzag(bars, deviation_abs=0.01), spec_6e(), log)
        assert wide[0].fmz_pips == pytest.approx(2 * narrow[0].fmz_pips)

    def test_pivots_before_any_reading_are_skipped(self, tmp_path):
        log = MarginLog(tmp_path / "m.csv")
        log.add(MarginObservation("6E", date(2099, 1, 1), 2900.0, source="t"))
        bars = ramp([1.10, 1.11, 1.12, 1.11, 1.10, 1.11, 1.12])
        pivots = zigzag(bars, deviation_abs=0.01)

        assert build_envelopes(bars, pivots, spec_6e(), log) == []
        covered, missing = margin_coverage(pivots, log, "6E")
        assert covered == [] and len(missing) == len(pivots)

    def test_touched_detects_price_reaching_the_band(self, log):
        # Rise far enough past the low pivot to enter its 232-pip zone.
        bars = ramp([1.10, 1.09, 1.08, 1.09, 1.12])
        pivots = zigzag(bars, deviation_abs=0.005)
        envelopes = build_envelopes(bars, pivots, spec_6e(), log)
        assert envelopes and envelopes[-1].touched(bars) in (True, False)

    def test_summarise_reports_coverage(self, log):
        bars = ramp([1.10, 1.11, 1.12, 1.11, 1.10, 1.11, 1.12])
        envelopes = build_envelopes(bars, zigzag(bars, deviation_abs=0.01), spec_6e(), log)
        stats = summarise(envelopes, bars)
        assert stats["envelopes"] == len(envelopes)
        assert stats["distinct_margins"] == 1
        assert stats["avg_fmz_pips"] == pytest.approx(232.0)

    def test_summarise_on_nothing(self):
        assert summarise([], []) == {"envelopes": 0}


class TestE50Level:
    """Revised spec §3.5: halfway between the pivot and the 50% MZ midpoint."""

    @pytest.fixture
    def log(self, tmp_path) -> MarginLog:
        log = MarginLog(tmp_path / "m.csv")
        log.add(MarginObservation("6E", date(2023, 1, 1), 2900.0, source="t"))
        return log

    def test_matches_the_spec_worked_example(self, log):
        # MM=2900, PP=6.25, NP=2 -> FMZ=232, IMZ=255.2, 50% MZ=243.6,
        # E50=121.8 pips (spec §6), i.e. 0.01218 at 0.0001 per pip.
        bars = ramp([1.10, 1.11, 1.12, 1.11, 1.10, 1.11, 1.12])
        pivots = zigzag(bars, deviation_abs=0.01)
        low = [e for e in build_envelopes(bars, pivots, spec_6e(), log) if not e.pivot.is_high][0]
        assert low.e50_price == pytest.approx(low.pivot.price + 0.01218)

    def test_is_halfway_between_pivot_and_mz50(self, log):
        bars = ramp([1.10, 1.11, 1.12, 1.11, 1.10, 1.11, 1.12])
        pivots = zigzag(bars, deviation_abs=0.01)
        for e in build_envelopes(bars, pivots, spec_6e(), log):
            assert e.e50_price == pytest.approx((e.pivot.price + e.mid_price) / 2)

    def test_a_high_pivots_e50_sits_below_the_pivot_and_outside_the_zone(self, log):
        bars = ramp([1.10, 1.11, 1.12, 1.11, 1.10, 1.11, 1.12])
        pivots = zigzag(bars, deviation_abs=0.01)
        high = [e for e in build_envelopes(bars, pivots, spec_6e(), log) if e.pivot.is_high][0]
        assert high.e50_price == pytest.approx(high.pivot.price - 0.01218)
        # E50 sits between the pivot and FMZ, not inside [FMZ, IMZ] — it is
        # nearer to the pivot than FMZ is.
        assert high.pivot.price - high.e50_price < high.pivot.price - high.fmz_price


class TestRolloverPoints:
    """Revised spec §5: one daily marker at the last price before T_roll."""

    def m15(self, prices: list[float], start: datetime) -> list[Bar]:
        return [
            Bar(time=start + timedelta(minutes=15 * i), open=p, high=p, low=p, close=p, volume=1)
            for i, p in enumerate(prices)
        ]

    def test_empty_bars(self):
        assert rollover_points([], rollover_hour=0, rollover_tz="UTC") == []

    def test_one_point_per_day_using_the_last_bar_before_midnight(self):
        # 23:00, 23:15, 23:30, 23:45 on day 1, then one bar on day 2 so the
        # scan reaches day 2's own T_roll -> the 23:45 close is its point.
        bars = self.m15([10, 11, 12, 13], start=datetime(2024, 1, 1, 23, 0, tzinfo=UTC))
        bars += self.m15([99], start=datetime(2024, 1, 2, 0, 30, tzinfo=UTC))
        points = rollover_points(bars, rollover_hour=0, rollover_tz="UTC")
        assert [p.day for p in points] == [date(2024, 1, 2)]
        assert points[0].price == 13
        assert points[0].roll_time == datetime(2024, 1, 2, 0, 0, tzinfo=UTC)
        assert points[0].bar_time == datetime(2024, 1, 1, 23, 45, tzinfo=UTC)

    def test_first_day_with_nothing_before_it_gets_no_point(self):
        # Data starts at day 1's own midnight, so T_roll(day 1) has no bar
        # before it at all; day 2 does, and must not be skipped too.
        bars = self.m15([10, 11, 12, 13], start=datetime(2024, 1, 1, 0, 0, tzinfo=UTC))
        bars += self.m15([99], start=datetime(2024, 1, 2, 0, 30, tzinfo=UTC))
        points = rollover_points(bars, rollover_hour=0, rollover_tz="UTC")
        assert [p.day for p in points] == [date(2024, 1, 2)]
        assert points[0].price == 13

    def test_the_bar_at_or_after_roll_time_is_never_used(self):
        # A bar exactly at midnight belongs to the new session, not the
        # previous one's rollover price (validation rule 12).
        bars = self.m15([10, 11], start=datetime(2024, 1, 1, 23, 45, tzinfo=UTC))
        assert bars[1].time == datetime(2024, 1, 2, 0, 0, tzinfo=UTC)
        points = rollover_points(bars, rollover_hour=0, rollover_tz="UTC")
        day2 = [p for p in points if p.day == date(2024, 1, 2)]
        assert day2 and day2[0].price == 10          # the 23:45 bar, not the 00:00 one

    def test_weekend_gap_collapses_into_one_point_not_two(self):
        # Friday bars, then nothing until Sunday night (the realistic FX/CFD
        # reopen, before Monday's own midnight). Saturday's T_roll finds
        # Friday's last bar; Sunday's T_roll finds that same bar again (no
        # new data yet) and must not repeat it; Monday's T_roll finds fresh
        # Sunday-night data.
        friday = self.m15([100, 101, 102, 103], start=datetime(2024, 1, 5, 23, 0, tzinfo=UTC))
        sunday = self.m15(
            [110, 111, 112, 113, 114, 115, 116, 117], start=datetime(2024, 1, 7, 22, 0, tzinfo=UTC)
        )
        monday_anchor = self.m15([120], start=datetime(2024, 1, 8, 0, 0, tzinfo=UTC))
        points = rollover_points(friday + sunday + monday_anchor, rollover_hour=0, rollover_tz="UTC")
        assert [p.day for p in points] == [date(2024, 1, 6), date(2024, 1, 8)]
        assert points[0].price == 103   # Saturday <- Friday 23:45
        assert points[1].price == 117   # Monday   <- Sunday 23:45, not Friday again

    def test_rollover_tz_shifts_which_bar_is_picked(self):
        # Break at 00:00 UTC+3 is 21:00 UTC the day before — earlier than the
        # plain-UTC case, so a different (earlier) bar becomes the price.
        bars = self.m15([5], start=datetime(2024, 1, 1, 20, 0, tzinfo=UTC))
        bars += self.m15([10, 11, 12, 13], start=datetime(2024, 1, 1, 22, 0, tzinfo=UTC))
        bars += self.m15([99], start=datetime(2024, 1, 2, 1, 0, tzinfo=UTC))
        utc = rollover_points(bars, rollover_hour=0, rollover_tz="UTC")
        shifted = rollover_points(bars, rollover_hour=0, rollover_tz="UTC+3")
        assert utc[0].price == 13      # last bar before 2024-01-02 00:00 UTC
        assert shifted[0].price == 5   # last bar before 2024-01-01 21:00 UTC


class TestEnvelopeAt:
    """Which Margin Zone was active at a given time — the crossing engine's lookup."""

    def envelope(self, direction: int, start_index: int, end_index: int) -> Envelope:
        pivot = Pivot(
            index=start_index, time=T0 + timedelta(hours=4 * start_index), price=100.0,
            kind="high" if direction < 0 else "low",
            confirm_index=start_index + 1, confirm_time=T0,
        )
        return Envelope(
            pivot=pivot, start_index=start_index, end_index=end_index, direction=direction,
            fmz_price=90.0, imz_price=80.0, fmz_pips=10.0, imz_pips=20.0,
            maintenance=1000.0, margin_as_of=date(2024, 1, 1),
        )

    def test_before_any_coverage_is_none(self):
        bars = ramp([1.0] * 10)
        env = self.envelope(-1, start_index=2, end_index=6)
        assert envelope_at([env], bars, bars[0].time) is None

    def test_within_range_returns_the_envelope(self):
        bars = ramp([1.0] * 10)
        env = self.envelope(-1, start_index=2, end_index=6)
        assert envelope_at([env], bars, bars[2].time) is env    # inclusive start
        assert envelope_at([env], bars, bars[5].time) is env

    def test_at_its_own_end_index_is_no_longer_covered(self):
        # end_index is the next pivot's own bar — it belongs to whatever
        # comes after, not to this envelope. A second envelope has to be
        # present, or the "last envelope is open-ended" rule below would
        # make this one open-ended too.
        bars = ramp([1.0] * 10)
        env = self.envelope(-1, start_index=2, end_index=6)
        following = self.envelope(-1, start_index=6, end_index=9)
        assert envelope_at([env, following], bars, bars[6].time) is following

    def test_a_gap_between_envelopes_resolves_to_none(self):
        # Simulates a pivot skipped for lacking a margin reading: envelope A
        # ends at bar 6, envelope B only starts at bar 7 — bar 6 itself is
        # covered by neither.
        bars = ramp([1.0] * 10)
        a = self.envelope(-1, start_index=2, end_index=6)
        b = self.envelope(-1, start_index=7, end_index=9)
        assert envelope_at([a, b], bars, bars[6].time) is None
        assert envelope_at([a, b], bars, bars[7].time) is b

    def test_the_last_envelope_is_open_ended(self):
        # Its end_index is just the last bar in the dataset, not a boundary
        # set by a following pivot — there is no "next" to close it off.
        bars = ramp([1.0] * 10)
        env = self.envelope(-1, start_index=7, end_index=9)
        assert envelope_at([env], bars, bars[9].time) is env
        assert envelope_at([env], bars, bars[9].time + timedelta(days=365)) is env

    def test_no_envelopes_is_none(self):
        bars = ramp([1.0] * 10)
        assert envelope_at([], bars, bars[0].time) is None


class TestRolloverCrossings:
    """§5.5: rollover-to-rollover crossings of the 50% Extremum-to-50% MZ level."""

    def envelope(self, direction: int, start_index: int = 0, end_index: int = 29) -> Envelope:
        pivot = Pivot(
            index=start_index, time=T0, price=100.0,
            kind="high" if direction < 0 else "low",
            confirm_index=start_index + 1, confirm_time=T0,
        )
        # fmz/imz chosen so mid_price = (fmz+imz)/2 = 80, e50 = (100+80)/2 = 90,
        # for a "high" envelope; a "low" one is the mirror image (e50 = 110).
        fmz, imz = (90.0, 70.0) if direction < 0 else (110.0, 130.0)
        return Envelope(
            pivot=pivot, start_index=start_index, end_index=end_index, direction=direction,
            fmz_price=fmz, imz_price=imz, fmz_pips=10.0, imz_pips=30.0,
            maintenance=1000.0, margin_as_of=date(2024, 1, 1),
        )

    def rp(self, day_offset: int, price: float, hour: int = 0) -> RolloverPoint:
        day = date(2024, 1, 1) + timedelta(days=day_offset)
        roll_time = datetime(day.year, day.month, day.day, hour, tzinfo=UTC)
        return RolloverPoint(day=day, roll_time=roll_time, price=price,
                             bar_time=roll_time - timedelta(minutes=15))

    def test_downward_crossing_of_a_max_zone_is_true(self):
        # Max zone (direction=-1): the zone sits below E_level, so a downward
        # crossing moves price toward it.
        env = self.envelope(-1)
        bars = ramp([1.0] * 30)
        points = [self.rp(1, env.e50_price + 5), self.rp(2, env.e50_price - 5)]
        events = rollover_crossings(points, [env], bars)
        assert len(events) == 1
        assert events[0].direction == "down"
        assert events[0].classification == "True"
        assert events[0].current is points[1]
        assert events[0].e_level == pytest.approx(env.e50_price)

    def test_upward_crossing_of_a_max_zone_is_false(self):
        env = self.envelope(-1)
        bars = ramp([1.0] * 30)
        points = [self.rp(1, env.e50_price - 5), self.rp(2, env.e50_price + 5)]
        events = rollover_crossings(points, [env], bars)
        assert len(events) == 1
        assert events[0].direction == "up"
        assert events[0].classification == "False"

    def test_upward_crossing_of_a_min_zone_is_true(self):
        # Min zone (direction=+1): the zone sits above E_level, so an upward
        # crossing moves price toward it.
        env = self.envelope(1)
        bars = ramp([1.0] * 30)
        points = [self.rp(1, env.e50_price - 5), self.rp(2, env.e50_price + 5)]
        events = rollover_crossings(points, [env], bars)
        assert len(events) == 1
        assert events[0].direction == "up"
        assert events[0].classification == "True"

    def test_downward_crossing_of_a_min_zone_is_false(self):
        env = self.envelope(1)
        bars = ramp([1.0] * 30)
        points = [self.rp(1, env.e50_price + 5), self.rp(2, env.e50_price - 5)]
        events = rollover_crossings(points, [env], bars)
        assert len(events) == 1
        assert events[0].classification == "False"

    def test_no_event_when_both_points_are_on_the_same_side(self):
        env = self.envelope(-1)
        bars = ramp([1.0] * 30)
        points = [self.rp(1, env.e50_price + 5), self.rp(2, env.e50_price + 3)]
        assert rollover_crossings(points, [env], bars) == []

    def test_no_event_when_a_point_sits_exactly_on_the_level(self):
        # Validation rule 17: equal-to-level does not count as "opposite sides."
        env = self.envelope(-1)
        bars = ramp([1.0] * 30)
        points = [self.rp(1, env.e50_price), self.rp(2, env.e50_price - 5)]
        assert rollover_crossings(points, [env], bars) == []

    def test_baseline_resets_across_a_new_envelope(self):
        # Two envelopes, back to back. A crossing entirely inside envelope A
        # fires; the transition into B does not, even though it also crosses
        # numerically; a fresh crossing entirely inside B fires again.
        bars = ramp([1.0] * 30)
        a = self.envelope(-1, start_index=0, end_index=16)
        b = self.envelope(-1, start_index=16, end_index=29)
        points = [
            self.rp(1, a.e50_price + 5, hour=0),                            # in A (t=T0+24h)
            self.rp(2, a.e50_price - 5, hour=0),                            # in A (t=T0+48h) -> event 1
            self.rp(3, b.e50_price + 5, hour=22),                           # in B (t=T0+70h)
            self.rp(4, b.e50_price - 5, hour=22),                           # in B (t=T0+94h) -> event 2
        ]
        assert envelope_at([a, b], bars, points[1].roll_time) is a
        assert envelope_at([a, b], bars, points[2].roll_time) is b

        events = rollover_crossings(points, [a, b], bars)
        assert len(events) == 2
        assert events[0].current is points[1]
        assert events[1].current is points[3]

    def test_a_wide_gap_is_not_bridged(self):
        env = self.envelope(-1)
        bars = ramp([1.0] * 30)
        points = [self.rp(1, env.e50_price + 5), self.rp(11, env.e50_price - 5)]
        assert rollover_crossings(points, [env], bars, max_gap_days=3) == []

    def test_a_weekend_sized_gap_is_bridged(self):
        env = self.envelope(-1)
        bars = ramp([1.0] * 30)
        points = [self.rp(1, env.e50_price + 5), self.rp(3, env.e50_price - 5)]
        events = rollover_crossings(points, [env], bars, max_gap_days=3)
        assert len(events) == 1


class TestReportName:
    """Timestamp-first, so runs sort chronologically and never collide."""

    def test_it_leads_with_a_utc_stamp_then_the_inputs(self):
        from backtester.strategies.margin_zones import report_name

        name = report_name("EURUSD", "H4", "6E", "2%", stamp=T0)
        assert name == "20240101-000000_zones_EURUSD_H4_6E_dev2pct"

    def test_two_runs_a_second_apart_do_not_collide(self):
        from datetime import timedelta

        from backtester.strategies.margin_zones import report_name

        a = report_name("EURUSD", "H4", "6E", "2%", stamp=T0)
        b = report_name("EURUSD", "H4", "6E", "2%", stamp=T0 + timedelta(seconds=1))
        assert a != b
        assert sorted([b, a]) == [a, b]      # chronological by string sort

    def test_pips_thresholds_survive_the_slug(self):
        from backtester.strategies.margin_zones import report_name

        assert report_name("EURUSD", "H4", "6E", "250 pips", stamp=T0).endswith("dev250pips")
