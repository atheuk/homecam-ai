"""Animal identification: species, breed, and refusing to guess.

The detector can only say dog/cat/bird/animal. Breed comes from the Foundry
vision deployment, and the value of that answer depends entirely on it being
honest -- a confidently wrong breed is worse than no breed at all.
"""
from __future__ import annotations

import pytest

from app.ai.animals import (
    ANIMAL_SPECIES,
    TAXONOMIC_GROUPS,
    AnimalIdentity,
    describe_animal,
    identify_animal_frames,
    normalize_species,
    parse_animal_reply,
    prepare_animal_crop,
    vote_animal_identities,
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
        "A likely Border Collie (dog) was seen in the driveway at Front Yard."
    )


def test_low_confidence_breed_is_only_possible_and_groups_are_counted():
    guess = AnimalIdentity(species="dog", breed="Akita", confidence=0.5)
    assert guess.breed_certainty == "possible"
    assert describe_animal(guess, "Garden") == "A possible Akita (dog) was seen at Garden."
    pair = AnimalIdentity(species="cat", confidence=0.9, count=2)
    assert describe_animal(pair, "Garden") == "2 cats were seen at Garden."


def test_rich_animal_reply_keeps_visible_traits_and_drops_junk():
    identity = parse_animal_reply(
        '{"species": "cat", "breed": null, "confidence": 0.9, "count": "2", '
        '"coat_colours": ["black", "white", "Black"], "coat_pattern": "tuxedo", '
        '"size": "Medium", "action": "sitting at the door", "collar_visible": "yes"}'
    )
    assert identity is not None
    assert identity.count == 2
    assert identity.coat_colours == ("black", "white")
    assert identity.coat_pattern == "tuxedo"
    assert identity.size == "medium"
    assert identity.action == "sitting at the door"
    assert identity.collar_visible is True
    data = identity.as_dict()
    assert data["breed_certainty"] is None and data["count"] == 2
    junk = parse_animal_reply('{"species": "dog", "count": 999, "size": "huge", "collar_visible": "maybe"}')
    assert junk is not None and junk.count is None and junk.size is None and junk.collar_visible is None


def test_description_falls_back_to_species_then_to_plain_animal():
    assert describe_animal(AnimalIdentity(species="cat"), "Back Yard") == (
        "A cat was seen at Back Yard."
    )
    assert describe_animal(AnimalIdentity(species="other"), "Back Yard") == (
        "An animal was seen at Back Yard."
    )


def test_taxonomy_accepts_common_scientific_and_group_names_with_safe_fallback():
    identity = parse_animal_reply(
        '{"species":"other","common_name":null,"scientific_name":null,'
        '"genus":null,"family":"Ranidae","taxonomic_group":"amphibian",'
        '"confidence":0.61}'
    )
    assert identity is not None
    assert identity.taxonomic_group in TAXONOMIC_GROUPS
    assert identity.kind == "Ranidae"
    assert identity.confidence == 0.61


def test_vote_prefers_consensus_over_a_single_stronger_but_weakly_supported_name():
    result = vote_animal_identities(
        [
            AnimalIdentity("other", common_name="red fox", confidence=0.65),
            AnimalIdentity("other", common_name="red fox", confidence=0.58),
            AnimalIdentity("other", common_name="grey fox", confidence=0.99),
        ]
    )
    assert result is not None
    assert result.common_name == "red fox"


def test_animal_crop_is_padded_and_upscaled():
    from PIL import Image
    from io import BytesIO

    source = BytesIO()
    Image.new("RGB", (100, 80), "white").save(source, format="JPEG")
    cropped = prepare_animal_crop(source.getvalue(), (0.4, 0.4, 0.5, 0.5), min_pixels=256)
    with Image.open(BytesIO(cropped)) as image:
        assert image.width >= 256
        assert image.height >= 256


@pytest.mark.asyncio
async def test_multi_frame_identification_votes_deterministically():
    class Stub:
        def __init__(self):
            self.results = iter(
                [
                    AnimalIdentity("dog", breed="Beagle", confidence=0.4),
                    AnimalIdentity("dog", breed="Beagle", confidence=0.7),
                    AnimalIdentity("dog", breed="Poodle", confidence=0.99),
                ]
            )

        async def identify_animal(self, image, content_type):
            return next(self.results)

    result = await identify_animal_frames(Stub(), [b"1", b"2", b"3"])
    assert result is not None
    assert result.breed == "Beagle"
