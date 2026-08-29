"""Margin-zone state for a symbol: the live ZigZag candidate and its zone.

Reports where price sits relative to the Margin Zone anchored on the candidate,
plus the most recent rollover point and crossing. No condition is applied — this
is the state a condition will be written against.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from backtester.data.loader import load_bars
from backtester.strategies.margin_zones import (
    CrossingTracker,
    MarginLog,
    build_payload,
    load_spec,
    report_name,
    write_chart,
    zone_spans,
)
from backtester.utils.params import StrategyParams

from ..telegram import escape
from .base import SignalStrategy, bar_facts
from .registry import register


@dataclass
class MarginZonesParams(StrategyParams):
    contract: str = "6E"                        # CME contract supplying the margin
    deviation_pct: float = 2.0                  # ZigZag reversal threshold
    margin_log: str = "data/margins/margins.csv"     # dated maintenance-margin readings
    initial_ratio: float = 1.1                  # IMZ = FMZ * this, when IM is unknown
    rollover_hour: int = 0                      # hour, in rollover_tz, the break starts
    rollover_tz: str = "UTC"                    # MT5 bars are broker time labelled UTC,
                                                 # so "UTC" means broker midnight
    max_gap_days: int = 3                       # widest gap a crossing may span


@register
class MarginZonesSignal(SignalStrategy):
    name = "margin_zones"
    params_class = MarginZonesParams
    description = "The Margin Zone anchored on the live ZigZag candidate, and its crossings"

    def evaluate(self, job, config) -> dict:
        p = self.p
        bars = load_bars(job.symbol, job.timeframe, data=config.data, validate=False)
        spec = load_spec(p.contract)
        log = MarginLog(Path(p.margin_log))
        code = p.contract.upper()

        def zones_for(candidate):
            observation = log.latest(code, on=candidate.time.date())
            if observation is None:
                return None
            from backtester.strategies.margin_zones import compute_zones

            return compute_zones(spec, observation, p.initial_ratio)

        tracker = CrossingTracker(
            zones_for=zones_for,
            pip_size=spec.pip_size,
            deviation_pct=p.deviation_pct,
            rollover_hour=p.rollover_hour,
            rollover_tz=p.rollover_tz,
            max_gap_days=p.max_gap_days,
        )
        for bar in bars:
            tracker.push(bar)

        zones = tracker.zones
        facts = bar_facts(bars, job.timeframe)
        facts.update({
            "pivots": len(zones.pivots),
            "zones": len(zones.versions),
            "margin_readings": len({v.zones.maintenance for v in zones.versions}),
            "digits": max(2, len(str(spec.pip_size).split(".")[-1])),
            "rollover_points": len(tracker.points),
            "crossings": len(tracker.crossings),
        })
        if zones.pivots:
            facts["last_pivot"] = zones.pivots[-1]
        zone = zones.active
        if zone is not None:
            facts["last_zone"] = zone
            # Where price stands relative to the live zone — the quantity a
            # condition will key on once one exists.
            facts["in_zone"] = zone.lo <= facts["last_close"] <= zone.hi
            facts["distance_to_zone"] = (
                0.0 if facts["in_zone"]
                else min(abs(facts["last_close"] - zone.lo), abs(facts["last_close"] - zone.hi))
                / spec.pip_size
            )
        if zones.candidate is not None:
            facts["candidate"] = zones.candidate
        if tracker.points:
            facts["last_rollover"] = tracker.points[-1]
        # "Just happened": the newest rollover point is itself the second half
        # of a crossing pair, not merely that a crossing exists in the history.
        if tracker.crossings and tracker.points \
                and tracker.crossings[-1].current is tracker.points[-1]:
            facts["latest_crossing"] = tracker.crossings[-1]

        # Held for write_artifacts, so the report is rendered from the very
        # objects the message quoted rather than a second computation.
        self._state = (bars, spec, tracker)
        return facts

    def write_artifacts(self, job, config, facts: dict):
        state = getattr(self, "_state", None)
        if state is None:
            return None
        bars, spec, tracker = state
        zones = tracker.zones
        deviation = f"{self.p.deviation_pct:g}%"
        payload = build_payload(
            symbol=job.symbol,
            timeframe=job.timeframe,
            bars=bars,
            pivots=zones.pivots,
            spans=zone_spans(zones.versions, zones.superseded, len(bars) - 1),
            spec=spec,
            deviation=deviation,
            initial_ratio=self.p.initial_ratio,
            candidate=zones.candidate,
            rollover=tracker.points,
            crossings=tracker.crossings,
        )
        directory = Path("runs") / report_name(
            job.symbol, job.timeframe, spec.code, deviation
        )
        write_chart(directory, payload)
        return directory

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
        candidate = facts.get("candidate")
        if candidate:
            lines.append(
                f"Candidate  {escape(candidate.kind)} {px(candidate.price)} "
                f"on {candidate.time:%Y-%m-%d} — the anchor the zone hangs off"
            )
        zone = facts.get("last_zone")
        if zone:
            lines.append(
                f"Zone       {px(zone.lo)} – {px(zone.hi)}  "
                f"(FMZ {zone.zones.fmz:.0f} / IMZ {zone.zones.imz:.0f} pips, "
                f"MM {zone.zones.maintenance:,.0f} as of {zone.zones.as_of})"
            )
            lines.append(f"50% Ext-MZ {px(zone.e50)}")
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
        crossing = facts.get("latest_crossing")
        if crossing:
            lines.append(
                f"🔔 <b>{crossing.classification} crossing</b> — rollover moved "
                f"{crossing.direction} through the 50% Ext-MZ level ({px(crossing.zone.e50)}): "
                f"{crossing.previous.day} {px(crossing.previous.price)} → "
                f"{crossing.current.day} {px(crossing.current.price)}"
            )

        lines += ["", "<i>No condition is configured yet — this is a scheduled heartbeat "
                      "proving the data path, not a trade signal.</i>"]
        return "\n".join(lines)
