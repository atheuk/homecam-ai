# Security modes, incidents, camera health, and audit trail

This is the "complete security system" layer added on top of the existing
per-camera event/AI pipeline (`docs/ai-pipeline.md`). It answers four
questions the AI pipeline alone does not: *is the household armed right
now*, *does this event deserve a human's attention as one incident, not N
duplicate notifications*, *can each camera currently be trusted*, and *who
did what, when*.

Security features are additive, but access to household API data is not
public: every `/api/v1` route requires authentication except login and
initial account registration. The root health and readiness probes remain
public.

## Design boundary: deterministic safety vs. AI assistance

This feature set deliberately draws a hard line and documents it in both
code comments and the UI:

- **Arming mode, camera health, escalation timing, and incident
  status/severity are 100% deterministic.** They are plain rules over
  data already in Postgres (mode + zone kind + event type; pixel-level
  frame diffing for tamper/obstruction; a wall-clock timer for
  escalation). None of it calls an AI/ML model, and none of it can be
  changed by a probabilistic model output. See `app/ai/camera_health.py`'s
  module docstring and `app/services/incidents.py`'s routing logic.
- **`ai_summary` on an incident is optional, additive, human-readable
  context only** (reuses the existing grounded AI-analysis pipeline over
  the incident's already-detected events). It is never used to decide
  whether an incident is raised, its severity, or whether it escalates,
  and the frontend always renders it in a visually distinct "AI summary"
  badge so a household member can never mistake an AI paraphrase for a
  deterministic system fact.
- **No autonomous emergency-service dispatch anywhere.** Escalation raises
  `escalation_level` and is visible in the incident list/audit trail; it
  never places a call, sends an SMS, or contacts a third party. Wiring an
  actual notification channel (push/SMS/email) is an explicit, separate,
  not-yet-implemented gap (see "Remaining gaps" in the PR description).
- **No face recognition, no gender/ethnicity inference, no new identity
  claims.** This feature reuses the existing `Person`/trust model exactly
  as-is (human-set trust only, per `apps/api/app/ai/appearance.py`); it
  adds no new person-identification capability. Zone `kind` (e.g.
  `driveway` vs `entry`) is a property of a *camera zone an admin drew*,
  not of a detected person.

## API access and account bootstrap

Every `/api/v1` route requires a valid bearer token or the `homecam_session`
HttpOnly cookie, including camera snapshots/live streams, event and person
data, photos, activity, settings, security, and the SSE event stream. The
only unauthenticated versioned endpoints are `POST /api/v1/auth/login` and
`POST /api/v1/auth/register`; `/health` and `/ready` remain public for
platform probes. Browser fetches use an in-memory bearer token, which the
HLS player sends only to URLs on the configured API origin, never to
provider-supplied cross-origin manifests or segments. The dashboard does
not persist bearer tokens: on reload it restores the login from the
`HttpOnly` session cookie and sends authenticated requests with browser
credentials. The cookie is `Secure; SameSite=None` in production to support
separate web/API origins; unsafe cookie-authenticated requests require the
`X-HomeCam-Request: 1` header and are CORS-restricted to configured origins.

Production registration requires the out-of-band `AUTH_BOOTSTRAP_SECRET`
environment setting and a matching `X-HomeCam-Bootstrap-Secret` request
header. If the setting is absent, registration is disabled. When configured,
the first account is created under a database lock; later attempts return
`403`. No public endpoint reveals whether an account exists. The deployed
environment already has an owner account, so normal release rollout neither
requires nor attempts account creation. Development keeps registration
available without a bootstrap secret. The per-email login-lock map is capped
at 256 cached locks; database lockout updates remain the correctness boundary
across replicas.

## Arming modes

`security_state` is a single-row table: `mode` ∈ `disarmed | home | away |
night`, plus `changed_by` (user id) and `changed_at`. Every change goes
through `PUT /api/v1/security/mode` (auth required) and is recorded in the
audit trail — there is no code path that changes it automatically.

Per-zone behavior when routing a detection event to an incident
(`app/services/security_modes.py::is_alert_armed`, called from
`app/services/incidents.py::route_event`) reuses the zone `kind` already
introduced by the visual-zones feature (`driveway | parking | mailbox |
bin | entry | street | garden | other`) instead of adding a new field:

| Mode | `driveway` / `parking` / `street` zones | All other zone kinds (and zoneless events) | Camera health |
| --- | --- | --- | --- |
| `disarmed` | ignored (no incident) | ignored (no incident) | always raised |
| `home` | raises an `intrusion` incident | ignored (expected household activity, e.g. someone at the front `entry`) | always raised |
| `away` / `night` | raises an `intrusion` incident | raises an `intrusion` incident | always raised |

This mirrors how real alarm panels treat "home" partial-arming (vehicle/
street-facing zones only) vs. full away/night arming: a car pulling into
the driveway is still worth knowing about while the household is home, but
a person detected at the front door/entry while everyone is home should not
page anyone. It reuses the existing zone editor and kind vocabulary rather
than inventing new UI, so it is safe with zones that predate this change
(an unset/`other` kind is treated the same as any non-vehicle kind).

## Incidents: grouping, dedup, escalation, evidence

An `Incident` groups one or more related events instead of surfacing every
detection as a separate alert:

- **Dedup/grouping**: a new qualifying event is attached to an existing
  *open* incident of the same `(camera_id, kind)` within
  `incident_merge_window_seconds` (default 300s) instead of creating a new
  one, via `_open_incident_for` / `route_event`;
  `event_ids`/`event_count`/`last_seen_at` are updated in place.
  Camera-health incidents additionally dedupe against a recently
  *resolved* incident of the same camera/subtype for
  `camera_health_dedupe_seconds` (default 1800s) so a flapping camera
  doesn't spam a new incident every poll.
- **Severity**: derived deterministically from the underlying event
  type — `person` detections are `high`, `vehicle` detections are
  `medium` (`_SEVERITY_BY_EVENT_TYPE`) — and the higher-severity value
  wins when new events merge into an existing incident. Camera-health
  incidents are always `high` (`_CAMERA_HEALTH_SEVERITY`) since a camera
  that cannot be trusted is itself a security-relevant fact.
- **Escalation**: `escalate_due_incidents` (invoked from the ingestion
  poll loop, `app/services/ingestion.py`) bumps `escalation_level` and
  `last_escalated_at` on any incident that has stayed `open` past
  `incident_escalation_seconds` (default 180s) without acknowledgment, up
  to `incident_max_escalation_level` (default 3) — a wall-clock timer, not
  a model.
- **Acknowledge / Resolve**: `POST /incidents/{id}/acknowledge` and
  `/resolve`, both audit-logged, both require auth, both settable only by
  a human via the API/UI.
- **Timeline**: `GET` the incident, then look at its grouped
  `event_ids`/`event_count`; the frontend's "Show timeline" action re-fetches
  each event's existing detail for a chronological view.
- **Evidence export**: `GET /incidents/{id}/export` returns the incident
  plus every grouped event's existing photo path/metadata as JSON,
  downloadable client-side. It **never** includes camera credentials,
  RTSP/live URLs, or any secret — only data already returned by the
  existing `/events` endpoint. Every export is itself audit-logged
  (`incident.exported`).

## Camera health watchdog (`app/ai/camera_health.py`)

Runs on the same polling cadence as ingestion, independent of arming mode
(a camera that can't be trusted matters whether the house is armed or
not). Three deterministic, non-ML heuristics:

1. **Offline**: driven directly by the provider-reported `online` flag.
2. **Obstruction**: mean pixel brightness/variance of the latest frame
   drops below a threshold and stays there — consistent with a lens being
   covered or spray-painted, not a learned model.
3. **Frozen/tamper (replay)**: successive frames are near-identical for
   longer than a real camera's noise floor would allow.

Each raises/clears a `camera_offline` / `camera_obstruction` /
`camera_frozen` incident via `raise_camera_health` /
`resolve_camera_health`. Under the default `mock` detector backend, mock
cameras emit non-image placeholder bytes, so frame-based checks safely
no-op (consistent with the rest of the AI pipeline's "mock cannot see
pixels" design) — offline detection still works, since it doesn't need
frame decoding.

Watchdog state (how long a camera has looked offline/frozen, whether an
incident is already open) lives in an in-process dict, not the database —
by design, since the heuristics re-accumulate within seconds regardless.
The one exception is handled explicitly: on startup,
`seed_from_open_incidents` reads any still-`open`/`acknowledged`
`camera_*` incident from the database and pre-populates that camera's
in-memory state, so a camera that reconnects *after* an API process
restart is still recognized as an online→offline→online transition and
its stale incident auto-resolves, instead of silently never clearing
because the first post-restart observation looked like a first-ever
reading for that camera.

## Concurrency safety

Incident dedup/grouping and login lockout accounting are both
check-then-act (read a count/existing row, decide, write it back) across
`await` points, which a single Python process's cooperative event loop --
or two replicas of the API sharing one Postgres database, see
`infra/modules/api.bicep`'s `scale.maxReplicas: 2` -- can interleave
between two near-simultaneous requests for the *same* key. Each guard now
has two layers:

1. **In-process `asyncio.Lock`** keyed by the same tuple the dedup/lockout
   logic groups on -- `(camera_id, zone, kind)` for intrusion incidents,
   `(camera_id, kind)` for camera-health incidents, and the normalized
   email for login attempts. The login lock cache is capped at 256 entries
   so distinct unknown login names cannot grow it without limit. This
   serializes same-process concurrent
   requests for the *same* key with no database round-trip, and is a fast
   path only -- it does nothing across replicas.
2. **A database-level guarantee that holds even across replicas:**
   - Incident routing/grouping (`incidents._acquire_route_lock`) takes a
     Postgres transactional advisory lock
     (`pg_advisory_xact_lock(hashtextextended(key, 0))`) keyed on the same
     tuple, held only for the duration of the routing transaction and
     auto-released at commit/rollback. This is a pure mutual-exclusion
     lock, not a uniqueness constraint, so it composes correctly with the
     existing time-window based incident merge/reopen logic (two
     sequential incidents for the same camera/zone/kind, once the merge
     window elapses, are legitimate and must not be blocked). On SQLite
     (local dev/tests) this is a no-op: a single SQLite file already
     serializes all writers globally, so the in-process lock above is
     already sufficient there.
   - The login-lockout counter (`auth_routes._register_failed_attempt`)
     uses a single atomic `UPDATE ... SET failed_attempts =
     failed_attempts + 1, locked_until = CASE ... RETURNING` statement
     instead of a Python read-modify-write of ORM attributes. This is
     atomic per-row on both SQLite and Postgres (the engine evaluates the
     `SET` expression against the current row value as one statement), so
     no dialect branching is needed here, and the guarantee holds
     regardless of process count.
   - The *successful*-login path (`auth_routes._finalize_successful_login`)
     closes a second, subtler race than the counter alone: a login route
     verifies the submitted password against a snapshot of the user row
     read before any lock-state change, with no lock held across that
     verification. Two concurrent requests -- a wrong guess that reaches
     the lockout threshold, and a correct guess -- can both see the
     account as unlocked at the moment each checks it. The reset of
     `failed_attempts`/`locked_until` on a successful login is therefore
     also a single atomic, conditional
     `UPDATE ... WHERE locked_until IS NULL OR locked_until <= now ...
     RETURNING` rather than an unconditional write of the snapshot's
     values: if a sibling failed-attempt UPDATE that sets `locked_until`
     commits first, this statement's WHERE clause is re-evaluated against
     that new value at the database layer and matches no row, so the
     login is rejected as locked instead of completing on stale
     information.

Concurrent requests for *different* keys remain unaffected by either
layer. `apps/api/tests/test_incidents.py` and
`apps/api/tests/test_auth_hardening.py` include regression tests that
exercise the database-layer guarantee directly (bypassing the in-process
lock via separate sessions/dialect stubs), not just the in-process
fast path, including a test that reproduces the correct-guess-vs-lockout
race at `failed_attempts == threshold - 1` and confirms the correct guess
is rejected once the lockout commits first.

## Remaining gaps

- **Lockout responses use distinct status codes (`401` vs `423`).** This
  intentionally tells a caller that an account is locked so a legitimate
  user gets a clear "come back later" message. The trade-off is a minor
  account-enumeration signal; the response does not reveal whether the
  current password attempt would have succeeded.
- **Authentication is still a minimal local account system.** It has no
  MFA, email verification, password recovery, or external identity
  provider. Production bootstrap is an operator-controlled one-time process,
  but these controls remain separate production-hardening work.

## Audit trail (`app/services/audit.py`)

Every security-relevant human action — mode changes, acknowledge, resolve,
evidence export, login/logout, register — is written to `audit_log`
(`actor_user_id`, `actor_label`, `action`, `target_type`, `target_id`,
`details`, `created_at`) and readable at `GET /api/v1/security/audit-log`
(auth required, newest first). `details` is a small JSON object and is
never populated with secrets/passwords/tokens/RTSP URLs — verified by
`test_audit.py` and `SecurityPanel.test.tsx`'s content-safety test.

## Authentication hardening

Building on the existing PBKDF2 + opaque session-token auth (SPEC 27):
account lockout after `auth_max_failed_attempts` consecutive bad passwords
(returns `423 Locked` — including for the *correct* password while locked,
so a lockout never leaks whether the password would otherwise have
succeeded — until `auth_lockout_minutes` elapses or a human intervenes),
a failed-attempt counter that resets on success, and a new
`POST /api/v1/auth/sessions/revoke-all` endpoint to invalidate every
existing session token at once (e.g. after a suspected compromise). Every
lockout is audit-logged (`auth.account_locked`). The existing
`/auth/register`, `/auth/login`, `/auth/logout`, `/auth/me` request/response
shapes are unchanged. See `test_auth_hardening.py`.

## API summary

| Method | Path | Purpose |
| --- | --- | --- |
| GET | `/api/v1/security/mode` | Current arming mode |
| PUT | `/api/v1/security/mode` | Change arming mode (audit-logged) |
| GET | `/api/v1/security/incidents` | List incidents (`status`/`kind` filters) |
| GET | `/api/v1/security/incidents/{id}` | Incident detail |
| POST | `/api/v1/security/incidents/{id}/acknowledge` | Acknowledge (audit-logged) |
| POST | `/api/v1/security/incidents/{id}/resolve` | Resolve (audit-logged) |
| GET | `/api/v1/security/incidents/{id}/export` | Evidence export JSON (audit-logged) |
| GET | `/api/v1/security/audit-log` | Audit trail (`action` filter) |
| POST | `/api/v1/auth/sessions/revoke-all` | Invalidate all of the current user's session tokens (audit-logged) |

## Frontend

`apps/web/src/app/SecurityPanel.tsx` is a new "Security" dashboard tab,
self-contained like the existing Admin panel (its own login gate, bearer
token kept only in component state — never `localStorage`). It shows the
mode switcher (with explicit copy that arming logic never depends on AI
confidence), the incident list with acknowledge/resolve/export/timeline
actions and a visually distinct AI-summary badge, a camera-health banner
derived from open `camera_*` incidents, and the audit trail.

## Tests

Backend: `test_security_modes.py`, `test_incidents.py` (grouping/dedup,
zone-kind × mode routing matrix, escalation, export contents),
`test_camera_health.py` (including restart-seeding regression tests),
`test_audit.py`, `test_auth_hardening.py` (including a concurrent-login
regression test that the lockout threshold cannot be bypassed by
simultaneous requests), `test_realtime.py` (SSE frame payload is never
mutated in place, so concurrent subscribers all see the correct event
name).
Frontend: `SecurityPanel.test.tsx` (sign-in gating, mode display/switch,
incident acknowledge, audit-log content-safety, camera-health banner).
