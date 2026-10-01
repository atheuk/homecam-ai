"""Conservative, explainable behaviour signals; no identity or demographic inference."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import uuid
import base64
import logging

import httpx

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..config import settings
from ..models.db import Event, Person
from ..ai.detector import Detection
from ..ai.zones import Zone, intersection_area
from .persons import trust_of

BEHAVIOURS = {
    "facing_property": "stood facing the property",
    "looking_into_windows": "looked into windows or vehicles",
    "pacing": "paced back and forth",
    "taking_photos": "took photos of the property",
    "trying_handles": "tried a door or vehicle handle",
    "hands_at_vehicle": "hands near a vehicle door or window",
}
CONCEALMENT = ("hood up", "face covered", "balaclava")
logger = logging.getLogger(__name__)


async def assess_behaviour(image: bytes) -> list[str]:
    """One bounded Foundry call; only allowlisted visible actions survive parsing."""
    if not settings.foundry_endpoint or not settings.foundry_api_key:
        return []
    url = (f"{settings.foundry_endpoint.rstrip('/')}/openai/deployments/"
           f"{settings.foundry_vision_deployment}/chat/completions"
           f"?api-version={settings.foundry_vision_api_version}")
    payload = {
        "messages": [
            {"role": "system", "content": (
                "Assess only clearly visible actions in this camera image. Do not infer intent, "
                "identity, age, gender, ethnicity, or any protected attribute. "
                "If an action is not visually evident, return false for it."
            )},
            {"role": "user", "content": [
                {"type": "text", "text": "Which of these are clearly visible?"},
                {"type": "image_url", "image_url": {
                    "url": "data:image/jpeg;base64," + base64.b64encode(image).decode("ascii")
                }},
            ]},
        ],
        "response_format": {"type": "json_schema", "json_schema": {
            "name": "observable_behaviour", "strict": True,
            "schema": {"type": "object", "additionalProperties": False,
                       "properties": {key: {"type": "boolean"} for key in BEHAVIOURS},
                       "required": list(BEHAVIOURS)},
        }},
        "max_completion_tokens": 150,
    }
    try:
        async with httpx.AsyncClient(timeout=settings.foundry_timeout_seconds) as client:
            response = await client.post(url, json=payload, headers={"api-key": settings.foundry_api_key})
        response.raise_for_status()
        import json
        answer = json.loads(response.json()["choices"][0]["message"]["content"])
        return [key for key in BEHAVIOURS if answer.get(key) is True]
    except (httpx.HTTPError, KeyError, IndexError, ValueError) as exc:
        logger.warning("behaviour assessment unavailable: %s", exc)
        return []


def score(
    signals: dict,
    *,
    clothing: str | None = None,
    mode: str | None = None,
    night: bool = False,
    unusual: bool = False,
) -> dict:
    """Only allowlisted observations enter reasons (never model-supplied prose)."""
    reasons: list[str] = []
    value = 0.0
    dwell = float(signals.get("vehicle_seconds") or 0)
    if dwell >= settings.suspicious_vehicle_dwell_seconds:
        value += 3
        reasons.append(f"lingered {round(dwell)}s beside a parked vehicle")
        if signals.get("circling"):
            value += 1
            reasons.append("moved around multiple sides of the vehicle")
    dwell = float(signals.get("mailbox_seconds") or 0)
    if dwell >= settings.suspicious_mailbox_dwell_seconds and not signals.get("mailbox_outcome"):
        value += 3
        reasons.append(f"lingered {round(dwell)}s at the mailbox with no delivery or retrieval")
    dwell = float(signals.get("property_seconds") or 0)
    if dwell >= settings.suspicious_property_dwell_seconds:
        value += 2
        reasons.append(f"stood {round(dwell)}s in a street-facing property zone")
    for key in signals.get("behaviours", []):
        if key in BEHAVIOURS and (key != "hands_at_vehicle" or signals.get("vehicle_seconds")):
            value += 1.5 if key in {"trying_handles", "hands_at_vehicle"} else 0.75
            reasons.append(BEHAVIOURS[key])
    visits = int(signals.get("return_visits") or 0)
    if visits >= (settings.suspicious_night_return_visits if night else settings.suspicious_return_visits):
        value += 3
        period = "within 2 hours at night" if night else "today"
        reasons.append(f"possibly the same unrecognised person returned {visits} times {period}")
    behavioural = bool(reasons)
    if behavioural:
        lower = (clothing or "").casefold()
        cue = next((word for word in CONCEALMENT if word in lower), None)
        if cue and settings.suspicious_clothing_weight:
            value += settings.suspicious_clothing_weight
            reasons.append(f"{cue} (contributing)")
        if night:
            value *= 1.25
            reasons.append("night-time")
        if mode in {"away", "night"}:
            value *= 1.25
            reasons.append(f"armed {mode}")
        if unusual:
            value += 0.75
            reasons.append("unusual activity for this time")
    level = (
        "suspicious" if value >= settings.suspicious_incident_score
        else "elevated" if value >= settings.suspicious_elevated_score else None
    )
    return {"score": round(value, 2), "level": level, "reasons": reasons,
            "evidence_event_ids": list(signals.get("evidence_event_ids") or []),
            "appearance_confidence": signals.get("appearance_confidence")}


async def returning_visits(session: AsyncSession, row: Event, at: datetime, night: bool) -> dict:
    """Count distinct visits, not frames, of an unnamed untrusted appearance cluster."""
    if not row.person_id:
        return {}
    person = await session.get(Person, row.person_id)
    if person is None or person.name or trust_of(person) != "unknown":
        return {}
    if row.person_confidence is not None and row.person_confidence < settings.person_match_threshold:
        return {}
    window = timedelta(hours=settings.suspicious_night_return_window_hours if night
                       else settings.suspicious_return_window_hours)
    result = await session.execute(
        select(Event).where(
            Event.person_id == row.person_id,
            Event.start_time >= at - window,
            Event.start_time <= at,
            Event.id != row.id,
        ).order_by(Event.start_time.asc())
    )
    visits: list[Event] = []
    last_seen: datetime | None = None
    for event in [*result.scalars().all(), row]:
        seen = event.start_time.replace(tzinfo=timezone.utc) if event.start_time.tzinfo is None else event.start_time
        if last_seen is None or seen - last_seen > timedelta(seconds=settings.suspicious_visit_gap_seconds):
            visits.append(event)
        last_seen = seen
    if len(visits) < (settings.suspicious_night_return_visits if night else settings.suspicious_return_visits):
        return {}
    return {"return_visits": len(visits), "evidence_event_ids": [event.id for event in visits],
            "appearance_confidence": row.person_confidence}


def observe(scene, detections: list[Detection], zones: list[Zone], now: float) -> list:
    """Advance lease-fenced scene records, one per spatial person track."""
    from .scene_state import SceneTransition, ZoneRecord, _boost_sampling

    if not settings.suspicious_enabled:
        return []
    transitions = []
    records = [r for r in scene.zones.values() if r.kind == "suspicious"]
    used: set[str] = set()
    for person in (d for d in detections if d.label == "person"):
        box = person.bbox
        cx, cy = (box.x1 + box.x2) / 2, (box.y1 + box.y2) / 2
        candidates = [
            (abs(cx - r.data["center"][0]) + abs(cy - r.data["center"][1]), r)
            for r in records
            if r.zone_id not in used and 0 <= now - r.data["last"] <= settings.suspicious_gap_seconds
        ]
        nearest = min(candidates, key=lambda pair: pair[0]) if candidates else None
        if nearest is not None and nearest[0] < 0.3:
            record = nearest[1]
        else:
            key = "sus-" + uuid.uuid4().hex[:16]
            record = ZoneRecord(id=f"{scene.camera_id}|{key}", camera_id=scene.camera_id,
                                kind="suspicious", zone_id=key, state="observing",
                                data={"first": now, "last": now, "center": [cx, cy],
                                      "vehicle_since": None, "mailbox_since": None,
                                      "property_since": None, "sides": [], "alert_at": None})
            scene.zones[key] = record
            records.append(record)
        used.add(record.zone_id)
        data = dict(record.data)
        nearby = [
            track for track in scene.tracks.values()
            if (track.state == "stable" or track.data.get("interacted")) and box.area > 0 and
            intersection_area(box, track.box) / box.area >= 0.05
        ]
        # Near (not necessarily overlapping) the parked vehicle.
        nearby += [
            track for track in scene.tracks.values()
            if (track.state == "stable" or track.data.get("interacted")) and track not in nearby and
            abs(cx - (track.box.x1 + track.box.x2) / 2) < (track.box.x2 - track.box.x1) / 2 + 0.08
            and abs(cy - (track.box.y1 + track.box.y2) / 2) < (track.box.y2 - track.box.y1) / 2 + 0.1
        ]
        if nearby:
            _boost_sampling(scene.camera_id)
        property_zone = next((z for z in zones if z.kind.casefold() in
                             {"driveway", "perimeter", "street", "entry"} and
                             intersection_area(box, z.bbox) > 0), None)
        for field, present in (("vehicle_since", bool(nearby)),
                               ("property_since", property_zone is not None)):
            data[field] = (data.get(field) or now) if present else None
        if nearby:
            car = nearby[0].box
            side = "left" if cx < car.x1 else "right" if cx > car.x2 else "front"
            data["sides"] = list(set([*data.get("sides", []), side]))
        else:
            data["sides"] = []
        data["center"] = [cx, cy]
        data["last"] = now
        record.data = data
        scene.dirty_zones.add(record.zone_id)
        evidence = {
            "vehicle_seconds": now - data["vehicle_since"] if data["vehicle_since"] else 0,
            "property_seconds": now - data["property_since"] if data["property_since"] else 0,
            "circling": len(data["sides"]) >= 2,
        }
        result = score(evidence)
        if result["level"] and (data["alert_at"] is None or
                                now - data["alert_at"] >= settings.suspicious_dedupe_seconds):
            data["alert_at"] = now
            record.data = data
            zone_name = property_zone.name if property_zone else nearby[0].zone if nearby else next(
                (z.name for z in zones if z.kind.casefold() == "mailbox"), None)
            transitions.append(SceneTransition(
                camera_id=scene.camera_id, kind="suspicious", transition="lingering",
                event_type="suspicious_activity", description="Unusual lingering activity observed.",
                tags=["suspicious_activity"], metadata={"suspicious_signals": evidence},
                zone=zone_name, dedup_key=f"suspicious:{scene.camera_id}:{record.zone_id}",
                dedup_window_seconds=settings.suspicious_dedupe_seconds, observed_at=now,
            ))
    # A departed person must not leave unbounded persistent records behind.
    for record in records:
        if now - record.data["last"] > max(settings.suspicious_dedupe_seconds, settings.suspicious_gap_seconds):
            scene.zones.pop(record.zone_id, None)
            scene.deleted_zones.add(record.zone_id)
            scene.dirty_zones.discard(record.zone_id)
    return transitions
