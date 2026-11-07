"""Runtime, scheduler and recovery API (sim feed, in-memory journal, no network)."""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from datetime import datetime, time
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

from app import factory
from app.config import Settings, UnsafeConfigError, get_settings
from app.core.clock import IST, ManualClock
from app.execution.live import LiveExecutor
from app.execution.paper import PaperExecutor
from app.main import app
from app.notify.base import LogNotifier
from app.notify.telegram import CompositeNotifier
from app.runtime import Runtime
from app.scheduler import build_scheduler
from app.storage.journal import Journal, MemoryJournalStore

MONDAY_8AM = datetime(2025, 11, 3, 8, 0, tzinfo=IST)


def settings(tmp_path: Path, **kw: Any) -> Settings:
    yaml = tmp_path / "strategies.yaml"
    yaml.write_text(
        "strategies:\n"
        "  - {id: ema, strategy: ema_crossover, enabled: true,"
        " params: {interval_minutes: 1, fast: 3, slow: 8}}\n"
    )
    return Settings(_env_file=None, strategies_file=yaml, **kw)


def runtime(s: Settings, *, exit_cb: Any = None, clock: Any = None) -> Runtime:
    notifier = LogNotifier()
    return Runtime(
        s,
        notifier=notifier,
        journal=Journal(MemoryJournalStore(), s.mode, notifier),
        auth=None,
        clock=clock or ManualClock(MONDAY_8AM.replace(hour=10)),
        exit_process=exit_cb,
    )


def test_scheduler_jobs_and_times(tmp_path: Path) -> None:
    rt = runtime(settings(tmp_path, square_off_time=time(15, 20), trading_end=time(15, 10)))
    sched = build_scheduler(rt)
    jobs = {j.id: j for j in sched.get_jobs()}
    assert set(jobs) == {
        "reload_instruments",
        "risk_reset",
        "square_off",
        "end_of_day",
        "record_candles",
        "session_expiry",
    }

    def next_fire(job_id: str, now: datetime) -> datetime:
        return jobs[job_id].trigger.get_next_fire_time(None, now)  # type: ignore[no-any-return]

    assert next_fire("reload_instruments", MONDAY_8AM) == MONDAY_8AM.replace(hour=8, minute=45)
    assert next_fire("risk_reset", MONDAY_8AM) == MONDAY_8AM.replace(hour=9)
    assert next_fire("square_off", MONDAY_8AM) == MONDAY_8AM.replace(hour=15, minute=20)
    assert next_fire("end_of_day", MONDAY_8AM) == MONDAY_8AM.replace(hour=15, minute=35)
    assert next_fire("record_candles", MONDAY_8AM) == MONDAY_8AM.replace(hour=15, minute=40)
    friday_eve = datetime(2025, 11, 7, 16, 0, tzinfo=IST)
    assert next_fire("end_of_day", friday_eve).date().isoformat() == "2025-11-10"  # skip weekend
    assert next_fire("session_expiry", friday_eve) == datetime(2025, 11, 8, 6, 0, tzinfo=IST)


async def test_sim_runtime_starts_engine_and_writes_eod(tmp_path: Path) -> None:
    rt = runtime(settings(tmp_path))
    await rt.startup()
    try:
        assert rt.engine is not None and rt.engine.running
        assert rt.recovery is not None and rt.recovery.ok
        # Nothing trades until today's plan is submitted (Settings tab).
        assert rt.engine.slots["ema"].state.value == "STOPPED"
        assert not rt.engine.plan_submitted
        plan, submitted = await rt.plan_for_form()
        assert not submitted and plan.strategies["ema"].enabled
        result = await rt.submit_plan(plan)
        assert result["started"] == ["ema"]
        assert rt.engine.slots["ema"].state.value == "RUNNING"
        assert isinstance(rt.engine.executor, PaperExecutor)
        await rt.end_of_day()
        store = rt.journal.store
        assert isinstance(store, MemoryJournalStore)
        assert any(k[2] == "__account__" for k in store.daily_pnl)
        assert rt.status()["engine_running"] is True
    finally:
        await rt.shutdown()
    assert not rt.engine.running


async def test_recovery_failure_blocks_engine(tmp_path: Path) -> None:
    rt = runtime(settings(tmp_path))

    async def broken(*a: Any, **k: Any) -> Any:
        raise ConnectionError("db down")

    rt.journal.store.load_carry_fills = broken  # type: ignore[method-assign]
    await rt.startup()
    try:
        assert rt.engine is not None
        assert "database is not reachable" in rt.engine.blocked_reason
        assert rt.engine.slots["ema"].state.value == "STOPPED"
    finally:
        await rt.shutdown()


async def test_session_expiry_stops_and_requests_exit(tmp_path: Path) -> None:
    exits: list[bool] = []
    rt = runtime(settings(tmp_path, feed="kite"), exit_cb=lambda: exits.append(True))
    engine = MagicMock()
    engine.running = True

    async def stop() -> None:
        engine.running = False

    engine.stop = stop
    rt.engine = engine
    await rt.journal.start()
    await rt.session_expired()
    await rt.session_expired()  # idempotent
    assert exits == [True]
    assert engine.running is False
    await rt.journal.stop()


async def test_session_expiry_ignored_for_sim_feed(tmp_path: Path) -> None:
    exits: list[bool] = []
    rt = runtime(settings(tmp_path), exit_cb=lambda: exits.append(True))
    await rt.session_expired()
    assert exits == []


def test_factory_live_wiring(tmp_path: Path) -> None:
    live = Settings(
        _env_file=None,
        mode="live",
        live_trading_confirmed=True,
        admin_token="x" * 16,
        feed="kite",
        kite_api_key="k",
        kite_api_secret="s",
    )
    paper = Settings(_env_file=None)
    clock = ManualClock(MONDAY_8AM)
    from app.brokers.session import KiteSession  # noqa: PLC0415

    session = KiteSession("acc", "AB1234", MONDAY_8AM, MONDAY_8AM.replace(hour=23))
    broker = factory.build_broker(live, session)
    assert isinstance(factory.build_executor(live, clock, broker), LiveExecutor)
    with pytest.raises(RuntimeError, match="order endpoints"):
        factory.build_executor(live, clock, None)
    paper_broker = factory.build_broker(paper, session)
    assert isinstance(factory.build_executor(paper, clock, paper_broker), PaperExecutor)
    assert factory.reconciliation_broker(paper, paper_broker) is None
    assert factory.reconciliation_broker(live, broker) is broker
    n = factory.build_notifier(live)
    assert isinstance(n, CompositeNotifier)
    # A live executor is never built from unsafe settings, even if someone bypasses startup.
    unsafe = live.model_copy(update={"live_trading_confirmed": False})
    with pytest.raises(UnsafeConfigError):
        factory.build_executor(unsafe, clock, broker)


# --- recovery API --------------------------------------------------------------------------


class FakeRuntime:
    def __init__(self) -> None:
        self.engine = MagicMock()
        self.engine.blocked_reason = "NFO:X MIS: ours=0 kite=75"
        self.resolved: list[bool] = []

    def status(self) -> dict[str, Any]:
        return {"blocked_reason": self.engine.blocked_reason}

    async def resolve_recovery(self, adopt: bool) -> dict[str, Any]:
        self.resolved.append(adopt)
        self.engine.blocked_reason = ""
        return {"adopted_positions": 1 if adopt else 0}


@pytest.fixture
def api(monkeypatch: pytest.MonkeyPatch) -> Iterator[tuple[TestClient, FakeRuntime]]:
    monkeypatch.setenv("LOG_JSON", "false")
    monkeypatch.setenv("ADMIN_TOKEN", "a-very-long-admin-token")
    get_settings.cache_clear()
    fake = FakeRuntime()
    with TestClient(app) as client:
        app.state.runtime = fake
        yield client, fake
    app.state.runtime = None
    get_settings.cache_clear()


def test_recovery_api_requires_admin(api: tuple[TestClient, FakeRuntime]) -> None:
    client, _ = api
    assert client.get("/recovery").status_code == 401
    assert client.get("/recovery", headers={"X-Admin-Token": "wrong"}).status_code == 401
    ok = client.get("/recovery", headers={"X-Admin-Token": "a-very-long-admin-token"})
    assert ok.status_code == 200
    assert "kite=75" in ok.json()["blocked_reason"]


def test_recovery_resolve_needs_typed_confirmation(api: tuple[TestClient, FakeRuntime]) -> None:
    client, fake = api
    h = {"X-Admin-Token": "a-very-long-admin-token"}
    r = client.post("/recovery/resolve", json={"confirm": "yes"}, headers=h)
    assert r.status_code == 400 and fake.resolved == []
    r = client.post(
        "/recovery/resolve", json={"confirm": "RESOLVE", "adopt_broker_positions": True}, headers=h
    )
    assert r.status_code == 200 and r.json() == {"adopted_positions": 1}
    assert fake.resolved == [True]
    r = client.post("/recovery/resolve", json={"confirm": "RESOLVE"}, headers=h)
    assert r.status_code == 409  # no longer blocked


def test_admin_endpoints_refuse_without_configured_token(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LOG_JSON", "false")
    get_settings.cache_clear()
    with TestClient(app) as client:
        app.state.runtime = FakeRuntime()
        assert client.get("/recovery", headers={"X-Admin-Token": ""}).status_code == 503
    app.state.runtime = None
    get_settings.cache_clear()


async def test_on_login_starts_engine_once(tmp_path: Path) -> None:
    rt = runtime(settings(tmp_path, feed="kite"))
    started: list[Any] = []

    async def fake_start(session: Any) -> None:
        started.append(session)

    rt.start_engine = fake_start  # type: ignore[method-assign]
    await rt.on_login(MagicMock())
    assert len(started) == 1
    rt._kite_feed_used = True
    exits: list[bool] = []
    rt._exit_process = lambda: exits.append(True)
    await rt.on_login(MagicMock())
    assert exits == [True] and len(started) == 1
    await asyncio.sleep(0)
