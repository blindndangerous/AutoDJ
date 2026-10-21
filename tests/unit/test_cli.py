"""Unit tests for autodj.cli.

Tests the pure helper functions (_resolve_seed and others) and the
Click commands' error-path behaviour using CliRunner — no real audio, model,
or index required.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import click
import pytest
from click.testing import CliRunner

from autodj.cli import (
    _resolve_seed,
    cli,
)
from autodj.config import ENVIRONMENT_OVERLAY, ServerConfig, StationConfig, StreamConfig
from autodj.indexer import IndexEntry
from autodj.repeat_settings import RepeatSettings
from tests._layout import point_index

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def clear_autodj_environment(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.chdir(tmp_path)
    for variable in ENVIRONMENT_OVERLAY:
        monkeypatch.delenv(variable, raising=False)


@pytest.fixture(autouse=True)
def mock_library_startup():
    # CLI option tests use fake configs; catalog startup has separate integration tests.
    def create(cfg, sim):
        library = MagicMock()
        library.entries_snapshot.side_effect = sim.entries_snapshot
        library.snapshot.return_value = {
            "tracks_ready": sim.ntotal,
            "tracks_indexed": sim.ntotal,
            "import": {"errors": []},
        }
        return library

    from autodj.config import move_station_tables

    def migrate(cfg):
        return False if isinstance(cfg, MagicMock) else move_station_tables(cfg)

    with (
        patch("autodj.library.LibraryManager", side_effect=create),
        patch("autodj.config.move_station_tables", side_effect=migrate),
    ):
        yield


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


def _configure_sim_api(sim: MagicMock) -> MagicMock:
    sim.entries_snapshot.side_effect = lambda: tuple(sim.entries)
    sim.entry_for_path.side_effect = lambda path: next(
        (entry for entry in sim.entries if entry.path == path),
        None,
    )
    sim.ntotal = len(sim.entries)
    return sim


def _make_sim(n: int = 5) -> MagicMock:
    sim = MagicMock()
    entries = [_make_entry(i) for i in range(n)]
    sim.entries = entries
    _configure_sim_api(sim)
    sim.ntotal = n
    # Return a real IndexEntry so callers that serialize it do not crash
    sim.find_next_for_path.return_value = entries[0]
    return sim


@pytest.fixture(autouse=True)
def _no_serve_lock(request):
    """Serve tests use mock configurations: no real index folder to lock (#70)."""
    if request.cls is not None and request.cls.__name__ == "TestClaimIndexForServe":
        yield
        return
    with patch("autodj.cli._claim_index_for_serve"):
        yield


def _make_cfg(beets_db=None) -> MagicMock:
    cfg = MagicMock()
    cfg.library.beets_db = beets_db
    cfg.playback.repeat_rules = RepeatSettings()
    cfg.playback.crossfade_seconds = 3.0
    cfg.playback.discovery_every = None
    cfg.presets = {}
    cfg.index.name = "default"
    cfg.server = ServerConfig()
    cfg.stream = StreamConfig()
    cfg.station = StationConfig()
    cfg.default_station = None
    cfg.stations = ()
    cfg.config_sources = ("defaults",)
    return cfg


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
        result = _resolve_seed(sim, _make_cfg(), "Song 0", MagicMock())
        assert result is not None
        assert result.path == sim.entries[0].path

    def test_partial_title_match(self) -> None:
        sim = _make_sim()
        result = _resolve_seed(sim, _make_cfg(), "ong 0", MagicMock())
        assert result is not None

    def test_artist_match(self) -> None:
        sim = _make_sim()
        result = _resolve_seed(sim, _make_cfg(), "Artist 0", MagicMock())
        assert result is not None

    def test_multiple_matches_takes_first(self) -> None:
        """With multiple hits, the first match is chosen."""
        sim = _make_sim(5)
        # "Song" matches all 5 entries
        result = _resolve_seed(sim, _make_cfg(), "Song", MagicMock())
        assert result is not None
        assert result.path == sim.entries[0].path

    def test_case_insensitive_match(self) -> None:
        sim = _make_sim()
        result = _resolve_seed(sim, _make_cfg(), "song 0", MagicMock())
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

    def test_stats_exits_on_missing_config(self, tmp_path: Path) -> None:
        result = CliRunner().invoke(cli, ["--config", self._missing(tmp_path), "stats"])
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

    def test_enrich_no_beets_db_exits(self, tmp_path: Path) -> None:
        cfg = tmp_path / "c.toml"
        # Note: no beets_db key in [library] section.
        cfg.write_text(
            '[library]\nmusic_dir = "Z:/Music"\n'
            f'[index]\nindex_dir = "{(tmp_path / "index").as_posix()}"\nmodel_dir = "models"\n'
            "[playback]\ncrossfade_seconds = 3.0\nrepeat_rules = { track = { tracks = 50 } }\n"
            '[model]\nname = "OpenMuQ/MuQ-large-msd-iter"\n',
            encoding="utf-8",
        )
        result = CliRunner().invoke(cli, ["--config", str(cfg), "enrich"])
        assert result.exit_code == 1
        assert "beets" in result.output.lower()


class TestCliConfigSelection:
    @pytest.mark.parametrize("command", ["serve", "prune", "enrich", "stats"])
    def test_malformed_section_type_is_reported_as_invalid_config(
        self, tmp_path: Path, command: str
    ) -> None:
        config_path = tmp_path / "config.toml"
        config_path.write_text('[server]\nport = "not-an-integer"\n', encoding="utf-8")

        result = CliRunner().invoke(cli, ["--config", str(config_path), command])

        assert result.exit_code == 1
        assert "Config not found or invalid" in result.output

    @pytest.mark.parametrize(
        "section",
        [
            "library",
            "index",
            "playback",
            "model",
            "huggingface",
            "replaygain",
            "djmix",
            "transitions",
            "server",
        ],
    )
    def test_non_table_section_is_reported_as_invalid_config(
        self, tmp_path: Path, section: str
    ) -> None:
        config_path = tmp_path / "config.toml"
        config_path.write_text(f"{section} = []\n", encoding="utf-8")

        result = CliRunner().invoke(cli, ["--config", str(config_path), "serve"])

        assert result.exit_code == 1
        assert "Config not found or invalid" in result.output
        assert f"{section} section must be a table" in result.output

    def test_unknown_key_warns_and_continues_to_index_check(self, tmp_path: Path) -> None:
        config_path = tmp_path / "config.toml"
        config_path.write_text("[playback]\nbogus_key = 1\n", encoding="utf-8")

        result = CliRunner().invoke(cli, ["--config", str(config_path), "stats"])

        assert result.exit_code == 1
        # Rich markup used to swallow "[playback]" as a style tag.
        assert "Ignoring unknown config keys in [playback]: ['bogus_key']" in result.output
        assert "Current library not found" in result.output
        assert "Config not found or invalid" not in result.output

    def test_omitted_config_passes_none_to_loader(self) -> None:
        cfg = _make_cfg()
        with (
            patch("autodj.config.load_config", return_value=cfg) as loader,
            patch("autodj.cli._load_index_for_serve", return_value=_make_sim()),
            patch("autodj.server.serve"),
        ):
            result = CliRunner().invoke(cli, ["serve"])
        assert result.exit_code == 0
        loader.assert_called_once_with(None)

    def test_explicit_missing_config_exits_one(self, tmp_path: Path) -> None:
        result = CliRunner().invoke(cli, ["--config", str(tmp_path / "missing.toml"), "serve"])
        assert result.exit_code == 1
        assert "Config not found" in result.output

    def test_explicit_cli_host_port_override_effective_config(self) -> None:
        cfg = _make_cfg()
        cfg.server.host = "127.0.0.2"
        cfg.server.port = 8082
        with (
            patch("autodj.config.load_config", return_value=cfg),
            patch("autodj.cli._load_index_for_serve", return_value=_make_sim()),
            patch("autodj.server.serve") as serve_mock,
        ):
            result = CliRunner().invoke(cli, ["serve", "--host", "127.0.0.3", "--port", "8083"])
        assert result.exit_code == 0
        assert serve_mock.call_args.kwargs["host"] == "127.0.0.3"
        assert serve_mock.call_args.kwargs["port"] == 8083
        assert cfg.config_sources == ("defaults", "cli")

    def test_no_cli_override_does_not_claim_cli_config_source(self) -> None:
        cfg = _make_cfg()
        original_sources = cfg.config_sources
        with (
            patch("autodj.config.load_config", return_value=cfg),
            patch("autodj.cli._load_index_for_serve", return_value=_make_sim()),
            patch("autodj.server.serve"),
        ):
            result = CliRunner().invoke(cli, ["serve"])
        assert result.exit_code == 0
        assert cfg.config_sources == original_sources


# ---------------------------------------------------------------------------
# CLI commands — index-not-found exits with code 1
# ---------------------------------------------------------------------------


class TestCliIndexNotFound:
    """Offline commands should exit(1) when index is missing."""

    def _write_minimal_config(self, tmp_path: Path) -> Path:
        cfg = tmp_path / "config.toml"
        cfg.write_text(
            '[library]\nmusic_dir = "Z:/Music"\n'
            '[index]\nindex_dir = "Z:/no-such-index"\nmodel_dir = "models"\n'
            "[playback]\ncrossfade_seconds = 3.0\nrepeat_rules = { track = { tracks = 50 } }\n"
            '[model]\nname = "OpenMuQ/MuQ-large-msd-iter"\n',
            encoding="utf-8",
        )
        return cfg

    def test_stats_exits_on_missing_index(self, tmp_path: Path) -> None:
        cfg = self._write_minimal_config(tmp_path)
        result = CliRunner().invoke(cli, ["--config", str(cfg), "stats"])
        assert result.exit_code == 1


# ---------------------------------------------------------------------------
# CLI happy paths — real code, mocked heavy deps
# ---------------------------------------------------------------------------


class TestCliHappyPaths:
    """Cover the success branches of each command by mocking model/index/player."""

    def test_stats_happy_path(self) -> None:
        cfg_mock = _make_cfg()
        import tempfile

        from autodj.catalog import LibraryCatalog

        with tempfile.TemporaryDirectory() as dirname:
            point_index(cfg_mock.index, Path(dirname))
            with LibraryCatalog(cfg_mock.index.library_dir / "library.db") as catalog:
                for entry in [_make_entry(i) for i in range(5)]:
                    catalog.put_entry(entry)
            with patch("autodj.config.load_config", return_value=cfg_mock):
                result = CliRunner().invoke(cli, ["stats"])
        assert result.exit_code == 0


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
        _configure_sim_api(sim)

        cfg = _make_cfg(beets_db=db)

        result = _resolve_seed(sim, cfg, "Portishead", MagicMock())
        assert result is not None
        assert "Portishead" in result.path


# ---------------------------------------------------------------------------
# _require_ffmpeg_for_stream
# ---------------------------------------------------------------------------


class TestCmdServe:
    def test_serve_exits_zero(self) -> None:
        cfg_mock = _make_cfg()
        sim_mock = _make_sim()
        with (
            patch("autodj.config.load_config", return_value=cfg_mock),
            patch("autodj.cli._load_index_for_serve", return_value=sim_mock),
            patch("autodj.server.serve"),
        ):
            result = CliRunner().invoke(cli, ["serve"])
        assert result.exit_code == 0
        assert "autodj doctor" not in result.output

    @pytest.mark.parametrize(
        ("failure", "expected_code", "exception_type"),
        [
            (RuntimeError("server failed"), 1, RuntimeError),
            (SystemExit(7), 7, SystemExit),
            # CliRunner normalizes Click's Exit to SystemExit while retaining its code.
            (click.exceptions.Exit(8), 8, SystemExit),
        ],
    )
    def test_serve_failure_prints_doctor_hint_and_preserves_failure(
        self,
        failure: BaseException,
        expected_code: int,
        exception_type: type[BaseException],
    ) -> None:
        with (
            patch("autodj.config.load_config", return_value=_make_cfg()),
            patch("autodj.cli._load_index_for_serve", return_value=_make_sim()),
            patch("autodj.server.serve", side_effect=failure),
        ):
            result = CliRunner().invoke(cli, ["serve"])

        assert result.exit_code == expected_code
        assert isinstance(result.exception, exception_type)
        assert "autodj doctor" in result.output

    def test_serve_config_error_prints_doctor_hint_without_config_value(
        self, tmp_path: Path
    ) -> None:
        config_path = tmp_path / "private-config.toml"
        with patch(
            "autodj.config.load_config",
            side_effect=ValueError("invalid configuration"),
        ):
            result = CliRunner().invoke(cli, ["--config", str(config_path), "serve"])

        assert result.exit_code != 0
        assert "autodj doctor" in result.output
        assert "--config" in result.output
        assert str(config_path) not in result.output

    @pytest.mark.parametrize(
        "failure",
        [KeyboardInterrupt(), click.Abort(), click.exceptions.Exit(0)],
    )
    def test_serve_interruption_and_zero_exit_do_not_print_doctor_hint(
        self, failure: BaseException
    ) -> None:
        with (
            patch("autodj.config.load_config", return_value=_make_cfg()),
            patch(
                "autodj.cli._load_index_for_serve",
                return_value=_make_sim(),
            ),
            patch("autodj.server.serve", side_effect=failure),
        ):
            result = CliRunner().invoke(cli, ["serve"])

        assert "autodj doctor" not in result.output

    def test_serve_prints_web_ui_url(self) -> None:
        cfg_mock = _make_cfg()
        sim_mock = _make_sim()
        with (
            patch("autodj.config.load_config", return_value=cfg_mock),
            patch("autodj.cli._load_index_for_serve", return_value=sim_mock),
            patch("autodj.server.serve"),
        ):
            result = CliRunner().invoke(cli, ["serve", "--port", "9999"])
        assert "9999" in result.output or result.exit_code == 0

    def test_manual_allowlists_on_a_network_host_still_require_a_token(self) -> None:
        cfg = _make_cfg()
        with (
            patch("autodj.config.load_config", return_value=cfg),
            patch("autodj.cli._load_index_for_serve") as load_index,
            patch("autodj.server.serve") as serve_mock,
        ):
            result = CliRunner().invoke(
                cli,
                [
                    "serve",
                    "--host",
                    "0.0.0.0",
                    "--allowed-host",
                    "radio.local",
                    "--allowed-origin",
                    "http://radio.local:8080",
                ],
            )

        assert result.exit_code == 1
        assert "LAN binding requires" in result.output
        load_index.assert_not_called()
        serve_mock.assert_not_called()

    def test_authenticated_lan_overrides_reach_server_without_token_output(self) -> None:
        cfg = _make_cfg()
        original_server = cfg.server
        token = "s" * 32
        with (
            patch("autodj.config.load_config", return_value=cfg),
            patch(
                "autodj.cli._load_index_for_serve",
                return_value=_make_sim(),
            ),
            patch("autodj.server.serve") as serve_mock,
        ):
            result = CliRunner().invoke(
                cli,
                [
                    "serve",
                    "--host",
                    "0.0.0.0",
                    "--access-token",
                    token,
                    "--allowed-host",
                    "Radio.Local",
                    "--allowed-origin",
                    "HTTP://Radio.Local:8080/",
                ],
            )

        assert result.exit_code == 0
        assert token not in result.output
        assert cfg.server is not original_server
        assert cfg.server.access_token == token
        assert cfg.server.allowed_hosts == ["radio.local"]
        assert cfg.server.allowed_origins == ["http://radio.local:8080"]
        serve_mock.assert_called_once()
        assert serve_mock.call_args.kwargs["host"] == "0.0.0.0"
        assert serve_mock.call_args.kwargs["port"] == 8080

    def test_weak_cli_token_is_rejected_without_echo_or_index_load(self) -> None:
        cfg = _make_cfg()
        original_server = cfg.server
        weak = "do-not-print-me"
        with (
            patch("autodj.config.load_config", return_value=cfg),
            patch("autodj.cli._load_index_for_serve") as load_index,
            patch("autodj.server.serve") as serve_mock,
        ):
            result = CliRunner().invoke(
                cli,
                [
                    "serve",
                    "--host",
                    "0.0.0.0",
                    "--access-token",
                    weak,
                    "--allowed-host",
                    "radio.local",
                    "--allowed-origin",
                    "http://radio.local:8080",
                ],
            )

        assert result.exit_code == 1
        assert "at least 32 UTF-8 bytes" in result.output
        assert weak not in result.output
        assert weak not in repr(result.exception)
        assert cfg.server is original_server
        assert cfg.server == ServerConfig()
        load_index.assert_not_called()
        serve_mock.assert_not_called()

    def test_cli_rejects_allowed_origin_with_path(self) -> None:
        cfg = _make_cfg()
        original_server = cfg.server
        with (
            patch("autodj.config.load_config", return_value=cfg),
            patch("autodj.cli._load_index_for_serve") as load_index,
            patch("autodj.server.serve") as serve_mock,
        ):
            result = CliRunner().invoke(
                cli,
                [
                    "serve",
                    "--host",
                    "0.0.0.0",
                    "--insecure-lan",
                    "--allowed-host",
                    "radio.local",
                    "--allowed-origin",
                    "https://radio.local/private",
                ],
            )

        assert result.exit_code == 1
        assert "without userinfo, path, query, or fragment" in result.output
        assert cfg.server is original_server
        assert cfg.server == ServerConfig()
        load_index.assert_not_called()
        serve_mock.assert_not_called()

    def test_cli_rejects_invalid_host_without_mutating_config(self) -> None:
        cfg = _make_cfg()
        original_server = cfg.server
        with (
            patch("autodj.config.load_config", return_value=cfg),
            patch("autodj.cli._load_index_for_serve") as load_index,
            patch("autodj.server.serve") as serve_mock,
        ):
            result = CliRunner().invoke(cli, ["serve", "--host", "[::1]"])

        assert result.exit_code == 1
        assert "server.host" in result.output
        assert cfg.server is original_server
        assert cfg.server == ServerConfig()
        load_index.assert_not_called()
        serve_mock.assert_not_called()

    def test_specific_authenticated_lan_derives_policy_without_lists(self) -> None:
        cfg = _make_cfg()
        token = "s" * 32
        with (
            patch("autodj.config.load_config", return_value=cfg),
            patch(
                "autodj.cli._load_index_for_serve",
                return_value=_make_sim(),
            ),
            patch("autodj.server.serve") as serve_mock,
        ):
            result = CliRunner().invoke(
                cli,
                [
                    "serve",
                    "--host",
                    "192.168.1.10",
                    "--access-token",
                    token,
                ],
            )

        assert result.exit_code == 0
        assert cfg.server.effective_allowed_hosts() == ["192.168.1.10"]
        assert cfg.server.effective_allowed_origins() == ["http://192.168.1.10:8080"]
        assert "WARNING" not in result.output
        serve_mock.assert_called_once()

    def test_documented_insecure_lan_example_is_executable_and_warned(self) -> None:
        cfg = _make_cfg()
        with (
            patch("autodj.config.load_config", return_value=cfg),
            patch(
                "autodj.cli._load_index_for_serve",
                return_value=_make_sim(),
            ),
            patch("autodj.server.serve") as serve_mock,
        ):
            result = CliRunner().invoke(
                cli,
                [
                    "serve",
                    "--host",
                    "0.0.0.0",
                    "--insecure-lan",
                    "--allowed-host",
                    "radio.local",
                    "--allowed-origin",
                    "http://radio.local:8080",
                ],
            )

        assert result.exit_code == 0
        assert "WARNING" in result.output
        serve_mock.assert_called_once()

    def test_insecure_ack_on_loopback_emits_no_warning(self) -> None:
        cfg = _make_cfg()
        with (
            patch("autodj.config.load_config", return_value=cfg),
            patch(
                "autodj.cli._load_index_for_serve",
                return_value=_make_sim(),
            ),
            patch("autodj.server.serve"),
        ):
            result = CliRunner().invoke(cli, ["serve", "--insecure-lan"])

        assert result.exit_code == 0
        assert "WARNING" not in result.output

    def test_token_with_insecure_ack_does_not_warn_unauthenticated(self) -> None:
        cfg = _make_cfg()
        with (
            patch("autodj.config.load_config", return_value=cfg),
            patch(
                "autodj.cli._load_index_for_serve",
                return_value=_make_sim(),
            ),
            patch("autodj.server.serve"),
        ):
            result = CliRunner().invoke(
                cli,
                [
                    "serve",
                    "--host",
                    "0.0.0.0",
                    "--access-token",
                    "s" * 32,
                    "--insecure-lan",
                    "--allowed-host",
                    "radio.local",
                    "--allowed-origin",
                    "http://radio.local:8080",
                ],
            )

        assert result.exit_code == 0
        assert "WARNING" not in result.output

    def test_serve_uses_toml_host_and_port_when_flags_are_omitted(self) -> None:
        cfg = _make_cfg()
        cfg.server = ServerConfig(host="127.0.0.2", port=9090)
        with (
            patch("autodj.config.load_config", return_value=cfg),
            patch(
                "autodj.cli._load_index_for_serve",
                return_value=_make_sim(),
            ),
            patch("autodj.server.serve") as serve_mock,
        ):
            result = CliRunner().invoke(cli, ["serve"])

        assert result.exit_code == 0
        assert serve_mock.call_args.kwargs["host"] == "127.0.0.2"
        assert serve_mock.call_args.kwargs["port"] == 9090

    def test_explicit_cli_values_override_toml_server_values(self) -> None:
        cfg = _make_cfg()
        cfg.server = ServerConfig(host="127.0.0.2", port=9090)
        with (
            patch("autodj.config.load_config", return_value=cfg),
            patch(
                "autodj.cli._load_index_for_serve",
                return_value=_make_sim(),
            ),
            patch("autodj.server.serve") as serve_mock,
        ):
            result = CliRunner().invoke(
                cli,
                ["serve", "--host", "localhost", "--port", "8123"],
            )

        assert result.exit_code == 0
        assert serve_mock.call_args.kwargs["host"] == "localhost"
        assert serve_mock.call_args.kwargs["port"] == 8123


# ---------------------------------------------------------------------------
# cmd_prune + cmd_enrich
# ---------------------------------------------------------------------------


class TestCmdPrune:
    def test_prune_no_index(self, tmp_path: Path) -> None:
        cfg_mock = _make_cfg()
        point_index(cfg_mock.index, tmp_path)
        with patch("autodj.config.load_config", return_value=cfg_mock):
            result = CliRunner().invoke(cli, ["prune"])
        assert result.exit_code == 1
        assert "No current library" in result.output

    def test_prune_previews_and_confirms_catalog_removal(self, tmp_path: Path) -> None:
        cfg_mock = _make_cfg()
        point_index(cfg_mock.index, tmp_path)
        (tmp_path / "library.db").touch()

        class Manager:
            def __init__(self, *_args):
                pass

            def scan(self):
                pass

            def missing_removal_preview(self):
                return {
                    "ready": True,
                    "count": 1,
                    "total": 10,
                    "percentage": 10,
                    "requires_override": False,
                    "tracks": [{"artist": "A", "title": "T", "path": "p"}],
                    "token": "preview",
                }

            def remove_missing(self, token, **kwargs):
                assert token == "preview" and kwargs == {"confirmed": True, "allow_mass": False}
                return 1

            def close(self):
                pass

        with (
            patch("autodj.config.load_config", return_value=cfg_mock),
            patch("autodj.library.LibraryManager", Manager),
        ):
            result = CliRunner().invoke(cli, ["prune"], input="y\n")
        assert result.exit_code == 0
        assert "Removed 1" in result.output

    def test_prune_requires_force_for_mass_removal(self, tmp_path: Path) -> None:
        cfg_mock = _make_cfg()
        point_index(cfg_mock.index, tmp_path)
        (tmp_path / "library.db").touch()

        class Manager:
            def __init__(self, *_args):
                pass

            def scan(self):
                pass

            def missing_removal_preview(self):
                return {
                    "ready": True,
                    "count": 5,
                    "total": 10,
                    "percentage": 50,
                    "requires_override": True,
                    "tracks": [],
                    "token": "preview",
                }

            def close(self):
                pass

        with (
            patch("autodj.config.load_config", return_value=cfg_mock),
            patch("autodj.library.LibraryManager", Manager),
        ):
            result = CliRunner().invoke(cli, ["prune"])
        assert result.exit_code == 1
        assert "--force" in result.output

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
        point_index(cfg_mock.index, tmp_path)
        with patch("autodj.config.load_config", return_value=cfg_mock):
            result = CliRunner().invoke(cli, ["enrich"])
        assert result.exit_code == 1
        assert "No current library" in result.output

    def test_enrich_all_synced(self, tmp_path: Path) -> None:
        cfg_mock = _make_cfg(beets_db=tmp_path / "library.db")
        point_index(cfg_mock.index, tmp_path)
        (tmp_path / "library.db").touch()
        with (
            patch("autodj.config.load_config", return_value=cfg_mock),
            patch("autodj.indexer.enrich_from_beets", return_value=(0, 1000)),
        ):
            result = CliRunner().invoke(cli, ["enrich"])
        assert result.exit_code == 0
        assert "in sync" in result.output or "0 changed" in result.output

    def test_enrich_updates_some(self, tmp_path: Path) -> None:
        cfg_mock = _make_cfg(beets_db=tmp_path / "library.db")
        point_index(cfg_mock.index, tmp_path)
        (tmp_path / "library.db").touch()
        with (
            patch("autodj.config.load_config", return_value=cfg_mock),
            patch("autodj.indexer.enrich_from_beets", return_value=(42, 1000)),
        ):
            result = CliRunner().invoke(cli, ["enrich"])
        assert result.exit_code == 0
        assert "42" in result.output

    def test_enrich_error_exits_one(self, tmp_path: Path) -> None:
        cfg_mock = _make_cfg(beets_db=tmp_path / "library.db")
        point_index(cfg_mock.index, tmp_path)
        (tmp_path / "library.db").touch()
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
# ---------------------------------------------------------------------------


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
            patch("autodj.cli._load_cfg_or_exit", return_value=cfg_mock),
            patch("autodj.cli._load_index_for_serve", return_value=_make_sim()),
            patch("autodj.cli._can_import", return_value=True),
            patch("autodj.model_identity.model_analysis_available", return_value=True),
            patch.dict("sys.modules", {"autodj.model": MagicMock()}),
            patch("autodj.library.analyse_library", side_effect=RuntimeError("model crash")),
        ):
            result = CliRunner().invoke(cli, ["index"])
        assert result.exit_code == 1
        assert "Indexing failed: model crash" in result.output


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


# ---------------------------------------------------------------------------
# cmd_analyse
# ---------------------------------------------------------------------------


class TestCmdAnalyse:
    def _cfg_with_index(self, tmp_path: Path, entries: int = 1) -> MagicMock:
        from autodj.catalog import LibraryCatalog
        from autodj.indexer import IndexEntry

        idx_dir = tmp_path / "idx"
        idx_dir.mkdir()
        rows = [
            IndexEntry(
                path=str(tmp_path / f"x{i}.flac"),
                title="t",
                artist="a",
                album="al",
                genre="g",
                bpm=120.0,
                year=2020,
                length=180.0,
                energy=0.05,
                key=0,
                mode=1,
                tempo_confidence=0.5,
            )
            for i in range(entries)
        ]
        for entry in rows:
            Path(entry.path).write_bytes(b"track")
        with LibraryCatalog(idx_dir / "library.db") as catalog:
            catalog.add_source(tmp_path)
            for entry in rows:
                catalog.put_entry(entry)
        cfg = _make_cfg()
        point_index(cfg.index, idx_dir)
        cfg.index.name = "default"
        cfg.library.music_dir = tmp_path
        return cfg

    def test_config_missing(self) -> None:
        with patch("autodj.config.load_config", side_effect=FileNotFoundError("nope")):
            result = CliRunner().invoke(cli, ["analyse"])
        assert result.exit_code == 1
        assert "Config not found" in result.output

    def test_no_index(self, tmp_path: Path) -> None:
        cfg = _make_cfg()
        point_index(cfg.index, tmp_path / "missing")
        with patch("autodj.config.load_config", return_value=cfg):
            result = CliRunner().invoke(cli, ["analyse"])
        assert result.exit_code == 1
        assert "No current library" in result.output

    @pytest.mark.parametrize("command", ["analyse", "stats"])
    def test_old_index_format_exits_with_the_rebuild_hint(
        self, tmp_path: Path, command: str
    ) -> None:
        cfg = _make_cfg()
        point_index(cfg.index, tmp_path / "old-index")
        cfg.index.library_dir.mkdir(parents=True, exist_ok=True)
        cfg.index.shared_dir.mkdir(parents=True, exist_ok=True)
        (cfg.index.stations_dir / "default").mkdir(parents=True, exist_ok=True)
        (cfg.index.library_dir / "tracks.db").touch()  # a pre-manifest index
        cfg.library.music_dir = tmp_path
        with patch("autodj.config.load_config", return_value=cfg):
            result = CliRunner().invoke(cli, [command])
        assert result.exit_code == 1
        assert "current library" in " ".join(result.output.split()).lower()

    def test_success_invokes_backfill(self, tmp_path: Path) -> None:
        cfg = self._cfg_with_index(tmp_path, entries=3)
        captured = {}

        def fake_backfill(config, entries, *, limit=None, prune=True):
            captured["cfg"] = config
            captured["entries"] = list(entries)
            captured["limit"] = limit
            captured["prune"] = prune

        with (
            patch("autodj.config.load_config", return_value=cfg),
            patch("autodj.indexer.backfill_dj_meta", fake_backfill),
            patch(
                "autodj.cli._analyse_catalog_dj_meta",
                side_effect=lambda cfg, catalog, entries, limit=None: fake_backfill(
                    cfg, entries, limit=limit, prune=False
                ),
            ),
        ):
            result = CliRunner().invoke(cli, ["analyse", "--limit", "2"])
        assert result.exit_code == 0
        assert captured["cfg"] is cfg
        # Every indexed track goes in, so rows past the limit are not pruned.
        assert len(captured["entries"]) == 3
        assert captured["limit"] == 2
        assert captured["prune"] is False

    def test_analyse_covers_other_folders_and_preserves_manual_cues(self, tmp_path: Path) -> None:
        from autodj.catalog import LibraryCatalog
        from autodj.dj_meta import DjMeta, close_cache

        cfg = self._cfg_with_index(tmp_path, entries=2)
        cfg.library.music_dir = tmp_path / "unused-old-root"
        cfg.index.throttle_ms = 0
        cfg.playback.import_external_cues = False
        with LibraryCatalog(cfg.index.library_dir / "library.db") as catalog:
            entries = catalog.entries_snapshot("music")
            catalog.set_points(entries[0].path, {"intro": 9.0}, source="user")
        close_cache()
        try:
            with (
                patch("autodj.config.load_config", return_value=cfg),
                patch(
                    "autodj.indexer._analyse_one_track",
                    return_value=DjMeta(intro_end_s=5.0, outro_start_s=170.0, analysed=True),
                ) as analyse,
            ):
                result = CliRunner().invoke(cli, ["analyse"])
                assert result.exit_code == 0, result.output
                assert analyse.call_count == 2
                again = CliRunner().invoke(cli, ["analyse"])
                assert again.exit_code == 0, again.output
                assert analyse.call_count == 2
            with LibraryCatalog(cfg.index.library_dir / "library.db") as catalog:
                assert catalog.points(entries[0].path)["intro"] == 9.0
                # The cues are placed from the saved Intro; none is written back.
                assert catalog.points(entries[1].path)["intro"] is None
            assert any(
                call.args[1] and call.args[1]["intro"] == 9.0 for call in analyse.call_args_list
            )
        finally:
            close_cache()

    def test_backfill_exception_exits_one(self, tmp_path: Path) -> None:
        cfg = self._cfg_with_index(tmp_path)
        with (
            patch("autodj.config.load_config", return_value=cfg),
            patch("autodj.cli._analyse_catalog_dj_meta", side_effect=RuntimeError("boom")),
        ):
            result = CliRunner().invoke(cli, ["analyse"])
        assert result.exit_code == 1
        assert "Analyse failed" in result.output

    def test_missing_packages_exits(self, tmp_path: Path) -> None:
        import builtins

        real_import = builtins.__import__

        def fake_import(name, *a, **kw):
            if name in ("librosa", "soundfile"):
                raise ImportError("missing")
            return real_import(name, *a, **kw)

        with patch("builtins.__import__", side_effect=fake_import):
            result = CliRunner().invoke(cli, ["analyse"])
        assert result.exit_code == 1
        assert "missing packages" in result.output


def test_third_party_info_logs_are_quiet_unless_verbose() -> None:
    import logging

    CliRunner().invoke(cli, ["doctor", "--help"])
    for name in ("faiss.loader", "httpx"):
        assert logging.getLogger(name).getEffectiveLevel() == logging.WARNING

    CliRunner().invoke(cli, ["-v", "doctor", "--help"])
    for name in ("faiss.loader", "httpx"):
        assert logging.getLogger(name).getEffectiveLevel() == logging.DEBUG


# ---------------------------------------------------------------------------
# serve: a half-restored index and the serve lock (#70)
# ---------------------------------------------------------------------------


class TestClaimIndexForServe:
    def _ctx(self):
        closers = []
        ctx = MagicMock()
        ctx.call_on_close.side_effect = closers.append
        return ctx, closers

    def test_holds_the_lock_until_the_command_ends(self, tmp_path: Path) -> None:
        from autodj.backup import server_running
        from autodj.cli import _claim_index_for_serve

        ctx, closers = self._ctx()
        _claim_index_for_serve(ctx, tmp_path / "default")
        assert server_running(tmp_path / "default")
        for close in closers:
            close()
        assert not server_running(tmp_path / "default")

    def test_refuses_a_second_server_on_the_same_index(self, tmp_path: Path) -> None:
        from autodj.cli import _claim_index_for_serve

        ctx, closers = self._ctx()
        _claim_index_for_serve(ctx, tmp_path / "default")
        try:
            with pytest.raises(SystemExit):
                _claim_index_for_serve(self._ctx()[0], tmp_path / "default")
        finally:
            for close in closers:
                close()

    def test_refuses_a_half_restored_index(self, tmp_path: Path) -> None:
        from autodj.cli import _claim_index_for_serve

        active = tmp_path / "default"
        active.mkdir()
        (active / ".autodj-restore.json").write_text('{"state": "installing", "steps": []}')
        with pytest.raises(SystemExit):
            _claim_index_for_serve(self._ctx()[0], active)

    def test_starts_with_only_old_copies_left(self, tmp_path: Path) -> None:
        from autodj.cli import _claim_index_for_serve

        active = tmp_path / "default"
        active.mkdir()
        (active / ".autodj-restore.json").write_text('{"state": "installed", "steps": []}')
        ctx, closers = self._ctx()
        _claim_index_for_serve(ctx, active)
        for close in closers:
            close()


def test_serve_log_file_is_written_and_kept_for_a_week(tmp_path: Path) -> None:
    import logging

    from autodj.cli import _add_log_file

    path = tmp_path / "logs" / "autodj.log"
    root = logging.getLogger()
    before = list(root.handlers)
    _add_log_file(path)
    added = [handler for handler in root.handlers if handler not in before]
    try:
        assert len(added) == 1 and added[0].backupCount == 7
        logging.getLogger("autodj.test").warning("written to the file")
        added[0].flush()
        assert "written to the file" in path.read_text(encoding="utf-8")
    finally:
        for handler in added:
            root.removeHandler(handler)
            handler.close()


def test_serve_log_file_gets_the_file_text_of_a_console_only_record(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """A record with LOG_FILE_TEXT (the start-up pairing code) keeps its code off disk."""
    import logging

    from autodj.cli import _add_log_file
    from autodj.lan import LOG_FILE_TEXT

    path = tmp_path / "logs" / "autodj.log"
    root = logging.getLogger()
    before = list(root.handlers)
    _add_log_file(path)
    added = [handler for handler in root.handlers if handler not in before]
    try:
        with caplog.at_level(logging.INFO, logger="autodj.test"):
            logging.getLogger("autodj.test").info(
                "Pair this browser within five minutes using code: %s",
                "12345678",
                extra={LOG_FILE_TEXT: "A pairing code was shown on the console."},
            )
        added[0].flush()
        text = path.read_text(encoding="utf-8")
        assert "12345678" not in text
        assert "A pairing code was shown on the console." in text
        assert "using code: 12345678" in caplog.text  # the console still shows it
    finally:
        for handler in added:
            root.removeHandler(handler)
            handler.close()


@pytest.mark.parametrize("completed", [0, 7])
def test_analyse_library_reports_embedded_track_count(completed: int, tmp_path: Path) -> None:
    cfg = MagicMock()
    cfg.model = MagicMock()
    cfg.index = MagicMock()
    cfg.huggingface = MagicMock()
    with (
        patch("autodj.cli._load_cfg_or_exit", return_value=cfg),
        patch("autodj.model_identity.model_analysis_available", return_value=True),
        patch.dict("sys.modules", {"autodj.model": MagicMock()}),
        patch("autodj.library.analyse_library", return_value=completed),
    ):
        result = CliRunner().invoke(cli, ["analyse-library"])

    assert result.exit_code == 0
    assert f"Advanced analysis embedded {completed} tracks" in result.output


@pytest.mark.parametrize(
    "arguments",
    [
        ["analyse-library"],
        ["worker", "--server", "https://example.invalid", "--token", "unused"],
    ],
)
def test_model_commands_reject_no_model_before_import_or_connection(monkeypatch, arguments):
    import sys

    monkeypatch.setenv("AUTODJ_NO_MODEL", "1")
    with patch.dict(sys.modules, {"autodj.model": None}):
        result = CliRunner().invoke(cli, arguments)
    assert result.exit_code == 1
    assert "autodj setup --backend cpu" in result.output
    assert "Traceback" not in result.output


@pytest.mark.parametrize("pending", [False, True])
def test_index_in_no_model_mode_preserves_basic_import(monkeypatch, pending):
    import sys

    monkeypatch.setenv("AUTODJ_NO_MODEL", "1")
    cfg = _make_cfg()
    library = MagicMock()
    library.analysis_pending.return_value = [object()] if pending else []
    library.snapshot.return_value = {"tracks_ready": 1, "import": {"errors": []}}
    with (
        patch("autodj.cli._load_cfg_or_exit", return_value=cfg),
        patch("autodj.cli._load_index_for_serve", return_value=MagicMock()),
        patch("autodj.library.LibraryManager", return_value=library),
        patch.dict(sys.modules, {"autodj.model": None}),
    ):
        result = CliRunner().invoke(cli, ["index", "--no-enrich", "--no-analyse"])
    library.scan.assert_called_once()
    assert "1 songs available" in result.output
    assert result.exit_code == 0, result.output
    if pending:
        assert "analysis pending" in result.output.lower()
