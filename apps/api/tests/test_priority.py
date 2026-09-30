"""Smart notification priority scoring and its use as a noise filter."""
import pytest

from app.config import settings
from app.services import priority


def test_person_while_away_scores_high():
    result = priority.score_event(event_type="person", mode="away")
    assert result.priority == "high"
    assert "armed away" in result.reasons


def test_person_while_disarmed_is_quieter_than_while_armed():
    disarmed = priority.score_event(event_type="person", mode="disarmed")
    away = priority.score_event(event_type="person", mode="away")
    assert disarmed.rank < away.rank


def test_package_removal_while_armed_is_critical():
    result = priority.score_event(event_type="package", mode="away", package_theft=True)
    assert result.priority == "critical"
    assert "package removed" in result.reasons


def test_loitering_and_unusual_raise_priority():
    plain = priority.score_event(event_type="person", mode="home")
    flagged = priority.score_event(event_type="person", mode="home", loitering=True, unusual=True)
    assert flagged.score > plain.score
    assert "loitering" in flagged.reasons
    assert "unusual for this time" in flagged.reasons


def test_restricted_zone_outranks_entry_zone():
    entry = priority.score_event(event_type="person", zone_kind="entry")
    restricted = priority.score_event(event_type="person", zone_kind="restricted")
    assert restricted.score > entry.score


def test_low_confidence_lowers_priority():
    confident = priority.score_event(event_type="person", mode="home", confidence=0.9)
    unsure = priority.score_event(event_type="person", mode="home", confidence=0.2)
    assert unsure.score < confident.score
    assert "low confidence" in unsure.reasons


def test_tags_are_equivalent_to_flags():
    by_flag = priority.score_event(event_type="person", loitering=True)
    by_tag = priority.score_event(event_type="person", tags=["loitering"])
    assert by_flag.score == by_tag.score


def test_animal_while_disarmed_is_low():
    assert priority.score_event(event_type="animal", mode="disarmed").priority == "low"


def test_score_never_goes_negative():
    result = priority.score_event(event_type="animal", mode="disarmed", confidence=0.1)
    assert result.score == 0


def test_priority_scoring_ignores_identity_inputs():
    """Priority is a function of what/where/when only.

    ``score_event`` deliberately has no parameter that could carry a
    person's identity, gender, ethnicity or age - this test fails if one
    is ever added.
    """
    import inspect

    params = set(inspect.signature(priority.score_event).parameters)
    forbidden = {"person", "person_id", "identity", "name", "gender", "ethnicity", "race", "age"}
    assert params & forbidden == set()


def test_meets_minimum_threshold():
    assert priority.meets_minimum("high", "normal") is True
    assert priority.meets_minimum("low", "normal") is False
    assert priority.meets_minimum("normal", "normal") is True


def test_meets_minimum_fails_open_for_unknown_values():
    assert priority.meets_minimum(None, "normal") is True
    assert priority.meets_minimum("weird", "normal") is True
    assert priority.meets_minimum("low", "nonsense") is True


def test_meets_minimum_is_bypassed_when_feature_disabled(monkeypatch):
    monkeypatch.setattr(settings, "notification_priority_enabled", False)
    assert priority.meets_minimum("low", "critical") is True


@pytest.mark.asyncio
async def test_events_expose_notification_priority(client):
    r = await client.post("/api/v1/mock/events", json={"camera_id": "mock-front-door", "type": "person"})
    assert r.status_code == 200
    event_id = r.json()["id"]
    events = (await client.get("/api/v1/events")).json()
    created = next(e for e in events if e["id"] == event_id)
    assert created["notification_priority"] in priority.PRIORITIES
    assert isinstance(created["priority_reasons"], list)
