"""Margin Zones anchored on the ZigZag candidate, versioned as it moves.

Implements §3-§6 of `impl-spec/Provisional_ZigZag_MZ50_Strategy_Spec.md`.

A zone drawn from a *confirmed* pivot is knowable only weeks after the extreme
that defines it, and most of what it would have signalled has already happened
by then. A zone drawn from the *candidate* — the running extreme of the leg in
progress — is knowable immediately and uses nothing but closed bars. The catch
is that the candidate moves, and a level that slides through a price series
manufactures crossings on its own.

**Zone versions are the answer to that.** Every strict extension of the
candidate freezes a new, immutable zone: its own levels, its own `known_time`,
its own crossing baseline. A crossing is only real when both observations were
measured against the *same* version. Nothing is ever recalculated, so a level
that existed at some moment keeps the value it had then, and a version cannot
act on the bar that created it.

Note what is deliberately *not* a new version (§3.3): an equal high or low
moves the ZigZag's index to a later bar but changes no level, so it neither
supersedes the current zone nor counts as an adverse extension. Confirmation of
the pivot is likewise not an update of the candidate it confirms — it ends the
leg, and the next leg's first candidate is a new zone in its own right.

The level a signal is taken from is `e50`, the 50% Extremum-to-50% MZ level the
margin-zone specification already defines — `anchor ± (dFMZ + dIMZ) / 4`, which
sits between the extremum and the near boundary. The strategy specification
calls that level `MZ50` while defining it as the zone midpoint; this project
keeps its existing meaning, so the two documents differ on the name and on
nothing else. `mz50_midpoint` is carried alongside for reference.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Callable, Optional

from ...core.types import Bar
from ...indicators.zigzag import Candidate, Pivot, ZigZagTracker
from .margins import MarginZones

#: What produced a zone version, for the §12 record.
INITIAL = "initial"
STRICT_EXTENSION = "strict_extension"
ZONE_INPUT_CHANGE = "zone_input_change"

ZonesFor = Callable[[Candidate], Optional[MarginZones]]


@dataclass(frozen=True, slots=True)
class ZoneVersion:
    """One immutable Margin Zone, valid from the moment it became knowable.

    Frozen on purpose: §14's invariant 4 is that a historical zone level never
    changes, and the cheapest way to guarantee that is to make it impossible.
    A new anchor or a new margin reading produces a *new* version rather than
    editing this one, and a trade keeps a reference to the exact version that
    created it.
    """

    zone_id: int
    leg: int
    version: int
    kind: str                   # "high" | "low"
    anchor_price: float
    anchor_index: int
    anchor_time: datetime
    known_index: int            # bar whose close made this version knowable
    known_time: datetime
    zones: MarginZones          # the margin reading behind the distances
    pip_size: float
    event_type: str

    # ------------------------------------------------------------- geometry

    @property
    def direction(self) -> int:
        """Down from a high, up from a low."""
        return -1 if self.kind == "high" else 1

    @property
    def d_fmz(self) -> float:
        return self.zones.fmz * self.pip_size

    @property
    def d_imz(self) -> float:
        return self.zones.imz * self.pip_size

    @property
    def mz0(self) -> float:
        """The near boundary."""
        return self.anchor_price + self.direction * self.d_fmz

    @property
    def mz100(self) -> float:
        """The far boundary — the target."""
        return self.anchor_price + self.direction * self.d_imz

    @property
    def mz50_midpoint(self) -> float:
        """Halfway between the boundaries. Carried for reference, not traded."""
        return (self.mz0 + self.mz100) / 2.0

    @property
    def e50(self) -> float:
        """The signal level: halfway from the anchor to the zone's midpoint.

        `anchor ± (dFMZ + dIMZ) / 4`. This is the 50% Extremum-to-50% MZ level
        of the margin-zone specification, kept deliberately — see the module
        docstring on where the two documents diverge.
        """
        return (self.anchor_price + self.mz50_midpoint) / 2.0

    def beyond_target(self, price: float) -> bool:
        """Has price already reached or passed MZ100? Then there is no trade."""
        return price <= self.mz100 if self.direction < 0 else price >= self.mz100

    def as_row(self, invalidated: datetime | None = None) -> dict:
        return {
            "zone_id": self.zone_id,
            "candidate_leg_id": self.leg,
            "candidate_version": self.version,
            "candidate_kind": self.kind.upper(),
            "candidate_price": self.anchor_price,
            "candidate_time": self.anchor_time.isoformat(),
            "known_time": self.known_time.isoformat(),
            "mz0": self.mz0,
            "e50": self.e50,
            "mz50_midpoint": self.mz50_midpoint,
            "mz100": self.mz100,
            "d_fmz_pips": round(self.zones.fmz, 2),
            "d_imz_pips": round(self.zones.imz, 2),
            "maintenance": self.zones.maintenance,
            "margin_as_of": self.zones.as_of.isoformat(),
            "event_type": self.event_type,
            "valid_from_time": self.known_time.isoformat(),
            "invalidated_time": invalidated.isoformat() if invalidated else "",
        }


class ProvisionalZoneTracker:
    """Bars in, zone versions out. Sees nothing but closed bars.

    Feed each closed bar; `push` returns the version created by it, if any.
    Only the newest version is eligible for new entries — `active` — but every
    version is kept, because a trade outlives the anchor that created it and
    §12 wants the whole history, including candidates that never confirmed.
    """

    def __init__(
        self,
        zones_for: ZonesFor,
        pip_size: float,
        deviation_pct: float | None = 2.0,
        deviation_abs: float | None = None,
    ):
        if pip_size <= 0:
            raise ValueError("pip_size must be > 0")
        self._zigzag = ZigZagTracker(deviation_pct, deviation_abs)
        self._zones_for = zones_for
        self._pip_size = pip_size
        self._count = 0
        self._next_id = 1
        self._candidate: Candidate | None = None
        self._version = 0

        self.active: ZoneVersion | None = None
        self.versions: list[ZoneVersion] = []
        self.invalidated: dict[int, datetime] = {}
        self.uncovered: list[Candidate] = []   # no margin reading on that date

    @property
    def pivots(self) -> list[Pivot]:
        """Confirmed pivots. Diagnostics only — never an entry filter (§3.1)."""
        return self._zigzag.pivots

    @property
    def candidate(self) -> Candidate | None:
        return self._candidate

    def push(self, bar: Bar) -> ZoneVersion | None:
        """Feed the next closed bar; returns the zone version it created, if any.

        Called *after* the bar's crossing observations have been processed
        against the previously active version — §5.2's ordering, which stops a
        closed bar from creating a zone and then signalling inside itself.
        """
        index = self._count
        self._count += 1
        self._zigzag.push(bar)

        candidate = self._zigzag.candidate
        if candidate is None:                      # no search direction yet (§3.2)
            return None

        previous = self._candidate
        self._candidate = candidate

        event = self._event_for(candidate, previous)
        if event is None:
            return None

        zones = self._zones_for(candidate)
        if zones is None:
            self.uncovered.append(candidate)
            return None

        if event == STRICT_EXTENSION and previous is not None and candidate.leg == previous.leg:
            self._version += 1
        else:
            self._version = 1

        if self.active is not None:
            self.invalidated[self.active.zone_id] = bar.time

        version = ZoneVersion(
            zone_id=self._next_id,
            leg=candidate.leg,
            version=self._version,
            kind=candidate.kind,
            anchor_price=candidate.price,
            anchor_index=candidate.index,
            anchor_time=candidate.time,
            known_index=index,
            known_time=bar.time,
            zones=zones,
            pip_size=self._pip_size,
            event_type=event,
        )
        self._next_id += 1
        self.active = version
        self.versions.append(version)
        return version

    def _event_for(self, candidate: Candidate, previous: Candidate | None) -> str | None:
        """Why a new version is due, or None if nothing changed that matters."""
        if self.active is None or candidate.leg != self.active.leg:
            return INITIAL
        if candidate.extends(previous):
            return STRICT_EXTENSION
        # The anchor stands. A different margin reading still moves every level,
        # so it is a new version even though the candidate has not moved (§3.4).
        zones = self._zones_for(candidate)
        if zones is not None and zones.as_of != self.active.zones.as_of:
            return ZONE_INPUT_CHANGE
        return None
