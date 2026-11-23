"""Ranks what's moving. Pure functions over the Store so they're easy to test."""
from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime, time, timedelta, timezone

from .store import Store

try:
    from zoneinfo import ZoneInfo

    NY = ZoneInfo("America/New_York")
except Exception:  # pragma: no cover
    NY = timezone(timedelta(hours=-5))

SESSION_OPEN = time(9, 30)
SESSION_MINUTES = 390.0


@dataclass
class Mover:
    symbol: str
    price: float
    pct: float
    rvol: float  # day volume vs expected pace from yesterday
    spike: float  # latest minute volume vs recent average minute volume
    news: int  # headlines tagged with this symbol in the last hour
    score: float = 0.0


def session_fraction(now: datetime | None = None) -> float:
    """How far through the regular session we are, 0..1. Outside hours = 1."""
    now = (now or datetime.now(timezone.utc)).astimezone(NY)
    if now.weekday() >= 5:
        return 1.0
    opened = now.replace(hour=SESSION_OPEN.hour, minute=SESSION_OPEN.minute, second=0, microsecond=0)
    mins = (now - opened).total_seconds() / 60.0
    if mins <= 0 or mins >= SESSION_MINUTES:
        return 1.0
    # floor so the first few minutes don't blow rvol up to silly numbers
    return max(mins / SESSION_MINUTES, 15 / SESSION_MINUTES)


def relative_volume(day_vol: float, prev_vol: float, frac: float) -> float:
    if prev_vol <= 0:
        return 0.0
    return day_vol / (prev_vol * frac)


def minute_spike(history) -> float:
    vals = list(history)
    if len(vals) < 6:
        return 0.0
    *base, last = vals
    base = base[-20:]
    avg = sum(base) / len(base)
    return last / avg if avg > 0 else 0.0


def build_movers(store: Store, now: datetime | None = None) -> list[Mover]:
    frac = session_fraction(now)
    counts = store.news_counts(60)
    out = []
    for sym, q in store.quotes.items():
        if not q.prev_close:
            continue
        m = Mover(
            symbol=sym,
            price=q.price,
            pct=q.pct,
            rvol=relative_volume(q.day_volume, q.prev_volume, frac),
            spike=minute_spike(store.minute_vols.get(sym, ())),
            news=counts.get(sym, 0),
        )
        m.score = heat_score(m)
        out.append(m)
    return out


def heat_score(m: Mover) -> float:
    """One number for "look at this now".

    Size of the move, boosted by unusual volume and by fresh news. A 3% move
    on 4x volume with two headlines beats a 6% move nobody is trading.
    """
    vol_boost = 1.0 + math.log2(max(m.rvol, 1.0)) + 0.5 * math.log2(max(m.spike, 1.0))
    news_boost = 1.0 + 0.6 * math.log2(1 + m.news)
    return abs(m.pct) * vol_boost * news_boost


def top_gainers(movers: list[Mover], n: int = 15) -> list[Mover]:
    return sorted((m for m in movers if m.pct > 0), key=lambda m: m.pct, reverse=True)[:n]


def top_losers(movers: list[Mover], n: int = 15) -> list[Mover]:
    return sorted((m for m in movers if m.pct < 0), key=lambda m: m.pct)[:n]


def volume_spikes(movers: list[Mover], n: int = 15, min_rvol: float = 1.5) -> list[Mover]:
    hits = [m for m in movers if m.rvol >= min_rvol or m.spike >= 3.0]
    return sorted(hits, key=lambda m: max(m.rvol, m.spike), reverse=True)[:n]


def hottest(movers: list[Mover], n: int = 15) -> list[Mover]:
    return sorted(movers, key=lambda m: m.score, reverse=True)[:n]
