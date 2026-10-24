import pytest

from autodj.clock_conditions import condition_matcher, genres, validate_metadata_condition


def matches_condition(condition, entry, beets_item=None):
    return condition_matcher(condition)(entry, beets_item)


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
    assert not matches_condition({"field": field, "operator": "is_not", "value": "Bono"}, {})
    assert not matches_condition(
        {"field": field, "operator": "is_not", "value": "Bono"}, {field: "Bono"}
    )
    assert matches_condition(
        {"field": field, "operator": "is_not", "value": "Bono"}, {field: "Someone else"}
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
    assert matches_condition(
        {"field": "beets", "name": "mood", "operator": "is_not", "value": "sad"},
        {},
        {"mood": "happy"},
    )
    assert not matches_condition(
        {"field": "beets", "name": "mood", "operator": "is_not", "value": "sad"},
        {},
        {"mood": "sad"},
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


def test_contains_finds_text_inside_a_numeric_beets_column():
    condition = {"field": "beets", "name": "bpm", "operator": "contains", "value": "12"}
    assert matches_condition(condition, {}, {"bpm": 120})
    assert not matches_condition(condition, {}, {"bpm": 130})


def test_negative_conditions_require_known_metadata_and_exclude_any_matching_genre():
    assert not matches_condition({"field": "bpm", "operator": "not_eq", "value": 90}, {})
    assert matches_condition({"field": "bpm", "operator": "not_eq", "value": 90}, {"bpm": 91})
    assert not matches_condition(
        {"field": "genre", "operator": "is_not", "value": "slow jam"}, {"genre": "Pop;Slow Jam"}
    )
    assert matches_condition(
        {"field": "genre", "operator": "is_not", "value": "slow jam"}, {"genre": "Pop;Rock"}
    )
    assert not matches_condition({"field": "artist", "operator": "not_contains", "value": "x"}, {})
    assert not matches_condition(
        {"field": "year", "operator": "outside", "value": 2000, "upper": 2010}, {"year": 2005}
    )
    assert matches_condition(
        {"field": "year", "operator": "outside", "value": 2000, "upper": 2010}, {"year": 1999}
    )


#: A wall-clock time half an hour into an hour, for the relative date tests.
NOW = 1_760_000_000 // 3600 * 3600 + 1800
HOUR = NOW - 1800
DAY = 86400


def test_within_days_matches_beets_dates_in_the_last_n_days():
    """ADR-0009-07: within the last N days reads a Beets date against the given time."""
    added = {"field": "beets", "name": "added", "operator": "within_days", "value": 30}
    test = condition_matcher(added)
    # The window ends at the start of the hour and reaches back N whole days from there.
    assert test({}, {"added": HOUR - 30 * DAY}, NOW)
    assert not test({}, {"added": HOUR - 30 * DAY - 1}, NOW)
    # Added since the hour began, or dated a little ahead, is recent too.
    assert test({}, {"added": NOW - 60}, NOW)
    assert test({}, {"added": NOW + 600}, NOW)
    # Beets' 0 means no date; a missing value or text never matches.
    for item in ({"added": 0}, {}, None, {"added": "yesterday"}):
        assert not test({}, item, NOW)
    # Text holding a number, as a flexible attribute is stored, is read as one.
    assert test({}, {"added": str(NOW - DAY)}, NOW)
    mtime = {"field": "beets", "name": "mtime", "operator": "within_days", "value": 1}
    assert condition_matcher(mtime)({}, {"mtime": HOUR - DAY + 5}, NOW)
    assert not condition_matcher(mtime)({}, {"mtime": HOUR - 2 * DAY}, NOW)


def test_within_days_moves_with_the_clock():
    """ADR-0009-07: the same saved condition matches less as time passes."""
    test = condition_matcher(
        {"field": "beets", "name": "added", "operator": "within_days", "value": 7}
    )
    song = {"added": NOW - 6 * DAY}
    assert test({}, song, NOW)
    assert test({}, song, NOW + DAY - 1800)  # Still inside the hour the window ends at.
    assert not test({}, song, NOW + 2 * DAY)


@pytest.mark.parametrize(
    ("value", "upper", "field"),
    [
        (0, None, "beets"),
        (-1, None, "beets"),
        (3651, None, "beets"),
        (1.5, None, "beets"),
        (True, None, "beets"),
        ("30", None, "beets"),
        (None, None, "beets"),
        (30, 40, "beets"),
        (30, None, "year"),
        (30, None, "artist"),
    ],
)
def test_within_days_refuses_anything_but_a_whole_number_of_days(value, upper, field):
    condition = {"field": field, "operator": "within_days", "value": value}
    if field == "beets":
        condition["name"] = "added"
    if upper is not None:
        condition["upper"] = upper
    with pytest.raises(ValueError, match="within the last N days"):
        validate_metadata_condition(condition)


def test_within_days_keeps_its_bounds_and_whole_days():
    condition = {"field": "beets", "name": "added", "operator": "within_days"}
    assert validate_metadata_condition({**condition, "value": 1})["value"] == 1
    assert validate_metadata_condition({**condition, "value": 3650})["value"] == 3650
    assert validate_metadata_condition({**condition, "value": 30.0})["value"] == 30
    assert type(validate_metadata_condition({**condition, "value": 30.0})["value"]) is int
