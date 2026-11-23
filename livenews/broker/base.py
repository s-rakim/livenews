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
    type: str = "market"  # market, limit, stop, stop_limit, trailing_stop
    limit_price: float | None = None
    stop_price: float | None = None
    tif: str = "day"  # day or gtc
    trail_percent: float | None = None

    def describe(self) -> str:
        if self.type == "limit":
            px = f" @ {self.limit_price:.2f} LMT"
        elif self.type == "stop":
            px = f" STOP {self.stop_price:.2f}"
        elif self.type == "stop_limit":
            px = f" STOP {self.stop_price:.2f} LMT {self.limit_price:.2f}"
        elif self.type == "trailing_stop":
            px = f" TRAIL {self.trail_percent:g}%"
        else:
            px = " @ MKT"
        return f"{self.side.upper()} {self.qty:g} {self.symbol}{px} {self.tif.upper()}"


def check_order(req: OrderRequest, ref_price: float | None, max_value: float) -> float:
    """Sanity checks that run before anything leaves the machine.
    Returns the estimated notional value."""
    if req.side not in ("buy", "sell"):
        raise OrderRejected("side must be buy or sell")
    if req.qty <= 0 or req.qty != req.qty:
        raise OrderRejected("quantity must be positive")
    if not req.symbol.replace(".", "").isalpha():
        raise OrderRejected(f"bad symbol {req.symbol!r}")
    if req.type not in ("market", "limit", "stop", "stop_limit", "trailing_stop"):
        raise OrderRejected("unknown order type")
    if req.tif not in ("day", "gtc"):
        raise OrderRejected("time in force must be DAY or GTC")
    if req.type in ("limit", "stop_limit") and (not req.limit_price or req.limit_price <= 0):
        raise OrderRejected("limit order needs a positive price")
    if req.type in ("stop", "stop_limit") and (not req.stop_price or req.stop_price <= 0):
        raise OrderRejected("stop order needs a positive stop price")
    if req.type == "trailing_stop" and not (req.trail_percent and 0 < req.trail_percent < 50):
        raise OrderRejected("trail percent must be between 0 and 50")
    if not ref_price and req.type != "limit":
        raise OrderRejected(f"no price for {req.symbol}, look it up first so we can size the order")
    for label, p in (("limit", req.limit_price), ("stop", req.stop_price)):
        if p and ref_price and abs(p - ref_price) / ref_price > 0.10:
            raise OrderRejected(f"{label} {p:.2f} is more than 10% from last {ref_price:.2f}, typo?")
    if req.type == "stop" and ref_price:
        # a buy stop below market (or sell stop above) would trigger instantly
        if (req.side == "buy" and req.stop_price <= ref_price) or (req.side == "sell" and req.stop_price >= ref_price):
            raise OrderRejected(f"{req.side} stop at {req.stop_price:.2f} would trigger immediately (last {ref_price:.2f})")
    px = max(p for p in (req.limit_price, req.stop_price, ref_price) if p)
    if not px:
        raise OrderRejected(f"no price for {req.symbol}")
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
