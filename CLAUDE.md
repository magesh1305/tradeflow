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
- Executor callbacks that arrive outside a dispatch (live updates from background tasks) are pumped immediately. Callbacks that arrive while a strategy hook runs wait for the end of that dispatch (`_hook_depth`).
- Accepted orders are journaled as PENDING **before** reaching the executor. The journal only enqueues (it never blocks); a background writer retries with backoff and alerts at most once a minute.

Live execution and recovery:
- `LiveExecutor` matches Kite updates by `tag`, then by `order_id`. Partial fills become incremental fills, `px = (avg_new*q_new - avg_old*q_old)/(q_new - q_old)`, with ids `{order_id}-{cumulative_qty}` (idempotent). If `average_price` is 0 it waits for a later update or the reconciliation.
- `BrokerError.outcome_unknown` (timeouts, network, 5xx): never retry. The order stays PENDING and the 20s reconciliation finds it by tag, or marks it REJECTED after 60s.
- Startup recovery (`engine/recovery.py`) replays today's fills, carried NRML fills (P&L zeroed) and open orders. Live also compares with `kite.positions()`/`kite.orders()`. Any mismatch, or a recovery that can't run (e.g. DB down), calls `engine.block()`: no strategy can start until `POST /recovery/resolve` (admin, `confirm: "RESOLVE"`).
- Session expiry (06:00 job, or a TokenException): stop the engine, flush the journal, then SIGTERM self so Docker restarts a fresh process. The Twisted reactor can't restart.

F&O rules:
- Order path: `prepare_group` (risk check -> option MARKET-to-LIMIT conversion) -> `check_margin` (opening orders only; paper estimate is synchronous, live `KiteMargin` is async and fails closed) -> `send`. Orders waiting for margin are `_unsent`; `cancel_order` handles them.
- Option entries need a two-sided quote, a spread <= `OPTION_MAX_SPREAD_PCT` and volume >= `OPTION_MIN_VOLUME`. Converted LIMIT orders are re-priced toward the market every `OPTION_CHASE_AFTER_SEC`, at most `OPTION_CHASE_MAX` times (Kite caps modifications at 25).
- Leg groups (`engine/legs.py`): every leg is risk-checked or none is sent, plus one basket margin check; BUY legs go first. On failure the pending legs are cancelled and filled quantity is unwound, never more than is held. On the kill switch or square-off, groups are ABORTED **before** anything else, so there is no unwind on top of the square-off (double exit).
- Protective stops (`engine/stops.py`): one SL (stop-limit) order per position, flagged `protective=True` and excluded from pending-exit accounting. Strategy exits release the stop first. A rejected stop alerts and is not retried.
- Auto-slice children have `tag: null`. `LiveExecutor` maps them via the placement response, and buffers child updates that arrive before it.
- Expiry: no entries on expiry day after `EXPIRY_NO_ENTRY_AFTER`. Stock (non-index) F&O positions are exited `PHYSICAL_SETTLEMENT_EXIT_DAYS` trading days before expiry at `PHYSICAL_SETTLEMENT_EXIT_TIME`.
- Greeks use `vollib` (the maintained successor of the deprecated `py_vollib`).

Charges, history, backtests:
- `engine/charges.py` charges every fill (rates in config; **they change**, so verify them at zerodha.com/charges). Brokerage is charged once per order across partial fills. Positions carry charges, so `net` = gross - charges everywhere, including the daily-loss kill switch.
- Kite reuses instrument tokens after expiry and drops expired contracts from its dump. The `instruments` table (migration 0002) keeps contract history with `first_seen`; backtests load only the contracts live on each day.
- `HistoricalFeed` replays each candle as 4 ticks: O, then L/H (low first on up candles), then C. Mid-day subscriptions load on demand, with day volume counted from the open. Gaps go into `missing`, and days with no data at all are reported as non-trading days.
- `factory.build_backtest_engine` always uses PaperExecutor and never journals, whatever `MODE` is. The runner restarts strategies each morning, as the live process does after its daily restart.
- The daily 15:40 job records 1-minute candles for `RECORD_UNDERLYINGS` (ATM +/- `RECORD_STRIKES_WIDTH`, nearest `RECORD_EXPIRIES`).

Control API (`app/api/`):
- Everything except `/health` and `/auth/*` needs `X-Admin-Token` (constant-time compare). `/ws?token=` streams the status snapshot every second, and access logs redact the token. `tests/test_api.py` walks every route via OpenAPI and asserts it returns 401 without the token, so **update its count when adding routes**.
- `/docs` and `/openapi.json` are off unless `API_DOCS=true` (they are unauthenticated).
- `/orders` and `/fills` merge the journal (history) with live engine state (today); `/pnl/daily` adds today's live row (`live: true`).
- `PUT /strategies/{id}/params` only works while the strategy is stopped. Changes are validated and saved to `STRATEGY_OVERRIDES_FILE`, so they survive the daily restart.
- `POST /backtests` returns 202 straight away. The run happens in a separate process (`backtest/job.py`) so it can't delay the live event loop.
- API response models live in `api/schemas.py`; the frontend types in `frontend/src/api/` mirror them.

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
python -m app.cli download-history --underlying NIFTY --from 2026-09-01 --to 2026-10-31
python -m app.cli backtest --strategy ema_crossover --params '{"fast": 9, "slow": 21}' --from 2026-09-01 --to 2026-10-31
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
