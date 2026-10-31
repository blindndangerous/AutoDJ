"""Edit a recording's tags from Track info (#53).

:func:`validate_tags` checks what the page sent and :func:`write_tags`
writes it with mutagen.  The write never touches the original until it is
done: the file is copied next to itself, the copy is tagged and read back,
and only then does it replace the original in one rename.  A failure at any
step leaves the original as it was.

:func:`update_beets` copies the same fields into a beets ``library.db``
when the library is managed by beets and the page asked for it.
"""

from __future__ import annotations

import contextlib
import errno
import glob
import logging
import os
import re
import shutil
import sqlite3
import stat
import threading
import time
import uuid
from collections.abc import Callable, Iterable, Mapping
from contextlib import AbstractContextManager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from autodj.audio_meta import normalize_isrc, read_reporting_tags
from autodj.beets import _path_candidates, parse_initial_key
from autodj.dj_meta import musical_label
from autodj.errors import FieldError
from autodj.fsutil import fsync_directory

logger = logging.getLogger(__name__)

#: The fields Track info can change, in the order the page shows them.
TAG_FIELDS = ("title", "artist", "album", "genre", "year", "bpm", "key", "isrc")
_TEXT_FIELDS = ("title", "artist", "album", "genre")
_MAX_TEXT = 500
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")

# How long a write waits for a file another reader holds open (Windows will
# not replace an open file) before it gives up and says the file is busy.
_REPLACE_ATTEMPTS = 10
_REPLACE_WAIT_S = 0.2
_BEETS_TIMEOUT_S = 5.0
#: The end of a write's working copy (see :func:`temp_path`).
TEMP_SUFFIX = ".autodj-tags.tmp"
# A working copy older than this is a crashed write's, not one in progress.
_STALE_COPY_S = 3600.0

# One write at a time: two edits of one file must not race their renames.
_write_lock = threading.Lock()


#: Wraps each try of the final rename; see :func:`write_tags`.
Guard = Callable[[Path], AbstractContextManager[Any]]


class TagError(FieldError):
    """A tag edit that cannot be made; ``field`` names the field at fault."""


class TagWriteError(TagError):
    """A file operation refused after the form has already been validated."""

    def __init__(self, message: str, field: str | None = None) -> None:
        super().__init__(message, field, 409, include_field=False)


class TagFileBusyError(TagWriteError):
    """The file stayed open in another program for the whole wait."""


@dataclass(frozen=True)
class TagValues:
    """Validated values; only the fields in ``changed`` are written.

    ``year`` is 0 and ``bpm`` 0.0 when cleared; ``key`` is
    ``(pitch class, mode)`` or ``None`` when cleared; ``isrc`` is ``""``
    when cleared.
    """

    changed: frozenset[str]
    title: str = ""
    artist: str = ""
    album: str = ""
    genre: str = ""
    year: int = 0
    bpm: float = 0.0
    key: tuple[int, int] | None = None
    isrc: str = ""


def _bpm_text(bpm: float) -> str:
    return f"{bpm:.2f}".rstrip("0").rstrip(".")


def validate_tags(raw: Mapping[str, str]) -> TagValues:
    """Check the fields the page sent and return them ready to write.

    Args:
        raw: Field name to the text typed; absent fields are left alone.

    Raises:
        TagError: The first field that is not acceptable, with that field
            named, or no field at all.
    """
    unknown = sorted(set(raw) - set(TAG_FIELDS))
    if unknown:
        raise TagError(f"{unknown[0]} is not a tag Track info can edit.")
    if not raw:
        raise TagError("Change at least one tag before saving.")
    values: dict[str, Any] = {}
    for field in TAG_FIELDS:
        if field not in raw:
            continue
        text = str(raw[field]).strip()
        label = field.upper() if field in {"bpm", "isrc"} else field.capitalize()
        if field in _TEXT_FIELDS:
            if field == "title" and not text:
                raise TagError("Enter a title.", "title")
            if len(text) > _MAX_TEXT:
                raise TagError(f"{label} must be {_MAX_TEXT} characters or fewer.", field)
            if _CONTROL.search(text):
                raise TagError(f"{label} cannot contain line breaks or tabs.", field)
            values[field] = text
        elif field == "year":
            if text and (not re.fullmatch(r"\d{4}", text) or int(text) < 1000):
                raise TagError(
                    "Enter the year as four digits, such as 1999, or leave it empty.", field
                )
            values[field] = int(text) if text else 0
        elif field == "bpm":
            bpm = float(text) if re.fullmatch(r"\d{1,3}(?:\.\d{1,2})?", text) else 0.0
            if text and not 1 <= bpm <= 400:
                raise TagError(
                    "Enter BPM as a number from 1 to 400, such as 128 or 127.5, or leave it empty.",
                    field,
                )
            values[field] = bpm
        elif field == "key":
            key = parse_initial_key(text) if text else None
            if text and key is None:
                raise TagError(
                    "Enter a key such as Am, F#m, Db or C major, or a Camelot key such as 8A, "
                    "or leave it empty.",
                    field,
                )
            values[field] = key
        else:  # isrc
            code = normalize_isrc(text) if text else ""
            if text and not code:
                raise TagError(
                    "Enter an ISRC of 12 characters: 2 letters, 3 letters or digits, then 7 "
                    "digits, such as AB1CD2600001. Hyphens and spaces are ignored. Or leave it "
                    "empty.",
                    field,
                )
            values[field] = code
    return TagValues(changed=frozenset(values), **values)


# ---------------------------------------------------------------------------
# Writing with mutagen
# ---------------------------------------------------------------------------


def _open(path: Path, kind: Callable[..., Any] | None = None) -> Any:
    import mutagen

    try:
        audio = kind(str(path)) if kind is not None else mutagen.File(str(path))
    except mutagen.MutagenError as exc:
        logger.exception("Could not read tags from %s", path)
        raise TagWriteError(
            "AutoDJ could not read the tags of this file. "
            "Check the file and the server log before trying again."
        ) from exc
    if audio is None:
        raise TagWriteError("AutoDJ cannot edit the tags of this kind of file.")
    if audio.tags is None:
        try:
            audio.add_tags()
        except (mutagen.MutagenError, NotImplementedError) as exc:
            raise TagWriteError("AutoDJ cannot edit the tags of this kind of file.") from exc
    return audio


def _family(audio: Any) -> str:
    from mutagen._vorbis import VCommentDict
    from mutagen.id3 import ID3
    from mutagen.mp4 import MP4Tags

    if isinstance(audio.tags, ID3):
        return "id3"
    if isinstance(audio.tags, VCommentDict):
        return "vorbis"
    if isinstance(audio.tags, MP4Tags):
        return "mp4"
    raise TagWriteError("AutoDJ cannot edit the tags of this kind of file.")


def tags_writable(path: str | Path) -> bool:
    """Whether :func:`write_tags` knows this file's kind of tags.

    Any failure to read the file, including a parser error mutagen does not
    wrap, counts as not writable: a damaged file must never break Track info.
    """
    try:
        _family(_open(Path(path)))
    except Exception:  # any reader failure means "cannot edit"
        logger.debug("Tags of %s are not editable", path, exc_info=True)
        return False
    return True


def _key_text(values: TagValues) -> str:
    return "" if values.key is None else musical_label(*values.key)


# Where each family keeps each field, and the older keys a write removes so
# a stale value cannot shadow the new one.
_KEYS = {
    "id3": {
        "title": "TIT2",
        "artist": "TPE1",
        "album": "TALB",
        "genre": "TCON",
        "year": "TDRC",
        "bpm": "TBPM",
        "key": "TKEY",
        "isrc": "TSRC",
    },
    "vorbis": {
        "title": "title",
        "artist": "artist",
        "album": "album",
        "genre": "genre",
        "year": "date",
        "bpm": "bpm",
        "key": "initialkey",
        "isrc": "isrc",
    },
    "mp4": {
        "title": "\xa9nam",
        "artist": "\xa9ART",
        "album": "\xa9alb",
        "genre": "\xa9gen",
        "year": "\xa9day",
        "bpm": "tmpo",
        "key": "----:com.apple.iTunes:initialkey",
        "isrc": "----:com.apple.iTunes:ISRC",
    },
}
_STALE_KEYS: dict[str, dict[str, tuple[str, ...]]] = {
    "id3": {"year": ("TYER", "TDAT"), "isrc": ("TXXX:ISRC",)},
    "vorbis": {"year": ("year",)},
    "mp4": {},
}


def _text(values: TagValues, field: str, family: str) -> str:
    """The text *field* is written as in *family*; ``""`` removes the tag."""
    if field == "year":
        return str(values.year) if values.year else ""
    if field == "bpm":
        if not values.bpm:
            return ""
        # MP4 keeps BPM as a whole number.
        return str(round(values.bpm)) if family == "mp4" else _bpm_text(values.bpm)
    if field == "key":
        return _key_text(values)
    return str(getattr(values, field))


def _delete(tags: Any, family: str, key: str) -> None:
    if family == "id3":
        tags.delall(key)
    elif key in tags:
        del tags[key]


def _set(tags: Any, family: str, key: str, text: str) -> None:
    if family == "id3":
        import mutagen.id3

        tags.add(getattr(mutagen.id3, key)(encoding=3, text=[text]))
    elif family == "mp4" and key == "tmpo":
        tags[key] = [int(text)]
    elif family == "mp4" and key.startswith("----:"):
        from mutagen.mp4 import MP4FreeForm

        tags[key] = [MP4FreeForm(text.encode("utf-8"))]
    else:
        tags[key] = [text]


def _read(tags: Any, family: str, key: str) -> str:
    """The first value of *key* as text, read the way :func:`_set` wrote it."""
    if family == "id3":
        frames = tags.getall(key)
        items = list(getattr(frames[0], "text", [])) if frames else []
    else:
        items = list(tags.get(key) or [])
    if not items:
        return ""
    item = items[0]
    return item.decode("utf-8", "replace") if isinstance(item, bytes) else str(item)


def _apply(tags: Any, family: str, values: TagValues) -> None:
    for field in TAG_FIELDS:
        if field not in values.changed:
            continue
        key = _KEYS[family][field]
        for stale in (key, *_STALE_KEYS[family].get(field, ())):
            _delete(tags, family, stale)
        text = _text(values, field, family)
        if text:
            _set(tags, family, key, text)


def _verify(tags: Any, family: str, values: TagValues) -> None:
    """Refuse a copy whose changed fields do not read back as written."""
    for field in TAG_FIELDS:
        if field not in values.changed:
            continue
        if _read(tags, family, _KEYS[family][field]) != _text(values, field, family):
            raise TagWriteError(
                "The tags did not read back as written, so the file was not changed."
            )


def _replace(source: Path, target: Path, guard: Guard | None) -> None:
    """Rename *source* over *target*, one try at a time inside *guard*.

    Windows refuses to replace a file another reader has open, such as a
    browser still fetching it; that reader lets go soon, so the rename is
    tried again.  The guard is entered for each single try and left before
    the wait, so stations are never held still while it sleeps.
    """
    for attempt in range(_REPLACE_ATTEMPTS):
        with guard(target) if guard is not None else contextlib.nullcontext():
            try:
                os.replace(source, target)
                return
            except PermissionError:
                pass
        if attempt == _REPLACE_ATTEMPTS - 1:
            raise TagFileBusyError(
                "This file is open in another program, so its tags were not changed. "
                "Try again in a moment."
            )
        time.sleep(_REPLACE_WAIT_S)


def _os_message(exc: OSError) -> str:
    if exc.errno in {errno.EROFS, errno.EACCES, errno.EPERM}:
        return (
            "AutoDJ is not allowed to change this file or its folder, so its tags were not "
            "changed. Check that the music folder is not read-only."
        )
    if exc.errno == errno.ENOSPC:
        return "The disk is full, so the tags were not changed."
    return (
        "The tags could not be written, so the file was not changed. "
        "Check whether the file is open in another program, the disk or share is disconnected, "
        "or the file is damaged, then try again. See the server log for more details."
    )


def temp_path(target: Path) -> Path:
    """A new working copy's name for a write of *target*, beside it.

    Unique per write, so two servers sharing one library never write into
    the same copy; not a music suffix, so a library scan never lists it.
    """
    return target.with_name(f".{target.name}.{uuid.uuid4().hex}{TEMP_SUFFIX}")


def _sweep(target: Path) -> None:
    """Remove copies of *target* left by a crashed write.

    Only copies older than :data:`_STALE_COPY_S` go: a younger one may be
    another server's write in progress.
    """
    cutoff = time.time() - _STALE_COPY_S
    for leftover in target.parent.glob(f".{glob.escape(target.name)}.*{TEMP_SUFFIX}"):
        with contextlib.suppress(OSError):
            if leftover.stat().st_mtime < cutoff:
                leftover.unlink()


def inside_roots(path: str | Path, roots: Iterable[str | Path]) -> bool:
    """Whether the real *path* lies inside one of the real *roots*."""
    real = os.path.normcase(os.path.realpath(path))
    for root in roots:
        real_root = os.path.normcase(os.path.realpath(root))
        try:
            if os.path.commonpath([real_root, real]) == real_root:
                return True
        except ValueError:  # different drives on Windows
            continue
    return False


def _flush(path: Path) -> None:
    descriptor = os.open(path, os.O_RDWR | getattr(os, "O_BINARY", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _keep_owner(source: os.stat_result, temp: Path) -> None:
    """Give the copy the original's owner and group where the system allows it."""
    chown = getattr(os, "chown", None)
    if chown is None:
        return
    try:
        chown(temp, source.st_uid, source.st_gid)
    except OSError:
        logger.debug("Could not keep the owner of %s", temp, exc_info=True)


def write_tags(
    path: str | Path,
    values: TagValues,
    *,
    roots: Iterable[str | Path] | None = None,
    guard: Guard | None = None,
) -> Path:
    """Write *values* into the file at *path*, or leave it exactly as it was.

    The tags are written to a copy beside the file, flushed to disk and read
    back field by field, and the copy then replaces the original in one
    rename, after which the folder is flushed too (Windows has no folder
    flush; there the rename is as durable as the file system makes it).  A reader sees either the
    old file or the new one, never half of each.  An ID3 tag keeps its
    version (2.3 stays 2.3).  The copy keeps the original's permissions and,
    where allowed, its owner; the modification time is the time of the
    write, and a hard link to the old file keeps the old file.

    Args:
        path: The recording.  Its real path is written, so a link stays a
            link and the file keeps the letter case of its name.
        values: What to write.
        roots: When given, the real path must lie inside one of these
            folders; a link pointing out of them is refused.
        guard: Called with the real path for each try of the rename,
            which runs inside the context it returns; that context may
            raise :class:`TagError` on entry to stop the write (a last
            check that the file is not on air).

    Returns:
        The real path written.

    Raises:
        TagError: The file's kind of tags is not supported, it is outside
            *roots*, or the write failed; the original is unchanged.
        TagFileBusyError: The file stayed open in another program.
    """
    import mutagen

    target = Path(os.path.realpath(path))
    if roots is not None and not inside_roots(target, roots):
        raise TagWriteError(
            "This file is outside the library folders, so AutoDJ will not change its tags."
        )
    with _write_lock:
        original = _open(target)
        if os.name == "nt":
            try:
                writable = bool(target.stat().st_mode & stat.S_IWRITE)
            except OSError as exc:
                logger.exception("Could not inspect %s before writing tags", target)
                raise TagWriteError(_os_message(exc)) from exc
            if not writable:
                raise TagWriteError(
                    "AutoDJ is not allowed to change this read-only file, so its tags were not changed."
                )
        family = _family(original)
        kind = type(original)
        id3_version = 3 if family == "id3" and original.tags.version[1] == 3 else 4
        _sweep(target)
        temp = temp_path(target)
        try:
            try:
                shutil.copy2(target, temp)
                _keep_owner(target.stat(), temp)
                audio = _open(temp, kind)
                _apply(audio.tags, family, values)
                if family == "id3":
                    if id3_version == 3:
                        # 2.3 has no TDRC: the year goes back to TYER.
                        audio.tags.update_to_v23()
                    audio.save(v2_version=id3_version)
                else:
                    audio.save()
                _flush(temp)
                _verify(_open(temp, kind).tags, family, values)
            except OSError as exc:
                logger.exception("Could not write tags to the working copy of %s", target)
                raise TagWriteError(_os_message(exc)) from exc
            except mutagen.MutagenError as exc:
                logger.exception("Could not encode tags in the working copy of %s", target)
                raise TagWriteError(
                    "The tags could not be written, so the file was not changed. "
                    "Check whether the file is open in another program, the disk or share is "
                    "disconnected, or the file is damaged, then try again. "
                    "See the server log for more details."
                ) from exc
            try:
                _replace(temp, target, guard)
            except OSError as exc:
                logger.exception("Could not replace %s after writing tags", target)
                raise TagWriteError(_os_message(exc)) from exc
            # The file is changed now: a failed folder flush (some network
            # shares refuse it) must not be reported as an unchanged file.
            try:
                fsync_directory(target.parent)
            except OSError:
                logger.warning("Could not flush the folder of %s", target, exc_info=True)
        finally:
            temp.unlink(missing_ok=True)
    return target


def read_isrc(path: str | Path) -> str:
    """The file's ISRC, or ``""``, also when the file cannot be parsed."""
    try:
        return read_reporting_tags(path)[0]
    except Exception:  # a damaged file must never break Track info
        logger.debug("Could not read the ISRC of %s", path, exc_info=True)
        return ""


# ---------------------------------------------------------------------------
# Beets write-back
# ---------------------------------------------------------------------------


# Track info field to the beets ``items`` column that holds it.
_BEETS_COLUMNS = {
    "title": "title",
    "artist": "artist",
    "album": "album",
    "genre": "genre",
    "year": "year",
    "bpm": "bpm",
    "key": "initial_key",
    "isrc": "isrc",
}


def update_beets(db_path: Path, track_path: str, music_dir: Path | None, values: TagValues) -> str:
    """Copy the changed fields into the beets item for *track_path*.

    Only columns the beets database has are changed, and its ``mtime`` is
    set to the file's, so ``beet update`` finds nothing new to read.  When
    beets has a column for none of the changed fields, nothing is written,
    not even ``mtime``, so ``beet update`` still reads the new file.

    Returns:
        ``"updated"``; ``"not_found"`` when beets has no item for this file;
        ``"unchanged"`` when beets keeps none of the changed fields.

    Raises:
        sqlite3.Error: The database could not be written, for example while
            beets holds it.
    """
    wanted: dict[str, Any] = {
        "title": values.title,
        "artist": values.artist,
        "album": values.album,
        "genre": values.genre,
        "year": values.year,
        "bpm": round(values.bpm),
        "key": _key_text(values),
        "isrc": values.isrc,
    }
    conn = sqlite3.connect(str(db_path), timeout=_BEETS_TIMEOUT_S)
    try:
        present = {row[1] for row in conn.execute("PRAGMA table_info(items)")}
        fields = {
            _BEETS_COLUMNS[field]: wanted[field]
            for field in TAG_FIELDS
            if field in values.changed and _BEETS_COLUMNS[field] in present
        }
        if not fields:
            return "unchanged"
        if "mtime" in present:
            fields["mtime"] = os.stat(track_path).st_mtime
        assignments = ", ".join(f'"{column}"=?' for column in fields)
        sql = f"UPDATE items SET {assignments} WHERE path=?"  # nosec B608 -- columns from the fixed map above
        # The catalog keeps paths case-folded on Windows; beets keeps them
        # as written, which the real path on disk gives back.
        real = os.path.realpath(track_path)
        candidates = dict.fromkeys(
            [*_path_candidates(real, music_dir), *_path_candidates(track_path, music_dir)]
        )
        with conn:
            for candidate in candidates:
                for stored in (candidate.encode("utf-8"), candidate):
                    if conn.execute(sql, (*fields.values(), stored)).rowcount:
                        return "updated"
        return "not_found"
    finally:
        conn.close()


__all__ = [
    "TAG_FIELDS",
    "TagError",
    "TagFileBusyError",
    "TagValues",
    "read_isrc",
    "tags_writable",
    "update_beets",
    "validate_tags",
    "write_tags",
]
