#!/usr/bin/env python3
"""Generate synthetic bars, so the whole pipeline is runnable without MT5.

    scripts/make_synthetic.py --symbol XAUUSD --timeframe M15 \
        --start 2022-01-01 --end 2024-12-31

The series is a geometric random walk with an optional drift, skipping weekends
the way a real FX/metals feed does. It is **not** market data: no fat tails, no
volatility clustering, no news gaps, no session structure. Use it to check that
a strategy and the engine behave, and to sanity-check that a symmetric
stop/target strategy comes out near break-even before costs. Never use it to
judge whether an edge exists.
"""

from __future__ import annotations

import argparse
import math
import random
import sys
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from backtester.core.types import Bar  # noqa: E402
from backtester.data.registry import store_from_uri  # noqa: E402
from backtester.utils.timeframes import TIMEFRAME_SECONDS, normalize  # noqa: E402
from backtester.utils.timeutil import parse_dt  # noqa: E402


def generate(
    start: datetime,
    end: datetime,
    timeframe: str,
    price: float,
    annual_vol: float,
    annual_drift: float,
    spread: float,
    seed: int,
    skip_weekends: bool = True,
) -> list[Bar]:
    rng = random.Random(seed)
    step = TIMEFRAME_SECONDS[normalize(timeframe)]
    bars_per_year = 365 * 24 * 3600 / step

    # Per-bar parameters of the walk, from the annualised inputs.
    sigma = annual_vol / math.sqrt(bars_per_year)
    mu = annual_drift / bars_per_year - 0.5 * sigma**2

    bars: list[Bar] = []
    time = start
    delta = timedelta(seconds=step)

    while time <= end:
        # Friday 22:00 UTC to Sunday 22:00 UTC: no quotes.
        if skip_weekends and (
            time.weekday() == 5
            or (time.weekday() == 4 and time.hour >= 22)
            or (time.weekday() == 6 and time.hour < 22)
        ):
            time += delta
            continue

        open_ = price
        close = open_ * math.exp(mu + sigma * rng.gauss(0, 1))
        # Wicks: a fraction of the bar's own move, so range scales with volatility.
        reach = abs(close - open_) + open_ * sigma * abs(rng.gauss(0, 0.6))
        high = max(open_, close) + reach * rng.random() * 0.5
        low = min(open_, close) - reach * rng.random() * 0.5

        bars.append(
            Bar(
                time=time,
                open=round(open_, 2),
                high=round(high, 2),
                low=round(low, 2),
                close=round(close, 2),
                volume=rng.randint(50, 5000),
                spread=spread,
            )
        )
        price = close
        time += delta

    return bars


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Write a synthetic price series into a store.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("--symbol", "-s", default="XAUUSD")
    parser.add_argument("--timeframe", "-t", default="M15")
    parser.add_argument("--start", default="2022-01-01")
    parser.add_argument("--end", default="2024-12-31")
    parser.add_argument("--data", "-d", default="parquet://data/bars", help="store URI")
    parser.add_argument("--price", type=float, default=1800.0, help="starting price")
    parser.add_argument("--vol", type=float, default=0.15, help="annualised volatility")
    parser.add_argument("--drift", type=float, default=0.05, help="annualised drift")
    parser.add_argument("--spread", type=float, default=0.25, help="spread in price units")
    parser.add_argument("--seed", type=int, default=42)
    args = parser.parse_args(argv)

    bars = generate(
        start=parse_dt(args.start),
        end=parse_dt(args.end),
        timeframe=args.timeframe,
        price=args.price,
        annual_vol=args.vol,
        annual_drift=args.drift,
        spread=args.spread,
        seed=args.seed,
    )
    if not bars:
        print("Generated nothing — check the date range.")
        return 1

    store = store_from_uri(args.data)
    written = store.write(args.symbol, args.timeframe, bars)
    print(
        f"Wrote {written:,} synthetic {args.symbol} {normalize(args.timeframe)} bars "
        f"({bars[0].time:%Y-%m-%d} .. {bars[-1].time:%Y-%m-%d}, "
        f"{bars[0].close:,.2f} -> {bars[-1].close:,.2f}) to {store}"
    )
    print("Reminder: synthetic data proves the plumbing works, nothing more.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
