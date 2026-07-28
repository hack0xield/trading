"""ZigZag pivots and margin-zone envelopes."""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import pytest

from backtester.analysis import (
    Pivot, build_envelopes, legs, pivots_known_by, provisional, swing_sizes, zigzag,
)
from backtester.analysis.envelopes import margin_coverage, summarise
from backtester.core.types import Bar
from backtester.data.margins import ContractSpec, MarginLog, MarginObservation

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
