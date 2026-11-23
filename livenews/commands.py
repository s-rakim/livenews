"""Command line grammar. Ticker first, then a function. Anything without a
ticker is a market-wide screen.

Security functions (ticker + code):
  AAPL            quote on the main screen (same as AAPL Q)
  AAPL N          news for AAPL only
  AAPL CH [span]  chart, span = 1D 5D 1M 3M 6M 1Y 5Y (default 6M)
  AAPL DES        company profile and recent SEC filings
  AAPL FA [Q]     financial statements, annual (or quarterly)
  AAPL EST        analyst ratings, EPS surprises, key stats
  AAPL INS        insider trades (Form 4)
  AAPL OPT [n]    options chain, n = expiry number or YYYY-MM-DD
  BTC/USD CH      crypto works anywhere a ticker does

Market screens:
  MAIN            back to the main screen (Esc does the same)
  MKTS            indices, rates, FX, commodities, crypto (FX RATES CMDTY CRYPTO also work)
  ECO [days]      economic calendar + earnings for your watchlist
  PORT            portfolio P&L and risk
  ORD             orders and executions blotter
  N               all news
  PPL [name]      congressional trades and commentator mentions

Watchlists and alerts:
  W               show watchlist        W ADD NVDA AMD   W DEL AMD
  W USE tech      switch to (or create) the list "tech"
  ALRT            list alerts           ALRT DEL 3
  ALRT NVDA > 150     ALRT NVDA < 120     ALRT NVDA MOVE 5
  ALRT NEWS FDA       ALRT NEWS GUIDANCE NVDA

Orders (a ticket always opens for confirmation):
  BUY AAPL 10                  market
  BUY AAPL 10 LMT 182.50       limit
  SELL AAPL 10 STP 175         stop
  SELL AAPL 10 STP 175 LMT 174 stop limit
  SELL AAPL 10 TRAIL 3         trailing stop, 3%
  add GTC to keep it past today (default DAY)
  CXL ALL  or  CXL <order id>
"""
from __future__ import annotations

from dataclasses import dataclass, field

SECURITY_FUNCS = {"Q": "quote", "QUOTE": "quote", "GO": "quote", "N": "news", "NEWS": "news",
                  "CH": "chart", "CHART": "chart", "G": "chart", "DES": "des", "FA": "fa", "EST": "est",
                  "INS": "ins", "OWN": "ins", "OPT": "opt", "OPTIONS": "opt"}
SPANS = ("1D", "5D", "1M", "3M", "6M", "1Y", "5Y")
# words whose own arguments can look like a function code, so "ALRT NEWS x" stays an alert
NO_ESCAPE = {"BUY", "SELL", "ALRT", "ALERT", "ALERTS", "CXL", "CANCEL", "PPL", "PEOPLE", "HELP"}
MARKET_ALIASES = {"MKTS": "", "FX": "FX", "RATES": "RATES", "CMDTY": "COMMODITIES", "CRYPTO": "CRYPTO"}


@dataclass
class Command:
    kind: str
    symbol: str = ""
    side: str = ""
    qty: float = 0.0
    limit: float | None = None
    stop: float | None = None
    trail: float | None = None
    tif: str = "day"
    arg: str = ""
    args: list[str] = field(default_factory=list)
    value: float = 0.0
    error: str = ""


def is_ticker(tok: str) -> bool:
    if "/" in tok:  # crypto pair like BTC/USD
        a, _, b = tok.partition("/")
        return a.isalpha() and b.isalpha() and 2 <= len(a) <= 6 and 3 <= len(b) <= 4
    t = tok.replace(".", "")
    return t.isalpha() and 1 <= len(t) <= 6


def _num(tok: str) -> float | None:
    try:
        return float(tok.replace(",", "").lstrip("$"))
    except ValueError:
        return None


def _order(side: str, toks: list[str]) -> Command:
    usage = f"{side.upper()} <ticker> <qty> [LMT p] [STP p] [TRAIL pct] [GTC]"
    if len(toks) < 2 or not is_ticker(toks[0]):
        return Command("error", error=usage)
    qty = _num(toks[1])
    if qty is None:
        return Command("error", error=f"bad quantity {toks[1]!r}")
    c = Command("order", symbol=toks[0], side=side, qty=qty)
    rest = toks[2:]
    i = 0
    while i < len(rest):
        t = rest[i]
        if t in ("GTC", "DAY"):
            c.tif = t.lower()
            i += 1
            continue
        if t in ("LMT", "LIMIT", "@", "STP", "STOP", "TRAIL") and i + 1 < len(rest):
            v = _num(rest[i + 1].rstrip("%"))
            if v is None:
                return Command("error", error=f"bad number after {t}: {rest[i + 1]!r}")
            if t in ("LMT", "LIMIT", "@"):
                c.limit = v
            elif t in ("STP", "STOP"):
                c.stop = v
            else:
                c.trail = v
            i += 2
            continue
        return Command("error", error=usage)
    if c.trail is not None and (c.limit is not None or c.stop is not None):
        return Command("error", error="trailing stop can't be combined with LMT or STP")
    return c


def order_type(c: Command) -> str:
    if c.trail is not None:
        return "trailing_stop"
    if c.stop is not None and c.limit is not None:
        return "stop_limit"
    if c.stop is not None:
        return "stop"
    if c.limit is not None:
        return "limit"
    return "market"


def _alert(toks: list[str]) -> Command:
    if not toks:
        return Command("alerts")
    if toks[0] == "DEL" and len(toks) == 2 and toks[1].isdigit():
        return Command("alert_del", value=float(toks[1]))
    if toks[0] == "NEWS" and len(toks) >= 2:
        sym = toks[2] if len(toks) == 3 and is_ticker(toks[2]) else ""
        kw = toks[1] if sym or len(toks) == 2 else " ".join(toks[1:])
        return Command("alert_add", arg="news", args=[kw.lower()], symbol=sym)
    if len(toks) == 3 and is_ticker(toks[0]):
        v = _num(toks[2].rstrip("%"))
        kind = {">": "above", ">=": "above", "<": "below", "<=": "below", "MOVE": "move"}.get(toks[1])
        if kind and v is not None:
            return Command("alert_add", arg=kind, symbol=toks[0], value=v)
    return Command("error", error="ALRT <ticker> > <price> | ALRT <ticker> MOVE <pct> | ALRT NEWS <word> [ticker] | ALRT DEL <id>")


def parse(line: str) -> Command:
    toks = line.strip().upper().split()
    if not toks:
        return Command("error", error="empty")
    head, rest = toks[0], toks[1:]

    # a ticker that happens to be a command word, followed by a function: "W Q"
    if rest and rest[0] in SECURITY_FUNCS and is_ticker(head) and head not in NO_ESCAPE:
        return _security(head, rest)

    if head in ("HELP", "?", "H"):
        return Command("help")
    if head in ("MAIN", "HOME", "MENU"):
        return Command("main")
    if head == "N" and not rest:
        return Command("allnews")
    if head in MARKET_ALIASES:
        return Command("mkts", arg=MARKET_ALIASES[head])
    if head == "ECO":
        days = int(rest[0]) if rest and rest[0].isdigit() else 14
        return Command("eco", value=float(min(days, 60)))
    if head in ("PORT", "PRT", "RISK"):
        return Command("port")
    if head in ("ORD", "BLOT", "FILLS"):
        return Command("blotter")
    if head in ("PPL", "PEOPLE"):
        return Command("people", arg=" ".join(rest))
    if head == "W":
        if not rest:
            return Command("watch")
        if rest[0] in ("ADD", "DEL") and len(rest) > 1 and all(is_ticker(t) for t in rest[1:]):
            return Command("watch_" + rest[0].lower(), args=rest[1:])
        if rest[0] == "USE" and len(rest) == 2:
            return Command("watch_use", arg=rest[1])
        return Command("error", error="W | W ADD <tickers> | W DEL <tickers> | W USE <name>")
    if head in ("ALRT", "ALERT", "ALERTS"):
        return _alert(rest)
    if head in ("CXL", "CANCEL"):
        if len(rest) != 1:
            return Command("error", error="CXL <order id> or CXL ALL")
        return Command("cancel", arg=rest[0])
    if head in ("BUY", "SELL", "B", "S") and rest:
        return _order("buy" if head.startswith("B") else "sell", rest)
    if is_ticker(head):
        return _security(head, rest)
    return Command("error", error=f"don't know {line.strip()!r}, type HELP")


def _security(sym: str, rest: list[str]) -> Command:
    fn = SECURITY_FUNCS.get(rest[0], None) if rest else "quote"
    if fn is None:
        return Command("error", error=f"unknown function {rest[0]!r}. Try Q N CH DES FA EST INS OPT")
    args = rest[1:]
    if fn == "chart":
        span = args[0] if args else "6M"
        if span not in SPANS:
            return Command("error", error=f"chart span is one of {' '.join(SPANS)}")
        return Command("chart", symbol=sym, arg=span)
    if fn == "fa":
        per = "Q" if args and args[0] in ("Q", "QTR", "QUARTERLY") else "A"
        return Command("fa", symbol=sym, arg=per)
    if fn == "opt":
        return Command("opt", symbol=sym, arg=args[0] if args else "")
    return Command(fn, symbol=sym)
