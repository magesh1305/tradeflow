"""Daily trading plan: gate, limits, loosening rules, persistence and the Settings API."""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import datetime, time
from pathlib import Path
from typing import Any

import httpx
import pytest

from app.config import Settings, get_settings
from app.core.clock import IST, ManualClock
from app.core.models import OrderStatus, Product, Side
from app.engine.engine import EngineConfig, PlanRequiredError, StrategyState
from app.engine.legs import LegSpec
from app.main import create_app
from app.notify.base import LogNotifier
from app.risk.manager import PlanLimits, StrategyLimits
from app.risk.plan import AccountPlan, DailyPlan, StrategyPlan, describe_changes, loosened
from app.runtime import PlanError, PlanLooseningError, Runtime
from app.storage.journal import Journal, MemoryJournalStore
from app.storage.plans import MemoryPlanStore
from tests.builders import CE, FUT, ist
from tests.engine_kit import PE, build, feed, liquid

# --- plan model and loosening rules ------------------------------------------------------


def account(**kw: Any) -> AccountPlan:
    base: dict[str, Any] = {
        "daily_loss_limit": 10_000,
        "max_open_positions": 6,
        "max_lots_per_order": 10,
        "trading_start": time(9, 15),
        "trading_end": time(15, 10),
    }
    base.update(kw)
    return AccountPlan(**base)


def plan(acct: AccountPlan | None = None, **strategies: StrategyPlan) -> DailyPlan:
    return DailyPlan(account=acct or account(), strategies=strategies)


def test_plan_validation() -> None:
    with pytest.raises(ValueError, match="trading_start"):
        account(trading_start=time(15, 0), trading_end=time(10, 0))
    with pytest.raises(ValueError):
        account(daily_loss_limit=0)
    with pytest.raises(ValueError):
        StrategyPlan(lots=0)


def test_loosening_detection() -> None:
    old = plan(
        account(profit_target=5000, max_trades_per_day=5),
        s=StrategyPlan(enabled=True, lots=1, loss_limit=2000, max_trades_per_day=3),
    )
    tighter = plan(
        account(
            daily_loss_limit=8000, profit_target=4000, max_trades_per_day=4, trading_end=time(15, 0)
        ),
        s=StrategyPlan(enabled=True, lots=1, loss_limit=1500, max_trades_per_day=2),
    )
    assert loosened(old, tighter) == []
    looser = plan(
        account(
            daily_loss_limit=12_000,
            max_trades_per_day=None,
            trading_start=time(9, 15),
            trading_end=time(15, 10),
        ),
        s=StrategyPlan(enabled=True, lots=2, loss_limit=None, max_trades_per_day=3),
    )
    found = loosened(old, looser)
    assert any("daily loss limit" in f for f in found)
    assert any("max trades per day" in f and "no limit" in f for f in found)
    assert any("profit target" in f for f in found)  # removed target = looser
    assert any(f.startswith("s lots: 1 -> 2") for f in found)
    assert any("s loss limit" in f for f in found)
    assert describe_changes(None, old) == ["plan submitted"]
    assert any("daily_loss_limit" in c for c in describe_changes(old, tighter))


# --- risk enforcement --------------------------------------------------------------------


async def test_max_trades_per_day_counts_entries_not_exits() -> None:
    engine, s, _ = await build()
    engine.risk.plan = PlanLimits(strategies={"s": StrategyLimits(max_trades_per_day=2)})
    await feed(engine, liquid(FUT, 24000.0))
    assert s.buy(FUT, 75).status is OrderStatus.COMPLETE
    engine.clock.set(ist(10, 1))
    assert s.exit_position(FUT)[0].status is OrderStatus.COMPLETE  # exits don't count
    assert s.sell(FUT, 75).status is OrderStatus.COMPLETE
    engine.clock.set(ist(10, 2))
    third = s.buy(FUT, 150)
    assert third.status is OrderStatus.REJECTED and "max 2 trades" in third.status_message
    # ...but getting out is always allowed.
    assert s.exit_position(FUT)[0].status is not OrderStatus.REJECTED
    assert engine.risk.trades_today("s") == 2
    await engine.stop()


async def test_leg_group_counts_as_one_trade() -> None:
    engine, s, _ = await build()
    engine.risk.plan = PlanLimits(max_trades_per_day=1)
    await feed(engine, liquid(CE, 100.0), liquid(PE, 90.0))
    group = s.place_legs(LegSpec(CE, Side.SELL, 75), LegSpec(PE, Side.SELL, 75))
    engine._pump()
    assert group.status.value == "FILLED"
    assert engine.risk.trades_today() == 1
    await engine.stop()


async def test_profit_target_stops_new_trades_but_keeps_positions() -> None:
    engine, s, notifier = await build()
    engine.risk.plan = PlanLimits(strategies={"s": StrategyLimits(profit_target=500)})
    await feed(engine, liquid(FUT, 24000.0))
    s.buy(FUT, 75)
    engine._pump()
    await feed(engine, liquid(FUT, 24010.0, ts=ist(10, 1)))  # +~742 net > 500
    engine.clock.set(ist(10, 2))
    o = s.sell(FUT, 150)  # a flip needs a new entry: blocked
    assert o.status is OrderStatus.REJECTED and "profit target" in o.status_message
    assert engine.positions.net_quantity("s", FUT.token) == 75  # position kept
    assert engine.slots["s"].state is StrategyState.RUNNING
    engine.check_pnl()
    engine.check_pnl()
    import asyncio  # noqa: PLC0415

    await asyncio.sleep(0)
    assert sum("Profit target reached (s)" in m for _, m in notifier.sent) == 1  # alerted once
    await engine.stop()


async def test_account_profit_target() -> None:
    engine, s, _ = await build()
    engine.risk.plan = PlanLimits(profit_target=100)
    await feed(engine, liquid(FUT, 24000.0))
    s.buy(FUT, 75)
    engine._pump()
    await feed(engine, liquid(FUT, 24010.0, ts=ist(10, 1)))
    engine.clock.set(ist(10, 2))
    assert "account profit target" in s.buy(FUT, 150).status_message
    await engine.stop()


async def test_strategy_loss_limit_stops_and_exits_only_that_strategy() -> None:
    engine, s, notifier = await build()
    from tests.test_engine import Recorder  # noqa: PLC0415

    other = Recorder("other")
    engine.add_strategy(other)
    engine.start_strategy("other")
    engine.risk.plan = PlanLimits(strategies={"s": StrategyLimits(loss_limit=500)})
    await feed(engine, liquid(FUT, 24000.0))
    s.buy(FUT, 75)
    other.buy(FUT, 150)
    engine._pump()
    await feed(engine, liquid(FUT, 23990.0, ts=ist(10, 1)))  # s: -~750
    engine.check_pnl()
    await feed(engine, liquid(FUT, 23990.0, ts=ist(10, 2)))
    assert engine.slots["s"].state is StrategyState.STOPPED
    assert "loss limit" in engine.slots["s"].error
    assert engine.positions.net_quantity("s", FUT.token) == 0
    assert engine.slots["other"].state is StrategyState.RUNNING
    assert engine.positions.net_quantity("other", FUT.token) == 150
    import asyncio  # noqa: PLC0415

    await asyncio.sleep(0)
    assert any(lvl == "critical" and "s loss limit hit" in m for lvl, m in notifier.sent)
    await engine.stop()


async def test_max_lots_and_capital_per_strategy() -> None:
    engine, s, _ = await build()
    engine.risk.plan = PlanLimits(
        strategies={"s": StrategyLimits(max_lots_per_order=1, max_capital=300_000)}
    )
    await feed(engine, liquid(FUT, 24000.0))
    o = s.buy(FUT, 150)
    assert o.status is OrderStatus.REJECTED and "max 1 per order" in o.status_message
    assert s.buy(FUT, 75).status is OrderStatus.COMPLETE  # ~270k of 300k
    engine.clock.set(ist(10, 1))
    o = s.sell(CE, 75, product=Product.NRML)  # short option: strike-based margin, over cap
    assert o.status is OrderStatus.REJECTED and "capital limit" in o.status_message
    await engine.stop()


async def test_engine_gate_requires_todays_plan() -> None:
    engine, _s, _ = await build(config=EngineConfig(require_daily_plan=True))
    assert engine.slots["s"].state is StrategyState.STOPPED  # build() asked to start it
    with pytest.raises(PlanRequiredError, match="Settings"):
        engine.start_strategy("s")
    engine.plan_date = engine.clock.now().date() - __import__("datetime").timedelta(days=1)
    with pytest.raises(PlanRequiredError):
        engine.start_strategy("s")  # yesterday's plan doesn't count
    engine.plan_date = engine.clock.now().date()
    engine.start_strategy("s")
    assert engine.slots["s"].state is StrategyState.RUNNING
    await engine.stop()


# --- runtime: submit, loosen, persist, restart ---------------------------------------------


def sim_settings(tmp_path: Path, **kw: Any) -> Settings:
    yaml = tmp_path / "strategies.yaml"
    yaml.write_text(
        "strategies:\n"
        "  - {id: ema, strategy: ema_crossover, enabled: true,"
        " params: {interval_minutes: 1, fast: 3, slow: 8, lots: 1}}\n"
        "  - {id: straddle, strategy: short_straddle, enabled: false}\n"
    )
    return Settings(
        _env_file=None,
        strategies_file=yaml,
        strategy_overrides_file=tmp_path / "overrides.yaml",
        **kw,
    )


def runtime(s: Settings, clock: ManualClock, store: MemoryPlanStore) -> Runtime:
    notifier = LogNotifier()
    return Runtime(
        s,
        notifier=notifier,
        journal=Journal(MemoryJournalStore(), s.mode, notifier),
        auth=None,
        clock=clock,
        plans=store,
    )


MONDAY_0900 = datetime(2025, 11, 3, 9, 0, tzinfo=IST)


async def test_submit_applies_limits_and_starts_enabled(tmp_path: Path) -> None:
    store = MemoryPlanStore()
    rt = runtime(sim_settings(tmp_path), ManualClock(MONDAY_0900), store)
    await rt.startup()
    try:
        assert rt.engine is not None
        form, submitted = await rt.plan_for_form()
        assert not submitted
        assert set(form.strategies) == {"ema", "straddle"}
        assert form.strategies["ema"].enabled and form.strategies["ema"].lots == 1
        form.account.daily_loss_limit = 5000
        form.account.max_trades_per_day = 4
        form.strategies["ema"].lots = 2
        form.strategies["ema"].loss_limit = 1500
        result = await rt.submit_plan(form)
        assert result["started"] == ["ema"]
        assert rt.engine.risk.limits.daily_loss_limit == 5000
        assert rt.engine.risk.plan.max_trades_per_day == 4
        assert rt.engine.risk.plan.strategies["ema"].loss_limit == 1500
        assert rt.engine.slots["ema"].strategy.params.lots == 2  # lots applied to params
        assert (await rt.plan_for_form())[1] is True
        (event,) = await rt.plan_history()
        assert event["changes"] == ["plan submitted"]
        notifier = rt.notifier
        assert isinstance(notifier, LogNotifier)
        assert any("Daily plan submitted" in m for _, m in notifier.sent)
    finally:
        await rt.shutdown()


async def test_loosening_during_market_hours_needs_confirmation(tmp_path: Path) -> None:
    clock = ManualClock(MONDAY_0900)
    rt = runtime(sim_settings(tmp_path), clock, MemoryPlanStore())
    await rt.startup()
    try:
        form, _ = await rt.plan_for_form()
        await rt.submit_plan(form)
        clock.set(datetime(2025, 11, 3, 11, 0, tzinfo=IST))  # market hours
        tighter = form.model_copy(deep=True)
        tighter.account.daily_loss_limit = 8000
        await rt.submit_plan(tighter)  # tightening: no confirmation needed
        looser = tighter.model_copy(deep=True)
        looser.account.daily_loss_limit = 20_000
        with pytest.raises(PlanLooseningError) as exc:
            await rt.submit_plan(looser)
        assert any("daily loss limit" in f for f in exc.value.loosened)
        assert rt.engine is not None and rt.engine.risk.limits.daily_loss_limit == 8000
        with pytest.raises(PlanLooseningError):
            await rt.submit_plan(looser, confirm="yes")
        result = await rt.submit_plan(looser, confirm="LOOSEN")
        assert result["loosened"] and rt.engine.risk.limits.daily_loss_limit == 20_000
        notifier = rt.notifier
        assert isinstance(notifier, LogNotifier)
        assert any(lvl == "critical" and "LOOSENED" in m for lvl, m in notifier.sent)
    finally:
        await rt.shutdown()


async def test_plan_validation_and_running_lots_change(tmp_path: Path) -> None:
    rt = runtime(sim_settings(tmp_path), ManualClock(MONDAY_0900), MemoryPlanStore())
    await rt.startup()
    try:
        form, _ = await rt.plan_for_form()
        bad = form.model_copy(deep=True)
        bad.account.trading_end = time(15, 20)  # after square-off
        with pytest.raises(PlanError, match="square-off"):
            await rt.submit_plan(bad)
        bad = form.model_copy(deep=True)
        bad.strategies["nope"] = StrategyPlan()
        with pytest.raises(PlanError, match="unknown"):
            await rt.submit_plan(bad)
        await rt.submit_plan(form)
        change = form.model_copy(deep=True)
        change.strategies["ema"].lots = 3
        with pytest.raises(PlanError, match="stop ema"):
            await rt.submit_plan(change)
        # Disabling a running strategy stops it (positions kept).
        off = form.model_copy(deep=True)
        off.strategies["ema"].enabled = False
        result = await rt.submit_plan(off)
        assert result["stopped"] == ["ema"]
    finally:
        await rt.shutdown()


async def test_plan_survives_restart_and_prefills_next_day(tmp_path: Path) -> None:
    store = MemoryPlanStore()
    s = sim_settings(tmp_path)
    clock = ManualClock(MONDAY_0900)
    rt = runtime(s, clock, store)
    await rt.startup()
    form, _ = await rt.plan_for_form()
    form.account.daily_loss_limit = 4321
    await rt.submit_plan(form)
    await rt.shutdown()

    rt2 = runtime(s, clock, store)  # crash + restart on the same day
    await rt2.startup()
    try:
        assert rt2.engine is not None and rt2.engine.plan_submitted
        assert rt2.engine.risk.limits.daily_loss_limit == 4321
        assert rt2.engine.slots["ema"].state is StrategyState.RUNNING
    finally:
        await rt2.shutdown()

    clock.set(datetime(2025, 11, 4, 9, 0, tzinfo=IST))  # next morning
    rt3 = runtime(s, clock, store)
    await rt3.startup()
    try:
        assert rt3.engine is not None and not rt3.engine.plan_submitted
        assert rt3.engine.slots["ema"].state is StrategyState.STOPPED
        form, submitted = await rt3.plan_for_form()
        assert not submitted and form.account.daily_loss_limit == 4321  # pre-filled
    finally:
        await rt3.shutdown()


async def test_save_failure_still_applies_and_alerts(tmp_path: Path) -> None:
    store = MemoryPlanStore()
    store.fail = True
    rt = runtime(sim_settings(tmp_path), ManualClock(MONDAY_0900), store)
    await rt.startup()
    try:
        form, _ = await rt.plan_for_form()
        result = await rt.submit_plan(form)
        assert any("not saved" in w for w in result["warnings"])
        assert result["started"] == ["ema"]
    finally:
        await rt.shutdown()


# --- API -------------------------------------------------------------------------------------

TOKEN = "plan-test-admin-token-123"
H = {"X-Admin-Token": TOKEN}


@pytest.fixture
async def api(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> AsyncIterator[tuple[httpx.AsyncClient, Runtime, ManualClock]]:
    monkeypatch.setenv("ADMIN_TOKEN", TOKEN)
    monkeypatch.setenv("LOG_JSON", "false")
    get_settings.cache_clear()
    clock = ManualClock(MONDAY_0900)
    rt = runtime(sim_settings(tmp_path), clock, MemoryPlanStore())
    await rt.startup()
    app = create_app()
    app.state.runtime = rt
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://t"
    ) as client:
        yield client, rt, clock
    await rt.shutdown()
    get_settings.cache_clear()


async def test_settings_api_flow(api: tuple[httpx.AsyncClient, Runtime, ManualClock]) -> None:
    client, _rt, clock = api
    status = (await client.get("/status", headers=H)).json()
    assert status["plan_submitted"] is False
    r = await client.post("/strategies/ema/start", headers=H)
    assert r.status_code == 409 and "Settings" in r.json()["detail"]

    form = (await client.get("/settings", headers=H)).json()
    assert form["submitted"] is False and form["loosen_confirmation"] == "LOOSEN"
    assert {s["id"]: s["has_lots"] for s in form["strategies"]} == {"ema": True, "straddle": True}
    plan = form["plan"]
    plan["account"]["max_trades_per_day"] = 5
    r = await client.post("/settings/plan", json={"plan": plan}, headers=H)
    assert r.status_code == 200, r.text
    assert r.json()["started"] == ["ema"]
    assert (await client.get("/status", headers=H)).json()["plan_submitted"] is True

    clock.set(datetime(2025, 11, 3, 11, 0, tzinfo=IST))
    plan["account"]["max_trades_per_day"] = 50
    r = await client.post("/settings/plan", json={"plan": plan}, headers=H)
    assert r.status_code == 409
    assert any("max trades per day" in f for f in r.json()["detail"]["loosened"])
    r = await client.post("/settings/plan", json={"plan": plan, "confirm": "LOOSEN"}, headers=H)
    assert r.status_code == 200 and r.json()["loosened"]

    plan["account"]["daily_loss_limit"] = -1
    assert (await client.post("/settings/plan", json={"plan": plan}, headers=H)).status_code == 422
    history = (await client.get("/settings/history", headers=H)).json()
    assert len(history) == 2 and history[0]["loosened"]
    assert (await client.get("/settings")).status_code == 401
