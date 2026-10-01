from __future__ import annotations

import base64
import json
from types import SimpleNamespace

import pytest

from app.ai.vehicles import (
    MAX_VEHICLE_FRAMES,
    AzureFoundryVehicleIdentifier,
    FallbackVehicleIdentifier,
    MockVehicleIdentifier,
    VehicleIdentity,
    build_vehicle_identifier,
    parse_vehicle_reply,
)


def _reply(**overrides) -> str:
    result = {
        "vehicle_present": "yes",
        "make": "Toyota",
        "make_confidence": 0.9,
        "model": "Corolla",
        "model_confidence": 0.8,
        "year_generation": "2019-2022 generation",
        "year_generation_confidence": 0.65,
        "colour": "silver",
        "colour_confidence": 0.95,
        "body_type": "sedan",
        "body_type_confidence": 0.85,
        "trim": "unknown",
        "trim_confidence": 0,
    }
    result.update(overrides)
    return json.dumps(result)


def test_parses_independent_field_confidences_and_explicit_unknowns():
    identity = parse_vehicle_reply(_reply())

    assert identity == VehicleIdentity(
        vehicle_present="yes",
        make="Toyota",
        make_confidence=0.9,
        model="Corolla",
        model_confidence=0.8,
        year_generation="2019-2022 generation",
        year_generation_confidence=0.65,
        colour="silver",
        colour_confidence=0.95,
        body_type="sedan",
        body_type_confidence=0.85,
        trim="unknown",
        trim_confidence=0,
    )
    assert identity.as_dict()["trim"] == "unknown"


def test_unknown_fields_never_keep_confidence_and_confidence_is_clamped():
    identity = parse_vehicle_reply(
        _reply(make="not sure", make_confidence=0.99, colour_confidence=125, model_confidence=-2)
    )

    assert identity is not None
    assert identity.make == "unknown"
    assert identity.make_confidence == 0
    assert identity.colour_confidence == 1
    assert identity.model_confidence == 0


def test_no_vehicle_clears_vehicle_attributes():
    identity = parse_vehicle_reply(_reply(vehicle_present="no", make="Toyota", make_confidence=1))

    assert identity is not None
    assert identity.vehicle_present == "no"
    assert identity.make == "unknown"
    assert identity.make_confidence == 0


@pytest.mark.parametrize("reply", [None, "", "not json", "[]", '{"vehicle_present":"maybe"}'])
def test_invalid_reply_yields_no_identification(reply):
    assert parse_vehicle_reply(reply) is None


@pytest.mark.asyncio
async def test_foundry_call_requests_strict_json_schema_and_sends_image(monkeypatch):
    captured = {}

    class Response:
        def raise_for_status(self):
            pass

        def json(self):
            return {"choices": [{"message": {"content": _reply()}}]}

    class Client:
        def __init__(self, timeout):
            captured["timeout"] = timeout

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def post(self, url, json, headers):
            captured.update(url=url, payload=json, headers=headers)
            return Response()

    monkeypatch.setattr("app.ai.vehicles.httpx.AsyncClient", Client)
    identifier = AzureFoundryVehicleIdentifier(
        endpoint="https://example.test/", api_key="secret", deployment="vision"
    )
    identity = await identifier.identify_vehicle(b"frame", "image/png")

    assert identity is not None and identity.model == "Corolla"
    assert captured["url"] == (
        "https://example.test/openai/deployments/vision/chat/completions?api-version=2024-10-21"
    )
    assert captured["payload"]["response_format"]["type"] == "json_schema"
    assert captured["payload"]["response_format"]["json_schema"]["strict"] is True
    image_url = captured["payload"]["messages"][1]["content"][1]["image_url"]["url"]
    assert image_url == f"data:image/png;base64,{base64.b64encode(b'frame').decode('ascii')}"


@pytest.mark.asyncio
async def test_frame_consensus_uses_majority_and_reduces_confidence(monkeypatch):
    identifier = AzureFoundryVehicleIdentifier("https://example.test", "key", "vision")
    replies = iter(
        [
            VehicleIdentity(vehicle_present="yes", make="Toyota", make_confidence=0.9),
            VehicleIdentity(vehicle_present="yes", make="Toyota", make_confidence=0.7),
            VehicleIdentity(vehicle_present="yes", make="Honda", make_confidence=1.0),
        ]
    )

    async def identify_one(image, content_type):
        return next(replies)

    monkeypatch.setattr(identifier, "_identify_one", identify_one)
    result = await identifier.identify_vehicle_frames([b"a", b"b", b"c"])

    assert result is not None
    assert result.make == "Toyota"
    assert result.make_confidence == pytest.approx((0.9 + 0.7) / 2 * 2 / 3)


@pytest.mark.asyncio
async def test_frame_consensus_marks_ties_unknown(monkeypatch):
    identifier = AzureFoundryVehicleIdentifier("https://example.test", "key", "vision")
    replies = iter(
        [
            VehicleIdentity(vehicle_present="yes", model="Corolla", model_confidence=0.9),
            VehicleIdentity(vehicle_present="yes", model="Civic", model_confidence=0.8),
        ]
    )

    async def identify_one(image, content_type):
        return next(replies)

    monkeypatch.setattr(identifier, "_identify_one", identify_one)
    result = await identifier.identify_vehicle_frames([b"a", b"b"])

    assert result is not None
    assert result.model == "unknown"
    assert result.model_confidence == 0


@pytest.mark.asyncio
async def test_no_vehicle_consensus_clears_attributes(monkeypatch):
    identifier = AzureFoundryVehicleIdentifier("https://example.test", "key", "vision")
    replies = iter(
        [
            VehicleIdentity(vehicle_present="no"),
            VehicleIdentity(vehicle_present="no"),
            VehicleIdentity(vehicle_present="yes", make="Toyota", make_confidence=0.9),
        ]
    )

    async def identify_one(image, content_type):
        return next(replies)

    monkeypatch.setattr(identifier, "_identify_one", identify_one)
    result = await identifier.identify_vehicle_frames([b"a", b"b", b"c"])

    assert result is not None
    assert result.vehicle_present == "no"
    assert result.make == "unknown"
    assert result.make_confidence == 0


@pytest.mark.asyncio
async def test_multi_frame_input_is_bounded():
    identifier = AzureFoundryVehicleIdentifier("https://example.test", "key", "vision")
    with pytest.raises(ValueError, match="at most"):
        await identifier.identify_vehicle_frames([b"x"] * (MAX_VEHICLE_FRAMES + 1))


@pytest.mark.asyncio
async def test_foundry_failure_falls_back_to_deterministic_unknown():
    class BrokenIdentifier:
        name = "broken"

        async def identify_vehicle(self, image, content_type="image/jpeg"):
            raise RuntimeError("offline")

        async def identify_vehicle_frames(self, frames):
            raise RuntimeError("offline")

    fallback = FallbackVehicleIdentifier(BrokenIdentifier(), MockVehicleIdentifier())
    assert await fallback.identify_vehicle(b"image") == VehicleIdentity()
    assert await fallback.identify_vehicle_frames([b"image"]) == VehicleIdentity()


def test_builder_selects_mock_when_foundry_is_unconfigured():
    identifier = build_vehicle_identifier(
        SimpleNamespace(foundry_endpoint="", foundry_api_key="")
    )
    assert isinstance(identifier, MockVehicleIdentifier)
