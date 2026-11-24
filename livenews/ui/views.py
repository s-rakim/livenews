"""Function screens. Each one takes over the main area when you type its
code, keeps the header, feed bar and command line, and Esc goes back."""
from __future__ import annotations

import asyncio
from datetime import date, datetime, timezone

from rich.text import Text
from textual.binding import Binding
from textual.containers import Grid, Horizontal, Vertical, VerticalScroll
from textual.widgets import DataTable, Static
from textual_plotext import PlotextPlot

from .. import indicators as ta
from ..hub import Hub, Unavailable
from ..risk import Holding, portfolio_risk
from ..store import Store
from ..userdata import AlertBook, Watchlists
from .style import AMBER, CYAN, DIM, DOWN, UP, WHITE, age_str, fmt_vol, pct_text, price, signed

AMBER_RGB = (255, 176, 0)
CYAN_RGB = (95, 215, 255)
GREY_RGB = (138, 122, 85)
WHITE_RGB = (230, 230, 230)


class FnView(Vertical):
    """Base class. Subclasses fill compose(), load() and optionally tick()."""

    code = ""
    refresh_every: float | None = None

    def __init__(self, hub: Hub, store: Store, **kw):
        super().__init__(classes="fnview")
        self.hub = hub
        self.store = store
        self.kw = kw
        self._timer = None

    @property
    def title(self) -> str:
        return self.code

    def on_mount(self) -> None:
        self.run_load()
        if self.refresh_every:
            self._timer = self.set_interval(self.refresh_every, self.run_load)

    def run_load(self) -> None:
        self.run_worker(self._safe_load, exclusive=True, group="load")

    async def _safe_load(self) -> None:
        try:
            await self.load()
        except Unavailable as e:
            self.show_error(str(e))
        except Exception as e:  # network etc
            self.show_error(f"{type(e).__name__}: {str(e)[:200]}")

    def show_error(self, msg: str) -> None:
        try:
            self.query_one("#fn_err", Static).update(Text(" " + msg, style=DOWN))
        except Exception:
            self.app.notify(msg, severity="error")

    async def load(self) -> None:
        pass


def _panel(widget, title: str, wid: str | None = None, **kw):
    v = Vertical(widget, classes="panel", id=wid, **kw)
    v.border_title = title
    return v


def _table(tid: str, *cols: str) -> DataTable:
    t = DataTable(id=tid, cursor_type="row", zebra_stripes=False)
    t.add_columns(*cols)
    return t


# --- chart --------------------------------------------------------------------

class ChartView(FnView):
    code = "CH"

    def __init__(self, hub, store, symbol: str, span: str = "6M"):
        super().__init__(hub, store)
        self.symbol, self.span = symbol, span
        self.refresh_every = 15 if span in ("1D", "5D") else 120

    @property
    def title(self):
        return f"{self.symbol} CH {self.span}"

    def compose(self):
        yield Static(id="ch_head")
        yield Static(id="fn_err")
        p = PlotextPlot(id="ch_price")
        v = PlotextPlot(id="ch_vol")
        r = PlotextPlot(id="ch_rsi")
        m = PlotextPlot(id="ch_macd")
        for w in (p, v, r, m):
            w.theme = "dark"
        yield _panel(p, "PRICE", "ch_p")
        with Horizontal(id="ch_lower"):
            yield _panel(v, "VOLUME")
            yield _panel(r, "RSI 14")
            yield _panel(m, "MACD 12/26/9")

    async def load(self):
        bars, src = await self.hub.bars(self.symbol, self.span)
        if not bars:
            raise Unavailable(f"no bars for {self.symbol} {self.span} (market closed and no history yet?)")
        q = self.store.quotes.get(self.symbol)
        o = [b.o for b in bars]
        h = [b.h for b in bars]
        l = [b.l for b in bars]  # noqa: E741
        c = [b.c for b in bars]
        v = [b.v for b in bars]
        xs = list(range(len(bars)))
        intraday = self.span in ("1D", "5D", "1M")
        local = [b.t.astimezone() for b in bars]
        fmt = "%H:%M" if self.span == "1D" else "%m-%d %H:%M" if intraday else "%Y-%m-%d" if self.span == "5Y" else "%m-%d"
        step = max(1, len(xs) // 8)
        ticks = xs[::step]
        labels = [local[i].strftime(fmt) for i in ticks]

        pp = self.query_one("#ch_price", PlotextPlot)
        plt = pp.plt
        plt.clear_data()
        plt.clear_figure()
        plt.candlestick(xs, {"Open": o, "Close": c, "High": h, "Low": l})
        if intraday:
            keys = [d.date() for d in local]
            vw = ta.vwap(h, l, c, v, keys)
            self._line(plt, xs, vw, CYAN_RGB, "VWAP")
        else:
            lo, mid, up = ta.bollinger(c, 20, 2)
            self._line(plt, xs, ta.sma(c, 50), CYAN_RGB, "SMA50")
            self._line(plt, xs, mid, AMBER_RGB, "SMA20")
            self._line(plt, xs, up, GREY_RGB, "BB")
            self._line(plt, xs, lo, GREY_RGB)
        plt.xticks(ticks, labels)
        pp.refresh()

        vp = self.query_one("#ch_vol", PlotextPlot)
        vp.plt.clear_data()
        vp.plt.clear_figure()
        up_x = [i for i in xs if c[i] >= o[i]]
        dn_x = [i for i in xs if c[i] < o[i]]
        if up_x:
            vp.plt.bar(up_x, [v[i] for i in up_x], color=(46, 232, 108), width=0.8)
        if dn_x:
            vp.plt.bar(dn_x, [v[i] for i in dn_x], color=(255, 77, 77), width=0.8)
        vmax = max(v) if v else 0
        if vmax:
            yt = [vmax * f for f in (0, 0.5, 1)]
            vp.plt.yticks(yt, [fmt_vol(y) for y in yt])
        vp.plt.xticks([], [])
        vp.refresh()

        r = ta.rsi(c)
        rp = self.query_one("#ch_rsi", PlotextPlot)
        rp.plt.clear_data()
        rp.plt.clear_figure()
        self._line(rp.plt, xs, r, AMBER_RGB)
        rp.plt.hline(70, GREY_RGB)
        rp.plt.hline(30, GREY_RGB)
        rp.plt.ylim(0, 100)
        rp.plt.xticks([], [])
        rp.refresh()

        line, sig, hist = ta.macd(c)
        mp = self.query_one("#ch_macd", PlotextPlot)
        mp.plt.clear_data()
        mp.plt.clear_figure()
        pos_x = [i for i in xs if hist[i] is not None and hist[i] >= 0]
        neg_x = [i for i in xs if hist[i] is not None and hist[i] < 0]
        if pos_x:
            mp.plt.bar(pos_x, [hist[i] for i in pos_x], color=(46, 232, 108), width=0.8)
        if neg_x:
            mp.plt.bar(neg_x, [hist[i] for i in neg_x], color=(255, 77, 77), width=0.8)
        self._line(mp.plt, xs, line, CYAN_RGB)
        self._line(mp.plt, xs, sig, AMBER_RGB)
        mp.plt.xticks([], [])
        mp.refresh()

        last = c[-1] if q is None else q.price
        chg = last - c[0]
        head = Text()
        head.append(f" {self.symbol} ", style=f"bold #000000 on {AMBER}")
        head.append(f"  {price(last)} ", style=f"bold {WHITE}")
        head.append_text(signed(chg))
        head.append("  ")
        head.append_text(pct_text(chg / c[0] * 100 if c[0] else None))
        head.append(f"  over {self.span}", style=DIM)
        head.append(f"   O {price(o[-1])}  H {price(h[-1])}  L {price(l[-1])}  C {price(c[-1])}  V {fmt_vol(v[-1])}", style=AMBER)
        rr = r[-1]
        head.append(f"   RSI {rr:.1f}" if rr is not None else "   RSI -", style=DOWN if rr and rr > 70 else UP if rr and rr < 30 else AMBER)
        head.append(f"   MACD {line[-1]:+.2f}/{sig[-1]:+.2f}" if line[-1] is not None and sig[-1] is not None else "", style=AMBER)
        head.append(f"\n {len(bars)} bars from {src}, last bar {local[-1]:%Y-%m-%d %H:%M}.  "
                    f"Spans: {self.symbol} CH 1D 5D 1M 3M 6M 1Y 5Y", style=DIM)
        self.query_one("#ch_head", Static).update(head)
        self.query_one("#fn_err", Static).update("")
        self.query_one("#ch_p").border_subtitle = "candles, VWAP" if intraday else "candles, SMA20 (amber), SMA50 (cyan), Bollinger 20/2 (grey)"

    @staticmethod
    def _line(plt, xs, ys, color, label=None):
        pts = [(x, y) for x, y in zip(xs, ys) if y is not None]
        if pts:
            plt.plot([p[0] for p in pts], [p[1] for p in pts], color=color, marker="braille")


# --- markets ------------------------------------------------------------------

class MarketsView(FnView):
    code = "MKTS"
    refresh_every = 5.0
    SECTIONS = ("INDICES", "FUTURES", "RATES", "FX", "COMMODITIES", "CRYPTO")

    def __init__(self, hub, store, focus: str = ""):
        super().__init__(hub, store)
        self.focus_section = focus

    @property
    def title(self):
        return "MKTS " + (self.focus_section or "world markets")

    def compose(self):
        yield Static(id="fn_err")
        with Grid(id="mk_grid"):
            for s in self.SECTIONS:
                yield _panel(_table(f"mk_{s}", "NAME", "LAST", "CHG", "%CHG", "AS OF", "SOURCE"), s, f"mkp_{s}")
        yield Static(Text(" AS OF is the time of the last print, so you see the real delay. Alpaca = real time. "
                          "Finnhub/OANDA = streaming FX. Yahoo = delayed (CME futures ~10 min). FRED/ECB = official "
                          "daily values. Enter on a row charts it.", style=DIM))

    def on_mount(self):
        super().on_mount()
        if self.focus_section:
            self.query_one(f"#mk_{self.focus_section}", DataTable).focus()

    async def load(self):
        data = await self.hub.markets()
        errs = data.pop("_errors", [])
        now = datetime.now(timezone.utc)
        for s in self.SECTIONS:
            t = self.query_one(f"#mk_{s}", DataTable)
            cur = t.cursor_row
            t.clear()
            self._seen: set[str] = set()
            for r in data.get(s, []):
                key = ("FRED:" + r.symbol) if "FRED" in r.source else r.symbol
                if isinstance(r.asof, datetime):
                    asof = Text(age_str((now - r.asof).total_seconds()) + " ago", style=UP if r.live else AMBER)
                elif isinstance(r.asof, date):
                    asof = Text(r.asof.isoformat(), style=AMBER)
                else:
                    asof = Text("-", style=DIM)
                val = "-" if r.value is None else (f"{r.value:.3f}%" if r.unit == "%" else price(r.value))
                ch = signed(r.change, "{:+.3f}" if r.unit == "%" else "{:+,.4f}" if r.value and r.value < 10 else "{:+,.2f}")
                if key in self._seen:
                    continue
                self._seen.add(key)
                t.add_row(Text(r.name, style="bold"), val, ch, pct_text(r.pct) if r.pct is not None else Text(""), asof,
                          Text(r.source + ("" if r.live else " (daily)"), style=DIM), key=key)
            if cur is not None and t.row_count:
                t.move_cursor(row=min(cur, t.row_count - 1), animate=False)
        self.query_one("#fn_err", Static).update(Text(" " + " | ".join(errs), style=DOWN) if errs else "")

    def on_data_table_row_selected(self, e: DataTable.RowSelected) -> None:
        e.stop()
        sym = str(e.row_key.value)
        self.app.show_fn(ChartView(self.hub, self.store, sym, "1D" if not sym.startswith("FRED:") else "1Y"))


# --- economic calendar --------------------------------------------------------

class EcoView(FnView):
    code = "ECO"
    refresh_every = 600

    def __init__(self, hub, store, days: int = 14, symbols=None):
        super().__init__(hub, store)
        self.days, self.symbols = days, symbols or []

    @property
    def title(self):
        return f"ECO next {self.days} days"

    def compose(self):
        yield Static(id="fn_err")
        yield _panel(_table("eco_t", "DATE", "DAY", "ET (USUAL)", "EVENT", "TYPE", "DETAIL", "SOURCE"),
                     "ECONOMIC CALENDAR", "eco_p")

    async def load(self):
        evs = await self.hub.eco(self.days, self.symbols)
        t = self.query_one("#eco_t", DataTable)
        t.clear()
        today = date.today()
        for e in evs:
            style = "bold white" if e.important else DIM
            if e.kind == "fomc":
                style = f"bold {DOWN}"
            elif e.kind == "earnings":
                style = CYAN
            t.add_row(Text(e.when.isoformat(), style=UP if e.when == today else AMBER), e.when.strftime("%a"),
                      Text(e.time, style=AMBER if e.time[:1].isdigit() else DIM),
                      Text(e.name, style=style), e.kind, e.detail, Text(e.source, style=DIM))
        self.query_one("#eco_p").border_subtitle = (
            "dates from FRED, times are each agency's usual slot, earnings for your watchlist from Finnhub")


# --- company ------------------------------------------------------------------

class DesView(FnView):
    code = "DES"

    def __init__(self, hub, store, symbol):
        super().__init__(hub, store)
        self.symbol = symbol

    @property
    def title(self):
        return f"{self.symbol} DES"

    def compose(self):
        yield Static(id="fn_err")
        with Horizontal():
            yield _panel(VerticalScroll(Static(id="des_s")), "PROFILE", "des_p")
            yield _panel(_table("des_f", "DATE", "FORM", "LINK"), "RECENT SEC FILINGS", "des_fp")

    async def load(self):
        p = await self.hub.profile(self.symbol)
        q = self.store.quotes.get(self.symbol)
        mcap = p.market_cap or (p.shares * q.price if p.shares and q else None)
        t = Text()
        t.append(f"{p.name or self.symbol}\n\n", style=f"bold {WHITE}")
        for k, v in (("Ticker", p.symbol), ("Exchange", p.exchange), ("Industry", p.industry or p.sic),
                     ("SIC", p.sic), ("Location", p.country), ("Market cap", fmt_vol(mcap) if mcap else "-"),
                     ("Shares out", fmt_vol(p.shares) if p.shares else "-"), ("IPO", p.ipo),
                     ("Fiscal year end", p.fiscal_year_end), ("CIK", p.cik), ("Web", p.web)):
            if v:
                t.append(f"{k:<16}", style=DIM)
                t.append(f"{v}\n", style=AMBER)
        if q:
            t.append(f"\n{'Last':<16}", style=DIM)
            t.append(f"{price(q.price)}  ", style="bold white")
            t.append_text(pct_text(q.pct))
        t.append(f"\n\nSources: {', '.join(p.sources)}", style=DIM)
        t.append(f"\nMore: {self.symbol} FA  {self.symbol} EST  {self.symbol} INS  {self.symbol} OPT  {self.symbol} CH", style=DIM)
        self.query_one("#des_s", Static).update(t)
        ft = self.query_one("#des_f", DataTable)
        ft.clear()
        for f in p.filings:
            ft.add_row(f["date"], Text(f["form"], style="bold"), Text(f["url"], style=DIM))


class FaView(FnView):
    code = "FA"

    def __init__(self, hub, store, symbol, period="A"):
        super().__init__(hub, store)
        self.symbol, self.period = symbol, period

    @property
    def title(self):
        return f"{self.symbol} FA {'annual' if self.period == 'A' else 'quarterly'}"

    def compose(self):
        yield Static(id="fn_err")
        yield _panel(DataTable(id="fa_t", cursor_type="row"), "FINANCIALS", "fa_p")

    async def load(self):
        f = await self.hub.financials(self.symbol, self.period)
        t = self.query_one("#fa_t", DataTable)
        t.clear(columns=True)
        t.add_columns("", *[p for p in f.periods])
        for label, vals, fmt in f.rows:
            cells = []
            for v in vals:
                if v is None:
                    cells.append(Text("-", style=DIM))
                elif fmt == "pct":
                    cells.append(signed(v, "{:.1f}", "%") if label == "Revenue growth" else Text(f"{v:.1f}%", style=AMBER))
                elif fmt == "eps":
                    cells.append(Text(f"{v:.2f}", style=DOWN if v < 0 else WHITE))
                else:
                    cells.append(Text(fmt_vol(v), style=DOWN if v < 0 else WHITE))
            t.add_row(Text(label, style="bold" if fmt != "pct" else DIM), *cells)
        self.query_one("#fa_p").border_subtitle = (
            f"{f.source}, period end dates. {self.symbol} FA Q for quarterly. Figures are as filed, not restated")


class EstView(FnView):
    code = "EST"

    def __init__(self, hub, store, symbol):
        super().__init__(hub, store)
        self.symbol = symbol

    @property
    def title(self):
        return f"{self.symbol} EST"

    def compose(self):
        yield Static(id="fn_err")
        with Horizontal():
            yield _panel(_table("est_r", "MONTH", "STR BUY", "BUY", "HOLD", "SELL", "STR SELL", "SCORE"), "ANALYST RATINGS")
            yield _panel(_table("est_e", "QUARTER", "EST", "ACTUAL", "SURPRISE"), "EPS VS ESTIMATE")
        with Horizontal(id="est_bottom"):
            yield _panel(Static(id="est_pt"), "PRICE TARGET", "est_ptp")
            yield _panel(Static(id="est_m"), "KEY STATS", "est_mp")

    def _target(self, pt: dict | None) -> Text:
        t = Text()
        if not pt or not pt.get("mean"):
            t.append((pt or {}).get("missing", "no price target published"), style=DIM)
            return t
        q = self.store.quotes.get(self.symbol)
        last = q.price if q else None
        for label, k in (("Mean", "mean"), ("Median", "median"), ("High", "high"), ("Low", "low")):
            v = pt.get(k)
            if v is None:
                continue
            t.append(f"{label:<7}", style=DIM)
            t.append(f"{v:>10,.2f}", style=AMBER)
            if last:
                up = (v / last - 1) * 100
                t.append(f"  {up:+6.1f}% vs last", style=UP if up >= 0 else DOWN)
            t.append("\n")
        if pt.get("high") and pt.get("low") and last:
            lo, hi = pt["low"], pt["high"]
            width = 30
            pos = 0 if hi <= lo else int(round((min(max(last, lo), hi) - lo) / (hi - lo) * (width - 1)))
            bar = "".join("\u25cf" if i == pos else "\u2500" for i in range(width))
            t.append(f"\n{lo:,.0f} ", style=DIM)
            t.append(bar, style=CYAN)
            t.append(f" {hi:,.0f}\n", style=DIM)
            t.append(f"last {last:,.2f} within the street range\n", style=DIM)
        if pt.get("updated"):
            t.append(f"updated {pt['updated']}", style=DIM)
        return t

    async def load(self):
        a = await self.hub.analysts(self.symbol)
        self.query_one("#est_pt", Static).update(self._target(a.get("target")))
        self.query_one("#est_ptp").border_subtitle = (a.get("target") or {}).get("source", "")
        rt = self.query_one("#est_r", DataTable)
        rt.clear()
        for r in a["recs"][:6]:
            sb, b, h, s, ss = (r.get(k, 0) or 0 for k in ("strongBuy", "buy", "hold", "sell", "strongSell"))
            n = sb + b + h + s + ss
            score = (sb * 5 + b * 4 + h * 3 + s * 2 + ss) / n if n else 0
            rt.add_row(r.get("period", "")[:7], Text(str(sb), style=UP), Text(str(b), style=UP), str(h),
                       Text(str(s), style=DOWN), Text(str(ss), style=DOWN), f"{score:.2f}/5")
        et = self.query_one("#est_e", DataTable)
        et.clear()
        for e in a["earnings"][:8]:
            est, act = e.get("estimate"), e.get("actual")
            et.add_row(e.get("period", ""), price(est), price(act), pct_text(e.get("surprisePercent")))
        m = a.get("metric") or {}
        t = Text()
        for k, label in (("peTTM", "P/E ttm"), ("peBasicExclExtraTTM", "P/E ttm"), ("psTTM", "P/S ttm"),
                         ("pbQuarterly", "P/B"), ("epsTTM", "EPS ttm"), ("52WeekHigh", "52w high"), ("52WeekLow", "52w low"),
                         ("beta", "Beta"), ("dividendYieldIndicatedAnnual", "Div yield %"), ("roeTTM", "ROE ttm %"),
                         ("netProfitMarginTTM", "Net margin ttm %"), ("10DayAverageTradingVolume", "10d avg vol (M)")):
            v = m.get(k)
            if isinstance(v, (int, float)) and label not in t.plain:
                t.append(f"{label}: ", style=DIM)
                t.append(f"{v:,.2f}    ", style=AMBER)
        self.query_one("#est_m", Static).update(t)
        self.query_one("#est_mp").border_subtitle = a.get("source", "")


class InsView(FnView):
    code = "INS"

    def __init__(self, hub, store, symbol):
        super().__init__(hub, store)
        self.symbol = symbol

    @property
    def title(self):
        return f"{self.symbol} INS"

    def compose(self):
        yield Static(id="fn_err")
        yield _panel(_table("ins_t", "FILED", "TRADED", "INSIDER", "CODE", "CHANGE", "PRICE", "VALUE", "HOLDS AFTER"), "INSIDER TRANSACTIONS", "ins_p")

    async def load(self):
        d = await self.hub.insiders(self.symbol)
        t = self.query_one("#ins_t", DataTable)
        t.clear()
        codes = {"P": "buy", "S": "sell", "A": "grant", "M": "option ex", "F": "tax w/h", "G": "gift"}
        for r in d["rows"][:200]:
            ch, px = r.get("change") or 0, r.get("transactionPrice") or 0
            code = r.get("transactionCode", "")
            t.add_row(r.get("filingDate", ""), r.get("transactionDate", ""), r.get("name", ""),
                      Text(f"{code} {codes.get(code, '')}", style=UP if code == "P" else DOWN if code == "S" else DIM),
                      signed(ch, "{:+,.0f}"), price(px) if px else "-", fmt_vol(abs(ch * px)) if px else "-",
                      fmt_vol(r.get("share")))
        self.query_one("#ins_p").border_subtitle = f"{d['source']}. {d.get('note', '')}"


class OwnView(FnView):
    code = "OWN"

    def __init__(self, hub, store, symbol):
        super().__init__(hub, store)
        self.symbol = symbol

    @property
    def title(self):
        return f"{self.symbol} OWN"

    def compose(self):
        yield Static(id="own_head")
        yield Static(id="fn_err")
        yield _panel(_table("own_t", "#", "HOLDER", "SHARES", "VALUE", "% OF SHARES OUT", "% OF 13F TOTAL", "FILED"),
                     "INSTITUTIONAL HOLDERS (13F)", "own_p")

    async def load(self):
        self.query_one("#own_head", Static).update(Text(
            " loading 13F holdings. The first lookup each quarter downloads the SEC data set (~100MB) and indexes it, "
            "watch the 13F feed on line 2.", style=DIM))
        d = await self.hub.holders(self.symbol)
        so = d.get("shares_out")
        t = self.query_one("#own_t", DataTable)
        t.clear()
        for i, r in enumerate(d["rows"], 1):
            t.add_row(str(i), Text(r["manager"], style="bold"), fmt_vol(r["shares"]), "$" + fmt_vol(r["value"]),
                      f"{r['shares'] / so * 100:.2f}%" if so else "-",
                      f"{r['shares'] / d['total_shares'] * 100:.2f}%" if d["total_shares"] else "-", r.get("filed", ""))
        head = Text()
        head.append(f" {self.symbol} ", style=f"bold #000000 on {AMBER}")
        head.append(f"  {d['issuer']}  CUSIP {d['cusip']}   ", style=WHITE)
        head.append(f"{d['count']:,} institutions hold {fmt_vol(d['total_shares'])} shares (${fmt_vol(d['total_value'])})", style=AMBER)
        if so:
            head.append(f" = {d['total_shares'] / so * 100:.1f}% of {fmt_vol(so)} shares out", style=AMBER)
        head.append(f"\n period {d['period']}. 13Fs are filed up to 45 days after quarter end and list long positions only.",
                    style=DIM)
        self.query_one("#own_head", Static).update(head)
        self.query_one("#own_p").border_subtitle = d.get("source", "")


class OptView(FnView):
    code = "OPT"
    refresh_every = 15.0
    BINDINGS = [Binding("b", "trade('buy')", "Buy contract"), Binding("s", "trade('sell')", "Sell contract")]

    def __init__(self, hub, store, symbol, which: str = ""):
        super().__init__(hub, store)
        self.symbol, self.which = symbol, which
        self._chain = None
        self._strikes: list[float] = []

    def action_trade(self, side: str) -> None:
        """Put an order for the contract under the cursor on the command line.
        Nothing is sent: Enter opens the usual ticket."""
        if not self._chain or not self._strikes:
            return
        t = self.query_one("#opt_t", DataTable)
        row, col = t.cursor_coordinate
        if col == 8 or not 0 <= row < len(self._strikes):
            self.app.notify("Move left onto a call or right onto a put first.")
            return
        k = self._strikes[row]
        q = (self._chain.calls if col < 8 else self._chain.puts).get(k)
        if q is None:
            return
        px = (q.bid + q.ask) / 2 if q.bid and q.ask else q.last or q.ask
        line = f"{side.upper()} {q.symbol} 1" + (f" LMT {px:.2f}" if px else "")
        from textual.widgets import Input

        inp = self.app.query_one("#cmd", Input)
        inp.value = line
        inp.focus()
        inp.cursor_position = len(line)

    @property
    def title(self):
        return f"{self.symbol} OPT"

    def compose(self):
        yield Static(id="opt_head")
        yield Static(id="fn_err")
        t = _table("opt_t", "BID", "ASK", "LAST", "IV", "DELTA", "GAMMA", "THETA", "VOL", "│ STRIKE │",
                   "BID ", "ASK ", "LAST ", "IV ", "DELTA ", "GAMMA ", "THETA ", "VOL ")
        t.cursor_type = "cell"
        yield _panel(t, "CALLS  |  PUTS", "opt_p")

    def _expiry(self) -> date | None:
        w = self.which
        if not w:
            return None
        try:
            return date.fromisoformat(w)
        except ValueError:
            return None

    async def load(self):
        exp = self._expiry()
        ch = await self.hub.chain(self.symbol, exp)
        if self.which.isdigit() and not exp:
            i = int(self.which) - 1
            if 0 <= i < len(ch.expiries) and ch.expiries[i] != ch.expiry:
                self.which = ch.expiries[i].isoformat()
                ch = await self.hub.chain(self.symbol, ch.expiries[i])
        t = self.query_one("#opt_t", DataTable)
        keep = t.cursor_coordinate if self._chain is not None and self._chain.expiry == ch.expiry else None
        t.clear()
        strikes = sorted(set(ch.calls) | set(ch.puts))
        self._chain, self._strikes = ch, strikes
        atm = min(strikes, key=lambda k: abs(k - ch.spot)) if strikes and ch.spot else None

        def side(q, itm):
            if q is None:
                return ["-"] * 8
            st = WHITE if itm else AMBER
            op = lambda v: "-" if v is None else f"{v:.2f}"  # noqa: E731
            return [Text(op(q.bid), style=st), Text(op(q.ask), style=st), op(q.last),
                    f"{q.iv * 100:.1f}" if q.iv else "-", f"{q.delta:+.2f}" if q.delta is not None else "-",
                    f"{q.gamma:.3f}" if q.gamma is not None else "-", f"{q.theta:.3f}" if q.theta is not None else "-",
                    fmt_vol(q.volume) if q.volume else "-"]

        for k in strikes:
            c, p = ch.calls.get(k), ch.puts.get(k)
            mid = Text(f"│ {k:>7.2f} │", style=f"bold #000000 on {AMBER}" if k == atm else "bold")
            t.add_row(*side(c, ch.spot and k < ch.spot), mid, *side(p, ch.spot and k > ch.spot), key=str(k))
        if keep is not None and keep.row < len(strikes):
            t.move_cursor(row=keep.row, column=keep.column)
        elif atm is not None:
            t.move_cursor(row=strikes.index(atm), column=2)
        dte = (ch.expiry - date.today()).days if ch.expiry else None
        head = Text()
        head.append(f" {self.symbol} ", style=f"bold #000000 on {AMBER}")
        head.append(f"  spot {price(ch.spot)}   expiry {ch.expiry} ({dte}d)   ", style=WHITE)
        head.append("expiries: ", style=DIM)
        for i, e in enumerate(ch.expiries[:12], 1):
            head.append(f"{i}:{e:%m-%d} ", style=f"bold {AMBER}" if e == ch.expiry else DIM)
        head.append(f"\n {ch.note}  {self.symbol} OPT <n> for another expiry.  B/S on a contract puts an order "
                    "on the command line.", style=DIM)
        self.query_one("#opt_head", Static).update(head)
        self.query_one("#opt_p").border_subtitle = ch.source


# --- portfolio ----------------------------------------------------------------

class PortView(FnView):
    code = "PORT"
    refresh_every = 30.0
    title = "PORT portfolio and risk"

    def compose(self):
        yield Static(id="fn_err")
        yield _panel(Static(id="port_s"), "SUMMARY", "port_sp")
        yield _panel(_table("port_t", "SYM", "QTY", "AVG", "LAST", "VALUE", "WEIGHT", "UNREAL P&L", "DAY P&L",
                            "BETA", "VOL 1Y", "MAX DD"), "POSITIONS", "port_p")

    async def load(self):
        pos = list(self.store.positions)
        if not pos:
            self.query_one("#port_s", Static).update(Text(" No positions. Risk numbers appear once you hold something.", style=DIM))
            self.query_one("#port_t", DataTable).clear()
            return

        async def none():
            return []

        # an option's own price history is too short and gappy for beta or VaR
        closes = await asyncio.gather(*(none() if p.mult != 1 else self.hub.daily_closes(p.symbol) for p in pos),
                                      self.hub.daily_closes("SPY"), return_exceptions=True)
        bench = closes[-1] if not isinstance(closes[-1], Exception) else []
        hs = []
        for p, cl in zip(pos, closes):
            q = self.store.quotes.get(p.symbol)
            hs.append(Holding(p.symbol, p.qty, p.market_price, p.avg_entry, q.prev_close if q else 0.0,
                              cl if not isinstance(cl, Exception) else [], p.mult))
        r = portfolio_risk(hs, bench)
        t = Text()

        def kv(k, v, style=AMBER):
            t.append(f"{k} ", style=DIM)
            t.append(v + "     ", style=style)

        kv("Gross", f"${r.gross:,.0f}")
        kv("Net", f"${r.net:,.0f}")
        t.append("Unrealized ", style=DIM)
        t.append_text(signed(r.unrealized, "${:+,.0f}"))
        t.append("     Day ", style=DIM)
        t.append_text(signed(r.day_pl, "${:+,.0f}"))
        t.append("\n")
        kv("Beta vs SPY", f"{r.beta:.2f}")
        kv("Vol (ann.)", f"{r.vol_annual * 100:.1f}%")
        kv("1d VaR 95% (normal)", f"${r.var95_param:,.0f}", DOWN)
        kv("1d VaR 95% (historical)", f"${r.var95_hist:,.0f}" if r.var95_hist else "n/a", DOWN)
        t.append("\n")
        kv("Largest", f"{r.max_weight[0]} {r.max_weight[1] * 100:.0f}%")
        kv("Concentration (HHI)", f"{r.hhi:.2f}  (1.0 = one stock)")
        a = self.store.account
        if a:
            kv("Equity", f"${a.equity:,.0f}")
            kv("Cash", f"${a.cash:,.0f}")
        t.append(f"\n{r.days} days of daily closes. Beta and VaR assume the past year looks like tomorrow, "
                 "which it often doesn't.", style=DIM)
        self.query_one("#port_s", Static).update(t)
        tbl = self.query_one("#port_t", DataTable)
        tbl.clear()
        by = {p["symbol"]: p for p in r.per_symbol}
        for h in hs:
            p = by.get(h.symbol, {})
            tbl.add_row(Text(h.symbol, style="bold"), f"{h.qty:g}", price(h.avg), price(h.price), f"${h.value:,.0f}",
                        f"{p.get('weight', 0) * 100:.1f}%", signed(h.unrealized), signed(h.day_pl),
                        f"{p.get('beta', 0):.2f}", f"{p.get('vol', 0) * 100:.0f}%", pct_text(p.get("max_dd", 0) * 100))


# --- blotter ------------------------------------------------------------------

class BlotterView(FnView):
    code = "ORD"
    refresh_every = 1.0
    title = "ORD orders and executions"

    def compose(self):
        yield _panel(_table("ord_o", "SUBMITTED", "ID", "SIDE", "QTY", "SYM", "TYPE", "LMT", "STP", "TIF",
                            "FILLED", "AVG", "STATUS"), "ORDERS", "ord_op")
        yield _panel(_table("ord_x", "TIME", "EVENT", "SIDE", "QTY", "SYM", "PRICE", "POS AFTER", "SUBMIT->EVENT", "ID"),
                     "EXECUTIONS", "ord_xp")

    async def load(self):
        ot = self.query_one("#ord_o", DataTable)
        ot.clear()
        for o in self.store.orders:
            st = UP if o.status == "filled" else DOWN if o.status in ("rejected", "canceled", "expired") else AMBER
            ot.add_row(f"{o.submitted_at.astimezone():%m-%d %H:%M:%S}", o.id[:8], Text(o.side.upper(), style=UP if o.side == "buy" else DOWN),
                       f"{o.qty:g}", Text(o.symbol, style="bold"), o.type, price(o.limit_price) if o.limit_price else "",
                       price(o.stop_price) if o.stop_price else "", o.tif.upper(), f"{o.filled_qty:g}",
                       price(o.filled_avg_price) if o.filled_avg_price else "", Text(o.status, style=st))
        xt = self.query_one("#ord_x", DataTable)
        xt.clear()
        for x in list(self.store.executions)[:200]:
            ev_style = UP if x.event == "fill" else CYAN if x.event == "partial_fill" else DOWN if x.event in ("rejected", "canceled") else DIM
            xt.add_row(f"{x.ts.astimezone():%H:%M:%S.%f}"[:-3], Text(x.event, style=ev_style), x.side.upper(), f"{x.qty:g}",
                       Text(x.symbol, style="bold"), price(x.price) if x.price else "",
                       f"{x.position_qty:g}" if x.position_qty is not None else "",
                       f"{x.latency_ms:,.0f} ms" if x.latency_ms is not None else "", x.order_id[:8])
        src = self.store.feeds.get("FILLS")
        self.query_one("#ord_xp").border_subtitle = (src.source if src else "local sim") + ", newest first"


# --- watchlist + alerts -------------------------------------------------------

class WatchView(FnView):
    code = "W"
    refresh_every = 1.0

    def __init__(self, hub, store, watch: Watchlists, alerts: AlertBook):
        super().__init__(hub, store)
        self.watch, self.alerts = watch, alerts

    @property
    def title(self):
        return f"W {self.watch.active}"

    def compose(self):
        yield _panel(_table("w_t", "SYM", "LAST", "CHG", "%CHG", "BID", "ASK", "HIGH", "LOW", "VOL", "RVOL", "NEWS 1H", "LAST TRADE"),
                     "WATCHLIST", "w_p")
        yield _panel(_table("a_t", "ID", "ALERT", "STATUS", "FIRED", "DETAIL"), "ALERTS", "a_p")

    async def load(self):
        from .. import engine
        t = self.query_one("#w_t", DataTable)
        counts = self.store.news_counts(60)
        frac = engine.session_fraction()
        rows = []
        for s in self.watch.symbols:
            q = self.store.quotes.get(s)
            if not q:
                rows.append((s, (Text(s, style="bold"), *["-"] * 11)))
                continue
            rv = engine.relative_volume(q.day_volume, q.prev_volume, frac)
            age = (datetime.now(timezone.utc) - q.updated_at).total_seconds()
            rows.append((s, (Text(s, style="bold"), Text(price(q.price), style="bold white"), signed(q.change), pct_text(q.pct),
                             price(q.bid), price(q.ask), price(q.high), price(q.low), fmt_vol(q.day_volume), f"{rv:.1f}x",
                             Text(str(counts.get(s, 0) or ""), style=CYAN), age_str(age))))
        _replace(t, rows)
        self.query_one("#w_p").border_title = f"WATCHLIST  {self.watch.active}  ({', '.join(self.watch.lists)})"
        self.query_one("#w_p").border_subtitle = "W ADD <tickers>  W DEL <tickers>  W USE <name>"
        at = self.query_one("#a_t", DataTable)
        arows = []
        for a in self.alerts.alerts:
            arows.append((str(a.id), (str(a.id), a.describe(), Text("fired" if a.fired_at else "armed", style=DOWN if a.fired_at else UP),
                                      a.fired_at.replace("T", " ")[:19], a.note)))
        _replace(at, arows)
        self.query_one("#a_p").border_subtitle = "ALRT NVDA > 150 | ALRT NVDA MOVE 5 | ALRT NEWS FDA [NVDA] | ALRT DEL <id>"


def _replace(table: DataTable, rows):
    from .app import Terminal
    Terminal._replace(table, rows)


# --- chat ---------------------------------------------------------------------

class ChatView(FnView):
    code = "MSG"
    refresh_every = 0.5

    def __init__(self, hub, store, chat, room: str = ""):
        super().__init__(hub, store)
        self.chat = chat
        self.start_room = room
        self._last = None

    def _label(self, room: str) -> str:
        if room.startswith("@") and "+" in room:
            me = self.chat.state.user.lower()
            others = [n for n in room[1:].split("+") if n != me] or [me]
            return "@" + others[0]
        return room

    @property
    def title(self):
        return f"MSG {self._label(self.start_room or self.chat.state.room)}"

    def compose(self):
        from textual.widgets import Input
        with Horizontal():
            with Vertical(id="chat_side"):
                yield _panel(_table("chat_rooms", "ROOM", "NEW"), "ROOMS", "chat_rp")
                yield _panel(_table("chat_users", "ONLINE"), "PEOPLE", "chat_up")
            with Vertical(id="chat_main"):
                yield _panel(VerticalScroll(Static(id="chat_log"), id="chat_scroll"), "MESSAGES", "chat_mp")
                yield Input(placeholder="type a message, Enter sends.  /join #room   /dm name   $TICKER links a ticker",
                            id="chat_in")

    def on_mount(self):
        self.chat.viewing = True
        super().on_mount()
        if self.start_room:
            self.run_worker(self.chat.switch(self.start_room))
        self.query_one("#chat_in").focus()

    def on_unmount(self):
        self.chat.viewing = False

    async def on_input_submitted(self, e) -> None:
        if e.input.id != "chat_in":
            return
        e.stop()
        text = e.value.strip()
        e.input.value = ""
        if not text:
            return
        try:
            if text.startswith("/join "):
                room = text.split(None, 1)[1].strip()
                await self.chat.switch(room if room.startswith("#") else "#" + room)
            elif text.startswith("/dm "):
                parts = text.split(None, 2)
                await self.chat.switch("@" + parts[1].lstrip("@"))
                if len(parts) > 2:
                    await self.chat.send(parts[2])
            else:
                await self.chat.send(text)
        except Exception as ex:
            self.app.say(f"chat: {ex}", err=True)
        await self.load()

    def on_data_table_row_selected(self, e: DataTable.RowSelected) -> None:
        e.stop()
        key = str(e.row_key.value)
        target = key if key.startswith(("#", "@")) else "@" + key
        self.run_worker(self._switch(target))

    async def _switch(self, target):
        await self.chat.switch(target)
        await self.load()

    async def load(self):
        from ..chat import TICKER_RX
        st = self.chat.state
        st.unread[st.room] = 0
        rooms = sorted(set(st.rooms) | {st.room, "#general"})
        rrows = []
        for r in rooms:
            rrows.append((r, (Text(self._label(r), style=f"bold {AMBER}" if r == st.room else WHITE),
                              Text(str(st.unread.get(r, 0) or ""), style=DOWN))))
        rt = self.query_one("#chat_rooms", DataTable)
        _replace(rt, rrows)
        if st.room in rooms and rt.cursor_row != rooms.index(st.room) and not rt.has_focus:
            rt.move_cursor(row=rooms.index(st.room), animate=False)
        if getattr(self, "_titled", None) != st.room:
            self._titled = st.room
            self.app.set_fnbar(f"MSG {self._label(st.room)}")
        _replace(self.query_one("#chat_users", DataTable),
                 [(u, (Text(u + (" (you)" if u == st.user else ""), style=UP),)) for u in st.users])
        msgs = list(st.rooms.get(st.room, ()))
        sig = (st.room, len(msgs), msgs[-1]["ts"] if msgs else "", st.status)
        mp = self.query_one("#chat_mp")
        where = "hosting on this machine" if st.hosting else st.url
        mp.border_title = f"MESSAGES  {self._label(st.room)}"
        mp.border_subtitle = f"you are {st.user}  |  {st.status}  |  {where}"
        if sig == self._last:
            return
        self._last = sig
        t = Text()
        for m in msgs[-300:]:
            ts = m.get("ts", "")
            try:
                local = datetime.fromisoformat(ts).astimezone().strftime("%H:%M:%S")
            except ValueError:
                local = ts[-8:]
            t.append(f"{local} ", style=DIM)
            t.append(f"{m.get('from', '?')}: ", style=f"bold {CYAN if m.get('from') == st.user else AMBER}")
            body = m.get("text", "")
            pos = 0
            for mt in TICKER_RX.finditer(body):
                t.append(body[pos:mt.start()], style=WHITE)
                t.append(mt.group(0).upper(), style=f"bold {UP}")
                pos = mt.end()
            t.append(body[pos:] + "\n", style=WHITE)
        if not msgs:
            t.append("No messages in this room yet.", style=DIM)
        self.query_one("#chat_log", Static).update(t)
        self.query_one("#chat_scroll", VerticalScroll).scroll_end(animate=False)
