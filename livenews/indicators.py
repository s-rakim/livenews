"""Technical indicators over plain lists. None where there isn't enough data
yet, so the output lines up index-for-index with the input bars."""
from __future__ import annotations

import math
from typing import Sequence

Series = list[float | None]


def sma(xs: Sequence[float], n: int) -> Series:
    out: Series = [None] * len(xs)
    s = 0.0
    for i, x in enumerate(xs):
        s += x
        if i >= n:
            s -= xs[i - n]
        if i >= n - 1:
            out[i] = s / n
    return out


def ema(xs: Sequence[float], n: int) -> Series:
    out: Series = [None] * len(xs)
    if len(xs) < n:
        return out
    k = 2 / (n + 1)
    prev = sum(xs[:n]) / n  # seed with the SMA, the usual convention
    out[n - 1] = prev
    for i in range(n, len(xs)):
        prev = xs[i] * k + prev * (1 - k)
        out[i] = prev
    return out


def rsi(closes: Sequence[float], n: int = 14) -> Series:
    """Wilder's RSI."""
    out: Series = [None] * len(closes)
    if len(closes) <= n:
        return out
    gains = losses = 0.0
    for i in range(1, n + 1):
        d = closes[i] - closes[i - 1]
        gains += max(d, 0)
        losses += max(-d, 0)
    ag, al = gains / n, losses / n
    out[n] = 100.0 if al == 0 else 100 - 100 / (1 + ag / al)
    for i in range(n + 1, len(closes)):
        d = closes[i] - closes[i - 1]
        ag = (ag * (n - 1) + max(d, 0)) / n
        al = (al * (n - 1) + max(-d, 0)) / n
        out[i] = 100.0 if al == 0 else 100 - 100 / (1 + ag / al)
    return out


def macd(closes: Sequence[float], fast: int = 12, slow: int = 26, signal: int = 9):
    ef, es = ema(closes, fast), ema(closes, slow)
    line: Series = [f - s if f is not None and s is not None else None for f, s in zip(ef, es)]
    start = next((i for i, v in enumerate(line) if v is not None), None)
    sig: Series = [None] * len(closes)
    if start is not None:
        tail = ema([v for v in line[start:]], signal)  # type: ignore[arg-type]
        sig[start:] = tail
    hist: Series = [m - s if m is not None and s is not None else None for m, s in zip(line, sig)]
    return line, sig, hist


def bollinger(closes: Sequence[float], n: int = 20, k: float = 2.0):
    mid = sma(closes, n)
    up: Series = [None] * len(closes)
    lo: Series = [None] * len(closes)
    for i in range(n - 1, len(closes)):
        w = closes[i - n + 1 : i + 1]
        m = mid[i]
        sd = math.sqrt(sum((x - m) ** 2 for x in w) / n)  # population sd, as most charting tools do
        up[i], lo[i] = m + k * sd, m - k * sd
    return lo, mid, up


def vwap(highs, lows, closes, vols, session_keys=None) -> Series:
    """Cumulative VWAP using typical price. Resets whenever session_keys changes
    (pass the date of each bar for intraday charts)."""
    out: Series = []
    pv = vv = 0.0
    last = object()
    for i, (h, l, c, v) in enumerate(zip(highs, lows, closes, vols)):
        key = session_keys[i] if session_keys else None
        if key != last:
            pv = vv = 0.0
            last = key
        pv += (h + l + c) / 3 * v
        vv += v
        out.append(pv / vv if vv else None)
    return out


def atr(highs, lows, closes, n: int = 14) -> Series:
    trs = []
    for i in range(len(closes)):
        if i == 0:
            trs.append(highs[0] - lows[0])
        else:
            pc = closes[i - 1]
            trs.append(max(highs[i] - lows[i], abs(highs[i] - pc), abs(lows[i] - pc)))
    out: Series = [None] * len(closes)
    if len(trs) < n:
        return out
    a = sum(trs[:n]) / n
    out[n - 1] = a
    for i in range(n, len(trs)):
        a = (a * (n - 1) + trs[i]) / n
        out[i] = a
    return out
