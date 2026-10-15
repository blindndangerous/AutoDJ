"""Time check files added and removed from the web page (#388).

A station's time checks folder (``[time_checks] clips_folder``, by default
``time_checks`` in the station's folder) holds the recorded minute clips,
the intro and outro clips, and one subfolder per voice pack.  A paired
browser may:

- add a voice pack by uploading a zip of its audio files
  (:func:`install_pack`);
- remove a voice pack that is a folder there (:func:`remove_pack`);
- upload one clip, such as an intro, an outro or a minute clip
  (:func:`install_clip`).

Every name that arrives over HTTP is one plain name
(:func:`~autodj.liner_files.validate_plain_name`), so nothing can reach
outside the folder.  A zip is read with these limits before a byte is
written: no absolute paths, drive letters or ``..``; no links; no
encrypted entries; no folders inside the pack (one top folder holding
everything is taken off); only ``.wav``, ``.mp3``, ``.flac`` and ``.ogg``
files, each a plain name and none twice; at most :data:`MAX_PACK_FILES`
files and :data:`MAX_PACK_BYTES` bytes unpacked, counted as they are
written as well as from the headers.  Each file must then read as the
audio its extension says, no longer than :data:`MAX_PIECE_SECONDS`.  A pack
is unpacked into a hidden folder beside the others and renamed into place
only when all of that has passed, so a refused or broken upload leaves
nothing behind and a pack is never seen half written.
"""

from __future__ import annotations

import contextlib
import logging
import os
import re
import secrets
import shutil
import stat
import zipfile
from collections.abc import Iterable
from pathlib import Path, PurePosixPath
from typing import IO

from autodj.break_audio import BreakAudioError, check_break_audio
from autodj.liner_files import AsyncReader, InvalidLinerName, validate_plain_name
from autodj.stereo import TrackTooLongError
from autodj.voice_packs import AUDIO_EXTENSIONS

logger = logging.getLogger(__name__)

#: The largest zip a browser may upload, in bytes.
MAX_PACK_UPLOAD_BYTES = 256 * 1024 * 1024
#: The most a pack may hold once unpacked, in bytes.
MAX_PACK_BYTES = 512 * 1024 * 1024
#: The most files a pack may hold: a full set of minute clips is 1440.
MAX_PACK_FILES = 2000
#: The longest one file in a pack or one uploaded clip may play, in seconds.
MAX_PIECE_SECONDS = 120.0
#: The longest voice pack name.
MAX_PACK_NAME = 100
#: Files an operating system leaves in a zip, skipped rather than refused.
_OS_LEFTOVERS = frozenset({".ds_store", "thumbs.db", "desktop.ini"})
#: A recorded minute clip's name without its extension, such as ``03_05``.
MINUTE_CLIP = re.compile(r"\d{2}_\d{2}")
_CHUNK_BYTES = 1024 * 1024
_S_IFLNK = 0o120000


class PackError(ValueError):
    """A pack or clip AutoDJ will not take, with the reason in words."""


class PackConflict(PackError):
    """The name is taken and replacing it was not asked for, or is not allowed."""


class PackExists(PackConflict):
    """The name is taken by a pack or clip the page may replace, when asked to."""


class PackTooLarge(PackError):
    """The upload or what it unpacks to is over a limit."""


class PackNotFound(PackError):
    """No pack or clip by that name in the station's folder."""


def _sentence(exc: Exception) -> str:
    message = str(exc)
    return message[:1].upper() + message[1:]


def check_pack_name(name: str) -> str:
    """The pack name, trimmed, if it can name a folder; else :class:`PackError`."""
    name = name.strip()
    try:
        validate_plain_name(name, "The voice pack name")
    except InvalidLinerName as exc:
        raise PackError(_sentence(exc)) from None
    if name.startswith("."):
        raise PackError("The voice pack name must not start with a dot")
    if len(name) > MAX_PACK_NAME:
        raise PackError(f"The voice pack name must be at most {MAX_PACK_NAME} characters")
    return name


def check_clip_name(name: str) -> str:
    """*name* if it can name a clip file in the folder; else :class:`PackError`."""
    try:
        validate_plain_name(name, "The clip name")
    except InvalidLinerName as exc:
        raise PackError(_sentence(exc)) from None
    if name.startswith("."):
        raise PackError("The clip name must not start with a dot")
    if Path(name).suffix.lower() not in AUDIO_EXTENSIONS:
        raise PackError("A clip must be one of these types: " + ", ".join(AUDIO_EXTENSIONS))
    return name


def _entries(root: Path, folders: bool) -> list[str]:
    """Names of the folders (or files) directly in *root*, hidden ones left out.

    ``os.scandir`` gives each entry's type with the listing on Windows and
    most Unix file systems, so a folder of 1440 minute clips on a share is
    one listing, not 1440 more requests.
    """
    try:
        with os.scandir(root) as listing:
            return [
                entry.name
                for entry in listing
                if not entry.name.startswith(".")
                and (entry.is_dir() if folders else entry.is_file())
            ]
    except OSError:
        return []


def _by_name(names: Iterable[str]) -> list[str]:
    return sorted(names, key=lambda name: (name.casefold(), name))


def subfolders(root: Path) -> list[Path]:
    """The folders directly in *root*, by name; hidden ones (``.name``) are left out."""
    return [root / name for name in _by_name(_entries(root, folders=True))]


def clip_files(root: Path) -> list[str]:
    """Audio files directly in *root* that can be an intro or outro, by name.

    Recorded minute clips (``03_05.wav``) and hidden files are left out.
    """
    return _by_name(
        name
        for name in _entries(root, folders=False)
        if Path(name).suffix.lower() in AUDIO_EXTENSIONS
        and not MINUTE_CLIP.fullmatch(Path(name).stem)
    )


async def receive_upload(reader: AsyncReader, target: Path, max_bytes: int) -> int:
    """Stream an upload into *target*, a new file; :class:`PackTooLarge` over *max_bytes*.

    *target* is removed when anything goes wrong.

    Returns:
        The number of bytes written.
    """
    total = 0
    target.parent.mkdir(parents=True, exist_ok=True)
    try:
        with open(target, "xb") as out:
            while True:
                chunk = await reader.read(min(_CHUNK_BYTES, max_bytes - total + 1))
                if not chunk:
                    break
                total += len(chunk)
                if total > max_bytes:
                    raise PackTooLarge(f"The upload is over {max_bytes // (1024 * 1024)} MB")
                out.write(chunk)
    except BaseException:
        with contextlib.suppress(OSError):
            target.unlink()
        raise
    return total


def hidden_name(root: Path, label: str, suffix: str = "") -> Path:
    """A new hidden path in *root*, never listed: ``.<label>-<hex><suffix>``."""
    return root / f".{label}-{secrets.token_hex(8)}{suffix}"


def _is_link(info: zipfile.ZipInfo) -> bool:
    """Whether a zip entry is a symbolic link (Unix mode bits in its attributes)."""
    return stat.S_IFMT(info.external_attr >> 16) == _S_IFLNK


def _entry_parts(info: zipfile.ZipInfo) -> tuple[str, ...]:
    """The entry's path parts, refusing absolute paths, drive letters and ``..``."""
    raw = info.filename.replace("\\", "/")
    if raw.startswith("/") or re.match(r"[A-Za-z]:", raw):
        raise PackError(f"{info.filename} has an absolute path")
    parts = PurePosixPath(raw).parts
    if any(part in ("..", ".") for part in raw.split("/")):
        raise PackError(f"{info.filename} points outside the pack")
    return parts


def plan_pack(infos: Iterable[zipfile.ZipInfo]) -> tuple[list[tuple[zipfile.ZipInfo, str]], str]:
    """Check a zip's entries and say where each audio file goes.

    Returns:
        Each entry to unpack with its file name in the pack, and the name of
        the one folder that held them all (``""`` when they were at the top).

    Raises:
        PackError: An entry is unsafe or not audio, or the zip is empty.
        PackTooLarge: Too many files, or too many bytes unpacked.
    """
    files: list[tuple[zipfile.ZipInfo, tuple[str, ...]]] = []
    for info in infos:
        parts = _entry_parts(info)
        if _is_link(info):
            raise PackError(f"{info.filename} is a link; a pack holds only audio files")
        if info.flag_bits & 0x1:
            raise PackError(f"{info.filename} is encrypted")
        if info.is_dir() or not parts:
            continue
        if parts[0].casefold() == "__macosx" or parts[-1].casefold() in _OS_LEFTOVERS:
            continue
        files.append((info, parts))
    if not files:
        raise PackError("The zip holds no audio files")
    top = ""
    tops = {parts[0] for _info, parts in files}
    if len(tops) == 1 and all(len(parts) == 2 for _info, parts in files):
        top = tops.pop()
    elif any(len(parts) != 1 for _info, parts in files):
        raise PackError(
            "The audio files must be at the top of the zip or in one folder, not in subfolders"
        )
    if len(files) > MAX_PACK_FILES:
        raise PackTooLarge(f"A pack may hold at most {MAX_PACK_FILES} files")
    if sum(info.file_size for info, _parts in files) > MAX_PACK_BYTES:
        raise PackTooLarge(f"A pack may hold at most {MAX_PACK_BYTES // (1024 * 1024)} MB")
    planned: list[tuple[zipfile.ZipInfo, str]] = []
    seen: set[str] = set()
    for info, parts in files:
        name = parts[-1]
        if Path(name).suffix.lower() not in AUDIO_EXTENSIONS:
            raise PackError(
                f"{name} is not an audio file; a pack holds only "
                + ", ".join(AUDIO_EXTENSIONS)
                + " files"
            )
        try:
            check_clip_name(name)
        except PackError as exc:
            raise PackError(f"{name}: {exc}") from None
        if name.casefold() in seen:
            raise PackError(f"{name} is in the zip twice")
        seen.add(name.casefold())
        planned.append((info, name))
    return planned, top


def _copy_bounded(source: IO[bytes], out: IO[bytes], limit: int, budget: list[int]) -> None:
    """Copy at most *limit* bytes, and no more than the pack's remaining *budget*."""
    written = 0
    while True:
        chunk = source.read(_CHUNK_BYTES)
        if not chunk:
            return
        written += len(chunk)
        budget[0] -= len(chunk)
        if written > limit or budget[0] < 0:
            raise PackTooLarge(
                f"A pack may hold at most {MAX_PACK_BYTES // (1024 * 1024)} MB unpacked"
            )
        out.write(chunk)


def check_audio(path: Path, shown: str) -> None:
    """Refuse a file that is not the audio its extension says, or is too long."""
    try:
        check_break_audio(path, MAX_PIECE_SECONDS)
    except BreakAudioError as exc:
        raise PackError(f"{shown}: {exc}") from None
    except TrackTooLongError:
        raise PackError(
            f"{shown} is longer than {MAX_PIECE_SECONDS:.0f} seconds; "
            "time check pieces are short recordings"
        ) from None


def unpack(zip_path: Path, stage: Path) -> str:
    """Unpack a checked zip into the empty folder *stage* and check each file's audio.

    Returns:
        The name of the folder that held the files in the zip, or ``""``.

    Raises:
        PackError: The zip is not a zip, or :func:`plan_pack` or
            :func:`check_audio` refuses it.
    """
    try:
        archive = zipfile.ZipFile(zip_path)
    except (zipfile.BadZipFile, OSError) as exc:
        raise PackError("The upload is not a zip file") from exc
    with archive:
        planned, top = plan_pack(archive.infolist())
        budget = [MAX_PACK_BYTES]
        for info, name in planned:
            try:
                with archive.open(info) as source, open(stage / name, "xb") as out:
                    _copy_bounded(source, out, info.file_size, budget)
            except (zipfile.BadZipFile, NotImplementedError, EOFError, RuntimeError) as exc:
                # A bad CRC, an unknown compression method or a damaged entry.
                raise PackError(f"{name} could not be unpacked: {exc}") from exc
    for _info, name in planned:
        check_audio(stage / name, name)
    return top


def _remove_tree(path: Path) -> None:
    """Delete a hidden staging or retired folder; a failure is logged, not raised."""
    try:
        shutil.rmtree(path)
    except FileNotFoundError:
        pass
    except OSError:
        logger.warning("Could not delete %s; it is hidden and can be deleted by hand", path)


def install_pack(root: Path, name: str, zip_path: Path, *, replace: bool) -> Path:
    """Unpack *zip_path* as voice pack *name* in *root*.

    The zip is unpacked into a hidden folder in *root*, checked, and then
    renamed to *name*.  With *replace*, a pack folder of that name is first
    renamed aside, and deleted once the new one is in place (put back if
    the rename fails).

    Returns:
        The new pack folder.

    Raises:
        PackConflict: *name* exists and *replace* is false.
        PackError: The zip is refused (see :func:`unpack`).
        OSError: The folder could not be written.
    """
    name = check_pack_name(name)
    target = root / name
    if not replace and _exists(target):
        raise PackExists(f"A voice pack named {name} already exists")
    root.mkdir(parents=True, exist_ok=True)
    stage = hidden_name(root, "pack-upload")
    stage.mkdir()
    try:
        unpack(zip_path, stage)
        if not _exists(target):
            os.rename(stage, target)
            return target
        if not replace:
            raise PackExists(f"A voice pack named {name} already exists")
        _check_real_folder(target, name)
        retired = hidden_name(root, "pack-replaced")
        os.rename(target, retired)
        try:
            os.rename(stage, target)
        except OSError:
            os.rename(retired, target)
            raise
        _remove_tree(retired)
        return target
    finally:
        if stage.exists():
            _remove_tree(stage)


def _exists(path: Path) -> bool:
    """Whether anything, even a broken link, has *path*'s name."""
    return path.exists() or path.is_symlink()


def _check_real_folder(path: Path, name: str) -> None:
    """Refuse to replace or remove a link: only the folder AutoDJ made may go."""
    if path.is_symlink() or path.is_junction() or not path.is_dir():
        raise PackConflict(
            f"{name} is a link or not a folder; change it on the server, not from the page"
        )


def remove_pack(root: Path, name: str) -> None:
    """Delete voice pack folder *name* from *root*.

    It is renamed to a hidden name first, so it stops being a pack at once,
    then deleted.

    Raises:
        PackNotFound: No folder by that name.
        PackConflict: It is a link, not a folder AutoDJ can delete.
        OSError: It could not be renamed (a file in it is open, say).
    """
    name = check_pack_name(name)
    target = root / name
    if not _exists(target):
        raise PackNotFound(f"No voice pack named {name}")
    _check_real_folder(target, name)
    retired = hidden_name(root, "pack-removed")
    os.rename(target, retired)
    _remove_tree(retired)


def install_clip(root: Path, name: str, upload: Path, *, replace: bool) -> Path:
    """Move a received clip *upload* (a hidden file in *root*) to *name*, after checking it.

    Raises:
        PackConflict: *name* exists and *replace* is false.
        PackError: The name or the audio is refused.
    """
    name = check_clip_name(name)
    target = root / name
    try:
        if not replace and _exists(target):
            raise PackExists(f"A clip named {name} already exists")
        if target.is_symlink() or (target.exists() and not target.is_file()):
            raise PackConflict(f"{name} is not a plain file; change it on the server")
        check_audio(upload, name)
        if replace:
            os.replace(upload, target)
        else:
            # A link fails when the name was taken in the meantime, where a
            # rename would replace it on Unix.
            try:
                os.link(upload, target)
            except FileExistsError:
                raise PackExists(f"A clip named {name} already exists") from None
            except OSError:
                if _exists(target):
                    raise PackExists(f"A clip named {name} already exists") from None
                os.replace(upload, target)
    finally:
        with contextlib.suppress(OSError):
            upload.unlink()
    return target


__all__ = [
    "MAX_PACK_BYTES",
    "MAX_PACK_FILES",
    "MAX_PACK_UPLOAD_BYTES",
    "MAX_PIECE_SECONDS",
    "MINUTE_CLIP",
    "PackConflict",
    "PackError",
    "PackExists",
    "PackNotFound",
    "PackTooLarge",
    "check_clip_name",
    "check_pack_name",
    "clip_files",
    "hidden_name",
    "install_clip",
    "install_pack",
    "plan_pack",
    "receive_upload",
    "remove_pack",
    "subfolders",
    "unpack",
]
