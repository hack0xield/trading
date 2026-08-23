"""Senior swing structure — `backtester/indicators/swings.py`.

The rule moved here out of the margin-zone package: it needs nothing but
pivots, and a strategy wanting dominant swings should not have to import CME
margin machinery to get them. These tests are the reason that move is safe —
they exercise the rule with no instrument, no margin log and no zone in sight.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from backtester.indicators import Pivot, is_senior, newest_senior, senior_pivots

UTC = timezone.utc
START = datetime(2024, 1, 1, tzinfo=UTC)


def pivot(index: int, price: float, kind: str) -> Pivot:
    """A pivot confirmed two bars after its extreme."""
    return Pivot(
        index=index, time=START + timedelta(hours=4 * index), price=price, kind=kind,
        confirm_index=index + 2, confirm_time=START + timedelta(hours=4 * (index + 2)),
    )


def alternating(prices: list[float], first: str = "low") -> list[Pivot]:
    kinds = ("low", "high") if first == "low" else ("high", "low")
    return [pivot(i * 10, p, kinds[i % 2]) for i, p in enumerate(prices)]


class TestIsSenior:
    def test_a_high_above_both_same_type_neighbours(self):
        assert is_senior(pivot(2, 110, "high"), pivot(0, 100, "high"), pivot(4, 105, "high"))

    def test_a_low_below_both(self):
        assert is_senior(pivot(2, 90, "low"), pivot(0, 100, "low"), pivot(4, 95, "low"))

    def test_beaten_on_either_side_is_not_senior(self):
        assert not is_senior(pivot(2, 110, "high"), pivot(0, 120, "high"), pivot(4, 105, "high"))
        assert not is_senior(pivot(2, 110, "high"), pivot(0, 100, "high"), pivot(4, 115, "high"))

    def test_an_equal_neighbour_does_not_dominate(self):
        """A flat double top is not senior — the comparison is strict."""
        assert not is_senior(pivot(2, 110, "high"), pivot(0, 110, "high"), pivot(4, 100, "high"))


class TestSeniorPivots:
    def test_it_compares_same_type_neighbours_two_apart(self):
        """The intervening pivot is the other type; comparing against it would
        make almost every pivot look senior."""
        # lows 100, 95, 98 with highs 120, 130 between them: the middle low wins.
        pivots = alternating([100, 120, 95, 130, 98, 140, 105])
        (senior,) = senior_pivots(pivots)
        assert senior.kind == "low" and senior.price == 95
        assert senior.left.kind == senior.right.kind == "low"

    def test_the_ends_cannot_be_senior(self):
        """Nothing to dominate on one side."""
        assert senior_pivots(alternating([100, 120, 95, 130])) == []

    def test_it_carries_the_confirmation_of_the_right_neighbour(self):
        pivots = alternating([100, 120, 95, 130, 98, 140, 105])
        (senior,) = senior_pivots(pivots)
        assert senior.confirm_index == senior.right.confirm_index
        assert senior.confirm_index > senior.index
        assert senior.lag_bars == senior.right.confirm_index - senior.pivot.index


class TestNewestSenior:
    """The streaming form: what the pivot that just landed confirmed."""

    def test_it_confirms_the_pivot_two_back(self):
        """`pivots[-3]`, confirmed by the pivot that has just landed."""
        pivots = alternating([100, 120, 95, 130, 98])
        senior = newest_senior(pivots)
        assert senior is not None and senior.price == 95
        assert senior.pivot is pivots[-3]
        assert senior.left is pivots[-5] and senior.right is pivots[-1]

    def test_nothing_is_confirmed_when_the_newest_pivot_confirms_nothing(self):
        """The 98 low does not dominate the 95 before it, so no event."""
        assert newest_senior(alternating([100, 120, 95, 130, 98, 140, 105])) is None

    def test_nothing_to_confirm_below_five_pivots(self):
        assert newest_senior(alternating([100, 120, 95, 130])) is None

    def test_it_agrees_with_the_batch_scan(self):
        """Feeding pivots one at a time must find what one pass finds."""
        pivots = alternating([100, 120, 95, 130, 98, 140, 90, 135, 99, 128, 104])
        streamed = [
            senior for k in range(len(pivots))
            if (senior := newest_senior(pivots[: k + 1])) is not None
        ]
        assert [s.price for s in streamed] == [s.price for s in senior_pivots(pivots)]
