import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from .ai.detector import detector_status
from .api.admin_routes import router as admin_router
from .api.admin_routes import zones_router as admin_zones_router
from .api.auth_routes import router as auth_router
from .api.notification_routes import router as notification_router
from .api.retention_routes import router as retention_router
from .api.routes import router
from .api.security_routes import router as security_router
from .auth.dependencies import COOKIE_NAME
from .ai import camera_health
from .config import settings
from .db import SessionLocal, init_db
from .services.arming_scheduler import arming_scheduler
from .services.cameras import sync_cameras
from .services.digest_scheduler import digest_scheduler
from .services.ingestion import ingestion_service
from .services.provider_registry import discover_all_cameras
from .services.retention_scheduler import retention_scheduler
from .services import detector_watchdog, event_photos, incident_clips

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
    await camera_health.seed_from_open_incidents(SessionLocal)
    await event_photos.recover_pending()
    if settings.event_ingestion_enabled:
        ingestion_service.start()
    digest_scheduler.start()
    arming_scheduler.start()
    retention_scheduler.start()
    try:
        yield
    finally:
        await incident_clips.stop()
        await ingestion_service.stop()
        await event_photos.stop()
        await digest_scheduler.stop()
        await arming_scheduler.stop()
        await retention_scheduler.stop()


app = FastAPI(title="HomeCam AI API", version="0.1.0", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=[origin.strip() for origin in settings.cors_origins.split(",") if origin.strip()],
    allow_methods=["*"],
    allow_headers=["*"],
    allow_credentials=True,
)


@app.middleware("http")
async def protect_cookie_authenticated_writes(request: Request, call_next):
    if (
        request.method not in {"GET", "HEAD", "OPTIONS"}
        and request.url.path not in {"/api/v1/auth/login", "/api/v1/auth/register"}
        and COOKIE_NAME in request.cookies
        and not request.headers.get("authorization")
        and request.headers.get("x-homecam-request") != "1"
    ):
        return JSONResponse(status_code=403, content={"detail": "Invalid browser request"})
    return await call_next(request)

app.include_router(router)
app.include_router(auth_router)
app.include_router(admin_router)
app.include_router(admin_zones_router)
app.include_router(security_router)
app.include_router(retention_router)
app.include_router(notification_router)


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
