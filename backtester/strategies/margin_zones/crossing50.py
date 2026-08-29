"""Provisional ZigZag candidate -> E50 crossing -> MZ100.

Implements `impl-spec/Provisional_ZigZag_MZ50_Strategy_Spec.md`.

The Margin Zone is anchored on the ZigZag *candidate* — the running extreme of
the leg in progress — not on a confirmed pivot. The candidate is knowable from
closed bars alone, so nothing here reads the future; what it is not is *final*,
and the whole design follows from that. Every strict extension of the candidate
freezes a new immutable zone version, a crossing counts only when both
observations were measured against the same version, and a trade holds a
permanent reference to the version that created it.

Entry is a True crossing of `e50` toward the zone: down through it from a high
(SHORT), up through it from a low (LONG). Target is `MZ100`, the far boundary.
The stop mirrors the target distance about the fill, so reward and risk are 1:1
against the price actually paid.

Two variants, run over identical data and signals (§9, §10):

* `KEEP_OPEN` — the entry was a decision made on the information available, and
  a later extreme does not revisit it. Exits are target, stop, or end of data.
* `CLOSE_ON_CANDIDATE_UPDATE` — a strictly higher high (or lower low) on the
  *originating* leg invalidates the setup, and the position leaves at the first
  executable price afterwards.

Only the second behaviour differs between them. Entry counts before the first
candidate-update exit must match, and §16 treats any other difference as a bug
rather than a finding.

**Where this deviates from the document, deliberately.** §4 defines `MZ50` as
the midpoint between the boundaries; this project keeps `e50`, the 50%
Extremum-to-50% MZ level the margin-zone specification already defines, which
sits between the extremum and the near boundary. Same target, signal level
about twice as close to the anchor. The midpoint is still recorded on every
zone version as `mz50_midpoint`.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from ...core.context import BarOpen, Context
from ...core.strategy import Strategy
from ...core.types import Bar, ExitReason, Side, Trade
from ...data.results import write_rows
from ...utils.params import StrategyParams
from ..registry import register
from .crossing import DEFAULT_MAX_GAP_DAYS, CrossingSignal, CrossingTracker
from .margins import DEFAULT_INITIAL_RATIO, MARGIN_LOG, MarginLog, compute_zones, load_spec
from .provisional import STRICT_EXTENSION

KEEP_OPEN = "KEEP_OPEN"
CLOSE_ON_CANDIDATE_UPDATE = "CLOSE_ON_CANDIDATE_UPDATE"
VARIANTS = (KEEP_OPEN, CLOSE_ON_CANDIDATE_UPDATE)


@dataclass
class Crossing50Params(StrategyParams):
    volume: float = 0.1

    # --------------------------------------------------------------- the zone
    contract: str = "6E"
    contracts_dir: str = ""
    margin_log: str = ""
    initial_ratio: float = DEFAULT_INITIAL_RATIO
    deviation_pct: float = 2.0        # retracement confirming a ZigZag pivot, % of price
    deviation_pips: float = 0.0       # the same threshold in pips; takes priority

    # ------------------------------------------------------------- the signal
    rollover_hour: int = 0
    rollover_tz: str = "UTC"
    max_gap_days: int = DEFAULT_MAX_GAP_DAYS
    expect_timeframe: str = "H4"

    # ------------------------------------------------------------ the trading
    variant: str = KEEP_OPEN
    trade: bool = True                # false = detect only, still records signals

    # ----------------------------------------------------------- the records
    signals_csv: str = ""


@register
class Crossing50Strategy(Strategy):
    name = "crossing50"
    params_class = Crossing50Params
    description = "Provisional-candidate Margin Zone: E50 crossing to MZ100, 1:1"

    # ------------------------------------------------------------- lifecycle

    def on_start(self, ctx: Context) -> None:
        p = self.p
        if p.variant not in VARIANTS:
            raise ValueError(f"variant must be one of {list(VARIANTS)}, got {p.variant!r}")
        if p.volume <= 0:
            raise ValueError("volume must be > 0")

        self._spec = load_spec(p.contract, p.contracts_dir or None)
        errors = [t for t in self._spec.problems() if t.startswith("ERROR")]
        if errors:
            raise ValueError(f"contract {p.contract}: " + "; ".join(errors))

        self._log = MarginLog(p.margin_log or MARGIN_LOG)
        self._code = p.contract.upper()
        if not self._log.for_code(self._code):
            raise ValueError(
                f"No margin readings for {self._code} in {self._log.path}. "
                f"Without one there is no Margin Zone to anchor."
            )
        if p.expect_timeframe and ctx.timeframe.upper() != p.expect_timeframe.upper():
            ctx.log(f"WARN: specified on {p.expect_timeframe}, running on {ctx.timeframe}")

        self.tracker = CrossingTracker(
            zones_for=self._zones_for,
            pip_size=self._spec.pip_size,
            deviation_pct=p.deviation_pct,
            deviation_abs=p.deviation_pips * self._spec.pip_size or None,
            rollover_hour=p.rollover_hour,
            rollover_tz=p.rollover_tz,
            max_gap_days=p.max_gap_days,
        )
        self._fed = 0
        self._symbol, self._timeframe = ctx.symbol, ctx.timeframe
        self._bars: list[Bar] = []

        self._open_signal: CrossingSignal | None = None   # the live trade's origin
        self._pending: CrossingSignal | None = None       # ordered, not yet filled
        self._exit_due = False                            # variant B, next open
        self._forced = False                              # this exit was an update
        self._used_zones: set[int] = set()                # §8: one trade per version
        self._signal_rows: list[dict] = []
        self._trade_rows: list[dict] = []
        self._updates_while_open = 0
        self._first_update = None

    def _zones_for(self, candidate):
        """Margin as it stood on the candidate's own date."""
        observation = self._log.latest(self._code, on=candidate.time.date())
        if observation is None:
            return None
        return compute_zones(self._spec, observation, self.p.initial_ratio)

    # ------------------------------------------------------------ the bar loop

    def on_bar_open(self, ctx: Context, event: BarOpen) -> None:
        """Settle last bar's order, then run any scheduled exit.

        Both belong here rather than in `on_bar`: the engine fills pending
        orders at this open *before* this hook, and walks the bar's range for
        stops and targets *after* it. Correcting the bracket here means it is
        already exact for the first bar the position lives through, including a
        position that opens and closes inside that same bar.

        §10: the scheduled exit leaves at the first executable price.

        Closing in `on_bar` would fill at the close of the very bar that made
        the update known, which is not a price the strategy could have traded
        after learning of it. This open is.
        """
        self._settle(ctx)
        if self._exit_due and ctx.positions:
            # The engine has no BREAK-type reason for this; the record names it
            # `candidate_update` as §10 requires, and `_forced` carries that
            # across to `on_trade`.
            self._forced = True
            ctx.close_all(ExitReason.STRATEGY)
            self._exit_due = False

    def on_bar(self, ctx: Context, bar: Bar) -> None:
        index = len(ctx.history) - 1
        for pending in ctx.history[self._fed : index + 1]:
            before = self.tracker.active
            signals = self.tracker.push(pending)
            after = self.tracker.active

            for signal in signals:
                if signal.index == index:
                    self._consider(ctx, signal)

            # A new version on this bar. If it strictly extends the leg the open
            # trade came from, variant B schedules the exit (§10).
            if after is not None and after is not before:
                self._on_new_version(ctx, after)
        self._fed = index + 1

    def _settle(self, ctx: Context) -> None:
        """Reconcile the order placed last bar with what the broker did.

        It either filled — and becomes the trade whose origin a candidate
        update is measured against — or it was rejected, in which case holding
        on to it would block every later signal.
        """
        if self._pending is None:
            return
        if ctx.positions:
            # §7 measures the stop from the price actually paid, and invariant 7
            # requires the two distances to match exactly. The order carried a
            # stop measured from the signal bar's close, because an order placed
            # without one leaves its first bar unprotected; now that the fill is
            # known, re-measure so the bracket is truly symmetric.
            position = ctx.positions[0]
            ctx.modify(position, sl=self._pending.stop_for(position.entry_price))
            self._open_signal, self._pending = self._pending, None
        elif ctx.is_flat:
            self._pending = None

    def _on_new_version(self, ctx: Context, version) -> None:
        origin = self._open_signal
        if origin is None or version.event_type != STRICT_EXTENSION:
            return
        if version.leg != origin.zone.leg:
            return                       # a different leg is not this setup's update
        self._updates_while_open += 1
        if self._first_update is None:
            self._first_update = (version.known_time, version.anchor_price)
        if self.p.variant == CLOSE_ON_CANDIDATE_UPDATE:
            self._exit_due = True

    # -------------------------------------------------------------- the entry

    def _consider(self, ctx: Context, signal: CrossingSignal) -> None:
        p = self.p
        status = self._reject(ctx, signal)
        if status is None:
            side = Side.BUY if signal.is_long else Side.SELL
            entry_ref = ctx.bar.close
            ctx.order(
                side,
                volume=p.volume,
                sl=signal.stop_for(entry_ref),
                tp=signal.take_profit(),
                tag=tag_for(signal),
            )
            self._pending = signal
            self._used_zones.add(signal.zone.zone_id)
            status = "entered"
        self._signal_rows.append(signal.as_row(status))

    def _reject(self, ctx: Context, signal: CrossingSignal) -> str | None:
        """§6/§8's reasons a signal produces no trade. None means take it."""
        if not self.p.trade:
            return "detect_only"
        if self._pending is not None or not ctx.is_flat:
            return "ignored_open_trade"
        if signal.zone.zone_id in self._used_zones:
            return "zone_already_traded"
        # §6: no trade if price is already at or past the target. The fill is
        # the next open and unknown here, so this bar's close stands in for it.
        if signal.zone.beyond_target(ctx.bar.close):
            return "entry_beyond_target"
        return None

    def on_trade(self, ctx: Context, trade: Trade) -> None:
        """§7 and §11: fix the stop against the real fill, and record the case."""
        signal = self._open_signal or self._pending
        if signal is None:
            return
        ambiguous = (
            trade.sl is not None and trade.tp is not None and ctx.bar is not None
            and ctx.bar.low <= min(trade.sl, trade.tp)
            and ctx.bar.high >= max(trade.sl, trade.tp)
        )
        self._trade_rows.append({
            "trade_id": trade.id,
            "backtest_variant": self.p.variant,
            "instrument": self._symbol,
            "direction": "LONG" if signal.is_long else "SHORT",
            "origin_zone_id": signal.zone.zone_id,
            "origin_candidate_leg_id": signal.zone.leg,
            "origin_candidate_version": signal.zone.version,
            "origin_anchor_price": signal.zone.anchor_price,
            "origin_anchor_time": signal.zone.anchor_time.isoformat(),
            "signal_time": signal.signal_time.isoformat(),
            "entry_time": trade.entry_time.isoformat(),
            "entry_price": trade.entry_price,
            "initial_tp": trade.tp,
            "initial_sl": trade.sl,
            "initial_risk": trade.risk,
            "exit_time": trade.exit_time.isoformat(),
            "exit_price": trade.exit_price,
            "exit_reason": (
                "candidate_update"
                if self._forced and trade.reason is ExitReason.STRATEGY
                else trade.reason.value.lower()
            ),
            "pnl_currency": round(trade.net_pnl, 2),
            "r_multiple": round(trade.r_multiple, 3) if trade.r_multiple is not None else "",
            "mae": round(trade.mae, 6),
            "mfe": round(trade.mfe, 6),
            "bars_held": trade.bars_held,
            "candidate_updates_while_open": self._updates_while_open,
            "first_candidate_update_time": self._first_update[0].isoformat() if self._first_update else "",
            "first_candidate_update_price": self._first_update[1] if self._first_update else "",
            "ambiguous_tp_sl": bool(ambiguous),
        })
        # §8: a fresh crossing is required after any exit.
        self._open_signal = self._pending = None
        self._exit_due = self._forced = False
        self._updates_while_open = 0
        self._first_update = None
        self.tracker.reset_baseline()

    def on_finish(self, ctx: Context) -> None:
        self._bars = list(ctx.history)
        z = self.tracker.zones
        entered = sum(1 for r in self._signal_rows if r["status"] == "entered")
        ctx.log(
            f"crossing50[{self.p.variant}]: {len(z.versions)} zone versions from "
            f"{len(z.pivots)} confirmed pivots, {self.tracker.observations} rollover "
            f"observations, {len(self.tracker.signals)} crossings, {entered} entries"
        )
        for status in sorted({r["status"] for r in self._signal_rows} - {"entered"}):
            n = sum(1 for r in self._signal_rows if r["status"] == status)
            ctx.log(f"crossing50: {n} signal(s) not traded — {status}")
        if self.p.signals_csv:
            path = Path(self.p.signals_csv)
            path.parent.mkdir(parents=True, exist_ok=True)
            write_rows(path, self._signal_rows)

    # ------------------------------------------------------------- the chart

    def chart(self, run_dir, data_uri: str, timeframe: str):
        """The margin-zones chart, with this run's trades and their setups on it.

        The zones drawn from confirmed pivots are the familiar analysis picture
        and are kept, along with the daily rollover points that are this
        strategy's observation stream. What the strategy actually traded is a
        different thing — a zone anchored on the provisional candidate — so
        each traded setup is drawn as its own layer: a marker on the anchor and
        the band the trade was aiming through, from `e50` to `MZ100`.

        The crossing rings on the base chart come from the confirmed envelopes
        and will not line up one-for-one with the entries. That is the gap this
        strategy exists to close, not a drawing error.
        """
        from .report import write_zone_run

        if not self._bars or not self.tracker.zones.pivots:
            return None
        p = self.p
        return write_zone_run(
            run_dir,
            symbol=self._symbol,
            timeframe=self._timeframe,
            bars=self._bars,
            pivots=self.tracker.zones.pivots,
            spec=self._spec,
            log=self._log,
            initial_ratio=p.initial_ratio,
            code=self._code,
            deviation=(
                f"{p.deviation_pips:g} pips" if p.deviation_pips else f"{p.deviation_pct:g}%"
            ),
            deviation_pct=p.deviation_pct,
            deviation_abs=p.deviation_pips * self._spec.pip_size or None,
            rollover_hour=p.rollover_hour,
            rollover_tz=p.rollover_tz,
            levels=self._chart_levels(),
            strategy=f"{self.name} [{p.variant}]",
        )

    def _chart_levels(self) -> list[dict]:
        """One entry per traded setup: its anchor, its e50 and its target.

        Only the zone versions that produced a trade are drawn. There are 800-odd
        versions in a run — every strict extension of the candidate makes one —
        and drawing them all would bury the chart in levels that never traded.
        """
        from bisect import bisect_right

        from ...utils.timeutil import parse_dt

        times = [int(b.time.timestamp()) for b in self._bars]

        def index_at(stamp: str) -> int:
            return max(0, bisect_right(times, int(parse_dt(stamp).timestamp())) - 1)

        by_id = {v.zone_id: v for v in self.tracker.zones.versions}
        out = []
        for case in self._trade_rows:
            zone = by_id.get(case["origin_zone_id"])
            if zone is None:
                continue
            out.append({
                "kind": zone.kind.upper(),
                "iE": zone.anchor_index,
                "pE": round(zone.anchor_price, 6),
                "iC": index_at(case["signal_time"]),
                "lvl": round(zone.e50, 6),
                # The band is the move the trade was taken for: from the level
                # it entered on to the boundary it was aiming at.
                "lo": round(min(zone.e50, zone.mz100), 6),
                "hi": round(max(zone.e50, zone.mz100), 6),
                "iA": index_at(case["entry_time"]),
                "traded": True,
            })
        return out

    def artifacts(self) -> dict[str, list[dict]]:
        """§12's three records."""
        z = self.tracker.zones
        out = {}
        if z.versions:
            out["zones"] = [v.as_row(z.invalidated.get(v.zone_id)) for v in z.versions]
        if self._signal_rows:
            out["signals"] = self._signal_rows
        if self._trade_rows:
            out["cases"] = self._trade_rows
        return out


def tag_for(signal: CrossingSignal) -> str:
    """Joins a trade back to the exact zone version that created it."""
    return f"crossing50 z{signal.zone.zone_id} {signal.current.day}"
