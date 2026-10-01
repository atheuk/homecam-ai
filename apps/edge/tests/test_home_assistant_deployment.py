"""Ensure the Home Assistant add-on carries event snapshot timeout settings."""
import asyncio
import importlib.util
import sys
import time
from pathlib import Path

import httpx
import pytest

ADDON_DIR = Path(__file__).resolve().parents[3] / "homecam-edge"
ADDON_APP_PATH = ADDON_DIR / "app.py"
spec = importlib.util.spec_from_file_location("homecam_edge_app", ADDON_APP_PATH)
assert spec is not None and spec.loader is not None
homecam_edge = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = homecam_edge
spec.loader.exec_module(homecam_edge)


def test_addon_timeout_setting_defaults_and_reads_environment(monkeypatch):
    monkeypatch.delenv("DAHUA_EVIDENCE_SNAPSHOT_TIMEOUT_SECONDS", raising=False)
    assert homecam_edge.settings_from_env().evidence_snapshot_timeout_seconds == 3.0

    monkeypatch.setenv("DAHUA_EVIDENCE_SNAPSHOT_TIMEOUT_SECONDS", "1.25")
    assert homecam_edge.settings_from_env().evidence_snapshot_timeout_seconds == 1.25


def test_addon_timeout_setting_must_be_positive():
    with pytest.raises(ValueError, match="finite positive"):
        homecam_edge.EdgeSettings(evidence_snapshot_timeout_seconds=0)


@pytest.mark.asyncio
async def test_addon_full_snapshot_uses_configured_timeout():
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, content=b"\xff\xd8\xff\xd9")

    settings = homecam_edge.EdgeSettings(
        dahua_host="192.0.2.1",
        dahua_username="user",
        dahua_password="password",
        edge_token="edge-token",
        evidence_snapshot_timeout_seconds=1.25,
    )
    app = homecam_edge.create_app(settings, httpx.MockTransport(handler))
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://edge.local"
    ) as client:
        response = await client.get(
            "/channels/1/snapshot?full=true",
            headers={"Authorization": "Bearer edge-token"},
        )

    assert response.status_code == 200
    assert response.content == b"\xff\xd8\xff\xd9"
    assert len(requests) == 1
    assert requests[0].url.params["subtype"] == "0"
    assert requests[0].extensions["timeout"]["read"] == 1.25


@pytest.mark.asyncio
async def test_addon_evidence_deadline_includes_waiting_for_the_nvr_lock():
    settings = homecam_edge.EdgeSettings(
        dahua_host="192.0.2.1",
        dahua_username="user",
        dahua_password="password",
        evidence_snapshot_timeout_seconds=0.04,
    )
    client = homecam_edge.DahuaClient(settings=settings)
    started = time.monotonic()
    async with client._lock:
        with pytest.raises(httpx.TimeoutException, match="total deadline"):
            await client.evidence_snapshot(1)
    assert time.monotonic() - started < 0.2


@pytest.mark.asyncio
async def test_addon_evidence_deadline_caps_slow_drip_response():
    class SlowDrip(httpx.AsyncByteStream):
        async def __aiter__(self):
            for part in (b"\xff\xd8", b"\x00", b"\x00", b"\x00", b"\x00", b"\xff\xd9"):
                await asyncio.sleep(0.02)
                yield part

    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, stream=SlowDrip())

    settings = homecam_edge.EdgeSettings(
        dahua_host="192.0.2.1",
        dahua_username="user",
        dahua_password="password",
        evidence_snapshot_timeout_seconds=0.07,
    )
    client = homecam_edge.DahuaClient(settings=settings, transport=httpx.MockTransport(handler))
    started = time.monotonic()
    with pytest.raises(httpx.TimeoutException, match="total deadline"):
        await client.evidence_snapshot(1)
    elapsed = time.monotonic() - started
    assert len(requests) == 1
    assert 0.05 <= elapsed < 0.2, "deadline must stop slow-drip bodies, not reset per chunk"


def test_addon_options_are_wired_through_supervisor_to_the_connector():
    config = (ADDON_DIR / "config.yaml").read_text(encoding="utf-8")
    run_script = (ADDON_DIR / "run.sh").read_text(encoding="utf-8")
    compose_env = (ADDON_DIR.parent / "apps" / "edge" / ".env.example").read_text(encoding="utf-8")

    assert "dahua_evidence_snapshot_timeout_seconds: 3.0" in config
    assert "dahua_evidence_snapshot_timeout_seconds: float" in config
    assert "DAHUA_EVIDENCE_SNAPSHOT_TIMEOUT_SECONDS=3" in compose_env
    assert (
        'export DAHUA_EVIDENCE_SNAPSHOT_TIMEOUT_SECONDS="$(read_option '
        'dahua_evidence_snapshot_timeout_seconds)"'
    ) in run_script
