"""Enforced retention: what gets purged, and what must never be."""
import uuid
from datetime import datetime, timedelta, timezone

import pytest

from app.config import settings
from app.db import SessionLocal
from app.models.db import (
    AIAnalysis,
    AuditLog,
    Event,
    EventEvidence,
    EventPhoto,
    Incident,
)
from app.services import retention


def _old(days: int) -> datetime:
    return datetime.now(timezone.utc) - timedelta(days=days)


async def _make_event(session, *, age_days: int, hold: bool = False, with_media: bool = True) -> str:
    event_id = f"retention-{uuid.uuid4().hex[:12]}"
    when = _old(age_days)
    session.add(
        Event(
            id=event_id,
            camera_id="retention-cam",
            type="person",
            source="test",
            start_time=when,
            description="retention fixture",
            event_metadata={},
            tags=[],
            retention_hold=hold,
        )
    )
    if with_media:
        session.add(EventPhoto(event_id=event_id, image=b"jpeg-bytes", created_at=when))
        session.add(
            EventEvidence(event_id=event_id, label="before", image=b"jpeg-bytes", created_at=when)
        )
        session.add(
            AIAnalysis(
                id=f"an-{uuid.uuid4().hex[:12]}",
                event_id=event_id,
                provider="test",
                model="test-model",
                summary="old analysis",
                embedding=[0.1, 0.2],
                embedding_dimensions=2,
                created_at=when,
            )
        )
    await session.commit()
    return event_id


async def _cleanup(session, event_ids: list[str]) -> None:
    from sqlalchemy import delete

    await session.execute(delete(EventPhoto).where(EventPhoto.event_id.in_(event_ids)))
    await session.execute(delete(EventEvidence).where(EventEvidence.event_id.in_(event_ids)))
    await session.execute(delete(AIAnalysis).where(AIAnalysis.event_id.in_(event_ids)))
    await session.execute(delete(Event).where(Event.id.in_(event_ids)))
    await session.commit()


@pytest.mark.asyncio
async def test_dry_run_counts_without_deleting(api_transport):
    async with SessionLocal() as session:
        event_id = await _make_event(session, age_days=400)
        report = await retention.run(session, dry_run=True)
        assert report.dry_run is True
        assert report.counts["events"] >= 1
        assert report.counts["media"] >= 2
        assert await session.get(Event, event_id) is not None
        assert await session.get(EventPhoto, event_id) is not None
        await _cleanup(session, [event_id])


@pytest.mark.asyncio
async def test_purge_removes_old_events_and_their_children(api_transport):
    async with SessionLocal() as session:
        event_id = await _make_event(session, age_days=400)
        await retention.run(session, dry_run=False)
        assert await session.get(Event, event_id) is None
        assert await session.get(EventPhoto, event_id) is None
        assert await session.get(EventEvidence, (event_id, "before")) is None


@pytest.mark.asyncio
async def test_recent_events_are_untouched(api_transport):
    async with SessionLocal() as session:
        event_id = await _make_event(session, age_days=1)
        await retention.run(session, dry_run=False)
        assert await session.get(Event, event_id) is not None
        await _cleanup(session, [event_id])


@pytest.mark.asyncio
async def test_held_events_outrank_every_cutoff(api_transport):
    async with SessionLocal() as session:
        held = await _make_event(session, age_days=5000, hold=True)
        await retention.run(session, dry_run=False)
        assert await session.get(Event, held) is not None
        assert await session.get(EventPhoto, held) is not None, "a hold must protect the imagery too"
        await _cleanup(session, [held])


@pytest.mark.asyncio
async def test_evidence_of_an_unresolved_incident_is_never_purged(api_transport):
    async with SessionLocal() as session:
        event_id = await _make_event(session, age_days=400)
        incident_id = f"inc-{uuid.uuid4().hex[:12]}"
        now = datetime.now(timezone.utc)
        session.add(
            Incident(
                id=incident_id,
                kind="intrusion",
                status="open",
                severity="high",
                camera_id="retention-cam",
                mode_at_creation="away",
                event_ids=[event_id],
                event_count=1,
                first_seen_at=_old(400),
                last_seen_at=_old(400),
                summary="old but unresolved",
                created_at=_old(400),
                updated_at=now,
            )
        )
        await session.commit()

        report = await retention.run(session, dry_run=False)
        assert report.protected["events_in_incidents"] >= 1
        assert await session.get(Event, event_id) is not None
        assert await session.get(EventPhoto, event_id) is not None
        # An unresolved incident is never aged out, however old it is.
        assert await session.get(Incident, incident_id) is not None

        from sqlalchemy import delete

        await session.execute(delete(Incident).where(Incident.id == incident_id))
        await session.commit()
        await _cleanup(session, [event_id])


@pytest.mark.asyncio
async def test_resolved_incidents_age_out_and_release_their_events(api_transport):
    async with SessionLocal() as session:
        event_id = await _make_event(session, age_days=400)
        incident_id = f"inc-{uuid.uuid4().hex[:12]}"
        session.add(
            Incident(
                id=incident_id,
                kind="intrusion",
                status="resolved",
                severity="low",
                camera_id="retention-cam",
                mode_at_creation="away",
                event_ids=[event_id],
                event_count=1,
                first_seen_at=_old(400),
                last_seen_at=_old(400),
                resolved_at=_old(399),
                summary="long resolved",
                created_at=_old(400),
                updated_at=_old(399),
            )
        )
        await session.commit()

        # First pass drops the incident; its events are still protected
        # because the protected set is read before the deletion commits.
        await retention.run(session, dry_run=False)
        assert await session.get(Incident, incident_id) is None

        # The next pass is free to purge the now-unreferenced events.
        await retention.run(session, dry_run=False)
        assert await session.get(Event, event_id) is None


@pytest.mark.asyncio
async def test_audit_log_is_kept_longer_than_imagery(api_transport):
    async with SessionLocal() as session:
        keep_id = f"audit-{uuid.uuid4().hex[:12]}"
        drop_id = f"audit-{uuid.uuid4().hex[:12]}"
        session.add(AuditLog(id=keep_id, action="test.kept", details={}, created_at=_old(100)))
        session.add(AuditLog(id=drop_id, action="test.dropped", details={}, created_at=_old(900)))
        await session.commit()

        await retention.run(session, dry_run=False)
        # 100 days is past the 30-day event cutoff but well inside 365.
        assert await session.get(AuditLog, keep_id) is not None
        assert await session.get(AuditLog, drop_id) is None

        from sqlalchemy import delete

        await session.execute(delete(AuditLog).where(AuditLog.id == keep_id))
        await session.commit()


@pytest.mark.asyncio
async def test_batching_is_bounded_and_reports_truncation(api_transport, monkeypatch):
    monkeypatch.setattr(settings, "retention_batch_size", 1)
    monkeypatch.setattr(settings, "retention_max_batches_per_run", 1)
    async with SessionLocal() as session:
        ids = [await _make_event(session, age_days=400, with_media=False) for _ in range(3)]
        report = await retention.run(session, dry_run=False)
        assert report.counts["events"] == 1
        assert report.truncated is True
        remaining = [i for i in ids if await session.get(Event, i) is not None]
        assert len(remaining) == 2
        await _cleanup(session, ids)


@pytest.mark.asyncio
async def test_embeddings_are_cleared_before_the_analysis_is_deleted(api_transport, monkeypatch):
    monkeypatch.setattr(settings, "retention_embedding_days", 10)
    monkeypatch.setattr(settings, "retention_ai_analysis_days", 3650)
    monkeypatch.setattr(settings, "retention_event_days", 3650)
    monkeypatch.setattr(settings, "retention_media_days", 3650)
    async with SessionLocal() as session:
        event_id = await _make_event(session, age_days=40)
        await retention.run(session, dry_run=False)
        from sqlalchemy import select

        row = (
            await session.execute(select(AIAnalysis).where(AIAnalysis.event_id == event_id))
        ).scalar_one()
        assert row.embedding_dimensions == 0
        assert row.embedding == []
        assert row.summary == "old analysis", "the text must survive; only the vector goes"
        await _cleanup(session, [event_id])


def test_reported_retention_days_tracks_config(monkeypatch):
    monkeypatch.setattr(settings, "retention_event_days", 45)
    monkeypatch.setattr(settings, "retention_media_days", 14)
    assert retention.reported_retention_days() == 14


@pytest.mark.asyncio
async def test_settings_endpoint_reports_configured_retention(client, monkeypatch):
    monkeypatch.setattr(settings, "retention_event_days", 60)
    monkeypatch.setattr(settings, "retention_media_days", 60)
    body = (await client.get("/api/v1/settings")).json()
    assert body["retention_days"] == 60
    assert body["retention"]["event_days"] == 60
    assert body["retention"]["audit_days"] == settings.retention_audit_days


@pytest.mark.asyncio
async def test_admin_retention_endpoint_reports_policy_and_dry_run_counts(client):
    body = (await client.get("/api/v1/admin/retention")).json()
    assert body["policy"]["audit_days"] == settings.retention_audit_days
    assert body["report"]["dry_run"] is True
    assert set(retention.CATEGORIES) <= set(body["report"]["counts"])


@pytest.mark.asyncio
async def test_admin_purge_defaults_to_dry_run_and_is_audited(client):
    async with SessionLocal() as session:
        event_id = await _make_event(session, age_days=400, with_media=False)

    dry = await client.post("/api/v1/admin/retention/purge", json={})
    assert dry.status_code == 200
    assert dry.json()["report"]["dry_run"] is True
    async with SessionLocal() as session:
        assert await session.get(Event, event_id) is not None

    real = await client.post("/api/v1/admin/retention/purge", json={"dry_run": False})
    assert real.json()["report"]["dry_run"] is False
    async with SessionLocal() as session:
        assert await session.get(Event, event_id) is None

    audit = await client.get("/api/v1/security/audit-log", params={"action": "retention.purged"})
    assert audit.json()


@pytest.mark.asyncio
async def test_retention_hold_endpoint_marks_and_clears(client):
    async with SessionLocal() as session:
        event_id = await _make_event(session, age_days=400, with_media=False)

    held = await client.put(f"/api/v1/events/{event_id}/retention-hold", json={"hold": True})
    assert held.status_code == 200
    assert held.json()["retention_hold"] is True

    async with SessionLocal() as session:
        await retention.run(session, dry_run=False)
        assert await session.get(Event, event_id) is not None

    cleared = await client.put(f"/api/v1/events/{event_id}/retention-hold", json={"hold": False})
    assert cleared.json()["retention_hold"] is False

    audit = await client.get("/api/v1/security/audit-log", params={"action": "retention.hold_set"})
    assert any(entry["target_id"] == event_id for entry in audit.json())

    async with SessionLocal() as session:
        await _cleanup(session, [event_id])


@pytest.mark.asyncio
async def test_scheduler_tick_logs_a_dry_run_report(api_transport, monkeypatch):
    from app.services.retention_scheduler import retention_scheduler

    monkeypatch.setattr(settings, "retention_dry_run", True)
    payload = await retention_scheduler.tick()
    assert payload["dry_run"] is True
    assert set(retention.CATEGORIES) <= set(payload["counts"])
