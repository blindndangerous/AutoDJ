"""Folder listings for the web UI's Browse buttons.

The browser cannot pick a path on the machine running AutoDJ: a native file
input hands over a copy of a file from the browser's own computer, never a
server path, and AutoDJ often runs in a container or on another machine.  So
the Browse dialog asks the server what is in a folder.

Two listings exist:

- :func:`list_server_folder` lists the folders inside any absolute folder
  on the server, for typing a new library folder.  A paired browser may already
  register any folder the server can read, so the listing's roots are the
  file system's roots; it names folders only, never files, and refuses
  relative paths and ``..`` parts.
- :func:`list_library_folder` lists folders and audio files inside one
  registered library folder, by a relative path with the same rules as a
  clockwheel step (no ``..``, drive letter, colon or backslash).  The resolved
  folder must stay inside the library folder, so a symbolic link cannot lead
  out of it, and linked entries that point outside are left out.

Both read names only: no file is opened, and each listing is capped.
"""

from __future__ import annotations

import os
import string
from collections.abc import Collection
from pathlib import Path, PurePosixPath
from typing import Any

MAX_ENTRIES = 2000
_MAX_PATH = 4096


class BrowseError(ValueError):
    """A listing the browser asked for that cannot be given; *status* is the HTTP code."""

    def __init__(self, message: str, status: int = 400) -> None:
        super().__init__(message)
        self.status = status


def filesystem_roots() -> list[str]:
    """The roots a server folder listing starts from: drives on Windows, ``/`` elsewhere."""
    if os.name != "nt":
        return ["/"]
    listdrives = getattr(os, "listdrives", None)
    if listdrives is not None:
        try:
            return sorted(listdrives())
        except OSError:
            pass
    return [f"{letter}:\\" for letter in string.ascii_uppercase if os.path.isdir(f"{letter}:\\")]


def _scan(folder: Path) -> list[os.DirEntry[str]]:
    try:
        with os.scandir(folder) as entries:
            return sorted(entries, key=lambda entry: (entry.name.casefold(), entry.name))
    except (FileNotFoundError, NotADirectoryError):
        raise BrowseError("That folder no longer exists", 404) from None
    except PermissionError:
        raise BrowseError("AutoDJ is not allowed to read that folder", 403) from None
    except OSError:
        raise BrowseError("That folder could not be read", 503) from None


def _is_dir(entry: os.DirEntry[str]) -> bool:
    try:
        return entry.is_dir()
    except OSError:
        return False


def _is_file(entry: os.DirEntry[str]) -> bool:
    try:
        return entry.is_file()
    except OSError:
        return False


def _check_text(path: Any) -> str:
    if not isinstance(path, str) or len(path) > _MAX_PATH or "\0" in path:
        raise BrowseError("That is not a folder path")
    return path


def list_server_folder(path: str = "") -> dict[str, Any]:
    """List the folders in the absolute folder *path*; ``""`` lists the roots.

    Where there is a single root (``/``), ``""`` lists it directly.
    """
    path = _check_text(path).strip()
    roots = filesystem_roots()
    if not path:
        if len(roots) != 1:
            return {
                "path": "",
                "parent": None,
                "folders": [{"name": root, "path": root} for root in roots],
                "files": [],
                "truncated": False,
            }
        path = roots[0]
    parts = path.replace("\\", "/").split("/")
    if not os.path.isabs(path) or any(part == ".." for part in parts):
        raise BrowseError("Enter a full folder path, without ..")
    folder = Path(os.path.normpath(path))
    if not folder.is_dir():
        raise BrowseError("That folder does not exist on the AutoDJ machine", 404)
    folders: list[dict[str, str]] = []
    truncated = False
    for entry in _scan(folder):
        if not _is_dir(entry):
            continue
        if len(folders) >= MAX_ENTRIES:
            truncated = True
            break
        folders.append({"name": entry.name, "path": str(folder / entry.name)})
    parent = str(folder.parent)
    at_root = parent == str(folder)
    return {
        "path": str(folder),
        "parent": ("" if len(roots) > 1 else None) if at_root else parent,
        "folders": folders,
        "files": [],
        "truncated": truncated,
    }


def check_relative(path: Any) -> str:
    """Return the relative library *path* in ``/`` form, or raise :class:`BrowseError`."""
    path = _check_text(path).strip()
    if not path:
        return ""
    if "\\" in path or ":" in path or path.startswith("/"):
        raise BrowseError("Use a path inside the library folder, such as Rock/Live")
    parts = path.rstrip("/").split("/")
    if any(part in {"", ".", ".."} for part in parts):
        raise BrowseError("Use a path inside the library folder, without ..")
    return "/".join(parts)


def _inside(path: Path, root: Path) -> bool:
    return path == root or path.is_relative_to(root)


def list_library_folder(
    root: str | Path, relative: str = "", *, files: bool = False, extensions: Collection[str] = ()
) -> dict[str, Any]:
    """List the folders, and with *files* the audio files, in *relative* under *root*.

    *extensions* are lower-case suffixes with their dot, such as ``.flac``.
    Returned paths are relative to *root*, with ``/`` separators.
    """
    relative = check_relative(relative)
    try:
        base = Path(root).resolve(strict=True)
        folder = (base / relative).resolve(strict=True) if relative else base
    except OSError:
        raise BrowseError("That folder does not exist in the library folder", 404) from None
    if not _inside(folder, base):
        raise BrowseError("That folder is outside the library folder", 403)
    if not folder.is_dir():
        raise BrowseError("That is not a folder", 404)
    folders: list[dict[str, str]] = []
    found: list[dict[str, str]] = []
    truncated = False
    prefix = PurePosixPath(relative) if relative else None
    for entry in _scan(folder):
        is_dir = _is_dir(entry)
        if not is_dir and not (files and Path(entry.name).suffix.lower() in extensions):
            continue
        if is_dir or _is_file(entry):
            if entry.is_symlink():
                try:
                    if not _inside(Path(entry.path).resolve(strict=True), base):
                        continue
                except OSError:
                    continue
            if len(folders) + len(found) >= MAX_ENTRIES:
                truncated = True
                break
            item = {"name": entry.name, "path": str(prefix / entry.name if prefix else entry.name)}
            (folders if is_dir else found).append(item)
    parent = None
    if relative:
        parent = str(PurePosixPath(relative).parent)
        parent = "" if parent == "." else parent
    return {
        "path": relative,
        "parent": parent,
        "folders": folders,
        "files": found,
        "truncated": truncated,
    }
