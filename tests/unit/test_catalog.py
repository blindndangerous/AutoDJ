import json
import os
import sqlite3
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

import numpy as np
import pytest

from autodj import catalog as catalog_module
from autodj.catalog import _SCHEMA, LibraryCatalog
from autodj.indexer import FEATURE_DIM, IndexEntry
from tests.unit._catalog_vectors import set_vector, vectors_snapshot


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


def test_catalog_open_skips_orphan_purge_when_no_orphans(tmp_path: Path) -> None:
    db = tmp_path / "catalog.db"
    with LibraryCatalog(db):
        pass
    statements: list[str] = []
    connect = catalog_module._connect

    def traced_connect(*args, **kwargs):
        conn = connect(*args, **kwargs)
        conn.set_trace_callback(statements.append)
        return conn

    with patch.object(catalog_module, "_connect", side_effect=traced_connect), LibraryCatalog(db):
        pass
    assert not any("DELETE FROM catalog_vectors" in sql for sql in statements)

    with closing(sqlite3.connect(db)) as conn:
        conn.execute("INSERT INTO catalog_vectors VALUES (?, ?)", ("orphan", b"vector"))
        conn.commit()
    with LibraryCatalog(db) as catalog:
        assert catalog._db().execute("SELECT 1 FROM catalog_vectors").fetchone() is None


def test_remove_entries_rejects_stale_preview_versions(tmp_path: Path) -> None:
    first = tmp_path / "first.wav"
    second = tmp_path / "second.wav"
    first.write_bytes(b"first")
    second.write_bytes(b"second")
    with LibraryCatalog(tmp_path / "catalog.db") as catalog:
        catalog.put_entry(entry(first))
        preview_versions = catalog.change_versions()
        catalog.put_entry(entry(second))

        with pytest.raises(RuntimeError, match="catalog changed"):
            catalog.remove_entries([str(first)], expected_versions=preview_versions)

        assert catalog.record_for_path(first) is not None


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
        catalog.set_intro_lift(song, 3.25)
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
        catalog.set_points(liner, {"overlap_start": -15})
        assert catalog.points(liner)["overlap_start"] == -15
        with pytest.raises(ValueError, match="-15"):
            catalog.set_points(liner, {"overlap_start": -15.01})
        catalog.set_points(liner, {"overlap_start": 5})
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
        {"overlap_start": -0.01},
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


def test_cues_within_display_rounding_move_onto_their_limit(tmp_path: Path) -> None:
    """#237: Track info rounds cues to 0.01 s, which must not make a save invalid."""
    root = tmp_path / "music"
    root.mkdir()
    track, liner = root / "song.flac", root / "voice.wav"
    track.write_bytes(b"audio")
    liner.write_bytes(b"voice")
    with LibraryCatalog(tmp_path / "catalog.db") as catalog:
        catalog.add_source(root)
        catalog.put_entry(entry(track))
        catalog.set_points(track, {"start": 10.004, "end": 170.004}, source="analysis")
        # Start and End shown as 0:10 and 2:50; an Intro and Outro typed to match.
        catalog.set_points(track, {"intro": 10.0, "outro": 170.01, "end": 180.005})
        assert catalog.points(track)["intro"] == 10.004
        assert catalog.points(track)["outro"] == 170.01
        assert catalog.points(track)["end"] == 180
        catalog.set_points(track, {"end": 170.004, "outro": 170.01})
        assert catalog.points(track)["outro"] == 170.004
        catalog.set_points(track, {"overlap_start": 160.01})
        assert catalog.points(track)["overlap_start"] == pytest.approx(160.0)
        for bad in ({"intro": 9.98}, {"outro": 170.02}, {"overlap_start": 160.02}):
            with pytest.raises(ValueError):
                catalog.set_points(track, bad)

        catalog.put_entry(entry(liner), "liner")
        catalog.set_points(liner, {"start": 2.01, "segue": 8.0})
        catalog.set_points(liner, {"overlap_start": 6.0})
        assert catalog.points(liner)["overlap_start"] == pytest.approx(5.99)
        assert catalog.record_for_path(liner)["point_sources"]["overlap_start"] == "user"
        with pytest.raises(ValueError, match="overlap_start"):
            catalog.set_points(liner, {"overlap_start": 6.01})


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
    connection = sqlite3.connect(db)
    connection.execute("CREATE TABLE catalog_settings (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
    connection.execute(
        "INSERT INTO catalog_settings VALUES ('play_history',?)", (json.dumps(legacy),)
    )
    connection.commit()
    connection.close()
    with LibraryCatalog(db) as catalog:
        assert catalog.recent_history() == []
        catalog.migrate_legacy_history()
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
        assert catalog.recent_history() == []
        catalog.migrate_legacy_history()
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
        records, vectors = vectors_snapshot(catalog)
        assert records[0].path == os.path.normcase(str(new))
        assert catalog.record_for_path(new)["id"] == record_id
        np.testing.assert_array_equal(vectors[0], vector)


def test_changed_content_invalidates_vector_and_keeps_prepared_audio_external(
    tmp_path: Path,
) -> None:
    root = tmp_path / "music"
    root.mkdir()
    track = root / "song.flac"
    track.write_bytes(b"old audio")
    with LibraryCatalog(tmp_path / "catalog.db") as catalog:
        # Existing databases can retain the old column; catalog operations ignore it.
        catalog._db().execute("ALTER TABLE catalog_entries ADD COLUMN prepared_path TEXT")
        catalog.add_source(root)
        catalog.put_entry(entry(track))
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
        set_vector(catalog, track, np.ones(FEATURE_DIM, dtype=np.float32))
        track.write_bytes(b"new audio, a different file")
        catalog.put_entry(entry(track))
        assert catalog.record_for_path(track)["loudness"] == {}
        assert "prepared_path" not in catalog.record_for_path(track)
        records, vectors = vectors_snapshot(catalog)
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


def test_change_counters_track_records_vectors_and_sources_from_any_process(
    tmp_path: Path,
) -> None:
    db = tmp_path / "catalog.db"
    # A catalog made before the counters existed gets them when opened.
    with closing(sqlite3.connect(db)) as conn:
        conn.executescript(_SCHEMA)
    music = tmp_path / "music"
    music.mkdir()
    song = music / "song.flac"
    song.write_bytes(b"audio")
    with LibraryCatalog(db) as catalog, LibraryCatalog(db) as other:
        start = catalog.change_versions()
        source = catalog.add_source(music)
        catalog.put_entry(entry(song))
        after_put = catalog.change_versions()
        assert after_put["sources"] == start["sources"] + 1
        assert after_put["entries"] > start["entries"]
        assert after_put["records"] > start["records"]

        catalog.remember_play(song, 12.0)
        catalog.set_setting("clock", {"cursor": 1})
        catalog.write_history(
            [
                {
                    "event_id": "e1",
                    "path": str(song),
                    "title": "T",
                    "artist": "A",
                    "duration": 1.0,
                    "played_at": "2026-01-01T00:00:00+00:00",
                }
            ]
        )
        assert catalog.change_versions() == after_put

        catalog.set_points(song, {"intro": 5.0})
        catalog.set_intro_lift(song, 1.0)
        cues = catalog.change_versions()
        assert cues["entries"] == after_put["entries"]
        assert cues["records"] > after_put["records"]

        set_vector(other, song, np.ones(FEATURE_DIM, dtype=np.float32))
        other.update_source(source["id"], recursive=False, advanced=True)
        changed = catalog.change_versions()
        assert changed["vectors"] == cues["vectors"] + 1
        assert changed["sources"] == cues["sources"] + 1


def test_unembedded_count_matches_paths_and_tracks_external_changes(tmp_path: Path) -> None:
    music, nested, liners = tmp_path / "music", tmp_path / "music" / "nested", tmp_path / "liners"
    nested.mkdir(parents=True)
    liners.mkdir()
    songs = [music / "top.flac", nested / "nested.flac", liners / "liner.flac"]
    for song in songs:
        song.write_bytes(b"audio")
    with (
        LibraryCatalog(tmp_path / "catalog.db") as catalog,
        LibraryCatalog(tmp_path / "catalog.db") as other,
    ):
        catalog.add_source(music, recursive=False, advanced=True)
        nested_source = catalog.add_source(nested, recursive=True, advanced=False)
        catalog.add_source(liners, "liner", recursive=True, advanced=False)
        catalog.add_source(music, "liner", recursive=False, advanced=False)
        catalog.put_entry(entry(songs[0]))
        catalog.put_entry(entry(songs[1]))
        catalog.put_entry(entry(songs[2]))
        assert catalog.unembedded_count() == len(catalog.unembedded_paths()) == 0

        other.update_source(nested_source["id"], recursive=True, advanced=True)
        assert catalog.unembedded_count() == len(catalog.unembedded_paths()) == 1
        set_vector(other, songs[1], np.ones(FEATURE_DIM, dtype=np.float32))
        assert catalog.unembedded_count() == len(catalog.unembedded_paths()) == 0
        other.put_entry(entry(nested / "new.flac"))
        assert catalog.unembedded_count() == len(catalog.unembedded_paths()) == 1


def test_removing_entry_cascades_to_vector(tmp_path: Path) -> None:
    song = tmp_path / "song.flac"
    song.write_bytes(b"x" * 100)
    with LibraryCatalog(tmp_path / "library.db") as catalog:
        catalog.put_entry(entry(song))
        vector = np.zeros(FEATURE_DIM, np.float32)
        vector[0] = 1
        set_vector(catalog, song, vector)
        assert catalog.remove_entries([str(song)]) == 1
        assert catalog._db().execute("PRAGMA foreign_keys").fetchone()[0] == 1
        assert catalog._db().execute("SELECT COUNT(*) FROM catalog_vectors").fetchone()[0] == 0


@pytest.mark.parametrize("kind", [None, "music", "liner"])
def test_bulk_records_match_single_lookups_with_one_query(tmp_path, kind):
    """Bulk reads preserve uncovered records and decoding without per-row queries."""
    with LibraryCatalog(tmp_path / "library.db") as catalog:
        paths = [tmp_path / name for name in ("z.flac", "a.flac", "liner.wav")]
        for path in paths:
            catalog.put_entry(entry(path), "liner" if path.suffix == ".wav" else "music")
        catalog._db().execute(
            "UPDATE catalog_entries SET loudness=? WHERE path=?", ("broken", str(paths[0]))
        )
        expected = tuple(
            catalog.record_for_path(path)
            for path in sorted(paths)
            if kind is None or ("liner" if path.suffix == ".wav" else "music") == kind
        )
        statements = []
        catalog._db().set_trace_callback(statements.append)
        try:
            assert catalog.all_entries(kind) == expected
        finally:
            catalog._db().set_trace_callback(None)
        assert sum(sql.lstrip().upper().startswith("SELECT") for sql in statements) == 1
