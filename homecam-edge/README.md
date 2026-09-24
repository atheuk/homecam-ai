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

## Camera online/offline reporting

`/channels` reports each channel individually: a channel with no camera
physically attached is `online: false` even though the NVR answers fine.

This NVR's embedded HTTP server only sustains ~1-2 concurrent CGI sessions
and refuses a session whenever it is busy, so a failed whole-NVR
reachability probe is *not* treated as proof that the cameras went away. A
channel that produced a real snapshot within `CHANNEL_LIVENESS_TTL_SECONDS`
(default 60) keeps its online status through such a blip; once that
evidence expires without a successful probe it does drop to offline.
Failed reachability probes are re-checked after
`DAHUA_PROBE_FAILURE_TTL_SECONDS` (default 5) rather than being cached for
the full `DAHUA_PROBE_TTL_SECONDS` (default 30).

The same reasoning applies to the per-channel snapshot probe, which is the
dominant cause of cameras flickering offline: a *successful* snapshot
proves the camera is there, but a *failed* one usually just means the NVR
was busy. A channel that is currently known online therefore needs
`CHANNEL_FAILURE_THRESHOLD` (default 3) consecutive failed probes, retried
`CHANNEL_FAILURE_RETRY_SECONDS` (default 5) apart, before it is reported
offline. A channel that has never been seen online costs exactly one probe
per TTL, so genuinely disconnected channels stay cheap and are reported
offline immediately.

If cameras still flicker on your NVR, raise `CHANNEL_FAILURE_THRESHOLD` or
`CHANNEL_FAILURE_RETRY_SECONDS`.
