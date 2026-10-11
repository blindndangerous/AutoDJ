"""Install the editable related-artists file on first use."""

from __future__ import annotations

import hashlib
import importlib.resources
import json
import os
import tomllib
from collections.abc import Mapping
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from autodj.repeat_rules import RelatedArtists

DEFAULT_RELATED_ARTISTS_FILE = "related-artists.toml"


def ensure_related_artists_file(active_dir: Path, configured: str = "") -> Path:
    """Return the configured file, or install the bundled sample if absent."""
    if configured:
        return Path(configured).expanduser()

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


def load_configured_related_artists(
    active_dir: Path | None, configured: str = ""
) -> RelatedArtists:
    """Load user links, using an empty set when no index folder is configured."""
    from autodj.repeat_rules import RelatedArtists, load_related_artists

    if active_dir is None and not configured:
        return RelatedArtists()
    path = ensure_related_artists_file(active_dir or Path(), configured)
    return load_related_artists(path)


def migrate_legacy_related_file(
    active_dir: Path | None, legacy: Mapping[str, object]
) -> str | None:
    """Copy legacy starter links together with a custom file, leaving it untouched."""
    source = legacy.get("related_artists_file")
    if active_dir is None or not legacy.get("related_artists_starter") or not source:
        return None
    if not isinstance(source, str):
        raise ValueError("playback.related_artists_file must be a file path")

    from autodj.repeat_rules import load_related_artists, parse_related_artists

    source_path = Path(source).expanduser()
    identity = os.path.normcase(str(source_path.resolve(strict=False)))
    digest = hashlib.sha256(identity.encode("utf-8")).hexdigest()[:16]
    destination = Path(active_dir) / f"related-artists-migrated-{digest}.toml"
    if destination.exists():
        return str(destination)

    sample = importlib.resources.files("autodj").joinpath("data", DEFAULT_RELATED_ARTISTS_FILE)
    related = parse_related_artists(
        tomllib.loads(sample.read_text(encoding="utf-8")), "bundled sample"
    ).merged(load_related_artists(source_path))
    names = related.names

    def quote(text: str) -> str:
        return json.dumps(text, ensure_ascii=False)

    lines = [
        "single_acts = ["
        + ", ".join(quote(name) for name in dict.fromkeys(related.single_acts))
        + "]",
        "",
        "[related]",
    ]
    for artist, targets in sorted(related.links.items()):
        if targets:
            lines.append(
                f"{quote(names.get(artist, artist))} = "
                + "["
                + ", ".join(quote(names.get(target, target)) for target in sorted(targets))
                + "]"
            )
    destination.parent.mkdir(parents=True, exist_ok=True)
    try:
        with destination.open("x", encoding="utf-8", newline="\n") as handle:
            handle.write("\n".join(lines) + "\n")
    except FileExistsError:
        pass
    return str(destination)
