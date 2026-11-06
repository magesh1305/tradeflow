"""Backtests run in a separate process, so a long CPU-bound replay never delays the
live engine's event loop. The child builds its own DB connections from settings and
writes the result (or failure) to `backtest_runs`."""

from __future__ import annotations

import asyncio
from concurrent.futures import ProcessPoolExecutor
from dataclasses import asdict
from datetime import date
from typing import Any

from app.backtest.runner import BacktestRequest, run_backtest
from app.backtest.store import SqlBacktestStore
from app.config import Settings
from app.data.historical import SqlHistoryStore
from app.storage.db import make_engine, make_sessionmaker


def _request_from(d: dict[str, Any]) -> BacktestRequest:
    return BacktestRequest(
        strategy=d["strategy"],
        params=d["params"],
        start=date.fromisoformat(d["start"]),
        end=date.fromisoformat(d["end"]),
        interval_seconds=d["interval_seconds"],
        strategy_id=d["strategy_id"],
    )


async def _job(settings_data: dict[str, Any], req_data: dict[str, Any], run_id: int) -> None:
    # Backtests never trade: whatever the parent's MODE, the child runs as paper.
    settings = Settings(
        _env_file=None, **{**settings_data, "mode": "paper", "live_trading_confirmed": False}
    )
    engine = make_engine(settings.database_url)
    try:
        sessions = make_sessionmaker(engine)
        runs = SqlBacktestStore(sessions)
        try:
            result = await run_backtest(
                settings, _request_from(req_data), SqlHistoryStore(sessions)
            )
        except Exception as exc:
            await runs.fail(run_id, f"{type(exc).__name__}: {exc}")
            return
        await runs.finish(run_id, result)
    finally:
        await engine.dispose()


def run_job(settings_data: dict[str, Any], req_data: dict[str, Any], run_id: int) -> None:
    """Entry point executed in the worker process."""
    asyncio.run(_job(settings_data, req_data, run_id))


class ProcessBacktestExecutor:
    def __init__(self, settings: Settings, workers: int = 1) -> None:
        # Secrets aren't needed (or serialised); only plain settings cross the boundary.
        self._settings = {
            k: v
            for k, v in settings.model_dump(mode="json").items()
            if not k.endswith(("_token", "_secret"))
        }
        self._pool = ProcessPoolExecutor(max_workers=workers)

    async def __call__(self, req: BacktestRequest, run_id: int) -> None:
        data = asdict(req)
        data["start"], data["end"] = req.start.isoformat(), req.end.isoformat()
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(self._pool, run_job, self._settings, data, run_id)

    def shutdown(self) -> None:
        self._pool.shutdown(wait=False, cancel_futures=True)
