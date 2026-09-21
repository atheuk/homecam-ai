import os

import pytest

from app.providers.dahua import DahuaProvider, DahuaSettings
from app.providers.eufy import EufyEdgeProvider, EufySettings


@pytest.mark.asyncio
@pytest.mark.skipif(os.environ.get("HOME_CAM_REAL_DAHUA_TESTS") != "1", reason="real Dahua hardware tests are opt-in")
async def test_real_dahua_health_opt_in():
    provider = DahuaProvider(
        DahuaSettings(
            scheme=os.environ.get("DAHUA_SCHEME", "http"),
            host=os.environ["DAHUA_HOST"],
            port=int(os.environ.get("DAHUA_PORT", "80")),
            username=os.environ["DAHUA_USERNAME"],
            password=os.environ["DAHUA_PASSWORD"],
            serial=os.environ.get("DAHUA_SERIAL", "5J006FCPAZ6B52A"),
            channels=os.environ.get("DAHUA_CHANNELS", "1:Front Door"),
        )
    )

    health = await provider.get_health()
    assert health["status"] in {"ONLINE", "DEGRADED"}


@pytest.mark.asyncio
@pytest.mark.skipif(os.environ.get("HOME_CAM_REAL_EUFY_TESTS") != "1", reason="real Eufy adapter tests are opt-in")
async def test_real_eufy_adapter_health_opt_in():
    provider = EufyEdgeProvider(
        EufySettings(
            adapter_url=os.environ["EUFY_ADAPTER_URL"],
            adapter_token=os.environ.get("EUFY_ADAPTER_TOKEN"),
        )
    )

    health = await provider.get_health()
    assert health["status"] in {"ONLINE", "DEGRADED", "UNKNOWN"}
