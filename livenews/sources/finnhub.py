"""Optional second news wire from Finnhub (free key at finnhub.io).

Free tier: 60 API calls per minute. We poll general market news every 2s by
default (30/min) so there's headroom. Finnhub's general feed often has no
ticker tags, so these items mostly land in the wire without a symbol.
"""
from __future__ import annotations

import asyncio
import time
from datetime import datetime, timezone

import httpx

from ..models import NewsItem
from ..ratelimit import RateLimiter
from ..store import Store


def parse_finnhub(raw: dict) -> NewsItem | None:
    if not raw.get("headline") or not raw.get("datetime"):
        return None
    related = raw.get("related") or ""
    return NewsItem(
        id=f"fh-{raw.get('id')}",
        headline=raw["headline"].strip(),
        source=(raw.get("source") or "finnhub").upper(),
        created_at=datetime.fromtimestamp(int(raw["datetime"]), tz=timezone.utc),
        symbols=[s.strip().upper() for s in related.split(",") if s.strip()],
        summary=(raw.get("summary") or "").strip(),
        url=raw.get("url") or "",
    )


class FinnhubNews:
    def __init__(self, key: str, store: Store, interval: float = 2.0):
        self.key = key
        self.store = store
        self.interval = max(interval, 1.0)
        self.limiter = RateLimiter(55)
        self.client = httpx.AsyncClient(base_url="https://finnhub.io/api/v1", timeout=10.0)
        self.min_id = 0
        store.feed("WIRE2", "Finnhub", f"polled every {self.interval:g}s, 60 calls/min cap")

    async def poll_once(self) -> int:
        await self.limiter.acquire()
        t0 = time.perf_counter()
        r = await self.client.get(
            "/news", params={"category": "general", "minId": self.min_id},
            headers={"X-Finnhub-Token": self.key},
        )
        rtt = (time.perf_counter() - t0) * 1000
        r.raise_for_status()
        added, newest = 0, None
        for raw in r.json() or []:
            self.min_id = max(self.min_id, int(raw.get("id") or 0))
            item = parse_finnhub(raw)
            if item and self.store.add_news(item):
                added += 1
                newest = item.age_seconds if newest is None else min(newest, item.age_seconds)
        self.store.feed_ok("WIRE2", rtt, newest)
        return added

    async def run(self) -> None:
        while True:
            try:
                await self.poll_once()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                self.store.feed_err("WIRE2", str(e)[:120])
            await asyncio.sleep(self.interval)

    async def aclose(self) -> None:
        await self.client.aclose()
