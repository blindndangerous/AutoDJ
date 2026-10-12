import json
import os
import sqlite3
from contextlib import closing
from pathlib import Path

import numpy as np
import pytest

from autodj.catalog import _SCHEMA, LibraryCatalog
from autodj.indexer import FEATURE_DIM, IndexEntry


def entry(path: Path, *, title: str = "Title", energy: float = 0.2) -> IndexEntry:
    return IndexEntry(str(path), title, "Artist", "Album", "", 120, 2000, 180, energy, -1, -1, 0.0)


def test_seed_sources_migrates_once_and_keeps_explicit_empty(tmp_path: Path) -> None:
    music, liners = tmp_path / "music", tmp_path / "liners"
    music.mkdir()
    liners.mkdir()
    with LibraryCatalog(tmp_path / "catalog.db") as catalog:
        assert catalog.seed_sources(music, liners)
        assert not catalog.seed_sources(tmp_path / "other", tmp_path / "other-liners")
        assert catalog.sources("music")[0]["recursive"] is True
        assert catalog.sources("music")[0]["advanced"] is True
        assert catalog.sources("liner")[0]["recursive"] is False
        catalog.remove_source(catalog.sources("music")[0]["id"])
        catalog.set_setting("sources_seeded", True)
        assert not catalog.seed_sources(music, liners)
        assert catalog.sources("music") == []


def test_intro_lift_column_migrates_and_persists(tmp_path: Path) -> None:
    db = tmp_path / "catalog.db"
    with closing(sqlite3.connect(db)) as conn:
        conn.executescript(_SCHEMA.replace("intro_lift_db REAL,", ""))
    music = tmp_path / "music"
    music.mkdir()
    song = music / "song.wav"
    song.write_bytes(b"source")
    with LibraryCatalog(db) as catalog:
        catalog.put_entry(entry(song))
        assert catalog.record_for_path(song)["intro_lift_db"] is None
        revision = catalog.content_revision()
        catalog.set_intro_lift(song, 3.25)
        assert catalog.content_revision() > revision
    with LibraryCatalog(db) as catalog:
        assert catalog.record_for_path(song)["intro_lift_db"] == 3.25


def test_loudness_column_migrates_and_persists(tmp_path: Path) -> None:
    db = tmp_path / "catalog.db"
    song = tmp_path / "song.wav"
    song.write_bytes(b"source")
    with closing(sqlite3.connect(db)) as conn:
        conn.executescript(_SCHEMA.replace("        loudness TEXT NOT NULL DEFAULT '{}',\n", ""))
        conn.execute(
            "INSERT INTO catalog_entries (id,path,kind,title,size) VALUES ('old',?,'music','Old',?)",
            (os.path.normcase(str(song)), song.stat().st_size),
        )
        conn.commit()
    measurement = {
        "version": "1",
        "kind": "music",
        "size": song.stat().st_size,
        "mtime_ns": song.stat().st_mtime_ns,
        "lufs": -18.5,
        "gain_db": 2.5,
    }
    with LibraryCatalog(db) as catalog:
        assert catalog.record_for_path(song)["title"] == "Old"
        assert catalog.record_for_path(song)["loudness"] == {}
        catalog.set_loudness(song, measurement)
        assert catalog.record_for_path(song)["loudness"] == measurement
        catalog._db().execute(
            "UPDATE catalog_entries SET loudness='not-json' WHERE path=?",
            (os.path.normcase(str(song)),),
        )
        assert catalog.record_for_path(song)["loudness"] == {}


@pytest.mark.parametrize(
    "updates",
    [
        {"lufs": float("nan")},
        {"lufs": float("inf")},
        {"gain_db": float("nan")},
        {"gain_db": 18.1},
        {"gain_db": -60.1},
        {"size": True},
    ],
)
def test_set_loudness_rejects_invalid_measurements(tmp_path: Path, updates: dict) -> None:
    song = tmp_path / "song.wav"
    song.write_bytes(b"source")
    stat = song.stat()
    measurement = {
        "version": "1",
        "kind": "music",
        "size": stat.st_size,
        "mtime_ns": stat.st_mtime_ns,
        "lufs": -18.5,
        "gain_db": 2.5,
        **updates,
    }
    with LibraryCatalog(tmp_path / "catalog.db") as catalog:
        catalog.put_entry(entry(song))
        with pytest.raises(ValueError):
            catalog.set_loudness(song, measurement)


def test_set_loudness_discards_stale_source_measurement(tmp_path: Path) -> None:
    song = tmp_path / "song.wav"
    song.write_bytes(b"source")
    stat = song.stat()
    measurement = {
        "version": "1",
        "kind": "music",
        "size": stat.st_size,
        "mtime_ns": stat.st_mtime_ns,
        "lufs": -18.5,
        "gain_db": 2.5,
    }
    with LibraryCatalog(tmp_path / "catalog.db") as catalog:
        catalog.put_entry(entry(song))
        os.utime(song, ns=(stat.st_atime_ns, stat.st_mtime_ns + 1_000_000))
        catalog.set_loudness(song, measurement)
        assert catalog.record_for_path(song)["loudness"] == {}


def test_overlapping_sources_deduplicate_and_liners_are_excluded_from_music(tmp_path: Path) -> None:
    root, child = tmp_path / "music", tmp_path / "music" / "child"
    child.mkdir(parents=True)
    liner_root = root / "voice"
    liner_root.mkdir()
    song, liner = child / "song.flac", liner_root / "id.wav"
    song.write_bytes(b"music")
    liner.write_bytes(b"voice")
    with LibraryCatalog(tmp_path / "catalog.db") as catalog:
        catalog.add_source(root)
        catalog.add_source(child)
        catalog.add_source(liner_root, "liner", recursive=True, advanced=False)
        catalog.put_entry(entry(song))
        catalog.put_entry(entry(liner, title="ID"), "liner")
        assert [Path(e.path) for e in catalog.entries_snapshot()] == [song]
        assert len(catalog.entries_snapshot("liner")) == 1
        catalog.remove_source(catalog.sources("music")[0]["id"])
        assert len(catalog.entries_snapshot()) == 1  # child source keeps covered tracks
        catalog.remove_source(catalog.sources("music")[0]["id"])
        assert catalog.entries_snapshot() == ()
        assert catalog.record_for_path(song)["title"] == "Title"  # removal is non-destructive


def test_seeded_index_fields_survive_put_and_reopen(tmp_path: Path) -> None:
    root = tmp_path / "music"
    root.mkdir()
    track = root / "track.flac"
    track.write_bytes(b"audio")
    original = entry(track, title="Indexed title")
    db = tmp_path / "catalog.db"
    with LibraryCatalog(db) as catalog:
        catalog.add_source(root)
        assert catalog.seed_entries([original]) == 1
        assert catalog.seed_entries([entry(track, title="stale title")]) == 0
        catalog.put_entry(entry(track, title="Indexed title", energy=0.7), points={"segue": 150.0})
    with LibraryCatalog(db) as catalog:
        assert catalog.entry_for_path(track).title == "Indexed title"
        assert catalog.entry_for_path(track).energy == 0.7
        assert catalog.points(track)["segue"] == 150.0


@pytest.mark.parametrize("point_source", ["user", "imported"])
def test_manual_points_survive_same_file_auto_updates_and_can_be_cleared(
    tmp_path: Path, point_source: str
) -> None:
    root = tmp_path / "music"
    root.mkdir()
    track = root / "song.flac"
    track.write_bytes(b"audio")
    with LibraryCatalog(tmp_path / "catalog.db") as catalog:
        catalog.add_source(root)
        catalog.put_entry(entry(track))
        catalog.set_points(track, {"start": 0, "outro": 170}, source=point_source)
        catalog.set_points(track, {"start": 2, "outro": 160, "intro": 5}, source="analysis")
        assert catalog.points(track)["start"] == 0
        assert catalog.points(track)["outro"] == 170
        assert catalog.points(track)["intro"] == 5
        assert catalog.record_for_path(track)["point_sources"]["start"] == point_source
        catalog.set_points(track, {"start": None}, source="user")
        assert catalog.points(track)["start"] is None
        assert catalog.record_for_path(track)["point_sources"]["start"] == "user"


def test_liner_allow_outro_persists_and_overlap_tracks_effective_segue(tmp_path: Path) -> None:
    root = tmp_path / "liners"
    root.mkdir()
    liner = root / "voice.wav"
    liner.write_bytes(b"voice")
    db = tmp_path / "catalog.db"
    with LibraryCatalog(db) as catalog:
        catalog.add_source(root, "liner", recursive=False, advanced=False)
        catalog.put_entry(entry(liner), "liner")
        assert catalog.record_for_path(liner)["allow_outro"] is False
        catalog.set_points(
            liner, {"start": 2, "segue": 8, "overlap_start": 5}, "analysis", allow_outro=True
        )
        with pytest.raises(ValueError, match="overlap_start"):
            catalog.set_points(liner, {"start": 2, "segue": 6, "overlap_start": 7})
        catalog.set_points(liner, {"segue": 6}, source="analysis")
        assert catalog.points(liner)["overlap_start"] == 4
        assert catalog.record_for_path(liner)["point_sources"]["overlap_start"] == "analysis"
        with pytest.raises(ValueError, match="boolean"):
            catalog.set_points(liner, {}, allow_outro="true")
    with LibraryCatalog(db) as catalog:
        assert catalog.record_for_path(liner)["allow_outro"] is True


@pytest.mark.parametrize("overlap, expected", [(None, None), (0, 0), (3, 3), (5, 4)])
@pytest.mark.parametrize("source", ["auto", "imported"])
def test_automatic_cue_merge_clamps_liner_overlap(
    tmp_path: Path, overlap: int | None, expected: int | None, source: str
) -> None:
    root = tmp_path / "liners"
    root.mkdir()
    liner = root / "voice.wav"
    liner.write_bytes(b"voice")
    with LibraryCatalog(tmp_path / "catalog.db") as catalog:
        catalog.add_source(root, "liner", recursive=False, advanced=False)
        catalog.put_entry(entry(liner), "liner")
        catalog.set_points(
            liner,
            {"start": 2, "end": 10, "segue": 8, "overlap_start": overlap},
            "analysis",
        )
        if overlap is not None:
            catalog.set_points(liner, {"overlap_start": overlap})
        catalog.set_points(liner, {"segue": 6}, source=source)
        assert catalog.points(liner)["overlap_start"] == expected
        assert catalog.points(liner)["segue"] == 6
        assert catalog.record_for_path(liner)["point_sources"]["overlap_start"] == (
            source if overlap == 5 else "user" if overlap is not None else "analysis"
        )
        points = catalog.points(liner)
        catalog.set_points(
            liner,
            {name: points[name] for name in ("start", "end", "segue", "overlap_start")},
            allow_outro=True,
        )
        assert catalog.record_for_path(liner)["allow_outro"] is True


def test_allow_outro_can_only_be_set_on_liners(tmp_path: Path) -> None:
    root = tmp_path / "music"
    root.mkdir()
    track = root / "song.flac"
    track.write_bytes(b"audio")
    with LibraryCatalog(tmp_path / "catalog.db") as catalog:
        catalog.add_source(root)
        catalog.put_entry(entry(track))
        with pytest.raises(ValueError, match="set for liners"):
            catalog.set_points(track, {}, allow_outro=True)


def test_catalog_adds_allow_outro_to_existing_database(tmp_path: Path) -> None:
    db = tmp_path / "catalog.db"
    with LibraryCatalog(db):
        pass
    legacy_path = os.path.normcase(os.path.abspath("/legacy.wav"))
    with closing(sqlite3.connect(db)) as conn, conn:
        conn.execute("ALTER TABLE catalog_entries DROP COLUMN allow_outro")
        conn.execute(
            "INSERT INTO catalog_entries (id,path,kind) VALUES ('legacy',?,'liner')",
            (legacy_path,),
        )
    with LibraryCatalog(db) as catalog:
        assert catalog.record_for_path(legacy_path)["allow_outro"] is False
    with LibraryCatalog(db) as catalog:
        assert catalog.record_for_path(legacy_path)["allow_outro"] is False


@pytest.mark.parametrize(
    "points",
    [
        {"unknown": 1},
        {"start": float("nan")},
        {"start": -1},
        {"start": 181},
        {"overlap_start": True},
    ],
)
def test_points_reject_unknown_or_invalid_times(tmp_path: Path, points: dict) -> None:
    root = tmp_path / "music"
    root.mkdir()
    track = root / "song.flac"
    track.write_bytes(b"audio")
    with LibraryCatalog(tmp_path / "catalog.db") as catalog:
        catalog.add_source(root)
        catalog.put_entry(entry(track))
        with pytest.raises(ValueError):
            catalog.set_points(track, points)


def test_rekey_preserves_record_identity_points_and_history(tmp_path: Path) -> None:
    root = tmp_path / "music"
    root.mkdir()
    old, new = root / "old.flac", root / "new.flac"
    old.write_bytes(b"audio")
    with LibraryCatalog(tmp_path / "catalog.db") as catalog:
        catalog.add_source(root)
        record_id = catalog.put_entry(entry(old))
        catalog.set_points(old, {"segue": 120}, "user")
        catalog.remember_play(old, 1234.5)
        catalog.write_history(
            [
                {
                    "event_id": "archived-play",
                    "path": str(old),
                    "title": "Title",
                    "artist": "Artist",
                    "duration": 180,
                    "played_at": "2025-02-03T12:30:00Z",
                }
            ]
        )
        old.rename(new)
        assert catalog.rekey(old, new)
        record = catalog.record_for_path(new)
        assert record["id"] == record_id
        assert record["points"]["segue"] == 120
        assert catalog.last_played()[os.path.normcase(str(new))] == 1234.5
        history = catalog.recent_history()[0]
        assert history["entry_id"] == record_id
        assert history["path"] == os.path.normcase(str(old))
        assert catalog.record_for_path(old) is None


def test_settings_and_source_update_are_persistent(tmp_path: Path) -> None:
    root = tmp_path / "music"
    root.mkdir()
    db = tmp_path / "catalog.db"
    with LibraryCatalog(db) as catalog:
        source = catalog.add_source(root)
        assert catalog.update_source(source["id"], recursive=False, advanced=False)
        catalog.set_setting("clockwheel", {"enabled": True})
    with LibraryCatalog(db) as catalog:
        assert catalog.sources()[0]["recursive"] is False
        assert catalog.sources()[0]["advanced"] is False
        assert catalog.get_setting("clockwheel") == {"enabled": True}


def test_play_history_is_unbounded_idempotent_and_recent_is_limited(tmp_path: Path) -> None:
    root = tmp_path / "music"
    root.mkdir()
    track = root / "song.flac"
    track.write_bytes(b"audio")
    db = tmp_path / "catalog.db"
    with LibraryCatalog(db) as catalog:
        catalog.put_entry(entry(track))
        catalog.remember_play(track, 1234.5)
        rows = [
            {
                "event_id": f"play-{i}",
                "path": str(track),
                "title": "Title",
                "artist": "Artist",
                "duration": 180.0,
                "played_at": f"2025-01-{i % 28 + 1:02d}T12:00:00+00:00",
            }
            for i in range(501)
        ]
        catalog.write_history(rows)
        catalog.write_history(
            [rows[0], {**rows[0], "title": "retry"}, {**rows[0], "event_id": "second"}]
        )
        catalog.write_history([], interrupted=["play-0"])
        assert len(catalog.recent_history()) == 500
        assert catalog.recent_history()[0]["event_id"] == "play-2"
        assert catalog.recent_history()[-1]["event_id"] == "second"
        assert catalog.recent_history(limit=1)[0]["event_id"] == "second"
        assert catalog._db().execute("SELECT COUNT(*) FROM play_history").fetchone()[0] == 502
        assert (
            catalog._db()
            .execute("SELECT interrupted FROM play_history WHERE event_id='play-0'")
            .fetchone()[0]
            == 1
        )
        assert (
            catalog._db()
            .execute("SELECT entry_id FROM play_history WHERE event_id='play-1'")
            .fetchone()[0]
            == catalog.record_for_path(track)["id"]
        )
    with LibraryCatalog(db) as catalog:
        assert catalog._db().execute("SELECT COUNT(*) FROM play_history").fetchone()[0] == 502
        assert catalog.last_played()[os.path.normcase(str(track))] == 1234.5


def test_play_history_migrates_all_valid_legacy_rows_once(tmp_path: Path) -> None:
    db = tmp_path / "catalog.db"
    legacy = [
        {
            "title": f"Title {i}",
            "artist": "Artist",
            "duration": 180,
            "played_at": "2025-02-03T12:30:00Z",
        }
        for i in range(503)
    ]
    legacy += [
        {"title": "bad", "artist": "Artist", "duration": -1, "played_at": "2025-02-03"},
        {"title": "bad", "artist": "Artist", "duration": 1, "played_at": "no date"},
    ]
    connection = sqlite3.connect(db)
    connection.execute("CREATE TABLE catalog_settings (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
    connection.execute(
        "INSERT INTO catalog_settings VALUES ('play_history',?)", (json.dumps(legacy),)
    )
    connection.commit()
    connection.close()
    with LibraryCatalog(db) as catalog:
        assert len(catalog.recent_history()) == 500
        assert catalog._db().execute("SELECT COUNT(*) FROM play_history").fetchone()[0] == 503
        assert catalog.get_setting("play_history") == legacy
    with LibraryCatalog(db) as catalog:
        assert catalog._db().execute("SELECT COUNT(*) FROM play_history").fetchone()[0] == 503


def test_play_history_import_waits_for_legacy_key_to_appear(tmp_path: Path) -> None:
    db = tmp_path / "catalog.db"
    with LibraryCatalog(db) as catalog:
        assert catalog.recent_history() == []
        assert catalog.get_setting("play_history_migrated") is None
        catalog.set_setting(
            "play_history",
            [
                {
                    "title": "Title",
                    "artist": "Artist",
                    "duration": 180,
                    "played_at": "2025-02-03T12:30:00Z",
                }
            ],
        )
    with LibraryCatalog(db) as catalog:
        assert len(catalog.recent_history()) == 1
        assert catalog.get_setting("play_history_migrated") is True


@pytest.mark.parametrize("raw", ["{bad json", "{}", "null", "[1, null]"])
def test_corrupt_legacy_play_history_does_not_block_startup(tmp_path: Path, raw: str) -> None:
    db = tmp_path / "catalog.db"
    connection = sqlite3.connect(db)
    connection.execute("CREATE TABLE catalog_settings (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
    connection.execute("INSERT INTO catalog_settings VALUES ('play_history',?)", (raw,))
    connection.commit()
    connection.close()
    with LibraryCatalog(db) as catalog:
        assert catalog.recent_history() == []
        assert (
            catalog._db()
            .execute("SELECT value FROM catalog_settings WHERE key='play_history'")
            .fetchone()[0]
            == raw
        )


def test_seed_vectors_and_confirmed_move_preserve_vector(tmp_path: Path) -> None:
    root = tmp_path / "music"
    root.mkdir()
    old, new = root / "old.flac", root / "new.flac"
    old.write_bytes(b"audio")
    original = entry(old)
    vector = np.arange(FEATURE_DIM, dtype=np.float32)
    with LibraryCatalog(tmp_path / "catalog.db") as catalog:
        catalog.add_source(root)
        catalog.seed_entries([original])
        assert catalog.seed_vectors([original], vector[None, :]) == 1
        assert catalog.seed_vectors([original], vector[None, :]) == 0
        record_id = catalog.record_for_path(old)["id"]
        old.rename(new)
        assert catalog.rekey(old, new)
        records, vectors = catalog.vectors_snapshot()
        assert records[0].path == os.path.normcase(str(new))
        assert catalog.record_for_path(new)["id"] == record_id
        np.testing.assert_array_equal(vectors[0], vector)


def test_changed_content_invalidates_vector_and_prepared_audio(tmp_path: Path) -> None:
    root = tmp_path / "music"
    root.mkdir()
    track = root / "song.flac"
    track.write_bytes(b"old audio")
    with LibraryCatalog(tmp_path / "catalog.db") as catalog:
        catalog.add_source(root)
        catalog.put_entry(entry(track), prepared_path=str(root / "prepared.wav"))
        stat = track.stat()
        catalog.set_loudness(
            track,
            {
                "version": "1",
                "kind": "music",
                "size": stat.st_size,
                "mtime_ns": stat.st_mtime_ns,
                "lufs": -18.5,
                "gain_db": 2.5,
            },
        )
        catalog.set_vector(track, np.ones(FEATURE_DIM, dtype=np.float32))
        track.write_bytes(b"new audio, a different file")
        catalog.put_entry(entry(track), prepared_path=None)
        assert catalog.record_for_path(track)["prepared_path"] is None
        assert catalog.record_for_path(track)["loudness"] == {}
        records, vectors = catalog.vectors_snapshot()
        assert records == []
        assert vectors.shape == (0, FEATURE_DIM)


def test_recent_plays_for_the_repeat_rules(tmp_path: Path) -> None:
    root = tmp_path / "music"
    root.mkdir()
    song, liner, gone = root / "song.flac", root / "id.flac", root / "gone.flac"
    with LibraryCatalog(tmp_path / "catalog.db") as catalog:
        catalog.put_entry(entry(song))
        catalog.put_entry(entry(liner), kind="liner")

        def play(event_id: str, path: Path, minute: int) -> dict:
            return {
                "event_id": event_id,
                "path": str(path),
                "title": f"Title {event_id}",
                "artist": "Artist",
                "duration": 180.0,
                "played_at": f"2026-01-01T12:{minute:02d}:00+00:00",
            }

        catalog.write_history(
            [
                play("old", song, 0),
                play("liner", liner, 1),
                play("gone", gone, 2),
                play("cut", song, 3),
                play("new", song, 4),
            ],
            interrupted=["cut"],
        )
        plays = catalog.recent_plays(10)
        # Oldest first; no liner, no play a set stop cut short.
        assert [row["title"] for row in plays] == ["Title old", "Title gone", "Title new"]
        assert [row["album"] for row in plays] == ["Album", "", "Album"]
        assert set(plays[0]) == {"path", "title", "artist", "album", "played_at"}
        assert [row["title"] for row in catalog.recent_plays(2)] == ["Title gone", "Title new"]
        assert catalog.recent_plays(0) == []
        with pytest.raises(ValueError, match="limit"):
            catalog.recent_plays(-1)
