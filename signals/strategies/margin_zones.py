"""Margin-zone state for a symbol: ZigZag pivots and the current [FMZ, IMZ] band.

Reports where price sits relative to the zone projected from the most recent
*confirmed* pivot. No condition is applied yet — this is the state a condition
will eventually be written against.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from backtester.indicators.zigzag import provisional, zigzag
from backtester.strategies.margin_zones import (
    build_envelopes,
    build_payload,
    report_name,
    rollover_points,
    write_report,
)
from backtester.data.loader import load_bars
from backtester.strategies.margin_zones import MarginLog, load_spec
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
    rollover_timeframe: str = "M15"             # bars sampled for the pre-break price
    rollover_hour: int = 0                      # hour, in rollover_tz, the break starts
    rollover_tz: str = "UTC"                    # MT5 bars are broker time labelled UTC,
                                                 # so "UTC" means broker midnight; the
                                                 # terminal has no API for the real
                                                 # schedule — see backtester/.../rollover.py


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

        rollover = []
        if p.rollover_timeframe == job.timeframe:
            rollover = rollover_points(bars, p.rollover_hour, p.rollover_tz)
        else:
            # A finer timeframe is nice-to-have for the rollover sample, but
            # not fetched for every symbol; a job must not fail its heartbeat
            # over it, so this is the one soft-fail spot in evaluate().
            try:
                roll_bars = load_bars(job.symbol, p.rollover_timeframe, data=config.data,
                                      validate=False)
            except ValueError:
                roll_bars = None
            if roll_bars is not None:
                rollover = rollover_points(roll_bars, p.rollover_hour, p.rollover_tz)

        facts = bar_facts(bars, job.timeframe)
        facts.update({
            "pivots": len(pivots),
            "zones": len(envelopes),
            "margin_readings": len({e.maintenance for e in envelopes}),
            "digits": max(2, len(str(spec.pip_size).split(".")[-1])),
            "rollover_points": len(rollover),
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
        if rollover:
            facts["last_rollover"] = rollover[-1]

        # Held for write_artifacts, so the report is rendered from the very
        # objects the message quoted rather than a second computation.
        self._state = (bars, pivots, envelopes, prov, spec, rollover)
        return facts

    def write_artifacts(self, job, config, facts: dict):
        state = getattr(self, "_state", None)
        if state is None:
            return None
        bars, pivots, envelopes, prov, spec, rollover = state
        deviation = f"{self.p.deviation_pct:g}%"
        payload = build_payload(
            job.symbol, job.timeframe, bars, pivots, envelopes, prov, spec,
            deviation, self.p.initial_ratio, rollover,
        )
        directory = Path("runs") / report_name(
            job.symbol, job.timeframe, spec.code, deviation
        )
        return write_report(
            directory, payload, bars, pivots, envelopes, spec, self.p.margin_log,
            rollover=rollover,
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
            lines.append(f"50% Ext-MZ {px(zone.e50_price)}")
            lines.append(
                "Price      <b>inside the zone</b>" if facts["in_zone"]
                else f"Price      {facts['distance_to_zone']:.0f} pips from the zone"
            )
        roll = facts.get("last_rollover")
        if roll:
            lines.append(
                f"Rollover   {px(roll.price)} on {roll.day} "
                f"({facts['rollover_points']} points, "
                f"break {self.p.rollover_hour:02d}:00 {self.p.rollover_tz})"
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
