import asyncio
import math
from datetime import date, timedelta

import pytest

from livenews import indicators as ta
from livenews.broker import OrderRejected, OrderRequest, check_order
from livenews.broker.sim import SimBroker
from livenews.commands import order_type, parse
from livenews.hub.mock import MockHub
from livenews.hub.sec import extract_financials, recent_filings
from livenews.models import NewsItem, Quote, utcnow
from livenews.options_math import black_scholes, occ_symbol, parse_occ
from livenews.risk import Holding, beta, max_drawdown, portfolio_risk
from livenews.sources.trade_updates import parse_update
from livenews.store import Store
from livenews.userdata import AlertBook, Watchlists


# indicators -------------------------------------------------------------------

def test_sma_ema():
    assert ta.sma([1, 2, 3, 4], 2) == [None, 1.5, 2.5, 3.5]
    e = ta.ema([1, 2, 3, 4, 5], 3)
    assert e[:2] == [None, None] and e[2] == 2.0 and e[3] == pytest.approx(3.0) and e[4] == pytest.approx(4.0)


def test_rsi_bounds_and_extremes():
    up = list(range(1, 40))
    assert ta.rsi(up)[-1] == 100.0
    zig = [10, 11] * 20
    r = ta.rsi(zig)[-1]
    assert 40 < r < 60


def test_rsi_matches_reference():
    # classic Wilder example closes; RSI(14) on bar 15 is about 70.5
    closes = [44.34, 44.09, 44.15, 43.61, 44.33, 44.83, 45.10, 45.42, 45.84, 46.08, 45.89, 46.03, 45.61, 46.28, 46.28]
    assert ta.rsi(closes)[14] == pytest.approx(70.46, abs=0.1)


def test_macd_lengths_and_signal():
    xs = [100 + math.sin(i / 5) * 5 for i in range(80)]
    line, sig, hist = ta.macd(xs)
    assert len(line) == len(sig) == len(hist) == 80
    assert line[24] is None and line[25] is not None
    assert sig[33] is not None and sig[32] is None
    assert hist[-1] == pytest.approx(line[-1] - sig[-1])


def test_bollinger_and_vwap():
    lo, mid, up = ta.bollinger([10] * 25, 20, 2)
    assert lo[-1] == mid[-1] == up[-1] == 10
    v = ta.vwap([11, 12], [9, 10], [10, 11], [100, 300], ["d1", "d2"])
    assert v == [10, 11]  # resets each session
    v2 = ta.vwap([11, 12], [9, 10], [10, 11], [100, 300])
    assert v2[1] == pytest.approx((10 * 100 + 11 * 300) / 400)


# risk -------------------------------------------------------------------------

def test_beta_and_drawdown():
    bench = [0.01, -0.02, 0.015, -0.005, 0.02]
    assert beta([2 * x for x in bench], bench) == pytest.approx(2.0)
    assert max_drawdown([100, 120, 90, 130]) == pytest.approx(-0.25)


def test_portfolio_risk_single_stock_is_concentrated():
    closes = [100 * (1.01 if i % 2 else 0.99) ** i for i in range(60)]
    r = portfolio_risk([Holding("A", 10, 110, 100, 108, closes)], closes)
    assert r.hhi == pytest.approx(1.0)
    assert r.beta == pytest.approx(1.0)
    assert r.unrealized == pytest.approx(100)
    assert r.day_pl == pytest.approx(20)
    assert r.var95_param > 0


# options ----------------------------------------------------------------------

def test_black_scholes_reference_and_parity():
    c = black_scholes(100, 100, 1, 0.05, 0.2, "C")
    p = black_scholes(100, 100, 1, 0.05, 0.2, "P")
    assert c["price"] == pytest.approx(10.4506, abs=1e-3)
    assert c["price"] - p["price"] == pytest.approx(100 - 100 * math.exp(-0.05), abs=1e-6)
    assert c["delta"] - p["delta"] == pytest.approx(1.0)


def test_occ_round_trip():
    s = occ_symbol("AAPL", date(2026, 1, 16), "P", 182.5)
    assert s == "AAPL260116P00182500"
    c = parse_occ(s)
    assert (c.root, c.expiry, c.right, c.strike) == ("AAPL", date(2026, 1, 16), "P", 182.5)
    assert parse_occ("junk") is None


# sec --------------------------------------------------------------------------

def _fact(start, end, val, form="10-K", filed="2025-02-01", fp="FY"):
    d = {"end": end, "val": val, "form": form, "filed": filed, "fp": fp}
    if start:
        d["start"] = start
    return d


def test_extract_financials_annual_picks_full_years_and_latest_filing():
    facts = {"facts": {"us-gaap": {
        "Revenues": {"units": {"USD": [
            _fact("2023-01-01", "2023-12-31", 100.0, filed="2024-02-01"),
            _fact("2023-01-01", "2023-12-31", 101.0, filed="2025-02-01"),  # restated in next 10-K, newer wins
            _fact("2024-01-01", "2024-12-31", 120.0),
            _fact("2024-10-01", "2024-12-31", 33.0, form="10-Q", fp="Q4"),  # a quarter, ignored for annual
        ]}},
        "NetIncomeLoss": {"units": {"USD": [_fact("2023-01-01", "2023-12-31", 10.0), _fact("2024-01-01", "2024-12-31", 12.0)]}},
        "Assets": {"units": {"USD": [_fact(None, "2024-12-31", 500.0)]}},
    }}}
    ends, rows = extract_financials(facts, "A")
    assert ends == ["2023-12-31", "2024-12-31"]
    d = {label: vals for label, vals, _ in rows}
    assert d["Revenue"] == [101.0, 120.0]
    assert d["Net margin"][1] == pytest.approx(10.0)
    assert d["Revenue growth"][1] == pytest.approx((120 / 101 - 1) * 100)
    assert d["Total assets"] == [None, 500.0]


def test_extract_financials_quarterly():
    facts = {"facts": {"us-gaap": {"Revenues": {"units": {"USD": [
        _fact("2024-07-01", "2024-09-30", 30.0, form="10-Q", fp="Q3"),
        _fact("2024-01-01", "2024-09-30", 80.0, form="10-Q", fp="Q3"),  # year-to-date, not a quarter
    ]}}}}}
    ends, rows = extract_financials(facts, "Q")
    assert ends == ["2024-09-30"] and rows[0][1] == [30.0]


def test_recent_filings_links():
    sub = {"filings": {"recent": {"form": ["8-K", "424B2", "10-Q"], "filingDate": ["2025-01-02", "2025-01-01", "2024-11-01"],
                                  "accessionNumber": ["0000320193-25-000001", "x", "0000320193-24-000100"],
                                  "primaryDocument": ["a.htm", "b.htm", "q.htm"]}}}
    f = recent_filings(sub, "0000320193")
    assert [x["form"] for x in f] == ["8-K", "10-Q"]
    assert f[0]["url"] == "https://www.sec.gov/Archives/edgar/data/320193/000032019325000001/a.htm"


# commands ---------------------------------------------------------------------

@pytest.mark.parametrize("line,kind,sym,arg", [
    ("nvda ch", "chart", "NVDA", "6M"), ("nvda ch 1d", "chart", "NVDA", "1D"), ("btc/usd ch 1m", "chart", "BTC/USD", "1M"),
    ("aapl fa q", "fa", "AAPL", "Q"), ("aapl fa", "fa", "AAPL", "A"), ("aapl des", "des", "AAPL", ""),
    ("aapl opt 3", "opt", "AAPL", "3"), ("fx", "mkts", "", "FX"), ("mkts", "mkts", "", ""),
    ("port", "port", "", ""), ("ord", "blotter", "", ""), ("main", "main", "", ""), ("w q", "quote", "W", ""),
    ("eco", "eco", "", ""),
])
def test_function_commands(line, kind, sym, arg):
    c = parse(line)
    assert (c.kind, c.symbol, c.arg) == (kind, sym, arg)


def test_order_variants():
    c = parse("sell aapl 10 stp 175 lmt 174 gtc")
    assert (order_type(c), c.stop, c.limit, c.tif) == ("stop_limit", 175, 174, "gtc")
    assert order_type(parse("sell aapl 10 trail 3%")) == "trailing_stop"
    assert parse("sell aapl 10 trail 3 lmt 5").kind == "error"
    assert parse("nvda ch 2w").kind == "error"


def test_alert_commands():
    assert (lambda c: (c.kind, c.arg, c.symbol, c.value))(parse("alrt nvda > 150")) == ("alert_add", "above", "NVDA", 150)
    c = parse("alrt news fda")
    assert (c.kind, c.arg, c.args, c.symbol) == ("alert_add", "news", ["fda"], "")
    c = parse("alrt news guidance nvda")
    assert (c.args, c.symbol) == (["guidance"], "NVDA")
    assert parse("alrt del 2").kind == "alert_del"


# orders -----------------------------------------------------------------------

def test_stop_checks():
    with pytest.raises(OrderRejected):  # buy stop below market fires instantly
        check_order(OrderRequest("A", "buy", 1, "stop", stop_price=95), 100, 5000)
    assert check_order(OrderRequest("A", "sell", 1, "stop", stop_price=95), 100, 5000) == 100
    with pytest.raises(OrderRejected):
        check_order(OrderRequest("A", "sell", 1, "trailing_stop", trail_percent=0), 100, 5000)
    with pytest.raises(OrderRejected):
        check_order(OrderRequest("A", "sell", 1, tif="ioc"), 100, 5000)


def test_sim_stop_and_trailing():
    store = Store()
    store.update_quote(Quote("A", 100.0, 100.0))
    b = SimBroker(store)

    async def go():
        await b.submit(OrderRequest("A", "buy", 10))
        stop = await b.submit(OrderRequest("A", "sell", 5, "stop", stop_price=95))
        trail = await b.submit(OrderRequest("A", "sell", 5, "trailing_stop", trail_percent=5))
        store.update_quote(Quote("A", 110.0, 100.0))
        await b.refresh()
        assert stop.status == "new" and trail.stop_price == pytest.approx(104.5)
        store.update_quote(Quote("A", 104.0, 100.0))
        await b.refresh()
        assert trail.status == "filled" and stop.status == "new"
        store.update_quote(Quote("A", 94.0, 100.0))
        await b.refresh()
        assert stop.status == "filled" and store.positions == []
        events = [x.event for x in store.executions]
        assert events.count("fill") == 3 and events.count("new") == 3

    asyncio.run(go())


def test_trade_update_parse():
    msg = {"event": "fill", "timestamp": "2026-11-23T15:00:00.250Z", "price": "101.5", "qty": "10", "position_qty": "10",
           "order": {"id": "abc", "symbol": "AAPL", "side": "buy", "qty": "10", "submitted_at": "2026-11-23T15:00:00Z"}}
    x = parse_update(msg)
    assert (x.event, x.symbol, x.price, x.qty, x.position_qty) == ("fill", "AAPL", 101.5, 10.0, 10.0)
    assert x.latency_ms == pytest.approx(250)
    assert parse_update({"event": "new"}) is None


# watchlists and alerts --------------------------------------------------------

def test_watchlists_persist(tmp_path):
    p = tmp_path / "w.json"
    w = Watchlists(["SPY"], p)
    w.add("NVDA", "AMD")
    w.use("tech")
    w.add("MSFT")
    w2 = Watchlists(["XXX"], p)
    assert w2.active == "tech" and w2.symbols == ["MSFT"] and w2.lists["main"] == ["SPY", "NVDA", "AMD"]


def test_alerts_fire_once(tmp_path):
    store = Store()
    book = AlertBook.load(tmp_path / "a.json")
    book.add("above", "A", 105)
    book.add("move", "A", 3)
    book.add("news", keyword="fda")
    store.update_quote(Quote("A", 104.0, 100.0))
    assert [a.kind for a in book.check(store)] == ["move"]
    store.update_quote(Quote("A", 106.0, 100.0))
    assert [a.kind for a in book.check(store)] == ["above"]
    store.add_news(NewsItem("n1", "FDA approves drug", "X", utcnow()))
    store.add_news(NewsItem("n0", "old FDA story", "X", utcnow() - timedelta(hours=2)))
    fired = book.check(store)
    assert [a.kind for a in fired] == ["news"] and "approves" in fired[0].note
    assert book.check(store) == []
    assert len(AlertBook.load(tmp_path / "a.json").armed()) == 0


# mock hub covers every screen --------------------------------------------------

def test_mock_hub_everything():
    store = Store()
    h = MockHub(store)

    async def go():
        for span in ("1D", "5D", "1M", "6M", "1Y", "5Y"):
            bars, _ = await h.bars("AAPL", span)
            assert len(bars) > 20 and all(b.l <= min(b.o, b.c) and b.h >= max(b.o, b.c) for b in bars)
        m = await h.markets()
        assert set(m) >= {"INDICES", "RATES", "FX", "COMMODITIES", "CRYPTO"}
        assert await h.eco(14, ["AAPL"])
        assert (await h.profile("AAPL")).name
        f = await h.financials("AAPL", "Q")
        assert len(f.periods) == 8
        assert (await h.analysts("AAPL"))["recs"]
        assert (await h.insiders("AAPL"))["rows"]
        ch = await h.chain("AAPL")
        k = min(ch.calls, key=lambda x: abs(x - ch.spot))
        assert 0.3 < ch.calls[k].delta < 0.7

    asyncio.run(go())
