"""Prepared speech revocation only replaces audio the bus has not reached."""

from dataclasses import replace

import numpy as np
import pytest

from autodj.mixbus import MixBus
from tests.unit.test_mixbus import _bus, _track


def test_unheard_prepared_tail_replacement_preserves_music_and_position():
    original = _track(0.2, 6 * MixBus.BLOCK)
    original.audio[4 * MixBus.BLOCK :] = 0.7
    replacement = replace(original, audio=np.full_like(original.audio, 0.2), break_events=())
    bus, started, positions = _bus([original])
    bus.start_set()
    first = bus.render_block()
    assert bus.replace_unheard_tail(original, replacement, before_frame=4 * MixBus.BLOCK)
    blocks = [first] + [bus.render_block() for _ in range(5)]
    np.testing.assert_allclose(np.concatenate(blocks), 0.2)
    assert started == [original]
    # Position callbacks describe a current track; its final block clears it.
    assert positions == [MixBus.BLOCK * n for n in range(1, 6)]


@pytest.mark.parametrize("crossed", [False, True])
def test_unheard_tail_replacement_refuses_raced_or_already_started_audio(crossed):
    original = _track(0.2, 5 * MixBus.BLOCK)
    replacement = replace(original, audio=np.full_like(original.audio, 0.7))
    bus, _, _ = _bus([original])
    bus.start_set()
    bus.render_block()
    expected = original if crossed else replace(original)
    boundary = MixBus.BLOCK if crossed else 4 * MixBus.BLOCK
    assert not bus.replace_unheard_tail(expected, replacement, before_frame=boundary)
    np.testing.assert_allclose(bus.render_block(), 0.2)


def _current_handoff(tmp_path, *, intro=None):
    import asyncio
    from types import SimpleNamespace

    import soundfile as sf

    from autodj.stereo import SAMPLE_RATE as SR
    from tests.unit._rotation_station import song
    from tests.unit.test_prepared_playout import setup

    entries = []
    for n in range(2):
        entry = song(n, "Music", length=10)
        path = tmp_path / f"music-{n}.wav"
        sf.write(path, np.full((10 * SR, 2), 0.2, np.float32), SR, subtype="FLOAT")
        entries.append(replace(entry, path=str(path)))
    runner, source, _bridge, editions, _station = setup(tmp_path, entries)
    voice = tmp_path / "take.wav"
    sf.write(voice, np.full((SR, 2), 0.6, np.float32), SR, subtype="FLOAT")
    take = SimpleNamespace(
        take_id="d" * 32,
        asset_id="e" * 32,
        path=voice,
        seconds=1,
        placement="standalone",
        allow_outro=False,
        label="Old take",
    )
    editions.plan.handoffs = {("0", "1"): (take,)}
    asyncio.run(runner.start_edition(editions.plan.edition_id))
    current = source.player._state.queue[0]
    source.player._set_render_cursor(current, 0, "queue", from_queue=True)
    old = source.player._next_rendered()
    assert old.break_events
    if intro is not None:
        from autodj.shows import prepared_break

        frozen = source.player._render_track(
            old.entry, old.next_entry, old.start_offset, breaks=(prepared_break(take),), intro=intro
        )
        old = replace(
            old,
            audio=frozen.audio,
            break_events=frozen.break_events,
            intro_events=frozen.intro_events,
            incoming_intro=frozen.incoming_intro,
            overlap_frames=frozen.overlap_frames,
            next_start_offset=frozen.next_start_offset,
        )
    bus, _started, _positions = _bus([old])
    source.player.bus = bus
    bus.start_set()
    first = bus.render_block()
    source.player._playing_render = old
    source.player._state.current_track = old.entry
    boundary = source.player.prepared_tail_boundary(old)
    assert boundary > MixBus.BLOCK
    return runner, source, editions, old, bus, boundary, first


def test_edit_after_render_ahead_replaces_current_future_speech_and_cursor(tmp_path):
    runner, source, editions, old, bus, boundary, first = _current_handoff(tmp_path)
    editions.plan.handoffs = {}
    editions.plan.omissions = ("Old take: needs review; speech skipped",)
    runner.prepared_changed()
    new = source.player._playing_render
    assert new is not old
    assert not new.break_events
    np.testing.assert_array_equal(old.audio[:boundary], new.audio[:boundary])
    np.testing.assert_array_equal(first, new.audio[: MixBus.BLOCK])
    assert source.player._pending_entry is new.next_entry
    assert source.player._pending_offset == new.next_start_offset
    # The bus continues from the next frame; the old voiced tail is gone.
    np.testing.assert_array_equal(bus.render_block(), new.audio[MixBus.BLOCK : 2 * MixBus.BLOCK])


def test_atomic_replacement_retargets_pending_liner_cue():
    from autodj.mixbus import LinerCue

    original = _track(0.2, 6 * MixBus.BLOCK)
    replacement = replace(original, audio=original.audio.copy())
    bus, _, _ = _bus([original])
    bus.start_set()
    bus.render_block()
    cue = LinerCue(original, 2 * MixBus.BLOCK)
    bus.play_liner(np.full((MixBus.BLOCK, 2), 0.3, np.float32), 0, at=cue)
    assert bus.replace_unheard_tail(original, replacement, before_frame=4 * MixBus.BLOCK)
    assert cue.track is replacement
    # Cues fire in the block whose ending position reaches the cue frame.
    np.testing.assert_allclose(bus.render_block(), 0.5)
    assert cue.started
    np.testing.assert_allclose(bus.render_block(), 0.2)


def test_queue_held_refresh_defers_validation_to_coalesced_control_task(tmp_path, monkeypatch):
    import asyncio
    import threading

    from tests.unit.test_prepared_playout import setup

    runner, source, _bridge, editions, _station = setup(tmp_path)
    called = []

    def changed():
        assert source.player._state.queue_lock.acquire(blocking=False)
        source.player._state.queue_lock.release()
        called.append(threading.current_thread())

    monkeypatch.setattr(runner, "prepared_changed", changed)

    async def exercise():
        await runner.start_edition(editions.plan.edition_id)
        await asyncio.sleep(0)
        for task in list(runner._tasks):
            await task
        called.clear()
        with source.player._state.queue_lock:
            source.player.refresh_playout()
            source.player.refresh_playout()
            assert called == []
        await asyncio.sleep(0)
        for task in list(runner._tasks):
            await task

    asyncio.run(exercise())
    assert len(called) == 1
    assert called[0] is not threading.current_thread()


def test_second_edit_during_render_prevents_first_edit_pcm_commit(tmp_path, monkeypatch):
    runner, source, editions, old, _bus_instance, _boundary, _first = _current_handoff(tmp_path)
    render = source.player._render_track
    changed = False

    def edit_during_render(*args, **kwargs):
        nonlocal changed
        result = render(*args, **kwargs)
        if not changed:
            changed = True
            editions.plan.omissions = ("Second edit invalidates the first render",)
        return result

    monkeypatch.setattr(source.player, "_render_track", edit_during_render)
    editions.plan.handoffs = {}
    runner.prepared_changed()
    assert source.player._playing_render is old
    runner.prepared_changed()
    assert source.player._playing_render is not old
    assert not source.player._playing_render.break_events


def test_prepared_refresh_preserves_existing_frozen_matched_intro(tmp_path):
    import soundfile as sf

    from autodj.matched_intros import Chosen
    from autodj.stereo import SAMPLE_RATE as SR

    path = tmp_path / "intro.wav"
    sf.write(path, np.full((SR, 2), 0.4, np.float32), SR, subtype="FLOAT")
    intro = Chosen("f" * 32, path, "Frozen intro")
    runner, source, editions, old, _bus_instance, _boundary, _first = _current_handoff(
        tmp_path, intro=intro
    )
    assert old.intro_events
    editions.plan.handoffs = {}
    runner.prepared_changed()
    new = source.player._playing_render
    assert new is not old
    assert [event[1] for event in new.intro_events] == [intro.intro_id]
    assert new.incoming_intro is intro


def test_prepared_invalidation_discards_existing_fallback_and_rewinds_it():
    from autodj.render_ahead import RenderAhead

    old = _track(0.7, 5 * MixBus.BLOCK)
    rewound, discarded = [], []
    worker = RenderAhead(
        lambda: None,
        is_stale=lambda track: False,
        rewind=rewound.append,
        skip_past=lambda track: None,
        discard=discarded.append,
    )
    worker._fallback = old
    worker.invalidate_prepared()
    assert worker.pop() is None
    assert rewound == [old]
    assert discarded == [old]


def test_prepared_invalidation_rejects_inflight_pcm_and_rewinds_completed_track():
    from autodj.render_ahead import RenderAhead

    old = _track(0.7, 5 * MixBus.BLOCK)
    rewound, discarded = [], []
    worker = RenderAhead(
        lambda: None,
        is_stale=lambda track: False,
        rewind=rewound.append,
        skip_past=lambda track: None,
        discard=discarded.append,
    )
    worker._rendering = True
    worker.invalidate_prepared()
    worker._settle(old, worker._generation)
    assert worker.pop() is None
    assert rewound == [old]
    assert discarded == [old]


def test_boundary_crossed_after_edit_preflight_freezes_current_run_approval(tmp_path):
    """ADR-0015-08: edits do not revoke approval of already committed audio."""
    runner, source, editions, old, bus, boundary, _first = _current_handoff(tmp_path)
    # The API's early check passed, but playback advances before the save.
    assert runner.prepared_editable(editions.plan.edition_id)
    bus.seek(boundary)
    editions.plan.handoffs = {}
    runner.prepared_changed()
    assert editions.plan.handoffs == {}  # the saved edit remains effective
    assert source.player._playing_render is old
    status = runner.status()
    assert "under its previous approval" in status["committed_audio_notice"]
    assert "current committed audio continues" in status["committed_audio_notice"]
    assert not runner.prepared_editable(editions.plan.edition_id)
    # Following uncommitted PCM has no stale fallback to the old approval.
    assert source.player._playout.pop() is None
    np.testing.assert_array_equal(bus.render_block(), old.audio[boundary : boundary + MixBus.BLOCK])


def test_future_take_edit_is_allowed_while_opening_clip_is_committed(tmp_path):
    """ADR-0015-08: unrelated future approvals do not retract committed opening speech."""
    runner, _source, editions, old, bus, _, _ = _current_handoff(tmp_path)
    old.entry._prepared_clip = True
    bus.seek(MixBus.BLOCK)
    assert runner.prepared_editable(editions.plan.edition_id, take_ids={"closing"})
    assert not runner.prepared_editable(editions.plan.edition_id, take_ids={"d" * 32})


def test_song_only_overlap_does_not_lock_future_edits(tmp_path):
    runner, source, editions, old, _bus_instance, _, _ = _current_handoff(tmp_path)
    runner._on_air.prepared.handoffs = {}
    editions.plan.handoffs = {}
    source.player._playing_render = replace(old, break_events=())
    assert runner.prepared_editable(editions.plan.edition_id, take_ids={"closing"})


def test_edit_lock_uses_rendered_speech_after_approval_changes(tmp_path):
    """ADR-0015-08: newly approved current speech commits even if admission omitted it."""
    runner, _, editions, _, bus, boundary, _ = _current_handoff(tmp_path)
    runner._on_air.prepared.handoffs = {}
    bus.seek(boundary)
    assert not runner.prepared_editable(editions.plan.edition_id, take_ids={"d" * 32})
