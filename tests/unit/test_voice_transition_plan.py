"""Conflict resolution for editable voice-track transition plans (#507)."""

from types import SimpleNamespace

import pytest

from autodj.breaks import BreakError, TransitionItem
from autodj.player import Player


def _plan(items):
    return SimpleNamespace(
        transition_items=tuple(items),
        transition_order=(),
        slot_id="slot",
    )


@pytest.mark.parametrize(
    ("source", "kind"),
    [
        ("queued", "liner"),
        ("clockwheel", "liner"),
        ("automatic", "liner"),
        ("matched_intro", "matched_intro"),
        ("intro_rule", "matched_intro"),
    ],
)
@pytest.mark.parametrize("action", ["replace", "before", "after"])
def test_each_spoken_item_supports_replace_before_and_after(source, kind, action):
    item = TransitionItem("item", kind, source, "Station ID")
    order, replaced = Player._voice_transition_order(
        SimpleNamespace(break_desk=None),
        _plan([item]),
        "new",
        [{"id": "item", "action": action}],
    )
    expected = {
        "replace": (("break:new",), ("item",)),
        "before": (("break:new", "item"), ()),
        "after": (("item", "break:new"), ()),
    }[action]
    assert (order, replaced) == expected


@pytest.mark.parametrize(
    ("actions", "expected"),
    [
        (["after", "after", "replace"], ("a", "b", "break:new")),
        (["before", "before", "replace"], ("break:new", "a", "b")),
        (["replace", "after", "replace"], ("b", "break:new")),
    ],
)
def test_multiple_items_resolve_to_one_order(actions, expected):
    items = [TransitionItem(key, "liner", "queued", key) for key in "abc"]
    order, replaced = Player._voice_transition_order(
        SimpleNamespace(break_desk=None),
        _plan(items),
        "new",
        [{"id": key, "action": action} for key, action in zip("abc", actions, strict=True)],
    )
    assert order == expected
    assert replaced == tuple(
        key for key, action in zip("abc", actions, strict=True) if action == "replace"
    )


def test_contradictory_constraints_are_rejected_before_booking():
    items = [TransitionItem(key, "liner", "automatic", key) for key in "ab"]
    with pytest.raises(BreakError, match="choices conflict") as exc:
        Player._voice_transition_order(
            SimpleNamespace(break_desk=None),
            _plan(items),
            "new",
            [{"id": "a", "action": "before"}, {"id": "b", "action": "after"}],
        )
    assert exc.value.status == 409 and exc.value.reason == "transition_conflict"


def test_rebooking_keeps_committed_breaks_in_their_existing_order():
    items = [
        TransitionItem("a", "liner", "queued", "Queue liner"),
        TransitionItem("b", "liner", "automatic", "Automatic liner"),
    ]
    plan = _plan(items)
    plan.transition_order = ("a", "break:old")
    plan.transition_items += (
        TransitionItem(
            "break:old",
            "booked_voice",
            "voice_track",
            "Earlier voice track",
            committed=True,
            editable=False,
        ),
    )
    desk = SimpleNamespace(
        listing=lambda: [
            SimpleNamespace(
                break_id="old",
                slot_id="slot",
                state="queued",
            )
        ]
    )
    order, replaced = Player._voice_transition_order(
        SimpleNamespace(break_desk=desk),
        plan,
        "new",
        [{"id": "a", "action": "after"}, {"id": "b", "action": "replace"}],
    )
    assert order == ("a", "break:new", "break:old")
    assert replaced == ("b",)


def test_discarding_an_unplayed_render_releases_its_frozen_liner_reservation():
    released = []
    player = SimpleNamespace(
        break_desk=None,
        liner_scheduler=SimpleNamespace(release_plan=released.append),
        _release_intros=lambda _track: None,
    )
    track = SimpleNamespace(
        break_events=(),
        liner_plan_key="frozen-liner-plan",
    )

    Player._discard_render(player, track)

    assert released == ["frozen-liner-plan"]
