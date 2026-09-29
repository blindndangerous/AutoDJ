"""Additional CLI unit tests covering small uncovered branches."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import click
import numpy as np
from click.testing import CliRunner

from autodj.backup import BackupError
from autodj.cli import _can_import, cli
from autodj.indexer import FEATURE_DIM, IndexEntry, save_index


def _publish_one_entry(index_dir: Path) -> None:
    entry = _entry(0)
    entry.path = "song_0.flac"
    save_index([entry], np.zeros((1, FEATURE_DIM), dtype=np.float32), index_dir, base_generation=0)


def _entry(i: int = 0) -> IndexEntry:
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


def _configure_sim_api(sim: MagicMock) -> MagicMock:
    sim.entries_snapshot.side_effect = lambda: tuple(sim.entries)
    sim.entry_for_path.side_effect = lambda path: next(
        (entry for entry in sim.entries if entry.path == path),
        None,
    )
    sim.ntotal = len(sim.entries)
    return sim


def _cfg() -> MagicMock:
    cfg = MagicMock()
    cfg.library.beets_db = None
    cfg.library.music_dir = Path("Z:/Music")
    cfg.playback.no_repeat_window = 50
    cfg.playback.artist_repeat_window = 3
    cfg.playback.crossfade_seconds = 3.0
    cfg.playback.discovery_every = None
    cfg.playback.pick_top_k = 1
    cfg.playback.pick_temperature = 0.3
    cfg.presets = {}
    return cfg


# ---------------------------------------------------------------------------
# stats --name validation branch (lines 1907-1914)
# ---------------------------------------------------------------------------


def _write_min_cfg(tmp_path: Path) -> Path:
    cfg = tmp_path / "config.toml"
    cfg.write_text(
        '[library]\nmusic_dir = "Z:/Music"\n'
        '[index]\nindex_dir = "Z:/idx"\nmodel_dir = "models"\n'
        "[playback]\ncrossfade_seconds = 3.0\nno_repeat_window = 50\n"
        '[model]\nname = "OpenMuQ/MuQ-large-msd-iter"\n',
        encoding="utf-8",
    )
    return cfg


class TestStatsNameFlag:
    def test_stats_invalid_name_exits(self, tmp_path: Path) -> None:
        cfg = _write_min_cfg(tmp_path)
        result = CliRunner().invoke(cli, ["--config", str(cfg), "stats", "--name", "../bad"])
        assert result.exit_code == 1

    def test_stats_valid_name_applied(self, tmp_path: Path) -> None:
        cfg = _write_min_cfg(tmp_path)
        cfg_mock = _cfg()
        with (
            patch("autodj.config.load_config", return_value=cfg_mock),
            patch("autodj.indexer.load_index", return_value=([_entry(0)], None, None)),
        ):
            result = CliRunner().invoke(cli, ["--config", str(cfg), "stats", "--name", "workout"])
        assert result.exit_code == 0


# ---------------------------------------------------------------------------
# index command — config error path
# ---------------------------------------------------------------------------


class TestIndexCommand:
    def test_import_probe_returns_false_for_missing_module(self) -> None:
        assert not _can_import("autodj_module_that_does_not_exist")

    def test_index_build_failure_exits_one(self, tmp_path: Path) -> None:
        cfg = _write_min_cfg(tmp_path)
        with (
            patch("autodj.cli._can_import", return_value=True),
            patch("autodj.cli._load_cfg_or_exit", return_value=_cfg()),
            patch("autodj.model.download_model_if_needed", return_value=tmp_path / "m"),
            patch("autodj.model.load_model", return_value=MagicMock()),
            patch("autodj.indexer.build_index", side_effect=RuntimeError("explode")),
        ):
            result = CliRunner().invoke(cli, ["--config", str(cfg), "index"])
        assert result.exit_code == 1
        assert "Indexing failed" in result.output

    def test_index_enrich_skipped_without_beets(self, tmp_path: Path) -> None:
        cfg = _write_min_cfg(tmp_path)
        cfg_mock = _cfg()
        cfg_mock.library.beets_db = None
        cfg_mock.index.active_dir = tmp_path / "noindex"
        with (
            patch("autodj.cli._can_import", return_value=True),
            patch("autodj.cli._load_cfg_or_exit", return_value=cfg_mock),
            patch("autodj.model.download_model_if_needed", return_value=tmp_path / "m"),
            patch("autodj.model.load_model", return_value=MagicMock()),
            patch("autodj.indexer.build_index"),
        ):
            result = CliRunner().invoke(cli, ["--config", str(cfg), "index"])
        assert result.exit_code == 0
        assert "skipped" in result.output

    def test_index_enrich_success_branch(self, tmp_path: Path) -> None:
        cfg = _write_min_cfg(tmp_path)
        cfg_mock = _cfg()
        cfg_mock.library.beets_db = tmp_path / "library.db"
        cfg_mock.index.active_dir = tmp_path / "noindex"
        with (
            patch("autodj.cli._can_import", return_value=True),
            patch("autodj.cli._load_cfg_or_exit", return_value=cfg_mock),
            patch("autodj.model.download_model_if_needed", return_value=tmp_path / "m"),
            patch("autodj.model.load_model", return_value=MagicMock()),
            patch("autodj.indexer.build_index"),
            patch("autodj.indexer.enrich_from_beets", return_value=(3, 10)),
        ):
            result = CliRunner().invoke(cli, ["--config", str(cfg), "index"])
        assert result.exit_code == 0
        assert "Enrich" in result.output

    def test_index_enrich_failure_does_not_abort(self, tmp_path: Path) -> None:
        cfg = _write_min_cfg(tmp_path)
        cfg_mock = _cfg()
        cfg_mock.library.beets_db = tmp_path / "library.db"
        cfg_mock.index.active_dir = tmp_path / "noindex"
        with (
            patch("autodj.cli._can_import", return_value=True),
            patch("autodj.cli._load_cfg_or_exit", return_value=cfg_mock),
            patch("autodj.model.download_model_if_needed", return_value=tmp_path / "m"),
            patch("autodj.model.load_model", return_value=MagicMock()),
            patch("autodj.indexer.build_index"),
            patch("autodj.indexer.enrich_from_beets", side_effect=RuntimeError("nope")),
        ):
            result = CliRunner().invoke(cli, ["--config", str(cfg), "index"])
        # Enrich error should not propagate -- index exits 0
        assert result.exit_code == 0
        assert "Enrich failed" in result.output

    def test_index_runs_post_passes_by_default(self, tmp_path: Path) -> None:
        cfg = _write_min_cfg(tmp_path)
        cfg_mock = _cfg()
        cfg_mock.library.beets_db = tmp_path / "library.db"
        active = tmp_path / "idx"
        active.mkdir()
        cfg_mock.index.active_dir = active
        _publish_one_entry(active)

        with (
            patch("autodj.cli._can_import", return_value=True),
            patch("autodj.cli._load_cfg_or_exit", return_value=cfg_mock),
            patch("autodj.model.download_model_if_needed", return_value=tmp_path / "m"),
            patch("autodj.model.load_model", return_value=MagicMock()),
            patch("autodj.indexer.build_index"),
            patch("autodj.indexer.enrich_from_beets", return_value=(1, 1)) as enrich,
            patch("autodj.indexer.backfill_dj_meta") as backfill,
        ):
            result = CliRunner().invoke(cli, ["--config", str(cfg), "index"])

        assert result.exit_code == 0
        enrich.assert_called_once()
        backfill.assert_called_once()

    def test_index_analyse_no_metadata_skipped(self, tmp_path: Path) -> None:
        cfg = _write_min_cfg(tmp_path)
        cfg_mock = _cfg()
        cfg_mock.index.active_dir = tmp_path / "noindex"  # doesn't exist
        with (
            patch("autodj.cli._can_import", return_value=True),
            patch("autodj.cli._load_cfg_or_exit", return_value=cfg_mock),
            patch("autodj.model.download_model_if_needed", return_value=tmp_path / "m"),
            patch("autodj.model.load_model", return_value=MagicMock()),
            patch("autodj.indexer.build_index"),
        ):
            result = CliRunner().invoke(cli, ["--config", str(cfg), "index"])
        assert result.exit_code == 0
        assert "skipped" in result.output

    def test_index_analyse_runs_backfill(self, tmp_path: Path) -> None:
        cfg = _write_min_cfg(tmp_path)
        cfg_mock = _cfg()
        # Build a real tracks.db so the analyse branch runs
        active = tmp_path / "idx"
        active.mkdir()
        cfg_mock.index.active_dir = active
        _publish_one_entry(active)

        with (
            patch("autodj.cli._can_import", return_value=True),
            patch("autodj.cli._load_cfg_or_exit", return_value=cfg_mock),
            patch("autodj.model.download_model_if_needed", return_value=tmp_path / "m"),
            patch("autodj.model.load_model", return_value=MagicMock()),
            patch("autodj.indexer.build_index"),
            patch("autodj.indexer.backfill_dj_meta") as bf,
        ):
            result = CliRunner().invoke(cli, ["--config", str(cfg), "index"])
        assert result.exit_code == 0
        bf.assert_called_once()

    def test_index_analyse_failure_does_not_abort(self, tmp_path: Path) -> None:
        cfg = _write_min_cfg(tmp_path)
        cfg_mock = _cfg()
        active = tmp_path / "idx"
        active.mkdir()
        cfg_mock.index.active_dir = active
        _publish_one_entry(active)
        with (
            patch("autodj.cli._can_import", return_value=True),
            patch("autodj.cli._load_cfg_or_exit", return_value=cfg_mock),
            patch("autodj.model.download_model_if_needed", return_value=tmp_path / "m"),
            patch("autodj.model.load_model", return_value=MagicMock()),
            patch("autodj.indexer.build_index"),
            patch("autodj.indexer.backfill_dj_meta", side_effect=RuntimeError("x")),
        ):
            result = CliRunner().invoke(cli, ["--config", str(cfg), "index"])
        assert result.exit_code == 0
        assert "Analyse failed" in result.output

    def test_index_missing_deps_exits_one(self, tmp_path: Path) -> None:
        cfg = _write_min_cfg(tmp_path)
        with patch("autodj.cli._can_import", return_value=False):
            result = CliRunner().invoke(cli, ["--config", str(cfg), "index"])
        assert result.exit_code == 1
        assert "missing packages" in result.output

    def test_index_force_without_post_passes_reports_mode(self, tmp_path: Path) -> None:
        cfg = _write_min_cfg(tmp_path)
        with (
            patch("autodj.cli._can_import", return_value=True),
            patch("autodj.cli._load_cfg_or_exit", return_value=_cfg()),
            patch("autodj.model.download_model_if_needed", return_value=tmp_path / "m"),
            patch("autodj.model.load_model", return_value=MagicMock()),
            patch("autodj.indexer.build_index"),
        ):
            result = CliRunner().invoke(
                cli,
                [
                    "--config",
                    str(cfg),
                    "index",
                    "--force",
                    "--no-enrich",
                    "--no-analyse",
                ],
            )

        assert result.exit_code == 0
        assert "FORCE REBUILD" in result.output
        assert "Post-pass" in result.output
        assert "skipped" in result.output


class TestDoctorAndBackupErrors:
    def test_doctor_propagates_config_exit_in_text_mode(self) -> None:
        with patch("autodj.cli._load_cfg_or_exit", side_effect=click.exceptions.Exit(1)):
            result = CliRunner().invoke(cli, ["doctor"])

        assert result.exit_code == 1

    def test_backup_error_becomes_click_error(self, tmp_path: Path) -> None:
        with (
            patch("autodj.cli._load_cfg_or_exit", return_value=_cfg()),
            patch(
                "autodj.backup.create_backup",
                side_effect=BackupError("snapshot unavailable"),
            ),
        ):
            result = CliRunner().invoke(cli, ["backup", str(tmp_path / "backup.zip")])

        assert result.exit_code == 1
        assert "snapshot unavailable" in result.output

    def test_restore_error_becomes_click_error(self, tmp_path: Path) -> None:
        archive = tmp_path / "backup.zip"
        archive.touch()
        with (
            patch("autodj.cli._load_cfg_or_exit", return_value=_cfg()),
            patch(
                "autodj.backup.restore_backup",
                side_effect=BackupError("archive invalid"),
            ),
        ):
            result = CliRunner().invoke(cli, ["restore", str(archive)])

        assert result.exit_code == 1
        assert "archive invalid" in result.output


# ---------------------------------------------------------------------------
# list-devices
# ---------------------------------------------------------------------------


class TestListDevices:
    def test_no_sounddevice_exits(self) -> None:
        # Force ImportError inside the command
        import builtins

        real_import = builtins.__import__

        def fake_import(name, *args, **kwargs):
            if name == "sounddevice":
                raise ImportError("nope")
            return real_import(name, *args, **kwargs)

        with patch.object(builtins, "__import__", fake_import):
            result = CliRunner().invoke(cli, ["list-devices"])
        assert result.exit_code == 1

    def test_no_output_devices(self) -> None:
        sd_mock = MagicMock()
        sd_mock.default.device = (0, 0)
        sd_mock.query_devices.return_value = [
            {"name": "Mic", "max_output_channels": 0, "default_samplerate": 44100},
        ]
        with patch.dict("sys.modules", {"sounddevice": sd_mock}):
            result = CliRunner().invoke(cli, ["list-devices"])
        assert result.exit_code == 0
        assert "No output devices found" in result.output

    def test_lists_output_devices(self) -> None:
        sd_mock = MagicMock()
        sd_mock.default.device = (0, 1)
        sd_mock.query_devices.return_value = [
            {"name": "Speakers", "max_output_channels": 2, "default_samplerate": 48000},
            {"name": "USB DAC", "max_output_channels": 2, "default_samplerate": 96000},
        ]
        with patch.dict("sys.modules", {"sounddevice": sd_mock}):
            result = CliRunner().invoke(cli, ["list-devices"])
        assert result.exit_code == 0
        assert "Speakers" in result.output

    def test_default_device_attribute_error(self) -> None:
        # sd.default.device raises AttributeError
        sd_mock = MagicMock()

        class BrokenDefault:
            @property
            def device(self) -> object:
                raise AttributeError("device unavailable")

        sd_mock.default = BrokenDefault()
        sd_mock.query_devices.return_value = [
            {"name": "Speakers", "max_output_channels": 2, "default_samplerate": 48000},
        ]
        with patch.dict("sys.modules", {"sounddevice": sd_mock}):
            result = CliRunner().invoke(cli, ["list-devices"])
        assert result.exit_code == 0


# ---------------------------------------------------------------------------
# prune --name validation already covered.  Add enrich --name valid path
# ---------------------------------------------------------------------------


class TestEnrichValidName:
    def test_enrich_valid_name_runs(self, tmp_path: Path) -> None:
        cfg = _write_min_cfg(tmp_path)
        cfg_mock = _cfg()
        cfg_mock.library.beets_db = tmp_path / "library.db"
        with (
            patch("autodj.config.load_config", return_value=cfg_mock),
            patch("autodj.indexer.enrich_from_beets", return_value=(2, 5)),
        ):
            result = CliRunner().invoke(cli, ["--config", str(cfg), "enrich", "--name", "workout"])
        assert result.exit_code == 0
