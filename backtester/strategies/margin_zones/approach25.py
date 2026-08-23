"""Trade the 25% Control Zone approach off a senior H4 extremum.

`senior.py` implements the pattern from `impl-spec/H4 Senior Extremum 25%
Control Zone Approach Pattern.pdf` and stops where the specification stops: at
the approach event. This file is the part the specification does not cover — what to do
about one — so every trading rule here is a parameter with a stated default,
not a claim the document makes.

**The default reading.** A senior maximum's Control Zone lies below it, so the
25% level sits between the two, a quarter of the way down. Price reaching that
level from below is the market returning to the senior high; the margin-zone
thesis is that it then travels the rest of the way to the zone. So the default
`bias=zone` sells that approach with the stop above the extremum and the target
at the near boundary — `0.25 → 1.0` of FMZ, against a risk of `0.25` plus the
buffer, which is where the roughly 3:1 shape of the default comes from.
Mirrored for a senior minimum. `bias=extremum` takes the opposite view and
`bias=none` places no orders at all, which turns the strategy into a pure
event study you can still run through `run_backtest.py` for the event table.

**Timing.** Detection happens on a closed bar, so the fill is the *next* bar's
open — the engine enforces that, and it is why the approach range is checked
against the candle range (§6) rather than its close. An approach bar that
already closed beyond where the stop would go places no order: the trade would
open and stop out on the same bar, which flatters nothing and costs a spread.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from ...core.context import Context
from ...core.strategy import Strategy
from ...core.types import Bar, ExitReason, Side
from ...data.results import write_rows
from ...utils.params import StrategyParams
from ...utils.timeutil import parse_dt
from ..registry import register
from .envelopes import build_envelopes
from .margins import DEFAULT_INITIAL_RATIO, MARGIN_LOG, MarginLog, compute_zones, load_spec
from .senior import LEVEL_FRACTION, TOLERANCE_FRACTION, SeniorApproachTracker, SeniorExtremum

BIASES = ("zone", "extremum", "none")
STOP_MODES = ("extremum", "distance", "pct", "none")
TARGET_MODES = ("fraction", "rr", "pct", "none")


@dataclass
class Senior25Params(StrategyParams):
    volume: float = 0.1

    # ------------------------------------------------------------- the pattern
    contract: str = "6E"              # CME code the margin is read from
    contracts_dir: str = ""           # default: configs/contracts
    margin_log: str = ""              # default: data/margins/margins.csv
    initial_ratio: float = DEFAULT_INITIAL_RATIO
    deviation_pct: float = 1.0        # ZigZag reversal, percent of price
    deviation_pips: float = 0.0       # in pips instead; overrides deviation_pct
    level_fraction: float = LEVEL_FRACTION        # §4.1 — the "25%"
    tolerance_fraction: float = TOLERANCE_FRACTION  # §5.2 — the "10%"
    max_wait_bars: int = 0            # drop a pattern never approached (0 = never)
    expect_timeframe: str = "H4"      # warn if the run is on something else

    # ------------------------------------------------------------- the trading
    bias: str = "zone"                # zone | extremum | none
    stop_mode: str = "extremum"       # extremum | distance | pct | none
    stop_buffer: float = 0.10         # multiples of Distance25, beyond the extremum
    stop_distance: float = 1.0        # multiples of Distance25, for stop_mode=distance
    stop_pct: float = 1.0             # percent of the fill, for stop_mode=pct
    target_mode: str = "fraction"     # fraction | rr | pct | none
    target_fraction: float = 1.0      # of FMZ from the extremum; 1.0 = the boundary
    target_rr: float = 2.0
    take_pct: float = 2.0
    one_position: bool = True         # one pattern at a time
    max_hold_bars: int = 0            # time stop, in bars (0 = none)

    # ------------------------------------------------------------- the chart
    rollover_timeframe: str = "M15"   # bars the daily rollover price is read from
    rollover_hour: int = 0            # hour, in rollover_tz, the daily break starts
    rollover_tz: str = "UTC"

    # ------------------------------------------------------------- the reporting
    events_csv: str = ""              # write the §8 event table here


@register
class Senior25Strategy(Strategy):
    name = "senior25"
    params_class = Senior25Params
    description = "Trade the first approach to a senior H4 extremum's 25% Control Zone level"

    # ------------------------------------------------------------- lifecycle

    def on_start(self, ctx: Context) -> None:
        p = self.p
        for field, value, allowed in (
            ("bias", p.bias, BIASES),
            ("stop_mode", p.stop_mode, STOP_MODES),
            ("target_mode", p.target_mode, TARGET_MODES),
        ):
            if value not in allowed:
                raise ValueError(f"{field} must be one of {list(allowed)}, got {value!r}")

        # A stop beyond the extremum is only a stop when the trade runs away
        # from it. Trading back toward the extremum, that price is the target.
        if p.bias == "extremum" and p.stop_mode == "extremum":
            raise ValueError(
                "stop_mode=extremum puts the stop where bias=extremum aims; "
                "use stop_mode=distance or stop_mode=pct instead"
            )
        if p.target_mode == "fraction":
            if p.bias == "zone" and p.target_fraction <= p.level_fraction:
                raise ValueError(
                    f"target_fraction ({p.target_fraction}) must be beyond level_fraction "
                    f"({p.level_fraction}) when trading toward the zone"
                )
            if p.bias == "extremum" and p.target_fraction >= p.level_fraction:
                raise ValueError(
                    f"target_fraction ({p.target_fraction}) must be inside level_fraction "
                    f"({p.level_fraction}) when trading toward the extremum"
                )
        if p.target_mode == "rr" and p.stop_mode == "none":
            raise ValueError("target_mode=rr needs a stop to measure risk against")
        if p.volume <= 0:
            raise ValueError("volume must be > 0")

        self._spec = load_spec(p.contract, p.contracts_dir or None)
        problems = self._spec.problems()
        errors = [text for text in problems if text.startswith("ERROR")]
        if errors:
            raise ValueError(f"contract {p.contract}: " + "; ".join(errors))
        for text in problems:
            ctx.log(f"contract {p.contract}: {text}")

        self._log = MarginLog(p.margin_log or MARGIN_LOG)
        self._code = p.contract.upper()
        if not self._log.for_code(self._code):
            raise ValueError(
                f"No margin readings for {self._code} in {self._log.path}. "
                f"The pattern cannot build an FMZ without one."
            )

        if p.expect_timeframe and ctx.timeframe.upper() != p.expect_timeframe.upper():
            ctx.log(
                f"WARN: the pattern is specified on {p.expect_timeframe}, "
                f"running on {ctx.timeframe}"
            )

        self.tracker = SeniorApproachTracker(
            zones_for=self._zones_for,
            pip_size=self._spec.pip_size,
            deviation_pct=p.deviation_pct,
            deviation_abs=p.deviation_pips * self._spec.pip_size or None,
            level_fraction=p.level_fraction,
            tolerance_fraction=p.tolerance_fraction,
            max_wait_bars=p.max_wait_bars,
        )
        self._fed = 0
        self._skipped: dict[int, str] = {}   # id(extremum) -> why no order
        self._traded: set[int] = set()
        self._events: list[dict] = []
        self._bars: list[Bar] = []
        self._symbol = ctx.symbol
        self._timeframe = ctx.timeframe


    def _zones_for(self, pivot):
        """FMZ as it stood on the extremum's own date — never a later reading.

        The same rule `build_envelopes` follows. It is not lookahead: the figure
        is already history by the time the pattern confirms, which is strictly
        later than the extremum itself.
        """
        observation = self._log.latest(self._code, on=pivot.time.date())
        if observation is None:
            return None
        return compute_zones(self._spec, observation, self.p.initial_ratio)

    def on_bar(self, ctx: Context, bar: Bar) -> None:
        # Feed from history rather than from `bar`, so a run with `warmup_bars`
        # set still sees a complete ZigZag instead of one that starts late.
        index = len(ctx.history) - 1
        for pending in ctx.history[self._fed : index + 1]:
            events = self.tracker.push(pending)
            for event in events:
                # Events recovered from skipped warmup bars are recorded but
                # not traded: their entry bar is already in the past.
                if event.index == index:
                    self._enter(ctx, event.extremum)
        self._fed = index + 1

        if self.p.max_hold_bars:
            for position in ctx.positions:
                if position.bars_held >= self.p.max_hold_bars:
                    ctx.close(position, ExitReason.SESSION_END)

    def on_finish(self, ctx: Context) -> None:
        tracker = self.tracker
        ctx.log(
            f"senior25: {len(tracker.pivots)} pivots, {len(tracker.extremums)} senior extremums, "
            f"{len(tracker.events)} approaches, {len(self._traded)} entries, "
            f"{len(tracker.pending)} still pending, {len(tracker.expired)} expired, "
            f"{len(tracker.uncovered)} without a margin reading"
        )
        for reason in sorted(set(self._skipped.values())):
            count = sum(1 for text in self._skipped.values() if text == reason)
            ctx.log(f"senior25: {count} approach(es) not traded — {reason}")

        self._bars = list(ctx.history)
        self._events = self._event_rows(ctx)
        if self.p.events_csv:
            path = Path(self.p.events_csv)
            path.parent.mkdir(parents=True, exist_ok=True)
            write_rows(path, self._events)
            ctx.log(f"senior25: event table written to {path}")

    def artifacts(self) -> dict[str, list[dict]]:
        """Publish what the run knew, for the chart and for later analysis.

        The §8 table, one row per confirmed senior extremum. The zone CSVs
        (pivots, envelopes, rollover, crossings) come from `chart`, which
        writes the same report layout a `plot_zones.py` run produces.
        """
        return {"events": self._events} if self._events else {}

    # ----------------------------------------------------------------- trading

    def _side(self, extremum: SeniorExtremum) -> Side:
        """Toward the zone is down from a high and up from a low (§4.2-4.3)."""
        toward_zone = Side.BUY if extremum.direction > 0 else Side.SELL
        return toward_zone if self.p.bias == "zone" else toward_zone.opposite

    def _stop_price(self, extremum: SeniorExtremum, side: Side) -> float | None:
        """Where the stop sits, as a price, for every mode that has one.

        `pct` is anchored on the 25% level here even though the broker resolves
        the real stop against the fill: this value is only used to size an `rr`
        target and to check the approach bar has not already run through it.
        """
        p = self.p
        if p.stop_mode == "none":
            return None
        if p.stop_mode == "extremum":
            return extremum.price - extremum.direction * p.stop_buffer * extremum.distance25
        if p.stop_mode == "distance":
            return extremum.level25 - side.sign * p.stop_distance * extremum.distance25
        return extremum.level25 * (1 - side.sign * p.stop_pct / 100.0)

    def _target_price(
        self, extremum: SeniorExtremum, side: Side, stop: float | None
    ) -> float | None:
        p = self.p
        if p.target_mode == "none" or p.target_mode == "pct":
            return None
        if p.target_mode == "fraction":
            return extremum.level_at(p.target_fraction)
        return extremum.level25 + side.sign * p.target_rr * abs(extremum.level25 - stop)

    def _enter(self, ctx: Context, extremum: SeniorExtremum) -> None:
        p = self.p
        if p.bias == "none":
            self._skipped[id(extremum)] = "bias=none, detection only"
            return
        if p.one_position and not ctx.is_flat:
            self._skipped[id(extremum)] = "another position was open"
            return

        side = self._side(extremum)
        stop = self._stop_price(extremum, side)
        target = self._target_price(extremum, side, stop)
        close = ctx.bar.close

        # `side.sign * (close - level)` is positive when the close is on the
        # profitable side of `level`, so both checks read the same way.
        if stop is not None and side.sign * (close - stop) <= 0:
            self._skipped[id(extremum)] = "the approach bar closed beyond the stop"
            return
        if target is not None and side.sign * (close - target) >= 0:
            self._skipped[id(extremum)] = "the approach bar closed beyond the target"
            return

        ctx.order(
            side,
            volume=p.volume,
            sl=stop if p.stop_mode not in ("none", "pct") else None,
            tp=target,
            sl_pct=p.stop_pct if p.stop_mode == "pct" else None,
            tp_pct=p.take_pct if p.target_mode == "pct" else None,
            tag=tag_for(extremum),
        )
        self._traded.add(id(extremum))

    # --------------------------------------------------------------- the chart

    def chart(self, run_dir, data_uri: str, timeframe: str):
        """The margin-zones chart, with this run's trades and levels on it.

        Not a second, plainer chart drawn from the trade list: the zone picture
        *is* the analysis this strategy was built on, so the run gets that
        exact chart — same pivots, same envelopes, same rollover points, same
        colours — with the 25% levels, approach events and orders added as
        layers you can switch off. It writes the same report layout a
        `plot_zones.py` run produces, so a backtest directory and an analysis
        directory hold the same files.

        Everything is drawn from what the run itself computed. There is no
        second ZigZag and no second margin read, so the chart cannot disagree
        with the backtest it came from.
        """
        from ...data.loader import load_bars
        from ...indicators.zigzag import provisional
        from .report import build_payload, write_report
        from .rollover import rollover_crossings, rollover_points

        bars, pivots = self._bars, self.tracker.pivots
        if not bars or not pivots:
            return None

        envelopes = build_envelopes(
            bars, pivots, self._spec, self._log, self.p.initial_ratio, self._code
        )
        deviation = (
            f"{self.p.deviation_pips:g} pips" if self.p.deviation_pips
            else f"{self.p.deviation_pct:g}%"
        )

        # Rollover points are sampled from a finer series than the H4 the
        # pattern runs on, exactly as plot_zones.py does it. A missing store
        # for that timeframe is not a reason to lose the whole chart.
        roll, crossings = [], []
        if self.p.rollover_timeframe:
            try:
                fine = load_bars(
                    self._symbol, self.p.rollover_timeframe, data=data_uri,
                    start=bars[0].time, end=bars[-1].time, validate=False,
                )
                roll = rollover_points(fine, self.p.rollover_hour, self.p.rollover_tz)
                crossings = rollover_crossings(roll, envelopes, bars)
            except Exception:
                roll, crossings = [], []

        payload = build_payload(
            symbol=self._symbol,
            timeframe=self._timeframe,
            bars=bars,
            pivots=pivots,
            envelopes=envelopes,
            prov=provisional(
                bars, self.p.deviation_pct,
                self.p.deviation_pips * self._spec.pip_size or None, pivots,
            ),
            spec=self._spec,
            deviation=deviation,
            initial_ratio=self.p.initial_ratio,
            rollover=roll,
            crossings=crossings,
            levels=self._chart_levels(bars),
            trades=self._chart_trades(run_dir, bars),
            backtest=self._chart_metrics(run_dir),
        )
        write_report(
            Path(run_dir), payload, bars, pivots, envelopes, self._spec,
            margin_log=str(self._log.path), chart=True,
            rollover=roll, crossings=crossings,
            # summary.json is the backtest's own; the zone summary goes beside it.
            summary_name="zones.json",
        )
        return Path(run_dir) / "chart.html"

    def _chart_levels(self, bars: list[Bar]) -> list[dict]:
        """The §9 geometry, in bar indices the chart can draw directly."""
        approach = {id(event.extremum): event for event in self.tracker.events}
        out = []
        for extremum in self.tracker.extremums:
            event = approach.get(id(extremum))
            out.append({
                "kind": extremum.label,
                "iE": extremum.index,
                "pE": round(extremum.price, 6),
                "iC": extremum.confirm_index,
                "lvl": round(extremum.level25, 6),
                "lo": round(extremum.lower, 6),
                "hi": round(extremum.upper, 6),
                "iA": None if event is None else event.index,
                "tA": None if event is None else int(event.time.timestamp()),
                "traded": id(extremum) in self._traded,
            })
        return out

    def _chart_metrics(self, run_dir) -> dict | None:
        """The run's headline numbers, read back from the summary just written.

        Taken from `summary.json` rather than recomputed, so the chart and the
        printed report cannot quote different figures for the same run.
        """
        import json

        path = Path(run_dir) / "summary.json"
        if not path.exists():
            return None
        with open(path, "r", encoding="utf-8") as fh:
            metrics = json.load(fh).get("metrics") or {}
        keep = (
            "net_profit", "return_pct", "trades", "wins", "losses", "win_rate_pct",
            "profit_factor", "payoff_ratio", "expectancy", "max_drawdown_pct",
            "avg_bars_held", "initial_balance",
        )
        return {k: metrics[k] for k in keep if k in metrics} or None

    def _chart_trades(self, run_dir, bars: list[Bar]) -> list[dict]:
        """This run's orders, read back from the trades.csv already written."""
        import csv
        from bisect import bisect_right

        path = Path(run_dir) / "trades.csv"
        if not path.exists() or path.stat().st_size == 0:
            return []
        times = [int(b.time.timestamp()) for b in bars]

        def index_at(stamp: str) -> int:
            return max(0, bisect_right(times, int(parse_dt(stamp).timestamp())) - 1)

        out = []
        with open(path, "r", encoding="utf-8") as fh:
            for row in csv.DictReader(fh):
                out.append({
                    "id": int(row["id"]),
                    "side": row["side"],
                    "i0": index_at(row["entry_time"]),
                    "i1": index_at(row["exit_time"]),
                    "t0": int(parse_dt(row["entry_time"]).timestamp()),
                    "t1": int(parse_dt(row["exit_time"]).timestamp()),
                    "p0": round(float(row["entry_price"]), 6),
                    "p1": round(float(row["exit_price"]), 6),
                    "pnl": round(float(row["net_pnl"]), 2),
                    "reason": row["reason"],
                    "bars": int(row["bars_held"]),
                    "tag": row.get("tag", ""),
                })
        return out

    # --------------------------------------------------------------- reporting

    def _event_rows(self, ctx: Context) -> list[dict]:
        """The §8 table: one row per confirmed senior extremum, approached or not."""
        events = {id(event.extremum): event for event in self.tracker.events}
        rows = []
        for case, extremum in enumerate(self.tracker.extremums, start=1):
            event = events.get(id(extremum))
            row = event.as_row() if event else {
                **extremum.as_row(),
                "approached": False,
                "approach_time": "",
                "approach_day": "",
                "approach_bars_waited": "",
                "approach_bar_high": "",
                "approach_bar_low": "",
            }
            rows.append({
                "case": case,
                "instrument": ctx.symbol,
                "timeframe": ctx.timeframe,
                **row,
                "traded": id(extremum) in self._traded,
                "not_traded_because": self._skipped.get(id(extremum), ""),
                "tag": tag_for(extremum),
            })

        return rows


def tag_for(extremum: SeniorExtremum) -> str:
    """Joins a trade back to the pattern that produced it.

    An extremum's type and timestamp identify it uniquely, so `trades.csv` and
    the event table can be lined up on this column without a synthetic id that
    would change between runs.
    """
    return f"senior25 {extremum.label} {extremum.time:%Y-%m-%d %H:%M}"
