#!/bin/sh
set -eu

options=/data/options.json
if [ ! -f "$options" ]; then
  echo "Home Assistant options file is missing" >&2
  exit 1
fi

read_option() {
  python3 - "$1" <<'PY'
import json
import sys

with open("/data/options.json", encoding="utf-8") as handle:
    value = json.load(handle).get(sys.argv[1], "")
print("" if value is None else value)
PY
}

# Only HomeCam's own adapter token is configured here. The Eufy account,
# its password, 2FA/captcha and the persistent session stay inside the
# eufy-security-ws add-on; this add-on never sees them.
export EUFY_WS_URL="$(read_option eufy_ws_url)"
export HOME_CAM_EUFY_TOKEN="$(read_option home_cam_eufy_token)"
export LIVE_IDLE_STOP_SECONDS="$(read_option live_idle_stop_seconds)"
port="$(read_option port)"
port="${port:-8091}"

if [ -z "$HOME_CAM_EUFY_TOKEN" ]; then
  echo "home_cam_eufy_token is required (generate a long random value; never reuse a Eufy password)" >&2
  exit 1
fi

stream_base_url="$(read_option stream_base_url)"
case "$stream_base_url" in
  http://*|https://*) ;;
  *)
    echo "stream_base_url must be this host's private Tailscale URL for the adapter port, for example http://homeassistant.tailnet.ts.net:8091" >&2
    exit 1
    ;;
esac
export HLS_PUBLIC_BASE_URL="${stream_base_url%/}"

# go2rtc is private to this add-on: its API and RTSP server bind to
# loopback on ports that do not collide with Home Assistant Core's built-in
# go2rtc (API 11984, RTSP 127.0.0.1:18554, WebRTC 18555), Frigate
# (1984/8554/8555), the Dahua edge add-on (8443/8888/8189) or RTSP servers
# on the host network. Only the modules the adapter needs are loaded, so
# WebRTC and SRTP never open. Nothing is persisted: streams are registered in
# memory with PATCH /api/streams on every /live call.
go2rtc_api_port=21984
go2rtc_rtsp_port=28554

# go2rtc only logs a listen error and keeps running when a port is taken,
# and the adapter would then register streams with whatever owns that port.
# Refuse to start instead.
python3 - "$go2rtc_api_port" "$go2rtc_rtsp_port" <<'PY'
import socket
import sys

for port in sys.argv[1:]:
    probe = socket.socket()
    probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        probe.bind(("127.0.0.1", int(port)))
    except OSError:
        sys.exit(f"127.0.0.1:{port} is already in use on this host; refusing to start the private go2rtc")
    finally:
        probe.close()
PY

cat > /tmp/go2rtc.yaml <<EOF
app:
  modules: [api, rtsp, http, hls, mp4, ffmpeg, exec]
api:
  listen: "127.0.0.1:${go2rtc_api_port}"
rtsp:
  listen: "127.0.0.1:${go2rtc_rtsp_port}"
log:
  level: warn
EOF

export GO2RTC_URL="http://127.0.0.1:${go2rtc_api_port}"
export SELF_URL="http://127.0.0.1:${port}"
export PORT="$port"
# Fresh per start, never logged: the go2rtc -> adapter ingest token and the
# HLS relay path token. HomeCam re-reads the HLS URL from /live.
export STREAM_TOKEN="$(python3 -c 'import secrets; print(secrets.token_urlsafe(32))')"
export HLS_TOKEN="$(python3 -c 'import secrets; print(secrets.token_urlsafe(32))')"
export INTERNAL_LOOPBACK_ONLY=1

# go2rtc may echo a failing source URL; strip the ingest token from its log.
/usr/local/bin/go2rtc -config /tmp/go2rtc.yaml 2>&1 \
  | python3 -u -c 'import re, sys
for line in sys.stdin:
    sys.stdout.write(re.sub(r"token(=|%3D)[^&%#\s\"]+", r"token\1***", line, flags=re.I))' &
go2rtc_pid=$!
uvicorn_pid=""

shutdown() {
  [ -n "$uvicorn_pid" ] && kill "$uvicorn_pid" 2>/dev/null || true
  pkill -f /usr/local/bin/go2rtc 2>/dev/null || true
  wait "$uvicorn_pid" 2>/dev/null || true
  wait "$go2rtc_pid" 2>/dev/null || true
}
trap shutdown EXIT INT TERM

cd /app
# Bound on all interfaces like the Dahua edge add-on; every route except
# /healthz requires the bearer token or a per-start random token, and the
# raw /internal ingest route only answers loopback clients.
uvicorn app:app --host 0.0.0.0 --port "$port" &
uvicorn_pid=$!
wait "$uvicorn_pid"
