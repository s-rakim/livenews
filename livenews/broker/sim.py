"""Local fake broker for mock mode. Market orders fill at the simulated last
price. Limit, stop, stop-limit and trailing stops sit until the price says
they should trigger. Every event goes to the execution blotter."""
from __future__ import annotations

import itertools

from ..models import Account, Execution, Order, Position, utcnow
from ..store import Store
from .base import Broker, OrderRejected, OrderRequest


class SimBroker(Broker):
    name = "local sim"
    is_live = False

    def __init__(self, store: Store, cash: float = 100_000.0):
        self.store = store
        self.cash = cash
        self.start = cash
        self.pos: dict[str, tuple[float, float]] = {}  # sym -> (qty, avg)
        self.orders: list[Order] = []
        self._ids = itertools.count(1)
        self._trail_ref: dict[str, float] = {}

    def _last(self, sym: str) -> float:
        q = self.store.quotes.get(sym)
        if not q:
            raise OrderRejected(f"no price for {sym}")
        return q.price

    def _event(self, o: Order, ev: str, qty: float, price: float | None = None) -> None:
        now = utcnow()
        self.store.executions.appendleft(Execution(
            now, ev, o.id, o.symbol, o.side, qty, price, self.pos.get(o.symbol, (0.0, 0.0))[0],
            (now - o.submitted_at).total_seconds() * 1000 if ev == "fill" else None, "local sim"))

    def _fill(self, o: Order, px: float) -> None:
        qty, avg = self.pos.get(o.symbol, (0.0, 0.0))
        signed = o.qty if o.side == "buy" else -o.qty
        new = qty + signed
        if qty == 0 or (qty > 0) == (signed > 0):
            avg = (avg * abs(qty) + px * abs(signed)) / abs(new) if new else 0.0
        elif (new > 0) != (qty > 0) and new != 0:
            avg = px  # flipped through zero
        self.cash -= signed * px
        if abs(new) < 1e-9:
            self.pos.pop(o.symbol, None)
        else:
            self.pos[o.symbol] = (new, avg)
        o.status = "filled"
        o.filled_avg_price = px
        o.filled_qty = o.qty
        self._event(o, "fill", o.qty, px)

    async def submit(self, req: OrderRequest) -> Order:
        px = self._last(req.symbol)
        o = Order(id=f"sim-{next(self._ids)}", symbol=req.symbol, side=req.side, qty=req.qty,
                  type=req.type, status="new", limit_price=req.limit_price, stop_price=req.stop_price,
                  tif=req.tif, trail_percent=req.trail_percent)
        self.orders.insert(0, o)
        self._event(o, "new", o.qty)
        if req.type == "trailing_stop":
            self._trail_ref[o.id] = px
        if req.type == "market":
            self._fill(o, px)
        else:
            self._work(o)
        return o

    def _work(self, o: Order) -> None:
        px = self._last(o.symbol)
        buy = o.side == "buy"
        if o.type == "trailing_stop":
            ref = self._trail_ref.get(o.id, px)
            ref = min(ref, px) if buy else max(ref, px)
            self._trail_ref[o.id] = ref
            o.stop_price = round(ref * (1 + o.trail_percent / 100) if buy else ref * (1 - o.trail_percent / 100), 2)
        if o.type in ("stop", "stop_limit", "trailing_stop"):
            triggered = px >= o.stop_price if buy else px <= o.stop_price
            if not triggered:
                return
            if o.type in ("stop", "trailing_stop"):
                self._fill(o, px)
                return
            o.type = "limit"  # stop-limit becomes a limit once triggered
        if o.type == "limit" and ((buy and px <= o.limit_price) or (not buy and px >= o.limit_price)):
            self._fill(o, o.limit_price)

    async def cancel(self, order_id: str) -> None:
        for o in self.orders:
            if o.id == order_id and o.status == "new":
                o.status = "canceled"
                self._event(o, "canceled", o.qty)
                return
        raise OrderRejected(f"no open order {order_id}")

    async def cancel_all(self) -> None:
        for o in self.orders:
            if o.status == "new":
                o.status = "canceled"
                self._event(o, "canceled", o.qty)

    async def refresh(self) -> None:
        for o in self.orders:
            if o.status == "new":
                self._work(o)
        positions = []
        mv = 0.0
        for sym, (qty, avg) in self.pos.items():
            px = self._last(sym)
            positions.append(Position(sym, qty, avg, px))
            mv += qty * px
        eq = self.cash + mv
        self.store.account = Account(equity=eq, cash=self.cash, buying_power=max(self.cash, 0), day_pl=eq - self.start)
        self.store.positions = positions
        self.store.orders = self.orders[:30]
