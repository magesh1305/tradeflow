# tradeflow

Algorithmic F&O trading platform for NSE (NFO segment) via Zerodha Kite Connect.
Strategies run automatically. A human logs in to Kite each morning, watches the
dashboard, and controls strategies and the kill switch.

- `backend/`: Python 3.12, FastAPI, asyncio trading engine
- `frontend/`: React 18 + TypeScript (strict) control dashboard (Vite, Tailwind, TanStack Query)

All times are IST (Asia/Kolkata). Prices and P&L are in INR.

## Safety rules (non-negotiable)

- Two modes only. `paper` uses live market data with simulated fills and sends no real orders. `live` sends real orders with real money. **Paper is the default everywhere.**
- Live mode refuses to start unless `MODE=live`, `LIVE_TRADING_CONFIRMED=true`, an `ADMIN_TOKEN` of 16+ characters and the Kite data feed (`FEED=kite`) are all set. `Settings.assert_safe()` enforces this. **Never** set these values in code, Docker files, tests or CI.
- Never call real Kite order endpoints in tests. Mock `KiteConnect` and `KiteTicker` completely.
- **Every order goes through `RiskManager`.** Nothing bypasses it: not strategies, API endpoints or scheduled jobs.
- Exit (position-reducing) orders are always allowed, even after the kill switch, so the system can always get flat.
- Strategies never talk to the broker. They use only the `Strategy` base-class helpers and their context object.
- `app/factory.py` is the **only** module that knows whether the mode is paper or live. Strategy code runs unchanged in backtest, paper and live.
- No secrets in git. Only `.env.example` with placeholders.
- Pin every dependency to an exact version that is at least 2 weeks old.

## Kite Connect rules

- API order placement needs a static IP whitelisted in the Kite developer console. Data endpoints and the WebSocket work from any IP.
- MARKET and SL-M orders sent via the API must include `market_protection`: `-1` means automatic, or a value from 0 to 100 percent. It has no effect on LIMIT or SL orders. The value comes from config (`MARKET_PROTECTION`, default `-1`). Requires `kiteconnect>=5.2.2`.
- Rate limits (verified in the Kite docs): orders 10/sec, 400/min, 5000/day per user/API key; 25 modifications per order (after that, cancel and re-place); quote 1 req/sec; historical 3 req/sec; other endpoints 10 req/sec. Stay **below** 10 orders/sec. Going above it needs exchange strategy registration.
- Order `tag`: alphanumeric, max 20 characters. We use a 16-character client order id.
- `autoslice=True` slices orders above the freeze quantity, up to 10 slices.
- Access tokens expire every morning. The daily login flow is `/auth/login` -> Kite -> `/auth/callback`. The token is stored in Redis until the next 06:00 IST.
- WebSocket (KiteTicker): max 3000 instruments per connection and 3 connections per API key. `full` mode carries LTP, volume, OI, the exchange timestamp and 5-level depth. Order updates arrive as `type: "order"` text frames (`on_order_update`).
- KiteTicker runs on the Twisted reactor, which **cannot be restarted in a process**. `KiteFeed` starts once per process. After the token expires the engine stops and the container restarts. Calls into the ticker go through `reactor.callFromThread`, and callbacks come back via `loop.call_soon_threadsafe`.
- `KITE_USER_ID` (optional, recommended): the login callback rejects sessions from any other Kite account.
- Order placement is never retried automatically: a timeout may mean the order was placed. Reconciliation by tag resolves unknown outcomes.
- Tests: `tests/conftest.py` makes any real `KiteConnect._request` or `KiteTicker.connect` call fail the test.
- If you are unsure about a Kite API detail, check https://kite.trade/docs/connect/v3/ instead of guessing.

## Architecture

Single asyncio loop:

```
KiteFeed / HistoricalFeed / RandomWalkFeed
  -> TradingEngine tick queue -> CandleAggregator
  -> strategies (on_tick / on_candle) -> RiskManager
  -> Executor (PaperExecutor | LiveExecutor) -> order updates and fills
  -> PositionBook -> P&L -> daily-loss kill switch
  -> Postgres journal + Telegram alerts -> dashboard (REST + WebSocket)
```

Engine invariants (keep these when changing the engine):
- `TradingEngine.submit_order` is the only path to an executor, and it runs `RiskManager.check` **before** recording the order. If the order were recorded first, it would count as its own pending exit and real exits would be blocked.
- Fills update the `PositionBook` immediately. Strategy callbacks (`on_order`, `on_fill`) are queued and run after the current dispatch (`_pump`), so hooks are never re-entered.
- Market time comes from tick timestamps. In realtime mode a 1s wall-clock timer also advances it.
- The scheduled square-off closes **MIS only** (NRML carries). The kill switch and manual square-off close everything, each using the position's own product.
- Kill switch: stops all strategies, cancels open orders and squares off. Reset keeps strategies stopped.

```
backend/app/
  config.py    settings, mode switch, safety latches (assert_safe)
  factory.py   wires paper vs live (only place that knows the mode)
  main.py      FastAPI app, control API, scheduler
  cli.py       history download, backtest runs
  core/        models, IST clock, rate limiter
  data/        instrument master, option chain, historical data
  feeds/       KiteFeed, HistoricalFeed, RandomWalkFeed
  brokers/     Kite REST wrapper, daily login flow
  execution/   PaperExecutor, LiveExecutor
  engine/      event loop, candles, positions, charges, leg groups
  risk/        pre-trade checks, kill switch, margin checks
  strategies/  base class, registry, examples/
  backtest/    runner, metrics
  storage/     SQLAlchemy models, journal, Alembic migrations
  notify/      Telegram alerts
backend/tests/
frontend/src/api/   single typed API client
```

## Commands

The commands below are filled in as each phase lands.

```
# backend (from backend/)
python -m pip install -r requirements.txt -r requirements-dev.txt
ruff check . && ruff format --check .
mypy app
pytest
python -m app.cli download-history ...
python -m app.cli backtest ...
uvicorn app.main:app --reload

# frontend (from frontend/)
npm ci && npm run lint && npm run typecheck && npm test && npm run build

# everything
docker compose up --build
```

CI always runs with `MODE=paper FEED=sim`.

## Working rules

- Work one phase at a time. At the end of each phase: run all tests, show the results, summarize the changes, list anything uncertain, and wait for approval.
- Make small, focused commits.
- Never mark something done if its tests fail.
- If an earlier design decision blocks progress, explain the problem and propose an alternative. Don't silently rewrite it.
- Ask before adding any large dependency or external service.
