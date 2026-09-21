# HomeCam AI

Provider-independent, local-first camera dashboard foundation. Phase 0/1 mock foundations are implemented, with opt-in provider-isolated Dahua and Eufy integration seams that are disabled by default unless local runtime configuration is supplied.

## Start with Docker

```bash
copy .env.example .env       # PowerShell: Copy-Item .env.example .env
docker compose up -d --build
```

Open http://localhost:3000. API docs: http://localhost:8000/docs. API health: http://localhost:8000/health.

## Local development

Backend (Python 3.12):
```bash
cd apps/api
pip install -r requirements.txt
uvicorn app.main:app --reload
pytest
```
Frontend (Node 22):
```bash
cd apps/web
npm install
npm run dev
npm test
npm run build
```

## What is included

- FastAPI `/api/v1` routes with OpenAPI, typed settings, structured basic logging, health/readiness, and CORS.
- SQLAlchemy models, PostgreSQL/pgvector-compatible Compose database, and Alembic migrations (initial schema + auth/status). SQLite is the default for lightweight local tests; run migrations with `alembic upgrade head` from `apps/api`.
- Cameras and events are persisted to the database (not just held in memory): the mock providers are the source of camera/event *facts*, but every camera sync and every created event is written through the SQLAlchemy models, and `GET /cameras` / `GET /events` read back from the database. An in-memory pub/sub bus (`app/services/events.py`) is layered on top purely for real-time SSE fan-out.
- HomeCam-native authentication (SPEC section 27): `POST /api/v1/auth/register`, `POST /api/v1/auth/login`, `POST /api/v1/auth/logout`, `GET /api/v1/auth/me`. Passwords are hashed with PBKDF2-HMAC-SHA256 (260k iterations, stdlib `hashlib` only — chosen over bcrypt/argon2-cffi specifically because those need native wheels that are not reliably available on Windows ARM64). Sessions are opaque tokens stored **hashed** in the `auth_sessions` table with an expiry (`SESSION_TTL_MINUTES`, default 720) and are accepted either as a `Authorization: Bearer <token>` header or an httponly `homecam_session` cookie set at login.
- Redis and a worker service seam; the worker is intentionally idle until background jobs are added.
- Deterministic mock providers with five cameras (including mock Eufy T8210 doorbell), snapshots, HLS-shaped live URLs, and controllable mock events. Capabilities are returned as a **status map** (`{"snapshot": "SUPPORTED", "vehicleEvents": "UNSUPPORTED", ...}` with values `SUPPORTED` / `UNSUPPORTED` / `UNAVAILABLE` / `UNKNOWN`) per SPEC section 5, not a flat feature list.
- Provider-isolated Dahua and Eufy adapters live under `apps/api/app/providers/dahua` and `apps/api/app/providers/eufy`. They are opt-in via environment variables, keep credentials server-side, and degrade cleanly when host/adapter settings are absent. See `docs/dahua.md` and `docs/eufy.md`.
- **AI event-analysis pipeline** applied identically to every provider (SPEC sections 12–15, 18/19, 31/32): local object detection (person/car/truck/bicycle/motorcycle/dog/cat/package), user-defined camera zones, dwell-based parked-vs-passing vehicle logic, mailbox/driveway/animal semantics, a sharp cropped "best photo" per detected event, grounded AI analysis + embeddings persisted to `ai_analyses`, and cross-camera activity correlation exposed at `GET /api/v1/activities`. Everything runs with **zero extra dependencies** by default (`AI_DETECTOR_BACKEND=mock`, `MockAIProvider`); the ONNX detector and audio VAD are strictly opt-in and fall back with a log line instead of failing a request. Zones are managed from the Admin panel or `/api/v1/admin/cameras/{id}/zones`. New capability key `audioDetection` joins the existing status map (`UNAVAILABLE` for Dahua/Eufy today — no provider exposes an audio buffer yet). See `docs/ai-pipeline.md`.
- Simulation controls for local development/testing: `POST /api/v1/mock/cameras/{id}/status` (`online`/`offline`/`degraded`/`unknown`), `POST /api/v1/mock/cameras/{id}/battery` (drains battery and automatically raises a high-priority `battery_low` event once below `LOW_BATTERY_THRESHOLD`, default 20%), `POST /api/v1/mock/providers/{id}/outage` (simulates a whole provider — e.g. the Eufy HomeBase — becoming unreachable while the other provider keeps working, proving provider-failure isolation). `GET /api/v1/providers` reports each provider's aggregated health (`ONLINE`/`DEGRADED`/`OFFLINE`) based on how many of its cameras are online.
- Offline/degraded cameras return `503` from `/snapshot` and `/live` instead of crashing or silently returning stale/fake data; unknown camera/provider ids return `404`; invalid payloads return `422`.
- SSE real-time stream at `/api/v1/ws` (named `event.created` events).
- Responsive dashboard with overview, live/events/system/settings navigation scaffolding.
- Backend tests cover provider discovery/capabilities, event persistence, camera offline/degraded state, doorbell/battery-low high-priority events, provider failure isolation, real-time SSE delivery, auth register/login/logout/me, API validation (404/422), mocked Dahua/Eufy adapter contracts, and the AI pipeline (detector determinism, zone-overlap math, dwell/parked-car logic, mailbox/animal derivation, best-photo scoring, AI-analysis persistence and linkage, activity correlation, audio VAD, `audioDetection` advertisement, and zone admin CRUD/auth). Frontend tests (Vitest + Testing Library) cover rendering, camera list display, the events tab, and the admin zone editor. GitHub Actions CI runs backend lint/tests, frontend lint/typecheck/tests/build, and a Docker Compose config + build validation job.

Run the smoke test against a running API with `python scripts/smoke.py`.

Generate an event:
```bash
curl -X POST http://localhost:8000/api/v1/mock/events -H "Content-Type: application/json" -d "{\"camera_id\":\"mock-eufy-doorbell\",\"type\":\"doorbell\"}"
```

Register and log in:
```bash
curl -X POST http://localhost:8000/api/v1/auth/register -H "Content-Type: application/json" -d "{\"email\":\"owner@example.com\",\"password\":\"supersecret1\"}"
curl -X POST http://localhost:8000/api/v1/auth/login -H "Content-Type: application/json" -d "{\"email\":\"owner@example.com\",\"password\":\"supersecret1\"}"
```

## Limitations

The mock snapshot is a deterministic placeholder, not a real image. MediaMTX is present but not connected to camera hardware. Authentication is a minimal local scaffold (PBKDF2 + opaque DB-backed session tokens) suitable for local development only — there is no email verification, password reset, rate limiting, MFA, or production identity provider integration, and the frontend has no login UI yet (auth is API-only in this phase). Dahua and Eufy code is an opt-in integration boundary with mocked contract tests; no live hardware verification has been claimed without a locally configured reachable LAN host/adapter. The AI pipeline ships with a deterministic mock detector and mock AI provider: the ONNX detector backend is opt-in and has **not** been verified here against a real model or hardware, no model weights are bundled, audio detection finds speech-like activity only (no transcription, no speaker identity) and is `UNAVAILABLE` because no provider currently exposes an audio buffer, there is no person identity/face clustering, and embeddings are stored as JSON arrays rather than a native pgvector column so similarity search is not index-accelerated yet (see `docs/ai-pipeline.md`). There are no Azure, cloud AI inference, notifications, search, retention jobs, or production deployment integrations. Do not expose this development stack to the internet.

## Repository

`apps/api` contains the FastAPI service and provider abstraction. `apps/web` contains the Next.js UI. `infrastructure` and Compose contain local services. `docs` is reserved for architecture and integration notes. The full source specification is copied to `SPEC.md`.

A previous version of this README noted that `greenlet` (a SQLAlchemy async dependency) could not be built from source on Windows ARM64. As of this pass, `greenlet==3.5.5` ships a prebuilt `win_arm64` wheel, so host-native `pytest`, `uvicorn`, and `alembic upgrade head` all now run directly on Windows ARM64 without Docker. `asyncpg` (the PostgreSQL driver) still has no Windows ARM64 wheel, so real PostgreSQL connectivity should be exercised through Docker Compose (Linux wheels); SQLite (`aiosqlite`) remains the default for host-native tests and development. Docker Compose itself has not been runtime-validated on this development host (no Docker CLI available here); the `docker-validate` CI job runs `docker compose config` and `docker compose build` on every push to catch Compose/Dockerfile regressions.
