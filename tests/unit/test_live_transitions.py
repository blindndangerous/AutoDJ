from types import SimpleNamespace

import numpy as np

from autodj import live_transitions
from autodj.live_audio import LiveAudio
from autodj.live_transitions import build_live_transition
from autodj.mixbus import read_audio
from autodj.player import Player, _FxPlan


class _Deck:
    def __init__(self, frames: int) -> None:
        self.frames = frames
        self.filters = ""


class _Glide:
    def __init__(self, origin: int, source_end: int, played: int = 8_820) -> None:
        self.origin, self.source_end, self.played = origin, source_end, played
        self.ratio, self.semitones = 1.05, 0.0
        self.ramp_frames, self.beats, self.entry = 1_000, (), 0

    def fits(self, length: int) -> bool:
        return self.source_end + 10_000 < length

    def head(self, audio: np.ndarray, samples: int) -> np.ndarray:
        return (audio[:samples] * np.float32(0.8)).copy()

    def continuation(self, audio: np.ndarray) -> np.ndarray:
        return (audio[self.origin : self.source_end] * np.float32(0.8)).copy()


def test_live_transition_without_next_streams_only_remaining_body() -> None:
    entry = SimpleNamespace(path="track.wav", length=10.0)
    deck = _Deck(441_000)
    player = SimpleNamespace(
        _live_file=lambda _entry: deck,
        music_playback_rate=lambda _entry: 1.1,
        saved_points=lambda _entry: {"start": 0, "end": 10},
        _peek_incoming_meta=lambda _entry: None,
        _cfg=SimpleNamespace(playback=SimpleNamespace(loudness_processing="off")),
    )

    rendered = build_live_transition(player, entry, None, 44_100)

    assert rendered is not None
    assert isinstance(rendered.audio, LiveAudio)
    assert rendered.audio.frames == 360_818
    assert len(rendered.audio.layers) == 1
    layer = rendered.audio.layers[0]
    assert layer.source is deck
    assert (layer.start, layer.length, layer.offset) == (0, 360_818, 40_091)
    assert rendered.full_audio is rendered.audio


def test_live_transition_mixes_only_bounded_tail(monkeypatch) -> None:
    """ADR-0004-10: live transition effects inspect only a bounded tail window."""
    current = SimpleNamespace(path="out.wav", length=10.0)
    upcoming = SimpleNamespace(path="in.wav", length=10.0)
    outgoing_deck, incoming_deck = _Deck(441_000), _Deck(441_000)
    djmix = SimpleNamespace(
        outro_intro_align=False,
        phrase_align=False,
        beatmatch=False,
        drop_mix=False,
        key_shift=False,
    )
    player = SimpleNamespace(
        _live_file=lambda entry: outgoing_deck if entry is current else incoming_deck,
        music_playback_rate=lambda _entry: 1.0,
        _peek_incoming_meta=lambda _entry: None,
        _cfg=SimpleNamespace(
            playback=SimpleNamespace(transition_mode="fixed", loudness_processing="off"),
            djmix=djmix,
        ),
        _effective_crossfade_seconds=lambda *_args: 0.2,
        _skip_incoming_silence_samples=lambda *_args: 0,
        _crossfade_start_in_a=lambda *_args: 300_000,
        _drop_mix=lambda *_args, **_kwargs: None,
        _beatmatch_plan=lambda *_args, **_kwargs: SimpleNamespace(ratio=1.0, semitones=0),
        _guard_vocals=lambda _a, _b, _ma, _match, **kwargs: (
            kwargs["earliest"] + 300_000,
            kwargs["crossfade"],
        ),
        _with_key_shift=lambda match, *_args: match,
        _is_liner=lambda _entry: False,
        _fx_plan=lambda *_args: _FxPlan(name="none"),
        _MIN_FX_DURATION_S={"none": 0.0},
        _apply_outgoing_filter_sweep=lambda audio, *_args: audio,
        _apply_transition_effect=lambda a, b, *_args, **_kwargs: (
            a,
            b,
            np.zeros((0, 2), np.float32),
            "none",
        ),
        _mix_overlap=lambda _a, _b, frames, _rate, _extra: np.zeros((frames, 2), np.float32),
        _incoming_glide=lambda *_args: None,
    )
    monkeypatch.setattr(
        live_transitions,
        "_read_window",
        lambda _player, _entry, _deck, first, last: np.zeros((last - first, 2), np.float32),
    )

    rendered = build_live_transition(player, current, upcoming, 0)

    assert rendered is not None
    assert isinstance(rendered.audio, LiveAudio)
    assert rendered.audio.frames == 308_820
    assert len(rendered.audio.layers) == 2
    body, overlap = rendered.audio.layers
    assert (body.source, body.start, body.length, body.offset) == (outgoing_deck, 0, 300_000, 0)
    assert isinstance(overlap.source, np.ndarray)
    assert (overlap.start, overlap.length) == (300_000, 8_820)
    assert rendered.next_start_offset == 8_820


def _parity_player(monkeypatch, audio, rate, fx_name, incoming_glide):
    current, upcoming = (
        SimpleNamespace(path="out.wav", length=8.0),
        SimpleNamespace(path="in.wav", length=8.0),
    )
    decks = {
        current.path: _Deck(len(audio[current.path])),
        upcoming.path: _Deck(len(audio[upcoming.path])),
    }
    rates = {current.path: rate, upcoming.path: 1.0}
    cfg = SimpleNamespace(
        playback=SimpleNamespace(
            transition_mode="fixed",
            loudness_processing="off",
            crossfade_seconds=0.2,
            crossfade_eq_duck=False,
        ),
        djmix=SimpleNamespace(
            outro_intro_align=False,
            phrase_align=False,
            phrase_bars=4,
            beatmatch=False,
            drop_mix=False,
            key_shift=False,
            filter_sweep=False,
        ),
        transitions=SimpleNamespace(wet_mix=1.0),
    )
    player = object.__new__(Player)
    player._cfg = cfg
    player._live_file = lambda entry: decks[entry.path]
    player._playout_audio = lambda entry, _decode: audio[entry.path]
    player._load_incoming = lambda entry, *_args: audio[entry.path]
    player.music_playback_rate = lambda entry: rates[entry.path]
    player._ensure_dj_cache = lambda: None
    player._outgoing_meta = lambda *_args: None
    player._peek_incoming_meta = lambda _entry: None
    player._effective_crossfade_seconds = lambda *_args: 0.2
    player._skip_incoming_silence_samples = lambda *_args, **_kwargs: 0
    player._drop_mix = lambda *_args, **_kwargs: None
    player._beatmatch_plan = lambda *_args, **_kwargs: SimpleNamespace(
        ratio=1.05 if incoming_glide is not None else 1.0,
        semitones=0,
        ramp_frames=1_000,
        beats=(),
    )
    player._guard_vocals = lambda _a, _b, _meta, _match, **kwargs: (
        kwargs["length"] - kwargs["crossfade"],
        kwargs["crossfade"],
    )
    player._with_key_shift = lambda match, *_args: match
    player._incoming_glide = lambda *_args: incoming_glide
    player._is_liner = lambda _entry: False
    player._fx_plan = lambda *_args: _FxPlan(name=fx_name)
    player._apply_outgoing_filter_sweep = lambda segment, *_args: segment
    player._apply_transition_effect = _parity_effect
    player._mix_overlap = _parity_mix
    player._live_files = {}
    player._cfg = cfg

    window_requests = []

    def read_window(_player, entry, _deck, first, last):
        window_requests.append((entry.path, first, last))
        return audio[entry.path][first:last].copy()

    monkeypatch.setattr(live_transitions, "_read_window", read_window)
    return player, current, upcoming, window_requests


def _parity_effect(a, b, b_head, _sr, crossfade, *, plan=None):
    fx = plan.name
    extra = np.zeros((len(a), 2), dtype=np.float32)
    if fx == "none":
        return a, b_head, extra, fx
    effect_size = min(len(a), max(crossfade, 3 * 44_100))
    tail = a[-effect_size:] * np.float32(0.5)
    head = b[:effect_size] * np.float32(0.75)
    return np.concatenate([a[:-effect_size], tail]), head[:crossfade], extra, fx


def _parity_mix(a, b, crossfade, _sr, extra):
    from autodj.player import _apply_crossfade

    mixed = _apply_crossfade(a, b, crossfade)
    mixed += extra
    return mixed


def test_live_transition_matches_offline_reference_for_rate_glide_and_effect(monkeypatch) -> None:
    from autodj.player import resample_music

    rng = np.random.default_rng(7)
    audio = {
        "out.wav": rng.normal(0, 0.1, (8 * 44_100, 2)).astype(np.float32),
        "in.wav": rng.normal(0, 0.1, (8 * 44_100, 2)).astype(np.float32),
    }
    incoming = _Glide(0, 22_050)  # deterministic stand-in for the vocoder plan

    for rate, effect, incoming_plan, current_glide in (
        (1.0, "none", None, None),
        (1.08, "none", None, None),
        (1.0, "none", incoming, None),
        (1.0, "none", None, _Glide(22_050, 44_100, played=0)),
        (1.0, "echo_out", None, None),
    ):
        player, current, upcoming, requests = _parity_player(
            monkeypatch, audio, rate, effect, incoming_plan
        )
        live_deck = resample_music(audio[current.path], rate)
        player._live_file = (
            lambda entry, decks={current.path: live_deck, upcoming.path: audio[upcoming.path]}: (
                decks[entry.path]
            )
        )
        live = build_live_transition(
            player, current, upcoming, 22_050 if current_glide else 0, current_glide
        )
        offline = Player._render_decoded(
            player,
            current,
            upcoming,
            22_050 if current_glide else 0,
            glide_in=current_glide,
            liners=(),
            source_decode=None,
            incoming_decode=None,
        )

        assert live is not None and offline is not None
        expected = offline.audio
        actual = read_audio(live.audio, 0, len(live.audio))
        np.testing.assert_allclose(
            actual,
            expected,
            rtol=0,
            atol=1e-5,
            err_msg=f"rate={rate}, effect={effect}, incoming_glide={incoming_plan is not None}, current_glide={current_glide is not None}",
        )
        assert live.next_start_offset == offline.next_start_offset
        assert live.next_glide == offline.next_glide
        assert live.overlap_frames == offline.overlap_frames
        assert live.transition_fx == offline.transition_fx
        assert requests
        assert all(last - first < len(audio[path]) for path, first, last in requests)


def test_live_transition_preserves_liner_rate_special_case(monkeypatch) -> None:
    from autodj.player import resample_music

    rng = np.random.default_rng(19)
    audio = {
        "out.wav": rng.normal(0, 0.1, (8 * 44_100, 2)).astype(np.float32),
        "in.wav": rng.normal(0, 0.1, (8 * 44_100, 2)).astype(np.float32),
    }
    player, current, upcoming, _requests = _parity_player(
        monkeypatch, audio, 1.08, "echo_out", None
    )
    player._is_liner = lambda entry: entry is current
    player.music_playback_rate = lambda entry: 1.0 if entry is current else 1.08
    player._live_file = lambda entry: (
        audio[current.path] if entry is current else resample_music(audio[upcoming.path], 1.08)
    )

    live = build_live_transition(player, current, upcoming, 0)
    offline = Player._render_decoded(
        player,
        current,
        upcoming,
        0,
        glide_in=None,
        liners=(),
        source_decode=None,
        incoming_decode=None,
    )

    assert live is not None and offline is not None
    np.testing.assert_allclose(
        read_audio(live.audio, 0, len(live.audio)), offline.audio, rtol=0, atol=1e-6
    )
    assert live.transition_fx == "none"
    assert live.playback_rate == 1.0
    assert live.next_playback_rate == 1.08


def test_player_dispatches_live_non_saved_render_without_decoding_whole_track(monkeypatch) -> None:
    """ADR-0004-10: the live transition worker path avoids whole-track decoding."""
    player = object.__new__(Player)
    player.live_mixing = True
    player._cfg = SimpleNamespace(playback=SimpleNamespace(transition_mode="fixed"))
    expected = object()
    monkeypatch.setattr(live_transitions, "build_live_transition", lambda *args: expected)

    rendered = Player._render_decoded(
        player,
        SimpleNamespace(path="out.wav"),
        SimpleNamespace(path="in.wav"),
        1_234,
        glide_in=None,
        liners=(),
        source_decode=None,
        incoming_decode=None,
    )

    assert rendered is expected
