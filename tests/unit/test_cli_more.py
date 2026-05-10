"""Additional CLI unit tests covering small uncovered branches."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

from click.testing import CliRunner

from autodj.cli import _coerce_audio_device, _resolve_seed, cli
from autodj.indexer import IndexEntry


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


def _cfg() -> MagicMock:
    cfg = MagicMock()
    cfg.library.beets_db = None
    cfg.library.music_dir = Path("Z:/Music")
    cfg.library.path_remap = []
    cfg.playback.no_repeat_window = 50
    cfg.playback.artist_repeat_window = 3
    cfg.playback.crossfade_seconds = 3.0
    cfg.playback.history_file = None
    cfg.playback.discovery_every = None
    cfg.presets = {}
    return cfg


# ---------------------------------------------------------------------------
# _coerce_audio_device
# ---------------------------------------------------------------------------


class TestCoerceAudioDevice:
    def test_none(self) -> None:
        assert _coerce_audio_device(None) is None

    def test_int(self) -> None:
        assert _coerce_audio_device("3") == 3

    def test_str(self) -> None:
        assert _coerce_audio_device("USB") == "USB"


# ---------------------------------------------------------------------------
# _resolve_seed interactive prompt path
# ---------------------------------------------------------------------------


class TestResolveSeedInteractive:
    def test_prompt_choice_picks_match(self) -> None:
        sim = MagicMock()
        sim.entries = [_entry(0), _entry(1), _entry(2)]
        # Simulate `click.prompt` returning the second choice.
        with patch("autodj.cli.click.prompt", return_value=2):
            chosen = _resolve_seed(sim, _cfg(), "Song", MagicMock(), interactive=True)
        assert chosen is not None
        # second entry chosen
        assert chosen.path == sim.entries[1].path

    def test_prompt_eof_aborts_to_none(self) -> None:
        sim = MagicMock()
        sim.entries = [_entry(0), _entry(1)]
        with patch("autodj.cli.click.prompt", side_effect=EOFError()):
            chosen = _resolve_seed(sim, _cfg(), "Song", MagicMock(), interactive=True)
        assert chosen is None


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
            patch("autodj.indexer.load_index", return_value=([_entry(0)], None)),
        ):
            result = CliRunner().invoke(cli, ["--config", str(cfg), "stats", "--name", "workout"])
        assert result.exit_code == 0


# ---------------------------------------------------------------------------
# index command — config error path + --reindex-modified-since
# ---------------------------------------------------------------------------


class TestIndexCommand:
    def test_index_with_reindex_modified_since_invalid(self, tmp_path: Path) -> None:
        cfg = _write_min_cfg(tmp_path)
        # Pretend deps are present + cfg loads, but build_index gets to the date parse.
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
                    "--reindex-modified-since",
                    "garbage-date",
                ],
            )
        # ValueError on date parse -> sys.exit(1)
        assert result.exit_code == 1
        assert "Bad --reindex-modified-since" in result.output

    def test_index_with_reindex_modified_since_valid(self, tmp_path: Path) -> None:
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
                    "--reindex-modified-since",
                    "2024-01-01",
                ],
            )
        assert result.exit_code == 0

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
        with (
            patch("autodj.cli._can_import", return_value=True),
            patch("autodj.cli._load_cfg_or_exit", return_value=cfg_mock),
            patch("autodj.model.download_model_if_needed", return_value=tmp_path / "m"),
            patch("autodj.model.load_model", return_value=MagicMock()),
            patch("autodj.indexer.build_index"),
        ):
            result = CliRunner().invoke(cli, ["--config", str(cfg), "index", "--enrich"])
        assert result.exit_code == 0
        assert "skipped" in result.output

    def test_index_enrich_success_branch(self, tmp_path: Path) -> None:
        cfg = _write_min_cfg(tmp_path)
        cfg_mock = _cfg()
        cfg_mock.library.beets_db = tmp_path / "library.db"
        with (
            patch("autodj.cli._can_import", return_value=True),
            patch("autodj.cli._load_cfg_or_exit", return_value=cfg_mock),
            patch("autodj.model.download_model_if_needed", return_value=tmp_path / "m"),
            patch("autodj.model.load_model", return_value=MagicMock()),
            patch("autodj.indexer.build_index"),
            patch("autodj.indexer.enrich_from_beets", return_value=(3, 10)),
        ):
            result = CliRunner().invoke(cli, ["--config", str(cfg), "index", "--enrich"])
        assert result.exit_code == 0
        assert "Enrich" in result.output

    def test_index_enrich_failure_does_not_abort(self, tmp_path: Path) -> None:
        cfg = _write_min_cfg(tmp_path)
        cfg_mock = _cfg()
        cfg_mock.library.beets_db = tmp_path / "library.db"
        with (
            patch("autodj.cli._can_import", return_value=True),
            patch("autodj.cli._load_cfg_or_exit", return_value=cfg_mock),
            patch("autodj.model.download_model_if_needed", return_value=tmp_path / "m"),
            patch("autodj.model.load_model", return_value=MagicMock()),
            patch("autodj.indexer.build_index"),
            patch("autodj.indexer.enrich_from_beets", side_effect=RuntimeError("nope")),
        ):
            result = CliRunner().invoke(cli, ["--config", str(cfg), "index", "--enrich"])
        # Enrich error should not propagate -- index exits 0
        assert result.exit_code == 0
        assert "Enrich failed" in result.output

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
            result = CliRunner().invoke(cli, ["--config", str(cfg), "index", "--analyse"])
        assert result.exit_code == 0
        assert "skipped" in result.output

    def test_index_analyse_runs_backfill(self, tmp_path: Path) -> None:
        cfg = _write_min_cfg(tmp_path)
        cfg_mock = _cfg()
        # Build a real metadata.json so the analyse branch runs
        active = tmp_path / "idx"
        active.mkdir()
        cfg_mock.index.active_dir = active
        from dataclasses import asdict

        meta_payload = [asdict(_entry(0))]
        import json as _json

        (active / "metadata.json").write_text(_json.dumps(meta_payload), encoding="utf-8")

        with (
            patch("autodj.cli._can_import", return_value=True),
            patch("autodj.cli._load_cfg_or_exit", return_value=cfg_mock),
            patch("autodj.model.download_model_if_needed", return_value=tmp_path / "m"),
            patch("autodj.model.load_model", return_value=MagicMock()),
            patch("autodj.indexer.build_index"),
            patch("autodj.indexer._backfill_dj_meta") as bf,
        ):
            result = CliRunner().invoke(cli, ["--config", str(cfg), "index", "--analyse"])
        assert result.exit_code == 0
        bf.assert_called_once()

    def test_index_analyse_failure_does_not_abort(self, tmp_path: Path) -> None:
        cfg = _write_min_cfg(tmp_path)
        cfg_mock = _cfg()
        active = tmp_path / "idx"
        active.mkdir()
        cfg_mock.index.active_dir = active
        from dataclasses import asdict

        meta_payload = [asdict(_entry(0))]
        import json as _json

        (active / "metadata.json").write_text(_json.dumps(meta_payload), encoding="utf-8")
        with (
            patch("autodj.cli._can_import", return_value=True),
            patch("autodj.cli._load_cfg_or_exit", return_value=cfg_mock),
            patch("autodj.model.download_model_if_needed", return_value=tmp_path / "m"),
            patch("autodj.model.load_model", return_value=MagicMock()),
            patch("autodj.indexer.build_index"),
            patch("autodj.indexer._backfill_dj_meta", side_effect=RuntimeError("x")),
        ):
            result = CliRunner().invoke(cli, ["--config", str(cfg), "index", "--analyse"])
        assert result.exit_code == 0
        assert "Analyse failed" in result.output

    def test_index_missing_deps_exits_one(self, tmp_path: Path) -> None:
        cfg = _write_min_cfg(tmp_path)
        with patch("autodj.cli._can_import", return_value=False):
            result = CliRunner().invoke(cli, ["--config", str(cfg), "index"])
        assert result.exit_code == 1
        assert "missing packages" in result.output


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
        sd_mock.default.device = "not iterable"
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


# ---------------------------------------------------------------------------
# playlist random seed path (no seed found)
# ---------------------------------------------------------------------------


class TestPlaylistRandomSeed:
    def test_playlist_with_no_seed_picks_random(self) -> None:
        cfg_mock = _cfg()
        sim_mock = MagicMock()
        sim_mock.entries = [_entry(i) for i in range(5)]
        sim_mock.find_next_for_path.return_value = sim_mock.entries[0]

        with (
            patch("autodj.config.load_config", return_value=cfg_mock),
            patch("autodj.similarity.SimilarityIndex.from_index_dir", return_value=sim_mock),
        ):
            # No --seed -> _resolve_seed returns None -> random.choice path (line 1735+)
            result = CliRunner().invoke(cli, ["playlist", "--tracks", "2"])
        assert result.exit_code == 0
