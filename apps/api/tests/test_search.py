"""Natural-language search, and the Responsible-AI refusals it enforces.

The refusal tests are the important ones here: they are the executable
statement of the promise in docs/ai-features.md that HomeCam cannot be
talked into identifying anybody.
"""
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import delete

from app.ai.query_moderation import moderate_query
from app.db import SessionLocal
from app.models.db import Event
from app.services import search


@pytest.fixture(autouse=True)
async def _clean_events():
    # init_db so this module also passes when run on its own, before any
    # test has pulled in the client fixture's lifespan.
    from app.db import init_db

    await init_db()

    async def _clear():
        async with SessionLocal() as session:
            await session.execute(delete(Event))
            await session.commit()

    await _clear()
    yield
    await _clear()


async def _seed(**overrides) -> str:
    now = datetime.now(timezone.utc)
    defaults = dict(
        id=f"evt-{overrides.get('description', 'x')}-{overrides.get('camera_id', 'c')}",
        camera_id="mock-front-door",
        type="person",
        priority="normal",
        source="test",
        start_time=now,
        description="a person at the door",
        tags=[],
        event_metadata={},
    )
    defaults.update(overrides)
    async with SessionLocal() as session:
        session.add(Event(**defaults))
        await session.commit()
    return defaults["id"]


# --- moderation (Responsible AI) ---------------------------------------------------


@pytest.mark.parametrize(
    "query,category",
    [
        ("who is this person", "identity"),
        ("run facial recognition on the visitor", "identity"),
        ("identify the man at the door", "identity"),
        ("what gender was it", "gender"),
        ("what ethnicity were they", "ethnicity"),
        ("how old was the visitor", "age"),
        ("read the licence plate", "plate"),
    ],
)
def test_identity_style_queries_are_refused(query, category):
    result = moderate_query(query)
    assert result.refused
    assert category in result.categories
    assert result.query == ""
    assert result.message


def test_mixed_query_is_stripped_not_refused():
    result = moderate_query("man in the driveway last night")
    assert not result.refused
    assert "man" not in result.query.split()
    assert "driveway" in result.query
    assert "gender" in result.categories
    assert result.message


def test_benign_query_is_untouched():
    result = moderate_query("package at the front door")
    assert not result.refused
    assert result.categories == ()
    assert result.message is None
    assert result.query == "package at the front door"


def test_empty_query_is_refused():
    assert moderate_query("   ").refused


@pytest.mark.asyncio
async def test_search_service_returns_no_hits_for_refused_query():
    await _seed(description="person at the front door")
    async with SessionLocal() as session:
        moderation, hits = await search.search_events(session, "who is this person")
    assert moderation.refused
    assert hits == []


# --- ranking -----------------------------------------------------------------------


def test_keyword_score_counts_matched_words():
    assert search.keyword_score(search.tokenize("blue van"), "a blue van parked") == 1.0
    assert search.keyword_score(search.tokenize("blue van"), "a red bicycle") == 0.0


def test_cosine_similarity_bounds():
    assert search.cosine_similarity([1.0, 0.0], [1.0, 0.0]) == pytest.approx(1.0)
    assert search.cosine_similarity([1.0, 0.0], [0.0, 1.0]) == pytest.approx(0.0)
    # Opposed vectors clamp to zero rather than going negative.
    assert search.cosine_similarity([1.0, 0.0], [-1.0, 0.0]) == 0.0
    assert search.cosine_similarity([], [1.0]) == 0.0


@pytest.mark.asyncio
async def test_search_ranks_matching_event_first():
    await _seed(id="evt-van", description="a blue van parked on the driveway", type="vehicle")
    await _seed(id="evt-cat", description="a cat crossed the lawn", type="animal")
    async with SessionLocal() as session:
        _moderation, hits = await search.search_events(session, "blue van driveway")
    assert hits
    assert hits[0].row.id == "evt-van"
    assert all(hit.row.id != "evt-cat" for hit in hits)


@pytest.mark.asyncio
async def test_search_filters_by_camera_and_time():
    old = datetime.now(timezone.utc) - timedelta(days=5)
    await _seed(id="evt-a", camera_id="mock-front-door", description="package delivered")
    await _seed(id="evt-b", camera_id="mock-garden", description="package delivered")
    await _seed(id="evt-c", camera_id="mock-front-door", description="package delivered", start_time=old)

    async with SessionLocal() as session:
        _m, hits = await search.search_events(session, "package", camera_id="mock-garden")
        assert [hit.row.id for hit in hits] == ["evt-b"]

        _m, hits = await search.search_events(
            session, "package", since=datetime.now(timezone.utc) - timedelta(days=1)
        )
        assert {hit.row.id for hit in hits} == {"evt-a", "evt-b"}

        _m, hits = await search.search_events(session, "package", until=old + timedelta(hours=1))
        assert [hit.row.id for hit in hits] == ["evt-c"]


@pytest.mark.asyncio
async def test_search_matches_on_tags_and_zone():
    await _seed(id="evt-loiter", description="movement", zone="driveway", tags=["loitering"])
    async with SessionLocal() as session:
        _m, hits = await search.search_events(session, "loitering driveway")
    assert [hit.row.id for hit in hits] == ["evt-loiter"]


# --- API ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_search_endpoint_returns_results(client):
    await client.post("/api/v1/mock/events", json={"camera_id": "mock-front-door", "type": "person"})
    r = await client.get("/api/v1/search", params={"q": "person"})
    assert r.status_code == 200
    body = r.json()
    assert body["refused"] is False
    assert body["results"]
    assert "score" in body["results"][0]


@pytest.mark.asyncio
async def test_search_endpoint_refuses_identity_query(client):
    r = await client.get("/api/v1/search", params={"q": "who is the person at my door"})
    assert r.status_code == 200
    body = r.json()
    assert body["refused"] is True
    assert body["results"] == []
    assert "identity" in body["blocked_categories"]
    assert "face recognition" in body["notice"].lower()
