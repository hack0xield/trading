#!/usr/bin/env python3
"""Render a saved run as a self-contained, scrollable price chart with its trades.

    scripts/plot_run.py runs/20260727-130800_day_open_XAUUSD_M15
    scripts/plot_run.py runs/<dir> --timeframe H1 --out /tmp/chart.html

The output is one HTML file with the data inlined — no server, no CDN, no build
step. Open it, or publish it.

Two choices worth knowing about:

**The x-axis is bar index, not clock time.** Plotting gold against a real time
axis leaves a flat gap every weekend, which is 28% of the width spent showing
that the market was shut. Bar index removes them; the axis labels still carry
real dates.

**Trades are coloured blue/red, not green/red.** The obvious choice fails: run
through the dataviz validator, green vs red scores a CVD separation of ΔE 4.1
for deuteranopia, against a floor of 8 — the two commonest colours in trading
charts are the two a red-green colourblind reader cannot tell apart. Blue/red
scores 21.6. Direction is *also* encoded by marker shape (▲ win / ▼ loss), so
colour is never the only channel.
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
from bisect import bisect_right
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from backtester.data.loader import load_bars, resample  # noqa: E402
from backtester.utils.timeutil import parse_dt  # noqa: E402

TEMPLATE_PATH = Path(__file__).resolve().parent / "plot_run_template.html"
STYLE_PATH = Path(__file__).resolve().parent / "chart_style.css"


def build_payload(run_dir: Path, data_uri: str, timeframe: str | None) -> dict:
    with open(run_dir / "summary.json", "r", encoding="utf-8") as fh:
        summary = json.load(fh)

    symbol = summary["symbol"]
    source_tf = summary["timeframe"]
    period = summary.get("period", {})

    bars = load_bars(
        symbol,
        source_tf,
        data=data_uri,
        start=period.get("start"),
        end=period.get("end"),
        validate=False,
    )
    # Plotting every M15 bar would be ~100k points and a 400k-pixel canvas, so
    # coarsen for display. Trades keep their real timestamps and are placed on
    # the bar that contains them.
    if timeframe and timeframe.upper() != source_tf:
        bars = resample(bars, timeframe)
        shown_tf = timeframe.upper()
    else:
        shown_tf = source_tf

    times = [int(b.time.timestamp()) for b in bars]

    trades = []
    with open(run_dir / "trades.csv", "r", encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            entry = int(parse_dt(row["entry_time"]).timestamp())
            exit_ = int(parse_dt(row["exit_time"]).timestamp())
            trades.append(
                {
                    "id": int(row["id"]),
                    "side": row["side"],
                    "i0": max(0, bisect_right(times, entry) - 1),
                    "i1": max(0, bisect_right(times, exit_) - 1),
                    "t0": entry,
                    "t1": exit_,
                    "p0": round(float(row["entry_price"]), 2),
                    "p1": round(float(row["exit_price"]), 2),
                    "pnl": round(float(row["net_pnl"]), 2),
                    "reason": row["reason"],
                    "bars": int(row["bars_held"]),
                }
            )

    metrics = summary.get("metrics", {})
    return {
        "symbol": symbol,
        "strategy": summary["strategy"],
        "sourceTf": source_tf,
        "shownTf": shown_tf,
        "params": summary.get("params", {}),
        "times": times,
        "close": [round(b.close, 2) for b in bars],
        "high": [round(b.high, 2) for b in bars],
        "low": [round(b.low, 2) for b in bars],
        "trades": trades,
        "stats": {
            "net_profit": metrics.get("net_profit", 0.0),
            "return_pct": metrics.get("return_pct", 0.0),
            "trades": metrics.get("trades", len(trades)),
            "win_rate_pct": metrics.get("win_rate_pct", 0.0),
            "max_drawdown_pct": metrics.get("max_drawdown_pct", 0.0),
            "initial_balance": metrics.get("initial_balance", 0.0),
        },
        "generated": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Render a saved run as an interactive HTML chart.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("run_dir", help="a directory under runs/")
    parser.add_argument("--data", "-d", default="parquet://data/bars", help="bar store URI")
    parser.add_argument(
        "--timeframe",
        "-t",
        default="H4",
        help="timeframe to draw; the run's own is usually too dense (default H4)",
    )
    parser.add_argument("--out", "-o", help="output HTML (default: chart.html in the run dir)")
    args = parser.parse_args(argv)

    run_dir = Path(args.run_dir)
    if not (run_dir / "summary.json").exists():
        raise SystemExit(f"{run_dir} does not look like a run directory (no summary.json)")

    payload = build_payload(run_dir, args.data, args.timeframe)
    template = TEMPLATE_PATH.read_text(encoding="utf-8")

    # `</script>` inside the JSON would end the block early; `<` escaping is the
    # standard fix and stays valid JSON.
    blob = json.dumps(payload, separators=(",", ":")).replace("<", "\\u003c")
    title = (
        f"{payload['symbol']} {payload['strategy']} — "
        f"{len(payload['trades'])} trades on price"
    )
    html = (
        template.replace("/*__DATA__*/null", blob)
        .replace("__TITLE__", title)
        .replace("/*__STYLE__*/", STYLE_PATH.read_text(encoding="utf-8"))
    )

    out = Path(args.out) if args.out else run_dir / "chart.html"
    out.write_text(html, encoding="utf-8")
    size = out.stat().st_size / 1024
    print(
        f"Wrote {out}  ({size:,.0f} KB)\n"
        f"  {len(payload['close']):,} {payload['shownTf']} bars, "
        f"{len(payload['trades']):,} trades"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
