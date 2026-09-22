# HomeCam edge connector

Runs on a Raspberry Pi (or other host on the same LAN as your Dahua NVR) —
typically the same box already running Home Assistant, or an adjacent
Docker/Compose host. It lets Azure/HomeCam AI reach your Dahua NVR over a
private overlay network (Tailscale recommended) **without** forwarding any
Dahua ports on your home router, and without Azure ever holding your Dahua
NVR username/password or a raw credentialed RTSP URL.

See `../../docs/edge-connector.md` for the full walkthrough (Tailscale
install, wiring the Azure Admin UI, troubleshooting). Quick start:

```bash
cd apps/edge
cp .env.example .env
# edit .env: DAHUA_HOST/DAHUA_USERNAME/DAHUA_PASSWORD/DAHUA_CHANNELS,
# and generate a HOME_CAM_EDGE_TOKEN (openssl rand -hex 32)
docker compose up -d --build
```

Then, on the Pi:

```bash
curl http://127.0.0.1:8443/healthz
curl -H "Authorization: Bearer $HOME_CAM_EDGE_TOKEN" http://127.0.0.1:8443/health
```

Install Tailscale on this host, note its Tailscale IP or MagicDNS name,
and in the Azure Admin UI's Dahua "Home Assistant / Raspberry Pi edge
connector" mode enter:

- Edge connector base URL: `https://<this-host>.<tailnet>.ts.net:8443`
  (or `http://<tailscale-ip>:8443` if you are not terminating TLS here)
- Edge connector token: the `HOME_CAM_EDGE_TOKEN` value

## Contract

Implemented in `app.py`; also documented in
`apps/api/app/providers/dahua/edge_provider.py` (the Azure-side client).
Every endpoint except `/healthz` requires `Authorization: Bearer <token>`.

| Method/path | Purpose |
|---|---|
| `GET /healthz` | Unauthenticated process liveness only. |
| `GET /health` | `{"dahua_reachable": bool, "message": str}` |
| `GET /channels` | `{"channels": [{"channel","name","type","online"}]}` |
| `GET /channels/{n}/snapshot` | `image/jpeg` bytes |
| `GET /channels/{n}/live` | `{"kind": "hls", "url": str}` (MediaMTX HLS URL; never raw RTSP) |

## Why MediaMTX?

`app.py` never proxies live video itself. `docker-compose.yml` also runs
[MediaMTX](https://github.com/bluenviron/mediamtx), which pulls the
credentialed Dahua RTSP stream on this LAN (see `mediamtx.yml`) and
re-serves it as HLS/WebRTC. The edge connector's `/channels/{n}/live`
response simply points at MediaMTX's own output URL — Dahua credentials
never leave this host, in either direction.
