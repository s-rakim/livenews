"""Settings come from environment variables (and an optional .env file).

Nothing secret is ever read from a file that lives in the repo. Copy
.env.example to .env, fill it in, and .gitignore keeps it out of git.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field

try:
    from dotenv import load_dotenv
except ImportError:  # optional dependency
    load_dotenv = None

LIVE_ACK_PHRASE = "yes-i-understand"

PAPER_TRADING_URL = "https://paper-api.alpaca.markets"
LIVE_TRADING_URL = "https://api.alpaca.markets"
DATA_URL = "https://data.alpaca.markets"

DEFAULT_PEOPLE = "Pelosi,Cramer,Tuberville,Crenshaw,Khanna,McCaul,Greene,Gottheimer,Mullin"
DEFAULT_WATCHLIST = "SPY,QQQ,AAPL,MSFT,NVDA,TSLA,AMZN,META,GOOGL,AMD"


class ConfigError(Exception):
    pass


def _f(name: str, default: float) -> float:
    raw = os.getenv(name)
    if raw is None or raw == "":
        return default
    try:
        return float(raw)
    except ValueError as e:
        raise ConfigError(f"{name} must be a number, got {raw!r}") from e


@dataclass
class Settings:
    data_mode: str  # "alpaca" or "mock"
    trading_mode: str  # "paper" or "live"
    alpaca_key: str = ""
    alpaca_secret: str = ""
    alpaca_feed: str = "iex"
    finnhub_key: str = ""
    fmp_key: str = ""
    people: list[str] = field(default_factory=list)
    people_interval: float = 900.0
    watchlist: list[str] = field(default_factory=list)
    news_interval: float = 1.0
    quotes_interval: float = 2.0
    movers_interval: float = 5.0
    max_order_value: float = 5000.0
    data_rate_per_min: int = 180

    @property
    def is_live(self) -> bool:
        return self.trading_mode == "live"

    @property
    def trading_url(self) -> str:
        return LIVE_TRADING_URL if self.is_live else PAPER_TRADING_URL

    @property
    def has_alpaca_keys(self) -> bool:
        return bool(self.alpaca_key and self.alpaca_secret)


def load_settings(env_file: str | None = ".env", force_mock: bool = False) -> Settings:
    if load_dotenv and env_file and os.path.exists(env_file):
        load_dotenv(env_file, override=False)

    key = os.getenv("ALPACA_API_KEY_ID", "").strip()
    secret = os.getenv("ALPACA_API_SECRET_KEY", "").strip()

    data_mode = os.getenv("LIVENEWS_DATA", "").strip().lower()
    if force_mock:
        data_mode = "mock"
    if not data_mode:
        data_mode = "alpaca" if key and secret else "mock"
    if data_mode not in ("alpaca", "mock"):
        raise ConfigError("LIVENEWS_DATA must be 'alpaca' or 'mock'")
    if data_mode == "alpaca" and not (key and secret):
        raise ConfigError(
            "LIVENEWS_DATA=alpaca needs ALPACA_API_KEY_ID and ALPACA_API_SECRET_KEY"
        )

    trading_mode = os.getenv("LIVENEWS_TRADING_MODE", "paper").strip().lower()
    if trading_mode not in ("paper", "live"):
        raise ConfigError("LIVENEWS_TRADING_MODE must be 'paper' or 'live'")
    if trading_mode == "live":
        # Two separate switches on purpose. Flipping one by accident isn't enough.
        if os.getenv("LIVENEWS_ALLOW_LIVE", "") != LIVE_ACK_PHRASE:
            raise ConfigError(
                "Live trading is locked. Set LIVENEWS_ALLOW_LIVE="
                f"{LIVE_ACK_PHRASE} as well as LIVENEWS_TRADING_MODE=live."
            )
        if data_mode == "mock":
            raise ConfigError("Live trading with mock market data makes no sense. Refusing.")

    watch = os.getenv("LIVENEWS_WATCHLIST", DEFAULT_WATCHLIST)
    watchlist = [s.strip().upper() for s in watch.split(",") if s.strip()]

    return Settings(
        data_mode=data_mode,
        trading_mode=trading_mode,
        alpaca_key=key,
        alpaca_secret=secret,
        alpaca_feed=os.getenv("ALPACA_FEED", "iex").strip().lower() or "iex",
        finnhub_key=os.getenv("FINNHUB_API_KEY", "").strip(),
        fmp_key=os.getenv("FMP_API_KEY", "").strip(),
        people=[p.strip() for p in os.getenv("LIVENEWS_PEOPLE", DEFAULT_PEOPLE).split(",") if p.strip()],
        people_interval=_f("LIVENEWS_PEOPLE_INTERVAL", 900.0),
        watchlist=watchlist,
        news_interval=max(0.5, _f("LIVENEWS_NEWS_INTERVAL", 1.0)),
        quotes_interval=max(1.0, _f("LIVENEWS_QUOTES_INTERVAL", 2.0)),
        movers_interval=max(2.0, _f("LIVENEWS_MOVERS_INTERVAL", 5.0)),
        max_order_value=_f("LIVENEWS_MAX_ORDER_VALUE", 5000.0),
        data_rate_per_min=int(_f("LIVENEWS_DATA_RATE_PER_MIN", 180)),
    )
