"""Voice liners — DJ-style spoken drops layered over the live mix.

A "liner" is a short audio clip (e.g. ``"You're listening to AutoDJ FM"``)
that plays over the top of the currently-playing track every so often
to give the impression of a real station.  This module owns:

- File discovery in the configured liner folder.
- Trigger evaluation (every N tracks, every X minutes, random window).
- Pick rotation (random / sequential / weighted).

Playback itself happens in the browser (Web Audio decoding the raw
file bytes from ``GET /api/liner/<index>`` and ducking the active deck
during the overlay).  This module is dependency-free apart from the
stdlib so it can be unit-tested without spinning up a player.

Example:
    >>> from autodj.liners import LinerLibrary, LinerTrigger
    >>> lib = LinerLibrary.from_folder(Path("./liners"))
    >>> trigger = LinerTrigger(every_n_songs=5, every_minutes=None,
    ...                         random_min_minutes=None,
    ...                         random_max_minutes=None)
    >>> trigger.should_fire(track_count=5, minutes_since_last=2.0)
    True
"""

from __future__ import annotations

import logging
import random
from dataclasses import dataclass, field
from pathlib import Path

logger = logging.getLogger(__name__)

# File extensions recognised as liner clips.
LINER_EXTS: tuple[str, ...] = (".mp3", ".wav", ".ogg", ".m4a", ".flac", ".aac")


@dataclass
class LinerTrigger:
    """Trigger configuration for when a liner should fire.

    Multiple modes can be active at once; the first one to fire wins
    each evaluation tick.

    Attributes:
        every_n_songs: Fire after every Nth track advance.  ``None`` =
            track-count trigger disabled.
        every_minutes: Fire after this many wall-clock minutes since
            the last liner.  ``None`` = time-based trigger disabled.
        random_min_minutes: Minimum delay for random-window trigger.
            Both ``random_min_minutes`` and ``random_max_minutes`` must
            be set for the random trigger to be active.
        random_max_minutes: Maximum delay for random-window trigger.
        enabled: Master on/off switch.  When ``False``, ``should_fire``
            always returns ``False`` regardless of the trigger fields.
    """

    every_n_songs: int | None = None
    every_minutes: float | None = None
    random_min_minutes: float | None = None
    random_max_minutes: float | None = None
    enabled: bool = False

    def should_fire(
        self,
        *,
        track_count: int,
        minutes_since_last: float,
        random_target_minutes: float | None = None,
    ) -> bool:
        """Return ``True`` when *any* configured trigger condition is met.

        Args:
            track_count: Number of tracks played since the LAST liner
                fired (not since session start).
            minutes_since_last: Wall-clock minutes since the last liner.
            random_target_minutes: Pre-rolled random target from
                :meth:`roll_random_target`, or ``None`` when no random
                window has been rolled yet.
        """
        if not self.enabled:
            return False
        if (
            self.every_n_songs is not None
            and self.every_n_songs > 0
            and track_count >= self.every_n_songs
        ):
            return True
        if (
            self.every_minutes is not None
            and self.every_minutes > 0
            and minutes_since_last >= self.every_minutes
        ):
            return True
        return (
            random_target_minutes is not None
            and random_target_minutes > 0
            and minutes_since_last >= random_target_minutes
        )

    def roll_random_target(self, *, rng: random.Random | None = None) -> float | None:
        """Roll a fresh random delay in the configured min/max window.

        Returns ``None`` when the random trigger is not configured.
        """
        if (
            self.random_min_minutes is None
            or self.random_max_minutes is None
            or self.random_min_minutes > self.random_max_minutes
            or self.random_max_minutes <= 0
        ):
            return None
        r = rng if rng is not None else random
        return float(r.uniform(self.random_min_minutes, self.random_max_minutes))


@dataclass
class LinerLibrary:
    """Discovered liner clips on disk + a rotation cursor.

    The rotation modes match what the web UI exposes:

    - ``random``: pick uniformly at random each time.
    - ``sequential``: play in directory-listing order, wrap at end.
    - ``weighted``: random pick weighted by ``weights`` (parallel list,
      defaults to all 1.0 for uniform when no weights are set).

    Attributes:
        folder: Source directory.  ``None`` when the library was
            constructed in-memory for testing.
        files: Discovered clip paths sorted ascending (case-insensitive).
        weights: Optional per-file weight (parallel to ``files``).
        cursor: Sequential-mode cursor; ignored by other modes.
    """

    folder: Path | None = None
    files: list[Path] = field(default_factory=list)
    weights: list[float] = field(default_factory=list)
    cursor: int = 0

    @classmethod
    def from_folder(cls, folder: Path) -> LinerLibrary:
        """Walk *folder* and collect every audio file with a known extension.

        Recursive: any file under *folder* matching :data:`LINER_EXTS`
        is included.  Returns an empty library when the folder is
        missing or empty — callers fall back to no-op rather than
        raising.
        """
        if not folder.exists() or not folder.is_dir():
            logger.debug("Liner folder %s missing or not a directory", folder)
            return cls(folder=folder, files=[], weights=[])
        files: list[Path] = []
        for p in folder.rglob("*"):
            if p.is_file() and p.suffix.lower() in LINER_EXTS:
                files.append(p)
        files.sort(key=lambda f: f.name.lower())
        return cls(folder=folder, files=files, weights=[1.0] * len(files))

    def pick(
        self,
        mode: str = "random",
        *,
        rng: random.Random | None = None,
    ) -> Path | None:
        """Return the next liner path under *mode* rotation.

        Args:
            mode: One of ``"random"``, ``"sequential"``, ``"weighted"``.
                Unknown modes fall back to ``"random"``.
            rng: Inject a deterministic RNG for testing.

        Returns:
            Selected path, or ``None`` when the library is empty.
        """
        if not self.files:
            return None
        r = rng if rng is not None else random
        if mode == "sequential":
            pick = self.files[self.cursor % len(self.files)]
            self.cursor = (self.cursor + 1) % len(self.files)
            return pick
        if mode == "weighted" and self.weights and len(self.weights) == len(self.files):
            total = sum(self.weights)
            if total <= 0:
                return r.choice(self.files)
            target = r.uniform(0, total)
            running = 0.0
            for f, w in zip(self.files, self.weights, strict=False):
                running += w
                if running >= target:
                    return f
            return self.files[-1]
        # Default: random
        return r.choice(self.files)


__all__ = [
    "LINER_EXTS",
    "LinerLibrary",
    "LinerTrigger",
]
