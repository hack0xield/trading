"""ZigZag swing pivots.

A ZigZag pivot is an alternating local high and low, where each leg must move
at least `deviation` before the previous extreme counts as a turning point.

**The thing to understand about ZigZag is that it repaints.** The bar that
*holds* an extreme is not the bar on which you *know* it was an extreme — you
only know once price has retraced far enough to confirm it, which can be many
bars later. Drawing the line back to the extreme makes historical charts look
prophetic, and it is the single easiest way to build a strategy that cannot be
traded.

So every `Pivot` carries two indices:

* `index` — where the extreme price actually happened (use this to *draw*)
* `confirm_index` — where the reversal threshold was met, i.e. the first bar on
  which a strategy could have known (use this to *trade*)

Only confirmed pivots are ever returned. The swing still in progress at the end
of the data is deliberately not emitted, because it is exactly the pivot that
would change if one more bar arrived.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from ..core.types import Bar


@dataclass(frozen=True, slots=True)
class Pivot:
    index: int              # bar holding the extreme price
    time: datetime
    price: float
    kind: str               # "high" or "low"
    confirm_index: int      # first bar on which this pivot was knowable
    confirm_time: datetime

    @property
    def is_high(self) -> bool:
        return self.kind == "high"

    @property
    def direction(self) -> int:
        """Which way price is expected to travel away from this extreme.

        Up from a low, down from a high — the sign a margin zone is projected in.
        """
        return -1 if self.is_high else 1

    @property
    def lag_bars(self) -> int:
        """How many bars after the extreme the pivot became knowable."""
        return self.confirm_index - self.index


def zigzag(
    bars: list[Bar],
    deviation_pct: float | None = 0.5,
    deviation_abs: float | None = None,
) -> list[Pivot]:
    """Confirmed swing pivots, oldest first, alternating high/low.

    `deviation_pct` is the reversal size as a percentage of the extreme's own
    price; `deviation_abs` gives it in price units instead and takes priority.
    """
    if not bars:
        return []
    if deviation_abs is None and (deviation_pct is None or deviation_pct <= 0):
        raise ValueError("give a positive deviation_pct or deviation_abs")
    if deviation_abs is not None and deviation_abs <= 0:
        raise ValueError("deviation_abs must be > 0")

    def threshold(price: float) -> float:
        if deviation_abs is not None:
            return deviation_abs
        return abs(price) * deviation_pct / 100.0

    pivots: list[Pivot] = []
    direction = 0            # 0 unknown, +1 in an up leg, -1 in a down leg
    hi, hi_i = bars[0].high, 0
    lo, lo_i = bars[0].low, 0

    for i, bar in enumerate(bars):
        if direction >= 0 and bar.high >= hi:
            hi, hi_i = bar.high, i
        if direction <= 0 and bar.low <= lo:
            lo, lo_i = bar.low, i

        fell = direction != -1 and bar.low <= hi - threshold(hi)
        rose = direction != 1 and bar.high >= lo + threshold(lo)

        # A bar wide enough to trigger both ways resolves in favour of the
        # older extreme, so pivots always come out in chronological order.
        if fell and rose:
            fell, rose = (hi_i <= lo_i), not (hi_i <= lo_i)

        if fell:
            pivots.append(Pivot(hi_i, bars[hi_i].time, hi, "high", i, bar.time))
            direction = -1
            lo, lo_i = bar.low, i
        elif rose:
            pivots.append(Pivot(lo_i, bars[lo_i].time, lo, "low", i, bar.time))
            direction = 1
            hi, hi_i = bar.high, i

    return pivots


@dataclass(frozen=True, slots=True)
class Provisional:
    """The swing extreme currently in progress — **not** a pivot.

    A deliberately separate type from `Pivot`, so it cannot be passed to
    anything expecting a confirmed extreme. It is the running high (or low)
    since the last confirmed pivot; `confirm_at` is the price that would turn
    it into one. Until price gets there it can move further, or be erased by a
    new extreme, which is exactly why the engine never trades on it.

    It is worth *drawing*, though. Without it a chart appears to stop dead at
    the last confirmation — on EURUSD H4 at 2% that left three months of chart
    with no ZigZag on it, which reads as a broken plot rather than an honest one.
    """

    index: int
    time: datetime
    price: float
    kind: str               # "high" or "low" — the extreme it would become
    confirm_at: float       # price that would confirm it
    bars_since: int


def provisional(
    bars: list[Bar],
    deviation_pct: float | None = 0.5,
    deviation_abs: float | None = None,
    pivots: list[Pivot] | None = None,
) -> Provisional | None:
    """The unconfirmed extreme after the last confirmed pivot, if any."""
    if not bars:
        return None
    if pivots is None:
        pivots = zigzag(bars, deviation_pct, deviation_abs)
    if not pivots:
        return None

    last = pivots[-1]
    tail = bars[last.index:]
    if len(tail) < 2:
        return None

    # After a high the market is seeking a low, and vice versa.
    seeking_low = last.is_high
    best_i, best = last.index, tail[0].low if seeking_low else tail[0].high
    for offset, bar in enumerate(tail):
        value = bar.low if seeking_low else bar.high
        if (value <= best) if seeking_low else (value >= best):
            best, best_i = value, last.index + offset

    threshold = deviation_abs if deviation_abs is not None else abs(best) * deviation_pct / 100.0
    return Provisional(
        index=best_i,
        time=bars[best_i].time,
        price=best,
        kind="low" if seeking_low else "high",
        confirm_at=best + threshold if seeking_low else best - threshold,
        bars_since=len(bars) - 1 - best_i,
    )


def legs(pivots: list[Pivot]) -> list[tuple[Pivot, Pivot]]:
    """Consecutive pivot pairs — the ZigZag line segments."""
    return list(zip(pivots, pivots[1:]))


def swing_sizes(pivots: list[Pivot], as_pct: bool = False) -> list[float]:
    """Absolute (or percentage) size of each leg. Useful for tuning `deviation`."""
    out = []
    for a, b in legs(pivots):
        move = abs(b.price - a.price)
        out.append(move / a.price * 100.0 if as_pct and a.price else move)
    return out


def pivots_known_by(pivots: list[Pivot], index: int) -> list[Pivot]:
    """Only the pivots a strategy standing on `index` could already have seen.

    The whole point of `confirm_index`: filtering on `pivot.index <= i` would
    quietly hand the strategy an extreme that had not yet been confirmed.
    """
    return [p for p in pivots if p.confirm_index <= index]
