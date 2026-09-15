"""The broker a strategy trades through on a live MT5 account.

Presents the surface `SimulatedBroker` does — positions, pending orders, closed
trades, submit, modify, close — so a strategy runs against it unchanged. The
account's server holds stops and targets; `sync` reads back what it did.

Only positions and orders on `symbol` carrying `magic` belong to this broker.
`mt5` is the `MetaTrader5` module, or anything with the same calls.
"""

from __future__ import annotations

from dataclasses import dataclass

from ..core.instrument import Instrument
from ..core.types import Bar, EquityPoint, ExitReason, OrderRequest, Position, Side, Trade
from ..utils.timeutil import from_epoch
from .events import (
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


class BrokerUnavailable(RuntimeError):
    """The terminal did not answer; the call can be retried later."""


@dataclass(slots=True)
class Resting:
    """A limit order sitting at the broker, and its ticket there."""

    order: OrderRequest
    ticket: int


def comment_for(tag: str) -> str:
    return (tag or "")[:COMMENT_LENGTH]


class LiveBroker:
    def __init__(
        self,
        mt5,
        symbol: str,
        instrument: Instrument,
        magic: int,
        notify: Notifier,
        deviation: int = 20,
    ):
        self.mt5 = mt5
        self.symbol = symbol
        self.instrument = instrument
        self.magic = int(magic)
        self.notify = notify
        self.deviation = int(deviation)
        self.positions: list[Position] = []
        self.trades: list[Trade] = []
        self.cancelled: list[OrderRequest] = []
        self.rejected: list[tuple[OrderRequest, str]] = []
        self._queued: list[OrderRequest] = []
        self._resting: list[Resting] = []
        self._filling: int | None = None
        self.balance = 0.0
        self.equity = 0.0
        self.refresh_account()

    # ------------------------------------------------------------------ state

    @property
    def pending(self) -> list[OrderRequest]:
        """Orders not yet filled: queued for the next open, or resting at the broker."""
        return self._queued + [r.order for r in self._resting]

    def refresh_account(self) -> None:
        info = self.mt5.account_info()
        if info is None:
            raise BrokerUnavailable(f"account_info: {self.mt5.last_error()}")
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

    # ----------------------------------------------------------------- orders

    def submit(self, order: OrderRequest) -> None:
        """Queue an order; `execute_pending` sends it at the next open."""
        volume = self.instrument.round_volume(order.volume)
        if volume <= 0:
            self._reject(order, "volume rounds to zero")
            return
        order.volume = volume
        self._queued.append(order)

    def execute_pending(self) -> None:
        """Send every queued order: market orders fill now, limit orders go to rest.

        Without a quote the orders stay queued for the next attempt.
        """
        if not self._queued:
            return
        try:
            tick = self.tick()
        except BrokerUnavailable:
            return
        orders, self._queued = self._queued, []
        for order in orders:
            if order.limit_price is None:
                self._fill(order, tick)
            elif _voided(order, tick.bid):
                self.cancelled.append(order)
                self.notify.emit(ORDER_CANCELLED, **_describe(order), reason="void level reached")
            elif _limit_reached(order, tick):
                self._fill(order, tick)
            else:
                self._rest(order)

    def cancel_pending(self) -> int:
        """Drop queued orders and remove resting ones from the broker."""
        count = len(self._queued) + len(self._resting)
        self._queued.clear()
        for resting in list(self._resting):
            self._remove(resting, "cancelled by strategy")
        return count

    def withdraw_resting(self, reason: str) -> None:
        """Remove resting orders from the broker, as the runner stops."""
        for resting in list(self._resting):
            self._remove(resting, reason)

    def void_reached(self, high: float, low: float) -> None:
        """Remove resting orders whose void level the market has reached."""
        for resting in list(self._resting):
            order = resting.order
            if order.cancel_price is None:
                continue
            if order.side is Side.BUY:
                reached = high >= order.cancel_price
            else:
                reached = low <= order.cancel_price
            if reached:
                self._remove(resting, "void level reached")

    def _fill(self, order: OrderRequest, tick) -> None:
        price = tick.ask if order.side is Side.BUY else tick.bid
        sl, tp = self._levels(order, price)
        problem = _beyond(order.side, sl, tp, tick.bid if order.side is Side.BUY else tick.ask)
        if problem:
            return self._reject(order, problem, price=price, sl=sl, tp=tp)

        self.notify.emit(ORDER_INTENT, **_describe(order), price=price, sl=sl, tp=tp)
        result, problem = self._send({
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
        if result is None:
            return self._reject(order, problem, price=price, sl=sl, tp=tp)

        position = self._opened(result, order, sl, tp, tick)
        self.positions.append(position)
        self.notify.emit(ORDER_FILLED, **_position_row(position))

    def _rest(self, order: OrderRequest) -> None:
        limit = self.instrument.round_price(order.limit_price)
        sl, tp = self._levels(order, limit)
        problem = _beyond(order.side, sl, tp, limit)
        if problem:
            return self._reject(order, problem, limit=limit, sl=sl, tp=tp)

        self.notify.emit(ORDER_INTENT, **_describe(order), limit=limit, sl=sl, tp=tp)
        result, problem = self._send({
            "action": self.mt5.TRADE_ACTION_PENDING,
            "symbol": self.symbol,
            "volume": float(order.volume),
            "type": (self.mt5.ORDER_TYPE_BUY_LIMIT if order.side is Side.BUY
                     else self.mt5.ORDER_TYPE_SELL_LIMIT),
            "price": float(limit),
            "sl": float(sl or 0.0),
            "tp": float(tp or 0.0),
            "magic": self.magic,
            "comment": comment_for(order.tag),
            "type_time": self.mt5.ORDER_TIME_GTC,
            "type_filling": self._filling_mode(),
        })
        if result is None:
            return self._reject(order, problem, limit=limit, sl=sl, tp=tp)
        self._resting.append(Resting(order, int(result.order)))
        self.notify.emit(ORDER_PLACED, **_describe(order), ticket=int(result.order),
                         limit=limit, sl=sl, tp=tp)

    def _remove(self, resting: Resting, reason: str) -> None:
        result, problem = self._send({
            "action": self.mt5.TRADE_ACTION_REMOVE, "order": resting.ticket,
        })
        if result is None:
            # Most often it has just filled; the next sync will say.
            self.notify.emit(ORDER_REJECTED, **_describe(resting.order), ticket=resting.ticket,
                             action="remove", reason=problem)
            return
        self._resting.remove(resting)
        self.cancelled.append(resting.order)
        self.notify.emit(ORDER_CANCELLED, **_describe(resting.order), ticket=resting.ticket,
                         reason=reason)

    def _reject(self, order: OrderRequest, reason: str, **detail) -> None:
        self.rejected.append((order, reason))
        self.notify.emit(ORDER_REJECTED, **_describe(order), **detail, reason=reason)

    # -------------------------------------------------------------- positions

    def modify(self, position: Position, sl: float | None = None, tp: float | None = None) -> None:
        """Move a stop or target at the broker.

        A stop the market has already passed closes the position instead, as the
        backtest's next bar would.
        """
        new_sl = self.instrument.round_price(sl) if sl is not None else position.sl
        new_tp = self.instrument.round_price(tp) if tp is not None else position.tp
        if (new_sl, new_tp) == (position.sl, position.tp):
            return
        try:
            tick = self.tick()
        except BrokerUnavailable as exc:
            self.notify.emit(ORDER_REJECTED, ticket=position.id, action="modify", reason=str(exc))
            return
        exit_price = tick.bid if position.side is Side.BUY else tick.ask
        if new_sl is not None and (exit_price - new_sl) * position.side.sign <= 0:
            self.close_position(position, reason=ExitReason.STOP_LOSS)
            return

        result, problem = self._send({
            "action": self.mt5.TRADE_ACTION_SLTP,
            "symbol": self.symbol,
            "position": position.id,
            "sl": float(new_sl or 0.0),
            "tp": float(new_tp or 0.0),
            "magic": self.magic,
        })
        if result is None:
            self.notify.emit(ORDER_REJECTED, ticket=position.id, action="modify",
                             sl=new_sl, tp=new_tp, reason=problem)
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
        """Close at market. The price arguments exist for the simulated signature."""
        if not self._close_at_market(position, reason.value):
            return None
        self.positions.remove(position)
        try:
            trade = self._closed(position, reason)
        except BrokerUnavailable:
            trade = None
        if trade is None:
            trade = self._estimated_trade(position, reason)
        self.trades.append(trade)
        self.notify.emit(POSITION_CLOSED, **trade.as_row(), ticket=position.id)
        return trade

    def close_all(self, bid=None, time=None, reason=ExitReason.STRATEGY, bar=None) -> list[Trade]:
        closed = [self.close_position(p, reason=reason) for p in list(self.positions)]
        return [t for t in closed if t is not None]

    def _close_at_market(self, position: Position, why: str) -> bool:
        try:
            tick = self.tick()
        except BrokerUnavailable as exc:
            self.notify.emit(ORDER_REJECTED, ticket=position.id, action="close", reason=str(exc))
            return False
        price = tick.bid if position.side is Side.BUY else tick.ask
        self.notify.emit(EXIT_INTENT, ticket=position.id, side=position.side,
                         volume=position.volume, price=price, reason=why, tag=position.tag)
        result, problem = self._send({
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
        if result is None:
            self.notify.emit(ORDER_REJECTED, ticket=position.id, action="close", reason=problem)
            return False
        return True

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
        """Read back what the server did since the last call.

        Resting orders that filled become positions, positions its stops or
        targets closed become trades. Raises `BrokerUnavailable` before changing
        anything when the terminal does not answer.
        """
        self.refresh_account()
        live = {int(p.ticket): p for p in self.our_positions()}
        open_orders = {int(o.ticket) for o in self.our_orders()}

        for resting in list(self._resting):
            if resting.ticket not in open_orders:
                self._settle_resting(resting, live)

        for position in list(self.positions):
            row = live.get(position.id)
            if row is not None:
                position.sl, position.tp = row.sl or None, row.tp or None
                position.swap = float(row.swap)
                continue
            trade = self._closed(position, None)
            if trade is None:
                continue                         # its deals are not in history yet
            self.positions.remove(position)
            self.trades.append(trade)
            self.notify.emit(POSITION_CLOSED, **trade.as_row(), ticket=position.id)

    def _settle_resting(self, resting: Resting, live: dict) -> None:
        history = self._query(self.mt5.history_orders_get, ticket=resting.ticket)
        if not history:
            return                               # not in history yet
        row = history[0]
        if int(row.state) != self.mt5.ORDER_STATE_FILLED:
            self._resting.remove(resting)
            self.cancelled.append(resting.order)
            self.notify.emit(ORDER_CANCELLED, **_describe(resting.order), ticket=resting.ticket,
                             reason=f"order state {row.state} at the broker")
            return
        ticket = int(row.position_id)
        if ticket in live:
            position = self._position_from(live[ticket], resting.order.tag)
        else:
            position = self._position_from_deals(ticket, resting.order)
        self._resting.remove(resting)
        self.positions.append(position)
        self.notify.emit(ORDER_FILLED, **_position_row(position))

    def _closed(self, position: Position, reason: ExitReason | None) -> Trade | None:
        """The trade a closed position made, from its deals; None until they exist."""
        deals = self._query(self.mt5.history_deals_get, position=position.id)
        exits = [d for d in deals if int(d.entry) in self._exit_entries()]
        if not exits:
            return None
        last = max(exits, key=lambda d: d.time)
        if reason is None:
            reason = self._reason_for(int(last.reason))
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
            side=Side.BUY if int(row.type) == self.mt5.POSITION_TYPE_BUY else Side.SELL,
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

    # --------------------------------------------------------------- adoption

    def adopt_position(self, row, tag: str) -> Position:
        position = self._position_from(row, tag)
        self.positions.append(position)
        return position

    def adopt_order(self, order: OrderRequest, row) -> None:
        self._resting.append(Resting(order, int(row.ticket)))

    def close_leftover(self, row) -> None:
        """Close a position of ours the strategy does not hold, outside its trade record."""
        position = self._position_from(row)
        if self._close_at_market(position, "not held by the strategy"):
            self.notify.emit(POSITION_CLOSED, ticket=position.id, side=position.side,
                             volume=position.volume, tag=position.tag, leftover=True)

    def remove_leftover(self, row) -> None:
        """Remove an order of ours the strategy does not hold."""
        order = OrderRequest(
            side=Side.BUY if int(row.type) == self.mt5.ORDER_TYPE_BUY_LIMIT else Side.SELL,
            volume=float(row.volume_current), limit_price=float(row.price_open),
            tag=row.comment,
        )
        self._resting.append(Resting(order, int(row.ticket)))
        self._remove(self._resting[-1], "not held by the strategy")

    # ------------------------------------------------------------------ plumbing

    def _send(self, request: dict):
        """(result, "") on success, (None, reason) otherwise."""
        result = self.mt5.order_send(request)
        if result is None:
            return None, f"order_send failed: {self.mt5.last_error()}"
        done = (self.mt5.TRADE_RETCODE_DONE, self.mt5.TRADE_RETCODE_PLACED)
        if int(result.retcode) not in done:
            return None, f"{result.comment} (retcode {result.retcode})"
        return result, ""

    def _query(self, fn, **kwargs):
        rows = fn(**kwargs)
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

    def _exit_entries(self) -> tuple[int, ...]:
        return (self.mt5.DEAL_ENTRY_OUT, self.mt5.DEAL_ENTRY_OUT_BY)

    def _reason_for(self, deal_reason: int) -> ExitReason:
        return {
            self.mt5.DEAL_REASON_SL: ExitReason.STOP_LOSS,
            self.mt5.DEAL_REASON_TP: ExitReason.TAKE_PROFIT,
            self.mt5.DEAL_REASON_SO: ExitReason.MARGIN_CALL,
        }.get(deal_reason, ExitReason.STRATEGY)

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


def _position_row(position: Position) -> dict:
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
