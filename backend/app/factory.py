"""Composition root. This is the ONLY module that branches on paper vs live.

Everything else receives already-wired collaborators and never inspects the mode.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import date
from typing import TYPE_CHECKING, Any

from app.brokers.kite import KiteBroker
from app.brokers.session import KiteAuth, KiteSession, RedisTokenStore, TokenStore
from app.config import Settings
from app.core.clock import Clock, ManualClock, SystemClock
from app.core.models import Tick
from app.core.ratelimit import RateLimiter
from app.data.instruments import InstrumentStore
from app.engine.engine import EngineConfig, OrderJournal, TradingEngine
from app.engine.positions import PositionBook
from app.engine.smart_orders import OptionOrderRules
from app.execution.base import Executor
from app.execution.live import LiveExecutor
from app.execution.paper import PaperExecutor
from app.feeds.base import Feed
from app.feeds.kite import KiteFeed
from app.feeds.random_walk import RandomWalkFeed, synthetic_nifty
from app.notify.base import LogNotifier, Notifier
from app.notify.telegram import CompositeNotifier, TelegramNotifier
from app.risk.manager import RiskLimits, RiskManager
from app.risk.margin import EstimatedMargin, KiteMargin, MarginChecker
from app.storage.db import make_engine, make_sessionmaker
from app.storage.journal import Journal, SqlJournalStore
from app.strategies.registry import StrategyConfig, discover
from app.strategies.registry import build as build_strategy

if TYPE_CHECKING:
    from app.runtime import Runtime


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


def build_broker(
    settings: Settings,
    session: KiteSession,
    on_session_expired: Callable[[], None] | None = None,
) -> KiteBroker:
    """Kite REST wrapper. Order endpoints are enabled only in live mode."""
    return KiteBroker(
        build_kite_client(settings, session.access_token),
        orders_enabled=settings.is_live,
        market_protection=settings.market_protection,
        order_limiter=RateLimiter(
            [(int(settings.order_rate_per_sec), 1.0), (settings.order_rate_per_min, 60.0)]
        ),
        on_session_expired=on_session_expired,
    )


def reconciliation_broker(settings: Settings, broker: KiteBroker | None) -> KiteBroker | None:
    """Live mode checks recovered state against Kite; paper has no broker state to compare."""
    return broker if settings.is_live else None


async def load_instruments(
    settings: Settings, broker: KiteBroker | None, today: date
) -> InstrumentStore:
    store = InstrumentStore()
    if settings.feed == "kite":
        if broker is None:
            raise RuntimeError("FEED=kite needs a Kite session to load instruments")
        rows: list[dict[str, Any]] = []
        for exchange in ("NSE", "NFO"):
            rows.extend(await broker.instruments(exchange))
        store.load_kite_rows(rows, today)
    else:
        store.load(synthetic_nifty(today).all, today)
    return store


def build_notifier(settings: Settings) -> Notifier:
    sinks: list[Notifier] = [LogNotifier()]
    if settings.telegram_enabled:
        sinks.append(
            TelegramNotifier(
                settings.telegram_bot_token.get_secret_value(), settings.telegram_chat_id
            )
        )
    return CompositeNotifier(*sinks, prefix="[LIVE] " if settings.is_live else "[PAPER] ")


def build_journal(settings: Settings, notifier: Notifier) -> Journal:
    sessions = make_sessionmaker(make_engine(settings.database_url))
    return Journal(SqlJournalStore(sessions), settings.mode, notifier)


def build_feed(settings: Settings, session: KiteSession | None, today: date) -> Feed:
    if settings.feed == "kite":
        if session is None:
            raise RuntimeError("FEED=kite needs a valid Kite session; log in via /auth/login")
        return KiteFeed(settings.kite_api_key, session.access_token)
    return RandomWalkFeed(synthetic_nifty(today))


def build_executor(settings: Settings, clock: Clock, broker: KiteBroker | None) -> Executor:
    if settings.is_live:
        settings.assert_safe()  # belt and braces: never build a live executor unsafely
        if broker is None or not broker.orders_enabled:
            raise RuntimeError("live mode needs a Kite session with order endpoints enabled")
        return LiveExecutor(broker, clock, freeze_limits=settings.freeze_limits)
    return PaperExecutor(clock, slippage_ticks=settings.paper_slippage_ticks)


def engine_config(settings: Settings) -> EngineConfig:
    return EngineConfig(
        square_off_time=settings.square_off_time,
        market_open=settings.market_open,
        option_rules=OptionOrderRules(
            enabled=settings.option_limit_orders,
            buffer_ticks=settings.option_limit_buffer_ticks,
            exit_buffer_pct=settings.option_exit_buffer_pct,
            chase_after_sec=settings.option_chase_after_sec,
            chase_max=settings.option_chase_max,
        ),
        stop_limit_buffer_pct=settings.stop_limit_buffer_pct,
        leg_group_timeout_sec=settings.leg_group_timeout_sec,
        risk_free_rate=settings.risk_free_rate,
        index_underlyings=frozenset(settings.index_underlyings),
        physical_settlement_exit_days=settings.physical_settlement_exit_days,
        physical_settlement_exit_time=settings.physical_settlement_exit_time,
    )


class _QuoteProxy:
    """Lets the paper margin estimator read prices from the engine built after it."""

    engine: TradingEngine | None = None

    def __call__(self, token: int) -> Tick | None:
        return self.engine.last_tick(token) if self.engine else None


def build_margin_checker(
    settings: Settings,
    broker: KiteBroker | None,
    positions: PositionBook,
    quote: Callable[[int], Tick | None],
) -> MarginChecker | None:
    """Live: Kite's margin APIs. Paper/backtest: a configurable estimate."""
    if not settings.margin_check_enabled:
        return None
    if settings.is_live:
        if broker is None:
            raise RuntimeError("live mode needs a Kite session for margin checks")
        return KiteMargin(broker)
    return EstimatedMargin(settings.paper_capital, settings.paper_margin_pct, quote)


def build_engine(
    settings: Settings,
    *,
    instruments: InstrumentStore,
    feed: Feed,
    clock: ManualClock,
    notifier: Notifier,
    strategy_configs: list[StrategyConfig],
    realtime: bool,
    broker: KiteBroker | None = None,
    journal: OrderJournal | None = None,
) -> tuple[TradingEngine, list[str]]:
    """Wire the engine. Returns the engine and the ids of strategies enabled at start."""
    positions = PositionBook()
    risk = RiskManager(RiskLimits.from_settings(settings), positions, clock)
    clock_quote = _QuoteProxy()
    engine = TradingEngine(
        instruments=instruments,
        feed=feed,
        executor=build_executor(settings, clock, broker),
        risk=risk,
        positions=positions,
        clock=clock,
        notifier=notifier,
        config=engine_config(settings),
        wall_clock=SystemClock() if realtime else None,
        journal=journal,
        margin=build_margin_checker(settings, broker, positions, clock_quote),
    )
    clock_quote.engine = engine
    discover()
    for cfg in strategy_configs:
        engine.add_strategy(build_strategy(cfg))
    return engine, [c.id for c in strategy_configs if c.enabled]


def build_runtime(settings: Settings, auth: KiteAuth | None) -> Runtime:
    import os  # noqa: PLC0415
    import signal  # noqa: PLC0415

    from app.runtime import Runtime  # noqa: PLC0415 - runtime imports factory

    notifier = build_notifier(settings)

    def exit_process() -> None:
        # Let uvicorn shut down gracefully; Docker's restart policy brings us back.
        os.kill(os.getpid(), signal.SIGTERM)

    return Runtime(
        settings,
        notifier=notifier,
        journal=build_journal(settings, notifier),
        auth=auth,
        exit_process=exit_process if settings.exit_on_session_expiry else None,
    )
