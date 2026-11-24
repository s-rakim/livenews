"""Made-up data for every function screen, so --mock shows the whole terminal.
Deterministic per symbol so the same ticker looks the same each time."""
from __future__ import annotations

import math
import random
from datetime import date, datetime, timedelta, timezone

from ..options_math import black_scholes, occ_symbol, parse_occ
from ..sources.mock import UNIVERSE
from ..store import Store
from .base import Bar, Chain, EcoEvent, Financials, Hub, MarketRow, OptionQuote, Profile, Unavailable, usual_time
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

        def live_row(name, sym):
            q = self.store.quotes.get(sym)
            if not q:
                return row(name, sym, UNIVERSE.get(sym, 100))
            ch = q.price - q.prev_close
            return MarketRow(name, sym, q.price, ch, q.pct, q.updated_at, "MOCK", True, "")

        return {
            "INDICES": [row("S&P 500", "^GSPC", 5950), row("Nasdaq Comp", "^IXIC", 19200), row("Dow 30", "^DJI", 44100),
                        row("Russell 2000", "^RUT", 2350), row("VIX", "^VIX", 15.8), row("FTSE 100", "^FTSE", 8250),
                        row("DAX", "^GDAXI", 19300), row("Nikkei 225", "^N225", 38400),
                        live_row("S&P 500 (ETF)", "SPY"), live_row("Nasdaq 100 (ETF)", "QQQ")],
            "FUTURES": [row("S&P 500 e-mini", "ES=F", 5975), row("Nasdaq 100 e-mini", "NQ=F", 20900),
                        row("Dow e-mini", "YM=F", 44300), row("Russell e-mini", "RTY=F", 2360),
                        row("10Y T-note", "ZN=F", 109.6), row("30Y T-bond", "ZB=F", 116.2), row("Dollar index", "DX-Y.NYB", 106.4)],
            "RATES": [row("13W T-bill yield", "^IRX", 4.31, "%"), row("10Y yield", "^TNX", 4.35, "%"),
                      row("Fed funds (eff)", "FRED:DFF", 4.58, "%", False), row("2Y Treasury", "FRED:DGS2", 4.21, "%", False),
                      row("10Y Treasury", "FRED:DGS10", 4.35, "%", False), row("30Y Treasury", "FRED:DGS30", 4.55, "%", False)],
            "FX": [row("EURUSD", "EURUSD=X", 1.052), row("GBPUSD", "GBPUSD=X", 1.257), row("USDJPY", "JPY=X", 154.3),
                   row("USDCHF", "CHF=X", 0.884), row("USDCAD", "CAD=X", 1.397), row("AUDUSD", "AUDUSD=X", 0.651)],
            "COMMODITIES": [row("WTI crude", "CL=F", 71.2), row("Brent crude", "BZ=F", 74.9), row("Natural gas", "NG=F", 3.21),
                            row("Gold", "GC=F", 2650), row("Silver", "SI=F", 30.6), row("Copper", "HG=F", 4.12),
                            row("Corn", "ZC=F", 4.28)],
            "CRYPTO": [live_row("BTC", "BTC/USD"), live_row("ETH", "ETH/USD"), live_row("SOL", "SOL/USD")],
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
        evs = [EcoEvent(t + timedelta(days=rng.randint(0, days)), n, "fomc" if "FOMC" in n else "release", imp, "", "MOCK",
                        usual_time(n))
               for n, imp in names]
        for s in (symbols or [])[:6]:
            evs.append(EcoEvent(t + timedelta(days=rng.randint(1, days)), f"{s} earnings", "earnings", True,
                                f"EPS est {rng.uniform(0.2, 4):.2f}", "MOCK", rng.choice(["pre-mkt", "post-mkt"])))
        return sorted(evs, key=lambda e: (e.when, e.time or "99", e.name))

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
        spot = self._spot(symbol)
        mean = spot * rng.uniform(0.95, 1.3)
        target = {"high": mean * rng.uniform(1.15, 1.5), "low": mean * rng.uniform(0.6, 0.85), "mean": mean,
                  "median": mean * rng.uniform(0.97, 1.03), "updated": date.today().isoformat(), "source": "MOCK"}
        return {"recs": recs, "earnings": earn, "target": target,
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
                "note": "Institutional holders are on the OWN screen."}

    async def holders(self, symbol: str):
        rng = _rng("h", symbol)
        names = ["Vanguard Group Inc", "BlackRock Inc", "State Street Corp", "Fidelity (FMR LLC)", "Geode Capital Management",
                 "Morgan Stanley", "JPMorgan Chase & Co", "T. Rowe Price Associates", "Northern Trust Corp", "Bank of America Corp",
                 "Capital World Investors", "Norges Bank", "Goldman Sachs Group", "Wellington Management", "UBS Group AG"]
        px = self._spot(symbol)
        shares_out = rng.uniform(5e8, 1e10)
        rows = []
        left = shares_out * 0.7
        for n in names:
            sh = left * rng.uniform(0.05, 0.2)
            left -= sh
            rows.append({"manager": n, "shares": sh, "value": sh * px, "period": "30-SEP-2026", "filed": "14-NOV-2026"})
        rows.sort(key=lambda r: -r["value"])
        tot = sum(r["shares"] for r in rows) * 1.6
        return {"rows": rows, "count": rng.randint(800, 4000), "total_value": tot * px, "total_shares": tot,
                "shares_out": shares_out, "period": "30-SEP-2026", "cusip": "000000000", "issuer": f"{symbol} HOLDINGS INC",
                "source": "MOCK"}

    async def chain(self, symbol: str, expiry: date | None = None):
        spot = self._spot(symbol)
        today = date.today()
        fridays = [today + timedelta(days=(4 - today.weekday()) % 7 + 7 * i) for i in range(8)]
        exp = expiry if expiry in fridays else fridays[0]
        step = 1 if spot < 50 else 2.5 if spot < 200 else 5 if spot < 500 else 10
        base = round(spot / step) * step
        calls, puts = {}, {}
        rng = _rng("c", symbol, exp)
        for i in range(-12, 13):
            k = base + i * step
            if k <= 0:
                continue
            for right, book in (("C", calls), ("P", puts)):
                book[k] = self._price_option(occ_symbol(symbol, exp, right, k), rng.randint(0, 5000))
        return Chain(symbol, spot, fridays, exp, calls, puts, "MOCK (Black-Scholes)", "Made-up chain, priced with Black-Scholes.")

    def _price_option(self, occ: str, volume: float = 0) -> OptionQuote:
        c = parse_occ(occ)
        spot = self._spot(c.root)
        t = max((c.expiry - date.today()).days, 0.5) / 365
        iv = 0.35 + 0.0015 * (abs(c.strike - spot) / spot * 100) ** 1.5  # a smile
        g = black_scholes(spot, c.strike, t, 0.045, iv, c.right)
        mid = g["price"]
        spr = max(0.01, mid * 0.04)
        return OptionQuote(occ, c.strike, c.right, max(mid - spr, 0), mid + spr, mid, iv,
                           g["delta"], g["gamma"], g["theta"], g["vega"], volume)

    def option_mid(self, occ: str) -> float | None:
        if not parse_occ(occ):
            return None
        return self._price_option(occ).last

    async def option_quote(self, occ: str) -> OptionQuote:
        if not parse_occ(occ):
            raise Unavailable(f"{occ} isn't an OCC option symbol")
        return self._price_option(occ)
