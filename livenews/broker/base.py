from __future__ import annotations

import re
from dataclasses import dataclass

from ..models import Order
from ..options_math import parse_occ

_CRYPTO = re.compile(r"^[A-Z]{2,6}/(USD|USDT|USDC|BTC)$")
CRYPTO_TYPES = ("market", "limit", "stop_limit")
OPTION_TYPES = ("market", "limit")


def asset_class(symbol: str) -> str:
    """equity, option (OCC symbol), crypto (BTC/USD) or data (index, future, FX: quotes only)."""
    if parse_occ(symbol):
        return "option"
    if _CRYPTO.match(symbol):
        return "crypto"
    if symbol.replace(".", "").isalpha():
        return "equity"
    return "data"


def multiplier(symbol: str) -> float:
    return 100.0 if asset_class(symbol) == "option" else 1.0


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
    Returns the estimated notional value. Crypto has no session, so a DAY
    order on a coin pair is switched to GTC here (the only TIF Alpaca takes
    for crypto besides IOC)."""
    if req.side not in ("buy", "sell"):
        raise OrderRejected("side must be buy or sell")
    if req.qty <= 0 or req.qty != req.qty:
        raise OrderRejected("quantity must be positive")
    cls = asset_class(req.symbol)
    if cls == "data":
        raise OrderRejected(f"{req.symbol} is a delayed data symbol (index, future or FX), it can't be traded here")
    if req.type not in ("market", "limit", "stop", "stop_limit", "trailing_stop"):
        raise OrderRejected("unknown order type")
    if req.tif not in ("day", "gtc"):
        raise OrderRejected("time in force must be DAY or GTC")
    if cls == "crypto":
        if req.type not in CRYPTO_TYPES:
            raise OrderRejected("crypto takes market, limit or stop-limit orders only")
        req.tif = "gtc"
    if cls == "option":
        if req.type not in OPTION_TYPES:
            raise OrderRejected("options take market or limit orders only")
        if req.qty != int(req.qty):
            raise OrderRejected("options trade in whole contracts")
        if req.tif != "day":
            raise OrderRejected("option orders are DAY only")
    if req.type in ("limit", "stop_limit") and (not req.limit_price or req.limit_price <= 0):
        raise OrderRejected("limit order needs a positive price")
    if req.type in ("stop", "stop_limit") and (not req.stop_price or req.stop_price <= 0):
        raise OrderRejected("stop order needs a positive stop price")
    if req.type == "trailing_stop" and not (req.trail_percent and 0 < req.trail_percent < 50):
        raise OrderRejected("trail percent must be between 0 and 50")
    if not ref_price and req.type != "limit":
        raise OrderRejected(f"no price for {req.symbol}, look it up first so we can size the order")
    # options have wide spreads and move several percent on a small stock move
    band = 0.5 if cls == "option" else 0.10
    for label, p in (("limit", req.limit_price), ("stop", req.stop_price)):
        if p and ref_price and abs(p - ref_price) / ref_price > band:
            raise OrderRejected(f"{label} {p:.2f} is more than {band:.0%} from last {ref_price:.2f}, typo?")
    if req.type == "stop" and ref_price:
        # a buy stop below market (or sell stop above) would trigger instantly
        if (req.side == "buy" and req.stop_price <= ref_price) or (req.side == "sell" and req.stop_price >= ref_price):
            raise OrderRejected(f"{req.side} stop at {req.stop_price:.2f} would trigger immediately (last {ref_price:.2f})")
    px = max(p for p in (req.limit_price, req.stop_price, ref_price) if p)
    if not px:
        raise OrderRejected(f"no price for {req.symbol}")
    value = px * req.qty * multiplier(req.symbol)
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
