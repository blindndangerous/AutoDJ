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


def test_condition_cache_answers_like_a_fresh_scan():
    from autodj.clockwheel import ConditionPaths

    entries = tuple(
        Track(
            rf"C:\music\{'rock' if i % 2 else 'pop'}\song{i}.flac",
            artist=f"Artist {i % 3}",
            genre="Rock; Live" if i % 4 == 0 else "Pop",
            year=1980 + i,
        )
        for i in range(24)
    )
    beets = {
        normalize_path(entry.path): {"mood": "happy" if i % 5 else "sad"}
        for i, entry in enumerate(entries)
    }
    slots = [
        ClockSlot(
            conditions=(
                {"field": "folder", "folder": r"C:\music\rock", "recursive": False},
                meta("genre", "is", "rock"),
                meta("year", "lt", 2000),
            ),
            match=match,
        )
        for match in ("all", "any")
    ] + [ClockSlot(conditions=(meta("beets", "contains", "APP", name="mood"),))]
    for index in range(len(slots)):
        cache = ConditionPaths(slots, entries, (), beets)
        for skip in range(3):
            pool = tuple(entry for number, entry in enumerate(entries) if number % 3 != skip)
            fresh = pick_slot(slots, index, pool, {}, lambda _: True, Random(skip), beets=beets)
            cached = pick_slot(
                slots,
                index,
                pool,
                {},
                lambda _: True,
                Random(skip),
                beets=beets,
                condition_cache=cache,
            )
            assert (cached.entry, cached.next_cursor, cached.skipped) == (
                fresh.entry,
                fresh.next_cursor,
                fresh.skipped,
            )


def test_a_second_pick_does_not_evaluate_conditions_again(monkeypatch):
    from autodj import clock_conditions
    from autodj.clockwheel import ConditionPaths

    entries = (Track(r"C:\music\old.flac", year=1990), Track(r"C:\music\new.flac", year=2010))
    slots = [ClockSlot(conditions=(meta("year", "lt", 2000), meta("artist", "is", "Artist")))]
    built = []
    real = clock_conditions.condition_value_test
    monkeypatch.setattr(
        clock_conditions,
        "condition_value_test",
        lambda condition: built.append(condition) or real(condition),
    )
    cache = ConditionPaths(slots, entries, (), None)
    for _ in range(2):
        picked = pick_slot(
            slots, 0, tuple(iter(entries)), {}, lambda _: True, condition_cache=cache
        )
        assert picked.entry is entries[0]
    assert len(built) == 2  # Each condition once, for the first pick only.
    assert cache.current(slots, entries, (), None)
    assert not cache.current(slots, entries[:1] + entries[1:], (), None)


def test_match_all_stops_once_no_entry_can_pass(monkeypatch):
    from autodj import clock_conditions

    built = []
    real = clock_conditions.condition_value_test
    monkeypatch.setattr(
        clock_conditions,
        "condition_value_test",
        lambda condition: built.append(condition) or real(condition),
    )
    slot = ClockSlot(conditions=(meta("artist", "is", "Nobody"), meta("year", "lt", 2000)))
    picked = pick_slot([slot], 0, [Track(r"C:\music\song.flac")], {}, lambda _: True)
    assert picked.entry is None
    assert built == [meta("artist", "is", "Nobody")]


def test_relative_date_steps_are_worked_out_again_each_hour():
    """ADR-0009-07: a within the last N days step follows the pick time, cached by the hour."""
    from autodj.clockwheel import ConditionPaths

    now = 1_760_000_000 // 3600 * 3600 + 600
    day = 86400
    old, new = Track(r"C:\music\old.flac"), Track(r"C:\music\new.flac")
    entries = (old, new)
    beets = {
        normalize_path(old.path): {"added": now - 40 * day},
        normalize_path(new.path): {"added": now - 29 * day},
    }
    recent = ClockSlot(conditions=(meta("beets", "within_days", 30, name="added"),))
    plain = ClockSlot(conditions=(meta("year", "lt", 2010),))
    slots = [recent]
    cache = ConditionPaths(slots, entries, (), beets, None, now)
    assert cache.paths(0) == {new.path}
    # The same hour keeps the answer; the next hour works it out again.
    assert cache.current(slots, entries, (), beets, None, now + 2000)
    assert not cache.current(slots, entries, (), beets, None, now + 3600)
    later = ConditionPaths(slots, entries, (), beets, None, now + 2 * day)
    assert later.paths(0) == set()
    picked = pick_slot(slots, 0, entries, {}, lambda _: True, beets=beets, condition_cache=later)
    assert picked.entry is None
    # A step without one keeps its cache whatever the time.
    plain_slots = [plain]
    steady = ConditionPaths(plain_slots, entries, (), None, None, now)
    assert steady.current(plain_slots, entries, (), None, None, now + 30 * day)


def _every_entry(slot, entries, beets=None, categories=None, now=None):
    """What a conditions slot matches, testing each entry one by one (the reference)."""
    from autodj.clock_conditions import condition_matcher
    from autodj.clockwheel import _slot_entries

    found = None
    for condition in slot.conditions:
        if condition["field"] == "folder":
            matched = _slot_entries(
                ClockSlot(folder=condition["folder"], recursive=condition["recursive"]), entries
            )
            if condition.get("operator") == "is_not":
                inside = {entry.path for entry in matched}
                matched = [entry for entry in entries if entry.path not in inside]
        elif condition["field"] == "category":
            members = (categories or {}).get(condition["category"])
            matched = (
                []
                if members is None
                else [
                    entry
                    for entry in entries
                    if (entry.path in members[1]) == (condition.get("operator") != "is_not")
                ]
            )
        else:
            test = condition_matcher(condition)
            matched = [
                entry
                for entry in entries
                if test(
                    entry,
                    (beets or {}).get(normalize_path(entry.path))
                    if condition["field"] == "beets"
                    else None,
                    now,
                )
            ]
        paths = {entry.path for entry in matched}
        if found is None:
            found = paths
        elif slot.match == "all":
            found &= paths
        else:
            found |= paths
    return found or set()


def test_whole_pool_matching_gives_the_songs_testing_each_one_gives():
    from autodj.clockwheel import ConditionPaths, condition_paths

    rng = Random(3)
    now = 1_760_000_000
    entries = tuple(
        Track(
            "C:\\music\\" + rng.choice(["rock", "pop", "jazz"]) + f"\\song{i}.flac",
            artist=f"Artist {i % 7}",
            genre=rng.choice(["Rock", "Pop; Live", "Jazz", ""]),
            bpm=rng.choice([0, 96, 118, 120, 124, 150]),
            year=rng.randint(1970, 2020),
        )
        for i in range(300)
    )
    values = [None, "", "3", "3.0", 3, 3.0, 4.5, "high", b"4", True, 0, ["3"]]
    beets = {
        normalize_path(entry.path): {
            "radio_energy": rng.choice(values),
            "radio_tier": rng.choice(["a", "A", "b", " ", None]),
            "added": now - rng.randint(0, 60) * 86400,
            "year": rng.choice([1999, 0, None]),
            "original_year": rng.choice([1980, 0, None, "1975"]),
        }
        for entry in entries
        if rng.random() < 0.8
    }
    categories = {
        "a": ("A", frozenset(entry.path for entry in entries[::3])),
        "b": ("B", frozenset(entry.path for entry in entries[::5])),
        "empty": ("Empty", frozenset()),
    }
    energy = meta("beets", "gte", 3.0, name="radio_energy")
    conditions = [
        {"field": "category", "category": "a"},
        {"field": "category", "category": "b", "operator": "is_not"},
        {"field": "category", "category": "gone"},
        {"field": "category", "category": "empty"},
        energy,
        meta("beets", "eq", 3.0, name="radio_energy"),
        meta("beets", "is", "high", name="radio_energy"),
        meta("beets", "contains", "a", name="radio_tier"),
        meta("beets", "within_days", 30, name="added"),
        meta("beets", "between", 1970, name="original_year", upper=1990),
        meta("bpm", "between", 118, upper=124),
        meta("year", "lt", 2000),
        meta("genre", "is", "live"),
        {"field": "folder", "folder": "C:\\music\\rock", "recursive": False},
        {"field": "folder", "folder": "C:\\music\\pop", "recursive": False, "operator": "is_not"},
    ]
    slots = [
        ClockSlot(conditions=tuple(rng.sample(conditions, rng.randint(1, 4))), match=match)
        for match in ("all", "any")
        for _ in range(40)
    ]
    slots += [ClockSlot(conditions=(energy,)), ClockSlot(conditions=(energy, conditions[0]))]
    for at in (now, None):
        cache = ConditionPaths(slots, entries, (), beets, categories, at)
        for index, slot in enumerate(slots):
            expected = _every_entry(slot, entries, beets, categories, at)
            assert condition_paths(slot, entries, beets, categories, at) == expected
            assert cache.paths(index) == expected
    assert condition_paths(slots[-2], entries, None, categories) == set()


def test_steps_that_share_a_condition_match_it_once(monkeypatch):
    from autodj import clock_conditions
    from autodj.clockwheel import ConditionPaths

    built = []
    real = clock_conditions.condition_value_test
    monkeypatch.setattr(
        clock_conditions,
        "condition_value_test",
        lambda condition: built.append(condition) or real(condition),
    )
    entries = tuple(Track(f"C:\\music\\song{i}.flac", bpm=110 + i) for i in range(20))
    fast = meta("bpm", "gte", 120)
    slow = meta("bpm", "lt", 125)
    slots = [
        ClockSlot(conditions=(fast,)),
        ClockSlot(conditions=(fast, slow)),
        ClockSlot(conditions=(slow, fast), match="any"),
    ]
    cache = ConditionPaths(slots, entries, (), None)
    assert [len(cache.paths(index)) for index in range(3)] == [10, 5, 20]
    assert built == [fast, slow]

    # A dry run's copy starts from the conditions matched so far; what it
    # matches later stays its own.
    twin = cache.copy_for(list(slots))
    assert twin is not None
    old = meta("year", "lt", 2001)
    assert twin._terms["music"].matched(old) == {entry.path for entry in entries}
    assert built == [fast, slow, old]
    assert len(twin._terms["music"]._found) == 3
    assert len(cache._terms["music"]._found) == 2


def test_beets_values_follow_new_songs_and_a_new_read_of_beets(monkeypatch):
    """The Beets values of a large pool are looked up once per library and Beets read."""
    from autodj import catalog, clockwheel
    from autodj.clockwheel import ConditionPaths

    looked = []
    real = catalog.normalize_path
    monkeypatch.setattr(catalog, "normalize_path", lambda path: looked.append(path) or real(path))
    monkeypatch.setattr(clockwheel, "_BEETS_ROWS", ())
    size = clockwheel._BEETS_ROWS_MIN
    entries = tuple(Track(f"C:\\music\\song{i}.flac") for i in range(size))
    high = meta("beets", "gte", 3.0, name="radio_energy")
    low = meta("beets", "lt", 3.0, name="radio_energy")
    slots = [ClockSlot(conditions=(high,)), ClockSlot(conditions=(low,))]
    beets = {real(entry.path): {"radio_energy": 4.0} for entry in entries[: size // 2]}
    assert len(ConditionPaths(slots, entries, (), beets).paths(0)) == size // 2
    assert len(looked) == size
    # Another step, another pick's cache, a dry run's copy: no lookups again.
    again = ConditionPaths(slots, entries, (), beets)
    assert len(again.paths(1)) == 0 and len(again.paths(0)) == size // 2
    assert len(looked) == size

    # Beets read again: the new values count.
    reread = {real(entry.path): {"radio_energy": 1.0} for entry in entries}
    assert len(ConditionPaths(slots, entries, (), reread).paths(1)) == size
    assert len(looked) == 2 * size
    # A song added: it is looked up with the rest.
    added = (*entries, Track("C:\\music\\new.flac"))
    reread[real(added[-1].path)] = {"radio_energy": 5.0}
    assert ConditionPaths(slots, added, (), reread).paths(0) == {added[-1].path}
    assert len(looked) == 3 * size + 1
    # A small pool, such as the one song a rule checks, is not kept in its place.
    ConditionPaths(slots, (Track("C:\\music\\single.flac"),), (), reread).paths(0)
    assert ConditionPaths(slots, added, (), reread).paths(0) == {added[-1].path}
    assert len(looked) == 3 * size + 2
