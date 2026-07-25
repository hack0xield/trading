"""Open position queries and management."""

import MetaTrader5 as mt5

import mt5client
from constants import POSITION_TYPE_NAMES
from handlers import orders


def get_positions(symbol: str | None = None) -> dict:
    found = mt5.positions_get(symbol=symbol) if symbol else mt5.positions_get()
    if found is None:
        return {"count": 0, "total_profit": 0.0, "positions": []}
    items = []
    for p in found:
        data = mt5client.to_dict(p)
        data["type"] = POSITION_TYPE_NAMES.get(p.type, p.type)
        data["time"] = mt5client.ts(p.time)
        items.append(data)
    return {
        "count": len(items),
        "total_profit": round(sum(p.profit for p in found), 2),
        "positions": items,
    }


def close_position(ticket: int, volume: float | None = None, deviation: int = 20) -> dict:
    found = mt5.positions_get(ticket=ticket)
    if not found:
        raise mt5client.MT5Error(f"No open position with ticket {ticket}", -1)
    pos = found[0]

    # Closing means sending the opposite deal against this position ticket.
    close_type = mt5.ORDER_TYPE_SELL if pos.type == mt5.POSITION_TYPE_BUY else mt5.ORDER_TYPE_BUY
    tick = mt5client.call(mt5.symbol_info_tick, pos.symbol)
    price = tick.bid if pos.type == mt5.POSITION_TYPE_BUY else tick.ask

    request = {
        "action": mt5.TRADE_ACTION_DEAL,
        "symbol": pos.symbol,
        "volume": float(volume) if volume else pos.volume,
        "type": close_type,
        "position": int(ticket),
        "price": price,
        "deviation": int(deviation),
        "magic": pos.magic,
        "comment": "mcp close",
        "type_time": mt5.ORDER_TIME_GTC,
        "type_filling": orders.pick_filling(pos.symbol),
    }
    return orders.send(request)


def modify_position(ticket: int, sl: float | None = None, tp: float | None = None) -> dict:
    found = mt5.positions_get(ticket=ticket)
    if not found:
        raise mt5client.MT5Error(f"No open position with ticket {ticket}", -1)
    pos = found[0]
    request = {
        "action": mt5.TRADE_ACTION_SLTP,
        "symbol": pos.symbol,
        "position": int(ticket),
        "sl": float(sl) if sl is not None else pos.sl,
        "tp": float(tp) if tp is not None else pos.tp,
    }
    return orders.send(request)


def close_all(symbol: str | None = None) -> dict:
    found = mt5.positions_get(symbol=symbol) if symbol else mt5.positions_get()
    if not found:
        return {"closed": 0, "results": []}
    results = [
        {"ticket": p.ticket, "symbol": p.symbol, **close_position(p.ticket)} for p in found
    ]
    return {"closed": sum(1 for r in results if r.get("success")), "results": results}
