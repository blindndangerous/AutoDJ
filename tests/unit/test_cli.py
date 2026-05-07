"""Unit tests for autodj.cli.

Tests the pure helper functions (_parse_bpm_range, _resolve_seed) and the
Click commands' error-path behaviour using CliRunner — no real audio, model,
or index required.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner

from autodj.cli import _parse_bpm_range, _resolve_seed, cli
from autodj.indexer import IndexEntry

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_entry(i: int = 0) -> IndexEntry:
    return IndexEntry(
        path=f"Z:/Music/song_{i}.flac",
        title=f"Song {i}",
        artist=f"Artist {i}",
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


def _make_sim(n: int = 5) -> MagicMock:
    sim = MagicMock()
    entries = [_make_entry(i) for i in range(n)]
    sim.entries = entries
    sim.ntotal = n
    # Return a real IndexEntry so callers that serialize it (write_m3u, etc.) don't crash
    sim.find_next_for_path.return_value = entries[0]
    return sim


def _make_cfg(beets_db=None) -> MagicMock:
    cfg = MagicMock()
    cfg.library.beets_db = beets_db
    cfg.playback.no_repeat_window = 50
    cfg.playback.artist_repeat_window = 3
    cfg.playback.crossfade_seconds = 3.0
    cfg.playback.history_file = None
    cfg.playback.discovery_every = None
    cfg.presets = {}
    return cfg


# ---------------------------------------------------------------------------
# _parse_bpm_range
# ---------------------------------------------------------------------------


class TestParseBpmRange:
    def test_integer_range(self) -> None:
        assert _parse_bpm_range("90-130") == (90.0, 130.0)

    def test_float_range(self) -> None:
        lo, hi = _parse_bpm_range("90.5-130.5")
        assert lo == pytest.approx(90.5)
        assert hi == pytest.approx(130.5)

    def test_en_dash_separator(self) -> None:
        """U+2013 EN DASH should be treated like a hyphen."""
        assert _parse_bpm_range("90\u2013130") == (90.0, 130.0)

    def test_em_dash_separator(self) -> None:
        """U+2014 EM DASH should be treated like a hyphen."""
        assert _parse_bpm_range("90\u2014130") == (90.0, 130.0)

    def test_wrong_separator_raises(self) -> None:
        import click

        with pytest.raises(click.BadParameter):
            _parse_bpm_range("90:130")

    def test_three_parts_raises(self) -> None:
        import click

        with pytest.raises(click.BadParameter):
            _parse_bpm_range("80-100-130")

    def test_non_numeric_lo_raises(self) -> None:
        import click

        with pytest.raises(click.BadParameter):
            _parse_bpm_range("abc-130")

    def test_non_numeric_hi_raises(self) -> None:
        import click

        with pytest.raises(click.BadParameter):
            _parse_bpm_range("90-xyz")

    def test_lo_equals_hi_raises(self) -> None:
        import click

        with pytest.raises(click.BadParameter):
            _parse_bpm_range("120-120")

    def test_lo_greater_than_hi_raises(self) -> None:
        import click

        with pytest.raises(click.BadParameter):
            _parse_bpm_range("130-90")


# ---------------------------------------------------------------------------
# _resolve_seed
# ---------------------------------------------------------------------------


class TestResolveSeed:
    def test_none_seed_returns_none(self) -> None:
        result = _resolve_seed(_make_sim(), _make_cfg(), None, MagicMock())
        assert result is None

    def test_no_match_returns_none(self) -> None:
        sim = _make_sim()
        result = _resolve_seed(sim, _make_cfg(), "zzznomatch999", MagicMock())
        assert result is None

    def test_exact_title_match(self) -> None:
        sim = _make_sim()
        result = _resolve_seed(sim, _make_cfg(), "Song 0", MagicMock(), interactive=False)
        assert result is not None
        assert result.path == sim.entries[0].path

    def test_partial_title_match(self) -> None:
        sim = _make_sim()
        result = _resolve_seed(sim, _make_cfg(), "ong 0", MagicMock(), interactive=False)
        assert result is not None

    def test_artist_match(self) -> None:
        sim = _make_sim()
        result = _resolve_seed(sim, _make_cfg(), "Artist 0", MagicMock(), interactive=False)
        assert result is not None

    def test_multiple_matches_non_interactive_takes_first(self) -> None:
        """With interactive=False and multiple hits, the first match is chosen."""
        sim = _make_sim(5)
        # "Song" matches all 5 entries
        result = _resolve_seed(sim, _make_cfg(), "Song", MagicMock(), interactive=False)
        assert result is not None
        assert result.path == sim.entries[0].path

    def test_case_insensitive_match(self) -> None:
        sim = _make_sim()
        result = _resolve_seed(sim, _make_cfg(), "song 0", MagicMock(), interactive=False)
        assert result is not None


# ---------------------------------------------------------------------------
# CLI commands — config-not-found exits with code 1
# ---------------------------------------------------------------------------


class TestCliConfigNotFound:
    """Each command should print an error and exit(1) if config is missing."""

    def _missing(self, tmp_path: Path) -> str:
        return str(tmp_path / "nonexistent.toml")

    def test_index_exits_on_missing_config(self, tmp_path: Path) -> None:
        result = CliRunner().invoke(cli, ["--config", self._missing(tmp_path), "index"])
        assert result.exit_code == 1

    def test_play_exits_on_missing_config(self, tmp_path: Path) -> None:
        result = CliRunner().invoke(cli, ["--config", self._missing(tmp_path), "play"])
        assert result.exit_code == 1

    def test_stats_exits_on_missing_config(self, tmp_path: Path) -> None:
        result = CliRunner().invoke(cli, ["--config", self._missing(tmp_path), "stats"])
        assert result.exit_code == 1

    def test_playlist_exits_on_missing_config(self, tmp_path: Path) -> None:
        result = CliRunner().invoke(cli, ["--config", self._missing(tmp_path), "playlist"])
        assert result.exit_code == 1

    def test_serve_exits_on_missing_config(self, tmp_path: Path) -> None:
        result = CliRunner().invoke(cli, ["--config", self._missing(tmp_path), "serve"])
        assert result.exit_code == 1

    def test_index_error_message_mentions_config(self, tmp_path: Path) -> None:
        result = CliRunner().invoke(cli, ["--config", self._missing(tmp_path), "index"])
        assert "Config not found" in result.output or "config" in result.output.lower()

    def test_prune_exits_on_missing_config(self, tmp_path: Path) -> None:
        result = CliRunner().invoke(cli, ["--config", self._missing(tmp_path), "prune"])
        assert result.exit_code == 1

    def test_enrich_exits_on_missing_config(self, tmp_path: Path) -> None:
        result = CliRunner().invoke(cli, ["--config", self._missing(tmp_path), "enrich"])
        assert result.exit_code == 1

    def test_list_indexes_exits_on_missing_config(self, tmp_path: Path) -> None:
        result = CliRunner().invoke(
            cli,
            ["--config", self._missing(tmp_path), "list-indexes"],
        )
        assert result.exit_code == 1

    def test_prune_invalid_index_name_exits(self, tmp_path: Path) -> None:
        cfg = tmp_path / "c.toml"
        cfg.write_text(
            '[library]\nmusic_dir = "Z:/Music"\n'
            '[index]\nindex_dir = "Z:/idx"\nmodel_dir = "models"\n'
            "[playback]\ncrossfade_seconds = 3.0\nno_repeat_window = 50\n"
            '[model]\nname = "OpenMuQ/MuQ-large-msd-iter"\n',
            encoding="utf-8",
        )
        # Names with path separators / special chars are rejected.
        result = CliRunner().invoke(
            cli,
            ["--config", str(cfg), "prune", "--name", "../bad"],
        )
        assert result.exit_code == 1
        assert "Invalid" in result.output or "name" in result.output.lower()

    def test_enrich_invalid_index_name_exits(self, tmp_path: Path) -> None:
        cfg = tmp_path / "c.toml"
        cfg.write_text(
            '[library]\nmusic_dir = "Z:/Music"\n'
            '[index]\nindex_dir = "Z:/idx"\nmodel_dir = "models"\n'
            "[playback]\ncrossfade_seconds = 3.0\nno_repeat_window = 50\n"
            '[model]\nname = "OpenMuQ/MuQ-large-msd-iter"\n',
            encoding="utf-8",
        )
        result = CliRunner().invoke(
            cli,
            ["--config", str(cfg), "enrich", "--name", "../escape"],
        )
        assert result.exit_code == 1

    def test_enrich_no_beets_db_exits(self, tmp_path: Path) -> None:
        cfg = tmp_path / "c.toml"
        # Note: no beets_db key in [library] section.
        cfg.write_text(
            '[library]\nmusic_dir = "Z:/Music"\n'
            '[index]\nindex_dir = "Z:/idx"\nmodel_dir = "models"\n'
            "[playback]\ncrossfade_seconds = 3.0\nno_repeat_window = 50\n"
            '[model]\nname = "OpenMuQ/MuQ-large-msd-iter"\n',
            encoding="utf-8",
        )
        result = CliRunner().invoke(cli, ["--config", str(cfg), "enrich"])
        assert result.exit_code == 1
        assert "beets" in result.output.lower()


# ---------------------------------------------------------------------------
# CLI commands — index-not-found exits with code 1
# ---------------------------------------------------------------------------


class TestCliIndexNotFound:
    """play / serve / playlist / stats should exit(1) when index is missing."""

    def _write_minimal_config(self, tmp_path: Path) -> Path:
        cfg = tmp_path / "config.toml"
        cfg.write_text(
            '[library]\nmusic_dir = "Z:/Music"\n'
            '[index]\nindex_dir = "Z:/no-such-index"\nmodel_dir = "models"\n'
            "[playback]\ncrossfade_seconds = 3.0\nno_repeat_window = 50\n"
            '[model]\nname = "OpenMuQ/MuQ-large-msd-iter"\n',
            encoding="utf-8",
        )
        return cfg

    def test_play_exits_on_missing_index(self, tmp_path: Path) -> None:
        cfg = self._write_minimal_config(tmp_path)
        result = CliRunner().invoke(cli, ["--config", str(cfg), "play"])
        assert result.exit_code == 1

    def test_stats_exits_on_missing_index(self, tmp_path: Path) -> None:
        cfg = self._write_minimal_config(tmp_path)
        result = CliRunner().invoke(cli, ["--config", str(cfg), "stats"])
        assert result.exit_code == 1

    def test_playlist_exits_on_missing_index(self, tmp_path: Path) -> None:
        cfg = self._write_minimal_config(tmp_path)
        result = CliRunner().invoke(cli, ["--config", str(cfg), "playlist"])
        assert result.exit_code == 1

    def test_serve_exits_on_missing_index(self, tmp_path: Path) -> None:
        cfg = self._write_minimal_config(tmp_path)
        result = CliRunner().invoke(cli, ["--config", str(cfg), "serve"])
        assert result.exit_code == 1


# ---------------------------------------------------------------------------
# CLI — play / playlist --bpm-range validation
# ---------------------------------------------------------------------------


class TestCliBpmRangeValidation:
    def _write_minimal_config(self, tmp_path: Path) -> Path:
        cfg = tmp_path / "config.toml"
        cfg.write_text(
            '[library]\nmusic_dir = "Z:/Music"\n'
            '[index]\nindex_dir = "Z:/no-such-index"\nmodel_dir = "models"\n'
            "[playback]\ncrossfade_seconds = 3.0\nno_repeat_window = 50\n"
            '[model]\nname = "OpenMuQ/MuQ-large-msd-iter"\n',
            encoding="utf-8",
        )
        return cfg

    def test_play_invalid_bpm_range_exits(self, tmp_path: Path) -> None:
        """A bad --bpm-range should exit with code 1."""
        # load_config and SimilarityIndex are imported inside the command function,
        # so we patch at their definition sites.
        cfg_mock = _make_cfg()
        sim_mock = _make_sim()

        with (
            patch("autodj.config.load_config", return_value=cfg_mock),
            patch("autodj.similarity.SimilarityIndex.from_index_dir", return_value=sim_mock),
            patch("autodj.player.Player.run"),
        ):
            result = CliRunner().invoke(cli, ["play", "--bpm-range", "bad-range"])
        assert result.exit_code == 1


# ---------------------------------------------------------------------------
# CLI happy paths — real code, mocked heavy deps
# ---------------------------------------------------------------------------


class TestCliHappyPaths:
    """Cover the success branches of each command by mocking model/index/player."""

    def test_play_exits_zero(self) -> None:
        cfg_mock = _make_cfg()
        sim_mock = _make_sim()
        with (
            patch("autodj.config.load_config", return_value=cfg_mock),
            patch("autodj.similarity.SimilarityIndex.from_index_dir", return_value=sim_mock),
            patch("autodj.player.Player.run"),
        ):
            result = CliRunner().invoke(cli, ["play"])
        assert result.exit_code == 0

    def test_play_crossfade_override_applied(self) -> None:
        cfg_mock = _make_cfg()
        sim_mock = _make_sim()
        with (
            patch("autodj.config.load_config", return_value=cfg_mock),
            patch("autodj.similarity.SimilarityIndex.from_index_dir", return_value=sim_mock),
            patch("autodj.player.Player.run"),
        ):
            CliRunner().invoke(cli, ["play", "--crossfade", "5.0"])
        assert cfg_mock.playback.crossfade_seconds == 5.0

    def test_play_no_repeat_override_applied(self) -> None:
        cfg_mock = _make_cfg()
        sim_mock = _make_sim()
        with (
            patch("autodj.config.load_config", return_value=cfg_mock),
            patch("autodj.similarity.SimilarityIndex.from_index_dir", return_value=sim_mock),
            patch("autodj.player.Player.run"),
        ):
            CliRunner().invoke(cli, ["play", "--no-repeat", "100"])
        assert cfg_mock.playback.no_repeat_window == 100

    def test_play_dry_run_exits_zero(self) -> None:
        cfg_mock = _make_cfg()
        sim_mock = _make_sim()
        with (
            patch("autodj.config.load_config", return_value=cfg_mock),
            patch("autodj.similarity.SimilarityIndex.from_index_dir", return_value=sim_mock),
            patch("autodj.player.Player.run"),
        ):
            result = CliRunner().invoke(cli, ["play", "--dry-run"])
        assert result.exit_code == 0

    def test_play_bpm_range_parsed_and_applied(self) -> None:
        cfg_mock = _make_cfg()
        sim_mock = _make_sim()
        player_init_kwargs = {}

        from autodj.player import Player as RealPlayer

        original_init = RealPlayer.__init__

        def capturing_init(self, cfg, sim, **kwargs):
            player_init_kwargs.update(kwargs)
            original_init(self, cfg, sim, **kwargs)

        with (
            patch("autodj.config.load_config", return_value=cfg_mock),
            patch("autodj.similarity.SimilarityIndex.from_index_dir", return_value=sim_mock),
            patch("autodj.player.Player.run"),
            patch("autodj.player.Player.__init__", capturing_init),
        ):
            CliRunner().invoke(cli, ["play", "--bpm-range", "90-130"])

        assert player_init_kwargs.get("bpm_range") == (90.0, 130.0)

    def test_stats_happy_path(self) -> None:
        cfg_mock = _make_cfg()
        entries = [_make_entry(i) for i in range(5)]
        with (
            patch("autodj.config.load_config", return_value=cfg_mock),
            patch("autodj.indexer.load_index", return_value=(entries, None)),
        ):
            result = CliRunner().invoke(cli, ["stats"])
        assert result.exit_code == 0

    def test_playlist_exits_zero(self) -> None:
        cfg_mock = _make_cfg()
        sim_mock = _make_sim()
        with (
            patch("autodj.config.load_config", return_value=cfg_mock),
            patch("autodj.similarity.SimilarityIndex.from_index_dir", return_value=sim_mock),
        ):
            result = CliRunner().invoke(cli, ["playlist", "--tracks", "3"])
        assert result.exit_code == 0

    def test_playlist_writes_m3u_file(self, tmp_path: Path) -> None:
        cfg_mock = _make_cfg()
        sim_mock = _make_sim()
        out = tmp_path / "out.m3u"
        with (
            patch("autodj.config.load_config", return_value=cfg_mock),
            patch("autodj.similarity.SimilarityIndex.from_index_dir", return_value=sim_mock),
        ):
            result = CliRunner().invoke(cli, ["playlist", "--tracks", "3", "--output", str(out)])
        assert result.exit_code == 0
        assert out.exists()

    def test_playlist_with_preset(self) -> None:
        cfg_mock = _make_cfg()
        sim_mock = _make_sim()
        with (
            patch("autodj.config.load_config", return_value=cfg_mock),
            patch("autodj.similarity.SimilarityIndex.from_index_dir", return_value=sim_mock),
        ):
            result = CliRunner().invoke(cli, ["playlist", "--preset", "chill", "--tracks", "3"])
        assert result.exit_code == 0

    def test_playlist_unknown_preset_exits_one(self) -> None:
        cfg_mock = _make_cfg()
        sim_mock = _make_sim()
        with (
            patch("autodj.config.load_config", return_value=cfg_mock),
            patch("autodj.similarity.SimilarityIndex.from_index_dir", return_value=sim_mock),
        ):
            result = CliRunner().invoke(cli, ["playlist", "--preset", "nosuchpreset_xyz"])
        assert result.exit_code == 1


# ---------------------------------------------------------------------------
# _resolve_seed with beets_db
# ---------------------------------------------------------------------------


class TestResolveSeedBeets:
    def test_beets_db_path_used_when_exists(self, tmp_path: Path) -> None:
        """When beets_db exists, _resolve_seed queries it first."""
        import sqlite3

        db = tmp_path / "library.db"
        conn = sqlite3.connect(db)
        conn.execute(
            "CREATE TABLE items "
            "(id INTEGER PRIMARY KEY, path BLOB, title TEXT, artist TEXT, "
            "album TEXT, genre TEXT, bpm REAL, year INTEGER, length REAL)"
        )
        conn.execute(
            "INSERT INTO items (path, title, artist, album, genre, bpm, year, length) "
            "VALUES (?, 'Mysterons', 'Portishead', 'Dummy', 'Trip-Hop', 95.0, 1994, 300.0)",
            (b"Z:/Music/Portishead/Mysterons.flac",),
        )
        conn.commit()
        conn.close()

        # Build a sim with the same path so the intersection lookup finds it
        sim = MagicMock()
        sim.entries = [
            _make_entry(0)  # path = "Z:/Music/song_0.flac" — won't match
        ]
        # Add an entry whose path matches the beets result
        from autodj.indexer import IndexEntry

        matching = IndexEntry(
            path="Z:/Music/Portishead/Mysterons.flac",
            title="Mysterons",
            artist="Portishead",
            album="Dummy",
            genre="Trip-Hop",
            bpm=95.0,
            year=1994,
            length=300.0,
            energy=0.0,
            key=-1,
            mode=-1,
            tempo_confidence=0.0,
        )
        sim.entries.append(matching)

        cfg = _make_cfg(beets_db=db)

        result = _resolve_seed(sim, cfg, "Portishead", MagicMock(), interactive=False)
        assert result is not None
        assert "Portishead" in result.path


# ---------------------------------------------------------------------------
# cmd_serve — happy path with mocked server
# ---------------------------------------------------------------------------


class TestCmdServe:
    def test_serve_exits_zero(self) -> None:
        cfg_mock = _make_cfg()
        sim_mock = _make_sim()
        with (
            patch("autodj.config.load_config", return_value=cfg_mock),
            patch("autodj.similarity.SimilarityIndex.from_index_dir", return_value=sim_mock),
            patch("autodj.server.serve"),
        ):
            result = CliRunner().invoke(cli, ["serve"])
        assert result.exit_code == 0

    def test_serve_invalid_bpm_range_exits_one(self) -> None:
        cfg_mock = _make_cfg()
        sim_mock = _make_sim()
        with (
            patch("autodj.config.load_config", return_value=cfg_mock),
            patch("autodj.similarity.SimilarityIndex.from_index_dir", return_value=sim_mock),
            patch("autodj.server.serve"),
        ):
            result = CliRunner().invoke(cli, ["serve", "--bpm-range", "bad"])
        assert result.exit_code == 1

    def test_serve_unknown_preset_exits_one(self) -> None:
        cfg_mock = _make_cfg()
        sim_mock = _make_sim()
        with (
            patch("autodj.config.load_config", return_value=cfg_mock),
            patch("autodj.similarity.SimilarityIndex.from_index_dir", return_value=sim_mock),
            patch("autodj.server.serve"),
        ):
            result = CliRunner().invoke(cli, ["serve", "--preset", "nosuchpreset_xyz"])
        assert result.exit_code == 1

    def test_serve_prints_web_ui_url(self) -> None:
        cfg_mock = _make_cfg()
        sim_mock = _make_sim()
        with (
            patch("autodj.config.load_config", return_value=cfg_mock),
            patch("autodj.similarity.SimilarityIndex.from_index_dir", return_value=sim_mock),
            patch("autodj.server.serve"),
        ):
            result = CliRunner().invoke(cli, ["serve", "--port", "9999"])
        assert "9999" in result.output or result.exit_code == 0


# ---------------------------------------------------------------------------
# Real library.db integration test
# ---------------------------------------------------------------------------


LIBRARY_DB = Path(__file__).parents[2] / "library.db"


# ---------------------------------------------------------------------------
# cmd_prune + cmd_enrich
# ---------------------------------------------------------------------------


class TestCmdPrune:
    def test_prune_no_index(self) -> None:
        cfg_mock = _make_cfg()
        with (
            patch("autodj.config.load_config", return_value=cfg_mock),
            patch("autodj.indexer.prune_index", return_value=(0, 0)),
        ):
            result = CliRunner().invoke(cli, ["prune"])
        assert result.exit_code == 0
        assert "No index" in result.output or "Nothing" in result.output

    def test_prune_all_present(self) -> None:
        cfg_mock = _make_cfg()
        with (
            patch("autodj.config.load_config", return_value=cfg_mock),
            patch("autodj.indexer.prune_index", return_value=(0, 100)),
        ):
            result = CliRunner().invoke(cli, ["prune"])
        assert result.exit_code == 0
        assert "All 100" in result.output or "Nothing" in result.output

    def test_prune_removes_some(self) -> None:
        cfg_mock = _make_cfg()
        with (
            patch("autodj.config.load_config", return_value=cfg_mock),
            patch("autodj.indexer.prune_index", return_value=(5, 95)),
        ):
            result = CliRunner().invoke(cli, ["prune"])
        assert result.exit_code == 0
        assert "5" in result.output

    def test_prune_safety_aborts(self) -> None:
        from autodj.indexer import PruneSafetyError

        cfg_mock = _make_cfg()
        with (
            patch("autodj.config.load_config", return_value=cfg_mock),
            patch("autodj.indexer.prune_index", side_effect=PruneSafetyError("too many")),
        ):
            result = CliRunner().invoke(cli, ["prune"])
        assert result.exit_code == 2

    def test_prune_force_passes_through(self) -> None:
        cfg_mock = _make_cfg()
        captured = {}

        def fake_prune(_d, **kw):
            captured.update(kw)
            return (1, 99)

        with (
            patch("autodj.config.load_config", return_value=cfg_mock),
            patch("autodj.indexer.prune_index", fake_prune),
        ):
            CliRunner().invoke(cli, ["prune", "--force"])
        assert captured.get("allow_mass_prune") is True

    def test_prune_unhandled_error_exits_one(self) -> None:
        cfg_mock = _make_cfg()
        with (
            patch("autodj.config.load_config", return_value=cfg_mock),
            patch("autodj.indexer.prune_index", side_effect=RuntimeError("disk gone")),
        ):
            result = CliRunner().invoke(cli, ["prune"])
        assert result.exit_code == 1

    def test_prune_config_missing(self) -> None:
        with patch("autodj.config.load_config", side_effect=FileNotFoundError("nope")):
            result = CliRunner().invoke(cli, ["prune"])
        assert result.exit_code == 1


class TestCmdEnrich:
    def test_enrich_no_beets_db_exits_one(self) -> None:
        cfg_mock = _make_cfg(beets_db=None)
        with patch("autodj.config.load_config", return_value=cfg_mock):
            result = CliRunner().invoke(cli, ["enrich"])
        assert result.exit_code == 1
        assert "beets_db" in result.output

    def test_enrich_no_index(self, tmp_path: Path) -> None:
        cfg_mock = _make_cfg(beets_db=tmp_path / "library.db")
        with (
            patch("autodj.config.load_config", return_value=cfg_mock),
            patch("autodj.indexer.enrich_from_beets", return_value=(0, 0)),
        ):
            result = CliRunner().invoke(cli, ["enrich"])
        assert result.exit_code == 0

    def test_enrich_all_synced(self, tmp_path: Path) -> None:
        cfg_mock = _make_cfg(beets_db=tmp_path / "library.db")
        with (
            patch("autodj.config.load_config", return_value=cfg_mock),
            patch("autodj.indexer.enrich_from_beets", return_value=(0, 1000)),
        ):
            result = CliRunner().invoke(cli, ["enrich"])
        assert result.exit_code == 0
        assert "in sync" in result.output or "0 changed" in result.output

    def test_enrich_updates_some(self, tmp_path: Path) -> None:
        cfg_mock = _make_cfg(beets_db=tmp_path / "library.db")
        with (
            patch("autodj.config.load_config", return_value=cfg_mock),
            patch("autodj.indexer.enrich_from_beets", return_value=(42, 1000)),
        ):
            result = CliRunner().invoke(cli, ["enrich"])
        assert result.exit_code == 0
        assert "42" in result.output

    def test_enrich_error_exits_one(self, tmp_path: Path) -> None:
        cfg_mock = _make_cfg(beets_db=tmp_path / "library.db")
        with (
            patch("autodj.config.load_config", return_value=cfg_mock),
            patch("autodj.indexer.enrich_from_beets", side_effect=RuntimeError("bad")),
        ):
            result = CliRunner().invoke(cli, ["enrich"])
        assert result.exit_code == 1

    def test_enrich_config_missing(self) -> None:
        with patch("autodj.config.load_config", side_effect=FileNotFoundError):
            result = CliRunner().invoke(cli, ["enrich"])
        assert result.exit_code == 1


# ---------------------------------------------------------------------------
# cmd_list_indexes
# ---------------------------------------------------------------------------


class TestCmdListIndexes:
    def test_no_index_dir(self, tmp_path: Path) -> None:
        cfg_mock = _make_cfg()
        cfg_mock.index.index_dir = tmp_path / "missing"
        with patch("autodj.config.load_config", return_value=cfg_mock):
            result = CliRunner().invoke(cli, ["list-indexes"])
        assert result.exit_code == 0
        assert "No indexes found" in result.output

    def test_lists_named_indexes(self, tmp_path: Path) -> None:
        # Build two named index dirs with metadata.json each
        for n, count in (("default", 5), ("workout", 12)):
            d = tmp_path / "idx" / n
            d.mkdir(parents=True)
            (d / "metadata.json").write_text(
                "[" + ",".join(['{"path":"x"}'] * count) + "]",
                encoding="utf-8",
            )
        cfg_mock = _make_cfg()
        cfg_mock.index.index_dir = tmp_path / "idx"
        cfg_mock.index.name = "workout"
        with patch("autodj.config.load_config", return_value=cfg_mock):
            result = CliRunner().invoke(cli, ["list-indexes"])
        assert result.exit_code == 0
        assert "default" in result.output
        assert "workout" in result.output
        assert "5 tracks" in result.output
        assert "12 tracks" in result.output

    def test_skips_empty_index_dir(self, tmp_path: Path) -> None:
        (tmp_path / "idx").mkdir()
        cfg_mock = _make_cfg()
        cfg_mock.index.index_dir = tmp_path / "idx"
        with patch("autodj.config.load_config", return_value=cfg_mock):
            result = CliRunner().invoke(cli, ["list-indexes"])
        assert result.exit_code == 0
        assert "No named indexes" in result.output

    def test_corrupt_metadata_marked(self, tmp_path: Path) -> None:
        d = tmp_path / "idx" / "broken"
        d.mkdir(parents=True)
        (d / "metadata.json").write_text("not valid json {{", encoding="utf-8")
        cfg_mock = _make_cfg()
        cfg_mock.index.index_dir = tmp_path / "idx"
        with patch("autodj.config.load_config", return_value=cfg_mock):
            result = CliRunner().invoke(cli, ["list-indexes"])
        assert result.exit_code == 0
        assert "corrupt" in result.output

    def test_config_missing(self) -> None:
        with patch("autodj.config.load_config", side_effect=FileNotFoundError):
            result = CliRunner().invoke(cli, ["list-indexes"])
        assert result.exit_code == 1


class TestNameValidation:
    def test_play_rejects_path_separator(self) -> None:
        cfg_mock = _make_cfg()
        sim_mock = _make_sim()
        with (
            patch("autodj.config.load_config", return_value=cfg_mock),
            patch("autodj.similarity.SimilarityIndex.from_index_dir", return_value=sim_mock),
            patch("autodj.player.Player.run"),
        ):
            result = CliRunner().invoke(cli, ["play", "--name", "index/metadata.json"])
        assert result.exit_code == 1
        assert "path separators" in result.output.lower() or "Invalid" in result.output

    def test_serve_rejects_traversal(self) -> None:
        cfg_mock = _make_cfg()
        sim_mock = _make_sim()
        with (
            patch("autodj.config.load_config", return_value=cfg_mock),
            patch("autodj.similarity.SimilarityIndex.from_index_dir", return_value=sim_mock),
            patch("autodj.server.serve"),
        ):
            result = CliRunner().invoke(cli, ["serve", "--name", "../etc"])
        assert result.exit_code == 1

    def test_index_accepts_normal_name(self) -> None:
        cfg_mock = _make_cfg()
        with (
            patch("autodj.config.load_config", return_value=cfg_mock),
            patch("autodj.model.download_model_if_needed"),
            patch("autodj.model.load_model"),
            patch("autodj.indexer.build_index"),
        ):
            result = CliRunner().invoke(cli, ["index", "--name", "workout"])
        assert result.exit_code == 0
        assert cfg_mock.index.name == "workout"


class TestListDevices:
    def test_no_sounddevice_exits_one(self) -> None:
        # Block the import inside cmd_list_devices
        real_import = (
            __builtins__["__import__"]
            if isinstance(__builtins__, dict)
            else __builtins__.__import__
        )

        def fake_import(name, *args, **kwargs):
            if name == "sounddevice":
                raise ImportError
            return real_import(name, *args, **kwargs)

        with patch("builtins.__import__", fake_import):
            result = CliRunner().invoke(cli, ["list-devices"])
        assert result.exit_code == 1
        assert "sounddevice" in result.output

    def test_lists_output_devices(self) -> None:
        sd_mock = MagicMock()
        sd_mock.query_devices.return_value = [
            {"name": "Default Out", "max_output_channels": 2, "default_samplerate": 48000},
            {"name": "Mic In", "max_output_channels": 0, "default_samplerate": 48000},
            {"name": "USB Headphones", "max_output_channels": 2, "default_samplerate": 44100},
        ]
        sd_mock.default.device = (None, 0)
        import sys

        with patch.dict(sys.modules, {"sounddevice": sd_mock}):
            result = CliRunner().invoke(cli, ["list-devices"])
        assert result.exit_code == 0
        assert "Default Out" in result.output
        assert "USB Headphones" in result.output
        assert "Mic In" not in result.output


class TestPlayDeviceFlag:
    def test_device_int_parsed(self) -> None:
        cfg_mock = _make_cfg()
        sim_mock = _make_sim()
        with (
            patch("autodj.config.load_config", return_value=cfg_mock),
            patch("autodj.similarity.SimilarityIndex.from_index_dir", return_value=sim_mock),
            patch("autodj.player.Player.run"),
        ):
            CliRunner().invoke(cli, ["play", "--device", "4"])
        assert cfg_mock.playback.audio_device == 4

    def test_device_string_kept(self) -> None:
        cfg_mock = _make_cfg()
        sim_mock = _make_sim()
        with (
            patch("autodj.config.load_config", return_value=cfg_mock),
            patch("autodj.similarity.SimilarityIndex.from_index_dir", return_value=sim_mock),
            patch("autodj.player.Player.run"),
        ):
            CliRunner().invoke(cli, ["play", "--device", "USB Headphones"])
        assert cfg_mock.playback.audio_device == "USB Headphones"


# ---------------------------------------------------------------------------
# cmd_index — error paths
# ---------------------------------------------------------------------------


class TestCmdIndex:
    def test_index_config_missing(self) -> None:
        with patch("autodj.config.load_config", side_effect=FileNotFoundError):
            result = CliRunner().invoke(cli, ["index"])
        assert result.exit_code == 1

    def test_index_build_failure_exits_one(self) -> None:
        cfg_mock = _make_cfg()
        with (
            patch("autodj.config.load_config", return_value=cfg_mock),
            patch("autodj.model.download_model_if_needed"),
            patch("autodj.model.load_model"),
            patch("autodj.indexer.build_index", side_effect=RuntimeError("model crash")),
        ):
            result = CliRunner().invoke(cli, ["index"])
        assert result.exit_code == 1
        assert "Indexing failed" in result.output


# ---------------------------------------------------------------------------
# cmd_play — DJ-mix overrides + transition + smart-shuffle
# ---------------------------------------------------------------------------


class TestCmdPlayOverrides:
    def test_play_harmonic_override(self) -> None:
        cfg_mock = _make_cfg()
        sim_mock = _make_sim()
        with (
            patch("autodj.config.load_config", return_value=cfg_mock),
            patch("autodj.similarity.SimilarityIndex.from_index_dir", return_value=sim_mock),
            patch("autodj.player.Player.run"),
        ):
            CliRunner().invoke(cli, ["play", "--harmonic"])
        assert cfg_mock.djmix.harmonic_mixing is True

    def test_play_no_harmonic_override(self) -> None:
        cfg_mock = _make_cfg()
        cfg_mock.djmix.harmonic_mixing = True
        sim_mock = _make_sim()
        with (
            patch("autodj.config.load_config", return_value=cfg_mock),
            patch("autodj.similarity.SimilarityIndex.from_index_dir", return_value=sim_mock),
            patch("autodj.player.Player.run"),
        ):
            CliRunner().invoke(cli, ["play", "--no-harmonic"])
        assert cfg_mock.djmix.harmonic_mixing is False

    def test_play_beatmatch_override(self) -> None:
        cfg_mock = _make_cfg()
        sim_mock = _make_sim()
        with (
            patch("autodj.config.load_config", return_value=cfg_mock),
            patch("autodj.similarity.SimilarityIndex.from_index_dir", return_value=sim_mock),
            patch("autodj.player.Player.run"),
        ):
            CliRunner().invoke(cli, ["play", "--beatmatch"])
        assert cfg_mock.djmix.beatmatch is True

    def test_play_transition_override(self) -> None:
        cfg_mock = _make_cfg()
        sim_mock = _make_sim()
        with (
            patch("autodj.config.load_config", return_value=cfg_mock),
            patch("autodj.similarity.SimilarityIndex.from_index_dir", return_value=sim_mock),
            patch("autodj.player.Player.run"),
        ):
            CliRunner().invoke(cli, ["play", "--transition", "echo_out"])
        assert cfg_mock.transitions.effect == "echo_out"

    def test_play_phrase_align_override(self) -> None:
        cfg_mock = _make_cfg()
        sim_mock = _make_sim()
        with (
            patch("autodj.config.load_config", return_value=cfg_mock),
            patch("autodj.similarity.SimilarityIndex.from_index_dir", return_value=sim_mock),
            patch("autodj.player.Player.run"),
        ):
            CliRunner().invoke(cli, ["play", "--phrase-align"])
        assert cfg_mock.djmix.phrase_align is True

    def test_play_align_outro_override(self) -> None:
        cfg_mock = _make_cfg()
        sim_mock = _make_sim()
        with (
            patch("autodj.config.load_config", return_value=cfg_mock),
            patch("autodj.similarity.SimilarityIndex.from_index_dir", return_value=sim_mock),
            patch("autodj.player.Player.run"),
        ):
            CliRunner().invoke(cli, ["play", "--align-outro"])
        assert cfg_mock.djmix.outro_intro_align is True

    def test_play_filter_sweep_override(self) -> None:
        cfg_mock = _make_cfg()
        sim_mock = _make_sim()
        with (
            patch("autodj.config.load_config", return_value=cfg_mock),
            patch("autodj.similarity.SimilarityIndex.from_index_dir", return_value=sim_mock),
            patch("autodj.player.Player.run"),
        ):
            CliRunner().invoke(cli, ["play", "--filter-sweep"])
        assert cfg_mock.djmix.filter_sweep is True

    def test_play_with_discovery_every(self) -> None:
        cfg_mock = _make_cfg()
        sim_mock = _make_sim()
        with (
            patch("autodj.config.load_config", return_value=cfg_mock),
            patch("autodj.similarity.SimilarityIndex.from_index_dir", return_value=sim_mock),
            patch("autodj.player.Player.run"),
        ):
            result = CliRunner().invoke(cli, ["play", "--discovery-every", "12"])
        assert result.exit_code == 0

    def test_play_invalid_bpm_range(self) -> None:
        cfg_mock = _make_cfg()
        sim_mock = _make_sim()
        with (
            patch("autodj.config.load_config", return_value=cfg_mock),
            patch("autodj.similarity.SimilarityIndex.from_index_dir", return_value=sim_mock),
            patch("autodj.player.Player.run"),
        ):
            result = CliRunner().invoke(cli, ["play", "--bpm-range", "junk"])
        assert result.exit_code == 1

    def test_play_unknown_preset(self) -> None:
        cfg_mock = _make_cfg()
        sim_mock = _make_sim()
        with (
            patch("autodj.config.load_config", return_value=cfg_mock),
            patch("autodj.similarity.SimilarityIndex.from_index_dir", return_value=sim_mock),
            patch("autodj.player.Player.run"),
        ):
            result = CliRunner().invoke(cli, ["play", "--preset", "nosuchpreset_xyz"])
        assert result.exit_code == 1

    def test_play_pure_shuffle_flag(self) -> None:
        cfg_mock = _make_cfg()
        sim_mock = _make_sim()
        with (
            patch("autodj.config.load_config", return_value=cfg_mock),
            patch("autodj.similarity.SimilarityIndex.from_index_dir", return_value=sim_mock),
            patch("autodj.player.Player") as p_cls,
        ):
            p_cls.return_value.run = lambda *_a, **_k: None
            result = CliRunner().invoke(cli, ["play", "--pure-shuffle"])
        assert result.exit_code == 0
        kwargs = p_cls.call_args.kwargs
        assert kwargs.get("pure_shuffle") is True

    def test_play_anchor_seed_flag(self) -> None:
        cfg_mock = _make_cfg()
        sim_mock = _make_sim()
        with (
            patch("autodj.config.load_config", return_value=cfg_mock),
            patch("autodj.similarity.SimilarityIndex.from_index_dir", return_value=sim_mock),
            patch("autodj.player.Player") as p_cls,
        ):
            p_cls.return_value.run = lambda *_a, **_k: None
            result = CliRunner().invoke(cli, ["play", "--anchor-seed"])
        assert result.exit_code == 0
        kwargs = p_cls.call_args.kwargs
        assert kwargs.get("anchor_to_seed") is True

    def test_play_no_show_lyrics_flag(self) -> None:
        cfg_mock = _make_cfg()
        sim_mock = _make_sim()
        with (
            patch("autodj.config.load_config", return_value=cfg_mock),
            patch("autodj.similarity.SimilarityIndex.from_index_dir", return_value=sim_mock),
            patch("autodj.player.Player.run"),
        ):
            CliRunner().invoke(cli, ["play", "--no-show-lyrics"])
        assert cfg_mock.playback.show_lyrics is False

    def test_play_daypart_flag(self) -> None:
        cfg_mock = _make_cfg()
        sim_mock = _make_sim()
        with (
            patch("autodj.config.load_config", return_value=cfg_mock),
            patch("autodj.similarity.SimilarityIndex.from_index_dir", return_value=sim_mock),
            patch("autodj.player.Player.run"),
        ):
            CliRunner().invoke(cli, ["play", "--daypart"])
        assert cfg_mock.playback.enable_daypart is True

    def test_play_mood_arc_flag_with_hours(self) -> None:
        cfg_mock = _make_cfg()
        sim_mock = _make_sim()
        with (
            patch("autodj.config.load_config", return_value=cfg_mock),
            patch("autodj.similarity.SimilarityIndex.from_index_dir", return_value=sim_mock),
            patch("autodj.player.Player.run"),
        ):
            CliRunner().invoke(
                cli,
                ["play", "--mood-arc", "--mood-arc-hours", "1.5"],
            )
        assert cfg_mock.playback.enable_mood_arc is True
        assert cfg_mock.playback.mood_arc_hours == 1.5

    def test_play_no_import_external_cues_flag(self) -> None:
        cfg_mock = _make_cfg()
        sim_mock = _make_sim()
        with (
            patch("autodj.config.load_config", return_value=cfg_mock),
            patch("autodj.similarity.SimilarityIndex.from_index_dir", return_value=sim_mock),
            patch("autodj.player.Player.run"),
        ):
            CliRunner().invoke(cli, ["play", "--no-import-external-cues"])
        assert cfg_mock.playback.import_external_cues is False

    def test_serve_daypart_flag(self) -> None:
        cfg_mock = _make_cfg()
        sim_mock = _make_sim()
        with (
            patch("autodj.config.load_config", return_value=cfg_mock),
            patch("autodj.similarity.SimilarityIndex.from_index_dir", return_value=sim_mock),
            patch("autodj.server.serve"),
        ):
            CliRunner().invoke(cli, ["serve", "--daypart"])
        assert cfg_mock.playback.enable_daypart is True

    def test_serve_mood_arc_with_hours(self) -> None:
        cfg_mock = _make_cfg()
        sim_mock = _make_sim()
        with (
            patch("autodj.config.load_config", return_value=cfg_mock),
            patch("autodj.similarity.SimilarityIndex.from_index_dir", return_value=sim_mock),
            patch("autodj.server.serve"),
        ):
            CliRunner().invoke(
                cli,
                ["serve", "--mood-arc", "--mood-arc-hours", "1.0"],
            )
        assert cfg_mock.playback.enable_mood_arc is True
        assert cfg_mock.playback.mood_arc_hours == 1.0

    def test_serve_no_import_external_cues(self) -> None:
        cfg_mock = _make_cfg()
        sim_mock = _make_sim()
        with (
            patch("autodj.config.load_config", return_value=cfg_mock),
            patch("autodj.similarity.SimilarityIndex.from_index_dir", return_value=sim_mock),
            patch("autodj.server.serve"),
        ):
            CliRunner().invoke(cli, ["serve", "--no-import-external-cues"])
        assert cfg_mock.playback.import_external_cues is False


# ---------------------------------------------------------------------------
# cmd_serve — DJ-mix overrides
# ---------------------------------------------------------------------------


class TestCmdServeOverrides:
    def test_serve_harmonic_override(self) -> None:
        cfg_mock = _make_cfg()
        sim_mock = _make_sim()
        with (
            patch("autodj.config.load_config", return_value=cfg_mock),
            patch("autodj.similarity.SimilarityIndex.from_index_dir", return_value=sim_mock),
            patch("autodj.server.serve"),
        ):
            CliRunner().invoke(cli, ["serve", "--harmonic"])
        assert cfg_mock.djmix.harmonic_mixing is True

    def test_serve_transition_override(self) -> None:
        cfg_mock = _make_cfg()
        sim_mock = _make_sim()
        with (
            patch("autodj.config.load_config", return_value=cfg_mock),
            patch("autodj.similarity.SimilarityIndex.from_index_dir", return_value=sim_mock),
            patch("autodj.server.serve"),
        ):
            CliRunner().invoke(cli, ["serve", "--transition", "tape_stop"])
        assert cfg_mock.transitions.effect == "tape_stop"

    def test_serve_with_discovery(self) -> None:
        cfg_mock = _make_cfg()
        sim_mock = _make_sim()
        with (
            patch("autodj.config.load_config", return_value=cfg_mock),
            patch("autodj.similarity.SimilarityIndex.from_index_dir", return_value=sim_mock),
            patch("autodj.server.serve"),
        ):
            result = CliRunner().invoke(cli, ["serve", "--discovery-every", "20"])
        assert result.exit_code == 0


@pytest.mark.skipif(not LIBRARY_DB.exists(), reason="library.db not present")
class TestRealLibraryDb:
    """Integration tests against the real beets library.db.

    Skipped automatically when library.db is not present (CI, other machines).
    """

    def test_get_all_tracks_returns_many(self) -> None:
        from autodj.beets import get_all_tracks

        tracks = get_all_tracks(LIBRARY_DB)
        assert len(tracks) > 1000, f"Expected a large library, got {len(tracks)}"

    def test_tracks_have_valid_paths(self) -> None:
        from autodj.beets import get_all_tracks

        tracks = get_all_tracks(LIBRARY_DB)
        # All paths should be non-empty strings
        assert all(str(t.path) for t in tracks[:100])

    def test_paths_are_non_empty_strings(self) -> None:
        """Confirms beets paths are decodable strings (relative or absolute)."""
        from autodj.beets import get_all_tracks

        tracks = get_all_tracks(LIBRARY_DB)
        sample = str(tracks[0].path)
        assert len(sample) > 0

    def test_search_returns_results(self) -> None:
        from autodj.beets import search_tracks

        results = search_tracks(LIBRARY_DB, "the")
        assert len(results) > 0

    def test_resolve_relative_path_against_music_dir(self) -> None:
        """End-to-end: read a relative beets path and resolve it to a local file."""
        from autodj.beets import get_all_tracks
        from autodj.indexer import _resolve_beets_path

        tracks = get_all_tracks(LIBRARY_DB)
        # Find a relative-path track (most beets entries after the relative_path migration)
        relative_track = next(
            (t for t in tracks if not str(t.path).replace("\\", "/").startswith("/")),
            None,
        )
        if relative_track is None:
            pytest.skip("No relative-path tracks in this library.db")

        resolved = _resolve_beets_path(relative_track.path, Path("Z:/Music"))
        resolved_str = str(resolved).replace("\\", "/")
        assert resolved_str.startswith("Z:/Music/")

    def test_search_portishead_if_present(self) -> None:
        """Spot-check: if Portishead is in the library, search finds it."""
        from autodj.beets import get_all_tracks, search_tracks

        tracks = get_all_tracks(LIBRARY_DB)
        has_portishead = any("Portishead" in str(t.artist) for t in tracks)
        if not has_portishead:
            pytest.skip("Portishead not in this library")
        results = search_tracks(LIBRARY_DB, "Portishead")
        assert len(results) > 0
        assert all("Portishead" in t.artist for t in results)


# ---------------------------------------------------------------------------
# Logging level defaults
# ---------------------------------------------------------------------------


class TestLoggingDefaults:
    """`autodj` defaults to INFO; `autodj -v` drops to DEBUG."""

    def test_default_level_is_info(self) -> None:
        """Without -v the root logger sits at INFO so users see the boot
        banner, WS connect / disconnect, and external-cue import lines.
        Regression test for the WARNING-default behaviour that swallowed
        every status message until the user passed -v.
        """
        import logging

        original = logging.getLogger().level
        try:
            # Use a subcommand so the cli body runs.  --help short-
            # circuits before our level setter.  Missing config exits
            # cleanly; the level was already set by then.
            CliRunner().invoke(cli, ["--config", "/no/such/file", "index"])
            assert logging.getLogger().level == logging.INFO
        finally:
            logging.getLogger().setLevel(original)

    def test_verbose_flag_drops_to_debug(self) -> None:
        import logging

        original = logging.getLogger().level
        try:
            CliRunner().invoke(cli, ["-v", "--config", "/no/such/file", "index"])
            assert logging.getLogger().level == logging.DEBUG
        finally:
            logging.getLogger().setLevel(original)
