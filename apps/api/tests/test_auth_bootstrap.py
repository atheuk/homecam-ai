"""Production bootstrap requires an out-of-band secret and is single-use."""
import asyncio
from types import SimpleNamespace

import pytest
from sqlalchemy import func, select
from sqlalchemy.engine import URL
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine


async def _isolated_database(app, get_db, tmp_path):
    from app.models.db import Base

    engine = create_async_engine(
        URL.create("sqlite+aiosqlite", database=str(tmp_path / "bootstrap.db"))
    )
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)

    async def override_get_db():
        async with session_factory() as session:
            yield session

    previous_override = app.dependency_overrides.get(get_db)
    app.dependency_overrides[get_db] = override_get_db
    return engine, session_factory, previous_override


def _restore_database_override(app, get_db, previous_override):
    if previous_override is None:
        app.dependency_overrides.pop(get_db, None)
    else:
        app.dependency_overrides[get_db] = previous_override


@pytest.mark.asyncio
async def test_production_bootstrap_requires_secret_and_closes_after_first_user(
    anonymous_client, monkeypatch, tmp_path
):
    from app.config import settings
    from app.db import get_db
    from app.main import app
    from app.models.db import User

    secret = "out-of-band-bootstrap-secret"
    monkeypatch.setattr(settings, "app_env", "production")
    monkeypatch.setattr(settings, "auth_bootstrap_secret", secret)
    engine, session_factory, previous_override = await _isolated_database(app, get_db, tmp_path)
    payload = {"email": "first-user@example.com", "password": "safe-password-123"}
    try:
        missing = await anonymous_client.post("/api/v1/auth/register", json=payload)
        wrong = await anonymous_client.post(
            "/api/v1/auth/register",
            json=payload,
            headers={"X-HomeCam-Bootstrap-Secret": "incorrect"},
        )
        assert missing.status_code == wrong.status_code == 403
        assert secret not in missing.text and secret not in wrong.text

        async with session_factory() as session:
            assert await session.scalar(select(func.count()).select_from(User)) == 0

        created = await anonymous_client.post(
            "/api/v1/auth/register",
            json=payload,
            headers={"X-HomeCam-Bootstrap-Secret": secret},
        )
        assert created.status_code == 201, created.text
        login = await anonymous_client.post("/api/v1/auth/login", json=payload)
        assert login.status_code == 200
        set_cookie = login.headers["set-cookie"].lower()
        assert "httponly" in set_cookie
        assert "secure" in set_cookie
        assert "samesite=none" in set_cookie
        assert "path=/" in set_cookie

        later = await anonymous_client.post(
            "/api/v1/auth/register",
            json={"email": "second-user@example.com", "password": "safe-password-123"},
            headers={"X-HomeCam-Bootstrap-Secret": secret},
        )
        assert later.status_code == 403
        assert secret not in later.text
        async with session_factory() as session:
            users = list((await session.execute(select(User))).scalars())
            assert [user.email for user in users] == [payload["email"]]
    finally:
        _restore_database_override(app, get_db, previous_override)
        await engine.dispose()


@pytest.mark.asyncio
async def test_production_bootstrap_is_disabled_without_configured_secret(
    anonymous_client, monkeypatch, tmp_path
):
    from app.config import settings
    from app.db import get_db
    from app.main import app
    from app.models.db import User

    monkeypatch.setattr(settings, "app_env", "production")
    monkeypatch.setattr(settings, "auth_bootstrap_secret", None)
    engine, session_factory, previous_override = await _isolated_database(app, get_db, tmp_path)
    try:
        response = await anonymous_client.post(
            "/api/v1/auth/register",
            json={"email": "unconfigured@example.com", "password": "safe-password-123"},
            headers={"X-HomeCam-Bootstrap-Secret": "any-value"},
        )
        assert response.status_code == 403
        async with session_factory() as session:
            assert await session.scalar(select(func.count()).select_from(User)) == 0
    finally:
        _restore_database_override(app, get_db, previous_override)
        await engine.dispose()


@pytest.mark.asyncio
async def test_concurrent_production_bootstrap_attempts_create_exactly_one_user(
    anonymous_client, monkeypatch, tmp_path
):
    from app.config import settings
    from app.db import get_db
    from app.main import app
    from app.models.db import User

    secret = "concurrent-bootstrap-secret"
    monkeypatch.setattr(settings, "app_env", "production")
    monkeypatch.setattr(settings, "auth_bootstrap_secret", secret)
    engine, session_factory, previous_override = await _isolated_database(app, get_db, tmp_path)
    try:
        responses = await asyncio.gather(*(
            anonymous_client.post(
                "/api/v1/auth/register",
                json={"email": f"owner-{index}@example.com", "password": "safe-password-123"},
                headers={"X-HomeCam-Bootstrap-Secret": secret},
            )
            for index in range(2)
        ))
        assert sorted(response.status_code for response in responses) == [201, 403]
        async with session_factory() as session:
            users = list((await session.execute(select(User))).scalars())
            assert len(users) == 1
    finally:
        _restore_database_override(app, get_db, previous_override)
        await engine.dispose()


@pytest.mark.asyncio
async def test_registration_lock_uses_a_database_advisory_lock_for_postgres():
    from app.api.auth_routes import _lock_initial_registration

    class PostgresSession:
        def __init__(self):
            self.statement = None
            self.params = None

        def get_bind(self):
            return SimpleNamespace(dialect=SimpleNamespace(name="postgresql"))

        async def execute(self, statement, params):
            self.statement = str(statement)
            self.params = params

    session = PostgresSession()
    await _lock_initial_registration(session)
    assert "pg_advisory_xact_lock(hashtextextended" in session.statement
    assert session.params == {"key": "homecam-ai:initial-account"}
