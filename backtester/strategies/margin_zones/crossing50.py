"""Trade a True 50% crossing toward the Margin Zone.

Implements `impl-spec/Backtest Strategy.pdf`:

    True Crossing -> enter toward the Margin Zone -> TP at 100% MZ
    -> SL symmetric about entry -> R:R = 1:1

Unlike the senior-extremum pattern, this specification *does* define the trade,
so there is almost nothing here to parameterise. The signal is §5.5's crossing
event, which `rollover.py` already detects and `plot_zones.py` already draws:
two consecutive daily CFD rollover points, under the same active Margin Zone,
straddling the 50% Extremum-to-50% MZ level. Crossing toward the zone is True
and is the signal; crossing away is False and is not.

    a zone below a high  -> rollover falls through E50 -> SHORT
    a zone above a low   -> rollover rises through E50 -> LONG

Take profit is 100% MZ — the *far* boundary, IMZ, not the near one. Stop loss
is the same distance the other side of the fill, giving 1:1 by construction.

**Entry price.** §1 puts the entry at the second rollover point's price. That
price is a bar's close, and a close is not a tradeable moment: the earliest
honest fill is the next bar's open, which is what the engine gives. The stop is
therefore measured from the fill rather than from the signal price, so §3's
`SL_distance = TP_distance = abs(EntryPrice - TP)` holds against the price
actually paid. `signal_price` and `entry_price` are both recorded, so the gap
between them is visible rather than assumed away.

**Timeframe.** The zones and the ZigZag are H4. Rollover points are the last
price before the daily break, so the bars must be fine enough to land close to
it: on H4 with a midnight break the 20:00 bar closes exactly there, which is
why H4 is the default and finer data changes nothing. A rollover hour that does
not fall on the bar grid needs a finer series, and is warned about.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from ...core.context import Context
from ...core.strategy import Strategy
from ...core.types import Bar, ExitReason, Side
from ...data.results import write_rows
from ...utils.params import StrategyParams
from ..registry import register
from .crossing import DEFAULT_MAX_GAP_DAYS, CrossingSignal, CrossingTracker
from .margins import DEFAULT_INITIAL_RATIO, MARGIN_LOG, MarginLog, compute_zones, load_spec


@dataclass
class Crossing50Params(StrategyParams):
    volume: float = 0.1

    # ------------------------------------------------------------- the zones
    contract: str = "6E"              # CME code the margin is read from
    contracts_dir: str = ""           # default: configs/contracts
    margin_log: str = ""              # default: data/margins/margins.csv
    initial_ratio: float = DEFAULT_INITIAL_RATIO
    deviation_pct: float = 2.0        # retracement confirming a ZigZag pivot, % of price
    deviation_pips: float = 0.0       # the same threshold in pips; takes priority

    # ------------------------------------------------------------- the signal
    rollover_hour: int = 0            # hour, in rollover_tz, the daily break starts
    rollover_tz: str = "UTC"
    max_gap_days: int = DEFAULT_MAX_GAP_DAYS
    expect_timeframe: str = "H4"

    # ------------------------------------------------------------- the trading
    one_position: bool = True         # one crossing at a time
    max_hold_bars: int = 0            # time stop, in bars (0 = none)
    trade: bool = True                # false = detect only, place no orders

    # ------------------------------------------------------------- reporting
    signals_csv: str = ""             # write the §6 trade table here too


@register
class Crossing50Strategy(Strategy):
    name = "crossing50"
    params_class = Crossing50Params
    description = "Enter on a True 50% crossing, target 100% MZ, stop symmetric (1:1)"

    # ------------------------------------------------------------- lifecycle

    def on_start(self, ctx: Context) -> None:
        p = self.p
        if p.volume <= 0:
            raise ValueError("volume must be > 0")

        self._spec = load_spec(p.contract, p.contracts_dir or None)
        errors = [text for text in self._spec.problems() if text.startswith("ERROR")]
        if errors:
            raise ValueError(f"contract {p.contract}: " + "; ".join(errors))

        self._log = MarginLog(p.margin_log or MARGIN_LOG)
        self._code = p.contract.upper()
        if not self._log.for_code(self._code):
            raise ValueError(
                f"No margin readings for {self._code} in {self._log.path}. "
                f"Without one there is no Margin Zone and no E50 level to cross."
            )

        if p.expect_timeframe and ctx.timeframe.upper() != p.expect_timeframe.upper():
            ctx.log(
                f"WARN: the zones are specified on {p.expect_timeframe}, "
                f"running on {ctx.timeframe}"
            )

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
        self._pending: dict[int, CrossingSignal] = {}   # position id -> signal
        self._open: list[tuple[CrossingSignal, int]] = []
        self._rows: list[dict] = []
        self._skipped: list[str] = []
        self._symbol, self._timeframe = ctx.symbol, ctx.timeframe
        self._bars: list[Bar] = []

    def _zones_for(self, pivot):
        """Margin as it stood on the extremum's own date — never a later reading."""
        observation = self._log.latest(self._code, on=pivot.time.date())
        if observation is None:
            return None
        return compute_zones(self._spec, observation, self.p.initial_ratio)

    def on_bar(self, ctx: Context, bar: Bar) -> None:
        index = len(ctx.history) - 1
        for pending in ctx.history[self._fed : index + 1]:
            for signal in self.tracker.push(pending):
                # Only this bar's signals are tradeable; anything recovered
                # from skipped warmup bars has an entry bar in the past.
                if signal.index == index:
                    self._enter(ctx, signal)
        self._fed = index + 1

        self._apply_stops(ctx)

        if self.p.max_hold_bars:
            for position in ctx.positions:
                if position.bars_held >= self.p.max_hold_bars:
                    ctx.close(position, ExitReason.SESSION_END)

    def _enter(self, ctx: Context, signal: CrossingSignal) -> None:
        p = self.p
        if not p.trade:
            self._skipped.append("trade=false, detection only")
            return
        if p.one_position and not ctx.is_flat:
            self._skipped.append("another position was open")
            return
        if signal.risk <= 0:
            self._skipped.append("the rollover price sat on the 100% MZ boundary")
            return

        side = Side.BUY if signal.is_long else Side.SELL
        # The stop is placed off the signal price now and corrected to the fill
        # on the next bar. Placing it later would leave the first bar unstopped,
        # which is the one bar most likely to run against a fresh entry.
        ctx.order(
            side,
            volume=p.volume,
            sl=signal.stop_for(signal.entry),
            tp=signal.take_profit,
            tag=tag_for(signal),
        )
        self._pending[len(ctx.trades) + len(ctx.positions)] = signal

    def _apply_stops(self, ctx: Context) -> None:
        """Re-measure each stop from the price actually paid (§3).

        The order was placed at a bar's close and filled at the next open, so
        the fill is never exactly the rollover price the signal quoted. §3 says
        the stop is the take-profit distance mirrored about `EntryPrice`, and
        `EntryPrice` is what was paid.
        """
        known = {id(position) for position, _ in self._open}
        for position in ctx.positions:
            if id(position) in known:
                continue
            signal = self._signal_for(position.tag)
            if signal is None:
                continue
            ctx.modify(position, sl=signal.stop_for(position.entry_price))
            self._open.append((position, signal))
            self._rows.append({
                "instrument": self._symbol,
                **signal.as_row(),
                "signal_price": signal.entry,
                "entry_price": position.entry_price,
                "entry_timestamp": position.entry_time.isoformat(),
                "stop_loss": position.sl,
                "take_profit": position.tp,
                "risk_distance": abs(position.entry_price - signal.take_profit),
                "tag": position.tag,
            })

    def _signal_for(self, tag: str) -> CrossingSignal | None:
        for signal in reversed(self.tracker.signals):
            if tag_for(signal) == tag:
                return signal
        return None

    def on_finish(self, ctx: Context) -> None:
        tracker = self.tracker
        false_count = sum(1 for c in tracker.crossings if c.classification == "False")
        ctx.log(
            f"crossing50: {len(tracker.pivots)} pivots, {len(tracker.zones)} zones, "
            f"{len(tracker.points)} rollover points, {len(tracker.crossings)} crossings "
            f"({len(tracker.signals)} True, {false_count} False), "
            f"{len(self._rows)} entries, "
            f"{len(tracker.uncovered)} pivots without a margin reading"
        )
        for reason in sorted(set(self._skipped)):
            count = sum(1 for text in self._skipped if text == reason)
            ctx.log(f"crossing50: {count} signal(s) not traded — {reason}")

        self._bars = list(ctx.history)
        self._finish_rows(ctx)
        if self.p.signals_csv:
            path = Path(self.p.signals_csv)
            path.parent.mkdir(parents=True, exist_ok=True)
            write_rows(path, self._rows)
            ctx.log(f"crossing50: signal table written to {path}")

    def _finish_rows(self, ctx: Context) -> None:
        """Attach each trade's outcome to its signal row (§6)."""
        by_tag = {trade.tag: trade for trade in ctx.trades}
        for row in self._rows:
            trade = by_tag.get(row["tag"])
            if trade is None:
                continue
            won = trade.net_pnl > 0
            row.update({
                "exit_timestamp": trade.exit_time.isoformat(),
                "exit_price": trade.exit_price,
                "exit_reason": trade.reason.value,
                "result": "WIN" if won else "LOSS",
                # §6 asks for +1R / -1R. The stop and target are equidistant by
                # construction, so anything that is neither — a gap through a
                # level, or the run ending mid-trade — is reported as it landed
                # rather than rounded to a whole R it did not earn.
                "R_result": round(
                    (trade.exit_price - trade.entry_price)
                    * trade.side.sign
                    / row["risk_distance"], 3
                ) if row["risk_distance"] else 0.0,
                "net_pnl": round(trade.net_pnl, 2),
            })

    def chart(self, run_dir, data_uri: str, timeframe: str):
        """The margin-zones chart with this run's orders on it.

        No extra layer is needed: the zones chart already draws the daily
        rollover points and marks every crossing True or False, which is this
        strategy's entire signal. The orders simply land on top of it.

        The crossings it draws are the batch ones, anchored at each pivot's
        extreme, so it shows more of them than the run could trade — the
        difference is the confirmation lag, and `run.log` reports both counts.
        """
        from .report import write_zone_run

        if not self._bars or not self.tracker.pivots:
            return None
        p = self.p
        return write_zone_run(
            run_dir,
            symbol=self._symbol,
            timeframe=self._timeframe,
            bars=self._bars,
            pivots=self.tracker.pivots,
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
            strategy=self.name,
        )

    def artifacts(self) -> dict[str, list[dict]]:
        """The §6 trade table, saved beside trades.csv as `signals.csv`."""
        return {"signals": self._rows} if self._rows else {}


def tag_for(signal: CrossingSignal) -> str:
    """Joins a trade back to the crossing that produced it."""
    return f"crossing50 {signal.crossing.current.day} {signal.zone.pivot.kind}"
