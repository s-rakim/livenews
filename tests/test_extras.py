"""House Clerk trades, delayed quotes, FX stream, 13F holders, chat relay,
price targets, release times, options and crypto orders."""
import asyncio
import io
import json
import zipfile
from datetime import date, timedelta

import httpx
import pytest

from livenews.broker import OrderRejected, OrderRequest, asset_class, check_order, multiplier
from livenews.broker.alpaca import _px, _qty, position_symbol
from livenews.broker.sim import SimBroker
from livenews.commands import parse
from livenews.hub.base import usual_time
from livenews.hub.delayed import parse_chart_bars, parse_chart_meta
from livenews.hub.mock import MockHub
from livenews.models import Quote, utcnow
from livenews.options_math import occ_symbol
from livenews.sources.house import parse_index, parse_transactions
from livenews.sources.people import PeopleTracker, house_events
from livenews.store import Store


def run_async(fn):
    """Run an async test with asyncio.run, no plugin needed."""
    import functools

    @functools.wraps(fn)
    def wrapper(*a, **kw):
        return asyncio.run(fn(*a, **kw))

    return wrapper

INDEX_XML = b"""<?xml version="1.0" encoding="utf-8"?>
<FinancialDisclosure>
  <Member><Prefix>Hon.</Prefix><Last>Pelosi</Last><First>Nancy</First><Suffix/><FilingType>P</FilingType>
    <StateDst>CA11</StateDst><Year>2026</Year><FilingDate>{d1}</FilingDate><DocID>20031111</DocID></Member>
  <Member><Prefix>Hon.</Prefix><Last>Pelosi</Last><First>Nancy</First><Suffix/><FilingType>O</FilingType>
    <StateDst>CA11</StateDst><Year>2026</Year><FilingDate>{d1}</FilingDate><DocID>10066666</DocID></Member>
  <Member><Prefix>Hon.</Prefix><Last>Khanna</Last><First>Ro</First><Suffix/><FilingType>P</FilingType>
    <StateDst>CA17</StateDst><Year>2026</Year><FilingDate>{d2}</FilingDate><DocID>8222222</DocID></Member>
  <Member><Prefix>Hon.</Prefix><Last>Nobody</Last><First>Some</First><Suffix/><FilingType>P</FilingType>
    <StateDst>TX01</StateDst><Year>2026</Year><FilingDate>{d2}</FilingDate><DocID>20033333</DocID></Member>
</FinancialDisclosure>
"""

PTR_TEXT = """
SP NVIDIA Corporation - Common Stock (NVDA) [ST]
P 06/20/2026 06/20/2026 $1,000,001 - $5,000,000
F S : New
SP Visa Inc. Class A (V) [ST] S (partial) 06/24/2026 06/24/2026 $500,001 -
$1,000,000
SP Alphabet Inc. - Class A (GOOGL) [OP] P 01/14/2026 01/14/2026 Over $50,000,000
"""


def _index_bytes():
    today = date.today()
    d1 = (today - timedelta(days=3)).strftime("%m/%d/%Y")
    d2 = (today - timedelta(days=10)).strftime("%m/%d/%Y")
    return INDEX_XML.replace(b"{d1}", d1.encode()).replace(b"{d2}", d2.encode())


# --- House Clerk ---------------------------------------------------------------

def test_house_index_keeps_only_trade_reports():
    fs = parse_index(_index_bytes())
    assert [f.doc_id for f in fs] == ["20031111", "8222222", "20033333"]
    assert fs[0].name == "Nancy Pelosi"
    assert fs[0].url.endswith("/ptr-pdfs/2026/20031111.pdf")


def test_house_transactions_from_pdf_text():
    txs = parse_transactions(PTR_TEXT)
    assert [t["ticker"] for t in txs] == ["NVDA", "V", "GOOGL"]
    assert txs[0]["action"] == "Purchase" and txs[0]["amount"] == "$1,000,001 - $5,000,000"
    assert txs[1]["action"] == "Sale (partial)" and txs[1]["amount"] == "$500,001 - $1,000,000"
    assert txs[2]["asset"] == "option" and txs[2]["amount"] == "Over $50,000,000"
    assert txs[0]["traded"] == "2026-06-20"


def test_house_events_include_unreadable_filings():
    f = parse_index(_index_bytes())[0]
    rows = house_events(f, parse_transactions(PTR_TEXT))
    assert rows[0].person == "Nancy Pelosi (H)" and rows[0].symbol == "NVDA"
    assert rows[2].action == "Purchase option"
    assert all(r.source == "House Clerk PTR" and r.url == f.url for r in rows)
    scanned = house_events(f, [])
    assert len(scanned) == 1 and scanned[0].action == "PDF only" and scanned[0].symbol == "-"


@run_async
async def test_people_tracker_polls_house_clerk_without_fmp(tmp_path):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("2026FD.xml", _index_bytes())
    zip_bytes = buf.getvalue()
    pdf_hits = []

    def handler(req: httpx.Request):
        if req.url.path.endswith("FD.zip"):
            if str(date.today().year) in req.url.path:
                return httpx.Response(200, content=zip_bytes)
            return httpx.Response(404)
        pdf_hits.append(req.url.path)
        return httpx.Response(200, content=b"%PDF-1.4 not really a pdf")

    store = Store()
    pt = PeopleTracker(store, ["Pelosi", "Khanna"], house_dir=tmp_path)
    await pt.house.aclose()
    pt.house.http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    # Pelosi's report was already read once and cached, Khanna's is a scan we can't parse
    (tmp_path / "20031111.json").write_text(json.dumps(parse_transactions(PTR_TEXT)))
    n = await pt.poll_house_once()
    assert n == 4
    who = {(e.person, e.symbol) for e in pt.events}
    assert ("Nancy Pelosi (H)", "NVDA") in who and ("Ro Khanna (H)", "-") in who
    assert not any("Nobody" in e.person for e in pt.events)
    assert pdf_hits == ["/public_disc/ptr-pdfs/2026/8222222.pdf"]
    assert await pt.poll_house_once() == 0  # second pass adds nothing new
    assert "House Clerk" in store.feeds["PEOPLE"].source
    await pt.aclose()


def test_people_tracker_without_house_or_fmp_has_nothing_to_poll():
    pt = PeopleTracker(Store(), ["Pelosi"])
    assert pt.house is None
    assert "Senate needs FMP_API_KEY" in pt.store.feeds["PEOPLE"].note


# --- delayed quotes and FX stream ---------------------------------------------

YAHOO = {"chart": {"result": [{
    "meta": {"regularMarketPrice": 5987.25, "chartPreviousClose": 5950.0, "regularMarketTime": 1764000000,
             "currency": "USD", "exchangeName": "CME"},
    "timestamp": [1764000000, 1764000300, 1764000600],
    "indicators": {"quote": [{"open": [5950, 5960, None], "high": [5962, 5970, 5990],
                              "low": [5949, 5958, 5980], "close": [5960, 5968, 5987], "volume": [100, 200, 50]}]},
}], "error": None}}


def test_yahoo_chart_parsing():
    m = parse_chart_meta(YAHOO)
    assert m["price"] == 5987.25 and m["prev"] == 5950.0 and m["exchange"] == "CME"
    bars = parse_chart_bars(YAHOO)
    assert len(bars) == 2  # the bar with a missing open is skipped
    assert bars[1][4] == 5968.0
    assert parse_chart_meta({"chart": {"result": []}}) is None


def test_fx_stream_writes_store():
    from livenews.sources.finnhub_ws import FxStream

    store = Store()
    fx = FxStream("k", store)
    n = fx.handle({"type": "trade", "data": [{"s": "OANDA:EUR_USD", "p": 1.0523, "t": 1764000000000},
                                             {"s": "OANDA:NOPE", "p": 1.0}]})
    assert n == 1 and store.fx["EURUSD"][0] == 1.0523
    assert fx.handle({"type": "ping"}) == 0


# --- 13F -------------------------------------------------------------------------

def _tsv(rows):
    head = list(rows[0])
    return "\t".join(head) + "\n" + "\n".join("\t".join(str(r[h]) for h in head) for r in rows) + "\n"


def test_13f_index_and_holders(tmp_path):
    from livenews.hub import thirteenf as tf

    sub = [{"ACCESSION_NUMBER": "a1", "FILING_DATE": "14-NOV-2026", "SUBMISSIONTYPE": "13F-HR", "PERIODOFREPORT": "30-SEP-2026"},
           {"ACCESSION_NUMBER": "a2", "FILING_DATE": "13-NOV-2026", "SUBMISSIONTYPE": "13F-HR", "PERIODOFREPORT": "30-SEP-2026"},
           {"ACCESSION_NUMBER": "a3", "FILING_DATE": "13-NOV-2026", "SUBMISSIONTYPE": "13F-NT", "PERIODOFREPORT": "30-SEP-2026"}]
    cover = [{"ACCESSION_NUMBER": "a1", "FILINGMANAGER_NAME": "Big Index Co", "REPORTCALENDARORQUARTER": "30-SEP-2026"},
             {"ACCESSION_NUMBER": "a2", "FILINGMANAGER_NAME": "Hedge Fund LP", "REPORTCALENDARORQUARTER": "30-SEP-2026"},
             {"ACCESSION_NUMBER": "a3", "FILINGMANAGER_NAME": "Notice Only", "REPORTCALENDARORQUARTER": "30-SEP-2026"}]
    info = [
        {"ACCESSION_NUMBER": "a1", "NAMEOFISSUER": "NVIDIA CORPORATION", "TITLEOFCLASS": "COM", "CUSIP": "67066g104",
         "VALUE": 9000000, "SSHPRNAMT": 50000, "SSHPRNAMTTYPE": "SH", "PUTCALL": ""},
        {"ACCESSION_NUMBER": "a2", "NAMEOFISSUER": "NVIDIA CORP", "TITLEOFCLASS": "COM", "CUSIP": "67066G104",
         "VALUE": 3000000, "SSHPRNAMT": 16000, "SSHPRNAMTTYPE": "SH", "PUTCALL": ""},
        {"ACCESSION_NUMBER": "a2", "NAMEOFISSUER": "NVIDIA CORP", "TITLEOFCLASS": "CALL", "CUSIP": "67066G104",
         "VALUE": 5000000, "SSHPRNAMT": 30000, "SSHPRNAMTTYPE": "SH", "PUTCALL": "Call"},
        {"ACCESSION_NUMBER": "a3", "NAMEOFISSUER": "NVIDIA CORP", "TITLEOFCLASS": "COM", "CUSIP": "67066G104",
         "VALUE": 1, "SSHPRNAMT": 1, "SSHPRNAMTTYPE": "SH", "PUTCALL": ""},
        {"ACCESSION_NUMBER": "a1", "NAMEOFISSUER": "APPLE INC", "TITLEOFCLASS": "COM", "CUSIP": "037833100",
         "VALUE": 7000000, "SSHPRNAMT": 30000, "SSHPRNAMTTYPE": "SH", "PUTCALL": ""},
    ]
    zp = tmp_path / "13f.zip"
    with zipfile.ZipFile(zp, "w") as z:
        z.writestr("SUBMISSION.tsv", _tsv(sub))
        z.writestr("COVERPAGE.tsv", _tsv(cover))
        z.writestr("INFOTABLE.tsv", _tsv(info))
    db = tmp_path / "13f.sqlite"
    tf.build_index(zp, db)
    h = tf.holders(db, "NVIDIA CORP")
    assert h["cusip"] == "67066G104"
    assert [r["manager"] for r in h["rows"]] == ["Big Index Co", "Hedge Fund LP"]  # calls and the 13F-NT left out
    assert h["count"] == 2 and h["total_shares"] == 66000
    assert tf.holders(db, "Nonexistent Holdings Inc")["rows"] == []


def test_13f_newest_link_picks_latest_quarter():
    from livenews.hub import thirteenf as tf

    html = ('<a href="/files/structureddata/data/form-13f-data-sets/01jun2026-31aug2026_form13f.zip">x</a>'
            '<a href="/files/structureddata/data/form-13f-data-sets/01sep2026-30nov2026_form13f.zip">y</a>'
            '<a href="/files/structureddata/data/form-13f-data-sets/01mar2026-31may2026_form13f.zip">z</a>')
    assert tf.newest_link(html).endswith("01sep2026-30nov2026_form13f.zip")


# --- chat ----------------------------------------------------------------------

@run_async
async def test_chat_rooms_and_direct_messages(tmp_path):
    from livenews.chat import ChatClient, Relay

    relay = Relay(tmp_path / "hist.jsonl")
    ready, bound = asyncio.Event(), []
    server = asyncio.create_task(relay.serve_forever("127.0.0.1", 0, ready, bound))
    await asyncio.wait_for(ready.wait(), 5)
    url = f"ws://127.0.0.1:{bound[0]}"
    a = ChatClient(user="ann", url=url, serve_at="", token="")
    b = ChatClient(user="bob", url=url, serve_at="", token="")
    ta, tb = asyncio.create_task(a.run()), asyncio.create_task(b.run())

    async def until(cond, what):
        for _ in range(100):
            if cond():
                return
            await asyncio.sleep(0.05)
        raise AssertionError(what)

    try:
        await until(lambda: a.ws is not None and b.ws is not None and len(a.state.users) == 2, "both connected")
        await a.send("watching $NVDA into the print")
        await until(lambda: any(m.get("text", "").startswith("watching") for m in b.state.rooms["#general"]), "room msg")
        await b.send("same, small size", to="@ann")
        await until(lambda: a.state.rooms.get("@ann+bob"), "dm delivered")
        assert a.state.unread["@ann+bob"] == 1
        assert (tmp_path / "hist.jsonl").read_text().count("\n") >= 2
    finally:
        for t in (ta, tb, server):
            t.cancel()
        await asyncio.gather(ta, tb, server, return_exceptions=True)


# --- price targets and calendar times ----------------------------------------------

def _live_hub(handler, finnhub="fh", fmp="fm"):
    from livenews.config import load_settings
    from livenews.hub.live import LiveHub
    from livenews.sources.alpaca import AlpacaData

    s = load_settings(env_file=None, force_mock=True)
    s.finnhub_key, s.fmp_key = finnhub, fmp
    hub = LiveHub(s, AlpacaData(s, Store()))
    hub.http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return hub


@run_async
async def test_price_target_falls_back_to_fmp():
    def handler(req):
        if "finnhub" in req.url.host:
            if req.url.path.endswith("price-target"):
                return httpx.Response(403, json={"error": "You don't have access to this resource."})
            if req.url.path.endswith("recommendation"):
                return httpx.Response(200, json=[{"period": "2026-11-01", "strongBuy": 10, "buy": 20, "hold": 5,
                                                  "sell": 1, "strongSell": 0}])
            return httpx.Response(200, json=[] if "earnings" in req.url.path else {"metric": {}})
        assert req.url.params["symbol"] == "NVDA"
        return httpx.Response(200, json=[{"symbol": "NVDA", "targetHigh": 250, "targetLow": 120,
                                          "targetConsensus": 205.5, "targetMedian": 210}])

    hub = _live_hub(handler)
    a = await hub.analysts("NVDA")
    assert a["target"]["mean"] == 205.5 and a["target"]["source"] == "FMP consensus"
    assert a["recs"][0]["strongBuy"] == 10
    assert "FMP" in a["source"]
    await hub.aclose()


@run_async
async def test_price_target_missing_says_why():
    def handler(req):
        if req.url.path.endswith("price-target"):
            return httpx.Response(403)
        if req.url.path.endswith("recommendation"):
            return httpx.Response(200, json=[{"period": "2026-11-01", "strongBuy": 1, "buy": 1, "hold": 1}])
        return httpx.Response(200, json=[])

    hub = _live_hub(handler, fmp="")
    a = await hub.analysts("AAPL")
    assert "not on your plan" in a["target"]["missing"]
    await hub.aclose()


def test_usual_release_times():
    assert usual_time("Consumer Price Index") == "08:30"
    assert usual_time("FOMC Press Release") == "14:00"
    assert usual_time("Industrial Production and Capacity Utilization") == "09:15"
    assert usual_time("Job Openings and Labor Turnover Survey") == "10:00"
    assert usual_time("Some Obscure Regional Survey") == ""


# --- options and crypto orders ------------------------------------------------------

OCC = "AAPL" + (date.today() + timedelta(days=30)).strftime("%y%m%d") + "C00230000"


def test_asset_classes():
    assert asset_class("AAPL") == "equity" and asset_class("BRK.B") == "equity"
    assert asset_class("BTC/USD") == "crypto"
    assert asset_class(OCC) == "option" and multiplier(OCC) == 100
    assert asset_class("^GSPC") == "data" and asset_class("ES=F") == "data"


def test_check_order_options():
    req = OrderRequest(OCC, "buy", 2, "limit", limit_price=4.10)
    assert check_order(req, 4.00, 10_000) == pytest.approx(820.0)  # 2 contracts x 100 x 4.10
    with pytest.raises(OrderRejected, match="whole contracts"):
        check_order(OrderRequest(OCC, "buy", 1.5, "limit", limit_price=4.1), 4.0, 10_000)
    with pytest.raises(OrderRejected, match="market or limit"):
        check_order(OrderRequest(OCC, "sell", 1, "stop", stop_price=3.0), 4.0, 10_000)
    with pytest.raises(OrderRejected, match="DAY only"):
        check_order(OrderRequest(OCC, "buy", 1, "limit", limit_price=4.1, tif="gtc"), 4.0, 10_000)
    with pytest.raises(OrderRejected, match="LIVENEWS_MAX_ORDER_VALUE"):
        check_order(OrderRequest(OCC, "buy", 30, "limit", limit_price=4.1), 4.0, 10_000)


def test_check_order_crypto_and_data_symbols():
    req = OrderRequest("BTC/USD", "buy", 0.05)
    assert check_order(req, 96_000, 10_000) == pytest.approx(4800)
    assert req.tif == "gtc"
    with pytest.raises(OrderRejected, match="crypto takes"):
        check_order(OrderRequest("ETH/USD", "sell", 1, "trailing_stop", trail_percent=3), 3400, 10_000)
    with pytest.raises(OrderRejected, match="can't be traded"):
        check_order(OrderRequest("ES=F", "buy", 1), 6000, 1e9)


def test_alpaca_formatting_helpers():
    assert _qty(1.0) == "1" and _qty(0.05) == "0.05" and _qty(1234567.0) == "1234567"
    assert _px(182.5) == "182.50" and _px(0.1234) == "0.1234"
    assert position_symbol({"symbol": "BTCUSD", "asset_class": "crypto"}) == "BTC/USD"
    assert position_symbol({"symbol": "ETHUSDT", "asset_class": "crypto"}) == "ETH/USDT"
    assert position_symbol({"symbol": OCC, "asset_class": "us_option"}) == OCC


def test_commands_take_option_and_crypto_orders():
    c = parse(f"BUY {OCC} 2 LMT 4.10")
    assert c.kind == "order" and c.symbol == OCC and c.qty == 2 and c.limit == 4.10
    c = parse("sell btc/usd 0.25")
    assert c.kind == "order" and c.symbol == "BTC/USD" and c.qty == 0.25


def _store_with(sym, px):
    st = Store()
    st.update_quote(Quote(symbol=sym, price=px, prev_close=px, updated_at=utcnow()))
    return st


@run_async
async def test_sim_broker_options_use_contract_multiplier():
    st = _store_with("AAPL", 228.0)
    hub = MockHub(st)
    b = SimBroker(st, option_mark=hub.option_mid)
    mid = round(hub.option_mid(OCC), 2)
    o = await b.submit(OrderRequest(OCC, "buy", 2))
    assert o.status == "filled" and o.filled_avg_price == mid
    await b.refresh()
    pos = st.positions[0]
    assert pos.symbol == OCC and pos.mult == 100
    assert b.cash == pytest.approx(100_000 - 2 * 100 * mid)
    assert st.account.equity == pytest.approx(100_000, abs=2 * 100 * 0.02)


@run_async
async def test_sim_broker_settles_expired_option_at_intrinsic():
    st = _store_with("AAPL", 240.0)
    old = occ_symbol("AAPL", date.today() - timedelta(days=2), "C", 230)
    b = SimBroker(st, option_mark=lambda s: 12.0)
    await b.submit(OrderRequest(old, "buy", 1))
    assert b.cash == pytest.approx(100_000 - 1200)
    await b.refresh()
    assert st.positions == []
    assert b.cash == pytest.approx(100_000 - 1200 + 1000)  # (240 - 230) x 100


@run_async
async def test_sim_broker_crypto_fractional():
    st = _store_with("BTC/USD", 96_000.0)
    b = SimBroker(st)
    req = OrderRequest("BTC/USD", "buy", 0.01)
    check_order(req, 96_000, 10_000)
    o = await b.submit(req)
    assert o.status == "filled" and o.tif == "gtc"
    await b.refresh()
    assert st.positions[0].qty == 0.01
    assert b.cash == pytest.approx(100_000 - 960)


def test_crypto_quotes_stay_out_of_movers():
    from livenews import engine

    st = _store_with("BTC/USD", 96_000.0)
    st.quotes["BTC/USD"].prev_close = 90_000.0
    st.update_quote(Quote(symbol="AAPL", price=230, prev_close=228, updated_at=utcnow()))
    assert [m.symbol for m in engine.build_movers(st)] == ["AAPL"]


def test_stock_universe_skips_crypto_and_data_symbols():
    from livenews.config import load_settings
    from livenews.sources.alpaca import AlpacaData

    s = load_settings(env_file=None, force_mock=True)
    s.watchlist[:] = ["AAPL", "BTC/USD", "^GSPC", "ES=F", "BRK.B"]
    d = AlpacaData(s, Store())
    assert d.universe() == ["AAPL", "BRK.B"]
    assert d.crypto_universe() == ["BTC/USD"]
