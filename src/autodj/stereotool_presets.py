"""Validated Stereo Tool preset files stored beside the active index."""

from __future__ import annotations

import asyncio
import ntpath
import os
import re
import secrets
from collections.abc import Iterable
from pathlib import Path

from autodj.liner_files import AsyncReader

MAX_PRESET_BYTES = 2 * 1024 * 1024
INCLUDED = "included"
INCLUDED_PATH = Path(__file__).with_name("presets") / "stereo-tool-free.sts"
_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9 _.-]{0,95}\.sts", re.IGNORECASE)
# ntpath.isreserved leaves out COM0 and LPT0, which Windows also reserves.
_EXTRA_RESERVED = {"COM0", "LPT0"}


class PresetInUseError(RuntimeError):
    """The preset is the one Stereo Tool is set to use."""


def validate_preset_name(name: str) -> str:
    """Accept the included identifier or one portable, plain .sts filename."""
    if name == INCLUDED:
        return name
    if not isinstance(name, str) or not _NAME.fullmatch(name):
        raise ValueError(
            "a preset file name must end in .sts, start with a letter or digit and use only "
            "letters, digits, spaces, dots, hyphens and underscores, up to 100 characters"
        )
    stem = name.partition(".")[0].rstrip(" ").upper()
    if ntpath.isreserved(name) or stem in _EXTRA_RESERVED:
        raise ValueError(f"{name} is a reserved Windows file name")
    return name


def preset_folder(active_index: Path | str) -> Path:
    return Path(active_index).parent / "stereo-tool-presets"


def preset_path(active_index: Path | str, name: str, *, require_existing: bool = True) -> Path:
    validate_preset_name(name)
    if name == INCLUDED:
        return INCLUDED_PATH
    path = preset_folder(active_index) / name
    if require_existing and (path.is_symlink() or not path.is_file()):
        raise FileNotFoundError(f"preset {name} was not found")
    return path


def list_presets(active_index: Path | str) -> list[dict[str, str | bool]]:
    folder = preset_folder(active_index)
    names = (
        sorted(
            (
                path.name
                for path in folder.iterdir()
                if _valid_name(path.name) and path.is_file() and not path.is_symlink()
            ),
            key=str.casefold,
        )
        if folder.is_dir()
        else []
    )
    result: list[dict[str, str | bool]] = [
        {"name": INCLUDED, "label": "Included", "included": True}
    ]
    result.extend({"name": name, "label": name, "included": False} for name in names)
    return result


def _valid_name(name: str) -> bool:
    try:
        validate_preset_name(name)
    except ValueError:
        return False
    return name != INCLUDED


def _check_contents(data: bytes) -> None:
    if not data or len(data) > MAX_PRESET_BYTES or b"\0" in data:
        raise ValueError("the preset must be a text file of at most 2 MB")
    text = data.decode("latin-1")
    if not re.search(r"(?m)^\[[^\]\r\n]{1,100}\]\s*$", text) or not re.search(
        r"(?m)^[^=\r\n]{1,100}=", text
    ):
        raise ValueError("the file does not look like a Stereo Tool preset")


def agc_drive_preset_data(data: bytes, drive_db: float) -> bytes:
    """Return a preset copy with Pre Compressor Drive raised by drive_db."""
    import math

    if not math.isfinite(drive_db) or not 0 <= drive_db <= 12:
        raise ValueError("Stereo Tool AGC drive must be between 0 and 12 dB")
    lines = data.decode("latin-1").splitlines(keepends=True)
    sections = [
        i
        for i, line in enumerate(lines)
        if re.fullmatch(r"\s*\[Pre Compressor\]\s*(?:\r?\n)?", line, re.IGNORECASE)
    ]
    if len(sections) != 1:
        raise ValueError("preset must contain one [Pre Compressor] section")
    end = next(
        (i for i in range(sections[0] + 1, len(lines)) if re.match(r"\s*\[", lines[i])),
        len(lines),
    )
    drives = [
        i for i in range(sections[0] + 1, end) if re.match(r"\s*Drive\s*=", lines[i], re.IGNORECASE)
    ]
    if len(drives) != 1:
        raise ValueError("[Pre Compressor] must contain one Drive value")
    index = drives[0]
    match = re.fullmatch(r"(\s*Drive\s*=\s*)([^\r\n]*?)(\r?\n)?", lines[index], re.IGNORECASE)
    if match is None:
        raise ValueError("invalid [Pre Compressor] Drive value")
    try:
        base = float(match.group(2).strip())
    except ValueError as exc:
        raise ValueError("invalid [Pre Compressor] Drive value") from exc
    if not math.isfinite(base) or base <= 0:
        raise ValueError("invalid [Pre Compressor] Drive value")
    lines[index] = (
        match.group(1) + format(base * 10 ** (drive_db / 20), ".12g") + (match.group(3) or "")
    )
    return "".join(lines).encode("latin-1")


def _write_new(folder: Path, name: str, data: bytes) -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    target = folder / name
    if target.exists() or target.is_symlink():
        raise FileExistsError(name)
    part = folder / f".{secrets.token_hex(12)}.part"
    try:
        with part.open("xb") as out:
            out.write(data)
            out.flush()
            os.fsync(out.fileno())
        # Linking is an atomic create that refuses a concurrent upload of
        # the same name (case-insensitively on Windows); rename would
        # replace it on POSIX.
        os.link(part, target)
    finally:
        part.unlink(missing_ok=True)
    return target


async def store_preset(active_index: Path, name: str, reader: AsyncReader) -> Path:
    """Store a new upload; never replaces an existing file or the included preset."""
    validate_preset_name(name)
    if name == INCLUDED:
        raise ValueError("the included preset is read-only")
    data = await reader.read(MAX_PRESET_BYTES + 1)
    _check_contents(data)
    return await asyncio.to_thread(_write_new, preset_folder(active_index), name, data)


def delete_preset(active_index: Path, name: str, *, active: str | Iterable[str] = INCLUDED) -> None:
    """Remove one upload unless a station uses it (whatever its letter case).

    Args:
        active_index: The active index folder, which holds the presets.
        name: The preset to delete.
        active: The preset each station is set to use.

    Raises:
        PresetInUseError: A station uses it; its argument is the name that station gives.
    """
    if name == INCLUDED:
        raise ValueError("the included preset is read-only")
    path = preset_path(active_index, name)
    for selected in [active] if isinstance(active, str) else active:
        if selected == INCLUDED:
            continue
        try:
            current = preset_path(active_index, selected, require_existing=False)
        except ValueError:
            continue  # a name no preset can have: it cannot be this one
        if current.is_file() and os.path.samefile(path, current):
            raise PresetInUseError(selected)
    path.unlink()
