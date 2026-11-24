"""Shared in-memory state. Sources write to it, the engine and UI read it."""
from __future__ import annotations

from collections import deque
from datetime import timedelta

from dataclasses import dataclass
from datetime import datetime

from .models import Account, Execution, NewsItem, Order, Position, Quote, utcnow


@dataclass
class FeedStatus:
    """What the header shows for each feed: where data comes from and how fresh."""

    name: str
    source: str
    note: str = ""  # e.g. "IEX only, ~2-3% of US volume"
    last_ok: datetime | None = None
    rtt_ms: float = 0.0  # request round trip
    data_age_s: float | None = None  # age of the newest datapoint when received
    error: str = ""

    @property
    def since_ok(self) -> float | None:
        return (utcnow() - self.last_ok).total_seconds() if self.last_ok else None


class Store:
    def __init__(self, max_news: int = 500, minute_history: int = 30):
        self.news: deque[NewsItem] = deque(maxlen=max_news)
        self._news_ids: set[str] = set()
        self.quotes: dict[str, Quote] = {}
        # per symbol rolling list of observed minute-bar volumes
        self.minute_vols: dict[str, deque[float]] = {}
        self._minute_history = minute_history
        self._last_minute: dict[str, str] = {}
        self.provider_gainers: list[str] = []
        self.provider_losers: list[str] = []
        self.most_active: list[str] = []
        self.account: Account | None = None
        self.positions: list[Position] = []
        self.orders: list[Order] = []
        self.feeds: dict[str, FeedStatus] = {}
        self.log: deque[str] = deque(maxlen=200)
        self.executions: deque[Execution] = deque(maxlen=500)
        self.fx: dict[str, tuple[float, datetime]] = {}  # streaming FX / metals, label -> (price, time)
        self.news_version = 0

    # news -----------------------------------------------------------------
    def add_news(self, item: NewsItem) -> bool:
        if item.id in self._news_ids:
            return False
        self._news_ids.add(item.id)
        if len(self._news_ids) > 5000:
            self._news_ids = {n.id for n in self.news}
            self._news_ids.add(item.id)
        # keep newest first
        if self.news and item.created_at < self.news[0].created_at:
            items = sorted([*self.news, item], key=lambda n: n.created_at, reverse=True)
            self.news.clear()
            self.news.extend(items[: self.news.maxlen])
        else:
            self.news.appendleft(item)
        self.news_version += 1
        return True

    def news_for(self, symbol: str, minutes: float | None = None) -> list[NewsItem]:
        cutoff = utcnow() - timedelta(minutes=minutes) if minutes else None
        out = []
        for n in self.news:
            if cutoff and n.created_at < cutoff:
                break
            if symbol in n.symbols:
                out.append(n)
        return out

    def news_counts(self, minutes: float = 60) -> dict[str, int]:
        cutoff = utcnow() - timedelta(minutes=minutes)
        counts: dict[str, int] = {}
        for n in self.news:
            if n.created_at < cutoff:
                break
            for s in n.symbols:
                counts[s] = counts.get(s, 0) + 1
        return counts

    # quotes ---------------------------------------------------------------
    def update_quote(self, q: Quote) -> None:
        self.quotes[q.symbol] = q

    def record_minute(self, symbol: str, minute_key: str, volume: float) -> bool:
        """Store one completed minute bar's volume. Same bar seen twice (stream
        and snapshot both report it) is only counted once."""
        if not minute_key or volume <= 0 or self._last_minute.get(symbol) == minute_key:
            return False
        self._last_minute[symbol] = minute_key
        hist = self.minute_vols.setdefault(symbol, deque(maxlen=self._minute_history))
        hist.append(volume)
        return True

    def apply_trade(self, symbol: str, price: float, size: float, ts: datetime) -> Quote | None:
        q = self.quotes.get(symbol)
        if q is None:
            return None
        # trades can arrive out of order; never move the last price backwards in time
        if ts >= q.updated_at:
            q.price = price
            q.updated_at = ts
            if q.high and price > q.high:
                q.high = price
            if q.low and price < q.low:
                q.low = price
        q.day_volume += size
        return q

    def feed(self, name: str, source: str = "", note: str = "") -> FeedStatus:
        f = self.feeds.get(name)
        if f is None:
            f = self.feeds[name] = FeedStatus(name, source, note)
        return f

    def feed_ok(self, name: str, rtt_ms: float, data_age_s: float | None = None) -> None:
        f = self.feed(name)
        f.last_ok = utcnow()
        f.rtt_ms = rtt_ms
        if data_age_s is not None:
            f.data_age_s = data_age_s
        f.error = ""

    def feed_err(self, name: str, err: str) -> None:
        f = self.feed(name)
        if f.error != err:
            self.note(f"{name}: {err}")
        f.error = err

    def note(self, msg: str) -> None:
        self.log.appendleft(f"{utcnow():%H:%M:%S} {msg}")
