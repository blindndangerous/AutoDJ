"""Play dates for voice liners and carts: stored, described, and skipped outside them."""

from __future__ import annotations

import contextlib
import json
import random
from datetime import date
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock
from zoneinfo import ZoneInfo

import numpy as np
import pytest
import soundfile as sf

from autodj.clip_dates import (
    ClipDates,
    ClipDatesError,
    LinerDates,
    LinerGate,
    parse_dates,
    station_today,
)
from autodj.clockwheel import ClockSlot
from autodj.liner_scheduler import LinerScheduler
from autodj.liners import LinerLibrary
from tests.unit.test_dmca import _player, _with_library
from tests.unit.test_liner_scheduler import _cfg

CHRISTMAS = ClipDates(not_before=date(2026, 12, 1), not_after=date(2026, 12, 26))


def test_dates_are_both_included() -> None:
    assert CHRISTMAS.refusal(date(2026, 12, 1)) is None
    assert CHRISTMAS.refusal(date(2026, 12, 26)) is None
    assert CHRISTMAS.refusal(date(2026, 12, 27)) == "expired after 26 December 2026"
    assert CHRISTMAS.refusal(date(2026, 11, 30)) == "not playable before 1 December 2026"
    assert ClipDates().refusal(date(1999, 1, 1)) is None


def test_dates_in_words() -> None:
    assert CHRISTMAS.describe() == "Plays from 1 December 2026 until 26 December 2026"
    assert ClipDates(not_after=date(2026, 12, 26)).describe() == "Plays until 26 December 2026"
    assert ClipDates(not_before=date(2026, 12, 1)).describe() == "Plays from 1 December 2026"
    assert ClipDates().describe() == ""
    assert CHRISTMAS.view(date(2026, 12, 27)) == {
        "not_before": "2026-12-01",
        "not_after": "2026-12-26",
        "dates_text": "Plays from 1 December 2026 until 26 December 2026",
        "playable": False,
        "unplayable_text": "expired after 26 December 2026",
    }


@pytest.mark.parametrize(
    ("body", "field", "message"),
    [
        (
            {"not_before": "2026-12-26", "not_after": "2026-12-01"},
            "not_after",
            "Do not play after must be on or after the Not before date",
        ),
        ({"not_after": "26/12/2026"}, "not_after", None),
        ({"until": "2026-12-26"}, "until", "Unknown field until"),
    ],
)
def test_bad_dates_name_the_field(body: dict, field: str, message: str | None) -> None:
    with pytest.raises(ClipDatesError) as caught:
        parse_dates(body)
    assert caught.value.field == field
    if message is not None:
        assert caught.value.message == message
    else:
        assert caught.value.message.startswith("Do not play after: ")


def test_station_today_reads_the_station_time_zone() -> None:
    cfg = SimpleNamespace(server=SimpleNamespace(station_timezone="Pacific/Kiritimati"))
    from datetime import datetime

    assert station_today(cfg) == datetime.now(ZoneInfo("Pacific/Kiritimati")).date()


def test_liner_dates_are_saved_shared_and_removed(tmp_path: Path) -> None:
    path = tmp_path / "liner_dates.json"
    first, second = LinerDates(path), LinerDates(path)
    assert first.all() == {}
    first.set("promo.mp3", CHRISTMAS)
    # Another instance on the same file sees the change.
    assert second.get("promo.mp3") == CHRISTMAS
    assert second.refusal("promo.mp3", date(2027, 1, 1)) == "expired after 26 December 2026"
    assert second.refusal("other.mp3", date(2027, 1, 1)) is None
    assert json.loads(path.read_text(encoding="utf-8"))["liners"]["promo.mp3"] == {
        "not_before": "2026-12-01",
        "not_after": "2026-12-26",
    }
    first.set("promo.mp3", ClipDates())
    assert second.all() == {}


def test_saves_at_once_never_lose_each_other(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two saves at once (two requests, or a save and a delete) both land."""
    import threading

    store = LinerDates(tmp_path / "liner_dates.json")
    both_read = threading.Barrier(2, timeout=0.5)
    real_empty = ClipDates.empty

    def empty_after_both_read(dates: ClipDates) -> bool:
        # Without one lock over read, change and write, both saves have read
        # the empty file here, and the second write drops the first save.
        with contextlib.suppress(threading.BrokenBarrierError):
            both_read.wait()
        return real_empty.fget(dates)

    monkeypatch.setattr(ClipDates, "empty", property(empty_after_both_read))
    saves = [
        threading.Thread(target=store.set, args=(name, CHRISTMAS))
        for name in ("one.mp3", "two.mp3")
    ]
    for save in saves:
        save.start()
    for save in saves:
        save.join()
    assert set(LinerDates(store.path).all()) == {"one.mp3", "two.mp3"}


def test_a_replaced_file_of_the_same_size_and_time_is_read_again(tmp_path: Path) -> None:
    import os

    path = tmp_path / "liner_dates.json"
    store = LinerDates(path)
    store.set("a.mp3", CHRISTMAS)
    stamp = path.stat()
    # A restore writes a new file of the same size in the same clock tick.
    other = ClipDates(not_before=date(2026, 12, 2), not_after=date(2026, 12, 26))
    replacement = tmp_path / "new.json"
    replacement.write_bytes(path.read_bytes().replace(b"2026-12-01", b"2026-12-02"))
    os.utime(replacement, ns=(stamp.st_atime_ns, stamp.st_mtime_ns))
    os.replace(replacement, path)
    assert path.stat().st_size == stamp.st_size
    assert store.get("a.mp3") == other


def test_a_bad_liner_dates_file_is_set_aside(tmp_path: Path) -> None:
    path = tmp_path / "liner_dates.json"
    path.write_text('{"schema_version": 1, "liners": {"a": {"when": 1}}}', encoding="utf-8")
    assert LinerDates(path).all() == {}
    assert len(list(tmp_path.glob("liner_dates.json.bad-*"))) == 1
    assert not path.exists()


def test_liner_gate_passes_every_liner_when_none_has_dates(tmp_path: Path) -> None:
    entries = (SimpleNamespace(path="/l/a.wav"), SimpleNamespace(path="/l/b.wav"))
    gate = LinerGate(LinerDates(tmp_path / "d.json"), lambda: date(2027, 1, 1))
    assert gate.playable(entries) is entries
    gate.store.set("a.wav", CHRISTMAS)
    assert gate.playable(entries) == (entries[1],)
    assert gate.refusal("a.wav") == "expired after 26 December 2026"


# -- liner picking ------------------------------------------------------------


def _folder(tmp_path: Path) -> Path:
    for name in ("a.wav", "b.wav", "c.wav"):
        sf.write(tmp_path / name, np.zeros(441, np.float32), 44100)
    return tmp_path


def test_library_picks_pass_over_refused_clips(tmp_path: Path) -> None:
    library = LinerLibrary.from_folder(_folder(tmp_path))
    allowed = lambda path: path.name != "b.wav"  # noqa: E731
    picks = [library.pick("sequential", allowed=allowed).name for _ in range(4)]
    assert picks == ["a.wav", "c.wav", "a.wav", "c.wav"]
    rng = random.Random(3)
    assert {library.pick("random", rng=rng, allowed=allowed).name for _ in range(20)} == {
        "a.wav",
        "c.wav",
    }
    assert library.pick("sequential", allowed=lambda _path: False) is None
    assert library.pick("random", allowed=lambda _path: False) is None


def test_automatic_liners_pass_over_expired_ones_but_a_named_one_is_told(tmp_path: Path) -> None:
    bus = MagicMock()
    expired = {"b.wav": "expired after 26 December 2026"}
    scheduler = LinerScheduler(
        _cfg(), _folder(tmp_path), bus, clock=lambda: 0.0, play_dates=expired.get
    )
    assert [scheduler.fire() for _ in range(4)] == ["a.wav", "c.wav", "a.wav", "c.wav"]
    assert scheduler.refusal("b.wav") == "expired after 26 December 2026"
    assert scheduler.refusal("a.wav") is None


def test_registered_liners_pass_over_expired_ones(tmp_path: Path) -> None:
    first, second = tmp_path / "one.wav", tmp_path / "two.wav"
    scheduler = LinerScheduler(
        _cfg(liners_pick_mode="random"),
        tmp_path / "flat",
        MagicMock(),
        registered_liners=lambda: [("rec-1", first), ("rec-2", second)],
        path_decoder=lambda _path: np.zeros((100, 2), np.float32),
        play_dates={"rec-1": "expired after 26 December 2026"}.get,
    )
    assert {scheduler.fire() for _ in range(10)} == {"rec-2"}
    scheduler.play_dates = lambda _name: "expired after 26 December 2026"
    assert scheduler.fire() is None


def test_unreadable_dates_never_stop_the_liners(tmp_path: Path) -> None:
    def broken(_name: str) -> str | None:
        raise OSError("disk gone")

    scheduler = LinerScheduler(
        _cfg(), _folder(tmp_path), MagicMock(), clock=lambda: 0.0, play_dates=broken
    )
    assert scheduler.fire() == "a.wav"


def test_clockwheel_liner_steps_pass_over_expired_liners(tmp_path: Path) -> None:
    player = _with_library(_player("clock", dmca=False))
    liners = tuple(
        SimpleNamespace(path=f"Z:/Liners/{name}.wav", is_silent=False, length=5.0)
        for name in ("Old promo", "Station ID")
    )
    music = player._sim.entries_snapshot()
    player._library.entries_snapshot = lambda kind="music": liners if kind == "liner" else music
    player._clock = {"cursor": 0}
    player._clock_slots = [
        ClockSlot(kind="liner", folder="Z:/Liners", selection="random"),
        ClockSlot(folder="Z:/Music", recursive=True, selection="random"),
    ]
    gate = LinerGate(LinerDates(tmp_path / "d.json"), lambda: date(2027, 1, 1))
    gate.store.set("Old promo.wav", CHRISTMAS)
    player.liner_filter = gate.playable
    for _ in range(8):
        picked = player._clock_pick(player._pick_context(), 0)
        assert picked.liner.path == "Z:/Liners/Station ID.wav"
    gate.store.set("Station ID.wav", CHRISTMAS)
    picked = player._clock_pick(player._pick_context(), 0)
    assert picked.liner is None
    assert picked.entry is not None  # the music still plays


def _registered(tmp_path: Path, mode: str = "sequential", **kwargs: object) -> LinerScheduler:
    paths = [tmp_path / f"{name}.wav" for name in ("one", "two", "three", "four")]
    return LinerScheduler(
        _cfg(liners_pick_mode=mode, **kwargs),
        tmp_path / "flat",
        MagicMock(),
        registered_liners=lambda: [(path.stem, path) for path in paths],
        path_decoder=lambda _path: np.zeros((100, 2), np.float32),
    )


def test_sequential_rotation_keeps_its_place_when_a_registered_liner_expires(
    tmp_path: Path,
) -> None:
    scheduler = _registered(tmp_path)
    assert [scheduler.fire() for _ in range(2)] == ["one", "two"]
    # "two" expires after it played: "three" is still next, not "four".
    scheduler.play_dates = {"two": "expired after 26 December 2026"}.get
    assert [scheduler.fire() for _ in range(4)] == ["three", "four", "one", "three"]
    # It plays again in its own place when its dates allow it once more.
    scheduler.play_dates = lambda _name: None
    assert [scheduler.fire() for _ in range(3)] == ["four", "one", "two"]


def test_sequential_rotation_passes_over_the_expired_liner_that_would_be_next(
    tmp_path: Path,
) -> None:
    scheduler = _registered(tmp_path)
    scheduler.play_dates = {"one": "expired", "two": "expired"}.get
    assert [scheduler.fire() for _ in range(3)] == ["three", "four", "three"]


def test_an_undone_sequential_pick_gives_back_every_step_it_passed_over(tmp_path: Path) -> None:
    scheduler = _registered(tmp_path)
    scheduler.play_dates = {"one": "expired", "two": "expired"}.get
    with scheduler._work_lock:
        liner = scheduler._decode(None)
    assert liner is not None and liner.name == "three"
    assert liner.rotation == ("registered", 4, 3)
    with scheduler._lock:
        scheduler._undo_pick_locked(liner)
    assert scheduler._registered_cursor == 0
    (tmp_path / "f").mkdir()
    folder = LinerScheduler(
        _cfg(), _folder(tmp_path / "f"), MagicMock(), play_dates={"a.wav": "expired"}.get
    )
    with folder._work_lock:
        liner = folder._decode(None)
    assert liner is not None and (liner.name, liner.rotation) == ("b.wav", ("folder", 3, 2))
    with folder._lock:
        folder._undo_pick_locked(liner)
    assert folder._library.cursor == 0


@pytest.mark.parametrize("registered", [True, False])
def test_the_talk_up_planner_passes_over_expired_liners_and_keeps_its_place(
    tmp_path: Path, registered: bool
) -> None:
    cfg = {"liners_every_n_songs": 1, "liners_talk_up": True, "transition_mode": "saved_points"}
    if registered:
        scheduler = _registered(tmp_path, **cfg)
        scheduler._vocal_start = lambda _entry: 0.8
        names = ["one", "two", "three", "four"]
    else:
        scheduler = LinerScheduler(
            _cfg(**cfg), _folder(tmp_path), MagicMock(), vocal_start=lambda _entry: 0.8
        )
        names = ["a.wav", "b.wav", "c.wav"]
    scheduler.play_dates = {names[0]: "expired after 26 December 2026"}.get
    planned = scheduler.plan_for_handoff("h1", tracks_ahead=1, seconds_ahead=0)
    assert planned == names[1]
    assert scheduler.mark_planned_fired("h1")
    assert scheduler.plan_for_handoff("h2", tracks_ahead=1, seconds_ahead=0) == names[2]
    scheduler.play_dates = lambda _name: "expired"
    scheduler.release_plan("h2")
    assert scheduler.plan_for_handoff("h3", tracks_ahead=1, seconds_ahead=0) is None


# -- liners deleted outside AutoDJ -------------------------------------------------


def test_forget_removes_many_in_one_write_and_leaves_an_untouched_file(tmp_path: Path) -> None:
    store = LinerDates(tmp_path / "liner_dates.json")
    assert store.forget(["a.wav"]) == []
    assert not store.path.exists()  # nothing to forget: nothing written
    for name in ("a.wav", "b.wav", "c.wav"):
        store.set(name, CHRISTMAS)
    assert store.forget(["c.wav", "a.wav", "x.wav"]) == ["a.wav", "c.wav"]
    assert set(LinerDates(store.path).all()) == {"b.wav"}


def _station(tmp_path: Path, records: dict[str, dict], gone: set[str]) -> SimpleNamespace:
    folder = tmp_path / "liners"
    folder.mkdir(exist_ok=True)
    library = SimpleNamespace(
        catalog=SimpleNamespace(record_for_id=records.get),
        is_gone=lambda path: str(path) in gone,
    )
    return SimpleNamespace(
        slug="default",
        liner_gate=SimpleNamespace(store=LinerDates(tmp_path / "liner_dates.json")),
        bridge=SimpleNamespace(library=library),
        paths=SimpleNamespace(liners=folder),
    )


def test_a_scan_forgets_the_dates_of_liners_that_are_gone(tmp_path: Path, caplog) -> None:
    from autodj.stations import prune_liner_dates

    records = {
        "rec-gone": {"kind": "liner", "path": "Z:/Liners/old.wav"},
        "rec-away": {"kind": "liner", "path": "//share/liners/away.wav"},
        "rec-here": {"kind": "liner", "path": "Z:/Liners/here.wav"},
    }
    station = _station(tmp_path, records, gone={"Z:/Liners/old.wav"})
    (station.paths.liners / "kept.wav").write_bytes(b"x")
    store = station.liner_gate.store
    for liner_id in ("rec-gone", "rec-away", "rec-here", "kept.wav", "deleted.wav"):
        store.set(liner_id, CHRISTMAS)
    with caplog.at_level("INFO", logger="autodj.stations"):
        assert prune_liner_dates(station) == ["deleted.wav", "rec-gone"]
    assert set(store.all()) == {"rec-away", "rec-here", "kept.wav"}
    assert "forgot the play dates of 2 liners that are gone" in caplog.text
    assert prune_liner_dates(station) == []


def test_a_missing_liner_folder_forgets_nothing(tmp_path: Path) -> None:
    from autodj.stations import prune_liner_dates

    station = _station(tmp_path, {}, gone=set())
    station.liner_gate.store.set("promo.wav", CHRISTMAS)
    station.paths.liners.rmdir()  # an unmounted share, say
    assert prune_liner_dates(station) == []
    assert set(station.liner_gate.store.all()) == {"promo.wav"}
