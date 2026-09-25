"""Animal identification: species, breed, and refusing to guess.

The detector can only say dog/cat/bird/animal. Breed comes from the Foundry
vision deployment, and the value of that answer depends entirely on it being
honest -- a confidently wrong breed is worse than no breed at all.
"""
from __future__ import annotations

from app.ai.animals import (
    ANIMAL_SPECIES,
    AnimalIdentity,
    describe_animal,
    normalize_species,
    parse_animal_reply,
)
from app.ai.detector import ANIMAL_CLASSES, COCO_CLASS_NAMES


def test_the_three_named_species_plus_a_catch_all_are_supported():
    assert set(ANIMAL_SPECIES) == {"dog", "cat", "bird", "other"}


def test_detector_classes_cover_the_named_species():
    assert {"dog", "cat", "bird"} <= ANIMAL_CLASSES
    assert "animal" in ANIMAL_CLASSES, "something else must still be an animal"


def test_coco_species_outside_the_named_set_become_the_generic_animal():
    # 21 is COCO "bear": a real animal, not a dog/cat/bird.
    assert COCO_CLASS_NAMES[21] == "animal"
    assert COCO_CLASS_NAMES[14] == "bird"


def test_species_synonyms_are_understood():
    assert normalize_species("Puppy") == "dog"
    assert normalize_species("kitten") == "cat"
    assert normalize_species("BIRD") == "bird"
    assert normalize_species("fox") == "other"


def test_no_animal_reply_is_not_an_identification():
    """The vision model doubles as a false-positive check on the detector."""
    assert normalize_species("none") is None
    assert parse_animal_reply('{"species": "none", "breed": null}') is None


def test_breed_is_parsed_with_its_confidence():
    identity = parse_animal_reply(
        '{"species": "dog", "breed": "Border Collie", "confidence": 0.82,'
        ' "description": "A black and white dog on the lawn."}'
    )

    assert identity == AnimalIdentity(
        species="dog",
        breed="Border Collie",
        confidence=0.82,
        description="A black and white dog on the lawn.",
    )
    assert identity.kind == "Border Collie"


def test_a_breed_the_model_will_not_commit_to_is_dropped():
    for answer in ("unknown", "", "not sure", None):
        identity = parse_animal_reply(
            '{"species": "cat", "breed": %s, "confidence": 0.9}'
            % ("null" if answer is None else f'"{answer}"')
        )
        assert identity is not None
        assert identity.species == "cat"
        assert identity.breed is None
        # Confidence describes the breed, so it cannot survive without one.
        assert identity.confidence == 0.0
        assert identity.kind == "cat"


def test_code_fenced_and_chatty_replies_are_still_parsed():
    fenced = parse_animal_reply('```json\n{"species": "bird", "breed": "Magpie"}\n```')
    assert fenced is not None and fenced.breed == "Magpie"

    chatty = parse_animal_reply('Sure! {"species": "bird", "breed": "Magpie"} Hope that helps.')
    assert chatty is not None and chatty.species == "bird"


def test_unparseable_replies_yield_nothing_rather_than_a_fake_species():
    assert parse_animal_reply("I think it might be a dog?") is None
    assert parse_animal_reply("") is None
    assert parse_animal_reply(None) is None


def test_confidence_is_clamped_to_a_real_probability():
    identity = parse_animal_reply('{"species": "dog", "breed": "Beagle", "confidence": 7}')
    assert identity is not None and identity.confidence == 1.0

    junk = parse_animal_reply('{"species": "dog", "breed": "Beagle", "confidence": "high"}')
    assert junk is not None and junk.confidence == 0.0


def test_description_names_the_breed_when_known():
    identity = AnimalIdentity(species="dog", breed="Border Collie", confidence=0.8)
    assert describe_animal(identity, "Front Yard", "driveway") == (
        "A Border Collie (dog) was seen in the driveway at Front Yard."
    )


def test_description_falls_back_to_species_then_to_plain_animal():
    assert describe_animal(AnimalIdentity(species="cat"), "Back Yard") == (
        "A cat was seen at Back Yard."
    )
    assert describe_animal(AnimalIdentity(species="other"), "Back Yard") == (
        "An animal was seen at Back Yard."
    )
