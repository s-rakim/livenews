"""OCC option symbols and Black-Scholes. The real chain comes with greeks
from Alpaca; this is for parsing symbols and for mock mode."""
from __future__ import annotations

import math
import re
from dataclasses import dataclass
from datetime import date

_OCC = re.compile(r"^([A-Z.]{1,6})(\d{2})(\d{2})(\d{2})([CP])(\d{8})$")


@dataclass
class Contract:
    symbol: str
    root: str
    expiry: date
    right: str  # "C" or "P"
    strike: float


def parse_occ(sym: str) -> Contract | None:
    m = _OCC.match(sym.strip().upper())
    if not m:
        return None
    root, yy, mm, dd, right, k = m.groups()
    return Contract(sym, root, date(2000 + int(yy), int(mm), int(dd)), right, int(k) / 1000)


def occ_symbol(root: str, expiry: date, right: str, strike: float) -> str:
    return f"{root}{expiry:%y%m%d}{right}{int(round(strike * 1000)):08d}"


def _ncdf(x: float) -> float:
    return 0.5 * (1 + math.erf(x / math.sqrt(2)))


def _npdf(x: float) -> float:
    return math.exp(-x * x / 2) / math.sqrt(2 * math.pi)


def black_scholes(s: float, k: float, t: float, r: float, iv: float, right: str) -> dict:
    """Price and greeks. t in years. theta per calendar day, vega per 1 vol point."""
    if t <= 0 or iv <= 0:
        intrinsic = max(0.0, s - k) if right == "C" else max(0.0, k - s)
        return {"price": intrinsic, "delta": (1.0 if s > k else 0.0) if right == "C" else (-1.0 if s < k else 0.0),
                "gamma": 0.0, "theta": 0.0, "vega": 0.0}
    st = iv * math.sqrt(t)
    d1 = (math.log(s / k) + (r + iv * iv / 2) * t) / st
    d2 = d1 - st
    if right == "C":
        price = s * _ncdf(d1) - k * math.exp(-r * t) * _ncdf(d2)
        delta = _ncdf(d1)
        theta = -s * _npdf(d1) * iv / (2 * math.sqrt(t)) - r * k * math.exp(-r * t) * _ncdf(d2)
    else:
        price = k * math.exp(-r * t) * _ncdf(-d2) - s * _ncdf(-d1)
        delta = _ncdf(d1) - 1
        theta = -s * _npdf(d1) * iv / (2 * math.sqrt(t)) + r * k * math.exp(-r * t) * _ncdf(-d2)
    return {"price": price, "delta": delta, "gamma": _npdf(d1) / (s * st),
            "theta": theta / 365, "vega": s * _npdf(d1) * math.sqrt(t) / 100}
