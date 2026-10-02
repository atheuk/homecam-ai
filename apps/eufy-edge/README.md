# HomeCam Eufy edge adapter

Bridges HomeCam's Eufy provider to a local [`eufy-security-ws`][ws] bridge,
and hands live video to [go2rtc][go2rtc] so the browser receives playable
HLS instead of raw P2P H.264.

```
Eufy doorbell (T8210)
  └─ P2P ─▶ eufy-security-ws        ← owns the Eufy account session
              └─ WebSocket ─▶ this adapter
                                ├─ REST  ──▶ HomeCam API (over Tailscale)
                                └─ H.264 ──▶ go2rtc ──▶ HLS ──▶ HomeCam API ──▶ browser
```

## Why an adapter instead of talking to Eufy directly

There is no stable public Eufy API. HomeCam therefore never owns Eufy
credentials, 2FA/captcha flows or SDK protocol details — the bridge does,
on your hardware. This adapter only translates the bridge's WebSocket
protocol into the small REST contract documented in `docs/eufy.md`.

**Your Eufy email and password are never sent to Azure, never stored in
this repo, and never reach the frontend.** They exist only in the bridge's
own configuration.

## Requirements

| Component | Where it comes from |
| --- | --- |
| `eufy-security-ws` | Home Assistant add-on, or its Docker image |
| go2rtc >= 1.2.0 | the standalone add-on, or the copy bundled in Frigate (port 1984) |

### Upstream maintenance warning

`bropat/eufy-security-ws` was **archived by its author in September 2026**
and is no longer maintained, as is the `fuatakgun/eufy_security` HA
integration. Existing installs keep working, but no fixes are coming; if
Eufy changes its cloud login again, it will break. The successor is
[`mega-yfue/eufy-sdk`][successor] (Apache-2.0). All Eufy protocol knowledge
in this adapter is confined to `eufy_ws.py` specifically so that
re-pointing it at the successor is a small, contained change.

Also note: **no Eufy doorbell supports local RTSP or ONVIF.** Eufy's own
documentation lists Home Assistant, HomeKit, ONVIF and Blue Iris as "Not
Supported". A P2P bridge is the only option, and P2P live view takes a few
seconds to start and can fail transiently. That is a property of the
hardware, not of this adapter.

## Setup

**On Home Assistant OS, use the add-on instead:** `homecam-eufy-edge/`
(store name "HomeCam Eufy edge adapter"). It bundles its own loopback-only
go2rtc and the token-gated HLS relay; see `homecam-eufy-edge/README.md`.
The steps below are for a plain Docker host.

1. Get `eufy-security-ws` running and authenticated (including 2FA/captcha)
   and note its WebSocket port, normally `3000`.
2. Copy `.env.example` to `.env` and fill it in. Generate the token with
   `openssl rand -hex 32`.
3. `docker compose up -d --build`
4. Check it is alive: `curl http://127.0.0.1:8091/healthz`
5. In HomeCam: **Settings → Eufy adapter**, set the adapter URL to this
   host's Tailscale address (e.g. `http://homeassistant.your-tailnet.ts.net:8091`)
   and paste the same token. Use **Test Connection**.

### If go2rtc runs inside Frigate

Frigate's bundled go2rtc listens on port `1984`. Set `GO2RTC_URL` to it,
and set `SELF_URL` to an address the Frigate container can reach — with
`network_mode: host` on both, `http://127.0.0.1:8091` is correct.

## Endpoints

Everything except `/healthz` requires `Authorization: Bearer $HOME_CAM_EUFY_TOKEN`.

| Endpoint | Purpose |
| --- | --- |
| `GET /healthz` | Unauthenticated liveness only — reports nothing about Eufy |
| `GET /health` | `{"auth_state": ...}` so HomeCam can surface 2FA/captcha needs |
| `GET /devices` | Normalised device list |
| `GET /devices/{id}/snapshot` | Latest event image as JPEG |
| `GET /devices/{id}/live` | `{"hls_url": ...}` pointing at go2rtc, or at the HLS relay below when `HLS_PUBLIC_BASE_URL` is set |
| `GET /hls/{hls_token}/eufy-{id}/stream.m3u8` and `.../hls/{playlist.m3u8,segment.ts,segment.m4s,init.mp4}` | Read-only HLS relay to a loopback go2rtc; gated by a random path token (`HLS_TOKEN`), because HLS players cannot send bearer headers |
| `GET /internal/devices/{id}/h264` | Raw H.264 for go2rtc's ffmpeg source (separate token; loopback-only when `INTERNAL_LOOPBACK_ONLY=1`) |

Request tokens (`/hls/<token>/`, `?token=`) are redacted from uvicorn and
httpx logs.

## Battery behaviour

The T8210 is battery powered, so the adapter is deliberately conservative:

- **Snapshots never wake the device.** They return the bridge's most recent
  event image — the same picture the Eufy app's notification shows. A
  snapshot poll that woke the doorbell over P2P would flatten the battery.
- **Livestreams stop as soon as the last viewer disconnects**, rather than
  running until a timeout. A stream started by `/live` that go2rtc never
  connects to is stopped after `LIVE_IDLE_STOP_SECONDS` (default 60).

If no event image exists yet, `/snapshot` returns 404 rather than inventing
one; ring or trigger the doorbell once and it will populate.

## How the bridge protocol is handled

- eufy-security-ws (schema ≥ 13) lists devices as bare serial numbers, so
  the adapter fetches `device.get_properties` for each one. Without this
  the doorbell showed up as a nameless "camera" with no snapshot or
  doorbell-event support.
- Devices the bridge loads after the adapter connected (for example while
  it was still logging in or waiting on 2FA) arrive as `device added`
  events and are picked up without restarting the adapter.
- go2rtc's ffmpeg always joins after the P2P stream started. The adapter
  caches the current H.264 GOP (from the last IDR, max 4 MB / 300 chunks)
  plus the latest SPS/PPS, and replays `SPS, PPS, GOP` to new subscribers
  so ffmpeg can decode immediately even when the camera sends parameter
  sets only once. After an overflow the cache restarts at the next IDR.
- The go2rtc stream is registered with `PATCH /api/streams` on every
  `/live` call and confirmed with `GET /api/streams?src=<name>`. This is
  memory-only (the stream token is never written to `go2rtc.yaml`), works
  with a read-only go2rtc config, and recovers automatically after go2rtc
  restarts. **go2rtc >= 1.2.0 is required** (PATCH was added in v1.2.0;
  verified against upstream source through v1.9.14). `PUT` is never used,
  because it persists the token-bearing source to `go2rtc.yaml`; older
  go2rtc versions get an explicit 502 instead.

## Troubleshooting

- `/devices` empty or `/health` not `authenticated`: the bridge has not
  finished logging in. Check the eufy-security-ws logs for a pending 2FA
  code or captcha and complete it there.
- Doorbell listed but live view never starts: the HomeBase/doorbell must
  be reachable from this host over the LAN for P2P, and go2rtc must be
  able to reach `SELF_URL`.
- Snapshot 404: ring the doorbell once so the bridge has an event image.

## Tests

```bash
pip install -r requirements.txt pytest
python -m pytest tests -q
```

The tests use a fake bridge. They never require a Eufy account, a
HomeBase, a running bridge or go2rtc.

[ws]: https://github.com/bropat/eufy-security-ws
[go2rtc]: https://github.com/AlexxIT/go2rtc
[successor]: https://github.com/mega-yfue/eufy-sdk
