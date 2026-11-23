"""Everything the function screens ask for goes through a Hub. The live one
talks to Alpaca, FRED, Frankfurter, SEC EDGAR and Finnhub; the mock one makes
things up. Screens don't care which they got."""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import date, datetime


class Unavailable(Exception):
    """Feature can't run: missing key, plan doesn't include it, provider down.
    The message is shown to the user as-is, so make it say what to do."""


@dataclass
class Bar:
    t: datetime
    o: float
    h: float
    l: float  # noqa: E741
    c: float
    v: float


@dataclass
class MarketRow:
    name: str
    symbol: str
    value: float | None
    change: float | None
    pct: float | None
    asof: datetime | date | None
    source: str
    live: bool  # True = intraday/streaming, False = daily close or slower
    unit: str = ""


@dataclass
class EcoEvent:
    when: date
    name: str
    kind: str  # "release", "earnings", "fomc"
    important: bool = False
    detail: str = ""
    source: str = ""


@dataclass
class Profile:
    symbol: str
    name: str = ""
    exchange: str = ""
    industry: str = ""
    country: str = ""
    market_cap: float | None = None
    shares: float | None = None
    ipo: str = ""
    web: str = ""
    cik: str = ""
    sic: str = ""
    fiscal_year_end: str = ""
    filings: list[dict] = field(default_factory=list)  # form, date, url
    sources: list[str] = field(default_factory=list)


@dataclass
class Financials:
    symbol: str
    period: str  # "A" or "Q"
    periods: list[str]  # column headers, oldest -> newest
    rows: list[tuple[str, list[float | None], str]]  # label, values, format ("usd", "eps", "pct")
    source: str


@dataclass
class OptionQuote:
    symbol: str
    strike: float
    right: str
    bid: float | None = None
    ask: float | None = None
    last: float | None = None
    iv: float | None = None
    delta: float | None = None
    gamma: float | None = None
    theta: float | None = None
    vega: float | None = None
    volume: float | None = None


@dataclass
class Chain:
    underlying: str
    spot: float | None
    expiries: list[date]
    expiry: date | None
    calls: dict[float, OptionQuote]
    puts: dict[float, OptionQuote]
    source: str
    note: str = ""


class TTLCache:
    def __init__(self):
        self._d: dict = {}

    def get(self, key):
        v = self._d.get(key)
        if v and v[0] > time.monotonic():
            return v[1]
        return None

    def put(self, key, val, ttl: float):
        self._d[key] = (time.monotonic() + ttl, val)
        return val


class Hub:
    """Interface. Every method may raise Unavailable."""

    name = "base"

    async def bars(self, symbol: str, span: str) -> tuple[list[Bar], str]:
        """span: 1D 5D 1M 6M 1Y 5Y. Returns bars and a label saying where they came from."""
        raise NotImplementedError

    async def daily_closes(self, symbol: str, days: int = 260) -> list[float]:
        bars, _ = await self.bars(symbol, "1Y")
        return [b.c for b in bars][-days:]

    async def markets(self) -> dict[str, list[MarketRow]]:
        raise NotImplementedError

    async def eco(self, days: int = 14, symbols: list[str] | None = None) -> list[EcoEvent]:
        raise NotImplementedError

    async def profile(self, symbol: str) -> Profile:
        raise NotImplementedError

    async def financials(self, symbol: str, period: str = "A") -> Financials:
        raise NotImplementedError

    async def analysts(self, symbol: str) -> dict:
        """{"recs": [...], "earnings": [...], "source": str}"""
        raise NotImplementedError

    async def insiders(self, symbol: str) -> dict:
        """{"rows": [...], "source": str, "note": str}"""
        raise NotImplementedError

    async def chain(self, symbol: str, expiry: date | None = None) -> Chain:
        raise NotImplementedError

    async def aclose(self) -> None:
        pass
