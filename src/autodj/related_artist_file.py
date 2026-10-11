"""Install the editable related-artists file on first use."""

from __future__ import annotations

import importlib.resources
import tomllib
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from autodj.repeat_rules import RelatedArtists

DEFAULT_RELATED_ARTISTS_FILE = "related-artists.toml"


def invalid_related_artists_message(exc: OSError | ValueError) -> str:
    """Say what is wrong with the file without quoting any of it.

    For the browser and the Repeats panel: the message names no path,
    artist or key from the file, only the line of a TOML syntax error.
    """
    from autodj.repeat_rules import RelatedArtistsFileTooLargeError

    if isinstance(exc, RelatedArtistsFileTooLargeError):
        return "The related artists file is larger than 1 MiB."
    if isinstance(exc, OSError):
        return "The related artists file could not be read."
    cause = exc.__cause__ if isinstance(exc.__cause__, tomllib.TOMLDecodeError) else exc
    line = getattr(cause, "lineno", None)
    return f"The related artists file is not valid{f': line {line}' if line else ''}."


def unused_related_artists_message(exc: OSError | ValueError) -> str:
    """The Repeats panel's status line for a file AutoDJ started without."""
    return (
        f"{invalid_related_artists_message(exc)} Related artists are off until the file "
        "is fixed and repeat settings are saved."
    )


def ensure_related_artists_file(active_dir: Path) -> Path:
    """Return the active index file, installing the bundled sample if absent."""
    destination = Path(active_dir) / DEFAULT_RELATED_ARTISTS_FILE
    destination.parent.mkdir(parents=True, exist_ok=True)
    sample = importlib.resources.files("autodj").joinpath("data", DEFAULT_RELATED_ARTISTS_FILE)
    if destination.exists():
        return destination
    contents = sample.read_bytes()
    try:
        with destination.open("xb") as target:
            target.write(contents)
    except FileExistsError:
        pass
    return destination


def load_configured_related_artists(active_dir: Path | None) -> RelatedArtists:
    """Load user links, using an empty set when no index folder is configured."""
    from autodj.repeat_rules import RelatedArtists, load_related_artists

    if active_dir is None:
        return RelatedArtists()
    path = ensure_related_artists_file(active_dir)
    return load_related_artists(path)
