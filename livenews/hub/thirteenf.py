"""Institutional holders from the SEC's quarterly Form 13F data sets.

Every fund manager with over $100M in US equities files a 13F each quarter,
listing its holdings. The SEC publishes all of them as one zip per quarter
(free, no key, ~100MB). The first OWN lookup downloads the newest zip once,
indexes it into a local SQLite file, and every lookup after that is instant.

Limits worth knowing: 13Fs are filed up to 45 days after quarter end, so the
newest data is 1.5 to 4.5 months old. Short positions aren't reported. We use
original 13F-HR filings and skip amendments to avoid double counting.
"""
from __future__ import annotations

import csv
import io
import re
import sqlite3
import zipfile
from datetime import datetime
from pathlib import Path

INDEX_PAGE = "https://www.sec.gov/data-research/sec-markets-data/form-13f-data-sets"
_ZIP_RX = re.compile(r'href="([^"]*?form13f\.zip)"', re.I)
_RANGE_RX = re.compile(r"(\d{2}[a-z]{3}\d{4})-(\d{2}[a-z]{3}\d{4})_form13f\.zip", re.I)
_QTR_RX = re.compile(r"(\d{4})q([1-4])_form13f\.zip", re.I)

SUFFIXES = {"INC", "INCORPORATED", "CORP", "CORPORATION", "CO", "COMPANY", "LTD", "LIMITED", "PLC", "LLC", "LP",
            "HOLDINGS", "HLDGS", "HOLDING", "GROUP", "GRP", "SA", "NV", "AG", "SE", "THE", "DEL", "NEW", "COM", "CL", "A"}


def normalize_issuer(name: str) -> str:
    words = re.sub(r"[^A-Z0-9 ]", " ", name.upper()).split()
    while words and words[-1] in SUFFIXES:
        words.pop()
    if words and words[0] == "THE":
        words = words[1:]
    return " ".join(words)


def zip_links(html: str, base: str = "https://www.sec.gov") -> list[str]:
    out = []
    for href in _ZIP_RX.findall(html):
        out.append(href if href.startswith("http") else base + href)
    return list(dict.fromkeys(out))


def link_end_date(url: str) -> datetime | None:
    m = _RANGE_RX.search(url)
    if m:
        return datetime.strptime(m.group(2).lower(), "%d%b%Y")
    m = _QTR_RX.search(url)
    if m:
        y, q = int(m.group(1)), int(m.group(2))
        return datetime(y, q * 3, 28)
    return None


def newest_link(html: str) -> str | None:
    links = [(link_end_date(u), u) for u in zip_links(html)]
    links = [x for x in links if x[0]]
    return max(links)[1] if links else None


def _member(z: zipfile.ZipFile, name: str) -> str | None:
    for n in z.namelist():
        if Path(n).stem.upper() == name:
            return n
    return None


def _rows(z: zipfile.ZipFile, name: str):
    m = _member(z, name)
    if not m:
        raise ValueError(f"{name} not found in 13F zip")
    with z.open(m) as f:
        reader = csv.DictReader(io.TextIOWrapper(f, encoding="utf-8", errors="replace"), delimiter="\t")
        yield from reader


def _num(x) -> float:
    try:
        return float(x)
    except (TypeError, ValueError):
        return 0.0


def build_index(zip_path: Path, db_path: Path, progress=None) -> None:
    """Zip -> SQLite. Runs once per quarter, takes a minute or so."""
    tmp = db_path.with_suffix(".building")
    if tmp.exists():
        tmp.unlink()
    con = sqlite3.connect(tmp)
    con.executescript("""
        CREATE TABLE filings(accession TEXT PRIMARY KEY, manager TEXT, period TEXT, filed TEXT);
        CREATE TABLE holdings(accession TEXT, issuer TEXT, issuer_norm TEXT, title TEXT, cusip TEXT,
                              value REAL, shares REAL, sh_type TEXT, putcall TEXT);
    """)
    with zipfile.ZipFile(zip_path) as z:
        keep: dict[str, dict] = {}
        for r in _rows(z, "SUBMISSION"):
            if (r.get("SUBMISSIONTYPE") or "").upper() == "13F-HR":
                keep[r["ACCESSION_NUMBER"]] = {"filed": r.get("FILING_DATE", ""), "period": r.get("PERIODOFREPORT", "")}
        for r in _rows(z, "COVERPAGE"):
            acc = r.get("ACCESSION_NUMBER")
            if acc in keep:
                keep[acc]["manager"] = r.get("FILINGMANAGER_NAME", "")
                keep[acc]["period"] = r.get("REPORTCALENDARORQUARTER") or keep[acc]["period"]
        con.executemany("INSERT INTO filings VALUES (?,?,?,?)",
                        [(a, v.get("manager", ""), v.get("period", ""), v.get("filed", "")) for a, v in keep.items()])
        batch, n = [], 0
        for r in _rows(z, "INFOTABLE"):
            acc = r.get("ACCESSION_NUMBER")
            if acc not in keep:
                continue
            issuer = r.get("NAMEOFISSUER", "")
            batch.append((acc, issuer, normalize_issuer(issuer), r.get("TITLEOFCLASS", ""), (r.get("CUSIP") or "").upper(),
                          _num(r.get("VALUE")), _num(r.get("SSHPRNAMT")), r.get("SSHPRNAMTTYPE", ""),
                          (r.get("PUTCALL") or "").strip()))
            if len(batch) >= 50000:
                con.executemany("INSERT INTO holdings VALUES (?,?,?,?,?,?,?,?,?)", batch)
                n += len(batch)
                batch.clear()
                if progress:
                    progress(f"indexed {n:,} holdings")
        if batch:
            con.executemany("INSERT INTO holdings VALUES (?,?,?,?,?,?,?,?,?)", batch)
    con.executescript("CREATE INDEX ix_cusip ON holdings(cusip); CREATE INDEX ix_norm ON holdings(issuer_norm);")
    con.commit()
    con.close()
    tmp.replace(db_path)


def find_cusip(con: sqlite3.Connection, company_title: str) -> tuple[str, str] | None:
    """Ticker's SEC company name -> the CUSIP 13F filers use for its common stock."""
    norm = normalize_issuer(company_title)
    if not norm:
        return None
    q = """SELECT cusip, MAX(issuer), SUM(value) v FROM holdings
           WHERE issuer_norm = ? AND putcall = '' AND sh_type = 'SH' GROUP BY cusip ORDER BY v DESC LIMIT 1"""
    row = con.execute(q, (norm,)).fetchone()
    if not row:
        row = con.execute(q.replace("issuer_norm = ?", "issuer_norm LIKE ?"), (norm + " %",)).fetchone()
    return (row[0], row[1]) if row else None


def holders(db_path: Path, company_title: str, limit: int = 40) -> dict:
    con = sqlite3.connect(db_path)
    try:
        hit = find_cusip(con, company_title)
        if not hit:
            return {"rows": [], "cusip": "", "issuer": "", "total_value": 0, "total_shares": 0, "count": 0, "period": ""}
        cusip, issuer = hit
        tot = con.execute("""SELECT COUNT(DISTINCT accession), SUM(value), SUM(shares) FROM holdings
                             WHERE cusip = ? AND putcall = '' AND sh_type = 'SH'""", (cusip,)).fetchone()
        rows = con.execute("""SELECT f.manager, SUM(h.shares), SUM(h.value), MAX(f.period), MAX(f.filed)
                              FROM holdings h JOIN filings f ON f.accession = h.accession
                              WHERE h.cusip = ? AND h.putcall = '' AND h.sh_type = 'SH'
                              GROUP BY h.accession ORDER BY SUM(h.value) DESC LIMIT ?""", (cusip, limit)).fetchall()
        period = con.execute("SELECT MAX(period) FROM filings").fetchone()[0] or ""
        return {
            "rows": [{"manager": r[0], "shares": r[1], "value": r[2], "period": r[3], "filed": r[4]} for r in rows],
            "cusip": cusip, "issuer": issuer, "count": tot[0] or 0, "total_value": tot[1] or 0,
            "total_shares": tot[2] or 0, "period": period,
        }
    finally:
        con.close()
