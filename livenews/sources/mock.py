"""Simulated market and news wire so the terminal runs with no keys at all.

Everything here is made up. Prices start near plausible levels but are a
random walk; headlines come from templates. The header shows MOCK in big
letters whenever this is active.
"""
from __future__ import annotations

import asyncio
import itertools
import random
from dataclasses import dataclass

from ..models import NewsItem, Quote, utcnow
from ..store import Store

UNIVERSE = {
    "SPY": 590, "QQQ": 510, "AAPL": 228, "MSFT": 420, "NVDA": 140, "TSLA": 340, "AMZN": 205,
    "META": 570, "GOOGL": 170, "AMD": 140, "NFLX": 880, "AVGO": 170, "JPM": 245, "BAC": 46,
    "XOM": 120, "CVX": 160, "PFE": 26, "MRNA": 40, "LLY": 780, "UNH": 590, "WMT": 88,
    "COST": 920, "DIS": 112, "BA": 155, "INTC": 24, "MU": 100, "PLTR": 62, "SOFI": 15,
    "COIN": 300, "MSTR": 380, "RIVN": 12, "LCID": 3, "NIO": 4.5, "F": 11, "GM": 55,
    "UBER": 70, "ABNB": 135, "SHOP": 110, "SQ": 90, "PYPL": 85, "CRM": 330, "ORCL": 185,
    "ADBE": 510, "SNOW": 170, "SMCI": 30, "ARM": 140, "TSM": 195, "BABA": 88, "GME": 28,
    "AMC": 4.6, "HOOD": 38, "MARA": 23, "RIOT": 12,
}
CRYPTO = {"BTC/USD": 96000, "ETH/USD": 3400, "SOL/USD": 180}

UP = [
    "{s} beats on revenue and raises full-year guidance",
    "{s} shares jump after FDA clears key product",
    "{s} announces ${n}B buyback program",
    "Analyst upgrades {s} to Buy, sees {n}0% upside",
    "{s} wins multi-year contract worth ${n}B",
    "Report: {s} in talks for strategic partnership",
]
DOWN = [
    "{s} misses estimates, cuts outlook",
    "{s} falls after CEO departure announced",
    "Regulators open probe into {s}",
    "{s} downgraded to Sell on margin concerns",
    "{s} recalls products over safety issue",
    "{s} prices secondary offering below market",
]
MACRO = [
    "Treasury yields edge higher ahead of jobs data",
    "Fed officials signal patience on rate path",
    "Oil steady as traders weigh supply outlook",
    "Dollar firms against yen in quiet trade",
    "US weekly jobless claims come in near forecast",
    "European stocks close mixed, banks lead",
]

CRAMER = [
    "Jim Cramer says {s} is a buy here, 'don't sell it'",
    "Cramer on Mad Money: {s} has more room to run",
    "Jim Cramer warns investors to take profits in {s}",
    "Cramer's lightning round: stay away from {s}",
]

MOCK_MINUTE_SECONDS = 5  # a "minute bar" closes every 5s so spikes show quickly


@dataclass
class _Sym:
    price: float
    prev: float
    day_vol: float
    prev_vol: float
    minute_vol: float = 0.0
    high: float = 0.0
    low: float = 0.0
    drift: float = 0.0
    drift_ticks: int = 0
    vol_mult: float = 1.0


class MockMarket:
    def __init__(self, store: Store, seed: int | None = None, tick: float = 1.0):
        self.store = store
        self.rng = random.Random(seed)
        self.tick = tick
        self.syms: dict[str, _Sym] = {}
        from ..engine import session_fraction

        frac = session_fraction()
        for s, px in {**UNIVERSE, **CRYPTO}.items():
            prev = px * (1 + self.rng.uniform(-0.01, 0.01))
            pv = self.rng.uniform(5e6, 60e6)
            # most names trade near normal pace, a few are already running hot
            rv = self.rng.lognormvariate(0, 0.45)
            self.syms[s] = _Sym(price=prev, prev=prev, day_vol=pv * frac * rv, prev_vol=pv, high=prev, low=prev)
        self._ids = itertools.count(1)
        self._ticks = 0
        store.feed("NEWS", "MOCK wire", "simulated headlines")
        store.feed("QUOTES", "MOCK", "simulated prices, not real")
        store.feed("MOVERS", "local engine", "ranked from simulated quotes")

    def price(self, sym: str) -> float | None:
        s = self.syms.get(sym)
        return round(s.price, 2) if s else None

    def _headline(self) -> NewsItem:
        r = self.rng.random()
        if r < 0.08:
            sym = self.rng.choice(list(UNIVERSE))
            text, syms = self.rng.choice(CRAMER).format(s=sym), [sym]
        elif r < 0.30:
            text, syms = self.rng.choice(MACRO), []
        else:
            sym = self.rng.choice(list(UNIVERSE))
            up = self.rng.random() < 0.55
            text = self.rng.choice(UP if up else DOWN).format(s=sym, n=self.rng.randint(1, 9))
            syms = [sym]
            st = self.syms[sym]
            # the news actually moves the stock for a while
            st.drift = (1 if up else -1) * self.rng.uniform(0.002, 0.008)
            st.drift_ticks = self.rng.randint(8, 30)
            st.vol_mult = self.rng.uniform(15, 60)
            st.day_vol += st.prev_vol * self.rng.uniform(0.1, 0.6)  # the news itself brings a burst
        return NewsItem(
            id=f"mock-{next(self._ids)}", headline=text, source="MOCK",
            created_at=utcnow(), symbols=syms,
        )

    def step(self) -> None:
        self._ticks += 1
        new_minute = self._ticks % MOCK_MINUTE_SECONDS == 0
        for sym, st in self.syms.items():
            shock = self.rng.gauss(0, 0.0012)
            if st.drift_ticks > 0:
                shock += st.drift
                st.drift_ticks -= 1
            else:
                st.vol_mult = max(1.0, st.vol_mult * 0.9)
            st.price = max(0.5, st.price * (1 + shock))
            st.high = max(st.high, st.price)
            st.low = min(st.low, st.price)
            v = st.prev_vol / 23400 * self.rng.uniform(0.5, 1.5) * st.vol_mult
            st.day_vol += v
            st.minute_vol += v
            px = round(st.price, 2)
            spread = max(0.01, round(px * 0.0002, 2))
            q = Quote(
                symbol=sym, price=px, prev_close=round(st.prev, 2), day_volume=st.day_vol,
                prev_volume=st.prev_vol, minute_volume=st.minute_vol, bid=round(px - spread / 2, 2),
                ask=round(px + spread / 2, 2), high=round(st.high, 2), low=round(st.low, 2),
            )
            self.store.update_quote(q)
            if new_minute:
                self.store.record_minute(sym, str(self._ticks), st.minute_vol)
                st.minute_vol = 0.0
        self.store.feed_ok("QUOTES", 0.0, 0.0)
        self.store.feed_ok("MOVERS", 0.0, 0.0)

    def maybe_news(self) -> None:
        if self.rng.random() < 0.35:
            self.store.add_news(self._headline())
        self.store.feed_ok("NEWS", 0.0, self.store.news[0].age_seconds if self.store.news else None)

    def seed_history(self, n: int = 20) -> None:
        for _ in range(n):
            self.store.add_news(self._headline())

    async def run(self) -> None:
        self.seed_history(8)
        while True:
            self.step()
            self.maybe_news()
            await asyncio.sleep(self.tick)

    def tasks(self) -> list:
        return [self.run()]


def mock_disclosures(tracker, seed: int | None = None, n: int = 8) -> None:
    """Fake congressional filings so the PEOPLE panel isn't empty in mock mode."""
    from datetime import timedelta

    from .people import PeopleEvent

    rng = random.Random(seed)
    names = ["Nancy Pelosi (H)", "Tommy Tuberville (S)", "Ro Khanna (H)", "Dan Crenshaw (H)",
             "Markwayne Mullin (S)", "Josh Gottheimer (H)"]
    amounts = ["$1,001 - $15,000", "$15,001 - $50,000", "$50,001 - $100,000",
               "$250,001 - $500,000", "$1,000,001 - $5,000,000"]
    today = utcnow()
    for i in range(n):
        filed = today - timedelta(days=rng.randint(0, 6))
        traded = (filed - timedelta(days=rng.randint(3, 44))).date()
        sym = rng.choice(list(UNIVERSE))
        tracker._add(PeopleEvent(
            kind="TRADE", person=rng.choice(names), symbol=sym,
            action=rng.choice(["Purchase", "Sale", "Purchase"]), amount=rng.choice(amounts),
            traded=traded, filed=filed, source="MOCK", key=f"mock-disc-{i}",
        ))
