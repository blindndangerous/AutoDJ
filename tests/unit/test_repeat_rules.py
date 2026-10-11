"""Repeat rules: title and artist keys, credit splitting, related artists, windows."""

from __future__ import annotations

from collections import deque
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest
from hypothesis import given
from hypothesis import strategies as st

from autodj.config import PlaybackConfig
from autodj.repeat_rules import (
    HISTORY_LIMIT,
    MAX_REPEAT_MINUTES,
    ArtistSplitter,
    PlayRecord,
    RepeatFilter,
    RepeatRules,
    TrackKeyCache,
    TrackKeys,
    album_key,
    artist_key,
    build_filter,
    featured_in_title,
    load_related_artists,
    load_related_artists_or_warn,
    parse_related_artists,
    plays_to_load,
    prune_history,
    records_from_plays,
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
            ("Mark Ronson feat. Bruno Mars", {"mark ronson", "bruno mars"}),
            ("Mark Ronson ft. Bruno Mars", {"mark ronson", "bruno mars"}),
            ("Mark Ronson featuring Bruno Mars", {"mark ronson", "bruno mars"}),
            ("Mark Ronson (feat. Bruno Mars)", {"mark ronson", "bruno mars"}),
            ("Armin van Buuren vs. Tiësto", {"armin van buuren", "tiesto"}),
            ("Rihanna;Drake", {"rihanna", "drake"}),
            ("Rihanna\x00Drake", {"rihanna", "drake"}),
            ("Calvin Harris feat. Rihanna", {"calvin harris", "rihanna"}),
        ],
        ids=[
            "ampersand",
            "comma",
            "x",
            "with",
            "and",
            "jay-z",
            "lil-nas-x",
            "orchestra",
            "feat",
            "ft",
            "featuring",
            "parenthesized-feat",
            "versus",
            "semicolon",
            "nul",
            "feat-rihanna",
        ],
    )
    def test_multi_word_and_featured_credits_split(self, credit: str, parts: set[str]) -> None:
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
        related = load_related_artists(str(path))
        assert related.related_to("bruno mars") == {"hooligans"}
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

    def test_bundled_sample_file(self) -> None:
        related = load_related_artists(
            Path(__file__).parents[2] / "src/autodj/data/related-artists.toml"
        )
        assert related.related_to("bruno mars") == {"silk sonic"}
        assert related.related_to("anderson paak") >= {"silk sonic"}


# ---------------------------------------------------------------------------
# Rules, history and the filter
# ---------------------------------------------------------------------------


def _record(i: int, artist: str = "", title: str = "", started: float = 0.0, album: str = ""):
    return PlayRecord(f"{i}.flac", artist, album, title, started)


class TestRepeatRules:
    def test_from_playback_defaults(self) -> None:
        rules = RepeatRules.from_playback(PlaybackConfig())
        assert (
            rules.track_window,
            rules.artist_window,
            rules.album_window,
            rules.title_window,
        ) == (50, 8, 0, 0)
        assert (
            rules.track_minutes,
            rules.artist_minutes,
            rules.album_minutes,
            rules.title_minutes,
        ) == (0, 0, 0, 0)

    def test_from_playback_new_keys(self) -> None:
        pb = PlaybackConfig.from_dict(
            {
                "repeat_rules": {
                    "track": {"tracks": 500, "minutes": 120},
                    "artist": {"tracks": 3, "minutes": 30},
                    "album": {"tracks": 3},
                    "title": {"tracks": 10, "minutes": 240},
                }
            }
        )
        rules = RepeatRules.from_playback(pb, track_window=7)
        assert rules == RepeatRules(7, 120.0, 3, 30.0, 10, 240.0)
        assert rules.keep_tracks == 10
        assert rules.keep_seconds == 240 * 60

    def test_prune_keeps_what_a_count_or_minute_window_needs(self) -> None:
        rules = RepeatRules(artist_window=2, title_window=1, artist_minutes=10, album_window=0)
        history = deque(_record(i, started=i * 60.0) for i in range(30))
        prune_history(history, rules, now=30 * 60.0)
        # 10 minutes back from minute 30 keeps the starts after minute 20.
        assert [r.path for r in history] == [f"{i}.flac" for i in range(21, 30)]
        prune_history(history, replace(rules, artist_minutes=0), now=30 * 60.0)
        assert [r.path for r in history] == ["28.flac", "29.flac"]


class TestBuildFilter:
    def test_album_window_is_independent_from_artist_window(self) -> None:
        history = [_record(1, "Old Artist", "Old Song", album="Old Album")]
        rules = RepeatRules(artist_window=0, album_window=1, title_window=0)
        repeat, _ = build_filter(history, rules, 0.0, ArtistSplitter())
        assert repeat.artists == set()
        assert repeat.albums == {"old album"}
        assert repeat.reason(_entry(artist="Other Artist", album="Old Album")) == "same album"
        assert repeat.reason(_entry(artist="Old Artist", album="New Album")) is None

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
        rules = RepeatRules(artist_window=0, title_window=0, album_window=0)
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
    def test_legacy_keys_migrate(self) -> None:
        pb = PlaybackConfig.from_dict({"no_repeat_window": 50, "artist_repeat_window": 4})
        rules = RepeatRules.from_playback(pb)
        assert (
            rules.track_window,
            rules.artist_window,
            rules.album_window,
            rules.title_window,
        ) == (50, 4, 4, 4)

    def test_new_keys_reject_invalid_values(self) -> None:
        for repeat_rules in (
            {"artist": {"tracks": -1}},
            {"track": {"minutes": 10081}},
            {"title": {"tracks": 2.5}},
            {"album": {"minutes": float("inf")}},
        ):
            with pytest.raises(ValueError):
                PlaybackConfig.from_dict({"repeat_rules": repeat_rules})


# ---------------------------------------------------------------------------
# Per-track keys and speed
# ---------------------------------------------------------------------------


class TestTrackKeyCache:
    def test_keys_are_worked_out_once_per_track(self, monkeypatch) -> None:
        cache = TrackKeyCache()
        calls = []
        credited = ArtistSplitter.credited
        monkeypatch.setattr(
            ArtistSplitter,
            "credited",
            lambda self, *args: calls.append(args) or credited(self, *args),
        )
        entry = _entry("a", artist="Calvin Harris & Dua Lipa", title="One Kiss (Live)", album="X")
        keys = cache.keys(entry)
        assert keys == TrackKeys(
            frozenset({"calvin harris and dua lipa", "calvin harris", "dua lipa"}), "x", "one kiss"
        )
        assert cache.keys(entry) is keys
        assert len(calls) == 1

    def test_a_retagged_track_gets_new_keys(self) -> None:
        cache = TrackKeyCache()
        assert cache.keys(_entry("a", artist="Old Name")).artists == {"old name"}
        assert cache.keys(_entry("a", artist="New Name")).artists == {"new name"}

    def test_missing_tags_give_empty_keys(self) -> None:
        keys = TrackKeyCache().keys(SimpleNamespace(path="a", artist=None, title=None, album=None))
        assert keys == TrackKeys(frozenset(), "", "")

    def test_a_large_library_pick_only_looks_keys_up(self, monkeypatch) -> None:
        """Repeated picks reuse keys computed for each track on the first pick."""
        entries = [
            _entry(
                f"e{i}.flac",
                artist=f"Artist {i % 5000} & Guest {i % 7}",
                title=f"Song {i}",
                album=f"Album {i % 900}",
            )
            for i in range(1000)
        ]
        history = [
            _record(i, f"Artist {i} & Guest 1", f"Song {i}", album=f"Album {i}") for i in range(3)
        ]
        cache = TrackKeyCache()
        repeat, _ = build_filter(history, RepeatRules(), 0.0, cache)
        import autodj.repeat_rules as rules_module

        calls = {"credited": 0, "title": 0, "album": 0}

        def count(name, original):
            def wrapped(*args, **kwargs):
                calls[name] += 1
                return original(*args, **kwargs)

            return wrapped

        monkeypatch.setattr(ArtistSplitter, "credited", count("credited", ArtistSplitter.credited))
        monkeypatch.setattr(rules_module, "title_key", count("title", rules_module.title_key))
        monkeypatch.setattr(rules_module, "album_key", count("album", rules_module.album_key))
        allowed = sum(map(repeat, entries))
        first_pick = calls.copy()
        assert first_pick == {
            "credited": len(entries),
            "title": len(entries),
            "album": len(entries),
        }

        repeat, _ = build_filter(history, RepeatRules(), 0.0, cache)
        assert sum(map(repeat, entries)) == allowed
        assert calls == first_pick


class TestHistoryBounds:
    def test_history_is_capped(self) -> None:
        rules = RepeatRules(artist_minutes=MAX_REPEAT_MINUTES)
        history = deque(_record(i, started=float(i)) for i in range(HISTORY_LIMIT + 10))
        prune_history(history, rules, now=float(HISTORY_LIMIT + 10))
        assert len(history) == HISTORY_LIMIT
        assert history[0].path == "10.flac"

    def test_plays_to_load(self) -> None:
        assert plays_to_load(RepeatRules(track_window=500, artist_window=3)) == 500
        assert plays_to_load(RepeatRules(track_window=0, artist_window=0, title_window=7)) == 7
        assert plays_to_load(RepeatRules(track_window=10, title_minutes=60)) == HISTORY_LIMIT
        assert plays_to_load(RepeatRules(track_window=9000, title_minutes=60)) == 9000
        assert plays_to_load(RepeatRules(0, 0, 0, 0, 0, 0, album_window=0)) == 0

    def test_a_start_in_the_future_never_counts_for_a_rule_without_minutes(self) -> None:
        # The wall clock was set back: the record is 100 s in the future.
        history = [_record(1, "Some Artist", "Some Song", started=100.0)]
        rules = RepeatRules(artist_window=0, title_window=0)
        repeat, paths = build_filter(history, rules, 0.0, ArtistSplitter())
        assert not repeat
        assert paths == []
        rules = RepeatRules(artist_window=0, artist_minutes=1, title_window=0)
        repeat, _ = build_filter(history, rules, 0.0, ArtistSplitter())
        assert repeat.artists == {"some artist"}  # counts as just played


class TestRecordsFromPlays:
    def test_rows_become_records(self) -> None:
        rows = [
            {
                "path": "a",
                "artist": "A",
                "album": "X",
                "title": "T",
                "played_at": "2026-01-01T00:00:00+00:00",
            },
            {
                "path": "b",
                "artist": "B",
                "album": "",
                "title": "U",
                "played_at": "2026-01-01T00:01:00Z",
            },
            {"path": "c", "artist": None, "title": "V", "played_at": "2026-01-01T00:02:00"},
        ]
        records = records_from_plays(rows, now=datetime(2026, 1, 2, tzinfo=UTC).timestamp())
        assert [r.path for r in records] == ["a", "b", "c"]
        assert records[1].started - records[0].started == 60
        assert records[2].started - records[0].started == 120  # no zone reads as UTC
        assert (records[2].artist, records[2].album) == ("", "")

    def test_bad_and_future_rows_are_skipped(self) -> None:
        now = datetime(2026, 1, 1, tzinfo=UTC).timestamp()
        rows = [
            {"path": "", "played_at": "2025-12-31T00:00:00+00:00"},
            {"path": "a", "played_at": "not a time"},
            {"path": "b", "played_at": None},
            {"path": "c", "played_at": "2026-01-01T00:00:01+00:00"},
            {"path": "d", "played_at": "2025-12-31T23:59:59+00:00"},
        ]
        assert [r.path for r in records_from_plays(rows, now)] == ["d"]


class TestLoadRelatedArtistsOrWarn:
    def test_a_good_file_has_no_problem(self, tmp_path: Path) -> None:
        path = tmp_path / "related.toml"
        path.write_bytes(b'[related]\n"A B" = ["C D"]\n')
        related, problem = load_related_artists_or_warn(str(path))
        assert problem is None
        assert related.related_to("a b") == {"c d"}

    @pytest.mark.parametrize(
        "content", [b"[related\n", b"related = 3\n", b'[related]\n"A" = "B"\n', b"\xff\xfe", None]
    )
    def test_a_bad_file_falls_back(self, tmp_path: Path, content: bytes | None) -> None:
        path = tmp_path / "related.toml"
        if content is not None:
            path.write_bytes(content)
        related, problem = load_related_artists_or_warn(str(path))
        assert problem is not None
        assert str(path) in problem
        assert related.links == {}
        related, _ = load_related_artists_or_warn(str(path))
        assert related.links == {}
