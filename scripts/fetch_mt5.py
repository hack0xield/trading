#!/usr/bin/env python3
"""Pull bar history out of the MetaTrader 5 terminal into a store.

**This script runs under the Wine Python, not the Linux one.** The MetaTrader5
package is a Windows IPC client, so use the wrapper:

    scripts/fetch-mt5.sh --symbol XAUUSD --timeframe M15 --start 2020-01-01

It therefore sticks to the standard library plus MetaTrader5, and writes CSV by
default — pyarrow is not installed in the Wine prefix. Convert to Parquet
afterwards on the Linux side, which is one command:

    scripts/manage_data.py convert --from csv://data/bars --to parquet://data/bars

Credentials are read from `mt5-mcp-server/config.json`, the same file the MCP
server uses, so there is only ever one copy of the password.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from backtester.core.instrument import Instrument, save_instrument  # noqa: E402
from backtester.core.types import Bar  # noqa: E402
from backtester.data.csv_store import CsvBarStore  # noqa: E402
from backtester.utils.timeframes import TIMEFRAME_SECONDS, normalize  # noqa: E402
from backtester.utils.timeutil import parse_dt  # noqa: E402

CONFIG_PATH = ROOT / "mt5-mcp-server" / "config.json"


BROKER_CLOCK_PATH = ROOT / "configs" / "broker.json"

#: Real server clocks run between these. A tick left stale over a weekend
#: differences into something far outside the range, which is what makes the
#: check worth having.
OFFSET_BOUNDS = (-12.0, 14.0)


def server_offset_hours(server_epoch: int, now_utc: datetime) -> float:
    """Hours the terminal's clock runs ahead of true UTC.

    MT5 reports every time — ticks and bars alike — as the server's *wall
    clock* encoded as though it were UTC. Differencing one against real UTC
    therefore measures the server's offset directly.

    Snapped to a quarter hour: no exchange runs on a stranger offset, and the
    raw difference carries network latency and whatever drift the local clock
    has.
    """
    raw = (server_epoch - now_utc.timestamp()) / 3600.0
    return round(raw * 4) / 4


def plausible_offset(hours: float) -> bool:
    return OFFSET_BOUNDS[0] <= hours <= OFFSET_BOUNDS[1]


def read_server_clock(mt5, symbol: str, now_utc: datetime | None = None) -> dict | None:
    """Measure the broker's clock from its latest tick, or None if it cannot.

    **This measurement assumes the market is open.** One tick gives one number
    and there are two unknowns in it — the clock offset and how stale the tick
    is — so a quote left sitting since Friday's close is indistinguishable from
    a live quote on a clock that many hours behind. There is no way to separate
    them from a single reading.

    What that does rule out is a *long* staleness: a weekend-old tick differences
    into tens of hours, far outside any real timezone, and is rejected. A tick a
    couple of hours old is the case that survives, so `main` compares the result
    against the previously recorded value and says so when they disagree rather
    than quietly replacing a good measurement with one taken after the close.
    """
    now_utc = now_utc or datetime.now(timezone.utc)
    tick = mt5.symbol_info_tick(symbol)
    if tick is None or not getattr(tick, "time", 0):
        return None

    offset = server_offset_hours(int(tick.time), now_utc)
    if not plausible_offset(offset):
        return None

    account = mt5.account_info()
    terminal = mt5.terminal_info()
    return {
        "utc_offset_hours": offset,
        "server": getattr(account, "server", "") if account else "",
        "company": getattr(terminal, "company", "") if terminal else "",
        "measured_at": now_utc.replace(microsecond=0).isoformat(),
        "measured_from": symbol,
        "_note": (
            "Hours the broker's server clock runs ahead of UTC, AS AT "
            "`measured_at`. MT5 stamps bars with that clock and labels them UTC, "
            "so bar timestamps in data/ are this many hours ahead of the real "
            "time they describe. Server clocks usually observe daylight saving "
            "(MetaQuotes-Demo runs EET/EEST: UTC+2 in winter, UTC+3 in summer), "
            "so this is the offset now, not a constant for the whole history -- "
            "do not subtract it from an old timestamp. Written on every fetch, "
            "and only trustworthy when the market was open at the time."
        ),
    }


def recorded_offset(path: Path = None) -> float | None:
    """The offset already on file, if any."""
    file = Path(path or BROKER_CLOCK_PATH)
    if not file.exists():
        return None
    try:
        value = json.loads(file.read_text(encoding="utf-8")).get("utc_offset_hours")
    except (OSError, ValueError):
        return None
    return float(value) if isinstance(value, (int, float)) else None


def write_server_clock(clock: dict, path: Path = BROKER_CLOCK_PATH) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(clock, indent=2) + "\n", encoding="utf-8")
    return path


def connect(mt5) -> None:
    config = {}
    if CONFIG_PATH.exists():
        config = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))

    kwargs = {}
    if config.get("mt5_path"):
        kwargs["path"] = config["mt5_path"]
    if config.get("login"):
        kwargs["login"] = int(config["login"])
    if config.get("password"):
        kwargs["password"] = config["password"]
    if config.get("server"):
        kwargs["server"] = config["server"]
    kwargs["timeout"] = int(config.get("timeout", 60)) * 1000

    if not mt5.initialize(**kwargs):
        raise SystemExit(f"MT5 initialize failed: {mt5.last_error()}")


def select(mt5, symbol: str):
    """Make sure a symbol is usable, and return its info.

    `symbol_select` is only called when the symbol is not already in Market
    Watch. Calling it on an already-visible symbol fails under Wine with a
    spurious `(-3, 'Terminal: Out of memory')` — the host has gigabytes free and
    `copy_rates_range` works perfectly regardless. Treating that as fatal is
    what made the first version of this script unable to fetch anything.

    `symbol_info` returning None is the real "unknown symbol" signal.
    """
    info = mt5.symbol_info(symbol)
    if info is None:
        raise SystemExit(
            f"Unknown symbol {symbol}: {mt5.last_error()}\n"
            f"Try: scripts/fetch-mt5.sh --symbol {symbol[:3]} --list"
        )
    if not info.visible:
        if not mt5.symbol_select(symbol, True):
            raise SystemExit(f"Cannot add {symbol} to Market Watch: {mt5.last_error()}")
        info = mt5.symbol_info(symbol) or info
    return info


def timeframe_constant(mt5, timeframe: str) -> int:
    name = f"TIMEFRAME_{normalize(timeframe)}"
    constant = getattr(mt5, name, None)
    if constant is None:
        raise SystemExit(f"MT5 has no {name}")
    return constant


def to_bars(rates, digits: int) -> list[Bar]:
    """MT5 rate rows -> Bars. Spread arrives in points, stored in price units."""
    point = 10.0 ** -digits
    return [
        Bar(
            time=datetime.fromtimestamp(int(r["time"]), tz=timezone.utc),
            open=float(r["open"]),
            high=float(r["high"]),
            low=float(r["low"]),
            close=float(r["close"]),
            volume=int(r["tick_volume"]),
            spread=float(r["spread"]) * point,
        )
        for r in rates
    ]


def fetch_range(mt5, symbol: str, timeframe: str, start: datetime, end: datetime, digits: int):
    """Walk the range in chunks.

    The terminal caps how many bars it will return in one call (and how many it
    holds at all — see Tools > Options > Charts > "Max bars in chart"), so a
    multi-year M1 pull has to be requested piecewise or it comes back
    truncated, silently.
    """
    seconds = TIMEFRAME_SECONDS[normalize(timeframe)]
    chunk = timedelta(seconds=seconds * 20_000)
    constant = timeframe_constant(mt5, timeframe)

    collected: list[Bar] = []
    cursor = start
    while cursor < end:
        window_end = min(cursor + chunk, end)
        rates = mt5.copy_rates_range(symbol, constant, cursor, window_end)
        if rates is not None and len(rates):
            collected.extend(to_bars(rates, digits))
            print(
                f"  {cursor:%Y-%m-%d} .. {window_end:%Y-%m-%d}  {len(rates):>7,} bars",
                flush=True,
            )
        else:
            print(f"  {cursor:%Y-%m-%d} .. {window_end:%Y-%m-%d}  (none)", flush=True)
        cursor = window_end
    return collected


def tick_value_of(mt5, info, tick_size: float) -> float:
    """The account-currency value of a one-tick move on one lot.

    Three sources, in decreasing order of trustworthiness:

    1. `order_calc_profit` — the broker's own P&L calculator, i.e. the thing
       that will actually settle your trades. Authoritative by definition.
    2. `contract_size * tick_size` — correct whenever the symbol is quoted in
       the account currency, which covers XAUUSD on a USD account.
    3. `trade_tick_value` as reported by the terminal.

    (3) is last on purpose. MetaQuotes-Demo reports `trade_tick_value = 0.1`
    for XAUUSD while `order_calc_profit` returns $100 for a $1 move on one lot
    — the correct tick value is 1.0. Trusting the reported field would have
    silently divided every P&L in this backtester by ten.
    """
    reported = float(getattr(info, "trade_tick_value", 0.0) or 0.0)
    derived = float(info.trade_contract_size) * tick_size

    measured = 0.0
    price = float(getattr(info, "ask", 0.0) or getattr(info, "bid", 0.0) or 0.0)
    if price > 0:
        profit = mt5.order_calc_profit(
            mt5.ORDER_TYPE_BUY, info.name, 1.0, price, price + tick_size
        )
        if profit:
            measured = abs(float(profit))

    chosen = measured or derived or reported
    for label, value in (("order_calc_profit", measured), ("contract_size*tick", derived),
                         ("terminal", reported)):
        if value and abs(value - chosen) / chosen > 0.01:
            print(
                f"  ! tick_value disagreement: {label} says {value:g}, using {chosen:g}. "
                f"Check the P&L scale before trusting absolute figures."
            )
    return chosen


def spec_from_mt5(mt5, info) -> Instrument:
    """Turn the terminal's symbol info into an Instrument spec.

    Worth doing once per symbol: the built-in defaults are typical retail
    values, while this is what your broker will actually charge you. The one
    field not taken at face value is `tick_value` — see `tick_value_of`.
    """
    tick_size = float(info.trade_tick_size or 10.0 ** -info.digits)
    return Instrument(
        symbol=info.name,
        digits=int(info.digits),
        contract_size=float(info.trade_contract_size),
        tick_size=tick_size,
        tick_value=tick_value_of(mt5, info, tick_size),
        min_volume=float(info.volume_min),
        max_volume=float(info.volume_max),
        volume_step=float(info.volume_step),
        spread_points=float(info.spread),
        swap_long=float(info.swap_long),
        swap_short=float(info.swap_short),
        currency=str(info.currency_profit),
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Download MT5 bar history (run via scripts/fetch-mt5.sh).",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("--symbol", "-s", required=True, help="e.g. XAUUSD")
    parser.add_argument(
        "--timeframe", "-t", default="M15", help="one or more, comma separated (M15,H1,D1)"
    )
    parser.add_argument("--start", default="2020-01-01", help="first bar")
    parser.add_argument("--end", default=None, help="last bar (default: now)")
    parser.add_argument(
        "--out",
        "-o",
        default="csv://data/bars",
        help="store URI (default csv://data/bars — the Wine prefix has no pyarrow)",
    )
    parser.add_argument(
        "--dump-spec",
        action="store_true",
        help="also write the broker's contract spec to configs/instruments/",
    )
    parser.add_argument("--list", action="store_true", help="list matching symbols and exit")
    args = parser.parse_args(argv)

    try:
        import MetaTrader5 as mt5
    except ImportError:
        raise SystemExit(
            "MetaTrader5 is not importable here.\n"
            "This script must run under the Wine Python:\n"
            "    scripts/fetch-mt5.sh " + " ".join(sys.argv[1:])
        )

    connect(mt5)
    try:
        if args.list:
            for s in mt5.symbols_get(args.symbol) or []:
                print(f"  {s.name:<16} {s.description}")
            return 0

        info = select(mt5, args.symbol)

        if args.dump_spec:
            path = save_instrument(spec_from_mt5(mt5, info))
            print(f"Wrote contract spec to {path}")

        # Measure the server clock while the terminal is open. Every bar this
        # script writes is stamped with that clock and labelled UTC, so without
        # the offset recorded here the timestamps in data/ cannot be turned back
        # into real times — and anything reading an hour off them, like a
        # trading-session breakdown, is guessing.
        clock = read_server_clock(mt5, args.symbol)
        if clock is None:
            print(
                "  ! could not measure the broker clock (market shut, or no recent "
                f"tick for {args.symbol}); leaving {BROKER_CLOCK_PATH.name} as it is"
            )
        else:
            was = recorded_offset()
            now_offset = clock["utc_offset_hours"]
            if was is not None and was != now_offset:
                print(
                    f"  ! broker clock now reads UTC{now_offset:+g}, was UTC{was:+g}. "
                    f"If the market was shut just now the new figure is a stale "
                    f"quote, not a clock change — check before trusting it."
                )
            write_server_clock(clock)
            print(
                f"Broker clock: UTC{now_offset:+g}"
                f"{' (' + clock['server'] + ')' if clock['server'] else ''}"
                f" -> {BROKER_CLOCK_PATH}"
            )

        start = parse_dt(args.start)
        end = parse_dt(args.end) if args.end else datetime.now(timezone.utc)

        # The store URI may name parquet/sql; CsvBarStore is the safe default
        # here because this process runs in the dependency-free Wine prefix.
        if args.out.startswith("csv://"):
            store = CsvBarStore(args.out[len("csv://") :])
        else:
            from backtester.data.registry import store_from_uri

            store = store_from_uri(args.out)

        for timeframe in [t.strip() for t in args.timeframe.split(",") if t.strip()]:
            print(f"{args.symbol} {normalize(timeframe)}  {start:%Y-%m-%d} -> {end:%Y-%m-%d}")
            bars = fetch_range(mt5, args.symbol, timeframe, start, end, int(info.digits))
            if not bars:
                print("  nothing returned — is the history downloaded in the terminal?")
                continue
            written = store.write(args.symbol, timeframe, bars)
            print(
                f"  stored {written:,} bars "
                f"({bars[0].time:%Y-%m-%d} .. {bars[-1].time:%Y-%m-%d}) in {store}\n"
            )
    finally:
        mt5.shutdown()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
