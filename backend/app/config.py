"""Application settings, the paper/live mode switch and the live-trading safety latches.

Every value comes from environment variables (or a local `.env`). Defaults are
always the safe choice: paper mode, simulated feed, no live confirmation.
"""

from __future__ import annotations

from datetime import time
from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

Mode = Literal["paper", "live"]
FeedSource = Literal["kite", "sim"]
Storage = Literal["postgres", "memory"]

MIN_ADMIN_TOKEN_LEN = 16


class UnsafeConfigError(RuntimeError):
    """Raised when the configuration does not satisfy the safety latches."""


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # --- mode -----------------------------------------------------------------
    mode: Mode = "paper"
    feed: FeedSource = "sim"
    live_trading_confirmed: bool = False

    # --- auth / Kite ------------------------------------------------------------
    admin_token: SecretStr = SecretStr("")
    kite_api_key: str = ""
    kite_api_secret: SecretStr = SecretStr("")
    # Optional but recommended: reject Kite logins from any other account.
    kite_user_id: str = ""
    # Where /auth/callback sends the browser after a successful Kite login.
    frontend_url: str = "http://localhost:5173"
    # CORS: the only origin allowed to call the API from a browser.
    frontend_origin: str = "http://localhost:5173"

    # --- infrastructure ---------------------------------------------------------
    redis_url: str = "redis://localhost:6379/0"
    # postgres: the durable journal (required for live). memory: practice without a
    # database (paper only); history, plans and backtest results are lost on exit.
    storage: Storage = "postgres"
    database_url: str = "postgresql+asyncpg://tradeflow:tradeflow@localhost:5432/tradeflow"
    strategies_file: Path = Path("strategies.yaml")
    # Params changed from the dashboard (PUT /strategies/{id}/params) survive the
    # daily restart here. Not committed to git.
    strategy_overrides_file: Path = Path("strategies.local.yaml")

    # --- trading window (IST) ---------------------------------------------------
    market_open: time = time(9, 15)
    market_close: time = time(15, 30)
    # New (risk-increasing) orders are only accepted inside [trading_start, trading_end).
    trading_start: time = time(9, 15)
    trading_end: time = time(15, 10)
    square_off_time: time = time(15, 15)
    # Kite access tokens expire every morning; sessions are valid until this time.
    session_expiry_time: time = time(6, 0)

    # --- risk limits ------------------------------------------------------------
    # Conservative defaults for beginners: 1 lot, 1 trade a day, lose at most 500,
    # stop new trades after a 1500 profit. They pre-fill the first daily plan.
    max_lots_per_order: int = Field(default=1, ge=1)
    max_open_positions: int = Field(default=6, ge=1)
    max_orders_per_minute: int = Field(default=30, ge=1)
    daily_loss_limit: float = Field(default=500.0, gt=0, description="INR, net of charges")
    # Daily-plan defaults; 0 = no limit.
    profit_target: float = Field(default=1_500.0, ge=0, description="INR, net; 0 = none")
    max_trades_per_day: int = Field(default=1, ge=0, description="new entries; 0 = no limit")
    duplicate_order_window_sec: float = Field(default=2.0, ge=0)

    # --- Kite order handling ----------------------------------------------------
    # Required by Kite for MARKET and SL-M API orders: -1 = automatic, else 0 < x <= 100 (%).
    market_protection: float = -1
    # Kept below Kite's 10 orders/sec/segment limit (above it needs exchange registration).
    order_rate_per_sec: float = Field(default=8.0, gt=0, le=9.0)
    order_rate_per_min: int = Field(default=350, ge=1, le=400)

    # --- paper trading ----------------------------------------------------------
    paper_slippage_ticks: int = Field(default=1, ge=0)
    # Paper margin estimate: capital and margin as a fraction of notional for futures
    # and short options (long options block their premium).
    paper_capital: float = Field(default=1_000_000.0, gt=0)
    paper_margin_pct: float = Field(default=0.15, gt=0, le=1)

    # --- F&O ----------------------------------------------------------------------
    # Pre-trade margin check for opening orders and leg groups.
    margin_check_enabled: bool = True
    # Exchange freeze quantity (units) per underlying. THESE CHANGE: verify on NSE
    # before going live. Orders above the limit are auto-sliced by Kite (max 10 slices).
    freeze_limits: dict[str, int] = Field(
        default_factory=lambda: {
            "NIFTY": 1800,
            "BANKNIFTY": 900,
            "FINNIFTY": 1800,
            "MIDCPNIFTY": 2800,
        }
    )
    # Index underlyings are cash-settled; everything else (stock F&O) is physically settled.
    index_underlyings: list[str] = Field(
        default_factory=lambda: ["NIFTY", "BANKNIFTY", "FINNIFTY", "MIDCPNIFTY", "SENSEX", "BANKEX"]
    )
    risk_free_rate: float = Field(default=0.065, ge=0, le=0.5)
    # Options: MARKET requests become LIMIT orders priced from bid/ask plus a buffer,
    # then re-priced toward the market if still open after `option_chase_after_sec`.
    option_limit_orders: bool = True
    option_limit_buffer_ticks: int = Field(default=2, ge=0)
    option_exit_buffer_pct: float = Field(default=2.0, ge=0, le=20)
    option_chase_after_sec: float = Field(default=5.0, gt=0)
    option_chase_max: int = Field(default=10, ge=0, le=24)  # Kite allows 25 modifications
    # Reject option entries whose spread or day volume is outside these limits.
    option_max_spread_pct: float = Field(default=5.0, gt=0)
    option_min_volume: int = Field(default=10_000, ge=0)
    # Exchange-side stop orders are SL (stop-limit) with this limit buffer beyond the trigger.
    stop_limit_buffer_pct: float = Field(default=5.0, gt=0, le=50)
    # Expiry handling.
    expiry_no_entry_after: time = time(14, 30)
    physical_settlement_exit_days: int = Field(default=1, ge=0, le=5)  # trading days before
    physical_settlement_exit_time: time = time(15, 0)
    # Multi-leg orders: unwind if not fully filled within this time.
    leg_group_timeout_sec: float = Field(default=30.0, gt=0)

    # --- Telegram ---------------------------------------------------------------
    telegram_bot_token: SecretStr = SecretStr("")
    telegram_chat_id: str = ""

    # --- charges (Zerodha NSE F&O) ----------------------------------------------
    # THESE RATES CHANGE. Taken from zerodha.com/charges on 2026-11-07, which gives
    # no effective dates. Verify them before trusting any net P&L figure.
    brokerage_per_order: float = 20.0  # flat, per executed order
    brokerage_futures_pct: float = 0.03  # futures: lower of this % or the flat fee
    stt_futures_sell_pct: float = 0.05
    stt_options_sell_pct: float = 0.15  # on premium
    exchange_txn_futures_pct: float = 0.00183
    exchange_txn_options_pct: float = 0.03553  # on premium
    sebi_fee_per_crore: float = 10.0
    ipft_per_crore: float = 0.01
    stamp_futures_buy_pct: float = 0.002
    stamp_options_buy_pct: float = 0.003
    gst_pct: float = 18.0  # on brokerage + exchange txn + SEBI (+ IPFT)

    # --- historical data ----------------------------------------------------------
    # Days per Kite historical request, by interval. The Kite docs don't state these
    # limits (checked); these are the commonly reported values. Verify them.
    history_chunk_days: dict[str, int] = Field(
        default_factory=lambda: {
            "minute": 60,
            "3minute": 100,
            "5minute": 100,
            "10minute": 100,
            "15minute": 200,
            "30minute": 200,
            "60minute": 400,
            "day": 2000,
        }
    )
    # Daily recording of 1-minute candles (expired contracts can't be fetched later).
    record_underlyings: list[str] = Field(default_factory=lambda: ["NIFTY", "BANKNIFTY"])
    record_strikes_width: int = Field(default=10, ge=1, le=50)  # strikes either side of ATM
    record_expiries: int = Field(default=2, ge=1, le=6)

    # --- API --------------------------------------------------------------------
    # /docs and /openapi.json are unauthenticated, so they're off unless enabled.
    api_docs: bool = False

    # --- process --------------------------------------------------------------
    # Start the engine when the API starts (tests turn this off).
    engine_autostart: bool = True
    # After the Kite session expires, exit so Docker restarts a fresh process
    # (the KiteTicker reactor cannot restart in-process).
    exit_on_session_expiry: bool = True

    # --- logging ----------------------------------------------------------------
    log_level: str = "INFO"
    log_json: bool = True

    @field_validator("market_protection")
    @classmethod
    def _check_market_protection(cls, v: float) -> float:
        if v == -1 or 0 < v <= 100:
            return v
        raise ValueError("market_protection must be -1 (automatic) or 0 < x <= 100")

    @model_validator(mode="after")
    def _check_times(self) -> Settings:
        if not self.trading_start < self.trading_end <= self.square_off_time:
            raise ValueError("expected trading_start < trading_end <= square_off_time")
        if not self.market_open <= self.trading_start or self.square_off_time > self.market_close:
            raise ValueError("trading window must lie inside market hours")
        return self

    @property
    def is_live(self) -> bool:
        return self.mode == "live"

    @property
    def telegram_enabled(self) -> bool:
        return bool(self.telegram_bot_token.get_secret_value() and self.telegram_chat_id)

    def live_latch_failures(self) -> list[str]:
        """Return the reasons live mode is not allowed (empty list = all latches set)."""
        problems: list[str] = []
        if self.mode != "live":
            problems.append("MODE is not 'live'")
        if not self.live_trading_confirmed:
            problems.append("LIVE_TRADING_CONFIRMED is not true")
        if len(self.admin_token.get_secret_value()) < MIN_ADMIN_TOKEN_LEN:
            problems.append(f"ADMIN_TOKEN must be at least {MIN_ADMIN_TOKEN_LEN} characters")
        if self.feed != "kite":
            problems.append("FEED must be 'kite' (live trading on a simulated feed is refused)")
        if not self.kite_api_key or not self.kite_api_secret.get_secret_value():
            problems.append("KITE_API_KEY and KITE_API_SECRET must be set")
        if self.storage != "postgres":
            problems.append("STORAGE must be 'postgres' (live needs the durable order journal)")
        return problems

    def assert_safe(self) -> None:
        """Refuse to run live unless every latch is set. Paper mode always passes."""
        if self.mode != "live":
            return
        problems = self.live_latch_failures()
        if problems:
            raise UnsafeConfigError("Refusing to start in LIVE mode: " + "; ".join(problems))


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
