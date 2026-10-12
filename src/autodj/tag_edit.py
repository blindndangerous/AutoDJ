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

import errno
import logging
import os
import re
import shutil
import sqlite3
import threading
import time
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from autodj.audio_meta import normalize_isrc, read_reporting_tags
from autodj.beets import _path_candidates, parse_initial_key
from autodj.dj_meta import musical_label

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

# One write at a time: two edits of one file must not race their renames.
_write_lock = threading.Lock()


class TagError(Exception):
    """A tag edit that cannot be made; ``field`` names the field at fault."""

    def __init__(self, message: str, field: str | None = None) -> None:
        """Keep the message for the page and the field it belongs to, if any."""
        super().__init__(message)
        self.field = field


class TagFileBusyError(TagError):
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
        raise TagError(f"AutoDJ could not read the tags of this file: {exc}") from exc
    if audio is None:
        raise TagError("AutoDJ cannot edit the tags of this kind of file.")
    if audio.tags is None:
        try:
            audio.add_tags()
        except (mutagen.MutagenError, NotImplementedError) as exc:
            raise TagError("AutoDJ cannot edit the tags of this kind of file.") from exc
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
    raise TagError("AutoDJ cannot edit the tags of this kind of file.")


def tags_writable(path: str | Path) -> bool:
    """Whether :func:`write_tags` knows this file's kind of tags."""
    try:
        _family(_open(Path(path)))
    except (TagError, OSError):
        return False
    return True


def _key_text(values: TagValues) -> str:
    return "" if values.key is None else musical_label(*values.key)


def _apply_id3(tags: Any, values: TagValues) -> None:
    from mutagen.id3 import TALB, TBPM, TCON, TDRC, TIT2, TKEY, TPE1, TSRC

    frames = {
        "title": (TIT2, values.title),
        "artist": (TPE1, values.artist),
        "album": (TALB, values.album),
        "genre": (TCON, values.genre),
        "year": (TDRC, str(values.year) if values.year else ""),
        "bpm": (TBPM, _bpm_text(values.bpm) if values.bpm else ""),
        "key": (TKEY, _key_text(values)),
        "isrc": (TSRC, values.isrc),
    }
    for field, (frame, text) in frames.items():
        if field not in values.changed:
            continue
        tags.delall(frame.__name__)
        if field == "year":
            tags.delall("TYER")
        if field == "isrc":
            tags.delall("TXXX:ISRC")
        if text:
            tags.add(frame(encoding=3, text=[text]))


def _apply_vorbis(tags: Any, values: TagValues) -> None:
    names = {
        "title": ("title", values.title),
        "artist": ("artist", values.artist),
        "album": ("album", values.album),
        "genre": ("genre", values.genre),
        "year": ("date", str(values.year) if values.year else ""),
        "bpm": ("bpm", _bpm_text(values.bpm) if values.bpm else ""),
        "key": ("initialkey", _key_text(values)),
        "isrc": ("isrc", values.isrc),
    }
    for field, (name, text) in names.items():
        if field not in values.changed:
            continue
        stale = [name, "year"] if field == "year" else [name]
        for key in stale:
            if key in tags:
                del tags[key]
        if text:
            tags[name] = [text]


def _apply_mp4(tags: Any, values: TagValues) -> None:
    from mutagen.mp4 import MP4FreeForm

    names = {
        "title": "\xa9nam",
        "artist": "\xa9ART",
        "album": "\xa9alb",
        "genre": "\xa9gen",
        "year": "\xa9day",
        "bpm": "tmpo",
        "key": "----:com.apple.iTunes:initialkey",
        "isrc": "----:com.apple.iTunes:ISRC",
    }
    for field, name in names.items():
        if field not in values.changed:
            continue
        if name in tags:
            del tags[name]
        if field == "year":
            value: Any = [str(values.year)] if values.year else None
        elif field == "bpm":
            value = [round(values.bpm)] if values.bpm else None
        elif field == "key":
            value = [MP4FreeForm(_key_text(values).encode())] if values.key else None
        elif field == "isrc":
            value = [MP4FreeForm(values.isrc.encode())] if values.isrc else None
        else:
            text = getattr(values, field)
            value = [text] if text else None
        if value is not None:
            tags[name] = value


def _replace(source: Path, target: Path) -> None:
    for attempt in range(_REPLACE_ATTEMPTS):
        try:
            os.replace(source, target)
            return
        except PermissionError:
            # Windows refuses to replace a file another reader has open,
            # such as a browser still fetching it; that reader lets go soon.
            if attempt == _REPLACE_ATTEMPTS - 1:
                raise TagFileBusyError(
                    "This file is open in another program, so its tags were not changed. "
                    "Try again in a moment."
                ) from None
            time.sleep(_REPLACE_WAIT_S)


def _os_message(exc: OSError) -> str:
    if exc.errno in {errno.EROFS, errno.EACCES, errno.EPERM}:
        return (
            "AutoDJ is not allowed to change this file or its folder, so its tags were not "
            "changed. Check that the music folder is not read-only."
        )
    if exc.errno == errno.ENOSPC:
        return "The disk is full, so the tags were not changed."
    return f"The tags could not be written, so the file was not changed: {exc.strerror or exc}"


def write_tags(path: str | Path, values: TagValues) -> None:
    """Write *values* into the file at *path*, or leave it exactly as it was.

    The tags are written to a copy beside the file, read back, and the copy
    then replaces the original in one rename, so a reader sees either the
    old file or the new one, never half of each.

    Raises:
        TagError: The file's kind of tags is not supported, or the write
            failed; the original is unchanged.
        TagFileBusyError: The file stayed open in another program.
    """
    import mutagen

    target = Path(path)
    with _write_lock:
        family = _family(_open(target))
        kind = type(mutagen.File(str(target)))
        # Not a music suffix, so a library scan running now never lists it.
        temp = target.with_name(f".{target.name}.{uuid.uuid4().hex[:8]}.autodj-tags.tmp")
        try:
            try:
                shutil.copy2(target, temp)
                audio = _open(temp, kind)
                {"id3": _apply_id3, "vorbis": _apply_vorbis, "mp4": _apply_mp4}[family](
                    audio.tags, values
                )
                audio.save()
                check = _open(temp, kind)
            except OSError as exc:
                raise TagError(_os_message(exc)) from exc
            except mutagen.MutagenError as exc:
                raise TagError(
                    f"The tags could not be written, so the file was not changed: {exc}"
                ) from exc
            if "title" in values.changed and _first_text(check, family, "title") != values.title:
                raise TagError(
                    "The tags did not read back as written, so the file was not changed."
                )
            try:
                _replace(temp, target)
            except OSError as exc:
                raise TagError(_os_message(exc)) from exc
        finally:
            temp.unlink(missing_ok=True)


def _first_text(audio: Any, family: str, field: str) -> str:
    key = {"id3": "TIT2", "vorbis": "title", "mp4": "\xa9nam"}[family]
    value = audio.tags.get(key)
    items = getattr(value, "text", value) or [""]
    return str(items[0])


def read_isrc(path: str | Path) -> str:
    """The file's ISRC, or ``""``."""
    return read_reporting_tags(path)[0]


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


def update_beets(db_path: Path, track_path: str, music_dir: Path | None, values: TagValues) -> bool:
    """Copy the changed fields into the beets item for *track_path*.

    Only columns the beets database has are changed, and its ``mtime`` is
    set to the file's, so ``beet update`` finds nothing new to read.

    Returns:
        ``False`` when beets has no item for this file.

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
        if "mtime" in present:
            fields["mtime"] = os.stat(track_path).st_mtime
        if not fields:
            return False
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
                        return True
        return False
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
