"""MT5 terminal connection wrapper.

Every mt5 call goes through _run_with_timeout: the underlying library talks to
the terminal over IPC and can block indefinitely if the terminal is busy,
starting up, or showing a modal dialog. An MCP server that hangs is worse than
one that returns an error.
"""

import json
import os
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeout
from datetime import datetime, timezone

import MetaTrader5 as mt5

CONFIG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.json")

_pool = ThreadPoolExecutor(max_workers=2)
_config = None


def load_config() -> dict:
    global _config
    if _config is None:
        try:
            with open(CONFIG_PATH, "r", encoding="utf-8") as fh:
                _config = json.load(fh)
        except FileNotFoundError:
            _config = {}
    return _config


def _run_with_timeout(fn, *args, timeout=15, **kwargs):
    # order_send/order_check reject a call that carries a keyword dict at all,
    # even an empty one ("Unnamed arguments not allowed"), and both
    # submit(fn, *args) and partial() end up forwarding one. Hence the explicit
    # positional-only branch below.
    def invoke():
        if kwargs:
            return fn(*args, **kwargs)
        return fn(*args)

    future = _pool.submit(invoke)
    try:
        return future.result(timeout=timeout)
    except FutureTimeout:
        return None


def init_mt5() -> bool:
    cfg = load_config()
    kwargs = {}
    if cfg.get("mt5_path"):
        kwargs["path"] = cfg["mt5_path"]
    if cfg.get("login"):
        kwargs["login"] = int(cfg["login"])
    if cfg.get("password"):
        kwargs["password"] = cfg["password"]
    if cfg.get("server"):
        kwargs["server"] = cfg["server"]
    kwargs["timeout"] = int(cfg.get("timeout", 60)) * 1000

    ok = _run_with_timeout(lambda: mt5.initialize(**kwargs), timeout=90)
    return bool(ok)


def ensure_initialized() -> bool:
    """True if the terminal is reachable and logged in, initialising if needed."""
    info = _run_with_timeout(mt5.terminal_info)
    if info is None:
        return init_mt5()
    acc = _run_with_timeout(mt5.account_info)
    if acc is not None and acc.login != 0:
        return True
    return init_mt5()


def error() -> dict:
    """Last MT5 error as a dict, for returning from a failed tool call."""
    code, message = mt5.last_error()
    return {"error": message, "code": code}


def call(fn, *args, timeout=15, **kwargs):
    """Run an mt5 API call, raising MT5Error on failure so tools stay terse."""
    result = _run_with_timeout(fn, *args, timeout=timeout, **kwargs)
    if result is None:
        code, message = mt5.last_error()
        raise MT5Error(f"{fn.__name__} failed: {message}", code)
    return result


class MT5Error(Exception):
    def __init__(self, message, code=0):
        super().__init__(message)
        self.code = code

    def as_dict(self) -> dict:
        return {"error": str(self), "code": self.code}


def ts(value) -> str:
    """MT5 epoch seconds -> ISO8601 UTC. Terminal times are broker-server time."""
    try:
        return datetime.fromtimestamp(int(value), tz=timezone.utc).isoformat()
    except (ValueError, OverflowError, OSError):
        return str(value)


def to_dict(obj) -> dict:
    """Named tuples returned by the MT5 API -> plain JSON-serialisable dicts."""
    data = obj._asdict()
    for key, value in list(data.items()):
        if hasattr(value, "item"):
            data[key] = value.item()
    return data


def select(symbol: str) -> None:
    """Make sure a symbol is in Market Watch; quotes are empty otherwise."""
    info = _run_with_timeout(mt5.symbol_info, symbol)
    if info is None:
        raise MT5Error(f"Unknown symbol: {symbol}", -1)
    if not info.visible:
        if not _run_with_timeout(mt5.symbol_select, symbol, True):
            raise MT5Error(f"Could not select symbol: {symbol}", -1)


def shutdown() -> None:
    _run_with_timeout(mt5.shutdown, timeout=10)
