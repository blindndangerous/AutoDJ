"""Playable live continuations keep their source timeline while the next pick runs."""

from __future__ import annotations

import threading
import time
from dataclasses import replace

import numpy as np
import pytest

from autodj.live_audio import AudioLayer, BufferedFile, LiveAudio
from autodj.mixbus import BusEvents, MixBus, RenderedTrack
from tests.unit._break_station import make_station


class _TimelineDeck(BufferedFile):
    """Seekable fake whose samples expose source-frame position."""

    def __init__(self, frames: int, song: int) -> None:
        self.frames = frames
        self.rate = 1.0
        self.song = song

    def __len__(self) -> int:
        return self.frames

    def ready(self, _position: int, _frames: int) -> bool:
        return True

    def read(self, position: int, frames: int) -> np.ndarray:
        values = self.song * 0.2 + (position + np.arange(frames)) * 1e-8
        return np.repeat(values.astype(np.float32)[:, None], 2, axis=1)


def _wait_for(predicate, timeout: float = 2.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.005)
    return predicate()


@pytest.mark.parametrize("timing", ["timely", "late", "ended"])
def test_continuation_stays_audible_while_successor_preparation_is_blocked(
    tmp_path, monkeypatch: pytest.MonkeyPatch, timing: str
) -> None:
    """ADR-0004-12: B keeps its samples; late preparation never skips C's cue."""
    player, entries, _points, _desk = make_station(tmp_path, stream_mode=True)
    a, b, c, _d = entries
    block = MixBus.BLOCK
    total = 24 * block
    overlap = 4 * block
    decks = {entry.path: _TimelineDeck(total, i + 1) for i, entry in enumerate(entries)}
    monkeypatch.setattr(player, "_live_file", lambda entry, **_kwargs: decks[entry.path])
    monkeypatch.setattr(player, "_live_source_is_current", lambda *_args: True)

    def render(current, upcoming, offset, **_kwargs):
        sources = player._render_live_sources
        assert sources is not None
        _entry, current_source, _upcoming, incoming_source = sources
        if current is a:
            length = 12 * block
            audio = LiveAudio(
                length,
                (
                    AudioLayer(current_source, 0, length, 0),
                    AudioLayer(incoming_source, length - overlap, overlap, 0),
                ),
            )
            return RenderedTrack(
                current,
                audio,
                upcoming,
                overlap,
                "",
                overlap_frames=overlap,
                next_playback_rate=1.0,
                next_live_source=incoming_source,
            )
        if upcoming is None:
            length = total - offset
            audio = LiveAudio(length, (AudioLayer(current_source, 0, length, offset),))
            return RenderedTrack(
                current,
                audio,
                None,
                0,
                "",
                start_offset=offset,
                live_source=current_source,
            )
        length = total - offset
        audio = LiveAudio(
            length,
            (
                AudioLayer(current_source, 0, length, offset),
                AudioLayer(incoming_source, length - overlap, overlap, 0),
            ),
        )
        return RenderedTrack(
            current,
            audio,
            upcoming,
            overlap,
            "",
            start_offset=offset,
            overlap_frames=overlap,
            next_playback_rate=1.0,
            next_live_source=incoming_source,
        )

    monkeypatch.setattr(player, "_render_track", render)
    started_b = threading.Event()
    entered, release = threading.Event(), threading.Event()
    original_prepare = player._next_rendered_serial
    ad_steps = ({"step": "ad", "break_id": "scheduled-ad"},)

    def blocked_prepare():
        if player._pending_entry is b and player.bus.current_render() is not None:
            entered.set()
            assert release.wait(2)
            return replace(original_prepare(), ad_steps=ad_steps)
        return original_prepare()

    monkeypatch.setattr(player, "_next_rendered_serial", blocked_prepare)

    def on_start(track):
        if track.entry is b:
            started_b.set()
        player._on_track_start(track)

    player.bus = MixBus(
        BusEvents(on_start, player._on_position, player._take_render),
        eq_gains=lambda: (1.0, 1.0, 1.0),
    )
    player._state.queue[:] = [b, c]
    player.begin_set(a, "seed")
    player._playout.start()
    player.bus.start_set()
    try:
        assert player._playout.wait_ready(2)
        first = player._playout.ready()
        assert first is not None and first.entry is a and first.next_entry is b
        player.bus.render_block()
        assert player._playout.wait_ready(2)
        # Let A's overlap finish. B must be promoted with its live deck still active.
        for _ in range(32):
            player.bus.render_block()
            if started_b.is_set():
                break
        assert _wait_for(started_b.is_set)
        continuation = player.bus.current_render()
        assert continuation is not None and continuation.entry is b
        position = player.bus.live_position(continuation)
        before = [player.bus.render_block()[:, 0].copy() for _ in range(3)]
        expected = decks[b.path].read(overlap + position, 3 * block)[:, 0]
        np.testing.assert_array_equal(np.concatenate(before), expected)
        assert _wait_for(entered.is_set)
        assert player.bus.current_render() is continuation

        if timing != "timely":
            # Move beyond the B->C overlap boundary before allowing the plan to finish.
            while (
                player.bus.live_position(continuation) < len(continuation.audio) - overlap + block
            ):
                assert np.any(player.bus.render_block())
            if timing == "ended":
                while player.bus.current_render() is continuation:
                    player.bus.render_block()
            release.set()
            assert _wait_for(lambda: not player._live_queue_dirty)
            if timing == "late":
                installed = player._playing_render
                assert installed.next_entry is c
                assert installed.next_start_offset == 0
                assert installed.overlap_frames == 0
                # Too-late planning must not splice or air any part of C.
                remaining = len(installed.audio) - player.bus.live_position(installed)
                for _ in range(max(1, remaining // block)):
                    piece = player.bus.render_block()
                    assert np.max(piece[:, 0]) < 0.41
            assert player._playout.wait_ready(2)
            next_track = player._playout.ready()
            assert next_track.entry is c and next_track.start_offset == 0

            def plays_c():
                player.bus.render_block()
                return player._playing_render.entry is c

            assert _wait_for(plays_c)
        else:
            # B keeps advancing at source-frame speed while C is selected and rendered.
            assert player.bus.live_position(continuation) == position + 3 * block
            release.set()
            assert _wait_for(
                lambda: (
                    player._playing_render is not None
                    and player._playing_render.entry is b
                    and player._playing_render.next_entry is c
                )
            )
            current = player._playing_render
            assert current is not None
            assert current.ad_steps == ad_steps
            assert current.handoff_id == continuation.handoff_id
            assert current.handoff_id
            # The splice retains B's already-heard prefix and continues its source timeline.
            position = player.bus.live_position(current)
            actual = np.concatenate([player.bus.render_block() for _ in range(3)])
            expected = decks[b.path].read(overlap + position, 3 * block)
            np.testing.assert_array_equal(actual, expected)
    finally:
        release.set()
        player._playout.stop(timeout=2)
        player.end_set()
