"""BPM-shaping preset envelopes for AutoDJ sessions.

A preset steers which tracks the DJ picks next by biasing the FAISS
similarity search toward a target BPM that may evolve over time.

Built-in presets cover common scenarios (wakeup, workout, etc.).
Users can add their own in ``config.toml`` under ``[presets.*]`` sections.
Only one field is required, the rest are inferred:

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
from collections.abc import Callable, Mapping
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
    """

    name: str
    bpm_weight: float
    _curve: Callable[[int], float | None] = field(repr=False)
    discovery_every: int | None = None

    def target_bpm(self, track_number: int) -> float | None:
        """Return the target BPM at *track_number* in the session.

        Args:
            track_number: Zero-based count of tracks auto-picked so far.

        Returns:
            Target BPM (float), or ``None`` if this preset has no BPM curve.
        """
        return self._curve(track_number)


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


_PRESET_KEYS = frozenset(
    {
        "bpm_target",
        "bpm_start",
        "bpm_end",
        "curve",
        "bpm_weight",
        "horizon_tracks",
        "discovery_every",
    }
)


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
        ValueError: If the section holds an unknown key or a nested table,
            or required BPM fields are missing.
    """
    nested = sorted(key for key, value in section.items() if isinstance(value, dict))
    if nested:
        raise ValueError(
            f"Preset '{name}': nested tables {nested} are not preset keys; each preset "
            "is one [presets.NAME] table in config.toml."
        )
    unknown = sorted(set(section) - _PRESET_KEYS)
    if unknown:
        raise ValueError(f"Preset '{name}': unknown keys {unknown}")
    horizon: int = int(section.get("horizon_tracks", 30))
    discovery_every_raw = section.get("discovery_every")
    discovery_every: int | None = (
        int(discovery_every_raw) if discovery_every_raw is not None else None
    )

    curve_name: str | None = section.get("curve")
    bpm_target = section.get("bpm_target")
    bpm_start = section.get("bpm_start")
    bpm_end = section.get("bpm_end")

    if curve_name == "slide":
        lo = float(bpm_start if bpm_start is not None else 80)
        pk = float(bpm_end if bpm_end is not None else (bpm_target or 130))
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
    )


def load_user_presets(sections: Mapping[str, Any]) -> dict[str, Preset]:
    """Load user-defined presets from ``config.toml``'s ``presets`` table.

    Sections that fail to parse are skipped with a warning so a typo
    in one preset doesn't kill the whole load.

    Args:
        sections: Preset name → parsed TOML table.

    Returns:
        Dict of preset name → :class:`Preset`.
    """
    result: dict[str, Preset] = {}
    for name, section in sections.items():
        if not isinstance(section, dict):
            logger.warning("Skipping invalid preset '%s': not a table", name)
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
