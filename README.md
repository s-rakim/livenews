# livenews

A market terminal that runs in your shell. Live headlines, live prices, what's
moving and why, what Congress and Cramer are trading, and an order ticket
wired to Alpaca (paper by default).

![screenshot](docs/screenshot.png)

*Screenshot is mock mode, so every number in it is made up.*

## Run it

```bash
pip install -r requirements.txt
python -m livenews --mock        # no keys, simulated market
cp .env.example .env             # then add your Alpaca paper keys
python -m livenews               # real data
```

Make the terminal window big. It's laid out for about 200x55.

## Using it

Type in the command line at the bottom. Ticker first, then a function.

| Type | Does |
|---|---|
| `NVDA` | quote panel for NVDA, and starts streaming it |
| `NVDA N` | news panel shows only NVDA |
| `N` | back to all news |
| `BUY NVDA 10` | market order ticket |
| `SELL NVDA 10 LMT 142.50` | limit order ticket |
| `CXL ALL` / `CXL <id>` | cancel open orders |
| `PPL` / `PPL PELOSI` | people panel, all or one name |
| `HELP` | all of the above |

F1 help, F2 news, F3 movers, F4 people, F5 positions, F9 back to the command
line. Enter on any row (a mover, a headline, a filing) pulls up that ticker.

## Where the data comes from, and how fresh it is

The second line of the screen shows every feed: source, age of the newest
datapoint, and round-trip time. Green dot means it answered in the last 15s.
Red means it's failing and the error is in the LOG panel.

**News**
- Alpaca news WebSocket (Benzinga). Headlines are pushed the moment Alpaca
  gets them, usually a second or two after publication.
- Alpaca news REST, polled every 1s as a backstop in case the socket drops.
  Duplicates are dropped by id.
- Finnhub general news, optional, polled every 2s. Free tier is 60 calls/min.

**Prices**
- Alpaca trade WebSocket for your watchlist plus whatever you look up. Every
  trade updates the price as it prints. The free plan allows 30 symbols on
  the stream, so the oldest looked-up ticker gets dropped past that.
- Alpaca snapshots every 2s for everything else on screen (movers,
  most-actives, tickers in recent news), up to 200 symbols.
- Free plan prices are IEX only. That's real time, but IEX is one exchange
  with a few percent of US volume. Prices can be a cent or two off the
  consolidated tape and volumes are much smaller than what you'd see on a
  chart site. `ALPACA_FEED=sip` fixes that if you pay for Alpaca's data plan.

**Movers**
- Alpaca's screener (top gainers/losers, most active) every 5s. Alpaca
  refreshes it about once a minute on their side.
- The local engine then ranks everything it has a price for:
  - GAINERS / LOSERS: percent change vs yesterday's close
  - VOLUME: RVOL (today's volume vs yesterday's, adjusted for how far into
    the session we are) and 1M× (the last minute bar vs the average of the
    previous ones)
  - HOT: move size x volume x headline count in the last hour. A 3% move on
    4x volume with two headlines ranks above a 6% move nobody is trading.

**Rate limits.** Alpaca's free data plan is 200 requests/min. With the
defaults this uses about 115 to 145/min (news 60, screener 24, snapshots 30
or 60 depending on how many tickers are on screen) and a
shared limiter holds it under 180 no matter what you set the intervals to.

**People**
- Congress trades come from STOCK Act filings via Financial Modeling Prep
  (`FMP_API_KEY`). Members have up to 45 days to file, so these are never
  live. Each row shows the filing date and the LAG between trade and filing.
  Polled every 15 min. I couldn't confirm which FMP plan includes these
  endpoints, so it may need a paid tier. If the key doesn't have access the
  feed goes red and the LOG says so.
- Cramer and anyone else in `LIVENEWS_PEOPLE` are matched by name in the news
  wire. Benzinga writes up a lot of what Cramer says on air, so this catches
  most of it within the hour, at no extra cost. It's a headline match, not a
  structured pick list.

## Trading

- Paper is the default. Paper keys go to `paper-api.alpaca.markets`.
- Live needs `LIVENEWS_TRADING_MODE=live` **and**
  `LIVENEWS_ALLOW_LIVE=yes-i-understand`, plus live keys. Live with mock data
  is refused outright.
- Every order opens a ticket you have to confirm. In live mode the Send button
  stays disabled until you type the ticker.
- Before anything is sent: orders over `LIVENEWS_MAX_ORDER_VALUE` are
  refused, limit prices more than 10% from last are refused as likely typos,
  and you can't place an order on a ticker with no price yet.
- Each Alpaca order carries a unique `client_order_id` so a retried request
  can't fill twice.
- In mock mode a local simulator fills market orders at the simulated price.

## Tests

```bash
pip install -r requirements-dev.txt
pytest
```

The tests cover the config safety switches, order checks, the command parser,
parsing of every API response shape, the ranking math, dedupe, the rate
limiter, and the UI flow (ticket opens, cancel sends nothing, live ticket
stays locked until the ticker is typed).

## Layout

```
livenews/
  config.py          env vars, paper/live switches
  store.py           shared state, feed health
  engine.py          RVOL, minute spikes, ranking
  commands.py        command line grammar
  sources/
    alpaca.py        REST: news, screener, snapshots
    alpaca_stream.py WebSocket: news, trades, minute bars
    finnhub.py       second news wire
    people.py        Congress filings + name matching in news
    mock.py          simulated market for --mock
  broker/
    base.py          order checks
    alpaca.py        Alpaca trading API
    sim.py           local fill simulator
  ui/app.py          the terminal
```
