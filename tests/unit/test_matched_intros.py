"""Matched intros (#127): the store, the choice at a song change, and the mix.

The render tests use the small breaks station (``tests/unit/_break_station.py``):
songs are a constant level on the left channel and a voice is a constant level
on the right channel, so a render shows where each one is.
"""

from __future__ import annotations

import json
import random
from datetime import date
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import numpy as np
import pytest

from autodj.breaks import new_id
from autodj.matched_intros import (
    MAX_INTROS,
    Chosen,
    Intro,
    IntroError,
    MatchedIntros,
    file_name,
    parse_rules,
    rules_text,
    wait_words,
)
from autodj.stereo import SAMPLE_RATE as SR
from tests.unit._break_station import make_break, make_station, write_voice

NOW = 1_800_000_000.0


def _store(tmp_path: Path, **kwargs) -> MatchedIntros:
    return MatchedIntros(
        tmp_path / "matched_intros.json", tmp_path / "intros", rng=random.Random(1), **kwargs
    )


def _add(
    store: MatchedIntros,
    *,
    track_id: str | None = "song-a",
    artist: str | None = None,
    seconds: float = 2.0,
    **fields,
) -> str:
    """Add an intro with a real WAV in the store's folder; its ID."""
    intro_id = new_id()
    store.folder.mkdir(parents=True, exist_ok=True)
    path = write_voice(store.folder / file_name(intro_id, ".wav"), seconds)
    store.add(
        intro_id,
        Intro(file=path.name, track_id=track_id, artist=artist, speech_s=seconds, **fields),
    )
    return intro_id


# ---------------------------------------------------------------------------
# The file
# ---------------------------------------------------------------------------


def test_intros_are_saved_by_id_and_read_back_by_another_store(tmp_path: Path) -> None:
    store = _store(tmp_path)
    intro_id = _add(store, label="New single", chance=50, cooldown_minutes=30)
    saved = json.loads(store.path.read_text(encoding="utf-8"))
    assert saved["schema_version"] == 1
    assert saved["intros"][intro_id]["label"] == "New single"
    # Unset fields stay out, so the file stays small and readable.
    assert "artist" not in saved["intros"][intro_id]
    other = _store(tmp_path)
    assert other.all()[intro_id].chance == 50


def test_an_intro_is_for_one_song_or_one_artist(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="one song or one artist"):
        Intro(file=file_name("a" * 32, ".wav"), track_id="x", artist="Someone")
    with pytest.raises(ValueError, match="one song or one artist"):
        Intro(file=file_name("a" * 32, ".wav"))
    with pytest.raises(ValueError, match="intro-<id>"):
        Intro(file="../escape.wav", track_id="x")


def test_a_file_that_is_not_valid_is_set_aside(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.path.write_text('{"schema_version": 1, "intros": {"nope": {}}}', encoding="utf-8")
    assert store.all() == {}
    assert not store.path.exists()
    assert [path.name.startswith("matched_intros.json.bad-") for path in tmp_path.iterdir()] == [
        True
    ]


def test_an_entry_naming_another_intros_file_is_refused(tmp_path: Path) -> None:
    store = _store(tmp_path)
    document = {
        "schema_version": 1,
        "intros": {"b" * 32: {"file": file_name("a" * 32, ".wav"), "track_id": "x"}},
    }
    store.path.write_text(json.dumps(document), encoding="utf-8")
    assert store.all() == {}


def test_a_file_that_cannot_be_read_is_kept_and_saves_are_refused(
    tmp_path: Path, monkeypatch
) -> None:
    store = _store(tmp_path)
    intro_id = _add(store)
    original = store.path.read_bytes()
    monkeypatch.setattr(
        Path, "read_bytes", lambda self: (_ for _ in ()).throw(PermissionError("busy"))
    )
    # The file changed on disk, so it must be read again, and cannot be.
    store._stamp = None
    assert intro_id in store.all()  # the intros last read
    with pytest.raises(OSError):
        store.add(new_id(), Intro(file=file_name(new_id(), ".wav"), track_id="y"))
    monkeypatch.undo()
    assert store.path.read_bytes() == original


def test_a_station_keeps_at_most_the_limit(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr("autodj.matched_intros.MAX_INTROS", 1)
    store = _store(tmp_path)
    _add(store)
    with pytest.raises(IntroError, match="at most 1 matched intros"):
        _add(store)
    assert MAX_INTROS == 2000


def test_remove_forgets_the_intro_and_the_caller_deletes_its_audio(tmp_path: Path) -> None:
    store = _store(tmp_path)
    intro_id = _add(store)
    removed = store.remove(intro_id)
    assert removed is not None and intro_id not in store.all()
    assert (store.folder / removed.file).exists()
    store.delete_file(removed)
    assert not (store.folder / removed.file).exists()
    assert store.remove(intro_id) is None


def test_audio_that_cannot_be_deleted_now_is_deleted_at_upkeep(tmp_path: Path, monkeypatch) -> None:
    store = _store(tmp_path)
    intro_id = _add(store)
    removed = store.remove(intro_id)
    assert removed is not None
    real_unlink = Path.unlink
    monkeypatch.setattr(
        Path,
        "unlink",
        lambda self, missing_ok=False: (_ for _ in ()).throw(PermissionError("open")),
    )
    store.delete_file(removed)
    assert store.needs_upkeep()
    monkeypatch.setattr(Path, "unlink", real_unlink)
    store.upkeep()
    assert not (store.folder / removed.file).exists()
    assert not store.needs_upkeep()


def test_rules_are_checked_and_named_by_field() -> None:
    assert parse_rules({"label": " New\tsingle ", "chance": "40", "cooldown_minutes": ""}) == {
        "label": "New single",
        "chance": 40,
        "cooldown_minutes": 60,
        "not_before": None,
        "not_after": None,
    }
    with pytest.raises(IntroError) as error:
        parse_rules({"chance": "0"})
    assert error.value.field == "chance"
    with pytest.raises(IntroError) as error:
        parse_rules({"not_before": "2026-12-26", "not_after": "2026-12-01"})
    assert error.value.field == "not_after"
    with pytest.raises(IntroError) as error:
        parse_rules({"cooldown_minutes": "99999"})
    assert error.value.message.startswith("Wait between plays:")


def test_how_often_it_plays_is_said_in_words() -> None:
    song = Intro(file=file_name("a" * 32, ".wav"), track_id="x", chance=100, cooldown_minutes=90)
    assert rules_text(song) == (
        "Plays every time this song comes up, at most once every 1 hour 30 minutes."
    )
    artist = Intro(
        file=file_name("a" * 32, ".wav"), artist="Example Artist", chance=25, cooldown_minutes=0
    )
    assert rules_text(artist) == (
        "Plays about 25% of the times a song by Example Artist comes up, with no wait between "
        "plays."
    )
    assert wait_words(24 * 60 + 1) == "1 day 1 minute"
    assert wait_words(0) == "0 minutes"


# ---------------------------------------------------------------------------
# Choosing one
# ---------------------------------------------------------------------------


def test_the_songs_own_intro_comes_before_its_artists(tmp_path: Path) -> None:
    store = _store(tmp_path)
    by_artist = _add(store, track_id=None, artist="The Example Band")
    own = _add(store, track_id="song-a")
    chosen = store.choose("song-a", "Example Band", NOW)
    assert isinstance(chosen, Chosen) and chosen.intro_id == own
    # Another song by the artist (the leading "The" and case aside) gets the artist's.
    chosen = store.choose("song-b", "example band", NOW)
    assert chosen is not None and chosen.intro_id == by_artist
    assert chosen.label == "for The Example Band"
    # A featured credit names the artist too.
    store.release([by_artist])
    assert store.choose("song-c", "Someone feat. The Example Band", NOW) is not None
    assert store.choose("song-d", "Somebody Else", NOW) is None


def test_outside_its_dates_an_intro_is_passed_over_by_the_day_it_airs(tmp_path: Path) -> None:
    days = {NOW: date(2026, 12, 27), NOW + 1: date(2026, 12, 26)}
    store = _store(tmp_path, day_at=lambda at: days[at])
    _add(store, not_after=date(2026, 12, 26))
    assert store.choose("song-a", "", NOW) is None
    assert store.choose("song-a", "", NOW + 1) is not None


def test_an_intro_waits_its_cooldown_after_it_is_held_or_heard(tmp_path: Path) -> None:
    store = _store(tmp_path)
    intro_id = _add(store, cooldown_minutes=10)
    assert store.choose("song-a", "", NOW) is not None
    # Held for that song change: the next one within ten minutes passes it over.
    assert store.choose("song-a", "", NOW + 300) is None
    # A render thrown away lets it go.
    store.release([intro_id])
    assert store.choose("song-a", "", NOW + 300) is not None
    store.heard(intro_id)
    store._heard[-1] = (intro_id, NOW + 300)  # heard when planned, for the test's clock
    store.release([intro_id])  # a later discard of another render changes nothing heard
    assert store.choose("song-a", "", NOW + 600) is None
    assert store.choose("song-a", "", NOW + 901) is not None


def test_with_no_wait_an_intro_may_play_at_every_change(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _add(store, cooldown_minutes=0)
    assert store.choose("song-a", "", NOW) is not None
    assert store.choose("song-a", "", NOW + 1) is not None


def test_the_one_heard_longest_ago_plays(tmp_path: Path) -> None:
    store = _store(tmp_path)
    first = _add(store, cooldown_minutes=0, last_aired=NOW - 50)
    second = _add(store, cooldown_minutes=0, last_aired=NOW - 100)
    chosen = store.choose("song-a", "", NOW)
    assert chosen is not None and chosen.intro_id == second
    store.heard(second)
    store._heard[-1] = (second, NOW)
    chosen = store.choose("song-a", "", NOW + 1)
    assert chosen is not None and chosen.intro_id == first


def test_the_chance_is_rolled_on_the_stores_own_random_source(tmp_path: Path) -> None:
    store = MatchedIntros(
        tmp_path / "matched_intros.json",
        tmp_path / "intros",
        rng=SimpleNamespace(random=lambda: 0.5),  # every roll lands at 50 %
    )
    _add(store, chance=50, cooldown_minutes=0)
    assert store.choose("song-a", "", NOW) is None
    by_artist = _add(store, track_id=None, artist="Example", chance=51, cooldown_minutes=0)
    # The song's own intro missed its chance, so the artist's is tried.
    chosen = store.choose("song-a", "Example", NOW)
    assert chosen is not None and chosen.intro_id == by_artist


def test_an_intro_whose_audio_is_missing_is_passed_over(tmp_path: Path) -> None:
    store = _store(tmp_path)
    intro_id = _add(store)
    (store.folder / store.all()[intro_id].file).unlink()
    assert store.choose("song-a", "", NOW) is None


def test_upkeep_writes_when_intros_aired(tmp_path: Path) -> None:
    store = _store(tmp_path)
    intro_id = _add(store)
    assert not store.needs_upkeep()
    store.heard(intro_id)
    assert store.needs_upkeep()
    store.upkeep()
    assert not store.needs_upkeep()
    aired = _store(tmp_path).all()[intro_id].last_aired
    assert aired is not None and aired > 0


def test_forget_removes_intros_and_their_audio(tmp_path: Path) -> None:
    store = _store(tmp_path)
    keep, gone = _add(store), _add(store, track_id="song-b")
    audio = store.folder / store.all()[gone].file
    assert store.forget([gone, "unknown"]) == [gone]
    assert list(store.all()) == [keep]
    assert not audio.exists()
    assert store.forget([]) == []


# ---------------------------------------------------------------------------
# The mix (saved-point playback)
# ---------------------------------------------------------------------------


def _intro_span(render) -> tuple[float, float]:
    first, _id, end, _label = render.intro_events[0]
    return (first + render.start_offset) / SR, (end + render.start_offset) / SR


def _incoming(render) -> float:
    return (len(render.audio) - render.overlap_frames + render.start_offset) / SR


def _chosen(tmp_path: Path, seconds: float) -> Chosen:
    path = write_voice(tmp_path / file_name(new_id(), ".wav"), seconds)
    return Chosen(new_id(), path, "New single")


def test_an_intro_that_fits_talks_up_the_song(tmp_path: Path) -> None:
    player, (a, b, _c, _d), _points, _desk = make_station(tmp_path)
    render = player._render_track(a, b, 0, intro=_chosen(tmp_path, 1.5))
    start, end = _intro_span(render)
    # b's intro is 2 s (0.25 to 2.25): the 1.5 s voice starts with b at a's segue.
    assert start == pytest.approx(10.0, abs=1e-3)
    assert end == pytest.approx(11.5, abs=1e-3)
    assert _incoming(render) == pytest.approx(10.0, abs=1e-3)
    assert render.intro_events[0][3] == "New single"


def test_an_intro_longer_than_the_songs_intro_plays_before_it(tmp_path: Path) -> None:
    player, (a, b, _c, _d), _points, _desk = make_station(tmp_path)
    render = player._render_track(a, b, 0, intro=_chosen(tmp_path, 3.0))
    start, end = _intro_span(render)
    assert start == pytest.approx(10.0, abs=1e-3)
    assert end == pytest.approx(13.0, abs=1e-3)
    # The song starts when the speech ends.
    assert _incoming(render) == pytest.approx(13.0, abs=1e-3)
    assert render.audio[round(12.0 * SR)][0] == 0.0  # a has faded, b has not started


def test_an_intro_follows_a_news_break(tmp_path: Path) -> None:
    player, (a, b, _c, _d), _points, desk = make_station(tmp_path)
    item = make_break(desk, new_id(), kind="news", seconds=2.0)
    desk.add(item)
    render = player._render_track(
        a, b, 0, breaks=tuple(desk.take(item.slot_id, 0.0)), intro=_chosen(tmp_path, 1.5)
    )
    news_start = (render.break_events[0][0] + render.start_offset) / SR
    start, end = _intro_span(render)
    assert news_start < start
    # The intro still talks up b: its speech ends before b's vocal.
    assert end <= _incoming(render) + 2.0 + 1e-3


def test_an_intro_that_cannot_be_decoded_is_left_out(tmp_path: Path) -> None:
    player, (a, b, _c, _d), _points, _desk = make_station(tmp_path)
    bad = tmp_path / file_name(new_id(), ".wav")
    bad.write_bytes(b"not audio")
    render = player._render_track(a, b, 0, intro=Chosen(new_id(), bad, "Broken"))
    assert render is not None and render.intro_events == ()
    assert _incoming(render) == pytest.approx(10.0, abs=1e-3)


def test_the_station_mixes_a_matched_intro_instead_of_an_automatic_liner(
    tmp_path: Path,
) -> None:
    player, entries, _points, _desk = make_station(tmp_path)
    a, b, _c, _d = entries
    store = _store(tmp_path)
    player.matched_intros = store
    intro_id = _add(store, track_id=f"song-{Path(b.path).stem}", seconds=1.5)
    scheduler = MagicMock()
    player.liner_scheduler = scheduler
    player._cfg.playback.pick_mode = "random"
    player._state.queue[:] = [b]
    player._set_render_cursor(a, 0, "seed")
    render = player._next_rendered()
    assert render.next_entry is b
    assert [event[1] for event in render.intro_events] == [intro_id]
    # The automatic liner was not asked for: its trigger stays due.
    scheduler.plan_for_handoff.assert_not_called()
    # The intro is heard as the bus plays past its start, without waiting.
    player._start_playing(render)
    player._on_position(render.intro_events[0][0] + 1)
    assert list(store._heard) and store._heard[0][0] == intro_id


def test_a_dj_break_in_the_change_wins_over_the_intro(tmp_path: Path) -> None:
    player, (_a, b, _c, _d), _points, desk = make_station(tmp_path)
    store = _store(tmp_path)
    player.matched_intros = store
    _add(store, track_id=f"song-{Path(b.path).stem}")
    item = make_break(desk, new_id())
    assert player._matched_intro(b, (item,), NOW) is None
    news = make_break(desk, new_id(), kind="news")
    assert player._matched_intro(b, (news,), NOW) is not None


def test_a_discarded_render_lets_its_intro_go(tmp_path: Path) -> None:
    player, (a, b, _c, _d), _points, _desk = make_station(tmp_path)
    store = _store(tmp_path)
    player.matched_intros = store
    intro_id = _add(store, track_id=f"song-{Path(b.path).stem}", cooldown_minutes=60)
    chosen = player._matched_intro(b, (), NOW)
    assert chosen is not None and intro_id in store._reserved
    render = player._render_track(a, b, 0, intro=chosen)
    player._discard_render(render)
    assert intro_id not in store._reserved


def test_choosing_an_intro_leaves_the_pickers_random_choices_alone(tmp_path: Path) -> None:
    # The rotation tester and the hour preview replay the picker's random
    # choices; an intro's chance must not draw from them.
    player, (_a, b, _c, _d), _points, _desk = make_station(tmp_path)
    store = _store(tmp_path)
    player.matched_intros = store
    _add(store, track_id=f"song-{Path(b.path).stem}", chance=50, cooldown_minutes=0)
    player._random = random.Random(5)
    state, shared = player._random.getstate(), random.getstate()
    for moment in range(20):
        player._matched_intro(b, (), NOW + moment)
    assert player._random.getstate() == state
    assert random.getstate() == shared  # the live picker's, when it has none of its own


def test_a_store_that_fails_never_stops_the_render(tmp_path: Path) -> None:
    player, (_a, b, _c, _d), _points, _desk = make_station(tmp_path)
    player.matched_intros = SimpleNamespace(
        choose=MagicMock(side_effect=OSError("away")), release=MagicMock()
    )
    assert player._matched_intro(b, (), NOW) is None


def test_the_hour_preview_lists_a_mixed_intro(tmp_path: Path) -> None:
    from autodj.hour_preview import _mixed_in, item_title

    player, (a, b, _c, _d), _points, _desk = make_station(tmp_path)
    render = player._render_track(a, b, 0, intro=_chosen(tmp_path, 1.5))
    items = _mixed_in(render, NOW, NOW, None, "ready", [])
    assert [(item.kind, item.status) for item in items] == [("intro", "ready")]
    assert items[0].seconds == pytest.approx(1.5, abs=1e-3)
    assert item_title(items[0]) == "Matched intro: New single"
    assert np.isfinite(items[0].starts)
