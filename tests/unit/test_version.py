import importlib.metadata
import json
import os
import subprocess
import sys
import tomllib
from pathlib import Path
from unittest.mock import patch

import pytest

import autodj
from autodj.server import _validated_bundle_version, _version_info
from autodj.version import _project_version, _source_pyproject, current_version

ROOT = Path(__file__).resolve().parents[2]
REQUIRED_BUILT_ASSETS = (
    "index.html",
    "app.js",
    "app.css",
    "bitcrusher-worklet.js",
    "stutter-worklet.js",
    "freeze-worklet.js",
    "glitch-worklet.js",
)


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


def write_required_built_assets(directory: Path) -> None:
    for name in REQUIRED_BUILT_ASSETS:
        (directory / name).write_text(name, encoding="utf-8")


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


def test_version_endpoint_uses_same_accessor() -> None:
    _version_info.cache_clear()
    try:
        with patch("autodj.server.current_version", return_value="9.8.7"):
            assert _version_info()["version"] == "9.8.7"
    finally:
        _version_info.cache_clear()


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
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "0.15.0"


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


def test_built_bundle_version_must_match_runtime(tmp_path: Path) -> None:
    write_required_built_assets(tmp_path)
    stamp = tmp_path / "build-info.json"
    stamp.write_text('{"version":"0.14.0"}\n', encoding="utf-8")

    with pytest.raises(RuntimeError, match=r"bundle version 0\.14\.0.*runtime version 0\.15\.0"):
        _validated_bundle_version(tmp_path, "0.15.0")


@pytest.mark.parametrize(
    ("contents", "message"),
    [
        (None, "missing build-info.json"),
        ("not json", "invalid build-info.json"),
        ('{"version": ""}', "non-empty string version"),
    ],
)
def test_built_bundle_stamp_failures_are_actionable(
    tmp_path: Path, contents: str | None, message: str
) -> None:
    write_required_built_assets(tmp_path)
    if contents is not None:
        (tmp_path / "build-info.json").write_text(contents, encoding="utf-8")

    with pytest.raises(RuntimeError, match=message):
        _validated_bundle_version(tmp_path, "0.15.0")


def test_built_bundle_stamp_rejects_invalid_utf8(tmp_path: Path) -> None:
    write_required_built_assets(tmp_path)
    (tmp_path / "build-info.json").write_bytes(b"\xff")

    with pytest.raises(RuntimeError, match=r"invalid build-info\.json"):
        _validated_bundle_version(tmp_path, "0.15.0")


def test_source_assets_do_not_require_bundle_stamp(tmp_path: Path) -> None:
    assert _validated_bundle_version(tmp_path, "0.15.0") is None


def test_version_timestamp_ignores_partial_built_bundle(tmp_path: Path, monkeypatch) -> None:
    import autodj.server as server

    source = tmp_path / "static"
    built = tmp_path / "static_dist"
    source.mkdir()
    built.mkdir()
    (source / "index.html").write_text("source", encoding="utf-8")
    source_app = source / "app.js"
    source_app.write_text("source", encoding="utf-8")
    built_app = built / "app.js"
    built_app.write_text("partial", encoding="utf-8")
    os.utime(source_app, (1_700_000_000, 1_700_000_000))
    os.utime(built_app, (1_710_000_000, 1_710_000_000))
    monkeypatch.setattr(server, "_PACKAGE_DIR", tmp_path)
    server._version_info.cache_clear()
    try:
        with patch("autodj.server.current_version", return_value="0.15.0"):
            assert _version_info()["built_at"].startswith("2023-11-14T22:13:20")
    finally:
        server._version_info.cache_clear()
