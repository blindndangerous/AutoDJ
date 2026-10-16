"""Time check uploads: the zip directory check, odd names, leftovers and shared folders (#388)."""

from __future__ import annotations

import io
import json
import logging
import os
import time
import unicodedata
import zipfile
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest
import soundfile as sf

from autodj import time_check_files
from autodj.config import TimeChecksConfig, load_config
from autodj.stations import StationPaths
from autodj.time_check_files import (
    PackError,
    PackTooLarge,
    check_directory,
    install_pack,
    sweep_leftovers,
)
from autodj.time_checks import TimeChecks, found_packs
from tests.unit.test_time_checks import _write_pack

EOCD = b"PK\x05\x06"


def _wav() -> bytes:
    buffer = io.BytesIO()
    sf.write(buffer, np.zeros((441, 2)), 44100, format="WAV")
    return buffer.getvalue()


def _zip(path: Path, entries: dict[str, bytes], comment: bytes = b"") -> Path:
    with zipfile.ZipFile(path, "w") as archive:
        for name, data in entries.items():
            archive.writestr(name, data)
        archive.comment = comment
    return path


def _patch_record(path: Path, offset: int, data: bytes) -> None:
    """Overwrite bytes of the end-of-directory record at the very end."""
    raw = bytearray(path.read_bytes())
    at = len(raw) - 22
    assert raw[at : at + 4] == EOCD
    raw[at + offset : at + offset + len(data)] = data
    path.write_bytes(bytes(raw))


def test_counts_that_spell_the_signature_do_not_hide_the_record(tmp_path: Path) -> None:
    """The record is found where zipfile finds it, not by the last signature in it."""
    upload = _zip(tmp_path / "pack.zip", {"hr_03.wav": _wav()})
    _patch_record(upload, 8, EOCD)
    with pytest.raises(PackTooLarge, match="at most 2000 files"):
        check_directory(upload)


@pytest.mark.parametrize(
    ("offset", "data"),
    [
        (10, (60_000).to_bytes(2, "little")),
        (8, (60_000).to_bytes(2, "little")),
        (12, (3 * 1024 * 1024).to_bytes(4, "little")),
    ],
)
def test_crafted_entry_counts_or_directory_size_are_refused(
    tmp_path: Path, offset: int, data: bytes
) -> None:
    upload = _zip(tmp_path / "pack.zip", {"hr_03.wav": _wav()})
    _patch_record(upload, offset, data)
    opened: list[object] = []
    real = zipfile.ZipFile
    zipfile.ZipFile = lambda *args, **kwargs: opened.append(args)  # type: ignore[assignment,misc]
    try:
        with pytest.raises(PackTooLarge):
            install_pack(tmp_path / "root", "Studio", upload, replace=False)
    finally:
        zipfile.ZipFile = real  # type: ignore[misc]
    assert opened == []


def test_a_record_that_does_not_end_the_file_is_refused(tmp_path: Path) -> None:
    # A comment holding a cut-off record: the last signature is not a whole
    # record, so zipfile would read nothing sensible there.
    upload = _zip(tmp_path / "pack.zip", {"hr_03.wav": _wav()}, comment=EOCD + b"\x00" * 10)
    with pytest.raises(PackError, match="not a zip file"):
        check_directory(upload)
    # A whole record whose comment length does not reach the end.
    upload = _zip(
        tmp_path / "other.zip", {"hr_03.wav": _wav()}, comment=EOCD + b"\x00" * 16 + b"\x05\x00!"
    )
    with pytest.raises(PackError, match="not a zip file"):
        check_directory(upload)
    tiny = tmp_path / "tiny.zip"
    tiny.write_bytes(b"PK")
    with pytest.raises(PackError, match="not a zip file"):
        check_directory(tiny)


def test_a_zip_with_an_ordinary_comment_is_read(tmp_path: Path) -> None:
    upload = _zip(tmp_path / "pack.zip", {"hr_03.wav": _wav()}, comment=b"made by hand")
    assert install_pack(tmp_path / "root", "Studio", upload, replace=False).is_dir()


def test_a_name_flagged_utf8_that_is_not_utf8_is_not_a_zip(tmp_path: Path) -> None:
    upload = _zip(tmp_path / "pack.zip", {"é.wav": _wav()})
    raw = upload.read_bytes()
    assert raw.count("é".encode()) == 2
    upload.write_bytes(raw.replace("é".encode(), b"\xff\xfe"))
    with pytest.raises(PackError, match="The upload is not a zip file"):
        install_pack(tmp_path / "root", "Studio", upload, replace=False)


def test_one_name_written_two_ways_is_in_the_zip_twice(tmp_path: Path) -> None:
    composed = unicodedata.normalize("NFC", "é.wav")
    decomposed = unicodedata.normalize("NFD", "É.wav")
    upload = _zip(tmp_path / "pack.zip", {composed: _wav(), decomposed: _wav()})
    with pytest.raises(PackError, match="in the zip twice"):
        install_pack(tmp_path / "root", "Studio", upload, replace=False)


def test_a_replace_that_cannot_be_undone_keeps_the_first_error_and_logs_where(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    root = tmp_path / "root"
    install_pack(root, "Studio", _zip(tmp_path / "a.zip", {"hr_03.wav": _wav()}), replace=False)
    real = os.rename
    calls: list[tuple[str, str]] = []

    def failing(source: os.PathLike[str] | str, target: os.PathLike[str] | str) -> None:
        calls.append((Path(source).name, Path(target).name))
        if Path(target).name == "Studio":
            raise PermissionError(13, f"in use: {Path(source).name}")
        real(source, target)

    monkeypatch.setattr(time_check_files.os, "rename", failing)
    with (
        caplog.at_level(logging.ERROR),
        pytest.raises(PermissionError, match=r"in use: .pack-upload-"),
    ):
        install_pack(root, "Studio", _zip(tmp_path / "b.zip", {"m_05.wav": _wav()}), replace=True)
    [retired] = [p for p in root.iterdir() if p.name.startswith(".pack-replaced-")]
    assert (retired / "hr_03.wav").is_file()
    assert str(retired) in caplog.text and "could not put voice pack Studio back" in caplog.text


def test_old_leftovers_are_swept_and_a_set_aside_pack_is_only_reported(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    hour_ago = time.time() - 3700
    names = {
        ".pack-upload-0123456789abcdef": "dir",
        ".pack-upload-1123456789abcdef.zip": "file",
        ".pack-removed-2123456789abcdef": "dir",
        ".clip-upload-3123456789abcdef.wav": "file",
        ".pack-replaced-4123456789abcdef": "dir",
        ".pack-upload-5123456789abcdef": "new",
        ".hidden-by-someone": "file",
    }
    for name, kind in names.items():
        path = tmp_path / name
        if kind == "file":
            path.write_bytes(b"x")
        else:
            path.mkdir()
            (path / "a.wav").write_bytes(b"x")
        if kind != "new":
            os.utime(path, (hour_ago, hour_ago))
    sweep_leftovers(tmp_path)
    assert sorted(path.name for path in tmp_path.iterdir()) == [
        ".hidden-by-someone",
        ".pack-replaced-4123456789abcdef",
        ".pack-upload-5123456789abcdef",
    ]
    assert "set aside during a replace" in caplog.text


def _station_cfg(tmp_path: Path):
    config = tmp_path / "config.toml"
    config.write_text(f'[index]\nindex_dir = "{tmp_path.as_posix()}"\n', encoding="utf-8")
    cfg = load_config(config, environ={})
    cfg.time_checks = TimeChecksConfig()
    return cfg


def test_another_station_skips_the_default_stations_minute_clips(tmp_path: Path) -> None:
    cfg = _station_cfg(tmp_path)
    shared = tmp_path / "shared" / "time_checks"
    _write_pack(shared / "Studio", [3], [5])
    for folder in ("Clips", "Chosen"):
        (shared / folder).mkdir()
        (shared / folder / "15_05.wav").write_bytes(_wav())
    (shared / "Chosen" / "intro.wav").write_bytes(_wav())
    StationPaths.default(cfg).time_checks.parent.mkdir(parents=True, exist_ok=True)
    StationPaths.default(cfg).time_checks.write_text(
        json.dumps({"version": 1, "settings": {"recorded_folder": "Chosen"}}), encoding="utf-8"
    )
    found = found_packs(cfg, StationPaths.for_station(cfg, "night"))
    # Clips holds only minute clips; Chosen is the default station's clips folder.
    assert list(found.packs) == ["Studio"]


def test_a_pack_the_default_station_shares_says_so_when_there_are_stations(
    tmp_path: Path,
) -> None:
    cfg = _station_cfg(tmp_path)
    cfg.time_checks.clips_folder = str(tmp_path / "shared" / "time_checks")
    checks = TimeChecks(cfg, "UTC")
    _write_pack(checks.root / "Studio", [3], [5])
    assert checks.status()["packs"][0]["shared_with_stations"] is False
    cfg.stations = (SimpleNamespace(slug="night"),)  # type: ignore[assignment]
    assert checks.status()["packs"][0]["shared_with_stations"] is True
    # With its own clips_folder, the default station's packs are its own.
    cfg.time_checks.clips_folder = str(tmp_path / "own")
    own = TimeChecks(cfg, "UTC")
    _write_pack(own.root / "Mine", [3], [5])
    assert own.status()["packs"][0]["shared_with_stations"] is False


def test_a_linked_folder_and_its_target_are_one_pack(tmp_path: Path) -> None:
    cfg = _station_cfg(tmp_path)
    shared = tmp_path / "shared" / "time_checks"
    real = _write_pack(shared / "Studio", [3], [5])
    link = tmp_path / "voices" / "Studio"
    link.parent.mkdir()
    try:
        link.symlink_to(real, target_is_directory=True)
    except OSError:
        pytest.skip("this account cannot make symbolic links")
    cfg.time_checks.voice_packs = [str(link)]
    found = found_packs(cfg)
    assert list(found.packs) == ["Studio"] and found.clashes == ()
