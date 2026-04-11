"""Smoke tests for the AutoDJ CLI.

Uses Click's CliRunner to invoke commands without starting a real process.
No audio playback, no model downloads — all heavy dependencies are mocked.
Tests verify that commands complete without crashing and produce expected output.
"""

import json
import sqlite3
from pathlib import Path
from unittest.mock import MagicMock, patch

import faiss
import numpy as np
import pytest
from click.testing import CliRunner

from autodj.cli import cli
from autodj.indexer import FEATURE_DIM, IndexEntry, save_index


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


@pytest.fixture
def project_dir(tmp_path: Path) -> Path:
    """A minimal project directory with config, beets DB, and pre-built index."""
    # Config file
    (tmp_path / "config.toml").write_text(
        f"""
[library]
music_dir = "{tmp_path / "Music"}".replace("\\\\", "/")
beets_db = "{tmp_path / "library.db"}".replace("\\\\", "/")
supported_formats = ["flac"]

[index]
index_dir = "{tmp_path / "index"}".replace("\\\\", "/")
model_dir = "{tmp_path / "models"}".replace("\\\\", "/")

[playback]
crossfade_seconds = 3.0
no_repeat_window = 5

[model]
name = "m-a-p/MERT-v1-330M"
""",
        encoding="utf-8",
    )

    # Beets DB with a few tracks
    conn = sqlite3.connect(tmp_path / "library.db")
    conn.execute(
        "CREATE TABLE items (id INTEGER PRIMARY KEY, path BLOB, title TEXT, "
        "artist TEXT, album TEXT, genre TEXT, bpm REAL, year INTEGER, length REAL)"
    )
    for i in range(5):
        conn.execute(
            "INSERT INTO items VALUES (?,?,?,?,?,?,?,?,?)",
            (i, f"Z:/Music/song_{i}.flac".encode(), f"Song {i}", "Artist", "Album", "Rock", 120.0, 2000, 180.0),
        )
    conn.commit()
    conn.close()

    # Pre-built index
    index_dir = tmp_path / "index"
    index_dir.mkdir()
    entries = [
        IndexEntry(
            path=f"Z:/Music/song_{i}.flac",
            title=f"Song {i}",
            artist="Artist",
            album="Album",
            genre="Rock",
            bpm=120.0,
            year=2000,
            length=180.0,
        )
        for i in range(5)
    ]
    vectors = np.array(
        [np.random.randn(FEATURE_DIM).astype(np.float32) for _ in range(5)]
    )
    norms = np.linalg.norm(vectors, axis=1, keepdims=True)
    vectors /= norms
    save_index(entries, vectors, index_dir)

    return tmp_path


def _write_config(path: Path, tmp_path: Path) -> None:
    """Write a valid config.toml using forward slashes (Windows-safe)."""
    music = str(tmp_path / "Music").replace("\\", "/")
    beets = str(tmp_path / "library.db").replace("\\", "/")
    index = str(tmp_path / "index").replace("\\", "/")
    models = str(tmp_path / "models").replace("\\", "/")
    path.write_text(
        f'[library]\nmusic_dir = "{music}"\nbeets_db = "{beets}"\n'
        f'supported_formats = ["flac"]\n'
        f'[index]\nindex_dir = "{index}"\nmodel_dir = "{models}"\n'
        f'[playback]\ncrossfade_seconds = 3.0\nno_repeat_window = 5\n'
        f'[model]\nname = "m-a-p/MERT-v1-330M"\n',
        encoding="utf-8",
    )


# ---------------------------------------------------------------------------
# --help checks
# ---------------------------------------------------------------------------


class TestHelpText:
    def test_root_help(self, runner: CliRunner) -> None:
        result = runner.invoke(cli, ["--help"])
        assert result.exit_code == 0
        assert "AutoDJ" in result.output

    def test_index_help(self, runner: CliRunner) -> None:
        result = runner.invoke(cli, ["index", "--help"])
        assert result.exit_code == 0
        assert "--limit" in result.output
        assert "--force" in result.output

    def test_play_help(self, runner: CliRunner) -> None:
        result = runner.invoke(cli, ["play", "--help"])
        assert result.exit_code == 0
        assert "--seed" in result.output
        assert "--dry-run" in result.output
        assert "--crossfade" in result.output


# ---------------------------------------------------------------------------
# index command
# ---------------------------------------------------------------------------


class TestIndexCommand:
    def test_index_with_missing_config_exits_1(self, runner: CliRunner, tmp_path: Path) -> None:
        result = runner.invoke(cli, ["--config", str(tmp_path / "nope.toml"), "index"])
        assert result.exit_code == 1

    def test_index_limit_runs_without_crash(self, runner: CliRunner, tmp_path: Path) -> None:
        """Smoke test: 'autodj index --limit 1' completes without crashing.

        Mocks the model loader and build_index so no real audio processing
        or model download occurs.
        """
        config_path = tmp_path / "config.toml"
        _write_config(config_path, tmp_path)

        fake_wrapper = MagicMock()

        with (
            patch("autodj.model.download_model_if_needed", return_value=tmp_path / "model"),
            patch("autodj.model.load_model", return_value=fake_wrapper),
            patch("autodj.indexer.build_index") as mock_build,
        ):
            result = runner.invoke(
                cli,
                ["--config", str(config_path), "index", "--limit", "1"],
            )

        assert result.exit_code == 0, f"Unexpected output:\n{result.output}"
        mock_build.assert_called_once()
        # Verify --limit was forwarded correctly
        call_kwargs = mock_build.call_args.kwargs
        assert call_kwargs.get("limit") == 1


# ---------------------------------------------------------------------------
# play command (dry-run)
# ---------------------------------------------------------------------------


class TestPlayCommand:
    def test_play_dry_run_exits_cleanly(self, runner: CliRunner, tmp_path: Path) -> None:
        config_path = tmp_path / "config.toml"
        _write_config(config_path, tmp_path)

        # Pre-build index
        index_dir = tmp_path / "index"
        index_dir.mkdir(exist_ok=True)
        entries = [
            IndexEntry(
                path=f"Z:/Music/song_{i}.flac",
                title=f"Song {i}",
                artist="Artist",
                album="Album",
                genre="Rock",
                bpm=120.0,
                year=2000,
                length=180.0,
            )
            for i in range(5)
        ]
        vectors = np.array(
            [np.random.randn(FEATURE_DIM).astype(np.float32) for _ in range(5)]
        )
        vectors /= np.linalg.norm(vectors, axis=1, keepdims=True)
        save_index(entries, vectors, index_dir)

        # Player.run loops forever — limit to 1 iteration via side effect
        call_count = {"n": 0}

        def fake_run(seed_entry):
            call_count["n"] += 1
            if call_count["n"] >= 2:
                raise KeyboardInterrupt

        with patch("autodj.player.Player.run", side_effect=fake_run):
            result = runner.invoke(
                cli,
                ["--config", str(config_path), "play", "--dry-run"],
            )

        assert result.exit_code == 0

    def test_play_missing_index_exits_1(self, runner: CliRunner, tmp_path: Path) -> None:
        config_path = tmp_path / "config.toml"
        _write_config(config_path, tmp_path)
        # Don't create the index directory

        result = runner.invoke(cli, ["--config", str(config_path), "play"])
        assert result.exit_code == 1
        assert "Index not found" in result.output or "not found" in result.output.lower()

    def test_play_missing_config_exits_1(self, runner: CliRunner, tmp_path: Path) -> None:
        result = runner.invoke(
            cli, ["--config", str(tmp_path / "nope.toml"), "play"]
        )
        assert result.exit_code == 1
