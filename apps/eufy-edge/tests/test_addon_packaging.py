"""Packaging checks for the Home Assistant add-on in ``homecam-eufy-edge/``.

HA builds an add-on with only its own folder as the Docker build context,
so the add-on carries copies of the adapter sources. These tests keep the
copies identical to ``apps/eufy-edge`` and pin the security-relevant parts
of the add-on configuration.
"""
from __future__ import annotations

import json
import re
import socket
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

ADAPTER = Path(__file__).resolve().parents[1]
ADDON = ADAPTER.parents[1] / "homecam-eufy-edge"


def _text(path: Path) -> str:
    return path.read_text(encoding="utf-8").replace("\r\n", "\n")


@pytest.mark.parametrize("name", ["app.py", "eufy_ws.py"])
def test_addon_sources_are_identical_to_the_adapter(name):
    assert _text(ADDON / name) == _text(ADAPTER / name), (
        f"homecam-eufy-edge/{name} is stale: copy apps/eufy-edge/{name} over it"
    )


def _config() -> dict:
    return yaml.safe_load(_text(ADDON / "config.yaml"))


def test_addon_config_is_a_valid_ha_addon_for_this_host():
    config = _config()
    for key in ("name", "version", "slug", "description", "arch", "options", "schema"):
        assert key in config
    assert config["slug"] == "homecam_eufy_edge"
    assert "aarch64" in config["arch"]  # the Home Assistant host is aarch64
    assert config["host_network"] is True
    assert set(config["options"]) == set(config["schema"])
    assert config["options"]["eufy_ws_url"] == "ws://127.0.0.1:3000"
    assert config["options"]["port"] == 8091


def test_addon_never_takes_eufy_account_credentials():
    config = _config()
    for key in config["schema"]:
        assert not re.search(r"eufy_(user|email|pass|password|2fa|captcha|country)", key)
    assert config["schema"]["home_cam_eufy_token"] == "password"
    # Defaults must never ship a usable token.
    assert config["options"]["home_cam_eufy_token"] == ""


def test_dockerfile_maps_every_supported_arch_and_pins_go2rtc():
    dockerfile = _text(ADDON / "Dockerfile")
    for arch in _config()["arch"]:
        assert re.search(rf"^\s*{arch}\)", dockerfile, re.M), arch
    version = re.search(r"GO2RTC_VERSION=(\d+)\.(\d+)\.(\d+)", dockerfile)
    assert version, "go2rtc version must be pinned"
    # PATCH /api/streams (in-memory registration) exists from 1.2.0.
    assert tuple(map(int, version.groups())) >= (1, 2, 0)
    assert "COPY app.py eufy_ws.py" in dockerfile


def test_bundled_go2rtc_stays_private_and_off_shared_host_ports():
    run = _text(ADDON / "run.sh")
    api_port = int(re.search(r"^go2rtc_api_port=(\d+)$", run, re.M).group(1))
    rtsp_port = int(re.search(r"^go2rtc_rtsp_port=(\d+)$", run, re.M).group(1))
    # Ports already owned on the HAOS host network: Home Assistant Core's
    # built-in go2rtc (API 11984, RTSP 18554, WebRTC 18555), Frigate/go2rtc
    # defaults, the Dahua edge add-on, eufy-security-ws and this adapter.
    reserved = {11984, 18554, 18555, 1984, 8554, 8555, 8443, 8888, 8189, 3000, 8091}
    assert api_port not in reserved and rtsp_port not in reserved
    assert api_port != rtsp_port
    config = yaml.safe_load(
        run.split("<<EOF\n", 1)[1].split("\nEOF\n", 1)[0]
        .replace("${go2rtc_api_port}", str(api_port))
        .replace("${go2rtc_rtsp_port}", str(rtsp_port))
    )
    assert config["api"]["listen"] == f"127.0.0.1:{api_port}"
    assert config["rtsp"]["listen"] == f"127.0.0.1:{rtsp_port}"
    assert 'GO2RTC_URL="http://127.0.0.1:${go2rtc_api_port}"' in run
    modules = set(config["app"]["modules"])
    # ffmpeg sources publish through go2rtc's own RTSP server, and HLS is
    # what HomeCam plays; WebRTC/SRTP would grab 8555/8443 on the host.
    assert {"api", "rtsp", "hls", "ffmpeg", "exec"} <= modules
    assert not modules & {"webrtc", "srtp", "homekit", "rtmp", "webtorrent", "ngrok"}
    assert "INTERNAL_LOOPBACK_ONLY=1" in run
    assert "HLS_PUBLIC_BASE_URL" in run


@pytest.mark.skipif(
    sys.platform == "win32",
    reason="SO_REUSEADDR lets Windows bind over a listener; run.sh only runs on Linux",
)
def test_run_script_refuses_to_start_when_a_private_port_is_taken():
    run = _text(ADDON / "run.sh")
    probe = run.index('python3 - "$go2rtc_api_port" "$go2rtc_rtsp_port"')
    assert probe < run.index("/usr/local/bin/go2rtc -config")
    script = run[probe:].split("<<'PY'\n", 1)[1].split("\nPY\n", 1)[0]
    with socket.socket() as taken:
        taken.bind(("127.0.0.1", 0))
        taken.listen()
        busy = taken.getsockname()[1]
        result = subprocess.run(
            [sys.executable, "-c", script, str(busy)], capture_output=True, text=True
        )
    assert result.returncode != 0
    assert f"127.0.0.1:{busy} is already in use" in result.stderr
    with socket.socket() as free:
        free.bind(("127.0.0.1", 0))
        idle = free.getsockname()[1]
    result = subprocess.run([sys.executable, "-c", script, str(idle)], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


def test_run_script_never_echoes_secrets():
    run = _text(ADDON / "run.sh")
    for line in run.splitlines():
        if re.search(r"\b(echo|printf)\b", line):
            assert not re.search(r"\$\{?(HOME_CAM_EUFY_TOKEN|STREAM_TOKEN|HLS_TOKEN)", line), line
    assert "set -x" not in run


def test_repository_manifest_exists_for_the_addon_store():
    manifest = yaml.safe_load(_text(ADDON.parent / "repository.yaml"))
    assert manifest["url"] == "https://github.com/atheuk/homecam-ai"


def test_options_round_trip_as_json_like_supervisor_writes_them():
    # Supervisor writes /data/options.json from these defaults; run.sh reads it.
    assert json.loads(json.dumps(_config()["options"]))["live_idle_stop_seconds"] == 60
