from dataclasses import dataclass
from datetime import datetime, timedelta
from random import Random

from autodj.catalog import normalize_path
from autodj.clockwheel import ClockSlot, pick_slot


@dataclass
class Track:
    path: str
    artist: str = "Artist"
    title: str = "Song"
    album: str = "Album"
    genre: str = "Rock"
    bpm: float = 120
    year: int = 2000
    length: float = 180


def meta(field, operator, value, **extra):
    return {"field": field, "operator": operator, "value": value, **extra}


def test_music_and_liner_accept_tuple_conditions():
    songs = [Track(r"C:\music\song.flac", artist="Selected")]
    liners = [Track(r"C:\liners\liner.wav", artist="Voice")]
    slots = [
        ClockSlot(kind="liner", conditions=(meta("artist", "is", "Voice"),)),
        ClockSlot(conditions=(meta("artist", "is", "Selected"),)),
    ]
    picked = pick_slot(slots, 0, songs, {}, lambda _: True, liners=liners)
    assert picked.entry == songs[0]
    assert picked.liner == liners[0]


def test_match_all_and_any_with_folder_and_metadata_conditions():
    inside_match = Track(r"C:\music\inside.flac", artist="Match")
    inside_miss = Track(r"C:\music\miss.flac", artist="Other")
    outside_match = Track(r"C:\other\outside.flac", artist="Match")
    entries = [inside_match, inside_miss, outside_match]
    now = datetime(2025, 1, 1)
    conditions = (
        {"field": "folder", "folder": r"C:\music", "recursive": False},
        meta("artist", "is", "Match"),
    )
    all_pick = pick_slot(
        [ClockSlot(conditions=conditions, match="all")],
        0,
        entries,
        {
            inside_match.path: now,
            inside_miss.path: now + timedelta(days=1),
            outside_match.path: now - timedelta(days=1),
        },
        lambda _: True,
    )
    any_pick = pick_slot(
        [ClockSlot(conditions=conditions, match="any")],
        0,
        entries,
        {
            inside_match.path: now,
            inside_miss.path: now + timedelta(days=1),
            outside_match.path: now - timedelta(days=1),
        },
        lambda _: True,
    )
    assert all_pick.entry == inside_match
    assert any_pick.entry == outside_match


def test_two_folder_conditions_union_for_any_and_intersect_for_all():
    top = Track(r"C:\music\root.flac")
    parent = Track(r"C:\music\artist\song.flac")
    child = Track(r"C:\music\artist\album\song.flac")
    entries = [top, parent, child]
    conditions = (
        {"field": "folder", "folder": r"C:\music", "recursive": True},
        {"field": "folder", "folder": r"C:\music\artist", "recursive": True},
    )
    now = datetime(2025, 1, 1)
    history = {
        top.path: now - timedelta(days=10),
        parent.path: now - timedelta(days=2),
        child.path: now,
    }
    intersection = pick_slot(
        [ClockSlot(conditions=conditions, match="all")],
        0,
        entries,
        history,
        lambda _: True,
        Random(0),
    )
    union = pick_slot(
        [ClockSlot(conditions=conditions, match="any")],
        0,
        entries,
        history,
        lambda _: True,
        Random(0),
    )
    assert intersection.entry == parent
    assert union.entry == top


def test_metadata_only_and_missing_metadata_exclusion():
    match = Track(r"C:\music\match.flac", artist="Wanted")
    missing = Track(r"C:\music\missing.flac", artist="")
    picked = pick_slot(
        [ClockSlot(conditions=(meta("artist", "matches", "*"),))],
        0,
        [match, missing],
        {},
        lambda _: True,
    )
    assert picked.entry == match


def test_least_recent_chooses_only_inside_matched_pool():
    selected = Track(r"C:\music\selected.flac", artist="Wanted")
    outside = Track(r"C:\music\outside.flac", artist="Other")
    now = datetime(2025, 1, 1)
    picked = pick_slot(
        [ClockSlot(conditions=(meta("artist", "is", "Wanted"),))],
        0,
        [selected, outside],
        {selected.path: now, outside.path: now - timedelta(days=10)},
        lambda _: True,
        Random(0),
    )
    assert picked.entry == selected


def test_rules_cannot_widen_the_condition_pool():
    matched_but_blocked = Track(r"C:\music\blocked.flac", artist="Wanted")
    unmatched_but_allowed = Track(r"C:\music\other.flac", artist="Other")
    picked = pick_slot(
        [ClockSlot(conditions=(meta("artist", "is", "Wanted"),))],
        0,
        [matched_but_blocked, unmatched_but_allowed],
        {},
        lambda entry: entry == unmatched_but_allowed,
    )
    assert picked.entry is None
    assert "all 1 matching entries blocked" in picked.skipped[0]


def test_beets_values_use_normalized_entry_paths():
    entry = Track(r"C:\Music\Artist\Song.flac", artist="Library Artist")
    slot = ClockSlot(conditions=(meta("beets", "is", "Beets Artist", name="artist"),))
    picked = pick_slot(
        [slot],
        0,
        [entry],
        {},
        lambda _: True,
        beets={normalize_path(entry.path): {"artist": "Beets Artist"}},
    )
    assert picked.entry == entry


def test_beets_condition_error_skips_entire_any_step():
    entry = Track(r"C:\music\song.flac", artist="Would match")
    slot = ClockSlot(
        conditions=(
            meta("artist", "is", "Would match"),
            meta("beets", "is", "anything", name="mood"),
        ),
        match="any",
    )
    picked = pick_slot(
        [slot], 0, [entry], {}, lambda _: True, condition_errors={0: "Beets unavailable"}
    )
    assert picked.entry is None
    assert picked.skipped == ["Step 1: Beets unavailable"]


def test_categories_coexist_and_preserve_preceding_liner_requirement():
    liner = Track(r"C:\liners\voice.wav")
    song = Track(r"C:\music\category.flac")
    slots = [
        ClockSlot(
            kind="liner", require_following=True, conditions=(meta("artist", "is", "Artist"),)
        ),
        ClockSlot(kind="category", category="cat"),
    ]
    picked = pick_slot(
        slots,
        0,
        [song],
        {},
        lambda _: True,
        liners=[liner],
        categories={"cat": ("Favorites", {song.path})},
    )
    assert (picked.entry, picked.liner, picked.liner_requires_following) == (song, liner, True)


def test_existing_folder_slots_still_pick_entries():
    entry = Track(r"C:\music\old-folder-song.flac")
    picked = pick_slot([ClockSlot(r"C:\music")], 0, [entry], {}, lambda _: True)
    assert picked.entry == entry
