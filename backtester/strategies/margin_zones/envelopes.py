"""Margin-zone envelopes projected from swing pivots.

Step 5 of `Margin Zones.md`: draw the [FMZ, IMZ] band from every H4 extremum,
recalculating at each new pivot.

The band is projected *away* from the extreme — upward from a low, downward
from a high — because FMZ is the distance the exchange's own margin implies
price could travel against a position opened there. The near edge is FMZ, the
far edge IMZ, and the gap between them is MR.

Margin is read **as it stood on the pivot's own date**, never today's figure
applied backwards. CME margins move at contract roll and on volatility, so a
three-year chart drawn with one constant MM would be quietly wrong for most of
its width — and wrong in the flattering direction if margins have since risen.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from ...core.types import Bar
from .margins import (
    DEFAULT_INITIAL_RATIO,
    ContractSpec,
    MarginLog,
    compute_zones,
)
from ...indicators.zigzag import Pivot


@dataclass(frozen=True, slots=True)
class Envelope:
    """One [FMZ, IMZ] band, valid from its pivot until the next one."""

    pivot: Pivot
    start_index: int
    end_index: int
    direction: int        # +1 projected up from a low, -1 down from a high
    fmz_price: float      # near boundary, 0% MZ
    imz_price: float      # far boundary, 100% MZ
    fmz_pips: float
    imz_pips: float
    maintenance: float
    margin_as_of: date

    @property
    def mid_price(self) -> float:
        """50% MZ — exactly halfway between the boundaries (validation rule 4)."""
        return (self.fmz_price + self.imz_price) / 2.0

    @property
    def e50_price(self) -> float:
        """50% Extremum-to-50% MZ level (revised spec §3.5).

        Halfway between the pivot itself and `mid_price` — not a level inside
        the zone. Since `mid_price = pivot + step * (fmz+imz)/2 * pip_size`,
        averaging it with the pivot price is algebraically identical to the
        spec's `pivot ± step * (fmz+imz)/4 * pip_size`, so no separate pip
        figure is needed here.
        """
        return (self.pivot.price + self.mid_price) / 2.0

    def level(self, fraction: float) -> float:
        """A price inside the zone: 0% at FMZ, 1.0 at IMZ (§3.4)."""
        return self.fmz_price + (self.imz_price - self.fmz_price) * fraction

    @property
    def lo(self) -> float:
        return min(self.fmz_price, self.imz_price)

    @property
    def hi(self) -> float:
        return max(self.fmz_price, self.imz_price)

    @property
    def width(self) -> float:
        """MZ, in price units — the width of the zone, not a distance to it."""
        return abs(self.imz_price - self.fmz_price)

    def touched(self, bars: list[Bar]) -> bool:
        """Did price reach the near edge while this envelope was in force?"""
        for bar in bars[self.start_index : self.end_index + 1]:
            if bar.low <= self.hi and bar.high >= self.lo:
                return True
        return False

    def reached_far(self, bars: list[Bar]) -> bool:
        for bar in bars[self.start_index : self.end_index + 1]:
            if self.direction > 0 and bar.high >= self.imz_price:
                return True
            if self.direction < 0 and bar.low <= self.imz_price:
                return True
        return False


def build_envelopes(
    bars: list[Bar],
    pivots: list[Pivot],
    spec: ContractSpec,
    log: MarginLog,
    initial_ratio: float = DEFAULT_INITIAL_RATIO,
    code: str | None = None,
) -> list[Envelope]:
    """Project a [FMZ, IMZ] band from each pivot, out to the following pivot.

    Pivots with no margin reading on or before their date are skipped — see
    `margin_coverage` to find out how many, and from when.
    """
    code = (code or spec.code).upper()
    out: list[Envelope] = []
    last = len(bars) - 1

    for position, pivot in enumerate(pivots):
        observation = log.latest(code, on=pivot.time.date())
        if observation is None:
            continue

        zones = compute_zones(spec, observation, initial_ratio)
        step = pivot.direction
        end = pivots[position + 1].index if position + 1 < len(pivots) else last

        out.append(
            Envelope(
                pivot=pivot,
                start_index=pivot.index,
                end_index=min(end, last),
                direction=step,
                fmz_price=pivot.price + step * zones.fmz * spec.pip_size,
                imz_price=pivot.price + step * zones.imz * spec.pip_size,
                fmz_pips=zones.fmz,
                imz_pips=zones.imz,
                maintenance=observation.maintenance,
                margin_as_of=observation.as_of,
            )
        )
    return out


def margin_coverage(
    pivots: list[Pivot], log: MarginLog, code: str
) -> tuple[list[Pivot], list[Pivot]]:
    """Split pivots into those with a margin reading in force, and those without."""
    covered, missing = [], []
    for pivot in pivots:
        (covered if log.latest(code, on=pivot.time.date()) else missing).append(pivot)
    return covered, missing


def summarise(envelopes: list[Envelope], bars: list[Bar]) -> dict:
    """How often price actually reached the zones. Description, not a signal."""
    if not envelopes:
        return {"envelopes": 0}
    near = sum(1 for e in envelopes if e.touched(bars))
    far = sum(1 for e in envelopes if e.reached_far(bars))
    return {
        "envelopes": len(envelopes),
        "reached_fmz": near,
        "reached_fmz_pct": near / len(envelopes) * 100.0,
        "reached_imz": far,
        "reached_imz_pct": far / len(envelopes) * 100.0,
        "avg_fmz_pips": sum(e.fmz_pips for e in envelopes) / len(envelopes),
        "distinct_margins": len({e.maintenance for e in envelopes}),
    }
