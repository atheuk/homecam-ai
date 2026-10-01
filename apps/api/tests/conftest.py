"""Shared pytest fixtures.

Two isolation concerns matter for these tests:

1. The database must be a fresh temp file per test session (not the dev
   ``homecam.db``), set *before* ``app.main`` is imported anywhere, since
   ``Settings`` reads ``DATABASE_URL`` at import time.
2. The mock providers are process-wide singletons. Tests that simulate
   offline/degraded/battery/outage state must not leak that state into
   other tests, so we snapshot and restore provider state around every test.

The async SQLAlchemy engine is created once at module import and its
connection pool becomes bound to whichever event loop first uses it, so all
tests must share a single session-scoped event loop rather than the default
per-test loop pytest-asyncio would otherwise create.
"""
import asyncio
import copy
import os
import shutil
import tempfile

_db_fd, _db_path = tempfile.mkstemp(suffix=".db")
os.close(_db_fd)
os.environ["DATABASE_URL"] = f"sqlite+aiosqlite:///{_db_path}"
# Best-photo files must land in a throwaway directory, never the repo.
_media_root = tempfile.mkdtemp(prefix="homecam-media-")
os.environ["MEDIA_ROOT"] = _media_root

import pytest  # noqa: E402
from httpx import ASGITransport, AsyncClient  # noqa: E402


@pytest.fixture(scope="session")
def event_loop():
    loop = asyncio.new_event_loop()
    yield loop
    loop.close()


@pytest.fixture(autouse=True)
def _reset_provider_state():
    from app.providers.dahua.edge_provider import reset_liveness_evidence
    from app.providers.mock import mock_eufy_provider, mock_provider
    from app.services.provider_registry import reset_discovery_cache

    reset_discovery_cache()
    reset_liveness_evidence()
    snapshots = {
        mock_provider.id: copy.deepcopy(mock_provider._cameras),
        mock_eufy_provider.id: copy.deepcopy(mock_eufy_provider._cameras),
    }
    yield
    mock_provider._cameras = copy.deepcopy(snapshots[mock_provider.id])
    mock_provider._unavailable = False
    mock_eufy_provider._cameras = copy.deepcopy(snapshots[mock_eufy_provider.id])
    mock_eufy_provider._unavailable = False
    reset_discovery_cache()
    reset_liveness_evidence()


@pytest.fixture(autouse=True)
def _reset_ai_state():
    """The detector script and dwell tracker are process-wide singletons;
    leaking them between tests would make pipeline assertions order
    dependent."""
    from app.ai.detector import mock_detector
    from app.ai.dwell import dwell_tracker

    mock_detector().clear_script()
    dwell_tracker.reset()
    yield
    mock_detector().clear_script()
    dwell_tracker.reset()


@pytest.fixture(autouse=True)
async def _reset_scene_state():
    """Vehicle tracks and zone states persist in the DB by design; tests
    must not inherit another test's parked car. The Foundry scene verifier
    is off unless a test installs a fake one."""
    from sqlalchemy import delete
    from sqlalchemy.exc import OperationalError

    from app.ai.scene_verifier import reset_scene_verifier, set_scene_verifier
    from app.db import SessionLocal
    from app.models.db import IngestionLease, SceneState, VehicleTrack
    from app.services import ingestion_lease, scene_state

    async def _clear():
        scene_state.reset_memory()
        ingestion_lease.keeper.reset()
        try:
            async with SessionLocal() as session:
                await session.execute(delete(VehicleTrack))
                await session.execute(delete(SceneState))
                await session.execute(delete(IngestionLease))
                await session.commit()
        except OperationalError:
            pass  # tables not created yet (no client fixture used so far)

    set_scene_verifier(None)
    await _clear()
    yield
    await _clear()
    reset_scene_verifier()


@pytest.fixture
async def client():
    from app.main import app

    async with app.router.lifespan_context(app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            yield c


def pytest_sessionfinish(session, exitstatus):
    try:
        os.remove(_db_path)
    except OSError:
        pass
    shutil.rmtree(_media_root, ignore_errors=True)


