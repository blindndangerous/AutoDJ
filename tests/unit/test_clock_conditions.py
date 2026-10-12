import pytest

from autodj.clock_conditions import genres, matches_condition, validate_metadata_condition


def test_genres_split_only_semicolon_and_nul_and_deduplicate():
    assert genres(["Rock; Pop", "rock\x00Jazz/Blues,Alt"]) == ["Rock", "Pop", "Jazz/Blues,Alt"]


def test_genre_condition_is_single_label():
    assert matches_condition(
        {"field": "genre", "operator": "is", "value": "rock"}, {"genre": "Rock;Jazz"}
    )
    assert not matches_condition({"field": "genre", "operator": "is", "value": "rock"}, {})


@pytest.mark.parametrize("field", ["artist", "title", "album"])
def test_text_case_and_wildcards(field):
    assert matches_condition(
        {"field": field, "operator": "matches", "value": "b*n?"}, {field: "Bono"}
    )
    assert not matches_condition({"field": field, "operator": "matches", "value": "*"}, {field: ""})
    assert matches_condition(
        {"field": field, "operator": "matches", "value": "[ab]"}, {field: "[ab]"}
    )
    assert not matches_condition(
        {"field": field, "operator": "matches", "value": "[]"}, {field: "a"}
    )


def test_numeric_inclusive_boundaries_and_missing_values():
    c = {"field": "year", "operator": "between", "value": 2000, "upper": 2000}
    assert matches_condition(c, {"year": "2000"})
    assert not matches_condition(c, {})
    assert not matches_condition({"field": "bpm", "operator": "eq", "value": 0}, {"bpm": 0})
    assert not matches_condition({"field": "year", "operator": "eq", "value": 0}, {"year": 0})
    assert not matches_condition({"field": "duration", "operator": "lt", "value": 1}, {"length": 0})
    assert matches_condition(
        {"field": "beets", "name": "rating", "operator": "between", "value": 3, "upper": 5},
        {},
        {"rating": "5"},
    )
    assert matches_condition(
        {"field": "beets", "name": "rating", "operator": "eq", "value": 0}, {}, {"rating": 0}
    )
    assert matches_condition(
        {"field": "beets", "name": "rg_track_gain", "operator": "lt", "value": -3},
        {},
        {"rg_track_gain": -4},
    )


def test_beets_custom_text_and_original_year():
    assert matches_condition(
        {"field": "beets", "name": "mood", "operator": "contains", "value": "HAP"},
        {},
        {"mood": "happy"},
    )
    assert matches_condition(
        {"field": "beets", "name": "original_year", "operator": "eq", "value": 1990},
        {},
        {"year": 1990},
    )
    assert matches_condition(
        {"field": "beets", "name": "artist", "operator": "is", "value": "Beets Artist"},
        {"artist": "Library Artist"},
        {"artist": "Beets Artist"},
    )
    assert not matches_condition(
        {"field": "beets", "name": "mood", "operator": "is", "value": "happy"}, {}, None
    )


def test_beets_text_operators_convert_numeric_scalars_to_text():
    rating = {"field": "beets", "name": "rating", "operator": "is", "value": "5"}
    assert matches_condition(rating, {}, {"rating": 5})
    assert matches_condition({**rating, "value": "5.0"}, {}, {"rating": 5.0}) is False
    assert matches_condition(
        {"field": "beets", "name": "rating", "operator": "contains", "value": "5"},
        {},
        {"rating": 5},
    )
    assert matches_condition(
        {"field": "beets", "name": "rating", "operator": "matches", "value": "?"},
        {},
        {"rating": 5},
    )
    assert matches_condition(rating, {}, {"rating": 5.0})


def test_arbitrary_fields_are_rejected_and_beets_names_are_validated():
    with pytest.raises(ValueError):
        validate_metadata_condition({"field": "mood", "operator": "is", "value": "happy"})
    for name in ("path", "id", "Bad Name", "x" * 65):
        with pytest.raises(ValueError):
            validate_metadata_condition(
                {"field": "beets", "name": name, "operator": "is", "value": "x"}
            )


@pytest.mark.parametrize(
    "condition",
    [
        [],
        {"field": "year", "operator": "eq", "value": True},
        {"field": "year", "operator": "eq", "value": "2000"},
        {"field": "year", "operator": "eq", "value": float("nan")},
        {"field": "year", "operator": "between", "value": 3, "upper": 2},
        {"field": "artist", "operator": "regex", "value": "x"},
        {"field": "artist", "operator": "matches", "value": ""},
        {"field": "artist", "operator": "is", "value": "x", "extra": 1},
        {"field": "beets", "name": "mood", "operator": "is", "value": "  "},
        {"field": "artist", "operator": [], "value": "x"},
        {"field": "artist", "operator": {}, "value": "x"},
    ],
)
def test_invalid_shapes_are_rejected(condition):
    with pytest.raises(ValueError):
        validate_metadata_condition(condition)
