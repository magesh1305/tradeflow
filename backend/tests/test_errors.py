from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.exc import OperationalError

from app.core.errors import DATABASE_DOWN, is_database_error
from app.main import create_app


def test_recognises_database_errors_even_when_wrapped() -> None:
    assert is_database_error(OperationalError("SELECT 1", {}, Exception("down")))
    assert is_database_error(ConnectionRefusedError(1225, "refused"))
    try:
        try:
            raise ConnectionRefusedError("refused")
        except ConnectionRefusedError as inner:
            raise RuntimeError("query failed") from inner
    except RuntimeError as outer:
        assert is_database_error(outer)
    assert not is_database_error(ValueError("bad input"))


@pytest.fixture
def client() -> TestClient:
    app = create_app()

    @app.get("/boom-db")
    async def boom_db() -> None:
        raise OperationalError("SELECT 1", {}, ConnectionRefusedError("refused"))

    @app.get("/boom")
    async def boom() -> None:
        raise ValueError("bug")

    # No `with`: the lifespan (engine, scheduler) does not start.
    return TestClient(app, raise_server_exceptions=False)


def test_database_outage_is_a_clear_503(client: TestClient) -> None:
    r = client.get("/boom-db")
    assert r.status_code == 503
    assert r.json() == {"detail": DATABASE_DOWN}


def test_unexpected_errors_are_json(client: TestClient) -> None:
    r = client.get("/boom")
    assert r.status_code == 500
    assert "backend log" in r.json()["detail"]
