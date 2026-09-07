"""Margin Zones anchored on the ZigZag candidate, versioned as the candidate moves.

A zone hangs off the *candidate* — the running extreme of the leg in progress —
so it is knowable from closed bars alone. The candidate moves, and a level that
slides through a price series would manufacture crossings on its own, so every
strict extension of it freezes a new immutable version carrying its own levels,
its own `known_time` and its own crossing baseline.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Callable, Optional

from ...core.types import Bar
from ...indicators.zigzag import Candidate, Pivot, ZigZagTracker
from .margins import MarginZones

#: What brought a zone version into existence.
INITIAL = "initial"
STRICT_EXTENSION = "strict_extension"
ZONE_INPUT_CHANGE = "zone_input_change"

ZonesFor = Callable[[Candidate], Optional[MarginZones]]


@dataclass(frozen=True, slots=True)
class ZoneVersion:
    """One immutable Margin Zone, valid from the moment it became knowable.

    Frozen: a level that existed at some moment keeps the value it had then. A
    new anchor or a new margin reading produces a new version instead.
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
        """Which way the zone is projected: down from a high, up from a low."""
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
        """The far boundary."""
        return self.anchor_price + self.direction * self.d_imz

    @property
    def mz50(self) -> float:
        """The zone's own midpoint, `anchor +- (dFMZ + dIMZ) / 2`.

        Inside the band, halfway between the boundaries.
        """
        return (self.mz0 + self.mz100) / 2.0

    @property
    def e50(self) -> float:
        """The signal level: 50% Extremum-to-50% MZ, `anchor +- (dFMZ + dIMZ) / 4`.

        Halfway from the anchor to `mz50`, so outside the band on the anchor's
        side. `Provisional_ZigZag_MZ50_Strategy_Spec.md` names this level MZ50.
        """
        return (self.anchor_price + self.mz50) / 2.0

    @property
    def lo(self) -> float:
        return min(self.mz0, self.mz100)

    @property
    def hi(self) -> float:
        return max(self.mz0, self.mz100)

    def beyond(self, price: float, level: float) -> bool:
        """Has price reached or passed `level`, travelling the zone's own way?"""
        return price <= level if self.direction < 0 else price >= level

    def as_row(self, until_time: datetime | None = None) -> dict:
        return {
            "zone_id": self.zone_id,
            "candidate_leg_id": self.leg,
            "candidate_version": self.version,
            "candidate_kind": self.kind.upper(),
            "candidate_price": self.anchor_price,
            "candidate_time": self.anchor_time.isoformat(),
            "known_time": self.known_time.isoformat(),
            "mz0": self.mz0,
            "mz50": self.mz50,
            "e50": self.e50,
            "mz100": self.mz100,
            "d_fmz_pips": round(self.zones.fmz, 2),
            "d_imz_pips": round(self.zones.imz, 2),
            "maintenance": self.zones.maintenance,
            "margin_as_of": self.zones.as_of.isoformat(),
            "event_type": self.event_type,
            "superseded_time": until_time.isoformat() if until_time else "",
        }


class ZoneTracker:
    """Bars in, zone versions out. Sees nothing but closed bars.

    Feed each closed bar; `push` returns the version that bar created, if any.
    Only the newest version — `active` — is current, and every version is kept.
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
        self.superseded: dict[int, tuple[int, datetime]] = {}
        self.uncovered: list[Candidate] = []   # no margin reading on that date

    @property
    def pivots(self) -> list[Pivot]:
        """Confirmed pivots, a by-product of the same ZigZag."""
        return self._zigzag.pivots

    @property
    def candidate(self) -> Candidate | None:
        return self._candidate

    def push(self, bar: Bar) -> ZoneVersion | None:
        """Feed the next closed bar; return the zone version it created, if any.

        Called after the bar's crossing observations have been read against the
        previously active version, so a bar cannot mint a zone and then signal
        against it using its own already-past prices.
        """
        index = self._count
        self._count += 1
        self._zigzag.push(bar)

        candidate = self._zigzag.candidate
        if candidate is None:                      # no search direction yet
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
            self.superseded[self.active.zone_id] = (index, bar.time)

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
        """Why a new version is due, or None if nothing that matters changed.

        An equal high or low moves the ZigZag's index to a later bar but changes
        no level, so it is not an extension.
        """
        if self.active is None or candidate.leg != self.active.leg:
            return INITIAL
        if candidate.extends(previous):
            return STRICT_EXTENSION
        # The anchor stands, but a different margin reading moves every level.
        zones = self._zones_for(candidate)
        if zones is not None and zones.as_of != self.active.zones.as_of:
            return ZONE_INPUT_CHANGE
        return None


def zone_spans(
    versions: list[ZoneVersion],
    superseded: dict[int, tuple[int, datetime]],
    last_index: int,
) -> list[tuple[ZoneVersion, int, int]]:
    """Each version with the bar range over which it was the active zone."""
    out = []
    for version in versions:
        end = superseded.get(version.zone_id, (last_index + 1, None))[0] - 1
        out.append((version, version.known_index, min(max(end, version.known_index), last_index)))
    return out


def summarise(spans: list[tuple[ZoneVersion, int, int]], bars: list[Bar]) -> dict:
    """How often price reached a zone while that zone was the active one."""
    if not spans:
        return {"zones": 0}
    near = far = 0
    for version, i0, i1 in spans:
        window = bars[i0 : i1 + 1]
        if any(b.low <= version.hi and b.high >= version.lo for b in window):
            near += 1
        if any(
            (b.high >= version.mz100 if version.direction > 0 else b.low <= version.mz100)
            for b in window
        ):
            far += 1
    n = len(spans)
    return {
        "zones": n,
        "reached_mz0": near,
        "reached_mz0_pct": near / n * 100.0,
        "reached_mz100": far,
        "reached_mz100_pct": far / n * 100.0,
        "avg_fmz_pips": sum(v.zones.fmz for v, _, _ in spans) / n,
        "distinct_margins": len({v.zones.maintenance for v, _, _ in spans}),
    }
