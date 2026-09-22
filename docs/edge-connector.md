# HomeCam edge connector (Home Assistant / Raspberry Pi bridge)

This is the deployment guide for **Dahua edge mode**: a small HTTP service
you run on your own network — typically the Raspberry Pi already running
Home Assistant, or an adjacent Docker/Compose host — so Azure/HomeCam AI
can reach your Dahua NVR over a private overlay network (Tailscale
recommended) instead of forwarding any Dahua ports on your home router.

See `docs/dahua.md` for the provider-level "why"/data-model summary; this
document is the practical "how".

## Why edge mode

- Most home routers have no public port forwarding for the Dahua NVR, and
  opening one is a real security risk (raw Dahua HTTP/RTSP with embedded
  credentials, directly on the internet).
- The Dahua device label QR code only encodes a serial number and a `DT`
  device-type code — it does not contain the LAN host, username, or
  password, so it cannot be used on its own to configure either direct or
  edge mode.
- The Dahua/DMSS mobile app instead uses Dahua's own P2P relay service,
  which is not something HomeCam can reuse without a Dahua partner
  SDK/API agreement — it is unrelated to this edge connector.
- Edge mode instead runs a tiny connector *inside* your home network, and
  only that connector's private-overlay address (not the NVR's) is ever
  given to Azure.

## Architecture

```
Azure (HomeCam API) --Tailscale (private)--> apps/edge (this host) --LAN--> Dahua NVR
                                                    |
                                                    v
                                              MediaMTX (this host) --LAN--> Dahua NVR (RTSP)
```

- `apps/edge` (this repo's `apps/edge/`) is a small FastAPI service. It
  holds the real Dahua NVR username/password and talks to the NVR's
  existing CGI endpoints on the LAN (same calls as HomeCam's direct mode).
  Azure authenticates to it with a single bearer token
  (`HOME_CAM_EDGE_TOKEN`) that has nothing to do with the Dahua password.
- It never returns the NVR's raw `rtsp://` URL (or any Dahua credential)
  to Azure. For live video it instead points at
  [MediaMTX](https://github.com/bluenviron/mediamtx), a second container
  in the same compose stack, which pulls the credentialed RTSP feed
  directly from the NVR on its own LAN hop and re-serves it as HLS
  (optionally WebRTC). Only that HLS/WebRTC URL — never RTSP, never
  credentials — is what reaches Azure and, from there, the browser.

## 1. Set up the Pi/HA host

Any Raspberry Pi 3B+ or newer running 64-bit Raspberry Pi OS (or any other
small Linux box on the same LAN as your Dahua NVR) works. Docker/Compose is
the only hard requirement:

```bash
curl -fsSL https://get.docker.com | sh
sudo usermod -aG docker $USER   # log out/in after this
```

If you run Home Assistant OS instead of Raspberry Pi OS, run this compose
stack on a separate lightweight host on the same LAN (e.g. a Docker
add-on host, or any spare Pi/NAS) rather than inside HAOS's own
supervisor, since HAOS does not run arbitrary `docker compose` stacks
directly.

## 2. Install Tailscale on that host

```bash
curl -fsSL https://tailscale.com/install.sh | sh
sudo tailscale up
```

Note the Tailscale IP or MagicDNS hostname assigned to this host (e.g.
`raspberrypi.your-tailnet.ts.net`) — `tailscale status` shows it. This is
the *only* address Azure will ever be given for this host.

## 3. Deploy the edge connector + MediaMTX

```bash
git clone <this repo> homecam-ai   # or copy just apps/edge/ to the Pi
cd homecam-ai/apps/edge
cp .env.example .env
# edit .env:
#   DAHUA_HOST/DAHUA_USERNAME/DAHUA_PASSWORD/DAHUA_CHANNELS -> your NVR
#   HOME_CAM_EDGE_TOKEN                                     -> openssl rand -hex 32
#   STREAM_BASE_URL                                         -> http://<tailscale-addr-of-this-host>:8888
# edit mediamtx.yml: add one `dahua-<channel>` path per DAHUA_CHANNELS entry
docker compose up -d --build
```

Verify locally on the Pi:

```bash
curl http://127.0.0.1:8443/healthz
curl -H "Authorization: Bearer $HOME_CAM_EDGE_TOKEN" http://127.0.0.1:8443/health
```

`/health` should report `"dahua_reachable": true`. If it reports `false`,
the edge connector is up but cannot reach the Dahua NVR — check
`DAHUA_HOST`/credentials and that this host is actually on the same LAN
as the NVR.

## 4. Point Azure at it (no redeploy needed)

In the HomeCam Admin UI (Settings tab), create/edit the Dahua provider
config and set **Connection mode** to "Home Assistant / Raspberry Pi edge
connector", then fill in:

- **Edge connector base URL**: `https://<tailscale-addr>:8443` (or
  `http://<tailscale-addr>:8443` if you are not terminating TLS on this
  host — Tailscale's own encryption still applies at the network layer)
- **Edge connector token**: the `HOME_CAM_EDGE_TOKEN` value from `.env`

Click **Test Connection**. This calls the Azure API, which is what needs
Tailscale connectivity to your Pi — see "Connecting Azure to your
tailnet" below. Then **Save** and enable the config.

This is a runtime, DB-backed change via the admin API — it does **not**
require a Bicep/`azd` redeploy, and the edge URL/token are never put in
Bicep parameters or committed to source control.

### Connecting Azure to your tailnet

The Azure Container Apps environment itself also needs a path onto your
tailnet to reach the Pi's Tailscale address. The two common options are:

1. **Tailscale subnet router / Azure Container App sidecar**: run a
   Tailscale client as a sidecar (or a small container instance) inside
   the same Container Apps environment, advertising itself as an exit
   node/subnet router into your tailnet, and route the API's outbound
   calls to the edge connector through it.
2. **Tailscale Funnel/Serve on the Pi** (simpler, slightly less private):
   expose the edge connector through Tailscale Funnel so it gets a public
   HTTPS hostname that still requires your `HOME_CAM_EDGE_TOKEN` bearer
   token to do anything beyond `/healthz`. This avoids needing tailnet
   connectivity from Azure at the cost of the endpoint being technically
   internet-reachable (still credential-gated).

Pick whichever matches your risk tolerance; both keep the Dahua NVR
itself off the public internet, which is the actual goal here.

## 5. Secrets guidance (Key Vault / Container Apps)

- The edge connector's own token (`HOME_CAM_EDGE_TOKEN`) is stored
  encrypted in HomeCam's database (via the admin API, same mechanism as
  the direct-mode Dahua password) — it is not a Bicep parameter and is
  never written to `infra/*.bicep` or Container Apps secrets.
- If you would rather manage it via Azure Key Vault (e.g. for audit/
  rotation tooling you already have), you can still do so out-of-band:
  store the value in Key Vault, then paste it into the Admin UI/API the
  same way — HomeCam's admin API is the single source of truth the
  running API process reads, regardless of where you also keep a copy.
- `infra/modules/api.bicep` only sets a non-secret `DAHUA_MODE=direct`
  default env var; it intentionally does not include a `DAHUA_EDGE_URL`/
  `DAHUA_EDGE_TOKEN` Bicep parameter, so that changing them never requires
  a redeploy or touches Bicep/Container Apps secret state at all.

## Troubleshooting

| Symptom | Likely cause |
|---|---|
| Test Connection: "edge connector unreachable" | Azure has no path to the Pi's Tailscale address yet (see step 4), or the Pi/container is down. |
| Test Connection: edge connector reachable, Dahua NVR not | Wrong `DAHUA_HOST`/credentials in `apps/edge/.env`, or the Pi is not actually on the NVR's LAN/VLAN. |
| `401` from the edge connector | Wrong/missing `HOME_CAM_EDGE_TOKEN` in the Admin UI vs. `apps/edge/.env`. |
| Live tab shows "not available in the browser" | The provider returned a `kind` other than `hls`/`link` (e.g. raw RTSP) — edge mode should always return `hls`; if you see this in edge mode, check MediaMTX is actually running and the `dahua-<channel>` path exists in `mediamtx.yml`. |
| QR code scan didn't give you a host/user/password | Expected — the label QR only encodes the serial/DT pair, not connectivity details. You must find the NVR's LAN IP yourself (router/DHCP table). |
