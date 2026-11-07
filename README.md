# tradeflow

Algorithmic F&O trading on NSE (NFO) through Zerodha Kite Connect. Strategies run
automatically. A human logs in to Kite each morning, watches the dashboard, and
controls strategies and the kill switch.

> **Paper is the default everywhere.** `paper` uses live market data with simulated
> fills and sends no orders. `live` sends real orders with real money and refuses to
> start unless `MODE=live`, `LIVE_TRADING_CONFIRMED=true`, an `ADMIN_TOKEN` of 16+
> characters, `FEED=kite` and Kite API keys are all set. Read the
> [going-live checklist](#going-live-checklist) before touching any of those.

- [Try it: one-click practice (Windows)](#try-it-one-click-practice-windows)
- [Architecture](#architecture)
- [Local setup](#local-setup)
- [Daily routine: Kite login](#daily-routine-kite-login)
- [The dashboard](#the-dashboard)
- [Example strategies](#example-strategies)
- [Adding a strategy](#adding-a-strategy)
- [Historical data and backtests](#historical-data-and-backtests)
- [Configuration](#configuration)
- [Control API](#control-api)
- [Deployment (static IP)](#deployment-static-ip)
- [Operations](#operations)
- [Testing and CI](#testing-and-ci)
- [Troubleshooting](#troubleshooting)
- [Known limitations](#known-limitations)
- [Going-live checklist](#going-live-checklist)

## Try it: one-click practice (Windows)

For anyone, no technical knowledge needed. Uses **pretend money and simulated
prices**: no Zerodha account, no Docker, no database.

1. Install [Python 3.12](https://www.python.org/downloads/) (tick **Add python.exe
   to PATH**) and [Node.js 22 LTS](https://nodejs.org/). Once only.
2. Double-click **`Start tradeflow.bat`** in the project folder. The first time it
   installs everything (a few minutes). Then the dashboard opens in your browser and
   the password is copied: click the password box and press **Ctrl+V**.
3. The **Start here** guide opens and walks you through it: what tradeflow is,
   practice mode, Zerodha, and your plan for today (two questions: the most you can
   lose today, and which robots may trade). Then the robots start.
4. Double-click **`Stop tradeflow.bat`** when you are done.

What it does: `scripts/practice.ps1` always forces `MODE=paper`, `FEED=sim` and
`STORAGE=memory` (live mode refuses memory storage), lets robots trade at any time of
day (the prices are simulated), and keeps its password and logs in `.practice\`
(gitignored). Nothing is saved between runs. It needs ports 8000 and 5180 free.

This is for learning. Real market prices and real money need the full setup below
(Docker, a Kite Connect app, a server with a static IP), done by someone technical.

## Architecture

```
Kite WebSocket (KiteTicker) ─┐
HistoricalFeed (backtests) ──┼─> TradingEngine (one asyncio loop)
RandomWalkFeed (sim) ────────┘     tick queue -> candles -> strategies
                                   -> RiskManager (every order, no exceptions)
                                   -> PaperExecutor | LiveExecutor (Kite REST)
                                   -> fills -> PositionBook -> P&L (net of charges)
                                   -> daily-loss kill switch
                                   -> Postgres journal  ·  Telegram alerts
                                   -> control API (REST + WebSocket) -> dashboard
```

| Piece | Where |
| --- | --- |
| Settings, live-mode safety latches | `backend/app/config.py` |
| Paper vs live wiring (the only place that knows the mode) | `backend/app/factory.py` |
| Engine, positions, charges, leg groups, stops | `backend/app/engine/` |
| Risk checks, kill switch, margin | `backend/app/risk/` |
| Kite REST wrapper, daily login | `backend/app/brokers/` |
| Strategies (base class, registry, examples) | `backend/app/strategies/` |
| Backtester | `backend/app/backtest/` |
| Control API | `backend/app/api/` |
| Dashboard (React) | `frontend/` |

Services (`docker-compose.yml`): TimescaleDB (orders, fills, candles, P&L, backtests),
Redis (the Kite session), the backend (engine + API on :8000), and the dashboard
(nginx on :8080, proxying `/api` and `/ws` to the backend).

More detail on the design rules is in [`CLAUDE.md`](CLAUDE.md).

## Local setup

Prerequisites: Docker with Compose v2. For development without Docker: Python 3.12
and Node 22.

```bash
cp .env.example .env
# Edit .env: set POSTGRES_PASSWORD (also inside DATABASE_URL) and ADMIN_TOKEN.
# Generate a token:  python -c "import secrets; print(secrets.token_urlsafe(24))"
docker compose up --build -d
```

Open <http://localhost:8080> and log in with your `ADMIN_TOKEN`. With the defaults
(`MODE=paper`, `FEED=sim`) everything runs on a simulated NIFTY market: no Kite
account needed. The `ema_nifty_5m` example strategy is enabled in
`backend/strategies.yaml`.

### Development without Docker

```bash
# Postgres and Redis only
docker compose up -d postgres redis

# backend (from backend/)
py -3.12 -m venv .venv && .venv/Scripts/activate        # Linux/macOS: source .venv/bin/activate
pip install -r requirements-dev.txt -c constraints.txt
alembic upgrade head
uvicorn app.main:app --reload                            # http://127.0.0.1:8000
ruff check . && ruff format --check . && mypy app tests && pytest

# frontend (from frontend/)
npx -y npm@11.21.1 ci                                    # npm 10 crashes on this tree
npm run dev                                              # http://localhost:5173
npm run lint && npm run typecheck && npm test && npm run build
```

Integration tests (migrations, SQL journal, history store) run when
`TEST_DATABASE_URL` points at a TimescaleDB database; otherwise they are skipped.
CI always runs them.

## Daily routine: Kite login

Kite access tokens expire every morning (06:00 IST, a regulatory rule), so a human
logs in once per trading day.

1. **Once:** in the [Kite developer console](https://developers.kite.trade), set your
   app's redirect URL to `https://<your-domain>/api/auth/callback` (locally:
   `http://localhost:8080/api/auth/callback`). Put the API key and secret in `.env`
   with `FEED=kite`. Also set `KITE_USER_ID` to your client id, so logins from any
   other Kite account are rejected.
2. **Each morning (before 09:15):** open the dashboard and click **Log in to Kite**.
   Kite redirects back and the engine starts with the day's session.
3. **Then open Today's plan and submit it** (see [The dashboard](#the-dashboard)).
   No strategy starts until you do. The form is pre-filled with your last plan, so
   most days it's a review and one click.
4. At 06:00 the next day the backend stops cleanly, flushes the journal and exits.
   Docker restarts it, and it waits for your next login. (KiteTicker's network
   reactor can only start once per process, so a fresh process is required.)

Scheduled jobs (IST, weekdays): 08:45 instrument reload · 09:00 daily risk reset
(the kill switch is only reset by a human) · `SQUARE_OFF_TIME` MIS square-off ·
15:35 end-of-day P&L summary (database + Telegram) · 15:40 recording of 1-minute
option candles around ATM.

## The dashboard

Log in with `ADMIN_TOKEN` (the "Admin password"). It is kept for the browser tab only
(sessionStorage) and is cleared when the backend answers 401.

The dashboard is written for people who have never traded: strategies are called
**robots**, every trading word has a dotted underline you can tap for a plain-English
meaning, and the **How it works** page explains the whole system, the daily routine,
a glossary and common questions. Desktop has a sidebar; phones have five bottom tabs
(Home, Plan, Robots, Activity, More).

| Page | What it shows / does |
| --- | --- |
| Top bar (every page) | **PRACTICE MODE** (blue) or **REAL MONEY** (red, pulsing) badge, Live/DISCONNECTED, "Submit today's plan" and "Needs a check" reminders, red **EMERGENCY STOP** (the kill switch; type `KILL` to confirm) |
| Home (`/`) | Today's result after charges (booked, still open, charges); morning checklist (Zerodha login, plan, robots running); open trades as cards ("Bought 1 lot of NIFTY 24000 Call…"); a "What's happening" timeline of today's trades; robots with today's result; **Close all trades**; a **I've checked, continue** button when crash recovery blocked the engine |
| Today's plan (`/plan`) | The daily plan, submitted every morning (see below) |
| Robots (`/robots`) | A card per robot with its summary and risk level. Detail page: how it works step by step, what can go wrong, **Start / Stop** (stop asks whether to close its trades), settings form with explanations (editable only while stopped), its open trades |
| Activity (`/activity`) | Tabs **Trades done** and **All orders**; filter by dates, robot and practice/real; **Export CSV** |
| Profit history (`/history`) | Bar chart and table of the result per day, before and after charges |
| Option prices (`/option-chain`) | Calls and puts around the current NIFTY level with price, offers, open contracts, IV and Greeks |
| Test on past data (`/backtests`) | Run form, list of tests, result page with summary cards, money-over-time chart and missing-data table |
| Safety limits (`/safety`) | Loss used today, open trades, orders per minute, account money (margin), every risk rule in plain words |
| Connection check (`/connection`) | Mode, price source and database status; whether the Kite keys are set (never the values), today's Zerodha login and a **Test connection** button (read-only profile call); the exact redirect URL for your Kite app; a checklist of everything real-money mode still needs |
| How it works (`/help`) | System overview, daily routine, practice vs real money, safety net, risk warning, glossary, FAQ |

### Today's plan

Every morning, review and submit the plan. **No robot can start that day until you
do.** The top bar shows **Submit today's plan** until then.

The table below uses the internal names; the form shows them in plain words
(for example "Most trades today" for max trades/day).

| Section | Field | What happens |
| --- | --- | --- |
| Account | Daily loss limit ₹ | Net loss reaches it: **kill switch** (everything stops and is squared off) |
| | Profit target ₹ | Net profit reaches it: **no new trades** today; open positions keep running |
| | Max trades / day | New entries across all strategies; exits never count |
| | Max open positions, max lots per order | Hard caps on every new order |
| | New trades from / until | The entry window (inside market hours, before square-off) |
| Per strategy | On / off | On: starts when you submit. Off: a running strategy is stopped (positions kept) |
| | Lots per trade | Sets the strategy's `lots` (stop the strategy to change it) |
| | Max trades / day | New entries for that strategy; a multi-leg group counts as one |
| | Loss limit ₹ | That strategy's net loss reaches it: **it stops and exits its positions**; others keep running |
| | Profit target ₹ | That strategy stops taking new trades |
| | Max lots / order, max capital ₹ | Caps per order and on the strategy's estimated margin |

- Empty optional fields mean **no limit**.
- The form is pre-filled from your last plan (or `.env` defaults the first time).
- **Tightening** any limit applies instantly. **Loosening** a limit during market hours
  (e.g. raising the loss limit after a loss) asks you to type `LOOSEN`; it's logged
  and sent as a critical alert.
- Exits, stop losses and square-offs are never blocked by these limits.
- Every submission and change is stored (`daily_plans`, `plan_events`) and shown under
  **Today's changes**. A restart during the day re-applies today's plan.

Status streams over one WebSocket (`/ws`) every second. If it drops, the header shows
**DISCONNECTED: reconnecting**, the last known data stays on screen, and the page
polls `/status` until the socket is back. On a phone the kill switch is pinned to the
bottom of the screen. Every failed request shows an error toast.

## Example strategies

Both live in `backend/app/strategies/examples/` and are configured in
`backend/strategies.yaml`.

- **`ema_crossover`** (`ema_nifty_5m`, enabled): trades the nearest NIFTY future, long
  when the fast EMA is above the slow EMA and short below, with no new entries after
  `last_entry_time`. Params: `underlying`, `interval_minutes`, `fast`, `slow`, `lots`,
  `last_entry_time`.
- **`short_straddle`** (`straddle_nifty`, **disabled**: short options carry unlimited
  risk): at `entry_time` it sells the ATM call and put of the nearest expiry as one leg
  group (if either leg fails, the other is unwound). It protects each leg with an
  exchange-side SL order at `stop_loss_pct` above the entry premium, plus the same stop
  in code, and exits at `exit_time`. Params: `underlying`, `entry_time`, `exit_time`,
  `lots`, `stop_loss_pct`, `exchange_stops`.

These are examples of the framework, not trading advice. Backtest and paper-trade
anything before using it.

## Adding a strategy

1. Create `backend/app/strategies/examples/my_strategy.py` (any module in that package
   is discovered automatically):

```python
from pydantic import BaseModel, ConfigDict, Field

from app.core.models import Candle, Product
from app.strategies.base import Strategy
from app.strategies.registry import register


class Params(BaseModel):
    model_config = ConfigDict(extra="forbid")
    underlying: str = "NIFTY"
    lots: int = Field(default=1, ge=1)


@register
class MyStrategy(Strategy):
    name = "my_strategy"
    Params = Params
    product = Product.MIS          # MIS intraday (squared off at 15:15) or NRML (carried)

    def on_start(self) -> None:
        self.fut = self.ctx.instruments.nearest_future(self.params.underlying, self.ctx.today)
        self.ctx.subscribe_candles(self.fut.token, 5 * 60)

    def on_candle(self, candle: Candle) -> None:
        if candle.close > candle.open and not self.position(self.fut):
            self.buy(self.fut, self.lots(self.fut, self.params.lots))
```

2. Add an instance to `backend/strategies.yaml`. One strategy can run several times
   with different params:

```yaml
  - id: my_nifty
    strategy: my_strategy
    enabled: false          # start it from the dashboard
    params: {underlying: NIFTY, lots: 1}
```

Rules that keep strategies safe and portable:

- Never call the broker. Use only `self.buy/sell/exit_position/exit_all/place_legs/protect/roll`
  and `self.ctx` (time, prices, positions, subscriptions, option chain). The same code
  runs in backtest, paper and live.
- Every order still goes through the RiskManager (lot multiples, max lots, trading
  window, max positions, orders per minute, duplicates, liquidity, expiry day, margin).
- Multi-leg trades (spreads, straddles, rolls): use `self.place_legs(...)`. If any leg
  fails, the filled legs are unwound automatically.
- Option orders are sent as LIMIT orders priced from the bid/ask, and re-priced if
  they don't fill. `self.protect(instrument, stop)` keeps a real SL order at the
  exchange; keep an in-code stop as well.
- An exception in a hook disables only that strategy and sends an alert.

Backtest it (next section), then paper-trade it for weeks before going live.

## Historical data and backtests

```bash
# Download candles (needs a Kite session: log in through the dashboard first).
docker compose exec backend python -m app.cli download-history \
    --underlying NIFTY --from 2026-09-01 --to 2026-10-31 --interval minute

# Run a backtest (also available on the dashboard's Backtests page).
docker compose exec backend python -m app.cli backtest \
    --strategy ema_crossover --params '{"fast": 9, "slow": 21}' \
    --from 2026-09-01 --to 2026-10-31
```

- Kite's historical API does **not** serve expired option contracts. That's why the
  backend records 1-minute candles around ATM every day at 15:40
  (`RECORD_UNDERLYINGS`, `RECORD_STRIKES_WIDTH`, `RECORD_EXPIRIES`). Option backtests
  only cover days you have recorded.
- Backtests replay candles through the same engine with the paper executor. Each
  candle becomes four ticks: open, then low and high (low first on up candles), then
  close. That is an approximation inside each candle.
- Results include gross and net P&L (after Zerodha charges), trades, win rate,
  average win/loss, max drawdown, daily Sharpe and an equity curve. **Missing data is
  listed explicitly** (instrument, day, candles found vs expected); read it before
  trusting a result.
- Charges and freeze limits change. Verify the rates in `.env` (`STT_*`,
  `EXCHANGE_TXN_*`, ... and `FREEZE_LIMITS`) against zerodha.com/charges and NSE
  circulars.

## Configuration

Everything is set through environment variables in `.env`. **Every setting, with its
default and a comment, is in [`.env.example`](.env.example).** The main groups:

| Group | Settings | Notes |
| --- | --- | --- |
| Mode | `MODE`, `FEED`, `LIVE_TRADING_CONFIRMED` | Defaults: `paper`, `sim`, `false` |
| Auth | `ADMIN_TOKEN`, `KITE_API_KEY`, `KITE_API_SECRET`, `KITE_USER_ID`, `FRONTEND_URL`, `FRONTEND_ORIGIN` | Token: 16+ random chars |
| Hours | `MARKET_OPEN`, `MARKET_CLOSE`, `TRADING_START`, `TRADING_END`, `SQUARE_OFF_TIME`, `SESSION_EXPIRY_TIME` | IST; entries only inside the trading window |
| Risk | `MAX_LOTS_PER_ORDER`, `MAX_OPEN_POSITIONS`, `MAX_ORDERS_PER_MINUTE`, `DAILY_LOSS_LIMIT`, `DUPLICATE_ORDER_WINDOW_SEC` | Daily loss is net of charges and trips the kill switch |
| Daily plan defaults | `DAILY_LOSS_LIMIT` (500), `PROFIT_TARGET` (1500), `MAX_TRADES_PER_DAY` (1), `MAX_LOTS_PER_ORDER` (1), `TRADING_START`/`TRADING_END` (09:15/15:10) | Beginner-safe values that pre-fill the first plan; after that the Plan page starts from your last plan. 0 = no limit for profit target and trades. New trades stop at 15:10 because intraday trades close at `SQUARE_OFF_TIME` (15:15), before the 15:30 market close |
| Kite orders | `MARKET_PROTECTION`, `ORDER_RATE_PER_SEC`, `ORDER_RATE_PER_MIN` | Rate stays below Kite's 10/sec |
| Margin / paper | `MARGIN_CHECK_ENABLED`, `PAPER_CAPITAL`, `PAPER_MARGIN_PCT`, `PAPER_SLIPPAGE_TICKS` | Set `PAPER_CAPITAL` to your real capital |
| F&O | `FREEZE_LIMITS`, `INDEX_UNDERLYINGS`, `RISK_FREE_RATE`, `OPTION_*`, `STOP_LIMIT_BUFFER_PCT`, `EXPIRY_NO_ENTRY_AFTER`, `PHYSICAL_SETTLEMENT_EXIT_*`, `LEG_GROUP_TIMEOUT_SEC` | Freeze limits change: verify |
| Charges | `BROKERAGE_*`, `STT_*`, `EXCHANGE_TXN_*`, `SEBI_FEE_PER_CRORE`, `IPFT_PER_CRORE`, `STAMP_*`, `GST_PCT` | Change over time: verify at zerodha.com/charges |
| History | `HISTORY_CHUNK_DAYS`, `RECORD_UNDERLYINGS`, `RECORD_STRIKES_WIDTH`, `RECORD_EXPIRIES` | Chunk limits are not documented by Kite: verify |
| Alerts | `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID` | Optional |
| Infra / process | `POSTGRES_*`, `DATABASE_URL`, `REDIS_URL`, `STRATEGIES_FILE`, `STRATEGY_OVERRIDES_FILE`, `API_DOCS`, `ENGINE_AUTOSTART`, `EXIT_ON_SESSION_EXPIRY`, `LOG_LEVEL`, `LOG_JSON`, `DASHBOARD_BIND`, `DASHBOARD_PORT` | Compose sets the DB/Redis URLs and the overrides path itself |

Restart the backend after changing `.env`: `docker compose up -d`.

## Control API

The backend listens on `127.0.0.1:8000`; the dashboard reaches it at `/api/...`.
Everything except `/health` and `/auth/*` needs the header `X-Admin-Token: <ADMIN_TOKEN>`.
Interactive docs are off unless `API_DOCS=true`, because they are unauthenticated.

| Method & path | Purpose |
| --- | --- |
| `GET /health` | Liveness and mode (public) |
| `GET /auth/login` · `GET /auth/callback` · `GET /auth/status` | Daily Kite login flow and session status (public) |
| `GET /status` | Full snapshot: mode, feed, kill switch, P&L, positions, open orders, strategies |
| `GET /strategies` · `GET /strategies/{id}` | List / detail (params, JSON schema, positions) |
| `POST /strategies/{id}/start` | Start (409 while the kill switch is active or the engine is blocked) |
| `POST /strategies/{id}/stop?exit_positions=true\|false` | Stop; optionally exit its positions |
| `PUT /strategies/{id}/params` | Body `{"params": {...}}`; only while stopped; validated; saved across restarts |
| `GET /orders` · `GET /fills` | Filters: `from`, `to` (YYYY-MM-DD), `strategy`, `mode`, `limit` |
| `GET /pnl/daily` | Daily gross/charges/net per strategy and `__account__` (`from`, `to`, `strategy`, `mode`) |
| `GET /risk` | Limits, today's usage, kill switch, margin |
| `GET /option-chain/{underlying}?expiry=&width=` | Live chain with IV and Greeks (subscribes on demand) |
| `POST /square-off` | Cancel open orders and exit all positions (MIS and NRML) |
| `POST /kill-switch` · `POST /kill-switch/reset` | Body `{"reason": "..."}`; reset keeps strategies stopped |
| `POST /backtests` · `GET /backtests` · `GET /backtests/{id}` | Body `{"strategy", "params", "start", "end", "interval"}`; runs in a separate process (202) |
| `GET /settings` · `POST /settings/plan` · `GET /settings/history` | Today's plan form; submit/update (`{"plan": {...}, "confirm": "LOOSEN"}` when loosening in market hours, else 409 lists what would loosen); audit trail |
| `GET /connection` · `POST /connection/kite` | Configuration and reachability report without secrets; read-only Kite profile test (no orders) |
| `GET /recovery` · `POST /recovery/resolve` | Crash-recovery status; resolve with `{"confirm": "RESOLVE", "adopt_broker_positions": false}` |
| `WS /ws?token=<ADMIN_TOKEN>` | Status snapshot every second (`{"type": "status", "data": {...}}`) |

## Deployment (static IP)

Kite only accepts API **orders** from a static IP whitelisted in the developer
console (data and the WebSocket work from anywhere). Example on AWS, region
**ap-south-1 (Mumbai)**, which is close to the exchange:

1. **Instance:** EC2 `t3.small` or larger, Ubuntu 24.04, 20+ GB gp3 disk.
2. **Static IP:** allocate an **Elastic IP** and associate it with the instance. Whitelist
   that IP in the Kite developer console.
3. **Security group:** inbound SSH (22) from your IP only, plus 443 if you use the TLS
   proxy below. Do **not** open 5432, 6379, 8000 or 8080: compose binds them to
   127.0.0.1.
4. **Install:** Docker Engine with the Compose plugin; enable time sync
   (`timedatectl set-ntp true`). The containers run on IST themselves.
5. **Deploy:**
   ```bash
   git clone <your repo> tradeflow && cd tradeflow
   cp .env.example .env && chmod 600 .env   # fill in secrets; keep MODE=paper for now
   docker compose up --build -d
   ```
6. **Access the dashboard** in one of two ways:
   - **SSH tunnel** (simplest, nothing exposed): `ssh -L 8080:127.0.0.1:8080 ubuntu@<elastic-ip>`,
     then open <http://localhost:8080>. The Kite redirect URL is then
     `http://localhost:8080/api/auth/callback`.
   - **TLS reverse proxy** for phone access: a domain pointing at the Elastic IP, with
     e.g. Caddy on the host (`your.domain { reverse_proxy 127.0.0.1:8080 }`) for
     automatic HTTPS. Set `FRONTEND_URL=https://your.domain` and the Kite redirect URL
     to `https://your.domain/api/auth/callback`. Never serve the dashboard over plain
     HTTP: the admin token travels with every request.
7. **Backups:** schedule `docker compose exec -T postgres pg_dump -U tradeflow tradeflow | gzip > backup-$(date +%F).sql.gz`
   daily and copy the file off the instance (e.g. to S3).
8. **Updates:** `git pull && docker compose up --build -d` outside market hours.
   Migrations run automatically on start.

## Operations

- **Kill switch:** the red button (always visible; pinned to the bottom on phones). Type
  `KILL` to confirm. It stops every strategy, cancels open orders and squares off all
  positions. Exits are always allowed, even after it trips. Reset keeps strategies
  stopped; start them again by hand.
- **Daily loss limit:** `DAILY_LOSS_LIMIT` (net of charges) trips the kill switch
  automatically.
- **Crash recovery:** on start, the backend rebuilds today's positions from the
  journal. In live mode it also compares them with Kite's positions and orders. Any
  mismatch blocks all strategies and alerts you. Fix things in Kite, then
  ```bash
  curl -X POST -H "X-Admin-Token: $ADMIN_TOKEN" -H "Content-Type: application/json" \
       -d '{"confirm": "RESOLVE", "adopt_broker_positions": false}' \
       http://127.0.0.1:8000/recovery/resolve
  ```
  (`adopt_broker_positions: true` books Kite's positions under `__recovered__` so the
  system can square them off.)
- **Telegram:** create a bot with @BotFather, put `TELEGRAM_BOT_TOKEN` and
  `TELEGRAM_CHAT_ID` in `.env`. You get alerts for engine start/stop, fills,
  rejections, strategy errors, the kill switch, recovery mismatches, leg-group
  failures and the end-of-day summary, each prefixed `[PAPER]` or `[LIVE]`.
- **Logs:** `docker compose logs -f backend` (JSON lines; the WebSocket token is
  redacted from access logs).
- **Strategy params** changed on the dashboard are saved to the `backendstate` volume
  and survive the daily restart.

## Testing and CI

```bash
# backend (from backend/): unit + simulated-day + API tests; Kite is fully mocked
ruff check . && ruff format --check . && mypy app tests && pytest
# integration tests need TimescaleDB:
TEST_DATABASE_URL=postgresql+asyncpg://user:pass@localhost:5432/tradeflow_test pytest

# frontend (from frontend/)
npm run lint && npm run typecheck && npm test && npm run build
```

Tests never touch Kite: `backend/tests/conftest.py` makes any real Kite HTTP call or
WebSocket connection fail the test. GitHub Actions (`.github/workflows/ci.yml`)
always runs with `MODE=paper FEED=sim` and has three jobs:

- **backend:** ruff, mypy, pytest against TimescaleDB and Redis. It fails if any
  integration test is skipped.
- **frontend:** lint, typecheck, Vitest, production build.
- **docker:** validate the compose file, build both images, start the whole stack
  and smoke-test it through nginx (health, auth, live status, param persistence).

Dependencies are pinned to exact versions at least 14 days old:
`backend/requirements*.txt` + `constraints.txt`, `frontend/package.json` (with
`overrides` for transitive packages), Docker image tags, and GitHub Actions commits.

## Troubleshooting

| Symptom | Fix |
| --- | --- |
| Dashboard says **DISCONNECTED** | Backend down or restarting: `docker compose ps`, `docker compose logs backend` |
| **Needs a check** / "Robots can't start yet" | Crash recovery found a mismatch or couldn't reach the database. Check Kite, then press **I've checked, continue** on Home (or `POST /recovery/resolve`, see Operations) |
| "Log in to Kite" fails | The redirect URL in the Kite console must equal `<dashboard origin>/api/auth/callback` exactly; `KITE_USER_ID` must match the account |
| Live orders rejected with an IP error | The server's static IP isn't whitelisted in the Kite developer console |
| Robot won't start (409) | Today's plan isn't submitted, the emergency stop is on (clear it), or the engine is blocked |
| Option entries rejected for spread/volume | The strike is illiquid; adjust `OPTION_MAX_SPREAD_PCT` / `OPTION_MIN_VOLUME` or pick another strike |
| Backtest shows missing data | Download it (`download-history`) or let the 15:40 recorder collect it; expired options can't be downloaded |
| `npm install` crashes with "reading 'edgesOut'" | Use npm 11: `npx -y npm@11.21.1 ci` (Node 22) |
| Backend exits every morning at 06:00 | Expected: the Kite token expired; Docker restarts it and it waits for your login |

## Known limitations

- Exchange holidays are not modelled. Scheduled jobs run on a holiday against an idle
  market (harmless), and "trading days before expiry" counts weekdays only.
- Backtests approximate the path inside each candle (O, L/H, C), and model neither
  exercise/assignment STT at expiry nor queue position for limit orders.
- The paper margin estimate is rough: no SPAN, no hedge benefit.
- Kite is fully mocked in tests. The first paper session with `FEED=kite` is the
  first contact with real Kite: watch order updates and fills closely.
- Exchange-side stops are placed when a strategy calls `protect()` (the straddle
  example does); they aren't added automatically for every option leg.
- Charges, freeze limits and Kite's historical-request limits are configuration
  values that change. Re-verify them periodically.

## Going-live checklist

Do not set `MODE=live` until **every** item is done:

- [ ] **Several weeks of profitable paper trading after charges**, using the same
      strategies, params and capital you plan to use live. Backtests alone don't count.
- [ ] Static IP (Elastic IP) **whitelisted** in the Kite developer console; a test
      order from the server has been accepted.
- [ ] `MARKET_PROTECTION` set deliberately (`-1` = automatic, or a % within circuit limits).
- [ ] Risk limits **sized to your capital**: `DAILY_LOSS_LIMIT`, `MAX_LOTS_PER_ORDER`,
      `MAX_OPEN_POSITIONS`, `MAX_ORDERS_PER_MINUTE`; `PAPER_CAPITAL` matched your real
      capital during paper trading.
- [ ] **Start with one lot** per strategy (`lots: 1`) and only one strategy enabled.
- [ ] **Telegram alerts tested:** you received a fill, a rejection and the end-of-day summary.
- [ ] **Kill switch tested from your phone**, through the same URL you'll use live,
      and you saw the positions flatten.
- [ ] Crash recovery tested: restart the backend mid-session in paper mode and check
      that positions are rebuilt correctly.
- [ ] Charges (`.env` rates) and `FREEZE_LIMITS` re-checked against zerodha.com/charges
      and current NSE circulars; lot sizes come from the instrument master.
- [ ] **Latest Zerodha and SEBI rules re-checked**, in particular the SEBI framework
      for retail algorithmic trading (algo ids/registration, order-rate thresholds;
      this system stays below 10 orders/sec) and any Kite API changes.
- [ ] Database backups running and a restore has been tested.
- [ ] `ADMIN_TOKEN` is long and random; the dashboard is only reachable over TLS or
      an SSH tunnel; `.env` is `chmod 600` and not in git.

Then set in `.env`: `MODE=live`, `FEED=kite`, `LIVE_TRADING_CONFIRMED=true`, and run
`docker compose up -d`. The dashboard badge turns **red: LIVE**.
