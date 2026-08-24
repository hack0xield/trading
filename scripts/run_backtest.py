#!/usr/bin/env python3
"""Run one backtest.

    # the mock strategy, straight from the brief
    scripts/run_backtest.py -S day_open -s XAUUSD -t M15 \
        -p volume=0.1 -p stop_pct=2 -p take_pct=2

    # same thing, from a config file, saving the run and its chart
    scripts/run_backtest.py --config configs/strategies/day_open_xauusd.yaml --save

    # how much of the result is the intrabar assumption?
    scripts/run_backtest.py --config configs/strategies/day_open_xauusd.yaml --intrabar optimistic
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from backtester import cli  # noqa: E402
from backtester.core.engine import Backtester  # noqa: E402
from backtester.data.loader import load_bars, validate_bars  # noqa: E402
from backtester.data.results import save_result  # noqa: E402
from backtester.metrics import casebook, compute, monthly_table, text_report, trades_preview  # noqa: E402
from backtester.strategies import describe_all, get_strategy  # noqa: E402


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Backtest a strategy over stored bars.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("--config", "-c", help="YAML or JSON run config")
    cli.add_strategy_args(parser)
    cli.add_data_args(parser)
    cli.add_execution_args(parser)

    output = parser.add_argument_group("output")
    output.add_argument("--save", action="store_true", help="write the run to --runs-dir")
    output.add_argument("--runs-dir", default="runs", help="where --save writes (default runs/)")
    output.add_argument("--label", default="", help="suffix for the run directory name")
    output.add_argument(
        "--no-chart", action="store_true",
        help="skip chart.html; --save renders it by default",
    )
    output.add_argument(
        "--chart-timeframe", default="H4",
        help="timeframe to draw the chart at (default H4)",
    )
    output.add_argument(
        "--be-threshold", type=float, default=casebook.DEFAULT_BE_THRESHOLD,
        help="a case returning within this many R of flat is break-even "
             f"(default {casebook.DEFAULT_BE_THRESHOLD})",
    )
    output.add_argument("--monthly", action="store_true", help="add a monthly returns table")
    output.add_argument("--trades", type=int, default=0, help="print the first N trades")
    output.add_argument("--json", action="store_true", help="emit metrics as JSON only")
    output.add_argument("--quiet", "-q", action="store_true", help="suppress the report")
    output.add_argument(
        "--list-strategies", action="store_true", help="show strategies and their parameters"
    )
    output.add_argument("--progress", type=int, default=0, help="heartbeat every N bars")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    if args.list_strategies:
        print(describe_all())
        return 0

    config = cli.merge(cli.load_config(args.config), args)
    cli.require(config, "strategy", "symbol", "timeframe")
    if args.progress:
        config["progress_every"] = args.progress

    symbol = config["symbol"]
    timeframe = config["timeframe"]

    strategy_cls = get_strategy(config["strategy"])
    strategy = strategy_cls(config.get("params"))

    bars = load_bars(
        symbol,
        timeframe,
        data=config.get("data") or cli.DEFAULT_DATA,
        start=config.get("start"),
        end=config.get("end"),
        validate=config.get("validate", True),
    )
    if not args.quiet:
        for problem in validate_bars(bars, timeframe):
            print(f"  ! {problem}", file=sys.stderr)

    execution = cli.execution_from(config)
    result = Backtester(
        strategy=strategy,
        symbol=symbol,
        timeframe=timeframe,
        instrument=cli.instrument_from(config, symbol),
        execution=execution,
        engine=cli.engine_from(config),
    ).run(bars)

    metrics = compute(result)

    if args.json:
        import json

        print(json.dumps(metrics.to_dict(), indent=2, default=str))
    elif not args.quiet:
        print(text_report(result, metrics))
        if args.monthly:
            print(monthly_table(result))
        if args.trades:
            print(trades_preview(result, args.trades))

    if args.save:
        directory = save_result(
            result,
            metrics.to_dict(),
            root=args.runs_dir,
            label=args.label,
            execution=execution.__dict__,
        )
        print(f"\nSaved to {directory}")

        # The Backtest Database and its views, per impl-spec/Claude
        # Specification_ Backtest Data Collection and Reporting.md.
        cases = casebook.write(directory, result, be_threshold=args.be_threshold)
        if cases:
            print(f"Casebook: {len(cases)} cases -> {directory}/report.md")

        if not args.no_chart:
            data_uri = config.get("data") or cli.DEFAULT_DATA
            # A strategy may render its own; margin-zone strategies draw the
            # zones chart with the trades on it rather than a bare price plot.
            drawn = strategy.chart(directory, data_uri, args.chart_timeframe)
            if drawn is None:
                # Imported here, not at module scope: drawing needs the chart
                # template, and an unsaved run should not pay for loading it.
                from scripts.plot_run import render

                drawn = render(directory, data_uri, args.chart_timeframe)
            print(f"Chart: {drawn}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
