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
| go2rtc | the standalone add-on, or the copy bundled in Frigate (port 1984) |

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
| `GET /devices/{id}/live` | `{"hls_url": ...}` pointing at go2rtc |
| `GET /internal/devices/{id}/h264` | Raw H.264 for go2rtc's ffmpeg source (separate token) |

## Battery behaviour

The T8210 is battery powered, so the adapter is deliberately conservative:

- **Snapshots never wake the device.** They return the bridge's most recent
  event image — the same picture the Eufy app's notification shows. A
  snapshot poll that woke the doorbell over P2P would flatten the battery.
- **Livestreams stop as soon as the last viewer disconnects**, rather than
  running until a timeout.

If no event image exists yet, `/snapshot` returns 404 rather than inventing
one; ring or trigger the doorbell once and it will populate.

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
