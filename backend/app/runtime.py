"""Process-level orchestration: when to start the engine, recovery, daily jobs and
session expiry. It does NOT know paper vs live; `factory` supplies the parts.

Startup:
- FEED=sim: start the engine immediately.
- FEED=kite: start once a Kite session exists (at boot, or right after /auth/callback).

Session expiry (06:00 IST, or a TokenException from Kite): stop the engine cleanly,
alert, then ask the process to exit so the container restarts. The Twisted reactor
behind KiteTicker cannot restart in-process, and the next login gets a fresh process.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

import yaml

from app import factory
from app.backtest.runner import BacktestRequest
from app.backtest.store import BacktestStore
from app.brokers.kite import KiteBroker
from app.brokers.session import KiteAuth, KiteSession
from app.config import Settings
from app.core.clock import Clock, ManualClock, SystemClock
from app.data.historical import (
    HistoryDownloader,
    HistoryStore,
    record_underlying,
    session_bounds,
)
from app.engine.engine import TradingEngine
from app.engine.recovery import (
    PositionDiff,
    RecoveryReport,
    adopt_broker_positions,
    compare_with_broker,
    rebuild,
)
from app.notify.base import Notifier
from app.storage.journal import Journal, daily_pnl_rows
from app.strategies.registry import (
    StrategyConfig,
    discover,
    load_configs,
)

BacktestExecutor = Callable[[BacktestRequest, int], Awaitable[None]]


def load_overrides(path: Path) -> dict[str, dict[str, Any]]:
    if not path.exists():
        return {}
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return {str(k): dict(v) for k, v in data.items()} if isinstance(data, dict) else {}


def save_overrides(path: Path, overrides: dict[str, dict[str, Any]]) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(
        "# Written by the dashboard (PUT /strategies/{id}/params). Overrides strategies.yaml.\n"
        + yaml.safe_dump(overrides, sort_keys=True),
        encoding="utf-8",
    )
    tmp.replace(path)


def apply_overrides(
    configs: list[StrategyConfig], overrides: dict[str, dict[str, Any]]
) -> list[StrategyConfig]:
    return [
        c.model_copy(update={"params": overrides[c.id]}) if c.id in overrides else c
        for c in configs
    ]


log = logging.getLogger(__name__)


class Runtime:
    def __init__(
        self,
        settings: Settings,
        *,
        notifier: Notifier,
        journal: Journal,
        auth: KiteAuth | None,
        clock: Clock | None = None,
        exit_process: Callable[[], None] | None = None,
        history: HistoryStore | None = None,
        backtests: BacktestStore | None = None,
        backtest_executor: BacktestExecutor | None = None,
    ) -> None:
        self.settings = settings
        self.notifier = notifier
        self.journal = journal
        self.auth = auth
        self.clock = clock or SystemClock()
        self._exit_process = exit_process
        self.history = history
        self.backtests = backtests
        self._backtest_executor = backtest_executor
        self._backtest_tasks: set[asyncio.Task[None]] = set()
        self.engine: TradingEngine | None = None
        self.broker: KiteBroker | None = None
        self.recovery: RecoveryReport | None = None
        self.session: KiteSession | None = None
        self._start_lock = asyncio.Lock()
        self._kite_feed_used = False
        self._stopping = False
        self.scheduler: Any = None

    # --- lifecycle ----------------------------------------------------------------------

    async def startup(self) -> None:
        await self.journal.start()
        from app.scheduler import build_scheduler  # noqa: PLC0415 - avoids import cycle

        self.scheduler = build_scheduler(self)
        self.scheduler.start()
        if self.settings.feed == "sim":
            await self.start_engine(None)
            return
        session = await self.auth.current_session() if self.auth else None
        if session is not None:
            await self.start_engine(session)
        else:
            log.warning("no Kite session; waiting for /auth/login")

    async def shutdown(self) -> None:
        if self.scheduler is not None:
            with contextlib.suppress(Exception):
                self.scheduler.shutdown(wait=False)
        await self.stop_engine("shutdown")
        await self.journal.stop()
        shutdown = getattr(self._backtest_executor, "shutdown", None)
        if shutdown is not None:
            shutdown()

    async def on_login(self, session: KiteSession) -> None:
        """Called after a successful /auth/callback."""
        if self.settings.feed != "kite":
            return
        if self.engine is not None and self.engine.running:
            log.info("login while engine running; keeping the current engine")
            return
        if self._kite_feed_used:
            await self.notifier.send(
                "New Kite login after the feed stopped: restarting process to reconnect.",
                "warning",
            )
            self._request_exit()
            return
        await self.start_engine(session)

    async def start_engine(self, session: KiteSession | None) -> None:
        async with self._start_lock:
            if self.engine is not None and self.engine.running:
                return
            s = self.settings
            now = self.clock.now()
            today = now.date()
            self.session = session
            self.broker = (
                factory.build_broker(s, session, on_session_expired=self._session_expired_hook)
                if session is not None
                else None
            )
            instruments = await factory.load_instruments(s, self.broker, today)
            feed = factory.build_feed(s, session, today)
            discover()
            configs = load_configs(s.strategies_file) if s.strategies_file.exists() else []
            configs = apply_overrides(configs, load_overrides(s.strategy_overrides_file))
            engine, enabled = factory.build_engine(
                s,
                instruments=instruments,
                feed=feed,
                clock=ManualClock(now),
                notifier=self.notifier,
                strategy_configs=configs,
                realtime=True,
                broker=self.broker,
                journal=self.journal,
            )
            self.engine = engine
            self.recovery = await self._recover(engine, today)
            if not self.recovery.ok:
                engine.block("; ".join(self.recovery.mismatches)[:500])
            await engine.start(enabled)
            if s.feed == "kite":
                self._kite_feed_used = True
            log.info("engine started", extra={"enabled": enabled})

    async def _recover(self, engine: TradingEngine, today: Any) -> RecoveryReport:
        try:
            report = await rebuild(engine, self.journal.store, self.journal.mode, today)
            broker = factory.reconciliation_broker(self.settings, self.broker)
            if broker is not None:
                await compare_with_broker(
                    engine, broker, self.journal.store, self.journal.mode, today, report=report
                )
        except Exception as exc:
            log.exception("crash recovery failed")
            report = RecoveryReport(error=f"recovery could not run: {type(exc).__name__}: {exc}")
        if not report.ok:
            await self.notifier.send(
                "RECOVERY MISMATCH - strategies disabled until resolved:\n"
                + "\n".join(report.mismatches[:20]),
                "critical",
            )
        return report

    async def stop_engine(self, reason: str) -> None:
        engine = self.engine
        if engine is None or not engine.running:
            return
        log.warning("stopping engine", extra={"reason": reason})
        await engine.stop()
        await self.journal.flush()

    # --- operator actions ---------------------------------------------------------------

    async def resolve_recovery(self, adopt_broker_positions_: bool) -> dict[str, Any]:
        engine = self.engine
        if engine is None:
            raise RuntimeError("engine not running")
        adopted = 0
        if adopt_broker_positions_ and self.recovery is not None:
            diffs: list[PositionDiff] = self.recovery.position_diffs
            adopted = adopt_broker_positions(engine, diffs, engine.clock.now())
        engine.unblock()
        if self.recovery is not None:
            self.recovery.position_diffs.clear()
            self.recovery.order_issues.clear()
            self.recovery.error = ""
        await self.notifier.send(
            f"Recovery resolved by operator (adopted {adopted} positions). "
            "Strategies remain stopped; start them from the dashboard.",
            "warning",
        )
        return {"adopted_positions": adopted}

    # --- strategy params & backtests -----------------------------------------------------

    def set_strategy_params(self, strategy_id: str, params: dict[str, Any]) -> dict[str, Any]:
        """Validate, apply (strategy must be stopped) and persist for the next restart."""
        if self.engine is None:
            raise RuntimeError("engine not running")
        validated = self.engine.set_strategy_params(strategy_id, params)
        data = validated.model_dump(mode="json")
        overrides = load_overrides(self.settings.strategy_overrides_file)
        overrides[strategy_id] = data
        save_overrides(self.settings.strategy_overrides_file, overrides)
        return dict(data)

    async def submit_backtest(self, req: BacktestRequest) -> int:
        if self.backtests is None or self._backtest_executor is None:
            raise RuntimeError("backtests are not configured")
        run_id = await self.backtests.create(req)
        store, executor = self.backtests, self._backtest_executor

        async def run() -> None:
            try:
                await executor(req, run_id)
            except Exception as exc:
                log.exception("backtest failed", extra={"run_id": run_id})
                await store.fail(run_id, f"{type(exc).__name__}: {exc}")

        task = asyncio.get_running_loop().create_task(run(), name=f"backtest-{run_id}")
        self._backtest_tasks.add(task)
        task.add_done_callback(self._backtest_tasks.discard)
        return run_id

    # --- scheduled jobs -----------------------------------------------------------------

    async def reload_instruments(self) -> None:
        if self.engine is None or self.broker is None:
            return
        today = self.clock.now().date()
        rows: list[dict[str, Any]] = []
        for exch in ("NSE", "NFO"):
            rows.extend(await self.broker.instruments(exch))
        self.engine.instruments.load_kite_rows(rows, today)

    async def risk_reset(self) -> None:
        if self.engine is not None:
            self.engine.risk.reset_day()

    async def square_off(self) -> None:
        if self.engine is not None and self.engine.running:
            self.engine.scheduled_square_off()

    async def end_of_day(self) -> None:
        engine = self.engine
        if engine is None:
            return
        now = self.clock.now()
        summary = engine.daily_summary()
        rows = daily_pnl_rows(now.date(), self.journal.mode, summary, now)
        try:
            await self.journal.store.upsert_daily_pnl(rows)
        except Exception as exc:
            log.exception("daily_pnl write failed")
            await self.notifier.send(f"daily_pnl write failed: {exc}", "critical")
        acct = summary["__account__"]
        lines = [
            f"End of day {now.date()}: net {acct['net']:.2f} "
            f"(gross {acct['gross']:.2f}, charges {acct['charges']:.2f}, "
            f"{int(acct['trades'])} fills)"
        ]
        for sid, v in summary.items():
            if sid != "__account__":
                lines.append(f"  {sid}: net {v['net']:.2f}, {int(v['trades'])} fills")
        await self.notifier.send("\n".join(lines), "info")

    async def record_candles(self) -> None:
        """Daily: store today's 1-minute candles around ATM (expired contracts can't be
        downloaded later)."""
        engine, broker, history = self.engine, self.broker, self.history
        if engine is None or broker is None or history is None:
            return
        s = self.settings
        start, end = session_bounds(self.clock.now().date())
        downloader = HistoryDownloader(broker, history, s.history_chunk_days)
        results = []
        for und in s.record_underlyings:
            try:
                results += await record_underlying(
                    downloader,
                    history,
                    engine.instruments,
                    und,
                    start=start,
                    end=end,
                    width=s.record_strikes_width,
                    expiries=s.record_expiries,
                )
            except Exception as exc:
                log.exception("candle recording failed", extra={"underlying": und})
                await self.notifier.send(f"Candle recording failed for {und}: {exc}", "warning")
        failed = [f"{r.instrument}: {f}" for r in results for f in r.failed_chunks]
        candles = sum(r.candles for r in results)
        await self.notifier.send(
            f"Recorded {candles} candles for {len(results)} instruments"
            + (f"; {len(failed)} failures, first: {failed[0]}" if failed else ""),
            "warning" if failed else "info",
        )

    async def session_expired(self) -> None:
        if self.settings.feed != "kite" or self._stopping:
            return
        self._stopping = True
        await self.notifier.send("Kite session expired: stopping engine.", "warning")
        await self.stop_engine("kite session expired")
        if self.auth is not None:
            with contextlib.suppress(Exception):
                await self.auth.logout()
        self._request_exit()

    def _session_expired_hook(self) -> None:
        # Called from KiteBroker inside a request; schedule the async stop.
        with contextlib.suppress(RuntimeError):
            asyncio.get_running_loop().create_task(self.session_expired())

    def _request_exit(self) -> None:
        if self._exit_process is not None:
            self._exit_process()

    # --- status -------------------------------------------------------------------------

    def status(self) -> dict[str, Any]:
        e = self.engine
        return {
            "engine_running": bool(e and e.running),
            "blocked_reason": e.blocked_reason if e else "",
            "recovery": self.recovery.as_dict() if self.recovery else None,
            "journal": {
                "backlog": self.journal.backlog,
                "failures": self.journal.failures,
                "last_error": self.journal.last_error,
            },
        }
