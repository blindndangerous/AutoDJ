"""Native clockwheel validation, persistence, PAL conversion and loading."""

import json
import threading
from pathlib import Path
from types import MethodType, SimpleNamespace

import pytest

from autodj.clockwheel import ClockSlot, parse_pal, pick_slot
from autodj.clockwheel_store import (
    OLD_CLOCK_MESSAGE,
    ClockwheelStore,
    from_pal,
    locate_folder,
    register_step_type,
    source_folder,
    validate_clockwheel,
    validate_name,
)
from autodj.player import Player
from autodj.profiles import ProfileSnapshot, ProfileStore

MUSIC = {"type": "music", "source": "src1"}


def clock(steps, name="Morning"):
    return {"version": 1, "name": name, "steps": steps, "resync_minutes": [0]}


def test_store_round_trip_and_strict_loading(tmp_path):
    store = ClockwheelStore(tmp_path / "clockwheels")
    assert store.list_names() == []
    saved = store.save(clock([{**MUSIC, "folder": "Rock"}, {"type": "settings", "preset": "x"}]))
    assert saved["steps"][0] == {
        "type": "music",
        "source": "src1",
        "folder": "Rock",
        "recursive": False,
        "selection": "least_recent",
        "enforce_rules": True,
    }
    assert store.list_names() == ["Morning"]
    assert store.load("Morning") == saved
    raw = (store.root / "Morning.json").read_bytes()
    assert b"\r\n" not in raw
    with pytest.raises(ValueError, match="already exists"):
        store.save(clock([MUSIC], name="MORNING"), overwrite=False)
    path = store.root / "Morning.json"
    path.write_text(json.dumps({**saved, "version": 2}), encoding="utf-8")
    with pytest.raises(ValueError, match="Unsupported clockwheel version 2"):
        store.load("Morning")
    path.write_text(json.dumps({**saved, "extra": 1}), encoding="utf-8")
    with pytest.raises(ValueError, match="unknown"):
        store.load("Morning")
    path.write_text(json.dumps({**saved, "name": "Night"}), encoding="utf-8")
    with pytest.raises(ValueError, match="must match the file name"):
        store.load("Morning")
    path.write_text("{", encoding="utf-8")
    with pytest.raises(ValueError, match="could not be loaded"):
        store.load("Morning")
    with pytest.raises(ValueError, match="no clockwheel named"):
        store.load("Night")
    assert store.delete("Morning") is True
    assert store.delete("Morning") is False


@pytest.mark.parametrize("name", ["", " Morning", "../x", "a.b", "CON", "lpt1", "x" * 65, 3])
def test_unsafe_names_are_refused(name):
    with pytest.raises(ValueError, match="clockwheel name needs"):
        validate_name(name)


@pytest.mark.parametrize(
    ("steps", "message"),
    [
        ([{"type": "cart"}, MUSIC], "Step 1: Unknown clockwheel step type 'cart'"),
        ([{"type": "time_check"}, MUSIC], "needs the AutoDJ version that added it"),
        ([{"folder": "x"}], "must be an object with a type"),
        ([{**MUSIC, "secret": "x"}], "unknown fields: secret"),
        ([{"type": "music"}], "needs: source"),
        ([{**MUSIC, "source": "../x"}], "ID of a registered library folder"),
        ([{**MUSIC, "folder": "../Rock"}], "no '..'"),
        ([{**MUSIC, "folder": "/Rock"}], "no '..'"),
        ([{**MUSIC, "folder": "Rock\\Pop"}], "no '..'"),
        ([{**MUSIC, "recursive": "yes"}], "true or false"),
        ([{**MUSIC, "require_following": "yes"}], "true or false"),
        ([{**MUSIC, "require_following": True}], "only valid for liner"),
        ([{**MUSIC, "selection": "best"}], "least_recent or random"),
        ([{"type": "track", "source": "src1", "path": "C:/x.flac"}], "no '..'"),
        ([{"type": "track", "source": "src1", "path": ""}], "must not be empty"),
        ([{"type": "settings", "preset": ""}, MUSIC], "name of a preset"),
        ([{"type": "settings", "preset": "x"}], "at least one music, category or track step"),
        (
            [{"type": "liner", "source": "l"}, {"type": "liner", "source": "l"}, MUSIC],
            "both liners",
        ),
        ([MUSIC] * 257, "at most 256"),
    ],
)
def test_invalid_steps_are_refused_with_the_reason(steps, message):
    with pytest.raises(ValueError, match=message):
        validate_clockwheel(clock(steps))


def test_resync_minutes_and_whole_shape_are_checked():
    with pytest.raises(ValueError, match="integers from 0 to 59"):
        validate_clockwheel({**clock([MUSIC]), "resync_minutes": [60]})
    with pytest.raises(ValueError, match="missing"):
        validate_clockwheel({"version": 1, "name": "x", "steps": [MUSIC]})
    with pytest.raises(ValueError, match="JSON object"):
        validate_clockwheel([])


def test_liner_require_following_is_strict_and_omitted_when_false(tmp_path):
    base = {"type": "liner", "source": "voice"}
    normalized = validate_clockwheel(clock([base, MUSIC]))
    assert "require_following" not in normalized["steps"][0]
    flagged = validate_clockwheel(clock([{**base, "require_following": True}, MUSIC]))
    assert flagged["steps"][0]["require_following"] is True
    store = ClockwheelStore(tmp_path / "clockwheels")
    store.save(clock([base, MUSIC]))
    assert b"require_following" not in (store.root / "Morning.json").read_bytes()
    for invalid in (1, "true", None):
        with pytest.raises(ValueError, match="true or false"):
            validate_clockwheel(clock([{**base, "require_following": invalid}, MUSIC]))


def test_features_register_their_own_step_types():
    register_step_type("test_cart", lambda value: {"type": "test_cart", "name": value["name"]})
    steps = validate_clockwheel(clock([{"type": "test_cart", "name": "Top"}, MUSIC]))["steps"]
    assert steps[0] == {"type": "test_cart", "name": "Top"}
    with pytest.raises(ValueError, match="already registered"):
        register_step_type("test_cart", lambda value: dict(value))
    with pytest.raises(ValueError, match="lowercase"):
        register_step_type("Bad-Type", lambda value: dict(value))
    register_step_type("test_broken", lambda value: {"type": "other"})
    with pytest.raises(ValueError, match="returned an invalid step"):
        validate_clockwheel(clock([{"type": "test_broken"}, MUSIC]))


WINDOWS_SOURCES = [
    {"id": "music", "kind": "music", "path": r"c:\station\throwback", "recursive": True},
    {"id": "top", "kind": "music", "path": r"c:\station\throwback\top", "recursive": False},
    {"id": "drops", "kind": "liner", "path": r"c:\station\drops", "recursive": True},
]


def test_locate_folder_picks_the_innermost_source_and_keeps_letter_case():
    sources = WINDOWS_SOURCES[:2]
    assert locate_folder(r"C:\Station\Throwback\Gold B", sources) == (sources[0], "Gold B")
    assert locate_folder(r"c:\station\throwback\top", sources) == (sources[1], "")
    assert locate_folder("c:/station/throwback/90a/", sources) == (sources[0], "90a")
    assert locate_folder(r"c:\station\throwback", sources) == (sources[0], "")
    with pytest.raises(ValueError, match="registered without subfolders"):
        locate_folder(r"c:\station\throwback\top\deep", sources)
    with pytest.raises(ValueError, match="not inside a registered library folder"):
        locate_folder(r"c:\station\throwbacks", sources)
    posix = [{"id": "m", "kind": "music", "path": "/srv/Music", "recursive": True}]
    assert locate_folder("/srv/Music/Rock/Live", posix) == (posix[0], "Rock/Live")
    with pytest.raises(ValueError, match="not inside"):
        locate_folder("/srv/music/Rock", posix)


def test_the_station_pal_converts_into_a_native_clockwheel():
    text = Path("plague2024.pal").read_text(encoding="utf-8")
    slots = parse_pal(text, [r"c:\station\drops"])
    result = from_pal("Plague", slots, WINDOWS_SOURCES, [0])
    assert len(result["steps"]) == len(slots) == 27
    assert result["steps"][0]["source"] == "top"
    assert result["steps"][0]["folder"] == ""
    assert result["steps"][1]["folder"] == "gold"
    assert result["steps"][2] == {
        "type": "liner",
        "source": "drops",
        "folder": "drops",
        "recursive": False,
        "selection": "random",
        "enforce_rules": False,
    }
    assert result["steps"][-1]["recursive"] is True
    assert result["steps"][-1]["folder"] == ""
    with pytest.raises(ValueError, match="PAL line for step 3"):
        from_pal("Plague", slots, WINDOWS_SOURCES[:2], [0])


def test_source_folder_joins_in_the_source_style():
    assert source_folder(r"c:\station", "a/b") == r"c:\station\a\b"
    assert source_folder("/srv/Music", "Rock/x.flac") == "/srv/Music/Rock/x.flac"
    assert source_folder("/srv/Music", "") == "/srv/Music"


def test_profile_keeps_the_clockwheel_name(tmp_path):
    store = ProfileStore(tmp_path / "profiles")
    store.save(ProfileSnapshot(name="Morning", pick_mode="clock", clockwheel="Morning"))
    assert store.load("Morning").clockwheel == "Morning"


def test_settings_steps_pass_their_preset_to_the_next_pick():
    entry = SimpleNamespace(path="/music/one.flac")
    slots = [
        ClockSlot(kind="settings", preset="winddown"),
        ClockSlot(kind="track", path="/music/one.flac"),
    ]
    selected = pick_slot(slots, 0, [entry], {}, lambda _: True)
    assert (selected.entry, selected.preset, selected.next_cursor) == (entry, "winddown", 0)
    blocked = pick_slot(slots, 0, [entry], {}, lambda _: False)
    assert blocked.entry is None
    assert blocked.skipped == ["track step /music/one.flac: recording missing or blocked by rules"]


def _player(tmp_path, setting, sources):
    catalog = SimpleNamespace(get_setting=lambda _key, _default: setting, sources=lambda: sources)
    player = SimpleNamespace(
        _cfg=SimpleNamespace(index=SimpleNamespace(active_dir=tmp_path), presets={}),
        _library=SimpleNamespace(catalog=catalog),
    )
    player._clock_lock = threading.RLock()
    player._clock_revision = 0
    player._clock = None
    player._pending_clock_update = None
    player._load_clockwheel = MethodType(Player._load_clockwheel, player)
    player.activate_clock = MethodType(Player.activate_clock, player)
    player._now = MethodType(Player._now, player)
    player._clock_off = MethodType(Player._clock_off, player)
    return player


def test_player_loads_the_active_clockwheel_from_its_sources(tmp_path):
    music = tmp_path / "music"
    voice = tmp_path / "voice"
    sources = [
        {"id": "src1", "kind": "music", "path": str(music), "recursive": True},
        {"id": "voice", "kind": "liner", "path": str(voice), "recursive": True},
    ]
    ClockwheelStore(tmp_path / "clockwheels").save(
        clock(
            [
                {**MUSIC, "folder": "Rock/Live", "recursive": True},
                {"type": "settings", "preset": "winddown"},
                {"type": "track", "source": "src1", "path": "one.flac"},
                {"type": "liner", "source": "voice", "require_following": True},
            ]
        )
    )
    player = _player(tmp_path, {"name": "Morning", "cursor": 1}, sources)
    Player.configure_clock(player)
    assert player._clock_status == "Ready"
    assert player._clock == {"name": "Morning", "cursor": 1, "resync_minutes": [0]}
    assert player._clock_slots[0].folder == str(music / "Rock" / "Live")
    assert player._clock_slots[0].recursive is True
    assert player._clock_slots[1].preset == "winddown"
    assert player._clock_slots[2].path == str(music / "one.flac")
    assert player._clock_slots[3].require_following is True
    assert player._clock_next_boundary is not None


def test_reloading_the_clockwheel_keeps_a_place_not_yet_written(tmp_path):
    # Saving an edit to the active clockwheel reloads it; the player's place
    # still waiting in the ledger is newer than the saved one.
    ClockwheelStore(tmp_path / "clockwheels").save(clock([MUSIC]))
    player = _player(
        tmp_path,
        {"name": "Morning", "cursor": 1},
        [{"id": "src1", "kind": "music", "path": str(tmp_path), "recursive": True}],
    )
    player._ledger = SimpleNamespace(
        get_setting=lambda name, default: {"name": "Morning", "cursor": 3, "resync_minutes": [0]}
    )
    Player.configure_clock(player)
    assert player._clock == {"name": "Morning", "cursor": 3, "resync_minutes": [0]}


def test_a_clockwheel_that_cannot_load_leaves_the_clock_off_and_says_why(tmp_path, caplog):
    ClockwheelStore(tmp_path / "clockwheels").save(clock([MUSIC]))
    player = _player(tmp_path, {"name": "Morning", "cursor": 0}, [])
    Player.configure_clock(player)
    assert player._clock is None
    assert "is not registered" in player._clock_status
    assert "is not registered" in caplog.text

    player = _player(tmp_path, {"name": "Gone", "cursor": 0}, [])
    Player.configure_clock(player)
    assert player._clock is None
    assert "no clockwheel named 'Gone'" in player._clock_status

    player = _player(tmp_path, {"name": None, "cursor": 0}, [])
    Player.configure_clock(player)
    assert (player._clock, player._clock_status) == (None, "No clockwheel is active")

    player = _player(tmp_path, {"name": "Clock", "slots": [{"folder": "x"}]}, [])
    Player.configure_clock(player)
    assert (player._clock, player._clock_status) == (None, OLD_CLOCK_MESSAGE)

    player._library = None
    Player.configure_clock(player)
    assert player._clock_status == "Clockwheel waiting for library"


def test_a_missing_preset_or_unplayable_type_stops_loading(tmp_path):
    sources = [{"id": "src1", "kind": "music", "path": str(tmp_path), "recursive": True}]
    ClockwheelStore(tmp_path / "clockwheels").save(
        clock([MUSIC, {"type": "settings", "preset": "nope"}])
    )
    player = _player(tmp_path, {"name": "Morning"}, sources)
    Player.configure_clock(player)
    assert player._clock is None
    assert "nope" in player._clock_status
    register_step_type("test_unplayable", lambda value: {"type": "test_unplayable"})
    ClockwheelStore(tmp_path / "clockwheels").save(
        clock([MUSIC, {"type": "test_unplayable"}], name="Other")
    )
    player = _player(tmp_path, {"name": "Other"}, sources)
    Player.configure_clock(player)
    assert "which the player cannot play" in player._clock_status


CATEGORY = {"type": "category", "category": "c1"}


def test_category_steps_validate_through_the_registry():
    steps = validate_clockwheel(clock([CATEGORY]))["steps"]
    assert steps == [
        {"type": "category", "category": "c1", "selection": "least_recent", "enforce_rules": True}
    ]
    for step, message in [
        ({**CATEGORY, "folder": "x"}, "unknown fields: folder"),
        ({"type": "category"}, "needs: category"),
        ({**CATEGORY, "category": "../c1"}, "saved category by its ID"),
        ({**CATEGORY, "selection": "weighted"}, "least_recent or random"),
        ({**CATEGORY, "enforce_rules": 1}, "true or false"),
    ]:
        with pytest.raises(ValueError, match=message):
            validate_clockwheel(clock([step, MUSIC]))
    with pytest.raises(ValueError, match="category step needs a category"):
        ClockSlot(kind="category")


def test_category_slots_pick_their_songs_and_skip_an_empty_or_missing_category():
    songs = [SimpleNamespace(path=f"/music/{n}.flac") for n in range(4)]
    members = {"a": ("A", frozenset({songs[1].path, songs[2].path})), "e": ("Empty", frozenset())}
    slots = [
        ClockSlot(kind="category", category="gone"),
        ClockSlot(kind="category", category="e"),
        ClockSlot(kind="category", category="a"),
    ]
    played = {songs[1].path: 10.0, songs[2].path: 5.0}
    picked = pick_slot(slots, 0, songs, played, lambda _: True, categories=members)
    assert (picked.entry, picked.next_cursor) == (songs[2], 0)
    assert picked.skipped == [
        "category step gone: the category no longer exists",
        "category Empty: the category has no songs",
    ]
    blocked = pick_slot(slots, 2, songs, played, lambda _: False, categories=members)
    assert blocked.entry is None
    assert "category A: all 2 matching entries blocked by rules" in blocked.skipped
    free = [ClockSlot(kind="category", category="a", selection="random", enforce_rules=False)]
    assert pick_slot(free, 0, songs, {}, lambda _: False, categories=members).entry in songs[1:3]
    # A category whose songs are all outside the pool (DMCA, just played) is skipped too.
    outside = pick_slot(slots[2:], 0, songs[:1], {}, lambda _: True, categories=members)
    assert outside.skipped == ["category A: no matching entries"]


def test_player_loads_category_steps_even_for_a_category_deleted_since(tmp_path):
    sources = [{"id": "src1", "kind": "music", "path": str(tmp_path), "recursive": True}]
    ClockwheelStore(tmp_path / "clockwheels").save(
        clock([{**CATEGORY, "selection": "random", "enforce_rules": False}, MUSIC])
    )
    player = _player(tmp_path, {"name": "Morning"}, sources)
    Player.configure_clock(player)
    assert player._clock_status == "Ready"
    slot = player._clock_slots[0]
    assert (slot.kind, slot.category, slot.selection, slot.enforce_rules) == (
        "category",
        "c1",
        "random",
        False,
    )
