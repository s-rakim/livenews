"""Alpaca WebSocket streams: news pushed as it's published, trades and minute
bars for the symbols you care about.

This is the low-latency path. The REST pollers in alpaca.py keep running as a
backstop, and duplicates are dropped by id, so if the socket dies nothing is
lost, it just gets slower until it reconnects.

Free plan limits as of writing:
  * one connection per stream endpoint per account (a second terminal open on
    the same keys gets error 406 and kicks nothing, it just fails)
  * 30 symbols on the IEX stock stream
  * news stream: all symbols via "*"
"""
from __future__ import annotations

import asyncio
import json

from ..broker.base import asset_class
from ..config import Settings
from ..models import utcnow
from ..store import Store
from ..timeparse import parse_ts
from .alpaca import minute_key, parse_news

try:
    from websockets.asyncio.client import connect
except ImportError:  # older websockets
    from websockets import connect  # type: ignore

STREAM_BASE = "wss://stream.data.alpaca.markets"


class StreamError(Exception):
    pass


async def _authenticate(ws, s: Settings) -> None:
    await ws.send(json.dumps({"action": "auth", "key": s.alpaca_key, "secret": s.alpaca_secret}))
    for _ in range(3):
        msgs = json.loads(await asyncio.wait_for(ws.recv(), 10))
        for m in msgs:
            if m.get("T") == "success" and m.get("msg") == "authenticated":
                return
            if m.get("T") == "error":
                raise StreamError(f"{m.get('code')} {m.get('msg')}")
    raise StreamError("no auth response")


class _Base:
    name = "STREAM"

    def __init__(self, settings: Settings, store: Store):
        self.s = settings
        self.store = store
        self.connected = False
        self.ws = None

    @property
    def url(self) -> str:
        raise NotImplementedError

    async def on_open(self, ws) -> None:
        raise NotImplementedError

    def handle(self, m: dict) -> None:
        raise NotImplementedError

    async def run(self) -> None:
        delay = 1.0
        while True:
            try:
                async with connect(self.url, open_timeout=10, ping_interval=20) as ws:
                    self.ws = ws
                    await _authenticate(ws, self.s)
                    await self.on_open(ws)
                    self.connected = True
                    self.store.note(f"{self.name} stream connected")
                    delay = 1.0
                    async for raw in ws:
                        for m in json.loads(raw):
                            if m.get("T") == "error":
                                self.store.feed_err(self.name, f"stream {m.get('code')} {m.get('msg')}")
                            else:
                                self.handle(m)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                self.store.feed_err(self.name, f"stream down: {str(e)[:80]}")
            self.connected = False
            self.ws = None
            await asyncio.sleep(delay)
            delay = min(delay * 2, 30.0)


class NewsStream(_Base):
    name = "NEWS"

    @property
    def url(self) -> str:
        return f"{STREAM_BASE}/v1beta1/news"

    async def on_open(self, ws) -> None:
        await ws.send(json.dumps({"action": "subscribe", "news": ["*"]}))
        self.store.feed("NEWS").source = "Alpaca/Benzinga stream"

    def handle(self, m: dict) -> None:
        if m.get("T") != "n":
            return
        item = parse_news(m)
        if item and self.store.add_news(item):
            self.store.feed_ok("NEWS", 0.0, item.age_seconds)


class TradeStream(_Base):
    name = "QUOTES"
    asset = "equity"

    def __init__(self, settings: Settings, store: Store, limit: int = 30):
        super().__init__(settings, store)
        self.limit = limit
        self.symbols: list[str] = [s for s in dict.fromkeys(settings.watchlist) if asset_class(s) == self.asset][:limit]
        self.last_trade_age: float | None = None

    @property
    def url(self) -> str:
        return f"{STREAM_BASE}/v2/{self.s.alpaca_feed}"

    async def on_open(self, ws) -> None:
        if self.symbols:
            await ws.send(json.dumps({"action": "subscribe", "trades": self.symbols, "bars": self.symbols}))

    async def follow(self, symbol: str) -> None:
        """Start streaming a symbol the user just looked up. Drops the oldest
        non-watchlist symbol if we're at the plan's limit."""
        if symbol in self.symbols or asset_class(symbol) != self.asset:
            return
        dropped = None
        if len(self.symbols) >= self.limit:
            for s in self.symbols:
                if s not in self.s.watchlist:
                    dropped = s
                    break
            if dropped is None:
                return
            self.symbols.remove(dropped)
        self.symbols.append(symbol)
        if self.ws is not None:
            if dropped:
                await self.ws.send(json.dumps({"action": "unsubscribe", "trades": [dropped], "bars": [dropped]}))
            await self.ws.send(json.dumps({"action": "subscribe", "trades": [symbol], "bars": [symbol]}))

    def handle(self, m: dict) -> None:
        t = m.get("T")
        sym = m.get("S")
        if not sym:
            return
        if t == "t":
            ts = parse_ts(m.get("t")) or utcnow()
            if self.store.apply_trade(sym, float(m["p"]), float(m.get("s") or 0), ts):
                age = (utcnow() - ts).total_seconds()
                self.store.feed_ok(self.name, 0.0, age)
        elif t == "b":
            self.store.record_minute(sym, minute_key(m.get("t", "")), float(m.get("v") or 0))
            q = self.store.quotes.get(sym)
            if q:
                q.minute_volume = float(m.get("v") or 0)


class CryptoStream(TradeStream):
    """Same message shape as the stock stream, different endpoint. Free and
    real-time, no 30-symbol cap that I know of; 100 here to be polite."""

    name = "CRYPTO"
    asset = "crypto"

    def __init__(self, settings: Settings, store: Store):
        super().__init__(settings, store, limit=100)

    @property
    def url(self) -> str:
        return f"{STREAM_BASE}/v1beta3/crypto/us"
