"""Automatic arming schedules: resolution, transitions, and override lifetime."""
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from app.db import SessionLocal
from app.models.db import ArmingSchedule, SecurityState
from app.services import arming_schedules, security_modes

AMS = ZoneInfo("Europe/Amsterdam")


def _schedule(**kwargs) -> ArmingSchedule:
    now = datetime.now(timezone.utc)
    defaults = dict(
        id=kwargs.pop("id", "s1"),
        name="night",
        mode="night",
        days_of_week=[0, 1, 2, 3, 4, 5, 6],
        start_time="23:00",
        end_time="07:00",
        enabled=True,
        priority=0,
        created_at=now,
        updated_at=now,
    )
    defaults.update(kwargs)
    return ArmingSchedule(**defaults)


def _utc(year, month, day, hour, minute=0) -> datetime:
    return datetime(year, month, day, hour, minute, tzinfo=AMS).astimezone(timezone.utc)


def test_overnight_window_wraps_past_midnight():
    night = _schedule()
    assert arming_schedules.resolve([night], _utc(2026, 3, 10, 23, 30), AMS)[0] == "night"
    assert arming_schedules.resolve([night], _utc(2026, 3, 11, 6, 59), AMS)[0] == "night"
    # 07:00 is the exclusive end; the household is back on the fallback.
    assert arming_schedules.resolve([night], _utc(2026, 3, 11, 7, 0), AMS)[0] == "disarmed"
    assert arming_schedules.resolve([night], _utc(2026, 3, 11, 12, 0), AMS)[0] == "disarmed"


def test_weekday_window_only_fires_on_its_days():
    away = _schedule(id="s2", name="workday", mode="away", days_of_week=[0, 1, 2, 3, 4], start_time="09:00", end_time="17:00")
    # 2026-03-09 is a Monday, 2026-03-14 a Saturday.
    assert arming_schedules.resolve([away], _utc(2026, 3, 9, 10, 0), AMS)[0] == "away"
    assert arming_schedules.resolve([away], _utc(2026, 3, 14, 10, 0), AMS)[0] == "disarmed"


def test_highest_priority_window_wins_when_they_overlap():
    away = _schedule(id="s2", mode="away", name="all day", start_time="00:00", end_time="23:59", priority=1)
    night = _schedule(id="s1", priority=5)
    assert arming_schedules.resolve([night, away], _utc(2026, 3, 10, 23, 30), AMS)[0] == "night"
    assert arming_schedules.resolve([night, away], _utc(2026, 3, 10, 12, 0), AMS)[0] == "away"


def test_wall_clock_times_survive_a_dst_shift():
    """23:00 stays 23:00 local across the spring-forward weekend."""
    night = _schedule()
    # 2026-03-29 is the European DST change (02:00 -> 03:00 local).
    before = _utc(2026, 3, 28, 23, 30)
    after = _utc(2026, 3, 29, 23, 30)
    assert arming_schedules.resolve([night], before, AMS)[0] == "night"
    assert arming_schedules.resolve([night], after, AMS)[0] == "night"
    # The offsets really did differ, so this is not a trivially equal pair.
    assert before.hour != after.hour


def test_boundaries_are_the_window_edges():
    night = _schedule()
    start = _utc(2026, 3, 10, 12, 0)
    found = arming_schedules.boundaries_between(night and [night], start, start + timedelta(days=1), AMS)
    assert _utc(2026, 3, 10, 23, 0) in found
    assert _utc(2026, 3, 11, 7, 0) in found


async def _clear_schedules(session):
    for row in await arming_schedules.list_schedules(session):
        await session.delete(row)
    state = await session.get(SecurityState, security_modes.STATE_ID)
    if state is not None:
        state.last_transition_at = None
        state.mode = "disarmed"
    await session.commit()


@pytest.mark.asyncio
async def test_schedule_crud_is_authenticated_and_audited(client):
    created = await client.post(
        "/api/v1/security/schedules",
        json={
            "name": "Nights",
            "mode": "night",
            "days_of_week": [0, 1, 2, 3, 4, 5, 6],
            "start_time": "23:00",
            "end_time": "07:00",
            "priority": 5,
        },
    )
    assert created.status_code == 201
    schedule_id = created.json()["id"]
    assert created.json()["mode"] == "night"

    listed = await client.get("/api/v1/security/schedules")
    assert any(row["id"] == schedule_id for row in listed.json())

    updated = await client.put(
        f"/api/v1/security/schedules/{schedule_id}", json={"enabled": False, "start_time": "22:30"}
    )
    assert updated.status_code == 200
    assert updated.json()["enabled"] is False
    assert updated.json()["start_time"] == "22:30"

    audit = await client.get("/api/v1/security/audit-log", params={"action": "security.schedule_updated"})
    assert any(entry["target_id"] == schedule_id for entry in audit.json())

    deleted = await client.delete(f"/api/v1/security/schedules/{schedule_id}")
    assert deleted.status_code == 204
    assert all(row["id"] != schedule_id for row in (await client.get("/api/v1/security/schedules")).json())


@pytest.mark.asyncio
async def test_invalid_schedule_is_rejected(client):
    bad_day = await client.post(
        "/api/v1/security/schedules",
        json={"name": "x", "mode": "night", "days_of_week": [9], "start_time": "23:00", "end_time": "07:00"},
    )
    assert bad_day.status_code == 400
    bad_time = await client.post(
        "/api/v1/security/schedules",
        json={"name": "x", "mode": "night", "days_of_week": [1], "start_time": "25:00", "end_time": "07:00"},
    )
    assert bad_time.status_code == 400
    bad_mode = await client.post(
        "/api/v1/security/schedules",
        json={"name": "x", "mode": "vacation", "days_of_week": [1], "start_time": "23:00", "end_time": "07:00"},
    )
    assert bad_mode.status_code == 422


@pytest.mark.asyncio
async def test_transition_is_applied_once_and_audited(client):
    async with SessionLocal() as session:
        await _clear_schedules(session)
        await arming_schedules.create_schedule(
            session,
            name="always away",
            mode="away",
            days_of_week=[0, 1, 2, 3, 4, 5, 6],
            start_time="00:00",
            end_time="00:00",
            priority=1,
        )
        applied = await arming_schedules.apply_due_transition(session)
        assert applied is not None
        assert applied["mode"] == "away"
        assert applied["changed"] is True

        # A second replica ticking against the same boundary is a no-op.
        assert await arming_schedules.apply_due_transition(session) is None
        assert await security_modes.get_mode(session) == "away"

    audit = await client.get("/api/v1/security/audit-log", params={"action": "security.mode_changed"})
    assert any(
        entry["details"].get("source") == "schedule" and entry["details"].get("to") == "away"
        for entry in audit.json()
    )

    async with SessionLocal() as session:
        await _clear_schedules(session)


@pytest.mark.asyncio
async def test_manual_override_holds_until_the_next_boundary(client):
    async with SessionLocal() as session:
        await _clear_schedules(session)
        await arming_schedules.create_schedule(
            session,
            name="daily night",
            mode="night",
            days_of_week=[0, 1, 2, 3, 4, 5, 6],
            start_time="00:00",
            end_time="00:00",
        )
        await arming_schedules.apply_due_transition(session)
        assert await security_modes.get_mode(session) == "night"

        # Human disarms. The override must survive every subsequent tick
        # until the schedule's next boundary.
        await security_modes.set_mode(session, "disarmed", changed_by=None)
        assert await arming_schedules.apply_due_transition(session) is None
        assert await security_modes.get_mode(session) == "disarmed"

        # Once the next boundary passes, the schedule reclaims the house.
        tomorrow = datetime.now(timezone.utc) + timedelta(days=1, hours=1)
        applied = await arming_schedules.apply_due_transition(session, now=tomorrow)
        assert applied is not None
        assert await security_modes.get_mode(session) == "night"

        await _clear_schedules(session)


@pytest.mark.asyncio
async def test_mode_endpoint_reports_schedule_context(client):
    async with SessionLocal() as session:
        await _clear_schedules(session)
        await arming_schedules.create_schedule(
            session,
            name="daily night",
            mode="night",
            days_of_week=[0, 1, 2, 3, 4, 5, 6],
            start_time="00:00",
            end_time="00:00",
        )

    await client.put("/api/v1/security/mode", json={"mode": "disarmed"})
    body = (await client.get("/api/v1/security/mode")).json()
    assert body["schedule"]["scheduled_mode"] == "night"
    assert body["schedule"]["override_active"] is True
    assert body["schedule"]["next_transition_at"]
    assert body["changed_source"] == "manual"

    async with SessionLocal() as session:
        await _clear_schedules(session)


@pytest.mark.asyncio
async def test_integration_endpoint_requires_a_configured_token(anonymous_client, monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings, "security_integration_token", None)
    disabled = await anonymous_client.post(
        "/api/v1/security/mode/integration", json={"mode": "away"}, headers={"X-HomeCam-Token": "anything"}
    )
    assert disabled.status_code == 503

    monkeypatch.setattr(settings, "security_integration_token", "test-token-value")
    missing = await anonymous_client.post("/api/v1/security/mode/integration", json={"mode": "away"})
    assert missing.status_code == 401
    wrong = await anonymous_client.post(
        "/api/v1/security/mode/integration", json={"mode": "away"}, headers={"X-HomeCam-Token": "nope"}
    )
    assert wrong.status_code == 401

    accepted = await anonymous_client.post(
        "/api/v1/security/mode/integration",
        json={"mode": "away", "source": "home-assistant:everyone-left"},
        headers={"X-HomeCam-Token": "test-token-value"},
    )
    assert accepted.status_code == 200
    assert accepted.json()["mode"] == "away"
    assert accepted.json()["changed_source"] == "integration"


@pytest.mark.asyncio
async def test_integration_mode_change_is_audited(client, monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings, "security_integration_token", "test-token-value")
    await client.post(
        "/api/v1/security/mode/integration",
        json={"mode": "home", "source": "home-assistant:arrived"},
        headers={"X-HomeCam-Token": "test-token-value"},
    )
    audit = await client.get("/api/v1/security/audit-log", params={"action": "security.mode_changed"})
    assert any(entry["actor_label"] == "home-assistant:arrived" for entry in audit.json())
    await client.put("/api/v1/security/mode", json={"mode": "disarmed"})


@pytest.mark.asyncio
async def test_scheduler_tick_is_safe_with_no_schedules():
    from app.services.arming_scheduler import arming_scheduler

    async with SessionLocal() as session:
        await _clear_schedules(session)
    assert await arming_scheduler.tick() is None
