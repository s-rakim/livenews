import asyncio

from textual.widgets import Button

from livenews.__main__ import make_app
from livenews.config import load_settings
from livenews.store import Store
from livenews.ui.app import OrderTicket


def _app():
    s = load_settings(env_file=None, force_mock=True)
    store = Store()
    app, parts = make_app(s, store)
    return app, store, parts["broker"]


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


def test_every_function_screen_loads():
    app, store, broker = _app()

    async def go():
        async with app.run_test(size=(200, 55)) as pilot:
            await pilot.pause(2)
            await broker.submit(__import__("livenews.broker", fromlist=["OrderRequest"]).OrderRequest("NVDA", "buy", 3))
            for cmd in ["NVDA CH", "NVDA CH 1D", "MKTS", "ECO", "NVDA DES", "NVDA FA", "NVDA FA Q", "NVDA EST",
                        "NVDA INS", "NVDA OPT", "NVDA OPT 2", "PORT", "ORD", "W", "W ADD COIN", "ALRT NVDA > 1"]:
                await app.run_command(cmd)
                await pilot.pause(0.8)
                if app.fn_view is not None:
                    errs = app.fn_view.query("#fn_err")
                    if errs:
                        assert str(errs.first().render()).strip() == "", f"{cmd}: {errs.first().render()}"
            assert "COIN" in app.watch.symbols
            await pilot.press("escape")
            await pilot.pause(0.3)
            assert app.fn_view is None

    asyncio.run(go())
