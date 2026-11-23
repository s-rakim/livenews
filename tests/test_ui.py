import asyncio

from textual.widgets import Button

from livenews.__main__ import build
from livenews.config import load_settings
from livenews.store import Store
from livenews.ui.app import OrderTicket, Terminal


def _app():
    s = load_settings(env_file=None, force_mock=True)
    store = Store()
    sources, broker, people, follow, closers = build(s, store)
    return Terminal(s, store, sources, broker, people, follow=follow, closers=closers), store, broker


async def _type(pilot, text):
    for ch in text:
        await pilot.press("space" if ch == " " else ch)
    await pilot.press("enter")


def test_mock_terminal_trades_on_paper():
    app, store, broker = _app()

    async def go():
        async with app.run_test(size=(200, 55)) as pilot:
            await pilot.pause(3)
            assert len(store.quotes) > 40 and len(store.news) > 0
            await _type(pilot, "buy aapl 5")
            await pilot.pause(0.5)
            assert isinstance(app.screen, OrderTicket)
            app.screen.query_one("#send", Button).press()
            await pilot.pause(1)
            assert store.orders and store.orders[0].status == "filled"

    asyncio.run(go())


def test_cancel_on_ticket_sends_nothing():
    app, store, broker = _app()

    async def go():
        async with app.run_test(size=(200, 55)) as pilot:
            await pilot.pause(2)
            await _type(pilot, "sell msft 1")
            await pilot.pause(0.5)
            await pilot.press("escape")
            await pilot.pause(0.5)
            assert broker.orders == []

    asyncio.run(go())


def test_live_ticket_needs_symbol_typed():
    app, store, broker = _app()
    broker.is_live = True  # pretend, to exercise the live confirmation path

    async def go():
        async with app.run_test(size=(200, 55)) as pilot:
            await pilot.pause(2)
            await _type(pilot, "buy nvda 1")
            await pilot.pause(0.5)
            send = app.screen.query_one("#send", Button)
            assert send.disabled
            app.screen.query_one("#confirm_sym").value = "NVD"
            await pilot.pause(0.2)
            assert send.disabled
            app.screen.query_one("#confirm_sym").value = "nvda"
            await pilot.pause(0.2)
            assert not send.disabled

    asyncio.run(go())
