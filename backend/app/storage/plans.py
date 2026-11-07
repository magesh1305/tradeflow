"""Persistence for daily trading plans and their audit trail."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any, Protocol

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.storage.models import DailyPlanRow, PlanEventRow


@dataclass(frozen=True, slots=True)
class StoredPlan:
    trading_date: date
    plan: dict[str, Any]
    submitted_at: datetime
    updated_at: datetime


class PlanStore(Protocol):
    async def get(self, trading_date: date, mode: str) -> StoredPlan | None: ...
    async def latest(self, mode: str) -> StoredPlan | None:
        """Most recent plan on any date (to pre-fill the next morning's form)."""

    async def save(
        self,
        trading_date: date,
        mode: str,
        plan: dict[str, Any],
        *,
        now: datetime,
        changes: list[str],
        loosened: list[str],
    ) -> None: ...
    async def events(self, trading_date: date, mode: str) -> list[dict[str, Any]]: ...


class SqlPlanStore:
    def __init__(self, sessions: async_sessionmaker[AsyncSession]) -> None:
        self._sessions = sessions

    @staticmethod
    def _row(r: Any) -> StoredPlan:
        return StoredPlan(r.trading_date, dict(r.plan), r.submitted_at, r.updated_at)

    async def get(self, trading_date: date, mode: str) -> StoredPlan | None:
        t = DailyPlanRow.__table__
        async with self._sessions() as s:
            r = (
                await s.execute(select(t).where(t.c.trading_date == trading_date, t.c.mode == mode))
            ).first()
            return self._row(r) if r else None

    async def latest(self, mode: str) -> StoredPlan | None:
        t = DailyPlanRow.__table__
        async with self._sessions() as s:
            r = (
                await s.execute(
                    select(t).where(t.c.mode == mode).order_by(t.c.trading_date.desc()).limit(1)
                )
            ).first()
            return self._row(r) if r else None

    async def save(
        self,
        trading_date: date,
        mode: str,
        plan: dict[str, Any],
        *,
        now: datetime,
        changes: list[str],
        loosened: list[str],
    ) -> None:
        async with self._sessions.begin() as s:
            stmt = insert(DailyPlanRow).values(
                trading_date=trading_date, mode=mode, plan=plan, submitted_at=now, updated_at=now
            )
            await s.execute(
                stmt.on_conflict_do_update(
                    index_elements=["trading_date", "mode"],
                    set_={"plan": stmt.excluded.plan, "updated_at": stmt.excluded.updated_at},
                )
            )
            await s.execute(
                insert(PlanEventRow).values(
                    ts=now,
                    trading_date=trading_date,
                    mode=mode,
                    changes=changes,
                    loosened=loosened,
                    plan=plan,
                )
            )

    async def events(self, trading_date: date, mode: str) -> list[dict[str, Any]]:
        t = PlanEventRow.__table__
        async with self._sessions() as s:
            rows = await s.execute(
                select(t)
                .where(t.c.trading_date == trading_date, t.c.mode == mode)
                .order_by(t.c.ts.desc())
            )
            return [dict(r._mapping) for r in rows]


@dataclass
class MemoryPlanStore:
    plans: dict[tuple[date, str], StoredPlan] = field(default_factory=dict)
    log: list[dict[str, Any]] = field(default_factory=list)
    fail = False

    async def get(self, trading_date: date, mode: str) -> StoredPlan | None:
        return self.plans.get((trading_date, mode))

    async def latest(self, mode: str) -> StoredPlan | None:
        mine = [p for (_, m), p in self.plans.items() if m == mode]
        return max(mine, key=lambda p: p.trading_date) if mine else None

    async def save(
        self,
        trading_date: date,
        mode: str,
        plan: dict[str, Any],
        *,
        now: datetime,
        changes: list[str],
        loosened: list[str],
    ) -> None:
        if self.fail:
            raise ConnectionError("database unavailable")
        prev = self.plans.get((trading_date, mode))
        self.plans[(trading_date, mode)] = StoredPlan(
            trading_date, plan, prev.submitted_at if prev else now, now
        )
        self.log.append(
            {
                "id": len(self.log) + 1,
                "ts": now,
                "trading_date": trading_date,
                "mode": mode,
                "changes": changes,
                "loosened": loosened,
                "plan": plan,
            }
        )

    async def events(self, trading_date: date, mode: str) -> list[dict[str, Any]]:
        rows = [e for e in self.log if e["trading_date"] == trading_date and e["mode"] == mode]
        return sorted(rows, key=lambda e: e["ts"], reverse=True)
