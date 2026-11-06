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
    database_url: str = "postgresql+asyncpg://tradeflow:tradeflow@localhost:5432/tradeflow"
    strategies_file: Path = Path("strategies.yaml")

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
    max_lots_per_order: int = Field(default=10, ge=1)
    max_open_positions: int = Field(default=6, ge=1)
    max_orders_per_minute: int = Field(default=30, ge=1)
    daily_loss_limit: float = Field(default=10_000.0, gt=0, description="INR, net of charges")
    duplicate_order_window_sec: float = Field(default=2.0, ge=0)

    # --- Kite order handling ----------------------------------------------------
    # Required by Kite for MARKET and SL-M API orders: -1 = automatic, else 0 < x <= 100 (%).
    market_protection: float = -1
    # Kept below Kite's 10 orders/sec/segment limit (above it needs exchange registration).
    order_rate_per_sec: float = Field(default=8.0, gt=0, le=9.0)
    order_rate_per_min: int = Field(default=350, ge=1, le=400)

    # --- paper trading ----------------------------------------------------------
    paper_slippage_ticks: int = Field(default=1, ge=0)

    # --- Telegram ---------------------------------------------------------------
    telegram_bot_token: SecretStr = SecretStr("")
    telegram_chat_id: str = ""

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
