"""SEC EDGAR: company facts (XBRL financials) and filing lists. Free, no key,
but the SEC requires a User-Agent naming you and an email, and asks for no
more than 10 requests a second. Set LIVENEWS_SEC_USER_AGENT="Your Name you@example.com".
"""
from __future__ import annotations

from datetime import date

FLOW = "flow"
INSTANT = "instant"

# label, concept fallbacks (first hit per period wins), unit, kind, format
CONCEPTS = [
    ("Revenue", ["RevenueFromContractWithCustomerExcludingAssessedTax", "Revenues", "SalesRevenueNet",
                 "RevenueFromContractWithCustomerIncludingAssessedTax"], "USD", FLOW, "usd"),
    ("Gross profit", ["GrossProfit"], "USD", FLOW, "usd"),
    ("Operating income", ["OperatingIncomeLoss"], "USD", FLOW, "usd"),
    ("Net income", ["NetIncomeLoss", "ProfitLoss"], "USD", FLOW, "usd"),
    ("EPS diluted", ["EarningsPerShareDiluted", "EarningsPerShareBasic"], "USD/shares", FLOW, "eps"),
    ("Operating cash flow", ["NetCashProvidedByUsedInOperatingActivities"], "USD", FLOW, "usd"),
    ("Capex", ["PaymentsToAcquirePropertyPlantAndEquipment"], "USD", FLOW, "usd"),
    ("Cash", ["CashAndCashEquivalentsAtCarryingValue",
              "CashCashEquivalentsRestrictedCashAndRestrictedCashEquivalents"], "USD", INSTANT, "usd"),
    ("Total assets", ["Assets"], "USD", INSTANT, "usd"),
    ("Total liabilities", ["Liabilities"], "USD", INSTANT, "usd"),
    ("Long-term debt", ["LongTermDebtNoncurrent", "LongTermDebt"], "USD", INSTANT, "usd"),
    ("Shareholders' equity", ["StockholdersEquity",
                              "StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest"], "USD", INSTANT, "usd"),
]


def _days(a: str, b: str) -> int:
    return (date.fromisoformat(b) - date.fromisoformat(a)).days


def _series(facts: dict, concept: str, unit: str, kind: str, period: str) -> dict[str, float]:
    """end date -> value, for one concept, keeping the most recently filed figure."""
    node = facts.get("facts", {}).get("us-gaap", {}).get(concept, {})
    rows = node.get("units", {}).get(unit, [])
    best: dict[str, tuple[str, float]] = {}
    for r in rows:
        end, val, filed, form = r.get("end"), r.get("val"), r.get("filed", ""), r.get("form", "")
        if end is None or val is None:
            continue
        if kind == FLOW:
            start = r.get("start")
            if not start:
                continue
            d = _days(start, end)
            if period == "A" and not (350 <= d <= 380):
                continue
            if period == "Q" and not (80 <= d <= 100):
                continue
        else:
            if period == "A" and not (form.startswith("10-K") or form.startswith("20-F") or r.get("fp") == "FY"):
                continue
        if not form.startswith(("10-K", "10-Q", "20-F", "40-F")):
            continue
        prev = best.get(end)
        if prev is None or filed > prev[0]:
            best[end] = (filed, float(val))
    return {k: v for k, (_, v) in best.items()}


def extract_financials(facts: dict, period: str = "A", n: int = 5):
    """Returns (period end dates oldest->newest, rows). Rows are (label, values, fmt)."""
    per_label: list[tuple[str, dict[str, float], str]] = []
    for label, concepts, unit, kind, fmt in CONCEPTS:
        merged: dict[str, float] = {}
        for c in concepts:
            for end, v in _series(facts, c, unit, kind, period).items():
                merged.setdefault(end, v)
        per_label.append((label, merged, fmt))

    # periods = the most recent n ends that have revenue or net income
    anchor = set(per_label[0][1]) | set(per_label[3][1])
    ends = sorted(anchor)[-n:]
    if not ends:
        return [], []
    rows = []
    vals = {label: [m.get(e) for e in ends] for label, m, _ in per_label}
    for label, m, fmt in per_label:
        rows.append((label, vals[label], fmt))
    # derived lines
    ocf, capex = vals["Operating cash flow"], vals["Capex"]
    rows.insert(7, ("Free cash flow", [o - c if o is not None and c is not None else None for o, c in zip(ocf, capex)], "usd"))
    rev = vals["Revenue"]
    for label, src in (("Gross margin", "Gross profit"), ("Operating margin", "Operating income"), ("Net margin", "Net income")):
        rows.append((label, [s / r * 100 if s is not None and r else None for s, r in zip(vals[src], rev)], "pct"))
    growth = [None] + [(b / a - 1) * 100 if a and b is not None else None for a, b in zip(rev, rev[1:])]
    rows.append(("Revenue growth", growth, "pct"))
    return ends, rows


def shares_outstanding(facts: dict) -> float | None:
    rows = facts.get("facts", {}).get("dei", {}).get("EntityCommonStockSharesOutstanding", {}).get("units", {}).get("shares", [])
    if not rows:
        return None
    latest = max(rows, key=lambda r: (r.get("end", ""), r.get("filed", "")))
    return float(latest["val"])


def recent_filings(sub: dict, cik: str, limit: int = 12, forms=("10-K", "10-Q", "8-K", "4", "S-1", "DEF 14A", "13D", "SC 13D", "SC 13G")):
    r = sub.get("filings", {}).get("recent", {})
    out = []
    for form, dt, acc, doc in zip(r.get("form", []), r.get("filingDate", []), r.get("accessionNumber", []), r.get("primaryDocument", [])):
        if form not in forms:
            continue
        url = f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/{acc.replace('-', '')}/{doc}"
        out.append({"form": form, "date": dt, "url": url})
        if len(out) >= limit:
            break
    return out
