import os
from pathlib import Path

import numpy as np
import pytest

from autodj.catalog import LibraryCatalog
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


def test_user_points_survive_auto_updates_and_can_be_explicitly_cleared(tmp_path: Path) -> None:
    root = tmp_path / "music"
    root.mkdir()
    track = root / "song.flac"
    track.write_bytes(b"audio")
    with LibraryCatalog(tmp_path / "catalog.db") as catalog:
        catalog.add_source(root)
        catalog.put_entry(entry(track))
        catalog.set_points(track, {"start": 0, "outro": 170}, source="user")
        catalog.set_points(track, {"start": 2, "outro": 160, "intro": 5}, source="analysis")
        assert catalog.points(track)["start"] == 0
        assert catalog.points(track)["outro"] == 170
        assert catalog.points(track)["intro"] == 5
        catalog.set_points(track, {"start": None}, source="user")
        assert catalog.points(track)["start"] is None
        assert catalog.record_for_path(track)["point_sources"]["start"] == "user"


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
        old.rename(new)
        assert catalog.rekey(old, new)
        record = catalog.record_for_path(new)
        assert record["id"] == record_id
        assert record["points"]["segue"] == 120
        assert catalog.last_played()[os.path.normcase(str(new))] == 1234.5
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
        catalog.set_vector(track, np.ones(FEATURE_DIM, dtype=np.float32))
        track.write_bytes(b"new audio, a different file")
        catalog.put_entry(entry(track), prepared_path=None)
        assert catalog.record_for_path(track)["prepared_path"] is None
        records, vectors = catalog.vectors_snapshot()
        assert records == []
        assert vectors.shape == (0, FEATURE_DIM)
