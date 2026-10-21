"""Smoke tests for the AutoDJ CLI.

Uses Click's CliRunner to invoke commands without starting a real process.
No audio playback, no model downloads — all heavy dependencies are mocked.
Tests verify that commands complete without crashing and produce expected output.
"""

import sqlite3
from pathlib import Path
from unittest.mock import patch

import numpy as np
import pytest
from click.testing import CliRunner

from autodj.cli import cli
from autodj.config import ENVIRONMENT_OVERLAY
from autodj.indexer import FEATURE_DIM, IndexEntry
from tests.unit._legacy_index import save_index

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def clear_autodj_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    for variable in ENVIRONMENT_OVERLAY:
        monkeypatch.delenv(variable, raising=False)


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


@pytest.fixture
def project_dir(tmp_path: Path) -> Path:
    """A minimal project directory with config, beets DB, and pre-built index."""
    # Config file — use _write_config to get proper path escaping
    _write_config(tmp_path / "config.toml", tmp_path)

    # Beets DB with a few tracks
    conn = sqlite3.connect(tmp_path / "library.db")
    conn.execute(
        "CREATE TABLE items (id INTEGER PRIMARY KEY, path BLOB, title TEXT, "
        "artist TEXT, album TEXT, genre TEXT, bpm REAL, year INTEGER, length REAL)"
    )
    for i in range(5):
        conn.execute(
            "INSERT INTO items VALUES (?,?,?,?,?,?,?,?,?)",
            (
                i,
                f"Z:/Music/song_{i}.flac".encode(),
                f"Song {i}",
                "Artist",
                "Album",
                "Rock",
                120.0,
                2000,
                180.0,
            ),
        )
    conn.commit()
    conn.close()

    # Pre-built index, in the library folder (see IndexConfig.library_dir).
    index_dir = tmp_path / "index" / "libraries" / "main"
    index_dir.mkdir(parents=True)
    entries = [
        IndexEntry(
            path=f"song_{i}.flac",
            title=f"Song {i}",
            artist="Artist",
            album="Album",
            genre="Rock",
            bpm=120.0,
            year=2000,
            length=180.0,
            energy=0.05,
            key=0,
            mode=1,
            tempo_confidence=0.8,
        )
        for i in range(5)
    ]
    vectors = np.array([np.random.randn(FEATURE_DIM).astype(np.float32) for _ in range(5)])
    norms = np.linalg.norm(vectors, axis=1, keepdims=True)
    vectors /= norms
    save_index(entries, vectors, index_dir, base_generation=0)

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
        f"[playback]\ncrossfade_seconds = 3.0\nrepeat_rules = {{ track = {{ tracks = 5 }} }}\n"
        f'[model]\nname = "OpenMuQ/MuQ-large-msd-iter"\n',
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

    def test_serve_help(self, runner: CliRunner) -> None:
        result = runner.invoke(cli, ["serve", "--help"])
        assert result.exit_code == 0
        assert "--lan" in result.output
        assert "--stream" in result.output
        assert "--server-audio" in result.output

    def test_stats_help(self, runner: CliRunner) -> None:
        result = runner.invoke(cli, ["stats", "--help"])
        assert result.exit_code == 0


# ---------------------------------------------------------------------------
# index command
# ---------------------------------------------------------------------------


class TestIndexCommand:
    def test_index_with_missing_config_exits_1(self, runner: CliRunner, tmp_path: Path) -> None:
        result = runner.invoke(cli, ["--config", str(tmp_path / "nope.toml"), "index"])
        assert result.exit_code == 1

    def test_index_empty_library_does_not_load_model(
        self, runner: CliRunner, tmp_path: Path
    ) -> None:
        config_path = tmp_path / "config.toml"
        _write_config(config_path, tmp_path)
        with patch.dict("sys.modules", {"autodj.model": None}):
            result = runner.invoke(cli, ["--config", str(config_path), "index", "--limit", "1"])
        assert result.exit_code == 0, result.output
        assert (tmp_path / "index" / "libraries" / "main" / "library.db").is_file()
        assert not (tmp_path / "index" / "libraries" / "main" / "index-manifest.json").exists()


# ---------------------------------------------------------------------------
# stats command
# ---------------------------------------------------------------------------


class TestStatsCommand:
    def test_stats_requires_library_catalog(self, runner: CliRunner, project_dir: Path) -> None:
        result = runner.invoke(cli, ["--config", str(project_dir / "config.toml"), "stats"])
        assert result.exit_code == 1
        assert "Current library not found" in result.output

    def test_stats_missing_config_exits_1(self, runner: CliRunner, tmp_path: Path) -> None:
        result = runner.invoke(cli, ["--config", str(tmp_path / "nope.toml"), "stats"])
        assert result.exit_code == 1

    def test_stats_missing_index_exits_1(self, runner: CliRunner, tmp_path: Path) -> None:
        config_path = tmp_path / "config.toml"
        _write_config(config_path, tmp_path)
        result = runner.invoke(cli, ["--config", str(config_path), "stats"])
        assert result.exit_code == 1
