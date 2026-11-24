"""Finnhub WebSocket for streaming FX and metals (OANDA prices). Free key,
50 symbols per connection. These are dealer quotes from OANDA, which is how
spot FX trades: there is no single exchange price like there is for stocks."""
from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone

from ..store import Store

try:
    from websockets.asyncio.client import connect
except ImportError:  # pragma: no cover
    from websockets import connect  # type: ignore

PAIRS = {
    "EURUSD": "OANDA:EUR_USD", "GBPUSD": "OANDA:GBP_USD", "USDJPY": "OANDA:USD_JPY", "USDCHF": "OANDA:USD_CHF",
    "USDCAD": "OANDA:USD_CAD", "AUDUSD": "OANDA:AUD_USD", "USDCNY": "OANDA:USD_CNH", "USDMXN": "OANDA:USD_MXN",
    "XAUUSD": "OANDA:XAU_USD", "XAGUSD": "OANDA:XAG_USD",
}


class FxStream:
    def __init__(self, key: str, store: Store):
        self.key = key
        self.store = store
        self.by_feed = {v: k for k, v in PAIRS.items()}
        store.feed("FX", "Finnhub/OANDA stream", "dealer quotes, streaming")

    def handle(self, msg: dict) -> int:
        if msg.get("type") != "trade":
            return 0
        n = 0
        for d in msg.get("data") or []:
            label = self.by_feed.get(d.get("s"))
            if not label or d.get("p") is None:
                continue
            ts = datetime.fromtimestamp(int(d["t"]) / 1000, tz=timezone.utc) if d.get("t") else datetime.now(timezone.utc)
            self.store.fx[label] = (float(d["p"]), ts)
            n += 1
        if n:
            self.store.feed_ok("FX", 0.0, (datetime.now(timezone.utc) - ts).total_seconds())
        return n

    async def run(self) -> None:
        delay = 1.0
        while True:
            try:
                async with connect(f"wss://ws.finnhub.io?token={self.key}", open_timeout=10, ping_interval=20) as ws:
                    for s in PAIRS.values():
                        await ws.send(json.dumps({"type": "subscribe", "symbol": s}))
                    delay = 1.0
                    async for raw in ws:
                        self.handle(json.loads(raw))
            except asyncio.CancelledError:
                raise
            except Exception as e:
                self.store.feed_err("FX", f"stream down: {str(e)[:80]}")
            await asyncio.sleep(delay)
            delay = min(delay * 2, 60.0)
