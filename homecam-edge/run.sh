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
print(value)
PY
}

export DAHUA_SCHEME="$(read_option dahua_scheme)"
export DAHUA_HOST="$(read_option dahua_host)"
export DAHUA_PORT="$(read_option dahua_port)"
export DAHUA_USERNAME="$(read_option dahua_username)"
export DAHUA_PASSWORD="$(read_option dahua_password)"
export DAHUA_CHANNELS="$(read_option dahua_channels)"
export HOME_CAM_EDGE_TOKEN="$(read_option home_cam_edge_token)"
export DAHUA_TIMEOUT_SECONDS="$(read_option dahua_timeout_seconds)"

if [ -z "$DAHUA_HOST" ] || [ -z "$DAHUA_USERNAME" ] || [ -z "$DAHUA_PASSWORD" ] || [ -z "$HOME_CAM_EDGE_TOKEN" ]; then
  echo "dahua_host, dahua_username, dahua_password, and home_cam_edge_token are required" >&2
  exit 1
fi

stream_base_url="$(read_option stream_base_url)"
if [ -z "$stream_base_url" ]; then
  echo "stream_base_url must be the private Tailscale URL for this host's HLS port, for example http://homeassistant.tailnet.ts.net:8888" >&2
  exit 1
fi
export STREAM_BASE_URL="${stream_base_url%/}"

python3 - <<'PY'
import json
import os
import re
from pathlib import Path

channels = os.environ["DAHUA_CHANNELS"].split(",")
paths = []
for raw in channels:
    match = re.fullmatch(r"\s*(\d+)(?::[^:]+)?(?::(?:camera|doorbell))?\s*", raw)
    if not match:
        raise SystemExit(f"Invalid dahua_channels entry: {raw!r}")
    channel = match.group(1)
    paths += [
        f"  dahua-{channel}:",
        (
            "    source: "
            f"rtsp://${{DAHUA_USERNAME}}:${{DAHUA_PASSWORD}}@${{DAHUA_HOST}}:554/"
            f"cam/realmonitor?channel={channel}&subtype=0"
        ),
        "    sourceOnDemand: yes",
        "",
    ]

Path("/tmp/mediamtx.yml").write_text(
    "hls: yes\nhlsAddress: :8888\nwebrtc: yes\nwebrtcAddress: :8189\npaths:\n"
    + "\n".join(paths),
    encoding="utf-8",
)
PY

/usr/local/bin/mediamtx /tmp/mediamtx.yml &
mediamtx_pid=$!
uvicorn_pid=""

shutdown() {
  [ -n "$uvicorn_pid" ] && kill "$uvicorn_pid" 2>/dev/null || true
  kill "$mediamtx_pid" 2>/dev/null || true
  wait "$uvicorn_pid" 2>/dev/null || true
  wait "$mediamtx_pid" 2>/dev/null || true
}
trap shutdown EXIT INT TERM

uvicorn app:app --host 0.0.0.0 --port 8443 &
uvicorn_pid=$!
wait "$uvicorn_pid"
