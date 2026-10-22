from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

from autodj.breaks import BreakCues
from autodj.config import PlaybackConfig
from autodj.live_audio import BufferedFile, LiveAudio
from autodj.mixbus import BusEvents, MixBus
from autodj.player import Player
from autodj.stereo import SAMPLE_RATE
from tests.unit._fakes import make_cfg_mock, make_entry, make_sim_index


class _UnconsumedDeck(BufferedFile):
    def __init__(self, frames: int, value: float) -> None:
        self.frames = frames
        self.rate = 1.0
        self.value = value
        self.read_calls = 0

    def ready(self, position: int, frames: int) -> bool:
        return True

    def read(self, position: int, frames: int) -> np.ndarray:
        self.read_calls += 1
        return np.full((frames, 2), self.value, dtype=np.float32)


class _CursorDeck(BufferedFile):
    """Tiny seekable fake with the same single-cursor readiness contract."""

    def __init__(self, frames: int, start: int = 0) -> None:
        self.frames = frames
        self.rate = 1.0
        self.position = start

    def ready(self, position: int, _frames: int) -> bool:
        if position != self.position:
            self.position = position
            return False
        return True

    def read(self, position: int, frames: int) -> np.ndarray:
        assert position == self.position
        self.position += frames
        return np.zeros((frames, 2), np.float32)


def test_stream_saved_track_planning_does_not_consume_either_song(monkeypatch) -> None:
    music, upcoming, station_id, voice = (make_entry(i) for i in range(4))
    music.length = upcoming.length = 4.0
    station_id.length, voice.length = 0.2, 0.25
    cfg = make_cfg_mock()
    cfg.playback = PlaybackConfig(
        transition_mode="saved_points",
        crossfade_seconds=0.6,
        loudness_processing="off",
        liners_duck_db=-6.0,
        pick_mode="random",
    )
    player = Player(cfg, make_sim_index(2), dry_run=True, stream_mode=True)
    points = {
        music.path: {"start": 0.0, "outro": 0.4, "segue": 2.8, "end": 4.0},
        upcoming.path: {"start": 0.0, "intro": 0.5, "segue": 3.5, "end": 4.0},
        station_id.path: {"start": 0.0, "segue": 0.15, "end": station_id.length},
        voice.path: {"start": 0.0, "segue": 0.2, "end": voice.length},
    }
    player._library = SimpleNamespace(
        catalog=SimpleNamespace(
            points=lambda path: points[path],
            record_for_path=lambda _path: {"allow_outro": True},
        ),
        entries_snapshot=lambda kind="music": (
            (station_id, voice) if kind == "liner" else (music, upcoming)
        ),
    )
    decks = {
        music.path: _UnconsumedDeck(round(music.length * SAMPLE_RATE), 0.7),
        upcoming.path: _UnconsumedDeck(round(upcoming.length * SAMPLE_RATE), 0.2),
    }
    monkeypatch.setattr(player, "_live_file", lambda entry, **_kwargs: decks[entry.path])
    liner_audio = {
        station_id.path: np.full((round(station_id.length * SAMPLE_RATE), 2), 0.05, np.float32),
        voice.path: np.full((round(voice.length * SAMPLE_RATE), 2), 0.1, np.float32),
    }
    monkeypatch.setattr(player, "_load_playout_audio", lambda entry: liner_audio[entry.path])

    rendered = player._render_saved_track(
        music,
        upcoming,
        0,
        liners=((station_id, False), (voice, False)),
    )

    assert rendered is not None
    assert isinstance(rendered.full_audio, LiveAudio)
    assert [event[1:] for event in rendered.liner_events] == [
        (station_id, False),
        (voice, False),
    ]
    assert rendered.liner_events[1][0] - rendered.liner_events[0][0] == round(
        station_id.length * SAMPLE_RATE
    )
    assert decks[music.path].read_calls == 0
    assert decks[upcoming.path].read_calls == 0


def test_stream_saved_track_offset_stops_at_incoming_end_cue(monkeypatch) -> None:
    music, upcoming = (make_entry(i) for i in range(2))
    music.length = upcoming.length = 4.0
    cfg = make_cfg_mock()
    cfg.playback = PlaybackConfig(
        transition_mode="saved_points",
        crossfade_seconds=0.6,
        loudness_processing="off",
        pick_mode="random",
    )
    player = Player(cfg, make_sim_index(2), dry_run=True, stream_mode=True)
    points = {
        music.path: {"start": 0.0, "outro": 2.4, "segue": 2.8, "end": 4.0},
        upcoming.path: {"start": 0.0, "intro": 0.0, "segue": 3.5, "end": 0.1},
    }
    player._library = SimpleNamespace(
        catalog=SimpleNamespace(
            points=lambda path: points[path],
            record_for_path=lambda _path: None,
        ),
        entries_snapshot=lambda _kind="music": (),
    )
    decks = {
        music.path: _UnconsumedDeck(round(music.length * SAMPLE_RATE), 0.7),
        upcoming.path: _UnconsumedDeck(round(upcoming.length * SAMPLE_RATE), 0.2),
    }
    monkeypatch.setattr(player, "_live_file", lambda entry, **_kwargs: decks[entry.path])

    rendered = player._render_saved_track(music, upcoming, 0)

    assert rendered is not None
    assert rendered.next_start_offset == round(0.1 * SAMPLE_RATE)


def _live_prepared_speech_setup(monkeypatch):
    from autodj._bridge import PlayerBridge

    music, old_next, new_next, following = (make_entry(i) for i in range(4))
    for entry in (music, old_next, new_next, following):
        entry.length = 4.0
    cfg = make_cfg_mock()
    cfg.playback = PlaybackConfig(
        transition_mode="fixed",
        crossfade_seconds=0.6,
        loudness_processing="off",
        pick_mode="random",
        liners_duck_db=0.0,
    )
    sim = make_sim_index(4)
    player = Player(cfg, sim, dry_run=True, stream_mode=True)
    points = {
        music.path: {"start": 0.0, "outro": 1.5, "segue": 2.5, "end": 4.0},
        old_next.path: {"start": 0.0, "intro": 0.0, "segue": 3.5, "end": 4.0},
        new_next.path: {"start": 0.0, "intro": 2.0, "segue": 3.5, "end": 4.0},
        following.path: {"start": 0.0, "intro": 0.0, "segue": 3.5, "end": 4.0},
    }
    player._library = SimpleNamespace(
        catalog=SimpleNamespace(
            points=lambda path: points[path],
            record_for_path=lambda _path: None,
        ),
        entries_snapshot=lambda _kind="music": (),
    )
    decks = {
        entry.path: _UnconsumedDeck(round(entry.length * SAMPLE_RATE), value)
        for entry, value in ((music, 0.1), (old_next, 0.2), (new_next, 0.4), (following, 0.6))
    }
    monkeypatch.setattr(player, "_live_file", lambda entry, **_kwargs: decks[entry.path])
    speech = np.linspace(0.2, 0.8, round(0.6 * SAMPLE_RATE), dtype=np.float32)
    speech = np.repeat(speech[:, None], 2, axis=1)
    voice = SimpleNamespace(
        break_id="prepared-test-break",
        kind="dj",
        source="prepared_edition",
        keep_file=False,
        path=Path("prepared-voice.wav"),
        limit_s=0.6,
        cues=BreakCues(0.0, 0.6, 0.6, 0.6),
        placement="talk_up",
        allow_outro=True,
    )
    player._load_break_audio = lambda _item: speech
    player.break_desk = SimpleNamespace(
        config=SimpleNamespace(
            dj_placement="talk_up", min_intro_seconds=0.5, max_seconds=0.6, duck_db=0.0
        ),
        mixed_as=lambda *_args: None,
        get=lambda _identity: None,
    )
    rendered = player._render_track(music, old_next, 0, breaks=(voice,))
    assert rendered is not None and len(rendered.break_events) == 1
    player.bus = MixBus(
        BusEvents(player._on_track_start, player._on_position, player._take_render),
        eq_gains=lambda: (1.0, 1.0, 1.0),
    )
    player._playing_render = rendered
    player._state.current_track = music
    player.bus._playing = True
    player.bus._current = rendered
    return (
        player,
        PlayerBridge(player=player, sim=sim),
        music,
        old_next,
        new_next,
        following,
        voice,
        speech,
    )


def test_live_queue_keep_renders_prepared_break_on_new_song_with_fixed_mode(monkeypatch) -> None:
    player, bridge, music, _old_next, new_next, _following, voice, _speech = (
        _live_prepared_speech_setup(monkeypatch)
    )
    assert player._cfg.playback.transition_mode == "fixed"
    assert player._playing_render is not None
    assert player._playing_render.next_entry.path != new_next.path

    from autodj.live_queue import VoiceQueueConflict

    with pytest.raises(VoiceQueueConflict) as caught:
        bridge.queue_add(new_next.path)
    assert bridge.queue_add(
        new_next.path,
        voice_action="keep",
        voice_revision=caught.value.detail["revision"],
    )

    player._refresh_live_queue()
    kept = player._playing_render
    assert kept is not None and kept.entry.path == music.path
    assert kept.next_entry is not None and kept.next_entry.path == new_next.path
    assert len(kept.break_events) == 1
    (planned,) = kept.voice_breaks
    assert planned.source == "operator_keep"
    assert planned.keep_file is True
    assert planned.cues == voice.cues


def test_live_queue_keep_preserves_started_speech_and_changes_incoming_song(monkeypatch) -> None:
    player, bridge, _music, _old_next, new_next, following, _voice, _speech = (
        _live_prepared_speech_setup(monkeypatch)
    )
    old = player._playing_render
    assert old is not None
    old_voice_start = old.break_events[0][0]
    boundary = old_voice_start + round(0.2 * SAMPLE_RATE)
    assert boundary < len(old.audio) - old.overlap_frames
    player.bus._pos = boundary

    from autodj.live_queue import VoiceQueueConflict

    with pytest.raises(VoiceQueueConflict) as caught:
        bridge.queue_add(new_next.path)
    assert bridge.queue_add(
        new_next.path,
        voice_action="keep",
        voice_revision=caught.value.detail["revision"],
    )
    player._refresh_live_queue()

    edited = player._playing_render
    assert edited is not None and edited.next_entry is not None
    assert edited.next_entry.path == new_next.path
    (planned,) = edited.voice_breaks
    assert planned.source == "operator_keep"
    assert planned.keep_file is True
    assert edited.break_events == old.break_events
    assert edited.overlap_frames == old.overlap_frames
    voice_end = old.break_events[0][2]
    np.testing.assert_array_equal(
        edited.audio.read(boundary, voice_end - boundary),
        old.audio.read(boundary, voice_end - boundary),
    )
    incoming_boundary = len(old.audio) - old.overlap_frames
    assert incoming_boundary > boundary
    assert not np.array_equal(
        edited.audio.read(incoming_boundary, 1), old.audio.read(incoming_boundary, 1)
    )

    # A second edit must still see the explicit keep decision, and an
    # already-started voice must remain committed while its successor changes.
    assert bridge.queue_add(following.path)
    with pytest.raises(VoiceQueueConflict) as repeated:
        bridge.queue_remove(new_next.path)
    assert bridge.queue_remove(
        new_next.path,
        voice_action="keep",
        voice_revision=repeated.value.detail["revision"],
    )
    player._refresh_live_queue()
    edited_again = player._playing_render
    assert edited_again is not None and edited_again.next_entry is not None
    assert edited_again.next_entry.path == following.path
    (planned_again,) = edited_again.voice_breaks
    assert planned_again.source == "operator_keep"
    assert planned_again.keep_file is True


def test_live_decks_follow_queue_occurrences_across_duplicate_and_returning_tracks(
    monkeypatch,
) -> None:
    import autodj.player as player_module
    from autodj.live_audio import AudioLayer
    from autodj.mixbus import RenderedTrack

    player, _bridge, a, b, _c, _d, _voice, _speech = _live_prepared_speech_setup(monkeypatch)
    original_live_file = Player._live_file.__get__(player, Player)
    monkeypatch.setattr(player, "_live_file", original_live_file)
    monkeypatch.setattr(
        player_module,
        "BufferedFile",
        lambda _path, frames, **kwargs: _CursorDeck(frames, kwargs.get("start", 0)),
    )
    picks = iter((a, b, a))
    monkeypatch.setattr(player, "_pick_context", lambda **_kwargs: SimpleNamespace(at=0.0))
    monkeypatch.setattr(player, "_choose_next", lambda *_args: (next(picks), "queue"))
    monkeypatch.setattr(player, "planned_transition", lambda *_args: None)

    def render(current, upcoming, offset, **_kwargs):
        outgoing = player._render_live_file(current, incoming=False)
        incoming = player._render_live_file(upcoming, incoming=True)
        audio = LiveAudio(
            256,
            (
                AudioLayer(outgoing, 0, 256, 100),
                AudioLayer(incoming, 0, 256, 0),
            ),
        )
        return RenderedTrack(
            current,
            audio,
            upcoming,
            128,
            "",
            start_offset=offset,
            overlap_frames=128,
            full_audio=audio,
        )

    monkeypatch.setattr(player, "_render_track", render)
    player._set_render_cursor(a, 0, "seed")

    aa = player._next_rendered_serial()
    assert aa is not None and aa.entry is a and aa.next_entry is a
    assert aa.live_source is not aa.next_live_source
    for _ in range(4):
        if aa.audio.ready(0, 1):
            break
    assert aa.audio.ready(0, 1)

    player._skip_past_render(aa)
    ab = player._next_rendered_serial()
    assert ab is not None and ab.entry is a and ab.next_entry is b
    assert ab.live_source is aa.next_live_source

    player._skip_past_render(ab)
    ba = player._next_rendered_serial()
    assert ba is not None and ba.entry is b and ba.next_entry is a
    assert ba.live_source is ab.next_live_source
    assert ba.next_live_source not in {aa.live_source, aa.next_live_source}
