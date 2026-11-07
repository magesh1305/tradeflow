"""Control API against a real engine (paper executor, manual feed) inside a Runtime."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator, Iterator
from datetime import date, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from app.backtest.runner import BacktestRequest, run_backtest
from app.backtest.store import MemoryBacktestStore
from app.config import Settings, get_settings
from app.core.models import Product, Tick
from app.engine.engine import TradingEngine
from app.feeds.random_walk import synthetic_nifty
from app.main import RedactTokenFilter, create_app
from app.notify.base import LogNotifier
from app.runtime import Runtime, load_overrides
from app.storage.journal import Journal, MemoryJournalStore
from tests.builders import CE, FUT, ist
from tests.engine_kit import build, feed, liquid

TOKEN = "test-admin-token-1234567890"
H = {"X-Admin-Token": TOKEN}
TODAY = date(2025, 11, 3)


@pytest.fixture(autouse=True)
def _env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterator[None]:
    monkeypatch.setenv("ADMIN_TOKEN", TOKEN)
    monkeypatch.setenv("LOG_JSON", "false")
    monkeypatch.setenv("FRONTEND_ORIGIN", "https://dash.example")
    monkeypatch.setenv("STRATEGY_OVERRIDES_FILE", str(tmp_path / "overrides.yaml"))
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


class Env:
    def __init__(
        self,
        client: httpx.AsyncClient,
        runtime: Runtime,
        engine: TradingEngine,
        store: MemoryJournalStore,
    ) -> None:
        self.client, self.runtime, self.engine, self.store = client, runtime, engine, store


async def inline_executor_for(history: Any, runs: MemoryBacktestStore, settings: Settings) -> Any:
    async def run(req: BacktestRequest, run_id: int) -> None:
        await runs.finish(run_id, await run_backtest(settings, req, history))

    return run


@pytest.fixture
async def env() -> AsyncIterator[Env]:
    engine, _s, notifier = await build(journal=True)
    journal = engine.journal
    assert isinstance(journal, Journal)
    store = journal.store
    assert isinstance(store, MemoryJournalStore)
    settings = get_settings()
    runs = MemoryBacktestStore()
    from tests.test_backtest import synth_history  # noqa: PLC0415

    history = await synth_history([TODAY])
    runtime = Runtime(
        settings,
        notifier=notifier,
        journal=journal,
        auth=None,
        clock=engine.clock,
        backtests=runs,
        history=history,
        backtest_executor=await inline_executor_for(history, runs, settings),
    )
    runtime.engine = engine
    app = create_app()
    app.state.runtime = runtime
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        yield Env(client, runtime, engine, store)
    await engine.stop()
    await journal.stop()


# --- auth & CORS --------------------------------------------------------------------------


async def test_every_route_except_health_and_auth_requires_admin_token(env: Env) -> None:
    app = create_app()
    checked = 0
    # FastAPI 0.16x includes routers lazily, so enumerate from the OpenAPI schema.
    for route_path, methods in app.openapi()["paths"].items():
        if route_path == "/health" or route_path.startswith("/auth"):
            continue
        path = (
            route_path.replace("{strategy_id}", "s")
            .replace("{run_id}", "1")
            .replace("{underlying}", "NIFTY")
        )
        for method in methods:
            r = await env.client.request(method.upper(), path)
            assert r.status_code == 401, (method, path, r.status_code)
            r = await env.client.request(method.upper(), path, headers={"X-Admin-Token": "x" * 30})
            assert r.status_code == 401, (method, path)
            checked += 1
    # 17 control + 2 recovery + 3 settings + 2 connection routes; update when adding routes
    assert checked == 24
    assert (await env.client.get("/health")).status_code == 200
    # Interactive docs are unauthenticated, so they are off by default.
    assert (await env.client.get("/docs")).status_code == 404
    assert (await env.client.get("/openapi.json")).status_code == 404


async def test_cors_only_for_configured_origin(env: Env) -> None:
    pre = {
        "Access-Control-Request-Method": "GET",
        "Access-Control-Request-Headers": "X-Admin-Token",
    }
    ok = await env.client.options("/status", headers={"Origin": "https://dash.example", **pre})
    assert ok.headers.get("access-control-allow-origin") == "https://dash.example"
    bad = await env.client.options("/status", headers={"Origin": "https://evil.example", **pre})
    assert "access-control-allow-origin" not in bad.headers


# --- status & strategies ------------------------------------------------------------------


async def test_status_reflects_engine(env: Env) -> None:
    await feed(env.engine, liquid(FUT, 24000.0))
    env.engine.slots["s"].strategy.buy(FUT, 75)
    env.engine._pump()
    await feed(env.engine, liquid(FUT, 24010.0, ts=ist(10, 1)))
    body = (await env.client.get("/status", headers=H)).json()
    assert body["mode"] == "paper"
    assert body["engine_running"] is True
    (pos,) = body["positions"]
    assert (pos["tradingsymbol"], pos["quantity"]) == (FUT.tradingsymbol, 75)
    assert body["pnl"]["gross"] == pytest.approx(10 * 75 - 0.1 * 75 * 0, abs=10)
    assert body["pnl"]["net"] == pytest.approx(body["pnl"]["gross"] - body["pnl"]["charges"])
    assert body["strategies"][0]["id"] == "s"
    assert body["kill_switch"]["tripped"] is False


async def test_strategy_lifecycle_and_params(env: Env, tmp_path: Path) -> None:
    c = env.client
    assert [s["id"] for s in (await c.get("/strategies", headers=H)).json()] == ["s"]
    detail = (await c.get("/strategies/s", headers=H)).json()
    assert detail["state"] == "RUNNING" and "params_schema" in detail
    assert (await c.get("/strategies/nope", headers=H)).status_code == 404

    # Params can only change while stopped.
    r = await c.put("/strategies/s/params", json={"params": {}}, headers=H)
    assert r.status_code == 409
    await feed(env.engine, liquid(FUT, 24000.0))
    env.engine.slots["s"].strategy.buy(FUT, 75)
    env.engine._pump()
    r = await c.post("/strategies/s/stop", params={"exit_positions": "true"}, headers=H)
    assert r.json()["state"] == "STOPPED"
    assert env.engine.positions.net_quantity("s", FUT.token) == 0  # exited
    r = await c.put("/strategies/s/params", json={"params": {"bogus": 1}}, headers=H)
    assert r.status_code == 422
    r = await c.put("/strategies/s/params", json={"params": {}}, headers=H)
    assert r.status_code == 200
    assert load_overrides(tmp_path / "overrides.yaml") == {"s": {}}
    assert (await c.post("/strategies/s/start", headers=H)).json()["state"] == "RUNNING"


async def test_start_refused_while_blocked_or_killed(env: Env) -> None:
    env.engine.stop_strategy("s")
    env.engine.block("recovery mismatch")
    r = await env.client.post("/strategies/s/start", headers=H)
    assert r.status_code == 409 and "blocked" in r.json()["detail"]
    env.engine.unblock()
    env.engine.trip_kill_switch("test")
    r = await env.client.post("/strategies/s/start", headers=H)
    assert r.status_code == 409 and "kill switch" in r.json()["detail"]


# --- orders, fills, P&L -------------------------------------------------------------------


async def test_orders_and_fills_merge_journal_and_live_with_filters(env: Env) -> None:
    await feed(env.engine, liquid(FUT, 24000.0))
    env.engine.slots["s"].strategy.buy(FUT, 75)
    env.engine._pump()
    old_day = TODAY - timedelta(days=3)
    env.store.orders["OLDORDER00000001"] = {
        "client_order_id": "OLDORDER00000001",
        "broker_order_id": "X1",
        "trading_date": old_day,
        "mode": "paper",
        "strategy_id": "other",
        "leg_group_id": None,
        "exchange": "NFO",
        "tradingsymbol": "NIFTYOLDFUT",
        "instrument_token": 1,
        "side": "SELL",
        "quantity": 75,
        "order_type": "MARKET",
        "product": "MIS",
        "price": None,
        "trigger_price": None,
        "status": "COMPLETE",
        "filled_quantity": 75,
        "average_price": 1.0,
        "status_message": "",
        "reason": "",
        "created_at": ist(10, 0, d=old_day),
        "updated_at": ist(10, 0, d=old_day),
    }
    c = env.client
    today = (await c.get("/orders", headers=H)).json()
    assert [o["strategy_id"] for o in today] == ["s"]  # default: today only
    both = (
        await c.get("/orders", params={"from": str(old_day), "to": str(TODAY)}, headers=H)
    ).json()
    assert {o["client_order_id"] for o in both} >= {"OLDORDER00000001"}
    only_other = (
        await c.get("/orders", params={"from": str(old_day), "strategy": "other"}, headers=H)
    ).json()
    assert [o["strategy_id"] for o in only_other] == ["other"]
    assert (await c.get("/orders", params={"mode": "live"}, headers=H)).json() == []
    bad = await c.get("/orders", params={"from": str(TODAY), "to": str(old_day)}, headers=H)
    assert bad.status_code == 422
    fills = (await c.get("/fills", headers=H)).json()
    assert len(fills) == 1 and fills[0]["quantity"] == 75


async def test_daily_pnl_includes_live_today(env: Env) -> None:
    env.store.daily_pnl[(TODAY - timedelta(days=1), "paper", "__account__")] = {
        "trading_date": TODAY - timedelta(days=1),
        "mode": "paper",
        "strategy_id": "__account__",
        "gross_pnl": 500.0,
        "charges": 40.0,
        "net_pnl": 460.0,
        "trades": 4,
    }
    rows = (await env.client.get("/pnl/daily", headers=H)).json()
    acct = [r for r in rows if r["strategy_id"] == "__account__"]
    assert [r["live"] for r in acct] == [False, True]
    assert acct[0]["net_pnl"] == 460.0


async def test_risk_view(env: Env) -> None:
    from app.risk.margin import EstimatedMargin  # noqa: PLC0415

    env.engine.margin = EstimatedMargin(1_000_000, 0.15, env.engine.last_tick)
    await feed(env.engine, liquid(FUT, 24000.0))
    env.engine.slots["s"].strategy.buy(FUT, 75)
    env.engine._pump()
    body = (await env.client.get("/risk", headers=H)).json()
    assert body["limits"]["max_open_positions"] == 6
    assert body["usage"]["orders_last_minute"] == 1
    assert body["usage"]["open_positions"] == 1
    assert body["margin"]["source"] == "paper estimate"
    assert body["margin"]["used"] == pytest.approx(24000.1 * 75 * 0.15, rel=0.01)


# --- option chain -------------------------------------------------------------------------


async def test_option_chain_subscribes_and_returns_greeks(env: Env) -> None:
    u = synthetic_nifty(TODAY)
    env.engine.instruments.load(u.all, TODAY)
    c = env.client
    first = (await c.get("/option-chain/nifty", headers=H)).json()
    assert first["rows"] == [] and "waiting" in first["note"]
    assert u.index.token in env.engine.feed.subscribed  # type: ignore[attr-defined]
    await feed(env.engine, Tick(u.index.token, ist(10, 0), 24010.0))
    second = (await c.get("/option-chain/NIFTY", params={"width": 2}, headers=H)).json()
    assert second["spot"] == 24010.0 and len(second["rows"]) == 5
    atm = next(r for r in second["rows"] if r["is_atm"])
    assert atm["strike"] == 24000.0
    ce = next(o for o in u.options if o.strike == 24000.0 and o.instrument_type == "CE")
    assert ce.token in env.engine.feed.subscribed  # type: ignore[attr-defined]
    await feed(env.engine, liquid(ce, 120.0, ts=ist(10, 0, 5)))
    third = (await c.get("/option-chain/NIFTY", params={"width": 2}, headers=H)).json()
    atm = next(r for r in third["rows"] if r["is_atm"])
    assert atm["call"]["greeks"]["iv"] > 0 and 0 < atm["call"]["greeks"]["delta"] < 1
    assert (await c.get("/option-chain/NOPE", headers=H)).status_code == 404


# --- actions ------------------------------------------------------------------------------


async def test_square_off_and_kill_switch(env: Env) -> None:
    await feed(env.engine, liquid(FUT, 24000.0), liquid(CE, 100.0))
    strat = env.engine.slots["s"].strategy
    strat.buy(FUT, 75)
    strat.buy(CE, 75, product=Product.NRML)
    env.engine._pump()
    body = (await env.client.post("/square-off", headers=H)).json()
    assert body["positions"] == []  # manual square-off flattens MIS and NRML
    r = await env.client.post("/kill-switch", json={"reason": "phone test"}, headers=H)
    assert r.json() == {
        "tripped": True,
        "reason": "phone test",
        "tripped_at": r.json()["tripped_at"],
    }
    assert env.engine.slots["s"].state.value == "STOPPED"
    r = await env.client.post("/kill-switch/reset", headers=H)
    assert r.json()["tripped"] is False
    assert env.engine.slots["s"].state.value == "STOPPED"  # stays stopped


# --- backtests ----------------------------------------------------------------------------


async def test_backtest_submit_list_and_detail(env: Env) -> None:
    c = env.client
    body = {
        "strategy": "ema_crossover",
        "params": {"interval_minutes": 1, "fast": 3, "slow": 8},
        "start": str(TODAY),
        "end": str(TODAY),
    }
    r = await c.post("/backtests", json=body, headers=H)
    assert r.status_code == 202
    run_id = r.json()["id"]
    for _ in range(200):
        detail = (await c.get(f"/backtests/{run_id}", headers=H)).json()
        if detail["status"] != "running":
            break
        await asyncio.sleep(0.05)
    assert detail["status"] == "done", detail
    assert detail["metrics"]["days"] == 1 and detail["equity_curve"]
    listed = (await c.get("/backtests", headers=H)).json()
    assert listed[0]["id"] == run_id and "trades_list" not in listed[0]["metrics"]
    bad = await c.post("/backtests", json={**body, "strategy": "nope"}, headers=H)
    assert bad.status_code == 422
    bad = await c.post("/backtests", json={**body, "params": {"fast": 30, "slow": 3}}, headers=H)
    assert bad.status_code == 422
    assert (await c.get("/backtests/999", headers=H)).status_code == 404


# --- websocket ----------------------------------------------------------------------------


def test_ws_streams_status_and_rejects_bad_token() -> None:
    notifier = LogNotifier()
    runtime = Runtime(
        get_settings(),
        notifier=notifier,
        journal=Journal(MemoryJournalStore(), "paper", notifier),
        auth=None,
    )
    app = create_app()
    with TestClient(app) as client:
        app.state.runtime = runtime
        with client.websocket_connect(f"/ws?token={TOKEN}") as ws:
            msg = ws.receive_json()
            assert msg["type"] == "status"
            assert msg["data"]["mode"] == "paper" and msg["data"]["engine_running"] is False
        with (
            pytest.raises(WebSocketDisconnect) as exc,
            client.websocket_connect("/ws?token=wrong") as ws,
        ):
            ws.receive_json()
        assert exc.value.code == 1008


def test_access_log_redacts_ws_token() -> None:
    rec = logging.LogRecord(
        "uvicorn.access",
        logging.INFO,
        "",
        0,
        '%s - "%s %s HTTP/%s" %d',
        ("1.2.3.4", "GET", f"/ws?token={TOKEN}", "1.1", 101),
        None,
    )
    RedactTokenFilter().filter(rec)
    assert TOKEN not in rec.getMessage() and "token=***" in rec.getMessage()


# --- connection check ---------------------------------------------------------------------


class _FakeAuth:
    def __init__(self, session: Any) -> None:
        self.session = session

    async def current_session(self) -> Any:
        return self.session


class _FakeBroker:
    async def profile(self) -> dict[str, Any]:
        return {"user_id": "AB1234", "user_name": "Test User", "broker": "ZERODHA"}


def _client_for(env: Env, auth: Any) -> httpx.AsyncClient:
    app = create_app()
    app.state.runtime = env.runtime
    app.state.auth = auth
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")


async def test_connection_report_never_leaks_secrets(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("KITE_API_KEY", "key-abc-123")
    monkeypatch.setenv("KITE_API_SECRET", "secret-xyz-789")
    monkeypatch.setenv("STORAGE", "memory")
    get_settings.cache_clear()
    async with _client_for(env, None) as client:
        r = await client.get("/connection", headers=H)
    assert r.status_code == 200
    body = r.json()
    assert "key-abc-123" not in r.text and "secret-xyz-789" not in r.text and TOKEN not in r.text
    assert body["mode"] == "paper" and body["live_ready"] is False
    assert body["kite"]["api_key_set"] and body["kite"]["api_secret_set"]
    assert body["kite"]["redirect_url"] == "https://dash.example/api/auth/callback"
    assert body["database"] == {"used": False, "ok": None, "detail": body["database"]["detail"]}
    checks = {c["label"]: c["ok"] for c in body["live_checks"]}
    assert checks["MODE=live"] is False
    assert checks["Kite API key and secret"] is True
    assert checks["Server's static IP whitelisted in the Kite console"] is None


async def test_kite_connection_test(env: Env) -> None:
    async with _client_for(env, None) as client:
        r = await client.post("/connection/kite", headers=H)
    assert r.status_code == 409 and "not set" in r.json()["detail"]

    async with _client_for(env, _FakeAuth(None)) as client:
        r = await client.post("/connection/kite", headers=H)
    assert r.status_code == 409 and "Log in" in r.json()["detail"]

    from app.brokers.session import KiteSession  # noqa: PLC0415

    session = KiteSession(
        access_token="t", user_id="AB1234", login_time=ist(9, 0), expires_at=ist(23, 0)
    )
    env.runtime.broker = _FakeBroker()  # type: ignore[assignment]
    async with _client_for(env, _FakeAuth(session)) as client:
        r = await client.post("/connection/kite", headers=H)
    assert r.status_code == 200
    assert r.json()["ok"] is True and r.json()["user_name"] == "Test User"
