import asyncio
from collections import deque
from datetime import datetime, timedelta, timezone

import pytest

from livenews import engine
from livenews.broker import OrderRejected, OrderRequest, check_order
from livenews.broker.sim import SimBroker
from livenews.commands import parse
from livenews.config import LIVE_TRADING_URL, PAPER_TRADING_URL, ConfigError, load_settings
from livenews.models import NewsItem, Quote, utcnow
from livenews.ratelimit import RateLimiter
from livenews.sources.alpaca import minute_key, parse_news, parse_snapshot
from livenews.sources.alpaca_stream import TradeStream
from livenews.sources.finnhub import parse_finnhub
from livenews.sources.people import PeopleTracker, parse_fmp
from livenews.store import Store
from livenews.timeparse import parse_ts

ENV_KEYS = ["ALPACA_API_KEY_ID", "ALPACA_API_SECRET_KEY", "LIVENEWS_DATA", "LIVENEWS_TRADING_MODE",
            "LIVENEWS_ALLOW_LIVE", "LIVENEWS_WATCHLIST"]


@pytest.fixture
def env(monkeypatch):
    for k in ENV_KEYS:
        monkeypatch.delenv(k, raising=False)
    return monkeypatch


# config / safety -------------------------------------------------------------

def test_no_keys_means_mock_and_paper(env):
    s = load_settings(env_file=None)
    assert s.data_mode == "mock"
    assert s.trading_mode == "paper"
    assert s.trading_url == PAPER_TRADING_URL


def test_keys_default_to_paper(env):
    env.setenv("ALPACA_API_KEY_ID", "k")
    env.setenv("ALPACA_API_SECRET_KEY", "s")
    s = load_settings(env_file=None)
    assert s.data_mode == "alpaca"
    assert not s.is_live
    assert s.trading_url == PAPER_TRADING_URL


def test_live_needs_second_switch(env):
    env.setenv("ALPACA_API_KEY_ID", "k")
    env.setenv("ALPACA_API_SECRET_KEY", "s")
    env.setenv("LIVENEWS_TRADING_MODE", "live")
    with pytest.raises(ConfigError):
        load_settings(env_file=None)
    env.setenv("LIVENEWS_ALLOW_LIVE", "yes")
    with pytest.raises(ConfigError):
        load_settings(env_file=None)
    env.setenv("LIVENEWS_ALLOW_LIVE", "yes-i-understand")
    s = load_settings(env_file=None)
    assert s.is_live and s.trading_url == LIVE_TRADING_URL


def test_live_refused_with_mock_data(env):
    env.setenv("LIVENEWS_TRADING_MODE", "live")
    env.setenv("LIVENEWS_ALLOW_LIVE", "yes-i-understand")
    with pytest.raises(ConfigError):
        load_settings(env_file=None, force_mock=True)


# order checks ----------------------------------------------------------------

def test_check_order_value_cap():
    assert check_order(OrderRequest("AAPL", "buy", 10), 100.0, 5000) == 1000.0
    with pytest.raises(OrderRejected):
        check_order(OrderRequest("AAPL", "buy", 100), 100.0, 5000)


def test_check_order_needs_price_and_sane_limit():
    with pytest.raises(OrderRejected):
        check_order(OrderRequest("AAPL", "buy", 1), None, 5000)
    with pytest.raises(OrderRejected):
        check_order(OrderRequest("AAPL", "buy", 1, "limit", 150.0), 100.0, 5000)  # fat finger
    with pytest.raises(OrderRejected):
        check_order(OrderRequest("AAPL", "buy", 0), 100.0, 5000)
    assert check_order(OrderRequest("AAPL", "sell", 2, "limit", 101.0), 100.0, 5000) == 202.0


def test_sim_broker_round_trip():
    store = Store()
    store.update_quote(Quote("AAPL", 100.0, 99.0))
    b = SimBroker(store, cash=10_000)

    async def go():
        await b.submit(OrderRequest("AAPL", "buy", 10))
        store.update_quote(Quote("AAPL", 110.0, 99.0))
        await b.refresh()
        assert store.positions[0].unrealized == pytest.approx(100.0)
        await b.submit(OrderRequest("AAPL", "sell", 10))
        await b.refresh()
        assert store.positions == []
        assert store.account.equity == pytest.approx(10_100.0)

    asyncio.run(go())


def test_sim_limit_waits_for_price():
    store = Store()
    store.update_quote(Quote("AAPL", 100.0, 99.0))
    b = SimBroker(store)

    async def go():
        o = await b.submit(OrderRequest("AAPL", "buy", 1, "limit", 95.0))
        assert o.status == "new"
        store.update_quote(Quote("AAPL", 94.5, 99.0))
        await b.refresh()
        assert o.status == "filled" and o.filled_avg_price == 95.0

    asyncio.run(go())


# commands --------------------------------------------------------------------

@pytest.mark.parametrize("line,kind,sym", [
    ("aapl", "quote", "AAPL"), ("AAPL Q", "quote", "AAPL"), ("nvda n", "news", "NVDA"),
    ("N", "allnews", ""), ("BRK.B", "quote", "BRK.B"),
])
def test_parse_lookup(line, kind, sym):
    c = parse(line)
    assert (c.kind, c.symbol) == (kind, sym)


def test_parse_orders():
    c = parse("sell tsla 5 lmt 300.5")
    assert (c.kind, c.side, c.symbol, c.qty, c.limit) == ("order", "sell", "TSLA", 5.0, 300.5)
    assert parse("buy aapl ten").kind == "error"
    assert parse("buy aapl 1 lmt").kind == "error"


# parsing ---------------------------------------------------------------------

def test_parse_ts_nanoseconds():
    t = parse_ts("2026-11-23T14:30:00.123456789Z")
    assert t == datetime(2026, 11, 23, 14, 30, 0, 123456, tzinfo=timezone.utc)
    assert parse_ts("garbage") is None


def test_parse_alpaca_news():
    n = parse_news({"id": 42, "headline": " Big news ", "created_at": "2026-11-23T14:30:00Z",
                    "symbols": ["aapl"], "source": "benzinga"})
    assert n.id == "alp-42" and n.headline == "Big news" and n.symbols == ["AAPL"]


def test_parse_snapshot():
    q, mt = parse_snapshot("AAPL", {
        "latestTrade": {"p": 101.5, "s": 100, "t": "2026-11-23T15:00:01.5Z"},
        "latestQuote": {"bp": 101.4, "ap": 101.6},
        "minuteBar": {"v": 5000, "t": "2026-11-23T15:00:00Z"},
        "dailyBar": {"c": 101.5, "v": 1e6, "h": 102, "l": 99},
        "prevDailyBar": {"c": 100.0, "v": 2e6},
    })
    assert q.pct == pytest.approx(1.5)
    assert minute_key(mt) == "202611231500"


def test_parse_finnhub_and_fmp():
    n = parse_finnhub({"id": 7, "headline": "x", "datetime": 1700000000, "related": "AAPL,MSFT"})
    assert n.symbols == ["AAPL", "MSFT"]
    ev = parse_fmp({"firstName": "Nancy", "lastName": "Pelosi", "symbol": "nvda", "type": "Purchase",
                    "amount": "$1,000,001 - $5,000,000", "transactionDate": "2026-10-01",
                    "disclosureDate": "2026-11-10"}, "H")
    assert ev.person == "Nancy Pelosi (H)" and ev.symbol == "NVDA" and ev.lag_days == 40


# store / stream --------------------------------------------------------------

def test_news_dedupe_and_order():
    s = Store()
    now = utcnow()
    assert s.add_news(NewsItem("a", "one", "X", now - timedelta(seconds=5)))
    assert not s.add_news(NewsItem("a", "one", "X", now - timedelta(seconds=5)))
    s.add_news(NewsItem("b", "two", "X", now))
    s.add_news(NewsItem("c", "late arrival", "X", now - timedelta(seconds=10)))
    assert [n.id for n in s.news] == ["b", "a", "c"]


def test_minute_bar_counted_once():
    s = Store()
    assert s.record_minute("AAPL", "202611231500", 100)
    assert not s.record_minute("AAPL", "202611231500", 100)
    assert list(s.minute_vols["AAPL"]) == [100]


def test_out_of_order_trade_does_not_rewind_price():
    s = Store()
    t0 = utcnow()
    s.update_quote(Quote("AAPL", 100.0, 99.0, updated_at=t0))
    s.apply_trade("AAPL", 101.0, 10, t0 + timedelta(seconds=1))
    s.apply_trade("AAPL", 99.5, 10, t0 + timedelta(milliseconds=500))  # late packet
    assert s.quotes["AAPL"].price == 101.0
    assert s.quotes["AAPL"].day_volume == 20


def test_trade_stream_message(env):
    s = Store()
    s.update_quote(Quote("AAPL", 100.0, 99.0, updated_at=utcnow() - timedelta(seconds=5)))
    ts = TradeStream(load_settings(env_file=None), s)
    ts.handle({"T": "t", "S": "AAPL", "p": 100.25, "s": 3, "t": utcnow().isoformat()})
    assert s.quotes["AAPL"].price == 100.25
    assert s.feeds["QUOTES"].data_age_s < 2


def test_people_news_mentions():
    s = Store()
    p = PeopleTracker(s, ["Cramer", "Pelosi"])
    s.add_news(NewsItem("1", "Jim Cramer says buy NVDA", "BZ", utcnow(), ["NVDA"]))
    s.add_news(NewsItem("2", "Cramerica is not a person", "BZ", utcnow()))
    assert p.scan_news() == 1
    assert p.events[0].person == "Cramer" and p.events[0].symbol == "NVDA"


# engine ----------------------------------------------------------------------

def test_session_fraction():
    open_plus_39 = datetime(2026, 11, 23, 15, 9, tzinfo=timezone.utc)  # 10:09 NY, Monday
    assert engine.session_fraction(open_plus_39) == pytest.approx(39 / 390)
    sat = datetime(2026, 11, 21, 16, 0, tzinfo=timezone.utc)
    assert engine.session_fraction(sat) == 1.0


def test_rvol_and_spike():
    assert engine.relative_volume(1e6, 4e6, 0.25) == pytest.approx(1.0)
    assert engine.minute_spike(deque([10, 10, 10, 10, 10, 50])) == pytest.approx(5.0)
    assert engine.minute_spike([10, 50]) == 0.0


def test_heat_prefers_news_and_volume():
    quiet = engine.Mover("A", 10, 6.0, 1.0, 0.0, 0)
    loud = engine.Mover("B", 10, 3.0, 4.0, 0.0, 2)
    assert engine.heat_score(loud) > engine.heat_score(quiet)


def test_rankings():
    s = Store()
    s.update_quote(Quote("UP", 110, 100, day_volume=5e6, prev_volume=1e6))
    s.update_quote(Quote("DN", 90, 100, day_volume=1e6, prev_volume=1e6))
    ms = engine.build_movers(s, datetime(2026, 11, 21, 16, 0, tzinfo=timezone.utc))
    assert [m.symbol for m in engine.top_gainers(ms)] == ["UP"]
    assert [m.symbol for m in engine.top_losers(ms)] == ["DN"]
    assert [m.symbol for m in engine.volume_spikes(ms)] == ["UP"]


# rate limiter ----------------------------------------------------------------

def test_rate_limiter_window():
    t = [0.0]
    rl = RateLimiter(3, clock=lambda: t[0])
    assert rl.try_acquire() and rl.try_acquire() and rl.try_acquire()
    assert not rl.try_acquire()
    t[0] = 60.01
    assert rl.try_acquire()
    assert rl.used() == 1
