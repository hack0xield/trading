"""Rollover crossings of a zone's E50 level.

Two consecutive rollover observations sitting strictly either side of `e50`,
both measured against the *same* immutable zone version. That last clause is
the safeguard: the anchor moves, so without it a level sliding under a static
price would register as a crossing price never made. When a new version appears
the baseline is dropped.

A crossing *toward* the zone — down through the level from a high, up from a
low — is classified True; the crossing back out toward the anchor is False.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from ...core.types import Bar
from .rollover import RolloverPoint, RolloverTracker
from .zones import ZoneTracker, ZoneVersion

#: Continuity limit in calendar days. A weekend collapses into one skipped day,
#: so consecutive points routinely span 2-3 days; wider than this is a hole in
#: the data and no crossing may be formed across it.
DEFAULT_MAX_GAP_DAYS = 3


@dataclass(frozen=True, slots=True)
class Crossing:
    """A rollover pair that moved through `e50`, and which way."""

    zone: ZoneVersion
    previous: RolloverPoint
    current: RolloverPoint
    index: int              # bar on which the pair completed

    @property
    def level(self) -> float:
        """The price the pair straddled."""
        return self.zone.e50

    def stop_for(self, entry: float) -> float:
        """The originating zone's stop, measured from the price actually paid."""
        return self.zone.stop_for(entry)

    @property
    def toward_zone(self) -> bool:
        """True crossing: the move went in the zone's own direction."""
        moved_down = self.current.price < self.previous.price
        return moved_down == (self.zone.direction < 0)

    @property
    def classification(self) -> str:
        return "True" if self.toward_zone else "False"

    @property
    def direction(self) -> str:
        return "down" if self.current.price < self.previous.price else "up"

    @property
    def is_long(self) -> bool:
        return self.zone.direction > 0

    @property
    def time(self) -> datetime:
        """When the pair completed — the second observation's instant."""
        return self.current.roll_time

    def as_row(self) -> dict:
        return {
            "zone_id": self.zone.zone_id,
            "candidate_leg_id": self.zone.leg,
            "candidate_version": self.zone.version,
            "candidate_kind": self.zone.kind.upper(),
            "candidate_price": self.zone.anchor_price,
            "zone_known_time": self.zone.known_time.isoformat(),
            "prev_day": self.previous.day.isoformat(),
            "prev_price": self.previous.price,
            "day": self.current.day.isoformat(),
            "price": self.current.price,
            "crossing_time": self.time.isoformat(),
            "e50": self.level,
            "mz50": self.zone.mz50,
            "mz100": self.zone.mz100,
            "direction": self.direction,
            "classification": self.classification,
        }

    def as_signal_row(self, status: str) -> dict:
        return {
            "zone_id": self.zone.zone_id,
            "candidate_leg_id": self.zone.leg,
            "candidate_version": self.zone.version,
            "direction": "LONG" if self.is_long else "SHORT",
            "previous_observation_time": self.previous.roll_time.isoformat(),
            "previous_observation_price": self.previous.price,
            "current_observation_time": self.current.roll_time.isoformat(),
            "current_observation_price": self.current.price,
            "signal_time": self.time.isoformat(),
            "e50": self.level,
            "mz100": self.zone.mz100,
            "status": status,
        }


class CrossingTracker:
    """Bars in, zone versions and crossings out. Never sees past the bar given.

    Owns the ZigZag, the zones and the rollover stream, so everything a
    margin-zone strategy observes comes from one forward pass.
    """

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
        self.zones = ZoneTracker(zones_for, pip_size, deviation_pct, deviation_abs)
        self.rollover = RolloverTracker(rollover_hour, rollover_tz)
        self._max_gap = max(0, int(max_gap_days))
        self._count = 0
        self._prev: RolloverPoint | None = None
        self._prev_zone_id: int | None = None

        self.crossings: list[Crossing] = []

    @property
    def active(self) -> ZoneVersion | None:
        return self.zones.active

    @property
    def points(self) -> list[RolloverPoint]:
        return self.rollover.points

    def push(self, bar: Bar) -> list[Crossing]:
        """Feed the next closed bar; return the crossings it completed.

        Crossings are read first, against the zone as it stood *before* this bar
        closed; only then does the bar update the ZigZag and possibly create a
        new version.
        """
        index = self._count
        self._count += 1

        fired: list[Crossing] = []
        for point in self.rollover.push(bar):
            crossing = self._pair(point, index)
            if crossing is not None:
                self.crossings.append(crossing)
                fired.append(crossing)
            self._prev = point
            self._prev_zone_id = self.active.zone_id if self.active else None

        self.zones.push(bar)
        return fired

    def reset_baseline(self) -> None:
        """Forget the previous observation, so no pair spans the reset."""
        self._prev = None
        self._prev_zone_id = None

    def _pair(self, point: RolloverPoint, index: int) -> Crossing | None:
        zone = self.active
        if zone is None or self._prev is None:
            return None
        # Both observations must belong to the same immutable version.
        if self._prev_zone_id != zone.zone_id:
            return None
        if (point.day - self._prev.day).days > self._max_gap:
            return None
        # A zone cannot act before it existed.
        if point.roll_time <= zone.known_time:
            return None

        level = zone.e50
        before = self._prev.price - level
        after = point.price - level
        # Strictly opposite sides; sitting exactly on the level is neutral.
        if before * after >= 0:
            return None

        return Crossing(zone=zone, previous=self._prev, current=point, index=index)
