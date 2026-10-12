"""Category conditions through the live player's filtering and fallback paths."""

from dataclasses import replace
from types import SimpleNamespace

import pytest

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


@pytest.mark.parametrize("legacy", [False, True])
def test_category_only_steps_keep_the_existing_empty_pool_fallback(legacy):
    player = _clock_player([("A", "One"), ("B", "Two")])
    player._clock_slots = [
        ClockSlot(kind="category", category="gold") if legacy else ClockSlot(conditions=(CATEGORY,))
    ]
    context = player._pick_context()
    assert player._clock_pick(context, 0).entry is None
    assert player._empty_category_fallback(None, context) in player.playable_entries()


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
