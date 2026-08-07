"""Margin-zone state for a symbol: ZigZag pivots and the current [FMZ, IMZ] band.

Reports where price sits relative to the zone projected from the most recent
*confirmed* pivot. No condition is applied yet — this is the state a condition
will eventually be written against.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from backtester.analysis import build_envelopes, provisional, zigzag
from backtester.analysis.report import build_payload, report_name, write_report
from backtester.data.loader import load_bars
from backtester.data.margins import MarginLog, load_spec
from backtester.utils.params import StrategyParams

from ..telegram import escape
from .base import SignalStrategy, bar_facts
from .registry import register


@dataclass
class MarginZonesParams(StrategyParams):
    contract: str = "6E"                        # CME contract supplying the margin
    deviation_pct: float = 2.0                  # ZigZag reversal threshold
    margin_log: str = "configs/margins.csv"     # dated maintenance-margin readings
    initial_ratio: float = 1.1                  # IMZ = FMZ * this, when IM is unknown


@register
class MarginZonesSignal(SignalStrategy):
    name = "margin_zones"
    params_class = MarginZonesParams
    description = "ZigZag pivots and the [FMZ, IMZ] margin zone projected from the latest"

    def evaluate(self, job, config) -> dict:
        p = self.p
        bars = load_bars(job.symbol, job.timeframe, data=config.data, validate=False)
        spec = load_spec(p.contract)
        log = MarginLog(Path(p.margin_log))

        pivots = zigzag(bars, deviation_pct=p.deviation_pct)
        envelopes = build_envelopes(bars, pivots, spec, log, p.initial_ratio, p.contract)
        prov = provisional(bars, deviation_pct=p.deviation_pct, pivots=pivots)

        facts = bar_facts(bars, job.timeframe)
        facts.update({
            "pivots": len(pivots),
            "zones": len(envelopes),
            "margin_readings": len({e.maintenance for e in envelopes}),
            "digits": max(2, len(str(spec.pip_size).split(".")[-1])),
        })
        if pivots:
            facts["last_pivot"] = pivots[-1]
        if envelopes:
            zone = envelopes[-1]
            facts["last_zone"] = zone
            # Where price stands relative to the live zone — the quantity a
            # condition will key on once one exists.
            facts["in_zone"] = zone.lo <= facts["last_close"] <= zone.hi
            facts["distance_to_zone"] = (
                0.0 if facts["in_zone"]
                else min(abs(facts["last_close"] - zone.lo), abs(facts["last_close"] - zone.hi))
                / spec.pip_size
            )
        if prov:
            facts["provisional"] = prov

        # Held for write_artifacts, so the report is rendered from the very
        # objects the message quoted rather than a second computation.
        self._state = (bars, pivots, envelopes, prov, spec)
        return facts

    def write_artifacts(self, job, config, facts: dict):
        state = getattr(self, "_state", None)
        if state is None:
            return None
        bars, pivots, envelopes, prov, spec = state
        deviation = f"{self.p.deviation_pct:g}%"
        payload = build_payload(
            job.symbol, job.timeframe, bars, pivots, envelopes, prov, spec,
            deviation, self.p.initial_ratio,
        )
        directory = Path("runs") / report_name(
            job.symbol, job.timeframe, spec.code, deviation
        )
        return write_report(
            directory, payload, bars, pivots, envelopes, spec, self.p.margin_log
        )

    def compose(self, job, facts: dict) -> str:
        d = facts["digits"]
        px = lambda v: f"{v:,.{d}f}"  # noqa: E731

        lines = [
            f"<b>{escape(job.symbol)} {escape(job.timeframe)}</b> — {escape(job.name)}",
            "",
            f"Last bar   <b>{px(facts['last_close'])}</b>  "
            f"at {facts['last_bar']:%Y-%m-%d %H:%M} UTC",
            f"Data       {facts['bars']:,} bars, "
            f"{facts['first_bar']:%Y-%m-%d} → {facts['last_bar']:%Y-%m-%d}",
        ]
        if facts["stale"]:
            lines.append(
                f"⚠ <b>Stale</b> — newest bar is {facts['age_hours']:.0f}h old; "
                f"treat the figures below with suspicion."
            )

        lines += ["", f"ZigZag     {facts['pivots']} confirmed pivots "
                      f"at {self.p.deviation_pct:g}% deviation"]
        pivot = facts.get("last_pivot")
        if pivot:
            lines.append(
                f"Last pivot {escape(pivot.kind)} {px(pivot.price)} "
                f"on {pivot.time:%Y-%m-%d}, confirmed +{pivot.lag_bars} bars"
            )
        zone = facts.get("last_zone")
        if zone:
            lines.append(
                f"Zone       {px(zone.lo)} – {px(zone.hi)}  "
                f"(FMZ {zone.fmz_pips:.0f} / IMZ {zone.imz_pips:.0f} pips, "
                f"MM {zone.maintenance:,.0f} as of {zone.margin_as_of})"
            )
            lines.append(
                "Price      <b>inside the zone</b>" if facts["in_zone"]
                else f"Price      {facts['distance_to_zone']:.0f} pips from the zone"
            )
        prov = facts.get("provisional")
        if prov:
            lines.append(
                f"In progress unconfirmed {escape(prov.kind)} {px(prov.price)}, "
                f"needs {px(prov.confirm_at)} to confirm"
            )

        lines += ["", "<i>No condition is configured yet — this is a scheduled heartbeat "
                      "proving the data path, not a trade signal.</i>"]
        return "\n".join(lines)
