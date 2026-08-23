"""Senior H4 extremums and the 25% Control Zone level they project.

Implements `impl_spec/H4 Senior Extremum 25% Control Zone Approach Pattern.pdf`, which
sits on top of the machinery already here: ZigZag pivots from `indicators/`,
FMZ from `margins.py`. Three stages, in order:

1. **Senior extremum** (§3). A ZigZag high is *senior* when the nearest high on
   each side of it is lower: `H-1 < H0 > H+1`. Mirrored for lows. Neighbours of
   the same type are two positions away in the alternating pivot list, so `H+1`
   is `pivots[k+2]`, not `pivots[k+1]`.

2. **The 25% level** (§4). Not a level *inside* the zone: it is a quarter of
   the way from the extremum toward the zone's near boundary, which sits at the
   FMZ distance. So `Level25 = E ± 0.25 × FMZ` — for EUR/USD at a 2,400 USD
   maintenance margin, 48 pips from the extremum rather than 192.

3. **The approach** (§5-§7). The first candle after confirmation whose *range*
   intersects `Level25 ± 10% × Distance25`. Range, not close: an approach that
   happened intraday and was given back still happened.

**The confirmation lag is the whole difficulty.** `H0` is not senior until
`H+1` exists, and `H+1` is not knowable until price has retraced far enough to
confirm it. That is why the pattern is anchored to `right.confirm_time` rather
than to the extremum's own timestamp, and why approach scanning starts there —
§3.4's no-lookahead rule. `SeniorApproachTracker` is fed one closed bar at a
time precisely so this cannot be got wrong: it has never seen the future.

Nothing here places an order. `approach25.py` is the strategy that does.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from typing import Callable, Optional

from ...core.types import Bar
from ...indicators.zigzag import Pivot, ZigZagTracker
from .margins import MarginZones

#: §4.1 — how far along the extremum-to-zone distance the level sits.
LEVEL_FRACTION = 0.25
#: §5.2 — the approach tolerance, as a fraction of `Distance25` (not of price).
TOLERANCE_FRACTION = 0.10

#: Margin lookup for one pivot. Returns `None` when no reading was in force on
#: the extremum's own date, which is a pattern that cannot be built rather than
#: an excuse to reach for a later figure.
ZonesFor = Callable[[Pivot], Optional[MarginZones]]


@dataclass(frozen=True, slots=True)
class SeniorExtremum:
    """A confirmed senior extremum and every level §8 asks to be stored.

    Distances are derived rather than stored, so `level25`, `tolerance` and the
    approach bounds cannot drift apart from the FMZ they all come from.
    """

    pivot: Pivot            # H0 / L0 — the anchor
    left: Pivot             # H-1 / L-1
    right: Pivot            # H+1 / L+1 — its confirmation confirms this pattern
    zones: MarginZones      # margin in force on the extremum's own date
    pip_size: float
    level_fraction: float = LEVEL_FRACTION
    tolerance_fraction: float = TOLERANCE_FRACTION

    # ------------------------------------------------------------- the anchor

    @property
    def kind(self) -> str:
        return self.pivot.kind

    @property
    def label(self) -> str:
        """§8's `HIGH` / `LOW`."""
        return self.kind.upper()

    @property
    def price(self) -> float:
        """`E` — the senior extremum's own price."""
        return self.pivot.price

    @property
    def time(self) -> datetime:
        """§3.4's `ExtremumTime`."""
        return self.pivot.time

    @property
    def index(self) -> int:
        return self.pivot.index

    @property
    def confirm_time(self) -> datetime:
        """§3.4's `ConfirmationTime` — when `H+1` became knowable, not when it happened."""
        return self.right.confirm_time

    @property
    def confirm_index(self) -> int:
        return self.right.confirm_index

    @property
    def direction(self) -> int:
        """+1 if the zone lies above the extremum (a low), -1 if below (a high)."""
        return self.pivot.direction

    @property
    def margin_as_of(self) -> date:
        return self.zones.as_of

    # -------------------------------------------------------------- the zone

    @property
    def fmz(self) -> float:
        """Distance from the extremum to the zone's near boundary, in price units."""
        return self.zones.fmz * self.pip_size

    @property
    def imz(self) -> float:
        return self.zones.imz * self.pip_size

    @property
    def zone_near(self) -> float:
        """The near Control Zone boundary — the 100% of which the level is 25%."""
        return self.level_at(1.0)

    @property
    def zone_far(self) -> float:
        return self.price + self.direction * self.imz

    def level_at(self, fraction: float) -> float:
        """A price `fraction` of the way from the extremum toward the near boundary.

        `level_at(0)` is the extremum, `level_at(0.25)` the 25% level,
        `level_at(1)` the boundary itself. Deliberately the same arithmetic the
        25% level uses, so a strategy targeting the zone and this pattern's own
        level can never disagree about which direction "toward the zone" is.
        """
        return self.price + self.direction * fraction * self.fmz

    # ------------------------------------------------------------- the level

    @property
    def distance25(self) -> float:
        """`FMZ × 0.25` — positive, in price units (§4.1)."""
        return self.level_fraction * self.fmz

    @property
    def level25(self) -> float:
        """`E - Distance25` for a maximum, `E + Distance25` for a minimum (§4.2-4.3)."""
        return self.level_at(self.level_fraction)

    @property
    def tolerance(self) -> float:
        """§5.2. Geometry of the pattern, never a percentage of the market price."""
        return self.tolerance_fraction * self.distance25

    @property
    def lower(self) -> float:
        return self.level25 - self.tolerance

    @property
    def upper(self) -> float:
        return self.level25 + self.tolerance

    def intersects(self, bar: Bar) -> bool:
        """§6: `CandleHigh >= LowerBound AND CandleLow <= UpperBound`."""
        return bar.high >= self.lower and bar.low <= self.upper

    # -------------------------------------------------------------- reporting

    def as_row(self) -> dict:
        """The §8 record, minus the approach — `ApproachEvent.as_row` adds that."""
        return {
            "extremum_type": self.label,
            "extremum_price": self.price,
            "extremum_time": self.time.isoformat(),
            "confirmation_time": self.confirm_time.isoformat(),
            "confirmation_lag_bars": self.confirm_index - self.index,
            "fmz_pips": round(self.zones.fmz, 2),
            "imz_pips": round(self.zones.imz, 2),
            "maintenance": self.zones.maintenance,
            "margin_as_of": self.margin_as_of.isoformat(),
            "level25": self.level25,
            "distance25": self.distance25,
            "tolerance": self.tolerance,
            "lower_bound": self.lower,
            "upper_bound": self.upper,
            "zone_near": self.zone_near,
        }


@dataclass(frozen=True, slots=True)
class ApproachEvent:
    """The first candle to enter a pattern's approach range (§7).

    First is load-bearing: the pattern is retired once it fires, so a level
    price oscillates around does not generate an event per oscillation.
    """

    extremum: SeniorExtremum
    index: int
    time: datetime
    high: float
    low: float

    @property
    def bars_waited(self) -> int:
        """Bars between confirmation and the approach. 0 = the confirming bar itself."""
        return self.index - self.extremum.confirm_index

    @property
    def day(self) -> date:
        """§8's trading date — the bar's own date, in whatever timezone the bars carry.

        MT5 bars are broker-server time stored verbatim (see `CLAUDE.md`), so
        this is the broker's trading day, which is the one the chart shows.
        """
        return self.time.date()

    def as_row(self) -> dict:
        return {
            **self.extremum.as_row(),
            "approached": True,
            "approach_time": self.time.isoformat(),
            "approach_day": self.day.isoformat(),
            "approach_bars_waited": self.bars_waited,
            "approach_bar_high": self.high,
            "approach_bar_low": self.low,
        }


def _senior(candidate: Pivot, left: Pivot, right: Pivot) -> bool:
    """§3.2 / §3.3. Strict on both sides: an equal neighbour does not dominate."""
    if candidate.is_high:
        return left.price < candidate.price > right.price
    return left.price > candidate.price < right.price


class SeniorApproachTracker:
    """Bars in, approach events out — the pattern as a stream (§10).

    Built for the engine's `on_bar`, where the only bar you may look at is the
    one that just closed. Push each closed bar; get back the events it fired.
    Because the tracker is never handed the series, the no-lookahead rule holds
    by construction rather than by review.

    A pattern is pending from its confirmation until it approaches, expires
    (`max_wait_bars`) or the data runs out. Several can be pending at once —
    a senior high and a senior low commonly overlap — and each fires once.
    """

    def __init__(
        self,
        zones_for: ZonesFor,
        pip_size: float,
        deviation_pct: float | None = 1.0,
        deviation_abs: float | None = None,
        level_fraction: float = LEVEL_FRACTION,
        tolerance_fraction: float = TOLERANCE_FRACTION,
        max_wait_bars: int = 0,
    ):
        if pip_size <= 0:
            raise ValueError("pip_size must be > 0")
        if not 0 < level_fraction <= 1:
            raise ValueError("level_fraction must be in (0, 1] — 0.25 is the spec's 25%")
        if tolerance_fraction < 0:
            raise ValueError("tolerance_fraction must be >= 0")

        self._zigzag = ZigZagTracker(deviation_pct, deviation_abs)
        self._zones_for = zones_for
        self._pip_size = pip_size
        self._level_fraction = level_fraction
        self._tolerance_fraction = tolerance_fraction
        self._max_wait = max(0, int(max_wait_bars))
        self._count = 0

        self.extremums: list[SeniorExtremum] = []
        self.events: list[ApproachEvent] = []
        self.pending: list[SeniorExtremum] = []
        self.expired: list[SeniorExtremum] = []
        self.uncovered: list[Pivot] = []   # senior, but no margin reading in force

    @property
    def pivots(self) -> list[Pivot]:
        return self._zigzag.pivots

    @property
    def bars_seen(self) -> int:
        return self._count

    def push(self, bar: Bar) -> list[ApproachEvent]:
        """Feed the next closed bar. Returns the approach events it fired."""
        index = self._count
        self._count += 1

        for _ in self._zigzag.push(bar):
            confirmed = self._confirm_candidate()
            if confirmed is not None:
                self.extremums.append(confirmed)
                self.pending.append(confirmed)

        return self._scan(bar, index)

    # ------------------------------------------------------------- internals

    def _confirm_candidate(self) -> SeniorExtremum | None:
        """A pivot just landed; the candidate it might confirm is two back.

        Pivots alternate, so the newest pivot is the same-type right neighbour
        of `pivots[-3]`, and that candidate's left neighbour is `pivots[-5]`.
        Fewer than five pivots means there is nothing to confirm yet.
        """
        pivots = self._zigzag.pivots
        if len(pivots) < 5:
            return None

        candidate, left, right = pivots[-3], pivots[-5], pivots[-1]
        if not _senior(candidate, left, right):
            return None

        zones = self._zones_for(candidate)
        if zones is None:
            self.uncovered.append(candidate)
            return None

        return SeniorExtremum(
            pivot=candidate,
            left=left,
            right=right,
            zones=zones,
            pip_size=self._pip_size,
            level_fraction=self._level_fraction,
            tolerance_fraction=self._tolerance_fraction,
        )

    def _scan(self, bar: Bar, index: int) -> list[ApproachEvent]:
        """Check every pending pattern against this bar's range."""
        fired: list[ApproachEvent] = []
        still: list[SeniorExtremum] = []

        for extremum in self.pending:
            if extremum.intersects(bar):
                event = ApproachEvent(
                    extremum=extremum,
                    index=index,
                    time=bar.time,
                    high=bar.high,
                    low=bar.low,
                )
                self.events.append(event)
                fired.append(event)
            elif self._max_wait and index - extremum.confirm_index >= self._max_wait:
                self.expired.append(extremum)
            else:
                still.append(extremum)

        self.pending = still
        return fired


# --------------------------------------------------------------------- batch

def senior_extremums(
    pivots: list[Pivot],
    zones_for: ZonesFor,
    pip_size: float,
    level_fraction: float = LEVEL_FRACTION,
    tolerance_fraction: float = TOLERANCE_FRACTION,
) -> list[SeniorExtremum]:
    """Every senior extremum in a finished pivot list — for charts and analysis.

    Same rule as the tracker, in one pass over a list you already have. Use the
    tracker inside a strategy; use this to draw §9's levels afterwards.
    """
    out = []
    for k in range(2, len(pivots) - 2):
        candidate, left, right = pivots[k], pivots[k - 2], pivots[k + 2]
        if not _senior(candidate, left, right):
            continue
        zones = zones_for(candidate)
        if zones is None:
            continue
        out.append(
            SeniorExtremum(
                pivot=candidate,
                left=left,
                right=right,
                zones=zones,
                pip_size=pip_size,
                level_fraction=level_fraction,
                tolerance_fraction=tolerance_fraction,
            )
        )
    return out


def first_approach(extremum: SeniorExtremum, bars: list[Bar]) -> ApproachEvent | None:
    """The first bar at or after confirmation to enter the approach range (§7).

    Scanning starts at `confirm_index`, so a crossing that happened between the
    extremum and its confirmation — which for a senior maximum is close to
    certain, the level being just below the high — is correctly not an event.
    """
    for index in range(extremum.confirm_index, len(bars)):
        bar = bars[index]
        if extremum.intersects(bar):
            return ApproachEvent(
                extremum=extremum, index=index, time=bar.time, high=bar.high, low=bar.low
            )
    return None
