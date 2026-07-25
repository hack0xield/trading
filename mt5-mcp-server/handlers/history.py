"""Closed trade history."""

from datetime import datetime, timedelta, timezone

import MetaTrader5 as mt5

import mt5client
from constants import DEAL_TYPE_NAMES, ORDER_TYPE_NAMES


def _range(days_back: int, date_from: str | None, date_to: str | None):
    if date_from:
        start = datetime.fromisoformat(date_from).replace(tzinfo=timezone.utc)
    else:
        start = datetime.now(timezone.utc) - timedelta(days=days_back)
    end = (
        datetime.fromisoformat(date_to).replace(tzinfo=timezone.utc)
        if date_to
        else datetime.now(timezone.utc) + timedelta(days=1)
    )
    return start, end


def get_deals(days_back: int = 7, symbol: str | None = None,
              date_from: str | None = None, date_to: str | None = None) -> dict:
    """Executed deals — the actual fills, including balance operations."""
    start, end = _range(days_back, date_from, date_to)
    deals = mt5.history_deals_get(start, end, group=symbol) if symbol \
        else mt5.history_deals_get(start, end)
    if deals is None:
        return {"count": 0, "total_profit": 0.0, "deals": []}
    items = []
    for d in deals:
        data = mt5client.to_dict(d)
        data["type"] = DEAL_TYPE_NAMES.get(d.type, d.type)
        data["time"] = mt5client.ts(d.time)
        items.append(data)
    return {
        "count": len(items),
        "total_profit": round(sum(d.profit + d.commission + d.swap for d in deals), 2),
        "from": start.isoformat(),
        "to": end.isoformat(),
        "deals": items,
    }


def get_orders_history(days_back: int = 7, symbol: str | None = None,
                       date_from: str | None = None, date_to: str | None = None) -> dict:
    """Historical orders — including ones that were cancelled or expired."""
    start, end = _range(days_back, date_from, date_to)
    orders = mt5.history_orders_get(start, end, group=symbol) if symbol \
        else mt5.history_orders_get(start, end)
    if orders is None:
        return {"count": 0, "orders": []}
    items = []
    for o in orders:
        data = mt5client.to_dict(o)
        data["type"] = ORDER_TYPE_NAMES.get(o.type, o.type)
        data["time_setup"] = mt5client.ts(o.time_setup)
        data["time_done"] = mt5client.ts(o.time_done)
        items.append(data)
    return {"count": len(items), "from": start.isoformat(), "to": end.isoformat(), "orders": items}
