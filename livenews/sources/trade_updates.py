"""Alpaca trade_updates stream: every order event (accepted, partial fill,
fill, cancel, reject) as it happens, so the blotter shows fills the moment
the broker reports them rather than on the next 2s refresh."""
from __future__ import annotations

import asyncio
import json

from ..config import Settings
from ..models import Execution, utcnow
from ..store import Store
from ..timeparse import parse_ts

try:
    from websockets.asyncio.client import connect
except ImportError:  # pragma: no cover
    from websockets import connect  # type: ignore


def parse_update(data: dict) -> Execution | None:
    o = data.get("order") or {}
    ev = data.get("event")
    if not ev or not o:
        return None
    ts = parse_ts(data.get("timestamp")) or utcnow()
    sub = parse_ts(o.get("submitted_at"))
    is_fill = ev in ("fill", "partial_fill")
    qty = float(data.get("qty") or 0) if is_fill else float(o.get("qty") or 0)
    price = float(data["price"]) if is_fill and data.get("price") else None
    return Execution(
        ts=ts, event=ev, order_id=o.get("id", ""), symbol=o.get("symbol", ""), side=o.get("side", ""),
        qty=qty, price=price,
        position_qty=float(data["position_qty"]) if data.get("position_qty") not in (None, "") else None,
        latency_ms=(ts - sub).total_seconds() * 1000 if sub and is_fill else None,
        source="Alpaca trade_updates",
    )


class TradeUpdates:
    def __init__(self, settings: Settings, store: Store, on_event=None):
        self.s = settings
        self.store = store
        self.on_event = on_event
        self.url = settings.trading_url.replace("https://", "wss://") + "/stream"
        store.feed("FILLS", "Alpaca trade_updates", "order events pushed live")

    def handle(self, msg: dict) -> None:
        if msg.get("stream") != "trade_updates":
            return
        ex = parse_update(msg.get("data") or {})
        if ex:
            self.store.executions.appendleft(ex)
            self.store.feed_ok("FILLS", 0.0, (utcnow() - ex.ts).total_seconds())
            px = f" @ {ex.price:.2f}" if ex.price else ""
            self.store.note(f"{ex.event.upper()} {ex.side} {ex.qty:g} {ex.symbol}{px}")
            if self.on_event:
                self.on_event(ex)

    async def run(self) -> None:
        delay = 1.0
        while True:
            try:
                async with connect(self.url, open_timeout=10, ping_interval=20) as ws:
                    await ws.send(json.dumps({"action": "auth", "key": self.s.alpaca_key, "secret": self.s.alpaca_secret}))
                    auth = json.loads(await asyncio.wait_for(ws.recv(), 10))
                    if (auth.get("data") or {}).get("status") != "authorized":
                        raise RuntimeError(f"auth refused: {auth}")
                    await ws.send(json.dumps({"action": "listen", "data": {"streams": ["trade_updates"]}}))
                    self.store.feed_ok("FILLS", 0.0)
                    delay = 1.0
                    async for raw in ws:
                        # the trading stream sends binary frames
                        if isinstance(raw, bytes):
                            raw = raw.decode()
                        self.handle(json.loads(raw))
            except asyncio.CancelledError:
                raise
            except Exception as e:
                self.store.feed_err("FILLS", f"stream down: {str(e)[:80]}")
            await asyncio.sleep(delay)
            delay = min(delay * 2, 30.0)
