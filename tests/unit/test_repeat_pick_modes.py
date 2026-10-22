"""Every pick mode that applied repeat rules before honours the richer ones."""

from __future__ import annotations

import logging
from collections import deque
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import faiss
import numpy as np
import pytest

import autodj.clockwheel as clockwheel
from autodj.clockwheel import ClockSlot
from autodj.config import PlaybackConfig
from autodj.indexer import FEATURE_DIM
from autodj.player import Player, PlayerState, _join_recent, apply_repeat_windows
from autodj.repeat_rules import RepeatRules
from autodj.repeat_settings import RepeatSettings
from autodj.similarity import SimilarityIndex
from tests._layout import point_index
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
    pick_mode: str = "similarity",
    tracks=TRACKS,
    *,
    repeat_rules: dict | None = None,
    active_dir: Path | None = None,
) -> Player:
    cfg = make_cfg_mock()
    point_index(cfg.index, active_dir or Path(__file__).parents[2] / "src" / "autodj" / "data")
    defaults = {
        "track": {"tracks": 1},
        "artist": {"tracks": 3},
        "album": {"tracks": 3},
        "title": {"tracks": 3},
        "use_related_artists": True,
    }
    cfg.playback = PlaybackConfig(
        pick_mode=pick_mode,
        repeat_rules=RepeatSettings.model_validate({**defaults, **(repeat_rules or {})}),
    )
    player = Player(cfg, _sim(tracks), dry_run=True)
    return player


def _library(player: Player) -> SimpleNamespace:
    entries = player._sim.entries_snapshot()
    return SimpleNamespace(
        entries_snapshot=lambda kind="music": () if kind == "liner" else entries,
        ledger=SimpleNamespace(
            last_played=lambda: {},
            pending_history=lambda: [],
            remember_setting=lambda *_args: None,
        ),
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
        if mode == "random":  # Other modes stop once a candidate passes.
            assert {player.playable_entries()[i].path: r for i, r in HELD_BACK.items()} == (
                context.repeat.held_back
            )


def test_anchored_similarity_honours_the_rules() -> None:
    player = _player()
    current = _played_first(player)
    player._anchor_to_seed, player._seed_path = True, current.path
    picked, how = player._choose_by_mode(current, player._pick_context())
    assert (picked.title, how) == ("Fine Song", "anchored")


def test_similarity_with_zero_limits_excludes_current_pending_and_starting() -> None:
    zero = {name: {"tracks": 0, "minutes": 0} for name in ("track", "artist", "album", "title")}
    player = _player(repeat_rules={**zero, "use_related_artists": False})
    current, pending, starting = player.playable_entries()[:3]
    context = player._pick_context(pending=pending, starting=starting)
    assert not context.recent
    picked, _ = player._choose_by_mode(current, context)
    assert picked.path not in {current.path, pending.path, starting.path}


def test_discovery_with_zero_limits_excludes_current_pending_and_starting() -> None:
    zero = {name: {"tracks": 0, "minutes": 0} for name in ("track", "artist", "album", "title")}
    player = _player(repeat_rules={**zero, "use_related_artists": False})
    current, pending, starting = player.playable_entries()[:3]
    player._discovery_every = 1
    player._state.discovery_enabled = True
    player._state.track_number = 1
    picked = player._try_discovery(
        current, player._pick_context(pending=pending, starting=starting)
    )
    assert picked is not None
    assert picked.path not in {current.path, pending.path, starting.path}


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


@pytest.mark.parametrize("selection", ["random", "least_recent"])
def test_clock_slot_without_rules_skips_protected_and_allows_recent(selection: str) -> None:
    player = _clock_player([("Protected", "Playing"), ("Recent", "Still allowed")])
    entries = tuple(player.playable_entries())
    protected, recent = entries
    player._clock_slots = [ClockSlot(folder="Z:/Music", selection=selection, enforce_rules=False)]
    now = datetime.now(UTC)
    player._library = SimpleNamespace(
        entries_snapshot=lambda kind="music": () if kind == "liner" else entries,
        ledger=SimpleNamespace(
            last_played=lambda: {
                protected.path: now - timedelta(days=10),
                recent.path: now,
            }
        ),
    )
    context = replace(
        player._pick_context(), protected=frozenset({protected.path}), recent=deque([recent.path])
    )

    picked = player._clock_pick(context, 0)

    assert picked.entry == recent


@pytest.mark.parametrize("kind", ["music", "track"])
@pytest.mark.parametrize("unavailable", ["protected", "silent"])
def test_unenforced_clock_slot_skips_unavailable_music(kind: str, unavailable: str) -> None:
    player = _clock_player([("Artist", "Unavailable")])
    entry = player.playable_entries()[0]
    player._clock_slots = [
        ClockSlot(folder="Z:/Music", kind=kind, path=entry.path, enforce_rules=False)
    ]
    context = player._pick_context()
    if unavailable == "protected":
        context = replace(context, protected=frozenset({entry.path}), follows=entry.path)
    else:
        silent = make_entry(0)
        silent.path = entry.path
        silent.energy = 0.0
        player._library.entries_snapshot = lambda kind="music": () if kind == "liner" else (silent,)
    assert player._clock_pick(context, 0).entry is None


def test_a_clock_slot_of_protected_songs_repeats_one_rather_than_go_silent(caplog) -> None:
    player = _clock_player([("Artist A", "Playing"), ("Artist B", "Just played")])
    playing, latest = player.playable_entries()
    context = replace(
        player._pick_context(),
        protected=frozenset({playing.path, latest.path}),
        follows=playing.path,
    )

    with caplog.at_level(logging.WARNING, logger="autodj.player"):
        first = player._clock_pick(context, 0)
        second = player._clock_pick(context, 0)

    assert first.entry == latest
    assert second.entry == latest
    assert caplog.text.count("rather than stopping the music") == 1


def test_a_track_step_for_a_protected_song_plays_it_unless_it_is_playing(caplog) -> None:
    player = _clock_player([("Artist A", "Step"), ("Artist B", "Playing")])
    step, playing = player.playable_entries()
    player._clock_slots = [ClockSlot(folder="Z:/Music", kind="track", path=step.path)]
    context = replace(
        player._pick_context(),
        protected=frozenset({step.path, playing.path}),
        follows=playing.path,
    )

    with caplog.at_level(logging.WARNING, logger="autodj.player"):
        assert player._clock_pick(context, 0).entry == step
    assert "rather than stopping the music" in caplog.text
    assert player._clock_pick(replace(context, follows=step.path), 0).entry is None


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
    for mode in ("similarity", "random", "least_recent"):
        player = _player(mode, tracks)
        if mode == "least_recent":
            player._library = _library(player)
        current = _played_first(player)
        picked, _how = player._choose_by_mode(current, player._pick_context())
        assert picked.path != current.path


def test_least_recent_stops_checking_rules_after_the_oldest_eligible_track() -> None:
    player = _player("least_recent")
    player._library = _library(player)
    entries = player.playable_entries()
    now = datetime(2026, 10, 11, tzinfo=UTC)
    history = {entry.path: now for entry in entries}
    history[entries[2].path] = now - timedelta(days=3)
    history[entries[4].path] = now - timedelta(days=2)
    player._library.ledger.last_played = lambda: history
    current = _played_first(player)
    context = player._pick_context()
    picked, _how = player._choose_by_mode(current, context)
    assert picked is entries[4]
    assert context.repeat.held_back == {entries[2].path: "same artist"}


@pytest.mark.parametrize("mode", ["random", "least_recent"])
def test_automatic_start_honours_restored_repeat_history(mode: str) -> None:
    player = _player(
        mode,
        [("Same Artist", "Played"), ("Same Artist", "Blocked"), ("Other Artist", "Allowed")],
        repeat_rules={
            "track": {"tracks": 0},
            "artist": {"tracks": 3},
            "album": {"tracks": 0},
            "title": {"tracks": 0},
        },
    )
    entries = player.playable_entries()
    history = {
        entries[1].path: datetime.now(UTC) - timedelta(days=3),
        entries[2].path: datetime.now(UTC) - timedelta(days=2),
        entries[0].path: datetime.now(UTC) - timedelta(days=1),
    }
    player._library = _library(player)
    player._library.ledger.last_played = lambda: history
    assert player.restore_repeat_history(_Catalog([_saved_play(entries[0], minutes_ago=1)])) == 1

    for _ in range(5):
        assert player._random_start_entry() is entries[2]


@pytest.mark.parametrize("mode", ["random", "least_recent"])
def test_automatic_start_honours_restored_title_minutes(mode: str) -> None:
    player = _player(
        mode,
        [("Artist A", "Same Song"), ("Artist B", "Same Song (Live)"), ("Artist C", "Other")],
        repeat_rules={
            "track": {"tracks": 0},
            "artist": {"tracks": 0},
            "album": {"tracks": 0},
            "title": {"minutes": 60},
        },
    )
    entries = player.playable_entries()
    player._library = _library(player)
    assert player.restore_repeat_history(_Catalog([_saved_play(entries[0], minutes_ago=1)])) == 1
    for _ in range(5):
        assert player._random_start_entry() is entries[2]


@pytest.mark.parametrize("mode", ["random", "least_recent"])
def test_automatic_start_relaxes_repeat_rules_when_library_is_exhausted(mode: str) -> None:
    player = _player(
        mode,
        [("Same Artist", "Played"), ("Same Artist", "Only remaining")],
        repeat_rules={
            "track": {"tracks": 0},
            "artist": {"tracks": 3},
            "album": {"tracks": 0},
            "title": {"tracks": 0},
        },
    )
    entries = player.playable_entries()
    player._library = _library(player)
    assert player.restore_repeat_history(_Catalog([_saved_play(entries[0], minutes_ago=1)])) == 1
    assert player._random_start_entry() in entries


def test_automatic_start_can_repeat_a_one_track_library_after_restored_history() -> None:
    player = _player(
        "random",
        [("Only Artist", "Only Song")],
        repeat_rules={
            "track": {"tracks": 1},
            "artist": {"tracks": 3},
            "album": {"tracks": 0},
            "title": {"tracks": 0},
        },
    )
    entry = player.playable_entries()[0]
    player._library = _library(player)
    assert player.restore_repeat_history(_Catalog([_saved_play(entry, minutes_ago=1)])) == 1
    assert player._random_start_entry() is entry


def test_automatic_start_relaxes_bpm_range_when_it_excludes_every_song() -> None:
    player = _player("random", [("Artist A", "One"), ("Artist B", "Two")])
    entries = player.playable_entries()
    player._library = _library(player)
    player._bpm_range = (180.0, 190.0)
    assert player._random_start_entry() in entries


@pytest.mark.parametrize("mode", ["random", "least_recent"])
def test_disabled_repeat_limits_still_protect_playing_and_prepared_tracks(mode: str) -> None:
    player = _player(
        mode,
        [("One", "A"), ("Two", "B"), ("Three", "C")],
        repeat_rules={
            name: {"tracks": 0, "minutes": 0} for name in ("artist", "album", "title", "track")
        },
    )
    player._library = _library(player)
    current, prepared, remaining = player.playable_entries()
    context = replace(player._pick_context(), protected=frozenset({prepared.path}))
    for _ in range(5):
        assert player._pick_pure_shuffle(current, context) is remaining


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
    path = tmp_path / "related-artists.toml"
    path.write_bytes(b'[related]\n"Other Artist" = ["Allowed Artist"]\n')
    player = _player("random", active_dir=tmp_path)
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
    state.set_repeat_rules(RepeatRules(track_window=5, artist_window=2))
    assert (state.rules.track_window, state.rules.artist_window, state.rules.title_window) == (
        5,
        2,
        0,
    )


# ---------------------------------------------------------------------------
# Plays from before a restart
# ---------------------------------------------------------------------------


class _Catalog:
    """Stands in for the station ledger's history methods."""

    def __init__(self, rows: list[dict], pending: list[dict] | None = None) -> None:
        self.rows = rows
        self.pending = pending or []
        self.limits: list[int] = []

    def recent_plays(self, limit: int) -> list[dict]:
        self.limits.append(limit)
        return self.rows[-limit:] if limit else []

    def pending_history(self) -> list[dict]:
        return self.pending


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


def test_refresh_does_not_shrink_track_window_when_database_is_shorter() -> None:
    player = _player(
        "random",
        repeat_rules={
            "track": {"tracks": 4},
            "artist": {"tracks": 0},
            "album": {"tracks": 0},
            "title": {"tracks": 0},
        },
    )
    entries = player.playable_entries()
    for entry in entries[:4]:
        player._state.record_played(entry)
    assert not player._state.play_history
    expected = [entry.path for entry in entries[:4]]
    player.restore_repeat_history(_Catalog([_saved_play(entries[3], minutes_ago=1)]), refresh=True)
    assert list(player._state.recently_played) == expected


@pytest.mark.parametrize(
    ("saved", "recent", "joined"),
    [
        (["a", "b"], [], ["a", "b"]),
        ([], ["a", "b"], ["a", "b"]),
        (["b", "c"], ["a", "b", "c", "d"], ["a", "b", "c", "d"]),
        (["x", "y", "a", "b"], ["a", "b", "c"], ["x", "y", "a", "b", "c"]),
        (["x", "y"], ["a", "b"], ["x", "y", "a", "b"]),
    ],
    ids=["startup", "nothing saved", "saved inside", "saved overlaps", "no overlap"],
)
def test_saved_plays_join_the_track_window_without_shortening_it(saved, recent, joined) -> None:
    assert _join_recent(saved, recent) == joined


def test_refresh_includes_pending_history_writes() -> None:
    player = _player(
        "random",
        repeat_rules={
            "track": {"tracks": 4},
            "artist": {"tracks": 0},
            "album": {"tracks": 1},
            "title": {"tracks": 0},
        },
    )
    entry = player.playable_entries()[2]
    player._library = SimpleNamespace(
        ledger=SimpleNamespace(pending_history=lambda: [_saved_play(entry, minutes_ago=0)])
    )
    player.restore_repeat_history(
        _Catalog([], pending=[_saved_play(entry, minutes_ago=0)]), refresh=True
    )
    assert list(player._state.recently_played) == [entry.path]
    assert player._state.play_history[0].album == entry.album


# ---------------------------------------------------------------------------
# A bad related artists file
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "content",
    [b"[related\n", b"related = 3\n", b'[related]\n"A" = "B"\n'],
    ids=["bad toml", "related not a table", "value not a list"],
)
def test_a_bad_related_artists_file_does_not_stop_the_player(
    tmp_path: Path, caplog, content: bytes
) -> None:
    path = tmp_path / "related-artists.toml"
    path.write_bytes(content)
    with caplog.at_level(logging.WARNING, logger="autodj.player"):
        player = _player("random", active_dir=tmp_path)
    assert player.related_artists_problem.startswith("The related artists file is not valid")
    assert player.related_artists_problem.endswith(
        "Related artists are off until the file is fixed and repeat settings are saved."
    )
    assert str(path) not in player.related_artists_problem
    assert str(path) in caplog.text
    assert player._state.rules.related.links == {}


def test_related_artists_file_is_not_loaded_when_disabled(tmp_path: Path) -> None:
    (tmp_path / "related-artists.toml").write_text("[related\n", encoding="utf-8")
    player = _player("random", active_dir=tmp_path, repeat_rules={"use_related_artists": False})
    assert player.related_artists_problem is None
    assert player._state.rules.related.links == {}


def test_single_acts_only_apply_while_related_artists_enabled(tmp_path: Path) -> None:
    (tmp_path / "related-artists.toml").write_text(
        'single_acts = ["Calvin Harris & Dua Lipa"]\n', encoding="utf-8"
    )
    tracks = [("Calvin Harris & Dua Lipa", "One"), ("Other Artist", "Two")]
    off = _player(
        "random", tracks, active_dir=tmp_path, repeat_rules={"use_related_artists": False}
    )
    on = _player("random", tracks, active_dir=tmp_path)
    entry = off.playable_entries()[0]
    assert "calvin harris" in off._track_keys().keys(entry).artists
    assert "calvin harris" not in on._track_keys().keys(entry).artists


# ---------------------------------------------------------------------------
# Clock slots relax like the other modes
# ---------------------------------------------------------------------------


def _clock_player(tracks: list[tuple[str, str]]) -> Player:
    player = _player("clock", tracks)
    player._library = _library(player)
    player._clock = {"cursor": 0}
    player._clock_slots = [ClockSlot(folder="Z:/Music", selection="random")]
    return player


def test_clock_pick_reuses_normalized_history_across_recent_relaxation(monkeypatch) -> None:
    player = _clock_player([("Artist A", "Old"), ("Artist B", "New")])
    player._clock_slots = [ClockSlot(folder="Z:/Music", selection="least_recent")]
    entries = player.playable_entries()
    old_path, new_path = r"z:\music\SONG_0.FLAC", r"z:\music\SONG_1.FLAC"
    now = datetime.now(UTC)
    player._library.ledger.last_played = lambda: {
        old_path: now - timedelta(days=1),
        new_path: now,
    }
    calls = []
    normalize = clockwheel._normalized

    def count_normalization(path: str) -> str:
        calls.append(path)
        return normalize(path)

    monkeypatch.setattr(clockwheel, "_normalized", count_normalization)
    context = replace(player._pick_context(), recent=deque(entry.path for entry in entries))

    picked = player._clock_pick(context, 0)

    assert picked.entry == entries[0]
    assert calls.count(old_path) == calls.count(new_path) == 1


def test_a_clock_slot_relaxes_the_rules_when_every_track_is_held_back(caplog) -> None:
    player = _clock_player([("Same Artist", f"Song {i}") for i in range(4)])
    current = _played_first(player)
    with caplog.at_level(logging.INFO, logger="autodj.player"):
        picked = player._clock_pick(player._pick_context(), 0)
    assert picked.entry is not None
    assert picked.entry.path != current.path
    assert (
        "relaxing the artist, related artist, album, title, related track, daypart and quarter-hour rules"
        in caplog.text
    )
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


def test_a_clock_slot_with_nothing_but_the_playing_track_still_finds_nothing() -> None:
    player = _clock_player([("Artist A", "One")])
    only = player.playable_entries()[0]
    context = replace(player._pick_context(), protected=frozenset({only.path}), follows=only.path)
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
    state.restore_history([PlayRecord.of(entry, 600), PlayRecord.of(entry, 897)])
    assert [record.started for record in state.play_history] == [600, 900]
