"""Run the terminal:  python -m livenews  [--mock]"""
from __future__ import annotations

import argparse
import sys

from .config import ConfigError, load_settings
from .store import Store


def build(settings, store):
    from .sources.people import PeopleTracker

    sources, closers = [], []
    follow = None
    people = PeopleTracker(store, settings.people, settings.fmp_key, settings.people_interval)
    closers.append(people.aclose)

    if settings.data_mode == "mock":
        from .broker.sim import SimBroker
        from .sources.mock import MockMarket, mock_disclosures

        mkt = MockMarket(store)
        sources += mkt.tasks()
        mock_disclosures(people)
        broker = SimBroker(store)
    else:
        from .broker.alpaca import AlpacaBroker
        from .sources.alpaca import AlpacaData
        from .sources.alpaca_stream import NewsStream, TradeStream

        data = AlpacaData(settings, store)
        trades = TradeStream(settings, store)
        sources += data.tasks() + [NewsStream(settings, store).run(), trades.run()]
        closers.append(data.aclose)

        async def follow(sym: str) -> None:
            data.extra_symbols.add(sym)
            await trades.follow(sym)

        broker = AlpacaBroker(settings, store)
        closers.append(broker.aclose)
        store.feed("BROKER", broker.name, "")
        if settings.finnhub_key:
            from .sources.finnhub import FinnhubNews

            fh = FinnhubNews(settings.finnhub_key, store)
            sources.append(fh.run())
            closers.append(fh.aclose)

    if settings.fmp_key:
        sources.append(people.run())
    return sources, broker, people, follow, closers


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="livenews")
    ap.add_argument("--mock", action="store_true", help="simulated data, no keys needed")
    args = ap.parse_args(argv)
    try:
        settings = load_settings(force_mock=args.mock)
    except ConfigError as e:
        print(f"config: {e}", file=sys.stderr)
        return 2

    from .ui.app import Terminal

    store = Store()
    sources, broker, people, follow, closers = build(settings, store)
    Terminal(settings, store, sources, broker, people, follow=follow, closers=closers).run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
