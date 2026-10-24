"""Shows (#106): the file, generating a list, airing it, and the hour preview."""

from __future__ import annotations

import asyncio
import json
import time
from datetime import UTC, datetime
from datetime import time as clock_time
from pathlib import Path
from typing import Any

import pytest

from autodj._bridge import PlayerBridge
from autodj.hour_preview import build_preview
from autodj.repeat_rules import PlayRecord
from autodj.scheduler import EventError, ScheduledEvent
from autodj.shows import (
    AHEAD,
    MAX_SONGS,
    PreviewShows,
    ShowError,
    ShowGenerations,
    ShowRunner,
    ShowSpec,
    ShowStore,
    generate,
    length_text,
    parse_spec,
    preview_shows,
    show_action,
    songs_view,
    with_songs,
)
from tests.unit._rotation_station import category_step, liner, liner_step, make_station, song

# Monday 12 October 2026, 12:10 UTC.
NOW = datetime(2026, 10, 12, 12, 10, tzinfo=UTC).timestamp()


def _station(tmp_path: Path, **changes: Any):
    """Hits (20 songs, the 2000s) and Gold (40) under a liner, Gold, Hits clockwheel."""
    hits = [song(n, f"Hits {n}", length=180.0) for n in range(20)]
    gold = [song(100 + n, f"Gold {n}", length=240.0) for n in range(40)]
    options: dict[str, Any] = {
        "liners": [liner(1), liner(2)],
        "categories": [
            {"id": "hits", "name": "Remembering the 2000s", "members": hits},
            {"id": "gold", "name": "Gold", "members": gold},
        ],
        "clockwheels": [
            {
                "name": "Throwback",
                "resync_minutes": [],
                "steps": [liner_step(), category_step("gold"), category_step("hits")],
            }
        ],
        "active_clock": "Throwback",
        "playback": {"pick_mode": "clock"},
    }
    options.update(changes)
    return make_station(tmp_path, [*hits, *gold], **options), hits, gold


def _spec(**fields: Any) -> ShowSpec:
    return parse_spec(
        {"name": "Remembering the 2000s", "source": "category", "category": "hits", **fields}
    )


def _playing(source: Any, entry: Any, started: float) -> None:
    state = source.player._state
    state.current_track = entry
    state.record_played(entry)
    state.play_history[-1] = PlayRecord.of(entry, started)


# -- the spec and the file -----------------------------------------------------------


def test_a_spec_names_the_field_at_fault() -> None:
    assert _spec().clockwheel == ""
    spec = parse_spec({"name": " Throwback hour ", "source": "clockwheel", "clockwheel": "Main"})
    assert (spec.name, spec.category, spec.target_minutes) == ("Throwback hour", "", 60)
    for body, field in [
        ({"name": "", "source": "category", "category": "hits"}, "name"),
        ({"name": "A", "source": "category", "category": ""}, "category"),
        ({"name": "A", "source": "clockwheel", "clockwheel": " "}, "clockwheel"),
        (
            {"name": "A", "source": "category", "category": "hits", "target_minutes": 2},
            "target_minutes",
        ),
        ({"name": "A", "source": "category", "category": "hits", "extra": 1}, "extra"),
        ({"name": "A", "source": "playlist"}, "source"),
    ]:
        with pytest.raises(ShowError) as caught:
            parse_spec(body)
        assert caught.value.field == field, body


def test_the_store_adds_changes_and_removes_shows(tmp_path: Path) -> None:
    store = ShowStore(tmp_path / "shows.json")
    assert store.all() == {}
    show_id, show = store.add(_spec())
    assert show.songs == []
    with pytest.raises(ShowError) as caught:
        store.add(_spec(name="remembering THE 2000s"))
    assert caught.value.field == "name"
    store.update(show_id, lambda current: with_songs(current, ["/music/a.flac"]))
    # Another reader sees the saved file.
    again = ShowStore(tmp_path / "shows.json")
    assert again.get(show_id).songs == ["/music/a.flac"]
    document = json.loads((tmp_path / "shows.json").read_text(encoding="utf-8"))
    assert document["schema_version"] == 1
    assert list(document["shows"]) == [show_id]
    assert store.delete(show_id).name == "Remembering the 2000s"
    with pytest.raises(KeyError):
        store.delete(show_id)
    with pytest.raises(ShowError):
        with_songs(show, ["/music/x.flac"] * (MAX_SONGS + 1))


def test_a_damaged_show_is_skipped_and_kept_and_a_damaged_file_set_aside(tmp_path: Path) -> None:
    path = tmp_path / "shows.json"
    good = _spec().model_dump()
    path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "shows": {"a" * 32: {**good, "songs": []}, "b" * 32: {"name": 5}},
            }
        ),
        encoding="utf-8",
    )
    store = ShowStore(path)
    assert list(store.all()) == ["a" * 32]
    assert "1 show in shows.json is not valid" in store.problem()
    store.update("a" * 32, lambda show: with_songs(show, ["/music/b.flac"]))
    kept = json.loads(path.read_text(encoding="utf-8"))["shows"]
    assert kept["b" * 32] == {"name": 5}
    path.write_text("not json", encoding="utf-8")
    assert store.all() == {}
    assert [item.name.startswith("shows.json.bad-") for item in tmp_path.iterdir()].count(True) == 1


def test_lengths_read_well() -> None:
    assert length_text(3725) == "1:02:05"
    assert length_text(65) == "1:05"


# -- generating a list -----------------------------------------------------------------


def _changes_nothing(source: Any) -> tuple:
    state = source.player._state
    return (
        list(state.play_history),
        list(state.recently_played),
        dict(source.player._clock or {}),
        list(state.queue),
        state.current_track,
    )


def test_a_category_show_is_generated_from_that_category_only(tmp_path: Path) -> None:
    source, hits, _gold = _station(tmp_path)
    before = _changes_nothing(source)
    made = generate(source, "c" * 32, _spec(target_minutes=30), start=NOW, seed=3)
    members = {entry.path for entry in hits}
    assert made.songs and set(made.songs) <= members
    assert len(made.songs) == len(set(made.songs))
    # Ten songs of three minutes reach the target of 30 minutes.
    assert made.seconds == pytest.approx(1800.0)
    assert made.note == ""
    assert _changes_nothing(source) == before
    assert source.ledger.writes == 0
    assert source.library.catalog.writes == 0
    # Another seed gives another order.
    other = generate(source, "c" * 32, _spec(target_minutes=30), start=NOW, seed=4)
    assert other.songs != made.songs


def test_a_category_that_runs_out_says_so(tmp_path: Path) -> None:
    source, hits, _gold = _station(tmp_path)
    made = generate(source, "c" * 32, _spec(target_minutes=180), start=NOW, seed=3)
    # Twenty songs, each at most once: an hour.
    assert len(made.songs) == len(hits)
    assert made.note.startswith("The list is 1:00:00, shorter than the target of 3:00:00")


def test_a_clockwheel_show_leaves_the_liners_out(tmp_path: Path) -> None:
    source, hits, gold = _station(tmp_path)
    spec = parse_spec({"name": "Throwback hour", "source": "clockwheel", "clockwheel": "Throwback"})
    made = generate(source, "d" * 32, spec, start=NOW, seed=5)
    paths = {entry.path for entry in (*hits, *gold)}
    assert made.songs and set(made.songs) <= paths
    golds = {entry.path for entry in gold}
    # The clockwheel's order: Gold, then Hits.
    assert made.songs[0] in golds and made.songs[1] not in golds


def test_a_missing_source_is_refused(tmp_path: Path) -> None:
    source, _hits, _gold = _station(tmp_path)
    with pytest.raises(ShowError) as caught:
        generate(source, "c" * 32, _spec(category="gone"))
    assert caught.value.field == "category"
    spec = parse_spec({"name": "A", "source": "clockwheel", "clockwheel": "Gone"})
    with pytest.raises(ShowError) as caught:
        generate(source, "c" * 32, spec)
    assert caught.value.field == "clockwheel"


def test_one_generation_at_a_time() -> None:
    generations = ShowGenerations()

    async def run() -> None:
        import threading

        release = threading.Event()

        def slow(_cancelled: Any) -> Any:
            release.wait(5)
            return "done"

        first = asyncio.ensure_future(generations.run(slow))
        await asyncio.sleep(0.05)
        with pytest.raises(ShowError, match="Another show"):
            await generations.run(lambda _cancelled: "never")
        release.set()
        assert await first == "done"
        assert await generations.run(lambda _cancelled: "again") == "again"

    asyncio.run(run())
    assert generations.shutdown(1.0)


# -- on the air ----------------------------------------------------------------------


class _Published:
    def __init__(self) -> None:
        self.messages: list[dict] = []

    async def __call__(self, message: dict) -> None:
        self.messages.append(message)


def _runner(source: Any, tmp_path: Path, songs: list[str], **spec: Any):
    bridge = PlayerBridge(
        player=source.player, sim=source.player._sim, library=source.library, ledger=source.ledger
    )
    store = ShowStore(tmp_path / "shows.json")
    show_id, _show = store.add(_spec(**spec))
    store.update(show_id, lambda show: with_songs(show, songs))
    published = _Published()
    runner = ShowRunner(bridge, store, published, state_path=tmp_path / "show_on_air.json")
    return runner, bridge, store, show_id, published


def _queue(source: Any) -> list[str]:
    return [entry.path for entry in source.player._state.queue]


def test_a_show_feeds_the_queue_and_ends(tmp_path: Path) -> None:
    source, hits, gold = _station(tmp_path)
    state = source.player._state
    _playing(source, gold[0], time.time() - 30)
    operator = gold[5]
    state.queue.append(operator)
    songs = [entry.path for entry in hits[:4]]
    runner, _bridge, _store, show_id, published = _runner(source, tmp_path, songs)

    async def run() -> None:
        await runner.start(show_id)
        # The show's first songs go first; the operator's song waits after them.
        assert _queue(source) == [*songs[:AHEAD], operator.path]
        status = runner.status()
        assert (status["name"], status["total"], status["waiting"], status["to_come"]) == (
            "Remembering the 2000s",
            4,
            AHEAD,
            2,
        )
        assert runner.upcoming() == ("Remembering the 2000s", tuple(songs[AHEAD:]))
        # The first show song starts: the next one joins after the show's last.
        for number in range(len(songs)):
            state.queue.pop(0)
            _playing(source, hits[number], time.time())
            runner.track_changed()
            await asyncio.sleep(0)
            for task in list(runner._tasks):
                await task
        assert runner.status() is None
        assert _queue(source) == [operator.path]

    asyncio.run(run())
    assert {"type": "shows_changed"} in published.messages


def test_a_show_skips_missing_songs_and_ones_that_break_dmca(tmp_path: Path) -> None:
    source, hits, gold = _station(tmp_path, dmca=True)
    state = source.player._state
    now = time.time()
    # Four songs by Hits 0 in the last hour: a fifth would break the artist limit.
    twin = [song(500 + n, "Hits 0", length=180.0) for n in range(4)]
    for number, entry in enumerate(twin):
        state.record_played(entry)
        state.play_history[-1] = PlayRecord.of(entry, now - 3000 + number * 200)
    _playing(source, gold[0], now - 20)
    songs = ["/music/gone.flac", hits[0].path, hits[1].path, hits[2].path]
    runner, *_rest, show_id, _published = _runner(source, tmp_path, songs)

    async def run() -> None:
        await runner.start(show_id)

    asyncio.run(run())
    assert _queue(source) == [hits[1].path, hits[2].path]
    status = runner.status()
    assert [item["reason"] for item in status["skipped"]] == [
        "it is not in the library",
        status["skipped"][1]["reason"],
    ]
    assert "DMCA" in status["skipped"][1]["reason"]
    assert status["skipped"][1]["title"] == "Hits 0, Song 0"


def test_stopping_a_show_takes_its_songs_out_of_the_queue(tmp_path: Path) -> None:
    source, hits, gold = _station(tmp_path)
    _playing(source, gold[0], time.time() - 30)
    runner, *_rest, show_id, _published = _runner(
        source, tmp_path, [entry.path for entry in hits[:5]]
    )

    async def run() -> str | None:
        await runner.start(show_id)
        assert len(_queue(source)) == AHEAD
        return await runner.stop()

    assert asyncio.run(run()) == {"name": "Remembering the 2000s", "done": True}
    assert _queue(source) == []
    assert runner.status() is None


def test_a_show_needs_songs_and_something_playing(tmp_path: Path) -> None:
    source, hits, _gold = _station(tmp_path)
    runner, _bridge, store, show_id, _published = _runner(source, tmp_path, [hits[0].path])
    with pytest.raises(ValueError, match="Nothing is playing"):
        asyncio.run(runner.start(show_id))
    store.update(show_id, lambda show: with_songs(show, []))
    with pytest.raises(ValueError, match="generate its list first"):
        asyncio.run(runner.start(show_id))
    with pytest.raises(ValueError, match="no longer exists"):
        asyncio.run(runner.start("f" * 32))


def test_the_schedule_action_names_shows_and_checks_them(tmp_path: Path) -> None:
    source, hits, _gold = _station(tmp_path)
    runner, _bridge, store, show_id, _published = _runner(source, tmp_path, [hits[0].path])
    action = show_action(runner, store)
    assert action.metadata()["fields"] == [
        {
            "name": "show",
            "label": "Show",
            "type": "select",
            "options": [{"value": show_id, "label": "Remembering the 2000s"}],
        }
    ]
    assert action.check({"show": show_id}, live=True) == {"show": show_id}
    with pytest.raises(EventError):
        action.check({"show": "e" * 32}, live=True)
    assert action.describe({"show": show_id}) == "Play a show: Remembering the 2000s"
    assert (action.interrupt, action.soft_waits, action.hard is not None) == ("none", False, True)


def test_songs_view_marks_missing_files(tmp_path: Path) -> None:
    source, hits, _gold = _station(tmp_path)
    show = with_songs(ShowStore(tmp_path / "x.json").add(_spec())[1], [hits[0].path, "/m/x.mp3"])
    rows, total = songs_view(show, source.library.entry_for_path)
    assert total == 180.0
    assert [(row["title"], row["missing"]) for row in rows] == [
        ("Hits 0, Song 0", False),
        ("x.mp3", True),
    ]
    assert rows[0]["length_text"] == "3:00"


# -- the hour preview ------------------------------------------------------------------


def test_the_preview_lists_the_rest_of_a_show_on_the_air_as_fixed(tmp_path: Path) -> None:
    source, hits, gold = _station(tmp_path)
    _playing(source, gold[0], NOW - 50)
    source.player._state.queue.append(hits[0])
    shows = PreviewShows(on_air=("Remembering the 2000s", (hits[1], hits[2])))
    from dataclasses import replace

    view = build_preview(replace(source, shows=shows), 60, now=NOW)
    rows = [(item["status"], item["title"], item["status_text"]) for item in view["items"][:4]]
    assert rows[1:] == [
        ("queued", "Hits 0, Song 0", "Fixed: in the queue"),
        ("show", "Hits 1, Song 1", "Fixed: show Remembering the 2000s"),
        ("show", "Hits 2, Song 2", "Fixed: show Remembering the 2000s"),
    ]
    assert all(item["fixed"] for item in view["items"][:4])


def test_the_preview_plays_a_scheduled_show_from_its_time(tmp_path: Path) -> None:
    event = ScheduledEvent(
        id="a" * 32,
        name="Saturday throwback",
        action="show",
        params={"show": "c" * 32},
        repeat="daily",
        time=clock_time(12, 30),
    )
    source, hits, gold = _station(tmp_path, events=[event])
    _playing(source, gold[0], NOW - 50)
    planned = (hits[5], hits[6], hits[7])
    shows = preview_shows(None, None, [event], source.library.entry_for_path)
    assert shows.scheduled == {}
    shows = PreviewShows(scheduled={"c" * 32: ("Remembering the 2000s", planned)})
    from dataclasses import replace

    view = build_preview(replace(source, shows=shows), 60, now=NOW)
    show_rows = [item for item in view["items"] if item["status"] == "show"]
    assert [item["title"] for item in show_rows] == [
        "Hits 5, Song 5",
        "Hits 6, Song 6",
        "Hits 7, Song 7",
    ]
    first = datetime.fromisoformat(show_rows[0]["starts_at"]).timestamp()
    # From the first song change after 12:30.
    assert NOW + 20 * 60 <= first < NOW + 20 * 60 + 300
    assert all(item["fixed"] for item in show_rows)
    events = [point for point in view["points"] if point["kind"] == "event"]
    assert [point["label"] for point in events] == ["Saturday throwback"]


def test_preview_shows_reads_the_scheduled_shows(tmp_path: Path) -> None:
    source, hits, _gold = _station(tmp_path)
    store = ShowStore(tmp_path / "shows.json")
    show_id, _show = store.add(_spec())
    store.update(show_id, lambda show: with_songs(show, [hits[0].path, "/gone.flac"]))
    event = ScheduledEvent(
        id="a" * 32,
        name="Throwback",
        action="show",
        params={"show": show_id},
        repeat="daily",
        time=clock_time(20, 0),
    )
    found = preview_shows(store, None, [event], source.library.entry_for_path)
    assert found.on_air is None
    assert found.scheduled == {show_id: ("Remembering the 2000s", (hits[0],))}
    assert json.loads(found.signature())[1][0][2] == [hits[0].path]


def _settle(runner: ShowRunner) -> None:
    """A song change as the station reports it, and the runner's work after it."""

    async def run() -> None:
        runner.track_changed()
        await asyncio.sleep(0)
        for task in list(runner._tasks):
            await task

    asyncio.run(run())


def test_a_hard_start_plays_the_first_song_at_once(tmp_path: Path) -> None:
    source, hits, gold = _station(tmp_path)
    state = source.player._state
    _playing(source, gold[0], time.time() - 30)
    songs = ["/music/gone.flac", *(entry.path for entry in hits[:4])]
    runner, *_rest, show_id, _published = _runner(source, tmp_path, songs)
    # The stand-in ledger keeps no history rows.
    source.ledger.remember_history = lambda _row: None
    asyncio.run(runner.start(show_id, now=True))
    # Browser playback: the skip took the show's first song from the queue,
    # and the browser's next song came from it too.
    assert state.current_track.path == hits[0].path
    assert state.next_track.path == hits[1].path
    _settle(runner)
    assert _queue(source) == [entry.path for entry in hits[2:4]]
    status = runner.status()
    assert (status["played"], status["to_come"]) == (2, 0)
    assert [item["reason"] for item in status["skipped"]] == ["it is not in the library"]


def test_a_hard_start_in_browser_playback_keeps_the_operators_play_next(tmp_path: Path) -> None:
    source, hits, gold = _station(tmp_path)
    state = source.player._state
    _playing(source, gold[0], time.time() - 30)
    state.queued_next = gold[7]
    runner, *_rest, show_id, _published = _runner(
        source, tmp_path, [entry.path for entry in hits[:3]]
    )
    source.ledger.remember_history = lambda _row: None
    asyncio.run(runner.start(show_id, now=True))
    # The show's first song plays at once; the operator's Play next song is
    # kept and plays right after it, as with server audio.
    assert state.current_track.path == hits[0].path
    assert state.next_track is gold[7]
    assert _queue(source)[0] == hits[1].path


def test_stopping_leaves_a_listeners_request_for_the_same_song(tmp_path: Path) -> None:
    source, hits, gold = _station(tmp_path)
    state = source.player._state
    _playing(source, gold[0], time.time() - 30)
    runner, *_rest, show_id, _published = _runner(
        source, tmp_path, [entry.path for entry in hits[:4]]
    )
    asyncio.run(runner.start(show_id))
    # A listener asks for the show's second song: the library's own entry.
    state.queue.append(hits[1])
    assert _queue(source) == [hits[0].path, hits[1].path, hits[1].path]
    assert runner.is_show_entry(state.queue[1]) and not runner.is_show_entry(state.queue[2])
    asyncio.run(runner.stop())
    assert list(state.queue) == [hits[1]]
    assert state.queue[0] is hits[1]


def test_the_show_on_the_air_resumes_after_a_restart(tmp_path: Path) -> None:
    source, hits, gold = _station(tmp_path)
    state = source.player._state
    _playing(source, gold[0], time.time() - 30)
    songs = [entry.path for entry in hits[:5]]
    runner, bridge, store, show_id, published = _runner(source, tmp_path, songs)
    asyncio.run(runner.start(show_id))
    # The first song plays; the next is fed.
    state.queue.pop(0)
    _playing(source, hits[0], time.time())
    _settle(runner)
    saved = json.loads((tmp_path / "show_on_air.json").read_text(encoding="utf-8"))
    assert (saved["show_id"], saved["next_index"], saved["played"]) == (show_id, 1, 1)
    # A restart: the queue is gone, a new runner reads the saved place.
    state.queue.clear()
    again = ShowRunner(bridge, store, published, state_path=tmp_path / "show_on_air.json")
    # Said before the next song: it will resume, and from where.
    pending = again.status()
    assert (pending["pending_resume"], pending["resume_at"], pending["total"]) == (True, 2, 5)
    _settle(again)
    assert again.status()["played"] == 1
    assert _queue(source) == songs[1 : 1 + AHEAD]


def test_a_show_whose_time_has_passed_is_not_resumed(tmp_path: Path) -> None:
    source, hits, gold = _station(tmp_path)
    _playing(source, gold[0], time.time() - 30)
    runner, bridge, store, show_id, published = _runner(
        source, tmp_path, [entry.path for entry in hits[:3]]
    )
    asyncio.run(runner.start(show_id))
    source.player._state.queue.clear()
    later = time.time() + 3 * 3600
    again = ShowRunner(
        bridge, store, published, state_path=tmp_path / "show_on_air.json", clock=lambda: later
    )
    _settle(again)
    assert again.status() is None
    assert _queue(source) == []
    assert not (tmp_path / "show_on_air.json").exists()


def test_a_damaged_file_that_cannot_be_set_aside_is_never_written_over(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "shows.json"
    path.write_text("not json", encoding="utf-8")
    store = ShowStore(path)

    def refuse(_self: Path, _target: Path) -> None:
        raise PermissionError("in use")

    monkeypatch.setattr(Path, "replace", refuse)
    assert store.all() == {}
    with pytest.raises(OSError, match="could not be set aside"):
        store.add(_spec())
    assert path.read_text(encoding="utf-8") == "not json"


def test_the_preview_shows_a_scheduled_show_song_dmca_would_skip(tmp_path: Path) -> None:
    event = ScheduledEvent(
        id="a" * 32,
        name="Saturday throwback",
        action="show",
        params={"show": "c" * 32},
        repeat="daily",
        time=clock_time(12, 30),
    )
    source, _hits, gold = _station(tmp_path, events=[event], dmca=True)
    _playing(source, gold[0], NOW - 50)
    # Five songs by one artist: past three in a row they break the artist limit.
    same = [song(700 + n, "One Artist", album=f"Album {n}", length=180.0) for n in range(5)]
    source.library._music = (*source.library._music, *same)
    source.library._by_path.update({entry.path: entry for entry in same})
    shows = PreviewShows(scheduled={"c" * 32: ("Remembering the 2000s", tuple(same))})
    from dataclasses import replace

    view = build_preview(replace(source, shows=shows), 90, now=NOW)
    rows = [item for item in view["items"] if item["status"] in {"show", "skipped"}]
    assert [item["status"] for item in rows] == ["show"] * 3 + ["skipped"] * 2
    assert rows[-1]["status_text"] == (
        "Skipped from show Remembering the 2000s: it would break a DMCA limit"
    )
    assert rows[-1]["seconds"] == 0


def test_a_restart_resumes_at_the_earliest_waiting_song_past_skips(tmp_path: Path) -> None:
    """Songs skipped after a waiting one never make a restart lose it."""
    source, hits, gold = _station(tmp_path)
    state = source.player._state
    _playing(source, gold[0], time.time() - 30)
    songs = [hits[0].path, hits[1].path, "/gone/x.flac", "/gone/y.flac", hits[2].path]
    runner, bridge, store, show_id, published = _runner(source, tmp_path, songs)
    asyncio.run(runner.start(show_id))
    state.queue.pop(0)
    _playing(source, hits[0], time.time())
    _settle(runner)
    # Waiting: hits[1] (place 1) and hits[2] (place 4); places 2 and 3 skipped.
    assert _queue(source) == [hits[1].path, hits[2].path]
    saved = json.loads((tmp_path / "show_on_air.json").read_text(encoding="utf-8"))
    assert saved["next_index"] == 1
    state.queue.clear()
    again = ShowRunner(bridge, store, published, state_path=tmp_path / "show_on_air.json")
    _settle(again)
    assert _queue(source) == [hits[1].path, hits[2].path]


def test_stop_cancels_a_pending_resume_and_never_resumes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source, hits, gold = _station(tmp_path)
    _playing(source, gold[0], time.time() - 30)
    runner, bridge, store, show_id, published = _runner(
        source, tmp_path, [entry.path for entry in hits[:4]]
    )
    asyncio.run(runner.start(show_id))
    source.player._state.queue.clear()
    state_file = tmp_path / "show_on_air.json"
    again = ShowRunner(bridge, store, published, state_path=state_file)
    assert again.status()["pending_resume"] is True
    # The file is held open: Stop leaves a stopped marker instead.
    original = Path.unlink

    def held(self: Path, missing_ok: bool = False) -> None:
        if self == state_file:
            raise PermissionError("in use")
        original(self, missing_ok=missing_ok)

    monkeypatch.setattr(Path, "unlink", held)
    monkeypatch.setattr(time, "sleep", lambda _s: None)
    assert asyncio.run(again.stop()) == {"name": "Remembering the 2000s", "done": True}
    assert again.status() is None
    assert json.loads(state_file.read_text(encoding="utf-8"))["stopped"] is True
    third = ShowRunner(bridge, store, published, state_path=state_file)
    assert third.status() is None
    _settle(third)
    assert _queue(source) == []
    # Once the file can go, the next song change removes it.
    monkeypatch.setattr(Path, "unlink", original)
    _settle(again)
    assert not state_file.exists()


def test_a_stop_that_cannot_take_the_songs_out_keeps_trying(tmp_path: Path) -> None:
    """ADR-0009-06: a show asked to stop is no longer the show on the air."""
    source, hits, gold = _station(tmp_path)
    _playing(source, gold[0], time.time() - 30)
    runner, bridge, _store, show_id, _published = _runner(
        source, tmp_path, [entry.path for entry in hits[:4]]
    )
    asyncio.run(runner.start(show_id))
    assert runner.on_air_show() == {"id": show_id, "name": "Remembering the 2000s"}
    real = bridge.queue_remove_if
    bridge.queue_remove_if = lambda _index, _view: False  # the queue keeps changing
    assert asyncio.run(runner.stop()) == {"name": "Remembering the 2000s", "done": False}
    status = runner.status()
    assert (status["stopping"], status["waiting"]) == (True, AHEAD)
    assert runner.on_air_name() is None
    assert runner.on_air_show() is None
    # Nothing more is fed while it stops; the next song change takes them out.
    bridge.queue_remove_if = real
    _settle(runner)
    assert runner.status() is None
    assert _queue(source) == []


def test_queue_edits_by_path_never_take_the_shows_own_copy(tmp_path: Path) -> None:
    source, hits, gold = _station(tmp_path)
    state = source.player._state
    _playing(source, gold[0], time.time() - 30)
    runner, bridge, _store, show_id, _published = _runner(
        source, tmp_path, [entry.path for entry in hits[:3]]
    )
    bridge.show_entry = runner.is_show_entry
    asyncio.run(runner.start(show_id))
    show_copy = state.queue[0]
    state.queue.append(hits[0])  # a listener's request of the same file
    # A remove by path alone takes the request, not the show's copy.
    assert bridge.queue_remove(hits[0].path)
    assert state.queue[0] is show_copy
    assert not any(entry is hits[0] for entry in state.queue)
    # A remove at the place the page showed takes that very entry.
    state.queue.append(hits[0])
    assert bridge.queue_remove(hits[0].path, position=2)
    assert [e is show_copy for e in state.queue] == [True, False]
    # A reorder that drops one of two entries of a file keeps the show's copy.
    state.queue.append(hits[0])
    assert bridge.queue_reorder([hits[0].path, hits[1].path])
    assert state.queue[0] is show_copy
    # With the page's view and order, the very entries move.
    state.queue.append(hits[0])
    shown = [e.path for e in state.queue]
    assert bridge.queue_reorder(
        [hits[0].path, hits[1].path, hits[0].path], from_paths=shown, order=[2, 1, 0]
    )
    assert state.queue[0] is hits[0] and state.queue[2] is show_copy
