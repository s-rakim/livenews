"""Delayed quotes for things with no free real-time feed: index levels,
futures, Treasury yields, VIX, the dollar index, FX crosses.

Source is Yahoo Finance's chart endpoint. It needs no key, but it's
unofficial and meant for personal use, and exchanges delay it: CME futures
about 10 minutes, most indices 0-15 minutes, FX a few seconds to a minute.
Every row shows the timestamp of the last print so you can see the real age.
Turn it off with LIVENEWS_DELAYED=off.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone

import httpx

from .base import MarketRow, TTLCache

CHART_URL = "https://query1.finance.yahoo.com/v8/finance/chart/{sym}"
UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"

INDICES = [("S&P 500", "^GSPC"), ("Nasdaq Comp", "^IXIC"), ("Dow 30", "^DJI"), ("Russell 2000", "^RUT"),
           ("VIX", "^VIX"), ("FTSE 100", "^FTSE"), ("DAX", "^GDAXI"), ("Nikkei 225", "^N225"), ("Hang Seng", "^HSI")]
FUTURES = [("S&P 500 e-mini", "ES=F"), ("Nasdaq 100 e-mini", "NQ=F"), ("Dow e-mini", "YM=F"),
           ("Russell e-mini", "RTY=F"), ("10Y T-note", "ZN=F"), ("30Y T-bond", "ZB=F"), ("Dollar index", "DX-Y.NYB")]
YIELDS = [("13W T-bill yield", "^IRX"), ("5Y yield", "^FVX"), ("10Y yield", "^TNX"), ("30Y yield", "^TYX")]
COMMODITY_FUT = [("WTI crude", "CL=F"), ("Brent crude", "BZ=F"), ("Natural gas", "NG=F"), ("Gold", "GC=F"),
                 ("Silver", "SI=F"), ("Copper", "HG=F"), ("Corn", "ZC=F"), ("Wheat", "ZW=F"), ("Soybeans", "ZS=F")]
FX = [("EURUSD", "EURUSD=X"), ("GBPUSD", "GBPUSD=X"), ("USDJPY", "JPY=X"), ("USDCHF", "CHF=X"),
      ("USDCAD", "CAD=X"), ("AUDUSD", "AUDUSD=X"), ("USDCNY", "CNY=X"), ("USDMXN", "MXN=X")]


def parse_chart_meta(js: dict) -> dict | None:
    res = (js.get("chart") or {}).get("result") or []
    if not res:
        return None
    m = res[0].get("meta") or {}
    px = m.get("regularMarketPrice")
    if px is None:
        return None
    prev = m.get("chartPreviousClose") or m.get("previousClose")
    t = m.get("regularMarketTime")
    return {
        "price": float(px),
        "prev": float(prev) if prev else None,
        "time": datetime.fromtimestamp(int(t), tz=timezone.utc) if t else None,
        "currency": m.get("currency") or "",
        "exchange": m.get("exchangeName") or "",
    }


YAHOO_SPANS = {"1D": ("1d", "5m"), "5D": ("5d", "15m"), "1M": ("1mo", "60m"), "3M": ("3mo", "1d"),
               "6M": ("6mo", "1d"), "1Y": ("1y", "1d"), "5Y": ("5y", "1wk")}


def parse_chart_bars(js: dict):
    """Yahoo chart -> list of (time, o, h, l, c, v). Skips bars with gaps."""
    res = (js.get("chart") or {}).get("result") or []
    if not res:
        return []
    r = res[0]
    ts = r.get("timestamp") or []
    q = ((r.get("indicators") or {}).get("quote") or [{}])[0]
    out = []
    for i, t in enumerate(ts):
        try:
            o, h, l, c = q["open"][i], q["high"][i], q["low"][i], q["close"][i]  # noqa: E741
        except (KeyError, IndexError):
            continue
        if None in (o, h, l, c):
            continue
        v = (q.get("volume") or [0] * len(ts))[i] or 0
        out.append((datetime.fromtimestamp(int(t), tz=timezone.utc), float(o), float(h), float(l), float(c), float(v)))
    return out


def is_yahoo_symbol(sym: str) -> bool:
    """Index (^GSPC), future (ES=F), FX (EURUSD=X) or index with a dot suffix (DX-Y.NYB)."""
    return sym.startswith("^") or "=" in sym or sym.endswith(".NYB")


class DelayedQuotes:
    def __init__(self, client: httpx.AsyncClient | None = None):
        self.http = client or httpx.AsyncClient(timeout=10.0, headers={"User-Agent": UA}, follow_redirects=True)
        self.cache = TTLCache()
        self._sem = asyncio.Semaphore(6)

    async def quote(self, sym: str) -> dict | None:
        hit = self.cache.get(sym)
        if hit is not None:
            return hit
        async with self._sem:
            r = await self.http.get(CHART_URL.format(sym=sym), params={"range": "1d", "interval": "5m"})
        if r.status_code == 429:
            raise RuntimeError("Yahoo rate limited, backing off")
        r.raise_for_status()
        q = parse_chart_meta(r.json())
        return self.cache.put(sym, q, 15.0)

    async def bars(self, sym: str, span: str):
        rng, iv = YAHOO_SPANS.get(span, YAHOO_SPANS["6M"])
        key = ("bars", sym, span)
        hit = self.cache.get(key)
        if hit is not None:
            return hit
        async with self._sem:
            r = await self.http.get(CHART_URL.format(sym=sym), params={"range": rng, "interval": iv})
        r.raise_for_status()
        return self.cache.put(key, parse_chart_bars(r.json()), 30.0 if span in ("1D", "5D") else 300.0)

    async def rows(self, items: list[tuple[str, str]], unit: str = "") -> list[MarketRow]:
        async def one(name, sym):
            try:
                q = await self.quote(sym)
            except Exception as e:
                return MarketRow(name, sym, None, None, None, None, f"Yahoo err {str(e)[:30]}", False, unit)
            if not q:
                return MarketRow(name, sym, None, None, None, None, "Yahoo (no data)", False, unit)
            ch = q["price"] - q["prev"] if q["prev"] else None
            pct = None if unit == "%" or ch is None else ch / q["prev"] * 100
            return MarketRow(name, sym, q["price"], ch, pct, q["time"], f"Yahoo delayed {q['exchange']}".strip(), True, unit)
        return list(await asyncio.gather(*(one(n, s) for n, s in items)))

    async def aclose(self) -> None:
        await self.http.aclose()
