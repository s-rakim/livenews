"""LiveHub against canned responses shaped like the providers' docs. This
checks our parsing and plumbing, not the providers themselves."""
import asyncio
from datetime import date, timedelta

import httpx
import pytest

from livenews.config import DATA_URL, load_settings
from livenews.hub.base import Unavailable
from livenews.hub.live import LiveHub
from livenews.sources.alpaca import AlpacaData
from livenews.store import Store

EXP = (date.today() + timedelta(days=4)).strftime("%y%m%d")


def alpaca_handler(req: httpx.Request) -> httpx.Response:
    p = req.url.path
    if p == "/v2/stocks/NVDA/bars":
        return httpx.Response(200, json={"bars": [
            {"t": "2026-11-20T05:00:00Z", "o": 140, "h": 145, "l": 139, "c": 144, "v": 1000},
            {"t": "2026-11-21T05:00:00Z", "o": 144, "h": 146, "l": 141, "c": 142, "v": 1200}], "next_page_token": None})
    if p == "/v1beta3/crypto/us/bars":
        return httpx.Response(200, json={"bars": {"BTC/USD": [
            {"t": "2026-11-21T00:00:00Z", "o": 1, "h": 2, "l": 0.5, "c": 1.5, "v": 10}]}})
    if p == "/v2/stocks/snapshots":
        syms = req.url.params["symbols"].split(",")
        return httpx.Response(200, json={s: {"latestTrade": {"p": 100.0, "t": "2026-11-23T20:59:59Z"},
                                             "prevDailyBar": {"c": 99.0}} for s in syms})
    if p == "/v1beta3/crypto/us/snapshots":
        return httpx.Response(200, json={"snapshots": {"BTC/USD": {"latestTrade": {"p": 97000, "t": "2026-11-23T23:00:00Z"},
                                                                   "prevDailyBar": {"c": 96000}}}})
    if p == "/v1beta1/options/snapshots/NVDA":
        assert req.url.params["feed"] == "indicative"
        return httpx.Response(200, json={"snapshots": {
            f"NVDA{EXP}C00100000": {"latestQuote": {"bp": 2.1, "ap": 2.3}, "greeks": {"delta": 0.52}, "impliedVolatility": 0.41},
            f"NVDA{EXP}P00100000": {"latestQuote": {"bp": 1.9, "ap": 2.0}, "greeks": {"delta": -0.48}, "impliedVolatility": 0.43},
        }, "next_page_token": None})
    return httpx.Response(404, json={"message": "not found"})


def other_handler(req: httpx.Request) -> httpx.Response:
    host, p = req.url.host, req.url.path
    if host == "api.stlouisfed.org" and p.endswith("/series/observations"):
        return httpx.Response(200, json={"observations": [{"date": "2026-11-21", "value": "4.35"},
                                                          {"date": "2026-11-20", "value": "."},
                                                          {"date": "2026-11-19", "value": "4.30"}]})
    if host == "api.stlouisfed.org" and p.endswith("/releases/dates"):
        d = (date.today() + timedelta(days=2)).isoformat()
        return httpx.Response(200, json={"release_dates": [{"release_id": 10, "release_name": "Consumer Price Index", "date": d},
                                                           {"release_id": 99, "release_name": "Some minor survey", "date": d}]})
    if host == "api.frankfurter.app":
        return httpx.Response(200, json={"base": "USD", "rates": {"2026-11-20": {"EUR": 0.95, "JPY": 154.0},
                                                                 "2026-11-21": {"EUR": 0.96, "JPY": 155.0}}})
    if host == "www.sec.gov" and p == "/files/company_tickers.json":
        assert "User-Agent" in req.headers
        return httpx.Response(200, json={"0": {"cik_str": 1045810, "ticker": "NVDA", "title": "NVIDIA CORP"}})
    if host == "data.sec.gov" and p == "/api/xbrl/companyfacts/CIK0001045810.json":
        return httpx.Response(200, json={"facts": {"us-gaap": {"Revenues": {"units": {"USD": [
            {"start": "2024-01-29", "end": "2025-01-26", "val": 130.5e9, "form": "10-K", "filed": "2025-02-26", "fp": "FY"}]}}}}})
    if host == "finnhub.io":
        return httpx.Response(403, json={"error": "You don't have access to this resource."})
    return httpx.Response(404)


@pytest.fixture
def hub(monkeypatch):
    monkeypatch.setenv("ALPACA_API_KEY_ID", "k")
    monkeypatch.setenv("ALPACA_API_SECRET_KEY", "s")
    monkeypatch.setenv("FRED_API_KEY", "f")
    monkeypatch.setenv("FINNHUB_API_KEY", "x")
    monkeypatch.setenv("LIVENEWS_SEC_USER_AGENT", "Test Person test@example.com")
    s = load_settings(env_file=None)
    store = Store()
    data = AlpacaData(s, store, client=httpx.AsyncClient(base_url=DATA_URL, transport=httpx.MockTransport(alpaca_handler)))
    h = LiveHub(s, data)
    h.http = httpx.AsyncClient(transport=httpx.MockTransport(other_handler))
    return h


def test_bars_stock_and_crypto(hub):
    bars, src = asyncio.run(hub.bars("NVDA", "6M"))
    assert [b.c for b in bars] == [144, 142] and "IEX" in src
    bars, src = asyncio.run(hub.bars("BTC/USD", "6M"))
    assert bars[0].c == 1.5 and "crypto" in src


def test_markets_mixes_live_and_daily(hub):
    m = asyncio.run(hub.markets())
    ten = next(r for r in m["RATES"] if r.symbol == "DGS10")
    assert ten.value == 4.35 and ten.change == pytest.approx(0.05) and ten.live is False
    eur = next(r for r in m["FX"] if r.symbol == "EURUSD")
    assert eur.value == pytest.approx(1 / 0.96)
    assert m["CRYPTO"][0].value == 97000
    assert next(r for r in m["INDICES"] if r.symbol == "SPY").pct == pytest.approx(100 / 99 * 100 - 100)


def test_eco_marks_important(hub):
    evs = asyncio.run(hub.eco(7))
    by = {e.name: e.important for e in evs}
    assert by == {"Consumer Price Index": True, "Some minor survey": False}  # finnhub 403 doesn't break it


def test_financials_from_sec(hub):
    f = asyncio.run(hub.financials("NVDA"))
    assert f.periods == ["2025-01-26"] and f.rows[0][1] == [130.5e9]


def test_finnhub_plan_error_is_readable(hub):
    with pytest.raises(Unavailable, match="not on your plan"):
        asyncio.run(hub.analysts("NVDA"))


def test_option_chain(hub):
    ch = asyncio.run(hub.chain("NVDA"))
    assert ch.calls[100.0].delta == 0.52 and ch.puts[100.0].bid == 1.9 and ch.spot == 100.0
    assert "indicative" in ch.source.lower()


def test_missing_keys_explain_themselves(monkeypatch, hub):
    hub.fred_key = ""
    hub.sec_ua = ""
    with pytest.raises(Unavailable, match="LIVENEWS_SEC_USER_AGENT"):
        asyncio.run(hub.financials("NVDA"))
    m = asyncio.run(hub.markets())
    assert any("FRED_API_KEY" in e for e in m["_errors"])
