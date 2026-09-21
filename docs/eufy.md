# Eufy provider

Target hardware: Eufy Video Doorbell 2K Battery-Powered, model T8210, connected to Eufy HomeBase 2.

## Security notice

Any Eufy email/password that was pasted into chat should be treated as compromised. Rotate the Eufy password before using this integration. Do not write Eufy account credentials to source code, docs, tests, commits, URLs, or logs. Credentials and persistent sessions must remain local to the edge adapter/bridge process, not HomeCam core or the frontend.

## Integration direction

There is no stable official public Eufy Security API suitable for direct HomeCam core integration. HomeCam therefore implements an edge-adapter boundary instead of embedding unofficial protocol code in the core API.

Recommended current adapter ecosystem: `@mega-yfue/eufy-sdk` in a separate local process that owns:

- login and persistent session storage;
- 2FA, captcha, and re-auth user flows;
- reconnect/backoff behavior;
- device discovery and status normalization;
- event subscription and snapshot/live-media retrieval where available.

The archived/deprecated `bropat/eufy-security-client` project should be treated as historical reference/fallback only, not as the preferred embedded dependency.

## HomeCam adapter contract

All Eufy-specific HomeCam code is isolated under `apps/api/app/providers/eufy`. Enable it only when a local adapter is running:

```dotenv
EUFY_ENABLED=true
EUFY_ADAPTER_URL=http://127.0.0.1:8090
EUFY_ADAPTER_TOKEN=<optional local adapter bearer token>
EUFY_TIMEOUT_SECONDS=10
EUFY_RETRIES=1
```

The current provider expects a small local HTTP contract:

- `GET /health` returns `{ "auth_state": "authenticated" | "ready" | "2fa_required" | "captcha_required" | "reauth_required" | "unauthenticated" | "unknown" }`.
- `GET /devices` returns `{ "devices": [...] }`, where each device can include `id`, `name`, `type`, `model`, `online`, `status`, `battery_level`, and capability booleans.
- `GET /devices/{id}/snapshot` returns image bytes when the adapter has a verified snapshot/event-image source.
- `GET /devices/{id}/live` returns `{ "hls_url": "..." }`, `{ "rtsp_url": "..." }`, or `{ "url": "..." }` only when live video is available.

HomeCam maps only verified/adapter-reported targets: doorbell press, motion, person, battery, snapshots/event images, and optional live video. Recordings, talkback, guard mode, and native RTSP are not mandatory and are reported `UNAVAILABLE` or `UNKNOWN` unless the adapter proves otherwise.

## Tests

CI uses mocked adapter responses only and never requires a Eufy account or HomeBase. The mocked contract tests verify capability mapping, health/auth-state reporting, snapshots, and optional live stream descriptors.

## Known limitations

- No direct Eufy cloud endpoints are implemented in HomeCam core.
- No Eufy credential, session token, or adapter bearer token is exposed to the frontend.
- A real adapter service/container is intentionally not added in this branch because adding an unofficial dependency without live-account validation would risk unsupported behavior. The integration seam is ready for a separate bridge service once the owner can configure and validate it locally.
- Live video, recording retrieval, talkback, and guard-mode controls remain optional and disabled unless a local adapter exposes verified support.
