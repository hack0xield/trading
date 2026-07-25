"""MetaTrader 5 MCP server.

Runs under the Wine Python that shares a prefix with the MT5 terminal, since the
MetaTrader5 package is a Windows-only IPC client. Transport is stdio, so nothing
may ever be printed to stdout except MCP protocol traffic.
"""

import sys

from fastmcp import FastMCP

import mt5client
from handlers import account, history, orders, positions, symbols

mcp = FastMCP("mt5")

READ_ONLY = bool(mt5client.load_config().get("read_only", False))


def _guard(fn, *args, **kwargs):
    """Ensure the terminal is up, then run a handler, turning errors into data."""
    if not mt5client.ensure_initialized():
        return {"error": "MetaTrader 5 terminal is not reachable or not logged in",
                **mt5client.error()}
    try:
        return fn(*args, **kwargs)
    except mt5client.MT5Error as exc:
        return exc.as_dict()
    except Exception as exc:  # a crashed tool would kill the whole session
        return {"error": f"{type(exc).__name__}: {exc}"}


def _trade_guard(fn, *args, **kwargs):
    if READ_ONLY:
        return {"error": "Server is in read_only mode; set read_only=false in config.json "
                         "to enable trading tools."}
    return _guard(fn, *args, **kwargs)


# --------------------------------------------------------------------------- account

@mcp.tool(annotations={"readOnlyHint": True})
def get_account() -> dict:
    """Get the current trading account: balance, equity, margin, free margin,
    leverage, currency, and whether the terminal is connected and allowed to trade."""
    return _guard(account.get_account)


# --------------------------------------------------------------------------- market data

@mcp.tool(annotations={"readOnlyHint": True})
def list_symbols(pattern: str | None = None, limit: int = 100) -> dict:
    """List tradable symbols. `pattern` filters with wildcards, e.g. "*EUR*" or "EURUSD*"."""
    return _guard(symbols.list_symbols, pattern, limit)


@mcp.tool(annotations={"readOnlyHint": True})
def get_symbol_info(symbol: str) -> dict:
    """Full contract specification for a symbol: digits, point size, spread, volume
    limits, margin requirements, trade/filling modes and session times."""
    return _guard(symbols.get_symbol_info, symbol)


@mcp.tool(annotations={"readOnlyHint": True})
def get_price(symbol: str) -> dict:
    """Current bid/ask quote and spread for a symbol."""
    return _guard(symbols.get_price, symbol)


@mcp.tool(annotations={"readOnlyHint": True})
def get_rates(symbol: str, timeframe: str = "H1", count: int = 100, start_pos: int = 0) -> dict:
    """OHLC candles for a symbol, newest last. Timeframe is one of M1 M5 M15 M30
    H1 H4 D1 W1 MN1 (and the other MT5 periods). `start_pos` offsets back from the
    current bar, so start_pos=0 ends at the live candle."""
    return _guard(symbols.get_rates, symbol, timeframe, count, start_pos)


@mcp.tool(annotations={"readOnlyHint": True})
def get_ticks(symbol: str, count: int = 100, minutes_back: int = 60) -> dict:
    """Raw tick history for a symbol from the last `minutes_back` minutes."""
    return _guard(symbols.get_ticks, symbol, count, minutes_back)


# --------------------------------------------------------------------------- positions

@mcp.tool(annotations={"readOnlyHint": True})
def get_positions(symbol: str | None = None) -> dict:
    """List open positions with entry price, volume, SL/TP and floating profit."""
    return _guard(positions.get_positions, symbol)


@mcp.tool(annotations={"destructiveHint": True})
def close_position(ticket: int, volume: float | None = None, deviation: int = 20) -> dict:
    """Close an open position by ticket, entirely or partially. Omit `volume` to
    close the whole position. This sends a real trade to the broker."""
    return _trade_guard(positions.close_position, ticket, volume, deviation)


@mcp.tool(annotations={"destructiveHint": True})
def close_all_positions(symbol: str | None = None) -> dict:
    """Close every open position, optionally only those on one symbol. This sends
    real trades to the broker — confirm with the user before calling."""
    return _trade_guard(positions.close_all, symbol)


@mcp.tool(annotations={"idempotentHint": True})
def modify_position(ticket: int, sl: float | None = None, tp: float | None = None) -> dict:
    """Set or change the stop loss / take profit of an open position. Pass an
    absolute price; omitted levels keep their current value, 0 removes a level."""
    return _trade_guard(positions.modify_position, ticket, sl, tp)


# --------------------------------------------------------------------------- orders

@mcp.tool(annotations={"destructiveHint": True})
def place_market_order(symbol: str, side: str, volume: float, sl: float | None = None,
                       tp: float | None = None, deviation: int = 20, comment: str = "mcp",
                       filling: str | None = None, dry_run: bool = False) -> dict:
    """Open a position at market. `side` is BUY or SELL, `volume` is in lots, `sl`
    and `tp` are absolute prices. Set dry_run=true to validate the request with the
    broker without trading. This sends real money orders — confirm the symbol,
    direction and volume with the user first."""
    return _trade_guard(orders.place_market_order, symbol, side, volume, sl, tp,
                        deviation, comment, filling, dry_run)


@mcp.tool(annotations={"destructiveHint": True})
def place_pending_order(symbol: str, order_type: str, volume: float, price: float,
                        sl: float | None = None, tp: float | None = None,
                        stoplimit: float | None = None, expiration: str = "GTC",
                        comment: str = "mcp", filling: str | None = None,
                        dry_run: bool = False) -> dict:
    """Place a pending order. `order_type` is BUY_LIMIT, SELL_LIMIT, BUY_STOP,
    SELL_STOP, BUY_STOP_LIMIT or SELL_STOP_LIMIT; `price` is the trigger price.
    Set dry_run=true to validate without placing."""
    return _trade_guard(orders.place_pending_order, symbol, order_type, volume, price,
                        sl, tp, stoplimit, expiration, comment, filling, dry_run)


@mcp.tool(annotations={"readOnlyHint": True})
def get_pending_orders(symbol: str | None = None) -> dict:
    """List pending (not yet triggered) orders."""
    return _guard(orders.get_pending_orders, symbol)


@mcp.tool(annotations={"idempotentHint": True})
def modify_pending_order(ticket: int, price: float | None = None, sl: float | None = None,
                         tp: float | None = None) -> dict:
    """Change the trigger price, stop loss or take profit of a pending order."""
    return _trade_guard(orders.modify_pending_order, ticket, price, sl, tp)


@mcp.tool(annotations={"destructiveHint": True})
def cancel_order(ticket: int) -> dict:
    """Cancel a pending order by ticket."""
    return _trade_guard(orders.cancel_order, ticket)


# --------------------------------------------------------------------------- history

@mcp.tool(annotations={"readOnlyHint": True})
def get_history_deals(days_back: int = 7, symbol: str | None = None,
                      date_from: str | None = None, date_to: str | None = None) -> dict:
    """Executed deals (fills) with realised profit, commission and swap. Defaults to
    the last 7 days; `date_from`/`date_to` accept ISO dates like 2026-07-01."""
    return _guard(history.get_deals, days_back, symbol, date_from, date_to)


@mcp.tool(annotations={"readOnlyHint": True})
def get_history_orders(days_back: int = 7, symbol: str | None = None,
                       date_from: str | None = None, date_to: str | None = None) -> dict:
    """Historical orders, including filled, cancelled and expired ones."""
    return _guard(history.get_orders_history, days_back, symbol, date_from, date_to)


if __name__ == "__main__":
    try:
        mcp.run()
    finally:
        mt5client.shutdown()
        sys.stderr.flush()
