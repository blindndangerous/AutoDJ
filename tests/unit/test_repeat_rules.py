"""Repeat rules: title and artist keys, credit splitting, related artists, windows."""

from __future__ import annotations

from collections import deque
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest
from hypothesis import given
from hypothesis import strategies as st

from autodj.config import PlaybackConfig
from autodj.repeat_rules import (
    MAX_REPEAT_MINUTES,
    STARTER_RELATED_ARTISTS,
    ArtistSplitter,
    PlayRecord,
    RepeatFilter,
    RepeatRules,
    album_key,
    artist_key,
    build_filter,
    featured_in_title,
    load_related_artists,
    parse_related_artists,
    prune_history,
    starter_related_artists,
    title_key,
)


def _entry(path: str = "a.flac", artist: str = "", title: str = "", album: str = ""):
    return SimpleNamespace(path=path, artist=artist, title=title, album=album)


# ---------------------------------------------------------------------------
# Title keys
# ---------------------------------------------------------------------------


class TestTitleKey:
    @pytest.mark.parametrize(
        "title",
        [
            "Jingle Bells",
            "Jingle Bells (Live)",
            "JINGLE BELLS - Remastered 2009",
            "Jingle Bells - 2011 Remaster",
            "Jingle Bells [Radio Edit]",
            "Jingle Bells (feat. Somebody)",
            "Jingle Bells feat. Somebody",
            "Jingle Bells (Extended Mix)",
            "jingle bells!",
            "Jingle-Bells",
            "Jingle Bells (Acoustic Version) - Live at Wembley",
            "Jingle Bells {Demo}",
            "Jingle Bells – Single Version",
            "Jingle Bells (with Somebody)",
            "Jingle Bells (From “A Movie”)",
        ],
    )
    def test_versions_of_one_song_collide(self, title: str) -> None:
        assert title_key(title) == "jingle bells"

    def test_accents_case_and_apostrophes_fold(self) -> None:
        assert title_key("Café Del Mar") == title_key("CAFE DEL MAR")
        assert title_key("Don’t Stop") == title_key("Dont Stop") == "dont stop"

    def test_ampersand_reads_as_and(self) -> None:
        assert title_key("Rock & Roll") == title_key("Rock and Roll")

    def test_different_songs_stay_apart(self) -> None:
        assert title_key("Jingle Bells") != title_key("Jingle Bell Rock")

    def test_plain_brackets_keep_their_words(self) -> None:
        assert title_key("(I Can't Get No) Satisfaction") == "i cant get no satisfaction"

    def test_dash_without_a_version_note_is_kept(self) -> None:
        assert title_key("Song - Part 2") == "song part 2"

    def test_title_that_is_only_a_note_keeps_its_words(self) -> None:
        assert title_key("(Live)") == "live"

    def test_inline_ft_is_not_mistaken_for_a_credit(self) -> None:
        assert title_key("50 Ft. Queenie") == "50 ft queenie"

    @given(st.text(max_size=60))
    def test_key_is_idempotent_and_normalised(self, title: str) -> None:
        key = title_key(title)
        assert key == key.strip()
        assert "  " not in key
        assert key == key.casefold()
        assert title_key(key) == key

    @given(
        st.text(alphabet="abcdefghij ", min_size=1, max_size=30).filter(str.strip),
        st.sampled_from(["(Live)", "[Remix]", "- 2011 Remaster", "(Radio Edit)", "(feat. X Y)"]),
    )
    def test_any_version_note_is_dropped(self, base: str, note: str) -> None:
        assert title_key(f"{base} {note}") == title_key(base)


class TestArtistAndAlbumKeys:
    def test_leading_the_case_accents_and_punctuation(self) -> None:
        assert artist_key("The Beatles") == artist_key("beatles") == "beatles"
        assert artist_key("Beyoncé") == "beyonce"
        assert artist_key("Anderson .Paak") == artist_key("Anderson Paak")
        assert artist_key("Simon & Garfunkel") == artist_key("Simon and Garfunkel")

    def test_a_name_that_is_only_the_keeps_it(self) -> None:
        assert artist_key("The") == "the"

    def test_album_key(self) -> None:
        assert album_key("Thriller (Deluxe!)") == "thriller deluxe"

    @given(st.text(max_size=40))
    def test_artist_key_is_idempotent(self, name: str) -> None:
        key = artist_key(name)
        assert artist_key(key) == key or key.startswith("the ")


# ---------------------------------------------------------------------------
# Splitting credits
# ---------------------------------------------------------------------------


class TestArtistSplitter:
    @pytest.mark.parametrize(
        "credit",
        [
            "Simon & Garfunkel",
            "Earth, Wind & Fire",
            "Mumford & Sons",
            "Hall & Oates",
            "Crosby, Stills, Nash & Young",
            "Tyler, the Creator",
            "Florence + the Machine",
            "Sonny & Cher",
            "AC/DC",
            "Lil Nas X",
            "X Ambassadors",
        ],
    )
    def test_single_acts_stay_whole(self, credit: str) -> None:
        assert ArtistSplitter().split(credit) == {artist_key(credit)}

    def test_single_act_list_wins_over_known_parts(self) -> None:
        splitter = ArtistSplitter(known={"simon", "garfunkel", "earth", "wind", "fire"})
        assert splitter.split("Simon & Garfunkel") == {"simon and garfunkel"}
        assert splitter.split("Earth, Wind & Fire") == {"earth wind and fire"}

    def test_user_single_acts(self) -> None:
        credit = "Tom Petty and the Heartbreakers"
        assert ArtistSplitter().split(credit) == {
            "tom petty and the heartbreakers",
            "tom petty",
            "heartbreakers",
        }
        assert ArtistSplitter(single_acts=[credit]).split(credit) == {artist_key(credit)}

    @pytest.mark.parametrize(
        ("credit", "parts"),
        [
            ("Calvin Harris & Dua Lipa", {"calvin harris", "dua lipa"}),
            ("Calvin Harris, Dua Lipa", {"calvin harris", "dua lipa"}),
            ("Calvin Harris x Dua Lipa", {"calvin harris", "dua lipa"}),
            ("Calvin Harris with Dua Lipa", {"calvin harris", "dua lipa"}),
            ("Calvin Harris and Dua Lipa", {"calvin harris", "dua lipa"}),
            ("Jay-Z & Kanye West", {"jay z", "kanye west"}),
            ("Lil Nas X & Billy Ray Cyrus", {"lil nas x", "billy ray cyrus"}),
            ("Duke Ellington and His Orchestra", {"duke ellington"}),
        ],
    )
    def test_multi_word_parts_split(self, credit: str, parts: set[str]) -> None:
        assert ArtistSplitter().split(credit) == {artist_key(credit), *parts}

    @pytest.mark.parametrize(
        ("credit", "parts"),
        [
            ("Mark Ronson feat. Bruno Mars", {"mark ronson", "bruno mars"}),
            ("Mark Ronson ft. Bruno Mars", {"mark ronson", "bruno mars"}),
            ("Mark Ronson featuring Bruno Mars", {"mark ronson", "bruno mars"}),
            ("Mark Ronson (feat. Bruno Mars)", {"mark ronson", "bruno mars"}),
            ("Armin van Buuren vs. Tiësto", {"armin van buuren", "tiesto"}),
            ("Rihanna;Drake", {"rihanna", "drake"}),
            ("Rihanna\x00Drake", {"rihanna", "drake"}),
            ("Calvin Harris feat. Rihanna", {"calvin harris", "rihanna"}),
        ],
    )
    def test_featured_credits_always_split(self, credit: str, parts: set[str]) -> None:
        assert ArtistSplitter().split(credit) == {artist_key(credit), *parts}

    def test_one_word_names_split_only_when_known(self) -> None:
        assert ArtistSplitter().split("Rihanna & Drake") == {"rihanna and drake"}
        known = ArtistSplitter(known={"rihanna", "drake"})
        assert known.split("Rihanna & Drake") == {"rihanna and drake", "rihanna", "drake"}
        # Every part has to look like an artist.
        assert ArtistSplitter(known={"cher"}).split("Sonny & Cher") == {"sonny and cher"}

    def test_featured_credit_is_split_too(self) -> None:
        keys = ArtistSplitter().split("DJ Snake feat. Selena Gomez & Cardi B")
        assert {"dj snake", "selena gomez", "cardi b"} <= keys

    def test_trailing_joiner_does_not_split(self) -> None:
        assert ArtistSplitter().split("Calvin Harris, ") == {"calvin harris"}

    def test_empty_credit(self) -> None:
        assert ArtistSplitter().split("") == frozenset()

    def test_results_are_cached(self) -> None:
        splitter = ArtistSplitter()
        assert splitter.split("A B & C D") is splitter.split("A B & C D")

    def test_credited_adds_featured_artists_from_the_title(self) -> None:
        keys = ArtistSplitter().credited("Mark Ronson", "Uptown Funk (feat. Bruno Mars)")
        assert keys == {"mark ronson", "bruno mars"}
        assert ArtistSplitter().credited("", "") == frozenset()

    def test_featured_in_title(self) -> None:
        assert featured_in_title("Song (feat. A B) feat. C D") == ["A B", "C D"]
        assert featured_in_title("Song") == []

    @given(st.text(max_size=50))
    def test_the_full_credit_always_counts(self, credit: str) -> None:
        key = artist_key(credit)
        keys = ArtistSplitter().split(credit)
        assert (key in keys) if key else "" not in keys

    @given(
        st.lists(
            st.sampled_from(["Dua Lipa", "Calvin Harris", "Bruno Mars", "Kanye West"]),
            min_size=2,
            max_size=4,
            unique=True,
        ),
        st.sampled_from([" & ", ", ", " x ", " and ", " with "]),
    )
    def test_each_multi_word_artist_counts(self, names: list[str], joiner: str) -> None:
        keys = ArtistSplitter().split(joiner.join(names))
        assert {artist_key(name) for name in names} <= keys


# ---------------------------------------------------------------------------
# Related artists
# ---------------------------------------------------------------------------


class TestRelatedArtists:
    def test_links_are_one_way(self) -> None:
        related = parse_related_artists({"related": {"Bruno Mars": ["Silk Sonic"]}}, "test")
        assert related.related_to("bruno mars") == {"silk sonic"}
        assert related.related_to("silk sonic") == frozenset()
        assert related.name("silk sonic") == "Silk Sonic"
        assert related.name("nobody") == "nobody"

    def test_self_links_are_ignored_and_names_merge(self) -> None:
        related = parse_related_artists(
            {"related": {"Bruno Mars": ["bruno mars", "Silk Sonic"], "BRUNO MARS": ["X Y"]}},
            "test",
        )
        assert related.related_to("bruno mars") == {"silk sonic", "x y"}

    @pytest.mark.parametrize(
        ("data", "message"),
        [
            ({"links": {}}, "unknown keys"),
            ({"related": []}, "must be a table"),
            ({"related": {"": ["A"]}}, "empty artist"),
            ({"related": {"A": "B"}}, "must be a list"),
            ({"related": {"A": [1]}}, "must be a list"),
            ({"related": {"A": ["!!"]}}, "must be a list"),
            ({"single_acts": "A & B"}, "single_acts"),
            ({"single_acts": [""]}, "single_acts"),
        ],
    )
    def test_invalid_tables_fail_loudly(self, data: dict, message: str) -> None:
        with pytest.raises(ValueError, match=message):
            parse_related_artists(data, "related.toml")

    def test_load_file_with_starter(self, tmp_path: Path) -> None:
        path = tmp_path / "related.toml"
        path.write_bytes(
            b'single_acts = ["Tom Petty and the Heartbreakers"]\n'
            b'[related]\n"Bruno Mars" = ["The Hooligans"]\n'
        )
        related = load_related_artists(str(path), starter=True)
        assert related.related_to("bruno mars") == {"silk sonic", "hooligans"}
        assert related.single_acts == ("Tom Petty and the Heartbreakers",)
        assert load_related_artists(str(path)).related_to("bruno mars") == {"hooligans"}

    def test_no_file_and_no_starter_is_empty(self) -> None:
        assert load_related_artists(None).links == {}

    def test_missing_file(self, tmp_path: Path) -> None:
        with pytest.raises(FileNotFoundError, match="related_artists_file"):
            load_related_artists(str(tmp_path / "nope.toml"))

    def test_bad_toml(self, tmp_path: Path) -> None:
        path = tmp_path / "bad.toml"
        path.write_bytes(b"[related\n")
        with pytest.raises(ValueError, match="not valid TOML"):
            load_related_artists(path)

    def test_starter_list(self) -> None:
        related = starter_related_artists()
        assert related.related_to("bruno mars") == {"silk sonic"}
        assert related.related_to("anderson paak") >= {"silk sonic"}
        assert len(STARTER_RELATED_ARTISTS) < 40


# ---------------------------------------------------------------------------
# Rules, history and the filter
# ---------------------------------------------------------------------------


def _record(i: int, artist: str = "", title: str = "", started: float = 0.0, album: str = ""):
    return PlayRecord(f"{i}.flac", artist, album, title, started)


class TestRepeatRules:
    def test_from_playback_defaults_follow_the_old_keys(self) -> None:
        rules = RepeatRules.from_playback(PlaybackConfig(artist_repeat_window=5))
        assert (rules.track_window, rules.artist_window, rules.title_window) == (500, 5, 5)
        assert (rules.track_minutes, rules.artist_minutes, rules.title_minutes) == (0, 0, 0)

    def test_from_playback_new_keys(self) -> None:
        pb = PlaybackConfig(
            no_repeat_minutes=120,
            artist_repeat_minutes=30,
            title_repeat_window=10,
            title_repeat_minutes=240,
        )
        rules = RepeatRules.from_playback(pb, track_window=7)
        assert rules == RepeatRules(7, 120.0, 3, 30.0, 10, 240.0)
        assert rules.keep_tracks == 10
        assert rules.keep_seconds == 240 * 60

    def test_an_older_settings_object_reads_defaults(self) -> None:
        pb = SimpleNamespace(no_repeat_window=9, artist_repeat_window=2, no_repeat_minutes=True)
        assert RepeatRules.from_playback(pb) == RepeatRules(9, 0.0, 2, 0.0, 2, 0.0)

    def test_prune_keeps_what_a_count_or_minute_window_needs(self) -> None:
        rules = RepeatRules(artist_window=2, title_window=1, artist_minutes=10)
        history = deque(_record(i, started=i * 60.0) for i in range(30))
        prune_history(history, rules, now=30 * 60.0)
        # 10 minutes back from minute 30 keeps the starts after minute 20.
        assert [r.path for r in history] == [f"{i}.flac" for i in range(21, 30)]
        prune_history(history, replace(rules, artist_minutes=0), now=30 * 60.0)
        assert [r.path for r in history] == ["28.flac", "29.flac"]


class TestBuildFilter:
    def test_count_windows(self) -> None:
        history = [
            _record(1, "Old Artist", "Old Song"),
            _record(2, "Mid Artist", "Mid Song"),
            _record(3, "New Artist", "New Song"),
        ]
        rules = RepeatRules(artist_window=1, title_window=2)
        repeat, paths = build_filter(history, rules, 0.0, ArtistSplitter())
        assert repeat.artists == {"new artist"}
        assert repeat.titles == {"mid song", "new song"}
        assert paths == []

    def test_minute_windows_with_a_fake_clock(self) -> None:
        # Starts at minutes 0, 20 and 50; it is now minute 60.
        history = [
            _record(1, "Artist One", "Song One", started=0.0),
            _record(2, "Artist Two", "Song Two", started=20 * 60.0),
            _record(3, "Artist Three", "Song Three", started=50 * 60.0),
        ]
        rules = RepeatRules(
            track_window=0,
            track_minutes=61,
            artist_window=0,
            artist_minutes=30,
            title_window=0,
            title_minutes=45,
        )
        repeat, paths = build_filter(history, rules, 60 * 60.0, ArtistSplitter())
        assert repeat.artists == {"artist three"}
        assert repeat.titles == {"song two", "song three"}
        assert paths == ["1.flac", "2.flac", "3.flac"]
        # Exactly on the boundary the track may play again.
        _, paths = build_filter(history, replace(rules, track_minutes=60), 3600.0, ArtistSplitter())
        assert paths == ["2.flac", "3.flac"]

    def test_either_count_or_minutes_holds_back(self) -> None:
        history = [_record(1, "Long Ago", started=0.0), _record(2, "Just Now", started=3000.0)]
        rules = RepeatRules(artist_window=1, artist_minutes=120, title_window=0)
        repeat, _ = build_filter(history, rules, 3600.0, ArtistSplitter())
        assert repeat.artists == {"long ago", "just now"}

    def test_zero_turns_a_rule_off(self) -> None:
        history = [_record(1, "Some Artist", "Some Song", album="Some Album")]
        rules = RepeatRules(artist_window=0, title_window=0)
        repeat, paths = build_filter(history, rules, 0.0, ArtistSplitter())
        assert not repeat
        assert repeat(_entry(artist="Some Artist", title="Some Song", album="Some Album"))
        assert paths == []

    def test_related_artists_hold_back_one_way(self) -> None:
        related = parse_related_artists({"related": {"Bruno Mars": ["Silk Sonic"]}}, "test")
        rules = RepeatRules(related=related)
        repeat, _ = build_filter([_record(1, "Bruno Mars")], rules, 0.0, ArtistSplitter())
        assert repeat.reason(_entry(artist="Silk Sonic")) == "related artist"
        assert repeat.reason(_entry(artist="Bruno Mars & Anderson .Paak")) == "same artist"
        reverse, _ = build_filter([_record(1, "Silk Sonic")], rules, 0.0, ArtistSplitter())
        assert reverse.reason(_entry(artist="Bruno Mars")) is None

    def test_a_featured_artist_holds_back_their_own_tracks(self) -> None:
        history = [_record(1, "Mark Ronson", "Uptown Funk (feat. Bruno Mars)")]
        repeat, _ = build_filter(history, RepeatRules(), 0.0, ArtistSplitter())
        assert repeat.reason(_entry(artist="Bruno Mars", title="Grenade")) == "same artist"

    def test_reasons_and_summary(self) -> None:
        history = [_record(1, "Some Artist", "Jingle Bells", album="Xmas")]
        repeat, _ = build_filter(history, RepeatRules(), 0.0, ArtistSplitter())
        candidates = [
            _entry("a", artist="Some Artist"),
            _entry("b", artist="Other Act", album="XMAS"),
            _entry("c", artist="Other Act", title="Jingle Bells (Live)"),
            _entry("d", artist="Other Act", title="Silent Night"),
        ]
        assert [repeat(entry) for entry in candidates] == [False, False, False, True]
        assert repeat.held_back == {"a": "same artist", "b": "same album", "c": "same title"}
        assert repeat.summary() == "1 same artist, 1 same album, 1 same title"

    def test_empty_filter_lets_everything_through(self) -> None:
        repeat = RepeatFilter()
        assert not repeat
        assert repeat(_entry(artist="Anyone", title="Anything"))
        assert repeat.summary() == ""


class TestPlaybackConfigRepeatKeys:
    def test_defaults_keep_the_old_behaviour(self) -> None:
        pb = PlaybackConfig()
        assert pb.no_repeat_minutes == pb.artist_repeat_minutes == pb.title_repeat_minutes == 0
        assert pb.title_repeat_window is None
        assert pb.related_artists_file is None
        assert pb.related_artists_starter is False

    def test_old_config_keys_still_load(self) -> None:
        pb = PlaybackConfig.from_dict({"no_repeat_window": 50, "artist_repeat_window": 4})
        rules = RepeatRules.from_playback(pb)
        assert (rules.track_window, rules.artist_window, rules.title_window) == (50, 4, 4)

    def test_valid_new_keys(self) -> None:
        pb = PlaybackConfig.from_dict(
            {
                "no_repeat_minutes": 90,
                "artist_repeat_minutes": 30.5,
                "title_repeat_window": 0,
                "title_repeat_minutes": MAX_REPEAT_MINUTES,
                "related_artists_file": "~/related.toml",
                "related_artists_starter": True,
            }
        )
        assert pb.no_repeat_minutes == 90.0
        assert isinstance(pb.no_repeat_minutes, float)
        assert pb.title_repeat_window == 0
        assert pb.related_artists_file == str(Path("~/related.toml").expanduser())

    def test_empty_file_means_none(self) -> None:
        assert PlaybackConfig(related_artists_file="").related_artists_file is None

    @pytest.mark.parametrize(
        ("key", "value", "error"),
        [
            ("no_repeat_minutes", -1, ValueError),
            ("artist_repeat_minutes", MAX_REPEAT_MINUTES + 1, ValueError),
            ("title_repeat_minutes", "60", TypeError),
            ("no_repeat_minutes", True, TypeError),
            ("title_repeat_window", -1, ValueError),
            ("title_repeat_window", 2.5, TypeError),
            ("title_repeat_window", True, TypeError),
            ("related_artists_file", 3, TypeError),
            ("related_artists_starter", "yes", TypeError),
        ],
    )
    def test_invalid_values_fail_loudly(self, key: str, value: object, error: type) -> None:
        with pytest.raises(error, match=f"playback.{key}"):
            PlaybackConfig.from_dict({key: value})
