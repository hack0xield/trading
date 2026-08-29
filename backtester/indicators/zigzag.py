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

`deviation` therefore does double duty: it decides how much swing structure
survives, and — being the distance price must travel before a pivot exists at
all — how late each one becomes knowable. On EUR/USD H4 the median lag is one
bar at 0.5% and twenty-two at 2%.

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
class Candidate:
    """The extreme a leg has reached so far — a pivot in waiting.

    Deliberately not a `Pivot`: it carries no confirmation, and the price may
    still move. `leg` counts search direction changes, so two candidates with
    the same `leg` are successive readings of the same developing extreme,
    and a change of `leg` means the previous one was resolved and a new search
    began in the other direction.
    """

    leg: int
    kind: str               # "high" or "low" — what it would become
    price: float
    index: int
    time: datetime

    @property
    def is_high(self) -> bool:
        return self.kind == "high"

    @property
    def direction(self) -> int:
        """Which way a Margin Zone is projected from it: down from a high."""
        return -1 if self.is_high else 1

    def extends(self, other: "Candidate | None") -> bool:
        """Is this a *strict* price extension of the same developing extreme?

        Equal extremes move the ZigZag's index to the later bar but are not
        extensions: nothing about the level they anchor has changed.
        """
        if other is None or other.leg != self.leg:
            return False
        return self.price > other.price if self.is_high else self.price < other.price


class ZigZagTracker:
    """`zigzag()` fed one bar at a time, for strategies running inside the engine.

    The batch function needs the whole series up front, which a strategy does
    not have — and recomputing it from the open of history on every bar is both
    quadratic and an invitation to accidentally read ahead. This is the same
    algorithm with its loop turned inside out: push each closed bar as it
    arrives and take back the pivots that bar confirmed.

    `push` returns at most one pivot, and it is returned on the bar that
    confirmed it, never on the bar that holds the extreme. Fed a whole series
    it ends up with exactly `zigzag(bars, ...)`.
    """

    __slots__ = (
        "pivots", "_deviation_pct", "_deviation_abs", "_direction",
        "_hi", "_hi_i", "_hi_time", "_lo", "_lo_i", "_lo_time", "_count", "_leg",
    )

    def __init__(
        self,
        deviation_pct: float | None = 0.5,
        deviation_abs: float | None = None,
    ):
        if deviation_abs is None and (deviation_pct is None or deviation_pct <= 0):
            raise ValueError("give a positive deviation_pct or deviation_abs")
        if deviation_abs is not None and deviation_abs <= 0:
            raise ValueError("deviation_abs must be > 0")
        self._deviation_pct = deviation_pct
        self._deviation_abs = deviation_abs
        self.pivots: list[Pivot] = []
        self._direction = 0
        self._hi = self._lo = 0.0
        self._hi_i = self._lo_i = 0
        self._hi_time = self._lo_time = None
        self._count = 0
        self._leg = 0

    def _threshold(self, price: float) -> float:
        if self._deviation_abs is not None:
            return self._deviation_abs
        return abs(price) * self._deviation_pct / 100.0

    @property
    def bars_seen(self) -> int:
        return self._count

    @property
    def candidate(self) -> "Candidate | None":
        """The running extreme of the leg in progress — the pivot it may become.

        This is knowable now, from closed bars only: it is the highest high (or
        lowest low) since the last confirmation. It is *not* a pivot, and it can
        still be extended or replaced, which is exactly why it is a separate
        type — but a strategy that waits for confirmation is discarding
        information it already holds.

        `None` before the first pivot, while the tracker has no search direction
        and is watching both ends at once.
        """
        if self._direction == 0:
            return None
        seeking_high = self._direction > 0
        return Candidate(
            leg=self._leg,
            kind="high" if seeking_high else "low",
            price=self._hi if seeking_high else self._lo,
            index=self._hi_i if seeking_high else self._lo_i,
            time=self._hi_time if seeking_high else self._lo_time,
        )

    def push(self, bar: Bar) -> list[Pivot]:
        """Feed the next *closed* bar. Returns the pivots it confirmed (0 or 1)."""
        i = self._count
        self._count += 1
        if i == 0:
            self._hi, self._hi_i, self._hi_time = bar.high, 0, bar.time
            self._lo, self._lo_i, self._lo_time = bar.low, 0, bar.time

        if self._direction >= 0 and bar.high >= self._hi:
            self._hi, self._hi_i, self._hi_time = bar.high, i, bar.time
        if self._direction <= 0 and bar.low <= self._lo:
            self._lo, self._lo_i, self._lo_time = bar.low, i, bar.time

        fell = self._direction != -1 and bar.low <= self._hi - self._threshold(self._hi)
        rose = self._direction != 1 and bar.high >= self._lo + self._threshold(self._lo)

        # Same tie-break as the batch version: the older extreme wins, so
        # pivots stay in chronological order.
        if fell and rose:
            fell, rose = (self._hi_i <= self._lo_i), not (self._hi_i <= self._lo_i)

        if fell:
            pivot = Pivot(self._hi_i, self._hi_time, self._hi, "high", i, bar.time)
            self._direction = -1
            self._leg += 1
            self._lo, self._lo_i, self._lo_time = bar.low, i, bar.time
        elif rose:
            pivot = Pivot(self._lo_i, self._lo_time, self._lo, "low", i, bar.time)
            self._direction = 1
            self._leg += 1
            self._hi, self._hi_i, self._hi_time = bar.high, i, bar.time
        else:
            return []

        self.pivots.append(pivot)
        return [pivot]


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
