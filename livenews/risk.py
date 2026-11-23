"""Portfolio math. Daily close series in, numbers out. No numpy needed."""
from __future__ import annotations

import math
from dataclasses import dataclass, field

TRADING_DAYS = 252
Z95 = 1.6449


def returns(closes: list[float]) -> list[float]:
    return [closes[i] / closes[i - 1] - 1 for i in range(1, len(closes)) if closes[i - 1]]


def mean(xs):
    return sum(xs) / len(xs) if xs else 0.0


def stdev(xs):
    if len(xs) < 2:
        return 0.0
    m = mean(xs)
    return math.sqrt(sum((x - m) ** 2 for x in xs) / (len(xs) - 1))


def cov(a, b):
    n = min(len(a), len(b))
    if n < 2:
        return 0.0
    a, b = a[-n:], b[-n:]
    ma, mb = mean(a), mean(b)
    return sum((x - ma) * (y - mb) for x, y in zip(a, b)) / (n - 1)


def beta(asset: list[float], bench: list[float]) -> float:
    v = cov(bench, bench)
    return cov(asset, bench) / v if v else 0.0


def max_drawdown(closes: list[float]) -> float:
    peak, dd = -math.inf, 0.0
    for c in closes:
        peak = max(peak, c)
        if peak > 0:
            dd = min(dd, c / peak - 1)
    return dd


@dataclass
class Holding:
    symbol: str
    qty: float
    price: float
    avg: float
    prev_close: float
    closes: list[float]

    @property
    def value(self) -> float:
        return self.qty * self.price

    @property
    def unrealized(self) -> float:
        return (self.price - self.avg) * self.qty

    @property
    def day_pl(self) -> float:
        return (self.price - self.prev_close) * self.qty if self.prev_close else 0.0


@dataclass
class RiskReport:
    gross: float
    net: float
    unrealized: float
    day_pl: float
    beta: float
    vol_annual: float  # portfolio, as a fraction
    var95_param: float  # dollars, one day
    var95_hist: float
    max_weight: tuple[str, float]
    hhi: float
    per_symbol: list[dict] = field(default_factory=list)
    days: int = 0


def portfolio_risk(holdings: list[Holding], bench_closes: list[float]) -> RiskReport:
    gross = sum(abs(h.value) for h in holdings)
    net = sum(h.value for h in holdings)
    bench_r = returns(bench_closes)
    n = min([len(bench_r)] + [len(returns(h.closes)) for h in holdings]) if holdings else 0
    per = []
    port_r = [0.0] * n
    for h in holdings:
        r = returns(h.closes)[-n:] if n else []
        w = h.value / gross if gross else 0.0
        for i, x in enumerate(r):
            port_r[i] += w * x
        b = beta(r, bench_r[-n:]) if n > 1 else 0.0
        per.append({
            "symbol": h.symbol, "value": h.value, "weight": w, "beta": b,
            "vol": stdev(r) * math.sqrt(TRADING_DAYS), "unrealized": h.unrealized,
            "day_pl": h.day_pl, "max_dd": max_drawdown(h.closes),
        })
    sd = stdev(port_r)
    hist = sorted(port_r)
    var_hist = -hist[int(0.05 * len(hist))] * gross if len(hist) >= 20 else 0.0
    weights = [(p["symbol"], abs(p["weight"])) for p in per]
    return RiskReport(
        gross=gross, net=net,
        unrealized=sum(h.unrealized for h in holdings),
        day_pl=sum(h.day_pl for h in holdings),
        beta=sum(p["beta"] * p["weight"] for p in per),
        vol_annual=sd * math.sqrt(TRADING_DAYS),
        var95_param=Z95 * sd * gross,
        var95_hist=max(var_hist, 0.0),
        max_weight=max(weights, key=lambda t: t[1]) if weights else ("-", 0.0),
        hhi=sum(w * w for _, w in weights),
        per_symbol=sorted(per, key=lambda p: -abs(p["value"])),
        days=n,
    )
