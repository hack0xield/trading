#!/usr/bin/env python3
"""Draw margin-zone envelopes over price — steps 1-5 of `Margin Zones.md`.

    scripts/plot_zones.py --symbol EURUSD --timeframe H4 --contract 6E \
        --start 2023-07-01 --deviation-pct 1.0

    1. EUR/USD chart data                --symbol / --start / --end
    2. H4                                --timeframe
    3. ZigZag local extremums            --deviation-pct or --deviation-pips
    4. FMZ / IMZ from historical margins  configs/margins.csv, read per pivot date
    5. [FMZ, IMZ] envelope from every extremum, recalculated at the next one

No orders are placed and none are implied — this draws the levels so you can
look at them.

Two properties the picture is built to keep honest:

**Margins are read as they stood at each pivot.** A three-year chart drawn with
today's MM would be wrong across most of its width, and wrong in the flattering
direction if margins have risen since.

**Pivots carry their confirmation lag.** A ZigZag extreme is only knowable once
price has retraced far enough to confirm it; the table shows how many bars late
that was for every pivot, so the drawn line is never mistaken for a signal you
could have acted on at the time.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from backtester.analysis.envelopes import build_envelopes, margin_coverage, summarise  # noqa: E402
from backtester.analysis.zigzag import provisional, swing_sizes, zigzag  # noqa: E402
from backtester.data.loader import load_bars  # noqa: E402
from backtester.data.margins import (  # noqa: E402
    DEFAULT_INITIAL_RATIO,
    MarginLog,
    load_spec,
)

TEMPLATE_PATH = Path(__file__).resolve().parent / "plot_zones_template.html"
STYLE_PATH = Path(__file__).resolve().parent / "chart_style.css"


def default_name(args, code: str) -> str:
    """Name the file after what produced it.

    A fixed default meant that plotting a second pair, or the same pair at a
    different threshold, silently overwrote the previous chart. Encoding the
    inputs means re-running the *same* configuration still overwrites — which
    is what you want while iterating — but changing any of them does not.
    """
    parts = [
        "zones", args.symbol.upper(), args.timeframe.upper(), code,
        f"dev{args.deviation_pips:g}pips" if args.deviation_pips
        else f"dev{args.deviation_pct:g}pct",
    ]
    if args.start or args.end:
        window = f"{args.start or 'start'}_{args.end or 'end'}".replace("-", "")
        parts.append(window)
    if args.initial_ratio != DEFAULT_INITIAL_RATIO:
        parts.append(f"ir{args.initial_ratio:g}")
    return "_".join(parts) + ".html"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Plot ZigZag pivots and margin-zone envelopes over price.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("--symbol", "-s", default="EURUSD")
    parser.add_argument("--timeframe", "-t", default="H4")
    parser.add_argument("--contract", "-c", default="6E", help="CME contract for the margin")
    parser.add_argument("--data", "-d", default="parquet://data/bars")
    parser.add_argument("--start")
    parser.add_argument("--end")
    parser.add_argument("--log", default="configs/margins.csv")
    parser.add_argument("--contracts", default=None, help="contract spec directory")
    parser.add_argument(
        "--deviation-pct", type=float, default=1.0,
        help="ZigZag reversal, as %% of price (default 1.0)",
    )
    parser.add_argument(
        "--deviation-pips", type=float,
        help="ZigZag reversal in pips instead; overrides --deviation-pct",
    )
    parser.add_argument("--initial-ratio", type=float, default=DEFAULT_INITIAL_RATIO)
    parser.add_argument(
        "--out", "-o",
        help="output HTML; the default is named after the inputs, e.g. "
             "runs/zones_EURUSD_H4_6E_dev2pct.html",
    )
    args = parser.parse_args(argv)

    spec = load_spec(args.contract, args.contracts)
    problems = spec.problems()
    for problem in problems:
        print(f"  ! {problem}")
    if any(p.startswith("ERROR") for p in problems):
        return 1

    bars = load_bars(args.symbol, args.timeframe, data=args.data,
                     start=args.start, end=args.end, validate=False)

    deviation_abs = args.deviation_pips * spec.pip_size if args.deviation_pips else None
    pivots = zigzag(
        bars,
        deviation_pct=None if deviation_abs else args.deviation_pct,
        deviation_abs=deviation_abs,
    )
    if not pivots:
        print(
            f"No pivots at this threshold. {args.symbol} moves less than "
            f"{args.deviation_pct}% between turns — try a smaller --deviation-pct."
        )
        return 1

    prov = provisional(
        bars,
        deviation_pct=None if deviation_abs else args.deviation_pct,
        deviation_abs=deviation_abs,
        pivots=pivots,
    )

    log = MarginLog(Path(args.log))
    envelopes = build_envelopes(bars, pivots, spec, log, args.initial_ratio, args.contract)
    covered, missing = margin_coverage(pivots, log, args.contract)
    stats = summarise(envelopes, bars)

    if not envelopes:
        print(
            f"{len(pivots)} pivots found, but no margin reading covers any of them.\n"
            f"  Import the history:  scripts/margins.py import -f <download> "
            f"--code {args.contract}\n"
            f"  Or record one:       scripts/margins.py add --code {args.contract} "
            f"--maintenance <MM> --as-of <date>"
        )
        return 1
    if missing:
        print(
            f"  ! {len(missing)} of {len(pivots)} pivots predate the first margin reading "
            f"and are drawn without a zone (earliest is {pivots[0].time:%Y-%m-%d})"
        )

    index_of = {id(p): k for k, p in enumerate(pivots)}
    sizes = swing_sizes(pivots, as_pct=True)
    median_swing = sorted(sizes)[len(sizes) // 2] if sizes else 0.0
    # FMZ as a percentage of price, so it is comparable with the swing sizes.
    mid_price = sum(b.close for b in bars) / len(bars)
    fmz_pct = stats["avg_fmz_pips"] * spec.pip_size / mid_price * 100.0
    caution = ""
    if stats.get("distinct_margins", 0) <= 1:
        caution = (
            "Every zone on this chart uses the same maintenance margin, so the bands "
            "scale with price alone. Real CME margin changes at contract roll and on "
            "volatility — import the history to see the zones move with it."
        )

    payload = {
        "symbol": args.symbol,
        "timeframe": args.timeframe.upper(),
        "contract": f"{spec.code} ({spec.name})",
        "digits": max(2, len(str(spec.pip_size).split(".")[-1])),
        "pipValue": spec.pip_value,
        "tickValue": spec.tick_value,
        "np": spec.np,
        "initialRatio": args.initial_ratio,
        "medianSwing": round(median_swing, 4),
        "fmzPct": round(fmz_pct, 4),
        "deviation": (
            f"{args.deviation_pips:g} pips" if args.deviation_pips else f"{args.deviation_pct:g}%"
        ),
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

    blob = json.dumps(payload, separators=(",", ":")).replace("<", "\\u003c")
    title = f"{args.symbol} {args.timeframe.upper()} — margin zones"
    html = (
        TEMPLATE_PATH.read_text(encoding="utf-8")
        .replace("/*__DATA__*/null", blob)
        .replace("__TITLE__", title)
        .replace("/*__STYLE__*/", STYLE_PATH.read_text(encoding="utf-8"))
    )

    out = Path(args.out) if args.out else Path("runs") / default_name(args, spec.code)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(html, encoding="utf-8")

    print(
        f"Wrote {out}  ({out.stat().st_size / 1024:,.0f} KB)\n"
        f"  {len(bars):,} {args.timeframe.upper()} bars, {len(pivots)} pivots, "
        f"{len(envelopes)} zones\n"
        f"  swing size    median {median_swing:.2f}% "
        f"(min {min(sizes):.2f}%, max {max(sizes):.2f}%)\n"
        f"  FMZ           {stats['avg_fmz_pips']:.0f} pips average, "
        f"from {stats['distinct_margins']} margin reading(s)\n"
        f"  reached FMZ   {stats['reached_fmz']}/{stats['envelopes']} "
        f"({stats['reached_fmz_pct']:.0f}%)   IMZ {stats['reached_imz_pct']:.0f}%"
        + (
            f"\n  in progress   unconfirmed {prov.kind} {prov.price:g} "
            f"{prov.bars_since} bars ago; needs {prov.confirm_at:g} to confirm "
            f"(drawn dashed)"
            if prov else ""
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
