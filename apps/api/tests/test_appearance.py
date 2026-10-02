"""Appearance analysis: what HomeCam will and will not say about a person.

The behaviour these tests pin down is as much a policy decision as a parsing
one. HomeCam records what a witness could describe - build, hair, clothing, what
someone was carrying or doing, and a broad, explicitly uncertain apparent age
range - and refuses to infer ethnicity, skin colour, gender, religion,
health, emotion or intent, which would turn a doorbell camera into a
profiling tool. Prohibited wording is dropped even when the model emits it.
"""
import pytest

from app.ai.appearance import (
    AGE_BANDS,
    APPEARANCE_SYSTEM_PROMPT,
    Appearance,
    normalize_age_band,
    parse_appearance_reply,
)


def test_prompt_forbids_protected_attributes():
    lowered = APPEARANCE_SYSTEM_PROMPT.lower()
    for forbidden in ("ethnicity", "race", "gender", "nationality"):
        assert forbidden in lowered, f"{forbidden} must be explicitly ruled out"
    assert "never" in lowered


def test_parses_a_full_reply():
    result = parse_appearance_reply(
        '{"person_present": true, "age_band": "adult", "age_confidence": 0.62,'
        ' "build": "tall", "clothing": "dark jacket and jeans",'
        ' "carrying": "a parcel", "face_visible": true,'
        ' "description": "An adult in a dark jacket carrying a parcel."}'
    )
    assert result is not None
    assert result.person_present is True
    assert result.age_band == "adult"
    # An apparent age from a crop is never more than a medium-certainty impression.
    assert result.age_confidence == 0.6
    assert result.age_range == "approx. 20-64"
    assert result.age_certainty == "medium"
    assert result.carrying == "a parcel"
    assert result.face_visible is True
    # Age words belong only in the hedged age field, not free text.
    assert result.description is None


def test_parses_rich_observable_fields():
    result = parse_appearance_reply(
        '{"person_present": true, "apparent_age_band": "teenager", "age_confidence": 0.3,'
        ' "hair": {"length": "shoulder-length", "colour": "dark brown", "style": "ponytail"},'
        ' "upper_clothing": {"colour": "red", "type": "hooded jacket"},'
        ' "lower_clothing": {"colour": "blue", "type": "jeans"},'
        ' "headwear": null, "footwear": "white trainers",'
        ' "accessories": ["glasses", "backpack", "Glasses"],'
        ' "action": "ringing the doorbell", "direction": "Towards the camera",'
        ' "description": "Red hooded jacket, blue jeans, ringing the doorbell."}'
    )
    assert result is not None
    assert result.hair == {"length": "shoulder-length", "colour": "dark brown", "style": "ponytail"}
    assert result.clothing == "red hooded jacket and blue jeans"
    assert result.footwear == "white trainers"
    assert result.headwear is None
    assert result.accessories == ("glasses", "backpack")
    assert result.action == "ringing the doorbell"
    assert result.direction == "toward camera"
    assert result.age_certainty == "low" and result.age_range == "approx. 13-19"
    assert result.description == "Red hooded jacket, blue jeans, ringing the doorbell"


def test_hidden_hair_and_unknowns_stay_unknown():
    result = parse_appearance_reply(
        '{"person_present": true, "hair": {"length": null, "colour": "not visible", "style": null},'
        ' "direction": "sideways", "accessories": "none"}'
    )
    assert result is not None
    assert result.hair is None
    assert result.direction is None
    assert result.accessories == ()
    assert result.age_band is None


@pytest.mark.parametrize(
    "field,value",
    [
        ("description", "A Caucasian man at the door"),
        ("description", "A nervous person looking suspicious"),
        ("build", "about 35 years old"),
        ("action", "a woman waiting"),
        ("carrying", "a religious book, appears muslim"),
        ("headwear", "skin-coloured cap"),
    ],
)
def test_prohibited_attribute_wording_is_dropped(field, value):
    result = parse_appearance_reply(f'{{"person_present": true, "{field}": "{value}"}}')
    assert result is not None
    assert getattr(result, field) is None


def test_age_without_confidence_is_not_reported():
    result = parse_appearance_reply('{"person_present": true, "age_band": "adult"}')
    assert result is not None and result.age_band is None


def test_output_never_carries_protected_attributes():
    result = parse_appearance_reply(
        '{"person_present": true, "age_band": "adult",'
        ' "ethnicity": "white", "gender": "male",'
        ' "description": "An adult at the door."}'
    )
    assert result is not None
    payload = result.as_dict()
    assert "ethnicity" not in payload
    assert "gender" not in payload


def test_unparseable_reply_is_none_not_a_negative():
    """The difference matters: "no one is there" is evidence, "I could not
    read the model's answer" is not, and must never suppress a detection."""
    assert parse_appearance_reply("the model was unwell today") is None
    assert parse_appearance_reply("") is None
    assert parse_appearance_reply(None) is None


def test_genuine_negative_is_distinguishable():
    result = parse_appearance_reply('{"person_present": false}')
    assert result is not None
    assert result.person_present is False


def test_tolerates_code_fences_and_surrounding_prose():
    result = parse_appearance_reply(
        'Sure! Here you go:\n```json\n{"person_present": true,'
        ' "age_band": "child"}\n```\nHope that helps.'
    )
    assert result is not None
    assert result.age_band is None


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("toddler", "child"),
        ("kid", "child"),
        ("teen", "teenager"),
        ("young adult", "adult"),
        ("middle-aged", "adult"),
        ("elderly", "older adult"),
        ("senior", "older adult"),
        ("ADULT", "adult"),
        ("  adult  ", "adult"),
    ],
)
def test_age_synonyms_map_onto_the_fixed_bands(raw, expected):
    assert normalize_age_band(raw) == expected
    assert expected in AGE_BANDS


@pytest.mark.parametrize("raw", ["unknown", "n/a", "none", "", None, "banana"])
def test_unusable_age_values_become_none(raw):
    assert normalize_age_band(raw) is None


def test_age_confidence_is_dropped_with_the_band():
    """A confidence score describes the band; without one it means nothing."""
    result = parse_appearance_reply(
        '{"person_present": true, "age_band": "unknown", "age_confidence": 0.9}'
    )
    assert result is not None
    assert result.age_band is None
    assert result.age_confidence is None


def test_null_string_fields_are_not_stored_as_text():
    result = parse_appearance_reply(
        '{"person_present": true, "clothing": "unknown", "carrying": "none",'
        ' "build": "n/a"}'
    )
    assert result is not None
    assert result.clothing is None
    assert result.carrying is None
    assert result.build is None


def test_summary_reads_like_a_sentence_fragment():
    appearance = Appearance(
        person_present=True,
        age_band="adult",
        age_confidence=0.5,
        build="tall",
        clothing="dark jacket",
        carrying="a parcel",
        face_visible=True,
        description="An adult in a dark jacket.",
    )
    summary = appearance.summary
    assert "Adult" in summary
    assert "dark jacket" in summary
    assert "parcel" in summary


def test_summary_of_an_empty_appearance_is_empty():
    assert Appearance(person_present=True).summary == ""


def test_a_reply_without_an_age_band_serialises():
    """Regression: enrichment crashed rounding a missing confidence."""
    result = parse_appearance_reply('{"person_present": true, "age_band": null}')
    assert result is not None
    assert result.as_dict()["age_confidence"] is None
