# Dahua provider

Target hardware: Dahua DHI-NVR4204-P-4KS2, serial `5J006FCPAZ6B52A`.

## Configuration

The provider is disabled by default. Enable it only on a trusted LAN and keep all credentials in the local `.env` file:

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

CI uses mocked HTTP transports only and does not require Dahua hardware. Real-hardware validation should be opt-in and run only with local environment variables set; do not paste credentials into test commands or logs.

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
