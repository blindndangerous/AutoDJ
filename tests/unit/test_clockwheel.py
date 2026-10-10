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
    with pytest.raises(ValueError, match="selection"):
        ClockSlot("music", selection="weighted")
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
    assert pick.next_cursor == 2
    assert not pick.skipped


def test_runtime_picker_uses_least_recent_after_repeat_rules_filter_candidates():
    now = datetime(2025, 1, 2)
    entries = [
        Track(r"C:\music\recent.flac", "same artist"),
        Track(r"C:\music\old.flac", "other artist"),
    ]
    slot = ClockSlot(r"C:\music", selection="least_recent", enforce_rules=True)
    picked = pick_slot(
        [slot], 0, entries,
        {entries[0].path: now, entries[1].path: now - timedelta(days=10)},
        lambda entry: entry.artist != "same artist",
    )
    assert picked.entry == entries[1]


def test_runtime_picker_reports_missing_slots_and_no_clock_pool_without_writing_history():
    slots = [ClockSlot(r"C:\missing"), ClockSlot(r"C:\music")]
    tracks = [Track(r"C:\music\song.flac")]
    history = {}
    pick = pick_slot(slots, 0, tracks, history, lambda _: True)
    assert pick.entry == tracks[0]
    assert pick.skipped == [r"music slot C:\missing: no matching entries"]
    assert pick.next_cursor == 0
    assert history == {}

    blocked = pick_slot([ClockSlot(r"C:\music")], 0, tracks, {}, lambda _: False)
    assert blocked.entry is None
    assert "blocked by rules" in blocked.skipped[0]

    empty = pick_slot([ClockSlot(r"C:\empty")], 0, [], {}, lambda _: True)
    assert empty.entry is None
    assert "no matching entries" in empty.skipped[0]


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
