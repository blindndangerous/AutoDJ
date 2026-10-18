"""Prepared editions share the queue and mixer without a near-air reservation."""

import asyncio
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest
import soundfile as sf

from autodj._bridge import PlayerBridge
from autodj.identity_map import attached
from autodj.shows import ShowRunner, ShowStore
from autodj.stereo import SAMPLE_RATE as SR
from tests.unit._rotation_station import make_station, song


class Editions:
    def __init__(self, plan):
        self.plan = plan
        self.started = 0

    def runtime_plan(self, identity, station, *, include_pending=False, from_occurrence=None):
        assert identity == self.plan.edition_id
        if from_occurrence is None:
            return self.plan
        index = next(
            i for i, item in enumerate(self.plan.items) if item.occurrence_id == from_occurrence
        )
        return SimpleNamespace(**{**vars(self.plan), "items": self.plan.items[index:]})

    def mark_started(self, identity, *, confirm_reair=False):
        if self.plan.previously_started and not confirm_reair:
            raise ValueError("Confirm re-airing this edition")
        self.started += 1
        self.plan.previously_started = True


def item(identity, entry, *, kind="song"):
    return SimpleNamespace(
        occurrence_id=identity,
        path=Path(entry.path),
        kind=kind,
        seconds=entry.length,
        take_id=None,
        label=entry.title,
    )


def setup(tmp_path, entries=None, *, omissions=()):
    entries = entries or [song(1, "One"), song(2, "Two"), song(3, "Three")]
    source = make_station(
        tmp_path / "station",
        entries,
        playback={"transition_mode": "saved_points", "loudness_processing": "off"},
    )
    source.player._state.current_track = entries[-1]
    source.player._build_bus()
    bridge = PlayerBridge(
        player=source.player, sim=source.player._sim, library=source.library, ledger=source.ledger
    )
    plan = SimpleNamespace(
        edition_id="a" * 32,
        name="Tomorrow",
        revision=1,
        items=tuple(item(str(n), entry) for n, entry in enumerate(entries)),
        handoffs={},
        omissions=omissions,
        estimated_seconds=600,
        previously_started=False,
    )
    editions = Editions(plan)
    station = SimpleNamespace(cfg=source.player._cfg, bridge=bridge, player=source.player)

    async def publish(message):
        pass

    runner = ShowRunner(
        bridge,
        ShowStore(tmp_path / "shows.json"),
        publish,
        state_path=tmp_path / "show_on_air.json",
        edition_store=editions,
        edition_station=station,
    )
    return runner, source, bridge, editions, station


def test_repeated_song_occurrences_bind_only_their_exact_transition(tmp_path):
    runner, source, _bridge, editions, _station = setup(tmp_path)
    first = source.library.entries_snapshot()[0]
    editions.plan.items = (item("first", first), item("second", first), item("third", first))
    speech = SimpleNamespace(
        take_id="b" * 32,
        asset_id="c" * 32,
        path=tmp_path / "take.wav",
        seconds=2,
        placement="standalone",
        allow_outro=False,
        label="Link",
    )
    editions.plan.handoffs = {("first", "second"): (speech,)}
    asyncio.run(runner.start_edition(editions.plan.edition_id))
    queued = source.player._state.queue
    assert len(queued) == 2
    assert queued[0] is not queued[1] and queued[0].path == queued[1].path
    assert [
        entry.occurrence_id for _, entry in attached(source.player, "_edition_occurrences").items()
    ] == ["first", "second"]
    assert [entry.break_id for entry in runner.prepared_handoff(queued[0], queued[1])] == ["b" * 32]
    assert runner.prepared_handoff(queued[1], queued[0]) == ()
    assert runner.prepared_handoff(first, queued[1]) == ()
    assert editions.started == 0


def test_exact_entry_hard_start_preserves_prepared_occurrence(tmp_path, monkeypatch):
    runner, source, _bridge, editions, _station = setup(tmp_path)
    played = []
    monkeypatch.setattr(source.player, "play_now", lambda entry: played.append(entry))
    asyncio.run(runner.start_edition(editions.plan.edition_id, now=True))
    assert len(played) == 1
    assert played[0]._prepared_occurrence_id == "0"
    assert played[0] is not source.library.entries_snapshot()[0]
    assert played[0] not in source.player._state.queue


def test_prepared_restart_requires_explicit_chosen_occurrence_and_preserves_ordinary_resume(
    tmp_path,
):
    """ADR-0015-05: an interrupted edition needs an explicit occurrence choice."""
    runner, source, bridge, editions, station = setup(tmp_path)
    asyncio.run(runner.start_edition(editions.plan.edition_id))
    source.player._state.queue.clear()

    async def publish(message):
        pass

    restarted = ShowRunner(
        bridge,
        runner._store,
        publish,
        state_path=tmp_path / "show_on_air.json",
        edition_store=editions,
        edition_station=station,
    )
    assert restarted.status()["interrupted"] is True

    async def observe():
        restarted.track_changed()
        await asyncio.sleep(0)
        for task in list(restarted._tasks):
            await task

    asyncio.run(observe())
    assert source.player._state.queue == []
    with pytest.raises(ValueError, match="Choose"):
        asyncio.run(restarted.start_edition(editions.plan.edition_id))
    asyncio.run(restarted.start_edition(editions.plan.edition_id, from_occurrence="1"))
    assert source.player._state.queue[0]._prepared_occurrence_id == "1"


def test_reair_requires_confirmation_before_queue_changes(tmp_path):
    runner, source, _bridge, editions, _station = setup(tmp_path)
    editions.plan.previously_started = True
    with pytest.raises(ValueError, match="Confirm"):
        asyncio.run(runner.start_edition(editions.plan.edition_id))
    assert source.player._state.queue == []
    asyncio.run(runner.start_edition(editions.plan.edition_id, confirm_reair=True))
    assert source.player._state.queue


def test_unusable_speech_is_reported_but_valid_songs_queue(tmp_path):
    runner, source, _bridge, editions, _station = setup(
        tmp_path, omissions=("Opening needs review",)
    )
    asyncio.run(runner.start_edition(editions.plan.edition_id))
    assert len(source.player._state.queue) == 2
    assert any("Opening needs review" in row["reason"] for row in runner.status()["skipped"])


def test_prepared_clip_is_not_a_liner_and_uses_native_rate_and_own_cues(tmp_path):
    from autodj.shows import prepared_entry

    _runner, source, _bridge, _editions, _station = setup(tmp_path)
    source.player._cfg.playback.music_speed_percent = 10
    clip = SimpleNamespace(
        occurrence_id="clip",
        path=tmp_path / "interview.wav",
        kind="clip",
        seconds=1800,
        take_id="d" * 32,
        label="Interview",
    )
    entry = prepared_entry(clip, source.library)
    assert source.player.music_playback_rate(entry) == 1
    assert source.player._is_liner(entry) is False
    assert source.player.saved_points(entry) == {"start": 0.0, "segue": 1800, "end": 1800}
    assert entry.length == 1800


@pytest.mark.parametrize("mode", ["saved_points", "fixed"])
def test_prepared_handoff_renders_when_announcer_breaks_are_disabled(tmp_path, mode):
    """ADR-0015-04: prepared speech uses the owned playout placement path."""
    entries = []
    for n in range(2):
        path = tmp_path / f"song-{n}.wav"
        sf.write(path, np.full((2 * SR, 2), 0.2, np.float32), SR, subtype="FLOAT")
        entries.append(replace(song(n, "Artist", length=2), path=str(path)))
    runner, source, _bridge, editions, _station = setup(tmp_path, entries)
    source.player._cfg.playback.transition_mode = mode
    voice = tmp_path / "speech.wav"
    sf.write(voice, np.full((SR, 2), 0.3, np.float32), SR, subtype="FLOAT")
    spec = SimpleNamespace(
        take_id="b" * 32,
        asset_id="c" * 32,
        path=voice,
        seconds=1,
        placement="standalone",
        allow_outro=False,
        label="Link",
    )
    source.player._cfg.breaks.enabled = False
    assert source.player._breaks_on() is False
    editions.plan.handoffs = {("0", "1"): (spec,)}
    asyncio.run(runner.start_edition(editions.plan.edition_id))
    source.player._set_render_cursor(source.player._state.queue[0], 0, "queue", from_queue=True)
    rendered = source.player._next_rendered()
    assert rendered is not None
    assert [event[1] for event in rendered.break_events] == [spec.take_id]
    assert editions.started == 1
    assert source.player._slot_plan is None


def settle(runner):
    async def changed():
        runner.track_changed()
        await asyncio.sleep(0)
        for task in list(runner._tasks):
            await task

    asyncio.run(changed())


def test_conservative_airing_guard_is_persisted_once_before_owned_pcm_can_escape(tmp_path):
    runner, source, _bridge, editions, _station = setup(tmp_path)
    asyncio.run(runner.start_edition(editions.plan.edition_id))
    assert editions.started == 0
    entry = source.player._state.queue[0]
    runner.prepared_render_committed(entry)
    assert editions.started == 1
    runner.prepared_render_committed(entry)
    assert editions.started == 1


def test_final_prepared_item_remains_active_until_automation_really_follows(tmp_path):
    runner, source, _bridge, editions, _station = setup(tmp_path)
    editions.plan.items = editions.plan.items[:1]
    asyncio.run(runner.start_edition(editions.plan.edition_id))
    source.player._state.current_track = source.player._state.queue.pop(0)
    settle(runner)
    assert runner.edition_active(editions.plan.edition_id)
    source.player._state.current_track = source.library.entries_snapshot()[-1]
    settle(runner)
    assert runner.status() is None
    assert not (tmp_path / "show_on_air.json").exists()


def test_approval_invalidated_after_admission_is_reported_without_replacement(tmp_path):
    runner, source, _bridge, editions, _station = setup(tmp_path)
    asyncio.run(runner.start_edition(editions.plan.edition_id))
    editions.plan.omissions = ("Link: needs review; speech skipped",)
    queued = source.player._state.queue
    assert runner.prepared_handoff(queued[0], queued[1]) == ()
    assert any("Link: needs review" in row["reason"] for row in runner.status()["skipped"])


def test_primary_speech_uses_bounded_long_decode_instead_of_break_decoder(tmp_path, monkeypatch):
    """ADR-0015-04: prepared speech uses the owned playout placement path."""
    from autodj import player
    from autodj.shows import prepared_entry

    _runner, source, _bridge, _editions, _station = setup(tmp_path)
    clip = SimpleNamespace(
        occurrence_id="clip",
        path=tmp_path / "interview.wav",
        kind="clip",
        seconds=1800,
        take_id="d" * 32,
        label="Interview",
    )
    entry = prepared_entry(clip, source.library)
    calls = []

    def decoded(path, rate, *, max_seconds):
        calls.append((path, rate, max_seconds))
        return np.zeros((SR, 2), np.float32)

    monkeypatch.setattr(player, "load_stereo", decoded)
    monkeypatch.setattr(
        source.player, "_load_voice_audio", lambda *_: pytest.fail("Short break decoder used")
    )
    assert len(source.player._load_playout_audio(entry)) == SR
    assert calls == [(str(clip.path), SR, 1800)]


@pytest.mark.parametrize("confirmed", [False, True])
def test_existing_show_scheduler_action_dispatches_prepared_editions(tmp_path, confirmed):
    from autodj.shows import show_action

    runner, _source, _bridge, editions, _station = setup(tmp_path)
    calls = []

    async def start(identity, *, now=False, confirm_reair=False):
        calls.append((identity, now, confirm_reair))

    runner.start_edition = start
    editions.plan.previously_started = True
    editions.all = lambda: {editions.plan.edition_id: editions.plan}
    action = show_action(runner, runner._store, editions)
    prefix = "edition-confirmed" if confirmed else "edition"
    value = f"{prefix}:{editions.plan.edition_id}"
    params = action.check({"show": value}, live=True)
    asyncio.run(action.hard(params))
    assert action.name == "show"
    assert calls == [(editions.plan.edition_id, True, confirmed)]


@pytest.mark.parametrize("during_decode", [False, True])
def test_primary_speech_is_revalidated_after_admission(tmp_path, monkeypatch, during_decode):
    from autodj import player

    runner, source, _bridge, editions, _station = setup(tmp_path)
    clip = SimpleNamespace(
        occurrence_id="opening",
        path=tmp_path / "opening.wav",
        kind="clip",
        seconds=1,
        take_id="d" * 32,
        label="Opening",
    )
    sf.write(clip.path, np.ones((SR, 2), np.float32) * 0.2, SR, subtype="FLOAT")
    editions.plan.items = (clip, *editions.plan.items)
    asyncio.run(runner.start_edition(editions.plan.edition_id))
    entry = source.player._state.queue[0]
    decode = player.load_stereo

    def invalidate():
        editions.plan.items = tuple(row for row in editions.plan.items if row.take_id is None)
        editions.plan.omissions = ("Opening: needs review; speech skipped",)

    if during_decode:

        def changed(*args, **kwargs):
            audio = decode(*args, **kwargs)
            invalidate()
            return audio

        monkeypatch.setattr(player, "load_stereo", changed)
    else:
        invalidate()
    assert source.player._render_track(entry, None, 0) is None
    assert any("Opening" in row["reason"] for row in runner.status()["skipped"])


def test_hard_dispatch_crash_keeps_conservative_reair_guard(tmp_path, monkeypatch):
    runner, _source, bridge, editions, station = setup(tmp_path)

    def crash(entry, now):
        assert editions.started == 1
        assert (tmp_path / "show_on_air.json").is_file()
        raise RuntimeError("crash immediately after dispatch")

    monkeypatch.setattr(bridge, "play_next_entry", crash)
    with pytest.raises(RuntimeError, match="crash immediately"):
        asyncio.run(runner.start_edition(editions.plan.edition_id, now=True))
    restarted = ShowRunner(
        bridge,
        runner._store,
        runner._publisher,
        state_path=tmp_path / "show_on_air.json",
        edition_store=editions,
        edition_station=station,
    )
    assert restarted.status()["interrupted"] is True
    with pytest.raises(ValueError, match="Confirm re-airing"):
        asyncio.run(restarted.start_edition(editions.plan.edition_id, from_occurrence="0"))


def test_prepared_handoff_keeps_approved_leading_and_trailing_silence(tmp_path, monkeypatch):
    from autodj.shows import prepared_break

    _runner, source, _bridge, _editions, _station = setup(tmp_path)
    audio = np.zeros((3 * SR, 2), np.float32)
    audio[SR : 2 * SR] = 0.3
    take = SimpleNamespace(
        take_id="d" * 32,
        path=tmp_path / "padded.wav",
        seconds=3,
        placement="standalone",
        allow_outro=False,
        label="Padded",
    )
    captured = []
    monkeypatch.setattr(source.player, "_load_break_audio", lambda item: audio)

    def add(samples, points, seconds, allow_outro):
        captured.append((points, seconds, samples))
        return (0, len(samples))

    spoken = source.player._add_breaks((prepared_break(take),), {}, add)
    points, seconds, retained = captured[0]
    assert seconds == 3
    assert points["start"] == 0
    assert points["end"] == 3
    assert retained is audio
    assert spoken == [(0, take.take_id, 3 * SR)]
