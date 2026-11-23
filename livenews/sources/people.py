"""Track what well-known traders are doing.

Two very different kinds of signal, and the panel labels them separately:

1. Congressional trades (Pelosi, Tuberville, etc). These come from STOCK Act
   periodic transaction reports. The law gives members up to 45 days after a
   trade to file, so this is never live. What's "new" is the filing, not the
   trade. Each row shows both dates so you can see the gap.
   Source: Financial Modeling Prep /stable/senate-latest and /stable/house-latest
   (needs FMP_API_KEY; I couldn't confirm which FMP plan includes these, so it
   may need a paid tier). Polled every 15 minutes by default to stay inside
   the free tier's 250 calls/day.

2. Commentators (Cramer etc). There is no clean free feed of TV stock picks,
   so we watch the news wire for headlines and summaries that mention the
   people you track. Benzinga publishes a steady stream of "Jim Cramer says..."
   pieces, so this catches most of what he says on air, usually within the
   hour. It costs no extra API calls.
"""
from __future__ import annotations

import asyncio
import re
import time
from dataclasses import dataclass
from datetime import date, datetime, timezone

import httpx

from ..models import NewsItem
from ..store import Store

from ..config import DEFAULT_PEOPLE  # noqa: E402,F401


@dataclass
class PeopleEvent:
    kind: str  # "TRADE" (disclosure) or "SAYS" (news mention)
    person: str
    symbol: str
    action: str  # Purchase / Sale / headline text
    amount: str
    traded: date | None
    filed: datetime
    source: str
    url: str = ""
    key: str = ""

    @property
    def lag_days(self) -> int | None:
        if self.traded is None:
            return None
        return (self.filed.date() - self.traded).days


def _d(raw: str | None) -> date | None:
    if not raw:
        return None
    try:
        return date.fromisoformat(raw[:10])
    except ValueError:
        return None


def parse_fmp(raw: dict, chamber: str) -> PeopleEvent | None:
    first = (raw.get("firstName") or "").strip()
    last = (raw.get("lastName") or "").strip()
    name = f"{first} {last}".strip() or (raw.get("office") or raw.get("representative") or "").strip()
    sym = (raw.get("symbol") or raw.get("ticker") or "").strip().upper()
    filed = _d(raw.get("disclosureDate"))
    if not name or not filed:
        return None
    traded = _d(raw.get("transactionDate"))
    action = (raw.get("type") or "").strip()
    amount = (raw.get("amount") or "").strip()
    return PeopleEvent(
        kind="TRADE", person=f"{name} ({chamber})", symbol=sym or "-",
        action=action, amount=amount, traded=traded,
        filed=datetime(filed.year, filed.month, filed.day, tzinfo=timezone.utc),
        source="FMP/" + ("Senate eFD" if chamber == "S" else "House Clerk"),
        url=raw.get("link") or "",
        key=f"{chamber}|{name}|{sym}|{traded}|{action}|{amount}|{filed}",
    )


class PeopleTracker:
    def __init__(self, store: Store, people: list[str], fmp_key: str = "", interval: float = 900.0):
        self.store = store
        self.people = [p.strip() for p in people if p.strip()]
        self._patterns = [(p, re.compile(rf"\b{re.escape(p)}\b", re.I)) for p in self.people]
        self.fmp_key = fmp_key
        self.interval = max(interval, 60.0)
        self.events: list[PeopleEvent] = []
        self._keys: set[str] = set()
        self._seen_news_version = -1
        self.client = httpx.AsyncClient(base_url="https://financialmodelingprep.com", timeout=15.0)
        store.feed(
            "PEOPLE",
            "FMP disclosures + news mentions" if fmp_key else "news mentions only",
            "filings lag trades by up to 45 days" if fmp_key else "set FMP_API_KEY for Congress trades",
        )

    def matches(self, text: str) -> list[str]:
        return [p for p, rx in self._patterns if rx.search(text)]

    def _add(self, ev: PeopleEvent) -> bool:
        if ev.key in self._keys:
            return False
        self._keys.add(ev.key)
        self.events.append(ev)
        self.events.sort(key=lambda e: e.filed, reverse=True)
        del self.events[300:]
        return True

    def tracked(self, ev: PeopleEvent) -> bool:
        return bool(self.matches(ev.person))

    # news mentions: cheap, runs on every UI refresh
    def scan_news(self) -> int:
        if self.store.news_version == self._seen_news_version:
            return 0
        self._seen_news_version = self.store.news_version
        added = 0
        for n in list(self.store.news)[:200]:
            added += self._scan_item(n)
        return added

    def _scan_item(self, n: NewsItem) -> int:
        hits = self.matches(n.headline + " " + n.summary)
        added = 0
        for person in hits:
            ev = PeopleEvent(
                kind="SAYS", person=person, symbol=",".join(n.symbols[:3]) or "-",
                action=n.headline, amount="", traded=None, filed=n.created_at,
                source=n.source, url=n.url, key=f"news|{n.id}|{person}",
            )
            added += self._add(ev)
        return added

    # congressional disclosures
    async def poll_fmp_once(self) -> int:
        added = 0
        t0 = time.perf_counter()
        for path, chamber in (("/stable/senate-latest", "S"), ("/stable/house-latest", "H")):
            r = await self.client.get(path, params={"page": 0, "limit": 100, "apikey": self.fmp_key})
            if r.status_code in (401, 402, 403):
                raise RuntimeError(f"FMP {r.status_code}: plan may not include {path}")
            r.raise_for_status()
            rows = r.json()
            if not isinstance(rows, list):
                raise RuntimeError(f"FMP unexpected reply: {str(rows)[:80]}")
            for raw in rows:
                ev = parse_fmp(raw, chamber)
                if ev and self.tracked(ev):
                    added += self._add(ev)
        self.store.feed_ok("PEOPLE", (time.perf_counter() - t0) * 1000)
        return added

    async def run(self) -> None:
        if not self.fmp_key:
            return
        while True:
            try:
                n = await self.poll_fmp_once()
                if n:
                    self.store.note(f"PEOPLE: {n} new disclosure(s)")
            except asyncio.CancelledError:
                raise
            except Exception as e:
                self.store.feed_err("PEOPLE", str(e)[:120])
            await asyncio.sleep(self.interval)

    async def aclose(self) -> None:
        await self.client.aclose()
