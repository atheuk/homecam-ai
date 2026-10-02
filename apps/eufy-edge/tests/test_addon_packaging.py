"""Packaging checks for the Home Assistant add-on in ``homecam-eufy-edge/``.

HA builds an add-on with only its own folder as the Docker build context,
so the add-on carries copies of the adapter sources. These tests keep the
copies identical to ``apps/eufy-edge`` and pin the security-relevant parts
of the add-on configuration.
"""
from __future__ import annotations

import json
import re
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
    config = yaml.safe_load(run.split("<<EOF\n", 1)[1].split("\nEOF\n", 1)[0].replace("${go2rtc_api_port}", "11984").replace("${go2rtc_rtsp_port}", "18554"))
    assert config["api"]["listen"].startswith("127.0.0.1:")
    assert config["rtsp"]["listen"].startswith("127.0.0.1:")
    modules = set(config["app"]["modules"])
    # ffmpeg sources publish through go2rtc's own RTSP server, and HLS is
    # what HomeCam plays; WebRTC/SRTP would grab 8555/8443 on the host.
    assert {"api", "rtsp", "hls", "ffmpeg", "exec"} <= modules
    assert not modules & {"webrtc", "srtp", "homekit", "rtmp", "webtorrent", "ngrok"}
    assert "INTERNAL_LOOPBACK_ONLY=1" in run
    assert "HLS_PUBLIC_BASE_URL" in run


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
