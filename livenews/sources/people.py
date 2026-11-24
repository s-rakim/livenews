"""Track what well-known traders are doing.

Two very different kinds of signal, and the panel labels them separately:

1. Congressional trades (Pelosi, Tuberville, etc). These come from STOCK Act
   periodic transaction reports. The law gives members up to 45 days after a
   trade to file, so this is never live. What's "new" is the filing, not the
   trade. Each row shows both dates so you can see the gap.
   House: read straight from the House Clerk's disclosure index and PTR PDFs
   (free, no key, see house.py), checked every 30 minutes.
   Senate: Financial Modeling Prep /stable/senate-latest, only if FMP_API_KEY
   is set (I couldn't confirm which FMP plan includes it). Polled every 15
   minutes by default to stay inside the free tier's 250 calls/day.

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
from pathlib import Path

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


def house_events(filing, txs: list[dict]) -> list[PeopleEvent]:
    """Turn one House Clerk PTR into panel rows. A filing we couldn't read
    (scanned paper form) still gets a row so you know it exists."""
    filed = datetime(filing.filed.year, filing.filed.month, filing.filed.day, tzinfo=timezone.utc)
    who = f"{filing.name} (H)"
    if not txs:
        return [PeopleEvent(kind="TRADE", person=who, symbol="-", action="PDF only",
                            amount="scanned, open link", traded=None, filed=filed,
                            source="House Clerk PTR", url=filing.url, key=f"house|{filing.doc_id}")]
    out = []
    for i, tx in enumerate(txs):
        action = tx["action"] if tx.get("asset") in (None, "stock") else f"{tx['action']} {tx['asset']}"
        out.append(PeopleEvent(kind="TRADE", person=who, symbol=tx["ticker"], action=action,
                               amount=tx["amount"], traded=_d(tx.get("traded")), filed=filed,
                               source="House Clerk PTR", url=filing.url, key=f"house|{filing.doc_id}|{i}"))
    return out


class PeopleTracker:
    def __init__(self, store: Store, people: list[str], fmp_key: str = "", interval: float = 900.0,
                 house_dir: Path | None = None, house_interval: float = 1800.0):
        self.store = store
        self.people = [p.strip() for p in people if p.strip()]
        self._patterns = [(p, re.compile(rf"\b{re.escape(p)}\b", re.I)) for p in self.people]
        self.fmp_key = fmp_key
        self.interval = max(interval, 60.0)
        self.house_interval = max(house_interval, 300.0)
        self.events: list[PeopleEvent] = []
        self._keys: set[str] = set()
        self._seen_news_version = -1
        self.client = httpx.AsyncClient(base_url="https://financialmodelingprep.com", timeout=15.0)
        self.house = None
        if house_dir is not None:
            from .house import HouseClerk

            self.house = HouseClerk(house_dir, self.people)
        parts = ["news mentions"]
        if self.house:
            parts.insert(0, "House Clerk PTRs")
        if fmp_key:
            parts.insert(0, "FMP Senate")
        note = "filings lag trades by up to 45 days"
        if not fmp_key:
            note += "; Senate needs FMP_API_KEY"
        store.feed("PEOPLE", " + ".join(parts), note)

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
    async def poll_house_once(self) -> int:
        t0 = time.perf_counter()
        added = 0
        for filing, txs in await self.house.recent():
            for ev in house_events(filing, txs):
                added += self._add(ev)
        self.store.feed_ok("PEOPLE", (time.perf_counter() - t0) * 1000)
        return added

    async def poll_fmp_once(self) -> int:
        added = 0
        t0 = time.perf_counter()
        chambers = [("/stable/senate-latest", "S")]
        if self.house is None:  # the Clerk is the primary source; FMP's House feed is a copy of it
            chambers.append(("/stable/house-latest", "H"))
        for path, chamber in chambers:
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

    async def _loop(self, poll, interval: float, label: str) -> None:
        while True:
            try:
                n = await poll()
                if n:
                    self.store.note(f"PEOPLE: {n} new {label} disclosure(s)")
            except asyncio.CancelledError:
                raise
            except Exception as e:
                self.store.feed_err("PEOPLE", f"{label}: {str(e)[:110]}")
            await asyncio.sleep(interval)

    async def run(self) -> None:
        loops = []
        if self.house is not None:
            loops.append(self._loop(self.poll_house_once, self.house_interval, "House"))
        if self.fmp_key:
            loops.append(self._loop(self.poll_fmp_once, self.interval, "Senate" if self.house else "FMP"))
        if loops:
            await asyncio.gather(*loops)

    async def aclose(self) -> None:
        await self.client.aclose()
        if self.house is not None:
            await self.house.aclose()
