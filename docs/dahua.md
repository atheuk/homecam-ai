# Dahua provider

Target hardware: Dahua DHI-NVR4204-P-4KS2, serial `5J006FCPAZ6B52A`.

## Runtime admin configuration (preferred)

The recommended way to configure Dahua is at runtime, through the authenticated
admin API/UI, without restarting the process:

- UI: sign in and open the **Settings** tab, which hosts the Admin panel. Fill
  in scheme/host/port/username/password and the channel list, then click
  **Save**. Use **Test Connection** to run a real (or currently stored)
  auth/health check before or after saving; the result is shown inline and is
  also persisted as the config's last test status/message.
- API: `POST /api/v1/admin/providers/dahua` to create a config, `PUT
  /api/v1/admin/providers/dahua/{id}` to update one, `POST
  /api/v1/admin/providers/{id}/enabled` to enable/disable, `DELETE
  /api/v1/admin/providers/{id}` to remove it, and `POST
  /api/v1/admin/providers/dahua/test` to test either a config already saved
  (`config_id`) or ad-hoc settings before saving. All of these require an
  authenticated session (see "Auth scope" below). `GET
  /api/v1/admin/providers` lists every configured provider with secrets
  redacted (`has_secret` is a boolean flag, never the value).
- Update semantics: `password` is write-only. Omitting it on an update keeps
  the previously stored password; it is never returned by any GET/list
  response.
- Precedence: at most one Dahua config can be enabled at a time (enabling a
  new one disables any other enabled Dahua config, keeping the `dahua-*`
  camera IDs stable). An enabled DB config always takes precedence over the
  `DAHUA_*` environment variables below. If no Dahua config is enabled in the
  database, the environment variables act as an optional local seed/default.
- Auth scope: in this phase, any authenticated HomeCam user is treated as
  admin (HomeCam is a single-user local system today). This is a deliberate,
  documented scope limitation, not silent privilege expansion — a real
  role/permission check should be added before HomeCam supports multiple
  accounts.
- Secrets at rest: the stored password is encrypted (not stored in
  plaintext) using a stdlib-only "encrypt-then-MAC" scheme in
  `apps/api/app/crypto.py` (SHA-256-derived keystream + HMAC-SHA256 tag),
  keyed from the app's `SECRET_KEY`. This was chosen because the `cryptography`
  package (which would normally provide Fernet/AES) cannot currently be built
  on this project's ARM64 Windows development host. **Tradeoff**: this is not
  as thoroughly audited as a maintained AEAD library, and rotating
  `SECRET_KEY` invalidates every stored secret (they must be re-entered). Swap
  in `cryptography`'s `Fernet` (or another audited AEAD) once it can be
  installed, without changing any caller of `encrypt_secret`/`decrypt_secret`.

## Optional env-var seed (legacy / local default)

Env vars are no longer required and are now only an optional seed used when
no Dahua config is enabled in the database. Enable them only on a trusted LAN
and keep all credentials in the local `.env` file:

```dotenv
DAHUA_ENABLED=true
DAHUA_SCHEME=http
DAHUA_HOST=192.168.x.x
DAHUA_PORT=80
DAHUA_USERNAME=<local Dahua user>
DAHUA_PASSWORD=<local Dahua password>
DAHUA_SERIAL=5J006FCPAZ6B52A
DAHUA_CHANNELS=1:Front Door,2:Driveway,3:Backyard,4:Garden
DAHUA_TIMEOUT_SECONDS=5
DAHUA_RETRIES=1
```

The device label QR code may contain setup credentials, but those values are secrets. They must not be committed, logged, placed in URLs, sent to the frontend, or written into documentation. Put QR-derived credentials only in the local `.env` file. The label and QR code do not provide a LAN host/IP, so `DAHUA_HOST` still must be discovered by the owner through the router/DHCP table or an existing Dahua configuration surface; HomeCam does not brute force or perform unsafe network scanning.

## Edge mode (Home Assistant / Raspberry Pi bridge)

If your Dahua NVR is not directly reachable from Azure (the normal case for a
home LAN with no port forwarding), configure Dahua in **edge mode** instead of
direct mode. In edge mode, Azure never talks to the Dahua NVR itself; it talks
to a small "edge connector" HTTP service (see `apps/edge/`) that you run on
your Home Assistant / Raspberry Pi host (or an adjacent Docker/Compose host)
on the same LAN as the NVR, reached over a private overlay network (Tailscale
recommended). See `docs/edge-connector.md` for the full Pi-side setup.

- UI: in the Dahua form's **Connection mode** selector, choose "Home
  Assistant / Raspberry Pi edge connector" and fill in the edge connector's
  base URL (its Tailscale address, e.g. `https://my-pi.tailnet.ts.net:8443`)
  and its token (`HOME_CAM_EDGE_TOKEN`, unrelated to the Dahua NVR password).
- Data model: a Dahua provider config now has a `mode` of `"direct"` (default,
  unchanged legacy behavior) or `"edge"`. In `"edge"` mode, `adapter_url` holds
  the edge connector base URL and `secret_encrypted` holds the encrypted edge
  token — the Dahua NVR's own username/password are never sent to or stored
  by Azure at all in this mode; they live only in the edge connector's own
  `.env` file on your Pi/LAN host.
- HTTP contract: `apps/api/app/providers/dahua/edge_provider.py` calls
  `GET {base_url}/health`, `GET {base_url}/channels`,
  `GET {base_url}/channels/{n}/snapshot`, and
  `GET {base_url}/channels/{n}/live` with `Authorization: Bearer <token>`.
  `/channels/{n}/live` returns a browser-safe `{"kind": "hls", "url": ...}`
  payload (served by a MediaMTX relay on the Pi) — it never returns a raw
  `rtsp://` URL or Dahua credentials to Azure.
- Optional env-var seed: `DAHUA_MODE=edge`, `DAHUA_EDGE_URL`,
  `DAHUA_EDGE_TOKEN`, `DAHUA_EDGE_TIMEOUT_SECONDS`, `DAHUA_EDGE_RETRIES` mirror
  the DB-config edge fields for local/dev use, same precedence rules as direct
  mode (an enabled DB config always wins).
- Validation: **Test Connection** in edge mode calls the edge connector's
  `/health` endpoint and reports whether the edge connector itself is
  reachable *and* whether it can, in turn, reach the Dahua NVR on its LAN —
  the message text distinguishes "edge connector unreachable" (Tailscale/Pi
  problem) from "edge connector reachable but Dahua NVR is not" (Pi-to-NVR LAN
  problem), and from the pre-existing QR/DMSS guidance (the QR code only
  contains a device serial/DT pair, not host/credentials, and DMSS's P2P
  relay is not something HomeCam can reuse without Dahua's partner SDK).

## Implemented provider behavior

All Dahua-specific code is isolated under `apps/api/app/providers/dahua`.

The adapter uses verified Dahua integration surfaces only:

- HTTP Digest authentication for Dahua HTTP/CGI calls.
- `/cgi-bin/magicBox.cgi?action=getSerialNo` for connectivity and serial validation.
- `/cgi-bin/snapshot.cgi?channel=<n>` for channel snapshots.
- RTSP descriptor shape `rtsp://<host>:554/cam/realmonitor?channel=<n>&subtype=0` for live-stream handoff. Credentials are intentionally not embedded in this URL.
- `/cgi-bin/eventManager.cgi?action=attach&codes=[VideoMotion]&channel=<n>` as the event subscription/polling seam.
- `/cgi-bin/mediaFileFind.cgi?action=findFile...` as the recording-search seam.
- `/cgi-bin/storage.cgi?action=getDeviceAllInfo` as the storage-health seam.

The core API continues to expose normalized HomeCam capabilities and health. If required settings are missing or the NVR is unreachable, provider health becomes `DEGRADED` or `OFFLINE` without affecting mock or other providers.

## Tests

CI uses mocked HTTP transports only and does not require Dahua hardware. Real-hardware validation should be opt-in and run only with local environment variables set; do not paste credentials into test commands or logs. `apps/api/tests/test_admin_provider_configs.py` covers the admin CRUD/test-connection endpoints and provider-registry wiring using mocked/unreachable addresses only (no real device contact).

Suggested local checks after configuring `.env`:

```powershell
Set-Location apps\api
pytest tests\test_dahua_provider.py
uvicorn app.main:app --reload
```

Then call `/api/v1/providers`, `/api/v1/cameras`, `/api/v1/cameras/dahua-channel-1/capabilities`, `/api/v1/cameras/dahua-channel-1/snapshot`, and `/api/v1/cameras/dahua-channel-1/live`.

## Unverified or blocked capabilities

- Live hardware verification has not been performed in this branch because no Dahua LAN host/IP was provided.
- Channel naming and enabled-channel state are configured explicitly via `DAHUA_CHANNELS`; automatic NVR channel inventory can be added after verification against the target NVR.
- Smart event taxonomy, person/vehicle detection, PTZ support, clip export, and playback URL formats are not marked `SUPPORTED` without hardware/API verification.
- ONVIF discovery is not enabled yet; HomeCam avoids unsafe LAN scanning and does not guess addresses.
- Storage health and recording search seams are present, but exact parsed response models should be finalized only after observing target-device responses.
