from __future__ import annotations

from dataclasses import dataclass

from ..models import Order


class OrderRejected(Exception):
    pass


@dataclass
class OrderRequest:
    symbol: str
    side: str  # "buy" or "sell"
    qty: float
    type: str = "market"  # "market" or "limit"
    limit_price: float | None = None

    def describe(self) -> str:
        px = f" @ {self.limit_price:.2f} LMT" if self.type == "limit" else " @ MKT"
        return f"{self.side.upper()} {self.qty:g} {self.symbol}{px}"


def check_order(req: OrderRequest, ref_price: float | None, max_value: float) -> float:
    """Sanity checks that run before anything leaves the machine.
    Returns the estimated notional value."""
    if req.side not in ("buy", "sell"):
        raise OrderRejected("side must be buy or sell")
    if req.qty <= 0 or req.qty != req.qty:
        raise OrderRejected("quantity must be positive")
    if not req.symbol.replace(".", "").isalpha():
        raise OrderRejected(f"bad symbol {req.symbol!r}")
    if req.type not in ("market", "limit"):
        raise OrderRejected("type must be market or limit")
    if req.type == "limit" and (not req.limit_price or req.limit_price <= 0):
        raise OrderRejected("limit order needs a positive price")
    px = req.limit_price if req.type == "limit" else ref_price
    if not px:
        raise OrderRejected(f"no price for {req.symbol}, look it up first so we can size the order")
    if req.type == "limit" and ref_price and abs(req.limit_price - ref_price) / ref_price > 0.10:
        raise OrderRejected(
            f"limit {req.limit_price:.2f} is more than 10% from last {ref_price:.2f}, typo?"
        )
    value = px * req.qty
    if value > max_value:
        raise OrderRejected(
            f"order value ${value:,.0f} is over LIVENEWS_MAX_ORDER_VALUE ${max_value:,.0f}"
        )
    return value


class Broker:
    name = "base"
    is_live = False

    async def submit(self, req: OrderRequest) -> Order:
        raise NotImplementedError

    async def cancel(self, order_id: str) -> None:
        raise NotImplementedError

    async def cancel_all(self) -> None:
        raise NotImplementedError

    async def refresh(self) -> None:
        raise NotImplementedError

    async def aclose(self) -> None:
        pass
