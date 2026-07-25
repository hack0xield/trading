"""Order execution: market orders, pending orders, modify and cancel."""

import MetaTrader5 as mt5

import mt5client
from constants import FILLING_MODES, ORDER_TYPE_NAMES, PENDING_TYPES, TIME_MODES


def pick_filling(symbol: str, requested: str | None = None) -> int:
    """Brokers only accept the filling modes advertised by the symbol.

    Guessing FOK on an IOC-only symbol is the usual cause of retcode 10030.
    """
    if requested:
        mode = FILLING_MODES.get(requested.upper())
        if mode is None:
            raise mt5client.MT5Error(
                f"Unknown filling mode '{requested}'. Valid: {', '.join(FILLING_MODES)}", -1
            )
        return mode
    info = mt5client.call(mt5.symbol_info, symbol)
    if info.filling_mode & 1:
        return mt5.ORDER_FILLING_FOK
    if info.filling_mode & 2:
        return mt5.ORDER_FILLING_IOC
    return mt5.ORDER_FILLING_RETURN


def send(request: dict) -> dict:
    result = mt5client.call(mt5.order_send, request, timeout=60)
    data = mt5client.to_dict(result)
    if "request" in data:
        data.pop("request")
    data["success"] = result.retcode == mt5.TRADE_RETCODE_DONE
    if not data["success"]:
        data["error"] = f"Trade request rejected: {result.comment} (retcode {result.retcode})"
    return data


def check(request: dict) -> dict:
    """Dry-run a request through the broker's validator without sending it."""
    result = mt5client.call(mt5.order_check, request, timeout=30)
    data = mt5client.to_dict(result)
    data.pop("request", None)
    data["valid"] = result.retcode == 0
    return data


def place_market_order(
    symbol: str,
    side: str,
    volume: float,
    sl: float | None = None,
    tp: float | None = None,
    deviation: int = 20,
    comment: str = "mcp",
    filling: str | None = None,
    dry_run: bool = False,
) -> dict:
    mt5client.select(symbol)
    side = side.upper()
    if side not in ("BUY", "SELL"):
        raise mt5client.MT5Error("side must be BUY or SELL", -1)

    tick = mt5client.call(mt5.symbol_info_tick, symbol)
    price = tick.ask if side == "BUY" else tick.bid
    request = {
        "action": mt5.TRADE_ACTION_DEAL,
        "symbol": symbol,
        "volume": float(volume),
        "type": mt5.ORDER_TYPE_BUY if side == "BUY" else mt5.ORDER_TYPE_SELL,
        "price": price,
        "deviation": int(deviation),
        "magic": 0,
        "comment": comment,
        "type_time": mt5.ORDER_TIME_GTC,
        "type_filling": pick_filling(symbol, filling),
    }
    if sl is not None:
        request["sl"] = float(sl)
    if tp is not None:
        request["tp"] = float(tp)
    return check(request) if dry_run else send(request)


def place_pending_order(
    symbol: str,
    order_type: str,
    volume: float,
    price: float,
    sl: float | None = None,
    tp: float | None = None,
    stoplimit: float | None = None,
    expiration: str = "GTC",
    comment: str = "mcp",
    filling: str | None = None,
    dry_run: bool = False,
) -> dict:
    mt5client.select(symbol)
    otype = PENDING_TYPES.get(order_type.upper())
    if otype is None:
        raise mt5client.MT5Error(
            f"Unknown order type '{order_type}'. Valid: {', '.join(PENDING_TYPES)}", -1
        )
    request = {
        "action": mt5.TRADE_ACTION_PENDING,
        "symbol": symbol,
        "volume": float(volume),
        "type": otype,
        "price": float(price),
        "magic": 0,
        "comment": comment,
        "type_time": TIME_MODES.get(expiration.upper(), mt5.ORDER_TIME_GTC),
        "type_filling": pick_filling(symbol, filling),
    }
    if sl is not None:
        request["sl"] = float(sl)
    if tp is not None:
        request["tp"] = float(tp)
    if stoplimit is not None:
        request["stoplimit"] = float(stoplimit)
    return check(request) if dry_run else send(request)


def get_pending_orders(symbol: str | None = None) -> dict:
    orders = mt5.orders_get(symbol=symbol) if symbol else mt5.orders_get()
    if orders is None:
        return {"count": 0, "orders": []}
    items = []
    for o in orders:
        data = mt5client.to_dict(o)
        data["type"] = ORDER_TYPE_NAMES.get(o.type, o.type)
        data["time_setup"] = mt5client.ts(o.time_setup)
        items.append(data)
    return {"count": len(items), "orders": items}


def modify_pending_order(
    ticket: int,
    price: float | None = None,
    sl: float | None = None,
    tp: float | None = None,
) -> dict:
    orders = mt5.orders_get(ticket=ticket)
    if not orders:
        raise mt5client.MT5Error(f"No pending order with ticket {ticket}", -1)
    order = orders[0]
    request = {
        "action": mt5.TRADE_ACTION_MODIFY,
        "order": int(ticket),
        "symbol": order.symbol,
        "price": float(price) if price is not None else order.price_open,
        "sl": float(sl) if sl is not None else order.sl,
        "tp": float(tp) if tp is not None else order.tp,
        "type_time": order.type_time,
    }
    return send(request)


def cancel_order(ticket: int) -> dict:
    return send({"action": mt5.TRADE_ACTION_REMOVE, "order": int(ticket)})
