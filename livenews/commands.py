"""Command line grammar. Ticker first, then a function, like a terminal:

  AAPL            quote AAPL (same as AAPL Q)
  AAPL N          news for AAPL only
  N               all news
  BUY AAPL 10     market order
  SELL AAPL 5 LMT 182.50
  CXL ALL         cancel all open orders (or CXL <order id>)
  PPL             people panel, all tracked names
  PPL PELOSI      just one person
  HELP            this list
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass
class Command:
    kind: str  # quote, news, allnews, order, cancel, people, help, error
    symbol: str = ""
    side: str = ""
    qty: float = 0.0
    limit: float | None = None
    arg: str = ""
    error: str = ""


def _is_ticker(tok: str) -> bool:
    t = tok.replace(".", "")
    return t.isalpha() and 1 <= len(t) <= 6


def parse(line: str) -> Command:
    toks = line.strip().upper().split()
    if not toks:
        return Command("error", error="empty")
    head = toks[0]
    if head in ("HELP", "?", "H"):
        return Command("help")
    if head == "N" and len(toks) == 1:
        return Command("allnews")
    if head in ("PPL", "PEOPLE"):
        return Command("people", arg=" ".join(toks[1:]))
    if head in ("CXL", "CANCEL"):
        if len(toks) != 2:
            return Command("error", error="CXL <order id> or CXL ALL")
        return Command("cancel", arg=toks[1])
    if head in ("BUY", "SELL", "B", "S"):
        side = "buy" if head.startswith("B") else "sell"
        if len(toks) < 3 or not _is_ticker(toks[1]):
            return Command("error", error=f"{head} <ticker> <qty> [LMT <price>]")
        try:
            qty = float(toks[2])
        except ValueError:
            return Command("error", error=f"bad quantity {toks[2]!r}")
        limit = None
        if len(toks) > 3:
            if toks[3] not in ("LMT", "LIMIT", "@") or len(toks) != 5:
                return Command("error", error="limit orders: ... LMT <price>")
            try:
                limit = float(toks[4])
            except ValueError:
                return Command("error", error=f"bad price {toks[4]!r}")
        return Command("order", symbol=toks[1], side=side, qty=qty, limit=limit)
    if _is_ticker(head):
        fn = toks[1] if len(toks) > 1 else "Q"
        if fn in ("Q", "QUOTE", "GO"):
            return Command("quote", symbol=head)
        if fn in ("N", "NEWS"):
            return Command("news", symbol=head)
        return Command("error", error=f"unknown function {fn!r}, try Q or N")
    return Command("error", error=f"don't know {line.strip()!r}, type HELP")
