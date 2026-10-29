import sqlite3
from pathlib import Path
from types import SimpleNamespace

from autodj.clock_condition_data import BeetsConditions


def _config(db: Path, music: Path | None = None) -> SimpleNamespace:
    return SimpleNamespace(library=SimpleNamespace(beets_db=db, music_dir=music))


def _db(path: Path) -> Path:
    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE items (id INTEGER PRIMARY KEY, path BLOB, title TEXT, year INTEGER, original_year INTEGER)"
    )
    conn.execute("CREATE TABLE item_attributes (entity_id INTEGER, key TEXT, value)")
    conn.execute("INSERT INTO items VALUES (1, ?, 'Song', 2001, NULL)", (b"Artist/Song.flac",))
    conn.execute("INSERT INTO item_attributes VALUES (1, 'mood', 'dreamy')")
    conn.commit()
    conn.close()
    return path


def test_discovers_fields_and_caches_union_of_requested_values(tmp_path: Path, monkeypatch) -> None:
    from autodj import categories, clock_condition_data

    db = _db(tmp_path / "library.db")
    cfg = _config(db, tmp_path / "music")
    calls = {"fields": 0, "items": []}
    source = categories.beets_source(cfg)
    original_fields, original_items = source.fields, source.items

    def fields():
        calls["fields"] += 1
        return original_fields()

    def items(wanted):
        calls["items"].append(frozenset(wanted))
        return original_items(wanted)

    monkeypatch.setattr(
        clock_condition_data,
        "beets_source",
        lambda _cfg: SimpleNamespace(fields=fields, items=items),
    )
    data = BeetsConditions(cfg)
    names, values, error = data.snapshot()
    assert error is None and {"mood", "year", "original_year"} <= names
    assert values == {}
    _, values, _ = data.snapshot(["mood"])
    assert next(iter(values.values()))["mood"] == "dreamy"
    _, again, _ = data.snapshot(["mood"])
    assert again is values
    _, extended, _ = data.snapshot(["original_year"])
    assert extended is not values and calls["items"] == [
        frozenset({"mood"}),
        frozenset({"original_year", "year"}),
    ]
    assert all("original_year" not in row for row in values.values())
    assert calls["fields"] == 1


def test_refreshes_on_database_and_configuration_change(tmp_path: Path) -> None:
    db = _db(tmp_path / "library.db")
    cfg = _config(db, tmp_path / "music")
    data = BeetsConditions(cfg)
    _, first, _ = data.snapshot(["year"])
    cfg.library.music_dir = tmp_path / "renamed-music"
    _, moved, _ = data.snapshot(["year"])
    assert moved is not first

    wal = Path(str(db) + "-wal")
    wal.write_bytes(b"fingerprint")
    _, wal_refresh, _ = data.snapshot(["year"])
    assert wal_refresh is not moved

    conn = sqlite3.connect(db)
    conn.execute("UPDATE items SET year=2002")
    conn.commit()
    conn.close()
    db.touch()
    _, updated, _ = data.snapshot(["year"])
    assert next(iter(updated.values()))["year"] == 2002
    assert updated is not first


def test_keeps_entries_reference_until_identity_changes(tmp_path: Path) -> None:
    db = _db(tmp_path / "library.db")
    entries = (SimpleNamespace(path=str(tmp_path / "music" / "Artist" / "Song.flac")),)
    current = [entries]
    data = BeetsConditions(_config(db, tmp_path / "music"), lambda: current[0])

    data.snapshot(["year"])
    assert data._cached_entries is entries
    current[0] = (*entries,)
    assert current[0] is not entries
    data.snapshot(["year"])
    assert data._cached_entries is current[0]


def test_original_year_fallback_and_missing_database_retry(tmp_path: Path) -> None:
    db = _db(tmp_path / "library.db")
    data = BeetsConditions(_config(db))
    _, values, error = data.snapshot(["original_year"])
    assert error is None
    assert next(iter(values.values()))["year"] == 2001

    missing = tmp_path / "missing.db"
    retry = BeetsConditions(_config(missing))
    _, empty, error = retry.snapshot(["year"])
    assert empty == {} and error
    _db(missing)
    _, values, error = retry.snapshot(["year"])
    assert error is None and next(iter(values.values()))["year"] == 2001


def test_missing_library_is_human_readable_and_legacy_config_safe() -> None:
    _, items, error = BeetsConditions(SimpleNamespace()).snapshot(["mood"])
    assert items == {}
    assert error == "Configure a Beets database to use Beets field conditions."


def test_a_read_with_the_same_songs_does_not_list_them_again(tmp_path: Path) -> None:
    """ADR-0009-14: every clockwheel pick reads Beets; an unchanged library is not walked."""
    looked = []

    class Song:
        def __init__(self, path: str) -> None:
            self.path = path

        @property
        def is_silent(self) -> bool:
            looked.append(self.path)
            return False

    db = _db(tmp_path / "library.db")
    entries = (Song(str(tmp_path / "music" / "Artist" / "Song.flac")),)
    current = [entries]
    data = BeetsConditions(_config(db, tmp_path / "music"), lambda: current[0])
    _, first, _ = data.snapshot(["year"])
    assert first and looked
    looked.clear()
    for _ in range(3):
        assert data.snapshot(["year"])[1] is first
    assert looked == []
    # A field not read yet lists the songs to match them, once.
    data.snapshot(["mood"])
    assert len(looked) == 1
    looked.clear()
    # New songs are listed and matched again.
    current[0] = (*entries, Song(str(tmp_path / "music" / "Artist" / "Other.flac")))
    data.snapshot(["year"])
    assert len(looked) == 2
