"""Composition root. This is the ONLY module that branches on paper vs live.

Everything else receives already-wired collaborators and never inspects the mode.
"""

from __future__ import annotations

from datetime import date
from typing import Any

from app.brokers.kite import KiteBroker
from app.brokers.session import KiteAuth, KiteSession, RedisTokenStore, TokenStore
from app.config import Settings
from app.core.clock import Clock, ManualClock, SystemClock
from app.core.ratelimit import RateLimiter
from app.data.instruments import InstrumentStore
from app.engine.engine import EngineConfig, TradingEngine
from app.engine.positions import PositionBook
from app.execution.base import Executor
from app.execution.paper import PaperExecutor
from app.feeds.base import Feed
from app.feeds.kite import KiteFeed
from app.feeds.random_walk import RandomWalkFeed, synthetic_nifty
from app.notify.base import Notifier
from app.risk.manager import RiskLimits, RiskManager
from app.strategies.registry import StrategyConfig, discover
from app.strategies.registry import build as build_strategy


def build_kite_client(settings: Settings, access_token: str | None = None) -> Any:
    from kiteconnect import KiteConnect  # noqa: PLC0415 - heavy import, only when needed

    return KiteConnect(api_key=settings.kite_api_key, access_token=access_token, timeout=10)


def build_token_store(settings: Settings) -> TokenStore:
    from redis.asyncio import Redis  # noqa: PLC0415

    return RedisTokenStore(Redis.from_url(settings.redis_url))


def build_auth(settings: Settings, store: TokenStore) -> KiteAuth:
    return KiteAuth(
        build_kite_client(settings),
        settings.kite_api_secret.get_secret_value(),
        store,
        expected_user_id=settings.kite_user_id,
        expiry_time=settings.session_expiry_time,
    )


def build_broker(settings: Settings, session: KiteSession) -> KiteBroker:
    """Kite REST wrapper. Order endpoints are enabled only in live mode."""
    return KiteBroker(
        build_kite_client(settings, session.access_token),
        orders_enabled=settings.is_live,
        market_protection=settings.market_protection,
        order_limiter=RateLimiter(
            [(int(settings.order_rate_per_sec), 1.0), (settings.order_rate_per_min, 60.0)]
        ),
    )


def build_feed(settings: Settings, session: KiteSession | None, today: date) -> Feed:
    if settings.feed == "kite":
        if session is None:
            raise RuntimeError("FEED=kite needs a valid Kite session; log in via /auth/login")
        return KiteFeed(settings.kite_api_key, session.access_token)
    return RandomWalkFeed(synthetic_nifty(today))


def build_executor(settings: Settings, clock: Clock) -> Executor:
    if settings.is_live:
        raise NotImplementedError("LiveExecutor arrives in Phase 4; live trading is unavailable")
    return PaperExecutor(clock, slippage_ticks=settings.paper_slippage_ticks)


def build_engine(
    settings: Settings,
    *,
    instruments: InstrumentStore,
    feed: Feed,
    clock: ManualClock,
    notifier: Notifier,
    strategy_configs: list[StrategyConfig],
    realtime: bool,
) -> tuple[TradingEngine, list[str]]:
    """Wire the engine. Returns the engine and the ids of strategies enabled at start."""
    positions = PositionBook()
    risk = RiskManager(RiskLimits.from_settings(settings), positions, clock)
    engine = TradingEngine(
        instruments=instruments,
        feed=feed,
        executor=build_executor(settings, clock),
        risk=risk,
        positions=positions,
        clock=clock,
        notifier=notifier,
        config=EngineConfig(
            square_off_time=settings.square_off_time, market_open=settings.market_open
        ),
        wall_clock=SystemClock() if realtime else None,
    )
    discover()
    for cfg in strategy_configs:
        engine.add_strategy(build_strategy(cfg))
    return engine, [c.id for c in strategy_configs if c.enabled]
