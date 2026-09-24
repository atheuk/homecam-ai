import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from .api.admin_routes import router as admin_router
from .api.admin_routes import zones_router as admin_zones_router
from .api.auth_routes import router as auth_router
from .api.routes import router
from .config import settings
from .db import SessionLocal, init_db
from .services.cameras import sync_cameras
from .services.ingestion import ingestion_service
from .services.provider_registry import discover_all_cameras

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")


@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_db()
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
    return {"status": "ready"}
