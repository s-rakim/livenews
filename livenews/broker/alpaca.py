from __future__ import annotations

import uuid

import httpx

from ..config import Settings
from ..models import Account, Order, Position
from ..store import Store
from ..timeparse import parse_ts
from .base import Broker, OrderRejected, OrderRequest


def _order(raw: dict) -> Order:
    return Order(
        id=raw["id"], symbol=raw["symbol"], side=raw["side"], qty=float(raw.get("qty") or 0),
        type=raw.get("type") or raw.get("order_type") or "market", status=raw["status"],
        limit_price=float(raw["limit_price"]) if raw.get("limit_price") else None,
        filled_avg_price=float(raw["filled_avg_price"]) if raw.get("filled_avg_price") else None,
        submitted_at=parse_ts(raw.get("submitted_at") or raw.get("created_at")) or parse_ts("1970-01-01T00:00:00Z"),
        stop_price=float(raw["stop_price"]) if raw.get("stop_price") else None,
        tif=raw.get("time_in_force") or "day",
        filled_qty=float(raw.get("filled_qty") or 0),
        trail_percent=float(raw["trail_percent"]) if raw.get("trail_percent") else None,
    )


def _qty(q: float) -> str:
    return format(q, "f").rstrip("0").rstrip(".") if q != int(q) else str(int(q))


def _px(p: float) -> str:
    return f"{p:.2f}" if p >= 1 else f"{p:.6f}".rstrip("0")


def position_symbol(raw: dict) -> str:
    """Alpaca reports crypto positions as BTCUSD while quotes and orders use
    BTC/USD. Put the slash back so the position lines up with its quote."""
    sym = raw["symbol"]
    if raw.get("asset_class") == "crypto" and "/" not in sym:
        for quote in ("USDT", "USDC", "USD", "BTC"):
            if sym.endswith(quote) and len(sym) > len(quote):
                return f"{sym[:-len(quote)]}/{quote}"
    return sym


class AlpacaBroker(Broker):
    """Alpaca trading API. Paper by default: the URL comes from Settings and
    only points at the live endpoint when both live switches are set."""

    def __init__(self, settings: Settings, store: Store):
        self.s = settings
        self.store = store
        self.is_live = settings.is_live
        self.name = "Alpaca LIVE" if self.is_live else "Alpaca paper"
        self.client = httpx.AsyncClient(
            base_url=settings.trading_url,
            headers={"APCA-API-KEY-ID": settings.alpaca_key, "APCA-API-SECRET-KEY": settings.alpaca_secret},
            timeout=10.0,
        )

    async def submit(self, req: OrderRequest) -> Order:
        body = {
            "symbol": req.symbol, "qty": _qty(req.qty), "side": req.side, "type": req.type,
            "time_in_force": req.tif,
            # idempotency: if the request times out and we retry, Alpaca won't double fill
            "client_order_id": f"ln-{uuid.uuid4().hex[:20]}",
        }
        if req.type in ("limit", "stop_limit"):
            body["limit_price"] = _px(req.limit_price)
        if req.type in ("stop", "stop_limit"):
            body["stop_price"] = _px(req.stop_price)
        if req.type == "trailing_stop":
            body["trail_percent"] = f"{req.trail_percent:g}"
        r = await self.client.post("/v2/orders", json=body)
        if r.status_code >= 400:
            try:
                msg = r.json().get("message", r.text)
            except Exception:
                msg = r.text
            raise OrderRejected(f"Alpaca {r.status_code}: {msg}")
        return _order(r.json())

    async def cancel(self, order_id: str) -> None:
        r = await self.client.delete(f"/v2/orders/{order_id}")
        if r.status_code not in (200, 204):
            raise OrderRejected(f"cancel failed {r.status_code}: {r.text[:100]}")

    async def cancel_all(self) -> None:
        r = await self.client.delete("/v2/orders")
        r.raise_for_status()

    async def refresh(self) -> None:
        a = (await self.client.get("/v2/account")).raise_for_status().json()
        equity = float(a["equity"])
        self.store.account = Account(
            equity=equity, cash=float(a["cash"]), buying_power=float(a["buying_power"]),
            day_pl=equity - float(a.get("last_equity") or equity),
        )
        pos = (await self.client.get("/v2/positions")).raise_for_status().json()
        self.store.positions = [
            Position(position_symbol(p), float(p["qty"]), float(p["avg_entry_price"]), float(p["current_price"]),
                     100.0 if p.get("asset_class") == "us_option" else 1.0)
            for p in pos
        ]
        orders = (await self.client.get("/v2/orders", params={"status": "all", "limit": 30})).raise_for_status().json()
        self.store.orders = [_order(o) for o in orders]

    async def aclose(self) -> None:
        await self.client.aclose()
