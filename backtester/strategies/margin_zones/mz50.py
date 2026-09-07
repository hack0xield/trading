"""Provisional ZigZag candidate -> MZ50 crossing -> MZ100.

Implements `impl-spec-old/Provisional_ZigZag_MZ50_Strategy_Spec.md`. One forward
pass: every level it records was knowable from closed bars at the moment it was
recorded, and nothing is redrawn afterwards.

The Margin Zone is anchored on the ZigZag *candidate*, so it is knowable
immediately; every strict extension of that candidate freezes a new immutable
version, and a crossing counts only when both rollover observations were
measured against the same one.

Entry is a True crossing of `e50` toward the zone: down through it from a high
(SHORT), up from a low (LONG). Target is `mz100`, the far boundary, and the stop
mirrors that distance about the actual fill, so reward and risk are 1:1 against
the price really paid. The specification names the entry level MZ50; that is the
50% Extremum-to-50% MZ level, `anchor +- (dFMZ + dIMZ) / 4`, which the code
calls `e50`. The band's own midpoint is recorded as `mz50` and is not traded.

`place_orders: false` runs the same pass with the trading rule switched off, so
the run draws the components and places nothing.

Two variants (§9, §10) run over identical data, versions and signals:

* `KEEP_OPEN` — the entry was a decision made on the information available, and
  a later extreme does not revisit it. Exits are target, stop, or end of data.
* `CLOSE_ON_CANDIDATE_UPDATE` — a strictly higher high (or lower low) on the
  *originating* leg invalidates the setup, and the position leaves at the first
  executable price afterwards.
"""

from __future__ import annotations

from dataclasses import dataclass

from ...core.context import BarOpen, Context
from ...core.strategy import Strategy
from ...core.types import Bar, ExitReason, Side, Trade
from ...utils.params import StrategyParams
from ..registry import register
from .crossing import DEFAULT_MAX_GAP_DAYS, Crossing, CrossingTracker
from .margins import DEFAULT_INITIAL_RATIO, MARGIN_LOG, MarginLog, compute_zones, load_spec
from .rollover import RolloverPoint
from .zones import STRICT_EXTENSION, ZoneVersion, zone_spans

KEEP_OPEN = "KEEP_OPEN"
CLOSE_ON_CANDIDATE_UPDATE = "CLOSE_ON_CANDIDATE_UPDATE"
VARIANTS = (KEEP_OPEN, CLOSE_ON_CANDIDATE_UPDATE)

#: Which boundary the trade targets. `mz0` is the near one.
NEAR = "mz0"
FAR = "mz100"
TAKE_PROFITS = (NEAR, FAR)

#: Reasons a scheduled market exit was raised.
TWO_CLOSE_RETURN = "e50_two_close_return"
CANDIDATE_UPDATE = "candidate_update"


@dataclass
class MZ50Params(StrategyParams):
    volume: float = 0.1

    # --------------------------------------------------------------- the zone
    contract: str = "6E"
    contracts_dir: str = ""
    margin_log: str = ""
    initial_ratio: float = DEFAULT_INITIAL_RATIO
    deviation_pct: float = 2.0        # retracement confirming a ZigZag pivot, % of price
    deviation_pips: float = 0.0       # the same threshold in pips; takes priority

    # ------------------------------------------------------- the observations
    rollover_hour: int = 0
    rollover_tz: str = "UTC"
    max_gap_days: int = DEFAULT_MAX_GAP_DAYS
    expect_timeframe: str = "H4"

    # ------------------------------------------------------------ the trading
    place_orders: bool = False        # false = draw the components, trade nothing
    take_profit: str = NEAR           # which boundary the trade aims at
    two_close_exit: bool = True       # leave on two adverse daily closes past e50
    variant: str = KEEP_OPEN


@register
class MZ50Strategy(Strategy):
    name = "mz50"
    params_class = MZ50Params
    description = "Provisional-candidate Margin Zone: MZ50 crossing to MZ100, 1:1"

    # ------------------------------------------------------------- lifecycle

    def on_start(self, ctx: Context) -> None:
        p = self.p
        if p.volume <= 0:
            raise ValueError("volume must be > 0")
        if p.variant not in VARIANTS:
            raise ValueError(f"variant must be one of {list(VARIANTS)}, got {p.variant!r}")
        if p.take_profit not in TAKE_PROFITS:
            raise ValueError(
                f"take_profit must be one of {list(TAKE_PROFITS)}, got {p.take_profit!r}"
            )

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

        self._deviation_abs = p.deviation_pips * self._spec.pip_size or None
        self.tracker = CrossingTracker(
            zones_for=self._zones_for,
            pip_size=self._spec.pip_size,
            deviation_pct=p.deviation_pct,
            deviation_abs=self._deviation_abs,
            rollover_hour=p.rollover_hour,
            rollover_tz=p.rollover_tz,
            max_gap_days=p.max_gap_days,
        )
        self._fed = 0
        self._max_gap = max(0, int(p.max_gap_days))
        self._symbol, self._timeframe = ctx.symbol, ctx.timeframe
        self._bars: list[Bar] = []

        self._open_signal: Crossing | None = None   # the live trade's origin
        self._pending: Crossing | None = None       # ordered, not yet filled
        self._entry_time = None                     # when the live trade filled
        self._exit_due: str | None = None           # a market exit, at the next open
        self._exit_taken: str | None = None         # the reason to record for it
        self._used_zones: set[int] = set()          # §9: one trade per version
        self._points_fed = 0
        self._signal_rows: list[dict] = []
        self._trade_rows: list[dict] = []
        self._warning_rows: list[dict] = []
        self._updates_while_open = 0
        self._first_update = None
        self._warning: RolloverPoint | None = None  # first adverse close, unconfirmed
        self._confirming: RolloverPoint | None = None
        self._warning_count = 0
        self._warning_resets = 0

    def _zones_for(self, candidate):
        """Margin as it stood on the candidate's own date."""
        observation = self._log.latest(self._code, on=candidate.time.date())
        if observation is None:
            return None
        return compute_zones(self._spec, observation, self.p.initial_ratio)

    # ------------------------------------------------------------ the bar loop

    def on_bar_open(self, ctx: Context, event: BarOpen) -> None:
        """Settle last bar's order, then run any scheduled exit.

        Both belong here: the engine fills pending orders at this open *before*
        this hook and walks the bar's range for stops and targets *after* it, so
        correcting the bracket here makes it exact for the first bar the
        position lives through — including one that opens and closes inside it.

        §7's scheduled exit leaves at the first executable price. Closing in
        `on_bar` would fill at the close of the very bar that made the decision
        knowable, which is not a price the strategy could have traded after
        learning of it. This open is.
        """
        self._settle(ctx)
        if self._exit_due and ctx.positions:
            # §8: a quote that satisfies both the bracket and the scheduled exit
            # is classified as the bracket, so an open already past TP or SL is
            # left for the engine to resolve this bar.
            if not self._open_resolves(ctx.positions[0], event.price):
                # The engine has no reason of its own for this; the record names
                # it, and `_exit_taken` carries that across to `on_trade`.
                self._exit_taken = self._exit_due
                ctx.close_all(ExitReason.STRATEGY)
            self._exit_due = None

    @staticmethod
    def _open_resolves(position, price: float) -> bool:
        """Is this open already at or beyond the position's own stop or target?

        The two sit on opposite sides of the entry, so each is tested its own
        way round.
        """
        if position.side is Side.SELL:
            return ((position.sl is not None and price >= position.sl)
                    or (position.tp is not None and price <= position.tp))
        return ((position.sl is not None and price <= position.sl)
                or (position.tp is not None and price >= position.tp))

    def on_bar(self, ctx: Context, bar: Bar) -> None:
        """§8's ordering: the open trade reads the daily close, then entries,
        then the bar updates the ZigZag and its zones."""
        index = len(ctx.history) - 1
        for pending in ctx.history[self._fed : index + 1]:
            before = self.tracker.active
            crossings = self.tracker.push(pending)
            after = self.tracker.active

            closes = self.tracker.points[self._points_fed :]
            self._points_fed = len(self.tracker.points)
            for close in closes:
                self._watch(ctx, close)

            for crossing in crossings:
                if crossing.index == index:
                    self._act_on(ctx, crossing)

            # A new version on this bar. If it strictly extends the leg the open
            # trade came from, variant B schedules the exit (§10).
            if after is not None and after is not before:
                self._on_new_version(ctx, after)
        self._fed = index + 1

    # ---------------------------------------------------- the two-close exit

    def _watch(self, ctx: Context, close: RolloverPoint) -> None:
        """§7: two consecutive daily closes back past the trade's own E50.

        Measured against the E50 the trade was taken on, never against whatever
        zone is active now.
        """
        signal = self._open_signal
        if signal is None or not self.p.two_close_exit or self._exit_due:
            return
        if self._entry_time is None or close.roll_time <= self._entry_time:
            return                       # the signal's own close cannot warn

        level = signal.zone.e50
        if close.price == level:
            return self._clear("on_level", close)
        adverse = close.price < level if signal.is_long else close.price > level
        if not adverse:
            return self._clear("zone_side", close)

        # A break in the daily sequence retires the standing warning; this close
        # can open a new pair but cannot confirm the old one.
        if self._warning is not None and (close.day - self._warning.day).days > self._max_gap:
            self._clear("sequence_break", close)

        if self._warning is None:
            self._warning = close
            self._warning_count += 1
            self._record_warning("warning", close)
            return

        self._confirming = close
        self._exit_due = TWO_CLOSE_RETURN
        self._record_warning("confirmed", close)

    def _clear(self, reason: str, close: RolloverPoint) -> None:
        if self._warning is None:
            return
        self._warning = None
        self._warning_resets += 1
        self._record_warning(f"reset_{reason}", close)

    def _record_warning(self, event: str, close: RolloverPoint) -> None:
        signal = self._open_signal
        self._warning_rows.append({
            "event": event,
            "origin_zone_id": signal.zone.zone_id,
            "session_day": close.day.isoformat(),
            "close_time": close.roll_time.isoformat(),
            "close_price": close.price,
            "origin_e50": signal.zone.e50,
        })

    def _settle(self, ctx: Context) -> None:
        """Reconcile the order placed last bar with what the broker did.

        It either filled — and becomes the trade a candidate update is measured
        against — or it was rejected, in which case holding on to it would block
        every later signal.
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
            stop = self._pending.stop_for(position.entry_price)
            if abs(position.entry_price - self._pending.zone.mz100) > 0:
                ctx.modify(position, sl=stop)
            self._open_signal, self._pending = self._pending, None
            self._entry_time = position.entry_time
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
        if self.p.variant == CLOSE_ON_CANDIDATE_UPDATE and self._exit_due is None:
            self._exit_due = CANDIDATE_UPDATE

    # -------------------------------------------------------------- the entry

    def _act_on(self, ctx: Context, crossing: Crossing) -> None:
        """Turn a True crossing into an order, or record why it was not taken."""
        if not self.p.place_orders or not crossing.toward_zone:
            return                       # §6: crossings back out are not signals
        status = self._reject(ctx, crossing)
        if status is None:
            ctx.order(
                Side.BUY if crossing.is_long else Side.SELL,
                volume=self.p.volume,
                sl=crossing.stop_for(ctx.bar.close),
                tp=self._target(crossing.zone),
                tag=tag_for(crossing),
            )
            self._pending = crossing
            self._used_zones.add(crossing.zone.zone_id)
            status = "entered"
        self._signal_rows.append(crossing.as_signal_row(status))

    def _target(self, zone: ZoneVersion) -> float:
        return zone.mz0 if self.p.take_profit == NEAR else zone.mz100

    def _reject(self, ctx: Context, crossing: Crossing) -> str | None:
        """§5/§9's reasons a signal produces no trade. None means take it."""
        if self._pending is not None or not ctx.is_flat:
            return "ignored_open_trade"
        if crossing.zone.zone_id in self._used_zones:
            return "zone_already_traded"
        # §5: no trade if price is already at or past the target. The fill is
        # the next open and unknown here, so this bar's close stands in for it.
        entry = ctx.bar.close
        target = self._target(crossing.zone)
        if crossing.zone.beyond(entry, target):
            return "entry_beyond_target"
        # §6: LONG needs SL < E < TP, SHORT needs TP < E < SL.
        stop = crossing.stop_for(entry)
        ordered = (
            stop < entry < target if crossing.is_long else target < entry < stop
        )
        if not ordered:
            return "invalid_trade_geometry"
        return None

    def on_trade(self, ctx: Context, trade: Trade) -> None:
        """§11: record the case, then require a fresh crossing."""
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
            "e50": signal.level,
            "origin_zone_id": signal.zone.zone_id,
            "origin_candidate_leg_id": signal.zone.leg,
            "origin_candidate_version": signal.zone.version,
            "origin_anchor_price": signal.zone.anchor_price,
            "origin_anchor_time": signal.zone.anchor_time.isoformat(),
            "origin_mz0": signal.zone.mz0,
            "origin_mz50": signal.zone.mz50,
            "origin_mz100": signal.zone.mz100,
            "signal_time": signal.time.isoformat(),
            "entry_time": trade.entry_time.isoformat(),
            "entry_price": trade.entry_price,
            "initial_tp": trade.tp,
            "initial_sl": trade.sl,
            "initial_risk": trade.risk,
            "initial_reward": (
                abs(trade.tp - trade.entry_price) if trade.tp is not None else ""
            ),
            "initial_rr": (
                round(trade.planned_rr, 4) if trade.planned_rr is not None else ""
            ),
            "exit_time": trade.exit_time.isoformat(),
            "exit_price": trade.exit_price,
            "exit_reason": (
                self._exit_taken
                if self._exit_taken and trade.reason is ExitReason.STRATEGY
                else trade.reason.value.lower()
            ),
            "pnl_currency": round(trade.net_pnl, 2),
            "r_multiple": round(trade.r_multiple, 3) if trade.r_multiple is not None else "",
            "mae": round(trade.mae, 6),
            "mfe": round(trade.mfe, 6),
            "bars_held": trade.bars_held,
            "candidate_updates_while_open": self._updates_while_open,
            "first_candidate_update_time": (
                self._first_update[0].isoformat() if self._first_update else ""
            ),
            "first_candidate_update_price": self._first_update[1] if self._first_update else "",
            "warning_count": self._warning_count,
            "warning_reset_count": self._warning_resets,
            "confirmed_warning_time": self._warning.roll_time.isoformat() if self._warning else "",
            "confirmed_warning_price": self._warning.price if self._warning else "",
            "confirming_close_time": (
                self._confirming.roll_time.isoformat() if self._confirming else ""
            ),
            "confirming_close_price": self._confirming.price if self._confirming else "",
            # §9: a confirmed exit still unfilled when the data ends is recorded
            # as pending, never as an executed early exit.
            "pending_exit_at_end": self._exit_due is not None,
            "execution_mode": "next_open",
            "ambiguous_tp_sl": bool(ambiguous),
        })
        # §9: a fresh crossing is required after any exit.
        self._open_signal = self._pending = None
        self._entry_time = None
        self._exit_due = self._exit_taken = None
        self._updates_while_open = 0
        self._first_update = None
        self._warning = self._confirming = None
        self._warning_count = self._warning_resets = 0
        self.tracker.reset_baseline()

    # ---------------------------------------------------------------- records

    def on_finish(self, ctx: Context) -> None:
        self._bars = list(ctx.history)
        z = self.tracker.zones
        true_count = sum(1 for c in self.tracker.crossings if c.toward_zone)
        ctx.log(
            f"mz50: {len(z.versions)} zone versions on {len(z.pivots)} confirmed pivots, "
            f"{len(self.tracker.points)} rollover points, {len(self.tracker.crossings)} "
            f"e50 crossings ({true_count} True)"
        )
        if z.uncovered:
            ctx.log(f"mz50: {len(z.uncovered)} candidate(s) had no margin reading and made no zone")
        if not self.p.place_orders:
            ctx.log("mz50: place_orders is false — support components only, no orders")
            return
        entered = sum(1 for r in self._signal_rows if r["status"] == "entered")
        exits = "two-close exit on" if self.p.two_close_exit else "two-close exit off"
        ctx.log(
            f"mz50[TP {self.p.take_profit}, {exits}, {self.p.variant}]: "
            f"{len(self._signal_rows)} signals, {entered} entered"
        )
        for status in sorted({r["status"] for r in self._signal_rows} - {"entered"}):
            n = sum(1 for r in self._signal_rows if r["status"] == status)
            ctx.log(f"mz50: {n} signal(s) not traded — {status}")

    def artifacts(self) -> dict[str, list[dict]]:
        """The run's own record of what it saw, bar by bar."""
        z = self.tracker.zones
        out: dict[str, list[dict]] = {}
        if z.versions:
            out["zones"] = [
                v.as_row(z.superseded.get(v.zone_id, (0, None))[1]) for v in z.versions
            ]
        if z.pivots:
            out["pivots"] = [
                {
                    "n": n, "kind": p.kind, "extreme_index": p.index,
                    "extreme_time": p.time.isoformat(), "price": p.price,
                    "confirm_index": p.confirm_index,
                    "confirm_time": p.confirm_time.isoformat(), "lag_bars": p.lag_bars,
                }
                for n, p in enumerate(z.pivots)
            ]
        if self.tracker.points:
            out["rollover"] = [
                {
                    "day": r.day.isoformat(), "roll_time": r.roll_time.isoformat(),
                    "price": round(r.price, 6), "bar_time": r.bar_time.isoformat(),
                }
                for r in self.tracker.points
            ]
        if self.tracker.crossings:
            out["crossings"] = [c.as_row() for c in self.tracker.crossings]
        if self._signal_rows:
            out["signals"] = self._signal_rows
        if self._trade_rows:
            out["cases"] = self._trade_rows
        if self._warning_rows:
            out["warnings"] = self._warning_rows
        return out

    # ------------------------------------------------------------- the chart

    def chart(self, run_dir, data_uri: str, timeframe: str):
        """The margin-zone chart, drawn from this run's own forward pass."""
        from .report import build_payload, read_metrics, read_trades, write_chart

        if not self._bars:
            return None
        p = self.p
        z = self.tracker.zones
        spans = zone_spans(z.versions, z.superseded, len(self._bars) - 1)
        payload = build_payload(
            symbol=self._symbol,
            timeframe=self._timeframe,
            bars=self._bars,
            pivots=z.pivots,
            spans=spans,
            spec=self._spec,
            deviation=(
                f"{p.deviation_pips:g} pips" if p.deviation_pips else f"{p.deviation_pct:g}%"
            ),
            initial_ratio=p.initial_ratio,
            candidate=z.candidate,
            confirm_at=self._confirm_at(z.candidate),
            rollover=self.tracker.points,
            crossings=self.tracker.crossings,
            trades=read_trades(run_dir, self._bars),
            backtest=read_metrics(run_dir),
            strategy=self._label(),
        )
        return write_chart(run_dir, payload)

    def _label(self) -> str:
        """Everything about this run that another run could differ by."""
        p = self.p
        if not p.place_orders:
            return f"{self.name} [no orders]"
        parts = [f"TP {p.take_profit}"]
        if p.two_close_exit:
            parts.append("two-close exit")
        parts.append(p.variant)
        return f"{self.name} [{', '.join(parts)}]"

    def _confirm_at(self, candidate) -> float | None:
        """The price that would turn the live candidate into a confirmed pivot."""
        if candidate is None:
            return None
        threshold = self._deviation_abs
        if threshold is None:
            threshold = abs(candidate.price) * self.p.deviation_pct / 100.0
        return candidate.price - threshold if candidate.is_high else candidate.price + threshold


def tag_for(crossing: Crossing) -> str:
    """Joins a trade back to the exact zone version that created it."""
    return f"mz50 z{crossing.zone.zone_id} {crossing.current.day}"
