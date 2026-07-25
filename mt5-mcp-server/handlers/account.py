"""Account-level queries."""

import MetaTrader5 as mt5

import mt5client


def get_account() -> dict:
    acc = mt5client.call(mt5.account_info)
    term = mt5client.call(mt5.terminal_info)
    data = mt5client.to_dict(acc)
    data["terminal"] = {
        "connected": term.connected,
        "trade_allowed": term.trade_allowed,
        "build": term.build,
        "path": term.path,
    }
    return data
