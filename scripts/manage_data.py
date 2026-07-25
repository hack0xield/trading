#!/usr/bin/env python3
"""Inspect and move bar data between stores.

    scripts/manage_data.py list                     --data parquet://data/bars
    scripts/manage_data.py check  -s XAUUSD -t M15
    scripts/manage_data.py convert --from csv://data/bars --to parquet://data/bars
    scripts/manage_data.py convert --from csv://data/bars --to postgresql://localhost/trading
    scripts/manage_data.py import --file gold.csv -s XAUUSD -t M1
    scripts/manage_data.py export -s XAUUSD -t M15 --file /tmp/xau.csv
    scripts/manage_data.py resample -s XAUUSD --from-tf M1 --to-tf M15
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from backtester.data.csv_store import CsvBarStore, read_csv_file  # noqa: E402
from backtester.data.loader import resample, validate_bars  # noqa: E402
from backtester.data.registry import store_from_uri  # noqa: E402
from backtester.utils.timeutil import parse_dt  # noqa: E402


def cmd_list(args) -> int:
    store = store_from_uri(args.data)
    series = store.list_series()
    if not series:
        print(f"{store} is empty.")
        return 0
    print(f"{store}")
    total = 0
    for item in series:
        print(f"  {item}")
        total += item.count
    print(f"  {'':10} {'':4} {total:>9,} bars total")
    return 0


def cmd_check(args) -> int:
    store = store_from_uri(args.data)
    bars = store.read(args.symbol, args.timeframe, parse_dt(args.start), parse_dt(args.end))
    if not bars:
        print(f"No {args.symbol} {args.timeframe} bars in {store}")
        return 1

    problems = validate_bars(bars, args.timeframe)
    print(f"{args.symbol} {args.timeframe}: {len(bars):,} bars")
    print(f"  range     {bars[0].time} .. {bars[-1].time}")
    print(f"  price     {min(b.low for b in bars):,.2f} .. {max(b.high for b in bars):,.2f}")
    spreads = [b.spread for b in bars if b.spread > 0]
    if spreads:
        print(f"  spread    avg {sum(spreads) / len(spreads):.5f}, max {max(spreads):.5f}")
    else:
        print("  spread    not recorded (the instrument default will be used)")
    if problems:
        for problem in problems:
            print(f"  ! {problem}")
    else:
        print("  no problems found")
    return 0 if not any(p.startswith("ERROR") for p in problems) else 1


def cmd_convert(args) -> int:
    source = store_from_uri(getattr(args, "from"))
    target = store_from_uri(args.to)
    series = source.list_series()
    if args.symbol:
        series = [s for s in series if s.symbol == args.symbol.upper()]
    if args.timeframe:
        series = [s for s in series if s.timeframe == args.timeframe.upper()]
    if not series:
        print(f"Nothing to copy from {source}")
        return 1

    for item in series:
        bars = source.read(item.symbol, item.timeframe)
        written = target.write(item.symbol, item.timeframe, bars)
        print(f"  {item.symbol} {item.timeframe:<4} {written:>9,} bars -> {target}")
    return 0


def cmd_import(args) -> int:
    bars = read_csv_file(args.file)
    if not bars:
        print(f"No rows read from {args.file}")
        return 1
    store = store_from_uri(args.data)
    written = store.write(args.symbol, args.timeframe, bars)
    print(
        f"Imported {written:,} bars ({bars[0].time:%Y-%m-%d} .. {bars[-1].time:%Y-%m-%d}) "
        f"as {args.symbol} {args.timeframe} into {store}"
    )
    for problem in validate_bars(bars, args.timeframe):
        print(f"  ! {problem}")
    return 0


def cmd_export(args) -> int:
    store = store_from_uri(args.data)
    bars = store.read(args.symbol, args.timeframe, parse_dt(args.start), parse_dt(args.end))
    if not bars:
        print("Nothing to export")
        return 1
    out = Path(args.file)
    temp = CsvBarStore(out.parent / ".export")
    temp.write(args.symbol, args.timeframe, bars)
    temp.path_for(args.symbol, args.timeframe).replace(out)
    (out.parent / ".export" / args.symbol.upper()).rmdir()
    (out.parent / ".export").rmdir()
    print(f"Wrote {len(bars):,} bars to {out}")
    return 0


def cmd_resample(args) -> int:
    store = store_from_uri(args.data)
    bars = store.read(args.symbol, getattr(args, "from_tf"))
    if not bars:
        print(f"No {args.symbol} {getattr(args, 'from_tf')} bars in {store}")
        return 1
    coarse = resample(bars, args.to_tf)
    written = store.write(args.symbol, args.to_tf, coarse)
    print(
        f"{len(bars):,} {getattr(args, 'from_tf')} bars -> {written:,} {args.to_tf} bars in {store}"
    )
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Inspect and move bar data.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "--data", "-d", default="parquet://data/bars", help="store URI (default parquet://data/bars)"
    )

    # `--data` is accepted on either side of the subcommand. SUPPRESS is what
    # makes that work: without it the subparser's own default would overwrite a
    # value the main parser already took from the left-hand side.
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--data", "-d", default=argparse.SUPPRESS, help=argparse.SUPPRESS)

    subs = parser.add_subparsers(dest="command", required=True)

    subs.add_parser(
        "list", parents=[common], help="show every series in the store"
    ).set_defaults(func=cmd_list)

    check = subs.add_parser("check", parents=[common], help="data quality report for one series")
    check.add_argument("--symbol", "-s", required=True)
    check.add_argument("--timeframe", "-t", required=True)
    check.add_argument("--start")
    check.add_argument("--end")
    check.set_defaults(func=cmd_check)

    convert = subs.add_parser("convert", parents=[common], help="copy series between stores")
    convert.add_argument("--from", dest="from", required=True, help="source store URI")
    convert.add_argument("--to", required=True, help="target store URI")
    convert.add_argument("--symbol", "-s", help="limit to one symbol")
    convert.add_argument("--timeframe", "-t", help="limit to one timeframe")
    convert.set_defaults(func=cmd_convert)

    imp = subs.add_parser("import", parents=[common], help="load an external CSV into the store")
    imp.add_argument("--file", "-f", required=True)
    imp.add_argument("--symbol", "-s", required=True)
    imp.add_argument("--timeframe", "-t", required=True)
    imp.set_defaults(func=cmd_import)

    exp = subs.add_parser("export", parents=[common], help="write one series out as CSV")
    exp.add_argument("--file", "-f", required=True)
    exp.add_argument("--symbol", "-s", required=True)
    exp.add_argument("--timeframe", "-t", required=True)
    exp.add_argument("--start")
    exp.add_argument("--end")
    exp.set_defaults(func=cmd_export)

    res = subs.add_parser("resample", parents=[common], help="build a coarser timeframe from a finer one")
    res.add_argument("--symbol", "-s", required=True)
    res.add_argument("--from-tf", dest="from_tf", required=True)
    res.add_argument("--to-tf", dest="to_tf", required=True)
    res.set_defaults(func=cmd_resample)

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
