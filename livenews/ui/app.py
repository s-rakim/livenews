from __future__ import annotations

import asyncio
from datetime import datetime, timezone

from rich.text import Text
from textual import on, work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Grid, Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, DataTable, Input, Static

from .. import engine
from ..broker import Broker, OrderRejected, OrderRequest, check_order
from ..commands import __doc__ as COMMAND_HELP
from ..commands import parse
from ..config import Settings
from ..models import utcnow
from ..sources.people import PeopleTracker
from ..store import FeedStatus, Store

AMBER = "#ffb000"
UP = "#2ee86c"
DOWN = "#ff4d4d"
DIM = "#8a7a55"
CYAN = "#5fd7ff"

CSS = f"""
Screen {{ background: #000000; color: {AMBER}; }}
#top {{ height: 1; background: #1a1200; }}
#feeds {{ height: 1; background: #0d0d0d; }}
#grid {{ grid-size: 4 2; grid-rows: 2fr 3fr; grid-gutter: 0; height: 1fr; }}
#bottom {{ height: 9; }}
.panel {{ border: round #5c4300; border-title-color: {AMBER}; border-title-style: bold;
         border-subtitle-color: {DIM}; background: #000000; padding: 0; }}
.panel:focus-within {{ border: round {AMBER}; }}
DataTable {{ background: #000000; scrollbar-size: 1 1; }}
DataTable > .datatable--header {{ background: #120c00; color: {DIM}; text-style: bold; }}
DataTable > .datatable--cursor {{ background: #3a2a00; color: #ffffff; }}
#news {{ column-span: 2; }}
#side {{ column-span: 2; }}
#quote {{ height: 13; }}
#people {{ height: 1fr; }}
#cmdrow {{ height: 3; }}
#prompt {{ width: 3; content-align: center middle; color: {AMBER}; text-style: bold; }}
#cmd {{ background: #0d0900; color: #ffffff; border: tall #5c4300; }}
#cmd:focus {{ border: tall {AMBER}; }}
#msg {{ height: 1; color: {DIM}; }}
#book {{ width: 1fr; }}
#log {{ width: 40; }}
OrderTicket {{ align: center middle; }}
#ticket {{ width: 64; height: auto; border: thick {AMBER}; background: #0d0900; padding: 1 2; }}
#ticket.live {{ border: thick {DOWN}; }}
#ticket Button {{ margin: 1 1 0 0; }}
#ticket Horizontal {{ height: auto; }}
#ticket Static {{ height: auto; }}
OrderTicket, HelpScreen {{ background: rgba(0,0,0,0.6); }}
HelpScreen {{ align: center middle; }}
#helpbox {{ width: 70; height: auto; border: thick {AMBER}; background: #0d0900; padding: 1 2; }}
"""


def fmt_vol(v: float) -> str:
    if v >= 1e9:
        return f"{v / 1e9:.1f}B"
    if v >= 1e6:
        return f"{v / 1e6:.1f}M"
    if v >= 1e3:
        return f"{v / 1e3:.0f}K"
    return f"{v:.0f}"


def pct_text(p: float) -> Text:
    return Text(f"{p:+.2f}%", style=UP if p > 0 else DOWN if p < 0 else AMBER)


def age_str(sec: float | None) -> str:
    if sec is None:
        return "--"
    if sec < 0:
        sec = 0
    if sec < 60:
        return f"{sec:.1f}s"
    if sec < 3600:
        return f"{sec / 60:.0f}m"
    if sec < 86400:
        return f"{sec / 3600:.0f}h"
    return f"{sec / 86400:.0f}d"


def feed_badge(f: FeedStatus) -> Text:
    since = f.since_ok
    if f.error and (since is None or since > 10):
        dot, col = "●", DOWN
    elif since is None:
        dot, col = "○", DIM
    elif since > 15:
        dot, col = "●", "#ffd75f"
    else:
        dot, col = "●", UP
    t = Text()
    t.append(f" {dot} ", style=col)
    t.append(f"{f.name} ", style="bold")
    t.append(f"{f.source}", style=AMBER)
    t.append(f" data {age_str(f.data_age_s)}", style=DIM)
    if f.rtt_ms:
        t.append(f" rtt {f.rtt_ms:.0f}ms", style=DIM)
    t.append("  │", style="#333333")
    return t


class OrderTicket(ModalScreen[bool]):
    BINDINGS = [Binding("escape", "dismiss(False)", "Cancel")]

    def __init__(self, req: OrderRequest, est: float, ref: float | None, broker: Broker):
        super().__init__()
        self.req, self.est, self.ref, self.broker = req, est, ref, broker

    def compose(self) -> ComposeResult:
        live = self.broker.is_live
        with Vertical(id="ticket", classes="live" if live else ""):
            mode = Text("LIVE MONEY", style=f"bold {DOWN}") if live else Text("PAPER", style=f"bold {UP}")
            head = Text("ORDER TICKET  ", style="bold")
            head.append_text(mode)
            yield Static(head)
            body = Text()
            body.append(f"\n{self.req.describe()}\n", style="bold white")
            body.append(f"last {self.ref:.2f}   " if self.ref else "last --   ", style=AMBER)
            body.append(f"est. value ${self.est:,.2f}\n", style=AMBER)
            body.append(f"via {self.broker.name}, time in force DAY\n", style=DIM)
            yield Static(body)
            if live:
                yield Static(Text(f"Type {self.req.symbol} to confirm a real order:", style=DOWN))
                yield Input(placeholder=self.req.symbol, id="confirm_sym")
            with Horizontal():
                yield Button("Send", variant="error" if live else "success", id="send", disabled=live)
                yield Button("Cancel", id="cancel")

    @on(Input.Changed, "#confirm_sym")
    def _typed(self, e: Input.Changed) -> None:
        self.query_one("#send", Button).disabled = e.value.strip().upper() != self.req.symbol

    @on(Button.Pressed, "#send")
    def _send(self) -> None:
        self.dismiss(True)

    @on(Button.Pressed, "#cancel")
    def _cancel(self) -> None:
        self.dismiss(False)


class HelpScreen(ModalScreen[None]):
    BINDINGS = [Binding("escape,f1,q", "dismiss(None)", "Close")]

    def compose(self) -> ComposeResult:
        keys = (
            "\nKeys: F1 help  F2 news  F3 movers  F4 people  F5 positions  "
            "F9 command line  Enter on a row selects it  Esc closes dialogs  Ctrl+Q quits"
        )
        yield Static(Text(COMMAND_HELP.strip() + "\n" + keys, style=AMBER), id="helpbox")


class Terminal(App):
    CSS = CSS
    TITLE = "livenews"
    BINDINGS = [
        Binding("f1", "help", "Help"),
        Binding("f2", "focus('news_t')", "News"),
        Binding("f3", "focus('gain_t')", "Movers"),
        Binding("f4", "focus('people_t')", "People"),
        Binding("f5", "focus('book_t')", "Positions"),
        Binding("f9", "focus('cmd')", "Command"),
        Binding("ctrl+q", "quit", "Quit"),
    ]

    def __init__(self, settings: Settings, store: Store, sources: list, broker: Broker,
                 people: PeopleTracker, follow=None, closers=()):
        super().__init__()
        self.s = settings
        self.store = store
        self.sources = sources
        self.broker = broker
        self.people = people
        self.follow = follow  # async fn(symbol) to add a symbol to live streaming
        self.closers = closers
        self.selected = settings.watchlist[0] if settings.watchlist else "SPY"
        self.news_filter: str | None = None
        self.people_filter = ""
        self._news_seen = -1
        self._tasks: list[asyncio.Task] = []

    # layout ---------------------------------------------------------------
    def compose(self) -> ComposeResult:
        yield Static(id="top")
        yield Static(id="feeds")
        with Grid(id="grid"):
            for wid, title in (("gain", "GAINERS"), ("lose", "LOSERS"), ("vol", "VOLUME"), ("hot", "HOT  move x volume x news")):
                with Vertical(classes="panel", id=wid) as v:
                    v.border_title = title
                    yield DataTable(id=f"{wid}_t", cursor_type="row", zebra_stripes=False)
            with Vertical(classes="panel", id="news") as v:
                v.border_title = "NEWS"
                yield DataTable(id="news_t", cursor_type="row")
            with Vertical(id="side"):
                with Vertical(classes="panel", id="quote") as v:
                    v.border_title = "QUOTE"
                    yield Static(id="quote_s")
                with Vertical(classes="panel", id="people") as v:
                    v.border_title = "PEOPLE"
                    yield DataTable(id="people_t", cursor_type="row")
        with Horizontal(id="bottom"):
            with Vertical(classes="panel", id="book") as v:
                v.border_title = "POSITIONS / ORDERS"
                yield DataTable(id="book_t", cursor_type="row")
            with Vertical(classes="panel", id="log") as v:
                v.border_title = "LOG"
                yield Static(id="log_s")
        with Horizontal(id="cmdrow"):
            yield Static(">", id="prompt")
            yield Input(placeholder="ticker + function, e.g.  NVDA <Enter>   NVDA N   BUY NVDA 10   HELP", id="cmd")
        yield Static(id="msg")

    def on_mount(self) -> None:
        for wid in ("gain", "lose"):
            self.query_one(f"#{wid}_t", DataTable).add_columns("SYM", "LAST", "CHG%", "RVOL", "N")
        self.query_one("#vol_t", DataTable).add_columns("SYM", "RVOL", "1M×", "CHG%", "VOL")
        self.query_one("#hot_t", DataTable).add_columns("SYM", "CHG%", "RVOL", "N", "SCORE")
        self.query_one("#news_t", DataTable).add_columns("TIME", "AGE", "SRC", "TICKERS", "HEADLINE")
        self.query_one("#people_t", DataTable).add_columns("FILED", "WHO", "SYM", "WHAT", "SIZE", "LAG")
        self.query_one("#book_t", DataTable).add_columns("SYM", "QTY", "AVG", "LAST", "P&L", "│", "ORDER", "STATUS")
        for src in self.sources:
            self._tasks.append(asyncio.create_task(src))
        self.set_interval(0.5, self.refresh_fast)
        self.set_interval(1.0, self.refresh_news)
        self.set_interval(2.0, self.refresh_slow)
        self.query_one("#cmd", Input).focus()
        self.refresh_fast()

    async def on_unmount(self) -> None:
        for t in self._tasks:
            t.cancel()
        for c in self.closers:
            try:
                await c()
            except Exception:
                pass

    # refreshers -------------------------------------------------------------
    def refresh_fast(self) -> None:
        now_local = datetime.now().astimezone()
        ny = datetime.now(timezone.utc).astimezone(engine.NY)
        open_ = engine.session_fraction() < 1.0
        top = Text()
        top.append(" LIVENEWS ", style=f"bold #000000 on {AMBER}")
        if self.s.data_mode == "mock":
            top.append("  MOCK DATA, NOT REAL  ", style=f"bold #000000 on {DOWN}")
        top.append(f"  {now_local:%a %d %b %H:%M:%S %Z}", style="bold white")
        top.append(f"   NY {ny:%H:%M:%S}", style=AMBER)
        top.append("   US EQ ", style=DIM)
        top.append("OPEN" if open_ else "CLOSED", style=UP if open_ else DOWN)
        top.append("   ORDERS: ", style=DIM)
        top.append(self.broker.name.upper(), style=f"bold {DOWN}" if self.broker.is_live else f"bold {UP}")
        a = self.store.account
        if a:
            top.append(f"   EQ ${a.equity:,.0f}  BP ${a.buying_power:,.0f}  DAY ", style=AMBER)
            top.append(f"{a.day_pl:+,.0f}", style=UP if a.day_pl >= 0 else DOWN)
        self.query_one("#top", Static).update(top)

        feeds = Text()
        for f in self.store.feeds.values():
            feeds.append_text(feed_badge(f))
        self.query_one("#feeds", Static).update(feeds)

        movers = engine.build_movers(self.store)
        self._fill_movers("gain_t", engine.top_gainers(movers))
        self._fill_movers("lose_t", engine.top_losers(movers))
        vt = self.query_one("#vol_t", DataTable)
        rows = [
            (m.symbol, (Text(m.symbol, style="bold"), f"{m.rvol:.1f}x", f"{m.spike:.1f}x" if m.spike else "-",
                        pct_text(m.pct), fmt_vol(self.store.quotes[m.symbol].day_volume)))
            for m in engine.volume_spikes(movers)
        ]
        self._replace(vt, rows)
        ht = self.query_one("#hot_t", DataTable)
        rows = [
            (m.symbol, (Text(m.symbol, style="bold"), pct_text(m.pct), f"{m.rvol:.1f}x",
                        Text(str(m.news), style=CYAN if m.news else DIM), f"{m.score:.1f}"))
            for m in engine.hottest(movers)
        ]
        self._replace(ht, rows)
        self.refresh_quote()

        q = self.query_one("#quote")
        q.border_subtitle = self.store.feeds.get("QUOTES").source if "QUOTES" in self.store.feeds else ""

    def _fill_movers(self, tid: str, ms: list[engine.Mover]) -> None:
        rows = [
            (m.symbol, (Text(m.symbol, style="bold"), f"{m.price:,.2f}", pct_text(m.pct),
                        f"{m.rvol:.1f}x", Text(str(m.news) if m.news else "", style=CYAN)))
            for m in ms
        ]
        self._replace(self.query_one(f"#{tid}", DataTable), rows)

    @staticmethod
    def _replace(table: DataTable, rows: list[tuple[str, tuple]]) -> None:
        """Rewrite a table but keep the cursor on the same symbol if it's still there."""
        cur_key = None
        if table.row_count and table.cursor_row is not None:
            try:
                cur_key = table.coordinate_to_cell_key((table.cursor_row, 0)).row_key.value
            except Exception:
                cur_key = None
        table.clear()
        seen = set()
        for key, cells in rows:
            k = key
            i = 1
            while k in seen:
                k = f"{key}#{i}"
                i += 1
            seen.add(k)
            table.add_row(*cells, key=k)
        if cur_key is not None and cur_key in seen:
            table.move_cursor(row=table.get_row_index(cur_key), animate=False)

    def refresh_quote(self) -> None:
        sym = self.selected
        q = self.store.quotes.get(sym)
        t = Text()
        t.append(f"{sym}", style="bold white")
        if not q:
            t.append("\n\nno data yet. If this ticker isn't in the watchlist it\nwill appear after the next quotes poll.", style=DIM)
            self.query_one("#quote_s", Static).update(t)
            return
        col = UP if q.change > 0 else DOWN if q.change < 0 else AMBER
        t.append(f"  {q.price:,.2f}  ", style=f"bold {col}")
        t.append(f"{q.change:+,.2f}  {q.pct:+.2f}%\n", style=col)
        spread = q.ask - q.bid if q.ask and q.bid else 0
        t.append(f"bid {q.bid:,.2f}  ask {q.ask:,.2f}  spr {spread:.2f}\n", style=AMBER)
        t.append(f"hi {q.high:,.2f}  lo {q.low:,.2f}  prev {q.prev_close:,.2f}\n", style=AMBER)
        rv = engine.relative_volume(q.day_volume, q.prev_volume, engine.session_fraction())
        sp = engine.minute_spike(self.store.minute_vols.get(sym, ()))
        t.append(f"vol {fmt_vol(q.day_volume)}  prev {fmt_vol(q.prev_volume)}  rvol {rv:.1f}x  1m {sp:.1f}x\n", style=AMBER)
        age = (utcnow() - q.updated_at).total_seconds()
        t.append(f"last trade {q.updated_at.astimezone():%H:%M:%S}  ({age_str(age)} ago)\n", style=DIM)
        heads = self.store.news_for(sym)[:4]
        for n in heads:
            t.append(f"{n.created_at.astimezone():%H:%M} ", style=DIM)
            t.append(n.headline[:70] + "\n", style="white")
        if not heads:
            t.append("no headlines tagged with this ticker yet\n", style=DIM)
        self.query_one("#quote_s", Static).update(t)

    def refresh_news(self) -> None:
        self.people.scan_news()
        nt = self.query_one("#news_t", DataTable)
        panel = self.query_one("#news")
        panel.border_title = f"NEWS  {self.news_filter}" if self.news_filter else "NEWS  all"
        f = self.store.feeds.get("NEWS")
        if f:
            panel.border_subtitle = f"{f.source}  {f.note}"
        items = self.store.news_for(self.news_filter) if self.news_filter else list(self.store.news)
        rows = []
        for n in items[:150]:
            age = n.age_seconds
            fresh = age < 30
            rows.append((n.id, (
                Text(f"{n.created_at.astimezone():%H:%M:%S}", style="white" if fresh else DIM),
                Text(age_str(age), style=UP if fresh else DIM),
                Text(n.source[:8], style=DIM),
                Text(" ".join(n.symbols[:4]), style=f"bold {CYAN}"),
                Text(n.headline, style="bold white" if fresh else AMBER),
            )))
        self._replace(nt, rows)

    async def refresh_slow(self) -> None:
        try:
            await self.broker.refresh()
            if "BROKER" in self.store.feeds:
                self.store.feed_ok("BROKER", 0.0)
        except Exception as e:
            if "BROKER" in self.store.feeds:
                self.store.feed_err("BROKER", str(e)[:100])
            else:
                self.store.note(f"broker: {e}")
        bt = self.query_one("#book_t", DataTable)
        rows = []
        pos = self.store.positions
        orders = [o for o in self.store.orders][:max(len(pos), 8)]
        for i in range(max(len(pos), len(orders))):
            p = pos[i] if i < len(pos) else None
            o = orders[i] if i < len(orders) else None
            cells = []
            if p:
                cells += [Text(p.symbol, style="bold"), f"{p.qty:g}", f"{p.avg_entry:,.2f}", f"{p.market_price:,.2f}",
                          Text(f"{p.unrealized:+,.2f}", style=UP if p.unrealized >= 0 else DOWN)]
            else:
                cells += ["", "", "", "", ""]
            cells.append("│")
            if o:
                px = f" {o.filled_avg_price:.2f}" if o.filled_avg_price else (f" L{o.limit_price:.2f}" if o.limit_price else "")
                cells += [f"{o.submitted_at.astimezone():%H:%M:%S} {o.side.upper()} {o.qty:g} {o.symbol}{px}", f"{o.status} {o.id[:8]}"]
            else:
                cells += ["", ""]
            rows.append((f"r{i}", tuple(cells)))
        self._replace(bt, rows)

        pt = self.query_one("#people_t", DataTable)
        evs = self.people.events
        if self.people_filter:
            evs = [e for e in evs if self.people_filter.lower() in e.person.lower()]
        prow = []
        for e in evs[:80]:
            if e.kind == "TRADE":
                act = Text(e.action[:10], style=UP if e.action.lower().startswith("p") else DOWN)
                lag = f"{e.lag_days}d" if e.lag_days is not None else "?"
                prow.append((e.key, (f"{e.filed:%m-%d}", e.person[:22], Text(e.symbol, style=f"bold {CYAN}"), act, e.amount, lag)))
            else:
                prow.append((e.key, (f"{e.filed.astimezone():%m-%d %H:%M}", e.person[:22], Text(e.symbol, style=f"bold {CYAN}"),
                                     Text(e.action[:48], style="white"), "news", "")))
        self._replace(pt, prow)
        pp = self.query_one("#people")
        pp.border_title = f"PEOPLE  {self.people_filter or ', '.join(self.people.people[:4]) + '...'}"
        f = self.store.feeds.get("PEOPLE")
        if f:
            pp.border_subtitle = f.note

        self.query_one("#log_s", Static).update(Text("\n".join(list(self.store.log)[:7]), style=DIM))

    # interaction -----------------------------------------------------------
    def say(self, msg: str, err: bool = False) -> None:
        self.query_one("#msg", Static).update(Text(" " + msg, style=DOWN if err else AMBER))

    def action_help(self) -> None:
        self.push_screen(HelpScreen())

    @on(DataTable.RowSelected)
    def _row(self, e: DataTable.RowSelected) -> None:
        tid = e.data_table.id
        key = str(e.row_key.value)
        if tid in ("gain_t", "lose_t", "vol_t", "hot_t"):
            self.select(key.split("#")[0])
        elif tid == "news_t":
            for n in self.store.news:
                if n.id == key:
                    if n.symbols:
                        self.select(n.symbols[0])
                    self.say(f"{n.headline}  {n.url}")
                    break
        elif tid == "people_t":
            for ev in self.people.events:
                if ev.key == key:
                    if ev.symbol and ev.symbol != "-":
                        self.select(ev.symbol.split(",")[0])
                    self.say(f"{ev.person}: {ev.action} {ev.symbol} {ev.amount} "
                             f"traded {ev.traded or '?'} filed {ev.filed:%Y-%m-%d} [{ev.source}] {ev.url}")
                    break

    def select(self, sym: str) -> None:
        self.selected = sym
        if self.follow:
            asyncio.create_task(self.follow(sym))
        self.refresh_quote()

    @on(Input.Submitted, "#cmd")
    async def _cmd(self, e: Input.Submitted) -> None:
        line = e.value
        e.input.value = ""
        c = parse(line)
        if c.kind == "error":
            self.say(c.error, err=True)
        elif c.kind == "help":
            self.action_help()
        elif c.kind == "quote":
            self.select(c.symbol)
            self.say(f"{c.symbol} quote")
        elif c.kind == "news":
            self.select(c.symbol)
            self.news_filter = c.symbol
            self.refresh_news()
            self.say(f"news filtered to {c.symbol}. N for all")
        elif c.kind == "allnews":
            self.news_filter = None
            self.refresh_news()
            self.say("all news")
        elif c.kind == "people":
            self.people_filter = c.arg
            await self.refresh_slow()
            self.say(f"people: {c.arg or 'all tracked'}")
        elif c.kind == "cancel":
            self.do_cancel(c.arg)
        elif c.kind == "order":
            self.do_order(OrderRequest(c.symbol, c.side, c.qty, "limit" if c.limit else "market", c.limit))

    @work(exclusive=False)
    async def do_cancel(self, arg: str) -> None:
        try:
            if arg == "ALL":
                await self.broker.cancel_all()
            else:
                match = [o.id for o in self.store.orders if o.id.upper().startswith(arg)]
                if len(match) != 1:
                    self.say(f"{len(match)} orders match {arg}", err=True)
                    return
                await self.broker.cancel(match[0])
            self.say(f"cancel sent: {arg}")
            self.store.note(f"cancel {arg}")
        except Exception as ex:
            self.say(f"cancel failed: {ex}", err=True)

    @work(exclusive=True)
    async def do_order(self, req: OrderRequest) -> None:
        q = self.store.quotes.get(req.symbol)
        ref = q.price if q else None
        if q:
            age = (utcnow() - q.updated_at).total_seconds()
            if req.type == "market" and age > 60 and self.s.data_mode != "mock":
                self.say(f"warning: last {req.symbol} trade is {age_str(age)} old, price may have moved", err=True)
        try:
            est = check_order(req, ref, self.s.max_order_value)
        except OrderRejected as ex:
            self.say(f"rejected: {ex}", err=True)
            return
        ok = await self.push_screen_wait(OrderTicket(req, est, ref, self.broker))
        if not ok:
            self.say("order cancelled, nothing sent")
            return
        try:
            o = await self.broker.submit(req)
        except Exception as ex:
            self.say(f"broker rejected: {ex}", err=True)
            self.store.note(f"REJECT {req.describe()}: {ex}")
            return
        self.say(f"sent {req.describe()} -> {o.status} ({o.id[:8]})")
        self.store.note(f"{'LIVE ' if self.broker.is_live else ''}{req.describe()} {o.status}")
        await self.refresh_slow()
