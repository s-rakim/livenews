"""Watchlists and alerts, saved as JSON under ~/.livenews (or $LIVENEWS_HOME)."""
from __future__ import annotations

import json
import os
import re
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta
from pathlib import Path

from .models import NewsItem, utcnow
from .store import Store


def home() -> Path:
    p = Path(os.getenv("LIVENEWS_HOME") or Path.home() / ".livenews")
    p.mkdir(parents=True, exist_ok=True)
    return p


def _load(path: Path, default):
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return default


def _save(path: Path, data) -> None:
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2))
    tmp.replace(path)  # atomic, so a crash mid-write can't corrupt the file


class Watchlists:
    def __init__(self, default: list[str], path: Path | None = None):
        self.path = path or home() / "watchlists.json"
        data = _load(self.path, None)
        if not data or not data.get("lists"):
            data = {"active": "main", "lists": {"main": list(default)}}
        self.lists: dict[str, list[str]] = data["lists"]
        self.active: str = data.get("active", "main")
        if self.active not in self.lists:
            self.active = next(iter(self.lists))

    @property
    def symbols(self) -> list[str]:
        return self.lists[self.active]

    def save(self) -> None:
        _save(self.path, {"active": self.active, "lists": self.lists})

    def add(self, *syms: str) -> None:
        for s in syms:
            if s not in self.symbols:
                self.symbols.append(s)
        self.save()

    def remove(self, *syms: str) -> None:
        self.lists[self.active] = [s for s in self.symbols if s not in syms]
        self.save()

    def use(self, name: str) -> None:
        self.lists.setdefault(name.lower(), [])
        self.active = name.lower()
        self.save()


@dataclass
class Alert:
    id: int
    kind: str  # "above", "below", "move", "news"
    symbol: str = ""
    value: float = 0.0
    keyword: str = ""
    fired_at: str = ""
    note: str = ""

    def describe(self) -> str:
        if self.kind == "above":
            return f"{self.symbol} >= {self.value:g}"
        if self.kind == "below":
            return f"{self.symbol} <= {self.value:g}"
        if self.kind == "move":
            return f"{self.symbol} moves {self.value:g}% on the day"
        return f"news mentions '{self.keyword}'" + (f" ({self.symbol})" if self.symbol else "")


@dataclass
class AlertBook:
    path: Path = field(default_factory=lambda: home() / "alerts.json")
    alerts: list[Alert] = field(default_factory=list)
    _news_seen: set[str] = field(default_factory=set)
    started: datetime = field(default_factory=utcnow)

    @classmethod
    def load(cls, path: Path | None = None) -> "AlertBook":
        b = cls(path=path or home() / "alerts.json")
        b.alerts = [Alert(**a) for a in _load(b.path, [])]
        return b

    def save(self) -> None:
        _save(self.path, [asdict(a) for a in self.alerts])

    def add(self, kind: str, symbol: str = "", value: float = 0.0, keyword: str = "") -> Alert:
        nid = max((a.id for a in self.alerts), default=0) + 1
        a = Alert(nid, kind, symbol, value, keyword)
        self.alerts.append(a)
        self.save()
        return a

    def remove(self, aid: int) -> bool:
        before = len(self.alerts)
        self.alerts = [a for a in self.alerts if a.id != aid]
        self.save()
        return len(self.alerts) != before

    def armed(self) -> list[Alert]:
        return [a for a in self.alerts if not a.fired_at]

    def check(self, store: Store) -> list[Alert]:
        """Evaluate armed alerts. Each fires once; re-add it to arm it again."""
        fired = []
        fresh_news: list[NewsItem] = []
        for n in store.news:
            if n.id in self._news_seen:
                break
            fresh_news.append(n)
        for n in fresh_news:
            self._news_seen.add(n.id)
        # only headlines published after the terminal started can fire a news alert,
        # otherwise the first poll's backlog would set everything off at once
        cutoff = self.started - timedelta(minutes=2)
        fresh_news = [n for n in fresh_news if n.created_at >= cutoff]
        for a in self.armed():
            hit = ""
            if a.kind in ("above", "below", "move"):
                q = store.quotes.get(a.symbol)
                if not q:
                    continue
                if a.kind == "above" and q.price >= a.value:
                    hit = f"{a.symbol} {q.price:.2f} >= {a.value:g}"
                elif a.kind == "below" and q.price <= a.value:
                    hit = f"{a.symbol} {q.price:.2f} <= {a.value:g}"
                elif a.kind == "move" and abs(q.pct) >= a.value:
                    hit = f"{a.symbol} {q.pct:+.2f}% on the day"
            elif a.kind == "news":
                rx = re.compile(rf"\b{re.escape(a.keyword)}\b", re.I)
                for n in fresh_news:
                    if (not a.symbol or a.symbol in n.symbols) and rx.search(n.headline + " " + n.summary):
                        hit = f"'{a.keyword}': {n.headline}"
                        break
            if hit:
                a.fired_at = utcnow().isoformat(timespec="seconds")
                a.note = hit[:200]
                fired.append(a)
        if fired:
            self.save()
        return fired

    def prime(self, store: Store) -> None:
        """Mark existing headlines as seen so old news doesn't fire new alerts."""
        self._news_seen.update(n.id for n in store.news)
