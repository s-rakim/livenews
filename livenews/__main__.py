"""Run the terminal:  python -m livenews  [--mock]"""
from __future__ import annotations

import argparse
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
    people = PeopleTracker(store, settings.people, settings.fmp_key, settings.people_interval)
    closers.append(people.aclose)

    if settings.data_mode == "mock":
        from .broker.sim import SimBroker
        from .hub.mock import MockHub
        from .sources.mock import MockMarket, mock_disclosures

        mkt = MockMarket(store)
        sources += mkt.tasks()
        mock_disclosures(people)
        broker = SimBroker(store)
        hub = MockHub(store)
    else:
        from .broker.alpaca import AlpacaBroker
        from .hub.live import LiveHub
        from .sources.alpaca import AlpacaData
        from .sources.alpaca_stream import NewsStream, TradeStream
        from .sources.trade_updates import TradeUpdates

        data = AlpacaData(settings, store)
        trades = TradeStream(settings, store)
        sources += data.tasks() + [NewsStream(settings, store).run(), trades.run()]
        closers.append(data.aclose)

        async def follow(sym: str) -> None:
            if "/" in sym:
                return  # crypto pairs are polled by the MKTS screen, not the stock stream
            data.extra_symbols.add(sym)
            await trades.follow(sym)

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

    if settings.fmp_key:
        sources.append(people.run())
    return {"sources": sources, "broker": broker, "people": people, "follow": follow, "closers": closers,
            "hub": hub, "watch": watch, "alerts": AlertBook.load()}


def make_app(settings, store, watch=None):
    from .ui.app import Terminal

    p = build(settings, store, watch)
    app = Terminal(settings, store, p["sources"], p["broker"], p["people"], follow=p["follow"],
                   closers=p["closers"], hub=p["hub"], watch=p["watch"], alerts=p["alerts"])
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
