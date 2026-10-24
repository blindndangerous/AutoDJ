"""The station name listeners see while a clockwheel or a show is on the air (#472).

A clockwheel and a show may each carry a "station name while active".  The
show on the air comes first, then the clockwheel picking the songs; a blank
name at either level leaves the one below it, and with neither the station
keeps its normal name (``[stream] station_name``).  The normal name is never
written over: the name on the air is held apart from it, on the player bridge,
and every place that publishes the name reads it through :func:`listener_name`.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any

logger = logging.getLogger(__name__)

#: The longest station name, as Settings, Stream allows.
STATION_NAME_MAX = 100
#: The label the clockwheel and show editors give the field.
LABEL = "Station name while active"


def checked_name(value: Any) -> str:
    """*value* stripped, for a clockwheel's or a show's station name; blank is allowed.

    Raises:
        ValueError: It is not text, is longer than :data:`STATION_NAME_MAX`
            characters, or holds control characters (it is sent as a header).
    """
    if not isinstance(value, str):
        raise ValueError(f"{LABEL} must be text")
    if len(value) > STATION_NAME_MAX:
        raise ValueError(f"{LABEL} can be at most {STATION_NAME_MAX} characters")
    name = value.strip()
    if not name.isprintable():
        raise ValueError(f"{LABEL} cannot contain tabs, line breaks or other control characters")
    return name


def _text(value: Any) -> str:
    # A test double's player or runner may answer with something else.
    return value if isinstance(value, str) else ""


def programmed_name(player: Any, show_runner: Any) -> str:
    """The name the programming on the air asks for, or ``""`` for the normal name.

    Blocking: the show's name is read from the shows file, which may have changed.
    """
    if show_runner is not None:
        name = _text(show_runner.on_air_station_name())
        if name:
            return name
    clock = getattr(player, "clock_station_name", None)
    return _text(clock()) if callable(clock) else ""


def listener_name(bridge: Any) -> str:
    """The station name to publish now: the programming's, or the normal name."""
    override = _text(getattr(bridge, "station_name_override", ""))
    return override or str(bridge.player._cfg.stream.station_name)


def sync_name(
    bridge: Any,
    show_runner: Any,
    refresh: Callable[[], None],
    reconnect: Callable[[], None],
) -> bool:
    """Publish the name the programming asks for; whether the published name changed.

    A change refreshes the now-playing feed (*refresh*) and reconnects the
    relays (*reconnect*), which take a new name only when they connect, as
    they do when the station name is saved in Settings.  Blocking.
    """
    wanted = programmed_name(bridge.player, show_runner)
    before = listener_name(bridge)
    bridge.station_name_override = wanted
    after = listener_name(bridge)
    if after == before:
        return False
    logger.info(
        "Station name on the air: %s%s", after, "" if wanted else " (the station's normal name)"
    )
    refresh()
    reconnect()
    return True
