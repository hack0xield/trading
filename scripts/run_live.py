#!/usr/bin/env python3
"""Trade a run config on the MT5 account until stopped.

**This script runs under the Wine Python**, like the fetcher. Use the wrapper:

    scripts/run-live.sh --config configs/strategies/mz50.yaml
    scripts/run-live.sh --config configs/strategies/mz50.yaml --paper

The config is the backtest's own. History from its `start` is replayed through
the backtest engine first, so the strategy reaches today in the state the
backtest would, and then trades each new bar. `--paper` keeps stepping the
backtest on live bars and sends nothing.

The session directory `runs-live/<strategy>_<symbol>_<magic>/` holds
`events.jsonl`, every event including each order before it is sent, and
`state.json`, the latest snapshot. Events also go to the console. A failure
to start is written to both files before the script exits.

The wrapper stops it on Ctrl-C or SIGTERM, by creating the stop file. Exit
status is 0 after a requested stop and 1 on failure. Credentials are read
from `mt5-mcp-server/config.json`.
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import zlib
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from backtester import cli  # noqa: E402
from backtester.core.types import Bar  # noqa: E402
from backtester.live import (  # noqa: E402
    BrokerUnavailable,
    ConsoleListener,
    JsonlListener,
    LiveRunner,
    Notifier,
    StateFile,
)
from backtester.live.broker import (  # noqa: E402
    DEFAULT_RETRY_MAX_SECONDS,
    DEFAULT_RETRY_SECONDS,
    trade_mode_name,
)
from backtester.live.events import ERROR  # noqa: E402
from backtester.live.state import DEFAULT_STALE_AFTER_DAYS  # noqa: E402
from backtester.strategies import get_strategy  # noqa: E402
from backtester.utils.timeframes import normalize  # noqa: E402
from backtester.utils.timeutil import parse_dt  # noqa: E402
from scripts.fetch_mt5 import (  # noqa: E402
    connect,
    fetch_range,
    select,
    timeframe_constant,
    to_bars,
)

LIVE_DIR = ROOT / "runs-live"
CONNECT_SECONDS = 120.0
#: Run in a child interpreter by `probe_terminal`.
PROBE = (
    "import sys; sys.path.insert(0, sys.argv[1])\n"
    "import MetaTrader5 as mt5\n"
    "from scripts.fetch_mt5 import connect\n"
    "connect(mt5)\n"
    "mt5.shutdown()\n"
)


class TerminalTimeout(SystemExit):
    """The terminal did not answer in time."""


class Mt5Feed:
    """Bars straight from the terminal, the one still forming last."""

    def __init__(self, mt5, symbol: str, timeframe: str, digits: int):
        self.mt5 = mt5
        self.symbol = symbol
        self.timeframe = normalize(timeframe)
        self.constant = timeframe_constant(mt5, timeframe)
        self.digits = digits

    def history(self, start: datetime) -> list[Bar]:
        # Bars carry the server clock, which runs hours ahead of UTC.
        end = datetime.now(timezone.utc) + timedelta(days=2)
        bars = fetch_range(self.mt5, self.symbol, self.timeframe, start, end, self.digits)
        forming = self.latest(1)[-1]
        return [b for b in bars if b.time < forming.time] + [forming]

    def latest(self, count: int) -> list[Bar]:
        rates = self.mt5.copy_rates_from_pos(self.symbol, self.constant, 0, count)
        if rates is None or not len(rates):
            raise BrokerUnavailable(f"copy_rates_from_pos: {self.mt5.last_error()}")
        return to_bars(rates, self.digits)


def default_magic(strategy: str, symbol: str) -> int:
    """A stable magic number per strategy and symbol."""
    return zlib.crc32(f"{strategy}:{symbol}".encode()) & 0x7FFFFFFF


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Trade a strategy on the MT5 account (run via scripts/run-live.sh).",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("--config", "-c", required=True, help="the backtest's YAML or JSON config")
    parser.add_argument("--param", "-p", action="append", metavar="KEY=VALUE",
                        help="strategy parameter override; repeatable")
    parser.add_argument("--start", help="replay from here instead of the config's start")

    trading = parser.add_argument_group("trading")
    trading.add_argument("--paper", action="store_true",
                         help="step the backtest on live bars and send nothing")
    trading.add_argument("--magic", type=int,
                         help="marks this runner's orders (default: from strategy and symbol)")
    trading.add_argument("--deviation", type=int, default=20,
                         help="accepted slippage on market orders, in points (default 20)")
    trading.add_argument("--poll", type=float, default=2.0,
                         help="seconds between polls of the terminal (default 2)")
    trading.add_argument("--retry-seconds", type=float, default=DEFAULT_RETRY_SECONDS,
                         help="first wait before resending what the broker answered 'not now', "
                              f"such as market closed (default {DEFAULT_RETRY_SECONDS:g})")
    trading.add_argument("--retry-max-seconds", type=float, default=DEFAULT_RETRY_MAX_SECONDS,
                         help="longest wait, which it doubles up to "
                              f"(default {DEFAULT_RETRY_MAX_SECONDS:g})")
    trading.add_argument("--allow-real", action="store_true",
                         help="permit a real-money account; refused otherwise")

    output = parser.add_argument_group("reporting")
    output.add_argument("--margin-stale-days", type=int, default=DEFAULT_STALE_AFTER_DAYS,
                        help="margin readings older than this are reported stale "
                             f"(default {DEFAULT_STALE_AFTER_DAYS})")
    output.add_argument("--connect-timeout", type=float, default=CONNECT_SECONDS,
                        help="seconds to wait for the terminal to answer a login "
                             f"(default {CONNECT_SECONDS:g})")
    output.add_argument("--stop-file", help="exit cleanly once this file exists")
    output.add_argument("--quiet", "-q", action="store_true", help="no event lines on the console")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        config = cli.merge(cli.load_config(args.config), args)
        cli.require(config, "strategy", "symbol", "timeframe", "start")
        strategy_class = get_strategy(config["strategy"])
        symbol, timeframe = config["symbol"], normalize(config["timeframe"])
    except (Exception, SystemExit) as exc:
        print(f"run_live: {failure_text(exc)}", file=sys.stderr)
        return 1

    name = strategy_class.name
    magic = args.magic if args.magic is not None else default_magic(name, symbol)
    session = LIVE_DIR / f"{name}_{symbol}_{magic}"
    notify = Notifier(name, symbol)
    if not args.quiet:
        notify.add(ConsoleListener())
    notify.add(JsonlListener(session / "events.jsonl"))
    state = StateFile(
        session / "state.json", notify,
        pid=int(os.environ.get("RUN_LIVE_PID") or os.getpid()), strategy=name, symbol=symbol,
        timeframe=timeframe, config=args.config, magic=magic, paper=args.paper,
    )
    state.write(running=True)

    mt5 = None
    try:
        mt5 = import_mt5()
        runner = prepare(mt5, args, config, strategy_class, symbol, timeframe, magic,
                         notify, state, session)
    except (Exception, SystemExit) as exc:
        reason = failure_text(exc)
        notify.emit(ERROR, error=reason, retrying=False)
        state.write(running=False, failure=reason)
        print(f"run_live: {reason}", file=sys.stderr)
        if mt5 is not None and not isinstance(exc, TerminalTimeout):
            mt5.shutdown()
        return 1

    try:
        runner.run()
    except Exception:
        return 1                 # the runner has reported it
    finally:
        mt5.shutdown()
    return 0


def prepare(mt5, args, config, strategy_class, symbol, timeframe, magic, notify, state,
            session) -> LiveRunner:
    """Everything between a known session and trading: connect, check, build the runner."""
    strategy = strategy_class(config.get("params"))
    if getattr(strategy.p, "place_orders", True) is False:
        print("  ! place_orders is false: the strategy will trade nothing")

    probe_terminal(args.connect_timeout)
    connect(mt5)
    info = select(mt5, symbol)
    account, terminal = mt5.account_info(), mt5.terminal_info()
    check_account(mt5, account, terminal, args)
    instrument = cli.instrument_from(config, symbol)
    if instrument.digits != int(info.digits):
        raise SystemExit(
            f"{symbol} spec has {instrument.digits} digits, the broker quotes {info.digits}. "
            f"Refresh it: scripts/fetch-mt5.sh --symbol {symbol} --dump-spec --timeframe D1"
        )

    stop_file = Path(args.stop_file) if args.stop_file else session / "stop"
    print(
        f"{strategy.describe()} on {symbol} {timeframe}\n"
        f"  account  {account.login} @ {account.server} "
        f"({trade_mode_name(mt5, account.trade_mode)})\n"
        f"  magic    {magic}{'   PAPER: nothing is sent' if args.paper else ''}\n"
        f"  session  {session}\n"
        f"  stop     create {stop_file}",
        flush=True,
    )
    return LiveRunner(
        mt5=mt5,
        feed=Mt5Feed(mt5, symbol, timeframe, int(info.digits)),
        strategy=strategy,
        symbol=symbol,
        timeframe=timeframe,
        start=parse_dt(config["start"]),
        magic=magic,
        notify=notify,
        instrument=instrument,
        execution=cli.execution_from(config),
        engine=cli.engine_from(config),
        deviation=args.deviation,
        poll_seconds=args.poll,
        paper=args.paper,
        stop_file=stop_file,
        state=state,
        margin_stale_days=args.margin_stale_days,
        retry_seconds=args.retry_seconds,
        retry_max_seconds=args.retry_max_seconds,
        reconnect=lambda: (probe_terminal(args.connect_timeout), connect(mt5)),
    )


def probe_terminal(seconds: float) -> None:
    """Log in from a child interpreter first, giving up after `seconds`.

    A terminal that does not answer blocks `MetaTrader5` with the interpreter
    lock held, so only a separate process can be timed out.
    """
    child = subprocess.Popen([sys.executable, "-c", PROBE, str(ROOT)],
                             stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
    try:
        _, errors = child.communicate(timeout=seconds)
    except subprocess.TimeoutExpired:
        child.kill()
        raise TerminalTimeout(
            f"the MT5 terminal did not answer a login within {seconds:g} s; "
            f"is it running and free of dialogs?"
        )
    if child.returncode != 0:
        lines = (errors or "").strip().splitlines()
        raise SystemExit(lines[-1] if lines else f"terminal login failed ({child.returncode})")


def import_mt5():
    try:
        import MetaTrader5
    except ImportError:
        raise SystemExit(
            "MetaTrader5 is not importable here; this script runs under the Wine Python, "
            "through scripts/run-live.sh"
        )
    return MetaTrader5


def check_account(mt5, account, terminal, args) -> None:
    """Refuse to start where every order would fail, or where the money is real."""
    if account is None or terminal is None:
        raise SystemExit(f"Terminal not logged in: {mt5.last_error()}")
    if args.paper:
        return
    if account.trade_mode == mt5.ACCOUNT_TRADE_MODE_REAL and not args.allow_real:
        raise SystemExit(
            f"Account {account.login} is a real-money account. This runner is for test "
            f"accounts; --allow-real overrides."
        )
    if not account.trade_allowed:
        raise SystemExit(f"Account {account.login} does not allow trading (investor password?).")
    if not terminal.trade_allowed:
        raise SystemExit(
            "Algo Trading is off in the terminal (toolbar button, or Ctrl+E), so every "
            "order would be rejected with retcode 10027."
        )


def failure_text(exc: BaseException) -> str:
    """A startup failure as one line: a refusal's own words, an exception with its type."""
    if isinstance(exc, SystemExit):
        return str(exc.code)
    return f"{type(exc).__name__}: {exc}"


if __name__ == "__main__":
    raise SystemExit(main())
