"""Every pick mode that applied repeat rules before honours the richer ones."""

from __future__ import annotations

import logging
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import faiss
import numpy as np
import pytest

from autodj.clockwheel import ClockSlot
from autodj.config import PlaybackConfig
from autodj.indexer import FEATURE_DIM
from autodj.player import Player, PlayerState, apply_repeat_windows
from autodj.repeat_rules import RepeatRules
from autodj.repeat_settings import RepeatSettings
from autodj.similarity import SimilarityIndex
from tests.unit._fakes import make_cfg_mock, make_entry

# (artist, title).  Track 0 has just played; only track 4 is allowed after it.
TRACKS = [
    ("Bruno Mars", "Uptown Funk"),
    ("Silk Sonic", "Leave the Door Open"),  # related to Bruno Mars (starter list)
    ("Mark Ronson feat. Bruno Mars", "Something Else"),  # same artist, split
    ("Other Artist", "Uptown Funk (Live)"),  # same title, another version
    ("Allowed Artist", "Fine Song"),
]
HELD_BACK = {1: "related artist", 2: "same artist", 3: "same title"}


def _sim(tracks: list[tuple[str, str]]) -> SimilarityIndex:
    rng = np.random.default_rng(7)
    vectors = rng.standard_normal((len(tracks), FEATURE_DIM)).astype(np.float32)
    vectors /= np.linalg.norm(vectors, axis=1, keepdims=True)
    index = faiss.IndexFlatIP(FEATURE_DIM)
    index.add(vectors)
    entries = []
    for i, (artist, title) in enumerate(tracks):
        entry = make_entry(i)
        entry.artist, entry.title, entry.album = artist, title, f"Album {i}"
        entries.append(entry)
    return SimilarityIndex(faiss_index=index, entries=entries)


def _player(
    pick_mode: str = "similarity", tracks=TRACKS, *, repeat_rules: dict | None = None
) -> Player:
    cfg = make_cfg_mock()
    defaults = {
        "track": {"tracks": 1},
        "use_related_artists": True,
        "related_artists_file": str(
            Path(__file__).parents[2] / "src" / "autodj" / "data" / "related-artists.toml"
        ),
    }
    cfg.playback = PlaybackConfig(
        pick_mode=pick_mode,
        repeat_rules=RepeatSettings.model_validate({**defaults, **(repeat_rules or {})}),
    )
    player = Player(cfg, _sim(tracks), dry_run=True)
    player._pure_shuffle = pick_mode == "random"
    return player


def _library(player: Player) -> SimpleNamespace:
    entries = player._sim.entries_snapshot()
    return SimpleNamespace(
        entries_snapshot=lambda kind="music": () if kind == "liner" else entries,
        last_played=lambda: {},
        remember_setting=lambda *_args: None,
    )


def _played_first(player: Player):
    first = player.playable_entries()[0]
    with player._pick_lock:
        player._state.record_played(first)
    return first


@pytest.mark.parametrize("mode", ["similarity", "random", "least_recent"])
def test_pick_modes_honour_artist_related_and_title_rules(mode: str) -> None:
    player = _player(mode)
    if mode == "least_recent":
        player._library = _library(player)
    current = _played_first(player)
    for _ in range(5):
        context = player._pick_context()
        picked, _how = player._choose_by_mode(current, context)
        assert picked.title == "Fine Song"
        if mode != "similarity":  # similarity only meets its nearest neighbours
            assert {player.playable_entries()[i].path: r for i, r in HELD_BACK.items()} == (
                context.repeat.held_back
            )


def test_anchored_similarity_honours_the_rules() -> None:
    player = _player()
    current = _played_first(player)
    player._anchor_to_seed, player._seed_path = True, current.path
    picked, how = player._choose_by_mode(current, player._pick_context())
    assert (picked.title, how) == ("Fine Song", "anchored")


def test_clock_pick_honours_the_rules() -> None:
    player = _player("clock")
    player._library = _library(player)
    player._clock = {"cursor": 0}
    player._clock_slots = [ClockSlot(folder="Z:/Music", selection="random")]
    _played_first(player)
    for _ in range(5):
        picked = player._clock_pick(player._pick_context(), 0)
        assert picked.entry.title == "Fine Song"


def test_clock_slot_without_rules_ignores_them() -> None:
    player = _player("clock")
    player._library = _library(player)
    player._clock = {"cursor": 0}
    player._clock_slots = [ClockSlot(folder="Z:/Music", selection="random", enforce_rules=False)]
    _played_first(player)
    titles = {player._clock_pick(player._pick_context(), 0).entry.title for _ in range(60)}
    assert "Leave the Door Open" in titles


def test_discovery_honours_the_rules() -> None:
    player = _player()
    current = _played_first(player)
    player._discovery_every, player._state.discovery_enabled = 1, True
    for track_number in range(1, 6):
        player._state.track_number = track_number
        picked, how = player._choose_by_mode(current, player._pick_context())
        assert (picked.title, how) == ("Fine Song", "discovery")


def test_queue_and_fallback_ignore_the_rules() -> None:
    player = _player()
    current = _played_first(player)
    blocked = player.playable_entries()[1]
    player._state.queue.append(blocked)
    picked, how = player._choose_next(current, player._pick_context())
    assert (picked, how) == (blocked, "queue")
    picked = player._fallback_pick(current, player._pick_context(), ValueError("test"))
    assert picked.path != current.path


def test_rules_relax_when_nothing_else_can_play() -> None:
    tracks = [("Same Artist", f"Song {i}") for i in range(4)]
    for mode in ("similarity", "random"):
        player = _player(mode, tracks)
        current = _played_first(player)
        picked, _how = player._choose_by_mode(current, player._pick_context())
        assert picked.path != current.path


def test_minute_windows_use_the_state_clock() -> None:
    now = [0.0]
    player = _player(
        "random",
        repeat_rules={
            "track": {"tracks": 0, "minutes": 60},
            "artist": {"tracks": 0, "minutes": 30},
            "album": {"tracks": 0},
            "title": {"tracks": 0},
        },
    )
    player._state.clock = lambda: now[0]
    first = _played_first(player)
    now[0] = 29 * 60.0
    context = player._pick_context()
    assert first.path in context.recent
    assert context.repeat.reason(player.playable_entries()[2]) == "same artist"
    now[0] = 31 * 60.0
    context = player._pick_context()
    assert first.path in context.recent  # the track window is still 60 minutes
    assert context.repeat.reason(player.playable_entries()[2]) is None
    now[0] = 61 * 60.0
    assert first.path not in player._pick_context().recent


def test_track_count_window_and_minutes_combine() -> None:
    player = _player("random", repeat_rules={"track": {"tracks": 1, "minutes": 60}})
    player._state.clock = lambda: 0.0
    entries = player.playable_entries()
    with player._pick_lock:
        for entry in entries[:3]:
            player._state.record_played(entry)
    recent = list(player._pick_context().recent)
    # no_repeat_window = 1 keeps the last path; the minute window adds the others.
    assert sorted(recent) == sorted(entry.path for entry in entries[:3])


def test_the_pick_logs_which_rules_held_tracks_back(caplog) -> None:
    player = _player("random")
    current = _played_first(player)
    caplog.set_level(logging.INFO, logger="autodj.player")
    with caplog.at_level(logging.DEBUG, logger="autodj.player.repeat"):
        player._choose_by_mode(current, player._pick_context())
    assert "1 same artist, 1 related artist, 1 same title" in caplog.text
    rows = {
        record.getMessage() for record in caplog.records if record.name == "autodj.player.repeat"
    }
    entries = player.playable_entries()
    assert rows == {
        "Candidate excluded from pool by repeat rule (rules may be relaxed): "
        f"candidate={entries[i].path} reason={reason} selected={entries[4].path}"
        for i, reason in HELD_BACK.items()
    }


def test_related_artists_file_is_read_at_start(tmp_path: Path) -> None:
    path = tmp_path / "related.toml"
    path.write_bytes(b'[related]\n"Other Artist" = ["Allowed Artist"]\n')
    player = _player(
        "random", repeat_rules={"related_artists_file": str(path), "use_related_artists": True}
    )
    with player._pick_lock:
        player._state.record_played(player.playable_entries()[3])
    context = player._pick_context()
    assert context.repeat.reason(player.playable_entries()[4]) == "related artist"


def test_known_artists_split_one_word_duos() -> None:
    tracks = [("Rihanna", "One"), ("Drake", "Two"), ("Rihanna & Drake", "Three"), ("Zed", "Z")]
    player = _player("random", tracks, repeat_rules={"use_related_artists": False})
    with player._pick_lock:
        player._state.record_played(player.playable_entries()[2])
    context = player._pick_context()
    assert context.repeat.reason(player.playable_entries()[0]) == "same artist"
    assert context.repeat.reason(player.playable_entries()[1]) == "same artist"
    assert player._track_keys() is player._track_keys()


def test_apply_repeat_windows_keeps_history_and_reads_new_keys() -> None:
    player = _player("random")
    _played_first(player)
    player._cfg.playback.repeat_rules.title.tracks = 7
    player._cfg.playback.repeat_rules.artist.minutes = 15.0
    apply_repeat_windows(player)
    rules = player._state.rules
    assert (rules.title_window, rules.artist_minutes) == (7, 15.0)
    assert rules.related.related_to("bruno mars") == {"silk sonic"}
    assert len(player._state.play_history) == 1
    before = player._state.play_history
    apply_repeat_windows(player)  # unchanged rules: nothing is rebuilt
    assert player._state.play_history is before


def test_player_state_built_from_rules_sets_the_old_windows() -> None:
    state = PlayerState(repeat_rules=RepeatRules(track_window=9, artist_window=4))
    assert (state.no_repeat_window, state.artist_repeat_window) == (9, 4)
    assert state.recently_played.maxlen == 9
    state.resize_repeat_windows(5, 2)
    assert (state.rules.track_window, state.rules.artist_window, state.rules.title_window) == (
        5,
        2,
        2,
    )


# ---------------------------------------------------------------------------
# Plays from before a restart
# ---------------------------------------------------------------------------


class _Catalog:
    """Stands in for LibraryCatalog.recent_plays."""

    def __init__(self, rows: list[dict]) -> None:
        self.rows = rows
        self.limits: list[int] = []

    def recent_plays(self, limit: int) -> list[dict]:
        self.limits.append(limit)
        return self.rows[-limit:] if limit else []


def _saved_play(entry, minutes_ago: float) -> dict:
    when = datetime.now(UTC) - timedelta(minutes=minutes_ago)
    return {
        "path": entry.path,
        "artist": entry.artist,
        "album": entry.album,
        "title": entry.title,
        "played_at": when.isoformat(),
    }


def test_a_minute_window_holds_across_a_restart() -> None:
    repeat_rules = {
        "track": {"tracks": 0, "minutes": 60},
        "artist": {"tracks": 0, "minutes": 60},
        "album": {"tracks": 0},
        "title": {"tracks": 0},
    }
    before = _player("random", repeat_rules=repeat_rules)
    entries = before.playable_entries()
    catalog = _Catalog([_saved_play(entries[0], minutes_ago=30)])
    # A new player is a restart: it remembers nothing until it reads the catalog.
    after = _player("random", repeat_rules=repeat_rules)
    assert after._pick_context().repeat.reason(entries[2]) is None
    assert after.restore_repeat_history(catalog) == 1
    context = after._pick_context()
    assert entries[0].path in context.recent
    assert context.repeat.reason(entries[2]) == "same artist"
    assert context.repeat.reason(entries[1]) == "related artist"
    assert context.repeat.reason(entries[4]) is None
    for _ in range(5):
        picked, _how = after._choose_by_mode(entries[4], after._pick_context())
        assert picked.title not in {"Uptown Funk", "Something Else", "Leave the Door Open"}


def test_an_old_play_is_outside_the_minute_window_after_a_restart() -> None:
    player = _player(
        "random",
        repeat_rules={
            "track": {"tracks": 0},
            "artist": {"tracks": 0, "minutes": 60},
        },
    )
    entries = player.playable_entries()
    player.restore_repeat_history(_Catalog([_saved_play(entries[0], minutes_ago=61)]))
    assert player._pick_context().repeat.reason(entries[2]) is None


def test_count_windows_hold_across_a_restart() -> None:
    player = _player(
        "random",
        repeat_rules={
            "track": {"tracks": 2},
            "artist": {"tracks": 1},
            "album": {"tracks": 0},
            "title": {"tracks": 0},
        },
    )
    entries = player.playable_entries()
    rows = [_saved_play(entries[i], minutes_ago=600 - i) for i in (3, 0)]
    catalog = _Catalog(rows)
    assert player.restore_repeat_history(catalog) == 2
    assert catalog.limits == [2]  # only what the windows can look at
    context = player._pick_context()
    assert list(context.recent) == [entries[3].path, entries[0].path]
    assert context.repeat.reason(entries[2]) == "same artist"  # the latest play
    assert "other artist" not in context.repeat.artists  # outside the 1-track window
    # Restored once: a second call reads nothing.
    assert player.restore_repeat_history(catalog) == 0


def test_widening_repeat_windows_reloads_older_plays_without_duplicates() -> None:
    player = _player(
        "random",
        repeat_rules={
            "track": {"tracks": 1},
            "artist": {"tracks": 0},
            "album": {"tracks": 0},
            "title": {"tracks": 0},
        },
    )
    entries = player.playable_entries()
    player._state.record_played(entries[3])
    catalog = _Catalog(
        [
            _saved_play(entries[0], minutes_ago=10),
            _saved_play(entries[1], minutes_ago=5),
            _saved_play(entries[3], minutes_ago=1),
        ]
    )
    player.restore_repeat_history(catalog)

    player._state.set_repeat_rules(
        RepeatRules(track_window=4, artist_window=3, album_window=0, title_window=0)
    )
    player.restore_repeat_history(catalog, refresh=True)

    assert catalog.limits == [1, 4]
    assert [record.path for record in player._state.play_history] == [
        entries[0].path,
        entries[1].path,
        entries[3].path,
    ]


def test_future_and_unreadable_saved_plays_are_skipped() -> None:
    player = _player("random", repeat_rules={"track": {"tracks": 5}})
    entries = player.playable_entries()
    rows = [
        _saved_play(entries[0], minutes_ago=-30),  # the clock was set back since
        {**_saved_play(entries[1], minutes_ago=5), "played_at": "yesterday"},
        _saved_play(entries[2], minutes_ago=5),
    ]
    assert player.restore_repeat_history(_Catalog(rows)) == 1
    assert list(player._pick_context().recent) == [entries[2].path]


def test_restored_plays_come_before_this_runs_plays() -> None:
    player = _player("random", repeat_rules={"track": {"tracks": 3}})
    entries = player.playable_entries()
    _played_first(player)
    player.restore_repeat_history(_Catalog([_saved_play(entries[3], minutes_ago=10)]))
    assert list(player._state.recently_played) == [entries[3].path, entries[0].path]


# ---------------------------------------------------------------------------
# A bad related artists file
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "content",
    [b"[related\n", b"related = 3\n", b'[related]\n"A" = "B"\n', None],
    ids=["bad toml", "related not a table", "value not a list", "missing file"],
)
def test_a_bad_related_artists_file_does_not_stop_the_player(
    tmp_path: Path, caplog, content: bytes | None
) -> None:
    path = tmp_path / "related.toml"
    if content is not None:
        path.write_bytes(content)
    with caplog.at_level(logging.WARNING, logger="autodj.player"):
        player = _player(
            "random",
            repeat_rules={"related_artists_file": str(path), "use_related_artists": True},
        )
    assert str(path) in player.related_artists_problem
    assert str(path) in caplog.text
    assert player._state.rules.related.links == {}


# ---------------------------------------------------------------------------
# Clock slots relax like the other modes
# ---------------------------------------------------------------------------


def _clock_player(tracks: list[tuple[str, str]]) -> Player:
    player = _player("clock", tracks)
    player._library = _library(player)
    player._clock = {"cursor": 0}
    player._clock_slots = [ClockSlot(folder="Z:/Music", selection="random")]
    return player


def test_a_clock_slot_relaxes_the_rules_when_every_track_is_held_back(caplog) -> None:
    player = _clock_player([("Same Artist", f"Song {i}") for i in range(4)])
    current = _played_first(player)
    with caplog.at_level(logging.INFO, logger="autodj.player"):
        picked = player._clock_pick(player._pick_context(), 0)
    assert picked.entry is not None
    assert picked.entry.path != current.path
    assert "relaxing the artist, album and title rules" in caplog.text
    assert "3 same artist" in caplog.text


def test_a_clock_slot_relaxes_the_track_window_last(caplog) -> None:
    tracks = [("Artist A", "One"), ("Artist B", "Two")]
    player = _clock_player(tracks)
    player._state.set_repeat_rules(RepeatRules(track_window=5, artist_window=0, title_window=0))
    entries = player.playable_entries()
    with player._pick_lock:
        for entry in entries:
            player._state.record_played(entry)
    context = replace(player._pick_context(), protected=frozenset({entries[1].path}))
    with caplog.at_level(logging.INFO, logger="autodj.player"):
        picked = player._clock_pick(context, 0)
    assert picked.entry is entries[0]
    assert "relaxing the track window" in caplog.text


def test_a_clock_slot_with_nothing_but_protected_tracks_still_finds_nothing() -> None:
    player = _clock_player([("Artist A", "One")])
    only = player.playable_entries()[0]
    context = replace(player._pick_context(), protected=frozenset({only.path}))
    assert player._clock_pick(context, 0).entry is None


def test_the_key_cache_outlives_a_library_refresh_with_the_same_artists() -> None:
    player = _player("random")
    entries = player._sim.entries_snapshot()
    snapshot = {"music": tuple(entries)}
    player._library = SimpleNamespace(entries_snapshot=lambda kind="music": snapshot.get(kind, ()))
    first = player._track_keys()
    snapshot["music"] = tuple(entries)  # a refresh after a play: a new tuple, same tracks
    assert player._track_keys() is first
    newcomer = make_entry(99)
    newcomer.artist = "Brand New Artist"
    snapshot["music"] = (*entries, newcomer)
    assert player._track_keys() is not first


def test_history_refresh_keeps_separate_plays_of_the_same_track():
    from autodj.repeat_rules import PlayRecord

    state = PlayerState(
        repeat_rules=RepeatRules(track_window=10, artist_window=10), clock=lambda: 1000
    )
    entry = make_entry(0)
    state.play_history.append(PlayRecord.of(entry, 900))
    state.restore_history([PlayRecord.of(entry, 600), PlayRecord.of(entry, 900.001)])
    assert [record.started for record in state.play_history] == [600, 900]
