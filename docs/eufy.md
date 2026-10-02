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

## Reference adapter

`apps/eufy-edge/` is a working implementation of the contract below,
bridging `eufy-security-ws` to HomeCam and handing live video to go2rtc.
See `apps/eufy-edge/README.md` for setup. It runs on the owner's Home
Assistant host, not in Azure. On Home Assistant OS, install it as the
**HomeCam Eufy edge adapter** add-on (`homecam-eufy-edge/`). The add-on:

- connects to the eufy-security-ws add-on at `ws://127.0.0.1:3000`, which
  keeps the Eufy login and its persistent session;
- runs its own loopback-only go2rtc 1.9.14 and leaves Frigate unchanged;
- exposes only port 8091, where every route needs the bearer token or a
  per-start random HLS path token.

HomeCam's adapter URL is `http://<ha-host>.<tailnet>.ts.net:8091`.

Upstream status (verified September 2026): `bropat/eufy-security-ws` and
`bropat/eufy-security-client` were **archived by their author in September
2026**, and `fuatakgun/eufy_security` is no longer updated. They still
work, but are frozen. The maintained successor is `mega-yfue/eufy-sdk`
(Apache-2.0). All protocol knowledge lives in `apps/eufy-edge/eufy_ws.py`
so that migration stays contained.

Hardware reality check: **no Eufy doorbell exposes local RTSP or ONVIF** —
Eufy's own documentation lists Home Assistant, HomeKit, ONVIF and Blue Iris
as "Not Supported" for these devices. A P2P bridge is the only route, live
view takes a few seconds to start, and it can fail transiently.

## HomeCam adapter contract

All Eufy-specific HomeCam code is isolated under `apps/api/app/providers/eufy`.

### Runtime admin configuration (preferred)

Configure the adapter connection at runtime through the authenticated admin
API/UI instead of only via `.env` at process startup:

- UI: sign in and open the **Settings** tab (Admin panel), fill in the
  adapter base URL and adapter token, then click **Save**. Use **Test
  Connection** to check the adapter's `/health` endpoint before or after
  saving.
- API: `POST /api/v1/admin/providers/eufy`, `PUT
  /api/v1/admin/providers/eufy/{id}`, `POST
  /api/v1/admin/providers/{id}/enabled`, `DELETE
  /api/v1/admin/providers/{id}`, and `POST
  /api/v1/admin/providers/eufy/test`, all behind the existing authenticated
  session. `GET /api/v1/admin/providers` lists configs with the adapter token
  redacted (`has_secret` only, never the value).
- Update semantics: `adapter_token` is write-only; omitting it on an update
  keeps the previously stored token.
- Precedence: at most one Eufy config can be enabled at a time. An enabled DB
  config always takes precedence over the `EUFY_*` environment variables
  below, which become only an optional local seed/default when no Eufy
  config is enabled in the database.
- Auth scope: any authenticated HomeCam user is currently treated as admin
  (single-user local system) — see the same limitation noted in
  `docs/dahua.md`.
- Secrets at rest: the adapter token is encrypted using the same
  stdlib-only encrypt-then-MAC scheme described in `docs/dahua.md`
  (`apps/api/app/crypto.py`), keyed from `SECRET_KEY`. Rotating `SECRET_KEY`
  invalidates stored tokens.

Enable the env-var seed only when a local adapter is running:

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

CI uses mocked adapter responses only and never requires a Eufy account or HomeBase. The mocked contract tests verify capability mapping, health/auth-state reporting, snapshots, and optional live stream descriptors. `apps/api/tests/test_admin_provider_configs.py` additionally covers the admin CRUD/test-connection endpoints and provider-registry wiring using an unreachable adapter URL only (no real adapter/account contact).

## Known limitations

- No direct Eufy cloud endpoints are implemented in HomeCam core.
- No Eufy credential, session token, or adapter bearer token is exposed to the frontend.
- Recording retrieval, talkback, and guard-mode controls remain optional and disabled unless a local adapter exposes verified support.
- Snapshots are the bridge's most recent *event image*, not a fresh
  capture: the T8210 is battery powered, so waking it over P2P for every
  snapshot poll would flatten it. Before the doorbell has produced any
  event, `/snapshot` returns 404 rather than a fabricated image.
- Live view depends on go2rtc being available on the adapter host to remux
  the raw P2P H.264 into browser-playable HLS. The adapter registers the
  go2rtc stream in memory only (`PATCH /api/streams`) on every live
  request (go2rtc >= 1.2.0 required; `PUT` is never used because it
  would persist the token-bearing source), so it survives go2rtc restarts
  and read-only go2rtc configs.
- The edge adapter enriches the bridge's serial-only device list with
  `device.get_properties`, picks up devices added after it connected, and
  replays the latest SPS/PPS plus the current IDR-started GOP to go2rtc. See
  `apps/eufy-edge/README.md` for details and troubleshooting.
- The edge adapter's own tests (`apps/eufy-edge/tests`) run in CI against a
  fake eufy-security-ws bridge and a mocked go2rtc API.
