from __future__ import annotations

import threading
import time

import numpy as np
import pytest

from autodj import live_audio
from autodj.live_audio import AudioLayer, AudioPending, BufferedFile, LiveAudio
from autodj.stereo import SAMPLE_RATE


def _stereo(value: float, frames: int) -> np.ndarray:
    return np.full((frames, 2), value, dtype=np.float32)


def test_timeline_mix_slices_music_id_and_speech_with_global_gains() -> None:
    music_a_gain = np.array([1, 0.5, 0.25, 1, 0.5, 0.25], np.float32)
    music_b_gain = np.array([0, 0, 0.5, 1, 0.5, 0], np.float32)
    id_gain = np.array([0, 1, 1, 0, 0, 0], np.float32)
    speech_gain = np.array([0, 0, 0, 1, 0.5, 0.25], np.float32)
    timeline = LiveAudio(
        6,
        (
            AudioLayer(_stereo(1, 6), 0, 6, gain=lambda at: music_a_gain[at]),
            AudioLayer(_stereo(2, 4), 2, 4, gain=lambda at: music_b_gain[at]),
            AudioLayer(_stereo(3, 2), 1, 2, gain=lambda at: id_gain[at]),
            AudioLayer(_stereo(4, 3), 3, 3, gain=lambda at: speech_gain[at]),
        ),
    )

    full = live_audio.read_audio(timeline, 0, 6)
    sliced = live_audio.read_audio(timeline[2:6], 0, 4)
    expected_mono = np.array([1, 3.5, 4.25, 7, 3.5, 1.25], np.float32)
    assert np.array_equal(full, np.repeat(expected_mono[:, None], 2, axis=1))
    assert np.array_equal(sliced, full[2:6])


def test_live_handoff_matches_offline_music_id_voice_chain_and_slices() -> None:
    from autodj.live_audio import mix_handoff, read_audio
    from autodj.playout import render_handoff

    frames = 3 * SAMPLE_RATE
    time_axis = np.arange(frames, dtype=np.float32) / frames
    outgoing = np.column_stack((0.7 * time_axis, 0.3 * time_axis)).astype(np.float32)
    incoming = np.column_stack((0.2 + 0.2 * time_axis, 0.1 + 0.1 * time_axis)).astype(np.float32)
    id_clip = _stereo(0.08, round(0.2 * SAMPLE_RATE))
    voice_clip = _stereo(0.16, round(0.25 * SAMPLE_RATE))
    chain = np.concatenate((id_clip, voice_clip))
    outgoing_points = {"start": 0.0, "outro": 0.45, "segue": 1.8, "end": 3.0}
    incoming_points = {"start": 0.1, "intro": 0.6, "end": 2.8}
    options = {
        "fade_seconds": 0.6,
        "overlap_start": 0.15,
        "liner_segue": 0.2,
        "outro_lead_limit": 0.0,
        "duck_spans": ((0.0, 0.2), (0.2, 0.45)),
        "duck_db": -8.0,
        "liner_duck_spans": ((0.2, 0.45),),
        "liner_duck_db": -5.0,
    }

    offline = render_handoff(outgoing, outgoing_points, incoming_points, liner=chain, **options)
    expected = offline.full_audio.copy()
    split = round(offline.incoming_s * SAMPLE_RATE) - offline.start_offset
    count = min(len(expected) - split, len(incoming) - offline.next_start_offset)
    head = incoming[offline.next_start_offset : offline.next_start_offset + count]
    if offline.overlay_gain is not None:
        head = head * offline.overlay_gain[:count, None]
    expected[split : split + count] += head

    live = mix_handoff(
        outgoing,
        outgoing_points,
        incoming_points,
        incoming=incoming,
        liner=chain,
        **options,
    )
    rendered = np.concatenate(
        [
            read_audio(live.full_audio, first, min(first + 8191, len(live.full_audio)))
            for first in range(0, len(live.full_audio), 8191)
        ]
    )
    assert rendered.shape == expected.shape
    difference = np.abs(rendered - expected)
    assert difference.max() <= 2e-6, (
        f"max difference {difference.max()} at {np.unravel_index(difference.argmax(), difference.shape)}"
    )
    sliced = read_audio(live.full_audio[123:10023], 0, 9900)
    assert np.allclose(sliced, expected[123:10023], atol=2e-6)


def test_overlap_underrun_preflight_consumes_no_source(monkeypatch) -> None:
    ready = {"music-a": threading.Event(), "music-b": threading.Event()}
    ready["music-a"].set()
    created: dict[str, list[_FakeDecoder]] = {"music-a": [], "music-b": []}
    made = threading.Condition()

    def factory(path: str, **kwargs) -> _FakeDecoder:
        decoder = _FakeDecoder(path, kwargs, ready[path])
        with made:
            created[path].append(decoder)
            made.notify_all()
        return decoder

    monkeypatch.setattr(live_audio, "StreamDecoder", factory)
    a = BufferedFile("music-a", 4)
    b = BufferedFile("music-b", 4)
    try:
        _wait_created(made, created, 2)
        timeline = LiveAudio(
            4,
            (AudioLayer(a, 0, 4), AudioLayer(b, 0, 4)),
        )
        with pytest.raises(AudioPending):
            timeline.read(0, 4)
        assert created["music-a"][0].read_calls == 0
        assert created["music-b"][0].read_calls == 0

        ready["music-b"].set()
        assert timeline.read(0, 4).tolist() == _stereo(3, 4).tolist()
        assert created["music-a"][0].read_calls == 1
        assert created["music-b"][0].read_calls == 1
    finally:
        a.close()
        b.close()
    assert all(decoder.closed.is_set() for decoders in created.values() for decoder in decoders)


def test_buffered_file_seeks_asynchronously_and_closes_decoders(monkeypatch) -> None:
    made = threading.Condition()
    created: list[_FakeDecoder] = []

    def factory(path: str, **kwargs) -> _FakeDecoder:
        decoder = _FakeDecoder(path, kwargs)
        with made:
            created.append(decoder)
            made.notify_all()
        return decoder

    monkeypatch.setattr(live_audio, "StreamDecoder", factory)
    deck = BufferedFile("song", 256, rate=1.5, filters="volume=0.8", start=4)
    try:
        _wait_count(made, created, 1)
        assert _wait_until(lambda: deck.ready(4, 5))
        assert np.array_equal(deck.read(4, 5), _ramp(4, 5))
        assert created[0].kwargs == {
            "start_seconds": 4 * 1.5 / SAMPLE_RATE,
            "filters": "volume=0.8",
            "playback_rate": 1.5,
        }

        assert deck.ready(100, 5) is False
        _wait_count(made, created, 2)
        assert created[0].closed.is_set()
        assert _wait_until(lambda: deck.ready(100, 5))
        assert np.array_equal(deck.read(100, 5), _ramp(100, 5))
        assert created[1].kwargs["start_seconds"] == 100 * 1.5 / SAMPLE_RATE
    finally:
        deck.close()
    assert not deck._worker.is_alive()
    assert all(decoder.closed.is_set() for decoder in created)


def test_repeating_seek_while_decoder_opens_does_not_restart_it(monkeypatch) -> None:
    made = threading.Condition()
    created: list[_FakeDecoder] = []
    opening_seek = threading.Event()
    release_seek = threading.Event()

    def factory(path: str, **kwargs) -> _FakeDecoder:
        if kwargs["start_seconds"] > 0:
            opening_seek.set()
            assert release_seek.wait(2)
        decoder = _FakeDecoder(path, kwargs)
        with made:
            created.append(decoder)
            made.notify_all()
        return decoder

    monkeypatch.setattr(live_audio, "StreamDecoder", factory)
    deck = BufferedFile("song", 256)
    try:
        _wait_count(made, created, 1)
        assert _wait_until(lambda: deck.ready(0, 4))
        assert deck.ready(100, 4) is False
        assert opening_seek.wait(2)

        # The same requested position during decoder construction must not
        # enqueue a second replacement when _position already moved to 100.
        assert deck.ready(100, 4) is False
        release_seek.set()
        assert _wait_until(lambda: deck.ready(100, 4))
        assert np.array_equal(deck.read(100, 4), _ramp(100, 4))
        time.sleep(0.02)
        assert len(created) == 2
    finally:
        release_seek.set()
        deck.close()
    assert all(decoder.closed.is_set() for decoder in created)


def test_decoder_error_is_preflighted_before_any_overlap_source_is_read(monkeypatch) -> None:
    made = threading.Condition()
    created: list[_FakeDecoder] = []

    def factory(path: str, **kwargs) -> _FakeDecoder:
        decoder = _FakeDecoder(path, kwargs)
        if path == "music-error":
            decoder._error = "broken audio"
        with made:
            created.append(decoder)
            made.notify_all()
        return decoder

    monkeypatch.setattr(live_audio, "StreamDecoder", factory)
    good = BufferedFile("music-good", 4)
    broken = BufferedFile("music-error", 4)
    try:
        _wait_count(made, created, 2)
        timeline = LiveAudio(4, (AudioLayer(good, 0, 4), AudioLayer(broken, 0, 4)))
        with pytest.raises(RuntimeError, match="broken audio"):
            timeline.read(0, 4)
        assert all(decoder.read_calls == 0 for decoder in created)
    finally:
        good.close()
        broken.close()


def test_buffered_file_surfaces_decoder_construction_failure(monkeypatch) -> None:
    entered = threading.Event()

    def factory(path: str, **kwargs):
        entered.set()
        raise OSError("decoder launch failed")

    monkeypatch.setattr(live_audio, "StreamDecoder", factory)
    deck = BufferedFile("broken-song", 32)
    try:
        assert entered.wait(2)
        assert _wait_until(lambda: deck._error is not None)
        with pytest.raises(RuntimeError, match="decoder launch failed"):
            deck.ready(0, 1)
        with pytest.raises(RuntimeError, match="decoder launch failed"):
            deck.read(0, 1)
    finally:
        with pytest.raises(RuntimeError, match="decoder launch failed"):
            deck.close()


def test_closed_buffered_file_read_is_not_silent() -> None:
    deck = BufferedFile("song", 32)
    deck.close()
    with pytest.raises(RuntimeError, match="closed"):
        deck.read(0, 1)


def test_snapshot_keeps_playing_decoder_cursor_and_intro_lift_is_slice_invariant(
    monkeypatch,
) -> None:
    made = threading.Condition()
    created = []

    class ConstantDecoder:
        def __init__(self, path, kwargs):
            self.kwargs = kwargs
            self.read_calls = 0
            self.closed = threading.Event()

        @property
        def buffered_frames(self):
            return 100000

        @property
        def eof(self):
            return False

        @property
        def error(self):
            return None

        def read(self, frames):
            self.read_calls += 1
            return _stereo(0.05, frames)

        def close(self):
            self.closed.set()

    def factory(path, **kwargs):
        decoder = ConstantDecoder(path, kwargs)
        with made:
            created.append(decoder)
            made.notify_all()
        return decoder

    monkeypatch.setattr(live_audio, "StreamDecoder", factory)
    deck = BufferedFile("song", 20000, rate=1.5, lift_db=6)
    try:
        _wait_count(made, created, 1)
        assert _wait_until(lambda: deck.ready(0, 3000))
        first = deck.read(0, 3000)
        live_decoder = created[0]
        live_calls = live_decoder.read_calls
        snapshot = deck.snapshot(100, 1200)
        assert len(created) == 2
        assert created[1].closed.is_set()
        assert deck._position == 3000
        assert live_decoder.read_calls == live_calls

        seconds = (100 + np.arange(len(snapshot))) * 1.5 / SAMPLE_RATE
        weights = np.clip((8.0 - seconds) / 7.0, 0.0, 1.0)
        expected = 0.05 * 10 ** (6 * weights / 20)
        np.testing.assert_allclose(snapshot[:, 0], expected, atol=1e-7)

        second = deck.read(3000, 3000)
        whole = BufferedFile("song", 20000, rate=1.5, lift_db=6)
        try:
            _wait_count(made, created, 3)
            assert _wait_until(lambda: whole.ready(0, 6000))
            combined = whole.read(0, 6000)
        finally:
            whole.close()
        np.testing.assert_allclose(np.concatenate((first, second)), combined, atol=1e-7)
    finally:
        deck.close()


class _FakeDecoder:
    def __init__(self, path: str, kwargs: dict, ready: threading.Event | None = None) -> None:
        self.path = path
        self.kwargs = kwargs
        self._ready = ready or threading.Event()
        self._ready.set() if ready is None else None
        self.read_calls = 0
        self.closed = threading.Event()
        output_origin = round(
            kwargs.get("start_seconds", 0) * SAMPLE_RATE / kwargs.get("playback_rate", 1)
        )
        self._next = output_origin

    @property
    def buffered_frames(self) -> int:
        return 10000 if self._ready.is_set() else 0

    @property
    def eof(self) -> bool:
        return False

    @property
    def error(self) -> None:
        return getattr(self, "_error", None)

    def read(self, frames: int) -> np.ndarray:
        self.read_calls += 1
        if self.path.startswith("music-"):
            return _stereo(1 if self.path == "music-a" else 2, frames)
        samples = np.arange(self._next, self._next + frames, dtype=np.float32)
        self._next += frames
        return np.repeat(samples[:, None], 2, axis=1)

    def close(self) -> None:
        self.closed.set()


def _wait_count(condition: threading.Condition, items: list, count: int) -> None:
    with condition:
        assert condition.wait_for(lambda: len(items) >= count, timeout=2)


def _wait_created(condition: threading.Condition, created: dict[str, list], count: int) -> None:
    with condition:
        assert condition.wait_for(lambda: sum(map(len, created.values())) >= count, timeout=2)


def _ramp(first: int, frames: int) -> np.ndarray:
    return np.repeat(np.arange(first, first + frames, dtype=np.float32)[:, None], 2, axis=1)


def _wait_until(predicate, timeout: float = 2) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.002)
    return bool(predicate())
