"""Category conditions through the live player's filtering and fallback paths."""

import logging
from collections import deque
from dataclasses import replace
from types import SimpleNamespace

from autodj.clockwheel import ClockSlot
from tests.unit.test_repeat_pick_modes import _clock_player

CATEGORY = {"field": "category", "category": "gold"}


def test_player_reuses_membership_cache_and_refreshes_it_after_category_edits():
    player = _clock_player([("A", "One"), ("B", "Two")])
    first, second = player.playable_entries()
    members = frozenset({first.path})
    player._library.categories = SimpleNamespace(members=lambda: {"gold": ("Gold", members)})
    player._clock_slots = [ClockSlot(conditions=(CATEGORY,))]
    context = player._pick_context()
    assert player._clock_pick(context, 0).entry is first
    cache = player._clock_condition_cache
    assert player._clock_pick(context, 0).entry is first
    assert player._clock_condition_cache is cache
    members = frozenset({second.path})
    assert player._clock_pick(context, 0).entry is second
    assert player._clock_condition_cache is not cache


def test_category_hours_change_without_rebuilding_membership_cache():
    player = _clock_player([("A", "One"), ("B", "Two")])
    first, second = player.playable_entries()
    members = frozenset({first.path})
    player._library.categories = SimpleNamespace(members=lambda: {"gold": ("Gold", members)})
    player._clock_slots = [ClockSlot(conditions=(CATEGORY,), enforce_rules=False)]
    context = player._pick_context()
    assert player._clock_pick(context, 0).entry is first
    cache = player._clock_condition_cache
    closed = replace(context, closed=members, closed_categories={"gold": "outside its hours"})
    assert player._clock_pick(closed, 0).entry is None
    assert player._clock_condition_cache is cache
    assert player._empty_category_fallback(None, closed) is second
    assert player._clock_pick(context, 0).entry is first


def test_legacy_category_steps_keep_the_existing_empty_pool_fallback():
    player = _clock_player([("A", "One"), ("B", "Two")])
    player._clock_slots = [ClockSlot(kind="category", category="gold")]
    context = player._pick_context()
    assert player._clock_pick(context, 0).entry is None
    assert player._empty_category_fallback(None, context) in player.playable_entries()


def test_empty_explicit_category_condition_never_falls_back_to_unfiltered_music():
    player = _clock_player([("A", "One"), ("B", "Two")])
    player._clock_slots = [ClockSlot(conditions=(CATEGORY,))]
    context = player._pick_context()
    assert player._clock_pick(context, 0).entry is None
    assert player._empty_category_fallback(None, context) is None


def test_a_category_with_additional_filters_cannot_fall_back_to_unfiltered_music():
    player = _clock_player([("A", "One"), ("B", "Two")])
    members = frozenset(entry.path for entry in player.playable_entries())
    player._library.categories = SimpleNamespace(members=lambda: {"gold": ("Gold", members)})
    player._clock_slots = [
        ClockSlot(
            conditions=(
                CATEGORY,
                {"field": "title", "operator": "is", "value": "Not in library"},
            )
        )
    ]
    context = player._pick_context()
    assert player._clock_pick(context, 0).entry is None
    assert player._empty_category_fallback(None, context) is None


def test_category_read_failure_is_reported_and_does_not_use_stale_membership():
    player = _clock_player([("A", "One"), ("B", "Two")])
    first, _ = player.playable_entries()

    class BrokenCategories:
        def members(self):
            raise OSError("categories temporarily unavailable")

    player._library.categories = BrokenCategories()
    player._clock_slots = [ClockSlot(conditions=(CATEGORY,))]
    context = player._pick_context()
    failed = player._clock_pick(context, 0)
    failed_cache = player._clock_condition_cache
    assert failed.entry is None
    assert failed.skipped == ["Step 1: Categories could not be read"]
    assert player._empty_category_fallback(None, context) is None

    player._library.categories = SimpleNamespace(
        members=lambda: {"gold": ("Gold", frozenset({first.path}))}
    )
    recovered = player._clock_pick(context, 0)
    assert recovered.entry is first
    assert player._clock_condition_cache is not failed_cache


def test_clock_skip_logs_deduplicate_and_reset_after_a_clear_pick(caplog):
    player = _clock_player([("A", "One")])
    player._dry_run = False
    reason = [r"Step 1: music slot C:\missing: no matching entries"]
    with caplog.at_level("WARNING", logger="autodj.player"):
        player._log_clock_skips(reason)
        player._log_clock_skips(reason)
        player._log_clock_skips([])
        player._log_clock_skips(reason)
    assert caplog.text.count("Clock selection skipped:") == 2


def test_clock_skip_logs_are_suppressed_during_forecasts(caplog):
    player = _clock_player([("A", "One")])
    reason = ["Step 1: music slot: no matching entries"]
    caplog.clear()
    player._dry_run = True
    with caplog.at_level(logging.INFO, logger="autodj.player"):
        player._log_clock_skips(reason)
        assert not caplog.records
        assert not hasattr(player, "_clock_skips_logged")
        player._dry_run = False
        player._log_clock_skips(reason)
    assert len(caplog.records) == 1
    assert caplog.records[0].levelno == logging.WARNING


def test_clock_skip_rule_blocks_are_info_but_missing_or_closed_steps_warn(caplog):
    player = _clock_player([("A", "One")])
    player._dry_run = False
    caplog.clear()
    with caplog.at_level(logging.INFO, logger="autodj.player"):
        player._log_clock_skips(["Step 1: category Power: all matching entries blocked by rules"])
        player._log_clock_skips(["Step 1: category Night: outside its hours"])
    assert [record.levelno for record in caplog.records] == [
        logging.INFO,
        logging.WARNING,
    ]


def test_clock_pick_logs_a_filtered_step_before_the_next_folder(caplog):
    player = _clock_player([("A", "One"), ("B", "Two")])
    player._dry_run = False
    first, second = player.playable_entries()
    player._clock_slots = [
        ClockSlot(kind="track", path=first.path),
        ClockSlot(folder="Z:/Music"),
    ]
    context = replace(player._pick_context(), recent=deque([first.path]))
    with caplog.at_level("WARNING", logger="autodj.player"):
        picked = player._clock_pick(context, 0)
    assert picked.entry is second
    assert "Clock selection skipped: Step 1: track step" in caplog.text


def test_a_relative_date_step_is_judged_for_when_the_song_starts():
    """ADR-0009-07: the player judges within the last N days at the pick's start time."""
    from autodj.catalog import normalize_path

    player = _clock_player([("A", "One"), ("B", "Two")])
    first, second = player.playable_entries()
    now = 1_760_000_000 // 3600 * 3600 + 600
    day = 86400
    items = {
        normalize_path(first.path): {"added": now - 40 * day},
        normalize_path(second.path): {"added": now - 6 * day},
    }
    player._library.clock_beets = SimpleNamespace(
        snapshot=lambda fields: (frozenset({"added"}), items, None)
    )
    player._clock_slots = [
        ClockSlot(
            conditions=(
                {"field": "beets", "name": "added", "operator": "within_days", "value": 7},
            ),
            enforce_rules=False,
        )
    ]
    context = replace(player._pick_context(), at=float(now))
    assert player._clock_pick(context, 0).entry is second
    cache = player._clock_condition_cache
    assert player._clock_pick(replace(context, at=now + 1200.0), 0).entry is second
    assert player._clock_condition_cache is cache
    # Two days later the newest song is eight days old, and the step has nothing.
    assert player._clock_pick(replace(context, at=now + 2.0 * day), 0).entry is None
    assert player._clock_condition_cache is not cache
