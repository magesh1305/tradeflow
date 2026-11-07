"""Recognising infrastructure failures, so the API can answer with a clear message
instead of a bare "Internal Server Error"."""

from __future__ import annotations

from sqlalchemy.exc import SQLAlchemyError

DATABASE_DOWN = (
    "The database is not reachable, so saved history is unavailable. "
    "Live trading data still works. Start the database (docker compose up) "
    "or check DATABASE_URL."
)


def is_database_error(exc: BaseException) -> bool:
    """True for errors raised while talking to Postgres (also when wrapped)."""
    seen: set[int] = set()
    cur: BaseException | None = exc
    while cur is not None and id(cur) not in seen:
        seen.add(id(cur))
        if isinstance(cur, SQLAlchemyError) or type(cur).__module__.startswith("asyncpg"):
            return True
        if isinstance(cur, ConnectionError | TimeoutError):
            return True
        cur = cur.__cause__ or cur.__context__
    return False
