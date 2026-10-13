"""Listener requests: limits, tokens, ids, blocklists, placement and the book (#49)."""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from autodj._bridge import RequestSlot
from autodj.config import RequestsConfig, load_config
from autodj.indexer import IndexEntry
from autodj.listener_requests import (
    EXPIRED,
    KEEP_DECIDED,
    PENDING,
    QUEUED,
    AddressLimiter,
    Blocklist,
    FormTokens,
    RequestBook,
    RequestDesk,
    TrackIds,
    clean_name,
    fit,
    place,
    rough_wait,
)


def _entry(n: int, **changes: Any) -> IndexEntry:
    base = IndexEntry(
        path=f"/music/rock/song_{n}.flac",
        title=f"Song {n}",
        artist=f"Artist {n}",
        album=f"Album {n}",
        genre="Rock",
        bpm=120.0,
        year=2000,
        length=180.0,
        energy=0.1,
        key=0,
        mode=1,
        tempo_confidence=0.8,
    )
    return replace(base, **changes)


class Clock:
    def __init__(self, now: float = 1_800_000_000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------


def test_requests_are_off_by_default_with_safe_limits() -> None:
    cfg = RequestsConfig()
    assert cfg.enabled is False
    assert cfg.approval == "manual"
    assert (cfg.per_listener, cfg.per_listener_minutes, cfg.daily_limit) == (3, 60, 50)


@pytest.mark.parametrize(
    ("values", "error", "match"),
    [
        ({"enabled": "yes"}, TypeError, "enabled"),
        ({"approval": "sometimes"}, ValueError, "manual or auto"),
        ({"per_listener": 0}, ValueError, "per_listener must be between"),
        ({"daily_limit": True}, TypeError, "daily_limit must be an integer"),
        ({"window_minutes": 1}, ValueError, "window_minutes"),
        ({"blocked_artists": ["ok", ""]}, TypeError, "blocked_artists"),
        ({"blocked_folders": "Xmas"}, TypeError, "blocked_folders"),
        ({"blocked_artists": ["a"] * 501}, ValueError, "at most 500"),
    ],
)
def test_bad_request_settings_are_refused(values: dict, error: type, match: str) -> None:
    with pytest.raises(error, match=match):
        RequestsConfig(**values)


def test_each_station_has_its_own_request_settings(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    path.write_text(
        """
[library]
music_dir = "music"

[requests]
enabled = true
approval = "auto"

[stations.nightowl.stream]
station_name = "Night Owl Radio"

[stations.nightowl.recording]
folder = "recordings/nightowl"

[stations.nightowl.requests]
enabled = true
blocked_artists = ["Loud Band"]
""",
        encoding="utf-8",
    )
    cfg = load_config(path, environ={})
    assert (cfg.requests.enabled, cfg.requests.approval) == (True, "auto")
    (owl,) = cfg.stations
    assert owl.cfg.requests.approval == "manual"
    assert owl.cfg.requests.blocked_artists == ["Loud Band"]


# ---------------------------------------------------------------------------
# Limits, tokens and ids
# ---------------------------------------------------------------------------


def test_the_address_limiter_counts_per_address_and_forgets_old_events() -> None:
    clock = Clock(0.0)
    limiter = AddressLimiter(2, 60.0, clock=clock)
    assert limiter.hit("192.0.2.1") is None
    assert limiter.hit("192.0.2.1") is None
    assert limiter.retry_after("192.0.2.1") == 60
    assert limiter.hit("192.0.2.1") == 60
    assert limiter.hit("192.0.2.2") is None  # another address has its own count
    clock.now = 45.0
    assert limiter.retry_after("192.0.2.1") == 15
    clock.now = 60.0
    assert limiter.retry_after("192.0.2.1") is None
    assert limiter.hit("192.0.2.1") is None


def test_the_address_limiter_keeps_a_bounded_number_of_addresses() -> None:
    limiter = AddressLimiter(1, 60.0, clock=Clock(0.0), max_keys=3)
    for number in range(10):
        limiter.hit(f"192.0.2.{number}")
    assert len(limiter._events) == 3


def test_form_tokens_are_signed_per_station_and_expire() -> None:
    clock = Clock()
    tokens = FormTokens(b"k" * 32, clock=clock)
    token = tokens.issue("default")
    assert tokens.valid(token, "default")
    assert not tokens.valid(token, "nightowl")
    assert not FormTokens(b"other-key" * 4, clock=clock).valid(token, "default")
    issued, _, mac = token.partition(".")
    assert not tokens.valid(f"{int(issued) + 1}.{mac}", "default")
    for bad in ("", "x", "1.2", f"{issued}.{'0' * 32}", f"-1.{mac}", f"{issued}{mac}"):
        assert not tokens.valid(bad, "default"), bad
    clock.now += 2 * 3600 + 1
    assert not tokens.valid(token, "default")


def test_track_ids_reveal_nothing_and_find_only_known_songs() -> None:
    ids = TrackIds(b"s" * 32)
    entries = [_entry(1), _entry(2)]
    indexed = [(entry, entry.title.lower()) for entry in entries]
    one = ids.id_for(entries[0].path)
    assert len(one) == 24 and "song" not in one
    assert ids.lookup(indexed, one) is entries[0]
    assert ids.lookup(indexed, "0" * 24) is None
    assert ids.lookup(indexed, "../../etc/passwd") is None
    assert TrackIds(b"t" * 32).id_for(entries[0].path) != one


def test_a_listener_name_is_trimmed_and_checked() -> None:
    assert clean_name("  Sam   from  Leeds ") == "Sam from Leeds"
    assert clean_name("") == ""
    assert clean_name("x" * 41) is None
    assert clean_name("bad\x07bell") is None


# ---------------------------------------------------------------------------
# What may be requested
# ---------------------------------------------------------------------------


def test_the_blocklist_holds_artists_and_folders(tmp_path: Path) -> None:
    music = tmp_path / "music"
    block = Blocklist(["The Loud Band"], ["Christmas", str(tmp_path / "private")], music)
    assert block.blocks(_entry(1, artist="Loud Band"))
    assert block.blocks(_entry(1, artist="Someone", title="Hit (feat. The Loud Band)"))
    assert block.blocks(_entry(1, path=str(music / "Christmas" / "a.flac")))
    assert block.blocks(_entry(1, path=str(tmp_path / "private" / "deep" / "b.flac")))
    assert not block.blocks(_entry(1, path=str(music / "Christmas songs" / "c.flac")))
    assert not block.blocks(_entry(1, path=str(music / "rock" / "d.flac")))
    assert not Blocklist([], [], music).blocks(_entry(1))


# ---------------------------------------------------------------------------
# Where a request fits
# ---------------------------------------------------------------------------


def _slot(
    position: int, before: IndexEntry | None, after: IndexEntry | None = None, *problems: str
) -> RequestSlot:
    return RequestSlot(position, 200.0 * position, before, after, tuple(problems))


def test_fit_counts_sound_tempo_key_and_energy() -> None:
    a = _entry(1, bpm=120.0, key=0, mode=1)
    same = fit(a, _entry(2, bpm=121.0, key=0, mode=1), lambda *_: 0.9)
    assert same.words == ("similar sound", "1 BPM apart", "same key")
    double = fit(a, _entry(2, bpm=240.0, key=7, mode=1), lambda *_: None)
    assert double.words == ("same tempo", "compatible key")
    clash = fit(a, _entry(2, bpm=90.0, key=6, mode=0, energy=0.9), lambda *_: 0.1)
    assert clash.words == ("30 BPM apart", "key change")
    assert clash.cost > double.cost > same.cost
    unknown = fit(_entry(1, bpm=0.0, key=-1, energy=0.0), a, lambda *_: None)
    assert unknown.words == ()
    assert unknown.cost == pytest.approx(2.0)


def test_place_picks_the_smoothest_place_that_keeps_the_rules() -> None:
    playing = _entry(1, bpm=120.0, key=0, mode=1)
    rough = _entry(2, bpm=80.0, key=6, mode=0)
    smooth = _entry(3, bpm=121.0, key=0, mode=1)
    request = _entry(4, bpm=122.0, key=0, mode=1)
    slots = [_slot(0, playing, rough), _slot(1, rough, smooth), _slot(2, smooth)]
    chosen = place(request, slots, lambda *_: None)
    assert chosen.slot is slots[2]
    assert chosen.note == "Fits after Artist 3 — Song 3: 1 BPM apart, same key."
    # A slot that breaks a rule never counts, however well it fits.
    blocked = [_slot(0, playing), _slot(1, smooth, None, "same artist")]
    assert place(request, blocked, lambda *_: None).slot is blocked[0]


def test_place_says_why_when_nothing_fits() -> None:
    slots = [_slot(0, _entry(1), None, "same artist"), _slot(1, _entry(2), None, "same album")]
    none = place(_entry(4), slots, lambda *_: None)
    assert none.slot is None
    assert none.problems == ("same artist",)
    assert none.note == "No place in the window keeps the rules: same artist."
    assert place(_entry(4), [_slot(0, None)], lambda *_: None).note == "Plays first."


def test_listeners_hear_only_a_rough_window() -> None:
    assert rough_wait(0) == "within the next half hour"
    assert rough_wait(45 * 60) == "within the next hour"
    assert rough_wait(3 * 3600) == "within the next few hours"


# ---------------------------------------------------------------------------
# The book
# ---------------------------------------------------------------------------


def test_the_book_saves_counts_the_day_and_expires(tmp_path: Path) -> None:
    clock = Clock()
    path = tmp_path / "listener_requests.json"
    book = RequestBook(path, timezone="UTC", clock=clock)
    first = book.add(_entry(1), "Sam", PENDING, "Fits after X.")
    book.add(_entry(2), "", QUEUED, "Plays first.")
    assert book.count_today() == 2
    saved = json.loads(path.read_text(encoding="utf-8"))
    assert saved["version"] == 1 and saved["today"] == 2
    again = RequestBook(path, timezone="UTC", clock=clock)
    assert [r.id for r in again.pending()] == [first.id]
    assert again.get(first.id).name == "Sam"
    # The public view has no path.
    assert "path" not in first.public()
    clock.now += 86400
    assert again.count_today() == 0
    assert again.tidy()
    assert again.get(first.id).status == EXPIRED
    assert not again.tidy()


def test_the_book_keeps_only_the_latest_decisions(tmp_path: Path) -> None:
    clock = Clock()
    book = RequestBook(tmp_path / "r.json", clock=clock)
    for n in range(KEEP_DECIDED + 5):
        clock.now += 1
        book.add(_entry(n), "", QUEUED, "")
    assert book.tidy()
    assert len(book.recent()) == KEEP_DECIDED
    assert book.recent()[0].title == f"Song {KEEP_DECIDED + 4}"


@pytest.mark.parametrize(
    "content", ["not json", '{"version": 9}', '{"version": 1, "requests": [{"x": 1}]}']
)
def test_a_damaged_book_is_set_aside(tmp_path: Path, content: str) -> None:
    path = tmp_path / "listener_requests.json"
    path.write_text(content, encoding="utf-8")
    book = RequestBook(path)
    assert book.pending() == []
    assert len(list(tmp_path.glob("listener_requests.json.*.bad"))) == 1


# ---------------------------------------------------------------------------
# The desk, with a stand-in bridge
# ---------------------------------------------------------------------------


class FakeBridge:
    def __init__(self, entries: list[IndexEntry]) -> None:
        self.entries = entries
        self.indexed = [(e, f"{e.title} — {e.artist} — {e.album}".lower()) for e in entries]
        self.queue: list[str] = []
        self.slots: list[RequestSlot] | None = None
        self.dmca: tuple[str, ...] = ()
        self.playing = entries[0]
        self.sim = SimpleNamespace(similarity=lambda *_: None)

    def search_index(self) -> list[tuple[IndexEntry, str]]:
        return self.indexed

    def entry_for_path(self, path: str) -> IndexEntry | None:
        return next((e for e in self.entries if e.path == path), None)

    def request_slots(self, entry: IndexEntry, horizon_s: float) -> list[RequestSlot]:
        return self.slots if self.slots is not None else [_slot(0, self.playing)]

    def coming_up(self) -> list[IndexEntry]:
        return [self.playing, *(self.entry_for_path(p) for p in self.queue)]

    def queue_insert(self, path: str, position: int) -> bool:
        self.queue.insert(position, path)
        return True

    def dmca_queue_limits(self, entry: IndexEntry, *, now: bool, append: bool) -> tuple[str, ...]:
        return self.dmca


def _desk(tmp_path: Path, **settings: Any) -> tuple[RequestDesk, FakeBridge, TrackIds, Clock]:
    entries = [_entry(n) for n in range(6)]
    bridge = FakeBridge(entries)
    ids = TrackIds(b"i" * 32)
    clock = Clock()
    refreshed: list[bool] = []
    desk = RequestDesk(
        "default",
        RequestsConfig(enabled=True, **settings),
        bridge,  # type: ignore[arg-type]
        RequestBook(tmp_path / "requests.json", clock=clock),
        music_dir=tmp_path / "music",
        tokens=FormTokens(b"f" * 32, clock=clock),
        track_ids=ids,
        monotonic=clock,
        on_queued=lambda: refreshed.append(True),
    )
    return desk, bridge, ids, clock


def test_search_finds_requestable_songs_by_every_word(tmp_path: Path) -> None:
    desk, bridge, ids, _ = _desk(tmp_path, blocked_artists=["Artist 2"])
    found = desk.search("song")
    assert [entry.title for _, entry in found] == ["Song 0", "Song 1", "Song 3", "Song 4", "Song 5"]
    assert found[0][0] == ids.id_for(bridge.entries[0].path)
    assert [e.title for _, e in desk.search("artist 4 album")] == ["Song 4"]
    assert desk.song(ids.id_for(bridge.entries[2].path)) is None  # blocked
    assert desk.song(ids.id_for(bridge.entries[3].path)) is bridge.entries[3]


def test_a_manual_request_waits_and_the_operator_decides(tmp_path: Path) -> None:
    desk, bridge, ids, _ = _desk(tmp_path)
    song = ids.id_for(bridge.entries[3].path)
    outcome = desk.submit(song, "Sam", "192.0.2.1")
    assert (outcome.status, outcome.headline) == (200, "Request received")
    assert "Thanks, Sam" in outcome.detail and "within the next hour or so" in outcome.detail
    assert bridge.queue == []
    (waiting,) = desk.snapshot()["pending"]
    assert waiting["title"] == "Song 3" and waiting["can_approve"] is True
    assert waiting["fit"].startswith("Fits after Artist 0 — Song 0")
    # The same song again is already on the way.
    assert desk.submit(song, "", "192.0.2.2").headline == "Already on the way"
    desk.approve(waiting["id"])
    assert bridge.queue == [bridge.entries[3].path]
    assert desk.snapshot()["pending"] == []
    assert desk.snapshot()["recent"][0]["status"] == QUEUED
    with pytest.raises(KeyError):
        desk.approve(waiting["id"])
    with pytest.raises(KeyError):
        desk.reject("not-an-id")


def test_auto_approval_queues_at_the_best_place_at_once(tmp_path: Path) -> None:
    desk, bridge, ids, _ = _desk(tmp_path, approval="auto")
    bridge.slots = [_slot(0, bridge.playing, None, "same artist"), _slot(1, bridge.entries[1])]
    outcome = desk.submit(ids.id_for(bridge.entries[4].path), "", "192.0.2.1")
    assert outcome.headline == "Request accepted"
    assert outcome.detail == "Thanks. Song 4 by Artist 4 should play within the next half hour."
    assert bridge.queue == [bridge.entries[4].path]
    assert desk.book.recent()[0].note.startswith("Fits after Artist 1 — Song 1")


def test_rules_limits_and_caps_turn_requests_away(tmp_path: Path) -> None:
    desk, bridge, ids, clock = _desk(tmp_path, per_listener=1, daily_limit=2, max_pending=5)
    song = lambda n: ids.id_for(bridge.entries[n].path)  # noqa: E731
    assert desk.submit("f" * 24, "", "192.0.2.1").status == 404
    bridge.slots = [_slot(0, bridge.playing, None, "same song")]
    refused = desk.submit(song(1), "", "192.0.2.1")
    assert (refused.status, refused.headline) == (409, "Not right now")
    bridge.slots = None
    # A refusal does not use up the listener's allowance.
    assert desk.submit(song(1), "", "192.0.2.1").status == 200
    limited = desk.submit(song(2), "", "192.0.2.1")
    assert (limited.status, limited.retry_after) == (429, 3600)
    assert "up to 1 song every hour" in limited.detail
    assert desk.submit(song(2), "", "192.0.2.2").status == 200
    capped = desk.submit(song(3), "", "192.0.2.3")
    assert capped.headline == "No more requests today"
    clock.now += 86400
    # The song playing now is on the way already.
    assert desk.submit(song(0), "", "192.0.2.4").headline == "Already on the way"


def test_waiting_requests_are_capped(tmp_path: Path) -> None:
    desk, bridge, ids, _ = _desk(tmp_path, max_pending=1)
    assert desk.submit(ids.id_for(bridge.entries[1].path), "", "192.0.2.1").status == 200
    full = desk.submit(ids.id_for(bridge.entries[2].path), "", "192.0.2.2")
    assert full.headline == "Too many requests waiting"


def test_the_operator_can_play_next_or_turn_down(tmp_path: Path) -> None:
    desk, bridge, ids, _ = _desk(tmp_path)
    for n, address in ((1, "a"), (2, "b"), (3, "c")):
        desk.submit(ids.id_for(bridge.entries[n].path), "", address)
    first, second, third = (r.id for r in desk.book.pending())
    bridge.queue = [bridge.entries[5].path]
    desk.play_next(first)
    assert bridge.queue[0] == bridge.entries[1].path
    desk.reject(second)
    assert desk.book.get(second).note == "Turned down by the operator."
    bridge.dmca = ("more than 3 songs from one album in 3 hours",)
    with pytest.raises(ValueError, match="DMCA mode"):
        desk.play_next(third)
    bridge.slots = [_slot(0, bridge.playing, None, "same album")]
    with pytest.raises(ValueError, match="cannot be queued within the next 30 minutes: same album"):
        desk.approve(third)
    assert desk.snapshot()["pending"][0]["can_approve"] is False
    bridge.entries.remove(bridge.entries[3])
    with pytest.raises(LookupError):
        desk.approve(third)
    assert desk.snapshot()["pending"][0]["fit"] == "The song is no longer in the library."
