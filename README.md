# HomeCam AI

Provider-independent, local-first camera dashboard foundation. Phase 0/1 mock foundations are implemented, with opt-in provider-isolated Dahua and Eufy integration seams that are disabled by default unless local runtime configuration is supplied.

## Start with Docker

```bash
copy .env.example .env       # PowerShell: Copy-Item .env.example .env
docker compose up -d --build
```

Open http://localhost:3000. API docs: http://localhost:8000/docs. API health: http://localhost:8000/health.

This default stack is the **mock** demo: mock cameras, `AI_DETECTOR_BACKEND=mock` (cannot see pixels) and no background ingestion. It needs nothing but Docker, but it will never detect anything on a real camera.

## Run the real stack locally

To run what Azure runs — real RT-DETR detection against your Dahua NVR — layer the committed `docker-compose.local.yml` on top. It sets `AI_DETECTOR_BACKEND=rtdetr`, `AI_DETECTOR_MODEL_PATH=/app/models/rtdetr.onnx` (baked into the image by `apps/api/Dockerfile`; no huggingface.co access at runtime) and `EVENT_INGESTION_ENABLED=true` for the `api` service, overriding whatever `.env` says.

1. In your private `.env` (never commit it), point at the NVR directly:
   ```bash
   DAHUA_ENABLED=true
   DAHUA_MODE=direct
   DAHUA_HOST=192.168.1.x        # your NVR's LAN IP
   DAHUA_USERNAME=...
   DAHUA_PASSWORD=...
   ```
   (Or leave these unset and configure Dahua at runtime in Settings → Admin panel; see `docs/dahua.md`.)
2. Start it:
   ```bash
   docker compose -f docker-compose.yml -f docker-compose.local.yml up -d --build
   ```

Things that surprise people:

- **No Tailscale, edge connector or HLS proxy needed on the LAN.** That chain (`DAHUA_MODE=edge`, `docs/edge-connector.md`, `PUBLIC_API_BASE_URL`) exists only because Azure sits outside the home network. On the same LAN as the NVR, `DAHUA_MODE=direct` just works.
- **Appearance matching needs Azure Foundry; detection does not.** Object detection is fully local and offline. Clothing/carried-item captions and automatic appearance matching call Azure AI Foundry and need `FOUNDRY_ENDPOINT` + `FOUNDRY_API_KEY` in `.env`. Without them matching is unavailable (every sighting is a new unknown person). No face recognition or age, gender, ethnicity or identity inference is performed. Trust status is only set by a human.
- **Don't run local and Azure against the NVR at the same time.** The NVR sustains only ~1–2 concurrent CGI sessions; two HomeCam instances polling it cause snapshot `503`s on both. Pause one (e.g. scale the Azure API to zero) while testing locally.

### Detector configuration (get this wrong loudly, not quietly)

`AI_DETECTOR_BACKEND` accepts exactly **`mock`**, **`opencv`**, **`onnx`**, **`rtdetr`** (a few spellings such as `rt-detr` and `rtdetr-r50` are normalised to `rtdetr`). The model path baked into the image is exactly **`/app/models/rtdetr.onnx`** — there is no `rtdetr-r50.onnx`.

Anything else, or a model path that does not exist, no longer silently downgrades the system to the blind mock detector: it logs at ERROR, marks the detector `degraded`, and makes `GET /api/v1/system/status` return **503** (`/ready` stays 200 on purpose so a readiness probe does not evict a working ingestion pipeline). Set `AI_DETECTOR_STRICT=true` to refuse startup instead. A zero-detection watchdog additionally flags a sustained blackout even when the configuration looks fine. See [`docs/ai-pipeline.md`](docs/ai-pipeline.md#detector-configuration-is-fail-loud).

## Local development

All `/api/v1` endpoints other than login and registration require a bearer token or session cookie; `/health` and `/ready` are public. Production registration is disabled unless an operator configures `AUTH_BOOTSTRAP_SECRET` for one-time account enrollment; the existing provisioned account remains usable and requires no rollout-time setup. See [`docs/security.md`](docs/security.md) for the access-control details.

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

## Building the web image for Azure

The web image **must** be built with the API base URL as a build argument:

```bash
az acr build --registry <acr> --image web:<tag> \
  --build-arg NEXT_PUBLIC_API_URL=https://<api-fqdn> apps/web
```

Next.js inlines `NEXT_PUBLIC_*` variables into the browser JavaScript at **build time**. Setting `NEXT_PUBLIC_API_URL` only as a Container Apps runtime env var (as `infra/modules/web.bicep` does) has no effect on the compiled client bundle. Without the build arg, the bundle falls back to `http://localhost:8000` and every visitor sees "HomeCam could not reach the local API" even though the API is healthy. `apps/web/Dockerfile` now fails the build if the arg is missing. Use `apps/web` as the build context, not the repo root.

To verify a deployment, check the served bundles. None should contain `localhost:8000`, and at least one should contain the API hostname:

```bash
base=https://<web-fqdn>
for js in $(curl -s "$base/" | grep -o '/_next/static/[^"]*\.js' | sort -u); do
  echo "$js: $(curl -s "$base$js" | grep -o -e 'localhost:8000' -e '<api-fqdn>' | sort -u | tr '\n' ' ')"
done
```

## What is included

- FastAPI `/api/v1` routes with OpenAPI, typed settings, structured basic logging, health/readiness, and CORS.
- SQLAlchemy models, PostgreSQL/pgvector-compatible Compose database, and Alembic migrations (initial schema + auth/status). SQLite is the default for lightweight local tests; run migrations with `alembic upgrade head` from `apps/api`.
- Cameras and events are persisted to the database (not just held in memory): the mock providers are the source of camera/event *facts*, but every camera sync and every created event is written through the SQLAlchemy models, and `GET /cameras` / `GET /events` read back from the database. An in-memory pub/sub bus (`app/services/events.py`) is layered on top purely for real-time SSE fan-out.
- HomeCam-native authentication (SPEC section 27): `POST /api/v1/auth/register`, `POST /api/v1/auth/login`, `POST /api/v1/auth/logout`, `GET /api/v1/auth/me`. Passwords are hashed with PBKDF2-HMAC-SHA256 (260k iterations, stdlib `hashlib` only — chosen over bcrypt/argon2-cffi specifically because those need native wheels that are not reliably available on Windows ARM64). Sessions are opaque tokens stored **hashed** in the `auth_sessions` table with an expiry (`SESSION_TTL_MINUTES`, default 720) and are accepted either as a `Authorization: Bearer <token>` header or an httponly `homecam_session` cookie set at login.
- Redis and a worker service seam; the worker is intentionally idle until background jobs are added.
- Deterministic mock providers with five cameras (including mock Eufy T8210 doorbell), snapshots, HLS-shaped live URLs, and controllable mock events. Capabilities are returned as a **status map** (`{"snapshot": "SUPPORTED", "vehicleEvents": "UNSUPPORTED", ...}` with values `SUPPORTED` / `UNSUPPORTED` / `UNAVAILABLE` / `UNKNOWN`) per SPEC section 5, not a flat feature list.
- Provider-isolated Dahua and Eufy adapters live under `apps/api/app/providers/dahua` and `apps/api/app/providers/eufy`. They are opt-in via environment variables, keep credentials server-side, and degrade cleanly when host/adapter settings are absent. See `docs/dahua.md` and `docs/eufy.md`.
- **AI event-analysis pipeline** applied identically to every provider (SPEC sections 12–15, 18/19, 31/32): local object detection (person/car/truck/bicycle/motorcycle/dog/cat/package), user-defined camera zones, dwell-based parked-vs-passing vehicle logic, mailbox/driveway/animal semantics, a sharp cropped "best photo" plus a full-frame image per detected event, grounded AI analysis + embeddings persisted to `ai_analyses`, and cross-camera activity correlation exposed at `GET /api/v1/activities`. Everything runs with **zero extra dependencies** by default (`AI_DETECTOR_BACKEND=mock`, `MockAIProvider`); the ONNX detector and audio VAD are strictly opt-in and fall back with a log line instead of failing a request. Zones are managed from the Admin panel or `/api/v1/admin/cameras/{id}/zones`. New capability key `audioDetection` joins the existing status map (`UNAVAILABLE` for Dahua/Eufy today — no provider exposes an audio buffer yet). See `docs/ai-pipeline.md`.

Event photo settings: `BEST_PHOTO_MIN_CROP_PIXELS=720`, `BEST_PHOTO_JPEG_QUALITY=94`, `BEST_PHOTO_SNAPSHOT_TIMEOUT_SECONDS=4` (API), and `DAHUA_EVIDENCE_SNAPSHOT_TIMEOUT_SECONDS=3` (edge connector). The Dahua main-stream retry happens only on detection; routine polling still uses its normal frame source.
- Simulation controls for local development/testing: `POST /api/v1/mock/cameras/{id}/status` (`online`/`offline`/`degraded`/`unknown`), `POST /api/v1/mock/cameras/{id}/battery` (drains battery and automatically raises a high-priority `battery_low` event once below `LOW_BATTERY_THRESHOLD`, default 20%), `POST /api/v1/mock/providers/{id}/outage` (simulates a whole provider — e.g. the Eufy HomeBase — becoming unreachable while the other provider keeps working, proving provider-failure isolation). `GET /api/v1/providers` reports each provider's aggregated health (`ONLINE`/`DEGRADED`/`OFFLINE`) based on how many of its cameras are online.
- Offline/degraded cameras return `503` from `/snapshot` and `/live` instead of crashing or silently returning stale/fake data; unknown camera/provider ids return `404`; invalid payloads return `422`.
- SSE real-time stream at `/api/v1/ws` (named `event.created` events).
- Responsive dashboard with overview, live/events/system/settings navigation scaffolding.
- **Security system layer** (arming modes, incidents, camera health, audit trail): a household arming mode (`disarmed`/`home`/`away`/`night`, `GET`/`PUT /api/v1/security/mode`) gates whether a person/vehicle detection is alert-worthy, with per-zone-kind nuance (a driveway/parking/street zone stays alert-worthy even while `home`). Qualifying detections are grouped into deduplicated `Incident` records (not one alert per detection) with deterministic severity, acknowledge/resolve actions, wall-clock escalation for stale unacknowledged incidents, a JSON evidence export (`GET /api/v1/security/incidents/{id}/export`, no credentials/secrets ever included), and an optional, clearly-labeled AI risk summary that never affects whether/how an incident is raised. A deterministic (non-ML) camera-health watchdog (`app/ai/camera_health.py`) independently raises/clears `camera_offline`/`camera_obstruction`/`camera_frozen` incidents regardless of arming mode. Every mode change, acknowledge/resolve, and evidence export is written to an append-only audit trail (`GET /api/v1/security/audit-log`). See `docs/security.md` for the full design, the arming-mode × zone-kind matrix, and the RAI/deterministic-vs-AI boundary; the dashboard's new "Security" tab (`apps/web/src/app/SecurityPanel.tsx`) exposes all of it.
- **Automatic arming schedules**: recurring windows (e.g. night 23:00–07:00 daily, away weekdays 09:00–17:00) stored per household and resolved in a configured timezone (`ARMING_SCHEDULE_TIMEZONE`, default `Europe/Amsterdam`), applied by a multi-replica-safe scheduler that claims each transition with a single conditional `UPDATE` (the same DB-atomic pattern as the digest scheduler and ingestion leases). A manual mode change overrides the schedule until its next transition — no separate expiry timer — and every automatic change is audit-logged. CRUD lives at `/api/v1/security/schedules` with UI in the Security tab, and an optional token-protected `POST /api/v1/security/mode/integration` lets Home Assistant presence automations set the mode (fails closed when `SECURITY_INTEGRATION_TOKEN` is unset, and is audited). See `docs/security.md`.
- **Enforced data retention**: a configurable per-category policy (events, media blobs, AI analyses, embeddings, resolved incidents, with the audit trail kept longer at 365 days) applied by a batched purge job that commits incrementally and bounds work per run. Evidence attached to an open/unresolved incident is never deleted, nor is anything marked keep-forever via `PUT /api/v1/events/{id}/retention-hold`. `GET /api/v1/settings` now reports `retention_days` from configuration instead of a hardcoded `30`, and admins get dry-run counts at `GET /api/v1/admin/retention`. Enforcement is off by default and dry-run-first. See `docs/retention.md`.
- **Modern AI security features** (natural-language search, loitering, package theft, unusual activity, daily digest, smart priority, confirmation-gated deterrence): `GET /api/v1/search?q=` answers plain-language questions about past events by blending an embedding similarity score (reusing the per-event embeddings the AI pipeline already stores) with a keyword match, plus camera/time filters; identity questions ("who was that?") are refused with a plain-language message rather than answered. A person who stays in a zone past its configurable `dwell_seconds` is tagged `loitering`; a package removed while armed `away`/`night` escalates to a `package_theft` incident with before/after evidence; a per-camera hour-of-week baseline tags events in historically quiet slots as `unusual_activity`. `GET /api/v1/digest?date=` returns a day-in-review summary (AI-written where available, deterministic template otherwise — never blocked on a model). Every event carries a computed `notification_priority` (`critical`/`high`/`normal`/`low`) with `priority_reasons`, used to keep low-value noise out of the incident feed. Deterrence (siren/light/voice) exists only as a request that an authenticated human must explicitly confirm before anything runs — there is no autonomous path, and no emergency-dispatch action exists at all. Presence tracking, package-removal dedup (`scene_dedup_claims`) and digest generation use database-level atomicity so the API can safely run multiple replicas. Search, digest, package before/after evidence images (`GET /api/v1/events/{id}/evidence/{label}`, stored in the DB) and deterrence all require authentication, and their UI lives in the signed-in Security tab. See **`docs/ai-features.md`**, which also documents the hard responsible-AI limits (no face recognition or identity claims, no gender/ethnicity/age inference, human-set trust only, no autonomous deterrence or emergency dispatch, no licence-plate recognition) that are enforced in code and asserted by tests.
- Backend tests cover provider discovery/capabilities, event persistence, camera offline/degraded state, doorbell/battery-low high-priority events, provider failure isolation, real-time SSE delivery, auth register/login/logout/me, API validation (404/422), mocked Dahua/Eufy adapter contracts, the AI pipeline (detector determinism, zone-overlap math, dwell/parked-car logic, mailbox/animal derivation, best-photo scoring, AI-analysis persistence and linkage, activity correlation, audio VAD, `audioDetection` advertisement, and zone admin CRUD/auth), and the security layer (arming-mode × zone-kind incident routing, dedup/merge window, escalation, evidence export, camera-health raise/resolve, audit trail, and auth hardening). Frontend tests (Vitest + Testing Library) cover rendering, camera list display, the events tab, the admin zone editor, and the Security tab (sign-in gating, mode switching, incident acknowledge, audit-log content-safety). GitHub Actions CI runs backend lint/tests, frontend lint/typecheck/tests/build, and a Docker Compose config + build validation job.

Run the smoke test against a running API with `python scripts/smoke.py`.

### Mailbox detection settings

A `mailbox` zone reports `mailbox_delivery`, `mailbox_retrieval`, `mailbox_opened` (lid change, even with nobody in view) and low-priority `mailbox_visit` events. The main settings are below; the full list is in `docs/ai-pipeline.md`.

| Environment variable | Default | Meaning |
| --- | --- | --- |
| `MAILBOX_MIN_OBSERVATIONS` | `1` | Near frames for a visit with no lid/package change (was `2`). |
| `MAILBOX_MIN_ZONE_OVERLAP` | `0.2` | Person cover of the expanded zone that counts as "near" (was `0.3`). |
| `MAILBOX_PROXIMITY_MARGIN` | `0.1` | Zone expansion (normalised) so reaching in from the side counts. |
| `MAILBOX_OPEN_DETECTION_ENABLED` | `true` | Detect the mailbox opening from a reference crop. |
| `MAILBOX_OPEN_THRESHOLD` | `0.4` | Normalised crop difference that means open; tune from `diff=` in the logs. |
| `MAILBOX_OPEN_MIN_FRAMES` | `2` | Frames an opening must persist when nobody is near. |
| `MAILBOX_OPEN_COOLDOWN_SECONDS` | `300` | One opened/visit event per zone per window. |
| `MAILBOX_DEDUPE_SECONDS` | `900` | One delivery/retrieval per zone per window (also the cross-replica claim). |
| `MAILBOX_BOOST_SECONDS` / `MAILBOX_BOOST_INTERVAL_SECONDS` | `60` / `1.0` | Faster stream sampling while someone is at the mailbox (stream cameras only). |

With several API replicas, each camera is ingested by exactly one replica at a time. That replica holds a lease row in `ingestion_leases` (migration `0012`); the others stand by and take over once the lease is stale. This keeps replicas from overwriting each other's vehicle, bin and mailbox scene state. Expiry uses the database clock, and every scene write is fenced on the lease epoch, so a replica that lost the lease mid-frame cannot write stale state.

| Environment variable | Default | Meaning |
| --- | --- | --- |
| `INGESTION_LEASE_ENABLED` | `true` | Only the lease holder ingests a camera. |
| `INGESTION_LEASE_TTL_SECONDS` | `30` | Failover time after the holder stops renewing (renewed every TTL/2). A clean shutdown hands over at once. |
| `INGESTION_REPLICA_ID` | `<host>-<pid>-<random>` | Name of this replica in the lease table. |

### Behaviour, vehicle and wildlife recognition

The existing `FOUNDRY_VISION_DEPLOYMENT` must support image inputs and strict JSON-schema chat completions (for example a vision-capable GPT-4o deployment). Without it, vehicle fields remain `unknown` and animal species fall back to detector groups; neither produces invented identifications. No licence plates are read. See [AI features](docs/ai-features.md).

| Environment variable | Recommended production value | Meaning |
| --- | --- | --- |
| `SUSPICIOUS_ENABLED` | `true` | Explainable behaviour assessment; clothing alone never alerts. |
| `SUSPICIOUS_VEHICLE_DWELL_SECONDS` | `45` | Dwell beside a parked vehicle. |
| `SUSPICIOUS_MAILBOX_DWELL_SECONDS` | `60` | Dwell at a mailbox with no delivery/retrieval. |
| `SUSPICIOUS_PROPERTY_DWELL_SECONDS` | `90` | Dwell in a driveway/street-facing zone. |
| `SUSPICIOUS_GAP_SECONDS` / `SUSPICIOUS_DEDUPE_SECONDS` | `20` / `900` | Visit continuity and per-track alert window. |
| `SUSPICIOUS_VISIT_GAP_SECONDS` | `300` | Minimum absence between separately counted return visits. |
| `SUSPICIOUS_ELEVATED_SCORE` / `SUSPICIOUS_INCIDENT_SCORE` | `3` / `5` | Tag versus armed incident threshold. |
| `SUSPICIOUS_CLOTHING_WEIGHT` | `0.5` | Small contributing weight only after a behaviour signal. |
| `SUSPICIOUS_RETURN_VISITS` / `SUSPICIOUS_RETURN_WINDOW_HOURS` | `3` / `24` | Appearance-matched visits for daytime return signal. |
| `SUSPICIOUS_NIGHT_RETURN_VISITS` / `SUSPICIOUS_NIGHT_RETURN_WINDOW_HOURS` | `2` / `2` | Night-time return signal. |
| `HOME_REGION` / `HOME_TIMEZONE` | `Netherlands, Northern Europe` / `Europe/Amsterdam` | Regional species prior and local night hours. |
| `ANIMAL_BIRD_CONFIDENCE_THRESHOLD` | `0.25` | Bird-specific RT-DETR/ONNX threshold; no CPU-expensive tiled pass. |

Generate an event:
```bash
curl -X POST http://localhost:8000/api/v1/mock/events -H "Authorization: Bearer <access-token>" -H "Content-Type: application/json" -d "{\"camera_id\":\"mock-eufy-doorbell\",\"type\":\"doorbell\"}"
```

Register and log in first, then use the returned access token for protected API calls:
```bash
curl -X POST http://localhost:8000/api/v1/auth/register -H "Content-Type: application/json" -d "{\"email\":\"owner@example.com\",\"password\":\"supersecret1\"}"
curl -X POST http://localhost:8000/api/v1/auth/login -H "Content-Type: application/json" -d "{\"email\":\"owner@example.com\",\"password\":\"supersecret1\"}"
```

## Instant incident alerts

Incidents can be pushed out of the dashboard the moment they are raised or
escalate. Everything is opt-in: with no channel configured, nothing is ever
sent. Configure it under **Security → Instant incident alerts**, or with the
`/api/v1/notifications/*` API (authentication required).

| Channel | Transport | Snapshot images |
| --- | --- | --- |
| Web push | VAPID push to subscribed browsers (PWA installable) | Never — text and a deep link only |
| ntfy | `POST` to a topic on `ntfy.sh` or your own server | Only with an access token configured and images enabled |
| Telegram | Bot API `sendMessage` / `sendPhoto` | Only when images are enabled |
| Webhook | `POST` JSON to an HTTPS URL you control, HMAC-signed | Never |

An alert carries the camera name, incident kind and severity, a short
deterministic summary, the local time and a link back to the incident. It
never claims to know *who* someone is, consistent with the RAI rules in
[docs/ai-features.md](docs/ai-features.md).

Delivery is fire-and-forget on a background task with per-request timeouts,
so a slow or broken channel can never delay event ingestion. Each incident is
delivered once per channel (once more per escalation level), rate-limited per
channel per hour, and suppressed during quiet hours unless the severity is at
or above the configured override.

| Environment variable | Default | Meaning |
| --- | --- | --- |
| `NOTIFICATIONS_ENABLED` | `true` | Master switch; channels are still individually opt-in. |
| `WEB_APP_BASE_URL` | empty | Public URL of the web app. Without it an alert carries no link. |
| `VAPID_PUBLIC_KEY` / `VAPID_PRIVATE_KEY` | empty | Web push keys. Web push stays unavailable until both are set. |
| `VAPID_SUBJECT` | `mailto:homecam@localhost` | Contact passed to the push service. |
| `NOTIFICATION_TIMEOUT_SECONDS` | `6` | Per-send HTTP timeout. |

The per-channel hourly cap is not an environment variable: it is stored per
channel (default `20`) and editable from the admin UI.

Generate a VAPID key pair (never commit or log the private key):

```bash
node -e "const{generateKeyPairSync}=require('crypto');const{publicKey,privateKey}=generateKeyPairSync('ec',{namedCurve:'prime256v1'});const pub=publicKey.export({format:'jwk'});const b=s=>Buffer.from(s,'base64url');console.log('VAPID_PUBLIC_KEY',Buffer.concat([Buffer.from([4]),b(pub.x),b(pub.y)]).toString('base64url'));console.log('VAPID_PRIVATE_KEY',privateKey.export({format:'jwk'}).d)"
```

Channel secrets (ntfy token, Telegram bot token, webhook signing secret) are
encrypted at rest with the same mechanism as camera provider secrets, are
never returned by the API (`has_secret: true` is all you get back) and are
never written to logs. Every channel configuration change is audit-logged.

`pywebpush` is an optional dependency: if it is not installed, every other
channel keeps working and the status endpoint reports web push as
unavailable.

To get alerts on a phone: open the web app, install it to the home screen
(it ships a PWA manifest and service worker), then use **Enable alerts in
this browser**. iOS only allows web push for an installed app. ntfy or
Telegram work without installing anything.

## Limitations

The mock snapshot is a deterministic placeholder, not a real image. MediaMTX is present but not connected to camera hardware. Authentication is a minimal local account system (PBKDF2, opaque DB-backed session tokens, failed-login lockout, and bulk session revocation; see `docs/security.md`); the dashboard has a shared sign-in and all household API routes require authentication. Production registration is disabled by default; an operator may temporarily configure `AUTH_BOOTSTRAP_SECRET` for controlled first-account enrollment, while the existing account remains usable without this setting. There is no email verification, password reset, MFA, or external identity provider integration. Dahua and Eufy code is an opt-in integration boundary with mocked contract tests; no live hardware verification has been claimed without a locally configured reachable LAN host/adapter.

The AI pipeline ships with a deterministic mock detector and mock AI provider: the ONNX detector backend is opt-in and has **not** been verified here against a real model or hardware, no model weights are bundled, audio detection finds speech-like activity only (no transcription, no speaker identity) and is `UNAVAILABLE` because no provider currently exposes an audio buffer, there is no person identity/face clustering, and embeddings are stored as JSON arrays rather than a native pgvector column so similarity search is not index-accelerated yet (see `docs/ai-pipeline.md`). The security layer (`docs/security.md`) raises/tracks incidents and escalation levels and can now notify outbound channels (web push, ntfy, Telegram, webhook - all opt-in, see "Instant incident alerts" above), though there is still no SMS or email channel. Retention is now enforced (`docs/retention.md`) but ships disabled and dry-run-first, so an operator must deliberately enable it; it deletes database rows and blobs, not recorded video files, which are not produced by this stack yet. The modern AI features (`docs/ai-features.md`) ship with the same mock-first posture: natural-language search falls back to keyword matching whenever an event was never embedded or the mock AI provider is active and is not index-accelerated (embeddings are JSON arrays, ranked in Python over a bounded candidate window), the daily digest falls back to a deterministic template when the provider is unavailable, and deterrence is a mock no-op provider that by design executes nothing without an explicit authenticated human confirmation. There are no Azure, cloud AI inference, or production deployment integrations. Do not expose this development stack to the internet.

## Repository

`apps/api` contains the FastAPI service and provider abstraction. `apps/web` contains the Next.js UI. `infrastructure` and Compose contain local services. `docs` is reserved for architecture and integration notes. The full source specification is copied to `SPEC.md`.

A previous version of this README noted that `greenlet` (a SQLAlchemy async dependency) could not be built from source on Windows ARM64. As of this pass, `greenlet==3.5.5` ships a prebuilt `win_arm64` wheel, so host-native `pytest`, `uvicorn`, and `alembic upgrade head` all now run directly on Windows ARM64 without Docker. `asyncpg` (the PostgreSQL driver) still has no Windows ARM64 wheel, so real PostgreSQL connectivity should be exercised through Docker Compose (Linux wheels); SQLite (`aiosqlite`) remains the default for host-native tests and development. Docker Compose itself has not been runtime-validated on this development host (no Docker CLI available here); the `docker-validate` CI job runs `docker compose config` and `docker compose build` on every push to catch Compose/Dockerfile regressions.
