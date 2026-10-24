"""Prepared editions share the queue and mixer without a near-air reservation."""

import asyncio
import copy
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest
import soundfile as sf

from autodj._bridge import PlayerBridge
from autodj.identity_map import attached
from autodj.shows import ShowRunner, ShowStore, parse_spec
from autodj.stereo import SAMPLE_RATE as SR
from tests.unit._rotation_station import make_station, song


class Editions:
    def __init__(self, plan):
        self.plan = plan
        self.started = 0
        # The show the edition was made from, which names the station (#472).
        self.show_id = "b" * 32

    def runtime_plan(self, identity, station, *, include_pending=False, from_occurrence=None):
        assert identity == self.plan.edition_id
        if from_occurrence is None:
            return copy.deepcopy(self.plan)
        index = next(
            i for i, item in enumerate(self.plan.items) if item.occurrence_id == from_occurrence
        )
        return SimpleNamespace(**{**vars(self.plan), "items": self.plan.items[index:]})

    def get(self, identity):
        assert identity == self.plan.edition_id
        return SimpleNamespace(show_id=self.show_id)

    def mark_started(self, identity, *, confirm_reair=False):
        if self.plan.previously_started and not confirm_reair:
            raise ValueError("Confirm re-airing this edition")
        self.started += 1
        self.plan.previously_started = True

    def plan_is_current(self, plan, station):
        return repr((plan.items, plan.handoffs, plan.omissions)) == repr(
            (self.plan.items, self.plan.handoffs, self.plan.omissions)
        )


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
    # This fake catalog uses exact keys; real Library normalizes native paths.
    entries = [replace(entry, path=str(Path(entry.path))) for entry in entries]
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


def test_one_prepared_plan_is_shared_through_render_and_decode(tmp_path, monkeypatch):
    runner, source, _, editions, _ = setup(tmp_path)
    clips = []
    for identity in ("opening", "segment"):
        path = tmp_path / f"{identity}.wav"
        sf.write(path, np.full((2 * SR, 2), 0.2, np.float32), SR, subtype="FLOAT")
        clips.append(
            SimpleNamespace(
                occurrence_id=identity,
                path=path,
                kind="clip",
                seconds=2,
                take_id=identity,
                label=identity,
            )
        )
    speech = tmp_path / "link.wav"
    sf.write(speech, np.full((SR, 2), 0.3, np.float32), SR, subtype="FLOAT")
    editions.plan.items = tuple(clips)
    editions.plan.handoffs = {
        ("opening", "segment"): (
            SimpleNamespace(
                take_id="link",
                asset_id="link-asset",
                path=speech,
                seconds=1,
                placement="standalone",
                allow_outro=False,
                label="Link",
            ),
        )
    }
    asyncio.run(runner.start_edition(editions.plan.edition_id))
    current, upcoming = source.player._state.queue
    plan = editions.runtime_plan
    calls = []

    def counted(*args, **kwargs):
        calls.append(True)
        return plan(*args, **kwargs)

    monkeypatch.setattr(editions, "runtime_plan", counted)
    source.player._set_render_cursor(current, 0, "queue", from_queue=True)
    rendered = source.player._next_rendered()
    assert rendered is not None and rendered.entry is current
    assert rendered.next_entry is upcoming
    assert [event[1] for event in rendered.break_events] == ["link"]
    assert len(calls) == 1


@pytest.mark.parametrize(
    "error", [PermissionError("sharing violation"), ValueError("invalid manifest")]
)
@pytest.mark.parametrize("manifest_unreadable", [False, True])
def test_post_decode_store_failure_keeps_song_and_drops_prepared_speech(
    tmp_path, monkeypatch, error, manifest_unreadable
):
    from tests.unit.test_prepared_tail import _current_handoff

    _runner, source, editions, old, _, _, _ = _current_handoff(tmp_path)
    original = editions.runtime_plan
    failed = False
    checks = 0

    def validate(*args):
        nonlocal failed, checks
        checks += 1
        if checks > 1:
            pytest.fail("A failed validation must not retry another unverifiable plan")
        failed = True
        raise error

    def plan(*args, **kwargs):
        if failed and manifest_unreadable:
            raise error
        return original(*args, **kwargs)

    monkeypatch.setattr(editions, "plan_is_current", validate)
    monkeypatch.setattr(editions, "runtime_plan", plan)
    source.player._set_render_cursor(old.entry, 0, "queue", from_queue=True)
    rendered = source.player._next_rendered()
    assert rendered is not None and rendered.entry is old.entry
    assert not rendered.break_events
    assert np.max(np.abs(rendered.audio)) > 0
    assert checks == 1


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


def test_progress_write_failure_does_not_retry_successful_airing_marker(tmp_path, monkeypatch):
    runner, source, _, editions, _ = setup(tmp_path)
    asyncio.run(runner.start_edition(editions.plan.edition_id))
    monkeypatch.setattr(
        runner, "_write_state", lambda *args, **kwargs: (_ for _ in ()).throw(OSError("disk full"))
    )
    entry = source.player._state.queue[0]
    runner.prepared_render_committed(entry)
    runner.prepared_render_committed(entry)
    assert editions.started == 1


@pytest.mark.parametrize("edge, expected", [("opening", "0"), ("closing", "2")])
def test_progress_saves_real_resume_occurrence_for_edge_clips(tmp_path, edge, expected):
    """ADR-0015-05: a restart never preselects an edge take as its occurrence."""
    import json

    runner, _, _, editions, _ = setup(tmp_path)
    clip = SimpleNamespace(
        occurrence_id=edge,
        take_id=edge,
        path=tmp_path / f"{edge}.wav",
        kind="clip",
        seconds=1,
        label=edge,
    )
    editions.plan.items = (
        (clip, *editions.plan.items) if edge == "opening" else (*editions.plan.items, clip)
    )
    asyncio.run(runner.start_edition(editions.plan.edition_id))
    air = runner._on_air
    air.waiting.clear()
    air.next_index = 0 if edge == "opening" else len(air.prepared.items) - 1
    runner._write_state(air, strict=True)
    document = json.loads((tmp_path / "show_on_air.json").read_text())
    assert document["resume_occurrence"] == expected


def test_preview_handoff_does_not_append_live_omissions(tmp_path):
    runner, source, _, editions, _ = setup(tmp_path)
    asyncio.run(runner.start_edition(editions.plan.edition_id))
    editions.plan.omissions = ("Future take needs review",)
    before = runner.status()["skipped"][:]
    queued = source.player._state.queue
    assert runner.prepared_handoff(queued[0], queued[1], report_omissions=False) == ()
    assert runner.status()["skipped"] == before


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


@pytest.mark.parametrize("pending", [False, True])
def test_stopping_another_show_preserves_interrupted_edition_and_restart_marker(tmp_path, pending):
    runner, _source, bridge, editions, station = setup(tmp_path)
    asyncio.run(runner.start_edition(editions.plan.edition_id))
    interrupted = {
        "id": "older",
        "edition_id": "older",
        "name": "Earlier edition",
        "interrupted": True,
        "resume_occurrence": "saved",
    }
    runner._interrupted = interrupted
    if pending:
        runner._on_air = None
        runner._resume = {"name": "Unrelated ordinary Show"}
    asyncio.run(runner.stop())
    assert runner.status() == interrupted
    restarted = ShowRunner(
        bridge,
        runner._store,
        runner._publish,
        state_path=tmp_path / "show_on_air.json",
        edition_store=editions,
        edition_station=station,
    )
    assert restarted.status() == interrupted
    asyncio.run(restarted.stop())
    assert restarted.status() is None


def test_render_guard_validates_outside_settings_lock_and_rejects_settings_race(
    tmp_path, monkeypatch
):
    from contextlib import contextmanager

    runner, source, _, editions, _ = setup(tmp_path)
    asyncio.run(runner.start_edition(editions.plan.edition_id))
    token = runner.prepared_render_token()
    held = False
    race_at_commit = False

    @contextmanager
    def settings():
        nonlocal held
        if race_at_commit:
            source.player._cfg.playback.crossfade_seconds += 1
        held = True
        try:
            yield
        finally:
            held = False

    original = editions.runtime_plan
    changed = False

    def plan(*args, **kwargs):
        assert not held, "file validation must not hold the process-wide settings lock"
        value = original(*args, **kwargs)
        if changed:
            source.player._cfg.playback.crossfade_seconds += 1
        return value

    monkeypatch.setattr(source.player, "settings_lock", settings(), raising=False)
    monkeypatch.setattr(editions, "runtime_plan", plan)
    with runner.prepared_render_guard(token) as valid:
        assert held and valid
    changed = True
    monkeypatch.setattr(source.player, "settings_lock", settings())
    with runner.prepared_render_guard(token) as valid:
        assert not valid
    changed = False
    token = runner.prepared_render_token()
    race_at_commit = True
    monkeypatch.setattr(source.player, "settings_lock", settings())
    with runner.prepared_render_guard(token) as valid:
        assert not valid


def test_a_prepared_edition_names_the_station_as_its_show_does(tmp_path):
    """ADR-0009-08: an edition on the air uses the station name of the show it was made from."""
    runner, _source, _bridge, editions, _station = setup(tmp_path)
    show_id, _show = runner._store.add(
        parse_spec(
            {
                "name": "Tomorrow",
                "source": "category",
                "category": "hits",
                "station_name": "Example FM Tomorrow",
            }
        )
    )
    editions.show_id = show_id
    assert runner.on_air_station_name() == ""
    asyncio.run(runner.start_edition(editions.plan.edition_id))
    assert runner.on_air_station_name() == "Example FM Tomorrow"
