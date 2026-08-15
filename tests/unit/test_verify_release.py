import io
import json
import os
import shutil
import stat
import subprocess
import sys
import tarfile
import tempfile
import tomllib
import zipfile
from pathlib import Path

import pytest
import yaml

from scripts.verify_release import ReleaseVerificationError, main, verify_release

ROOT = Path(__file__).parents[2]


@pytest.fixture(autouse=True)
def _close_global_dj_cache_between_tests():
    """Override the root cache fixture: release tests never import ``autodj``."""
    yield


def _metadata(name: str, version: str, *, extra: str = "") -> bytes:
    return (f"Metadata-Version: 2.3\nName: {name}\nVersion: {version}\n{extra}").encode()


def _wheel(
    path: Path,
    version: str,
    *,
    name: str = "autodj",
    dist_info_name: str | None = None,
    extra_metadata: str = "",
    metadata_payload: bytes | None = None,
    extra_members: tuple[tuple[str | zipfile.ZipInfo, bytes], ...] = (),
) -> None:
    dist_info = dist_info_name or f"autodj-{version}.dist-info"
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr(
            f"{dist_info}/METADATA",
            metadata_payload or _metadata(name, version, extra=extra_metadata),
        )
        for member, payload in extra_members:
            zf.writestr(member, payload)


def _sdist(
    path: Path,
    version: str,
    *,
    name: str = "autodj",
    root: str | None = None,
    extra_metadata: str = "",
    extra_members: tuple[tarfile.TarInfo, ...] = (),
) -> None:
    root = root or f"autodj-{version}"
    payload = _metadata(name, version, extra=extra_metadata)
    with tarfile.open(path, "w:gz") as tf:
        info = tarfile.TarInfo(f"{root}/PKG-INFO")
        info.size = len(payload)
        tf.addfile(info, io.BytesIO(payload))
        for member in extra_members:
            tf.addfile(member)


def _release_workflow() -> dict:
    path = ROOT / ".github" / "workflows" / "release.yml"
    return yaml.load(path.read_text(encoding="utf-8"), Loader=yaml.BaseLoader)


def _matching_identity_files(tmp_path: Path) -> tuple[Path, Path, Path, Path]:
    pyproject = tmp_path / "pyproject.toml"
    changelog = tmp_path / "CHANGELOG.md"
    wheel = tmp_path / "autodj-1.2.3-py3-none-any.whl"
    sdist = tmp_path / "autodj-1.2.3.tar.gz"
    pyproject.write_text('[project]\nname="autodj"\nversion="1.2.3"\n', encoding="utf-8")
    changelog.write_text("## [1.2.3] - 2026-08-02\n", encoding="utf-8")
    _wheel(wheel, "1.2.3")
    _sdist(sdist, "1.2.3")
    return pyproject, changelog, wheel, sdist


def test_matching_tag_project_changelog_and_wheel_pass(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname="autodj"\nversion="1.2.3"\n', encoding="utf-8"
    )
    (tmp_path / "CHANGELOG.md").write_text("## [1.2.3] - 2026-08-02\n", encoding="utf-8")
    wheel = tmp_path / "autodj-1.2.3-py3-none-any.whl"
    _wheel(wheel, "1.2.3")
    sdist = tmp_path / "autodj-1.2.3.tar.gz"
    _sdist(sdist, "1.2.3")
    assert (
        verify_release(
            "v1.2.3",
            tmp_path / "pyproject.toml",
            tmp_path / "CHANGELOG.md",
            wheel,
            sdist,
        )
        == "1.2.3"
    )


@pytest.mark.parametrize(
    ("tag", "project", "heading", "wheel_version", "sdist_version"),
    [
        ("v1.2.4", "1.2.3", "1.2.3", "1.2.3", "1.2.3"),
        ("v1.2.3", "1.2.4", "1.2.3", "1.2.3", "1.2.3"),
        ("v1.2.3", "1.2.3", "1.2.4", "1.2.3", "1.2.3"),
        ("v1.2.3", "1.2.3", "1.2.3", "1.2.4", "1.2.3"),
        ("v1.2.3", "1.2.3", "1.2.3", "1.2.3", "1.2.4"),
    ],
)
def test_any_identity_mismatch_fails(
    tmp_path: Path,
    tag: str,
    project: str,
    heading: str,
    wheel_version: str,
    sdist_version: str,
) -> None:
    (tmp_path / "pyproject.toml").write_text(
        f'[project]\nname="autodj"\nversion="{project}"\n', encoding="utf-8"
    )
    (tmp_path / "CHANGELOG.md").write_text(f"## [{heading}] - 2026-08-02\n", encoding="utf-8")
    wheel = tmp_path / f"autodj-{project}-py3-none-any.whl"
    _wheel(wheel, wheel_version)
    sdist = tmp_path / f"autodj-{project}.tar.gz"
    _sdist(sdist, sdist_version)
    with pytest.raises(ReleaseVerificationError):
        verify_release(
            tag,
            tmp_path / "pyproject.toml",
            tmp_path / "CHANGELOG.md",
            wheel,
            sdist,
        )


def test_release_must_be_latest_released_changelog_heading(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname="autodj"\nversion="1.2.3"\n', encoding="utf-8"
    )
    (tmp_path / "CHANGELOG.md").write_text(
        "## [Unreleased]\n## [1.2.4] - 2026-08-02\n## [1.2.3] - 2026-08-01\n",
        encoding="utf-8",
    )
    wheel = tmp_path / "autodj-1.2.3-py3-none-any.whl"
    _wheel(wheel, "1.2.3")
    sdist = tmp_path / "autodj-1.2.3.tar.gz"
    _sdist(sdist, "1.2.3")
    with pytest.raises(ReleaseVerificationError, match="latest released CHANGELOG"):
        verify_release(
            "v1.2.3",
            tmp_path / "pyproject.toml",
            tmp_path / "CHANGELOG.md",
            wheel,
            sdist,
        )


def test_wheel_project_name_must_match_pyproject(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname="autodj"\nversion="1.2.3"\n', encoding="utf-8"
    )
    (tmp_path / "CHANGELOG.md").write_text("## [1.2.3] - 2026-08-02\n", encoding="utf-8")
    wheel = tmp_path / "different_project-1.2.3-py3-none-any.whl"
    _wheel(
        wheel,
        "1.2.3",
        name="different-project",
        dist_info_name="different_project-1.2.3.dist-info",
    )
    sdist = tmp_path / "autodj-1.2.3.tar.gz"
    _sdist(sdist, "1.2.3")

    with pytest.raises(ReleaseVerificationError, match="project names differ"):
        verify_release(
            "v1.2.3",
            tmp_path / "pyproject.toml",
            tmp_path / "CHANGELOG.md",
            wheel,
            sdist,
        )


def test_wheel_filename_must_match_verified_identity(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname="autodj"\nversion="1.2.3"\n', encoding="utf-8"
    )
    (tmp_path / "CHANGELOG.md").write_text("## [1.2.3] - 2026-08-02\n", encoding="utf-8")
    wheel = tmp_path / "different_project-1.2.3-py3-none-any.whl"
    _wheel(wheel, "1.2.3")
    sdist = tmp_path / "autodj-1.2.3.tar.gz"
    _sdist(sdist, "1.2.3")

    with pytest.raises(ReleaseVerificationError, match="wheel filename"):
        verify_release(
            "v1.2.3",
            tmp_path / "pyproject.toml",
            tmp_path / "CHANGELOG.md",
            wheel,
            sdist,
        )


def test_wheel_dist_info_directory_must_match_metadata_identity(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname="autodj"\nversion="1.2.3"\n', encoding="utf-8"
    )
    (tmp_path / "CHANGELOG.md").write_text("## [1.2.3] - 2026-08-02\n", encoding="utf-8")
    wheel = tmp_path / "autodj-1.2.3-py3-none-any.whl"
    _wheel(wheel, "1.2.3", dist_info_name="different_project-1.2.3.dist-info")
    sdist = tmp_path / "autodj-1.2.3.tar.gz"
    _sdist(sdist, "1.2.3")

    with pytest.raises(ReleaseVerificationError, match="METADATA path"):
        verify_release(
            "v1.2.3",
            tmp_path / "pyproject.toml",
            tmp_path / "CHANGELOG.md",
            wheel,
            sdist,
        )


@pytest.mark.parametrize("duplicate", ["Name: autodj\n", "Version: 1.2.3\n"])
def test_wheel_identity_headers_must_occur_once(tmp_path: Path, duplicate: str) -> None:
    pyproject, changelog, wheel, sdist = _matching_identity_files(tmp_path)
    _wheel(wheel, "1.2.3", extra_metadata=duplicate)

    with pytest.raises(ReleaseVerificationError, match="exactly one Name and Version"):
        verify_release("v1.2.3", pyproject, changelog, wheel, sdist)


@pytest.mark.parametrize(
    "filename",
    [
        "autodj-1.2.3.whl",
        "autodj-1.2.3-py3-none.whl",
        "autodj-1.2.3-py3-none-any-extra.whl",
    ],
)
def test_wheel_filename_must_have_valid_complete_structure(tmp_path: Path, filename: str) -> None:
    pyproject, changelog, _, sdist = _matching_identity_files(tmp_path)
    wheel = tmp_path / filename
    _wheel(wheel, "1.2.3")

    with pytest.raises(ReleaseVerificationError, match="valid wheel filename"):
        verify_release("v1.2.3", pyproject, changelog, wheel, sdist)


@pytest.mark.parametrize(
    "member_name",
    [
        "../escape",
        "/absolute",
        "autodj-1.2.3\\bad",
        "autodj-1.2.3//bad",
        "autodj-1.2.3/./bad",
    ],
)
def test_wheel_rejects_unsafe_member_paths(
    tmp_path: Path, member_name: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    pyproject, changelog, wheel, sdist = _matching_identity_files(tmp_path)
    if "\\" in member_name:
        monkeypatch.setattr(os, "sep", "/")
    _wheel(wheel, "1.2.3", extra_members=((member_name, b"unsafe"),))

    with pytest.raises(ReleaseVerificationError, match="safe relative POSIX paths"):
        verify_release("v1.2.3", pyproject, changelog, wheel, sdist)


def test_wheel_rejects_duplicate_member_names(tmp_path: Path) -> None:
    pyproject, changelog, wheel, sdist = _matching_identity_files(tmp_path)
    with pytest.warns(UserWarning, match="Duplicate name"):
        _wheel(
            wheel,
            "1.2.3",
            extra_members=(("autodj-1.2.3/data.txt", b"one"),) * 2,
        )

    with pytest.raises(ReleaseVerificationError, match="duplicate member name"):
        verify_release("v1.2.3", pyproject, changelog, wheel, sdist)


def test_wheel_rejects_file_directory_collision_after_path_normalization(
    tmp_path: Path,
) -> None:
    pyproject, changelog, wheel, sdist = _matching_identity_files(tmp_path)
    directory = zipfile.ZipInfo("autodj-1.2.3/data/")
    _wheel(
        wheel,
        "1.2.3",
        extra_members=((directory, b""), ("autodj-1.2.3/data", b"file")),
    )

    with pytest.raises(ReleaseVerificationError, match="normalized member path"):
        verify_release("v1.2.3", pyproject, changelog, wheel, sdist)


@pytest.mark.parametrize("ancestor_first", [True, False])
def test_wheel_rejects_explicit_file_ancestor(tmp_path: Path, ancestor_first: bool) -> None:
    pyproject, changelog, wheel, sdist = _matching_identity_files(tmp_path)
    ancestor = ("autodj-1.2.3/data", b"file")
    descendant = ("autodj-1.2.3/data/child", b"child")
    members = (ancestor, descendant) if ancestor_first else (descendant, ancestor)
    _wheel(wheel, "1.2.3", extra_members=members)

    with pytest.raises(ReleaseVerificationError, match=r"ancestor.*directory"):
        verify_release("v1.2.3", pyproject, changelog, wheel, sdist)


@pytest.mark.parametrize("file_type", [stat.S_IFLNK, stat.S_IFIFO])
def test_wheel_rejects_symlink_and_special_members(tmp_path: Path, file_type: int) -> None:
    pyproject, changelog, wheel, sdist = _matching_identity_files(tmp_path)
    special = zipfile.ZipInfo("autodj-1.2.3/special")
    special.create_system = 3
    special.external_attr = (file_type | 0o644) << 16
    _wheel(wheel, "1.2.3", extra_members=((special, b"target"),))

    with pytest.raises(ReleaseVerificationError, match="regular files and directories"):
        verify_release("v1.2.3", pyproject, changelog, wheel, sdist)


def test_wheel_rejects_oversized_metadata_before_read(tmp_path: Path) -> None:
    pyproject, changelog, wheel, sdist = _matching_identity_files(tmp_path)
    oversized = _metadata("autodj", "1.2.3") + b"X" * (1024 * 1024)
    _wheel(wheel, "1.2.3", metadata_payload=oversized)

    with pytest.raises(ReleaseVerificationError, match="METADATA exceeds 1 MiB"):
        verify_release("v1.2.3", pyproject, changelog, wheel, sdist)


@pytest.mark.parametrize("version", ["not_a_version", "01.2.3", "1.2.3RC1", "1.0alpha1"])
def test_release_version_must_be_valid_pep440(tmp_path: Path, version: str) -> None:
    pyproject = tmp_path / "pyproject.toml"
    changelog = tmp_path / "CHANGELOG.md"
    wheel = tmp_path / f"autodj-{version}-py3-none-any.whl"
    sdist = tmp_path / f"autodj-{version}.tar.gz"
    pyproject.write_text(f'[project]\nname="autodj"\nversion="{version}"\n', encoding="utf-8")
    changelog.write_text(f"## [{version}] - 2026-08-02\n", encoding="utf-8")
    _wheel(wheel, version)
    _sdist(sdist, version)

    with pytest.raises(ReleaseVerificationError, match="PEP 440"):
        verify_release(f"v{version}", pyproject, changelog, wheel, sdist)


@pytest.mark.parametrize("version", ["1!2.0", "1.2.3+local.1"])
def test_valid_pep440_raw_versions_pass(tmp_path: Path, version: str) -> None:
    pyproject = tmp_path / "pyproject.toml"
    changelog = tmp_path / "CHANGELOG.md"
    wheel = tmp_path / f"autodj-{version}-py3-none-any.whl"
    sdist = tmp_path / f"autodj-{version}.tar.gz"
    pyproject.write_text(f'[project]\nname="autodj"\nversion="{version}"\n', encoding="utf-8")
    changelog.write_text(f"## [{version}] - 2026-08-02\n", encoding="utf-8")
    _wheel(wheel, version)
    _sdist(sdist, version)

    assert verify_release(f"v{version}", pyproject, changelog, wheel, sdist) == version


def test_normalized_artifact_distribution_names_pass(tmp_path: Path) -> None:
    pyproject = tmp_path / "pyproject.toml"
    changelog = tmp_path / "CHANGELOG.md"
    wheel = tmp_path / "auto_dj-1.2.3-py3-none-any.whl"
    sdist = tmp_path / "auto_dj-1.2.3.tar.gz"
    pyproject.write_text('[project]\nname="auto.dj"\nversion="1.2.3"\n', encoding="utf-8")
    changelog.write_text("## [1.2.3] - 2026-08-02\n", encoding="utf-8")
    _wheel(wheel, "1.2.3", name="auto.dj", dist_info_name="auto_dj-1.2.3.dist-info")
    _sdist(sdist, "1.2.3", name="auto.dj", root="auto_dj-1.2.3")

    assert verify_release("v1.2.3", pyproject, changelog, wheel, sdist) == "1.2.3"


def test_sdist_root_must_match_valid_artifact_stem(tmp_path: Path) -> None:
    pyproject = tmp_path / "pyproject.toml"
    changelog = tmp_path / "CHANGELOG.md"
    wheel = tmp_path / "auto_dj-1.2.3-py3-none-any.whl"
    sdist = tmp_path / "auto_dj-1.2.3.tar.gz"
    pyproject.write_text('[project]\nname="auto.dj"\nversion="1.2.3"\n', encoding="utf-8")
    changelog.write_text("## [1.2.3] - 2026-08-02\n", encoding="utf-8")
    _wheel(wheel, "1.2.3", name="auto.dj", dist_info_name="auto_dj-1.2.3.dist-info")
    _sdist(sdist, "1.2.3", name="auto.dj", root="auto.dj-1.2.3")

    with pytest.raises(ReleaseVerificationError, match="artifact stem"):
        verify_release("v1.2.3", pyproject, changelog, wheel, sdist)


@pytest.mark.parametrize("artifact", ["wheel", "sdist"])
def test_artifact_distribution_component_must_use_canonical_underscore(
    tmp_path: Path, artifact: str
) -> None:
    pyproject = tmp_path / "pyproject.toml"
    changelog = tmp_path / "CHANGELOG.md"
    wheel_distribution = "auto.dj" if artifact == "wheel" else "auto_dj"
    sdist_distribution = "auto.dj" if artifact == "sdist" else "auto_dj"
    wheel = tmp_path / f"{wheel_distribution}-1.2.3-py3-none-any.whl"
    sdist = tmp_path / f"{sdist_distribution}-1.2.3.tar.gz"
    pyproject.write_text('[project]\nname="auto.dj"\nversion="1.2.3"\n', encoding="utf-8")
    changelog.write_text("## [1.2.3] - 2026-08-02\n", encoding="utf-8")
    _wheel(
        wheel,
        "1.2.3",
        name="auto.dj",
        dist_info_name=f"{wheel_distribution}-1.2.3.dist-info",
    )
    _sdist(
        sdist,
        "1.2.3",
        name="auto.dj",
        root=f"{sdist_distribution}-1.2.3",
    )

    with pytest.raises(ReleaseVerificationError, match="canonical artifact stem"):
        verify_release("v1.2.3", pyproject, changelog, wheel, sdist)


@pytest.mark.parametrize("duplicate", ["Name: autodj\n", "Version: 1.2.3\n"])
def test_sdist_identity_headers_must_occur_once(tmp_path: Path, duplicate: str) -> None:
    pyproject, changelog, wheel, sdist = _matching_identity_files(tmp_path)
    _sdist(sdist, "1.2.3", extra_metadata=duplicate)

    with pytest.raises(ReleaseVerificationError, match="exactly one Name and Version"):
        verify_release("v1.2.3", pyproject, changelog, wheel, sdist)


def test_sdist_must_have_exactly_one_pkg_info(tmp_path: Path) -> None:
    pyproject, changelog, wheel, sdist = _matching_identity_files(tmp_path)
    second = tarfile.TarInfo("autodj-1.2.3/nested/PKG-INFO")
    _sdist(sdist, "1.2.3", extra_members=(second,))

    with pytest.raises(ReleaseVerificationError, match="exactly one PKG-INFO"):
        verify_release("v1.2.3", pyproject, changelog, wheel, sdist)


@pytest.mark.parametrize("bad_member", ["", "../escape", "/absolute", "other-root/file"])
def test_sdist_rejects_members_outside_exact_root(tmp_path: Path, bad_member: str) -> None:
    pyproject, changelog, wheel, sdist = _matching_identity_files(tmp_path)
    _sdist(sdist, "1.2.3", extra_members=(tarfile.TarInfo(bad_member),))

    with pytest.raises(ReleaseVerificationError):
        verify_release("v1.2.3", pyproject, changelog, wheel, sdist)


def test_sdist_filename_must_match_raw_identity(tmp_path: Path) -> None:
    pyproject, changelog, wheel, _ = _matching_identity_files(tmp_path)
    sdist = tmp_path / "artifact.tar.gz"
    _sdist(sdist, "1.2.3")

    with pytest.raises(ReleaseVerificationError, match="sdist filename"):
        verify_release("v1.2.3", pyproject, changelog, wheel, sdist)


def test_sdist_rejects_link_members(tmp_path: Path) -> None:
    pyproject, changelog, wheel, sdist = _matching_identity_files(tmp_path)
    link = tarfile.TarInfo("autodj-1.2.3/link")
    link.type = tarfile.SYMTYPE
    link.linkname = "../outside"
    _sdist(sdist, "1.2.3", extra_members=(link,))

    with pytest.raises(ReleaseVerificationError, match="regular files and directories"):
        verify_release("v1.2.3", pyproject, changelog, wheel, sdist)


def test_sdist_rejects_duplicate_member_names(tmp_path: Path) -> None:
    pyproject, changelog, wheel, sdist = _matching_identity_files(tmp_path)
    duplicate = tarfile.TarInfo("autodj-1.2.3/data.txt")
    _sdist(sdist, "1.2.3", extra_members=(duplicate, duplicate))

    with pytest.raises(ReleaseVerificationError, match="duplicate member name"):
        verify_release("v1.2.3", pyproject, changelog, wheel, sdist)


def test_sdist_rejects_file_directory_collision_after_path_normalization(
    tmp_path: Path,
) -> None:
    pyproject, changelog, wheel, sdist = _matching_identity_files(tmp_path)
    directory = tarfile.TarInfo("autodj-1.2.3/data/")
    directory.type = tarfile.DIRTYPE
    file = tarfile.TarInfo("autodj-1.2.3/data")
    _sdist(sdist, "1.2.3", extra_members=(directory, file))

    with pytest.raises(ReleaseVerificationError, match="normalized member path"):
        verify_release("v1.2.3", pyproject, changelog, wheel, sdist)


@pytest.mark.parametrize("ancestor_first", [True, False])
def test_sdist_rejects_explicit_file_ancestor(tmp_path: Path, ancestor_first: bool) -> None:
    pyproject, changelog, wheel, sdist = _matching_identity_files(tmp_path)
    ancestor = tarfile.TarInfo("autodj-1.2.3/data")
    descendant = tarfile.TarInfo("autodj-1.2.3/data/child")
    members = (ancestor, descendant) if ancestor_first else (descendant, ancestor)
    _sdist(sdist, "1.2.3", extra_members=members)

    with pytest.raises(ReleaseVerificationError, match=r"ancestor.*directory"):
        verify_release("v1.2.3", pyproject, changelog, wheel, sdist)


def test_sdist_rejects_regular_file_as_top_level_root_entry(tmp_path: Path) -> None:
    pyproject, changelog, wheel, sdist = _matching_identity_files(tmp_path)
    root_file = tarfile.TarInfo("autodj-1.2.3")
    _sdist(sdist, "1.2.3", extra_members=(root_file,))

    with pytest.raises(ReleaseVerificationError, match="top-level root entry must be a directory"):
        verify_release("v1.2.3", pyproject, changelog, wheel, sdist)


def test_cli_requires_sdist_argument(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _, _, wheel, _ = _matching_identity_files(tmp_path)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(
        sys,
        "argv",
        ["verify_release.py", "--tag", "v1.2.3", "--wheel", str(wheel)],
    )

    with pytest.raises(SystemExit, match="2"):
        main()
    assert "--sdist" in capsys.readouterr().err


def test_cli_rejects_more_than_one_sdist(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _, _, wheel, sdist = _matching_identity_files(tmp_path)
    second = tmp_path / "second.tar.gz"
    _sdist(second, "1.2.3")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "verify_release.py",
            "--tag",
            "v1.2.3",
            "--wheel",
            str(wheel),
            "--sdist",
            str(sdist),
            str(second),
        ],
    )

    with pytest.raises(SystemExit, match="2"):
        main()
    assert "unrecognized arguments" in capsys.readouterr().err


def test_release_workflow_serializes_publication_and_verifies_remote_tag() -> None:
    workflow = _release_workflow()
    assert workflow["concurrency"] == {
        "group": "release-${{ github.ref }}",
        "cancel-in-progress": "false",
    }
    publish_steps = workflow["jobs"]["publish"]["steps"]
    command = next(
        step["run"] for step in publish_steps if step.get("name") == "Publish GitHub release"
    )
    fetch = 'git fetch --force origin "refs/tags/$GITHUB_REF_NAME:refs/tags/$GITHUB_REF_NAME"'
    peel = 'git rev-parse "$GITHUB_REF_NAME^{commit}"'
    verify = 'test "$tag_commit" = "$GITHUB_SHA"'
    release = 'gh release create "$GITHUB_REF_NAME"'
    assert fetch in command
    assert verify in command
    assert (
        command.index(fetch) < command.index(peel) < command.index(verify) < command.index(release)
    )
    commands = command.strip().splitlines()
    assert commands[-2] == verify
    assert commands[-1].startswith(release)


def test_release_server_smoke_monitors_and_always_reaps_process() -> None:
    workflow = _release_workflow()
    steps = workflow["jobs"]["build-and-verify"]["steps"]
    command = next(
        step["run"] for step in steps if step.get("name") == "Verify runtime API version"
    )
    for required in (
        "set -euo pipefail",
        "server_pid=$!",
        "trap cleanup EXIT",
        'server_log="$RUNNER_TEMP/release-server.log"',
        'kill -0 "$server_pid"',
        'wait "$server_pid"',
        'cat "$server_log"',
        "for _shutdown_attempt in $(seq 1 10)",
        'kill -KILL "$server_pid"',
        "for _attempt in $(seq 1 30)",
        "--max-time 2",
    ):
        assert required in command
    assert command.index("trap cleanup EXIT") < command.index("server_pid=$!")
    runtime = command[command.index("server_pid=$!") :]
    assert 'kill -0 "$server_pid"' in runtime
    cleanup = command[command.index("cleanup() {") : command.index("trap cleanup EXIT")]
    assert 'wait "$server_pid"' in cleanup
    assert 'cat "$server_log"' in cleanup
    assert (
        cleanup.index('kill "$server_pid"')
        < cleanup.index("for _shutdown_attempt in $(seq 1 10)")
        < cleanup.index('kill -KILL "$server_pid"')
        < cleanup.index('wait "$server_pid"')
    )


def test_reusable_workflows_have_distinct_cancelling_concurrency_groups() -> None:
    workflows = []
    for filename in ("ci.yml", "security.yml"):
        path = ROOT / ".github" / "workflows" / filename
        workflows.append(yaml.load(path.read_text(encoding="utf-8"), Loader=yaml.BaseLoader))

    concurrency_groups = {workflow["concurrency"]["group"] for workflow in workflows}
    assert concurrency_groups == {
        "ci-${{ github.ref }}",
        "security-${{ github.ref }}",
    }
    assert all(workflow["concurrency"]["cancel-in-progress"] == "true" for workflow in workflows)


def test_release_verifier_dependency_is_directly_pinned_and_locked() -> None:
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    assert "packaging==26.3" in project["dependency-groups"]["dev"]

    lock = tomllib.loads((ROOT / "uv.lock").read_text(encoding="utf-8"))
    packages = {package["name"]: package for package in lock["package"]}
    assert packages["packaging"]["version"] == "26.3"
    assert {"name": "packaging"} in packages["autodj"]["dev-dependencies"]["dev"]


def test_release_backend_is_exactly_pinned_and_exports_hashed_lock_closure(
    tmp_path: Path,
) -> None:
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    requirement = "hatchling==1.31.0"
    assert project["build-system"] == {
        "requires": [requirement],
        "build-backend": "hatchling.build",
    }
    assert project["dependency-groups"]["build"] == [requirement]
    assert project["tool"]["hatch"]["build"]["artifacts"] == ["src/autodj/static_dist"]
    assert project["tool"]["hatch"]["build"]["targets"]["wheel"]["artifacts"] == [
        "src/autodj/static_dist"
    ]

    lock = tomllib.loads((ROOT / "uv.lock").read_text(encoding="utf-8"))
    packages = {package["name"]: package for package in lock["package"]}
    backend = packages["hatchling"]
    assert backend["version"] == "1.31.0"
    assert backend["sdist"]["hash"] == (
        "sha256:6b48ad4068a482ed7239b3a8215bc55b47aad3345d58dfc94e553c5d2d46211b"
    )
    assert {wheel["hash"] for wheel in backend["wheels"]} == {
        "sha256:aac80bec8b6fe35e8480f1c335be8910fa210a0e6f735a139be205dadcacb544"
    }
    assert {"name": "hatchling"} in packages["autodj"]["dev-dependencies"]["build"]

    constraints = tmp_path / "build-constraints.txt"
    result = subprocess.run(
        [
            "uv",
            "export",
            "--frozen",
            "--only-group",
            "build",
            "--no-emit-project",
            "--format",
            "requirements-txt",
            "--output-file",
            str(constraints),
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    exported = constraints.read_text(encoding="utf-8")
    assert f"{requirement} \\" in exported
    assert "--hash=sha256:" in exported


def test_release_and_container_builds_require_hashed_locked_backend() -> None:
    workflow = _release_workflow()
    steps = workflow["jobs"]["build-and-verify"]["steps"]
    export_index = next(
        index for index, step in enumerate(steps) if step.get("name") == "Export build constraints"
    )
    build_index = next(
        index for index, step in enumerate(steps) if step.get("name") == "Build distributions"
    )
    export_command = steps[export_index]["run"]
    build_command = steps[build_index]["run"]
    assert export_index < build_index
    assert "uv export --frozen --only-group build --no-emit-project" in export_command
    assert '--output-file "$RUNNER_TEMP/build-constraints.txt"' in export_command
    assert '--build-constraints "$RUNNER_TEMP/build-constraints.txt"' in build_command
    assert "--require-hashes" in build_command

    container = (ROOT / "Containerfile").read_text(encoding="utf-8")
    container_export = (
        "uv export --frozen --only-group build --no-emit-project "
        "--format requirements-txt --output-file /tmp/build-constraints.txt"
    )
    assert container_export in container
    container_build = (
        "uv build --wheel --out-dir /tmp/dist "
        "--build-constraints /tmp/build-constraints.txt --require-hashes"
    )
    container_install = "uv pip install --python /opt/venv/bin/python --no-deps /tmp/dist/*.whl"
    assert container_build in container
    assert container_install in container
    assert "FROM python-base AS package" in container
    assert container.index(container_export) < container.index(container_build)
    assert container.index(container_build) < container.index(container_install)


def test_release_workflow_clean_builds_bundle_into_wheel_and_sdist() -> None:
    workflow = _release_workflow()
    steps = workflow["jobs"]["build-and-verify"]["steps"]
    index_by_name = {
        step.get("name"): index for index, step in enumerate(steps) if step.get("name")
    }
    setup_node_index = next(
        index
        for index, step in enumerate(steps)
        if step.get("uses", "").startswith("actions/setup-node@")
    )
    assert steps[setup_node_index]["uses"] == (
        "actions/setup-node@249970729cb0ef3589644e2896645e5dc5ba9c38"
    )
    assert setup_node_index < index_by_name["Install frontend dependencies"]
    assert index_by_name["Install frontend dependencies"] < index_by_name["Build frontend assets"]
    assert index_by_name["Build frontend assets"] < index_by_name["Export build constraints"]
    assert (
        "npm ci --ignore-scripts --no-audit --no-fund"
        in steps[index_by_name["Install frontend dependencies"]]["run"]
    )
    assert "npm run build" in steps[index_by_name["Build frontend assets"]]["run"]

    temporary_directory = tempfile.TemporaryDirectory(prefix="autodj-release-build-")
    clean = Path(temporary_directory.name)
    for filename in (
        ".gitignore",
        "pyproject.toml",
        "uv.lock",
        "README.md",
        "LICENSE",
        "package.json",
        "package-lock.json",
        "vite.config.js",
    ):
        shutil.copy2(ROOT / filename, clean / filename)
    shutil.copytree(
        ROOT / "src",
        clean / "src",
        ignore=shutil.ignore_patterns("static_dist", "__pycache__"),
    )
    assert not (clean / "src" / "autodj" / "static_dist").exists()

    def run(*command: str) -> None:
        result = subprocess.run(command, cwd=clean, capture_output=True, text=True, check=False)
        assert result.returncode == 0, result.stderr

    npm = "npm.cmd" if os.name == "nt" else "npm"
    run(npm, "ci", "--ignore-scripts", "--no-audit", "--no-fund")
    run(npm, "run", "build")
    run(
        "uv",
        "export",
        "--frozen",
        "--only-group",
        "build",
        "--no-emit-project",
        "--format",
        "requirements-txt",
        "--output-file",
        "build-constraints.txt",
    )
    run(
        "uv",
        "build",
        "--sdist",
        "--wheel",
        "--out-dir",
        "dist",
        "--build-constraints",
        "build-constraints.txt",
        "--require-hashes",
    )

    project = tomllib.loads((clean / "pyproject.toml").read_text(encoding="utf-8"))
    version = project["project"]["version"]
    expected_assets = {
        "app.css",
        "app.js",
        "bitcrusher-worklet.js",
        "freeze-worklet.js",
        "glitch-worklet.js",
        "index.html",
        "stutter-worklet.js",
    }
    build_info = json.loads(
        (clean / "src" / "autodj" / "static_dist" / "build-info.json").read_text(encoding="utf-8")
    )
    assert build_info == {"version": version}

    with zipfile.ZipFile(clean / "dist" / f"autodj-{version}-py3-none-any.whl") as wheel:
        wheel_members = set(wheel.namelist())
        for asset in expected_assets:
            assert f"autodj/static_dist/{asset}" in wheel_members
        assert json.loads(wheel.read("autodj/static_dist/build-info.json")) == {"version": version}
    with tarfile.open(clean / "dist" / f"autodj-{version}.tar.gz") as sdist:
        sdist_members = set(sdist.getnames())
        for asset in expected_assets:
            assert f"autodj-{version}/src/autodj/static_dist/{asset}" in sdist_members
        assert json.loads(
            sdist.extractfile(f"autodj-{version}/src/autodj/static_dist/build-info.json").read()
        ) == {"version": version}
