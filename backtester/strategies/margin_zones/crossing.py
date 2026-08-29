"""True E50 crossings against a versioned provisional zone — §6 of the spec.

`impl-spec/Provisional_ZigZag_MZ50_Strategy_Spec.md` §6. Two consecutive valid
CFD rollover points, strictly either side of the signal level, both measured
against the *same* immutable zone version.

That last clause is the whole safeguard. The zone is anchored on a candidate
that moves, so without it a level sliding under a static price would register
as a crossing that price never made. When a new version appears the baseline is
dropped: an observation classified under the old level is never compared with
one classified under the new.

Direction follows the zone. A crossing *toward* the Margin Zone is the signal —
downward through the level from a high, upward from a low. The opposite
crossing, back out toward the extremum, is ignored (§6).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from ...core.types import Bar
from .provisional import ProvisionalZoneTracker, ZoneVersion
from .rollover import RolloverPoint, RolloverTracker

#: §6's continuity rule. `rollover_points` collapses a weekend into one skipped
#: day, so consecutive output points routinely span 2-3 calendar days without a
#: point being missing. Wider than this is a hole in the data, and no crossing
#: may be formed across it.
DEFAULT_MAX_GAP_DAYS = 3


@dataclass(frozen=True, slots=True)
class CrossingSignal:
    """A confirmed crossing of the signal level, toward the zone."""

    zone: ZoneVersion
    previous: RolloverPoint
    current: RolloverPoint
    index: int              # bar on which the pair completed

    @property
    def is_long(self) -> bool:
        return self.zone.direction > 0

    @property
    def signal_time(self) -> datetime:
        return self.current.roll_time

    @property
    def signal_price(self) -> float:
        """The observation that completed the crossing. Not the entry — §6 is
        explicit that the entry is the first executable price afterwards."""
        return self.current.price

    def take_profit(self) -> float:
        return self.zone.mz100

    def stop_for(self, entry: float) -> float:
        """§7: the stop mirrors the target distance about the price actually paid.

        Measured from the fill rather than from the signal, so the ratio is 1:1
        against what was really risked, whatever the open gapped to.
        """
        risk = abs(entry - self.zone.mz100)
        return entry - risk if self.is_long else entry + risk

    def as_row(self, status: str) -> dict:
        return {
            "zone_id": self.zone.zone_id,
            "candidate_leg_id": self.zone.leg,
            "candidate_version": self.zone.version,
            "direction": "LONG" if self.is_long else "SHORT",
            "previous_observation_time": self.previous.roll_time.isoformat(),
            "previous_observation_price": self.previous.price,
            "current_observation_time": self.current.roll_time.isoformat(),
            "current_observation_price": self.current.price,
            "signal_time": self.signal_time.isoformat(),
            "e50": self.zone.e50,
            "mz100": self.zone.mz100,
            "status": status,
        }


class CrossingTracker:
    """Bars in, crossing signals out. Never sees past the bar it is given."""

    def __init__(
        self,
        zones_for,
        pip_size: float,
        deviation_pct: float | None = 2.0,
        deviation_abs: float | None = None,
        rollover_hour: int = 0,
        rollover_tz: str = "UTC",
        max_gap_days: int = DEFAULT_MAX_GAP_DAYS,
    ):
        self.zones = ProvisionalZoneTracker(
            zones_for, pip_size, deviation_pct, deviation_abs
        )
        self._rollover = RolloverTracker(rollover_hour, rollover_tz)
        self._max_gap = max(0, int(max_gap_days))
        self._count = 0
        self._prev: RolloverPoint | None = None
        self._prev_zone_id: int | None = None

        self.signals: list[CrossingSignal] = []
        self.observations = 0

    @property
    def active(self) -> ZoneVersion | None:
        return self.zones.active

    @property
    def points(self) -> list[RolloverPoint]:
        return self._rollover.points

    def push(self, bar: Bar) -> list[CrossingSignal]:
        """Feed the next closed bar, in §5.2's order.

        Crossings are read first, against the zone as it stood *before* this bar
        was closed; only then does the bar update the ZigZag and possibly create
        a new version. Reversing the two would let a bar mint a zone and signal
        against it using its own already-past prices.
        """
        index = self._count
        self._count += 1

        fired: list[CrossingSignal] = []
        for point in self._rollover.push(bar):
            self.observations += 1
            signal = self._pair(point, index)
            if signal is not None:
                self.signals.append(signal)
                fired.append(signal)
            self._prev = point
            self._prev_zone_id = self.active.zone_id if self.active else None

        self.zones.push(bar)
        return fired

    def reset_baseline(self) -> None:
        """Forget the previous observation (§8: required after any trade exit)."""
        self._prev = None
        self._prev_zone_id = None

    def _pair(self, point: RolloverPoint, index: int) -> CrossingSignal | None:
        zone = self.active
        if zone is None or self._prev is None:
            return None
        # Both observations must belong to the same immutable version (§6).
        if self._prev_zone_id != zone.zone_id:
            return None
        if (point.day - self._prev.day).days > self._max_gap:
            return None
        # A zone cannot act before it existed (§5.1, invariant 2).
        if point.roll_time <= zone.known_time:
            return None

        before = self._prev.price - zone.e50
        after = point.price - zone.e50
        # Strictly opposite sides; sitting exactly on the level is neutral (§6).
        if before * after >= 0:
            return None

        moved_down = point.price < self._prev.price
        toward_zone = zone.direction < 0
        if moved_down != toward_zone:
            return None                     # crossing back out, ignored (§6)

        return CrossingSignal(zone=zone, previous=self._prev, current=point, index=index)
