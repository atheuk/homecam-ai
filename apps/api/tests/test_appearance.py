"""Appearance analysis: what HomeCam will and will not say about a person.

The behaviour these tests pin down is as much a policy decision as a parsing
one. HomeCam records what a witness could describe - build, clothing, what
someone was carrying, roughly how old they looked - and refuses to infer
ethnicity or gender, which are protected attributes it would be guessing at
and which would turn a doorbell camera into a profiling tool.
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
    assert result.age_confidence == pytest.approx(0.62)
    assert result.carrying == "a parcel"
    assert result.face_visible is True


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
    assert result.age_band == "child"


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
