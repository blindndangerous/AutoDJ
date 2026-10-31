"""Missing runtime packages must leave diagnosis available, never partial serving."""

import importlib
import json
import sys
from importlib import metadata
from unittest.mock import Mock, patch

import pytest
from click.testing import CliRunner

from autodj.cli import cli
from autodj.config import load_config


@pytest.fixture
def without_crypto(monkeypatch):
    import autodj

    for name in list(sys.modules):
        if name == "cryptography" or name.startswith("cryptography."):
            monkeypatch.delitem(sys.modules, name)
    monkeypatch.setitem(sys.modules, "cryptography", None)
    for name in ("autodj.certificates", "autodj.certificates_api"):
        attribute = name.rsplit(".", 1)[1]
        # Importing anew also replaces the parent package's attribute; restore both.
        monkeypatch.setattr(autodj, attribute, getattr(autodj, attribute, None), raising=False)
        monkeypatch.delitem(sys.modules, name, raising=False)
    version = metadata.version

    def installed(name):
        if name == "cryptography":
            raise metadata.PackageNotFoundError(name)
        return version(name)

    monkeypatch.setattr(metadata, "version", installed)


def config_file(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text(f"[index]\nindex_dir = '{tmp_path.as_posix()}/index'\n")
    return path


@pytest.mark.parametrize("selected", [False, True])
def test_config_keeps_saved_https_without_cryptography(tmp_path, without_crypto, selected):
    """ADR-0019-06: maintenance preserves the saved HTTPS selection."""
    path = config_file(tmp_path)
    ident = "a" * 32
    root = tmp_path / "index" / ".tls"
    if selected:
        root.mkdir(parents=True)
        (root / "inventory.json").write_text(
            json.dumps(
                {
                    "version": 1,
                    "active": ident,
                    "certificates": [{"id": ident, "name": "Local", "hosts": ["localhost"]}],
                }
            )
        )
    cfg = load_config(path, environ={})
    assert cfg.server.ssl_certfile == (str(root / ident / "cert.pem") if selected else None)
    assert cfg.server.ssl_keyfile == (str(root / ident / "key.pem") if selected else None)
    importlib.import_module("autodj.certificates_api")


def test_serve_refuses_before_configuration_or_server_start(without_crypto):
    """ADR-0019-06: required packages gate serving before any startup side effects."""
    with patch("autodj.cli._load_cfg_or_exit") as load:
        result = CliRunner().invoke(cli, ["serve"])
    assert result.exit_code == 1
    assert result.output == (
        "Error: Missing or too-old required Python packages: cryptography.\n"
        "Run `autodj doctor --fix` to install the missing packages.\n"
    )
    load.assert_not_called()


@pytest.mark.parametrize("flags", [["--json"], ["--no-fix"]])
def test_doctor_runs_without_crypto_and_never_repairs(tmp_path, without_crypto, flags):
    path = config_file(tmp_path)
    spill = tmp_path / "index" / "cache" / "spill" / "autodj-audio-stale"
    spill.parent.mkdir(parents=True)
    spill.write_bytes(b"preserve during diagnosis")
    before = {p.relative_to(tmp_path): p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()}
    with patch("autodj.launcher.run_setup") as setup:
        result = CliRunner().invoke(cli, ["--config", str(path), "doctor", "--offline", *flags])
    assert result.exit_code == 1, result.exception
    assert "cryptography" in result.output
    if "--json" in flags:
        checks = json.loads(result.stdout)["checks"]
        check = next(c for c in checks if c["name"] == "runtime-packages")
        assert check["repair"] == "sync-packages"
        assert check["status"] == "fail"
    else:
        assert "autodj setup" in result.output
    setup.assert_not_called()
    if "--json" in flags:
        assert before == {
            p.relative_to(tmp_path): p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()
        }


@pytest.fixture
def runtime(tmp_path, monkeypatch):
    from autodj import runtime_packages as packages

    (tmp_path / ".uv").mkdir()
    (tmp_path / "scripts").mkdir()
    (tmp_path / "scripts" / "setup.py").touch()
    (tmp_path / "uv.lock").write_text("""
[[package]]
name = "autodj"
[package.metadata]
requires-dist = [
  {name = "cryptography", specifier = ">=46.0.0"},
  {name = "sounddevice", specifier = ">=0.4.6", marker = "extra == 'play'"},
  {name = "torch", specifier = ">=2.2.0", marker = "extra == 'all'"},
]
""")
    monkeypatch.setattr(packages, "ROOT", tmp_path)
    return packages, tmp_path


def save_profile(root, *, backend="no-model", external=False, environment=None):
    (root / ".uv" / "setup.json").write_text(
        json.dumps(
            {
                "backend": backend,
                "external": external,
                "environment": environment or sys.prefix,
            }
        )
    )


@pytest.mark.parametrize(
    "version,broken",
    [
        (None, True),
        ("45.0.7", True),
        ("46.0.0", False),
        ("46.0.0rc1", True),
        ("46.0.0+local", False),
    ],
)
def test_runtime_versions_are_checked_offline(runtime, monkeypatch, version, broken):
    packages, _ = runtime
    if version is None:
        monkeypatch.setattr(metadata, "version", Mock(side_effect=metadata.PackageNotFoundError))
    else:
        monkeypatch.setattr(metadata, "version", lambda name: version)
    with patch("subprocess.run", side_effect=AssertionError("must not execute")):
        problems = packages.missing_runtime_packages()
    assert bool(problems) == broken
    if broken:
        assert problems[0].name == "cryptography"


@pytest.mark.parametrize(
    "backend,expected",
    [
        (None, {"cryptography"}),
        ("no-model", {"cryptography", "sounddevice"}),
        ("nvidia", {"cryptography", "torch"}),
    ],
)
def test_requirements_follow_only_this_runtime_profile(runtime, monkeypatch, backend, expected):
    packages, root = runtime
    if backend:
        save_profile(root, backend=backend)
    monkeypatch.setattr(metadata, "version", Mock(side_effect=metadata.PackageNotFoundError))
    assert {p.name for p in packages.missing_runtime_packages(include_extras=True)} == expected
    assert {p.name for p in packages.missing_runtime_packages()} == {"cryptography"}
    save_profile(root, environment=str(root / "another-env"))
    assert {p.name for p in packages.missing_runtime_packages()} == {"cryptography"}


@pytest.mark.parametrize("external", [False, True])
@pytest.mark.parametrize("auto_fix", [False, True])
def test_sync_repair_consent_and_recheck(runtime, monkeypatch, capsys, external, auto_fix):
    from autodj.doctor import DoctorReport, _runtime_packages_check
    from autodj.doctor_commands import run_recovery_commands

    _, root = runtime
    save_profile(root, external=external)
    monkeypatch.setattr(metadata, "version", Mock(side_effect=metadata.PackageNotFoundError))
    initial = DoctorReport((_runtime_packages_check(),))
    cfg = load_config(config_file(root), environ={})
    healthy = DoctorReport(())
    with (
        patch("autodj.doctor_commands.click.confirm", return_value=True) as confirm,
        patch("autodj.launcher.run_setup", return_value=0) as run,
        patch("autodj.doctor.run_doctor", return_value=healthy) as recheck,
    ):
        report, failed = run_recovery_commands(cfg, initial, auto_fix=auto_fix)
    args = ["--yes", "--model", "no"]
    if external:
        args.append("--manage-environment")
    run.assert_called_once_with(args)
    assert confirm.call_count == int(not auto_fix)
    recheck.assert_called_once()
    assert report is healthy and not failed
    output = capsys.readouterr().out
    assert "may download packages" in output
    if external:
        assert "permanently" in output
        if auto_fix:
            assert "--fix" in output and "missing" in output


def test_unknown_runtime_repair_prints_command_without_running(runtime, monkeypatch, capsys):
    from autodj.doctor import DoctorReport, _runtime_packages_check
    from autodj.doctor_commands import run_recovery_commands

    _, root = runtime
    monkeypatch.setattr(metadata, "version", Mock(side_effect=metadata.PackageNotFoundError))
    cfg = load_config(config_file(root), environ={})
    with patch("autodj.launcher.run_setup") as run:
        run_recovery_commands(cfg, DoctorReport((_runtime_packages_check(),)), auto_fix=True)
    run.assert_not_called()
    output = capsys.readouterr().out
    assert "autodj setup --environment" in output
    assert "--manage-environment" in output
    assert "cannot determine" in output


@pytest.mark.parametrize("entrypoint", ["serve", "create_app"])
def test_direct_server_entrypoints_refuse_missing_packages(without_crypto, entrypoint):
    from autodj import server

    with (
        patch("autodj.server.StationRegistry") as stations,
        pytest.raises(RuntimeError, match="cryptography"),
    ):
        if entrypoint == "serve":
            server.serve(Mock(), Mock(), None)
        else:
            server.create_app(services=Mock(), default_station=Mock())
    stations.assert_not_called()


@pytest.mark.parametrize("auto_fix", [False, True])
def test_doctor_cli_repairs_packages(runtime, monkeypatch, auto_fix):
    from autodj.doctor import DoctorReport, _runtime_packages_check

    _, root = runtime
    save_profile(root, external=True)
    monkeypatch.setattr(metadata, "version", Mock(side_effect=metadata.PackageNotFoundError))
    initial = DoctorReport((_runtime_packages_check(),))
    with (
        patch("click.testing._NamedTextIOWrapper.isatty", return_value=True),
        patch("autodj.launcher.run_setup", return_value=0) as setup,
        patch("autodj.doctor.run_doctor", side_effect=[initial, DoctorReport(())]) as check,
    ):
        result = CliRunner().invoke(
            cli,
            ["--config", str(config_file(root)), "doctor", *(["--fix"] if auto_fix else [])],
            input="y\n",
        )
    assert result.exit_code == 0, result.exception
    setup.assert_called_once_with(["--yes", "--model", "no", "--manage-environment"])
    assert check.call_count == 2
    assert ("Run this command now?" in result.output) is not auto_fix


@pytest.mark.parametrize("reason", ["declined", "too-old", "failed"])
def test_package_repair_does_not_run_without_consent_or_retry(runtime, monkeypatch, reason):
    from autodj.doctor import DoctorReport, _runtime_packages_check
    from autodj.doctor_commands import run_recovery_commands

    _, root = runtime
    save_profile(root, external=True)
    if reason == "too-old":
        monkeypatch.setattr(metadata, "version", lambda name: "0.0.1")
    else:
        monkeypatch.setattr(metadata, "version", Mock(side_effect=metadata.PackageNotFoundError))
    initial = DoctorReport((_runtime_packages_check(),))
    cfg = load_config(config_file(root), environ={})
    with (
        patch("autodj.doctor_commands.click.confirm", return_value=False),
        patch("autodj.launcher.run_setup", return_value=7) as setup,
        patch("autodj.doctor.run_doctor", return_value=initial) as recheck,
    ):
        report, failed = run_recovery_commands(cfg, initial, auto_fix=reason != "declined")
    assert report is initial
    assert failed is (reason in {"failed", "too-old"})
    assert setup.call_count == recheck.call_count == int(reason == "failed")


def test_wheel_metadata_fallback_does_not_guess_extras(runtime, monkeypatch):
    packages, root = runtime
    (root / "uv.lock").unlink()
    monkeypatch.setattr(
        metadata, "requires", lambda name: ["cryptography>=46.0.0", "torch>=2.2.0; extra == 'all'"]
    )
    monkeypatch.setattr(metadata, "version", Mock(side_effect=metadata.PackageNotFoundError))
    assert [p.name for p in packages.missing_runtime_packages()] == ["cryptography"]


def test_changed_requirement_syntax_is_reported_not_silently_ignored(runtime):
    from autodj.doctor import CheckStatus, _runtime_packages_check

    _, root = runtime
    path = root / "uv.lock"
    path.write_text(path.read_text().replace(">=46.0.0", "~=46.0.0"))
    check = _runtime_packages_check()
    assert check.status is CheckStatus.FAIL
    assert "Unsupported runtime requirement" in check.detail


@pytest.mark.parametrize("backend,extra", [("no-model", "sounddevice"), ("cpu", "torch")])
def test_missing_profile_extra_warns_and_offers_repair_without_blocking(
    runtime, monkeypatch, backend, extra
):
    from autodj.doctor import CheckStatus, _runtime_packages_check

    packages, root = runtime
    save_profile(root, backend=backend)

    def version(name):
        if name == extra:
            raise metadata.PackageNotFoundError(name)
        return "99.0.0"

    monkeypatch.setattr(metadata, "version", version)
    packages.require_runtime_packages()
    check = _runtime_packages_check()
    assert check.status is CheckStatus.WARN
    assert check.repair == "sync-packages"
    assert extra in check.summary and "optional" in check.summary


def test_unknown_ownership_fix_exits_nonzero_even_for_optional_repair(runtime):
    from autodj.doctor import CheckStatus, DoctorCheck, DoctorReport

    _, root = runtime
    report = DoctorReport(
        (
            DoctorCheck(
                "runtime-packages",
                CheckStatus.WARN,
                "optional packages missing",
                repair="sync-packages",
            ),
        )
    )
    with (
        patch("autodj.doctor.run_doctor", return_value=report),
        patch("autodj.launcher.run_setup") as setup,
    ):
        result = CliRunner().invoke(
            cli, ["--config", str(config_file(root)), "doctor", "--fix", "--offline"]
        )
    assert result.exit_code != 0
    assert "autodj setup --environment" in result.output
    assert "cannot determine" in result.output
    setup.assert_not_called()


def test_wheel_installation_explicitly_reports_manual_only_repair(runtime, monkeypatch):
    from autodj.doctor import _runtime_packages_check

    packages, root = runtime
    (root / "uv.lock").unlink()
    (root / "scripts/setup.py").unlink()
    save_profile(root)
    monkeypatch.setattr(metadata, "requires", lambda name: ["cryptography>=46.0.0"])
    monkeypatch.setattr(metadata, "version", Mock(side_effect=metadata.PackageNotFoundError))
    assert packages.runtime_selection() is None
    with pytest.raises(RuntimeError, match=r"wheel installation.*manual"):
        packages.require_runtime_packages()
    check = _runtime_packages_check()
    assert "wheel installation" in check.detail and "manual" in check.detail
    assert packages.package_repair()[2] is False


@pytest.mark.parametrize("as_json", [False, True])
def test_missing_package_keeps_independent_doctor_checks(tmp_path, without_crypto, as_json):
    path = config_file(tmp_path)
    cert, key = tmp_path / "cert.pem", tmp_path / "key.pem"
    cert.write_text("unavailable parser")
    key.write_text("unavailable parser")
    path.write_text(
        path.read_text()
        + f"[server]\nssl_certfile = '{cert.as_posix()}'\nssl_keyfile = '{key.as_posix()}'\n"
    )
    result = CliRunner().invoke(
        cli, ["--config", str(path), "doctor", "--offline", "--json" if as_json else "--no-fix"]
    )
    assert result.exit_code == 1, result.exception
    if as_json:
        checks = {check["name"]: check for check in json.loads(result.stdout)["checks"]}
        assert {
            "configuration",
            "network-safety",
            "index",
            "https",
            "runtime-packages",
        } <= checks.keys()
        assert checks["https"]["status"] == "skip"
        assert "cryptography" in checks["https"]["detail"]
    else:
        for name in ("configuration", "network-safety", "index", "https"):
            assert name + ":" in result.output
        assert "[SKIP] https" in result.output
