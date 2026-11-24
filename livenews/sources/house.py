"""Congressional stock trades straight from the House Clerk. Free, no key.

The Clerk publishes a yearly zip with an XML index of every financial
disclosure (https://disclosures-clerk.house.gov/public_disc/financial-pdfs/2026FD.zip).
Periodic Transaction Reports (FilingType P) are the stock trade filings. Each
one is a PDF; electronically filed ones have a text layer we parse for ticker,
buy/sell, dates and amount range. Paper filings scanned as images can't be
read without OCR, so those show up as a filing with a link to the PDF.

Senate filings live behind a terms-of-use click-through on efdsearch.senate.gov
and aren't scraped. Set FMP_API_KEY if you want Senate trades too.
"""
from __future__ import annotations

import asyncio
import io
import json
import re
import xml.etree.ElementTree as ET
import zipfile
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path

import httpx

BASE = "https://disclosures-clerk.house.gov/public_disc"
TX_RX = re.compile(
    r"\(([A-Z][A-Z0-9.\-]{0,6})\)\s*\[([A-Z]{2})\]\s*"
    r"(P|S \(partial\)|S|E)\s+"
    r"(\d{2}/\d{2}/\d{4})\s+(\d{2}/\d{2}/\d{4})\s+"
    r"(\$[\d,]+\s*-\s*\$[\d,]+|Over \$[\d,]+|\$[\d,]+)"
)
ACTIONS = {"P": "Purchase", "S": "Sale", "S (partial)": "Sale (partial)", "E": "Exchange"}
ASSET = {"ST": "stock", "OP": "option", "GS": "gov bond", "CS": "corp bond", "EF": "ETF", "OT": "other", "MF": "fund"}


@dataclass
class Filing:
    first: str
    last: str
    state_dst: str
    filed: date
    doc_id: str
    year: int

    @property
    def name(self) -> str:
        return f"{self.first} {self.last}".strip()

    @property
    def url(self) -> str:
        return f"{BASE}/ptr-pdfs/{self.year}/{self.doc_id}.pdf"


def parse_index(xml_bytes: bytes) -> list[Filing]:
    root = ET.fromstring(xml_bytes)
    out = []
    for m in root.iter("Member"):
        if (m.findtext("FilingType") or "").strip() != "P":
            continue
        try:
            filed = datetime.strptime((m.findtext("FilingDate") or "").strip(), "%m/%d/%Y").date()
            year = int((m.findtext("Year") or "").strip() or filed.year)
        except ValueError:
            continue
        out.append(Filing((m.findtext("First") or "").strip(), (m.findtext("Last") or "").strip(),
                          (m.findtext("StateDst") or "").strip(), filed, (m.findtext("DocID") or "").strip(), year))
    return out


def clean_text(raw: str) -> str:
    raw = raw.replace("\x00", "")
    return re.sub(r"\s+", " ", raw)


def parse_transactions(text: str) -> list[dict]:
    out = []
    for m in TX_RX.finditer(clean_text(text)):
        ticker, asset, act, tdate, _ndate, amount = m.groups()
        out.append({
            "ticker": ticker, "asset": ASSET.get(asset, asset), "action": ACTIONS.get(act, act),
            "traded": datetime.strptime(tdate, "%m/%d/%Y").date().isoformat(),
            "amount": re.sub(r"\s+", " ", amount),
        })
    return out


def pdf_text(data: bytes) -> str:
    from pypdf import PdfReader

    reader = PdfReader(io.BytesIO(data))
    return "\n".join((p.extract_text() or "") for p in reader.pages)


class HouseClerk:
    def __init__(self, cache_dir: Path, people: list[str], client: httpx.AsyncClient | None = None):
        self.dir = cache_dir
        self.dir.mkdir(parents=True, exist_ok=True)
        self.people = [p.lower() for p in people]
        self.http = client or httpx.AsyncClient(timeout=30.0, follow_redirects=True,
                                                headers={"User-Agent": "Mozilla/5.0 livenews"})

    def tracked(self, f: Filing) -> bool:
        full = f.name.lower()
        return any(p == f.last.lower() or p in full for p in self.people)

    async def index(self, year: int) -> list[Filing]:
        r = await self.http.get(f"{BASE}/financial-pdfs/{year}FD.zip")
        if r.status_code == 404:
            return []
        r.raise_for_status()
        with zipfile.ZipFile(io.BytesIO(r.content)) as z:
            name = next((n for n in z.namelist() if n.lower().endswith(".xml")), None)
            if not name:
                return []
            return parse_index(z.read(name))

    async def transactions(self, f: Filing) -> list[dict]:
        cache = self.dir / f"{f.doc_id}.json"
        if cache.exists():
            return json.loads(cache.read_text())
        r = await self.http.get(f.url)
        r.raise_for_status()
        try:
            txs = await asyncio.to_thread(lambda: parse_transactions(pdf_text(r.content)))
        except Exception:
            txs = []
        cache.write_text(json.dumps(txs))
        return txs

    async def recent(self, since_days: int = 120) -> list[tuple[Filing, list[dict]]]:
        today = datetime.now(timezone.utc).date()
        years = {today.year} | ({today.year - 1} if today.month <= 3 else set())
        filings = []
        for y in sorted(years):
            filings += await self.index(y)
        mine = [f for f in filings if self.tracked(f) and (today - f.filed).days <= since_days]
        mine.sort(key=lambda f: f.filed, reverse=True)
        out = []
        for f in mine[:60]:
            try:
                out.append((f, await self.transactions(f)))
            except Exception:
                out.append((f, []))
        return out

    async def aclose(self) -> None:
        await self.http.aclose()
