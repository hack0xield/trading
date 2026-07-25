"""Market data: symbol discovery, quotes, candles and raw ticks."""

from datetime import datetime, timedelta, timezone

import MetaTrader5 as mt5

import mt5client
from constants import TIMEFRAMES


def list_symbols(pattern: str | None = None, limit: int = 100) -> dict:
    symbols = mt5client.call(mt5.symbols_get, pattern) if pattern else mt5client.call(mt5.symbols_get)
    items = [
        {
            "name": s.name,
            "description": s.description,
            "path": s.path,
            "visible": s.visible,
            "digits": s.digits,
            "spread": s.spread,
        }
        for s in symbols[:limit]
    ]
    return {"count": len(symbols), "returned": len(items), "symbols": items}


def get_symbol_info(symbol: str) -> dict:
    mt5client.select(symbol)
    info = mt5client.call(mt5.symbol_info, symbol)
    data = mt5client.to_dict(info)
    data["time"] = mt5client.ts(info.time)
    return data


def get_price(symbol: str) -> dict:
    """Current bid/ask for a symbol."""
    mt5client.select(symbol)
    tick = mt5client.call(mt5.symbol_info_tick, symbol)
    info = mt5client.call(mt5.symbol_info, symbol)
    return {
        "symbol": symbol,
        "time": mt5client.ts(tick.time),
        "bid": tick.bid,
        "ask": tick.ask,
        "last": tick.last,
        "volume": tick.volume,
        "spread_points": info.spread,
        "digits": info.digits,
    }


def get_rates(symbol: str, timeframe: str = "H1", count: int = 100, start_pos: int = 0) -> dict:
    """OHLC candles counting back from the most recent bar."""
    tf = TIMEFRAMES.get(timeframe.upper())
    if tf is None:
        raise mt5client.MT5Error(
            f"Unknown timeframe '{timeframe}'. Valid: {', '.join(TIMEFRAMES)}", -1
        )
    mt5client.select(symbol)
    rates = mt5client.call(mt5.copy_rates_from_pos, symbol, tf, start_pos, count, timeout=30)
    bars = [
        {
            "time": mt5client.ts(r["time"]),
            "open": float(r["open"]),
            "high": float(r["high"]),
            "low": float(r["low"]),
            "close": float(r["close"]),
            "tick_volume": int(r["tick_volume"]),
            "spread": int(r["spread"]),
            "real_volume": int(r["real_volume"]),
        }
        for r in rates
    ]
    return {"symbol": symbol, "timeframe": timeframe.upper(), "count": len(bars), "bars": bars}


def get_ticks(symbol: str, count: int = 100, minutes_back: int = 60) -> dict:
    """Raw ticks from the last `minutes_back` minutes, newest last."""
    mt5client.select(symbol)
    start = datetime.now(timezone.utc) - timedelta(minutes=minutes_back)
    ticks = mt5client.call(mt5.copy_ticks_from, symbol, start, count, mt5.COPY_TICKS_ALL, timeout=30)
    items = [
        {
            "time": mt5client.ts(t["time"]),
            "bid": float(t["bid"]),
            "ask": float(t["ask"]),
            "last": float(t["last"]),
            "volume": int(t["volume"]),
        }
        for t in ticks
    ]
    return {"symbol": symbol, "count": len(items), "ticks": items}
