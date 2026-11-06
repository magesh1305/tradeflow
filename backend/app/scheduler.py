"""Daily jobs (IST). Trading jobs run on weekdays. Exchange holidays are not
modelled: on a holiday the jobs run against an idle market and do nothing harmful.

  08:45  reload instrument master
  09:00  reset daily risk counters (the kill switch is only reset by a human)
  SQUARE_OFF_TIME  MIS square-off (the engine also does this on market time; idempotent)
  15:35  end-of-day summary -> daily_pnl + Telegram
  15:40  record today's 1-minute candles around ATM (expired contracts can't be fetched later)
  SESSION_EXPIRY_TIME (06:00, every day)  stop the engine; the Kite token is dead
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from app.core.clock import IST

if TYPE_CHECKING:
    from app.runtime import Runtime

WEEKDAYS = "mon-fri"


def build_scheduler(runtime: Runtime) -> AsyncIOScheduler:
    s = runtime.settings
    sched = AsyncIOScheduler(timezone=IST)
    common = {"coalesce": True, "max_instances": 1, "misfire_grace_time": 300}

    def weekday(hour: int, minute: int) -> CronTrigger:
        return CronTrigger(day_of_week=WEEKDAYS, hour=hour, minute=minute, timezone=IST)

    sched.add_job(runtime.reload_instruments, weekday(8, 45), id="reload_instruments", **common)
    sched.add_job(runtime.risk_reset, weekday(9, 0), id="risk_reset", **common)
    sq = s.square_off_time
    sched.add_job(runtime.square_off, weekday(sq.hour, sq.minute), id="square_off", **common)
    sched.add_job(runtime.end_of_day, weekday(15, 35), id="end_of_day", **common)
    sched.add_job(runtime.record_candles, weekday(15, 40), id="record_candles", **common)
    exp = s.session_expiry_time
    sched.add_job(
        runtime.session_expired,
        CronTrigger(hour=exp.hour, minute=exp.minute, timezone=IST),
        id="session_expiry",
        **common,
    )
    return sched
