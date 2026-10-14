"""DMCA mode: the performance complement, its relax order and every pick mode."""

from __future__ import annotations

import logging
from types import SimpleNamespace

import faiss
import numpy as np
import pytest

from autodj.clockwheel import ClockSlot
from autodj.config import DmcaConfig, NowPlayingConfig, PlaybackConfig, load_config
from autodj.dmca import (
    WINDOW_SECONDS,
    complement,
    projected_history,
    relaxed,
    set_key,
)
from autodj.doctor import CheckStatus, _dmca_check
from autodj.indexer import FEATURE_DIM
from autodj.player import Player, PlayerState
from autodj.repeat_rules import (
    HISTORY_LIMIT,
    ArtistSplitter,
    PlayRecord,
    RepeatRules,
    TrackKeyCache,
    plays_to_load,
)
from autodj.repeat_settings import RepeatSettings
from autodj.similarity import SimilarityIndex
from tests.unit._fakes import make_cfg_mock, make_entry

# ---------------------------------------------------------------------------
# The complement itself
# ---------------------------------------------------------------------------


def song(number: int, artist: str = "Lead Band", album: str = "Album", title: str = ""):
    return SimpleNamespace(
        path=f"{number}.flac", artist=artist, album=album, title=title or f"Song {number}"
    )


def plays(*songs, start: float = 0.0, gap: float = 1.0) -> list[PlayRecord]:
    return [PlayRecord.of(entry, start + n * gap) for n, entry in enumerate(songs)]


def broken(history, candidate, when=None, keys=None) -> tuple[str, ...]:
    when = history[-1].started + 1 if when is None else when
    return complement(history, when, keys or TrackKeyCache()).broken(candidate)


def test_allows_agrees_with_broken_for_any_history() -> None:
    """The quick check a pick makes per track says what broken() would (#125)."""
    import random

    rng = random.Random(3)
    artists = ["Lead Band", "Other Band", "Lead Band feat. Guest", "Guest", ""]
    albums = ["Album", "Album (Disc 1)", "Album (Disc 2)", "Other", ""]
    library = [
        song(number, rng.choice(artists), rng.choice(albums), f"Song {number % 7}")
        for number in range(40)
    ]
    keys = TrackKeyCache()
    for _ in range(300):
        history = plays(*rng.choices(library, k=rng.randrange(0, 9)), gap=60.0)
        limits = complement(history, len(history) * 60.0, keys)
        for candidate in library:
            assert limits.allows(candidate) is not bool(limits.broken(candidate))
        if limits.blocks_nothing:
            assert not any(limits.broken(candidate) for candidate in library)


def test_a_fourth_album_song_breaks_the_album_limits() -> None:
    history = plays(song(1), song(2), song(3))
    assert broken(history, song(4, "Other Band")) == (
        "more than 3 songs from the album “Album” in 3 hours",
        "more than 2 songs in a row from the album “Album”",
    )


def test_a_third_album_song_in_a_row_breaks_only_the_in_a_row_limit() -> None:
    history = plays(song(1, "One Band"), song(2, "Two Band"))
    assert broken(history, song(3, "Six Band")) == (
        "more than 2 songs in a row from the album “Album”",
    )


def test_another_album_between_ends_the_run() -> None:
    history = plays(song(1, "A Band"), song(2, "B Band", "Elsewhere"), song(3, "C Band"))
    assert broken(history, song(4, "D Band")) == ()


def test_album_names_compare_like_the_repeat_rules() -> None:
    history = plays(song(1, "A Band", "Abbey Road"), song(2, "B Band", "abbey road!"))
    assert broken(history, song(3, "C Band", "ABBEY ROAD")) == (
        "more than 2 songs in a row from the album “ABBEY ROAD”",
    )


def test_the_window_is_three_hours_of_starts() -> None:
    history = plays(song(1, "A Band"), song(2, "B Band"), song(3, "C Band"), gap=60.0)
    first = history[0].started
    album_limit = "more than 3 songs from the album “Album” in 3 hours"
    # Just inside the window the first play still counts ...
    assert album_limit in broken(history, song(4, "D Band"), first + WINDOW_SECONDS - 1)
    # ... and three hours after it started, it no longer does.
    assert album_limit not in broken(history, song(4, "D Band"), first + WINDOW_SECONDS)


def test_plays_after_the_start_time_do_not_count() -> None:
    history = plays(song(1), song(2), song(3), start=100.0)
    assert broken(history, song(4, "Other"), when=100.5) == ()


def test_artist_limit_counts_four_in_three_hours() -> None:
    history = plays(*(song(n, "Lead Band", f"Album {n}") for n in range(4)), song(9, "Other", "X"))
    assert broken(history, song(5, "Lead Band", "New")) == (
        "more than 4 songs by Lead Band in 3 hours",
    )


def test_artist_in_a_row_limit_is_three() -> None:
    history = plays(*(song(n, "Lead Band", f"Album {n}") for n in range(3)))
    assert broken(history, song(5, "Lead Band", "New")) == (
        "more than 3 songs in a row by Lead Band",
    )
    assert broken(history[1:], song(5, "Lead Band", "New")) == ()


def test_featured_artists_count_through_the_repeat_rules_splitter() -> None:
    keys = TrackKeyCache(ArtistSplitter({"guest"}))
    history = plays(
        song(1, "Guest", "A"),
        song(2, "Lead Band", "B", "Tune (feat. Guest)"),
        song(3, "Other Band & Guest", "C"),
    )
    assert broken(history, song(4, "Guest", "D"), keys=keys) == (
        "more than 3 songs in a row by Guest",
    )
    assert broken(history, song(5, "Someone Else", "E", "Hit (feat. Guest)"), keys=keys) == (
        "more than 3 songs in a row by Guest",
    )


def test_different_featured_artists_are_counted_separately() -> None:
    keys = TrackKeyCache(ArtistSplitter({"one", "two"}))
    history = plays(
        song(1, "One", "A"), song(2, "Two", "B"), song(3, "One", "C"), song(4, "Two", "D")
    )
    assert broken(history, song(5, "Lead", "E", "Song (feat. One & Two)"), keys=keys) == ()


def test_numbered_discs_form_one_box_set() -> None:
    assert set_key("woodstock disc 2") == "woodstock"
    assert set_key("hits cd1") == "hits"
    assert set_key("anthology vol 3") == "anthology"
    assert set_key("box disc 1 of 4") == "box"
    assert set_key("abbey road") == ""
    assert set_key("disc 1") == ""
    history = plays(
        song(1, "A Band", "Box (Disc 1)"),
        song(2, "B Band", "Box (Disc 2)"),
        song(3, "C Band", "Box (Disc 3)"),
        song(4, "D Band", "Box (Disc 4)"),
    )
    part_of = "the box set or compilation that “Box (Disc 5)” is part of"
    assert broken(history, song(5, "E Band", "Box (Disc 5)")) == (
        f"more than 4 songs from {part_of} in 3 hours",
        f"more than 3 songs in a row from {part_of}",
    )


def test_empty_tags_count_toward_nothing() -> None:
    history = plays(*(song(n, "", "") for n in range(6)))
    assert broken(history, song(9, "", "")) == ()


def test_relaxed_takes_the_narrowest_tier_with_a_safe_track() -> None:
    limits = {"safe": (), "bad": ("x",)}.__getitem__
    assert relaxed([["bad"], ["safe", "bad"], ["safe"]], limits) == [("safe", ())]
    assert relaxed([[], ["safe"]], limits) == [("safe", ())]


def test_relaxed_falls_back_to_the_fewest_broken_limits_of_the_widest_tier() -> None:
    limits = {"bad": ("x",), "also bad": ("y",), "worse": ("x", "y")}.__getitem__
    assert relaxed([["worse"], ["worse", "bad", "also bad"], []], limits) == [
        ("bad", ("x",)),
        ("also bad", ("y",)),
    ]
    assert relaxed([[], []], limits) == []


def test_projected_history_counts_the_playing_track_and_the_queue() -> None:
    history = plays(song(1), start=1000.0)
    playing = SimpleNamespace(path="1.flac", artist="Lead", album="Album", title="", length=200)
    queued = SimpleNamespace(path="2.flac", artist="Q", album="Q", title="Q", length=100)
    records, when = projected_history(history, 1050.0, playing, [queued])
    assert [(record.path, record.started) for record in records] == [
        ("1.flac", 1000.0),
        ("2.flac", 1200.0),
    ]
    assert when == 1300.0


def test_projected_history_uses_the_played_length_when_given() -> None:
    history = plays(song(1), start=1000.0)
    playing = SimpleNamespace(path="1.flac", artist="Lead", album="Album", title="", length=220)
    queued = SimpleNamespace(path="2.flac", artist="Q", album="Q", title="Q", length=110)
    records, when = projected_history(
        history, 1050.0, playing, [queued], length=lambda entry: entry.length / 1.1
    )
    assert [record.started for record in records] == [1000.0, pytest.approx(1200.0)]
    assert when == pytest.approx(1300.0)


def test_projected_history_adds_an_unrecorded_playing_track_and_play_now_skips_it() -> None:
    playing = SimpleNamespace(path="p.flac", artist="A", album="B", title="C", length=60)
    records, when = projected_history([], 10.0, playing, [])
    assert [(record.path, record.started) for record in records] == [("p.flac", 10.0)]
    assert when == 70.0
    assert projected_history([], 10.0, None, []) == ([], 10.0)


# ---------------------------------------------------------------------------
# Config, doctor and history
# ---------------------------------------------------------------------------


def test_config_is_off_by_default_and_must_be_a_boolean(tmp_path) -> None:
    assert DmcaConfig().enabled is False
    config = tmp_path / "config.toml"
    config.write_text("[dmca]\nenabled = true\n", encoding="utf-8")
    assert load_config(config).dmca.enabled is True
    with pytest.raises(TypeError, match=r"dmca\.enabled"):
        DmcaConfig(enabled="yes")  # type: ignore[arg-type]


def test_doctor_names_the_conflicting_show_next_setting() -> None:
    on = DmcaConfig(enabled=True)
    conflict = _dmca_check(SimpleNamespace(dmca=on, nowplaying=NowPlayingConfig(show_next=True)))
    assert conflict.status is CheckStatus.WARN
    assert "[nowplaying] show_next = true" in conflict.detail
    assert "Set show_next = false" in conflict.detail
    fine = _dmca_check(SimpleNamespace(dmca=on, nowplaying=NowPlayingConfig()))
    assert (fine.status, fine.summary) == (CheckStatus.PASS, "on")
    off = _dmca_check(SimpleNamespace(dmca=DmcaConfig(), nowplaying=NowPlayingConfig()))
    assert (off.status, off.summary) == (CheckStatus.PASS, "off")


def test_history_keeps_three_hours_of_plays_in_dmca_mode() -> None:
    clock = [0.0]
    rules = RepeatRules(track_window=1, artist_window=1, title_window=1)
    state = PlayerState(repeat_rules=rules, dmca_enabled=True, clock=lambda: clock[0])
    plain = PlayerState(repeat_rules=rules, clock=lambda: clock[0])
    for n in range(5):
        clock[0] = n * 3600.0
        state.record_played(make_entry(n))
        plain.record_played(make_entry(n))
    assert [record.path for record in state.play_history] == [make_entry(n).path for n in (2, 3, 4)]
    assert len(plain.play_history) == 1
    assert plays_to_load(rules, state.dmca_seconds) == HISTORY_LIMIT
    assert plays_to_load(rules, plain.dmca_seconds) == 1


# ---------------------------------------------------------------------------
# Every pick mode
# ---------------------------------------------------------------------------

# (folder, artist, album, title).  Songs 0-2 have just played.  Song 3 is
# from the same album, song 4 features the artist who played three times
# in a row, and only song 5 keeps every limit.
TRACKS = [
    ("Rock", "Alpha Band", "Same Album", "One"),
    ("Rock", "Alpha Band", "Same Album", "Two"),
    ("Rock", "Alpha Band", "Same Album", "Three"),
    ("Rock", "Beta Band", "Same Album", "Four"),
    ("Rock", "Gamma Band", "Gamma", "Five (feat. Alpha Band)"),
    ("Jazz", "Delta Band", "Delta", "Fine"),
]


def _sim(tracks) -> SimilarityIndex:
    rng = np.random.default_rng(11)
    vectors = rng.standard_normal((len(tracks), FEATURE_DIM)).astype(np.float32)
    vectors /= np.linalg.norm(vectors, axis=1, keepdims=True)
    index = faiss.IndexFlatIP(FEATURE_DIM)
    index.add(vectors)
    entries = []
    for n, (folder, artist, album, title) in enumerate(tracks):
        entry = make_entry(n)
        entry.path = f"Z:/Music/{folder}/song_{n}.flac"
        entry.artist, entry.album, entry.title = artist, album, title
        entries.append(entry)
    return SimilarityIndex(faiss_index=index, entries=entries)


def _player(pick_mode: str = "similarity", tracks=TRACKS, *, dmca: bool = True) -> Player:
    cfg = make_cfg_mock()
    # Only the track rule (one song), so the DMCA limits are all that holds songs back.
    rules = {"track": {"tracks": 1}, "artist": {"tracks": 0}, "album": {"tracks": 0}}
    cfg.playback = PlaybackConfig(
        pick_mode=pick_mode, repeat_rules=RepeatSettings.model_validate(rules)
    )
    cfg.dmca = DmcaConfig(enabled=dmca)
    player = Player(cfg, _sim(tracks), dry_run=True)
    player._pure_shuffle = pick_mode == "random"
    return player


def _with_library(player: Player) -> Player:
    entries = player._sim.entries_snapshot()
    player._library = SimpleNamespace(
        entries_snapshot=lambda kind="music": () if kind == "liner" else entries,
        ledger=SimpleNamespace(last_played=lambda: {}, remember_setting=lambda *_args: None),
    )
    return player


def _with_clock(player: Player, folder: str = "Z:/Music", enforce_rules: bool = True) -> Player:
    _with_library(player)
    player._clock = {"cursor": 0}
    player._clock_slots = [
        ClockSlot(folder=folder, selection="random", recursive=True, enforce_rules=enforce_rules)
    ]
    return player


def _play_first_three(player: Player):
    entries = player.playable_entries()
    with player._pick_lock:
        for entry in entries[:3]:
            player._state.record_played(entry)
    return entries[2]


@pytest.mark.parametrize("mode", ["similarity", "random", "least_recent"])
def test_every_pick_mode_keeps_the_complement(mode: str) -> None:
    player = _player(mode)
    if mode == "least_recent":
        _with_library(player)
    current = _play_first_three(player)
    for _ in range(8):
        picked, _how = player._choose_next(current, player._pick_context())
        assert picked.title == "Fine"


def test_without_dmca_mode_the_limits_do_not_apply() -> None:
    player = _player("random", dmca=False)
    current = _play_first_three(player)
    titles = {player._choose_next(current, player._pick_context())[0].title for _ in range(60)}
    assert {"Four", "Five (feat. Alpha Band)"} <= titles


def test_discovery_keeps_the_complement() -> None:
    player = _player()
    current = _play_first_three(player)
    player._discovery_every, player._state.discovery_enabled = 1, True
    for track_number in range(1, 6):
        player._state.track_number = track_number
        picked, _how = player._choose_next(current, player._pick_context())
        assert picked.title == "Fine"


@pytest.mark.parametrize("enforce_rules", [True, False])
def test_clock_slots_keep_the_complement_even_without_repeat_rules(enforce_rules: bool) -> None:
    player = _with_clock(_player("clock"), enforce_rules=enforce_rules)
    _play_first_three(player)
    for _ in range(8):
        assert player._clock_pick(player._pick_context(), 0).entry.title == "Fine"


def test_a_clock_with_only_breaking_songs_plays_a_safe_one_from_the_library(caplog) -> None:
    player = _with_clock(_player("clock"), folder="Z:/Music/Rock")
    current = _play_first_three(player)
    with caplog.at_level(logging.INFO, logger="autodj.player"):
        picked, mode, _liners, _cursor, _key = player._choose_station_next(
            current, player._pick_context()
        )
    assert (picked.title, mode) == ("Fine", "clock")
    assert "plays instead" in caplog.text


def test_when_nothing_keeps_the_limits_music_still_plays_and_a_warning_names_them(
    caplog,
) -> None:
    tracks = [("Rock", "Alpha Band", "Same Album", f"Song {n}") for n in range(5)]
    player = _player("random", tracks)
    current = _play_first_three(player)
    with caplog.at_level(logging.WARNING, logger="autodj.player"):
        picked, _how = player._choose_next(current, player._pick_context())
    assert picked.path != current.path
    assert "no track keeps every limit" in caplog.text
    assert "more than 3 songs from the album “Same Album” in 3 hours" in caplog.text


def test_a_queued_song_plays_and_the_broken_limit_is_logged(caplog) -> None:
    player = _player()
    current = _play_first_three(player)
    blocked = player.playable_entries()[3]
    player._state.queue.append(blocked)
    with caplog.at_level(logging.WARNING, logger="autodj.player"):
        picked, how = player._choose_next(current, player._pick_context())
    assert (picked, how) == (blocked, "queue")
    assert "plays although it breaks the limit" in caplog.text
    assert "“Same Album”" in caplog.text


def test_the_fallback_pick_keeps_the_complement(monkeypatch) -> None:
    player = _player()
    current = _play_first_three(player)

    def broken_search(*_args, **_kwargs):
        raise RuntimeError("search failed")

    monkeypatch.setattr(player._sim, "find_next_for_path", broken_search)
    for _ in range(8):
        picked, how = player._choose_next(current, player._pick_context())
        assert (picked.title, how) == ("Fine", "fallback")


@pytest.mark.parametrize("mode", ["random", "least_recent", "clock"])
def test_a_set_starts_with_a_song_that_keeps_the_complement(mode: str) -> None:
    player = _with_clock(_player(mode)) if mode == "clock" else _with_library(_player(mode))
    _play_first_three(player)
    for _ in range(8):
        assert player._random_start_entry().title == "Fine"


def test_a_start_without_a_catalog_keeps_the_complement() -> None:
    player = _player("random")
    _play_first_three(player)
    for _ in range(8):
        assert player._random_start_entry().title == "Fine"


def test_with_nothing_else_to_play_the_pick_stands_and_a_warning_names_the_limit(
    caplog,
) -> None:
    tracks = [("Rock", "Alpha Band", "Same Album", f"Song {n}") for n in range(2)]
    player = _player("random", tracks)
    first, second = player.playable_entries()
    with player._pick_lock:
        player._state.record_played(first)
        player._state.record_played(second)
    context = player._pick_context(pending=first)
    with caplog.at_level(logging.WARNING, logger="autodj.player"):
        assert player._dmca_checked(second, context) is second
    assert "nothing else can play" in caplog.text


def _with_category_clock(player: Player, titles: set[str]) -> Player:
    """A clock of one category step holding the songs with *titles*."""
    _with_library(player)
    entries = player._sim.entries_snapshot()
    paths = frozenset(entry.path for entry in entries if entry.title in titles)
    player._library.categories = SimpleNamespace(members=lambda: {"c1": ("Rock gold", paths)})
    player._clock = {"cursor": 0}
    player._clock_slots = [ClockSlot(kind="category", category="c1", selection="random")]
    return player


def test_category_steps_keep_the_complement() -> None:
    player = _with_category_clock(_player("clock"), {"Four", "Five (feat. Alpha Band)", "Fine"})
    _play_first_three(player)
    for _ in range(8):
        assert player._clock_pick(player._pick_context(), 0).entry.title == "Fine"


def test_a_category_of_only_breaking_songs_plays_a_safe_one_from_the_library(caplog) -> None:
    player = _with_category_clock(_player("clock"), {"Four", "Five (feat. Alpha Band)"})
    current = _play_first_three(player)
    with caplog.at_level(logging.INFO, logger="autodj.player"):
        picked, mode, _liners, _cursor, _key = player._choose_station_next(
            current, player._pick_context()
        )
    assert (picked.title, mode) == ("Fine", "clock")


def test_an_empty_or_failing_category_never_stops_the_station(caplog) -> None:
    player = _with_category_clock(_player("clock"), set())
    player._dry_run = False
    current = _play_first_three(player)
    with caplog.at_level(logging.WARNING, logger="autodj.player"):
        for _ in range(3):
            picked, mode, _liners, _cursor, _key = player._choose_station_next(
                current, player._pick_context()
            )
            assert (picked.title, mode) == ("Fine", "fallback")
        assert player._random_start_entry().title == "Fine"
    assert caplog.text.count("Clock selection skipped:") == 1
    assert "picking one at random" in caplog.text
    assert "NoneType" not in caplog.text

    def broken():
        raise OSError("disk gone")

    player._library.categories = SimpleNamespace(members=broken)
    with caplog.at_level(logging.WARNING, logger="autodj.player"):
        picked, mode, *_rest = player._choose_station_next(current, player._pick_context())
    assert (picked.title, mode) == ("Fine", "fallback")
    assert "rotation categories could not be read" in caplog.text
    assert "Categories could not be read" in player._clock_status
    assert "the category no longer exists" not in player._clock_status
