# HomeCam Eufy edge adapter (Home Assistant add-on)

Runs `apps/eufy-edge` on the Home Assistant host, next to the
**eufy-security-ws** add-on, and serves the Eufy doorbell to HomeCam:
device list, event snapshots, doorbell/motion state, and on-demand live HLS.

## What it does and does not hold

- **No Eufy credentials.** The Eufy account, password, 2FA/captcha and the
  persistent device session stay in the eufy-security-ws add-on. This
  add-on only connects to its local WebSocket (`ws://127.0.0.1:3000`).
- **One HomeCam token** (`home_cam_eufy_token`), which HomeCam sends as a
  bearer token. It is unrelated to any Eufy credential.
- **Its own private go2rtc 1.9.14**: API on `127.0.0.1:21984` and RTSP on
  `127.0.0.1:28554` (loopback only), and WebRTC/SRTP not loaded. It does not
  touch Frigate, its go2rtc or Home Assistant Core's built-in go2rtc
  (11984/18554/18555), and does not use ports 1984/8554/8555/8443, so
  Frigate, Home Assistant and the Dahua edge add-on are unaffected. If
  either private port is already taken, the add-on refuses to start rather
  than talk to another server. Streams are
  registered in memory only; nothing is written to disk.
- **Token-gated HLS relay** on the adapter port:
  `/hls/<random per-start token>/eufy-<serial>/stream.m3u8`. HomeCam gets
  this URL from `/live` and relays it through its own API; go2rtc's API is
  never reachable from the network. The relay and ingest tokens are
  regenerated on every start and redacted from all logs.
- The livestream stops when the last viewer leaves, so the battery doorbell
  is not kept awake. Arming/guard mode is never changed.
- **Event clips (1.0.2+):** on a ring or motion/person event the adapter
  records one bounded post-roll clip (default 15 s) through its private
  go2rtc, then stops the stream. A sleeping battery doorbell has no earlier
  footage, so clips start when the camera woke: there is no pre-roll and
  none is fabricated. Recordings are held in memory only for a short time
  until HomeCam fetches them with the bearer token, with a 120 s cooldown
  and a daily cap to protect the battery. Since 1.0.3 recording starts only
  once the waking doorbell has sent its first keyframe (bounded by
  `LIVE_READY_TIMEOUT_SECONDS`); before that, go2rtc's ffmpeg could fail to
  probe the not-yet-decodable stream.

## Install

1. The HomeCam repository (`https://github.com/atheuk/homecam-ai`) is
   already in **Settings → Add-ons → Add-on Store → Repositories**. Click
   **⋮ → Check for updates**, then open **HomeCam Eufy edge adapter**.
2. **Install**. Do not start it yet.
3. **Configuration**:
   - `eufy_ws_url`: keep `ws://127.0.0.1:3000` (the eufy-security-ws add-on).
   - `home_cam_eufy_token`: a new long random value, generated on your own
     machine (e.g. `openssl rand -hex 32`). Never reuse a Eufy password.
   - `stream_base_url`: this host's Tailscale URL on the adapter port, e.g.
     `http://homeassistant.<tailnet>.ts.net:8091`.
   - `port`: `8091`, unless something else already uses it.
   - `live_idle_stop_seconds`: `60`.
   - `event_clips_enabled`: `true` to record a short clip when the doorbell
     rings or detects motion/a person (see below).
   - `event_clip_daily_limit`: `24` (0-96) recordings per day.
4. **Start**, then check the add-on **Log** shows
   `Uvicorn running on http://0.0.0.0:8091`.
5. In HomeCam, go to **Settings → Eufy adapter**. Set the adapter URL to
   `http://homeassistant.<tailnet>.ts.net:8091`, paste the same token,
   click **Test Connection**, then **Save**.

If `/health` reports `2fa_required`, `captcha_required` or
`unauthenticated`, fix it in the eufy-security-ws add-on (its log or
config), not here.
