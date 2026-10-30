"""``autodj doctor`` with several stations (#70): one report per station, repairs on consent."""

from __future__ import annotations

import json
import logging
import shutil
from pathlib import Path
from unittest.mock import patch

import pytest
from click.testing import CliRunner

from autodj.cli import cli
from autodj.config import AutoDJConfig, load_config, move_station_tables
from autodj.doctor import CheckStatus, DoctorCheck, DoctorReport, render_text, run_doctor
from autodj.terminal_text import format_text

STATION_CHECKS = [
    "station-folder",
    "speech",
    "saved-settings",
    "dmca",
    "stream",
    "time-checks",
    "categories",
    "recording",
    "liners",
]


def _config(root: Path, extra: str = "", *, missing_station_folder: bool = False) -> AutoDJConfig:
    (root / "music").mkdir(parents=True, exist_ok=True)
    path = root / "config.toml"
    path.write_text(
        f"""
[library]
music_dir = "{(root / "music").as_posix()}"

[index]
index_dir = "{(root / "index").as_posix()}"
model_dir = "{(root / "models").as_posix()}"

[recording]
folder = "{(root / "recordings" / "default").as_posix()}"

[stations.nightowl.stream]
station_name = "Night Owl Radio"

[stations.nightowl.recording]
folder = "{(root / "recordings" / "nightowl").as_posix()}"
"""
        + extra,
        encoding="utf-8",
    )
    cfg = load_config(path, environ={})
    move_station_tables(cfg)
    cfg = load_config(path, environ={})
    cfg.index.library_dir.mkdir(parents=True, exist_ok=True)
    cfg.index.shared_dir.mkdir(parents=True, exist_ok=True)
    (cfg.index.stations_dir / "default").mkdir(parents=True, exist_ok=True)
    cfg.index.model_dir.mkdir(parents=True, exist_ok=True)
    if missing_station_folder:
        shutil.rmtree(cfg.index.stations_dir / "nightowl")
    return cfg


def _owl(cfg: AutoDJConfig) -> Path:
    return cfg.index.stations_dir / "nightowl"


def _checks(report: DoctorReport, station: str | None) -> dict[str, DoctorCheck]:
    return {check.name: check for check in report.checks if check.station == station}


OBSOLETE = {
    "schema_version": 2,
    "playback": {"repeat_rules": {"related_artists_file": "", "title": {"minutes": 460}}},
    "eq": {"low": 1.5},
}


def test_each_station_gets_its_own_checks(tmp_path: Path) -> None:
    cfg = _config(tmp_path)

    report = run_doctor(cfg, python_version=(3, 14))

    owl = [check.name for check in report.checks if check.station == "nightowl"]
    assert owl == STATION_CHECKS
    # The default station's checks keep their names and carry no station.
    default = _checks(report, None)
    assert {"saved-settings", "stream", "time-checks", "recording", "liners"} <= set(default)
    # The default station's own folder is never an unused one.
    assert (cfg.index.stations_dir / "default").is_dir()
    assert default["station-folders"].status is CheckStatus.PASS
    last = run_doctor(cfg, python_version=(3, 14)).checks[-1]
    assert (last.name, last.status) == ("station-folders", CheckStatus.PASS)


def test_a_station_check_reads_that_station_only(tmp_path: Path) -> None:
    cfg = _config(tmp_path)
    _owl(cfg).mkdir(parents=True, exist_ok=True)
    (_owl(cfg) / "web_state.json").write_text(json.dumps(OBSOLETE), encoding="utf-8")

    report = run_doctor(cfg, python_version=(3, 14))

    owl, default = _checks(report, "nightowl"), _checks(report, None)
    assert owl["saved-settings"].status is CheckStatus.WARN
    assert owl["saved-settings"].repair == "repair-saved-settings"
    assert str(_owl(cfg) / "web_state.json") in str(owl["saved-settings"].detail)
    assert default["saved-settings"].status is CheckStatus.PASS
    assert owl["station-folder"].status is CheckStatus.PASS
    # Another station always streams.
    assert owl["stream"].summary != "stream mode off"
    assert default["stream"].summary == "stream mode off"


def test_text_and_json_name_the_station(tmp_path: Path) -> None:
    cfg = _config(tmp_path)

    report = run_doctor(cfg, python_version=(3, 14))

    text = render_text(report)
    assert "] Station nightowl, saved-settings: " in text
    assert "] saved-settings: " in text
    items = report.to_dict()["checks"]
    assert {item.get("station") for item in items} == {None, "nightowl"}
    assert all("station" not in item for item in items if item["name"] == "python")


@pytest.mark.parametrize(
    ("station", "owl_checks", "default_checks"),
    [("nightowl", True, False), ("default", False, True), (None, True, True)],
)
def test_station_filter(
    tmp_path: Path, station: str | None, owl_checks: bool, default_checks: bool
) -> None:
    cfg = _config(tmp_path)

    names = {
        (check.station, check.name)
        for check in run_doctor(cfg, python_version=(3, 14), station=station).checks
    }

    assert (("nightowl", "saved-settings") in names) is owl_checks
    assert ((None, "saved-settings") in names) is default_checks
    assert ((None, "dmca") in names) is default_checks
    # The server's own checks always run.
    assert {(None, "python"), (None, "index"), (None, "config-keys")} <= names


def test_station_filter_includes_only_selected_station_speech_and_categories(
    tmp_path: Path,
) -> None:
    from autodj.stations import StationPaths

    cfg = _config(tmp_path)
    root = StationPaths.for_station(cfg, "nightowl").speech
    root.mkdir(parents=True)
    (root / "speech.json").write_text("{")

    checks = {
        (check.station, check.name): check
        for check in run_doctor(cfg, python_version=(3, 14), station="nightowl").checks
    }

    assert checks[("nightowl", "speech")].status is CheckStatus.FAIL
    assert checks[("nightowl", "time-checks")].summary.startswith("disabled")
    assert ("nightowl", "categories") in checks
    assert (None, "speech") not in checks
    assert (None, "categories") not in checks


def test_station_relay_reads_its_own_status_file(tmp_path: Path) -> None:
    cfg = _config(
        tmp_path,
        """
[stations.nightowl.stream.relays.main]
type = "icecast"
host = "owl.example.net"
mount = "/owl"
password = "secret"
""",
    )
    _owl(cfg).mkdir(parents=True, exist_ok=True)
    (_owl(cfg) / "relay-status.json").write_text(
        json.dumps(
            {
                "updated": "2026-10-13T00:00:00Z",
                "relays": [{"name": "main", "state": "retrying", "error": "refused"}],
            }
        ),
        encoding="utf-8",
    )

    with patch("autodj.doctor.socket.create_connection", side_effect=OSError("down")):
        report = run_doctor(cfg, python_version=(3, 14))

    relay = _checks(report, "nightowl")["relay-main"]
    assert relay.status is CheckStatus.WARN
    assert "retrying" in str(relay.detail) and "refused" in str(relay.detail)
    assert "relay-main" not in _checks(report, None)


def test_dmca_conflict_names_the_station_table(tmp_path: Path) -> None:
    cfg = _config(
        tmp_path,
        "\n[stations.nightowl.dmca]\nenabled = true\n"
        "\n[stations.nightowl.nowplaying]\nshow_next = true\n",
    )

    check = _checks(run_doctor(cfg, python_version=(3, 14)), "nightowl")["dmca"]

    assert check.status is CheckStatus.WARN
    assert "[stations.nightowl.nowplaying] show_next" in str(check.detail)


def test_recording_folder_that_is_a_file_warns(tmp_path: Path) -> None:
    cfg = _config(tmp_path)
    (tmp_path / "recordings").mkdir()
    (tmp_path / "recordings" / "nightowl").write_text("not a folder", encoding="utf-8")

    owl = _checks(run_doctor(cfg, python_version=(3, 14)), "nightowl")

    assert owl["recording"].status is CheckStatus.WARN
    assert "[stations.nightowl.recording] folder" in str(owl["recording"].detail)
    assert _checks(run_doctor(cfg, python_version=(3, 14)), None)["recording"].status is (
        CheckStatus.PASS
    )


def test_unknown_station_keys_are_warned_about_and_ignored(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.WARNING):
        cfg = _config(
            tmp_path,
            "\n[stations.nightowl.playback]\ncrossfade_secs = 9\n"
            "\n[stations.nightowl.server]\nport = 9000\n",
        )
    # Loading ignores both and says so.
    assert "server" in caplog.text
    assert cfg.stations[0].cfg.playback.crossfade_seconds == 3.0

    check = next(
        c for c in run_doctor(cfg, python_version=(3, 14)).checks if c.name == "config-keys"
    )

    assert check.status is CheckStatus.WARN
    assert isinstance(check.detail, dict)
    listed = check.detail[str(cfg.config_path)]
    assert listed == [
        "stations.nightowl.playback.crossfade_secs",
        "[stations.nightowl.server]",
    ]


def test_config_keys_pass_without_unknown_keys(tmp_path: Path) -> None:
    cfg = _config(tmp_path)
    check = next(
        c for c in run_doctor(cfg, python_version=(3, 14)).checks if c.name == "config-keys"
    )
    assert check.status is CheckStatus.PASS


def test_a_folder_of_a_station_no_longer_configured_is_reported_not_removed(
    tmp_path: Path,
) -> None:
    cfg = _config(tmp_path)
    old = cfg.index.stations_dir / "oldies"
    old.mkdir(parents=True)
    (old / "schedule.json").write_text("{}", encoding="utf-8")
    # A leftover restore stage is hidden and not a station.
    (cfg.index.stations_dir / ".nightowl.restore-1").mkdir()

    check = run_doctor(cfg, python_version=(3, 14)).checks[-1]

    assert (check.name, check.status) == ("station-folders", CheckStatus.WARN)
    assert check.summary == "not in the configuration: oldies"
    assert (old / "schedule.json").exists()


def _cli(cfg: AutoDJConfig, args: list[str], answer: str = "", tty: bool = False):
    with (
        patch("autodj.cli._load_cfg_or_exit", return_value=cfg),
        patch(
            "autodj.doctor._model_cache_check",
            return_value=DoctorCheck("model-cache", CheckStatus.PASS, "ok"),
        ),
        patch(
            "autodj.doctor_commands.run_recovery_commands",
            side_effect=lambda cfg, report, **kwargs: (report, False),
        ),
        patch("click.testing._NamedTextIOWrapper.isatty", return_value=tty),
    ):
        return CliRunner().invoke(cli, ["doctor", *args], input=answer)


def test_cli_refuses_an_unknown_station(tmp_path: Path) -> None:
    cfg = _config(tmp_path)
    result = _cli(cfg, ["--station", "oldies"])
    assert result.exit_code == 2
    assert "no station is named 'oldies'" in result.output


def test_cli_dry_run_changes_nothing(tmp_path: Path) -> None:
    cfg = _config(tmp_path, missing_station_folder=True)
    result = _cli(cfg, ["--no-fix", "--station", "nightowl"], tty=True)
    assert "Station nightowl, station-folder:" in result.output
    assert "Create missing" not in result.output
    assert not _owl(cfg).exists()


def test_cli_json_is_read_only_and_names_the_station(tmp_path: Path) -> None:
    cfg = _config(tmp_path, missing_station_folder=True)
    result = _cli(cfg, ["--json", "--station", "nightowl"], tty=True)
    data = json.loads(result.output)
    assert {"station": "nightowl", "name": "station-folder"}.items() <= next(
        item for item in data["checks"] if item.get("station")
    ).items()
    assert "Welcome to AutoDJ" not in result.output
    assert not _owl(cfg).exists()


def test_cli_welcomes_before_checking_in_human_output(tmp_path: Path) -> None:
    cfg = _config(tmp_path)
    report = DoctorReport((DoctorCheck("example", CheckStatus.PASS, "healthy"),))
    with (
        patch("autodj.cli._load_cfg_or_exit", return_value=cfg),
        patch("autodj.doctor.run_doctor", return_value=report),
    ):
        result = CliRunner().invoke(cli, ["doctor", "--no-fix"])

    assert result.exit_code == 0
    assert result.output.startswith("Welcome to AutoDJ ")
    assert result.output.index("Checking your AutoDJ") < result.output.index("[PASS] example")


def test_format_text_wraps_words_without_breaking_paths_or_explicit_lines() -> None:
    text = "Run this command at C:\\some\\long\\path\nThen check the result"

    assert format_text(text, width=17, subsequent_indent="  ") == (
        "Run this command\n  at\n  C:\\some\\long\\path\nThen check the\n  result"
    )


@pytest.mark.parametrize(("answer", "made"), [("n\n", False), ("y\n", True)])
def test_cli_makes_the_station_folder_only_with_consent(
    tmp_path: Path, answer: str, made: bool
) -> None:
    cfg = _config(tmp_path, missing_station_folder=True)
    result = _cli(cfg, ["--station", "nightowl"], answer=answer, tty=True)
    assert f"Create missing folder for station nightowl: {_owl(cfg)}" in result.output
    assert _owl(cfg).is_dir() is made


@pytest.mark.parametrize(
    ("args", "answer", "repaired"), [([], "n\n", False), ([], "y\n", True), (["--fix"], "", True)]
)
def test_cli_repairs_a_station_saved_settings_with_consent_and_a_backup(
    tmp_path: Path, args: list[str], answer: str, repaired: bool
) -> None:
    cfg = _config(tmp_path)
    _owl(cfg).mkdir(parents=True, exist_ok=True)
    state = _owl(cfg) / "web_state.json"
    original = json.dumps(OBSOLETE).encode()
    state.write_bytes(original)
    default_state = (cfg.index.stations_dir / "default") / "web_state.json"
    default_state.write_bytes(original)

    result = _cli(cfg, [*args, "--station", "nightowl"], answer=answer, tty=True)

    assert f"Remove obsolete repeat keys from {state}" in result.output
    assert (state.read_bytes() != original) is repaired
    backups = list(_owl(cfg).glob("*.bak"))
    if repaired:
        assert [backup.read_bytes() for backup in backups] == [original]
        saved = json.loads(state.read_text(encoding="utf-8"))
        assert saved["eq"] == {"low": 1.5}
        assert "related_artists_file" not in saved["playback"]["repeat_rules"]
    else:
        assert backups == []
    # The filter keeps the default station's file out of it.
    assert default_state.read_bytes() == original


def test_station_filter_runs_no_server_wide_repairs(tmp_path: Path) -> None:
    cfg = _config(tmp_path)
    stray = (cfg.index.stations_dir / "default") / f".liners.old-{'d' * 32}"
    stray.mkdir()
    with (
        patch("autodj.cli._load_cfg_or_exit", return_value=cfg),
        patch(
            "autodj.doctor._model_cache_check",
            return_value=DoctorCheck("model-cache", CheckStatus.PASS, "ok"),
        ),
        patch("autodj.doctor_commands.run_recovery_commands") as recovery,
        patch("autodj.doctor_repair.plan_unknown_key_repairs") as key_repairs,
        patch("autodj.backup.recover_restore") as recover,
        patch("click.testing._NamedTextIOWrapper.isatty", return_value=True),
    ):
        CliRunner().invoke(cli, ["doctor", "--fix", "--station", "nightowl"], input="y\ny\ny\n")
    recovery.assert_not_called()
    key_repairs.assert_not_called()
    recover.assert_not_called()
    assert stray.exists()


def _interrupted_restore(cfg: AutoDJConfig) -> Path:
    """A journal for a restore that moved web_state.json aside and stopped."""
    state = (cfg.index.stations_dir / "default") / "web_state.json"
    aside = (cfg.index.stations_dir / "default") / f".web_state.json.old-{'e' * 32}"
    aside.write_text('{"old": true}', encoding="utf-8")
    journal = {
        "version": 1,
        "state": "installing",
        "stages": [],
        "steps": [{"op": "aside", "path": str(state), "aside": str(aside)}],
        "leftovers": [],
    }
    (cfg.index.library_dir / ".autodj-restore.json").parent.mkdir(parents=True, exist_ok=True)
    (cfg.index.library_dir / ".autodj-restore.json").write_text(
        json.dumps(journal), encoding="utf-8"
    )
    return state


@pytest.mark.parametrize(("answer", "rolled_back"), [("n\n", False), ("y\n", True)])
def test_doctor_rolls_back_an_interrupted_restore_only_when_asked(
    tmp_path: Path, answer: str, rolled_back: bool
) -> None:
    cfg = _config(tmp_path)
    state = _interrupted_restore(cfg)
    result = _cli(cfg, ["--fix"], answer=answer, tty=True)
    assert "A restore stopped part way." in result.output
    assert "Roll back the interrupted restore?" in result.output
    assert state.exists() is rolled_back
    if rolled_back:
        assert json.loads(state.read_text(encoding="utf-8")) == {"old": True}
        assert not (cfg.index.library_dir / ".autodj-restore.json").exists()


def test_doctor_never_rolls_back_unasked_outside_a_terminal(tmp_path: Path) -> None:
    cfg = _config(tmp_path)
    state = _interrupted_restore(cfg)
    result = _cli(cfg, ["--fix"], tty=False)
    assert "run `autodj doctor` in a terminal" in result.output
    assert not state.exists()
    assert result.exit_code == 1
