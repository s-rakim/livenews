"""Made-up data for every function screen, so --mock shows the whole terminal.
Deterministic per symbol so the same ticker looks the same each time."""
from __future__ import annotations

import math
import random
from datetime import date, datetime, timedelta, timezone

from ..options_math import black_scholes, occ_symbol
from ..sources.mock import UNIVERSE
from ..store import Store
from .base import Bar, Chain, EcoEvent, Financials, Hub, MarketRow, OptionQuote, Profile
from .live import SPANS


def _rng(*parts) -> random.Random:
    return random.Random("|".join(map(str, parts)))


class MockHub(Hub):
    name = "mock"

    def __init__(self, store: Store):
        self.store = store

    def _spot(self, symbol: str) -> float:
        q = self.store.quotes.get(symbol)
        return q.price if q else UNIVERSE.get(symbol, 50 + _rng(symbol).random() * 150)

    async def bars(self, symbol: str, span: str = "6M"):
        tf, days, keep = SPANS.get(span, SPANS["6M"])
        step = {"5Min": 5, "15Min": 15, "1Hour": 60, "1Day": 1440, "1Week": 10080}[tf]
        now = datetime.now(timezone.utc).replace(second=0, microsecond=0)
        n = {"1D": 78, "5D": 130, "1M": 150}.get(span) or max(30, days * 1440 // step * 5 // 7)
        rng = _rng(symbol, span)
        sigma = 0.02 * math.sqrt(step / 1440 if step < 1440 else step / 1440)
        px = [self._spot(symbol)]
        for _ in range(n - 1):
            px.append(px[-1] / math.exp(rng.gauss(0.0003, sigma)))
        px.reverse()
        bars = []
        t = now - timedelta(minutes=step * n)
        for i, c in enumerate(px):
            o = px[i - 1] if i else c
            h = max(o, c) * (1 + abs(rng.gauss(0, sigma / 2)))
            lo = min(o, c) * (1 - abs(rng.gauss(0, sigma / 2)))
            t += timedelta(minutes=step)
            bars.append(Bar(t, o, h, lo, c, rng.uniform(2e5, 2e6) * (step / 5) ** 0.5))
        return bars, f"MOCK {tf}"

    async def markets(self):
        rng = random.Random()

        def row(name, sym, base, unit="", live=True, src="MOCK"):
            v = base * (1 + rng.gauss(0, 0.004))
            ch = v - base
            return MarketRow(name, sym, v, ch, None if unit == "%" else ch / base * 100,
                             datetime.now(timezone.utc) if live else date.today(), src, live, unit)

        return {
            "INDICES": [row("S&P 500 (ETF)", "SPY", 590), row("Nasdaq 100 (ETF)", "QQQ", 510),
                        row("Dow 30 (ETF)", "DIA", 440), row("Russell 2000 (ETF)", "IWM", 230)],
            "RATES": [row("Fed funds (eff)", "DFF", 4.58, "%", False), row("2Y Treasury", "DGS2", 4.21, "%", False),
                      row("10Y Treasury", "DGS10", 4.35, "%", False), row("30Y Treasury", "DGS30", 4.55, "%", False),
                      row("20Y+ Treasury (ETF)", "TLT", 92)],
            "FX": [row("EURUSD", "EURUSD", 1.052, live=False), row("USDJPY", "USDJPY", 154.3, live=False),
                   row("GBPUSD", "GBPUSD", 1.257, live=False), row("USDCAD", "USDCAD", 1.397, live=False)],
            "COMMODITIES": [row("WTI crude $/bbl", "DCOILWTICO", 71.2, "$", False), row("Gold (ETF)", "GLD", 247),
                            row("Silver (ETF)", "SLV", 28.6), row("Nat gas (ETF)", "UNG", 14.1)],
            "CRYPTO": [row("BTC", "BTC/USD", 97000), row("ETH", "ETH/USD", 3350), row("SOL", "SOL/USD", 255)],
            "_errors": [],
        }

    async def eco(self, days: int = 14, symbols=None):
        t = date.today()
        names = [("Unemployment Insurance Weekly Claims Report", True), ("Consumer Price Index", True),
                 ("Employment Situation", True), ("Gross Domestic Product", True), ("Personal Income and Outlays", True),
                 ("FOMC Press Release", True), ("Industrial Production and Capacity Utilization", True),
                 ("Advance Monthly Sales for Retail and Food Services", True), ("Weekly Petroleum Status", False),
                 ("H.8 Assets and Liabilities of Commercial Banks", False)]
        rng = _rng("eco", t)
        evs = [EcoEvent(t + timedelta(days=rng.randint(0, days)), n, "fomc" if "FOMC" in n else "release", imp, "", "MOCK")
               for n, imp in names]
        for s in (symbols or [])[:6]:
            evs.append(EcoEvent(t + timedelta(days=rng.randint(1, days)), f"{s} earnings after close", "earnings", True,
                                f"EPS est {rng.uniform(0.2, 4):.2f}", "MOCK"))
        return sorted(evs, key=lambda e: (e.when, e.name))

    async def profile(self, symbol: str):
        rng = _rng("p", symbol)
        sh = rng.uniform(2e8, 1.5e10)
        return Profile(symbol, f"{symbol} Holdings Inc (mock)", "NASDAQ", rng.choice(["Semiconductors", "Software", "Banks", "Energy", "Retail"]),
                       "Somewhere, CA", sh * self._spot(symbol), sh, "1999-05-12", "https://example.com", "0000000000",
                       "Mock industry", "12/31",
                       [{"form": f, "date": (date.today() - timedelta(days=i * 17)).isoformat(), "url": "https://example.com"}
                        for i, f in enumerate(["8-K", "4", "10-Q", "4", "8-K", "10-K"])], ["MOCK"])

    async def financials(self, symbol: str, period: str = "A"):
        rng = _rng("f", symbol, period)
        n = 5 if period == "A" else 8
        rev = rng.uniform(5e9, 4e11) / (4 if period == "Q" else 1)
        g, gm, om = rng.uniform(-0.05, 0.25), rng.uniform(0.3, 0.75), rng.uniform(0.05, 0.4)
        ends, revs = [], []
        today = date.today()
        for i in range(n):
            ends.insert(0, (today - timedelta(days=(365 if period == "A" else 91) * (i + 1))).isoformat())
            revs.insert(0, rev / (1 + g) ** i)
        gp = [r * gm for r in revs]
        oi = [r * om for r in revs]
        ni = [o * 0.8 for o in oi]
        ocf = [x * 1.2 for x in ni]
        capex = [r * 0.06 for r in revs]
        rows = [("Revenue", revs, "usd"), ("Gross profit", gp, "usd"), ("Operating income", oi, "usd"),
                ("Net income", ni, "usd"), ("EPS diluted", [x / 2e9 for x in ni], "eps"),
                ("Operating cash flow", ocf, "usd"), ("Capex", capex, "usd"),
                ("Free cash flow", [a - b for a, b in zip(ocf, capex)], "usd"),
                ("Gross margin", [gm * 100] * n, "pct"), ("Operating margin", [om * 100] * n, "pct"),
                ("Net margin", [om * 80] * n, "pct"),
                ("Revenue growth", [None] + [g * 100] * (n - 1), "pct")]
        return Financials(symbol, period, ends, rows, "MOCK")

    async def analysts(self, symbol: str):
        rng = _rng("a", symbol)
        recs, earn = [], []
        t = date.today().replace(day=1)
        for i in range(4):
            m = (t - timedelta(days=30 * i)).replace(day=1)
            recs.append({"period": m.isoformat(), "strongBuy": rng.randint(3, 15), "buy": rng.randint(5, 25),
                         "hold": rng.randint(2, 15), "sell": rng.randint(0, 4), "strongSell": rng.randint(0, 2)})
        for i in range(4):
            est = rng.uniform(0.3, 3)
            act = est * (1 + rng.gauss(0.03, 0.06))
            q = date.today() - timedelta(days=91 * (i + 1))
            earn.append({"period": q.isoformat(), "estimate": est, "actual": act,
                         "surprisePercent": (act / est - 1) * 100})
        return {"recs": recs, "earnings": earn,
                "metric": {"peTTM": rng.uniform(10, 60), "52WeekHigh": self._spot(symbol) * 1.2,
                           "52WeekLow": self._spot(symbol) * 0.7, "beta": rng.uniform(0.6, 1.8),
                           "dividendYieldIndicatedAnnual": rng.uniform(0, 3)},
                "source": "MOCK"}

    async def insiders(self, symbol: str):
        rng = _rng("i", symbol)
        rows = []
        for i in range(10):
            d = (date.today() - timedelta(days=rng.randint(1, 300))).isoformat()
            chg = rng.choice([-1, -1, 1]) * rng.randint(500, 80000)
            rows.append({"name": rng.choice(["Smith John", "Lee Anna", "Patel Ravi", "Garcia Maria"]),
                         "filingDate": d, "transactionDate": d, "transactionCode": "S" if chg < 0 else "P",
                         "change": chg, "share": rng.randint(100_000, 5_000_000), "transactionPrice": self._spot(symbol) * rng.uniform(0.8, 1.1)})
        return {"rows": sorted(rows, key=lambda r: r["filingDate"], reverse=True), "source": "MOCK",
                "note": "Institutional 13F holdings need a paid data plan and aren't included."}

    async def chain(self, symbol: str, expiry: date | None = None):
        spot = self._spot(symbol)
        today = date.today()
        fridays = [today + timedelta(days=(4 - today.weekday()) % 7 + 7 * i) for i in range(8)]
        exp = expiry if expiry in fridays else fridays[0]
        t = max((exp - today).days, 0.5) / 365
        step = 1 if spot < 50 else 2.5 if spot < 200 else 5 if spot < 500 else 10
        base = round(spot / step) * step
        calls, puts = {}, {}
        rng = _rng("c", symbol, exp)
        for i in range(-12, 13):
            k = base + i * step
            if k <= 0:
                continue
            iv = 0.35 + 0.0015 * (abs(k - spot) / spot * 100) ** 1.5  # a smile
            for right, book in (("C", calls), ("P", puts)):
                g = black_scholes(spot, k, t, 0.045, iv, right)
                mid = g["price"]
                spr = max(0.01, mid * 0.04)
                book[k] = OptionQuote(occ_symbol(symbol, exp, right, k), k, right, max(mid - spr, 0), mid + spr,
                                      mid, iv, g["delta"], g["gamma"], g["theta"], g["vega"], rng.randint(0, 5000))
        return Chain(symbol, spot, fridays, exp, calls, puts, "MOCK (Black-Scholes)", "Made-up chain, priced with Black-Scholes.")
