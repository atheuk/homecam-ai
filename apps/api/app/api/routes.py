"""Versioned public API routes (SPEC section 33).

Camera and provider data always flows through the provider registry so
provider failures are isolated; camera/event state is persisted via the
services layer so it survives beyond the in-process provider objects.
"""
from __future__ import annotations

import base64
import binascii
import asyncio
import ipaddress
import json
import os
import time
from datetime import datetime, timezone
from urllib.parse import urlsplit, urlunsplit

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import Response, StreamingResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..auth.dependencies import get_current_auth_session, get_current_user
from ..config import settings
from ..db import SessionLocal, get_db
from ..models.db import AIAnalysis, AuthSession, Event, EventEvidence, EventPhoto, Person, User
from ..providers.base import CameraNotFoundError, CameraOfflineError, ProviderUnavailableError
from ..providers.capabilities import AUDIO_DETECTION
from ..schemas import (
    AudioAnalysisOut,
    CameraBatteryIn,
    CameraStatusIn,
    MockAudioIn,
    MockEventIn,
    PersonAssignIn,
    PersonMergeIn,
    PersonUpdateIn,
    PhotoRatingIn,
    ProviderOutageIn,
)
from ..ai.audio import analyze_pcm
from ..ai.detector import SUPPORTED_BACKENDS, detector_status
from ..ai.vision import get_image_embedder
from ..services import activities as activity_service
from ..services import cameras as camera_service
from ..services import detector_watchdog
from ..services import digest as digest_service
from ..services import events as event_service
from ..services import persons as person_service
from ..services import search as search_service
from ..services.ingestion import frame_stats as ingestion_service_stats
from ..services.stream_frames import stream_hub
from ..services.provider_registry import (
    active_mock_providers,
    discover_all_cameras,
    find_provider_for_camera,
    get_all_provider_health,
    hidden_provider_ids,
)

# Every API-v1 route in this router exposes household state or can mutate it.
# Keep authentication at the router boundary so new routes fail closed too.
router = APIRouter(prefix="/api/v1", dependencies=[Depends(get_current_user)])
SSE_AUTH_RECHECK_INTERVAL_SECONDS = 15


async def find_mock_provider_for_camera(camera_id: str):
    return next(
        (provider for provider in await active_mock_providers() if provider.has_camera(camera_id)), None
    )


def _classify_stream_url(stream_url: str) -> tuple[str, bool]:
    """Classify a provider-returned stream URL for safe browser rendering.

    Never used to sanitize credentials out of a URL: providers must not
    return credentialed URLs to this endpoint in the first place (direct
    Dahua mode returns a plain ``rtsp://host/...`` with no embedded auth;
    edge mode never returns raw RTSP at all). This only tells the frontend
    which kind of player (if any) is safe/possible to use.
    """
    lowered = stream_url.lower()
    if lowered.startswith("rtsp://"):
        return "rtsp", False
    if ".m3u8" in lowered or lowered.startswith("hls:"):
        return "hls", True
    if "webrtc" in lowered or lowered.startswith("whep:") or lowered.startswith("whip:"):
        return "webrtc", True
    if lowered.startswith("http://") or lowered.startswith("https://"):
        return "link", True
    return "unknown", False


def _is_private_host(url: str) -> bool:
    """Whether ``url``'s host is only reachable from inside the private
    overlay network (Tailscale MagicDNS, ``localhost``, or an RFC1918/
    loopback/link-local address) and therefore unreachable by a real
    browser, which is never on the tailnet."""
    host = urlsplit(url).hostname
    if not host:
        return True
    if host == "localhost" or host.endswith(".ts.net"):
        return True
    try:
        addr = ipaddress.ip_address(host)
    except ValueError:
        return False
    return addr.is_private or addr.is_loopback or addr.is_link_local


# Cache of the last-resolved upstream live-stream URL per camera. HLS
# playback polls the manifest and fetches several segments per interval;
# without this cache each of those requests would re-trigger a live-stream
# lookup against the (often capacity-limited) edge connector/provider.
_LIVE_STREAM_CACHE_TTL_SECONDS = 30.0
_live_stream_cache: dict[str, tuple[float, str]] = {}


async def _cached_live_stream(provider, camera_id: str) -> str:
    cached = _live_stream_cache.get(camera_id)
    now = time.monotonic()
    if cached is not None and now - cached[0] < _LIVE_STREAM_CACHE_TTL_SECONDS:
        return cached[1]
    url = await provider.get_live_stream(camera_id)
    _live_stream_cache[camera_id] = (now, url)
    return url


def _resolve_hls_target(upstream_manifest_url: str, path: str, query: str) -> str:
    """Map an incoming proxy path onto the upstream HLS URL.

    Two upstream shapes have to work:

    * directory-style (MediaMTX): ``http://host:8888/dahua-1/index.m3u8``,
      where sub-playlists and segments are siblings of the manifest;
    * query-style (go2rtc): ``http://host:1984/api/stream.m3u8?src=eufy-X``,
      where the manifest name and its query string both matter.

    ``/live`` always advertises ``index.m3u8`` as the entry point, so that
    name is resolved back to the real upstream manifest URL verbatim
    instead of being appended to a directory. Any query string on a child
    request is forwarded, because go2rtc identifies sub-playlists and
    segments with opaque query parameters rather than path segments.
    """
    if not path or path == "index.m3u8":
        return upstream_manifest_url
    parts = urlsplit(upstream_manifest_url)
    base_dir = parts.path.rsplit("/", 1)[0]
    return urlunsplit((parts.scheme, parts.netloc, f"{base_dir}/{path}", query, ""))


def _rewrite_hls_manifest(text: str) -> str:
    """Rewrite any absolute upstream URLs in an HLS manifest to bare
    filenames, so relative resolution against our own proxy route (rather
    than the private upstream host) is used for sub-playlists/segments.
    Manifests that already use relative references (the common case) are
    returned unchanged."""
    out_lines = []
    for line in text.splitlines():
        if line and not line.startswith("#") and "://" in line:
            line = line.rsplit("/", 1)[-1]
        out_lines.append(line)
    return "\n".join(out_lines) + "\n"


@router.get("/providers")
async def providers():
    return await get_all_provider_health()


@router.get("/providers/{provider_id}")
async def provider_detail(provider_id: str):
    for health in await get_all_provider_health():
        if health["provider_id"] == provider_id:
            return health
    raise HTTPException(404, "Provider not found")


@router.get("/cameras")
async def cameras(session: AsyncSession = Depends(get_db)):
    discovered = await discover_all_cameras(settings.camera_discovery_cache_seconds)
    await camera_service.sync_cameras(session, discovered)
    rows = await camera_service.list_cameras(session)
    hidden = await hidden_provider_ids()
    return [
        {
            "id": row.id, "provider_id": row.provider_id, "name": row.name, "type": row.type,
            "model": row.model, "online": row.online, "status": row.status,
            "battery_level": row.battery_level, "capabilities": row.capabilities,
        }
        for row in rows
        if row.provider_id not in hidden
    ]


@router.get("/cameras/{camera_id}")
async def camera(camera_id: str, session: AsyncSession = Depends(get_db)):
    row = await camera_service.get_camera(session, camera_id)
    if row is None or row.provider_id in await hidden_provider_ids():
        raise HTTPException(404, "Camera not found")
    return {
        "id": row.id, "provider_id": row.provider_id, "name": row.name, "type": row.type,
        "model": row.model, "online": row.online, "status": row.status,
        "battery_level": row.battery_level, "capabilities": row.capabilities,
    }


@router.get("/cameras/{camera_id}/capabilities")
async def camera_capabilities(camera_id: str):
    provider = await find_provider_for_camera(camera_id)
    if provider is None:
        raise HTTPException(404, "Camera not found")
    try:
        return await provider.get_capabilities(camera_id)
    except CameraNotFoundError as exc:
        raise HTTPException(404, "Camera not found") from exc
    except ProviderUnavailableError as exc:
        raise HTTPException(503, f"Provider '{provider.id}' is currently unavailable") from exc


@router.get("/cameras/{camera_id}/snapshot")
async def snapshot(camera_id: str):
    # A fresh frame already decoded from the camera's sub-stream costs the
    # NVR nothing; asking snapshot.cgi competes with ingestion for its ~1-2
    # CGI sessions. Only readers ingestion already runs are consulted.
    cached = stream_hub.latest(camera_id)
    if cached is not None:
        return Response(cached.frame, media_type="image/jpeg", headers={"X-Frame-Source": "stream"})
    provider = await find_provider_for_camera(camera_id)
    if provider is None:
        raise HTTPException(404, "Camera not found")
    try:
        image = await provider.get_snapshot(camera_id)
    except CameraNotFoundError as exc:
        raise HTTPException(404, "Camera not found") from exc
    except CameraOfflineError as exc:
        raise HTTPException(503, f"Camera '{camera_id}' is currently offline") from exc
    except ProviderUnavailableError as exc:
        raise HTTPException(503, f"Provider '{provider.id}' is currently unavailable") from exc
    stream_hub.note_snapshot(camera_id, image)
    return Response(image, media_type="image/jpeg", headers={"X-Frame-Source": "snapshot"})


@router.get("/cameras/{camera_id}/live")
async def live(camera_id: str, request: Request):
    provider = await find_provider_for_camera(camera_id)
    if provider is None:
        raise HTTPException(404, "Camera not found")
    try:
        stream_url = await _cached_live_stream(provider, camera_id)
    except CameraNotFoundError as exc:
        raise HTTPException(404, "Camera not found") from exc
    except CameraOfflineError as exc:
        raise HTTPException(503, f"Camera '{camera_id}' is currently offline") from exc
    except ProviderUnavailableError as exc:
        raise HTTPException(503, f"Provider '{provider.id}' is currently unavailable") from exc
    kind, browser_playable = _classify_stream_url(stream_url)
    public_url = stream_url
    if kind == "hls" and _is_private_host(stream_url):
        # The provider's stream URL is only reachable over the private
        # overlay network (Tailscale tailnet, or a container-local address).
        # A real browser has no route to it, so hand back our own public
        # HLS proxy path instead of the raw upstream URL.
        base = settings.public_api_base_url or f"https://{request.headers.get('host') or request.url.netloc}"
        public_url = f"{base.rstrip('/')}/api/v1/cameras/{camera_id}/hls/index.m3u8"
        browser_playable = True
    return {
        "camera_id": camera_id,
        "hls_url": public_url,
        "stream_url": public_url,
        "mode": provider.id,
        "provider_id": provider.id,
        "kind": kind,
        "browser_playable": browser_playable,
    }


@router.get("/cameras/{camera_id}/hls/{path:path}")
async def hls_proxy(camera_id: str, path: str, request: Request):
    """Public HLS relay for cameras whose real stream lives on the private
    overlay network. Streams the manifest/segments through the API's own
    (already tailnet-connected) network path so the browser only ever talks
    to the public Azure API domain, never a private tailnet/localhost host.
    """
    provider = await find_provider_for_camera(camera_id)
    if provider is None:
        raise HTTPException(404, "Camera not found")
    try:
        upstream_manifest_url = await _cached_live_stream(provider, camera_id)
    except CameraNotFoundError as exc:
        raise HTTPException(404, "Camera not found") from exc
    except CameraOfflineError as exc:
        raise HTTPException(503, f"Camera '{camera_id}' is currently offline") from exc
    except ProviderUnavailableError as exc:
        raise HTTPException(503, f"Provider '{provider.id}' is currently unavailable") from exc
    kind, _ = _classify_stream_url(upstream_manifest_url)
    if kind != "hls":
        raise HTTPException(409, f"Camera '{camera_id}' does not expose an HLS stream to proxy")
    target_url = _resolve_hls_target(upstream_manifest_url, path, request.url.query)

    client_options: dict = {"timeout": 10.0, "follow_redirects": True}
    proxy_url = os.environ.get("TAILSCALE_HTTP_PROXY")
    if proxy_url:
        client_options["proxy"] = proxy_url
    try:
        async with httpx.AsyncClient(**client_options) as client:
            upstream_response = await client.get(target_url)
    except (httpx.TimeoutException, httpx.TransportError) as exc:
        raise HTTPException(503, f"Provider '{provider.id}' is currently unavailable") from exc
    if upstream_response.status_code >= 400:
        raise HTTPException(
            upstream_response.status_code if upstream_response.status_code < 500 else 503,
            f"Upstream HLS resource for '{camera_id}' returned HTTP {upstream_response.status_code}",
        )
    content_type = upstream_response.headers.get("content-type") or "application/octet-stream"
    body = upstream_response.content
    if path.endswith(".m3u8") or "mpegurl" in content_type.lower():
        body = _rewrite_hls_manifest(body.decode("utf-8", errors="replace")).encode("utf-8")
        content_type = "application/vnd.apple.mpegurl"
    return Response(content=body, media_type=content_type)


@router.get("/events")
async def events(limit: int = 50, session: AsyncSession = Depends(get_db)):
    rows = await event_service.list_events(session, limit)
    return await _decorate_events(session, rows)


async def _decorate_events(session: AsyncSession, rows: list[Event]) -> list[dict]:
    """Serialize events with identity names and photo availability resolved.

    Person names are looked up once for the whole page rather than per row:
    the events list is the main screen, and an N+1 lookup there would be the
    slowest thing in the app.
    """
    payloads = [event_service.to_dict(row) for row in rows]
    person_ids = {row.person_id for row in rows if row.person_id}
    names: dict[str, dict] = {}
    if person_ids:
        result = await session.execute(select(Person).where(Person.id.in_(person_ids)))
        for person in result.scalars().all():
            names[person.id] = {
                "person_name": person.name,
                "person_display_name": person_service.display_name(person),
                "person_trust": person_service.trust_of(person),
            }
    photo_ids = set()
    event_ids = [row.id for row in rows]
    if event_ids:
        result = await session.execute(
            select(EventPhoto.event_id).where(EventPhoto.event_id.in_(event_ids))
        )
        photo_ids = set(result.scalars().all())
    for payload, row in zip(payloads, rows):
        payload.update(
            names.get(
                row.person_id or "",
                {"person_name": None, "person_display_name": None, "person_trust": None},
            )
        )
        payload["has_photo"] = row.id in photo_ids
        payload["photo_url"] = f"/api/v1/events/{row.id}/photo" if row.id in photo_ids else None
        payload["full_photo_url"] = (
            f"/api/v1/events/{row.id}/photo/full"
            if row.id in photo_ids and (row.event_metadata or {}).get("best_photo", {}).get("full_frame")
            else None
        )
    return payloads


@router.get("/search")
async def search(
    q: str,
    camera_id: str | None = None,
    since: datetime | None = None,
    until: datetime | None = None,
    limit: int = 20,
    session: AsyncSession = Depends(get_db),
    _user: User = Depends(get_current_user),
):
    """Natural-language event search ("blue van in the driveway last night").

    Requires sign-in: results carry AI descriptions, priority reasons and
    loitering/package-theft signals, the same class of data the incident
    endpoints protect. Queries asking *who* someone is are refused or
    stripped first - see :mod:`app.ai.query_moderation`.
    """
    if not settings.search_enabled:
        raise HTTPException(status_code=503, detail="Search is disabled")
    moderation, hits = await search_service.search_events(
        session, q, camera_id=camera_id, since=since, until=until, limit=limit
    )
    if moderation.refused:
        return {
            "query": q,
            "refused": True,
            "notice": moderation.message,
            "blocked_categories": list(moderation.categories),
            "results": [],
        }
    payloads = await _decorate_events(session, [hit.row for hit in hits])
    for payload, hit in zip(payloads, hits):
        payload["score"] = round(hit.score, 4)
        payload["semantic_score"] = round(hit.semantic_score, 4)
        payload["keyword_score"] = round(hit.keyword_score, 4)
    return {
        "query": moderation.query,
        "refused": False,
        "notice": moderation.message,
        "blocked_categories": list(moderation.categories),
        "results": payloads,
    }


@router.get("/digest")
async def digest(
    date: str | None = None,
    refresh: bool = False,
    session: AsyncSession = Depends(get_db),
    _user: User = Depends(get_current_user),
):
    """The day-in-review digest. Defaults to today (UTC).

    Requires sign-in, like the incident endpoints: the digest exposes
    incident ids and summaries.
    """
    if not settings.digest_enabled:
        raise HTTPException(status_code=503, detail="Digest is disabled")
    if date:
        try:
            day = datetime.strptime(date, "%Y-%m-%d").date()
        except ValueError:
            raise HTTPException(status_code=400, detail="date must be YYYY-MM-DD") from None
    else:
        day = datetime.now(timezone.utc).date()
    result = await digest_service.generate(session, day, refresh=refresh)
    return result.as_dict()


@router.get("/events/{event_id}/photo")
async def event_photo(
    event_id: str,
    session: AsyncSession = Depends(get_db),
    _user: User = Depends(get_current_user),
):
    """The stored best photo for an event, as real renderable image bytes.

    Served from the database rather than ``media_root`` because the deployed
    API's filesystem is per-replica and ephemeral; see
    ``app.models.db.EventPhoto``.
    """
    photo = await session.get(EventPhoto, event_id)
    if photo is None:
        raise HTTPException(404, "No photo stored for this event")
    return Response(
        content=photo.image,
        media_type=photo.content_type or "image/jpeg",
        headers={
            "Cache-Control": "private, no-store",
            "Content-Disposition": f'inline; filename="{event_id}.jpg"',
        },
    )


@router.get("/events/{event_id}/photo/full")
async def event_full_photo(
    event_id: str,
    session: AsyncSession = Depends(get_db),
    _user: User = Depends(get_current_user),
):
    """Original full-frame bytes corresponding to the selected event photo."""
    photo = await session.get(EventEvidence, (event_id, "full"))
    if photo is None:
        raise HTTPException(404, "No full-frame photo stored for this event")
    return Response(
        content=photo.image,
        media_type=photo.content_type or "image/jpeg",
        headers={
            "Cache-Control": "private, no-store",
            "Content-Disposition": f'inline; filename="{event_id}-full.jpg"',
        },
    )


@router.get("/events/{event_id}/evidence/{label}")
async def event_evidence(
    event_id: str,
    label: str,
    session: AsyncSession = Depends(get_db),
    _user: User = Depends(get_current_user),
):
    """A stored labelled evidence image for an event (e.g. package
    ``before``/``after`` crops referenced by ``Incident.evidence``).

    Authenticated because it is incident evidence, and served from the
    database so any replica can return it. See ``app.models.db.EventEvidence``.
    """
    row = await session.get(EventEvidence, (event_id, label))
    if row is None:
        raise HTTPException(404, "No evidence stored for this event and label")
    return Response(
        content=row.image,
        media_type=row.content_type or "image/jpeg",
        headers={
            "Cache-Control": "private, no-store",
            "Content-Disposition": f'inline; filename="{event_id}-{label}.jpg"',
        },
    )


@router.post("/events/{event_id}/rating")
async def rate_event_photo(
    event_id: str, payload: PhotoRatingIn, session: AsyncSession = Depends(get_db)
):
    """Rate how usable an event's photo is (1-5), or clear it with null."""
    row = await session.get(Event, event_id)
    if row is None:
        raise HTTPException(404, "Event not found")
    row.photo_rating = payload.rating
    row.photo_rating_at = datetime.now(timezone.utc) if payload.rating is not None else None

    # A highly-rated photo is the best available portrait of that identity,
    # so promote it to their cover image.
    if payload.rating is not None and payload.rating >= 4 and row.person_id:
        person = await session.get(Person, row.person_id)
        if person is not None:
            current_best = None
            if person.cover_event_id:
                cover = await session.get(Event, person.cover_event_id)
                current_best = cover.photo_rating if cover else None
            if current_best is None or payload.rating >= current_best:
                person.cover_event_id = row.id
    await session.commit()
    await session.refresh(row)
    return {"id": row.id, "photo_rating": row.photo_rating}


@router.post("/events/{event_id}/person")
async def assign_event_person(
    event_id: str, payload: PersonAssignIn, session: AsyncSession = Depends(get_db)
):
    """Say who is in an event; this is also how the matcher is taught."""
    row = await session.get(Event, event_id)
    if row is None:
        raise HTTPException(404, "Event not found")
    if not payload.person_id and not (payload.name and payload.name.strip()):
        raise HTTPException(400, "Provide either person_id or name")
    person = await person_service.assign_person(
        session, row, payload.person_id, payload.name
    )
    if person is None:
        raise HTTPException(404, "Person not found")
    await session.commit()
    await session.refresh(person)
    return person_service.to_dict(person)


@router.get("/persons")
async def persons(session: AsyncSession = Depends(get_db)):
    """Everyone HomeCam has grouped, named or not.

    ``recognition`` reports whether automatic re-identification is actually
    available: with no Foundry credentials the local fallback embedder cannot
    generalize across pose/lighting, and the UI must say so rather than imply
    recognition is working.
    """
    embedder = get_image_embedder()
    rows = await person_service.list_persons(session)
    counts = await person_service.sighting_counts(session)
    return {
        "recognition": {
            "enabled": settings.person_recognition_enabled,
            "backend": embedder.name,
            "semantic": embedder.semantic,
            "match_threshold": settings.person_match_threshold,
        },
        "persons": [person_service.to_dict(row, counts.get(row.id)) for row in rows],
    }


@router.get("/persons/{person_id}")
async def person_detail(person_id: str, session: AsyncSession = Depends(get_db)):
    person = await person_service.get_person(session, person_id)
    if person is None:
        raise HTTPException(404, "Person not found")
    events_seen = await person_service.events_for_person(session, person_id)
    payload = person_service.to_dict(person)
    payload["events"] = await _decorate_events(session, events_seen)
    return payload


@router.patch("/persons/{person_id}")
async def update_person(
    person_id: str, payload: PersonUpdateIn, session: AsyncSession = Depends(get_db)
):
    """Name (or rename) an identity. Retroactively labels every past sighting,
    because they were already clustered together before anyone was named."""
    person = await person_service.get_person(session, person_id)
    if person is None:
        raise HTTPException(404, "Person not found")
    if payload.name is not None:
        person.name = payload.name.strip()[:120] or None
    if payload.notes is not None:
        person.notes = payload.notes.strip()[:2000] or None
    if payload.trust is not None:
        # Human-declared only. Never inferred from what someone looks like.
        person.trust = person_service.normalize_trust(payload.trust)
    person.updated_at = datetime.now(timezone.utc)
    merged: list[str] = []
    if payload.name is not None and person.name:
        merged = await person_service.consolidate_identity(session, person)
    await session.commit()
    await session.refresh(person)
    result = person_service.to_dict(person)
    result["merged_person_ids"] = merged
    return result


@router.get("/persons/{person_id}/duplicates")
async def person_duplicates(person_id: str, session: AsyncSession = Depends(get_db)):
    """Other identities that look like the same person.

    Offered as a suggestion rather than applied automatically: the app can be
    confident enough to ask, without being confident enough to decide.
    """
    person = await person_service.get_person(session, person_id)
    if person is None:
        raise HTTPException(404, "Person not found")
    matches = await person_service.find_duplicates(session, person)
    return {
        "person_id": person.id,
        "candidates": [
            {**person_service.to_dict(candidate), "similarity": round(score, 4)}
            for candidate, score in matches
        ],
    }


@router.post("/persons/{person_id}/merge")
async def merge_persons(
    person_id: str, payload: PersonMergeIn, session: AsyncSession = Depends(get_db)
):
    """Fold ``source_id`` into this identity, keeping every sighting."""
    target = await person_service.get_person(session, person_id)
    source = await person_service.get_person(session, payload.source_id)
    if target is None or source is None:
        raise HTTPException(404, "Person not found")
    if target.id == source.id:
        raise HTTPException(400, "Cannot merge an identity into itself")
    await person_service.merge_person(session, target, source)
    await session.commit()
    await session.refresh(target)
    return {**person_service.to_dict(target), "merged_person_ids": [source.id]}


@router.get("/persons/{person_id}/photo")
async def person_photo(
    person_id: str,
    session: AsyncSession = Depends(get_db),
    _user: User = Depends(get_current_user),
):
    """Representative photo for an identity (their best-rated sighting)."""
    person = await person_service.get_person(session, person_id)
    if person is None:
        raise HTTPException(404, "Person not found")
    if not person.cover_event_id:
        raise HTTPException(404, "No photo stored for this person")
    photo = await session.get(EventPhoto, person.cover_event_id)
    if photo is None:
        raise HTTPException(404, "No photo stored for this person")
    return Response(
        content=photo.image,
        media_type=photo.content_type or "image/jpeg",
        headers={"Cache-Control": "private, no-store"},
    )


@router.get("/events/{event_id}")
async def event_detail(event_id: str, session: AsyncSession = Depends(get_db)):
    row = await session.get(Event, event_id)
    if row is None:
        raise HTTPException(404, "Event not found")
    payload = (await _decorate_events(session, [row]))[0]
    if row.ai_analysis_id:
        analysis = await session.get(AIAnalysis, row.ai_analysis_id)
        if analysis is not None:
            payload["ai_analysis"] = {
                "id": analysis.id,
                "provider": analysis.provider,
                "model": analysis.model,
                "summary": analysis.summary,
                "objects": analysis.objects,
                "actions": analysis.actions,
                "category": analysis.category,
                "confidence": analysis.confidence,
                "embedding_dimensions": analysis.embedding_dimensions,
                "detections": analysis.detections,
            }
    return payload


@router.get("/activities")
async def activities(limit: int = 50, session: AsyncSession = Depends(get_db)):
    """Cross-camera correlated activities (SPEC sections 18/19/33)."""
    rows = await activity_service.list_activities(session, limit)
    return [activity_service.to_dict(row) for row in rows]


@router.get("/activities/{activity_id}")
async def activity_detail(activity_id: str, session: AsyncSession = Depends(get_db)):
    row = await activity_service.get_activity(session, activity_id)
    if row is None:
        raise HTTPException(404, "Activity not found")
    payload = activity_service.to_dict(row)
    events_result = await event_service.list_events(session, 200)
    members = [event_service.to_dict(e) for e in events_result if e.id in set(payload["event_ids"])]
    payload["events"] = sorted(members, key=lambda item: item["start_time"])
    return payload


@router.post("/mock/events")
async def create_event(payload: MockEventIn, session: AsyncSession = Depends(get_db)):
    provider = await find_mock_provider_for_camera(payload.camera_id)
    if provider is None:
        raise HTTPException(404, "Camera not found")
    event = provider.event(payload.camera_id, payload.type)
    await event_service.create_and_broadcast_event(session, event)
    return event


@router.post("/mock/cameras/{camera_id}/status")
async def set_camera_status(camera_id: str, payload: CameraStatusIn, session: AsyncSession = Depends(get_db)):
    """Development control to simulate a camera going offline/degraded
    (SPEC section 40/43)."""
    provider = await find_mock_provider_for_camera(camera_id)
    if provider is None:
        raise HTTPException(404, "Camera not found")
    try:
        updated = provider.simulate_status(camera_id, payload.status)
    except CameraNotFoundError as exc:
        raise HTTPException(404, "Camera not found") from exc
    await camera_service.upsert_camera(session, updated)
    return updated


@router.post("/mock/cameras/{camera_id}/battery")
async def set_camera_battery(camera_id: str, payload: CameraBatteryIn, session: AsyncSession = Depends(get_db)):
    """Development control to simulate battery drain; automatically raises
    a high-priority ``battery_low`` event under the configurable threshold
    (SPEC section 8.3)."""
    provider = await find_mock_provider_for_camera(camera_id)
    if provider is None:
        raise HTTPException(404, "Camera not found")
    try:
        updated = provider.simulate_battery(camera_id, payload.battery_level)
    except CameraNotFoundError as exc:
        raise HTTPException(404, "Camera not found") from exc
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    await camera_service.upsert_camera(session, updated)
    if updated["battery_level"] is not None and updated["battery_level"] < settings.low_battery_threshold:
        event = provider.event(camera_id, "battery_low")
        event["description"] = f"{updated['name']} battery is low ({updated['battery_level']}%)"
        await event_service.create_and_broadcast_event(session, event)
    return updated


@router.post("/cameras/{camera_id}/audio/analyze", response_model=AudioAnalysisOut)
async def analyze_audio(
    camera_id: str, payload: MockAudioIn, session: AsyncSession = Depends(get_db)
):
    """Analyze a supplied audio buffer for speech-like activity.

    Gated by the ``audioDetection`` capability: a camera whose provider does
    not expose an audio buffer (or that has the feature disabled) returns
    ``503`` instead of HomeCam inventing audio. This detects *speech-like
    audio activity* only — never transcription or speaker identity.
    """
    provider = await find_provider_for_camera(camera_id)
    if provider is None:
        raise HTTPException(404, "Camera not found")
    try:
        capabilities = await provider.get_capabilities(camera_id)
    except CameraNotFoundError as exc:
        raise HTTPException(404, "Camera not found") from exc
    except ProviderUnavailableError as exc:
        raise HTTPException(503, f"Provider '{provider.id}' is currently unavailable") from exc
    if capabilities.get(AUDIO_DETECTION) != "SUPPORTED":
        raise HTTPException(
            503,
            f"Audio detection is {capabilities.get(AUDIO_DETECTION, 'UNKNOWN')} for camera '{camera_id}'",
        )
    try:
        pcm = base64.b64decode(payload.pcm_base64, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise HTTPException(422, "pcm_base64 must be valid base64") from exc

    analysis = analyze_pcm(pcm, settings.audio_energy_threshold)
    event_id: str | None = None
    if analysis.speech_like:
        mock = await find_mock_provider_for_camera(camera_id)
        if mock is not None:
            event = mock.event(camera_id, "motion")
            event["description"] = (
                f"Speech-like audio activity detected on {event.get('camera_name', camera_id)} "
                f"(confidence {analysis.confidence:.2f}); no transcription performed."
            )
            event["tags"] = ["audio", "speech-like"]
            row = await event_service.create_and_broadcast_event(session, event)
            event_id = row.id
    return AudioAnalysisOut(camera_id=camera_id, event_id=event_id, **analysis.as_dict())


@router.post("/mock/providers/{provider_id}/outage")
async def set_provider_outage(provider_id: str, payload: ProviderOutageIn):
    """Development control to simulate an entire provider (e.g. the Eufy
    HomeBase) becoming unreachable, to verify provider failure isolation."""
    provider = next((p for p in await active_mock_providers() if p.id == provider_id), None)
    if provider is None:
        raise HTTPException(404, "Provider not found")
    provider.simulate_outage(payload.unavailable)
    return await provider.get_health()


@router.get("/system/health")
async def health():
    provider_health = await get_all_provider_health()
    detector = detector_status()
    watchdog = detector_watchdog.status()
    providers_ok = all(h["status"] != "OFFLINE" for h in provider_health)
    blind = detector.degraded or watchdog["blackout"]
    overall = "ok" if providers_ok and not blind else "degraded"
    return {
        "status": overall,
        "service": "homecam-api",
        "providers": provider_health,
        "detector": detector.as_dict(),
        "detector_watchdog": watchdog,
    }


@router.get("/system/status")
async def system_status(response: Response):
    """Authoritative "are we actually seeing anything" surface.

    Returns **503** when the detector is not the one that was asked for, or
    when the zero-detection watchdog is firing, so a probe or a human can
    tell "running the real detector" from "blind" without reading container
    logs — the gap that let the 2026-09-30 incident run for three hours.
    """
    detector = detector_status()
    watchdog = detector_watchdog.status()
    blind = detector.degraded or watchdog["blackout"]
    if blind:
        response.status_code = 503
    reasons = []
    if detector.degraded:
        reasons.append(detector.reason or "detector degraded to mock")
    if watchdog["blackout"]:
        reasons.append(
            "zero detections on "
            f"{', '.join(watchdog['blackout_cameras'])} across a full "
            f"{watchdog['window_limit_seconds']:.0f}s window of successfully detected frames"
        )
    return {
        "status": "blind" if blind else "ok",
        "detecting": detector.detecting and not watchdog["blackout"],
        "detector": detector.as_dict(),
        "detector_watchdog": watchdog,
        "blind_cameras": watchdog["blackout_cameras"],
        "supported_backends": list(SUPPORTED_BACKENDS),
        "reasons": reasons,
        "ingestion": {
            "enabled": settings.event_ingestion_enabled,
            "frames": ingestion_service_stats(),
        },
    }


@router.get("/system/readiness")
async def readiness():
    detector = detector_status()
    watchdog = detector_watchdog.status()
    blind = detector.degraded or watchdog["blackout"]
    return {
        "status": "degraded" if blind else "ready",
        "database": "configured",
        "redis": "configured",
        "detector": detector.as_dict(),
        "detector_watchdog": watchdog,
    }


@router.get("/settings")
async def get_settings():
    return {
        "privacy_mode": "LOCAL ONLY",
        "retention_days": 30,
        "ai_provider": settings.ai_provider,
        "ai_detector_backend": settings.ai_detector_backend,
        "ai_analysis_enabled": settings.ai_analysis_enabled,
        "audio_detection_enabled": settings.audio_detection_enabled,
        "embedding_dimensions": settings.embedding_dimensions,
        "parked_vehicle_seconds": settings.parked_vehicle_seconds,
        "low_battery_threshold": settings.low_battery_threshold,
    }


def sse_frame(event: object) -> tuple[str, object]:
    """Compute the SSE event name and JSON-safe payload for one bus message.

    Every publisher other than the original event pipeline (incidents /
    security-mode broadcasts) tags its payload with ``_sse_event``; absent,
    this defaults to the original ``event.created`` name so every existing
    SSE consumer (Dashboard.tsx's EventSource listener) is unaffected.

    The same dict instance is broadcast to every subscriber (see
    ``EventBus.publish``), so this must never mutate it in place: popping
    the key would remove it before other concurrently-connected
    tabs/subscribers see it, corrupting their event name to the default.
    Returns a copy instead.
    """
    if isinstance(event, dict):
        sse_event = event.get("_sse_event", "event.created")
        payload = {k: v for k, v in event.items() if k != "_sse_event"}
        return sse_event, payload
    return "event.created", event


@router.get("/ws")
async def sse(auth_session: AuthSession = Depends(get_current_auth_session)):
    async def stream():
        queue = event_service.event_bus.subscribe()
        next_auth_check = asyncio.get_running_loop().time() + SSE_AUTH_RECHECK_INTERVAL_SECONDS
        try:
            yield "event: ready\ndata: {}\n\n"
            while True:
                timeout = max(0, next_auth_check - asyncio.get_running_loop().time())
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=timeout)
                except TimeoutError:
                    event = None

                if asyncio.get_running_loop().time() >= next_auth_check:
                    async with SessionLocal() as session:
                        active_session = await session.get(AuthSession, auth_session.token_hash)
                    expires_at = active_session.expires_at if active_session else None
                    if expires_at is not None:
                        if expires_at.tzinfo is None:
                            expires_at = expires_at.replace(tzinfo=timezone.utc)
                    if active_session is None or expires_at <= datetime.now(timezone.utc):
                        return
                    next_auth_check = asyncio.get_running_loop().time() + SSE_AUTH_RECHECK_INTERVAL_SECONDS

                if event is None:
                    yield ": keep-alive\n\n"
                    continue
                sse_event, payload = sse_frame(event)
                yield f"event: {sse_event}\ndata: {json.dumps(payload)}\n\n"
        finally:
            event_service.event_bus.unsubscribe(queue)

    return StreamingResponse(stream(), media_type="text/event-stream")
