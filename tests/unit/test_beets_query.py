"""The beets query subset AutoDJ parses and evaluates itself (autodj.beets_query)."""

from __future__ import annotations

import pytest

from autodj.beets_query import (
    DEFAULT_FIELDS,
    MAX_QUERY_LENGTH,
    MAX_TERMS,
    Query,
    QueryError,
    first_release_year,
    number,
    parse_query,
)

SONG = {
    "artist": "Bon Jovi",
    "title": "It's My Life",
    "album": "Crush",
    "albumartist": "Bon Jovi",
    "genre": "Rock",
    "comments": "",
    "year": 2005,
    "original_year": 2000,
    "bpm": 120.0,
    "mood": "happy",
    "rating": "4",
}


def matches(text: str, item: dict | None = None) -> bool:
    return parse_query(text).matches(SONG if item is None else item)


# ---------------------------------------------------------------------------
# field:value
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("genre:rock", True),
        ("genre:ROCK", True),
        ("genre:roc", True),  # part of the text, as beets does
        ("genre:pop", False),
        ("artist:jovi", True),
        ("mood:happy", True),  # a flexible attribute
        ("mood:sad", False),
        ("missing:x", False),  # a song without the field never matches
    ],
)
def test_field_value_matches_part_of_the_text_ignoring_case(text: str, expected: bool) -> None:
    assert matches(text) is expected


def test_a_number_field_matches_the_number_exactly() -> None:
    assert matches("year:2005")
    assert not matches("year:200")
    assert matches("bpm:120")
    assert matches("bpm:120.0")
    assert not matches("bpm:12")


def test_a_number_field_needs_a_number() -> None:
    with pytest.raises(QueryError, match="year is a number"):
        parse_query("year:two")


def test_a_number_stored_as_text_in_a_flexible_attribute_still_matches_as_text() -> None:
    assert matches("rating:4")
    assert matches("rating:4..5")
    assert not matches("rating:5..")


def test_field_names_are_read_without_case() -> None:
    assert matches("GENRE:rock")
    assert parse_query("Genre:rock").named_fields() == {"genre"}


def test_bytes_and_floats_are_compared_as_text() -> None:
    item = {"genre": b"Rock", "label": 5.0}
    assert matches("genre:rock", item)
    assert matches("label:5", item)


# ---------------------------------------------------------------------------
# Ranges
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("year:2000..2009", True),
        ("year:2005..2005", True),
        ("year:2006..2009", False),
        ("year:2000..", True),
        ("year:..2004", False),
        ("year:..2005", True),
        ("bpm:100.5..120", True),
        ("bpm:120.5..", False),
    ],
)
def test_ranges_include_both_ends_and_may_be_open(text: str, expected: bool) -> None:
    assert matches(text) is expected


def test_a_range_on_text_that_is_not_a_number_does_not_match() -> None:
    assert not matches("genre:1..5")


@pytest.mark.parametrize(
    ("text", "words"),
    [
        ("year:..", "give at least one end"),
        ("year:2009..2000", "starts after it ends"),
        ("year:1990..1995..1999", "one '..'"),
        ("added:2008-12..2009-10", "is not a number"),
    ],
)
def test_bad_ranges_are_refused_in_words(text: str, words: str) -> None:
    with pytest.raises(QueryError, match=words):
        parse_query(text)


# ---------------------------------------------------------------------------
# Negation, quotes, bare words and AND
# ---------------------------------------------------------------------------


def test_caret_and_minus_negate_a_term() -> None:
    assert not matches("^genre:rock")
    assert matches("^genre:pop")
    assert matches("-genre:pop")
    assert not matches("-year:2000..2009")


def test_a_negated_missing_field_matches() -> None:
    assert matches("^missing:x")


def test_a_lone_negation_is_refused() -> None:
    for text in ("^", "-"):
        with pytest.raises(QueryError, match="needs a term after it"):
            parse_query(text)


def test_quotes_keep_a_phrase_together() -> None:
    assert matches('artist:"bon jovi"')
    assert not matches('artist:"jovi bon"')
    assert matches("'my life'")
    assert matches('^artist:"the beatles"')
    query = parse_query('artist:"bon jovi" genre:rock')
    assert len(query.terms) == 2


def test_an_unclosed_quote_is_refused() -> None:
    with pytest.raises(QueryError, match="quotation mark is not closed"):
        parse_query('artist:"bon jovi')


def test_a_bare_word_searches_the_default_fields() -> None:
    assert matches("crush")  # album
    assert matches("life")  # title
    assert not matches("happy")  # mood is not a default field
    assert matches("^happy")
    query = parse_query("crush")
    assert query.fields() == frozenset(DEFAULT_FIELDS)
    assert query.named_fields() == frozenset()


def test_every_term_must_match() -> None:
    assert matches("genre:rock year:2005 artist:jovi")
    assert not matches("genre:rock year:2004")


def test_an_empty_query_matches_everything() -> None:
    query = parse_query("   ")
    assert not query
    assert query == Query()
    assert query.matches({})


def test_backslashes_are_kept_as_written() -> None:
    assert matches(r"title:\x", {"title": r"a\x"})


# ---------------------------------------------------------------------------
# What is not supported
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "words"),
    [
        ("genre::^ro", "regular expressions"),
        ("::rock", "regular expressions"),
        (":rock", "field name before the colon"),
        ("genre:=Rock", "exact matches"),
        ("genre:~rokc", "fuzzy matching"),
        ("path:/music/rock", "path queries"),
        ("genre:", "enter a value after the colon"),
        ("genre:rock , genre:pop", "comma"),
        ("genre:rock, genre:pop", "comma"),
        ("year+", "sorting"),
        ("artist-", "sorting"),
        ("my-field:x", "is not a field name"),
    ],
)
def test_unsupported_syntax_is_refused_naming_the_problem(text: str, words: str) -> None:
    with pytest.raises(QueryError, match=words):
        parse_query(text)


def test_an_error_quotes_the_term_at_fault() -> None:
    with pytest.raises(QueryError) as caught:
        parse_query("genre:rock year:abc")
    assert str(caught.value).startswith('"year:abc":')


def test_long_queries_and_many_terms_are_refused() -> None:
    with pytest.raises(QueryError, match=str(MAX_QUERY_LENGTH)):
        parse_query("a" * (MAX_QUERY_LENGTH + 1))
    with pytest.raises(QueryError, match=str(MAX_TERMS)):
        parse_query(" ".join(["rock"] * (MAX_TERMS + 1)))
    with pytest.raises(QueryError, match="must be text"):
        parse_query(5)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# The first-release year
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("item", "expected"),
    [
        ({"original_year": 1985, "year": 2005}, (1985, False)),
        ({"original_year": 0, "year": 2005}, (2005, True)),
        ({"year": 2005}, (2005, True)),
        ({"original_year": None, "year": "2005"}, (2005, True)),
        ({"original_year": 0, "year": 0}, (None, False)),
        ({}, (None, False)),
        ({"original_year": "", "year": "unknown"}, (None, False)),
    ],
)
def test_first_release_year_falls_back_to_the_release_year(item, expected) -> None:
    assert first_release_year(item) == expected


def test_original_year_in_a_query_uses_the_fallback() -> None:
    compilation = {"original_year": 1985, "year": 2005}
    untagged = {"original_year": 0, "year": 2005}
    undated = {"original_year": 0, "year": 0}
    query = parse_query("original_year:2000..2009")
    assert not query.matches(compilation)
    assert query.matches(untagged)
    assert not query.matches(undated)
    assert parse_query("^original_year:2000..2009").matches(undated)
    assert query.uses_first_release_year()
    assert "year" in query.fields()
    assert not parse_query("year:2005").uses_first_release_year()


def test_number_reads_numbers_and_refuses_the_rest() -> None:
    assert number(True) == 1.0
    assert number(3) == 3.0
    assert number(b" 4.5 ") == 4.5
    assert number("nan") is None
    assert number("abc") is None
    assert number(None) is None
    assert number(float("inf")) is None
