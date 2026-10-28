"""Liners, ads, matched intros and other inserted audio in the play history (#601).

Each inserted item that airs is a ``play_history`` row with its own kind.
The repeat rules, the music History view, the now-playing history and the
royalty reports keep counting songs only, unless asked for everything.
"""

from __future__ import annotations

import sqlite3
from contextlib import closing
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import numpy as np
import pytest

from autodj._bridge import PlayerBridge
from autodj.catalog import LibraryCatalog
from autodj.history_kinds import INSERT_KINDS, history_kind
from autodj.playlog import plays_between, soundexchange_report
from autodj.station_ledger import StationLedger
from autodj.station_store import StationStore
from tests.unit.test_library import make_entry

T0 = datetime(2026, 10, 1, 12, tzinfo=UTC)


def _at(seconds: float) -> str:
    return (T0 + timedelta(seconds=seconds)).isoformat()


def _song(event_id: str, path: str, seconds: float, duration: float = 180.0) -> dict:
    return {
        "event_id": event_id,
        "path": path,
        "title": f"Song {event_id}",
        "artist": "Artist",
        "duration": duration,
        "played_at": _at(seconds),
        "performances": 3,
    }


def _insert(event_id: str, kind: str, seconds: float, path: str | None = None) -> dict:
    return {
        "event_id": event_id,
        "path": path,
        "title": f"{kind} {event_id}",
        "artist": "",
        "duration": 8.0,
        "played_at": _at(seconds),
        "kind": kind,
    }


@pytest.fixture
def store(tmp_path: Path):
    opened = StationStore(tmp_path / "station.db", "default")
    try:
        yield opened
    finally:
        opened.close()


def _mixed(store: StationStore, music: str) -> None:
    store.write_history(
        [
            _song("a", music, 0),
            # A liner over the handoff, 170 s into the first song.
            _insert("liner", "liner", 170, path=str(Path(music).with_name("id.wav"))),
            _insert("intro", "matched_intro", 175),
            _song("b", music, 178),
            _insert("check", "time_check", 200),
            _insert("spot", "ad", 210),
        ],
        [],
    )


def test_the_kinds_are_the_documented_vocabulary() -> None:
    assert {"liner", "ad", "matched_intro", "time_check", "cart", "voice_track"} <= INSERT_KINDS
    assert not {"song", "live"} & INSERT_KINDS
    assert history_kind({"kind": "song", "track_kind": "liner"}) == "liner"
    assert history_kind({"kind": "song", "track_kind": "music"}) == "song"
    assert history_kind({}) == "song"
    assert history_kind({"kind": "matched_intro"}) == "matched_intro"
    assert history_kind({"kind": "live"}) == "live"


def test_inserted_items_are_stored_with_their_kind_and_no_path(store, tmp_path) -> None:
    _mixed(store, str(tmp_path / "song.flac"))
    rows = store.recent_history(10)
    assert [(row["event_id"], row["kind"]) for row in rows] == [
        ("a", "song"),
        ("liner", "liner"),
        ("intro", "matched_intro"),
        ("b", "song"),
        ("check", "time_check"),
        ("spot", "ad"),
    ]
    assert rows[2]["path"] is None
    assert rows[0]["track_kind"] == ""


def test_an_unknown_kind_is_still_a_song(store, tmp_path) -> None:
    store.write_history([{**_song("x", str(tmp_path / "x.flac"), 0), "kind": "mystery"}], [])
    assert store.recent_history(1)[0]["kind"] == "song"


def test_an_inserted_item_without_a_title_or_time_is_refused(store) -> None:
    bad = _insert("bad", "liner", 0)
    del bad["played_at"]
    store.write_history([bad, {**_insert("untitled", "ad", 0), "title": None}], [])
    assert store.recent_history(10) == []


def test_repeat_rules_count_songs_only(store, tmp_path) -> None:
    """ADR-0006-07."""
    _mixed(store, str(tmp_path / "song.flac"))
    assert [row["title"] for row in store.recent_plays(10)] == ["Song a", "Song b"]


def test_the_play_log_leaves_inserted_items_out_and_keeps_song_lengths(store, tmp_path) -> None:
    """ADR-0006-07."""
    _mixed(store, str(tmp_path / "song.flac"))
    rows = store.play_log(T0 - timedelta(hours=1), T0 + timedelta(hours=1))
    assert [(row["title"], row["kind"]) for row in rows] == [
        ("Song a", "song"),
        ("Song b", "song"),
    ]
    # The next song, not the liner over the handoff, ends the first one.
    assert rows[0]["following_at"] == _at(178)
    assert rows[1]["following_at"] is None
    assert [row["title"] for row in store.play_log()] == ["Song a", "Song b"]


def test_the_play_log_can_include_inserted_items(store, tmp_path) -> None:
    _mixed(store, str(tmp_path / "song.flac"))
    rows = store.play_log(T0 - timedelta(hours=1), T0 + timedelta(hours=1), include_inserts=True)
    assert [row["kind"] for row in rows] == [
        "song",
        "liner",
        "matched_intro",
        "song",
        "time_check",
        "ad",
    ]
    assert rows[0]["following_at"] == _at(178)
    # An inserted item keeps its own length: nothing cuts it short.
    assert rows[1]["following_at"] is None


def test_a_database_written_before_inserted_items_keeps_working(tmp_path: Path) -> None:
    """ADR-0006-07."""
    path = tmp_path / "station.db"
    with closing(sqlite3.connect(path)) as db:
        db.executescript(
            "CREATE TABLE play_history (event_id TEXT PRIMARY KEY NOT NULL, entry_id TEXT, "
            "path TEXT, title TEXT NOT NULL, artist TEXT NOT NULL, duration REAL NOT NULL, "
            "played_at TEXT NOT NULL, interrupted INTEGER NOT NULL DEFAULT 0, "
            "performances INTEGER, station TEXT NOT NULL DEFAULT 'default', "
            "kind TEXT NOT NULL DEFAULT 'song', ended_at TEXT, "
            "album TEXT NOT NULL DEFAULT '', track_kind TEXT NOT NULL DEFAULT '');"
            "INSERT INTO play_history (event_id,path,title,artist,duration,played_at,"
            "track_kind) VALUES ('old','/m/old.flac','Old','A',100,'2026-09-01T12:00:00+00:00',"
            "'music'), ('clip','/l/id.wav','ID','',5,'2026-09-01T12:01:40+00:00','liner');"
        )
        db.commit()
    opened = StationStore(path, "default")
    try:
        opened.write_history([_insert("new", "matched_intro", 0)], [])
        assert [row["kind"] for row in opened.recent_history(10)] == [
            "song",
            "song",
            "matched_intro",
        ]
        assert [history_kind(row) for row in opened.recent_history(10)] == [
            "song",
            "liner",
            "matched_intro",
        ]
        assert [row["title"] for row in opened.recent_plays(10)] == ["Old"]
        start = datetime(2026, 9, 1, tzinfo=UTC)
        assert [row["title"] for row in opened.play_log(start, start + timedelta(days=1))] == [
            "Old",
            "ID",
        ]
    finally:
        opened.close()


def test_pending_rows_for_the_repeat_rules_leave_inserted_items_out(store, tmp_path) -> None:
    """ADR-0006-07."""
    ledger = StationLedger(store)
    ledger.remember_history(_song("a", str(tmp_path / "a.flac"), 0))
    ledger.remember_history(_insert("liner", "liner", 5, path=str(tmp_path / "id.wav")))
    assert [row["event_id"] for row in ledger.pending_history()] == ["a"]
    ledger.flush()
    assert [row["kind"] for row in ledger.recent_history(10)] == ["song", "liner"]


@pytest.fixture
def catalog(tmp_path: Path):
    music = tmp_path / "Music"
    music.mkdir()
    song = music / "song.flac"
    song.write_bytes(b"audio")
    with LibraryCatalog(tmp_path / "library.db") as opened:
        opened.add_source(music)
        opened.put_entry(make_entry(song))
        yield opened, str(song)


def test_the_export_counts_songs_unless_asked_for_everything(catalog, store) -> None:
    """ADR-0006-07."""
    library, song = catalog
    _mixed(store, song)
    start, end = T0 - timedelta(hours=1), T0 + timedelta(hours=1)

    def read(_path: str) -> tuple[str, str]:
        return "", ""

    songs = plays_between(library, start, end, read_tags=read, station_store=store)
    assert [play.kind for play in songs] == ["song", "song"]
    assert songs[0].duration == 178
    everything = plays_between(
        library, start, end, read_tags=read, station_store=store, include_inserts=True
    )
    assert [play.kind for play in everything] == [
        "song",
        "liner",
        "matched_intro",
        "song",
        "time_check",
        "ad",
    ]
    assert everything[0].duration == 178
    assert everything[1].duration == 8
    # SoundExchange reports songs only, whatever it is given.
    report = soundexchange_report(everything, "Example", "A")
    assert "liner liner" not in report
    assert "matched_intro intro" not in report
    assert report.count("Song b") == 1


# ---------------------------------------------------------------------------
# Recording: the bridge, the player's handoff events and the liner scheduler
# ---------------------------------------------------------------------------


def _bridge(ledger=None, library=None) -> PlayerBridge:
    return PlayerBridge(player=MagicMock(), sim=MagicMock(), ledger=ledger, library=library)


def test_the_bridge_records_an_inserted_item_without_disturbing_song_history() -> None:
    """ADR-0006-08."""
    ledger = MagicMock()
    bridge = _bridge(ledger=ledger, library=MagicMock())
    bridge.library.kind_for_path.return_value = "music"
    bridge.on_track_started(
        SimpleNamespace(path="/music/song.flac", title="Song", artist="Artist", length=60)
    )
    bridge.record_aired("matched_intro", title="Intro for Song", duration=4.5)
    bridge.record_aired("liner", title="Station ID", path="/liners/id.wav", duration=6)

    # Now playing and other song readers see the songs only, as before.
    assert [row["title"] for row in bridge.history_snapshot()] == ["Song"]
    everything = bridge.history_snapshot(include_inserts=True)
    assert [(row["title"], row["kind"]) for row in everything] == [
        ("Song", "song"),
        ("Intro for Song", "matched_intro"),
        ("Station ID", "liner"),
    ]
    assert everything[1]["path"] is None
    assert everything[1]["duration"] == 4.5
    written = [call.args[0] for call in ledger.remember_history.call_args_list]
    assert [row.get("kind") for row in written] == [None, "matched_intro", "liner"]
    assert all(row["performances"] is None for row in written[1:])
    # An inserted item is not a song play: last-played times stay as they were.
    assert [call.args[0] for call in ledger.remember_play.call_args_list] == ["/music/song.flac"]


def test_the_bridge_marks_a_liner_file_played_as_a_track() -> None:
    bridge = _bridge(ledger=MagicMock(), library=MagicMock())
    bridge.library.kind_for_path.side_effect = lambda path: (
        "liner" if path.startswith("/liners") else "music"
    )
    bridge.on_track_started(SimpleNamespace(path="/liners/id.wav", title="ID", artist="", length=5))
    bridge.on_track_started(SimpleNamespace(path="/music/a.flac", title="A", artist="", length=5))
    assert [row["kind"] for row in bridge.history_snapshot(include_inserts=True)] == [
        "liner",
        "song",
    ]


def test_the_bridge_refuses_a_kind_it_does_not_know() -> None:
    bridge = _bridge()
    with pytest.raises(ValueError, match="kind"):
        bridge.record_aired("song", title="Not an insert")
    assert bridge.history_snapshot(include_inserts=True) == []


def test_the_history_keeps_room_for_inserted_items() -> None:
    bridge = _bridge()
    assert bridge._play_history.maxlen >= 2000


def _player():
    from autodj.player import Player
    from tests.unit._fakes import make_cfg_mock, make_sim_index

    player = Player(make_cfg_mock(), make_sim_index(4))
    player.load_lyrics_in_background = MagicMock()  # type: ignore[method-assign]
    return player


def test_the_player_reports_each_handoff_item_once_as_it_airs() -> None:
    """ADR-0006-08."""
    from autodj.breaks import AD_SCHEDULE, VOICE_TRACK
    from autodj.mixbus import RenderedTrack

    player = _player()
    song, following, liner = player._sim.entries[:3]
    breaks = {
        "spot": SimpleNamespace(kind="ad", source=AD_SCHEDULE, label="Example Motors, Sale"),
        "voice": SimpleNamespace(kind="dj", source=VOICE_TRACK, label="Back-announce"),
        "news": SimpleNamespace(kind="news", source="announcer", label="Headlines"),
    }
    desk = MagicMock()
    desk.get.side_effect = breaks.get
    player.break_desk = desk
    player.matched_intros = MagicMock()
    aired: list[tuple[str, dict]] = []
    player.on_item_aired = lambda kind, **fields: aired.append((kind, fields))
    render = RenderedTrack(
        song,
        np.zeros((44100, 2), np.float32),
        following,
        0,
        "",
        liner_events=((100, liner, False),),
        break_events=((200, "spot", 300), (400, "voice", 500), (600, "news", 700)),
        intro_events=((800, "intro-1", 44100 // 2 + 800, "Intro for the next song"),),
    )
    player._on_track_start(render)
    player._on_position(1000)
    player._on_position(2000)  # heard already: nothing more

    assert [kind for kind, _fields in aired] == [
        "liner",
        "ad",
        "voice_track",
        "news",
        "matched_intro",
    ]
    fields = dict(aired)
    assert fields["liner"]["path"] == liner.path
    assert fields["liner"]["title"] == liner.title
    assert fields["ad"]["title"] == "Example Motors, Sale"
    assert fields["ad"]["duration"] == pytest.approx(100 / 44100)
    assert fields["matched_intro"] == {
        "title": "Intro for the next song",
        "duration": pytest.approx(0.5),
    }


def test_the_player_reports_nothing_without_a_listener() -> None:
    from autodj.mixbus import RenderedTrack

    player = _player()
    song, following, liner = player._sim.entries[:3]
    render = RenderedTrack(
        song,
        np.zeros((4410, 2), np.float32),
        following,
        0,
        "",
        liner_events=((100, liner, False),),
    )
    player._on_track_start(render)
    player._on_position(1000)  # no on_item_aired: nothing breaks


def _scheduler(tmp_path: Path, bus=None):
    from autodj.liner_scheduler import LinerScheduler

    cfg = SimpleNamespace(
        liners_enabled=True,
        liners_every_n_songs=0,
        liners_every_minutes=0,
        liners_random_min_minutes=0,
        liners_random_max_minutes=0,
        liners_duck_db=-8.0,
        liners_talk_up=False,
        transition_mode="full_intro_outro",
        pick_mode="similarity",
    )
    folder = tmp_path / "liners"
    folder.mkdir()
    (folder / "Station ID.wav").write_bytes(b"x")
    return LinerScheduler(
        cfg,
        folder,
        bus or MagicMock(live_on_air=False),
        decoder=lambda _root, _name: np.zeros((44100 * 2, 2), np.float32),
    )


def test_the_scheduler_reports_liners_time_checks_and_carts_that_play(tmp_path) -> None:
    scheduler = _scheduler(tmp_path)
    aired: list[tuple[str, dict]] = []
    scheduler.on_aired = lambda kind, **fields: aired.append((kind, fields))

    assert scheduler.fire("Station ID.wav") == "Station ID.wav"
    scheduler.play_audio(np.zeros((44100, 2), np.float32))
    scheduler.play_audio(np.zeros((22050, 2), np.float32), kind="liner", title="Scheduled ID")
    scheduler.play_cart(np.zeros((44100 * 3, 2), np.float32), -6.0, title="Jingle")

    assert aired == [
        (
            "liner",
            {
                "title": "Station ID",
                "path": str(tmp_path / "liners" / "Station ID.wav"),
                "duration": 2.0,
            },
        ),
        ("time_check", {"title": "Time check", "path": None, "duration": 1.0}),
        ("liner", {"title": "Scheduled ID", "path": None, "duration": 0.5}),
        ("cart", {"title": "Jingle", "path": None, "duration": 3.0}),
    ]


def test_the_scheduler_does_not_report_what_a_live_dj_held_back(tmp_path) -> None:
    """ADR-0006-08."""
    scheduler = _scheduler(tmp_path, bus=MagicMock(live_on_air=True))
    aired: list[str] = []
    scheduler.on_aired = lambda kind, **_fields: aired.append(kind)
    scheduler.fire("Station ID.wav")
    scheduler.play_audio(np.zeros((44100, 2), np.float32))
    assert aired == []


def test_a_scheduler_snapshot_never_reports(tmp_path) -> None:
    """ADR-0006-08."""
    scheduler = _scheduler(tmp_path)
    scheduler.on_aired = MagicMock()
    assert scheduler.snapshot().on_aired is None
