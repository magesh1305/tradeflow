"""Persistence of backtest runs (`backtest_runs`)."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.backtest.runner import BacktestRequest, BacktestResult
from app.core.clock import now_ist
from app.storage.models import BacktestRunRow


class BacktestStore(Protocol):
    async def create(self, req: BacktestRequest) -> int: ...
    async def finish(self, run_id: int, result: BacktestResult) -> None: ...
    async def fail(self, run_id: int, error: str) -> None: ...
    async def get(self, run_id: int) -> dict[str, Any] | None: ...
    async def list(self, limit: int = 50) -> list[dict[str, Any]]: ...


def _result_fields(result: BacktestResult) -> dict[str, Any]:
    metrics = result.metrics.as_dict()
    metrics["trades_list"] = result.trades
    metrics["notes"] = result.notes
    metrics["non_trading_days"] = result.non_trading_days
    return {
        "status": "done",
        "finished_at": now_ist(),
        "metrics": metrics,
        "equity_curve": result.equity_curve,
        "missing_data": result.missing_data,
    }


class SqlBacktestStore:
    def __init__(self, sessions: async_sessionmaker[AsyncSession]) -> None:
        self._sessions = sessions

    async def create(self, req: BacktestRequest) -> int:
        row = BacktestRunRow(
            created_at=now_ist(),
            finished_at=None,
            status="running",
            strategy=req.strategy,
            params=req.params,
            start_date=req.start,
            end_date=req.end,
            metrics=None,
            equity_curve=None,
            missing_data=None,
            error=None,
        )
        async with self._sessions.begin() as s:
            s.add(row)
            await s.flush()
            return int(row.id)

    async def finish(self, run_id: int, result: BacktestResult) -> None:
        async with self._sessions.begin() as s:
            await s.execute(
                update(BacktestRunRow)
                .where(BacktestRunRow.id == run_id)
                .values(**_result_fields(result))
            )

    async def fail(self, run_id: int, error: str) -> None:
        async with self._sessions.begin() as s:
            await s.execute(
                update(BacktestRunRow)
                .where(BacktestRunRow.id == run_id)
                .values(status="failed", finished_at=now_ist(), error=error[:4000])
            )

    async def get(self, run_id: int) -> dict[str, Any] | None:
        t = BacktestRunRow.__table__
        async with self._sessions() as s:
            row = (await s.execute(select(t).where(t.c.id == run_id))).first()
            return dict(row._mapping) if row else None

    async def list(self, limit: int = 50) -> list[dict[str, Any]]:
        t = BacktestRunRow.__table__
        cols = [c for c in t.c if c.name not in ("equity_curve", "missing_data")]
        async with self._sessions() as s:
            rows = await s.execute(select(*cols).order_by(t.c.id.desc()).limit(limit))
            return [dict(r._mapping) for r in rows]


@dataclass
class MemoryBacktestStore:
    runs: dict[int, dict[str, Any]] = field(default_factory=dict)

    async def create(self, req: BacktestRequest) -> int:
        run_id = len(self.runs) + 1
        self.runs[run_id] = {
            "id": run_id,
            "created_at": now_ist(),
            "finished_at": None,
            "status": "running",
            "strategy": req.strategy,
            "params": req.params,
            "start_date": req.start,
            "end_date": req.end,
            "metrics": None,
            "equity_curve": None,
            "missing_data": None,
            "error": None,
        }
        return run_id

    async def finish(self, run_id: int, result: BacktestResult) -> None:
        self.runs[run_id].update(_result_fields(result))

    async def fail(self, run_id: int, error: str) -> None:
        self.runs[run_id].update(status="failed", finished_at=now_ist(), error=error)

    async def get(self, run_id: int) -> dict[str, Any] | None:
        return self.runs.get(run_id)

    async def list(self, limit: int = 50) -> list[dict[str, Any]]:
        rows = sorted(self.runs.values(), key=lambda r: r["id"], reverse=True)[:limit]
        return [
            {k: v for k, v in r.items() if k not in ("equity_curve", "missing_data")} for r in rows
        ]


async def execute(
    settings: Any, req: BacktestRequest, history: Any, runs: BacktestStore
) -> tuple[int, BacktestResult | None]:
    """Create the run row, run, and store the result or the failure."""
    from app.backtest.runner import run_backtest  # noqa: PLC0415

    run_id = await runs.create(req)
    try:
        result = await run_backtest(settings, req, history)
    except Exception as exc:
        await runs.fail(run_id, f"{type(exc).__name__}: {exc}")
        raise
    await runs.finish(run_id, result)
    return run_id, result
