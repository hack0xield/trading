"""Margin-zone observation run: ZigZag, zone versions and rollover crossings.

One forward pass over the bars. Every level it records was knowable from closed
bars at the moment it was recorded, and nothing is redrawn afterwards, so the
chart shows what a live run would have seen rather than what hindsight makes of
it.

Order placement is a hook that does nothing yet. With `place_orders: false` the
run is a drawing pass over the support components; with it true, `_act_on` is
where a trading rule goes.
"""

from __future__ import annotations

from dataclasses import dataclass

from ...core.context import Context
from ...core.strategy import Strategy
from ...core.types import Bar
from ...utils.params import StrategyParams
from ..registry import register
from .crossing import DEFAULT_MAX_GAP_DAYS, Crossing, CrossingTracker
from .margins import DEFAULT_INITIAL_RATIO, MARGIN_LOG, MarginLog, compute_zones, load_spec
from .zones import zone_spans


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

    # --------------------------------------------------------- the observations
    rollover_hour: int = 0
    rollover_tz: str = "UTC"
    max_gap_days: int = DEFAULT_MAX_GAP_DAYS
    expect_timeframe: str = "H4"

    # ------------------------------------------------------------ the trading
    place_orders: bool = False        # false = draw the components, trade nothing


@register
class MZ50Strategy(Strategy):
    name = "mz50"
    params_class = MZ50Params
    description = "Margin zones on the ZigZag candidate: zone versions and E50 crossings"

    # ------------------------------------------------------------- lifecycle

    def on_start(self, ctx: Context) -> None:
        p = self.p
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
        self._symbol, self._timeframe = ctx.symbol, ctx.timeframe
        self._bars: list[Bar] = []

    def _zones_for(self, candidate):
        """Margin as it stood on the candidate's own date."""
        observation = self._log.latest(self._code, on=candidate.time.date())
        if observation is None:
            return None
        return compute_zones(self._spec, observation, self.p.initial_ratio)

    # ------------------------------------------------------------ the bar loop

    def on_bar(self, ctx: Context, bar: Bar) -> None:
        index = len(ctx.history) - 1
        for pending in ctx.history[self._fed : index + 1]:
            for crossing in self.tracker.push(pending):
                self._act_on(ctx, crossing)
        self._fed = index + 1

    def _act_on(self, ctx: Context, crossing: Crossing) -> None:
        """Where a trading rule turns a crossing into an order.

        Does nothing while `place_orders` is false.
        """
        if not self.p.place_orders:
            return

    # ---------------------------------------------------------------- records

    def on_finish(self, ctx: Context) -> None:
        self._bars = list(ctx.history)
        z = self.tracker.zones
        true_count = sum(1 for c in self.tracker.crossings if c.toward_zone)
        ctx.log(
            f"mz50: {len(z.versions)} zone versions on {len(z.pivots)} confirmed pivots, "
            f"{len(self.tracker.points)} rollover points, {len(self.tracker.crossings)} "
            f"crossings ({true_count} True)"
        )
        if z.uncovered:
            ctx.log(f"mz50: {len(z.uncovered)} candidate(s) had no margin reading and made no zone")
        if not self.p.place_orders:
            ctx.log("mz50: place_orders is false — support components only, no orders")

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
            strategy=self.name,
        )
        return write_chart(run_dir, payload)

    def _confirm_at(self, candidate) -> float | None:
        """The price that would turn the live candidate into a confirmed pivot."""
        if candidate is None:
            return None
        threshold = self._deviation_abs
        if threshold is None:
            threshold = abs(candidate.price) * self.p.deviation_pct / 100.0
        return candidate.price - threshold if candidate.is_high else candidate.price + threshold
