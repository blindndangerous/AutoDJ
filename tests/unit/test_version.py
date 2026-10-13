import importlib.metadata
import json
import subprocess
import sys
import tomllib
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

import autodj
from autodj.config import TimeChecksConfig
from autodj.server import _version_info, create_app
from autodj.version import (
    _project_version,
    _source_pyproject,
    current_version,
)

ROOT = Path(__file__).resolve().parents[2]


def run_isolated_import(site: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            sys.executable,
            "-S",
            "-c",
            (
                "import sys; "
                f"sys.path.insert(0, {str(site)!r}); "
                "import autodj; print(autodj.__version__)"
            ),
        ],
        capture_output=True,
        text=True,
        check=False,
    )


def copy_version_package(destination: Path) -> None:
    package = destination / "autodj"
    package.mkdir(parents=True)
    for name in ("__init__.py", "version.py"):
        (package / name).write_bytes((ROOT / "src" / "autodj" / name).read_bytes())


def project_version() -> str:
    with (ROOT / "pyproject.toml").open("rb") as fh:
        return str(tomllib.load(fh)["project"]["version"])


def test_python_version_derives_from_project_metadata() -> None:
    expected = project_version()
    assert current_version() == expected
    assert autodj.__version__ == expected


def test_source_pyproject_rejects_installed_layout(tmp_path: Path, monkeypatch) -> None:
    import autodj.version as version_module

    installed_module = tmp_path / "site" / "autodj" / "version.py"
    installed_module.parent.mkdir(parents=True)
    installed_module.write_text("", encoding="utf-8")
    monkeypatch.setattr(version_module, "__file__", str(installed_module))

    assert _source_pyproject() is None


def test_source_pyproject_handles_path_without_checkout_parent(monkeypatch) -> None:
    import autodj.version as version_module

    class ShallowModulePath:
        parents: tuple[()] = ()

    class FakePath:
        def __init__(self, _value: str) -> None:
            pass

        def resolve(self) -> ShallowModulePath:
            return ShallowModulePath()

    monkeypatch.setattr(version_module, "Path", FakePath)

    assert _source_pyproject() is None


@pytest.mark.parametrize(
    "project",
    [
        "[project]\nname = 'other'\nversion = '1.0'\n",
        "[project]\nname = 'autodj'\nversion = ''\n",
        "[project]\nname = 'autodj'\nversion = [1]\n",
        "[tool.other]\nvalue = 1\n",
    ],
)
def test_project_version_rejects_invalid_metadata(tmp_path: Path, project: str) -> None:
    metadata = tmp_path / "pyproject.toml"
    metadata.write_text(project, encoding="utf-8")

    with pytest.raises(RuntimeError, match="Unable to read AutoDJ version"):
        _project_version(metadata)


@pytest.mark.parametrize(
    ("metadata", "message"),
    [
        (importlib.metadata.PackageNotFoundError("autodj"), "version unavailable"),
        (ValueError("corrupt metadata"), "metadata is invalid"),
        ("", "expected a non-empty string"),
    ],
)
def test_installed_version_failures_are_actionable(monkeypatch, metadata, message: str) -> None:
    import autodj.version as version_module

    current_version.cache_clear()
    monkeypatch.setattr(version_module, "_source_pyproject", lambda: None)
    if isinstance(metadata, BaseException):
        monkeypatch.setattr(
            version_module.importlib.metadata,
            "version",
            lambda _name: (_ for _ in ()).throw(metadata),
        )
    else:
        monkeypatch.setattr(version_module.importlib.metadata, "version", lambda _name: metadata)
    try:
        with pytest.raises(RuntimeError, match=message):
            current_version()
    finally:
        current_version.cache_clear()


def test_installed_version_is_returned(monkeypatch) -> None:
    import autodj.version as version_module

    current_version.cache_clear()
    monkeypatch.setattr(version_module, "_source_pyproject", lambda: None)
    monkeypatch.setattr(version_module.importlib.metadata, "version", lambda _name: "2.3.4")
    try:
        assert current_version() == "2.3.4"
    finally:
        current_version.cache_clear()


def test_api_version_derives_from_project_metadata(tmp_path: Path) -> None:
    bridge = MagicMock()
    bridge.player._cfg.server.station_timezone = "UTC"
    bridge.player._cfg.index.active_dir = tmp_path
    bridge.player._cfg.time_checks = TimeChecksConfig()
    assert create_app(bridge).version == current_version()


def test_version_endpoint_uses_same_accessor() -> None:
    _version_info.cache_clear()
    try:
        with patch("autodj.server.current_version", return_value="9.8.7"):
            assert _version_info()["version"] == "9.8.7"
    finally:
        _version_info.cache_clear()


REPO_COMMIT = "https://github.com/blindndangerous/AutoDJ/commit/"


@pytest.fixture
def fresh_version_info():
    _version_info.cache_clear()
    yield _version_info
    _version_info.cache_clear()


def test_version_info_prefers_git(fresh_version_info, monkeypatch) -> None:
    monkeypatch.setenv("AUTODJ_COMMIT", "fedcba9")
    with patch("subprocess.check_output", return_value="1a2b3c4\n") as git:
        info = fresh_version_info()
    assert git.call_args.args[0] == ["git", "rev-parse", "--short", "HEAD"]
    assert info["commit"] == "1a2b3c4"
    assert info["commit_url"] == REPO_COMMIT + "1a2b3c4"


@pytest.mark.parametrize(
    "error", [FileNotFoundError("git"), subprocess.CalledProcessError(128, "git")]
)
def test_version_info_falls_back_to_build_arg(fresh_version_info, monkeypatch, error) -> None:
    monkeypatch.setenv("AUTODJ_COMMIT", " 0123456789ABCDEF0123456789abcdef01234567\n")
    with patch("subprocess.check_output", side_effect=error):
        info = fresh_version_info()
    full = "0123456789abcdef0123456789abcdef01234567"
    assert info["commit"] == full
    assert info["commit_url"] == REPO_COMMIT + full


def test_version_info_without_git_or_build_arg_is_unknown(fresh_version_info, monkeypatch) -> None:
    monkeypatch.delenv("AUTODJ_COMMIT", raising=False)
    with patch("subprocess.check_output", side_effect=FileNotFoundError("git")):
        info = fresh_version_info()
    assert info["commit"] == "unknown"
    assert "commit_url" not in info


@pytest.mark.parametrize(
    "value",
    [
        "",
        "abc123",  # six digits: too short
        "a" * 41,  # longer than a SHA-1
        "1a2b3c4g",
        "1a2b3c4/../../evil",
        "1a2b3c4?x=https://evil.example",
        "1a2b3c4\nSet-Cookie: x=1",
        "<script>alert(1)</script>",
        "$(rm -rf /)",
        "１２３４５６７",  # full-width digits are not ASCII hex
    ],
)
def test_version_info_ignores_invalid_build_arg(fresh_version_info, monkeypatch, value) -> None:
    monkeypatch.setenv("AUTODJ_COMMIT", value)
    with patch("subprocess.check_output", side_effect=FileNotFoundError("git")):
        info = fresh_version_info()
    assert info["commit"] == "unknown"
    assert "commit_url" not in info


def test_version_info_ignores_unexpected_git_output(fresh_version_info, monkeypatch) -> None:
    monkeypatch.delenv("AUTODJ_COMMIT", raising=False)
    with patch("subprocess.check_output", return_value="fatal: not a git repository\n"):
        info = fresh_version_info()
    assert info["commit"] == "unknown"
    assert "commit_url" not in info


def test_frontend_has_no_independent_product_version() -> None:
    package = json.loads((ROOT / "package.json").read_text(encoding="utf-8"))
    assert "version" not in package

    package_lock = json.loads((ROOT / "package-lock.json").read_text(encoding="utf-8"))
    assert package_lock["name"] == package["name"]
    assert package_lock["packages"][""]["name"] == package["name"]
    assert "version" not in package_lock
    assert "version" not in package_lock["packages"][""]


def test_source_checkout_ignores_stale_ambient_distribution(tmp_path: Path) -> None:
    stale = tmp_path / "stale"
    metadata = stale / "autodj-0.14.0.dist-info"
    metadata.mkdir(parents=True)
    (metadata / "METADATA").write_text(
        "Metadata-Version: 2.1\nName: autodj\nVersion: 0.14.0\n",
        encoding="utf-8",
    )
    result = subprocess.run(
        [
            sys.executable,
            "-S",
            "-c",
            (
                "import sys; "
                f"sys.path[:0] = [{str(ROOT / 'src')!r}, {str(stale)!r}]; "
                "import autodj; print(autodj.__version__)"
            ),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    expected = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"][
        "version"
    ]
    assert expected != "0.14.0", "stale fixture must differ from the real project version"
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == expected


def test_installed_package_uses_distribution_metadata(tmp_path: Path) -> None:
    site = tmp_path / "site"
    copy_version_package(site)
    metadata = site / "autodj-7.8.9.dist-info"
    metadata.mkdir()
    (metadata / "METADATA").write_text(
        "Metadata-Version: 2.1\nName: autodj\nVersion: 7.8.9\n",
        encoding="utf-8",
    )
    result = run_isolated_import(site)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "7.8.9"


def test_installed_package_without_metadata_has_actionable_error(tmp_path: Path) -> None:
    site = tmp_path / "site"
    copy_version_package(site)
    result = run_isolated_import(site)
    assert result.returncode != 0
    assert "AutoDJ version unavailable" in result.stderr
    assert "FileNotFoundError" not in result.stderr
    assert "IndexError" not in result.stderr


def test_corrupt_installed_metadata_has_actionable_error(tmp_path: Path) -> None:
    site = tmp_path / "site"
    copy_version_package(site)
    metadata = site / "autodj-7.8.9.dist-info"
    metadata.mkdir()
    (metadata / "METADATA").write_text(
        "Metadata-Version: 2.1\nName: autodj\n",
        encoding="utf-8",
    )

    result = run_isolated_import(site)

    assert result.returncode != 0
    assert "AutoDJ installed version metadata is invalid" in result.stderr


def test_malformed_source_metadata_has_actionable_error(tmp_path: Path) -> None:
    root = tmp_path / "checkout"
    copy_version_package(root / "src")
    (root / "pyproject.toml").write_text(
        "[project]\nname = 'autodj'\nversion = [\n",
        encoding="utf-8",
    )
    result = run_isolated_import(root / "src")
    assert result.returncode != 0
    assert "Unable to read AutoDJ version" in result.stderr
