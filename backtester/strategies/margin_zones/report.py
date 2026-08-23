"""Rendering a margin-zone report: the chart, and the data behind it.

Extracted so `scripts/plot_zones.py` and the scheduled signal runner produce
*identical* output. They compute the same pivots and envelopes; if each also
rendered them separately the two would drift, and the artifact saved beside a
signal would stop being evidence of what the signal actually saw.

A report directory is self-describing and matches the backtest-run convention:

    runs/20260807-201900_zones_EURUSD_H4_6E_dev2pct/
        chart.html      the interactive chart, data inlined
        pivots.csv      every confirmed pivot and its confirmation lag
        envelopes.csv   every zone, with the margin reading behind it
        summary.json    inputs, contract spec, statistics
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from ...core.types import Bar
from .margins import ContractSpec
from ...data.results import write_rows
from .envelopes import Envelope, summarise
from .rollover import RolloverCrossing, RolloverPoint
from ...indicators.zigzag import Pivot, Provisional, swing_sizes

SCRIPTS = Path(__file__).resolve().parents[3] / "scripts"
TEMPLATE_PATH = SCRIPTS / "plot_zones_template.html"
STYLE_PATH = SCRIPTS / "chart_style.css"


def report_name(
    symbol: str, timeframe: str, contract: str, deviation: str, stamp: datetime | None = None
) -> str:
    """`<UTC stamp>_zones_<symbol>_<tf>_<contract>_dev<n>`.

    The timestamp leads so directories sort chronologically and a re-run never
    overwrites an earlier one — the same shape `save_result` uses for backtests.
    The inputs still follow it, so you can see what produced a report without
    opening it.
    """
    stamp = stamp or datetime.now(timezone.utc)
    slug = deviation.replace(" ", "").replace("%", "pct").replace(".", "_")
    return f"{stamp:%Y%m%d-%H%M%S}_zones_{symbol.upper()}_{timeframe.upper()}_{contract}_dev{slug}"


def build_payload(
    symbol: str,
    timeframe: str,
    bars: list[Bar],
    pivots: list[Pivot],
    envelopes: list[Envelope],
    prov: Provisional | None,
    spec: ContractSpec,
    deviation: str,
    initial_ratio: float,
    rollover: list[RolloverPoint] = (),
    crossings: list[RolloverCrossing] = (),
    levels: list[dict] = (),
    trades: list[dict] = (),
    backtest: dict | None = None,
) -> dict:
    """Everything the chart page needs, as one JSON-serialisable dict.

    `levels` and `trades` are empty for an analysis run and populated for a
    backtest, which is the whole difference between the two charts: the same
    zones, with what a strategy did about them drawn on top.
    """
    stats = summarise(envelopes, bars)
    index_of = {id(p): k for k, p in enumerate(pivots)}
    sizes = swing_sizes(pivots, as_pct=True)
    median_swing = sorted(sizes)[len(sizes) // 2] if sizes else 0.0
    mid_price = sum(b.close for b in bars) / len(bars) if bars else 0.0
    # FMZ as a percentage of price, so it is comparable with the swing sizes.
    fmz_pct = (
        stats.get("avg_fmz_pips", 0.0) * spec.pip_size / mid_price * 100.0 if mid_price else 0.0
    )

    caution = ""
    if stats.get("distinct_margins", 0) <= 1:
        caution = (
            "Every zone on this chart uses the same maintenance margin, so the bands "
            "scale with price alone. Real CME margin changes at contract roll and on "
            "volatility — import the history to see the zones move with it."
        )

    return {
        "symbol": symbol,
        "timeframe": timeframe.upper(),
        "contract": f"{spec.code} ({spec.name})",
        "digits": max(2, len(str(spec.pip_size).split(".")[-1])),
        "pipValue": spec.pip_value,
        "tickValue": spec.tick_value,
        "np": spec.np,
        "initialRatio": initial_ratio,
        "medianSwing": round(median_swing, 4),
        "fmzPct": round(fmz_pct, 4),
        "deviation": deviation,
        "times": [int(b.time.timestamp()) for b in bars],
        "close": [round(b.close, 6) for b in bars],
        "high": [round(b.high, 6) for b in bars],
        "low": [round(b.low, 6) for b in bars],
        "pivots": [
            {"i": p.index, "t": int(p.time.timestamp()), "p": round(p.price, 6),
             "kind": p.kind, "ci": p.confirm_index}
            for p in pivots
        ],
        "envelopes": [
            {
                "pi": index_of[id(e.pivot)],
                "i0": e.start_index, "i1": e.end_index, "dir": e.direction,
                "fmz": round(e.fmz_price, 6), "imz": round(e.imz_price, 6),
                "e50": round(e.e50_price, 6),
                "pips": round(e.fmz_pips, 1), "mm": e.maintenance,
                "asOf": e.margin_as_of.isoformat(),
                "hit": e.touched(bars),
            }
            for e in envelopes
        ],
        "rollover": [
            {"t": int(r.roll_time.timestamp()), "p": round(r.price, 6), "day": r.day.isoformat()}
            for r in rollover
        ],
        "crossings": [
            {
                "pi": index_of[id(c.envelope.pivot)],
                "t": int(c.current.roll_time.timestamp()), "p": round(c.current.price, 6),
                "day": c.current.day.isoformat(), "e50": round(c.e_level, 6),
                "dir": c.direction, "cls": c.classification,
                "prevT": int(c.previous.roll_time.timestamp()),
                "prevP": round(c.previous.price, 6),
                "prevDay": c.previous.day.isoformat(),
            }
            for c in crossings
        ],
        "prov": None if prov is None else {
            "i": prov.index, "p": round(prov.price, 6), "kind": prov.kind,
            "confirmAt": round(prov.confirm_at, 6), "bars": prov.bars_since,
        },
        "levels": list(levels),
        "trades": list(trades),
        "backtest": backtest or None,
        "stats": stats,
        "caution": caution,
        "generated": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
    }


def render_chart(payload: dict, title: str) -> str:
    """The self-contained HTML page, with data and stylesheet inlined."""
    # `</script>` inside the JSON would end the block early; escaping `<` is the
    # standard fix and stays valid JSON.
    blob = json.dumps(payload, separators=(",", ":")).replace("<", "\\u003c")
    return (
        TEMPLATE_PATH.read_text(encoding="utf-8")
        .replace("/*__DATA__*/null", blob)
        .replace("__TITLE__", title)
        .replace("/*__STYLE__*/", STYLE_PATH.read_text(encoding="utf-8"))
    )


def write_report(
    directory: Path,
    payload: dict,
    bars: list[Bar],
    pivots: list[Pivot],
    envelopes: list[Envelope],
    spec: ContractSpec,
    margin_log: str = "",
    chart: bool = True,
    rollover: list[RolloverPoint] = (),
    crossings: list[RolloverCrossing] = (),
    summary_name: str = "summary.json",
) -> Path:
    """Write chart.html plus the CSVs the chart was built from.

    `summary_name` exists because a backtest run directory already has a
    `summary.json` — its parameters and metrics — and writing the zone summary
    over it would destroy the record of what the run actually did. A strategy
    rendering this report into its own run directory passes another name.
    """
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)

    if chart:
        title = f"{payload['symbol']} {payload['timeframe']} — margin zones"
        (directory / "chart.html").write_text(render_chart(payload, title), encoding="utf-8")

    write_rows(directory / "pivots.csv", [
        {
            "n": n, "kind": p.kind, "extreme_index": p.index,
            "extreme_time": p.time.isoformat(), "price": p.price,
            "confirm_index": p.confirm_index, "confirm_time": p.confirm_time.isoformat(),
            "lag_bars": p.lag_bars,
        }
        for n, p in enumerate(pivots)
    ])

    index_of = {id(p): n for n, p in enumerate(pivots)}
    write_rows(directory / "envelopes.csv", [
        {
            "pivot_n": index_of[id(e.pivot)], "kind": e.pivot.kind, "direction": e.direction,
            "pivot_time": e.pivot.time.isoformat(), "pivot_price": e.pivot.price,
            "start_index": e.start_index, "end_index": e.end_index,
            "fmz_price": round(e.fmz_price, 6), "imz_price": round(e.imz_price, 6),
            "fmz_pips": round(e.fmz_pips, 2), "imz_pips": round(e.imz_pips, 2),
            "mz_pips": round(e.imz_pips - e.fmz_pips, 2),
            "mz50_price": round(e.mid_price, 6),
            "e50_price": round(e.e50_price, 6),
            "maintenance": e.maintenance, "margin_as_of": e.margin_as_of.isoformat(),
            "reached_fmz": e.touched(bars), "reached_imz": e.reached_far(bars),
        }
        for e in envelopes
    ])

    if rollover:
        write_rows(directory / "rollover.csv", [
            {
                "day": r.day.isoformat(), "roll_time": r.roll_time.isoformat(),
                "price": round(r.price, 6), "bar_time": r.bar_time.isoformat(),
            }
            for r in rollover
        ])

    if crossings:
        write_rows(directory / "crossings.csv", [
            {
                "pivot_n": index_of[id(c.envelope.pivot)], "kind": c.envelope.pivot.kind,
                "pivot_time": c.envelope.pivot.time.isoformat(),
                "pivot_price": c.envelope.pivot.price,
                "prev_day": c.previous.day.isoformat(), "prev_price": round(c.previous.price, 6),
                "day": c.current.day.isoformat(), "price": round(c.current.price, 6),
                "e50_level": round(c.e_level, 6), "direction": c.direction,
                "classification": c.classification,
            }
            for c in crossings
        ])

    summary = {
        "symbol": payload["symbol"],
        "timeframe": payload["timeframe"],
        "contract": spec.to_dict(),
        "period": {
            "start": bars[0].time.isoformat() if bars else None,
            "end": bars[-1].time.isoformat() if bars else None,
            "bars": len(bars),
        },
        "zigzag": {
            "deviation": payload["deviation"],
            "pivots": len(pivots),
            "median_swing_pct": payload["medianSwing"],
        },
        "zones": {
            "initial_ratio": payload["initialRatio"],
            "count": len(envelopes),
            "fmz_pct_of_price": payload["fmzPct"],
            **payload["stats"],
        },
        "provisional": payload["prov"],
        "rollover": {"points": len(rollover)},
        "crossings": {
            "count": len(crossings),
            "true": sum(1 for c in crossings if c.classification == "True"),
            "false": sum(1 for c in crossings if c.classification == "False"),
        },
        "margin_log": margin_log,
        "generated": payload["generated"],
    }
    with open(directory / summary_name, "w", encoding="utf-8") as fh:
        json.dump(summary, fh, indent=2, default=str)
    return directory


# ------------------------------------------------------- backtest run reports

def read_trades(run_dir: Path, bars: list[Bar]) -> list[dict]:
    """A run's orders, read back from the trades.csv already written.

    Bar indices are resolved by timestamp rather than taken from the run, so
    the same rows can be drawn against a chart at any timeframe.
    """
    import csv
    from bisect import bisect_right

    from ...utils.timeutil import parse_dt

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
                "id": int(row["id"]), "side": row["side"],
                "i0": index_at(row["entry_time"]), "i1": index_at(row["exit_time"]),
                "t0": int(parse_dt(row["entry_time"]).timestamp()),
                "t1": int(parse_dt(row["exit_time"]).timestamp()),
                "p0": round(float(row["entry_price"]), 6),
                "p1": round(float(row["exit_price"]), 6),
                "pnl": round(float(row["net_pnl"]), 2),
                "reason": row["reason"], "bars": int(row["bars_held"]),
                "tag": row.get("tag", ""),
            })
    return out


def read_metrics(run_dir: Path) -> dict | None:
    """The run's headline numbers, from the summary already written.

    Read rather than recomputed, so the chart and the printed report cannot
    quote different figures for the same run.
    """
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


def write_zone_run(
    run_dir: Path,
    symbol: str,
    timeframe: str,
    bars: list[Bar],
    pivots: list[Pivot],
    spec: ContractSpec,
    log,
    initial_ratio: float,
    code: str,
    deviation: str,
    deviation_pct: float | None = None,
    deviation_abs: float | None = None,
    rollover_hour: int = 0,
    rollover_tz: str = "UTC",
    rollover_bars: list[Bar] | None = None,
    levels: list[dict] = (),
) -> Path:
    """Render a backtest run as the margin-zones chart, with its trades on it.

    Shared by every margin-zone strategy, so they cannot drift into drawing
    subtly different pictures of the same zones. The strategy supplies its own
    `levels` layer — the 25% control-zone geometry, say — and everything else
    comes from the run: its pivots, its envelopes, its trades, its metrics.

    Writes the same report layout an analysis run produces, so a backtest
    directory and a `plot_zones.py` directory hold the same files. The zone
    summary goes to `zones.json`; `summary.json` is the backtest's own.
    """
    from .envelopes import build_envelopes
    from .rollover import rollover_crossings, rollover_points
    from ...indicators.zigzag import provisional

    run_dir = Path(run_dir)
    envelopes = build_envelopes(bars, pivots, spec, log, initial_ratio, code)

    source = rollover_bars if rollover_bars is not None else bars
    roll = rollover_points(source, rollover_hour, rollover_tz)
    crossings = rollover_crossings(roll, envelopes, bars)

    payload = build_payload(
        symbol=symbol,
        timeframe=timeframe,
        bars=bars,
        pivots=pivots,
        envelopes=envelopes,
        # The unconfirmed extreme in progress. Drawn, never traded — without
        # it the ZigZag appears to stop dead at the last confirmation.
        prov=(
            provisional(bars, deviation_pct, deviation_abs, pivots)
            if pivots and (deviation_pct or deviation_abs) else None
        ),
        spec=spec,
        deviation=deviation,
        initial_ratio=initial_ratio,
        rollover=roll,
        crossings=crossings,
        levels=list(levels),
        trades=read_trades(run_dir, bars),
        backtest=read_metrics(run_dir),
    )
    write_report(
        run_dir, payload, bars, pivots, envelopes, spec,
        margin_log=str(getattr(log, "path", "")), chart=True,
        rollover=roll, crossings=crossings,
        summary_name="zones.json",
    )
    return run_dir / "chart.html"
