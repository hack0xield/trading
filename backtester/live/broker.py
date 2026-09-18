"""The broker a strategy trades through on a live MT5 account.

Presents the surface `SimulatedBroker` does — positions, pending orders, closed
trades, submit, modify, close — so a strategy runs against it unchanged. The
account's server holds stops and targets; `sync` reads back what it did.

Only positions and orders on `symbol` carrying `magic` belong to this broker,
and nothing is sent while the terminal is logged in to any account but
`login`. The broker's answer to a send is one of four: done; refused; not now,
kept and sent again after `retry_seconds`; or possibly done, looked for at the
broker before it is counted or sent again.

`mt5` is the `MetaTrader5` module, or anything with the same calls.
"""

from __future__ import annotations

import time as _time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Callable

from ..core.instrument import Instrument
from ..core.types import Bar, EquityPoint, ExitReason, OrderRequest, Position, Side, Trade
from ..utils.timeutil import from_epoch
from .events import (
    ERROR,
    EXIT_INTENT,
    ORDER_CANCELLED,
    ORDER_FILLED,
    ORDER_INTENT,
    ORDER_PLACED,
    ORDER_REJECTED,
    POSITION_CLOSED,
    POSITION_MODIFIED,
    Notifier,
)

#: MT5 caps order comments at 31 characters.
COMMENT_LENGTH = 31
RES_S_OK = 1
#: How far either side of now to look for the deal of an unanswered send. Deal
#: times carry the server clock, hours off UTC.
DEAL_WINDOW = timedelta(days=2)

#: What became of a request.
DONE = "done"
REFUSED = "refused"
UNANSWERED = "unanswered"        # it may have been carried out
LATER = "later"                  # it was not carried out, and may be shortly

#: Answers, by their `TRADE_RETCODE_` name, for a request certainly not carried
#: out that the same request may get past shortly.
NOT_NOW = (
    "MARKET_CLOSED", "PRICE_OFF", "REQUOTE", "PRICE_CHANGED", "TOO_MANY_REQUESTS",
    "TRADE_DISABLED", "SERVER_DISABLES_AT", "CLIENT_DISABLES_AT", "FROZEN",
)
#: Answers after which the request may have been carried out, wholly or in part.
MAYBE_DONE = ("TIMEOUT", "CONNECTION", "LOCKED", "DONE_PARTIAL")
#: Not-now answers someone has to act on, and what to tell them.
NEEDS_ACTION = {
    "CLIENT_DISABLES_AT": "Algo Trading is off in the terminal",
    "SERVER_DISABLES_AT": "the broker has disabled automated trading",
    "TRADE_DISABLED": "trading is disabled for the symbol or the account",
}
DEFAULT_RETRY_SECONDS = 15.0


class BrokerUnavailable(RuntimeError):
    """Nothing can be sent or read right now; the call can be retried later."""

    detail: dict = {}


class AccountChanged(BrokerUnavailable):
    """The terminal is logged in to another account than the one traded."""

    def __init__(self, expected: int, actual: int):
        super().__init__(f"terminal is logged in to account {actual}, not {expected}")
        self.detail = {"expected_login": expected, "actual_login": actual}


@dataclass(slots=True)
class Queued:
    """An order waiting to be sent."""

    order: OrderRequest
    announced: bool = False      # its order_intent has gone out
    unanswered: bool = False     # a send may have been carried out, so look before resending
    problem: str = ""            # why it is still waiting
    not_before: float = 0.0      # clock time before which it is not sent again


@dataclass(slots=True)
class Resting:
    """A limit order sitting at the broker, and its ticket there."""

    order: OrderRequest
    ticket: int
    withdraw: str = ""           # why it is being removed; empty while it stands
    refused: bool = False        # the broker has refused its removal once


@dataclass(frozen=True, slots=True)
class Answer:
    status: str
    result: object = None
    reason: str = ""


def comment_for(tag: str) -> str:
    return (tag or "")[:COMMENT_LENGTH]


def trade_mode_name(mt5, mode: int) -> str:
    return {
        mt5.ACCOUNT_TRADE_MODE_DEMO: "demo",
        mt5.ACCOUNT_TRADE_MODE_CONTEST: "contest",
        mt5.ACCOUNT_TRADE_MODE_REAL: "REAL",
    }.get(mode, str(mode))


class LiveBroker:
    def __init__(
        self,
        mt5,
        symbol: str,
        instrument: Instrument,
        magic: int,
        login: int,
        notify: Notifier,
        deviation: int = 20,
        retry_seconds: float = DEFAULT_RETRY_SECONDS,
        clock: Callable[[], float] = _time.monotonic,
    ):
        self.mt5 = mt5
        self.symbol = symbol
        self.instrument = instrument
        self.magic = int(magic)
        self.login = int(login)
        self.notify = notify
        self.deviation = int(deviation)
        self.retry_seconds = float(retry_seconds)
        self.clock = clock
        self.positions: list[Position] = []
        self.trades: list[Trade] = []
        self.cancelled: list[OrderRequest] = []
        self.rejected: list[tuple[OrderRequest, str]] = []
        self.profit: dict[int, float] = {}          # floating profit by ticket, as of the last sync
        self._queued: list[Queued] = []
        self._resting: list[Resting] = []
        self._closing: dict[int, ExitReason] = {}   # closes still to get through
        self._exit_announced: set[int] = set()      # closes whose exit_intent has gone out
        self._modifying: dict[int, tuple[float | None, float | None]] = {}
        self._due: dict[tuple[str, int], float] = {}  # (action, ticket) -> clock time of next try
        self._leftovers: dict[int, Position] = {}   # positions not the strategy's, still to close
        self._known: set[int] = set()               # every ticket this broker has taken on
        self._reported: set[int] = set()            # untracked tickets already reported
        self._filling: int | None = None
        self._retcodes = {getattr(mt5, f"TRADE_RETCODE_{name}"): name
                          for name in NOT_NOW + MAYBE_DONE}
        self._blocked = ""                          # the need-action answer last reported
        self.balance = 0.0
        self.equity = 0.0
        self.refresh_account()

    # ------------------------------------------------------------------ state

    @property
    def pending(self) -> list[OrderRequest]:
        """Orders not yet filled: queued for sending, or resting at the broker."""
        return ([q.order for q in self._queued]
                + [r.order for r in self._resting if not r.withdraw])

    def refresh_account(self) -> None:
        info = self._account()
        self.balance, self.equity = float(info.balance), float(info.equity)

    def tick(self):
        tick = self.mt5.symbol_info_tick(self.symbol)
        if tick is None or not tick.bid:
            raise BrokerUnavailable(f"no quote for {self.symbol}: {self.mt5.last_error()}")
        return tick

    def our_positions(self) -> list:
        return [p for p in self._query(self.mt5.positions_get, symbol=self.symbol)
                if int(p.magic) == self.magic]

    def our_orders(self) -> list:
        return [o for o in self._query(self.mt5.orders_get, symbol=self.symbol)
                if int(o.magic) == self.magic]

    def record_equity(self, bar: Bar) -> EquityPoint:
        try:
            self.refresh_account()
        except BrokerUnavailable:
            pass                       # the last reading stands
        return EquityPoint(bar.time, self.balance, self.equity, len(self.positions))

    def position_rows(self) -> list[dict]:
        return [{**position_row(p), "profit": self.profit.get(p.id)} for p in self.positions]

    def resting_rows(self) -> list[dict]:
        rows = []
        for resting in self._resting:
            order = resting.order
            sl, tp = self._levels(order, order.limit_price)
            rows.append({
                "ticket": resting.ticket, "side": order.side, "volume": order.volume,
                "limit": order.limit_price, "sl": sl, "tp": tp, "tag": order.tag,
                "void": order.cancel_price, "withdrawing": resting.withdraw or None,
            })
        return rows

    def queued_rows(self) -> list[dict]:
        return [{**order_row(q.order), "unanswered": q.unanswered, "problem": q.problem or None}
                for q in self._queued]

    # ----------------------------------------------------------------- orders

    def submit(self, order: OrderRequest) -> None:
        """Queue an order; `execute_pending` sends it."""
        volume = self.instrument.round_volume(order.volume)
        if volume <= 0:
            self._reject(order, "volume rounds to zero")
            return
        order.volume = volume
        self._queued.append(Queued(order))

    def execute_pending(self, resend: bool = True) -> None:
        """Send every queued order: market orders fill now, limit orders go to rest.

        What cannot be sent now stays queued: an order the broker answered "not
        now" until `retry_seconds` have passed, and one that may have reached it
        until a later call with `resend`, so the broker has had a poll's time to
        show it first.
        """
        if not self._queued:
            return
        waiting, self._queued = self._queued, []
        keep: list[Queued] = []
        index = 0
        try:
            tick = self.tick()
            now = self.clock()
            for index, entry in enumerate(waiting):
                held = (entry.unanswered and not resend) or entry.not_before > now
                if held or not self._execute(entry, tick):
                    keep.append(entry)
        except BrokerUnavailable as exc:
            for entry in waiting[index:]:
                entry.problem = str(exc)
            keep.extend(waiting[index:])
            self._trouble(exc)
        self._queued = keep + self._queued

    def expire_queued(self) -> None:
        """Reject every order still queued: the bar it was due in has ended."""
        waiting, self._queued = self._queued, []
        for entry in waiting:
            try:
                if entry.unanswered and self._adopt_sent(entry):
                    continue
            except BrokerUnavailable:
                pass
            why = entry.problem or "no answer from the terminal"
            self._reject(entry.order, f"not sent before its bar ended: {why}")

    def cancel_pending(self) -> int:
        """Drop queued orders and remove resting ones from the broker."""
        count = len(self.pending)
        for entry in self._queued:
            if entry.unanswered:
                try:
                    self._adopt_sent(entry)          # it may be at the broker after all
                except BrokerUnavailable:
                    pass
        self._queued.clear()
        for resting in [r for r in self._resting if not r.withdraw]:
            self._remove(resting, "cancelled by strategy")
        return count

    def withdraw_resting(self, reason: str) -> None:
        """Remove every resting order from the broker, as the runner stops."""
        for resting in list(self._resting):
            self._remove(resting, resting.withdraw or reason)

    def void_reached(self, high: float, low: float) -> None:
        """Remove resting orders whose void level the market has reached."""
        for resting in [r for r in self._resting if not r.withdraw]:
            order = resting.order
            if order.cancel_price is None:
                continue
            if order.side is Side.BUY:
                reached = high >= order.cancel_price
            else:
                reached = low <= order.cancel_price
            if reached:
                self._remove(resting, "void level reached")

    def retry(self) -> None:
        """Try again the closes, stop and target moves and removals still owed,
        each once its wait after a not-now answer is over."""
        now = self.clock()
        for ticket, reason in list(self._closing.items()):
            position = self._position(ticket)
            if position is None:
                self._closing.pop(ticket)
            elif self._due.get(("close", ticket), 0.0) <= now:
                self.close_position(position, reason=reason)
        for ticket, (sl, tp) in list(self._modifying.items()):
            position = self._position(ticket)
            if position is None:
                self._modifying.pop(ticket)
            elif self._due.get(("modify", ticket), 0.0) <= now:
                self.modify(position, sl, tp)
        for resting in [r for r in self._resting if r.withdraw]:
            if self._due.get(("remove", resting.ticket), 0.0) <= now:
                self._remove(resting, resting.withdraw)
        for position in list(self._leftovers.values()):
            if self._due.get(("close", position.id), 0.0) <= now:
                try:
                    self._close_leftover(position)
                except BrokerUnavailable as exc:
                    self._trouble(exc)

    def _execute(self, entry: Queued, tick) -> bool:
        """Send one queued order; False keeps it queued."""
        order = entry.order
        if entry.unanswered and self._adopt_sent(entry):
            return True
        if order.limit_price is not None and _voided(order, tick.bid):
            self.cancelled.append(order)
            self.notify.emit(ORDER_CANCELLED, **_describe(order), reason="void level reached")
            return True
        if order.limit_price is None or _limit_reached(order, tick):
            return self._fill(entry, tick)
        return self._rest(entry)

    def _fill(self, entry: Queued, tick) -> bool:
        order = entry.order
        price = tick.ask if order.side is Side.BUY else tick.bid
        sl, tp = self._levels(order, price)
        problem = _beyond(order.side, sl, tp, tick.bid if order.side is Side.BUY else tick.ask)
        if problem:
            self._reject(order, problem, price=price, sl=sl, tp=tp)
            return True

        self._account()
        if not entry.announced:
            self.notify.emit(ORDER_INTENT, **_describe(order), price=price, sl=sl, tp=tp)
            entry.announced = True
        answer = self._send({
            "action": self.mt5.TRADE_ACTION_DEAL,
            "symbol": self.symbol,
            "volume": float(order.volume),
            "type": self._deal_type(order.side),
            "price": float(price),
            "sl": float(sl or 0.0),
            "tp": float(tp or 0.0),
            "deviation": self.deviation,
            "magic": self.magic,
            "comment": comment_for(order.tag),
            "type_time": self.mt5.ORDER_TIME_GTC,
            "type_filling": self._filling_mode(),
        })
        if answer.status == REFUSED:
            self._reject(order, answer.reason, price=price, sl=sl, tp=tp)
            return True
        if answer.status == LATER:
            return self._later(entry, answer)
        if answer.status == UNANSWERED:
            entry.unanswered, entry.problem = True, answer.reason
            return self._adopt_sent(entry)

        position = self._track(self._opened(answer.result, order, sl, tp, tick))
        self.notify.emit(ORDER_FILLED, **position_row(position))
        return True

    def _rest(self, entry: Queued) -> bool:
        order = entry.order
        limit = self.instrument.round_price(order.limit_price)
        sl, tp = self._levels(order, limit)
        problem = _beyond(order.side, sl, tp, limit)
        if problem:
            self._reject(order, problem, limit=limit, sl=sl, tp=tp)
            return True

        self._account()
        if not entry.announced:
            self.notify.emit(ORDER_INTENT, **_describe(order), limit=limit, sl=sl, tp=tp)
            entry.announced = True
        answer = self._send({
            "action": self.mt5.TRADE_ACTION_PENDING,
            "symbol": self.symbol,
            "volume": float(order.volume),
            "type": self._limit_type(order.side),
            "price": float(limit),
            "sl": float(sl or 0.0),
            "tp": float(tp or 0.0),
            "magic": self.magic,
            "comment": comment_for(order.tag),
            "type_time": self.mt5.ORDER_TIME_GTC,
            "type_filling": self._filling_mode(),
        })
        if answer.status == REFUSED:
            self._reject(order, answer.reason, limit=limit, sl=sl, tp=tp)
            return True
        if answer.status == LATER:
            return self._later(entry, answer)
        if answer.status == UNANSWERED:
            entry.unanswered, entry.problem = True, answer.reason
            return self._adopt_sent(entry)

        ticket = int(answer.result.order)
        self._resting.append(Resting(order, ticket))
        self._known.add(ticket)
        self.notify.emit(ORDER_PLACED, **_describe(order), ticket=ticket, limit=limit, sl=sl, tp=tp)
        return True

    def _remove(self, resting: Resting, reason: str) -> None:
        resting.withdraw = reason
        try:
            answer = self._send({"action": self.mt5.TRADE_ACTION_REMOVE, "order": resting.ticket})
            if answer.status == UNANSWERED and self._order_open(resting.ticket):
                return                           # still there; the next retry sends again
        except BrokerUnavailable as exc:
            self._trouble(exc)
            return
        if answer.status == LATER:
            self._wait("remove", resting.ticket)
            return
        if answer.status == REFUSED:
            # Most often it has just filled, which the next sync reads back.
            if not resting.refused:
                resting.refused = True
                self.notify.emit(ORDER_REJECTED, **_describe(resting.order),
                                 ticket=resting.ticket, action="remove", reason=answer.reason)
            return
        if answer.status == UNANSWERED:
            return                               # gone: the next sync reads whether it filled
        self._resting.remove(resting)
        self._due.pop(("remove", resting.ticket), None)
        self.cancelled.append(resting.order)
        self.notify.emit(ORDER_CANCELLED, **_describe(resting.order), ticket=resting.ticket,
                         reason=reason)

    def _later(self, entry: Queued, answer: Answer) -> bool:
        """Keep an order the broker answered "not now", to be sent again after the wait."""
        entry.problem = answer.reason
        entry.not_before = self.clock() + self.retry_seconds
        return False

    def _wait(self, action: str, ticket: int) -> None:
        self._due[(action, ticket)] = self.clock() + self.retry_seconds

    def _reject(self, order: OrderRequest, reason: str, **detail) -> None:
        self.rejected.append((order, reason))
        self.notify.emit(ORDER_REJECTED, **_describe(order), **detail, reason=reason)

    # -------------------------------------------------------------- positions

    def modify(self, position: Position, sl: float | None = None, tp: float | None = None) -> None:
        """Move a stop or target at the broker.

        A stop the market has already passed closes the position instead, as the
        backtest's next bar would. A move the broker cannot take now is retried.
        """
        new_sl = self.instrument.round_price(sl) if sl is not None else position.sl
        new_tp = self.instrument.round_price(tp) if tp is not None else position.tp
        self._modifying.pop(position.id, None)
        if (new_sl, new_tp) == (position.sl, position.tp):
            return
        try:
            tick = self.tick()
            exit_price = tick.bid if position.side is Side.BUY else tick.ask
            if new_sl is not None and (exit_price - new_sl) * position.side.sign <= 0:
                self.close_position(position, reason=ExitReason.STOP_LOSS)
                return
            answer = self._send({
                "action": self.mt5.TRADE_ACTION_SLTP,
                "symbol": self.symbol,
                "position": position.id,
                "sl": float(new_sl or 0.0),
                "tp": float(new_tp or 0.0),
                "magic": self.magic,
            })
            if answer.status == LATER:
                self._modifying[position.id] = (new_sl, new_tp)
                self._wait("modify", position.id)
                return
            if answer.status == UNANSWERED:
                found = self._query(self.mt5.positions_get, ticket=position.id)
                if not found:
                    return                       # closed meanwhile; sync books it
                rnd = self.instrument.round_price
                at_broker = (rnd(found[0].sl) if found[0].sl else None,
                             rnd(found[0].tp) if found[0].tp else None)
                if at_broker != (new_sl, new_tp):
                    self._modifying[position.id] = (new_sl, new_tp)
                    return
        except BrokerUnavailable as exc:
            self._modifying[position.id] = (new_sl, new_tp)
            self._trouble(exc)
            return
        if answer.status == REFUSED:
            self.notify.emit(ORDER_REJECTED, ticket=position.id, action="modify",
                             sl=new_sl, tp=new_tp, reason=answer.reason)
            return
        self.notify.emit(POSITION_MODIFIED, ticket=position.id, sl_from=position.sl, sl=new_sl,
                         tp_from=position.tp, tp=new_tp)
        position.sl, position.tp = new_sl, new_tp

    def close_position(
        self,
        position: Position,
        bid: float | None = None,
        time=None,
        reason: ExitReason = ExitReason.STRATEGY,
        bar: Bar | None = None,
    ) -> Trade | None:
        """Close at market. The price arguments exist for the simulated signature.

        A close that cannot get through is retried, and booked once the broker
        no longer holds the position.
        """
        announce = position.id not in self._exit_announced
        try:
            outcome = self._close_at_market(position, reason.value, announce)
        except BrokerUnavailable as exc:
            self._closing[position.id] = reason
            self._trouble(exc)
            return None
        if outcome in (UNANSWERED, LATER):
            self._closing[position.id] = reason
            if outcome == LATER:
                self._wait("close", position.id)
            return None
        self._closing.pop(position.id, None)
        self._due.pop(("close", position.id), None)
        if outcome == REFUSED:
            self._exit_announced.discard(position.id)
            return None
        return self._book_close(position, reason, ours=True)

    def close_all(self, bid=None, time=None, reason=ExitReason.STRATEGY, bar=None) -> list[Trade]:
        closed = [self.close_position(p, reason=reason) for p in list(self.positions)]
        return [t for t in closed if t is not None]

    def _close_at_market(self, position: Position, why: str, announce: bool = True) -> str:
        """DONE once the broker no longer holds the position, else REFUSED, LATER or UNANSWERED."""
        tick = self.tick()
        price = tick.bid if position.side is Side.BUY else tick.ask
        self._account()
        if announce:
            self.notify.emit(EXIT_INTENT, ticket=position.id, side=position.side,
                             volume=position.volume, price=price, reason=why, tag=position.tag)
            self._exit_announced.add(position.id)
        answer = self._send({
            "action": self.mt5.TRADE_ACTION_DEAL,
            "symbol": self.symbol,
            "volume": float(position.volume),
            "type": self._deal_type(position.side.opposite),
            "position": position.id,
            "price": float(price),
            "deviation": self.deviation,
            "magic": self.magic,
            "comment": comment_for(position.tag),
            "type_time": self.mt5.ORDER_TIME_GTC,
            "type_filling": self._filling_mode(),
        })
        if answer.status == REFUSED:
            self.notify.emit(ORDER_REJECTED, ticket=position.id, action="close",
                             reason=answer.reason)
        elif answer.status == UNANSWERED and not self._query(self.mt5.positions_get,
                                                            ticket=position.id):
            return DONE
        return answer.status

    def track_bar(self, bar: Bar) -> None:
        """Count a closed bar against every open position, and its excursions."""
        for position in self.positions:
            position.bars_held += 1
            if position.side is Side.BUY:
                best, worst = bar.high - position.entry_price, bar.low - position.entry_price
            else:
                best, worst = position.entry_price - bar.low, position.entry_price - bar.high
            position.mfe = max(position.mfe, best)
            position.mae = min(position.mae, worst)

    # ------------------------------------------------------------ reading back

    def sync(self) -> None:
        """Read back what the broker did since the last call.

        Unanswered sends that reached it are taken on, resting orders that
        filled become positions, and positions its stops or targets closed
        become trades. A position or order under this magic number that nothing
        here tracks is reported once. Raises `BrokerUnavailable` before changing
        anything when the terminal does not answer or is on another account.
        """
        self.refresh_account()
        rows = {int(p.ticket): p for p in self.our_positions()}
        orders = {int(o.ticket): o for o in self.our_orders()}
        self.profit = {ticket: float(row.profit) for ticket, row in rows.items()}

        for entry in [q for q in self._queued if q.unanswered]:
            if self._adopt_sent(entry, rows, orders):
                self._queued.remove(entry)

        for resting in list(self._resting):
            if resting.ticket not in orders:
                self._settle_resting(resting, rows)

        for position in list(self.positions):
            row = rows.get(position.id)
            if row is None:
                self._book_close(position)
            else:
                position.sl, position.tp = row.sl or None, row.tp or None
                position.swap = float(row.swap)

        for ticket in [t for t in self._leftovers if t not in rows]:
            self._leftover_closed(self._leftovers.pop(ticket))

        self._report_untracked(rows, orders)

    def _adopt_sent(self, entry: Queued, rows: dict | None = None,
                    orders: dict | None = None) -> bool:
        """Take on an unanswered send the broker holds; False when it does not."""
        order = entry.order
        comment = comment_for(order.tag)
        self._account()
        if rows is None:
            rows = {int(p.ticket): p for p in self.our_positions()}
        for ticket, row in rows.items():
            if (ticket not in self._known and row.comment == comment
                    and self._side_of(row.type, self.mt5.POSITION_TYPE_BUY) is order.side):
                position = self._track(self._position_from(row, order.tag))
                self.notify.emit(ORDER_FILLED, **position_row(position))
                return True

        if order.limit_price is not None:
            if orders is None:
                orders = {int(o.ticket): o for o in self.our_orders()}
            for ticket, row in orders.items():
                if (ticket not in self._known and row.comment == comment
                        and int(row.type) == self._limit_type(order.side)):
                    self._resting.append(Resting(order, ticket))
                    self._known.add(ticket)
                    self.notify.emit(ORDER_PLACED, **_describe(order), ticket=ticket,
                                     limit=float(row.price_open), sl=row.sl or None,
                                     tp=row.tp or None)
                    return True

        now = datetime.now(timezone.utc)
        for deal in self._query(self.mt5.history_deals_get, now - DEAL_WINDOW, now + DEAL_WINDOW):
            if (int(deal.magic) == self.magic and deal.comment == comment
                    and int(deal.entry) == self.mt5.DEAL_ENTRY_IN
                    and int(deal.position_id) not in self._known
                    and self._side_of(deal.type, self.mt5.DEAL_TYPE_BUY) is order.side):
                position = self._track(Position(
                    id=int(deal.position_id), side=order.side, volume=float(deal.volume),
                    entry_time=from_epoch(deal.time), entry_price=float(deal.price),
                    sl=order.sl_price, tp=order.tp_price, tag=order.tag,
                ))
                self.notify.emit(ORDER_FILLED, **position_row(position))
                return True
        return False

    def _settle_resting(self, resting: Resting, rows: dict) -> None:
        history = self._query(self.mt5.history_orders_get, ticket=resting.ticket)
        if not history:
            return                               # not in history yet
        row = history[0]
        if int(row.state) != self.mt5.ORDER_STATE_FILLED:
            self._resting.remove(resting)
            self.cancelled.append(resting.order)
            self.notify.emit(ORDER_CANCELLED, **_describe(resting.order), ticket=resting.ticket,
                             reason=resting.withdraw or f"order state {row.state} at the broker")
            return
        ticket = int(row.position_id)
        if ticket in rows:
            position = self._position_from(rows[ticket], resting.order.tag)
        else:
            position = self._position_from_deals(ticket, resting.order)
        self._resting.remove(resting)
        self._track(position)
        self.notify.emit(ORDER_FILLED, **position_row(position))
        if resting.withdraw:
            self.notify.emit(ERROR, retrying=True, ticket=position.id,
                             error=f"order {resting.ticket} filled before it could be removed "
                                   f"({resting.withdraw})")

    def _book_close(self, position: Position, reason: ExitReason | None = None,
                    ours: bool = False) -> Trade | None:
        """Record a position the broker no longer holds; None while its deals are missing.

        Its own close is booked regardless, priced off the quote until the deals arrive.
        """
        try:
            trade = self._closed(position, reason)
        except BrokerUnavailable:
            trade = None
        if trade is None:
            if not ours:
                return None
            trade = self._estimated_trade(position, reason or ExitReason.STRATEGY)
        self.positions.remove(position)
        self._closing.pop(position.id, None)
        self._modifying.pop(position.id, None)
        self._due.pop(("close", position.id), None)
        self._due.pop(("modify", position.id), None)
        self._exit_announced.discard(position.id)
        self.trades.append(trade)
        self.notify.emit(POSITION_CLOSED, **trade.as_row(), ticket=position.id)
        return trade

    def _closed(self, position: Position, reason: ExitReason | None) -> Trade | None:
        """The trade a closed position made, from its deals; None until they exist.

        A stop, target or stop-out named by the broker outranks `reason`.
        """
        deals = self._query(self.mt5.history_deals_get, position=position.id)
        exits = [d for d in deals if int(d.entry) in self._exit_entries()]
        if not exits:
            return None
        last = max(exits, key=lambda d: d.time)
        reason = (self._server_reason(int(last.reason)) or reason
                  or self._closing.get(position.id) or ExitReason.STRATEGY)
        volume = sum(float(d.volume) for d in exits) or position.volume
        price = sum(float(d.price) * float(d.volume) for d in exits) / volume
        self._refresh_quietly()
        return Trade(
            id=position.id,
            side=position.side,
            volume=position.volume,
            entry_time=position.entry_time,
            entry_price=position.entry_price,
            exit_time=from_epoch(last.time),
            exit_price=self.instrument.round_price(price),
            reason=reason,
            sl=position.sl,
            tp=position.tp,
            gross_pnl=sum(float(d.profit) for d in exits),
            commission=-sum(float(d.commission) + float(getattr(d, "fee", 0.0)) for d in deals),
            swap=sum(float(d.swap) for d in deals),
            bars_held=position.bars_held,
            mae=position.mae,
            mfe=position.mfe,
            balance_after=self.balance,
            tag=position.tag,
        )

    def _estimated_trade(self, position: Position, reason: ExitReason) -> Trade:
        """A closed trade priced off the current quote, while its deals are not in history."""
        tick = self.mt5.symbol_info_tick(self.symbol)
        price = position.entry_price
        if tick is not None:
            price = tick.bid if position.side is Side.BUY else tick.ask
        delta = (price - position.entry_price) * position.side.sign
        self._refresh_quietly()
        return Trade(
            id=position.id, side=position.side, volume=position.volume,
            entry_time=position.entry_time, entry_price=position.entry_price,
            exit_time=from_epoch(tick.time) if tick is not None else position.entry_time,
            exit_price=price, reason=reason, sl=position.sl, tp=position.tp,
            gross_pnl=self.instrument.value_of(delta, position.volume), commission=0.0,
            swap=position.swap, bars_held=position.bars_held, mae=position.mae,
            mfe=position.mfe, balance_after=self.balance, tag=position.tag,
        )

    def _report_untracked(self, rows: dict, orders: dict) -> None:
        tracked = {p.id for p in self.positions} | set(self._leftovers)
        for ticket, row in rows.items():
            if ticket not in tracked and ticket not in self._reported:
                self._reported.add(ticket)
                self.notify.emit(
                    ERROR, retrying=True, ticket=ticket,
                    side=self._side_of(row.type, self.mt5.POSITION_TYPE_BUY),
                    volume=float(row.volume), tag=row.comment,
                    error=f"the broker holds position {ticket} under magic {self.magic}, "
                          f"which this runner does not track",
                )
        resting = {r.ticket for r in self._resting}
        for ticket, row in orders.items():
            if ticket not in resting and ticket not in self._reported:
                self._reported.add(ticket)
                self.notify.emit(
                    ERROR, retrying=True, ticket=ticket, tag=row.comment,
                    error=f"the broker holds order {ticket} under magic {self.magic}, "
                          f"which this runner does not track",
                )

    def _opened(self, result, order: OrderRequest, sl, tp, tick) -> Position:
        """The position a filled deal opened, read back when the terminal allows."""
        ticket = int(result.order)
        try:
            deals = ()
            if result.deal:
                deals = self._query(self.mt5.history_deals_get, ticket=int(result.deal))
            if deals:
                ticket = int(deals[0].position_id)
            found = self._query(self.mt5.positions_get, ticket=ticket)
        except BrokerUnavailable:
            found = ()
        if found:
            return self._position_from(found[0], order.tag)
        return Position(
            id=ticket, side=order.side, volume=float(result.volume or order.volume),
            entry_time=from_epoch(tick.time), entry_price=float(result.price), sl=sl, tp=tp,
            tag=order.tag,
        )

    def _position_from(self, row, tag: str | None = None) -> Position:
        return Position(
            id=int(row.ticket),
            side=self._side_of(row.type, self.mt5.POSITION_TYPE_BUY),
            volume=float(row.volume),
            entry_time=from_epoch(row.time),
            entry_price=float(row.price_open),
            sl=row.sl or None,
            tp=row.tp or None,
            tag=row.comment if tag is None else tag,
            swap=float(row.swap),
        )

    def _position_from_deals(self, ticket: int, order: OrderRequest) -> Position:
        deals = self._query(self.mt5.history_deals_get, position=ticket)
        entry = next((d for d in deals if int(d.entry) == self.mt5.DEAL_ENTRY_IN), None)
        if entry is None:
            raise BrokerUnavailable(f"no entry deal for position {ticket} yet")
        return Position(
            id=ticket, side=order.side, volume=float(entry.volume),
            entry_time=from_epoch(entry.time), entry_price=float(entry.price),
            sl=order.sl_price, tp=order.tp_price, tag=order.tag,
        )

    def _track(self, position: Position) -> Position:
        self.positions.append(position)
        self._known.add(position.id)
        return position

    def _position(self, ticket: int) -> Position | None:
        return next((p for p in self.positions if p.id == ticket), None)

    def _order_open(self, ticket: int) -> bool:
        return any(int(o.ticket) == ticket for o in self.our_orders())

    # --------------------------------------------------------------- adoption

    def adopt_position(self, row, tag: str) -> Position:
        return self._track(self._position_from(row, tag))

    def adopt_order(self, order: OrderRequest, row) -> None:
        self._resting.append(Resting(order, int(row.ticket)))
        self._known.add(int(row.ticket))

    def close_leftover(self, row) -> None:
        """Close a position of ours the strategy does not hold, outside its trade record.

        A close the broker cannot take now is retried like the strategy's own.
        """
        position = self._position_from(row)
        self._known.add(position.id)
        self._leftovers[position.id] = position
        self._close_leftover(position)

    def _close_leftover(self, position: Position) -> None:
        outcome = self._close_at_market(position, "not held by the strategy",
                                        announce=position.id not in self._exit_announced)
        if outcome == LATER:
            self._wait("close", position.id)
        if outcome in (LATER, UNANSWERED):
            return
        del self._leftovers[position.id]
        self._due.pop(("close", position.id), None)
        if outcome == DONE:
            self._leftover_closed(position)

    def _leftover_closed(self, position: Position) -> None:
        self._exit_announced.discard(position.id)
        self.notify.emit(POSITION_CLOSED, ticket=position.id, side=position.side,
                         volume=position.volume, tag=position.tag, leftover=True)

    def remove_leftover(self, row) -> None:
        """Remove an order of ours the strategy does not hold."""
        order = OrderRequest(
            side=Side.BUY if int(row.type) == self.mt5.ORDER_TYPE_BUY_LIMIT else Side.SELL,
            volume=float(row.volume_current), limit_price=float(row.price_open),
            tag=row.comment,
        )
        self.adopt_order(order, row)
        self._remove(self._resting[-1], "not held by the strategy")

    # ------------------------------------------------------------------ plumbing

    def _account(self):
        """The terminal's account, provided it is still the one traded."""
        info = self.mt5.account_info()
        if info is None:
            raise BrokerUnavailable(f"account_info: {self.mt5.last_error()}")
        if int(info.login) != self.login:
            raise AccountChanged(self.login, int(info.login))
        return info

    def _send(self, request: dict) -> Answer:
        """Send one request, once the terminal is confirmed on the traded account,
        and say what became of it."""
        self._account()
        result = self.mt5.order_send(request)
        if result is None:
            return Answer(UNANSWERED, reason=f"no answer from order_send: {self.mt5.last_error()}")
        retcode = int(result.retcode)
        if retcode in (self.mt5.TRADE_RETCODE_DONE, self.mt5.TRADE_RETCODE_PLACED):
            self._blocked = ""
            return Answer(DONE, result)
        reason = f"{result.comment} (retcode {retcode})"
        name = self._retcodes.get(retcode, "")
        if name in NOT_NOW:
            self._report_blocked(name, reason)
            return Answer(LATER, reason=reason)
        if name in MAYBE_DONE:
            return Answer(UNANSWERED, reason=reason)
        return Answer(REFUSED, reason=reason)

    def _report_blocked(self, name: str, reason: str) -> None:
        """One retrying error for a not-now answer someone has to act on."""
        if name in NEEDS_ACTION and self._blocked != name:
            self._blocked = name
            self.notify.emit(ERROR, retrying=True, retcode=name,
                             error=f"{NEEDS_ACTION[name]}: {reason}; orders wait until it clears")

    def _trouble(self, exc: BrokerUnavailable) -> None:
        self.notify.emit(ERROR, error=str(exc), retrying=True, **exc.detail)

    def _query(self, fn, *args, **kwargs):
        rows = fn(*args, **kwargs)
        if rows is None:
            code, message = self.mt5.last_error()
            if code != RES_S_OK:
                raise BrokerUnavailable(f"{fn.__name__}: {message} ({code})")
            return ()
        return rows

    def _refresh_quietly(self) -> None:
        try:
            self.refresh_account()
        except BrokerUnavailable:
            pass

    def _filling_mode(self) -> int:
        """A filling mode the symbol accepts; guessing wrong is retcode 10030."""
        if self._filling is None:
            info = self.mt5.symbol_info(self.symbol)
            flags = int(getattr(info, "filling_mode", 0)) if info is not None else 0
            if flags & 1:
                self._filling = self.mt5.ORDER_FILLING_FOK
            elif flags & 2:
                self._filling = self.mt5.ORDER_FILLING_IOC
            else:
                self._filling = self.mt5.ORDER_FILLING_RETURN
        return self._filling

    def _deal_type(self, side: Side) -> int:
        return self.mt5.ORDER_TYPE_BUY if side is Side.BUY else self.mt5.ORDER_TYPE_SELL

    def _limit_type(self, side: Side) -> int:
        return self.mt5.ORDER_TYPE_BUY_LIMIT if side is Side.BUY else self.mt5.ORDER_TYPE_SELL_LIMIT

    @staticmethod
    def _side_of(kind, buy: int) -> Side:
        return Side.BUY if int(kind) == buy else Side.SELL

    def _exit_entries(self) -> tuple[int, ...]:
        return (self.mt5.DEAL_ENTRY_OUT, self.mt5.DEAL_ENTRY_OUT_BY)

    def _server_reason(self, deal_reason: int) -> ExitReason | None:
        return {
            self.mt5.DEAL_REASON_SL: ExitReason.STOP_LOSS,
            self.mt5.DEAL_REASON_TP: ExitReason.TAKE_PROFIT,
            self.mt5.DEAL_REASON_SO: ExitReason.MARGIN_CALL,
        }.get(deal_reason)

    def _levels(self, order: OrderRequest, price: float) -> tuple[float | None, float | None]:
        sign = order.side.sign
        sl, tp = order.sl_price, order.tp_price
        if sl is None and order.sl_pct is not None:
            sl = price * (1 - sign * order.sl_pct / 100.0)
        if tp is None and order.tp_pct is not None:
            tp = price * (1 + sign * order.tp_pct / 100.0)
        rnd = self.instrument.round_price
        return (rnd(sl) if sl is not None else None, rnd(tp) if tp is not None else None)


def _voided(order: OrderRequest, bid: float) -> bool:
    if order.cancel_price is None:
        return False
    return bid >= order.cancel_price if order.side is Side.BUY else bid <= order.cancel_price


def _limit_reached(order: OrderRequest, tick) -> bool:
    if order.side is Side.BUY:
        return tick.ask <= order.limit_price
    return tick.bid >= order.limit_price


def _beyond(side: Side, sl: float | None, tp: float | None, price: float) -> str:
    """Why a stop or target is already on the wrong side of `price`, or ""."""
    if sl is not None and (price - sl) * side.sign <= 0:
        return f"price {price} already at or past the stop {sl}"
    if tp is not None and (tp - price) * side.sign <= 0:
        return f"price {price} already at or past the target {tp}"
    return ""


def _describe(order: OrderRequest) -> dict:
    return {
        "side": order.side,
        "volume": order.volume,
        "order": "limit" if order.limit_price is not None else "market",
        "tag": order.tag,
        "signal_time": order.created_at,
    }


def order_row(order: OrderRequest) -> dict:
    return {
        "side": order.side, "volume": order.volume,
        "order": "limit" if order.limit_price is not None else "market",
        "limit": order.limit_price, "sl": order.sl_price, "tp": order.tp_price,
        "void": order.cancel_price, "tag": order.tag, "signal_time": order.created_at,
    }


def position_row(position: Position) -> dict:
    return {
        "ticket": position.id,
        "side": position.side,
        "volume": position.volume,
        "price": position.entry_price,
        "sl": position.sl,
        "tp": position.tp,
        "tag": position.tag,
        "entry_time": position.entry_time,
    }
