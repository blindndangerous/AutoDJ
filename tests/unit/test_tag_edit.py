"""Tag edits from Track info: validation, safe writes and beets (#53)."""

from __future__ import annotations

import errno
import os
import sqlite3
from pathlib import Path

import mutagen
import numpy as np
import pytest

from autodj import tag_edit
from autodj.audio_meta import read_file_tags, read_reporting_tags
from autodj.catalog import LibraryCatalog
from autodj.indexer import FEATURE_DIM, file_fingerprint
from autodj.tag_edit import (
    TagError,
    TagFileBusyError,
    tags_writable,
    update_beets,
    validate_tags,
    write_tags,
)
from tests.unit._tagged_audio import SUFFIXES, make_audio
from tests.unit.test_library import make_entry

ALL = {
    "title": "New Title",
    "artist": "New Artist",
    "album": "New Album",
    "genre": "Ambient",
    "year": "1999",
    "bpm": "127.5",
    "key": "F#m",
    "isrc": "ab-1cd-26-00001",
}


def _raw_key(path: Path) -> str:
    audio = mutagen.File(str(path))
    for name in ("TKEY", "initialkey", "----:com.apple.iTunes:initialkey"):
        value = audio.tags.get(name)
        if value:
            item = getattr(value, "text", value)[0]
            return item.decode() if isinstance(item, bytes) else str(item)
    return ""


@pytest.mark.parametrize("suffix", SUFFIXES)
def test_every_field_is_written_and_reads_back(tmp_path: Path, suffix: str) -> None:
    path = make_audio(tmp_path, suffix)
    length = mutagen.File(str(path)).info.length
    assert tags_writable(path)

    write_tags(path, validate_tags(ALL))

    tags = read_file_tags(path)
    assert (tags.title, tags.artist, tags.album, tags.genre, tags.year) == (
        "New Title",
        "New Artist",
        "New Album",
        "Ambient",
        1999,
    )
    # MP4 keeps BPM as a whole number.
    assert tags.bpm == (128 if suffix == ".m4a" else 127.5)
    assert read_reporting_tags(path)[0] == "AB1CD2600001"
    assert _raw_key(path) == "F#m"
    assert mutagen.File(str(path)).info.length == pytest.approx(length)
    assert not [p for p in tmp_path.iterdir() if p.name.endswith(".tmp")]


@pytest.mark.parametrize("suffix", SUFFIXES)
def test_only_changed_fields_are_written_and_empty_clears(tmp_path: Path, suffix: str) -> None:
    path = make_audio(tmp_path, suffix)
    write_tags(path, validate_tags(ALL))

    write_tags(path, validate_tags({"artist": "Other", "year": "", "isrc": "", "key": ""}))

    tags = read_file_tags(path)
    assert tags.title == "New Title"
    assert tags.artist == "Other"
    assert tags.year == 0
    assert read_reporting_tags(path)[0] == ""
    assert _raw_key(path) == ""


def test_audio_bytes_survive_a_flac_tag_write(tmp_path: Path) -> None:
    import soundfile as sf

    path = make_audio(tmp_path, ".flac")
    before, rate = sf.read(path)
    write_tags(path, validate_tags({"title": "Kept audio"}))
    after, after_rate = sf.read(path)
    assert after_rate == rate
    np.testing.assert_array_equal(after, before)


def test_id3_drops_an_old_txxx_isrc_and_year(tmp_path: Path) -> None:
    from mutagen.id3 import TXXX, TYER

    path = make_audio(tmp_path, ".mp3")
    audio = mutagen.File(str(path))
    audio.add_tags()
    audio.tags.add(TXXX(encoding=3, desc="ISRC", text=["ZZ0000000000"]))
    audio.tags.add(TYER(encoding=3, text=["1980"]))
    audio.save(v2_version=3)

    write_tags(path, validate_tags({"isrc": "AB1CD2600001", "year": "2001"}))

    tags = mutagen.File(str(path)).tags
    assert "TXXX:ISRC" not in tags
    assert str(tags["TSRC"]) == "AB1CD2600001"
    assert read_file_tags(path).year == 2001


def test_mp4_bpm_clears(tmp_path: Path) -> None:
    path = make_audio(tmp_path, ".m4a")
    write_tags(path, validate_tags({"bpm": "90", "genre": "Lounge"}))
    write_tags(path, validate_tags({"bpm": ""}))
    tags = mutagen.File(str(path)).tags
    assert "tmpo" not in tags
    assert tags["©gen"] == ["Lounge"]


@pytest.mark.parametrize(
    ("raw", "field", "words"),
    [
        ({"title": "  "}, "title", "Enter a title"),
        ({"artist": "a\nb"}, "artist", "line breaks"),
        ({"album": "x" * 501}, "album", "500 characters"),
        ({"year": "99"}, "year", "four digits"),
        ({"year": "0999"}, "year", "four digits"),
        ({"bpm": "fast"}, "bpm", "1 to 400"),
        ({"bpm": "401"}, "bpm", "1 to 400"),
        ({"bpm": "0"}, "bpm", "1 to 400"),
        ({"bpm": "120.125"}, "bpm", "1 to 400"),
        ({"key": "H minor"}, "key", "Camelot"),
        ({"isrc": "123"}, "isrc", "12 characters"),
        ({"composer": "Someone"}, None, "not a tag"),
        ({}, None, "at least one"),
    ],
)
def test_validation_names_the_field(raw: dict[str, str], field: str | None, words: str) -> None:
    with pytest.raises(TagError, match=words) as refused:
        validate_tags(raw)
    assert refused.value.field == field


def test_validation_normalises_accepted_values() -> None:
    values = validate_tags(
        {"key": "8A", "isrc": " ab1cd 2600001 ", "bpm": "128", "year": "2024", "genre": ""}
    )
    assert values.key == (9, 0)
    assert values.isrc == "AB1CD2600001"
    assert values.bpm == 128.0
    assert values.year == 2024
    assert values.genre == ""
    assert values.changed == {"key", "isrc", "bpm", "year", "genre"}
    assert validate_tags({"key": "C major"}).key == (0, 1)
    assert validate_tags({"bpm": ""}).bpm == 0.0


def test_a_failed_write_keeps_the_original(tmp_path: Path, monkeypatch) -> None:
    path = make_audio(tmp_path, ".flac")
    write_tags(path, validate_tags({"title": "Original"}))
    before = path.read_bytes()

    def full(*_args):
        raise OSError(errno.ENOSPC, "No space left on device")

    monkeypatch.setattr(tag_edit, "_apply_vorbis", full)
    with pytest.raises(TagError, match="disk is full"):
        write_tags(path, validate_tags({"title": "Changed"}))
    assert path.read_bytes() == before
    assert sorted(p.name for p in tmp_path.iterdir()) == [path.name]


@pytest.mark.parametrize(
    ("number", "words"),
    [(errno.EROFS, "not allowed"), (errno.EIO, "could not be written")],
)
def test_file_system_refusals_are_explained(tmp_path, monkeypatch, number, words) -> None:
    path = make_audio(tmp_path, ".flac")
    before = path.read_bytes()

    def refuse(*_args, **_kwargs):
        raise OSError(number, os.strerror(number))

    monkeypatch.setattr(tag_edit.shutil, "copy2", refuse)
    with pytest.raises(TagError, match=words):
        write_tags(path, validate_tags({"title": "Changed"}))
    assert path.read_bytes() == before


def test_a_mutagen_failure_keeps_the_original(tmp_path: Path, monkeypatch) -> None:
    path = make_audio(tmp_path, ".mp3")
    before = path.read_bytes()

    def broken(*_args, **_kwargs):
        raise mutagen.MutagenError("broken frame")

    monkeypatch.setattr(mutagen.mp3.MP3, "save", broken)
    with pytest.raises(TagError, match="broken frame"):
        write_tags(path, validate_tags({"title": "Changed"}))
    assert path.read_bytes() == before
    assert sorted(p.name for p in tmp_path.iterdir()) == [path.name]


def test_a_copy_that_reads_back_wrong_is_never_used(tmp_path: Path, monkeypatch) -> None:
    path = make_audio(tmp_path, ".ogg")
    before = path.read_bytes()
    monkeypatch.setattr(tag_edit, "_first_text", lambda *_args: "something else")
    with pytest.raises(TagError, match="did not read back"):
        write_tags(path, validate_tags({"title": "Changed"}))
    assert path.read_bytes() == before


def test_a_file_held_open_is_retried_then_reported_busy(tmp_path, monkeypatch) -> None:
    path = make_audio(tmp_path, ".flac")
    before = path.read_bytes()
    attempts = []

    def held(*_args):
        attempts.append(1)
        raise PermissionError(errno.EACCES, "in use")

    monkeypatch.setattr(tag_edit, "_REPLACE_WAIT_S", 0)
    monkeypatch.setattr(tag_edit.os, "replace", held)
    with pytest.raises(TagFileBusyError, match="open in another program"):
        write_tags(path, validate_tags({"title": "Changed"}))
    assert len(attempts) == tag_edit._REPLACE_ATTEMPTS
    assert path.read_bytes() == before
    assert sorted(p.name for p in tmp_path.iterdir()) == [path.name]


def test_a_rename_failure_other_than_busy_is_explained(tmp_path, monkeypatch) -> None:
    path = make_audio(tmp_path, ".flac")

    def gone(*_args):
        raise OSError(errno.EXDEV, "cross-device link")

    monkeypatch.setattr(tag_edit.os, "replace", gone)
    with pytest.raises(TagError, match="could not be written"):
        write_tags(path, validate_tags({"title": "Changed"}))


def test_a_held_file_that_lets_go_is_replaced(tmp_path, monkeypatch) -> None:
    path = make_audio(tmp_path, ".flac")
    real_replace = os.replace
    calls = []

    def once_busy(source, target):
        calls.append(1)
        if len(calls) == 1:
            raise PermissionError(errno.EACCES, "in use")
        real_replace(source, target)

    monkeypatch.setattr(tag_edit, "_REPLACE_WAIT_S", 0)
    monkeypatch.setattr(tag_edit.os, "replace", once_busy)
    write_tags(path, validate_tags({"title": "Second try"}))
    assert read_file_tags(path).title == "Second try"


@pytest.mark.parametrize("content", [b"", b"not audio at all", None])
def test_unknown_files_are_refused(tmp_path: Path, content: bytes | None) -> None:
    path = tmp_path / "notes.txt"
    if content is not None:
        path.write_bytes(content)
    assert not tags_writable(path)
    with pytest.raises((TagError, OSError)):
        write_tags(path, validate_tags({"title": "x"}))


def test_a_file_mutagen_cannot_parse_is_refused(tmp_path: Path) -> None:
    path = tmp_path / "broken.flac"
    path.write_bytes(b"fLaC" + b"\x00" * 10)
    assert not tags_writable(path)


def test_a_file_whose_tags_cannot_be_added_is_refused(tmp_path, monkeypatch) -> None:
    class NoTags:
        tags = None

        def add_tags(self):
            raise NotImplementedError

    monkeypatch.setattr(mutagen, "File", lambda *_args: NoTags())
    with pytest.raises(TagError, match="cannot edit"):
        tag_edit._open(tmp_path / "x.mod")


def test_tags_of_an_unknown_kind_are_refused() -> None:
    class Other:
        tags = object()

    with pytest.raises(TagError, match="cannot edit"):
        tag_edit._family(Other())


# ---------------------------------------------------------------------------
# Beets
# ---------------------------------------------------------------------------


def _beets_db(path: Path, rows: list[tuple[bytes, str]], *, key_column: bool = True) -> Path:
    db = path / "library.db"
    conn = sqlite3.connect(db)
    extra = ", initial_key TEXT" if key_column else ""
    conn.execute(
        "CREATE TABLE items (id INTEGER PRIMARY KEY, path BLOB, title TEXT, artist TEXT, "
        f"album TEXT, genre TEXT, year INTEGER, bpm INTEGER, mtime REAL{extra})"
    )
    conn.executemany("INSERT INTO items (path, title) VALUES (?, ?)", rows)
    conn.commit()
    conn.close()
    return db


def test_beets_gets_the_changed_columns_and_the_new_mtime(tmp_path: Path) -> None:
    music = tmp_path / "Music"
    music.mkdir()
    song = make_audio(music, ".flac")
    db = _beets_db(tmp_path, [(str(song).encode(), "Old")])
    values = validate_tags({"title": "New", "bpm": "127.6", "key": "8A", "isrc": "AB1CD2600001"})

    assert update_beets(db, str(song), music, values)

    conn = sqlite3.connect(db)
    row = conn.execute("SELECT title, artist, bpm, initial_key, mtime FROM items").fetchone()
    conn.close()
    assert row[:4] == ("New", None, 128, "Am")
    assert row[4] == pytest.approx(song.stat().st_mtime)


def test_beets_finds_a_relative_path_and_reports_a_missing_item(tmp_path: Path) -> None:
    music = tmp_path / "Music"
    (music / "Artist").mkdir(parents=True)
    song = make_audio(music / "Artist", ".flac")
    db = _beets_db(tmp_path, [(b"Artist/song.flac", "Old")], key_column=False)

    assert update_beets(db, str(song), music, validate_tags({"key": "Am", "artist": "Band"}))
    conn = sqlite3.connect(db)
    assert conn.execute("SELECT artist FROM items").fetchone() == ("Band",)
    conn.close()

    other = make_audio(music, ".flac", stem="other")
    assert not update_beets(db, str(other), music, validate_tags({"artist": "Band"}))


def test_beets_without_matching_columns_changes_nothing(tmp_path: Path) -> None:
    db = tmp_path / "library.db"
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE items (id INTEGER PRIMARY KEY, path BLOB)")
    conn.commit()
    conn.close()
    song = make_audio(tmp_path, ".flac")
    assert not update_beets(db, str(song), None, validate_tags({"isrc": "AB1CD2600001"}))


# ---------------------------------------------------------------------------
# The catalog keeps the analysis
# ---------------------------------------------------------------------------


def test_catalog_retag_keeps_vector_points_and_loudness(tmp_path: Path) -> None:
    song = make_audio(tmp_path, ".flac")
    catalog = LibraryCatalog(tmp_path / "library.db")
    try:
        entry = make_entry(song)
        entry.embedded_at = song.stat().st_mtime
        catalog.put_entry(entry, points={"start": 0.0, "end": 0.1}, point_source="user")
        vector = np.arange(FEATURE_DIM, dtype=np.float32)
        catalog.set_vector(song, vector)
        stat = song.stat()
        catalog.set_loudness(
            song,
            {
                "version": "1",
                "kind": "music",
                "size": stat.st_size,
                "mtime_ns": stat.st_mtime_ns,
                "lufs": -14.0,
                "gain_db": 0.0,
            },
        )
        catalog.set_intro_lift(song, 2.0)

        write_tags(song, validate_tags({"title": "Retitled", "key": "Am"}))
        assert catalog.retag(song, {"title": "Retitled", "key": 9, "mode": 0})

        record = catalog.record_for_path(song)
        assert (record["title"], record["key"], record["mode"]) == ("Retitled", 9, 0)
        assert (record["size"], record["fingerprint"]) == file_fingerprint(song)
        assert record["points"] == {"start": 0.0, "end": 0.1}
        assert record["intro_lift_db"] == 2.0
        assert record["loudness"]["size"] == song.stat().st_size
        assert record["loudness"]["mtime_ns"] == song.stat().st_mtime_ns
        assert record["embedded_at"] == song.stat().st_mtime
        assert catalog.has_vector(song)
        other = tmp_path / "other.flac"
        other.touch()
        assert not catalog.retag(other, {"title": "x"})
        with pytest.raises(ValueError, match="only tag fields"):
            catalog.retag(song, {"points": "{}"})
    finally:
        catalog.close()
