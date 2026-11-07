# tradeflow

Algorithmic F&O trading on NSE (NFO) through Zerodha Kite Connect. Strategies run
automatically. A human logs in to Kite each morning, watches the dashboard, and
controls strategies and the kill switch.

> **Paper is the default everywhere.** `paper` uses live market data with simulated
> fills and sends no orders. `live` sends real orders with real money and refuses to
> start unless `MODE=live`, `LIVE_TRADING_CONFIRMED=true`, an `ADMIN_TOKEN` of 16+
> characters, `FEED=kite` and Kite API keys are all set. Read the
> [going-live checklist](#going-live-checklist) before touching any of those.

- [Architecture](#architecture)
- [Local setup](#local-setup)
- [Daily routine: Kite login](#daily-routine-kite-login)
- [Adding a strategy](#adding-a-strategy)
- [Historical data and backtests](#historical-data-and-backtests)
- [Deployment (static IP)](#deployment-static-ip)
- [Operations](#operations)
- [Going-live checklist](#going-live-checklist)

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
3. At 06:00 the next day the backend stops cleanly, flushes the journal and exits.
   Docker restarts it, and it waits for your next login. (KiteTicker's network
   reactor can only start once per process, so a fresh process is required.)

Scheduled jobs (IST, weekdays): 08:45 instrument reload · 09:00 daily risk reset
(the kill switch is only reset by a human) · `SQUARE_OFF_TIME` MIS square-off ·
15:35 end-of-day P&L summary (database + Telegram) · 15:40 recording of 1-minute
option candles around ATM.

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
