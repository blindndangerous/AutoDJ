"""Verify that every release identity source names one version."""

from __future__ import annotations

import argparse
import email.policy
import re
import stat
import tarfile
import tomllib
import zipfile
from email.parser import BytesParser
from pathlib import Path, PurePosixPath

from packaging.utils import (
    InvalidSdistFilename,
    InvalidWheelFilename,
    canonicalize_name,
    parse_sdist_filename,
    parse_wheel_filename,
)
from packaging.version import InvalidVersion, Version


class ReleaseVerificationError(RuntimeError):
    """Raised when tag, source, changelog, and wheel identity disagree."""


def _metadata_identity(data: bytes, artifact: str) -> tuple[str, str]:
    message = BytesParser(policy=email.policy.default).parsebytes(data)
    names = message.get_all("Name", [])
    versions = message.get_all("Version", [])
    if len(names) != 1 or len(versions) != 1:
        raise ReleaseVerificationError(
            f"{artifact} metadata must contain exactly one Name and Version"
        )
    return str(names[0]), str(versions[0])


def _canonical_version(raw: str, artifact: str) -> Version:
    try:
        version = Version(raw)
    except InvalidVersion as exc:
        raise ReleaseVerificationError(
            f"{artifact} version must be valid PEP 440: {raw!r}"
        ) from exc
    if str(version) != raw:
        raise ReleaseVerificationError(
            f"{artifact} version must use canonical PEP 440 spelling: {raw!r}"
        )
    return version


def _canonical_artifact_stem(name: str, version: str) -> str:
    distribution = canonicalize_name(name).replace("-", "_")
    return f"{distribution}-{version}"


def _safe_archive_path(name: str, *, directory: bool, artifact: str) -> PurePosixPath:
    candidate = name[:-1] if directory and name.endswith("/") else name
    raw_parts = candidate.split("/")
    path = PurePosixPath(candidate)
    if (
        "\\" in name
        or not candidate
        or path.is_absolute()
        or any(part in {"", ".", ".."} for part in raw_parts)
    ):
        raise ReleaseVerificationError(f"{artifact} members must use safe relative POSIX paths")
    return path


def _record_archive_member(
    members: dict[str, bool], path: PurePosixPath, *, directory: bool, artifact: str
) -> None:
    key = path.as_posix()
    if key in members:
        raise ReleaseVerificationError(
            f"{artifact} contains duplicate member name for normalized member path {key!r}"
        )
    members[key] = directory


def _validate_archive_ancestors(members: dict[str, bool], artifact: str) -> None:
    for key in members:
        for ancestor in PurePosixPath(key).parents:
            ancestor_key = ancestor.as_posix()
            if ancestor_key == ".":
                break
            if ancestor_key in members and not members[ancestor_key]:
                raise ReleaseVerificationError(
                    f"{artifact} member ancestor {ancestor_key!r} must be a directory"
                )


def _wheel_identity(wheel: Path) -> tuple[str, str]:
    try:
        filename_name, filename_version, _, _ = parse_wheel_filename(wheel.name)
    except (InvalidVersion, InvalidWheelFilename) as exc:
        raise ReleaseVerificationError(
            f"wheel must have a valid wheel filename: {wheel.name}"
        ) from exc
    try:
        with zipfile.ZipFile(wheel) as zf:
            archive_members: dict[str, bool] = {}
            metadata_members: list[zipfile.ZipInfo] = []
            for member in zf.infolist():
                path = _safe_archive_path(
                    member.filename,
                    directory=member.is_dir(),
                    artifact="wheel",
                )
                _record_archive_member(
                    archive_members,
                    path,
                    directory=member.is_dir(),
                    artifact="wheel",
                )
                if member.create_system == 3:
                    file_type = stat.S_IFMT(member.external_attr >> 16)
                    if file_type not in {0, stat.S_IFREG, stat.S_IFDIR}:
                        raise ReleaseVerificationError(
                            "wheel may contain only regular files and directories"
                        )
                    if member.is_dir() != (file_type == stat.S_IFDIR) and file_type != 0:
                        raise ReleaseVerificationError(
                            "wheel may contain only regular files and directories"
                        )
                if member.filename.endswith(".dist-info/METADATA"):
                    metadata_members.append(member)
            _validate_archive_ancestors(archive_members, "wheel")
            if len(metadata_members) != 1:
                raise ReleaseVerificationError("wheel must contain exactly one METADATA file")
            metadata = metadata_members[0]
            if not 0 <= metadata.file_size <= 1024 * 1024:
                raise ReleaseVerificationError("wheel METADATA exceeds 1 MiB")
            name, version = _metadata_identity(zf.read(metadata), "wheel")
    except (OSError, UnicodeError, zipfile.BadZipFile) as exc:
        raise ReleaseVerificationError(f"cannot inspect wheel {wheel}") from exc
    if filename_name != canonicalize_name(name):
        raise ReleaseVerificationError(
            f"wheel filename project {filename_name!s} does not match METADATA {name!r}"
        )
    metadata_version = _canonical_version(version, "wheel METADATA")
    if filename_version != metadata_version:
        raise ReleaseVerificationError(
            f"wheel filename version {str(filename_version)!r} does not match METADATA {version!r}"
        )
    filename_parts = wheel.name.removesuffix(".whl").split("-")
    artifact_identity_stem = "-".join(filename_parts[:2])
    expected_artifact_stem = _canonical_artifact_stem(name, version)
    if artifact_identity_stem != expected_artifact_stem:
        raise ReleaseVerificationError(
            f"wheel canonical artifact stem must be {expected_artifact_stem!r}"
        )
    expected_metadata_name = f"{expected_artifact_stem}.dist-info/METADATA"
    if metadata.filename != expected_metadata_name:
        raise ReleaseVerificationError(
            f"wheel METADATA path {metadata.filename!r} must be {expected_metadata_name!r}"
        )
    return name, version


def _sdist_identity(sdist: Path) -> tuple[str, str]:
    try:
        filename_name, filename_version = parse_sdist_filename(sdist.name)
    except (InvalidVersion, InvalidSdistFilename) as exc:
        raise ReleaseVerificationError(
            f"sdist must have a valid sdist filename: {sdist.name}"
        ) from exc
    if not sdist.name.endswith(".tar.gz"):
        raise ReleaseVerificationError(f"sdist must have a valid .tar.gz filename: {sdist.name}")
    artifact_stem = sdist.name.removesuffix(".tar.gz")
    try:
        with tarfile.open(sdist, mode="r:gz") as tf:
            members = tf.getmembers()
            roots: set[str] = set()
            archive_members: dict[str, bool] = {}
            top_level_directory: str | None = None
            pkg_info_members: list[tarfile.TarInfo] = []
            for member in members:
                path = _safe_archive_path(
                    member.name,
                    directory=member.isdir(),
                    artifact="sdist",
                )
                roots.add(path.parts[0])
                if not (member.isfile() or member.isdir()):
                    raise ReleaseVerificationError(
                        "sdist may contain only regular files and directories"
                    )
                _record_archive_member(
                    archive_members,
                    path,
                    directory=member.isdir(),
                    artifact="sdist",
                )
                if len(path.parts) == 1:
                    if not member.isdir():
                        raise ReleaseVerificationError(
                            "sdist top-level root entry must be a directory"
                        )
                    if top_level_directory is not None:
                        raise ReleaseVerificationError(
                            "sdist must contain at most one top-level root directory entry"
                        )
                    top_level_directory = member.name
                if path.name == "PKG-INFO":
                    pkg_info_members.append(member)
            _validate_archive_ancestors(archive_members, "sdist")
            if len(roots) != 1:
                raise ReleaseVerificationError("sdist members must share one safe top-level root")
            if len(pkg_info_members) != 1 or not pkg_info_members[0].isfile():
                raise ReleaseVerificationError(
                    "sdist must contain exactly one PKG-INFO regular file"
                )
            pkg_info = pkg_info_members[0]
            if not 0 <= pkg_info.size <= 1024 * 1024:
                raise ReleaseVerificationError("sdist PKG-INFO exceeds 1 MiB")
            stream = tf.extractfile(pkg_info)
            if stream is None:
                raise ReleaseVerificationError("cannot read sdist PKG-INFO")
            name, version = _metadata_identity(stream.read(), "sdist")
    except (OSError, tarfile.TarError) as exc:
        raise ReleaseVerificationError(f"cannot inspect sdist {sdist}") from exc
    if filename_name != canonicalize_name(name):
        raise ReleaseVerificationError(
            f"sdist filename project {filename_name!s} does not match PKG-INFO {name!r}"
        )
    metadata_version = _canonical_version(version, "sdist PKG-INFO")
    if filename_version != metadata_version:
        raise ReleaseVerificationError(
            f"sdist filename version {str(filename_version)!r} does not match PKG-INFO {version!r}"
        )
    root = next(iter(roots))
    expected_artifact_stem = _canonical_artifact_stem(name, version)
    if artifact_stem != expected_artifact_stem:
        raise ReleaseVerificationError(
            f"sdist canonical artifact stem must be {expected_artifact_stem!r}"
        )
    if root != expected_artifact_stem or pkg_info.name != f"{expected_artifact_stem}/PKG-INFO":
        raise ReleaseVerificationError(
            f"sdist top-level root must match artifact stem {expected_artifact_stem!r}"
        )
    return name, version


def verify_release(
    tag: str,
    pyproject: Path,
    changelog: Path,
    wheel: Path,
    sdist: Path,
) -> str:
    """Verify all release identity sources and return the common version."""

    if not tag.startswith("v"):
        raise ReleaseVerificationError(f"release tag must start with v: {tag}")
    tag_version = tag[1:]
    with pyproject.open("rb") as fh:
        project = tomllib.load(fh)["project"]
    project_name = str(project["name"])
    project_version = str(project["version"])
    _canonical_version(project_version, "release")
    changelog_text = changelog.read_text(encoding="utf-8")
    wheel_name, wheel_version = _wheel_identity(wheel)
    sdist_name, sdist_version = _sdist_identity(sdist)
    names = {"project": project_name, "wheel": wheel_name, "sdist": sdist_name}
    if len(set(names.values())) != 1:
        raise ReleaseVerificationError(f"release project names differ: {names}")
    values = {
        "tag": tag_version,
        "project": project_version,
        "wheel": wheel_version,
        "sdist": sdist_version,
    }
    if len(set(values.values())) != 1:
        raise ReleaseVerificationError(f"release versions differ: {values}")
    latest = re.search(
        r"^## \[(?!Unreleased\])([^]]+)\]",
        changelog_text,
        flags=re.IGNORECASE | re.MULTILINE,
    )
    if latest is None or latest.group(1) != tag_version:
        found = latest.group(1) if latest else "none"
        raise ReleaseVerificationError(
            f"latest released CHANGELOG heading is {found}; expected {tag_version}"
        )
    return tag_version


def main() -> int:
    """Validate one built wheel against the current release checkout."""

    parser = argparse.ArgumentParser()
    parser.add_argument("--tag", required=True)
    parser.add_argument("--wheel", type=Path, required=True)
    parser.add_argument("--sdist", type=Path, required=True)
    args = parser.parse_args()
    version = verify_release(
        args.tag,
        Path("pyproject.toml"),
        Path("CHANGELOG.md"),
        args.wheel,
        args.sdist,
    )
    print(version)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
