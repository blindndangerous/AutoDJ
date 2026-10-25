"""The song picked to follow the playing one, kept across a restart (#584).

A station's first pick after a restart works out every category and
clockwheel condition over the whole library, which takes long enough on a
large library that the station stays silent.  The song the clockwheel had
already picked to follow the playing one obeyed the clockwheel and the
repeat rules when it was picked, so a restart soon after can play it
first, at once, while the full pick works out what follows it.

The pick is kept as a station setting in the ledger, which buffers it and
writes it on the library's tick, so nothing new runs on the mix bus.  It
is used at most once, at the first start pick after the player loads, and
only when every check in :func:`refusal` passes.
"""

from __future__ import annotations

import hashlib
import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Any

from autodj.stereo import SAMPLE_RATE

logger = logging.getLogger(__name__)

#: The ledger setting the pick is kept in.
SETTING = "upcoming"
#: The oldest pick a restart plays, in seconds.
MAX_AGE_S = 30 * 60.0


def clock_hash(definition: Any) -> str:
    """A short fingerprint of a clockwheel's saved definition."""
    text = json.dumps(definition, sort_keys=True, default=str)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def _show_holds_air(player: Any) -> bool:
    hook = getattr(player, "show_holds_air", None)
    try:
        return bool(hook()) if callable(hook) else False
    except Exception:
        return True  # Unknown: do not risk a stale song before a show.


def remember(player: Any, rendered: Any, now: float) -> None:
    """Keep the song *rendered* hands over to, or forget the last one.

    Only a song the clockwheel picked in the active wheel is kept, never
    one from the queue (listener requests and shows go through it).
    """
    ledger = getattr(player, "_ledger", None)
    if ledger is None or not getattr(player, "live_mixing", False):
        return
    upcoming = rendered.next_entry
    clock = getattr(player, "_clock", None)
    value = None
    if (
        upcoming is not None
        and rendered.next_pick_mode == "clock"
        and not rendered.next_from_queue
        and clock is not None
        and rendered.next_clock_name == clock.get("name")
        and rendered.next_clock_revision == getattr(player, "_clock_revision", None)
        and rendered.next_clock_cursor is not None
    ):
        frames = max(0, len(rendered.audio) - rendered.overlap_frames)
        value = {
            "path": upcoming.path,
            "picked_at": now,
            "starts_at": now + frames / SAMPLE_RATE,
            "clock": rendered.next_clock_name,
            "clock_hash": player._clock_fingerprint(),
            "cursor": rendered.next_clock_cursor,
            "boundary": rendered.next_clock_boundary,
            "step": rendered.next_clock_step,
            "show": _show_holds_air(player),
        }
    ledger.remember_setting(SETTING, value)


def _closures(player: Any, wall: datetime) -> tuple[frozenset[str], frozenset[int]]:
    """The dayparted categories and the clockwheel steps closed at local time *wall*."""
    from autodj.categories import daypart_refusal

    index = player._category_index()
    categories = index.categories() if index is not None else ()
    closed = frozenset(
        category.id
        for category in categories
        if category.dayparted and daypart_refusal(category, wall) is not None
    )
    steps = frozenset(
        number
        for number, hours in enumerate(getattr(player, "_clock_step_hours", ()) or ())
        if hours is not None and hours.refusal(wall)
    )
    return closed, steps


def refusal(player: Any, saved: Any, now: float) -> tuple[Any, str | None]:
    """The saved song and ``None``, or ``None`` and why it may not start the set."""
    if not isinstance(saved, dict) or not isinstance(saved.get("path"), str):
        return None, "none was saved"
    library = player._library
    clock = player._clock
    if library is None or clock is None or player._cfg.playback.pick_mode != "clock":
        return None, "the clockwheel is off"
    if not getattr(player, "live_mixing", False):
        return None, "the station does not stream"
    picked_at = saved.get("picked_at")
    if not isinstance(picked_at, int | float) or not 0 <= now - picked_at <= MAX_AGE_S:
        return None, "it was picked more than 30 minutes ago"
    entry = library.entry_for_path(saved["path"])
    if (
        entry is None
        or library.kind_for_path(entry.path) != "music"
        or entry.is_silent
        or not Path(entry.path).is_file()
    ):
        return None, "the song is no longer in the library"
    ledger = player._ledger
    played = ledger.last_played().get(entry.path) if ledger is not None else None
    if played is not None and played >= picked_at:
        return None, "it has played since"
    if saved.get("clock") != clock.get("name") or saved.get("clock_hash") != (
        player._clock_fingerprint()
    ):
        return None, "the clockwheel changed"
    boundary = saved.get("boundary")
    if isinstance(boundary, int | float) and now >= boundary:
        return None, "the clockwheel's restart time has passed"
    if saved.get("show") or _show_holds_air(player):
        return None, "a show is on the air"
    starts_at = saved.get("starts_at")
    then = player._station_now(starts_at if isinstance(starts_at, int | float) else picked_at)
    if _closures(player, then) != _closures(player, player._station_now(now)):
        return None, "a category or step has opened or closed since"
    # Picked with the hours and every version of recent songs held back; only
    # the song it followed has played since, and that one is protected.
    context = player._pick_context(dayparts=False, with_versions=False)
    if entry.path in context.recent or entry.path in context.protected:
        return None, "the track rule holds it back"
    if not context.repeat(entry):
        return None, "the repeat rules hold it back"
    if not context.allows(entry):
        return None, "it would break the DMCA limits"
    return entry, None


def restore(player: Any, now: float) -> Any:
    """The saved song when a restart may start with it, else ``None``; asked once."""
    if getattr(player, "_upcoming_checked", False):
        return None
    player._upcoming_checked = True
    ledger = getattr(player, "_ledger", None)
    if (
        ledger is None
        or not getattr(player, "live_mixing", False)  # never a dry run or a preview
        or player._cfg.playback.pick_mode != "clock"
    ):
        return None
    get_setting = getattr(ledger, "get_setting", None)
    saved = get_setting(SETTING, None) if callable(get_setting) else None
    if saved is None:
        return None
    ledger.remember_setting(SETTING, None)  # Used once, whatever happens.
    try:
        entry, why = refusal(player, saved, now)
    except Exception:
        logger.exception("Restart: could not check the song picked before the restart")
        return None
    if entry is None:
        logger.info("Restart: the song picked before the restart does not start the set: %s", why)
        return None
    player._seed_clock_name = player._clock_name()
    player._seed_clock_revision = player._clock_revision
    player._seed_clock_cursor = saved["cursor"]
    player._seed_clock_step = saved.get("step")
    player._restored_first = entry
    logger.info(
        "Restart: starting with %s, picked before the restart; the next pick follows while it plays",
        getattr(entry, "display_name", entry.path),
    )
    return entry
