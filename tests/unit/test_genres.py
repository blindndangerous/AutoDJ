"""Tests for autodj.genres — free-text → canonical genre normaliser."""

from __future__ import annotations

import pytest

from autodj.genres import canonicalise_list, matches, normalise


class TestNormalise:
    @pytest.mark.parametrize(
        "raw,expected",
        [
            ("Electronic", "electronic"),
            ("EDM", "electronic"),
            ("Synthwave", "electronic"),
            ("Trance", "electronic"),
            ("Hip-Hop", "hip-hop"),
            ("Hip Hop", "hip-hop"),
            ("Rap", "hip-hop"),
            ("Trap", "hip-hop"),
            ("Trip-Hop", "trip-hop"),
            ("Trip Hop", "trip-hop"),
            ("Drum and Bass", "drum-and-bass"),
            ("D&B", "drum-and-bass"),
            ("DnB", "drum-and-bass"),
            ("Jungle", "drum-and-bass"),
            ("House", "house"),
            ("Deep House", "house"),
            ("Tech House", "house"),
            ("Techno", "techno"),
            ("Indie Rock", "rock"),
            ("Alt Rock", "rock"),
            ("Alternative", "rock"),
            ("Post-Rock", "rock"),
            ("Pop", "pop"),
            ("Synth-Pop", "pop"),
            ("R&B", "r-n-b"),
            ("Soul", "r-n-b"),
            ("Jazz", "jazz"),
            ("Bebop", "jazz"),
            ("Classical", "classical"),
            ("Country", "country"),
            ("Folk", "folk"),
            ("Blues", "blues"),
            ("Reggae", "reggae"),
            ("Dub", "reggae"),
            ("Punk", "punk"),
            ("Metal", "metal"),
        ],
    )
    def test_canonical_mappings(self, raw, expected) -> None:
        assert normalise(raw) == expected

    def test_split_on_slash(self) -> None:
        assert normalise("Electronic / Trance") == "electronic"

    def test_split_on_comma(self) -> None:
        assert normalise("Hip Hop, Rap") == "hip-hop"

    def test_split_on_semicolon(self) -> None:
        assert normalise("House; Tech House") == "house"

    def test_first_known_token_wins(self) -> None:
        # "Rock" is canonical, "World" wouldn't normalise — first match
        assert normalise("Rock / World Music") == "rock"

    def test_unknown_returns_empty(self) -> None:
        assert normalise("Klezmer") == ""

    def test_none_returns_empty(self) -> None:
        assert normalise(None) == ""

    def test_empty_returns_empty(self) -> None:
        assert normalise("") == ""

    def test_whitespace_only_returns_empty(self) -> None:
        assert normalise("   ") == ""

    def test_case_insensitive(self) -> None:
        assert normalise("HIP HOP") == "hip-hop"
        assert normalise("electronic") == "electronic"
        assert normalise("MeTaL") == "metal"


class TestCanonicaliseList:
    def test_dedupes(self) -> None:
        # All three normalise to "electronic"
        assert canonicalise_list(["Electronic", "EDM", "Trance"]) == ["electronic"]

    def test_preserves_order(self) -> None:
        assert canonicalise_list(["Hip Hop", "Rock"]) == ["hip-hop", "rock"]

    def test_drops_unknown(self) -> None:
        assert canonicalise_list(["Klezmer", "Rock"]) == ["rock"]

    def test_empty_list(self) -> None:
        assert canonicalise_list([]) == []

    def test_none(self) -> None:
        assert canonicalise_list(None) == []

    def test_drops_empty_strings(self) -> None:
        assert canonicalise_list(["", "Rock", "  "]) == ["rock"]


class TestMatches:
    def test_match_in_allowed(self) -> None:
        assert matches("Electronic", ["electronic"]) is True

    def test_alias_matches_canonical(self) -> None:
        assert matches("EDM", ["electronic"]) is True
        assert matches("Indie Rock", ["rock"]) is True

    def test_no_match(self) -> None:
        assert matches("Jazz", ["rock", "metal"]) is False

    def test_empty_allowed_means_no_filter(self) -> None:
        assert matches("Anything", []) is True

    def test_unknown_genre_with_filter(self) -> None:
        assert matches("Klezmer", ["rock"]) is False

    def test_none_genre_with_filter(self) -> None:
        assert matches(None, ["rock"]) is False

    def test_none_genre_no_filter(self) -> None:
        assert matches(None, []) is True
