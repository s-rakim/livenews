"""Run the terminal:  python -m livenews  [--mock]"""
from __future__ import annotations

import argparse
import os
import sys

from .config import ConfigError, load_settings
from .store import Store


def build(settings, store, watch=None):
    """Wire sources, broker and data hub for the chosen mode. Returns a dict of
    the pieces the Terminal needs."""
    from .sources.people import PeopleTracker
    from .userdata import AlertBook, Watchlists

    watch = watch or Watchlists(settings.watchlist)
    settings.watchlist[:] = watch.symbols  # streams follow the saved watchlist
    sources, closers = [], []
    follow = None
    from .userdata import home

    live = settings.data_mode != "mock"
    house_dir = home() / "house" if live and os.getenv("LIVENEWS_HOUSE", "on").lower() != "off" else None
    people = PeopleTracker(store, settings.people, settings.fmp_key, settings.people_interval, house_dir=house_dir)
    closers.append(people.aclose)

    if settings.data_mode == "mock":
        from .broker.sim import SimBroker
        from .hub.mock import MockHub
        from .sources.mock import MockMarket, mock_disclosures

        mkt = MockMarket(store)
        sources += mkt.tasks()
        mock_disclosures(people)
        hub = MockHub(store)
        broker = SimBroker(store, option_mark=hub.option_mid)
    else:
        from .broker.alpaca import AlpacaBroker
        from .hub.live import LiveHub
        from .sources.alpaca import AlpacaData
        from .broker.base import asset_class
        from .sources.alpaca_stream import CryptoStream, NewsStream, TradeStream
        from .sources.trade_updates import TradeUpdates

        data = AlpacaData(settings, store)
        trades = TradeStream(settings, store)
        coins = CryptoStream(settings, store)
        sources += data.tasks() + [NewsStream(settings, store).run(), trades.run(), coins.run()]
        closers.append(data.aclose)

        async def follow(sym: str) -> None:
            cls = asset_class(sym)
            if cls not in ("equity", "crypto"):
                return  # options, indices, futures and FX are fetched by their own screens
            data.extra_symbols.add(sym)
            await (coins if cls == "crypto" else trades).follow(sym)

        broker = AlpacaBroker(settings, store)
        closers.append(broker.aclose)
        store.feed("BROKER", broker.name, "")
        sources.append(TradeUpdates(settings, store).run())
        hub = LiveHub(settings, data)
        closers.append(hub.aclose)
        if settings.finnhub_key:
            from .sources.finnhub import FinnhubNews

            fh = FinnhubNews(settings.finnhub_key, store)
            sources.append(fh.run())
            closers.append(fh.aclose)
            if os.getenv("LIVENEWS_FX_STREAM", "on").lower() != "off":
                from .sources.finnhub_ws import FxStream

                sources.append(FxStream(settings.finnhub_key, store).run())

    if live and (settings.fmp_key or people.house is not None):
        sources.append(people.run())
    chat = None
    if os.getenv("LIVENEWS_CHAT", "on").lower() != "off":
        from .chat import ChatClient

        chat = ChatClient(history=home() / "chat.jsonl")
        sources.append(chat.run())
        closers.append(chat.aclose)
    return {"sources": sources, "broker": broker, "people": people, "follow": follow, "closers": closers,
            "hub": hub, "watch": watch, "alerts": AlertBook.load(), "chat": chat}


def make_app(settings, store, watch=None):
    from .ui.app import Terminal

    p = build(settings, store, watch)
    app = Terminal(settings, store, p["sources"], p["broker"], p["people"], follow=p["follow"],
                   closers=p["closers"], hub=p["hub"], watch=p["watch"], alerts=p["alerts"], chat=p["chat"])
    p["alerts"].prime(store)
    return app, p


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="livenews")
    ap.add_argument("--mock", action="store_true", help="simulated data, no keys needed")
    args = ap.parse_args(argv)
    try:
        settings = load_settings(force_mock=args.mock)
    except ConfigError as e:
        print(f"config: {e}", file=sys.stderr)
        return 2
    app, _ = make_app(settings, Store())
    app.run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
