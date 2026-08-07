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
) -> dict:
    """Everything the chart page needs, as one JSON-serialisable dict."""
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
                "pips": round(e.fmz_pips, 1), "mm": e.maintenance,
                "asOf": e.margin_as_of.isoformat(),
                "hit": e.touched(bars),
            }
            for e in envelopes
        ],
        "prov": None if prov is None else {
            "i": prov.index, "p": round(prov.price, 6), "kind": prov.kind,
            "confirmAt": round(prov.confirm_at, 6), "bars": prov.bars_since,
        },
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
) -> Path:
    """Write chart.html plus the CSVs the chart was built from."""
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
            "maintenance": e.maintenance, "margin_as_of": e.margin_as_of.isoformat(),
            "reached_fmz": e.touched(bars), "reached_imz": e.reached_far(bars),
        }
        for e in envelopes
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
        "margin_log": margin_log,
        "generated": payload["generated"],
    }
    with open(directory / "summary.json", "w", encoding="utf-8") as fh:
        json.dump(summary, fh, indent=2, default=str)
    return directory
