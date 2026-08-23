"""Swing structure read off ZigZag pivots.

A *senior* pivot is one that dominates the nearest pivot of its own type on
each side: a high with a lower high either side of it, a low with a higher low
either side. It is the shape a chartist means by "the swing high that matters",
and it needs nothing but pivots to find — no instrument, no margin, no zone.

That is why it lives here rather than in `strategies/margin_zones/`, where it
started. The margin-zone pattern that introduced it uses the rule to pick an
anchor and then projects a level from it, but the rule itself is separable and
any strategy interested in dominant swings can have it without importing a
package full of CME margin machinery.

**Same-type neighbours are two positions apart.** ZigZag pivots alternate, so
the previous high of `pivots[k]` is `pivots[k - 2]`, not `pivots[k - 1]` — the
intervening pivot is a low. Getting that wrong compares a high against a low
and finds every pivot senior.

**A senior pivot is not knowable when it happens.** It needs the pivot to its
right, and that one needs price to retrace far enough to confirm it. So
`confirm_index` here is the *right* neighbour's, which is always later than the
candidate's own — see `zigzag.py` on why that distinction is the whole
difficulty of trading swing structure.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from .zigzag import Pivot


@dataclass(frozen=True, slots=True)
class SeniorPivot:
    """A pivot and the two same-type neighbours it dominates."""

    pivot: Pivot
    left: Pivot     # nearest same-type pivot before it
    right: Pivot    # nearest same-type pivot after it; its confirmation confirms this

    @property
    def kind(self) -> str:
        return self.pivot.kind

    @property
    def is_high(self) -> bool:
        return self.pivot.is_high

    @property
    def price(self) -> float:
        return self.pivot.price

    @property
    def time(self) -> datetime:
        """When the extreme happened."""
        return self.pivot.time

    @property
    def index(self) -> int:
        return self.pivot.index

    @property
    def confirm_time(self) -> datetime:
        """When this became knowable — the right neighbour's confirmation."""
        return self.right.confirm_time

    @property
    def confirm_index(self) -> int:
        return self.right.confirm_index

    @property
    def lag_bars(self) -> int:
        """Bars between the extreme and the first bar it could be acted on."""
        return self.confirm_index - self.index


def is_senior(candidate: Pivot, left: Pivot, right: Pivot) -> bool:
    """Does `candidate` dominate both same-type neighbours?

    Strict on both sides: a neighbour at the same price does not dominate and
    is not dominated, so an exactly equal double top is not senior.
    """
    if candidate.is_high:
        return left.price < candidate.price > right.price
    return left.price > candidate.price < right.price


def senior_pivots(pivots: list[Pivot]) -> list[SeniorPivot]:
    """Every senior pivot in a finished list, oldest first."""
    out = []
    for k in range(2, len(pivots) - 2):
        candidate, left, right = pivots[k], pivots[k - 2], pivots[k + 2]
        if is_senior(candidate, left, right):
            out.append(SeniorPivot(pivot=candidate, left=left, right=right))
    return out


def newest_senior(pivots: list[Pivot]) -> SeniorPivot | None:
    """The senior pivot the newest pivot just confirmed, if it confirmed one.

    For a tracker fed one pivot at a time: the pivot that has just landed is
    the right-hand neighbour of `pivots[-3]`, whose left neighbour is
    `pivots[-5]`. Fewer than five pivots means nothing can be confirmed yet.
    """
    if len(pivots) < 5:
        return None
    candidate, left, right = pivots[-3], pivots[-5], pivots[-1]
    if not is_senior(candidate, left, right):
        return None
    return SeniorPivot(pivot=candidate, left=left, right=right)
