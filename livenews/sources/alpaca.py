"""Alpaca market data: news, screener movers, and snapshots.

Free plan limits as of writing (check https://docs.alpaca.markets if in doubt):
  * 200 data API requests per minute, shared across all endpoints below.
  * Stock prices on the free plan come from IEX only. That's real-time but it
    is one exchange (a few percent of US volume), so volume numbers are much
    smaller than the consolidated tape and prices can differ by a cent or two
    from the SIP. The paid plan (feed=sip) fixes that.
  * News is Benzinga's feed, included free.
  * The movers/most-actives screener is built from SIP data but Alpaca
    refreshes it on its own schedule, roughly every minute, so polling it
    faster than that just burns requests.
"""
from __future__ import annotations

import asyncio
import time

import httpx

from ..config import DATA_URL, Settings
from ..models import NewsItem, Quote, utcnow
from ..ratelimit import RateLimiter
from ..store import Store
from ..timeparse import parse_ts


def auth_headers(s: Settings) -> dict[str, str]:
    return {"APCA-API-KEY-ID": s.alpaca_key, "APCA-API-SECRET-KEY": s.alpaca_secret}


def parse_news(raw: dict) -> NewsItem | None:
    ts = parse_ts(raw.get("created_at"))
    if not ts or not raw.get("headline"):
        return None
    return NewsItem(
        id=f"alp-{raw.get('id')}",
        headline=raw["headline"].strip(),
        source=(raw.get("source") or "benzinga").upper(),
        created_at=ts,
        symbols=[s.upper() for s in raw.get("symbols") or []],
        summary=(raw.get("summary") or "").strip(),
        url=raw.get("url") or "",
    )


def minute_key(raw: str) -> str:
    ts = parse_ts(raw)
    return ts.strftime("%Y%m%d%H%M") if ts else raw


def parse_snapshot(sym: str, snap: dict) -> tuple[Quote, str | None] | None:
    trade = snap.get("latestTrade") or {}
    day = snap.get("dailyBar") or {}
    prev = snap.get("prevDailyBar") or {}
    minute = snap.get("minuteBar") or {}
    quote = snap.get("latestQuote") or {}
    price = trade.get("p") or day.get("c")
    if not price:
        return None
    q = Quote(
        symbol=sym,
        price=float(price),
        prev_close=float(prev.get("c") or 0),
        day_volume=float(day.get("v") or 0),
        prev_volume=float(prev.get("v") or 0),
        minute_volume=float(minute.get("v") or 0),
        bid=float(quote.get("bp") or 0),
        ask=float(quote.get("ap") or 0),
        high=float(day.get("h") or 0),
        low=float(day.get("l") or 0),
        updated_at=parse_ts(trade.get("t")) or utcnow(),
    )
    return q, minute.get("t")


class AlpacaData:
    def __init__(self, settings: Settings, store: Store, client: httpx.AsyncClient | None = None):
        self.s = settings
        self.store = store
        self.limiter = RateLimiter(settings.data_rate_per_min)
        self.client = client or httpx.AsyncClient(
            base_url=DATA_URL, headers=auth_headers(settings), timeout=10.0
        )
        self.extra_symbols: set[str] = set()  # anything the user looked up
        feed_note = "IEX only (one exchange, real-time)" if settings.alpaca_feed == "iex" else "SIP consolidated"
        store.feed("NEWS", "Alpaca/Benzinga", f"polled every {settings.news_interval:g}s")
        store.feed("QUOTES", f"Alpaca {settings.alpaca_feed.upper()}", feed_note)
        store.feed("MOVERS", "Alpaca screener", "provider refreshes ~1/min")

    async def _get(self, path: str, params: dict | None = None) -> tuple[dict, float]:
        await self.limiter.acquire()
        t0 = time.perf_counter()
        r = await self.client.get(path, params=params)
        rtt = (time.perf_counter() - t0) * 1000
        if r.status_code == 429:
            raise RuntimeError("rate limited (429)")
        r.raise_for_status()
        return r.json(), rtt

    # news -------------------------------------------------------------------
    async def poll_news_once(self) -> int:
        data, rtt = await self._get("/v1beta1/news", {"limit": 50, "sort": "desc", "include_content": "false"})
        added = 0
        newest_age = None
        for raw in data.get("news", []):
            item = parse_news(raw)
            if item and self.store.add_news(item):
                added += 1
                age = item.age_seconds
                newest_age = age if newest_age is None else min(newest_age, age)
        self.store.feed_ok("NEWS", rtt, newest_age)
        return added

    # movers -----------------------------------------------------------------
    async def poll_movers_once(self) -> None:
        data, rtt = await self._get("/v1beta1/screener/stocks/movers", {"top": 25})
        self.store.provider_gainers = [g["symbol"] for g in data.get("gainers", [])]
        self.store.provider_losers = [g["symbol"] for g in data.get("losers", [])]
        act, rtt2 = await self._get("/v1beta1/screener/stocks/most-actives", {"by": "volume", "top": 25})
        self.store.most_active = [a["symbol"] for a in act.get("most_actives", [])]
        upd = parse_ts(data.get("last_updated"))
        self.store.feed_ok("MOVERS", max(rtt, rtt2), (utcnow() - upd).total_seconds() if upd else None)

    # quotes -----------------------------------------------------------------
    def universe(self) -> list[str]:
        syms = dict.fromkeys(
            [*self.s.watchlist, *self.extra_symbols, *self.store.provider_gainers,
             *self.store.provider_losers, *self.store.most_active]
        )
        # News-tagged tickers from the last 30 minutes too, so news movers show up.
        for n in list(self.store.news)[:60]:
            if n.age_seconds > 1800:
                break
            for s in n.symbols:
                if s.isalpha() and len(s) <= 5:
                    syms.setdefault(s)
        return list(syms)[:200]

    async def poll_quotes_once(self) -> None:
        syms = self.universe()
        if not syms:
            return
        worst_age = 0.0
        rtt_max = 0.0
        for i in range(0, len(syms), 100):
            chunk = syms[i : i + 100]
            data, rtt = await self._get(
                "/v2/stocks/snapshots", {"symbols": ",".join(chunk), "feed": self.s.alpaca_feed}
            )
            rtt_max = max(rtt_max, rtt)
            # The endpoint has returned both {"AAPL": {...}} and {"snapshots": {...}}
            snaps = data.get("snapshots", data) if isinstance(data, dict) else {}
            for sym, snap in snaps.items():
                if not isinstance(snap, dict):
                    continue
                parsed = parse_snapshot(sym, snap)
                if not parsed:
                    continue
                q, minute_t = parsed
                old = self.store.quotes.get(sym)
                if old and old.updated_at > q.updated_at:
                    # the stream already gave us a newer trade, keep that price
                    q.price, q.updated_at = old.price, old.updated_at
                    q.day_volume = max(q.day_volume, old.day_volume)
                self.store.update_quote(q)
                if minute_t:
                    self.store.record_minute(sym, minute_key(minute_t), q.minute_volume)
                if sym in self.s.watchlist:
                    worst_age = max(worst_age, (utcnow() - q.updated_at).total_seconds())
        self.store.feed_ok("QUOTES", rtt_max, worst_age)

    # loops ------------------------------------------------------------------
    async def _loop(self, name: str, fn, interval: float) -> None:
        backoff = interval
        while True:
            t0 = time.monotonic()
            try:
                await fn()
                backoff = interval
            except asyncio.CancelledError:
                raise
            except Exception as e:  # keep the terminal alive through network blips
                self.store.feed_err(name, str(e)[:120])
                backoff = min(backoff * 2, 30.0)
            elapsed = time.monotonic() - t0
            await asyncio.sleep(max(0.0, backoff - elapsed))

    def tasks(self) -> list:
        return [
            self._loop("NEWS", self.poll_news_once, self.s.news_interval),
            self._loop("MOVERS", self.poll_movers_once, self.s.movers_interval),
            self._loop("QUOTES", self.poll_quotes_once, self.s.quotes_interval),
        ]

    async def aclose(self) -> None:
        await self.client.aclose()
