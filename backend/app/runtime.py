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
import dataclasses
import logging
from collections.abc import Awaitable, Callable
from datetime import date, datetime
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
from app.engine.engine import StrategyState, TradingEngine
from app.engine.recovery import (
    PositionDiff,
    RecoveryReport,
    adopt_broker_positions,
    compare_with_broker,
    rebuild,
)
from app.notify.base import Notifier
from app.risk.manager import PlanLimits, StrategyLimits
from app.risk.plan import (
    LOOSEN_CONFIRMATION,
    AccountPlan,
    DailyPlan,
    StrategyPlan,
    describe_changes,
    loosened,
)
from app.storage.journal import Journal, daily_pnl_rows
from app.storage.plans import MemoryPlanStore, PlanStore
from app.strategies.registry import (
    StrategyConfig,
    discover,
    load_configs,
)

BacktestExecutor = Callable[[BacktestRequest, int], Awaitable[None]]


class PlanError(ValueError):
    """The submitted plan is invalid."""


class PlanLooseningError(RuntimeError):
    """Loosening limits during market hours needs `confirm: "LOOSEN"`."""

    def __init__(self, loosened: list[str]) -> None:
        super().__init__("loosening limits during market hours needs confirmation")
        self.loosened = loosened


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
        plans: PlanStore | None = None,
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
        self.plans: PlanStore = plans or MemoryPlanStore()
        self.plan: DailyPlan | None = None
        self.plan_date: date | None = None
        self.plan_submitted_at: datetime | None = None
        self._plan_lock = asyncio.Lock()
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
                require_daily_plan=True,  # no strategy starts before today's plan
            )
            self.engine = engine
            self.recovery = await self._recover(engine, today)
            if not self.recovery.ok:
                engine.block("; ".join(self.recovery.mismatches)[:500])
            await engine.start(enabled)
            await self._apply_stored_plan(today)
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

    # --- daily plan (Settings tab) --------------------------------------------------------

    def _configs(self) -> list[StrategyConfig]:
        s = self.settings
        configs = load_configs(s.strategies_file) if s.strategies_file.exists() else []
        return apply_overrides(configs, load_overrides(s.strategy_overrides_file))

    def default_plan(self) -> DailyPlan:
        """Pre-fill when no plan was ever submitted: the .env limits and the
        strategies.yaml enabled flags and lots."""
        s = self.settings
        strategies: dict[str, StrategyPlan] = {}
        for cfg in self._configs():
            lots = cfg.params.get("lots")
            if self.engine is not None and cfg.id in self.engine.slots:
                lots = getattr(self.engine.slots[cfg.id].strategy.params, "lots", lots)
            strategies[cfg.id] = StrategyPlan(
                enabled=cfg.enabled, lots=int(lots) if isinstance(lots, int) else None
            )
        return DailyPlan(
            account=AccountPlan(
                daily_loss_limit=s.daily_loss_limit,
                max_open_positions=s.max_open_positions,
                max_lots_per_order=s.max_lots_per_order,
                trading_start=s.trading_start,
                trading_end=s.trading_end,
            ),
            strategies=strategies,
        )

    async def plan_for_form(self) -> tuple[DailyPlan, bool]:
        """(plan to show, submitted today?). Not submitted: the last plan pre-filled."""
        today = self.clock.now().date()
        if self.plan is not None and self.plan_date == today:
            return self.plan, True
        base = self.default_plan()
        try:
            last = await self.plans.latest(self.settings.mode)
        except Exception:
            log.exception("could not load the last plan")
            last = None
        if last is not None:
            prev = DailyPlan.model_validate(last.plan)
            # Keep today's strategy list; carry over earlier values where they exist.
            base = DailyPlan(
                account=prev.account,
                strategies={
                    sid: prev.strategies.get(sid, sp) for sid, sp in base.strategies.items()
                },
            )
        return base, False

    def market_hours_now(self) -> bool:
        now = self.clock.now()
        s = self.settings
        return now.weekday() < 5 and s.market_open <= now.time() <= s.market_close

    def _validate_plan(self, plan: DailyPlan) -> None:
        s = self.settings
        a = plan.account
        if a.trading_start < s.market_open or a.trading_end > s.square_off_time:
            raise PlanError(
                f"trading window must lie between market open {s.market_open:%H:%M} "
                f"and square-off {s.square_off_time:%H:%M}"
            )
        known = {c.id for c in self._configs()}
        if self.engine is not None:
            known |= set(self.engine.slots)
        unknown = set(plan.strategies) - known
        if unknown:
            raise PlanError(f"unknown strategies: {sorted(unknown)}")
        engine = self.engine
        if engine is None:
            return
        for sid, sp in plan.strategies.items():
            slot = engine.slots.get(sid)
            if slot is None or sp.lots is None:
                continue
            if "lots" not in type(slot.strategy.params).model_fields:
                raise PlanError(f"{sid} has no `lots` parameter")
            if sp.lots != slot.strategy.params.lots and slot.state is StrategyState.RUNNING:
                raise PlanError(f"stop {sid} before changing its lots")

    def _apply_plan(self, plan: DailyPlan, today: date) -> dict[str, list[str]]:
        """Push the plan into the engine. Returns started / stopped / warnings."""
        engine = self.engine
        result: dict[str, list[str]] = {"started": [], "stopped": [], "warnings": []}
        if engine is None:
            result["warnings"].append("engine not running: the plan applies when it starts")
            return result
        a = plan.account
        engine.risk.limits = dataclasses.replace(
            engine.risk.limits,
            daily_loss_limit=a.daily_loss_limit,
            max_open_positions=a.max_open_positions,
            max_lots_per_order=a.max_lots_per_order,
            trading_start=a.trading_start,
            trading_end=a.trading_end,
        )
        engine.risk.plan = PlanLimits(
            profit_target=a.profit_target,
            max_trades_per_day=a.max_trades_per_day,
            strategies={
                sid: StrategyLimits(
                    max_trades_per_day=sp.max_trades_per_day,
                    loss_limit=sp.loss_limit,
                    profit_target=sp.profit_target,
                    max_lots_per_order=sp.max_lots_per_order,
                    max_capital=sp.max_capital,
                )
                for sid, sp in plan.strategies.items()
            },
        )
        engine.plan_date = today
        for sid, sp in plan.strategies.items():
            slot = engine.slots.get(sid)
            if slot is None:
                continue
            current_lots = getattr(slot.strategy.params, "lots", sp.lots)
            if sp.lots is not None and sp.lots != current_lots:
                self.set_strategy_params(
                    sid, {**slot.strategy.params.model_dump(mode="json"), "lots": sp.lots}
                )
            if sp.enabled and slot.state is not StrategyState.RUNNING:
                try:
                    engine.start_strategy(sid)
                    result["started"].append(sid)
                except RuntimeError as exc:  # blocked engine or kill switch
                    result["warnings"].append(f"{sid} not started: {exc}")
            elif not sp.enabled and slot.state is StrategyState.RUNNING:
                engine.stop_strategy(sid)  # positions kept, with their stops
                result["stopped"].append(sid)
        return result

    async def submit_plan(self, plan: DailyPlan, confirm: str | None = None) -> dict[str, Any]:
        """Validate, check loosening, apply, persist and alert."""
        async with self._plan_lock:
            today = self.clock.now().date()
            self._validate_plan(plan)
            previous = self.plan if self.plan_date == today else None
            looser = loosened(previous, plan) if previous is not None else []
            if looser and self.market_hours_now() and confirm != LOOSEN_CONFIRMATION:
                raise PlanLooseningError(looser)
            changes = describe_changes(previous, plan)
            result = self._apply_plan(plan, today)
            now = self.clock.now()
            if previous is None:
                self.plan_submitted_at = now
            self.plan, self.plan_date = plan, today
            try:
                await self.plans.save(
                    today,
                    self.settings.mode,
                    plan.model_dump(mode="json"),
                    now=now,
                    changes=changes,
                    loosened=looser,
                )
            except Exception as exc:
                log.exception("could not save the daily plan")
                result["warnings"].append(f"plan applied but not saved: {exc}")
                await self.notifier.send(f"Daily plan could not be saved: {exc}", "critical")
            if looser:
                await self.notifier.send(
                    "Daily plan LOOSENED during market hours:\n" + "\n".join(looser),
                    "critical",
                )
            summary = "Daily plan submitted" if previous is None else "Daily plan updated"
            if previous is not None:
                summary += ": " + "; ".join(changes[:10])
            if result["started"]:
                summary += f". Started: {', '.join(result['started'])}"
            await self.notifier.send(summary, "info")
            return {"changes": changes, "loosened": looser, **result}

    async def _apply_stored_plan(self, today: date) -> None:
        """After a (re)start, re-apply a plan already submitted today."""
        plan = self.plan if self.plan_date == today else None
        if plan is None:
            try:
                stored = await self.plans.get(today, self.settings.mode)
            except Exception:
                log.exception("could not load today's plan")
                stored = None
            if stored is not None:
                plan = DailyPlan.model_validate(stored.plan)
                self.plan, self.plan_date = plan, today
                self.plan_submitted_at = stored.submitted_at
        if plan is not None:
            try:
                self._validate_plan(plan)
                self._apply_plan(plan, today)
            except Exception:
                log.exception("today's plan could not be re-applied; submit it again")

    async def plan_history(self, day: date | None = None) -> list[dict[str, Any]]:
        return await self.plans.events(day or self.clock.now().date(), self.settings.mode)

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
