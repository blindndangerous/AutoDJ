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
import soundfile as sf

from autodj.breaks import new_id, speech_cues
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


def test_frozen_intro_reservation_does_not_draw_or_replace_candidate(tmp_path: Path) -> None:
    store = _store(tmp_path)
    intro_id = _add(store, cooldown_minutes=30)
    frozen = store.snapshot().choose("song-a", "", NOW)
    assert frozen is not None and frozen.intro_id == intro_id
    assert store.reserve_chosen(frozen.intro_id, NOW)
    assert not store.reserve_chosen(frozen.intro_id, NOW + 1)
    store.release((frozen.intro_id,))
    assert store.reserve_chosen(frozen.intro_id, NOW + 1)


def test_an_intro_is_for_one_song_or_one_artist(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="one song or one artist"):
        Intro(file=file_name("a" * 32, ".wav"), track_id="x", artist="Someone")
    with pytest.raises(ValueError, match="one song or one artist"):
        Intro(file=file_name("a" * 32, ".wav"))
    with pytest.raises(ValueError, match="intro-<id>"):
        Intro(file="../escape.wav", track_id="x")


@pytest.mark.parametrize(
    "text", ["not json", '{"schema_version": 2, "intros": {}}', '{"intros": []}']
)
def test_a_file_that_is_not_a_file_of_intros_is_set_aside(tmp_path: Path, text: str) -> None:
    store = _store(tmp_path)
    store.path.write_text(text, encoding="utf-8")
    assert store.all() == {}
    assert not store.path.exists()
    assert [path.name.startswith("matched_intros.json.bad-") for path in tmp_path.iterdir()] == [
        True
    ]


def test_one_bad_intro_is_skipped_kept_and_said_while_the_others_play(
    tmp_path: Path, caplog
) -> None:
    store = _store(tmp_path)
    good = _add(store)
    document = json.loads(store.path.read_text(encoding="utf-8"))
    bad = {
        # Names another intro's file.
        "b" * 32: {"file": file_name("a" * 32, ".wav"), "track_id": "song-a"},
        # Not an intro at all.
        "nope": {"chance": 500},
    }
    document["intros"].update(bad)
    store.path.write_text(json.dumps(document), encoding="utf-8")
    other = _store(tmp_path)
    assert list(other.all()) == [good]
    assert other.choose("song-a", "", NOW) is not None  # the good one still plays
    assert "skipping intro" in caplog.text
    problem = other.problem()
    assert problem is not None and problem.startswith("2 matched intros in matched_intros.json")
    assert "nope" in problem
    # A save keeps the bad entries as they were, so nothing is lost.
    _add(other, track_id="song-b")
    saved = json.loads(store.path.read_text(encoding="utf-8"))["intros"]
    for intro_id, raw in bad.items():
        assert saved[intro_id] == raw
    assert len(saved) == 4
    # Removed by hand, the problem goes.
    for intro_id in bad:
        del saved[intro_id]
    store.path.write_text(json.dumps({"schema_version": 1, "intros": saved}), encoding="utf-8")
    assert len(other.all()) == 2
    assert other.problem() is None


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


def test_snapshot_choosing_does_not_change_the_store_or_its_files(tmp_path: Path) -> None:
    store = _store(tmp_path)
    intro_id = _add(store, track_id="song-a", cooldown_minutes=0)
    before = store.path.read_bytes()
    source_rng = store._rng.getstate()
    store._aired[intro_id] = NOW - 120
    store._reserved[intro_id] = NOW - 60
    store._heard.append((intro_id, NOW - 30))
    store._released.append("other")
    clone = store.snapshot()
    clone._stat = lambda: (_ for _ in ()).throw(AssertionError("snapshot read the file"))

    assert clone._aired == store._aired
    assert clone._reserved == store._reserved
    assert list(clone._heard) == list(store._heard)
    assert list(clone._released) == list(store._released)
    assert clone.choose("song-a", "", NOW) is not None
    assert store._aired[intro_id] == NOW - 120
    assert store._reserved[intro_id] == NOW - 60
    assert list(store._heard) == [(intro_id, NOW - 30)]
    assert list(store._released) == ["other"]
    assert store._rng.getstate() == source_rng
    clone.upkeep()
    assert store.path.read_bytes() == before


def test_snapshot_reads_an_unopened_file_without_setting_aside_invalid_data(
    tmp_path: Path,
) -> None:
    written = _store(tmp_path)
    intro_id = _add(written)
    before = written.path.read_bytes()
    snapshot = _store(tmp_path).snapshot()
    assert intro_id in snapshot.all()
    assert written.path.read_bytes() == before

    written.path.write_text("not json", encoding="utf-8")
    before = written.path.read_bytes()
    snapshot = _store(tmp_path).snapshot()
    assert snapshot.all() == {}
    assert written.path.read_bytes() == before
    assert not list(tmp_path.glob("matched_intros.json.bad-*"))


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


def test_priority_tiers_can_choose_song_and_artist_separately(tmp_path: Path) -> None:
    store = _store(tmp_path)
    by_artist = _add(store, track_id=None, artist="Example Band")
    own = _add(store, track_id="song-a")
    assert store.choose("song-a", "Example Band", NOW, "artist").intro_id == by_artist
    assert store.choose("song-a", "Example Band", NOW, "song").intro_id == own


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


@pytest.mark.parametrize("speech", [1.0, 1.5, 2.0])
def test_a_talk_up_intro_follows_the_liner_intro_rule(tmp_path: Path, speech: float) -> None:
    # The rule liners follow when a positive overlap meets the next song's
    # intro (#195): the music enters max(main - intro, min(main, overlap))
    # after the voice starts.  A talk-up intro has overlap 0, so with speech
    # that fits the intro the song enters with the voice, and the speech is
    # over by the vocal; speech as long as the intro ends on the vocal.
    player, (a, b, _c, _d), _points, _desk = make_station(tmp_path)
    render = player._render_track(a, b, 0, intro=_chosen(tmp_path, speech))
    start, end = _intro_span(render)
    intro = 2.0  # b: Start 0.25, Intro 2.25
    release = max(speech - intro, min(speech, 0.0))
    assert _incoming(render) == pytest.approx(start + release, abs=1e-3)
    vocal = _incoming(render) + intro
    assert end == pytest.approx(start + speech, abs=1e-3)
    assert end <= vocal + 1e-3
    if speech == intro:
        assert end == pytest.approx(vocal, abs=1e-3)


def test_an_intro_spans_the_outro_and_short_incoming_intro(tmp_path: Path) -> None:
    player, (a, b, _c, _d), _points, _desk = make_station(tmp_path)
    render = player._render_track(a, b, 0, intro=_chosen(tmp_path, 3.0))
    start, end = _intro_span(render)
    assert start == pytest.approx(9.0, abs=1e-3)
    assert end == pytest.approx(12.0, abs=1e-3)
    assert _incoming(render) == pytest.approx(10.0, abs=1e-3)
    assert render.audio[round(11.0 * SR)][0] > 0.0


@pytest.mark.parametrize("outro", [None, 8.0])
@pytest.mark.parametrize("incoming_intro", [None, 0.75, 5.0])
def test_private_intro_uses_detected_cues_like_a_liner(tmp_path, outro, incoming_intro):
    from autodj.hour_preview import _preview_liner_group

    player, (a, b, _c, _d), points, _desk = make_station(tmp_path)
    points[a.path]["outro"] = outro
    points[b.path]["intro"] = (
        None if incoming_intro is None else points[b.path]["start"] + incoming_intro
    )
    path = tmp_path / file_name(new_id(), ".wav")
    audio = np.zeros((round(4.5 * SR), 2), dtype=np.float32)
    audio[round(0.5 * SR) : round(3.5 * SR), 1] = 0.5
    sf.write(path, audio, SR, subtype="FLOAT")
    detected = speech_cues(audio)
    assert detected.start == pytest.approx(0.45)
    assert detected.segue == pytest.approx(3.5)
    assert detected.end == pytest.approx(3.9)
    liner = SimpleNamespace(path=str(path), length=4.5)
    points[liner.path] = detected.points(True)
    player._library.catalog.record_for_path = lambda source: {
        "kind": "liner" if str(source) == liner.path else "music",
        "allow_outro": True,
    }
    load = player._load_playout_audio
    player._load_playout_audio = lambda entry: audio.copy() if entry is liner else load(entry)
    ordinary = player._render_saved_track(a, b, 0, ((liner, False),))
    chosen = Chosen(new_id(), path, "Private intro")
    matched = player._render_saved_track(a, b, 0, intro=chosen)
    assert ordinary is not None and matched is not None
    assert matched.intro_events[0][0] == ordinary.liner_events[0][0]
    assert _incoming(matched) == pytest.approx(_incoming(ordinary), abs=1 / SR)
    start, end = _intro_span(matched)
    assert end - start == pytest.approx(detected.speech, abs=1 / SR)
    assert start >= (10.0 if outro is None else outro) - 1 / SR
    assert end <= _incoming(matched) + (incoming_intro or 0) + 1 / SR

    voices = []

    def capture(_audio, cues, length, allow_outro):
        voices.append((SimpleNamespace(path=str(path), length=length), cues, allow_outro))
        return 0, round(detected.speech * SR)

    player._add_intro(chosen, capture)
    rows, delay = _preview_liner_group(player, a, b, (), 10.0, voice=voices[0])
    assert rows[0][1] == pytest.approx(start, abs=1 / SR)
    assert 10.0 + delay == pytest.approx(_incoming(matched), abs=1 / SR)


@pytest.mark.parametrize("overlap_start", [-1.0, 0.0, 0.5])
@pytest.mark.parametrize("incoming_intro", [0.0, 0.75, 5.0])
def test_an_intro_rule_uses_registered_liner_cues_like_a_liner(
    tmp_path: Path, overlap_start: float, incoming_intro: float
) -> None:
    player, (a, b, _c, _d), points, _desk = make_station(tmp_path)
    points[b.path]["intro"] = points[b.path]["start"] + incoming_intro
    liner_path = tmp_path / "year-2000.wav"
    liner = SimpleNamespace(path=str(liner_path), length=4.0)
    audio = np.zeros((round(liner.length * SR), 2), dtype=np.float32)
    audio[: round(3.0 * SR), 1] = 0.5
    points[liner.path] = {
        "start": 0.5,
        "segue": 2.5,
        "end": 3.5,
        "overlap_start": overlap_start,
    }
    player._library.catalog.entry_for_path = lambda path: liner if str(path) == liner.path else None
    player._library.catalog.record_for_path = lambda path: (
        {"id": "registered-year", "kind": "liner", "allow_outro": True}
        if str(path) == liner.path
        else {"id": f"song-{Path(path).stem}", "kind": "music"}
    )
    load_playout_audio = player._load_playout_audio

    def load(entry):
        return audio.copy() if entry.path == liner.path else load_playout_audio(entry)

    player._load_playout_audio = load  # type: ignore[method-assign]

    ordinary = player._render_saved_track(a, b, 0, ((liner, False),))
    chosen = Chosen(f"rule:{'a' * 32}", liner_path, "Year 2000")
    matched = player._render_saved_track(a, b, 0, intro=chosen)

    assert ordinary is not None and matched is not None
    assert matched.intro_events[0][0] == ordinary.liner_events[0][0]
    assert matched.intro_events[0][2] - matched.intro_events[0][0] == round(2.0 * SR)
    assert matched.overlap_frames == ordinary.overlap_frames
    assert matched.next_start_offset == ordinary.next_start_offset
    assert matched.intro_assets == ((chosen.intro_id, str(liner_path)),)


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


def test_an_intro_skipped_before_its_event_is_released(tmp_path: Path) -> None:
    player, (a, b, c, _d), _points, _desk = make_station(tmp_path)
    store = _store(tmp_path)
    player.matched_intros = store
    store.release = MagicMock(side_effect=AssertionError("callback must not take the store lock"))
    intro_id = _add(store, track_id=f"song-{Path(b.path).stem}", cooldown_minutes=60)
    chosen = player._matched_intro(b, (), NOW)
    assert chosen is not None
    first = player._render_track(a, b, 0, intro=chosen)
    player._start_playing(first)
    player._start_playing(player._render_track(b, c, 0))

    assert store.needs_upkeep()
    assert store.choose(f"song-{Path(b.path).stem}", "", NOW + 1) is not None
    store.upkeep()
    assert intro_id in store._reserved


def test_an_intro_heard_before_a_skip_keeps_its_cooldown(tmp_path: Path) -> None:
    player, (a, b, c, _d), _points, _desk = make_station(tmp_path)
    store = _store(tmp_path)
    player.matched_intros = store
    intro_id = _add(store, track_id=f"song-{Path(b.path).stem}", cooldown_minutes=60)
    chosen = player._matched_intro(b, (), NOW)
    assert chosen is not None
    first = player._render_track(a, b, 0, intro=chosen)
    player._start_playing(first)
    player._on_position(first.intro_events[0][0] + 1)
    player._start_playing(player._render_track(b, c, 0))

    store.upkeep()
    assert intro_id not in store._reserved
    assert store.all()[intro_id].last_aired is not None
    assert store.choose(f"song-{Path(b.path).stem}", "", store._aired[intro_id] + 1) is None


def test_stopping_before_an_intro_event_releases_its_reservation(tmp_path: Path) -> None:
    player, (a, b, _c, _d), _points, _desk = make_station(tmp_path)
    store = _store(tmp_path)
    player.matched_intros = store
    intro_id = _add(store, track_id=f"song-{Path(b.path).stem}", cooldown_minutes=60)
    chosen = player._matched_intro(b, (), NOW)
    assert chosen is not None
    player._start_playing(player._render_track(a, b, 0, intro=chosen))

    player.end_set()
    assert store.choose(f"song-{Path(b.path).stem}", "", NOW + 1) is not None
    store.upkeep()
    assert intro_id in store._reserved


def test_matched_intro_only_without_liner_uses_scheduled_auto_liner_first(
    tmp_path: Path,
) -> None:
    player, (a, b, c, _d), _points, _desk = make_station(tmp_path)
    player._cfg.playback.matched_intro_only_without_liner = True
    player._state.queue[:] = [b]
    player._set_render_cursor(a, 0, "seed")
    intro = _chosen(tmp_path, 1.5)
    player._matched_intro = MagicMock(return_value=intro)
    player._auto_liner = MagicMock(return_value=(((c, False),), "handoff"))
    player._render_track = MagicMock(return_value=None)

    assert player._next_rendered() is None

    player._auto_liner.assert_called()
    player._matched_intro.assert_not_called()
    assert all(
        call.kwargs["liners"] == ((c, False),) for call in player._render_track.call_args_list
    )
    assert all("intro" not in call.kwargs for call in player._render_track.call_args_list)


def test_matched_intro_only_without_liner_keeps_queued_liner_precedence(
    tmp_path: Path,
) -> None:
    player, (a, b, c, _d), _points, _desk = make_station(tmp_path)
    player._cfg.playback.matched_intro_only_without_liner = True
    player._state.queue[:] = [b]
    player._set_render_cursor(a, 0, "seed")
    player._choose_station_next = MagicMock(return_value=(b, "queue", ((c, True),), None, ""))
    player._matched_intro = MagicMock(return_value=_chosen(tmp_path, 1.5))
    player._auto_liner = MagicMock(return_value=((), ""))
    player._render_track = MagicMock(return_value=None)

    assert player._next_rendered() is None

    player._matched_intro.assert_not_called()
    player._auto_liner.assert_not_called()
    assert player._render_track.call_args.kwargs["liners"] == ((c, True),)
    assert "intro" not in player._render_track.call_args.kwargs


@pytest.mark.parametrize("queued_liner", [False, True])
def test_liner_only_setting_and_hour_preview_follow_the_rendered_handoff(
    tmp_path: Path, queued_liner: bool
) -> None:
    from autodj.hour_preview import _mixed_in

    player, (a, b, c, _d), _points, _desk = make_station(tmp_path)
    player._cfg.playback.matched_intro_only_without_liner = True
    store = _store(tmp_path)
    player.matched_intros = store
    intro_id = _add(store, track_id=f"song-{Path(b.path).stem}", seconds=3.0)
    player._set_render_cursor(a, 0, "seed")
    if queued_liner:
        player._library.entries_snapshot = lambda kind="music": (
            (c,) if kind == "liner" else (a, b, c, _d)
        )
        player._state.queue[:] = [c, b]
    else:
        player._state.queue[:] = [b]
        player._auto_liner = MagicMock(return_value=((), ""))

    render = player._next_rendered()
    assert render is not None
    items = _mixed_in(render, NOW, NOW, None, "ready", [])

    if queued_liner:
        assert render.liner_events
        assert render.intro_events == ()
        assert intro_id not in store._reserved
        assert [item.kind for item in items] == ["liner"]
    else:
        assert not render.liner_events
        assert render.intro_events
        assert _intro_span(render)[1] == pytest.approx(12.0, abs=1e-3)
        assert _incoming(render) == pytest.approx(10.0, abs=1e-3)
        assert [item.kind for item in items] == ["intro"]


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
    intro = _chosen(tmp_path, 1.5)
    render = player._render_track(a, b, 0, intro=intro)
    player._library.catalog.record_for_path = lambda _path: {"kind": "liner"}
    player._library.catalog.entry_for_path = lambda path: SimpleNamespace(path=str(path))
    items = _mixed_in(render, NOW, NOW, None, "ready", [], player=player)
    assert [(item.kind, item.status) for item in items] == [("intro", "ready")]
    assert items[0].seconds == pytest.approx(1.5, abs=1e-3)
    assert items[0].asset_id == intro.intro_id
    assert items[0].details_entry.path == str(intro.path)
    assert item_title(items[0]) == "Matched intro: New single"
    assert render.intro_assets == ((intro.intro_id, str(intro.path)),)
    assert np.isfinite(items[0].starts)
