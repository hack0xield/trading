"""A stand-in for the `MetaTrader5` module: one symbol on one hedging account.

Market orders fill at the quote. `walk` plays a closed bar through the server
using `SimulatedBroker`'s own rules for stops, targets and limit fills, so a
live run against it can be compared with the backtest trade for trade.
"""

from __future__ import annotations

from types import SimpleNamespace as Row

from backtester.core.broker import ExecutionConfig, SimulatedBroker
from backtester.core.instrument import Instrument
from backtester.core.types import Bar, ExitReason, Position, Side


class FakeMT5:
    TRADE_ACTION_DEAL = 1
    TRADE_ACTION_PENDING = 5
    TRADE_ACTION_SLTP = 6
    TRADE_ACTION_REMOVE = 8
    ORDER_TYPE_BUY = 0
    ORDER_TYPE_SELL = 1
    ORDER_TYPE_BUY_LIMIT = 2
    ORDER_TYPE_SELL_LIMIT = 3
    ORDER_TIME_GTC = 0
    ORDER_FILLING_FOK = 0
    ORDER_FILLING_IOC = 1
    ORDER_FILLING_RETURN = 2
    ORDER_STATE_CANCELED = 2
    ORDER_STATE_FILLED = 4
    POSITION_TYPE_BUY = 0
    POSITION_TYPE_SELL = 1
    DEAL_ENTRY_IN = 0
    DEAL_ENTRY_OUT = 1
    DEAL_ENTRY_OUT_BY = 3
    DEAL_REASON_CLIENT = 0
    DEAL_REASON_EXPERT = 3
    DEAL_REASON_SL = 4
    DEAL_REASON_TP = 5
    DEAL_REASON_SO = 6
    TRADE_RETCODE_PLACED = 10008
    TRADE_RETCODE_DONE = 10009
    TRADE_RETCODE_REJECT = 10006
    ACCOUNT_TRADE_MODE_DEMO = 0
    ACCOUNT_TRADE_MODE_CONTEST = 1
    ACCOUNT_TRADE_MODE_REAL = 2

    def __init__(self, instrument: Instrument, execution: ExecutionConfig,
                 balance: float = 10_000.0):
        self.instrument = instrument
        self.symbol = instrument.symbol
        self.rules = SimulatedBroker(instrument, execution)
        self.balance = balance
        self.bid = self.ask = 0.0
        self.time = 0
        self.spread = 0.0
        self.positions: dict[int, Row] = {}
        self.orders: dict[int, Row] = {}
        self.order_history: dict[int, Row] = {}
        self.deals: list[Row] = []
        self.requests: list[dict] = []
        self.reject_next = False
        self.online = True
        self._ticket = 1000

    # ------------------------------------------------------------- the market

    def open_bar(self, bar: Bar) -> None:
        """Quote the open of a bar that has just started."""
        self.spread = self.rules._spread(bar)
        self._quote(bar.open, bar)

    def walk(self, bar: Bar) -> None:
        """The bar has closed: stops and targets first, then resting limits."""
        for row in list(self.positions.values()):
            position = Position(
                id=row.ticket, side=_side(row.type), volume=row.volume, entry_time=bar.time,
                entry_price=row.price_open, sl=row.sl or None, tp=row.tp or None,
            )
            hit = self.rules._exit_within_bar(position, bar)
            if hit is not None:
                reason, price = hit
                why = self.DEAL_REASON_SL if reason is ExitReason.STOP_LOSS else self.DEAL_REASON_TP
                self._close(row, price, why, bar.time)
        for order in list(self.orders.values()):
            buy = order.type == self.ORDER_TYPE_BUY_LIMIT
            if buy:
                reached = bar.low + self.spread <= order.price_open
            else:
                reached = bar.high >= order.price_open
            if reached:
                del self.orders[order.ticket]
                order.state = self.ORDER_STATE_FILLED
                row = self._open(self.ORDER_TYPE_BUY if buy else self.ORDER_TYPE_SELL,
                                 order.volume_current, order.price_open, order.sl, order.tp,
                                 order.magic, order.comment, bar.time, order.ticket)
                order.position_id = row.ticket
        self._quote(bar.close, bar)

    def _quote(self, bid: float, bar: Bar) -> None:
        self.bid = bid
        self.ask = self.instrument.round_price(bid + self.spread)
        self.time = int(bar.time.timestamp())

    # ------------------------------------------------------------ the module

    def last_error(self):
        return (1, "Success") if self.online else (-10004, "No IPC connection")

    def account_info(self):
        if not self.online:
            return None
        return Row(login=1, server="Fake", balance=self.balance, equity=self.balance,
                   trade_mode=self.ACCOUNT_TRADE_MODE_DEMO, trade_allowed=True)

    def symbol_info(self, symbol):
        return Row(name=symbol, digits=self.instrument.digits, filling_mode=2)

    def symbol_info_tick(self, symbol):
        if not self.online:
            return None
        return Row(bid=self.bid, ask=self.ask, time=self.time)

    def positions_get(self, symbol=None, ticket=None):
        if not self.online:
            return None
        rows = list(self.positions.values())
        return tuple(r for r in rows if ticket is None or r.ticket == ticket)

    def orders_get(self, symbol=None):
        return tuple(self.orders.values()) if self.online else None

    def history_orders_get(self, ticket=None):
        found = self.order_history.get(ticket)
        return (found,) if found else ()

    def history_deals_get(self, ticket=None, position=None):
        return tuple(d for d in self.deals
                     if (ticket is None or d.ticket == ticket)
                     and (position is None or d.position_id == position))

    def order_send(self, request: dict):
        self.requests.append(dict(request))
        if self.reject_next:
            self.reject_next = False
            return Row(retcode=self.TRADE_RETCODE_REJECT, comment="Request rejected",
                       order=0, deal=0, price=0.0, volume=0.0)
        action = request["action"]
        if action == self.TRADE_ACTION_DEAL and "position" in request:
            row = self.positions[request["position"]]
            deal = self._close(row, request["price"], self.DEAL_REASON_EXPERT, None)
            return self._done(deal.order, deal.ticket, deal.price, deal.volume)
        if action == self.TRADE_ACTION_DEAL:
            row = self._open(request["type"], request["volume"], request["price"], request["sl"],
                             request["tp"], request["magic"], request["comment"], None, None)
            return self._done(row.ticket, row.deal, row.price_open, row.volume)
        if action == self.TRADE_ACTION_PENDING:
            ticket = self._next()
            order = Row(ticket=ticket, type=request["type"], volume_current=request["volume"],
                        price_open=request["price"], sl=request["sl"], tp=request["tp"],
                        magic=request["magic"], comment=request["comment"],
                        state=1, position_id=0, symbol=self.symbol)
            self.orders[ticket] = self.order_history[ticket] = order
            return Row(retcode=self.TRADE_RETCODE_PLACED, comment="placed", order=ticket,
                       deal=0, price=0.0, volume=request["volume"])
        if action == self.TRADE_ACTION_SLTP:
            row = self.positions[request["position"]]
            row.sl, row.tp = request["sl"], request["tp"]
            return self._done(0, 0, 0.0, 0.0)
        if action == self.TRADE_ACTION_REMOVE:
            order = self.orders.pop(request["order"])
            order.state = self.ORDER_STATE_CANCELED
            return self._done(order.ticket, 0, 0.0, 0.0)
        raise ValueError(f"unexpected request {request}")

    # ---------------------------------------------------------------- helpers

    def hold(self, side: Side, price: float, sl: float, tp: float, magic: int, comment: str) -> Row:
        """Put a position on the account, as an earlier session would have."""
        kind = self.ORDER_TYPE_BUY if side is Side.BUY else self.ORDER_TYPE_SELL
        return self._open(kind, 0.1, price, sl, tp, magic, comment, None, None)

    def _open(self, kind, volume, price, sl, tp, magic, comment, time, order):
        ticket = order or self._next()
        stamp = int(time.timestamp()) if time else self.time
        deal = self._deal(ticket, ticket, self.DEAL_ENTRY_IN, kind, volume, price, 0.0, stamp, 0)
        row = Row(ticket=ticket, type=kind, volume=volume, price_open=price, sl=sl, tp=tp,
                  time=stamp, magic=magic, comment=comment, swap=0.0, symbol=self.symbol,
                  deal=deal.ticket)
        self.positions[ticket] = row
        return row

    def _close(self, row, price, reason, time):
        del self.positions[row.ticket]
        sign = 1 if row.type == self.ORDER_TYPE_BUY else -1
        profit = self.instrument.value_of((price - row.price_open) * sign, row.volume)
        self.balance += profit
        stamp = int(time.timestamp()) if time else self.time
        return self._deal(self._next(), row.ticket, self.DEAL_ENTRY_OUT, 1 - row.type,
                          row.volume, price, profit, stamp, reason)

    def _deal(self, order, position, entry, kind, volume, price, profit, time, reason):
        deal = Row(ticket=self._next(), order=order, position_id=position, entry=entry, type=kind,
                   volume=volume, price=price, profit=profit, commission=0.0, swap=0.0,
                   fee=0.0, time=time, reason=reason, symbol=self.symbol)
        self.deals.append(deal)
        return deal

    def _done(self, order, deal, price, volume):
        return Row(retcode=self.TRADE_RETCODE_DONE, comment="done", order=order, deal=deal,
                   price=price, volume=volume)

    def _next(self) -> int:
        self._ticket += 1
        return self._ticket


def _side(kind: int) -> Side:
    return Side.BUY if kind == FakeMT5.ORDER_TYPE_BUY else Side.SELL


class Tape:
    """Bars as a terminal sees them: everything up to `closed`, then one forming."""

    def __init__(self, mt5: FakeMT5, bars: list[Bar], closed: int):
        self.mt5, self.bars, self.closed = mt5, bars, closed
        mt5.open_bar(bars[closed])

    def history(self, start):
        return list(self.bars[: self.closed + 1])

    def latest(self, count: int):
        return list(self.bars[max(0, self.closed + 1 - count) : self.closed + 1])

    @property
    def done(self) -> bool:
        return self.closed + 1 >= len(self.bars)

    def advance(self) -> None:
        """Close the forming bar at the server and open the next one."""
        self.mt5.walk(self.bars[self.closed])
        self.closed += 1
        self.mt5.open_bar(self.bars[self.closed])
