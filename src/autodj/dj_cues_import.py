"""Import cue points from external DJ software metadata.

AutoDJ auto-detects cues from raw audio (see :func:`autodj.dj_meta.detect_cues`),
but if you already use Mixxx, Rekordbox, Serato, or Traktor on the same library
this module pulls their cues straight from the canonical sources.

Discovery is fully automatic: :func:`auto_import_cues` walks a small list of
well-known locations (Mixxx's user-data dir, Rekordbox's exported XML, the
beets library directory, ID3 tags on each audio file) and merges everything
it finds.  Nothing to install — every reader uses the standard library or
``mutagen`` (already pinned in ``pyproject.toml`` for embedded album art).

Source ranking when two readers report a cue at the same time:
    user > mixxx == rekordbox == serato == traktor > auto

Each reader is best-effort: failures are logged at DEBUG level and the
empty list is returned, so a missing / corrupt source never breaks the
overall import.

Example:
    >>> from pathlib import Path
    >>> from autodj.dj_cues_import import auto_import_cues
    >>> by_path = auto_import_cues(library_root=Path("/music"))
    >>> by_path["/music/track.mp3"]
    [Cue(time_s=12.5, type='drop', source='rekordbox', label='Drop'), ...]
"""

from __future__ import annotations

import contextlib
import logging
import sqlite3

# DJ-software XML exports are user-owned files on the local filesystem
# (Rekordbox, Traktor) -- there is no untrusted-network path to them.
# `defusedxml` would add a runtime dependency that the user has to
# install separately for a feature with no real attack surface.
import xml.etree.ElementTree as ET  # nosec B405
from pathlib import Path
from typing import Any

from autodj.dj_meta import Cue

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Mixxx — SQLite database
# ---------------------------------------------------------------------------


def import_from_mixxx(db_path: Path) -> dict[str, list[Cue]]:
    """Read cues from a Mixxx ``mixxx.db`` SQLite library.

    The Mixxx schema (as of 2.3+) stores tracks in ``library`` and cues
    in ``cues`` keyed by ``track_id``.  Cue ``type`` is an enum:

    - 0 = invalid (skip)
    - 1 = hot cue
    - 2 = main cue / load cue
    - 3 = beat
    - 4 = loop
    - 5 = jump
    - 6 = intro start, 7 = intro end
    - 8 = outro start, 9 = outro end

    We map all of these to AutoDJ cue types where it makes sense.

    Args:
        db_path: Path to the Mixxx SQLite database.

    Returns:
        Dict ``{absolute_track_path: [Cue, ...]}``.  Empty when the
        database is missing / unreadable.
    """
    if not db_path.exists():
        return {}

    out: dict[str, list[Cue]] = {}

    type_map = {
        1: "user",  # hot cue
        2: "first_downbeat",  # main / load cue ≈ where the DJ wants to start
        4: "user",  # loop — surface as a cue marker
        5: "user",  # jump
        6: "first_downbeat",  # intro start
        7: "first_downbeat",  # intro end
        8: "outro_downbeat",  # outro start
        9: "outro_downbeat",  # outro end
    }

    try:
        # read-only — we never write to a Mixxx database we don't own
        con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    except sqlite3.Error as exc:  # pragma: no cover — depends on local FS
        logger.debug("Could not open Mixxx db %s: %s", db_path, exc)
        return {}

    try:
        cur = con.cursor()
        cur.execute(
            "SELECT track_locations.location, cues.position, cues.type, cues.label "
            "FROM cues "
            "JOIN library ON library.id = cues.track_id "
            "JOIN track_locations ON track_locations.id = library.location",
        )
        for row in cur.fetchall():
            entry = _mixxx_row_to_cue(row, type_map)
            if entry is None:
                continue
            location, cue = entry
            out.setdefault(location, []).append(cue)
    except sqlite3.DatabaseError as exc:
        logger.debug("Mixxx db read failed: %s", exc)
    finally:
        with contextlib.suppress(sqlite3.Error):
            con.close()

    return _normalise_keys(out)


def _mixxx_row_to_cue(
    row: tuple[Any, ...],
    type_map: dict[int, str],
) -> tuple[str, Cue] | None:
    """Convert one Mixxx ``cues`` row into ``(track_path, Cue)`` or None."""
    location, position, ctype, label = row
    if not location or position is None:
        return None
    mapped = type_map.get(int(ctype) if ctype is not None else 0)
    if mapped is None:
        return None
    # Mixxx stores cue position in samples (44100 Hz stereo => 2 samples/frame).
    time_s = float(position) / (44100.0 * 2.0)
    if time_s < 0:
        return None
    return (
        location,
        Cue(time_s=time_s, type=mapped, label=str(label or ""), source="mixxx"),
    )


# ---------------------------------------------------------------------------
# Rekordbox — exported XML
# ---------------------------------------------------------------------------


def import_from_rekordbox_xml(xml_path: Path) -> dict[str, list[Cue]]:
    """Read cues from a Rekordbox ``Library.xml`` export.

    Rekordbox export structure::

        <COLLECTION>
          <TRACK Location="file://localhost/C:/Music/track.mp3"
                 Name="..." Artist="...">
             <POSITION_MARK Name="Hot Cue 1" Type="0" Start="12.345"/>
             ...
          </TRACK>
        </COLLECTION>

    ``Type`` enum:
        0 = hot cue, 1 = fade-in, 2 = fade-out, 3 = load, 4 = loop

    Args:
        xml_path: Path to the Rekordbox-exported XML file.

    Returns:
        Dict ``{absolute_track_path: [Cue, ...]}``.
    """
    if not xml_path.exists():
        return {}

    try:
        tree = ET.parse(xml_path)  # nosec B314 — local user-owned export
    except (OSError, ET.ParseError) as exc:
        logger.debug("Rekordbox XML unreadable: %s", exc)
        return {}

    out: dict[str, list[Cue]] = {}

    rb_type_map = {
        "0": "user",  # hot cue
        "1": "first_downbeat",  # fade-in
        "2": "outro_downbeat",  # fade-out
        "3": "first_downbeat",  # load
        "4": "user",  # loop
    }

    for track in tree.iter("TRACK"):
        location = track.get("Location") or ""
        if not location:
            continue
        path = _file_url_to_path(location)
        if not path:
            continue
        for mark in track.iter("POSITION_MARK"):
            cue = _rekordbox_mark_to_cue(mark, rb_type_map)
            if cue is not None:
                out.setdefault(path, []).append(cue)

    return _normalise_keys(out)


def _rekordbox_mark_to_cue(mark: ET.Element, rb_type_map: dict[str, str]) -> Cue | None:
    """Convert one Rekordbox ``POSITION_MARK`` element into a Cue."""
    try:
        start = float(mark.get("Start") or 0.0)
    except (TypeError, ValueError):
        return None
    mapped = rb_type_map.get(str(mark.get("Type") or "0"), "user")
    label = mark.get("Name") or ""
    color = ""
    r, g, b = mark.get("Red"), mark.get("Green"), mark.get("Blue")
    if r is not None and g is not None and b is not None:
        with contextlib.suppress(ValueError):
            color = f"#{int(r):02x}{int(g):02x}{int(b):02x}"
    return Cue(time_s=start, type=mapped, label=label, source="rekordbox", color=color)


# ---------------------------------------------------------------------------
# Traktor — collection.nml
# ---------------------------------------------------------------------------


def import_from_traktor_nml(nml_path: Path) -> dict[str, list[Cue]]:
    """Read cues from a Traktor ``collection.nml`` XML file.

    Traktor structure::

        <ENTRY ARTIST="..." TITLE="...">
          <LOCATION DIR="/:Music/:" FILE="track.mp3" VOLUME="C:"/>
          <CUE_V2 NAME="Hot Cue 1" TYPE="0" START="12345.6" .../>
        </ENTRY>

    ``START`` is in milliseconds.  ``TYPE`` enum: 0 = cue, 1 = fade-in,
    2 = fade-out, 3 = load, 4 = grid, 5 = loop.

    Args:
        nml_path: Path to the Traktor collection NML file.

    Returns:
        Dict ``{absolute_track_path: [Cue, ...]}``.
    """
    if not nml_path.exists():
        return {}

    try:
        tree = ET.parse(nml_path)  # nosec B314 — local user-owned export
    except (OSError, ET.ParseError) as exc:
        logger.debug("Traktor NML unreadable: %s", exc)
        return {}

    out: dict[str, list[Cue]] = {}
    nml_type_map = {
        "0": "user",
        "1": "first_downbeat",
        "2": "outro_downbeat",
        "3": "first_downbeat",
        "4": "phrase",
        "5": "user",
    }

    for entry in tree.iter("ENTRY"):
        full = _traktor_entry_to_path(entry)
        if full is None:
            continue
        for cue in entry.iter("CUE_V2"):
            parsed = _traktor_cue_to_cue(cue, nml_type_map)
            if parsed is not None:
                out.setdefault(full, []).append(parsed)

    return _normalise_keys(out)


def _traktor_entry_to_path(entry: ET.Element) -> str | None:
    """Build the absolute file path from a Traktor ``<ENTRY><LOCATION>`` element."""
    loc = entry.find("LOCATION")
    if loc is None:
        return None
    volume = loc.get("VOLUME") or ""
    directory = (loc.get("DIR") or "").replace("/:", "/").lstrip("/")
    filename = loc.get("FILE") or ""
    if not filename:
        return None
    # Traktor encodes paths with colon separators after each directory,
    # prefixed by the volume.  Windows VOLUME is e.g. "C:".
    full = (
        f"{volume}/{directory}{filename}" if volume and ":" in volume else f"/{directory}{filename}"
    )
    return full.replace("//", "/")


def _traktor_cue_to_cue(cue: ET.Element, nml_type_map: dict[str, str]) -> Cue | None:
    """Convert one Traktor ``<CUE_V2>`` element into a Cue."""
    try:
        start_ms = float(cue.get("START") or 0.0)
    except (TypeError, ValueError):
        return None
    mapped = nml_type_map.get(str(cue.get("TYPE") or "0"), "user")
    return Cue(
        time_s=start_ms / 1000.0,
        type=mapped,
        label=cue.get("NAME") or "",
        source="traktor",
    )


# ---------------------------------------------------------------------------
# Auto-discovery
# ---------------------------------------------------------------------------


def auto_import_cues(
    library_root: Path | None = None,
    extra_paths: list[Path] | None = None,
) -> dict[str, list[Cue]]:
    """Walk well-known locations and merge cue imports from every source found.

    Discovery order:

    1. Explicit *extra_paths* (absolute paths to ``mixxx.db`` /
        Rekordbox XML / Traktor NML files).  Always checked first.
    2. ``<library_root>/mixxx.db`` and ``<library_root>/Library.xml`` —
        a few users keep these next to their music tree.
    3. Default per-OS user-data locations:

        - Mixxx:     ``~/AppData/Local/Mixxx/mixxxdb.sqlite`` (Win),
                     ``~/Library/Containers/org.mixxx.mixxx/...`` (mac),
                     ``~/.mixxx/mixxxdb.sqlite`` (Linux).
        - Rekordbox: ``~/Documents/rekordbox/Library.xml`` (the user has
            to *export* this from Rekordbox; we cannot read the live db
            because it is encrypted).
        - Traktor:   ``~/Documents/Native Instruments/Traktor*/collection.nml``.

    Each reader is best-effort: a missing / corrupt source returns ``{}``
    and is silently skipped (DEBUG-logged).

    Args:
        library_root: Optional directory to scan first (typically the
            beets ``music_dir``).  Pass ``None`` to skip step 2.
        extra_paths: Explicit override paths.  Use this when the user
            has supplied a non-default location.

    Returns:
        Merged dict ``{absolute_track_path: [Cue, ...]}`` with cues
        from all discovered sources.  When the same track shows up in
        multiple sources, all cues are concatenated; deduplication by
        timestamp is left to :func:`autodj.dj_meta.merge_cues`.
    """
    candidates = _gather_candidate_paths(extra_paths, library_root)
    merged: dict[str, list[Cue]] = {}
    for path in candidates:
        if not path.exists():
            continue
        for track_path, cues in _import_one(path).items():
            merged.setdefault(track_path, []).extend(cues)
    return merged


def _gather_candidate_paths(
    extra_paths: list[Path] | None,
    library_root: Path | None,
) -> list[Path]:
    """Build the search-order list of cue-source candidate paths."""
    candidates: list[Path] = list(extra_paths or [])
    if library_root is not None and library_root.is_dir():
        for name in ("mixxx.db", "mixxxdb.sqlite", "Library.xml", "collection.nml"):
            p = library_root / name
            if p.exists():
                candidates.append(p)
    candidates.extend(_default_search_paths(Path.home()))
    return candidates


def _import_one(path: Path) -> dict[str, list[Cue]]:
    """Dispatch to the correct importer based on file suffix / name."""
    suffix = path.suffix.lower()
    name = path.name.lower()
    if suffix in (".db", ".sqlite") or "mixxx" in name:
        return import_from_mixxx(path)
    if suffix == ".xml":
        return import_from_rekordbox_xml(path)
    if suffix == ".nml":
        return import_from_traktor_nml(path)
    return {}


def _default_search_paths(home: Path) -> list[Path]:
    """Return the per-OS default locations to scan for DJ-software libraries."""
    out: list[Path] = []

    # Mixxx
    out.extend(
        [
            home / "AppData" / "Local" / "Mixxx" / "mixxxdb.sqlite",  # Windows
            home / ".mixxx" / "mixxxdb.sqlite",  # Linux
            home
            / "Library"
            / "Containers"
            / "org.mixxx.mixxx"
            / "Data"
            / "Library"
            / "Application Support"
            / "Mixxx"
            / "mixxxdb.sqlite",  # macOS
        ],
    )

    # Rekordbox — exported library XML.  Live db is encrypted; we only
    # support the manual export.
    out.append(home / "Documents" / "rekordbox" / "Library.xml")

    # Traktor — try common version directories
    traktor_root = home / "Documents" / "Native Instruments"
    if traktor_root.exists():
        for child in traktor_root.iterdir():
            if child.is_dir() and child.name.startswith("Traktor"):
                nml = child / "collection.nml"
                if nml.exists():
                    out.append(nml)

    return out


def _normalise_keys(by_path: dict[str, list[Cue]]) -> dict[str, list[Cue]]:
    """Normalise importer output keys to match index path strings.

    The FAISS index stores ``str(Path(absolute_path))`` -- native OS
    separators (``\\`` on Windows, ``/`` on POSIX) -- whereas the DJ-
    software importers emit forward-slashed paths regardless of host.
    Without this normalisation, ``_merge_external_cues_into`` looks up
    ``next_entry.path`` (e.g. ``C:\\music\\track.mp3``) but the dict
    keys are ``C:/music/track.mp3`` and the lookup silently misses.

    Sorts each cue list in place so the merge step gets monotonically
    increasing time_s values (handy for debugging and bisection).
    """
    out: dict[str, list[Cue]] = {}
    for raw, cues in by_path.items():
        if not raw:
            continue
        try:
            key = str(Path(raw))
        except (OSError, ValueError):
            key = raw
        cues.sort(key=lambda c: c.time_s)
        # Two importer entries pointing at the same normalised path
        # (e.g. mixed case on Windows) get their cue lists concatenated.
        out.setdefault(key, []).extend(cues)
    return out


def _file_url_to_path(url: str) -> str:
    """Convert a ``file://`` URL (Rekordbox-flavoured) to a native path string.

    Rekordbox writes URLs like
    ``file://localhost/C:/Users/Foo/Music/track.mp3`` on Windows and
    ``file://localhost/Users/Foo/Music/track.mp3`` on macOS.  We strip
    the scheme + host and url-decode the percent escapes.
    """
    from urllib.parse import unquote, urlparse

    try:
        parsed = urlparse(url)
    except ValueError:
        return ""
    if parsed.scheme != "file":
        return ""
    path = unquote(parsed.path or "")
    # Windows: leading slash before the drive letter — strip it.
    if len(path) >= 3 and path[0] == "/" and path[2] == ":":
        path = path[1:]
    return path
