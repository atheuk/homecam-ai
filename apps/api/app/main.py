import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from .ai.detector import detector_status
from .api.admin_routes import router as admin_router
from .api.admin_routes import zones_router as admin_zones_router
from .api.auth_routes import router as auth_router
from .api.routes import router
from .config import settings
from .db import SessionLocal, init_db
from .services.cameras import sync_cameras
from .services.ingestion import ingestion_service
from .services.provider_registry import discover_all_cameras
from .services import detector_watchdog

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_db()
    # Build the detector at startup, not lazily on the first frame: a
    # misconfigured backend must be visible in the first seconds of a
    # rollout, not three hours later (incident 2026-09-30). With
    # AI_DETECTOR_STRICT=true this raises and the app refuses to start.
    status = detector_status()
    if status.degraded:
        logger.error(
            "STARTING BLIND: requested detector backend '%s' is not active (%s); "
            "see GET /api/v1/system/status",
            status.requested_backend,
            status.reason,
        )
    async with SessionLocal() as session:
        await sync_cameras(session, await discover_all_cameras())
    if settings.event_ingestion_enabled:
        ingestion_service.start()
    try:
        yield
    finally:
        await ingestion_service.stop()


app = FastAPI(title="HomeCam AI API", version="0.1.0", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=[origin.strip() for origin in settings.cors_origins.split(",") if origin.strip()],
    allow_methods=["*"],
    allow_headers=["*"],
)
app.include_router(router)
app.include_router(auth_router)
app.include_router(admin_router)
app.include_router(admin_zones_router)


@app.get("/health")
async def root_health():
    return {"status": "ok"}


@app.get("/ready")
async def root_ready():
    """Readiness, with the detector's real state attached.

    The HTTP status stays 200 even when the detector is degraded: failing
    the probe would have the platform pull a *working* ingestion pipeline
    out of service, which is worse than degraded operation for a security
    system. The body says so unambiguously instead, and
    ``GET /api/v1/system/status`` returns 503 for probes that should page.
    """
    status = detector_status()
    watchdog = detector_watchdog.status()
    blind = status.degraded or watchdog["blackout"]
    return {
        "status": "degraded" if blind else "ready",
        "detector": status.as_dict(),
        "detector_watchdog": watchdog,
    }
