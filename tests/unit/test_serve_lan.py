"""``autodj serve --lan``: config, staging, start-up block and pairing-code."""

from __future__ import annotations

import logging
import re
from pathlib import Path
from unittest.mock import MagicMock, patch

import click
import pytest
from click.testing import CliRunner

from autodj.cli import _stage_serve_server, cli
from autodj.config import ServerConfig, StreamConfig, load_config
from autodj.lan import load_or_create_access_token

_DETECTED = ["127.0.0.1", "192.168.1.20", "::1", "localhost", "nas", "nas.local"]
_TOKEN = "e" * 40


@pytest.fixture(autouse=True)
def _fixed_hosts(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("autodj.lan.detect_lan_hosts", lambda: list(_DETECTED))


def _cfg(tmp_path: Path, server: ServerConfig | None = None) -> MagicMock:
    cfg = MagicMock()
    cfg.library.beets_db = None
    cfg.playback.history_file = None
    cfg.playback.discovery_every = None
    cfg.presets = {}
    cfg.index.name = "default"
    cfg.index.index_dir = tmp_path / "index"
    cfg.server = server or ServerConfig()
    cfg.stream = StreamConfig()
    cfg.config_sources = ("defaults",)
    return cfg


def _stage(cfg: MagicMock, **overrides: object) -> ServerConfig:
    options: dict[str, object] = {
        "host": None,
        "port": None,
        "access_token": None,
        "insecure_lan": None,
        "allowed_hosts": (),
        "allowed_origins": (),
        "lan": None,
        "tls": False,
    }
    options.update(overrides)
    return _stage_serve_server(cfg, **options)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Staging
# ---------------------------------------------------------------------------


def test_lan_flag_binds_everywhere_and_creates_the_token(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path)

    staged = _stage(cfg, lan=True)

    token_file = tmp_path / "index" / ".access-token"
    assert staged.lan is True
    assert staged.host == "0.0.0.0"
    assert staged.allowed_hosts == sorted(_DETECTED)
    assert "http://192.168.1.20:8080" in (staged.allowed_origins or [])
    assert staged.access_token == token_file.read_text(encoding="utf-8").strip()


def test_config_lan_is_used_without_the_flag(tmp_path: Path) -> None:
    staged = _stage(_cfg(tmp_path, ServerConfig(lan=True, port=9000)))

    assert staged.host == "0.0.0.0"
    assert "http://nas:9000" in (staged.allowed_origins or [])


def test_without_lan_nothing_is_detected(tmp_path: Path) -> None:
    staged = _stage(_cfg(tmp_path))

    assert staged == ServerConfig()
    assert not (tmp_path / "index" / ".access-token").exists()


def test_cli_lists_merge_with_detected_hosts(tmp_path: Path) -> None:
    staged = _stage(
        _cfg(tmp_path, ServerConfig(allowed_hosts=["from-config.lan"])),
        lan=True,
        allowed_hosts=("Radio.Local",),
        allowed_origins=("http://proxy.example",),
    )

    assert staged.allowed_hosts is not None
    assert "radio.local" in staged.allowed_hosts
    assert "from-config.lan" not in staged.allowed_hosts  # the CLI list replaces config
    assert "nas" in staged.allowed_hosts
    assert "http://proxy.example" in (staged.allowed_origins or [])
    assert "http://radio.local:8080" in (staged.allowed_origins or [])


def test_explicit_token_wins_over_the_saved_file(tmp_path: Path) -> None:
    load_or_create_access_token(tmp_path / "index" / ".access-token")

    from_config = _stage(_cfg(tmp_path, ServerConfig(access_token=_TOKEN)), lan=True)
    from_flag = _stage(_cfg(tmp_path), lan=True, access_token="f" * 40)

    assert from_config.access_token == _TOKEN
    assert from_flag.access_token == "f" * 40


def test_saved_token_is_reused_across_starts(tmp_path: Path) -> None:
    first = _stage(_cfg(tmp_path), lan=True)
    second = _stage(_cfg(tmp_path), lan=True)

    assert first.access_token == second.access_token


def test_lan_with_insecure_lan_needs_no_token(tmp_path: Path) -> None:
    staged = _stage(_cfg(tmp_path), lan=True, insecure_lan=True)

    assert staged.access_token is None
    assert staged.insecure_lan is True
    assert staged.allowed_hosts == sorted(_DETECTED)
    assert not (tmp_path / "index" / ".access-token").exists()


def test_insecure_lan_alone_still_needs_explicit_lists(tmp_path: Path) -> None:
    with pytest.raises(click.ClickException, match="allowed_hosts"):
        _stage(_cfg(tmp_path), host="0.0.0.0", insecure_lan=True)


def test_explicit_non_loopback_host_is_kept(tmp_path: Path) -> None:
    staged = _stage(_cfg(tmp_path), lan=True, host="192.168.1.20", tls=True)

    assert staged.host == "192.168.1.20"
    assert "https://192.168.1.20:8080" in (staged.allowed_origins or [])


def test_unreadable_token_file_stops_start_up(tmp_path: Path) -> None:
    (tmp_path / "index" / ".access-token").mkdir(parents=True)

    with pytest.raises(click.ClickException, match="LAN mode cannot start"):
        _stage(_cfg(tmp_path), lan=True)


def test_invalid_cli_host_is_a_click_error(tmp_path: Path) -> None:
    with pytest.raises(click.ClickException, match="invalid host"):
        _stage(_cfg(tmp_path), lan=True, allowed_hosts=("",))


# ---------------------------------------------------------------------------
# serve command
# ---------------------------------------------------------------------------


def _invoke_serve(cfg: MagicMock, *args: str) -> tuple[click.testing.Result, MagicMock]:
    sim = MagicMock()
    sim.ntotal = 1
    with (
        patch("autodj.config.load_config", return_value=cfg),
        patch("autodj.similarity.SimilarityIndex.from_index_dir", return_value=sim),
        patch("autodj.server.serve") as serve_mock,
    ):
        result = CliRunner().invoke(cli, ["serve", *args])
    return result, serve_mock


def test_serve_lan_reaches_the_server_without_printing_the_token(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path)

    result, serve_mock = _invoke_serve(cfg, "--lan")

    assert result.exit_code == 0, result.output
    token = (tmp_path / "index" / ".access-token").read_text(encoding="utf-8").strip()
    assert token not in result.output
    assert serve_mock.call_args.kwargs["host"] == "0.0.0.0"
    assert cfg.server.lan is True
    assert cfg.server.access_token == token
    assert cfg.config_sources == ("defaults", "cli")
    assert serve_mock.call_args.kwargs["lan_configured_hosts"] is None


def test_serve_passes_the_hosts_configured_before_detection(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path, ServerConfig(allowed_hosts=["from-config.lan"]))

    _result, from_config = _invoke_serve(cfg, "--lan")
    cfg = _cfg(tmp_path, ServerConfig(allowed_hosts=["from-config.lan"]))
    _result, from_flag = _invoke_serve(cfg, "--lan", "--allowed-host", "Radio.Local")

    assert from_config.call_args.kwargs["lan_configured_hosts"] == ["from-config.lan"]
    assert from_flag.call_args.kwargs["lan_configured_hosts"] == ["Radio.Local"]


def test_serve_lan_insecure_warns(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path)

    result, serve_mock = _invoke_serve(cfg, "--lan", "--insecure-lan")

    assert result.exit_code == 0, result.output
    assert "unauthenticated" in result.output
    assert cfg.server.access_token is None
    serve_mock.assert_called_once()


def test_serve_lan_from_config_is_not_a_cli_override(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path, ServerConfig(lan=True))

    result, _serve_mock = _invoke_serve(cfg)

    assert result.exit_code == 0, result.output
    assert cfg.config_sources == ("defaults",)


def test_help_lists_lan_first_and_hides_advanced_overrides() -> None:
    result = CliRunner().invoke(cli, ["serve", "--help"])

    assert result.exit_code == 0
    assert "--lan" in result.output
    assert result.output.index("--lan") < result.output.index("--host")
    for hidden in ("--allowed-host", "--allowed-origin", "--access-token"):
        assert hidden not in result.output


# ---------------------------------------------------------------------------
# Environment
# ---------------------------------------------------------------------------


def test_autodj_lan_environment_variable(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)

    assert load_config(environ={}).server.lan is False
    assert load_config(environ={"AUTODJ_LAN": "yes"}).server.lan is True
    assert load_config(environ={"AUTODJ_LAN": "0"}).server.lan is False
    with pytest.raises(ValueError, match="AUTODJ_LAN"):
        load_config(environ={"AUTODJ_LAN": "maybe"})


# ---------------------------------------------------------------------------
# serve() start-up block
# ---------------------------------------------------------------------------


def _run_serve(
    server: ServerConfig,
    caplog: pytest.LogCaptureFixture,
    tmp_path: Path,
    *,
    in_container: bool = False,
    configured_hosts: list[str] | None = None,
) -> str:
    from autodj.server import serve

    cfg = MagicMock()
    cfg.index.index_dir = tmp_path
    cfg.playback.no_repeat_window = 50
    cfg.playback.artist_repeat_window = 3
    cfg.playback.crossfade_seconds = 3.0
    cfg.server = server
    sim = MagicMock()
    sim.entries = []
    sim.ntotal = 0
    with (
        patch("autodj.player.Player.run"),
        patch("uvicorn.run"),
        patch("autodj.server.running_in_container", return_value=in_container),
        caplog.at_level(logging.INFO, logger="autodj.server"),
    ):
        serve(cfg=cfg, sim=sim, seed_entry=None, lan_configured_hosts=configured_hosts)
    return caplog.text


def _lan_server(**overrides: object) -> ServerConfig:
    options: dict[str, object] = {
        "host": "0.0.0.0",
        "lan": True,
        "access_token": _TOKEN,
        "allowed_hosts": _DETECTED,
        "allowed_origins": ["http://nas:8080", "http://127.0.0.1:8080"],
    }
    options.update(overrides)
    return ServerConfig(**options)  # type: ignore[arg-type]


def test_serve_prints_addresses_and_pairing_code(
    caplog: pytest.LogCaptureFixture, tmp_path: Path
) -> None:
    text = _run_serve(_lan_server(), caplog, tmp_path)

    assert "Open AutoDJ from another device" in text
    assert "http://nas:8080" in text
    assert "http://192.168.1.20:8080" in text
    assert "http://localhost:8080" not in text.split("Open AutoDJ", 1)[1].split("Pairing", 1)[0]
    assert re.search(r"Pairing code: \d{8} \(valid for about \d+ more seconds\)", text)
    assert _TOKEN not in text


def test_serve_insecure_lan_says_no_pairing(
    caplog: pytest.LogCaptureFixture, tmp_path: Path
) -> None:
    text = _run_serve(_lan_server(access_token=None, insecure_lan=True), caplog, tmp_path)

    assert "http://nas:8080" in text
    assert "No pairing needed" in text


def test_serve_without_lan_keeps_the_old_pairing_line(
    caplog: pytest.LogCaptureFixture, tmp_path: Path
) -> None:
    server = ServerConfig(
        host="0.0.0.0",
        access_token=_TOKEN,
        allowed_hosts=["nas"],
        allowed_origins=["http://nas:8080"],
    )

    text = _run_serve(server, caplog, tmp_path)

    assert "Pair this browser within five minutes using code" in text
    assert "Open AutoDJ from another device" not in text


# ---------------------------------------------------------------------------
# devices pairing-code
# ---------------------------------------------------------------------------


@pytest.fixture
def project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("AUTODJ_ACCESS_TOKEN", raising=False)
    (tmp_path / "config.toml").write_text("[index]\nindex_dir = 'index'\n", encoding="utf-8")
    return tmp_path


def test_pairing_code_uses_the_saved_lan_token(project: Path) -> None:
    load_or_create_access_token(project / "index" / ".access-token")

    result = CliRunner().invoke(cli, ["devices", "pairing-code"])

    assert result.exit_code == 0, result.output
    assert re.fullmatch(r"\d{8}", result.stdout.strip())


def test_pairing_code_without_any_token_still_asks_for_setup(project: Path) -> None:
    result = CliRunner().invoke(cli, ["devices", "pairing-code"])

    assert result.exit_code == 1
    assert "Configure LAN access" in result.output


def test_pairing_code_reports_an_unreadable_token_file(project: Path) -> None:
    (project / "index" / ".access-token").mkdir(parents=True)

    result = CliRunner().invoke(cli, ["devices", "pairing-code"])

    assert result.exit_code == 1
    assert "cannot read access token" in result.output


def test_pairing_code_notes_that_it_comes_from_the_saved_token(project: Path) -> None:
    load_or_create_access_token(project / "index" / ".access-token")

    result = CliRunner().invoke(cli, ["devices", "pairing-code"])

    assert result.exit_code == 0, result.output
    assert "comes from the token `autodj serve --lan` saved" in result.stderr
    assert "comes from the token" not in result.stdout


def test_pairing_code_with_configured_token_has_no_saved_token_note(
    project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("AUTODJ_ACCESS_TOKEN", _TOKEN)
    load_or_create_access_token(project / "index" / ".access-token")

    result = CliRunner().invoke(cli, ["devices", "pairing-code"])

    assert result.exit_code == 0, result.output
    assert "comes from the token" not in result.stderr


def test_container_serve_lists_the_configured_address_first(
    caplog: pytest.LogCaptureFixture, tmp_path: Path
) -> None:
    server = _lan_server(
        allowed_hosts=[*_DETECTED, "radio.local", "172.17.0.2"],
        allowed_origins=["http://radio.local:8080"],
    )

    text = _run_serve(
        server,
        caplog,
        tmp_path,
        in_container=True,
        configured_hosts=["127.0.0.1", "radio.local"],
    )
    block = text.split("Open AutoDJ from another device on your network:", 1)[1]
    lines = block.splitlines()

    assert lines[1].strip() == "http://radio.local:8080"
    assert lines[2] == "Container addresses are not reachable from your network."
    assert "172.17.0.2" not in block
    assert "http://nas:8080" not in block
