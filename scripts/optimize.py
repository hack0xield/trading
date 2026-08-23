#!/usr/bin/env python3
"""Sweep strategy parameters over a grid and rank the results.

    scripts/optimize.py -S day_open -s XAUUSD -t M15 \
        --sweep stop_pct=1,1.5,2,2.5,3 \
        --sweep take_pct=1,2,3,4 \
        --sort sharpe

    # walk-forward-ish: fit on one window, then check the ranking out of sample
    scripts/optimize.py -c configs/strategies/day_open_xauusd.yaml \
        --sweep stop_pct=1,2,3 --start 2022-01-01 --end 2023-12-31

A word of warning that the tool cannot enforce: a grid this cheap to run is
also cheap to overfit. Five values of two parameters is 25 chances for noise to
look like signal. Treat the *shape* of the surface as the finding — a broad
plateau of decent results beats a single bright cell every time — and confirm
anything you like on data the sweep never saw.
"""

from __future__ import annotations

import argparse
import itertools
import sys
import time
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from backtester import cli  # noqa: E402
from backtester.core.engine import Backtester  # noqa: E402
from backtester.data.loader import load_bars  # noqa: E402
from backtester.data.results import save_sweep  # noqa: E402
from backtester.metrics import compute  # noqa: E402
from backtester.strategies import get_strategy  # noqa: E402

SORT_KEYS = [
    "net_profit",
    "return_pct",
    "cagr_pct",
    "sharpe",
    "sortino",
    "calmar",
    "profit_factor",
    "expectancy",
    "win_rate_pct",
    "recovery_factor",
    "max_drawdown_pct",
    "trades",
]

# Populated once per worker process; the bar list is the expensive part and is
# identical for every combination, so it is loaded once and reused.
_SHARED: dict = {}


def parse_sweep(items: list[str]) -> dict[str, list[str]]:
    grid: dict[str, list[str]] = {}
    for item in items:
        if "=" not in item:
            raise SystemExit(f"--sweep expects key=v1,v2,v3 — got {item!r}")
        key, _, values = item.partition("=")
        grid[key.strip()] = [v.strip() for v in values.split(",") if v.strip()]
    return grid


def combinations(grid: dict[str, list[str]]) -> list[dict[str, str]]:
    if not grid:
        return [{}]
    keys = list(grid)
    return [dict(zip(keys, values)) for values in itertools.product(*(grid[k] for k in keys))]


def _init_worker(payload: dict) -> None:
    _SHARED.update(payload)
    _SHARED["bars"] = load_bars(
        payload["symbol"],
        payload["timeframe"],
        data=payload["data"],
        start=payload["start"],
        end=payload["end"],
        validate=False,
    )


def _run_one(combo: dict) -> dict:
    config = _SHARED["config"]
    params = dict(config.get("params") or {})
    params.update(combo)

    strategy = get_strategy(config["strategy"])(params)
    result = Backtester(
        strategy=strategy,
        symbol=_SHARED["symbol"],
        timeframe=_SHARED["timeframe"],
        instrument=cli.instrument_from(config, _SHARED["symbol"]),
        execution=cli.execution_from(config),
        engine=cli.engine_from(config),
    ).run(_SHARED["bars"])

    metrics = compute(result).to_dict()
    row = {k: v for k, v in combo.items()}
    for key in SORT_KEYS:
        row[key] = round(float(metrics.get(key, 0.0)), 4)
    return row


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Grid-search strategy parameters.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("--config", "-c", help="YAML or JSON run config")
    parser.add_argument(
        "--sweep",
        action="append",
        default=[],
        metavar="KEY=V1,V2",
        help="parameter values to try; repeatable",
    )
    parser.add_argument("--sort", default="sharpe", choices=SORT_KEYS, help="ranking metric")
    parser.add_argument("--asc", action="store_true", help="sort ascending (for drawdown)")
    parser.add_argument("--top", type=int, default=20, help="rows to print (default 20)")
    parser.add_argument("--min-trades", type=int, default=1, help="discard thin results")
    parser.add_argument("--out", help="write the full table to this CSV")
    parser.add_argument("--jobs", "-j", type=int, default=0, help="workers (0 = cpu count)")
    cli.add_strategy_args(parser)
    cli.add_data_args(parser)
    cli.add_execution_args(parser)
    args = parser.parse_args(argv)

    config = cli.merge(cli.load_config(args.config), args)
    cli.require(config, "strategy", "symbol", "timeframe")

    grid = parse_sweep(args.sweep)
    combos = combinations(grid)
    if not grid:
        raise SystemExit("Nothing to sweep. Add at least one --sweep key=v1,v2")

    # Fail fast on a bad parameter name rather than inside 200 workers.
    strategy_cls = get_strategy(config["strategy"])
    strategy_cls({**(config.get("params") or {}), **combos[0]})

    payload = {
        "config": config,
        "symbol": config["symbol"],
        "timeframe": config["timeframe"],
        "data": config.get("data") or cli.DEFAULT_DATA,
        "start": config.get("start"),
        "end": config.get("end"),
    }

    print(
        f"Sweeping {len(combos):,} combination(s) of "
        f"{', '.join(grid)} on {config['symbol']} {config['timeframe']}"
    )
    started = time.perf_counter()

    workers = args.jobs or 0
    if workers == 1 or len(combos) == 1:
        _init_worker(payload)
        rows = [_run_one(c) for c in combos]
    else:
        with ProcessPoolExecutor(
            max_workers=workers or None, initializer=_init_worker, initargs=(payload,)
        ) as pool:
            rows = list(pool.map(_run_one, combos))

    elapsed = time.perf_counter() - started
    rows = [r for r in rows if r["trades"] >= args.min_trades]
    if not rows:
        print(f"No combination produced at least {args.min_trades} trade(s).")
        return 1

    rows.sort(key=lambda r: r[args.sort], reverse=not args.asc)
    print(_table(rows[: args.top], list(grid)))
    print(f"\n{len(combos):,} runs in {elapsed:.1f}s ({elapsed / len(combos) * 1000:.0f} ms each)")

    if args.out:
        print(f"Full table: {save_sweep(rows, args.out)}")
    return 0


def _table(rows: list[dict], sweep_keys: list[str]) -> str:
    shown = sweep_keys + [
        "trades",
        "net_profit",
        "return_pct",
        "max_drawdown_pct",
        "win_rate_pct",
        "profit_factor",
        "sharpe",
    ]
    widths = {c: max(len(c), 9) for c in shown}
    header = "  " + "".join(f"{c:>{widths[c] + 2}}" for c in shown)
    lines = ["", header, "  " + "-" * (len(header) - 2)]
    for row in rows:
        cells = []
        for column in shown:
            value = row.get(column, "")
            if isinstance(value, float):
                cells.append(f"{value:>{widths[column] + 2},.2f}")
            else:
                cells.append(f"{str(value):>{widths[column] + 2}}")
        lines.append("  " + "".join(cells))
    return "\n".join(lines)


if __name__ == "__main__":
    raise SystemExit(main())
