"""Production account bootstrap is atomic and closes after the first user."""
import asyncio
from types import SimpleNamespace

import pytest
from sqlalchemy import func, select
from sqlalchemy.engine import URL
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine


@pytest.mark.asyncio
async def test_production_registration_allows_one_concurrent_bootstrap_user(
    anonymous_client, monkeypatch, tmp_path
):
    from app.config import settings
    from app.db import get_db
    from app.main import app
    from app.models.db import Base, User

    monkeypatch.setattr(settings, "app_env", "production")
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
    try:
        registrations = await asyncio.gather(
            *[anonymous_client.post(
                "/api/v1/auth/register",
                json={"email": f"bootstrap-{index}@example.com", "password": "safe-password-123"},
            ) for index in range(2)]
        )
        assert sorted(response.status_code for response in registrations) == [201, 403]

        async with session_factory() as session:
            users = list((await session.execute(select(User))).scalars())
            assert len(users) == 1
            first_user_email = users[0].email

        blocked = await anonymous_client.post(
            "/api/v1/auth/register",
            json={"email": "later-user@example.com", "password": "safe-password-123"},
        )
        assert blocked.status_code == 403
        assert (await anonymous_client.post(
            "/api/v1/auth/register",
            json={"email": first_user_email, "password": "safe-password-123"},
        )).status_code == 403
        async with session_factory() as session:
            assert await session.scalar(select(func.count()).select_from(User)) == 1
    finally:
        if previous_override is None:
            app.dependency_overrides.pop(get_db, None)
        else:
            app.dependency_overrides[get_db] = previous_override
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
