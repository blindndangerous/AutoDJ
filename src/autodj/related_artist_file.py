"""Install the editable related artists and related tracks files on first use."""

from __future__ import annotations

import importlib.resources
import tomllib
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from autodj.repeat_rules import RelatedArtists, RelatedTracks

DEFAULT_RELATED_ARTISTS_FILE = "related-artists.toml"
DEFAULT_RELATED_TRACKS_FILE = "related-tracks.toml"


def invalid_related_artists_message(exc: OSError | ValueError, kind: str = "artists") -> str:
    """Say what is wrong with the file without quoting any of it.

    For the browser and the Repeats panel: the message names no path,
    artist, track or key from the file, only the line of a TOML syntax
    error.  *kind* is ``"artists"`` or ``"tracks"``.
    """
    from autodj.repeat_rules import RelatedArtistsFileTooLargeError, RelatedTracksFileTooLargeError

    if isinstance(exc, RelatedArtistsFileTooLargeError | RelatedTracksFileTooLargeError):
        return f"The related {kind} file is larger than 1 MiB."
    if isinstance(exc, OSError):
        return f"The related {kind} file could not be read."
    cause = exc.__cause__ if isinstance(exc.__cause__, tomllib.TOMLDecodeError) else exc
    line = getattr(cause, "lineno", None)
    return f"The related {kind} file is not valid{f': line {line}' if line else ''}."


def unused_related_artists_message(exc: OSError | ValueError, kind: str = "artists") -> str:
    """The Repeats panel's status line for a file AutoDJ started without."""
    return (
        f"{invalid_related_artists_message(exc, kind)} Related {kind} are off until the file "
        "is fixed and repeat settings are saved."
    )


def _ensure_file(active_dir: Path, name: str) -> Path:
    """Return the active index file *name*, installing the bundled sample if absent."""
    destination = Path(active_dir) / name
    destination.parent.mkdir(parents=True, exist_ok=True)
    sample = importlib.resources.files("autodj").joinpath("data", name)
    if destination.exists():
        return destination
    contents = sample.read_bytes()
    try:
        with destination.open("xb") as target:
            target.write(contents)
    except FileExistsError:
        pass
    return destination


def ensure_related_artists_file(active_dir: Path) -> Path:
    """Return the active index file, installing the bundled sample if absent."""
    return _ensure_file(active_dir, DEFAULT_RELATED_ARTISTS_FILE)


def ensure_related_tracks_file(active_dir: Path) -> Path:
    """Return the active index's related tracks file, installing the bundled sample if absent."""
    return _ensure_file(active_dir, DEFAULT_RELATED_TRACKS_FILE)


def load_configured_related_artists(active_dir: Path | None) -> RelatedArtists:
    """Load user links, using an empty set when no index folder is configured."""
    from autodj.repeat_rules import RelatedArtists, load_related_artists

    if active_dir is None:
        return RelatedArtists()
    path = ensure_related_artists_file(active_dir)
    return load_related_artists(path)


def load_configured_related_tracks(active_dir: Path | None) -> RelatedTracks:
    """Load the related track groups, empty when no index folder is configured."""
    from autodj.repeat_rules import RelatedTracks, load_related_tracks

    if active_dir is None:
        return RelatedTracks()
    return load_related_tracks(ensure_related_tracks_file(active_dir))
