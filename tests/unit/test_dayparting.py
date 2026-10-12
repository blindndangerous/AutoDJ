"""Dayparting: rotation categories with allowed days and hours, enforced in every pick mode."""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from zoneinfo import ZoneInfo

import pytest

from autodj.categories import (
    Category,
    CategoryError,
    CategoryIndex,
    CategoryStore,
    Resolution,
    describe_daypart,
    parse_spec,
    station_now,
)
from autodj.clockwheel import ClockSlot, pick_slot
from autodj.player import Player
from tests.unit.test_dmca import _player, _with_library

DENVER = ZoneInfo("America/Denver")


def category(**changes: Any) -> Category:
    values: dict[str, Any] = {"id": "c1", "name": "Late night", "source": "query", "query": "a:b"}
    values.update(changes)
    return Category.model_validate(values)


def wall(text: str) -> datetime:
    return datetime.fromisoformat(text)


# -- the rule ------------------------------------------------------------------


def test_a_category_without_days_or_hours_always_allows() -> None:
    plain = category()
    assert not plain.dayparted
    assert plain.allows(wall("2026-10-12 03:00"))
    assert describe_daypart(plain) == ""


def test_hours_on_chosen_days_start_included_end_left_out() -> None:
    mornings = category(days=[0, 1, 2, 3, 4], hours_from="06:00", hours_until="10:00")
    assert mornings.allows(wall("2026-10-12 06:00"))  # Monday
    assert mornings.allows(wall("2026-10-12 09:59"))
    assert not mornings.allows(wall("2026-10-12 10:00"))
    assert not mornings.allows(wall("2026-10-12 05:59"))
    assert not mornings.allows(wall("2026-10-17 07:00"))  # Saturday


def test_a_window_across_midnight_belongs_to_the_day_it_starts() -> None:
    friday_night = category(days=[4], hours_from="22:00", hours_until="02:00")
    assert friday_night.allows(wall("2026-10-16 22:00"))  # Friday
    assert friday_night.allows(wall("2026-10-17 01:59"))  # Saturday, still Friday's window
    assert not friday_night.allows(wall("2026-10-17 02:00"))
    assert not friday_night.allows(wall("2026-10-17 22:30"))  # Saturday's own night
    assert not friday_night.allows(wall("2026-10-16 01:00"))  # Thursday's window
    every_night = category(hours_from="22:00", hours_until="02:00")
    assert every_night.allows(wall("2026-10-13 00:30"))
    assert not every_night.allows(wall("2026-10-13 12:00"))


def test_days_without_hours_allow_the_whole_day() -> None:
    weekends = category(days=[6, 5, 5])
    assert weekends.days == [5, 6]
    assert weekends.allows(wall("2026-10-17 00:00"))
    assert not weekends.allows(wall("2026-10-19 12:00"))


def test_hours_are_station_wall_clock_times_across_daylight_saving() -> None:
    def at(utc_text: str) -> datetime:
        instant = datetime.fromisoformat(utc_text).replace(tzinfo=UTC)
        return instant.astimezone(DENVER).replace(tzinfo=None)

    # Fall back, 1 November 2026: 01:00 to 02:00 happens twice, and both are allowed.
    repeated = category(hours_from="01:00", hours_until="02:00")
    assert repeated.allows(at("2026-11-01 07:30"))
    assert repeated.allows(at("2026-11-01 08:30"))
    assert not repeated.allows(at("2026-11-01 09:00"))
    # Spring forward, 8 March 2026: 02:00 to 03:00 never happens.
    skipped = category(hours_from="02:00", hours_until="03:00")
    assert not skipped.allows(at("2026-03-08 08:59"))
    assert not skipped.allows(at("2026-03-08 09:00"))


def test_station_now_reads_the_station_time_zone() -> None:
    cfg = SimpleNamespace(server=SimpleNamespace(station_timezone="Pacific/Auckland"))
    expected = datetime.now(ZoneInfo("Pacific/Auckland")).replace(tzinfo=None)
    assert abs((station_now(cfg) - expected).total_seconds()) < 5


@pytest.mark.parametrize(
    ("changes", "field", "message"),
    [
        ({"days": [7]}, "days", "Days must be numbered 0 (Monday) to 6 (Sunday)"),
        (
            {"hours_from": "06:00"},
            "hours_until",
            "Enter both Allowed from and Allowed until, or leave both empty",
        ),
        (
            {"hours_until": "06:00"},
            "hours_from",
            "Enter both Allowed from and Allowed until, or leave both empty",
        ),
        (
            {"hours_from": "06:00", "hours_until": "06:00"},
            "hours_until",
            "Allowed until must differ from Allowed from",
        ),
        (
            {"hours_from": "06:00:30", "hours_until": "07:00"},
            "hours_from",
            "Allowed from must be a whole minute",
        ),
    ],
)
def test_bad_days_and_hours_name_the_field(
    changes: dict[str, Any], field: str, message: str
) -> None:
    body = {"name": "Late night", "source": "query", "query": "genre:Jazz", **changes}
    with pytest.raises(CategoryError) as caught:
        parse_spec(body)
    assert (caught.value.field, caught.value.message) == (field, message)


def test_an_unreadable_time_names_its_field() -> None:
    with pytest.raises(CategoryError) as caught:
        parse_spec({"name": "x", "source": "query", "query": "a:b", "hours_from": "late"})
    assert caught.value.field == "hours_from"


def test_dayparts_in_words() -> None:
    assert describe_daypart(
        category(days=[0, 1, 2, 3, 4], hours_from="06:00", hours_until="10:00")
    ) == ("Plays only on weekdays from 06:00 until 10:00")
    assert describe_daypart(category(days=[5, 6])) == "Plays only at weekends"
    assert describe_daypart(category(days=[4], hours_from="22:00", hours_until="02:00")) == (
        "Plays only on Friday from 22:00 until 02:00"
    )
    assert describe_daypart(category(days=[0, 2, 4])) == (
        "Plays only on Monday, Wednesday and Friday"
    )
    assert describe_daypart(category(hours_from="22:00", hours_until="02:00")) == (
        "Plays only every day from 22:00 until 02:00"
    )
    # All seven days and no hours hold nothing back, so the words say so.
    every_day = category(days=list(range(7)))
    assert describe_daypart(every_day) == "Plays every day, at any hour"
    assert every_day.allows(wall("2026-10-12 03:00"))


def test_files_without_dayparts_load_unchanged_and_stay_readable(tmp_path: Path) -> None:
    path = tmp_path / "categories.json"
    old = {"name": "A", "source": "query", "query": "genre:Rock", "playlist": "", "id": "a1"}
    path.write_text(json.dumps({"schema_version": 1, "categories": [old]}), encoding="utf-8")
    store = CategoryStore(path)
    [loaded] = store.load()
    assert not loaded.dayparted
    store.save([loaded, category(id="n1", days=[4], hours_from="22:00", hours_until="02:00")])
    saved = json.loads(path.read_text(encoding="utf-8"))["categories"]
    assert saved[0] == old
    assert (saved[1]["days"], saved[1]["hours_from"], saved[1]["hours_until"]) == (
        [4],
        "22:00:00",
        "02:00:00",
    )
    assert store.load()[1].allows(wall("2026-10-17 01:00"))


def test_the_index_names_the_songs_and_categories_outside_their_hours(tmp_path: Path) -> None:
    index = CategoryIndex.__new__(CategoryIndex)
    index._closed = None
    night = category(id="night", hours_from="22:00", hours_until="02:00")
    rock = category(id="rock", name="Rock")
    index.resolutions = lambda: (  # type: ignore[method-assign]
        (night, rock),
        {"night": Resolution(frozenset({"a", "b"})), "rock": Resolution(frozenset({"b", "c"}))},
    )
    assert index.closed_now(wall("2026-10-12 23:00")) == (frozenset(), {})
    paths, reasons = index.closed_now(wall("2026-10-12 12:00"))
    # "b" is also in Rock, which allows it, but Late night still holds it back.
    assert paths == frozenset({"a", "b"})
    assert reasons == {"night": "outside its hours: plays only every day from 22:00 until 02:00"}


# -- picking -------------------------------------------------------------------


def _dayparted(player: Player, titles: set[str], *, clock: bool = False) -> Player:
    """Hold back the songs with *titles*: their category is outside its hours."""
    _with_library(player)
    entries = player._sim.entries_snapshot()
    paths = frozenset(entry.path for entry in entries if entry.title in titles)
    reason = "outside its hours: plays only every day from 22:00 until 02:00"
    player._library.categories = SimpleNamespace(
        members=lambda: {"night": ("Late night", paths)},
        closed_now=lambda _wall: (paths, {"night": reason}) if paths else (frozenset(), {}),
    )
    if clock:
        player._clock = {"cursor": 0}
        player._clock_slots = [
            ClockSlot(kind="category", category="night", selection="random"),
            ClockSlot(folder="Z:/Music", recursive=True, selection="random"),
        ]
    return player


NIGHT = {"Four", "Five (feat. Alpha Band)", "Fine"}


@pytest.mark.parametrize("mode", ["similarity", "random", "least_recent"])
def test_every_pick_mode_leaves_out_songs_outside_their_hours(mode: str) -> None:
    player = _dayparted(_player(mode, dmca=False), NIGHT)
    current = player.playable_entries()[0]
    for track_number in range(1, 12):
        player._state.track_number = track_number
        picked, _how = player._choose_next(current, player._pick_context())
        assert picked.title not in NIGHT


def test_discovery_leaves_out_songs_outside_their_hours() -> None:
    player = _dayparted(_player("similarity", dmca=False), NIGHT)
    player._discovery_every = 1
    player._state.discovery_enabled = True
    current = player.playable_entries()[0]
    for track_number in range(1, 8):
        player._state.track_number = track_number
        picked, _how = player._choose_next(current, player._pick_context())
        assert picked.title not in NIGHT


def test_relaxing_the_repeat_rules_never_picks_a_song_outside_its_hours() -> None:
    # Only One is open; it just played, so the track window is relaxed for it.
    others = {"Two", "Three"} | NIGHT
    player = _dayparted(_player("random", dmca=False), others)
    first = player.playable_entries()[0]
    with player._pick_lock:
        player._state.record_played(first)
    for _ in range(6):
        picked, _how = player._choose_next(player.playable_entries()[1], player._pick_context())
        assert picked.title == "One"


def test_a_clock_skips_a_category_step_outside_its_hours_and_says_why(caplog) -> None:
    player = _dayparted(_player("clock", dmca=False), NIGHT, clock=True)
    with caplog.at_level(logging.WARNING, logger="autodj.player"):
        for _ in range(6):
            picked = player._clock_pick(player._pick_context(), 0)
            assert picked.entry.title not in NIGHT
    assert player._clock_status == (
        "category Late night: outside its hours: plays only every day from 22:00 until 02:00"
    )
    assert caplog.text.count("the clockwheel moved on to its next step") == 1


def test_a_clock_of_closed_categories_plays_an_allowed_song_from_the_library(caplog) -> None:
    player = _dayparted(_player("clock", dmca=False), NIGHT, clock=True)
    player._clock_slots = player._clock_slots[:1]
    current = player.playable_entries()[0]
    with caplog.at_level(logging.WARNING, logger="autodj.player"):
        for _ in range(4):
            picked, mode, *_rest = player._choose_station_next(current, player._pick_context())
            assert picked.title not in NIGHT
            assert mode == "fallback"
        assert player._random_start_entry().title not in NIGHT
    assert "picking one at random" in caplog.text


def test_a_clock_whose_only_songs_are_outside_their_hours_plays_an_allowed_song(
    caplog,
) -> None:
    # A track step (or a folder) holding only closed songs must not stop the
    # music: dayparting alone emptied it, so an allowed song plays instead.
    player = _dayparted(_player("clock", dmca=False), NIGHT)
    night = next(entry for entry in player.playable_entries() if entry.title == "Fine")
    player._clock = {"cursor": 0}
    player._clock_slots = [ClockSlot(kind="track", path=night.path)]
    current = player.playable_entries()[0]
    with caplog.at_level(logging.WARNING, logger="autodj.player"):
        picked, mode, *_rest = player._choose_station_next(current, player._pick_context())
        start = player._random_start_entry()
    assert picked.title not in NIGHT
    assert mode == "fallback"
    assert start is not None
    assert start.title not in NIGHT
    assert player._clock_status.endswith("every song this clockwheel can play is outside its hours")


def test_a_clock_with_nothing_to_play_at_any_hour_still_stops() -> None:
    # The fallback is only for dayparting: a missing recording is left as it was.
    player = _dayparted(_player("clock", dmca=False), NIGHT)
    player._clock = {"cursor": 0}
    player._clock_slots = [ClockSlot(kind="track", path="Z:/Music/missing.flac")]
    current = player.playable_entries()[0]
    with pytest.raises(ValueError, match="recording missing"):
        player._choose_station_next(current, player._pick_context())
    assert not player._clock_closed_only


def test_a_pick_outside_its_hours_is_replaced_and_a_queued_one_plays(caplog) -> None:
    player = _dayparted(_player("random", dmca=False), NIGHT)
    context = player._pick_context()
    night = next(entry for entry in player.playable_entries() if entry.title == "Fine")
    with caplog.at_level(logging.INFO, logger="autodj.player"):
        replaced = player._dmca_checked(night, context)
    assert replaced.title not in NIGHT
    assert "is outside its category's hours" in caplog.text
    assert player._dmca_checked(night, context, queued=True) is night


def test_when_every_song_is_outside_its_hours_music_plays_with_one_warning(caplog) -> None:
    everything = {entry.title for entry in _player("random").playable_entries()}
    player = _dayparted(_player("random", dmca=False), everything)
    current = player.playable_entries()[0]
    with caplog.at_level(logging.DEBUG, logger="autodj.player"):
        picked, how = player._choose_next(current, player._pick_context())
    assert how == "fallback"
    assert picked.path != current.path
    warnings = [
        r.getMessage()
        for r in caplog.records
        if r.levelno == logging.WARNING and r.getMessage().startswith("Dayparting")
    ]
    # The fallback and the final check both meet it; only the first warns.
    assert warnings == [
        "Dayparting: every song is outside its category's hours; "
        "playing one anyway rather than stopping the music"
    ]
    assert "no other song can play" in caplog.text  # at debug
    caplog.clear()
    with caplog.at_level(logging.WARNING, logger="autodj.player"):
        for _ in range(3):
            player._choose_next(current, player._pick_context())
    assert "Dayparting" not in caplog.text  # the same hours: no new warning


def test_the_all_closed_warning_comes_back_when_the_hours_change(caplog) -> None:
    everything = {entry.title for entry in _player("random").playable_entries()}
    player = _dayparted(_player("random", dmca=False), everything)
    current = player.playable_entries()[0]
    closed_now = player._library.categories.closed_now
    player._choose_next(current, player._pick_context())
    # The hours open for a while, then close again.
    player._library.categories.closed_now = lambda _wall: (frozenset(), {})
    player._choose_next(current, player._pick_context())
    player._library.categories.closed_now = closed_now
    caplog.clear()
    with caplog.at_level(logging.WARNING, logger="autodj.player"):
        player._choose_next(current, player._pick_context())
    assert caplog.text.count("every song is outside its category's hours") == 1


def test_categories_that_cannot_be_read_limit_nothing(caplog) -> None:
    player = _dayparted(_player("random", dmca=False), NIGHT)

    def broken(_wall: datetime) -> None:
        raise OSError("disk gone")

    player._library.categories.closed_now = broken
    with caplog.at_level(logging.ERROR, logger="autodj.player"):
        assert player._pick_context().closed == frozenset()
    assert "Dayparting: the rotation categories could not be read" in caplog.text


def test_pick_slot_skips_a_closed_category_without_a_player() -> None:
    entries = _player("random").playable_entries()
    paths = frozenset(entry.path for entry in entries)
    picked = pick_slot(
        [ClockSlot(kind="category", category="night", selection="random")],
        0,
        entries,
        {},
        lambda _entry: True,
        categories={"night": ("Late night", paths)},
        closed_categories={"night": "outside its hours"},
    )
    assert picked.entry is None
    assert picked.skipped == ["category Late night: outside its hours"]


def test_the_clock_pool_leaves_out_silent_protected_and_closed_songs_in_order() -> None:
    """The cached pool a clock pick starts from matches filtering the library each time."""
    import random

    player = Player.__new__(Player)
    entries = tuple(SimpleNamespace(path=f"{n}.flac", is_silent=n % 11 == 0) for n in range(60))
    rng = random.Random(5)
    for _ in range(50):
        protected = frozenset(f"{rng.randrange(70)}.flac" for _ in range(rng.randrange(4)))
        closed = frozenset(f"{n}.flac" for n in range(60) if rng.random() < 0.2)
        expected = tuple(
            entry
            for entry in entries
            if not entry.is_silent and entry.path not in protected and entry.path not in closed
        )
        assert player._eligible_entries(entries, protected, closed) == expected
    # Two entries with one path: still every copy but the protected one goes.
    twice = (*entries[:3], SimpleNamespace(path="1.flac", is_silent=False))
    assert player._eligible_entries(twice, frozenset({"1.flac"}), frozenset()) == (entries[2],)


# -- the cost of a pick ---------------------------------------------------------


def _reference_closed(
    categories: tuple[Category, ...], results: dict[str, Resolution], when: datetime
) -> tuple[frozenset[str], dict[str, str]]:
    """closed_now without its cache, as it was worked out before the cache."""
    paths: set[str] = set()
    reasons: dict[str, str] = {}
    from autodj.categories import daypart_refusal

    for item in categories:
        if item.dayparted and not item.allows(when):
            paths |= results[item.id].paths
            text = describe_daypart(item)
            word = daypart_refusal(item, when)
            reasons[item.id] = f"outside its {word}: {text[0].lower()}{text[1:]}"
    return frozenset(paths), reasons


def test_the_closed_songs_cached_per_minute_match_working_them_out_each_time() -> None:
    """Equivalence: the cache never changes an answer, whatever order the times come in."""
    import random
    from datetime import timedelta

    rng = random.Random(140)
    index = CategoryIndex.__new__(CategoryIndex)
    index._closed = None
    for _round in range(20):
        cats = []
        results: dict[str, Resolution] = {}
        for number in range(rng.randrange(1, 6)):
            changes: dict[str, Any] = {"id": f"c{number}", "name": f"C{number}"}
            if rng.random() < 0.7:
                changes["days"] = rng.sample(range(7), rng.randrange(0, 8))
            if rng.random() < 0.7:
                start, end = rng.sample(range(24 * 60), 2)
                changes["hours_from"] = f"{start // 60:02d}:{start % 60:02d}"
                changes["hours_until"] = f"{end // 60:02d}:{end % 60:02d}"
            cats.append(category(**changes))
            results[f"c{number}"] = Resolution(
                frozenset(f"{n}.flac" for n in rng.sample(range(50), rng.randrange(0, 20)))
            )
        resolved = (tuple(cats), results)
        index.resolutions = lambda resolved=resolved: resolved  # type: ignore[method-assign]
        base = datetime(2026, 10, 12) + timedelta(minutes=rng.randrange(7 * 24 * 60))
        for _ in range(60):
            # The same minute again, the next minute, or a jump anywhere in the week.
            step = rng.choice((0, 0, 1, 59, rng.randrange(-3000, 3000)))
            base += timedelta(minutes=step, seconds=rng.randrange(60) - base.second)
            assert index.closed_now(base) == _reference_closed(*resolved, base)


def test_the_closed_songs_are_one_set_while_they_stay_the_same() -> None:
    index = CategoryIndex.__new__(CategoryIndex)
    index._closed = None
    night = category(id="night", hours_from="22:00", hours_until="02:00")
    results = {"night": Resolution(frozenset({"a", "b"}))}
    index.resolutions = lambda: ((night,), results)  # type: ignore[method-assign]
    first, _ = index.closed_now(wall("2026-10-12 12:00:05"))
    same_minute, _ = index.closed_now(wall("2026-10-12 12:00:59"))
    later, reasons = index.closed_now(wall("2026-10-12 15:30"))
    assert first is same_minute is later  # one object, so the player's caches hold
    reasons["night"] = "changed by a caller"
    assert index.closed_now(wall("2026-10-12 15:30"))[1]["night"].startswith("outside its hours")
    # New category results (an edit, a rescan) are read again at once.
    results = {"night": Resolution(frozenset({"a"}))}
    assert index.closed_now(wall("2026-10-12 15:30"))[0] == frozenset({"a"})
    assert index.closed_now(wall("2026-10-12 23:00"))[0] == frozenset()


def test_a_saved_category_change_reaches_the_closed_songs_at_once(tmp_path: Path) -> None:
    song = SimpleNamespace(
        path="a.flac",
        is_silent=False,
        title="A",
        artist="",
        album="",
        genre="Jazz",
        year=2000,
        bpm=0,
        length=1,
    )
    cfg = SimpleNamespace(
        index=SimpleNamespace(active_dir=tmp_path),
        library=SimpleNamespace(beets_db=None, music_dir=None, playlists_folder=None),
    )
    entries = (song,)
    index = CategoryIndex(cfg, lambda: entries)  # type: ignore[arg-type]
    plain = category(id="j", name="Jazz", query="genre:Jazz")
    index.save([plain])
    noon = wall("2026-10-12 12:00")
    assert index.closed_now(noon)[0] == frozenset()
    late = category(
        id="j", name="Jazz", query="genre:Jazz", hours_from="22:00", hours_until="02:00"
    )
    index.save([late])
    assert index.closed_now(noon)[0] == frozenset({"a.flac"})


def test_a_pick_reads_the_hours_when_the_song_is_expected_to_start() -> None:
    player = _dayparted(_player("random", dmca=False), NIGHT)
    asked: list[datetime] = []
    closed_now = player._library.categories.closed_now

    def recording(when: datetime) -> Any:
        asked.append(when)
        return closed_now(when)

    player._library.categories.closed_now = recording
    player._cfg.server.station_timezone = "UTC"
    noon = datetime(2026, 10, 12, 12, tzinfo=UTC).timestamp()
    player._now = lambda: noon  # type: ignore[method-assign]
    player._pick_context()
    player._pick_context(at=noon + 240)
    assert asked == [wall("2026-10-12 12:00"), wall("2026-10-12 12:04")]


def test_the_expected_start_is_when_the_song_ahead_reaches_its_handoff() -> None:
    player = _player("random", dmca=False)
    current = player.playable_entries()[0]
    player._now = lambda: 1000.0  # type: ignore[method-assign]
    player._clock_music_span = lambda _entry: (5.0, 185.0)  # type: ignore[method-assign]
    assert player._handoff_time(current) == 1180.0
    # Rendering ahead: the song playing now has 30 seconds left before *current*.
    player._playing_render = SimpleNamespace(
        next_entry=current, audio=[0] * (44100 * 40), start_offset=0
    )
    player._playback_pos = [44100 * 10]
    assert player._handoff_time(current) == 1210.0

    def unreadable(_entry: Any) -> tuple[float, float]:
        raise OSError("catalog busy")

    # Cues that cannot be read: the whole song.
    player._playing_render = None
    player._clock_music_span = unreadable  # type: ignore[method-assign]
    assert player._handoff_time(current) == 1000.0 + current.length


def test_a_song_whose_window_closes_before_it_would_start_is_not_picked() -> None:
    # Late night ends at 02:00.  At 01:58, with four minutes of the song
    # playing still to go, a Late night song would start after its window.
    player = _dayparted(_player("random", dmca=False), set())
    entries = player._sim.entries_snapshot()
    night_paths = frozenset(entry.path for entry in entries if entry.title in NIGHT)
    reason = "outside its hours: plays only every day from 22:00 until 02:00"
    player._library.categories.closed_now = lambda when: (
        (frozenset(), {}) if when.hour < 2 else (night_paths, {"night": reason})
    )
    player._cfg.server.station_timezone = "UTC"
    almost = datetime(2026, 10, 13, 1, 58, tzinfo=UTC).timestamp()
    player._now = lambda: almost  # type: ignore[method-assign]
    current = next(entry for entry in entries if entry.title not in NIGHT)
    player._clock_music_span = lambda _entry: (0.0, 240.0)  # type: ignore[method-assign]
    player._state.current_track = current
    player.note_browser_start(current)
    for _ in range(12):
        assert player._pick_next(current).title not in NIGHT


def test_a_browser_pick_is_timed_from_what_is_left_of_the_song_playing() -> None:
    player = _player("random", dmca=False)
    current, other = player.playable_entries()[:2]
    now = [1000.0]
    player._now = lambda: now[0]  # type: ignore[method-assign]
    player._clock_music_span = lambda _entry: (0.0, 200.0)  # type: ignore[method-assign]
    player._state.current_track = current
    player.note_browser_start(current)
    assert player._browser_handoff(current) == 1200.0
    now[0] = 1150.0  # 150 seconds in, the next track is picked again (a skip, say)
    assert player._browser_handoff(current) == 1200.0
    now[0] = 1300.0  # running long: it starts now at the earliest
    assert player._browser_handoff(current) == 1300.0
    # A track that is no longer playing hands over now.
    assert player._browser_handoff(other) == 1300.0


def test_advancing_in_the_browser_times_each_pick_by_when_it_plays() -> None:
    from autodj._bridge import PlayerBridge

    player = _player("random", dmca=False)
    current = player.playable_entries()[0]
    player._state.current_track = current
    now = [5000.0]
    player._now = lambda: now[0]  # type: ignore[method-assign]
    player._clock_music_span = lambda entry: (0.0, float(entry.length))  # type: ignore[method-assign]
    asked: list[float | None] = []
    real = player._pick_next

    def recording(seed: Any, *, at: float | None = None) -> Any:
        asked.append(at)
        return real(seed, at=at)

    player._pick_next = recording  # type: ignore[method-assign]
    bridge = PlayerBridge(player=player, sim=player._sim)
    bridge.advance_now()
    nxt = player._state.current_track
    # The replacement plays now; the one after it when the new track ends.
    assert asked == [5000.0, 5000.0 + nxt.length]
