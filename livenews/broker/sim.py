"""Local fake broker for mock mode. Fills market orders at the simulated last
price, holds limit orders until the price crosses."""
from __future__ import annotations

import itertools

from ..models import Account, Order, Position
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

    def _last(self, sym: str) -> float:
        q = self.store.quotes.get(sym)
        if not q:
            raise OrderRejected(f"no price for {sym}")
        return q.price

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

    async def submit(self, req: OrderRequest) -> Order:
        px = self._last(req.symbol)
        o = Order(id=f"sim-{next(self._ids)}", symbol=req.symbol, side=req.side, qty=req.qty,
                  type=req.type, status="new", limit_price=req.limit_price)
        self.orders.insert(0, o)
        if req.type == "market":
            self._fill(o, px)
        else:
            self._check_limit(o)
        return o

    def _check_limit(self, o: Order) -> None:
        px = self._last(o.symbol)
        if (o.side == "buy" and px <= o.limit_price) or (o.side == "sell" and px >= o.limit_price):
            self._fill(o, o.limit_price)

    async def cancel(self, order_id: str) -> None:
        for o in self.orders:
            if o.id == order_id and o.status == "new":
                o.status = "canceled"
                return
        raise OrderRejected(f"no open order {order_id}")

    async def cancel_all(self) -> None:
        for o in self.orders:
            if o.status == "new":
                o.status = "canceled"

    async def refresh(self) -> None:
        for o in self.orders:
            if o.status == "new":
                self._check_limit(o)
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
