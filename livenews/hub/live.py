"""Real data for the function screens.

  Alpaca   bars, crypto, ETF proxies, options chains   (ALPACA keys, free)
  FRED     Treasury yields, Fed funds, SOFR, oil, gas, release calendar
           (FRED_API_KEY, free, 120 req/min). Daily data, not intraday.
  Frankfurter  FX reference rates from the ECB (no key). One fix per day.
  SEC EDGAR    financial statements and filings (no key, needs User-Agent)
  Finnhub  profile, analyst ratings, earnings surprises, insider trades,
           earnings calendar (FINNHUB_API_KEY, free, 60 req/min)
"""
from __future__ import annotations

import asyncio
import os
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import httpx

from ..config import Settings
from ..options_math import parse_occ
from ..sources.alpaca import AlpacaData
from ..timeparse import parse_ts
from . import sec
from .delayed import COMMODITY_FUT, FUTURES, FX, INDICES, YIELDS, DelayedQuotes, is_yahoo_symbol
from .base import (Bar, Chain, EcoEvent, Financials, Hub, MarketRow, OptionQuote, Profile, TTLCache, Unavailable,
                   usual_time)

SPANS = {
    # span: (timeframe, lookback days, keep last N sessions or None)
    "1D": ("5Min", 5, 1),
    "5D": ("15Min", 9, 5),
    "1M": ("1Hour", 31, None),
    "3M": ("1Day", 92, None),
    "6M": ("1Day", 183, None),
    "1Y": ("1Day", 366, None),
    "5Y": ("1Week", 5 * 366, None),
}

ETF_PROXIES = {
    "INDICES": [("S&P 500", "SPY"), ("Nasdaq 100", "QQQ"), ("Dow 30", "DIA"), ("Russell 2000", "IWM"),
                ("VIX futures", "VIXY")],
    "RATES": [("20Y+ Treasury", "TLT"), ("7-10Y Treasury", "IEF"), ("1-3Y Treasury", "SHY"), ("High yield", "HYG")],
    "COMMODITIES": [("Gold", "GLD"), ("Silver", "SLV"), ("WTI oil", "USO"), ("Nat gas", "UNG"),
                    ("Copper", "CPER"), ("Agriculture", "DBA")],
    "FX": [("US dollar index", "UUP"), ("Euro", "FXE"), ("Yen", "FXY")],
}
FRED_RATES = [("Fed funds (eff)", "DFF"), ("SOFR", "SOFR"), ("3M T-bill", "DGS3MO"), ("2Y Treasury", "DGS2"),
              ("5Y Treasury", "DGS5"), ("10Y Treasury", "DGS10"), ("30Y Treasury", "DGS30"), ("10Y-2Y spread", "T10Y2Y")]
FRED_CMDTY = [("WTI crude $/bbl", "DCOILWTICO"), ("Brent crude $/bbl", "DCOILBRENTEU"), ("Henry Hub gas $/MMBtu", "DHHNGSP")]
CRYPTO = ["BTC/USD", "ETH/USD", "SOL/USD", "XRP/USD", "DOGE/USD", "LTC/USD", "AVAX/USD", "LINK/USD"]
# pair label, currency, inverted (True = quote as XXXUSD)
FX_PAIRS = [("EURUSD", "EUR", True), ("GBPUSD", "GBP", True), ("USDJPY", "JPY", False), ("USDCHF", "CHF", False),
            ("USDCAD", "CAD", False), ("AUDUSD", "AUD", True), ("USDCNY", "CNY", False), ("USDMXN", "MXN", False)]

IMPORTANT_RELEASES = (
    "Consumer Price Index", "Employment Situation", "Gross Domestic Product", "Personal Income and Outlays",
    "Producer Price Index", "FOMC", "Advance Monthly Sales", "Unemployment Insurance Weekly Claims",
    "Job Openings", "Industrial Production", "New Residential Construction", "Existing Home Sales",
    "Surveys of Consumers", "Consumer Credit", "Beige Book",
)


def _num(x) -> float | None:
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def _option_quote(occ: str, c, sn: dict) -> OptionQuote:
    g = sn.get("greeks") or {}
    lq, lt = sn.get("latestQuote") or {}, sn.get("latestTrade") or {}
    return OptionQuote(occ, c.strike, c.right, _num(lq.get("bp")), _num(lq.get("ap")), _num(lt.get("p")),
                       _num(sn.get("impliedVolatility")), _num(g.get("delta")), _num(g.get("gamma")),
                       _num(g.get("theta")), _num(g.get("vega")), _num((sn.get("dailyBar") or {}).get("v")))


class LiveHub(Hub):
    name = "live"

    def __init__(self, settings: Settings, data: AlpacaData):
        self.s = settings
        self.data = data  # shares the Alpaca client and its rate limiter
        self.cache = TTLCache()
        self.fred_key = os.getenv("FRED_API_KEY", "").strip()
        self.sec_ua = os.getenv("LIVENEWS_SEC_USER_AGENT", "").strip()
        self.http = httpx.AsyncClient(timeout=15.0, follow_redirects=True)
        self._sec_lock = asyncio.Semaphore(4)
        self._13f_lock = asyncio.Lock()
        self.store = data.store
        self.delayed = DelayedQuotes() if os.getenv("LIVENEWS_DELAYED", "on").lower() != "off" else None

    async def aclose(self) -> None:
        await self.http.aclose()
        if self.delayed:
            await self.delayed.aclose()

    # alpaca -----------------------------------------------------------------
    async def bars(self, symbol: str, span: str = "6M") -> tuple[list[Bar], str]:
        span = span if span in SPANS else "6M"
        key = ("bars", symbol, span)
        hit = self.cache.get(key)
        if hit:
            return hit
        if symbol.startswith("FRED:"):
            sid = symbol.split(":", 1)[1]
            n = {"1D": 5, "5D": 10, "1M": 25, "3M": 65, "6M": 130, "1Y": 260, "5Y": 1300}.get(span, 260)
            obs = await self._fred_obs(sid, limit=n)
            bars = [Bar(datetime(d.year, d.month, d.day, tzinfo=timezone.utc), v, v, v, v, 0.0) for d, v in reversed(obs)]
            return self.cache.put(key, (bars, f"FRED {sid} daily"), 3600)
        if is_yahoo_symbol(symbol):
            if not self.delayed:
                raise Unavailable("delayed quotes are switched off (LIVENEWS_DELAYED=off)")
            raw = await self.delayed.bars(symbol, span)
            bars = [Bar(*b) for b in raw]
            if span in ("1D", "5D") and bars:
                keep_n = 1 if span == "1D" else 5
                days_seen = sorted({b.t.astimezone().date() for b in bars})[-keep_n:]
                bars = [b for b in bars if b.t.astimezone().date() in days_seen]
            return self.cache.put(key, (bars, "Yahoo delayed (unofficial, personal use)"), 30)
        tf, days, keep = SPANS[span]
        start = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")
        crypto = "/" in symbol
        out: list[Bar] = []
        token = None
        for _ in range(5):
            if crypto:
                params = {"symbols": symbol, "timeframe": tf, "start": start, "limit": 10000}
                path = "/v1beta3/crypto/us/bars"
            else:
                params = {"timeframe": tf, "start": start, "limit": 10000, "feed": self.s.alpaca_feed,
                          "adjustment": "all"}
                path = f"/v2/stocks/{symbol}/bars"
            if token:
                params["page_token"] = token
            js, _ = await self.data._get(path, params)
            raw = (js.get("bars") or {}).get(symbol, []) if crypto else (js.get("bars") or [])
            for b in raw:
                t = parse_ts(b.get("t"))
                if t:
                    out.append(Bar(t, float(b["o"]), float(b["h"]), float(b["l"]), float(b["c"]), float(b.get("v") or 0)))
            token = js.get("next_page_token")
            if not token:
                break
        if keep and out:
            days_seen = sorted({b.t.astimezone().date() for b in out})[-keep:]
            out = [b for b in out if b.t.astimezone().date() in days_seen]
        src = f"Alpaca {'crypto' if crypto else self.s.alpaca_feed.upper()} {tf}"
        return self.cache.put(key, (out, src), 30 if keep else 300)

    async def _stock_rows(self, items) -> list[MarketRow]:
        syms = [s for _, s in items]
        js, _ = await self.data._get("/v2/stocks/snapshots", {"symbols": ",".join(syms), "feed": self.s.alpaca_feed})
        snaps = js.get("snapshots", js)
        rows = []
        for name, s in items:
            sn = snaps.get(s) or {}
            px = _num((sn.get("latestTrade") or {}).get("p")) or _num((sn.get("dailyBar") or {}).get("c"))
            prev = _num((sn.get("prevDailyBar") or {}).get("c"))
            ch = px - prev if px is not None and prev else None
            rows.append(MarketRow(f"{name} (ETF)", s, px, ch, ch / prev * 100 if ch is not None else None,
                                  parse_ts((sn.get("latestTrade") or {}).get("t")), f"Alpaca {self.s.alpaca_feed.upper()}", True))
        return rows

    async def _crypto_rows(self) -> list[MarketRow]:
        js, _ = await self.data._get("/v1beta3/crypto/us/snapshots", {"symbols": ",".join(CRYPTO)})
        snaps = js.get("snapshots", {})
        rows = []
        for s in CRYPTO:
            sn = snaps.get(s) or {}
            px = _num((sn.get("latestTrade") or {}).get("p"))
            prev = _num((sn.get("prevDailyBar") or {}).get("c"))
            ch = px - prev if px is not None and prev else None
            rows.append(MarketRow(s.split("/")[0], s, px, ch, ch / prev * 100 if ch is not None else None,
                                  parse_ts((sn.get("latestTrade") or {}).get("t")), "Alpaca crypto", True))
        return rows

    # fred -------------------------------------------------------------------
    async def _fred_obs(self, series: str, limit: int = 10) -> list[tuple[date, float]]:
        if not self.fred_key:
            raise Unavailable("set FRED_API_KEY (free at fred.stlouisfed.org) for rates, oil and the calendar")
        key = ("fred", series, limit)
        hit = self.cache.get(key)
        if hit is not None:
            return hit
        r = await self.http.get("https://api.stlouisfed.org/fred/series/observations", params={
            "series_id": series, "api_key": self.fred_key, "file_type": "json", "sort_order": "desc", "limit": limit})
        if r.status_code != 200:
            raise Unavailable(f"FRED {series}: HTTP {r.status_code}")
        obs = [(date.fromisoformat(o["date"]), float(o["value"])) for o in r.json().get("observations", [])
               if o.get("value") not in (".", None, "")]
        return self.cache.put(key, obs, 3600)

    async def _fred_rows(self, items, unit: str) -> list[MarketRow]:
        async def one(name, sid):
            try:
                obs = await self._fred_obs(sid)
            except Unavailable:
                raise
            except Exception as e:
                return MarketRow(name, sid, None, None, None, None, f"FRED err {str(e)[:30]}", False, unit)
            if not obs:
                return MarketRow(name, sid, None, None, None, None, "FRED", False, unit)
            (d0, v0), prev = obs[0], (obs[1][1] if len(obs) > 1 else None)
            ch = v0 - prev if prev is not None else None
            pct = None if unit == "%" or not prev else ch / prev * 100
            return MarketRow(name, sid, v0, ch, pct, d0, "FRED daily", False, unit)
        return list(await asyncio.gather(*(one(n, s) for n, s in items)))

    async def _fx_rows(self) -> list[MarketRow]:
        hit = self.cache.get("fx")
        if hit:
            return hit
        start = (date.today() - timedelta(days=10)).isoformat()
        to = ",".join(c for _, c, _ in FX_PAIRS)
        r = await self.http.get(f"https://api.frankfurter.app/{start}..", params={"from": "USD", "to": to})
        r.raise_for_status()
        rates = r.json().get("rates", {})
        days = sorted(rates)
        rows = []
        if not days:
            return rows
        last, prev = rates[days[-1]], rates[days[-2]] if len(days) > 1 else {}
        for label, cur, inv in FX_PAIRS:
            v, p = last.get(cur), prev.get(cur)
            if v is None:
                continue
            v = 1 / v if inv else v
            p = (1 / p if inv else p) if p else None
            ch = v - p if p else None
            rows.append(MarketRow(label, label, v, ch, ch / p * 100 if ch is not None else None,
                                  date.fromisoformat(days[-1]), "ECB ref (Frankfurter)", False))
        return self.cache.put("fx", rows, 1800)

    def _fx_rows_stream(self, delayed_rows: list[MarketRow]) -> list[MarketRow]:
        """Prefer the streaming OANDA price when it's fresh, keep Yahoo's previous
        close for the change column."""
        now = datetime.now(timezone.utc)
        out = []
        for r in delayed_rows:
            live = self.store.fx.get(r.name)
            if live and (now - live[1]).total_seconds() < 120:
                px, ts = live
                prev = r.value - r.change if r.value is not None and r.change is not None else None
                ch = px - prev if prev else None
                out.append(MarketRow(r.name, r.symbol, px, ch, ch / prev * 100 if ch is not None else None, ts,
                                     "Finnhub/OANDA stream", True))
            else:
                out.append(r)
        return out

    async def markets(self) -> dict[str, list[MarketRow]]:
        errs = []

        async def guard(name, coro):
            try:
                return name, await coro
            except Unavailable as e:
                errs.append(str(e))
            except Exception as e:
                errs.append(f"{name}: {str(e)[:60]}")
            return name, []

        async def nothing():
            return []

        d = self.delayed
        etfs = ETF_PROXIES["INDICES"][:4]
        jobs = [
            guard("etf", self._stock_rows(etfs)),
            guard("crypto", self._crypto_rows()),
            guard("rates", self._fred_rows(FRED_RATES, "%")),
            guard("cmdty", self._fred_rows(FRED_CMDTY, "$")),
            guard("ecb", self._fx_rows()),
            guard("idx", d.rows(INDICES) if d else nothing()),
            guard("fut", d.rows(FUTURES) if d else nothing()),
            guard("yld", d.rows(YIELDS, "%") if d else nothing()),
            guard("cfut", d.rows(COMMODITY_FUT) if d else nothing()),
            guard("fx", d.rows(FX) if d else nothing()),
        ]
        res = dict(await asyncio.gather(*jobs))
        fx = self._fx_rows_stream(res["fx"]) if res["fx"] else []
        # ECB fixings fill any pair the delayed feed couldn't give us
        have = {r.name for r in fx if r.value is not None}
        fx = [r for r in fx if r.value is not None] + [r for r in res["ecb"] if r.name not in have]
        metals = []
        now = datetime.now(timezone.utc)
        for label, name in (("XAUUSD", "Gold spot"), ("XAGUSD", "Silver spot")):
            live = self.store.fx.get(label)
            if live and (now - live[1]).total_seconds() < 120:
                metals.append(MarketRow(name, label, live[0], None, None, live[1], "Finnhub/OANDA stream", True))
        out: dict[str, list[MarketRow]] = {
            "INDICES": res["idx"] + res["etf"],
            "FUTURES": res["fut"],
            "RATES": res["yld"] + res["rates"],
            "FX": fx,
            "COMMODITIES": metals + res["cfut"] + res["cmdty"],
            "CRYPTO": res["crypto"],
        }
        out["_errors"] = sorted(set(errs))  # type: ignore[assignment]
        return out

    async def eco(self, days: int = 14, symbols: list[str] | None = None) -> list[EcoEvent]:
        evs: list[EcoEvent] = []
        notes = []
        today = date.today()
        end = today + timedelta(days=days)
        if self.fred_key:
            hit = self.cache.get(("eco", days))
            if hit is None:
                r = await self.http.get("https://api.stlouisfed.org/fred/releases/dates", params={
                    "api_key": self.fred_key, "file_type": "json", "realtime_start": today.isoformat(),
                    "realtime_end": end.isoformat(), "include_release_dates_with_no_data": "true",
                    "order_by": "release_date", "sort_order": "asc", "limit": 1000})
                r.raise_for_status()
                hit = self.cache.put(("eco", days), r.json().get("release_dates", []), 3600)
            for d in hit:
                name = d.get("release_name", "")
                when = date.fromisoformat(d["date"])
                if today <= when <= end:
                    imp = any(k.lower() in name.lower() for k in IMPORTANT_RELEASES)
                    evs.append(EcoEvent(when, name, "fomc" if "FOMC" in name else "release", imp, "", "FRED",
                                        usual_time(name)))
        else:
            notes.append("FRED_API_KEY")
        if self.s.finnhub_key:
            try:
                js = await self._finnhub("/calendar/earnings", {"from": today.isoformat(), "to": end.isoformat()}, ttl=3600)
                want = set(symbols or [])
                for e in js.get("earningsCalendar", []) or []:
                    sym = e.get("symbol", "")
                    if want and sym not in want:
                        continue
                    est = e.get("epsEstimate")
                    hour = {"bmo": "pre-mkt", "amc": "post-mkt", "dmh": "in mkt"}.get(e.get("hour", ""), "")
                    detail = f"EPS est {est:.2f}" if isinstance(est, (int, float)) else ""
                    evs.append(EcoEvent(date.fromisoformat(e["date"]), f"{sym} earnings", "earnings",
                                        True, detail, "Finnhub", hour))
            except Unavailable:
                pass
        else:
            notes.append("FINNHUB_API_KEY")
        if not evs and notes:
            raise Unavailable("economic calendar needs " + " and/or ".join(notes))
        return sorted(evs, key=lambda e: (e.when, e.time or "99", not e.important, e.name))

    # finnhub ------------------------------------------------------------------
    async def _finnhub(self, path: str, params: dict, ttl: float = 600):
        if not self.s.finnhub_key:
            raise Unavailable("set FINNHUB_API_KEY (free at finnhub.io)")
        key = ("fh", path, tuple(sorted(params.items())))
        hit = self.cache.get(key)
        if hit is not None:
            return hit
        r = await self.http.get(f"https://finnhub.io/api/v1{path}", params=params,
                                headers={"X-Finnhub-Token": self.s.finnhub_key})
        if r.status_code in (401, 403):
            raise Unavailable(f"Finnhub {path}: not on your plan ({r.status_code})")
        if r.status_code == 429:
            raise Unavailable("Finnhub rate limit hit, try again in a minute")
        r.raise_for_status()
        return self.cache.put(key, r.json(), ttl)

    # sec -----------------------------------------------------------------------
    async def _sec(self, url: str, ttl: float = 6 * 3600):
        if not self.sec_ua:
            raise Unavailable('set LIVENEWS_SEC_USER_AGENT="Your Name you@example.com" (SEC requires it, free)')
        hit = self.cache.get(("sec", url))
        if hit is not None:
            return hit
        async with self._sec_lock:
            r = await self.http.get(url, headers={"User-Agent": self.sec_ua, "Accept-Encoding": "gzip, deflate"})
        if r.status_code == 404:
            raise Unavailable("SEC has no XBRL data for this company (foreign filer, fund or ETF?)")
        r.raise_for_status()
        return self.cache.put(("sec", url), r.json(), ttl)

    async def cik(self, symbol: str) -> str:
        m = await self._sec("https://www.sec.gov/files/company_tickers.json", ttl=86400)
        sym = symbol.upper().replace(".", "-")
        for row in m.values():
            if row.get("ticker", "").upper() == sym:
                return f"{int(row['cik_str']):010d}"
        raise Unavailable(f"{symbol} isn't in the SEC ticker list (not a US filer?)")

    async def profile(self, symbol: str) -> Profile:
        p = Profile(symbol)
        errs = []
        try:
            c = await self.cik(symbol)
            sub = await self._sec(f"https://data.sec.gov/submissions/CIK{c}.json")
            p.cik, p.name = c, sub.get("name", "")
            p.sic = sub.get("sicDescription", "")
            p.exchange = ",".join(sub.get("exchanges") or [])
            fye = sub.get("fiscalYearEnd") or ""
            p.fiscal_year_end = f"{fye[:2]}/{fye[2:]}" if len(fye) == 4 else fye
            addr = (sub.get("addresses") or {}).get("business") or {}
            p.country = ", ".join(x for x in (addr.get("city"), addr.get("stateOrCountryDescription")) if x)
            p.web = sub.get("website") or ""
            p.filings = sec.recent_filings(sub, c)
            p.sources.append("SEC EDGAR")
        except Unavailable as e:
            errs.append(str(e))
        try:
            fp = await self._finnhub("/stock/profile2", {"symbol": symbol}, ttl=86400)
            if fp:
                p.name = p.name or fp.get("name", "")
                p.industry = fp.get("finnhubIndustry", "")
                p.exchange = fp.get("exchange") or p.exchange
                mc = fp.get("marketCapitalization")
                p.market_cap = mc * 1e6 if mc else None
                so = fp.get("shareOutstanding")
                p.shares = so * 1e6 if so else None
                p.ipo = fp.get("ipo", "")
                p.web = fp.get("weburl") or p.web
                p.sources.append("Finnhub")
        except Unavailable as e:
            errs.append(str(e))
        if not p.sources:
            raise Unavailable("; ".join(errs))
        return p

    async def financials(self, symbol: str, period: str = "A") -> Financials:
        c = await self.cik(symbol)
        facts = await self._sec(f"https://data.sec.gov/api/xbrl/companyfacts/CIK{c}.json")
        ends, rows = sec.extract_financials(facts, period, n=5 if period == "A" else 8)
        if not ends:
            raise Unavailable(f"no {('annual' if period == 'A' else 'quarterly')} XBRL figures found for {symbol}")
        return Financials(symbol, period, ends, rows, "SEC EDGAR XBRL (as filed)")

    async def _sec_title(self, symbol: str) -> str:
        m = await self._sec("https://www.sec.gov/files/company_tickers.json", ttl=86400)
        sym = symbol.upper().replace(".", "-")
        for row in m.values():
            if row.get("ticker", "").upper() == sym:
                return row.get("title", "")
        raise Unavailable(f"{symbol} isn't in the SEC ticker list (not a US filer?)")

    async def _ensure_13f(self) -> Path:
        from ..userdata import home
        from . import thirteenf as tf

        async with self._13f_lock:
            if not self.sec_ua:
                raise Unavailable('set LIVENEWS_SEC_USER_AGENT="Your Name you@example.com" (SEC requires it, free)')
            hit = self.cache.get("13f_db")
            if hit:
                return hit
            folder = home() / "13f"
            folder.mkdir(exist_ok=True)
            r = await self.http.get(tf.INDEX_PAGE, headers={"User-Agent": self.sec_ua})
            r.raise_for_status()
            url = tf.newest_link(r.text)
            if not url:
                existing = sorted(folder.glob("*.db"))
                if existing:
                    return self.cache.put("13f_db", existing[-1], 86400)
                raise Unavailable("couldn't find the 13F data set link on sec.gov")
            name = url.rsplit("/", 1)[1].replace(".zip", "")
            db = folder / f"{name}.db"
            if not db.exists():
                zpath = folder / f"{name}.zip"
                feed = self.store.feed("13F", "SEC 13F data set", "one-time download per quarter")
                if not zpath.exists():
                    part = zpath.with_suffix(".part")
                    got = 0
                    async with self.http.stream("GET", url, headers={"User-Agent": self.sec_ua}, timeout=300) as resp:
                        resp.raise_for_status()
                        total = int(resp.headers.get("content-length") or 0)
                        with part.open("wb") as f:
                            async for chunk in resp.aiter_bytes(1 << 20):
                                f.write(chunk)
                                got += len(chunk)
                                feed.note = f"downloading {got / 1e6:.0f}" + (f"/{total / 1e6:.0f} MB" if total else " MB")
                    part.replace(zpath)
                feed.note = "indexing holdings (about a minute, once per quarter)"
                await asyncio.to_thread(tf.build_index, zpath, db, lambda m: setattr(feed, "note", m))
                zpath.unlink(missing_ok=True)
                for old in folder.glob("*.db"):
                    if old != db:
                        old.unlink(missing_ok=True)
                feed.note = f"ready: {name}"
                self.store.feed_ok("13F", 0.0)
            return self.cache.put("13f_db", db, 86400)

    async def holders(self, symbol: str) -> dict:
        from . import thirteenf as tf

        title = await self._sec_title(symbol)
        db = await self._ensure_13f()
        out = await asyncio.to_thread(tf.holders, db, title)
        if not out["rows"]:
            raise Unavailable(f"no 13F holdings matched '{title}' in {db.stem}")
        try:
            c = await self.cik(symbol)
            facts = await self._sec(f"https://data.sec.gov/api/xbrl/companyfacts/CIK{c}.json")
            out["shares_out"] = sec.shares_outstanding(facts)
        except Exception:
            out["shares_out"] = None
        out["source"] = f"SEC Form 13F data set {db.stem} (original 13F-HR filings, longs only)"
        return out

    async def price_target(self, symbol: str) -> dict | None:
        """Street price target. Finnhub's endpoint is premium, so on a free key it
        403s and we try FMP's consensus endpoint instead. Returns None when
        neither key can see it."""
        tried = []
        if self.s.finnhub_key:
            try:
                js = await self._finnhub("/stock/price-target", {"symbol": symbol}, ttl=3600)
                if isinstance(js, dict) and _num(js.get("targetMean")):
                    return {"high": _num(js.get("targetHigh")), "low": _num(js.get("targetLow")),
                            "mean": _num(js.get("targetMean")), "median": _num(js.get("targetMedian")),
                            "updated": (js.get("lastUpdated") or "")[:10], "source": "Finnhub"}
                tried.append("Finnhub: no target")
            except Unavailable as e:
                tried.append(str(e))
        if self.s.fmp_key:
            key = ("fmp-pt", symbol)
            js = self.cache.get(key)
            if js is None:
                r = await self.http.get("https://financialmodelingprep.com/stable/price-target-consensus",
                                        params={"symbol": symbol, "apikey": self.s.fmp_key})
                if r.status_code in (401, 402, 403):
                    tried.append(f"FMP price targets: not on your plan ({r.status_code})")
                    js = []
                else:
                    r.raise_for_status()
                    js = self.cache.put(key, r.json(), 3600)
            row = js[0] if isinstance(js, list) and js else js if isinstance(js, dict) else {}
            mean = _num(row.get("targetConsensus")) or _num(row.get("targetMean"))
            if mean:
                return {"high": _num(row.get("targetHigh")), "low": _num(row.get("targetLow")), "mean": mean,
                        "median": _num(row.get("targetMedian")), "updated": "", "source": "FMP consensus"}
        if not self.s.finnhub_key and not self.s.fmp_key:
            tried.append("set FINNHUB_API_KEY or FMP_API_KEY")
        return {"missing": "; ".join(tried) or "no price target published"}

    async def analysts(self, symbol: str) -> dict:
        out = {"recs": [], "earnings": [], "metric": {}, "target": None}
        errors = []

        async def part(name, coro, pick=lambda x: x):
            try:
                out[name] = pick(await coro)
            except Unavailable as e:
                errors.append(str(e))

        await part("recs", self._finnhub("/stock/recommendation", {"symbol": symbol}), lambda x: x or [])
        await part("earnings", self._finnhub("/stock/earnings", {"symbol": symbol}), lambda x: x or [])
        await part("metric", self._finnhub("/stock/metric", {"symbol": symbol, "metric": "all"}),
                   lambda x: (x or {}).get("metric", {}))
        out["target"] = await self.price_target(symbol)
        if not out["recs"] and not out["earnings"] and not (out["target"] or {}).get("mean"):
            raise Unavailable("; ".join(dict.fromkeys(errors)) or f"no analyst data for {symbol}")
        src = ["Finnhub ratings + EPS"] if self.s.finnhub_key else []
        if (out["target"] or {}).get("source"):
            src.append(f"targets: {out['target']['source']}")
        out["source"] = ", ".join(src)
        return out

    async def insiders(self, symbol: str) -> dict:
        start = (date.today() - timedelta(days=365)).isoformat()
        js = await self._finnhub("/stock/insider-transactions", {"symbol": symbol, "from": start})
        rows = js.get("data", []) if isinstance(js, dict) else []
        return {"rows": sorted(rows, key=lambda r: r.get("filingDate", ""), reverse=True), "source": "Finnhub (SEC Form 4)",
                "note": "Institutional holders are on the OWN screen."}

    # options ------------------------------------------------------------------
    async def chain(self, symbol: str, expiry: date | None = None) -> Chain:
        q = self.data.store.quotes.get(symbol)
        spot = q.price if q else None
        if spot is None:
            js, _ = await self.data._get("/v2/stocks/snapshots", {"symbols": symbol, "feed": self.s.alpaca_feed})
            sn = js.get("snapshots", js).get(symbol) or {}
            spot = _num((sn.get("latestTrade") or {}).get("p"))
        if not spot:
            raise Unavailable(f"no price for {symbol}")
        today = date.today()
        params = {"feed": "indicative", "limit": 1000,
                  "strike_price_gte": f"{spot * 0.8:.2f}", "strike_price_lte": f"{spot * 1.2:.2f}"}
        if expiry:
            params["expiration_date"] = expiry.isoformat()
        else:
            params["expiration_date_gte"] = today.isoformat()
            params["expiration_date_lte"] = (today + timedelta(days=60)).isoformat()
        key = ("chain", symbol, expiry)
        snaps = self.cache.get(key)
        if snaps is None:
            snaps, token = {}, None
            for _ in range(6):
                if token:
                    params["page_token"] = token
                try:
                    js, _ = await self.data._get(f"/v1beta1/options/snapshots/{symbol}", params)
                except httpx.HTTPStatusError as e:
                    raise Unavailable(f"Alpaca options: HTTP {e.response.status_code} (options data may need to be enabled on your account)")
                snaps.update(js.get("snapshots") or {})
                token = js.get("next_page_token")
                if not token:
                    break
            self.cache.put(key, snaps, 15)
        by_exp: dict[date, dict[str, dict[float, OptionQuote]]] = {}
        for occ, sn in snaps.items():
            c = parse_occ(occ)
            if not c:
                continue
            oq = _option_quote(occ, c, sn)
            side = by_exp.setdefault(c.expiry, {"C": {}, "P": {}})
            side[c.right][c.strike] = oq
        if not by_exp:
            raise Unavailable(f"no option contracts returned for {symbol}")
        known = self.cache.get(("exps", symbol)) or set()
        known |= set(by_exp)
        self.cache.put(("exps", symbol), known, 3600)
        exps = sorted(known)
        pick = expiry if expiry in by_exp else exps[0]
        return Chain(symbol, spot, exps, pick, by_exp[pick]["C"], by_exp[pick]["P"],
                     "Alpaca indicative feed",
                     "Indicative = free feed, quotes are approximate and may be delayed. Greeks/IV from Alpaca.")

    async def option_quote(self, occ: str) -> OptionQuote:
        c = parse_occ(occ)
        if not c:
            raise Unavailable(f"{occ} isn't an OCC option symbol")
        try:
            js, _ = await self.data._get("/v1beta1/options/snapshots", {"symbols": occ, "feed": "indicative"})
        except httpx.HTTPStatusError as e:
            raise Unavailable(f"Alpaca options: HTTP {e.response.status_code}")
        sn = (js.get("snapshots") or {}).get(occ)
        if not sn:
            raise Unavailable(f"no quote for {occ}, check the symbol (OCC format: ROOT YYMMDD C/P strike x1000)")
        return _option_quote(occ, c, sn)
