from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


@dataclass
class NewsItem:
    id: str
    headline: str
    source: str
    created_at: datetime
    symbols: list[str] = field(default_factory=list)
    summary: str = ""
    url: str = ""

    @property
    def age_seconds(self) -> float:
        return (utcnow() - self.created_at).total_seconds()


@dataclass
class Quote:
    symbol: str
    price: float
    prev_close: float
    day_volume: float = 0.0
    prev_volume: float = 0.0
    minute_volume: float = 0.0
    bid: float = 0.0
    ask: float = 0.0
    high: float = 0.0
    low: float = 0.0
    updated_at: datetime = field(default_factory=utcnow)

    @property
    def change(self) -> float:
        return self.price - self.prev_close if self.prev_close else 0.0

    @property
    def pct(self) -> float:
        return (self.change / self.prev_close * 100.0) if self.prev_close else 0.0


@dataclass
class Order:
    id: str
    symbol: str
    side: str  # buy / sell
    qty: float
    type: str  # market / limit
    status: str
    limit_price: float | None = None
    filled_avg_price: float | None = None
    submitted_at: datetime = field(default_factory=utcnow)


@dataclass
class Position:
    symbol: str
    qty: float
    avg_entry: float
    market_price: float

    @property
    def unrealized(self) -> float:
        return (self.market_price - self.avg_entry) * self.qty


@dataclass
class Account:
    equity: float
    cash: float
    buying_power: float
    day_pl: float = 0.0
