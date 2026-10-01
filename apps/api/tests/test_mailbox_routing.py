# ruff: noqa: F811  (``scene`` is a pytest fixture imported from test_scene_state)
"""Mailbox events beyond the scene tracker: cross-replica dedup, priority and
incident routing, the diagnostics stats line, and the sampling boost."""
from __future__ import annotations

import logging
from types import SimpleNamespace

import pytest
from sqlalchemy import delete, select

from app.ai.scene_verifier import set_scene_verifier
from app.config import settings
from app.db import SessionLocal, init_db
from app.models.db import Event, EventEvidence, Incident, SceneDedupClaim, SceneState
from app.services import ingestion, priority, scene_state, security_modes
from app.services.stream_frames import stream_hub
from test_scene_state import (
    CAMERA,
    EMPTY_MAILBOX,
    MAILBOX,
    NAME,
    OPENED,
    PARCEL,
    WITH_PERSON,
    YES,
    FakeVerifier,
    _kinds,
    _zone,
    person,
)
from test_scene_state import scene  # noqa: F401

RETRIEVED = {**YES, "item_deposited": "no", "action": "retrieved"}


@pytest.fixture(autouse=True)
async def _clean():
    await init_db()

    async def _clear():
        async with SessionLocal() as session:
            await session.execute(delete(Incident))
            await session.execute(delete(EventEvidence))
            await session.execute(delete(Event))
            await session.execute(delete(SceneDedupClaim))
            await session.commit()
            await security_modes.set_mode(session, "disarmed", None)

    await _clear()
    yield
    await _clear()
    stream_hub.clear_boosts()


def _replica_emitting(monkeypatch, transitions) -> None:
    async def fake_process_frame(session, camera_id, camera_name, image, detections, **kwargs):
        return list(transitions)

    async def fake_note_event(session, transition, event_id):
        return None

    monkeypatch.setattr(scene_state, "process_frame", fake_process_frame)
    monkeypatch.setattr(scene_state, "note_event", fake_note_event)


async def _emit() -> int:
    return await ingestion._emit_scene_transitions(SessionLocal, CAMERA, NAME, b"frame", [], None, "snapshot")


async def _events() -> list[Event]:
    async with SessionLocal() as session:
        return list((await session.execute(select(Event).where(Event.camera_id == CAMERA))).scalars())


async def _incidents() -> list[Incident]:
    async with SessionLocal() as session:
        return list((await session.execute(select(Incident))).scalars())


async def _set_mode(mode: str) -> None:
    async with SessionLocal() as session:
        await security_modes.set_mode(session, mode, None)


async def _opening(scene):
    await scene.run(3, [], EMPTY_MAILBOX)
    return await scene.run(3, [], OPENED)


async def _retrieval(scene):
    set_scene_verifier(FakeVerifier(mailbox=RETRIEVED))
    await scene.run(3, [], EMPTY_MAILBOX)
    out = await scene.run(2, [person()], WITH_PERSON)
    return out + await scene.run(4, [], EMPTY_MAILBOX)


async def _forget_memory() -> None:
    """Simulate a second replica: its own in-process memory and no shared
    scene state, only the shared database for dedup claims."""
    scene_state.reset_memory()
    async with SessionLocal() as session:
        await session.execute(delete(SceneState).where(SceneState.camera_id == CAMERA))
        await session.commit()


# --- cross-replica dedup -------------------------------------------------------------


@pytest.mark.parametrize("kind", ["mailbox_opened", "mailbox_retrieval"])
async def test_two_replicas_seeing_the_same_mailbox_event_emit_it_once(scene, monkeypatch, kind):
    await _zone("mailbox", MAILBOX, "Mailbox")
    produce = _opening if kind == "mailbox_opened" else _retrieval
    replica_a = await produce(scene)
    await _forget_memory()
    replica_b = await produce(scene)
    assert _kinds(replica_a) == [kind] == _kinds(replica_b)
    assert replica_a[0].dedup_key == replica_b[0].dedup_key
    assert replica_a[0].dedup_key.startswith(f"{kind}:{CAMERA}:")

    _replica_emitting(monkeypatch, replica_a)
    first = await _emit()
    _replica_emitting(monkeypatch, replica_b)
    second = await _emit()
    assert (first, second) == (1, 0)
    [event] = await _events()
    assert kind in event.tags


# --- priority and incidents -------------------------------------------------------


@pytest.mark.parametrize(
    "tags,mode,expected",
    [
        (["mailbox", "mailbox_delivery", "mail"], "home", "normal"),
        (["mailbox", "mailbox_delivery", "mail"], "disarmed", "normal"),
        (["mailbox", "mailbox_retrieval", "mail"], "home", "normal"),
        (["mailbox", "mailbox_retrieval", "mail"], "disarmed", "normal"),
        (["mailbox", "mailbox_retrieval", "mail"], "away", "high"),
        (["mailbox", "mailbox_retrieval", "parcel", "package_removed"], "away", "critical"),
        (["mailbox", "mailbox_opened"], "home", "normal"),
        (["mailbox", "mailbox_visit"], "home", "low"),
    ],
)
def test_mailbox_priority(tags, mode, expected):
    assert priority.score_event(event_type="package", mode=mode, tags=tags).priority == expected


async def test_mail_taken_out_while_away_opens_a_medium_incident(scene, monkeypatch):
    await _zone("mailbox", MAILBOX, "Mailbox")
    [retrieval] = await _retrieval(scene)
    await _set_mode("away")
    _replica_emitting(monkeypatch, [retrieval])
    assert await _emit() == 1

    [event] = await _events()
    [incident] = await _incidents()
    assert event.event_metadata["notification_priority"] == "high"
    assert incident.kind == "mailbox_retrieval"
    assert incident.severity == "medium"
    assert incident.summary.startswith("Something was taken out of the Mailbox zone")
    assert incident.event_ids == [event.id]


@pytest.mark.parametrize("mode", ["home", "disarmed"])
async def test_mail_taken_out_while_home_is_just_an_event(scene, monkeypatch, mode):
    await _zone("mailbox", MAILBOX, "Mailbox")
    transitions = await _retrieval(scene)
    await _set_mode(mode)
    _replica_emitting(monkeypatch, transitions)
    assert await _emit() == 1
    assert await _incidents() == []


async def test_deliveries_never_open_incidents_even_when_away(scene, monkeypatch):
    await _zone("mailbox", MAILBOX, "Mailbox")
    await scene.run(3, [], EMPTY_MAILBOX)
    delivery = await scene.run(1, [person()], WITH_PERSON)
    delivery += await scene.run(3, [PARCEL], EMPTY_MAILBOX)
    assert _kinds(delivery) == ["mailbox_delivery"]
    await _set_mode("away")
    _replica_emitting(monkeypatch, delivery)
    assert await _emit() == 1
    [event] = await _events()
    assert await _incidents() == []
    assert event.event_metadata["notification_priority"] == "normal"


# --- diagnostics ---------------------------------------------------------------------


async def test_each_visit_is_logged_and_counted_on_the_stats_line(scene, monkeypatch, caplog):
    await _zone("mailbox", MAILBOX, "Mailbox")
    with caplog.at_level(logging.INFO, logger="app.services.scene_state"):
        await scene.run(3, [], EMPTY_MAILBOX)
        await scene.run(1, [person()], WITH_PERSON)
        await scene.run(3, [], EMPTY_MAILBOX)
    [visit_line] = [r.getMessage() for r in caplog.records if "outcome=mailbox_visit" in r.getMessage()]
    assert f"mailbox {CAMERA}/Mailbox" in visit_line
    assert "observations=1" in visit_line and "cover=" in visit_line and "diff=" in visit_line

    monkeypatch.setattr(settings, "ingestion_stats_log_seconds", 0.0)
    ingestion._stats_for(CAMERA)
    caplog.clear()
    with caplog.at_level(logging.INFO, logger="app.services.ingestion"):
        ingestion._maybe_log_stats(CAMERA)
    [line] = [r.getMessage() for r in caplog.records if r.getMessage().startswith(f"ingestion frames {CAMERA}")]
    assert "boosted=no" in line
    assert "mailbox_visits=1" in line and "mailbox_events=1" in line and "mailbox_walk_by=0" in line
    # Counters are per window.
    assert scene_state.drain_mailbox_stats(CAMERA) == {}


# --- sampling boost ---------------------------------------------------------------


async def test_a_person_at_the_mailbox_boosts_a_stream_camera(scene, monkeypatch):
    monkeypatch.setattr(settings, "stream_frames_enabled", True)
    await _zone("mailbox", MAILBOX, "Mailbox")
    stream_hub._readers[CAMERA] = SimpleNamespace(unsupported=False)
    try:
        base = ingestion.tick_seconds()
        await scene.run(2, [], EMPTY_MAILBOX)
        assert not stream_hub.boosted(CAMERA)
        await scene.run(1, [person()], WITH_PERSON)
        assert stream_hub.boosted(CAMERA)
        assert stream_hub.sample_interval(CAMERA) == settings.mailbox_boost_interval_seconds
        assert ingestion.tick_seconds() == min(base, settings.mailbox_boost_interval_seconds)
    finally:
        stream_hub._readers.pop(CAMERA, None)
        stream_hub.clear_boosts()
    assert ingestion.tick_seconds() == base


async def test_snapshot_only_cameras_are_never_boosted(scene, monkeypatch):
    monkeypatch.setattr(settings, "stream_frames_enabled", True)
    await _zone("mailbox", MAILBOX, "Mailbox")
    await scene.run(2, [], EMPTY_MAILBOX)
    await scene.run(1, [person()], WITH_PERSON)
    assert not stream_hub.boosted(CAMERA)
    stream_hub._readers[CAMERA] = SimpleNamespace(unsupported=True)
    try:
        assert stream_hub.boost(CAMERA, 60) is False
    finally:
        stream_hub._readers.pop(CAMERA, None)
