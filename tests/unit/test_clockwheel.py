from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from random import Random

import pytest

from autodj.clockwheel import (
    ClockSlot,
    least_recent,
    next_boundary,
    parse_pal,
    pick_slot,
    validate_clock,
    validate_resync_minutes,
)


def test_parse_plague_blueprint_order_and_categories():
    source = Path("plague2024.pal").read_text(encoding="utf-8")
    slots = parse_pal(source, [r"C:\STATION\DROPS"])
    assert len(slots) == 27
    assert slots[0] == ClockSlot(r"c:\station\throwback\top")
    assert slots[2] == ClockSlot(
        r"c:\station\drops\drops", selection="random", enforce_rules=False, kind="liner"
    )
    assert slots[-1].folder == r"c:\station\throwback"
    assert slots[-1].recursive and slots[-1].selection == "random"
    assert [slot.folder for slot in slots[:5]] == [
        r"c:\station\throwback\top",
        r"c:\station\throwback\gold",
        r"c:\station\drops\drops",
        r"c:\station\throwback\00a",
        r"c:\station\throwback\recent",
    ]


def test_parse_ignores_clear_and_classifies_subdir_case_insensitively():
    slots = parse_pal(
        "Queue.Clear;\nSubDir['C:/liners/ads'].QueueBottom(smrandom, EnforceRules);", [r"c:\LINERS"]
    )
    assert slots == [ClockSlot(r"C:/liners/ads", recursive=True, selection="random", kind="liner")]


@pytest.mark.parametrize(
    "source",
    [
        "Execute('bad');",
        "Dir['music'].QueueBottom(smLRP, NoRules); extra",
        "Dir['music'].QueueBottom(smLRP, Maybe);",
        "Queue.Clear; Queue.Clear;",
    ],
)
def test_parse_rejects_unsupported_executable_statements(source):
    with pytest.raises(ValueError, match="line 1"):
        parse_pal(source)


def test_clock_slot_json_validation():
    slot = ClockSlot("music", recursive=True)
    assert ClockSlot.from_json(slot.to_json()) == slot
    assert not ClockSlot("music").require_following
    liner = ClockSlot("liners", kind="liner", require_following=True)
    assert ClockSlot.from_json(liner.to_json()) == liner
    with pytest.raises(ValueError, match="selection"):
        ClockSlot("music", selection="weighted")
    with pytest.raises(ValueError, match="only valid for liner"):
        ClockSlot("music", require_following=True)
    for invalid in (1, "true", None):
        with pytest.raises(ValueError, match="must be a boolean"):
            ClockSlot.from_json({"folder": "liners", "kind": "liner", "require_following": invalid})
    with pytest.raises(ValueError, match="unknown"):
        ClockSlot.from_json({"folder": "music", "code": "run"})


@dataclass
class Entry:
    path: str


def test_least_recent_prefers_never_played_then_randomizes_oldest_ties():
    entries = [Entry("a"), Entry("b"), Entry("c")]
    now = datetime(2025, 1, 1)
    assert least_recent(entries, {"a": now, "b": now + timedelta(days=1)}, Random(0)).path == "c"
    tie = [Entry("a"), Entry("b")]
    assert least_recent(tie, {"a": now, "b": now}, Random(2)) in tie
    with pytest.raises(ValueError, match="empty"):
        least_recent([], {})


def test_least_recent_checks_oldest_until_allowed():
    now = datetime(2025, 1, 1)
    entries = [Entry("new"), Entry("old"), Entry("middle")]
    history = {
        "old": now - timedelta(days=2),
        "middle": now - timedelta(days=1),
        "new": now,
    }
    checked = []

    def allowed(entry):
        checked.append(entry.path)
        return entry.path != "old"

    picked = least_recent(entries, history, Random(0), allowed=allowed)
    assert picked.path == "middle"
    assert checked == ["old", "middle"]


def test_least_recent_randomizes_allowed_oldest_ties_and_returns_none_when_blocked():
    now = datetime(2025, 1, 1)
    tie = [Entry("a"), Entry("b")]
    assert {
        least_recent(tie, {"a": now, "b": now}, Random(seed), allowed=lambda _: True).path
        for seed in range(8)
    } == {"a", "b"}

    checked = []

    def blocked(entry):
        checked.append(entry.path)
        return False

    assert least_recent(tie, {}, Random(0), allowed=blocked) is None
    assert set(checked) == {"a", "b"}


@dataclass
class Track:
    path: str
    artist: str = "Artist"


def test_runtime_picker_uses_exact_and_recursive_folder_scope_once():
    entries = [
        Track(r"C:\Music\one.flac"),
        Track(r"c:/music/ONE.flac"),
        Track(r"C:\Music\album\two.flac"),
        Track(r"C:\Music-other\no.flac"),
    ]
    exact = pick_slot([ClockSlot(r"c:\music")], 0, entries, {}, lambda _: True, Random(1))
    assert exact.entry.path == r"C:\Music\one.flac"
    recursive = pick_slot(
        [ClockSlot(r"c:\music", recursive=True)], 0, entries, {}, lambda _: True, Random(1)
    )
    assert recursive.entry.path in {r"C:\Music\one.flac", r"C:\Music\album\two.flac"}


def test_posix_clock_folders_preserve_case_sensitivity():
    tracks = [Track("/Music/artist/song.mp3"), Track("/music/artist/song.mp3")]
    picked = pick_slot([ClockSlot("/Music", recursive=True)], 0, tracks, {}, lambda _: True)
    assert picked.entry.path == "/Music/artist/song.mp3"


def test_runtime_picker_applies_rules_and_returns_liner_with_music_cursor():
    slots = [
        ClockSlot(r"C:\liners", kind="liner", selection="random"),
        ClockSlot(r"C:\music"),
        ClockSlot(r"C:\music\more"),
    ]
    tracks = [Track(r"C:\music\same.flac", "blocked"), Track(r"C:\music\ok.flac", "allowed")]
    liner = Track(r"C:\liners\id.wav")
    pick = pick_slot(
        slots, 0, tracks, {}, lambda entry: entry.artist != "blocked", Random(2), liners=[liner]
    )
    assert pick.entry.artist == "allowed"
    assert pick.liner == liner
    assert not pick.liner_requires_following
    assert pick.next_cursor == 2
    assert pick.step_index == 1
    assert pick.liner_step_index == 0
    assert not pick.skipped


def test_runtime_picker_preserves_liner_flag_through_skips_settings_and_fixed_track():
    liner = Track(r"C:\liners\id.wav")
    entry = Track(r"C:\music\fixed.flac")
    slots = [
        ClockSlot(r"C:\liners", kind="liner", require_following=True),
        ClockSlot(r"C:\missing"),
        ClockSlot(kind="settings", preset="winddown"),
        ClockSlot(kind="track", path=entry.path),
    ]
    pick = pick_slot(slots, 0, [entry], {}, lambda _: True, liners=[liner])
    assert (pick.entry, pick.liner, pick.preset, pick.liner_requires_following) == (
        entry,
        liner,
        "winddown",
        True,
    )
    assert pick.skipped == [r"Step 2: music slot C:\missing: no matching entries"]
    assert pick.step_index == 3
    assert pick.liner_step_index == 0

    no_liner = pick_slot(slots, 0, [entry], {}, lambda _: True, liners=[])
    assert no_liner.liner is None and no_liner.liner_requires_following is False


def test_runtime_picker_preserves_liner_flag_through_category_step():
    entry = Track(r"C:\Music\song.flac")
    liner = Track(r"C:\liners\id.wav")
    slots = [
        ClockSlot(r"C:\liners", kind="liner", require_following=True),
        ClockSlot(kind="category", category="favorites"),
    ]
    pick = pick_slot(
        slots,
        0,
        [entry],
        {},
        lambda _: True,
        liners=[liner],
        categories={"favorites": ("Favorites", frozenset({entry.path}))},
    )
    assert pick.entry is entry
    assert pick.liner is liner
    assert pick.liner_requires_following is True
    assert pick.step_index == 1
    assert pick.liner_step_index == 0


def test_clock_step_snapshot_keeps_the_selected_step_and_display_labels():
    from types import SimpleNamespace

    from autodj.player import Player

    native_step = {"type": "category", "category": "favorites", "selection": "random"}
    player = object.__new__(Player)
    player._clock_steps = [native_step]
    player._library = SimpleNamespace(
        catalog=SimpleNamespace(sources=lambda: []),
        categories=SimpleNamespace(
            categories=lambda: [SimpleNamespace(id="favorites", name="Favorites")]
        ),
    )
    snapshot = player._clock_step_snapshot(0, player._clock_steps)

    native_step["category"] = "renamed"
    assert snapshot == {
        "number": 1,
        "step": {"type": "category", "category": "favorites", "selection": "random"},
        "sources": [],
        "categories": [{"id": "favorites", "name": "Favorites"}],
    }


def test_clock_step_snapshot_keeps_category_condition_labels():
    from types import SimpleNamespace

    from autodj.player import Player

    step = {
        "type": "music",
        "match": "any",
        "conditions": [{"field": "category", "category": "favorites"}],
    }
    player = object.__new__(Player)
    player._library = SimpleNamespace(
        catalog=SimpleNamespace(sources=lambda: []),
        categories=SimpleNamespace(
            categories=lambda: [SimpleNamespace(id="favorites", name="Favorites")]
        ),
    )
    snapshot = player._clock_step_snapshot(0, [step])
    assert snapshot["categories"] == [{"id": "favorites", "name": "Favorites"}]


def test_runtime_picker_checks_rules_in_least_recent_order():
    now = datetime(2025, 1, 2)
    entries = [
        Track(r"C:\music\old.flac", "same artist"),
        Track(r"C:\music\newer.flac", "other artist"),
    ]
    slot = ClockSlot(r"C:\music", selection="least_recent", enforce_rules=True)
    checked = []

    def allowed(entry):
        checked.append(entry)
        return entry.artist != "same artist"

    picked = pick_slot(
        [slot],
        0,
        entries,
        {entries[0].path: now - timedelta(days=10), entries[1].path: now},
        allowed,
    )
    assert picked.entry == entries[1]
    assert checked == entries


def test_runtime_picker_least_recent_no_rules_does_not_check_allowed():
    now = datetime(2025, 1, 2)
    entries = [Track(r"C:\music\old.flac"), Track(r"C:\music\newer.flac")]
    slot = ClockSlot(r"C:\music", selection="least_recent", enforce_rules=False)
    picked = pick_slot(
        [slot],
        0,
        entries,
        {entries[0].path: now - timedelta(days=1), entries[1].path: now},
        lambda _: pytest.fail("NoRules must bypass allowed"),
    )
    assert picked.entry == entries[0]


def test_runtime_picker_reports_missing_slots_and_no_clock_pool_without_writing_history():
    slots = [ClockSlot(r"C:\missing"), ClockSlot(r"C:\music")]
    tracks = [Track(r"C:\music\song.flac")]
    history = {}
    pick = pick_slot(slots, 0, tracks, history, lambda _: True)
    assert pick.entry == tracks[0]
    assert pick.skipped == [r"Step 1: music slot C:\missing: no matching entries"]
    assert pick.next_cursor == 0
    assert history == {}

    blocked = pick_slot([ClockSlot(r"C:\music")], 0, tracks, {}, lambda _: False)
    assert blocked.entry is None
    assert "blocked by rules" in blocked.skipped[0]

    empty = pick_slot([ClockSlot(r"C:\empty")], 0, [], {}, lambda _: True)
    assert empty.entry is None
    assert empty.skipped == [r"Step 1: music slot C:\empty: no matching entries"]


def test_runtime_picker_numbers_repeat_blocked_folder_before_next_success():
    blocked, allowed = Entry(r"C:\music\blocked.flac"), Entry(r"C:\other\allowed.flac")
    pick = pick_slot(
        [ClockSlot(r"C:\music"), ClockSlot(r"C:\other")],
        0,
        [blocked, allowed],
        {},
        lambda entry: entry is allowed,
    )
    assert pick.entry is allowed
    assert pick.skipped == [
        r"Step 1: music slot C:\music: all 1 matching entries blocked by rules"
    ]


def test_clock_validation_rejects_missing_music_and_consecutive_liners_across_wrap():
    with pytest.raises(ValueError, match="at least one music"):
        validate_clock([ClockSlot("C:/liners", kind="liner")])
    with pytest.raises(ValueError, match="consecutive liner"):
        validate_clock(
            [
                ClockSlot("C:/liners/a", kind="liner"),
                ClockSlot("C:/music"),
                ClockSlot("C:/liners/b", kind="liner"),
            ]
        )
    assert (
        len(validate_clock(parse_pal(Path("plague2024.pal").read_text(), [r"c:\station\drops"])))
        == 27
    )


def test_resync_minute_validation_and_next_local_boundary():
    assert validate_resync_minutes() == [0]
    assert validate_resync_minutes([45, 0, 15, 15]) == [0, 15, 45]
    with pytest.raises(ValueError, match="0 to 59"):
        validate_resync_minutes([60])
    with pytest.raises(ValueError, match="list of integers"):
        validate_resync_minutes(3)
    assert validate_resync_minutes([]) == []
    assert next_boundary(datetime(2025, 1, 1, 10, 14, 59), [0, 15]) == datetime(2025, 1, 1, 10, 15)
    local = datetime(2025, 1, 1, 10, 15, tzinfo=timezone(timedelta(hours=5, minutes=30)))
    assert next_boundary(local, [0, 15]) == datetime(2025, 1, 1, 11, 0, tzinfo=local.tzinfo)
    assert next_boundary(local, []) is None


@pytest.mark.parametrize(
    "entry,duration,boundary,expected",
    [
        (3500, 240, 3600, True),
        (3400, 240, 3600, False),
        (3601, 180, 3600, True),
        (3200, 180, 3600, False),
    ],
)
def test_resync_uses_nearest_handoff(entry, duration, boundary, expected):
    from autodj.clockwheel import resync_due

    assert resync_due(entry, duration, boundary) is expected
