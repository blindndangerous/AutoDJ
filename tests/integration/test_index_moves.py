"""`autodj index` finds files that moved and keeps their vectors and cues."""

import shutil
import sqlite3
from collections.abc import Iterator
from pathlib import Path
from unittest.mock import patch

import numpy as np
import pytest

from autodj.config import (
    AutoDJConfig,
    HuggingFaceConfig,
    IndexConfig,
    LibraryConfig,
    ModelConfig,
    PlaybackConfig,
)
from autodj.dj_meta import DjMeta, close_cache, get_cache
from autodj.indexer import build_index, load_index
from tests.integration.test_index_pipeline import _fake_wrapper, _setup_librosa_mock

_FAKE_AUDIO = np.zeros(22050, dtype=np.float32)


def _content(i: int) -> bytes:
    return f"track {i} ".encode() * (500 + 37 * i)


class Library:
    """A music folder with a beets database that follows file moves."""

    def __init__(self, root: Path, count: int) -> None:
        self.music = root / "Music"
        self.music.mkdir()
        self.db = root / "library.db"
        conn = sqlite3.connect(self.db)
        conn.execute(
            "CREATE TABLE items (id INTEGER PRIMARY KEY, path BLOB, title TEXT, artist TEXT,"
            " album TEXT, genre TEXT, bpm REAL, year INTEGER, length REAL)"
        )
        conn.commit()
        conn.close()
        for i in range(count):
            self.add(f"song_{i}.flac", _content(i))
        self.wrapper = _fake_wrapper()
        index_dir = root / "index"
        index_dir.mkdir()
        self.cfg = AutoDJConfig(
            library=LibraryConfig(
                music_dir=self.music, beets_db=self.db, supported_formats=["flac"]
            ),
            index=IndexConfig(index_dir=index_dir, model_dir=root / "models"),
            playback=PlaybackConfig(),
            model=ModelConfig(),
            huggingface=HuggingFaceConfig(),
            config_path=root / "config.toml",
        )

    def add(self, rel: str, data: bytes, title: str = "Title") -> None:
        (self.music / rel).parent.mkdir(parents=True, exist_ok=True)
        (self.music / rel).write_bytes(data)
        conn = sqlite3.connect(self.db)
        conn.execute(
            "INSERT INTO items (path, title, artist, album, genre, bpm, year, length)"
            " VALUES (?, ?, 'A', 'B', 'G', 120, 2000, 180)",
            (rel.encode(), title),
        )
        conn.commit()
        conn.close()

    def move(self, old: str, new: str) -> None:
        (self.music / new).parent.mkdir(parents=True, exist_ok=True)
        shutil.move(self.music / old, self.music / new)
        conn = sqlite3.connect(self.db)
        conn.execute("UPDATE items SET path = ? WHERE path = ?", (new.encode(), old.encode()))
        conn.commit()
        conn.close()

    def index(self) -> None:
        with patch("autodj.indexer.sf") as sf, patch("autodj.indexer.librosa") as librosa:
            sf.read.return_value = (_FAKE_AUDIO, 22050)
            _setup_librosa_mock(librosa)
            build_index(self.cfg, wrapper=self.wrapper, limit=None, force=False)

    def entries(self) -> dict[str, int]:
        """Stored path relative to the music folder, mapped to its vector row."""
        entries, _, _ = load_index(self.cfg.index.active_dir, music_dir=self.music)
        return {
            Path(e.path).relative_to(self.music).as_posix(): row for row, e in enumerate(entries)
        }

    def vector(self, row: int) -> np.ndarray:
        _, faiss_index, _ = load_index(self.cfg.index.active_dir)
        return faiss_index.reconstruct(row)

    @property
    def embedded(self) -> int:
        return self.wrapper.embed_array.call_count


@pytest.fixture
def lib(tmp_path: Path) -> Iterator[Library]:
    library = Library(tmp_path, 10)
    yield library
    close_cache()


def test_moved_file_keeps_its_vector_and_cues_and_is_not_embedded(lib: Library) -> None:
    lib.index()
    row = lib.entries()["song_1.flac"]
    vector = lib.vector(row)
    cache = get_cache(lib.cfg.index.active_dir, music_dir=lib.music)
    assert cache is not None
    cache.set(str(lib.music / "song_1.flac"), DjMeta(intro_end_s=12.3, analysed=True))
    cache.flush(force=True)

    lib.move("song_1.flac", "Album/01 Song.flac")
    lib.index()

    assert lib.embedded == 10
    entries = lib.entries()
    assert len(entries) == 10
    assert "song_1.flac" not in entries
    np.testing.assert_array_equal(lib.vector(entries["Album/01 Song.flac"]), vector)
    assert cache.get(str(lib.music / "Album/01 Song.flac")).intro_end_s == 12.3


def test_whole_library_move_embeds_nothing_and_leaves_no_duplicates(lib: Library) -> None:
    lib.index()
    for i in range(10):
        lib.move(f"song_{i}.flac", f"Sorted/song_{i}.flac")

    lib.index()

    assert lib.embedded == 10
    assert sorted(lib.entries()) == sorted(f"Sorted/song_{i}.flac" for i in range(10))


def test_identical_twin_moved_is_not_guessed(lib: Library) -> None:
    lib.add("twin_a.flac", b"same bytes " * 900)
    lib.add("twin_b.flac", b"same bytes " * 900)
    lib.index()
    assert lib.embedded == 12

    lib.move("twin_a.flac", "Moved/twin_a.flac")
    lib.index()

    assert lib.embedded == 13
    assert "twin_a.flac" not in lib.entries()
    assert "Moved/twin_a.flac" in lib.entries()
    assert "twin_b.flac" in lib.entries()


def test_copy_is_a_new_track_and_the_original_stays(lib: Library) -> None:
    lib.index()
    lib.add("copy.flac", _content(2))

    lib.index()

    assert lib.embedded == 11
    assert {"song_2.flac", "copy.flac"} <= set(lib.entries())


def test_moved_and_retagged_file_is_embedded_again(lib: Library) -> None:
    lib.index()
    lib.move("song_3.flac", "Album/song_3.flac")
    path = lib.music / "Album/song_3.flac"
    path.write_bytes(path.read_bytes() + b"new tag block")

    lib.index()

    assert lib.embedded == 11
    entries = lib.entries()
    assert "song_3.flac" not in entries
    assert "Album/song_3.flac" in entries
