"""BPM-shaping preset envelopes for AutoDJ sessions.

A preset steers which tracks the DJ picks next by biasing the FAISS
similarity search toward a target BPM that may evolve over time.

Built-in presets cover common scenarios (wakeup, workout, etc.).
Users can add their own in ``config.toml`` under ``[presets.*]`` sections —
only one field is required, the rest are inferred:

.. code-block:: toml

    [presets.focus]
    bpm_target = 90               # constant — that's it

    [presets.warmup]
    bpm_start = 70
    bpm_end   = 130               # linear ramp inferred

    [presets.festival]
    bpm_start      = 90
    bpm_end        = 145
    curve          = "slide"
    bpm_weight     = 0.35
    horizon_tracks = 60
    discovery_every = 10

Example:
    >>> from autodj.presets import get_preset
    >>> p = get_preset("wakeup")
    >>> p.target_bpm(0)
    70.0
    >>> p.target_bpm(15)
    100.0
    >>> p.target_bpm(30)
    130.0
"""

from __future__ import annotations

import logging
import math
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Dataclass
# ---------------------------------------------------------------------------


@dataclass
class Preset:
    """A named BPM-shaping envelope for a DJ session.

    Attributes:
        name: Human-readable preset name.
        bpm_weight: How much BPM similarity influences track selection (0.0–1.0).
            Higher values = tighter BPM matching; 0.0 = pure sonic similarity.
        discovery_every: If set, a sonically distant "discovery" track is
            injected every *discovery_every* tracks.  ``None`` disables
            discovery for this preset.  The user must also toggle discovery
            ON at runtime (``D`` key or web UI button) before it fires.
        genres: Optional list of genre substrings to restrict picks to.
            Case-insensitive substring match against ``IndexEntry.genre``.
            Example: ``["electronic", "house"]`` matches "Deep House",
            "Electronica", etc.  Empty list = no filter.
    """

    name: str
    bpm_weight: float
    _curve: Callable[[int], float | None] = field(repr=False)
    discovery_every: int | None = None
    genres: list[str] = field(default_factory=list)

    def target_bpm(self, track_number: int) -> float | None:
        """Return the target BPM at *track_number* in the session.

        Args:
            track_number: Zero-based count of tracks auto-picked so far.

        Returns:
            Target BPM (float), or ``None`` if this preset has no BPM curve.
        """
        return self._curve(track_number)

    def matches_genre(self, entry_genre: str) -> bool:
        """Return ``True`` if *entry_genre* matches any of this preset's genres.

        Case-insensitive substring match.  Empty :attr:`genres` always
        returns ``True`` (no filter).  Empty *entry_genre* returns ``False``
        when a filter is in effect (unknown-genre tracks are excluded).

        Args:
            entry_genre: The candidate track's genre string.

        Returns:
            ``True`` if the candidate passes the genre filter.
        """
        if not self.genres:
            return True
        if not entry_genre:
            return False
        eg = entry_genre.lower()
        return any(g.lower() in eg for g in self.genres)


# ---------------------------------------------------------------------------
# Curve constructors
# ---------------------------------------------------------------------------


def constant_curve(bpm: float) -> Callable[[int], float]:
    """Return a curve that always returns *bpm*, regardless of track number."""
    bpm = float(bpm)

    def _curve(track_number: int) -> float:
        return bpm

    return _curve


def linear_curve(start: float, end: float, horizon: int = 30) -> Callable[[int], float]:
    """Return a curve that ramps linearly from *start* to *end* over *horizon* tracks.

    Past *horizon* the value plateaus at *end*.

    Args:
        start: BPM at track 0.
        end: BPM at *horizon* and beyond.
        horizon: Number of tracks over which the ramp occurs.
    """

    def _curve(track_number: int) -> float:
        t = min(track_number, horizon) / max(1, horizon)
        return start + (end - start) * t

    return _curve


def slide_curve(low: float, peak: float, horizon: int = 40) -> Callable[[int], float]:
    """Return a sine-arch that rises from *low* to *peak* then falls back to *low*.

    The arch peaks at the midpoint of *horizon*.  Past *horizon* the value
    returns to *low* and stays there.

    Args:
        low: BPM at track 0 and at *horizon*+.
        peak: BPM at the midpoint (track *horizon* // 2).
        horizon: Total number of tracks for the full arch.
    """

    def _curve(track_number: int) -> float:
        t = min(track_number, horizon) / max(1, horizon)
        return low + (peak - low) * math.sin(math.pi * t)

    return _curve


# ---------------------------------------------------------------------------
# Built-in presets
# ---------------------------------------------------------------------------


BUILTIN_PRESETS: dict[str, Preset] = {
    "wakeup": Preset(
        name="wakeup",
        bpm_weight=0.30,
        _curve=linear_curve(70, 130, horizon=30),
    ),
    "winddown": Preset(
        name="winddown",
        bpm_weight=0.30,
        _curve=linear_curve(130, 70, horizon=30),
    ),
    "sleep": Preset(
        name="sleep",
        bpm_weight=0.20,
        _curve=linear_curve(85, 55, horizon=40),
    ),
    "morning": Preset(
        name="morning",
        bpm_weight=0.15,
        _curve=linear_curve(60, 95, horizon=30),
    ),
    "slide": Preset(
        name="slide",
        bpm_weight=0.25,
        _curve=slide_curve(80, 135, horizon=40),
    ),
    "party": Preset(
        name="party",
        bpm_weight=0.30,
        _curve=constant_curve(128),
    ),
    "workout": Preset(
        name="workout",
        bpm_weight=0.40,
        _curve=constant_curve(145),
    ),
    "chill": Preset(
        name="chill",
        bpm_weight=0.20,
        _curve=constant_curve(75),
    ),
    "focus": Preset(
        name="focus",
        bpm_weight=0.10,
        _curve=constant_curve(85),
    ),
    "driving": Preset(
        name="driving",
        bpm_weight=0.25,
        _curve=constant_curve(112),
    ),
}


# ---------------------------------------------------------------------------
# User preset loading
# ---------------------------------------------------------------------------


def preset_from_config(name: str, section: dict[str, Any]) -> Preset:
    """Build a :class:`Preset` from a ``[presets.NAME]`` TOML section dict.

    Inference rules (applied in order):

    1. ``bpm_target`` only → ``curve = "constant"``, default weight 0.25
    2. ``bpm_start`` + ``bpm_end`` (or ``curve = "linear"``) → linear, default weight 0.30
    3. ``curve = "slide"`` → sine arch using ``bpm_start`` / ``bpm_end``, default weight 0.25
    4. ``horizon_tracks`` defaults to 30
    5. ``discovery_every`` defaults to ``None``

    Args:
        name: The preset name (the TOML sub-key, e.g. ``"focus"``).
        section: Dict of keys from the ``[presets.NAME]`` section.

    Returns:
        A :class:`Preset` instance.

    Raises:
        ValueError: If required BPM fields are missing or the curve name is unknown.
    """
    horizon: int = int(section.get("horizon_tracks", 30))
    discovery_every_raw = section.get("discovery_every")
    discovery_every: int | None = (
        int(discovery_every_raw) if discovery_every_raw is not None else None
    )
    genres_raw = section.get("genres", [])
    if isinstance(genres_raw, str):
        genres = [genres_raw]
    elif isinstance(genres_raw, list):
        genres = [str(g) for g in genres_raw]
    else:
        genres = []

    curve_name: str | None = section.get("curve")
    bpm_target = section.get("bpm_target")
    bpm_start = section.get("bpm_start")
    bpm_end = section.get("bpm_end")

    if curve_name == "slide":
        lo = float(bpm_start if bpm_start is not None else section.get("bpm_low", 80))
        pk = float(bpm_end if bpm_end is not None else section.get("bpm_peak", bpm_target or 130))
        curve: Callable[[int], float] = slide_curve(lo, pk, horizon=horizon)
        default_weight = 0.25

    elif curve_name in ("linear", None) and bpm_start is not None and bpm_end is not None:
        curve = linear_curve(float(bpm_start), float(bpm_end), horizon=horizon)
        default_weight = 0.30

    elif bpm_target is not None:
        curve = constant_curve(float(bpm_target))
        default_weight = 0.25

    elif curve_name == "linear":
        raise ValueError(f"Preset '{name}': curve='linear' requires both bpm_start and bpm_end.")

    else:
        raise ValueError(
            f"Preset '{name}': must specify bpm_target, bpm_start+bpm_end, "
            "or curve='slide' with bpm_start."
        )

    weight = float(section.get("bpm_weight", default_weight))
    return Preset(
        name=name,
        bpm_weight=weight,
        _curve=curve,
        discovery_every=discovery_every,
        genres=genres,
    )


def load_user_presets(raw_config: dict[str, Any]) -> dict[str, Preset]:
    """Load user-defined presets from a parsed TOML dict.

    Accepts two layouts:

    - **Sidecar / bare form** (``presets.toml``)::

        [wakeup]
        type = "ramp_up"
        ...

      Top-level keys are preset names directly.

    - **Legacy / inline form** (``config.toml``)::

        [presets.wakeup]
        type = "ramp_up"
        ...

      Wrapped under a ``[presets]`` table.

    Sections that fail to parse are skipped with a warning so a typo
    in one preset doesn't kill the whole load.

    Args:
        raw_config: The parsed TOML dict.

    Returns:
        Dict of preset name → :class:`Preset`.  Empty dict if neither
        layout produces any valid preset.
    """
    if "presets" in raw_config and isinstance(raw_config["presets"], dict):
        presets_raw = raw_config["presets"]
    else:
        # Treat every top-level table as a preset entry.
        presets_raw = {k: v for k, v in raw_config.items() if isinstance(v, dict)}

    result: dict[str, Preset] = {}
    for name, section in presets_raw.items():
        if not isinstance(section, dict):
            continue
        try:
            result[name] = preset_from_config(name, section)
        except (ValueError, KeyError, TypeError) as exc:
            logger.warning("Skipping invalid preset '%s': %s", name, exc)
    return result


def get_preset(
    name: str,
    user_presets: dict[str, Preset] | None = None,
) -> Preset:
    """Look up a preset by name, user presets taking priority over built-ins.

    Args:
        name: Preset name to look up (case-sensitive).
        user_presets: Optional dict of user-defined presets from ``config.toml``.

    Returns:
        The matching :class:`Preset`.

    Raises:
        ValueError: If the name is not found.  The error message lists all
            available preset names so the user can correct the typo.
    """
    if user_presets and name in user_presets:
        return user_presets[name]
    if name in BUILTIN_PRESETS:
        return BUILTIN_PRESETS[name]

    available = sorted(set(BUILTIN_PRESETS) | set(user_presets or {}))
    raise ValueError(f"Unknown preset '{name}'. Available: {', '.join(available)}")
