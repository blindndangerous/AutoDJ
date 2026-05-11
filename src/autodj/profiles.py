"""Profile bundles — saved snapshots of preset + DJ-mix + playback settings.

A "profile" is a JSON file under ``<config_dir>/profiles/`` that
records every user-tunable knob from a single AutoDJ session so the
user can switch between, say, *Wakeup*, *Workout*, and *Late night*
without manually toggling thirty checkboxes each time.

This is distinct from:

- **Index name** (``autodj index --name X``): scopes a *separate
  library + FAISS index*.  Lets you keep e.g. an Ambient-only library
  alongside a main one.
- **Cue points**: per-track markers (drop, breakdown, phrase, outro
  downbeat) cached inside ``dj_meta.db``.

Profiles are pure config.  They reference an index name (so a profile
can pin "use the workout index") but don't store track data
themselves.

Example::

    >>> snap = ProfileSnapshot(
    ...     name="Late night",
    ...     index_name="ambient",
    ...     preset="wind_down",
    ...     bpm_lo=70, bpm_hi=110,
    ...     harmonic_mode="compatible",
    ...     transition_mode="full_intro_outro",
    ...     beat_sync_fx=True,
    ...     key_sync_fx=True,
    ... )
    >>> ProfileStore("./profiles").save(snap)
    >>> ProfileStore("./profiles").load("Late night")
    ProfileSnapshot(name='Late night', ...)
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path

logger = logging.getLogger(__name__)

# Allowed name pattern: letters, digits, dash, underscore, space.
# Limits filename collisions on Windows + UNIX.
_NAME_RE = re.compile(r"^[A-Za-z0-9 _\-]{1,64}$")


def validate_name(name: str) -> str:
    """Return *name* if it's a safe profile filename; raise ``ValueError`` otherwise."""
    if not _NAME_RE.match(name):
        raise ValueError(
            "Profile name must be 1-64 chars from letters, digits, space, dash, underscore.",
        )
    return name


@dataclass
class ProfileSnapshot:
    """A serialisable bundle of user settings.

    Every field is optional — when restoring, only set fields are
    applied so a partial profile (e.g. just BPM range + preset)
    leaves untouched fields alone.

    Attributes:
        name: User-facing label.  Doubles as the filename stem.
        index_name: Name of the index (library) this profile is
            bound to, or ``None`` for "current index".
        preset: Built-in preset name, or ``None``.
        bpm_lo, bpm_hi: BPM range filter.
        harmonic_mode: Camelot harmonic-mode key.
        transition_mode: Transition mode key.
        beat_sync_fx: Beat-sync FX toggle.
        key_sync_fx: Key-sync FX toggle.
        beatmatch_on_skip: Beatmatch-on-skip toggle.
        crossfade_seconds: Crossfade window length.
        smart_shuffle: Smart-shuffle toggle.
        pure_shuffle: Pure-shuffle toggle.
        anchor_to_seed: Anchor-to-seed toggle.
        enable_daypart: Daypart toggle.
        enable_mood_arc: Mood-arc toggle.
        mood_arc_hours: Mood-arc duration.
        liners_enabled: Voice liner master toggle.
        liners_pick_mode: Liner rotation mode.
    """

    name: str
    index_name: str | None = None
    preset: str | None = None
    bpm_lo: float | None = None
    bpm_hi: float | None = None
    harmonic_mode: str | None = None
    transition_mode: str | None = None
    post_queue_seed: str | None = None
    beat_sync_fx: bool | None = None
    key_sync_fx: bool | None = None
    beatmatch_on_skip: bool | None = None
    crossfade_seconds: float | None = None
    fade_in_seconds: float | None = None
    smart_shuffle: bool | None = None
    pure_shuffle: bool | None = None
    anchor_to_seed: bool | None = None
    enable_daypart: bool | None = None
    enable_mood_arc: bool | None = None
    mood_arc_hours: float | None = None
    liners_enabled: bool | None = None
    liners_pick_mode: str | None = None
    # Free-form extension dict so future fields don't break old files.
    extra: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        """Return a plain JSON-serialisable representation."""
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> ProfileSnapshot:
        """Inverse of :meth:`to_dict`.

        Tolerates unknown keys (forward compatibility) by routing them
        into ``extra``.
        """
        known = {f for f in cls.__dataclass_fields__ if f != "extra"}
        kwargs: dict = {}
        extra: dict = dict(data.get("extra") or {})
        for k, v in data.items():
            if k == "extra":
                continue
            if k in known:
                kwargs[k] = v
            else:
                extra[k] = v
        kwargs["extra"] = extra
        return cls(**kwargs)


@dataclass
class ProfileStore:
    """JSON-file-backed registry of :class:`ProfileSnapshot` objects.

    One file per profile, at ``<root>/<name>.json``.  Names are
    validated via :func:`validate_name` so the filesystem can't be
    coerced into a path-traversal write.
    """

    root: Path

    def __post_init__(self) -> None:
        """Coerce string roots to Path so callers can pass either."""
        self.root = Path(self.root)

    def _path_for(self, name: str) -> Path:
        """Return the on-disk JSON path for the profile *name*."""
        validate_name(name)
        return self.root / f"{name}.json"

    def list_names(self) -> list[str]:
        """Return all profile names sorted ascending."""
        if not self.root.exists():
            return []
        out: list[str] = []
        for p in self.root.glob("*.json"):
            stem = p.stem
            if _NAME_RE.match(stem):
                out.append(stem)
        out.sort(key=str.lower)
        return out

    def save(self, snapshot: ProfileSnapshot) -> Path:
        """Write *snapshot* to disk; returns the resolved path."""
        target = self._path_for(snapshot.name)
        self.root.mkdir(parents=True, exist_ok=True)
        target.write_text(
            json.dumps(snapshot.to_dict(), indent=2, sort_keys=True),
            encoding="utf-8",
        )
        return target

    def load(self, name: str) -> ProfileSnapshot:
        """Read and return the snapshot named *name*.

        Raises:
            FileNotFoundError: When the profile does not exist.
        """
        target = self._path_for(name)
        if not target.is_file():
            raise FileNotFoundError(f"Profile not found: {name}")
        data = json.loads(target.read_text(encoding="utf-8"))
        return ProfileSnapshot.from_dict(data)

    def delete(self, name: str) -> bool:
        """Remove the profile named *name*; returns True when a file was removed."""
        target = self._path_for(name)
        if not target.is_file():
            return False
        target.unlink()
        return True


__all__ = [
    "ProfileSnapshot",
    "ProfileStore",
    "validate_name",
]
