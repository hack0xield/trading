"""The margin-zone chart and the CSV summary beside it.

Everything drawn comes from one forward pass over the bars: the zone versions a
`ZoneTracker` produced, the rollover points and crossings a `CrossingTracker`
saw, and the trades the run recorded. Nothing is recomputed, so the picture and
the run cannot disagree.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from ...core.types import Bar
from ...indicators.zigzag import Candidate, Pivot, swing_sizes
from .crossing import Crossing
from .margins import ContractSpec
from .rollover import RolloverPoint
from .zones import ZoneVersion, summarise

SCRIPTS = Path(__file__).resolve().parents[3] / "scripts"
TEMPLATE_PATH = SCRIPTS / "zone_chart_template.html"
STYLE_PATH = SCRIPTS / "chart_style.css"

Span = tuple[ZoneVersion, int, int]


def report_name(
    symbol: str, timeframe: str, contract: str, deviation: str, stamp: datetime | None = None
) -> str:
    """`<UTC stamp>_zones_<symbol>_<tf>_<contract>_dev<n>`.

    The timestamp leads so directories sort chronologically and a re-run never
    overwrites an earlier one.
    """
    stamp = stamp or datetime.now(timezone.utc)
    slug = deviation.replace(" ", "").replace("%", "pct").replace(".", "_")
    return f"{stamp:%Y%m%d-%H%M%S}_zones_{symbol.upper()}_{timeframe.upper()}_{contract}_dev{slug}"


def build_payload(
    symbol: str,
    timeframe: str,
    bars: list[Bar],
    pivots: list[Pivot],
    spans: list[Span],
    spec: ContractSpec,
    deviation: str,
    initial_ratio: float,
    candidate: Candidate | None = None,
    confirm_at: float | None = None,
    rollover: list[RolloverPoint] = (),
    crossings: list[Crossing] = (),
    signal_level: str = "mz50",
    trades: list[dict] = (),
    backtest: dict | None = None,
    strategy: str = "",
) -> dict:
    """Everything the chart page needs, as one JSON-serialisable dict."""
    stats = summarise(spans, bars)
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
        "zones": [
            {
                "id": z.zone_id, "leg": z.leg, "ver": z.version, "kind": z.kind,
                "i0": i0, "i1": i1, "dir": z.direction,
                # The pivot whose confirmation opened this leg, so the chart can
                # join the candidate back to the structure it came from.
                "pi": z.leg - 1 if 0 <= z.leg - 1 < len(pivots) else None,
                "anch": round(z.anchor_price, 6), "ai": z.anchor_index,
                "at": int(z.anchor_time.timestamp()),
                "kt": int(z.known_time.timestamp()),
                "mz0": round(z.mz0, 6), "mz100": round(z.mz100, 6),
                "mid": round(z.mz50, 6), "e50": round(z.e50, 6),
                "pips": round(z.zones.fmz, 1), "mm": z.zones.maintenance,
                "asOf": z.zones.as_of.isoformat(), "ev": z.event_type,
            }
            for z, i0, i1 in spans
        ],
        "rollover": [
            {"t": int(r.roll_time.timestamp()), "p": round(r.price, 6), "day": r.day.isoformat()}
            for r in rollover
        ],
        "crossings": [
            {
                "z": c.zone.zone_id,
                "t": int(c.time.timestamp()), "p": round(c.current.price, 6),
                "day": c.current.day.isoformat(), "lvl": round(c.level, 6),
                "dir": c.direction, "cls": c.classification,
                "prevT": int(c.previous.roll_time.timestamp()),
                "prevP": round(c.previous.price, 6),
                "prevDay": c.previous.day.isoformat(),
                "anch": round(c.zone.anchor_price, 6), "kind": c.zone.kind,
            }
            for c in crossings
        ],
        "cand": None if candidate is None else {
            "i": candidate.index, "p": round(candidate.price, 6), "kind": candidate.kind,
            "confirmAt": None if confirm_at is None else round(confirm_at, 6),
            "bars": max(0, len(bars) - 1 - candidate.index),
        },
        "strategy": strategy or None,
        "signalLevel": signal_level,
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


def write_chart(directory: Path, payload: dict, summary_name: str = "zones.json") -> Path:
    """Write `chart.html` and the run's zone summary into `directory`."""
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)

    name = payload.get("strategy")
    title = (
        f"{payload['symbol']} {payload['timeframe']} — {name}"
        if name else f"{payload['symbol']} {payload['timeframe']} — margin zones"
    )
    path = directory / "chart.html"
    path.write_text(render_chart(payload, title), encoding="utf-8")

    summary = {
        "symbol": payload["symbol"],
        "timeframe": payload["timeframe"],
        "strategy": name,
        "period": {
            "start": payload["times"] and payload["times"][0],
            "end": payload["times"] and payload["times"][-1],
            "bars": len(payload["times"]),
        },
        "zigzag": {
            "deviation": payload["deviation"],
            "pivots": len(payload["pivots"]),
            "median_swing_pct": payload["medianSwing"],
        },
        "zones": {
            "initial_ratio": payload["initialRatio"],
            "fmz_pct_of_price": payload["fmzPct"],
            **payload["stats"],
        },
        "candidate": payload["cand"],
        "rollover": {"points": len(payload["rollover"])},
        "crossings": {
            "level": payload["signalLevel"],
            "count": len(payload["crossings"]),
            "true": sum(1 for c in payload["crossings"] if c["cls"] == "True"),
            "false": sum(1 for c in payload["crossings"] if c["cls"] == "False"),
        },
        "generated": payload["generated"],
    }
    with open(directory / summary_name, "w", encoding="utf-8") as fh:
        json.dump(summary, fh, indent=2, default=str)
    return path


def read_trades(run_dir: Path, bars: list[Bar]) -> list[dict]:
    """A run's orders, read back from the `trades.csv` already written.

    Bar indices are resolved by timestamp, so the rows can be drawn against a
    chart at any timeframe.
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
    """The run's headline numbers, from the `summary.json` already written."""
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
