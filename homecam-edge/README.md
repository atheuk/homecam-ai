# HomeCam Dahua edge connector add-on

This is the Home Assistant OS/Supervisor deployment form of `apps/edge/`.
It runs the token-protected HomeCam edge API and MediaMTX in one managed
add-on container. It is for Home Assistant OS; use `apps/edge/docker-compose.yml`
only on a normal Docker host.

## Install

1. In Home Assistant, open **Settings → Apps → App store → ⋮ → Repositories**.
2. Add `https://github.com/atheuk/homecam-ai` and select **HomeCam AI add-ons**.
3. Install **HomeCam Dahua edge connector** and start it.
4. In the add-on configuration, set the Dahua LAN host/credentials/channels,
   a random `home_cam_edge_token`, and `stream_base_url` to this host's
   private Tailscale URL (for example
   `http://homeassistant.example.ts.net:8888`).

Install and join the official Tailscale Home Assistant app separately. Do not
router-port-forward 8443, 8554, 8888, or 8189. Configure HomeCam's Dahua
**edge** mode with `http://<tailscale-host>:8443` and the same token.

The Dahua NVR credentials stay only in the add-on's Supervisor-managed
configuration. Neither the connector API nor the live endpoint returns raw
RTSP URLs or Dahua credentials.
