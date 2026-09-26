# Radio Stream Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Serve the live AutoDJ set as a stereo MP3 radio stream at a secret URL that Sonos, VLC and other players can open, with AutoDJ's crossfades, effects, EQ, liners and track titles.

**Architecture:** The server-side playback path is split into a stereo track renderer (existing `Player` logic, now returning audio instead of playing it), a clock-paced mix bus (`src/autodj/mixbus.py`) that applies EQ, liners, pause and skip and fans 20 ms blocks out to outputs, and two outputs: a long-lived sound-card output and a stream output (`src/autodj/stream.py`) that encodes once with ffmpeg and fans the bytes out to HTTP listeners with ICY titles. A station controller (`src/autodj/station.py`) starts a set on the first listener and stops it 30 seconds after the last one leaves. The web page becomes a remote with a "Listen here" button.

**Tech Stack:** Python 3.14, NumPy, SciPy (`sosfilt`), librosa (resample, time stretch), soundfile, sounddevice, FastAPI/Starlette `StreamingResponse`, ffmpeg subprocess, vanilla ES modules, vitest with happy-dom, pytest.

**Spec:** `docs/superpowers/specs/2026-09-26-radio-stream-design.md` (read it before starting any task).

## Global Constraints

- Mix format: stereo `float32`, shape `(frames, 2)`, sample rate 44100 Hz everywhere after loading.
- Mix bus block: 882 frames (20 ms).
- Stream bitrates allowed: 128, 192, 256, 320 kbps; default 320.
- `idle_grace_seconds` default 30; `max_listeners` default 8; `station_name` default "AutoDJ".
- Listener queue bound: 5 seconds of encoded audio; start burst: 2 seconds, starting on an MP3 frame boundary.
- ICY: `icy-metaint: 16000`; title format `Artist - Title`; one metadata block at most 4080 bytes.
- Stream secret: 32 random bytes, URL-safe base64 without padding (43 characters), file `<index_dir>/.stream-secret`, never in `config.toml`, never equal to the access token.
- Encoder restart limit: 5 failures in 60 seconds, then 60 seconds stopped with 503 "stream encoder failed".
- ffmpeg is always started with an argument list, never a shell.
- Browser mode (no `--stream`) must behave exactly as before; every existing test must keep passing.
- Coverage gates: line ≥ 99.1%, branch ≥ 94.7% (`uv run --frozen python scripts/ci_pytest.py`). Only `# pragma: no cover` on real hardware paths (sounddevice callbacks, real ffmpeg process plumbing that the end-to-end test covers when ffmpeg exists).
- Every new public function, class and method has a docstring (interrogate is at 100%).
- Screen-reader rules: announce each state change once; never announce on the 1 Hz tick; visible text for listener count, not a live region.
- Never run `uv`, `npm` or git write commands in `Z:\Scripts\autodj` (live NAS deployment). Work only in the feature worktree, with `UV_PROJECT_ENVIRONMENT` pointing at a scratchpad venv.
- Commits: Conventional Commits, signed, one per task step group as shown; body ends with `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`. Commit hooks are the quick set; run the full suite at the end of every task.

## Review Focus

- A track shorter than the crossfade, or a zero-length / silent file, mid-set: expected to be skipped or cut without the mix bus stalling or the stream going silent forever. Test added to Task 3.
- A listener that connects and immediately disconnects repeatedly (Sonos probing the URL before playing): expected not to start and stop a new set each time within the grace period, and not to leak listener queues. Test added to Task 10.
- A liner file that cannot be decoded (corrupt, unsupported codec): expected to be skipped with a log line while the music carries on. Test added to Task 6.
- The stream secret file is missing, empty, unreadable or truncated at start-up: expected to be recreated (missing/empty/invalid) or to fail start-up with a clear message (unreadable), never to serve with an empty secret that matches `/stream/.mp3`. Test added to Task 8.
- Titles with quotes, semicolons, non-ASCII or over-long text: expected to reach the player readable and never corrupt the ICY framing. Test added to Task 9.

---

## Spec deviations discovered while planning (already reflected in the tasks)

The interface survey found facts that the spec did not state. The tasks handle them as follows:

1. The current server-audio loop replays the incoming track's crossfade head and skipped intro on the next pass, because no offset is carried between tracks. The renderer (Task 3) carries `start_offset` so every sample plays once.
2. `load_audio` returns the file's native rate on the soundfile path. The stereo loader (Task 1) always resamples to 44100 Hz.
3. Liner triggers are evaluated only in the browser today. Task 6 adds a server-side liner scheduler using the existing `LinerTrigger` and `LinerLibrary`.
4. There is no general request rate limiter. Task 10 uses a second `PairingRateLimiter` instance for wrong stream secrets.
5. Server-audio mode never updates `/api/history`. Task 11 records tracks into the bridge history from the mix bus in both server-mixed modes.
6. `ENVIRONMENT_OVERLAY` has no boolean converter. Task 7 adds one.

---

## File Structure

Create:
- `src/autodj/stereo.py` — stereo helpers: `to_stereo`, `per_channel`, `envelope`, `load_stereo`.
- `src/autodj/mixbus.py` — `MixBus`, `RenderedTrack`, `Output` protocol, `SystemClock`.
- `src/autodj/sound_output.py` — `SoundDeviceOutput` with ring buffer.
- `src/autodj/liner_scheduler.py` — `LinerScheduler`, `decode_liner`.
- `src/autodj/stream_secret.py` — `StreamSecret` load/create/rotate/verify, `paired_devices_path`, `stream_secret_path`.
- `src/autodj/icy.py` — `IcyInterleaver`, `format_stream_title`, `mp3_frame_offset`.
- `src/autodj/stream.py` — `StreamOutput`, `Listener`, `EncoderProcess`, `EncoderSupervisor`.
- `src/autodj/station.py` — `Station` (listener lifecycle, idle timer, set start/stop).
- `src/autodj/static/modules/stream-mode.js` — page behaviour in stream mode.
- Tests: `tests/unit/test_stereo.py`, `tests/unit/test_transitions_stereo.py`, `tests/unit/test_renderer.py`, `tests/unit/test_mixbus.py`, `tests/unit/test_sound_output.py`, `tests/unit/test_liner_scheduler.py`, `tests/unit/test_stream_config.py`, `tests/unit/test_stream_secret.py`, `tests/unit/test_icy.py`, `tests/unit/test_stream_output.py`, `tests/unit/test_station.py`, `tests/integration/test_stream_server.py`, `tests/integration/test_stream_ffmpeg.py`, `tests/jsmodules/stream-mode.test.js`.

Modify:
- `src/autodj/player.py` — stereo-safe DSP functions, `_render_track`, run loop uses the mix bus when not dry-run.
- `src/autodj/transitions.py` — `apply_transition(..., seed=None)` stereo wrapper; seeded randomness.
- `src/autodj/config.py` — `StreamConfig`, `[stream]` table, bool env converter.
- `src/autodj/cli.py` — `--stream/--no-stream`, ffmpeg check.
- `src/autodj/doctor.py` — `_stream_check`.
- `src/autodj/_bridge.py` — stream fields in `get_state`, stream-mode skip/pause/seek, history from mix bus, liner test into the bus, bitrate persistence.
- `src/autodj/runtime_state.py` — persist `stream_bitrate`.
- `src/autodj/security.py` — `/stream/` public prefix.
- `src/autodj/server.py` — stream routes, wiring, lifespan teardown, `paired_devices_path` use.
- `src/autodj/static/index.html`, `app.js`, `modules/hotkeys.js`, `app.css` — Listen here, Stream settings section, seek hidden, delayed lyric clock.
- `config.toml.example`, `compose.yaml`, `scripts/container_smoke.sh`, `docs/operations.md`, `README.md`, `THREAT_MODEL.md`, `CHANGELOG.md`.
- `tests/integration/_helpers.py` — real `StreamConfig` on the player mock.

---

### Task 1: Stereo helpers and stereo-safe DSP primitives

**Files:**
- Create: `src/autodj/stereo.py`
- Modify: `src/autodj/player.py` (`_apply_crossfade` 97-141, `_apply_crossfade_ducked` 150-223, `_time_stretch` 231-252, `apply_filter_sweep` 290-357, `make_eq_state` 400-418, `apply_eq` 438-end)
- Test: `tests/unit/test_stereo.py`

**Interfaces:**
- Produces:
  - `stereo.SAMPLE_RATE: int = 44100`
  - `stereo.to_stereo(audio: np.ndarray) -> np.ndarray` — `(n,)` → `(n, 2)` duplicate; `(n, 2)` unchanged; `(n, k>2)` → first two channels; always `float32`, C-contiguous.
  - `stereo.per_channel(fn: Callable[[np.ndarray], np.ndarray], audio: np.ndarray) -> np.ndarray` — 1-D passes straight through; 2-D applies `fn` to each column and stacks with `np.stack(..., axis=1)`; every column is trimmed or zero-padded to the first column's output length.
  - `stereo.envelope(env: np.ndarray, like: np.ndarray) -> np.ndarray` — returns `env` for 1-D `like`, `env[:, None]` for 2-D.
  - `stereo.load_stereo(path: str, target_sr: int = SAMPLE_RATE) -> np.ndarray` — `(n, 2)` float32 at `target_sr`.
  - `stereo.mono(audio: np.ndarray) -> np.ndarray` — `audio.mean(axis=1)` for 2-D, identity for 1-D.
  - `player.make_eq_state(sos_filters, channels: int = 1)`; `player.apply_eq` accepts `(n,)` or `(n, 2)`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/unit/test_stereo.py
"""Tests for stereo helpers and stereo-safe DSP primitives."""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest
import soundfile as sf

from autodj import player, stereo


def _tone(n: int = 4410, hz: float = 440.0, sr: int = 44100) -> np.ndarray:
    t = np.arange(n, dtype=np.float32) / sr
    return (0.5 * np.sin(2 * np.pi * hz * t)).astype(np.float32)


def test_to_stereo_duplicates_mono_and_keeps_stereo() -> None:
    mono = _tone(10)
    out = stereo.to_stereo(mono)
    assert out.shape == (10, 2)
    assert out.dtype == np.float32
    np.testing.assert_array_equal(out[:, 0], out[:, 1])
    both = np.stack([mono, -mono], axis=1)
    np.testing.assert_array_equal(stereo.to_stereo(both), both)
    many = np.stack([mono, -mono, mono * 0], axis=1)
    np.testing.assert_array_equal(stereo.to_stereo(many), both)


def test_per_channel_matches_mono_for_identical_channels() -> None:
    mono = _tone()
    out = stereo.per_channel(lambda ch: ch * 2.0, stereo.to_stereo(mono))
    np.testing.assert_allclose(out[:, 0], mono * 2.0)
    np.testing.assert_allclose(out[:, 1], mono * 2.0)
    np.testing.assert_array_equal(stereo.per_channel(lambda ch: ch + 1, mono), mono + 1)


def test_per_channel_equalises_column_lengths() -> None:
    two = stereo.to_stereo(_tone(10))
    calls = iter([np.ones(10, np.float32), np.ones(12, np.float32)])
    out = stereo.per_channel(lambda _ch: next(calls), two)
    assert out.shape == (10, 2)
    calls = iter([np.ones(10, np.float32), np.ones(7, np.float32)])
    out = stereo.per_channel(lambda _ch: next(calls), two)
    assert out.shape == (10, 2)
    assert out[9, 1] == 0.0


def test_envelope_broadcasts_for_stereo() -> None:
    env = np.linspace(0, 1, 5, dtype=np.float32)
    assert stereo.envelope(env, np.zeros(5)).shape == (5,)
    assert stereo.envelope(env, np.zeros((5, 2))).shape == (5, 1)


def test_load_stereo_resamples_and_duplicates(tmp_path: Path) -> None:
    path = tmp_path / "mono48k.wav"
    sf.write(path, _tone(4800, sr=48000), 48000)
    out = stereo.load_stereo(str(path))
    assert out.ndim == 2 and out.shape[1] == 2
    assert abs(out.shape[0] - 4410) <= 2
    np.testing.assert_array_equal(out[:, 0], out[:, 1])


def test_load_stereo_keeps_two_channels(tmp_path: Path) -> None:
    path = tmp_path / "st.wav"
    left, right = _tone(441), -_tone(441)
    sf.write(path, np.stack([left, right], axis=1), 44100)
    out = stereo.load_stereo(str(path))
    np.testing.assert_allclose(out[:, 0], left, atol=1e-4)
    np.testing.assert_allclose(out[:, 1], right, atol=1e-4)


@pytest.mark.parametrize("ducked", [False, True])
def test_crossfades_accept_stereo(ducked: bool) -> None:
    a, b = _tone(8820), _tone(8820, hz=220.0)
    cf = 4410
    if ducked:
        mono_out = player._apply_crossfade_ducked(a, b, cf, 44100)
        st_out = player._apply_crossfade_ducked(stereo.to_stereo(a), stereo.to_stereo(b), cf, 44100)
    else:
        mono_out = player._apply_crossfade(a, b, cf)
        st_out = player._apply_crossfade(stereo.to_stereo(a), stereo.to_stereo(b), cf)
    assert st_out.shape == (len(mono_out), 2)
    np.testing.assert_allclose(st_out[:, 0], mono_out, atol=1e-5)
    np.testing.assert_allclose(st_out[:, 1], mono_out, atol=1e-5)


def test_filter_sweep_accepts_stereo() -> None:
    a = _tone(8820)
    mono_out = player.apply_filter_sweep(a, 44100, 8000.0, 400.0)
    st_out = player.apply_filter_sweep(stereo.to_stereo(a), 44100, 8000.0, 400.0)
    np.testing.assert_allclose(st_out[:, 0], mono_out, atol=1e-5)
    np.testing.assert_allclose(st_out[:, 1], mono_out, atol=1e-5)


def test_time_stretch_accepts_stereo() -> None:
    a = _tone(22050)
    out = player._time_stretch(stereo.to_stereo(a), 1.05)
    assert out.ndim == 2 and out.shape[1] == 2
    np.testing.assert_allclose(out[:, 0], out[:, 1], atol=1e-6)


def test_eq_stateful_stereo_matches_mono() -> None:
    filters = player.make_eq_filters(44100)
    a = _tone(1764)
    mono_state = player.make_eq_state(filters)
    st_state = player.make_eq_state(filters, channels=2)
    st = stereo.to_stereo(a)
    for start in (0, 882):
        m = player.apply_eq(a[start : start + 882], filters, 0.5, 1.0, 1.5, state=mono_state)
        s = player.apply_eq(st[start : start + 882], filters, 0.5, 1.0, 1.5, state=st_state)
        np.testing.assert_allclose(s[:, 0], m, atol=1e-5)
        np.testing.assert_allclose(s[:, 1], m, atol=1e-5)


def test_mono_helper() -> None:
    st = np.stack([np.ones(3), np.zeros(3)], axis=1).astype(np.float32)
    np.testing.assert_allclose(stereo.mono(st), 0.5)
    np.testing.assert_array_equal(stereo.mono(np.ones(3)), np.ones(3))
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run --frozen pytest tests/unit/test_stereo.py -q -o addopts=""`
Expected: FAIL with `ModuleNotFoundError: No module named 'autodj.stereo'`.

- [ ] **Step 3: Write `src/autodj/stereo.py`**

```python
"""Stereo helpers shared by the server-side mix path.

The server mix is stereo ``float32`` shaped ``(frames, 2)`` at
:data:`SAMPLE_RATE`.  Most DSP in AutoDJ was written for mono arrays; these
helpers let that code run on each channel without rewriting it.
"""

from __future__ import annotations

from collections.abc import Callable

import numpy as np
import soundfile as sf

SAMPLE_RATE = 44_100


def to_stereo(audio: np.ndarray) -> np.ndarray:
    """Return *audio* as a C-contiguous ``(frames, 2)`` float32 array.

    Args:
        audio: Mono ``(frames,)`` or multi-channel ``(frames, channels)`` audio.

    Returns:
        Stereo audio.  Mono is duplicated to both channels; extra channels
        beyond the first two are dropped.
    """
    arr = np.asarray(audio, dtype=np.float32)
    if arr.ndim == 1:
        arr = np.stack([arr, arr], axis=1)
    elif arr.shape[1] == 1:
        arr = np.repeat(arr, 2, axis=1)
    elif arr.shape[1] > 2:
        arr = arr[:, :2]
    return np.ascontiguousarray(arr, dtype=np.float32)


def mono(audio: np.ndarray) -> np.ndarray:
    """Return a mono view of *audio* for analysis.

    Args:
        audio: Mono or ``(frames, channels)`` audio.

    Returns:
        The channel mean for 2-D input, or *audio* unchanged for 1-D input.
    """
    if audio.ndim == 1:
        return audio
    return audio.mean(axis=1).astype(np.float32)


def per_channel(fn: Callable[[np.ndarray], np.ndarray], audio: np.ndarray) -> np.ndarray:
    """Apply a mono function to each channel of *audio*.

    Args:
        fn: Function taking and returning a 1-D array.
        audio: Mono or ``(frames, channels)`` audio.

    Returns:
        ``fn(audio)`` for mono input; for 2-D input the per-channel results
        stacked on axis 1, each trimmed or zero-padded to the first
        channel's output length so the columns always line up.
    """
    if audio.ndim == 1:
        return fn(audio)
    columns = [np.asarray(fn(np.ascontiguousarray(audio[:, c]))) for c in range(audio.shape[1])]
    length = len(columns[0])
    fitted = []
    for column in columns:
        if len(column) >= length:
            fitted.append(column[:length])
        else:
            fitted.append(np.pad(column, (0, length - len(column))))
    return np.stack(fitted, axis=1).astype(np.float32)


def envelope(env: np.ndarray, like: np.ndarray) -> np.ndarray:
    """Shape a 1-D gain envelope so it broadcasts against *like*.

    Args:
        env: 1-D envelope, one gain per frame.
        like: The audio the envelope will multiply.

    Returns:
        *env* for mono audio, ``env[:, None]`` for 2-D audio.
    """
    return env if like.ndim == 1 else env[:, None]


def load_stereo(path: str, target_sr: int = SAMPLE_RATE) -> np.ndarray:
    """Load an audio file as stereo float32 at *target_sr*.

    Uses soundfile first and falls back to librosa for formats soundfile
    cannot read.  Both paths resample to *target_sr*.

    Args:
        path: Audio file path.
        target_sr: Output sample rate in Hz.

    Returns:
        ``(frames, 2)`` float32 audio.

    Raises:
        OSError: If neither decoder can read the file.
    """
    try:
        audio, sr = sf.read(path, dtype="float32", always_2d=True)
    except Exception:
        import librosa

        audio, sr = librosa.load(path, sr=None, mono=False)
        audio = audio.T if audio.ndim == 2 else audio
    audio = to_stereo(audio)
    if int(sr) != target_sr:
        import librosa

        audio = librosa.resample(audio, orig_sr=int(sr), target_sr=target_sr, axis=0)
    return to_stereo(audio)
```

- [ ] **Step 4: Make the player DSP stereo-safe**

In `src/autodj/player.py` add `from autodj.stereo import envelope, per_channel` to the imports, then:

1. `_apply_crossfade`: replace `overlap = (a_tail * fade_out) + (b_head * fade_in)` with
   `overlap = (a_tail * envelope(fade_out, a_tail)) + (b_head * envelope(fade_in, b_head))`.
   Update the docstring "Mono float32 audio array" to "Mono or stereo float32 audio array" for both arguments.
2. `_apply_crossfade_ducked`: replace `a_tail_hp = cast(np.ndarray, sosfilt(sos, a_tail)).astype(np.float32)` with
   `a_tail_hp = cast(np.ndarray, sosfilt(sos, a_tail, axis=0)).astype(np.float32)`;
   replace `a_ducked = a_tail * (1.0 - bass_remove) + a_tail_hp * bass_remove` with
   `duck = envelope(bass_remove, a_tail)` then `a_ducked = a_tail * (1.0 - duck) + a_tail_hp * duck`;
   replace `overlap = (a_ducked * fade_out) + (b_head * fade_in)` with
   `overlap = (a_ducked * envelope(fade_out, a_ducked)) + (b_head * envelope(fade_in, b_head))`.
3. `_time_stretch`: replace the `return librosa.effects.time_stretch(...)` line with

```python
        return per_channel(
            lambda channel: librosa.effects.time_stretch(y=channel, rate=1.0 / ratio),
            audio,
        ).astype(np.float32)
```

4. `apply_filter_sweep`: change `filt = cast(np.ndarray, sosfilt(sos, chunk)).astype(np.float32)` to
   `filt = cast(np.ndarray, sosfilt(sos, chunk, axis=0)).astype(np.float32)` and the blend line to

```python
            fade = envelope(np.linspace(0.0, 1.0, blend, dtype=np.float32), filt)
            filt[:blend] = prev_tail * (1.0 - fade) + filt[:blend] * fade
```

5. `make_eq_state(sos_filters, channels: int = 1)`: shape each band as
   `(sections, 2)` when `channels == 1` and `(sections, 2, channels)` otherwise:

```python
    shape_tail = () if channels == 1 else (channels,)
    return {
        name: np.zeros((np.asarray(sos).shape[0], 2, *shape_tail), dtype=np.float64)
        for name, sos in sos_filters.items()
    }
```

   Document the new argument in the docstring.
6. `apply_eq`: pass `axis=0` to every `sosfilt` call inside `_filter` (both the stateless and the `zi=state[band]` branches). SciPy's `zi` for `axis=0` on `(n, 2)` input has shape `(sections, 2, 2)`, which is what `make_eq_state(..., channels=2)` returns.

- [ ] **Step 5: Run the new tests and the existing player tests**

Run: `uv run --frozen pytest tests/unit/test_stereo.py tests/unit/test_player.py -q -o addopts=""`
Expected: all PASS.

- [ ] **Step 6: Commit**

```bash
git add src/autodj/stereo.py src/autodj/player.py tests/unit/test_stereo.py
git commit -m "feat: add stereo helpers and make mix dsp stereo-safe"
```

---

### Task 2: Stereo, seeded transition effects

**Files:**
- Modify: `src/autodj/transitions.py` (`noise_riser` 621, `glitch` 894, `_noise_drop_extra` 1553, `apply_transition` 1613)
- Test: `tests/unit/test_transitions_stereo.py`

**Interfaces:**
- Consumes: `stereo.per_channel`, `stereo.to_stereo`.
- Produces: `apply_transition(tail, head, sample_rate, effect, *, seed: int | None = None) -> tuple[np.ndarray, np.ndarray, np.ndarray]`. For 2-D `tail`/`head`, each returned array is 2-D; the extra layer is `(k, 2)` or `(0, 2)`. With the same `seed`, both channels of identical input produce identical output.
- `noise_riser(n_samples, sample_rate, ..., seed: int | None = None)`, `glitch(..., seed=None)` (existing parameter), `_noise_drop_extra(tail, sample_rate, seed: int | None = None)`.

- [ ] **Step 1: Write the failing test**

```python
# tests/unit/test_transitions_stereo.py
"""Every transition effect must accept stereo and keep channels in step."""

from __future__ import annotations

import numpy as np
import pytest

from autodj import stereo
from autodj.transitions import TransitionFx, apply_transition

_SR = 44100
_CONCRETE = [fx for fx in TransitionFx if fx not in (TransitionFx.RANDOM, TransitionFx.ROTATE)]


def _signal(n: int) -> np.ndarray:
    t = np.arange(n, dtype=np.float32) / _SR
    return (0.4 * np.sin(2 * np.pi * 330 * t)).astype(np.float32)


@pytest.mark.parametrize("effect", _CONCRETE, ids=lambda fx: fx.value)
def test_effect_accepts_stereo_and_keeps_channels_in_step(effect: TransitionFx) -> None:
    tail = stereo.to_stereo(_signal(_SR))
    head = stereo.to_stereo(_signal(_SR // 2))
    out_tail, out_head, extra = apply_transition(tail, head, _SR, effect, seed=7)
    assert out_tail.shape == tail.shape
    assert out_head.shape == head.shape
    assert extra.ndim == 2 and extra.shape[1] == 2
    np.testing.assert_allclose(out_tail[:, 0], out_tail[:, 1], atol=1e-6)
    np.testing.assert_allclose(out_head[:, 0], out_head[:, 1], atol=1e-6)
    if len(extra):
        np.testing.assert_allclose(extra[:, 0], extra[:, 1], atol=1e-6)


@pytest.mark.parametrize("effect", [TransitionFx.GLITCH, TransitionFx.NOISE_RISER])
def test_seed_makes_random_effects_repeatable(effect: TransitionFx) -> None:
    tail = stereo.to_stereo(_signal(_SR))
    head = stereo.to_stereo(_signal(_SR // 2))
    first = apply_transition(tail, head, _SR, effect, seed=3)
    second = apply_transition(tail, head, _SR, effect, seed=3)
    for a, b in zip(first, second, strict=True):
        np.testing.assert_array_equal(a, b)


def test_mono_path_unchanged() -> None:
    tail, head = _signal(_SR), _signal(_SR // 2)
    out_tail, out_head, extra = apply_transition(tail, head, _SR, TransitionFx.ECHO_OUT)
    assert out_tail.ndim == 1 and out_head.ndim == 1 and extra.ndim == 1
```

Before writing, confirm the enum member names used above (`GLITCH`, `NOISE_RISER`, `ECHO_OUT`, `RANDOM`, `ROTATE`) by reading `class TransitionFx` at `transitions.py:47`; rename in the test if they differ.

- [ ] **Step 2: Run to verify failure**

Run: `uv run --frozen pytest tests/unit/test_transitions_stereo.py -q -o addopts=""`
Expected: FAIL — `apply_transition() got an unexpected keyword argument 'seed'`.

- [ ] **Step 3: Implement**

1. `noise_riser`: add keyword `seed: int | None = None`; replace `np.random.default_rng()` with `np.random.default_rng(seed)`.
2. `_noise_drop_extra(tail, sample_rate, seed: int | None = None)`: same change.
3. Rename the existing body of `apply_transition` to `_apply_transition_mono(tail, head, sample_rate, effect, seed)`, passing `seed` into the `glitch`, `noise_riser` and `_noise_drop_extra` calls it makes (look up each call inside the dispatch; `glitch` already accepts `seed`).
4. New `apply_transition`:

```python
def apply_transition(
    tail: np.ndarray,
    head: np.ndarray,
    sample_rate: int,
    effect: TransitionFx,
    *,
    seed: int | None = None,
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Apply *effect* to a crossfade, for mono or stereo audio.

    Stereo input is processed one channel at a time with the same *seed*,
    so effects that use randomness treat both channels identically.

    Args:
        tail: Outgoing tail, ``(n,)`` or ``(n, 2)``.
        head: Incoming head, same channel layout as *tail*.
        sample_rate: Sample rate in Hz.
        effect: Concrete effect to apply.
        seed: Seed for effects that use randomness.  ``None`` draws a fresh
            seed, which is then shared by both channels.

    Returns:
        ``(tail, head, extra_layer)`` in the input's channel layout.  The
        extra layer is empty (length 0) when the effect adds none.
    """
    if tail.ndim == 1:
        return _apply_transition_mono(tail, head, sample_rate, effect, seed)
    shared_seed = seed if seed is not None else int(np.random.default_rng().integers(2**31))
    results = [
        _apply_transition_mono(
            np.ascontiguousarray(tail[:, c]),
            np.ascontiguousarray(head[:, c]),
            sample_rate,
            effect,
            shared_seed,
        )
        for c in range(tail.shape[1])
    ]
    stacked = []
    for part in range(3):
        columns = [result[part] for result in results]
        length = min(len(column) for column in columns)
        stacked.append(np.stack([column[:length] for column in columns], axis=1).astype(np.float32))
    return stacked[0], stacked[1], stacked[2]
```

- [ ] **Step 4: Run tests**

Run: `uv run --frozen pytest tests/unit/test_transitions_stereo.py tests/unit/test_transitions.py -q -o addopts=""`
Expected: all PASS. If an effect fails the "channels in step" check, find its randomness source and thread `seed` to it the same way.

- [ ] **Step 5: Commit**

```bash
git add src/autodj/transitions.py tests/unit/test_transitions_stereo.py
git commit -m "feat: run transition effects on stereo with a shared seed"
```

---

### Task 3: Track renderer

**Files:**
- Modify: `src/autodj/player.py` (`_play_with_crossfade` 1921-1997, `_load_incoming` 1688, `_apply_transition_effect` 1835, `_mix_overlap` 1885, `_outgoing_meta` 1461, `_skip_incoming_intro` 1752)
- Create: `RenderedTrack` in `src/autodj/mixbus.py` (only the dataclass in this task)
- Test: `tests/unit/test_renderer.py`

**Interfaces:**
- Produces:

```python
@dataclass(frozen=True)
class RenderedTrack:
    """One track's audio as the mix bus will play it."""
    entry: IndexEntry             # the track this audio belongs to
    audio: np.ndarray             # (frames, 2) float32 at 44100 Hz: body plus overlap into next
    next_entry: IndexEntry | None # the track mixed into the tail, if any
    next_start_offset: int        # samples of next_entry already played in the overlap (plus skipped intro)
    transition_fx: str            # effect name used for the overlap, "" if none
```

- `Player._render_track(current: IndexEntry, next_entry: IndexEntry | None, start_offset: int) -> RenderedTrack | None` — `None` when *current* cannot be loaded or has no audio left after `start_offset`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/unit/test_renderer.py
"""The renderer returns stereo audio and carries the next track's offset."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import numpy as np
import pytest

from autodj import player as player_mod
from autodj.mixbus import RenderedTrack


def _entry(name: str, length: float = 10.0) -> MagicMock:
    entry = MagicMock()
    entry.path = Path(f"/music/{name}.flac")
    entry.length = length
    entry.bpm = 120.0
    return entry


@pytest.fixture
def renderer(monkeypatch: pytest.MonkeyPatch) -> player_mod.Player:
    audio = {
        "a": np.full((44100 * 10, 2), 0.1, np.float32),
        "b": np.full((44100 * 10, 2), 0.2, np.float32),
        "short": np.full((100, 2), 0.3, np.float32),
        "empty": np.zeros((0, 2), np.float32),
    }
    monkeypatch.setattr(player_mod, "load_stereo", lambda path, target_sr=44100: audio[Path(path).stem])
    p = player_mod.Player.__new__(player_mod.Player)
    p._cfg = MagicMock()
    p._cfg.playback.crossfade_seconds = 2.0
    p._apply_replaygain = lambda audio_in, _path: audio_in
    p._load_lyrics = lambda _path: None
    p._ensure_dj_cache = lambda: None
    p._outgoing_meta = lambda *_a: None
    p._peek_incoming_meta = lambda _e: None
    p._effective_crossfade_seconds = lambda *_a: 2.0
    p._crossfade_start_in_a = lambda audio_a, _sr, _meta, cf: len(audio_a) - cf
    p._maybe_beatmatch = lambda audio_b, *_a: audio_b
    p._skip_incoming_intro_samples = lambda _audio_b, _sr, _e: 0
    p._apply_outgoing_filter_sweep = lambda a, *_a: a
    p._apply_transition_effect = lambda a, _b, head, *_a: (a, head, np.zeros((0, 2), np.float32))
    p._mix_overlap = lambda a, head, cf, _sr, _extra: player_mod._apply_crossfade(a, np.concatenate([head, head[:0]]), cf)
    p._last_transition_fx = ""
    return p


def test_render_returns_stereo_and_offset(renderer: player_mod.Player) -> None:
    out = renderer._render_track(_entry("a"), _entry("b"), start_offset=0)
    assert isinstance(out, RenderedTrack)
    assert out.audio.ndim == 2 and out.audio.shape[1] == 2
    assert out.next_start_offset == 2 * 44100
    assert len(out.audio) == 10 * 44100


def test_render_skips_already_played_head(renderer: player_mod.Player) -> None:
    out = renderer._render_track(_entry("b"), None, start_offset=2 * 44100)
    assert out is not None
    assert len(out.audio) == 8 * 44100
    assert out.next_start_offset == 0


def test_render_returns_none_for_unloadable_or_empty(renderer: player_mod.Player, monkeypatch: pytest.MonkeyPatch) -> None:
    assert renderer._render_track(_entry("empty"), None, start_offset=0) is None

    def _boom(path: str, target_sr: int = 44100) -> np.ndarray:
        raise OSError("bad file")

    monkeypatch.setattr(player_mod, "load_stereo", _boom)
    assert renderer._render_track(_entry("a"), None, start_offset=0) is None


def test_track_shorter_than_crossfade_is_cut_not_stalled(renderer: player_mod.Player) -> None:
    out = renderer._render_track(_entry("short"), _entry("b"), start_offset=0)
    assert out is not None
    assert len(out.audio) == 100
    assert out.next_start_offset == 0


def test_offset_beyond_track_returns_none(renderer: player_mod.Player) -> None:
    assert renderer._render_track(_entry("short"), None, start_offset=500) is None
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run --frozen pytest tests/unit/test_renderer.py -q -o addopts=""`
Expected: FAIL — `ImportError: cannot import name 'RenderedTrack'`.

- [ ] **Step 3: Implement**

1. Create `src/autodj/mixbus.py` with the module docstring `"""Clock-paced stereo mix bus for server-side playback."""`, `from __future__ import annotations`, the imports `from dataclasses import dataclass`, `from typing import TYPE_CHECKING`, `import numpy as np`, `if TYPE_CHECKING: from autodj.indexer import IndexEntry` (confirm the module that defines `IndexEntry` with `grep -n "class IndexEntry" src/autodj/*.py`), and the `RenderedTrack` dataclass from the Interfaces block with its docstring and an `Attributes:` section.
2. In `player.py` import `from autodj.stereo import SAMPLE_RATE, load_stereo, mono, to_stereo`.
3. Split `_skip_incoming_intro` into `_skip_incoming_intro_samples(audio_b, sr_a, next_entry) -> int` (returns the number of samples to skip; `analyse_audio(mono(audio_b), sr_a)` for analysis) and keep `_skip_incoming_intro` as `return audio_b[self._skip_incoming_intro_samples(audio_b, sr_a, next_entry):]` for the dry-run callers.
4. `_outgoing_meta`: pass `mono(audio_a)` to `analyse_audio`.
5. `_load_incoming`: load with `load_stereo(str(next_entry.path), sr_a)` and build the silence fallback as `np.zeros((crossfade_samples, 2), np.float32)`.
6. `_apply_transition_effect`: build `extra_layer = np.zeros((0, 2), np.float32)` when the input is stereo (`np.zeros((0,) if audio_a_trimmed.ndim == 1 else (0, 2), np.float32)`), and pass `seed=int(self._rng.integers(2**31))` to `apply_transition`, where `self._rng = np.random.default_rng()` is set in `__init__`.
7. `_mix_overlap`: the extra layer addition becomes `mixed[ex_start:overlap_end] += extra_layer[-ex_len:] * wet` unchanged in form; it works once the layer is `(k, 2)`.
8. New `_render_track`, placed directly above `_play_with_crossfade`:

```python
    def _render_track(
        self,
        current: IndexEntry,
        next_entry: IndexEntry | None,
        start_offset: int,
    ) -> RenderedTrack | None:
        """Render *current* from *start_offset* into the head of *next_entry*.

        The overlap with *next_entry* is mixed in, and the returned
        ``next_start_offset`` tells the next call where the incoming track's
        audio continues, so no sample plays twice.

        Args:
            current: Track to render.
            next_entry: Track mixed into the tail, or ``None`` for no overlap.
            start_offset: Samples of *current* already played by the previous
                overlap (including any skipped intro).

        Returns:
            The rendered track, or ``None`` when *current* cannot be loaded or
            has no audio left after *start_offset*.
        """
        try:
            audio_a = load_stereo(str(current.path), SAMPLE_RATE)
        except (OSError, ValueError, RuntimeError) as exc:
            logger.error("Cannot load %s: %s — skipping.", current.path, exc)
            return None
        audio_a = self._apply_replaygain(audio_a, current.path)
        if start_offset >= len(audio_a):
            return None
        self._load_lyrics(current.path)
        self._ensure_dj_cache()
        meta_a = self._outgoing_meta(audio_a, SAMPLE_RATE, current.path)
        audio_a = audio_a[start_offset:]
        if next_entry is None:
            return RenderedTrack(current, audio_a, None, 0, "")
        meta_b = self._peek_incoming_meta(next_entry)
        eff_s = self._effective_crossfade_seconds(meta_a, meta_b, current.length)
        crossfade = int(eff_s * SAMPLE_RATE)
        if crossfade >= len(audio_a):
            return RenderedTrack(current, audio_a, next_entry, 0, "")
        a_start = self._crossfade_start_in_a(audio_a, SAMPLE_RATE, meta_a, crossfade)
        audio_b = self._load_incoming(next_entry, SAMPLE_RATE, crossfade)
        audio_b = self._maybe_beatmatch(audio_b, current, next_entry)
        intro = self._skip_incoming_intro_samples(audio_b, SAMPLE_RATE, next_entry)
        audio_b = audio_b[intro:]
        if a_start + crossfade > len(audio_a):
            crossfade = max(0, len(audio_a) - a_start)
        a_trimmed = audio_a[: a_start + crossfade]
        if crossfade == 0 or len(audio_b) < crossfade:
            return RenderedTrack(current, a_trimmed, next_entry, intro, "")
        b_head = audio_b[:crossfade]
        a_trimmed = self._apply_outgoing_filter_sweep(a_trimmed, SAMPLE_RATE, crossfade)
        a_trimmed, b_head, extra = self._apply_transition_effect(
            a_trimmed, audio_b, b_head, SAMPLE_RATE, crossfade
        )
        mixed = self._mix_overlap(a_trimmed, b_head, crossfade, SAMPLE_RATE, extra)
        return RenderedTrack(
            current, to_stereo(mixed), next_entry, intro + crossfade, str(self._last_transition_fx)
        )
```

Note: the beat-match stretch changes `audio_b`'s timeline, so `next_start_offset` is in stretched samples. The next pass loads the unstretched file; convert with `int(offset / self._beatmatch_ratio)` when `_beatmatch_ratio != 1.0` and record that in a comment. Add a test `test_offset_scaled_by_beatmatch_ratio` that sets `renderer._maybe_beatmatch` to stretch by 1.25 and `renderer._beatmatch_ratio = 1.25`, and asserts `next_start_offset == int((2 * 44100) / 1.25)`.

9. Delete `_play_with_crossfade` and `_stream_audio` in Task 5, not here; this task only adds the renderer.

- [ ] **Step 4: Run tests**

Run: `uv run --frozen pytest tests/unit/test_renderer.py tests/unit/test_player.py -q -o addopts=""`
Expected: all PASS.

- [ ] **Step 5: Commit**

```bash
git add src/autodj/mixbus.py src/autodj/player.py tests/unit/test_renderer.py
git commit -m "feat: render tracks in stereo and carry the next track's offset"
```

---

### Task 4: Mix bus

**Files:**
- Modify: `src/autodj/mixbus.py`
- Test: `tests/unit/test_mixbus.py`

**Interfaces:**
- Consumes: `RenderedTrack` (Task 3), `player.make_eq_filters`, `player.make_eq_state(..., channels=2)`, `player.apply_eq`, `player.reset_eq_state`.
- Produces:

```python
class Output(Protocol):
    def write(self, block: np.ndarray) -> None: ...   # (882, 2) float32
    def close(self) -> None: ...

class Clock(Protocol):
    def now(self) -> float: ...
    def sleep(self, seconds: float) -> None: ...

class SystemClock: ...  # time.monotonic / time.sleep

@dataclass
class BusEvents:
    on_track_start: Callable[[RenderedTrack], None]    # first block of a track emitted
    on_position: Callable[[int], None]                 # frames played in current track, per block
    on_need_track: Callable[[], RenderedTrack | None]  # called when the bus needs the next track

class MixBus:
    BLOCK = 882
    def __init__(self, events: BusEvents, eq_gains: Callable[[], tuple[float, float, float]],
                 clock: Clock | None = None) -> None
    def add_output(self, output: Output) -> None
    def remove_output(self, output: Output) -> None
    def pause(self, paused: bool) -> None
    def skip(self) -> None              # 150 ms fade then next track
    def play_liner(self, audio: np.ndarray, duck_db: float) -> None  # (n, 2)
    def stop_set(self) -> None          # drop current and queued tracks; emit silence until start_set
    def start_set(self) -> None
    @property
    def playing(self) -> bool           # a set is active (started and not stopped)
    def render_block(self) -> np.ndarray   # one block; pure, used by tests
    def run(self, stop: threading.Event) -> None   # paced loop calling render_block and writing outputs
```

- [ ] **Step 1: Write the failing tests**

```python
# tests/unit/test_mixbus.py
"""Mix bus behaviour with a fake clock and fake outputs."""

from __future__ import annotations

import threading
from unittest.mock import MagicMock

import numpy as np
import pytest

from autodj.mixbus import BusEvents, MixBus, RenderedTrack


class FakeClock:
    def __init__(self) -> None:
        self.t = 0.0
        self.slept: list[float] = []

    def now(self) -> float:
        return self.t

    def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.t += seconds


class Recorder:
    def __init__(self) -> None:
        self.blocks: list[np.ndarray] = []
        self.closed = False

    def write(self, block: np.ndarray) -> None:
        self.blocks.append(block.copy())

    def close(self) -> None:
        self.closed = True


def _track(value: float, frames: int, name: str = "t") -> RenderedTrack:
    entry = MagicMock()
    entry.path = name
    return RenderedTrack(entry, np.full((frames, 2), value, np.float32), None, 0, "")


def _bus(tracks: list[RenderedTrack], gains=(1.0, 1.0, 1.0)) -> tuple[MixBus, list, list]:
    started: list[RenderedTrack] = []
    positions: list[int] = []
    queue = list(tracks)
    events = BusEvents(
        on_track_start=started.append,
        on_position=positions.append,
        on_need_track=lambda: queue.pop(0) if queue else None,
    )
    bus = MixBus(events, eq_gains=lambda: gains, clock=FakeClock())
    return bus, started, positions


def test_idle_bus_emits_silence() -> None:
    bus, started, _ = _bus([_track(0.5, 2000)])
    assert not bus.playing
    np.testing.assert_array_equal(bus.render_block(), np.zeros((882, 2), np.float32))
    assert started == []


def test_plays_tracks_back_to_back_and_reports_starts() -> None:
    first, second = _track(0.5, 1000, "a"), _track(0.25, 1000, "b")
    bus, started, positions = _bus([first, second])
    bus.start_set()
    b1 = bus.render_block()
    b2 = bus.render_block()
    assert started == [first, second]
    np.testing.assert_allclose(b1[:, 0], 0.5)
    np.testing.assert_allclose(b2[: 1000 - 882, 0], 0.5)
    np.testing.assert_allclose(b2[1000 - 882 :, 0], 0.25)
    assert positions[0] == 882


def test_pause_emits_silence_and_holds_position() -> None:
    bus, _, positions = _bus([_track(0.5, 5000)])
    bus.start_set()
    bus.render_block()
    bus.pause(True)
    np.testing.assert_array_equal(bus.render_block(), np.zeros((882, 2), np.float32))
    bus.pause(False)
    bus.render_block()
    assert positions == [882, 1764]


def test_skip_fades_then_moves_on() -> None:
    first, second = _track(1.0, 100_000, "a"), _track(0.5, 100_000, "b")
    bus, started, _ = _bus([first, second])
    bus.start_set()
    bus.render_block()
    bus.skip()
    fade_blocks = [bus.render_block() for _ in range(10)]
    assert fade_blocks[0][0, 0] <= 1.0
    assert started[-1] is second
    assert any(np.allclose(b[:, 0], 0.5) for b in fade_blocks)


def test_eq_gains_are_applied() -> None:
    bus, _, _ = _bus([_track(0.5, 5000)], gains=(0.0, 0.0, 0.0))
    bus.start_set()
    bus.render_block()
    assert np.max(np.abs(bus.render_block())) < 0.05


def test_liner_ducks_music_and_is_mixed_in() -> None:
    bus, _, _ = _bus([_track(0.5, 50_000)])
    bus.start_set()
    bus.play_liner(np.full((882 * 3, 2), 0.2, np.float32), duck_db=-12.0)
    block = bus.render_block()
    duck = 10 ** (-12.0 / 20.0)
    assert block[-1, 0] == pytest.approx(0.5 * duck + 0.2, abs=0.02)


def test_stop_set_drops_tracks_and_goes_silent() -> None:
    bus, _, _ = _bus([_track(0.5, 50_000), _track(0.5, 50_000)])
    bus.start_set()
    bus.render_block()
    bus.stop_set()
    assert not bus.playing
    np.testing.assert_array_equal(bus.render_block(), np.zeros((882, 2), np.float32))


def test_no_next_track_means_silence_not_stall() -> None:
    bus, _, _ = _bus([_track(0.5, 100)])
    bus.start_set()
    bus.render_block()
    np.testing.assert_array_equal(bus.render_block(), np.zeros((882, 2), np.float32))


def test_run_paces_by_clock_and_isolates_failing_output() -> None:
    bus, _, _ = _bus([_track(0.5, 882 * 10)])
    good, bad = Recorder(), MagicMock()
    bad.write.side_effect = RuntimeError("gone")
    bus.add_output(good)
    bus.add_output(bad)
    bus.start_set()
    stop = threading.Event()
    clock: FakeClock = bus._clock  # type: ignore[assignment]
    original_sleep = clock.sleep

    def sleep_and_maybe_stop(seconds: float) -> None:
        original_sleep(seconds)
        if len(good.blocks) >= 5:
            stop.set()

    clock.sleep = sleep_and_maybe_stop  # type: ignore[method-assign]
    bus.run(stop)
    assert len(good.blocks) >= 5
    assert bad not in bus._outputs
    assert clock.t == pytest.approx(len(good.blocks) * 0.02, abs=0.021)
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run --frozen pytest tests/unit/test_mixbus.py -q -o addopts=""`
Expected: FAIL — `ImportError: cannot import name 'BusEvents'`.

- [ ] **Step 3: Implement in `src/autodj/mixbus.py`**

```python
import logging
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol

from autodj.player import apply_eq, make_eq_filters, make_eq_state, reset_eq_state
from autodj.stereo import SAMPLE_RATE

logger = logging.getLogger(__name__)

_SKIP_FADE_FRAMES = int(0.15 * SAMPLE_RATE)


class Output(Protocol):
    """Destination for mixed blocks."""

    def write(self, block: np.ndarray) -> None:
        """Receive one ``(882, 2)`` float32 block."""

    def close(self) -> None:
        """Release resources."""


class Clock(Protocol):
    """Time source for pacing."""

    def now(self) -> float:
        """Return monotonic seconds."""

    def sleep(self, seconds: float) -> None:
        """Block for *seconds*."""


class SystemClock:
    """Real monotonic clock."""

    def now(self) -> float:
        """Return :func:`time.monotonic`."""
        return time.monotonic()

    def sleep(self, seconds: float) -> None:
        """Sleep with :func:`time.sleep`."""
        time.sleep(seconds)


@dataclass
class BusEvents:
    """Callbacks the mix bus makes while playing.

    Attributes:
        on_track_start: Called when a track's first block is emitted.
        on_position: Called after each block with frames played in the current track.
        on_need_track: Called for the next rendered track; ``None`` means none yet.
    """

    on_track_start: Callable[[RenderedTrack], None]
    on_position: Callable[[int], None]
    on_need_track: Callable[[], RenderedTrack | None]


class MixBus:
    """Play rendered tracks continuously, one 20 ms block at a time."""

    BLOCK = 882

    def __init__(
        self,
        events: BusEvents,
        eq_gains: Callable[[], tuple[float, float, float]],
        clock: Clock | None = None,
    ) -> None:
        """Create an idle bus.

        Args:
            events: Callbacks for track starts, position and track supply.
            eq_gains: Returns the current (low, mid, high) EQ gains.
            clock: Time source; defaults to :class:`SystemClock`.
        """
        self._events = events
        self._eq_gains = eq_gains
        self._clock: Clock = clock or SystemClock()
        self._lock = threading.RLock()
        self._outputs: list[Output] = []
        self._current: RenderedTrack | None = None
        self._pos = 0
        self._paused = False
        self._playing = False
        self._skip_fade = 0
        self._liner: np.ndarray | None = None
        self._liner_pos = 0
        self._duck = 1.0
        self._eq_filters = make_eq_filters(SAMPLE_RATE)
        self._eq_state = make_eq_state(self._eq_filters, channels=2)
        self._eq_engaged = False

    @property
    def playing(self) -> bool:
        """Whether a set is active."""
        return self._playing

    def add_output(self, output: Output) -> None:
        """Register *output* to receive every block."""
        with self._lock:
            self._outputs.append(output)

    def remove_output(self, output: Output) -> None:
        """Stop sending blocks to *output*."""
        with self._lock:
            if output in self._outputs:
                self._outputs.remove(output)

    def pause(self, paused: bool) -> None:
        """Emit silence and hold position while *paused*."""
        with self._lock:
            self._paused = paused

    def skip(self) -> None:
        """Fade the current track out over 150 ms, then move on."""
        with self._lock:
            if self._current is not None and self._skip_fade == 0:
                self._skip_fade = _SKIP_FADE_FRAMES

    def play_liner(self, audio: np.ndarray, duck_db: float) -> None:
        """Mix *audio* over the music, ducking the music by *duck_db* decibels."""
        with self._lock:
            self._liner = audio.astype(np.float32, copy=False)
            self._liner_pos = 0
            self._duck = float(10 ** (duck_db / 20.0))

    def start_set(self) -> None:
        """Begin pulling tracks from ``on_need_track``."""
        with self._lock:
            self._playing = True
            self._paused = False

    def stop_set(self) -> None:
        """Drop the current track and liner and emit silence."""
        with self._lock:
            self._playing = False
            self._current = None
            self._pos = 0
            self._skip_fade = 0
            self._liner = None

    def _advance(self) -> None:
        self._current = self._events.on_need_track()
        self._pos = 0
        self._skip_fade = 0
        if self._current is not None:
            self._events.on_track_start(self._current)

    def _music(self, frames: int) -> np.ndarray:
        out = np.zeros((frames, 2), np.float32)
        filled = 0
        while filled < frames and self._playing:
            if self._current is None:
                self._advance()
                if self._current is None:
                    break
            audio = self._current.audio
            take = min(frames - filled, len(audio) - self._pos)
            if take > 0:
                chunk = audio[self._pos : self._pos + take]
                if self._skip_fade:
                    remaining = self._skip_fade
                    ramp = np.clip(
                        (remaining - np.arange(take)) / _SKIP_FADE_FRAMES, 0.0, 1.0
                    ).astype(np.float32)
                    chunk = chunk * ramp[:, None]
                    self._skip_fade = max(0, remaining - take)
                    if self._skip_fade == 0:
                        self._pos = len(audio)
                out[filled : filled + take] = chunk
                self._pos += take
                filled += take
            if self._pos >= len(audio):
                self._current = None
        return out

    def render_block(self) -> np.ndarray:
        """Return the next block without pacing; used by :meth:`run` and tests."""
        with self._lock:
            if self._paused or not self._playing:
                return np.zeros((self.BLOCK, 2), np.float32)
            block = self._music(self.BLOCK)
            low, mid, high = self._eq_gains()
            engaged = self._eq_filters is not None and (low, mid, high) != (1.0, 1.0, 1.0)
            if engaged and not self._eq_engaged:
                reset_eq_state(self._eq_state)
            self._eq_engaged = engaged
            if engaged:
                block = apply_eq(block, self._eq_filters, low, mid, high, state=self._eq_state)
            if self._liner is not None:
                piece = self._liner[self._liner_pos : self._liner_pos + self.BLOCK]
                block = block * self._duck
                block[: len(piece)] += piece
                self._liner_pos += self.BLOCK
                if self._liner_pos >= len(self._liner):
                    self._liner = None
            np.clip(block, -1.0, 1.0, out=block)
            if self._current is not None:
                self._events.on_position(self._pos)
            return block.astype(np.float32, copy=False)

    def run(self, stop: threading.Event) -> None:
        """Emit blocks in real time until *stop* is set.

        An output that raises is logged and removed; the bus keeps running.
        """
        period = self.BLOCK / SAMPLE_RATE
        next_due = self._clock.now()
        while not stop.is_set():
            block = self.render_block()
            with self._lock:
                outputs = list(self._outputs)
            for output in outputs:
                try:
                    output.write(block)
                except Exception:
                    logger.exception("Mix bus output failed; removing it")
                    self.remove_output(output)
            next_due += period
            delay = next_due - self._clock.now()
            if delay > 0:
                self._clock.sleep(delay)
            elif delay < -1.0:
                next_due = self._clock.now()
```

- [ ] **Step 4: Run tests**

Run: `uv run --frozen pytest tests/unit/test_mixbus.py -q -o addopts=""`
Expected: all PASS.

- [ ] **Step 5: Commit**

```bash
git add src/autodj/mixbus.py tests/unit/test_mixbus.py
git commit -m "feat: add a clock-paced stereo mix bus"
```

---

### Task 5: Sound-card output and server-audio on the mix bus

**Files:**
- Create: `src/autodj/sound_output.py`
- Modify: `src/autodj/player.py` (`run` 914-1016; delete `_play_with_crossfade` and `_stream_audio`)
- Test: `tests/unit/test_sound_output.py`, extend `tests/unit/test_player.py`

**Interfaces:**
- Consumes: `MixBus`, `BusEvents`, `RenderedTrack`, `Player._render_track`.
- Produces:
  - `SoundDeviceOutput(state: PlayerState, device: str | None, buffer_blocks: int = 10, stream_factory: Callable[..., Any] | None = None)` with `write`, `close`, and `_fill(outdata, frames)` (the callback body, testable).
  - `Player.bus: MixBus | None` (created in `run` when not dry-run, or supplied by the station in stream mode).
  - `Player._next_rendered() -> RenderedTrack | None` — used as `on_need_track`: picks the next entry, renders from the carried offset, handles late `queued_next`.
  - `Player._on_track_start(rendered: RenderedTrack) -> None` — sets `current_track`, `next_track`, `_current_sr = 44100`, resets `_playback_pos[0] = 0`, records played/M3U/history, sets `_last_transition_fx`, and calls `self.on_track_started(rendered.entry)` if set (bridge hook, Task 11).
  - `Player._on_position(frames: int) -> None` — `self._playback_pos[0] = frames`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/unit/test_sound_output.py
"""Ring-buffered sound-card output."""

from __future__ import annotations

import numpy as np

from autodj.player import PlayerState
from autodj.sound_output import SoundDeviceOutput


def _output(state: PlayerState | None = None) -> SoundDeviceOutput:
    return SoundDeviceOutput(state or PlayerState(), device=None, buffer_blocks=3, stream_factory=lambda **_kw: None)


def test_fill_plays_buffered_blocks_in_order() -> None:
    out = _output()
    out.write(np.full((882, 2), 0.1, np.float32))
    out.write(np.full((882, 2), 0.2, np.float32))
    buf = np.zeros((882, 2), np.float32)
    out._fill(buf, 882)
    np.testing.assert_allclose(buf, 0.1)
    out._fill(buf, 882)
    np.testing.assert_allclose(buf, 0.2)


def test_underrun_pads_silence() -> None:
    out = _output()
    buf = np.ones((882, 2), np.float32)
    out._fill(buf, 882)
    np.testing.assert_array_equal(buf, 0.0)


def test_overrun_drops_oldest() -> None:
    out = _output()
    for value in (0.1, 0.2, 0.3, 0.4):
        out.write(np.full((882, 2), value, np.float32))
    buf = np.zeros((882, 2), np.float32)
    out._fill(buf, 882)
    np.testing.assert_allclose(buf, 0.2)


def test_volume_and_mute_are_applied() -> None:
    state = PlayerState()
    state.volume = 0.5
    out = _output(state)
    out.write(np.full((882, 2), 0.4, np.float32))
    buf = np.zeros((882, 2), np.float32)
    out._fill(buf, 882)
    np.testing.assert_allclose(buf, 0.2)
    state.is_muted = True
    out.write(np.full((882, 2), 0.4, np.float32))
    out._fill(buf, 882)
    np.testing.assert_array_equal(buf, 0.0)


def test_partial_frames_request() -> None:
    out = _output()
    out.write(np.arange(882 * 2, dtype=np.float32).reshape(882, 2) / 10_000)
    first = np.zeros((500, 2), np.float32)
    second = np.zeros((500, 2), np.float32)
    out._fill(first, 500)
    out._fill(second, 500)
    assert second[381, 0] == 0.0
    assert first[0, 1] == np.float32(1 / 10_000)
```

Add to `tests/unit/test_player.py`:

```python
def test_next_rendered_carries_offset_between_tracks(monkeypatch):
    from autodj.mixbus import RenderedTrack
    from autodj import player as player_mod
    p = player_mod.Player.__new__(player_mod.Player)
    p._state = player_mod.PlayerState()
    first, second, third = MagicMock(), MagicMock(), MagicMock()
    picks = iter([second, third])
    p._pick_next = lambda _current: next(picks)
    p._last_pick_mode = "similar"
    p._honour_late_queued_next = lambda picked, _q: picked
    calls = []

    def fake_render(current, nxt, offset):
        calls.append((current, nxt, offset))
        return RenderedTrack(current, np.zeros((10, 2), np.float32), nxt, 99, "")

    p._render_track = fake_render
    p._pending_entry = first
    p._pending_offset = 0
    p._next_rendered()
    p._next_rendered()
    assert calls == [(first, second, 0), (second, third, 99)]
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run --frozen pytest tests/unit/test_sound_output.py tests/unit/test_player.py -q -o addopts=""`
Expected: FAIL — `ModuleNotFoundError: No module named 'autodj.sound_output'`.

- [ ] **Step 3: Implement `src/autodj/sound_output.py`**

```python
"""Long-lived stereo sound-card output fed by the mix bus."""

from __future__ import annotations

import collections
import logging
import threading
from collections.abc import Callable
from typing import Any

import numpy as np

from autodj.player import PlayerState
from autodj.stereo import SAMPLE_RATE

logger = logging.getLogger(__name__)


class SoundDeviceOutput:
    """Play mix-bus blocks through one open sounddevice stream.

    A small ring buffer absorbs drift between the sound card's clock and the
    mix bus clock: an empty buffer plays silence and a full one drops its
    oldest block.
    """

    def __init__(
        self,
        state: PlayerState,
        device: str | None,
        buffer_blocks: int = 10,
        stream_factory: Callable[..., Any] | None = None,
    ) -> None:
        """Open the output stream.

        Args:
            state: Player state for volume and mute.
            device: sounddevice device name, or ``None`` for the default.
            buffer_blocks: Ring buffer size in 20 ms blocks.
            stream_factory: Builds the stream; defaults to ``sounddevice.OutputStream``.
        """
        self._state = state
        self._blocks: collections.deque[np.ndarray] = collections.deque(maxlen=buffer_blocks)
        self._head: np.ndarray | None = None
        self._head_pos = 0
        self._lock = threading.Lock()
        if stream_factory is None:  # pragma: no cover -- real audio hardware
            import sounddevice as sd

            stream_factory = sd.OutputStream
        self._stream = stream_factory(
            samplerate=SAMPLE_RATE,
            channels=2,
            dtype="float32",
            callback=self._callback,
            device=device,
        )
        if self._stream is not None:  # pragma: no cover -- real audio hardware
            self._stream.start()

    def write(self, block: np.ndarray) -> None:
        """Queue *block*; drops the oldest block when the buffer is full."""
        with self._lock:
            self._blocks.append(block)

    def _fill(self, outdata: np.ndarray, frames: int) -> None:
        """Copy *frames* buffered frames into *outdata*, padding with silence."""
        filled = 0
        with self._lock:
            while filled < frames:
                if self._head is None or self._head_pos >= len(self._head):
                    if not self._blocks:
                        break
                    self._head = self._blocks.popleft()
                    self._head_pos = 0
                take = min(frames - filled, len(self._head) - self._head_pos)
                outdata[filled : filled + take] = self._head[self._head_pos : self._head_pos + take]
                self._head_pos += take
                filled += take
        outdata[filled:frames] = 0.0
        if self._state.is_muted:
            outdata[:frames] = 0.0
        elif self._state.volume < 1.0:
            outdata[:frames] *= self._state.volume

    def _callback(self, outdata, frames, _time_info, _status) -> None:  # type: ignore[no-untyped-def]  # pragma: no cover
        self._fill(outdata, frames)

    def close(self) -> None:
        """Stop and close the stream."""
        if self._stream is not None:  # pragma: no cover -- real audio hardware
            try:
                self._stream.stop()
                self._stream.close()
            except Exception:
                logger.exception("Closing the sound output failed")
```

Fix `test_overrun_drops_oldest`: with `buffer_blocks=3` and four writes, the deque keeps 0.2, 0.3, 0.4, so the first fill plays 0.2. The test as written expects that.

- [ ] **Step 4: Rewire `Player.run` for server audio**

Replace the non-dry-run branch of `run` (the Rich `Live` loop that calls `_play_with_crossfade`) with:

```python
        self._pending_entry = current
        self._pending_offset = 0
        self.bus = MixBus(
            BusEvents(
                on_track_start=self._on_track_start,
                on_position=self._on_position,
                on_need_track=self._next_rendered,
            ),
            eq_gains=lambda: (self._eq_low, self._eq_mid, self._eq_high),
        )
        output = SoundDeviceOutput(
            self._state, getattr(self._cfg.playback, "audio_device", None) or None
        )
        self.bus.add_output(output)
        self.bus.start_set()
        stop = threading.Event()
        watcher = threading.Thread(
            target=self._stop_when_requested, args=(stop,), name="autodj-bus-stop", daemon=True
        )
        watcher.start()
        try:
            self.bus.run(stop)
        finally:
            output.close()
```

and add:

```python
    def _stop_when_requested(self, stop: threading.Event) -> None:
        """Set *stop* once the player is asked to stop."""
        while not self._state.should_stop:
            time.sleep(0.1)
        stop.set()

    def _next_rendered(self) -> RenderedTrack | None:
        """Pick and render the next track for the mix bus.

        Returns:
            The rendered track, or ``None`` when nothing could be rendered.
        """
        for _attempt in range(5):
            current = self._pending_entry
            if current is None:
                return None
            next_entry = self._pick_next(current)
            from_queue = self._last_pick_mode == "queue"
            next_entry = self._honour_late_queued_next(next_entry, from_queue)
            rendered = self._render_track(current, next_entry, self._pending_offset)
            self._pending_entry = next_entry
            self._pending_offset = rendered.next_start_offset if rendered else 0
            if rendered is not None:
                return rendered
        return None

    def _on_track_start(self, rendered: RenderedTrack) -> None:
        """Update state when the mix bus starts playing *rendered*."""
        self._previous_track = self._state.current_track
        self._state.current_track = rendered.entry
        self._state.next_track = rendered.next_entry
        self._current_sr = SAMPLE_RATE
        self._playback_pos[0] = 0
        self._playback_len = len(rendered.audio)
        self._last_transition_fx = rendered.transition_fx
        self._state.record_played(rendered.entry)
        self._state.track_number += 1
        if self._export_m3u:
            _append_m3u_entry(self._export_m3u, rendered.entry)
        if self._history_file:
            _append_history_entry(self._history_file, rendered.entry, datetime.now())
        callback = getattr(self, "on_track_started", None)
        if callback is not None:
            callback(rendered.entry)

    def _on_position(self, frames: int) -> None:
        """Record mix-bus progress through the current track."""
        self._playback_pos[0] = frames
```

Set `self.bus: MixBus | None = None`, `self.on_track_started: Callable[[IndexEntry], None] | None = None`, `self._pending_entry = None`, `self._pending_offset = 0` in `__init__`. The seed was already recorded before the loop (`run` 958-962); remove the duplicate `record_played(seed)` for the non-dry-run path because `_on_track_start` now records it, or skip recording in `_on_track_start` for the very first track by checking `self._state.track_number == 0 and rendered.entry is seed`. Choose the second option and add a unit test `test_seed_recorded_once` asserting `record_played` is called once for the seed.

Server-audio skip: `PlayerBridge.skip()` (non-dry-run) must call `player.bus.skip()` when `player.bus` is set, instead of `player._skip_event.set()`. Seek in server-audio: `seek_to`/`seek_relative` write `_playback_pos[0]`; add `MixBus.seek(frames: int)` that sets `self._pos` within the current track (clamped), and have `Player.seek_to` call `self.bus.seek(...)` when `self.bus` is set. Add tests for both in `tests/unit/test_mixbus.py` (`test_seek_clamps_within_track`) and `tests/integration/test_bridge_branches.py` (`test_skip_uses_bus_when_present`).

Delete `_play_with_crossfade` and `_stream_audio`. Remove the now-unused `sd.OutputStream` import path only if nothing else uses it.

- [ ] **Step 5: Run tests and the whole suite**

Run: `uv run --frozen pytest tests/unit/test_sound_output.py tests/unit/test_player.py tests/unit/test_mixbus.py tests/integration/test_bridge_branches.py -q -o addopts=""`
Expected: PASS.
Run: `uv run --frozen python scripts/ci_pytest.py`
Expected: PASS with coverage gates met.

- [ ] **Step 6: Commit**

```bash
git add src/autodj/sound_output.py src/autodj/player.py src/autodj/mixbus.py src/autodj/_bridge.py tests
git commit -m "feat: play server audio in stereo through the mix bus"
```

---

### Task 6: Server-side liners

**Files:**
- Create: `src/autodj/liner_scheduler.py`
- Modify: `src/autodj/_bridge.py` (liner test route handler), `src/autodj/server.py` (liner test route calls the scheduler in server-mixed modes)
- Test: `tests/unit/test_liner_scheduler.py`

**Interfaces:**
- Consumes: `LinerTrigger`, `LinerLibrary.from_folder`, `LinerLibrary.pick(mode, rng=)`, `open_liner_file(root, name) -> OpenedLiner(file, stat_result)`, `MixBus.play_liner(audio, duck_db)`.
- Produces:
  - `decode_liner(root: Path, name: str, *, runner: Callable[..., subprocess.CompletedProcess[bytes]] = subprocess.run) -> np.ndarray` — `(n, 2)` float32 at 44100. Reads the file through `open_liner_file`, tries `soundfile` on the open file object first, falls back to ffmpeg (`["ffmpeg", "-v", "error", "-i", "pipe:0", "-f", "f32le", "-ac", "2", "-ar", "44100", "pipe:1"]`, bytes on stdin). Raises `LinerDecodeError` on failure.
  - `LinerScheduler(cfg_playback, folder: Path, bus: MixBus, clock: Callable[[], float] = time.monotonic, rng: random.Random | None = None, decoder=decode_liner)`:
    - `on_track_start() -> None` — increments the track count and fires if the trigger says so.
    - `tick() -> None` — called once a second from the bridge broadcast loop; fires timed triggers.
    - `fire(name: str | None = None) -> str | None` — plays *name* or the next pick; returns the name played or `None`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/unit/test_liner_scheduler.py
"""Server-side liner triggers, picking and decoding."""

from __future__ import annotations

import random
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import numpy as np
import pytest
import soundfile as sf

from autodj.liner_scheduler import LinerDecodeError, LinerScheduler, decode_liner


def _cfg(**over):
    base = dict(
        liners_enabled=True,
        liners_every_n_songs=2,
        liners_every_minutes=None,
        liners_random_min_minutes=None,
        liners_random_max_minutes=None,
        liners_pick_mode="sequential",
        liners_duck_db=-10.0,
    )
    base.update(over)
    return SimpleNamespace(**base)


def _folder(tmp_path: Path) -> Path:
    for name in ("a.wav", "b.wav"):
        sf.write(tmp_path / name, np.zeros(441, np.float32), 44100)
    return tmp_path


def test_decode_wav_is_stereo(tmp_path: Path) -> None:
    sf.write(tmp_path / "x.wav", np.full(441, 0.25, np.float32), 22050)
    out = decode_liner(tmp_path, "x.wav")
    assert out.ndim == 2 and out.shape[1] == 2
    assert abs(len(out) - 882) <= 2


def test_decode_falls_back_to_ffmpeg(tmp_path: Path) -> None:
    (tmp_path / "x.m4a").write_bytes(b"not really m4a")
    pcm = np.full((10, 2), 0.5, np.float32).tobytes()
    runner = MagicMock(return_value=SimpleNamespace(returncode=0, stdout=pcm, stderr=b""))
    out = decode_liner(tmp_path, "x.m4a", runner=runner)
    assert out.shape == (10, 2)
    args = runner.call_args.args[0]
    assert args[0] == "ffmpeg" and "pipe:0" in args


def test_undecodable_liner_raises(tmp_path: Path) -> None:
    (tmp_path / "x.m4a").write_bytes(b"junk")
    runner = MagicMock(return_value=SimpleNamespace(returncode=1, stdout=b"", stderr=b"bad"))
    with pytest.raises(LinerDecodeError):
        decode_liner(tmp_path, "x.m4a", runner=runner)


def test_every_n_songs_fires_and_rotates(tmp_path: Path) -> None:
    bus = MagicMock()
    sched = LinerScheduler(_cfg(), _folder(tmp_path), bus, clock=lambda: 0.0, rng=random.Random(1))
    sched.on_track_start()
    assert bus.play_liner.call_count == 0
    sched.on_track_start()
    assert bus.play_liner.call_count == 1
    assert bus.play_liner.call_args.kwargs["duck_db"] == -10.0


def test_timed_trigger_fires_on_tick(tmp_path: Path) -> None:
    now = [0.0]
    bus = MagicMock()
    sched = LinerScheduler(
        _cfg(liners_every_n_songs=None, liners_every_minutes=1.0),
        _folder(tmp_path), bus, clock=lambda: now[0],
    )
    sched.tick()
    assert bus.play_liner.call_count == 0
    now[0] = 61.0
    sched.tick()
    assert bus.play_liner.call_count == 1


def test_bad_liner_is_skipped_and_music_carries_on(tmp_path: Path) -> None:
    bus = MagicMock()

    def broken(_root: Path, _name: str) -> np.ndarray:
        raise LinerDecodeError("corrupt")

    sched = LinerScheduler(_cfg(liners_every_n_songs=1), _folder(tmp_path), bus, decoder=broken)
    sched.on_track_start()
    bus.play_liner.assert_not_called()


def test_disabled_never_fires(tmp_path: Path) -> None:
    bus = MagicMock()
    sched = LinerScheduler(_cfg(liners_enabled=False, liners_every_n_songs=1), _folder(tmp_path), bus)
    sched.on_track_start()
    sched.tick()
    bus.play_liner.assert_not_called()


def test_fire_by_name_plays_that_file(tmp_path: Path) -> None:
    bus = MagicMock()
    sched = LinerScheduler(_cfg(), _folder(tmp_path), bus)
    assert sched.fire("b.wav") == "b.wav"
    assert bus.play_liner.call_count == 1
```

Before writing, confirm the liner config attribute names on `cfg.playback` (`config.py:358-365`) and rename `liners_*` fields in `_cfg()` to match exactly.

- [ ] **Step 2: Run to verify failure**

Run: `uv run --frozen pytest tests/unit/test_liner_scheduler.py -q -o addopts=""`
Expected: FAIL — `ModuleNotFoundError`.

- [ ] **Step 3: Implement `src/autodj/liner_scheduler.py`**

```python
"""Server-side voice-liner scheduling for server-mixed playback."""

from __future__ import annotations

import io
import logging
import random
import subprocess
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import numpy as np
import soundfile as sf

from autodj.liner_files import open_liner_file
from autodj.liners import LinerLibrary, LinerTrigger
from autodj.stereo import SAMPLE_RATE, to_stereo

logger = logging.getLogger(__name__)


class LinerDecodeError(Exception):
    """A liner file could not be decoded."""


def decode_liner(
    root: Path,
    name: str,
    *,
    runner: Callable[..., Any] = subprocess.run,
) -> np.ndarray:
    """Decode liner *name* from *root* to stereo float32 at 44.1 kHz.

    Args:
        root: Liner folder.
        name: Plain liner file name.
        runner: ``subprocess.run`` replacement for tests.

    Returns:
        ``(frames, 2)`` float32 audio.

    Raises:
        LinerDecodeError: If neither soundfile nor ffmpeg can decode it.
    """
    opened = open_liner_file(root, name)
    try:
        data = opened.file.read()
    finally:
        opened.file.close()
    try:
        audio, sr = sf.read(io.BytesIO(data), dtype="float32", always_2d=True)
        audio = to_stereo(audio)
        if int(sr) != SAMPLE_RATE:
            import librosa

            audio = librosa.resample(audio, orig_sr=int(sr), target_sr=SAMPLE_RATE, axis=0)
        return to_stereo(audio)
    except Exception:
        result = runner(
            ["ffmpeg", "-v", "error", "-i", "pipe:0", "-f", "f32le", "-ac", "2",
             "-ar", str(SAMPLE_RATE), "pipe:1"],
            input=data,
            capture_output=True,
            check=False,
            timeout=30,
        )
        if result.returncode != 0 or not result.stdout:
            raise LinerDecodeError(f"cannot decode liner {name!r}") from None
        return np.frombuffer(result.stdout, dtype=np.float32).reshape(-1, 2).copy()


class LinerScheduler:
    """Fire liners into the mix bus on the configured triggers."""

    def __init__(
        self,
        playback_cfg: Any,
        folder: Path,
        bus: Any,
        clock: Callable[[], float] = time.monotonic,
        rng: random.Random | None = None,
        decoder: Callable[[Path, str], np.ndarray] = decode_liner,
    ) -> None:
        """Build a scheduler.

        Args:
            playback_cfg: ``cfg.playback`` with the ``liners_*`` settings.
            folder: Liner folder.
            bus: Mix bus with ``play_liner(audio, duck_db=...)``.
            clock: Monotonic seconds.
            rng: Random source for picks and random windows.
            decoder: Liner decoder.
        """
        self._cfg = playback_cfg
        self._folder = folder
        self._bus = bus
        self._clock = clock
        self._rng = rng or random.Random()
        self._decoder = decoder
        self._tracks_since = 0
        self._last_fire = clock()
        self._library = LinerLibrary.from_folder(folder)
        self._random_target = self._trigger().roll_random_target(rng=self._rng)

    def _trigger(self) -> LinerTrigger:
        return LinerTrigger(
            every_n_songs=self._cfg.liners_every_n_songs,
            every_minutes=self._cfg.liners_every_minutes,
            random_min_minutes=self._cfg.liners_random_min_minutes,
            random_max_minutes=self._cfg.liners_random_max_minutes,
            enabled=bool(self._cfg.liners_enabled),
        )

    def _maybe_fire(self) -> None:
        minutes = (self._clock() - self._last_fire) / 60.0
        if self._trigger().should_fire(
            track_count=self._tracks_since,
            minutes_since_last=minutes,
            random_target_minutes=self._random_target,
        ):
            self.fire()

    def on_track_start(self) -> None:
        """Count a track and fire a liner if a trigger is due."""
        self._tracks_since += 1
        self._maybe_fire()

    def tick(self) -> None:
        """Check timed triggers; call about once a second."""
        self._maybe_fire()

    def fire(self, name: str | None = None) -> str | None:
        """Play liner *name*, or the next pick, into the mix bus.

        Returns:
            The liner name played, or ``None`` when nothing played.
        """
        if name is None:
            self._library = LinerLibrary.from_folder(self._folder)
            picked = self._library.pick(self._cfg.liners_pick_mode, rng=self._rng)
            if picked is None:
                return None
            name = picked.name
        try:
            audio = self._decoder(self._folder, name)
        except Exception:
            logger.warning("Skipping liner %s: it could not be decoded", name, exc_info=True)
            return None
        self._bus.play_liner(audio, duck_db=float(self._cfg.liners_duck_db))
        self._tracks_since = 0
        self._last_fire = self._clock()
        self._random_target = self._trigger().roll_random_target(rng=self._rng)
        return name
```

Check `LinerTrigger.should_fire`'s track-count semantics (`liners.py:68`) — whether it fires at `track_count >= every_n_songs` or `track_count % every_n_songs == 0` — and adjust `test_every_n_songs_fires_and_rotates` to match.

- [ ] **Step 4: Wire into the bridge**

In `PlayerBridge`, add `liner_scheduler: Any = None` (set by the server in server-mixed modes, Task 11). The existing "Test liner" route: when `bridge.liner_scheduler is not None`, call `bridge.liner_scheduler.fire(name)` and return `{"played": name}` instead of telling the browser to play it. Find the route with `grep -n "liners" src/autodj/server.py` and follow its body/response model. Add an integration test in `tests/integration/test_server_edges.py` asserting the route calls `fire` when a scheduler is set.

- [ ] **Step 5: Run tests**

Run: `uv run --frozen pytest tests/unit/test_liner_scheduler.py tests/integration -q -o addopts="" -k "liner"`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add src/autodj/liner_scheduler.py src/autodj/_bridge.py src/autodj/server.py tests
git commit -m "feat: fire voice liners into the server mix"
```

---

### Task 7: Stream configuration, CLI flag and doctor check

**Files:**
- Modify: `src/autodj/config.py` (new `StreamConfig`; `AutoDJConfig`; `_build_config` 1005-1042; `ENVIRONMENT_OVERLAY` 970), `src/autodj/cli.py` (`serve` options near 1805; before `serve(...)` ~1980), `src/autodj/doctor.py` (`run_doctor` 769-790), `config.toml.example`, `tests/integration/_helpers.py`
- Test: `tests/unit/test_stream_config.py`, extend `tests/unit/test_doctor.py`, `tests/unit/test_cli_flag_contract.py` if it enumerates serve flags

**Interfaces:**
- Produces:

```python
STREAM_BITRATES = (128, 192, 256, 320)

@dataclass
class StreamConfig:
    enabled: bool = False
    bitrate: int = 320
    idle_grace_seconds: float = 30.0
    max_listeners: int = 8
    station_name: str = "AutoDJ"
    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> StreamConfig
```

  - `AutoDJConfig.stream: StreamConfig`.
  - Environment: `AUTODJ_STREAM_ENABLED` (bool), `AUTODJ_STREAM_BITRATE` (int), `AUTODJ_STREAM_IDLE_GRACE_SECONDS` (float), `AUTODJ_STREAM_MAX_LISTENERS` (int), `AUTODJ_STREAM_STATION_NAME` (str).
  - `config.parse_env_bool(value: str) -> bool` — accepts `1/0/true/false/yes/no/on/off` case-insensitively; raises `ValueError` otherwise.
  - CLI: `serve --stream/--no-stream` (default `None` → config).
  - Doctor: `_stream_check(cfg) -> DoctorCheck` named `"stream"`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/unit/test_stream_config.py
"""[stream] configuration."""

from __future__ import annotations

from pathlib import Path

import pytest

from autodj.config import StreamConfig, load_config, parse_env_bool


def test_defaults() -> None:
    cfg = StreamConfig()
    assert (cfg.enabled, cfg.bitrate, cfg.idle_grace_seconds, cfg.max_listeners, cfg.station_name) == (
        False, 320, 30.0, 8, "AutoDJ",
    )


@pytest.mark.parametrize("bitrate", [128, 192, 256, 320])
def test_valid_bitrates(bitrate: int) -> None:
    assert StreamConfig.from_dict({"bitrate": bitrate}).bitrate == bitrate


@pytest.mark.parametrize(
    "data",
    [{"bitrate": 64}, {"bitrate": "320"}, {"idle_grace_seconds": -1}, {"max_listeners": 0},
     {"station_name": ""}, {"enabled": "yes"}, {"unknown": 1}],
)
def test_invalid_values_rejected(data: dict) -> None:
    with pytest.raises((ValueError, TypeError)):
        StreamConfig.from_dict(data)


def test_toml_and_environment(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    path.write_text("[stream]\nenabled = true\nbitrate = 192\n", encoding="utf-8")
    cfg = load_config(path, environ={"AUTODJ_STREAM_BITRATE": "256", "AUTODJ_STREAM_ENABLED": "off"})
    assert cfg.stream.bitrate == 256
    assert cfg.stream.enabled is False


@pytest.mark.parametrize(("raw", "expected"), [("1", True), ("TRUE", True), ("on", True), ("no", False), ("0", False)])
def test_parse_env_bool(raw: str, expected: bool) -> None:
    assert parse_env_bool(raw) is expected


def test_parse_env_bool_rejects_other() -> None:
    with pytest.raises(ValueError):
        parse_env_bool("maybe")
```

Doctor tests (append to `tests/unit/test_doctor.py`, following its existing `monkeypatch.setattr(doctor.shutil, "which", ...)` style):

```python
def test_stream_check_passes_when_disabled(base_cfg):
    assert doctor._stream_check(base_cfg).status is doctor.CheckStatus.PASS


def test_stream_check_fails_without_ffmpeg(base_cfg, monkeypatch):
    base_cfg.stream.enabled = True
    monkeypatch.setattr(doctor.shutil, "which", lambda _n: None)
    result = doctor._stream_check(base_cfg)
    assert result.status is doctor.CheckStatus.FAIL
    assert "ffmpeg" in result.summary.lower()


def test_stream_check_warns_on_loopback_only(base_cfg, monkeypatch):
    base_cfg.stream.enabled = True
    monkeypatch.setattr(doctor.shutil, "which", lambda _n: "/usr/bin/ffmpeg")
    base_cfg.server = replace(base_cfg.server, host="127.0.0.1")
    assert doctor._stream_check(base_cfg).status is doctor.CheckStatus.WARN
```

Use whatever config fixture `test_doctor.py` already provides in place of `base_cfg` (read the top of the file first).

- [ ] **Step 2: Run to verify failure**

Run: `uv run --frozen pytest tests/unit/test_stream_config.py -q -o addopts=""`
Expected: FAIL — `ImportError: cannot import name 'StreamConfig'`.

- [ ] **Step 3: Implement**

`config.py`:

```python
STREAM_BITRATES = (128, 192, 256, 320)
_STREAM_KEYS = frozenset({"enabled", "bitrate", "idle_grace_seconds", "max_listeners", "station_name"})


def parse_env_bool(value: str) -> bool:
    """Parse an environment boolean.

    Args:
        value: Raw environment text.

    Returns:
        The boolean it names.

    Raises:
        ValueError: If *value* is not a recognised boolean word.
    """
    text = value.strip().lower()
    if text in {"1", "true", "yes", "on"}:
        return True
    if text in {"0", "false", "no", "off"}:
        return False
    raise ValueError(f"not a boolean: {value!r}")


@dataclass
class StreamConfig:
    """Radio stream settings (``[stream]``).

    Attributes:
        enabled: Serve in stream mode.
        bitrate: MP3 bitrate in kbps, one of :data:`STREAM_BITRATES`.
        idle_grace_seconds: Seconds without listeners before the set stops.
        max_listeners: Concurrent listener limit.
        station_name: Name sent to players as ``icy-name``.
    """

    enabled: bool = False
    bitrate: int = 320
    idle_grace_seconds: float = 30.0
    max_listeners: int = 8
    station_name: str = "AutoDJ"

    def __post_init__(self) -> None:
        """Validate every field."""
        if not isinstance(self.enabled, bool):
            raise TypeError("stream.enabled must be true or false")
        if isinstance(self.bitrate, bool) or not isinstance(self.bitrate, int):
            raise TypeError("stream.bitrate must be an integer")
        if self.bitrate not in STREAM_BITRATES:
            raise ValueError(f"stream.bitrate must be one of {STREAM_BITRATES}, got {self.bitrate}")
        if isinstance(self.idle_grace_seconds, bool) or not isinstance(self.idle_grace_seconds, int | float):
            raise TypeError("stream.idle_grace_seconds must be a number")
        if not 0 < float(self.idle_grace_seconds) <= 3600:
            raise ValueError("stream.idle_grace_seconds must be between 0 and 3600")
        if isinstance(self.max_listeners, bool) or not isinstance(self.max_listeners, int):
            raise TypeError("stream.max_listeners must be an integer")
        if not 1 <= self.max_listeners <= 100:
            raise ValueError("stream.max_listeners must be between 1 and 100")
        if not isinstance(self.station_name, str) or not self.station_name.strip():
            raise ValueError("stream.station_name must be a non-empty string")
        self.idle_grace_seconds = float(self.idle_grace_seconds)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> StreamConfig:
        """Build from the ``[stream]`` table.

        Raises:
            ValueError: On an unknown key or invalid value.
            TypeError: On a wrongly typed value.
        """
        unknown = set(data) - _STREAM_KEYS
        if unknown:
            raise ValueError(f"unknown [stream] keys: {sorted(unknown)}")
        return cls(**data)
```

Add `stream: StreamConfig = field(default_factory=StreamConfig)` to `AutoDJConfig`; add `"stream"` to the section tuple in `_build_config` and `stream=StreamConfig.from_dict(raw.get("stream", {}))` to the constructor call. Widen `ENVIRONMENT_OVERLAY`'s type to `dict[str, tuple[str, str, Callable[[str], object]]]` and add the five entries with converters `parse_env_bool`, `int`, `float`, `int`, `str`. Check `_environment_overlay` still types correctly under mypy.

`cli.py`: add

```python
@click.option(
    "--stream/--no-stream",
    "stream",
    default=None,
    help="Serve the live mix as an MP3 radio stream (server mixes; the page is a remote).",
)
```

Pass `stream` into `cmd_serve`; after the config is staged, apply `cfg.stream.enabled = stream` when `stream is not None`. Before calling `serve(...)`:

```python
    if cfg.stream.enabled and shutil.which("ffmpeg") is None:
        raise click.ClickException(
            "Stream mode needs ffmpeg on the PATH. Install ffmpeg, or start without --stream."
        )
```

`serve(...)` gains `stream: bool` (Task 11 uses it); stream mode forces `no_playback=False` semantics for mixing (the player must not be dry-run).

`doctor.py`:

```python
def _stream_check(cfg: AutoDJConfig) -> DoctorCheck:
    """Check stream-mode prerequisites."""
    if not cfg.stream.enabled:
        return DoctorCheck("stream", CheckStatus.PASS, "stream mode off")
    if shutil.which("ffmpeg") is None:
        return DoctorCheck("stream", CheckStatus.FAIL, "FFmpeg missing; stream mode cannot start")
    if is_loopback_bind(cfg.server.host):
        return DoctorCheck(
            "stream",
            CheckStatus.WARN,
            "stream mode on, but the server only listens on this machine",
            "Speakers on your network cannot reach it. Use the LAN setup.",
        )
    return DoctorCheck("stream", CheckStatus.PASS, f"stream mode on at {cfg.stream.bitrate} kbps")
```

Register it in `run_doctor`'s tuple. Document `[stream]` in `config.toml.example` with each key and a one-line comment; the existing `test_config_examples.py` checks the environment block lists every overlay variable, so add the five variables there too. In `tests/integration/_helpers.py` set `cfg.stream = StreamConfig()` on the player mock.

- [ ] **Step 4: Run tests**

Run: `uv run --frozen pytest tests/unit/test_stream_config.py tests/unit/test_doctor.py tests/unit/test_config_examples.py tests/unit/test_cli_flag_contract.py tests/unit/test_config.py -q -o addopts=""`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/autodj/config.py src/autodj/cli.py src/autodj/doctor.py config.toml.example tests
git commit -m "feat: add stream configuration, --stream and a doctor check"
```

---

### Task 8: Stream secret

**Files:**
- Create: `src/autodj/stream_secret.py`
- Modify: `src/autodj/server.py:884`, `src/autodj/cli.py:656` (use `paired_devices_path`)
- Test: `tests/unit/test_stream_secret.py`

**Interfaces:**
- Produces:
  - `paired_devices_path(cfg: AutoDJConfig) -> Path` = `Path(cfg.index.index_dir) / ".paired-devices.sqlite3"`.
  - `stream_secret_path(cfg: AutoDJConfig) -> Path` = `Path(cfg.index.index_dir) / ".stream-secret"`.
  - `class StreamSecret`: `load_or_create(path: Path, *, forbidden: str | None = None) -> StreamSecret`, `.value: str`, `.matches(candidate: str) -> bool` (constant time), `.rotate() -> str` (writes atomically, returns the new value).
  - `class StreamSecretError(Exception)` — unreadable file.

- [ ] **Step 1: Write the failing tests**

```python
# tests/unit/test_stream_secret.py
"""Stream secret storage and checks."""

from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest

from autodj.stream_secret import StreamSecret, StreamSecretError


def test_created_on_first_load(tmp_path: Path) -> None:
    secret = StreamSecret.load_or_create(tmp_path / ".stream-secret")
    assert len(secret.value) == 43
    assert (tmp_path / ".stream-secret").read_text(encoding="ascii").strip() == secret.value


def test_reload_returns_same_value(tmp_path: Path) -> None:
    path = tmp_path / ".stream-secret"
    first = StreamSecret.load_or_create(path)
    assert StreamSecret.load_or_create(path).value == first.value


@pytest.mark.parametrize("content", ["", "   \n", "short", "has space in it and is long enough to pass length"])
def test_invalid_content_is_replaced(tmp_path: Path, content: str) -> None:
    path = tmp_path / ".stream-secret"
    path.write_text(content, encoding="ascii")
    secret = StreamSecret.load_or_create(path)
    assert len(secret.value) == 43
    assert secret.value != content.strip()


def test_never_equals_forbidden_value(tmp_path: Path) -> None:
    path = tmp_path / ".stream-secret"
    first = StreamSecret.load_or_create(path)
    again = StreamSecret.load_or_create(path, forbidden=first.value)
    assert again.value != first.value


def test_matches_is_exact(tmp_path: Path) -> None:
    secret = StreamSecret.load_or_create(tmp_path / ".stream-secret")
    assert secret.matches(secret.value)
    assert not secret.matches("")
    assert not secret.matches(secret.value[:-1])
    assert not secret.matches(secret.value + "x")


def test_rotate_invalidates_old(tmp_path: Path) -> None:
    path = tmp_path / ".stream-secret"
    secret = StreamSecret.load_or_create(path)
    old = secret.value
    new = secret.rotate()
    assert new != old and not secret.matches(old) and secret.matches(new)
    assert StreamSecret.load_or_create(path).value == new


@pytest.mark.skipif(os.name == "nt", reason="POSIX permissions")
def test_file_is_private(tmp_path: Path) -> None:
    StreamSecret.load_or_create(tmp_path / ".stream-secret")
    mode = stat.S_IMODE((tmp_path / ".stream-secret").stat().st_mode)
    assert mode == 0o600


def test_unreadable_file_fails_clearly(tmp_path: Path) -> None:
    path = tmp_path / ".stream-secret"
    path.mkdir()
    with pytest.raises(StreamSecretError):
        StreamSecret.load_or_create(path)
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run --frozen pytest tests/unit/test_stream_secret.py -q -o addopts=""`
Expected: FAIL — `ModuleNotFoundError`.

- [ ] **Step 3: Implement `src/autodj/stream_secret.py`**

```python
"""Listen-only secret for the radio stream URL."""

from __future__ import annotations

import hmac
import os
import re
import secrets
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from autodj.config import AutoDJConfig

_VALID = re.compile(r"^[A-Za-z0-9_-]{43}$")


class StreamSecretError(Exception):
    """The stream secret file exists but cannot be read or written."""


def paired_devices_path(cfg: AutoDJConfig) -> Path:
    """Return the paired-devices database path."""
    return Path(cfg.index.index_dir) / ".paired-devices.sqlite3"


def stream_secret_path(cfg: AutoDJConfig) -> Path:
    """Return the stream secret file path, beside the paired-devices database."""
    return Path(cfg.index.index_dir) / ".stream-secret"


def _new_value(forbidden: str | None) -> str:
    while True:
        value = secrets.token_urlsafe(32)
        if _VALID.match(value) and value != forbidden:
            return value


class StreamSecret:
    """A 43-character URL-safe secret stored in one private file."""

    def __init__(self, path: Path, value: str) -> None:
        """Wrap an already validated *value* stored at *path*."""
        self._path = path
        self.value = value

    @classmethod
    def load_or_create(cls, path: Path, *, forbidden: str | None = None) -> StreamSecret:
        """Load the secret, creating or replacing it when missing or invalid.

        Args:
            path: Secret file path.
            forbidden: A value the secret must never equal (the access token,
                or a value being replaced).

        Raises:
            StreamSecretError: If the file exists but cannot be read.
        """
        try:
            text = path.read_text(encoding="ascii").strip()
        except FileNotFoundError:
            text = ""
        except (OSError, UnicodeDecodeError) as exc:
            raise StreamSecretError(f"cannot read stream secret at {path}: {exc}") from exc
        if _VALID.match(text) and text != forbidden:
            return cls(path, text)
        secret = cls(path, _new_value(forbidden))
        secret._write()
        return secret

    def _write(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=self._path.parent, prefix=".stream-secret.")
        try:
            with os.fdopen(fd, "w", encoding="ascii") as handle:
                handle.write(self.value + "\n")
            os.chmod(tmp, 0o600)
            os.replace(tmp, self._path)
        except OSError as exc:
            Path(tmp).unlink(missing_ok=True)
            raise StreamSecretError(f"cannot write stream secret at {self._path}: {exc}") from exc

    def matches(self, candidate: str) -> bool:
        """Return whether *candidate* equals the secret, in constant time."""
        return hmac.compare_digest(candidate.encode("utf-8"), self.value.encode("ascii"))

    def rotate(self) -> str:
        """Replace the secret with a new one and return it."""
        self.value = _new_value(self.value)
        self._write()
        return self.value
```

Replace the two inlined paired-devices paths with `paired_devices_path(cfg)`.

- [ ] **Step 4: Run tests**

Run: `uv run --frozen pytest tests/unit/test_stream_secret.py tests/unit/test_pairing_cli.py tests/integration/test_pairing_server.py -q -o addopts=""`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/autodj/stream_secret.py src/autodj/server.py src/autodj/cli.py tests/unit/test_stream_secret.py
git commit -m "feat: store a listen-only stream secret beside the device database"
```

---

### Task 9: ICY metadata and MP3 frame helpers

**Files:**
- Create: `src/autodj/icy.py`
- Test: `tests/unit/test_icy.py`

**Interfaces:**
- Produces:
  - `METAINT = 16000`
  - `format_stream_title(artist: str, title: str) -> str` — `"Artist - Title"`, or the non-empty one alone; strips control characters; replaces `'` with `’` (U+2019) and `;` with `,`.
  - `encode_metadata(title: str) -> bytes` — `StreamTitle='...';` UTF-8, truncated at a character boundary so the payload fits 4080 bytes, padded with NULs to a multiple of 16, prefixed with one length byte (`len // 16`). An empty title encodes as `b"\x00"`.
  - `class IcyInterleaver(metaint: int = METAINT)`: `.feed(data: bytes, title: str) -> bytes` — returns data with a metadata block inserted every `metaint` audio bytes; a block carries the title only when it changed since the last block sent, else `b"\x00"`.
  - `mp3_frame_offset(data: bytes) -> int` — index of the first valid MPEG-1 Layer III frame header (sync `0xFFE`, layer bits `01`, bitrate index not 0/15, sample rate index not 3) whose following frame header is also valid; `-1` if none.

- [ ] **Step 1: Write the failing tests**

```python
# tests/unit/test_icy.py
"""ICY metadata framing and MP3 frame sync."""

from __future__ import annotations

from autodj.icy import IcyInterleaver, encode_metadata, format_stream_title, mp3_frame_offset


def test_title_format_and_escaping() -> None:
    assert format_stream_title("Daft Punk", "One More Time") == "Daft Punk - One More Time"
    assert format_stream_title("", "Solo") == "Solo"
    assert format_stream_title("Artist", "") == "Artist"
    assert format_stream_title("Guns N' Roses", "a;b\x07c") == "Guns N\u2019 Roses - a,bc"


def test_metadata_block_layout() -> None:
    block = encode_metadata("Artist - Title")
    payload = b"StreamTitle='Artist - Title';"
    assert block[0] == (len(block) - 1) // 16
    assert (len(block) - 1) % 16 == 0
    assert block[1 : 1 + len(payload)] == payload
    assert set(block[1 + len(payload) :]) <= {0}
    assert encode_metadata("") == b"\x00"


def test_long_non_ascii_title_is_truncated_on_char_boundary() -> None:
    block = encode_metadata("é" * 5000)
    assert len(block) - 1 <= 4080
    body = block[1:].rstrip(b"\x00")
    body.decode("utf-8")
    assert body.endswith(b"';")


def test_interleaver_inserts_every_metaint_bytes() -> None:
    icy = IcyInterleaver(metaint=10)
    out = icy.feed(b"A" * 25, "T1")
    first = encode_metadata("T1")
    assert out[:10] == b"A" * 10
    assert out[10 : 10 + len(first)] == first
    rest = out[10 + len(first) :]
    assert rest[:10] == b"A" * 10
    assert rest[10:11] == b"\x00"
    assert rest[11:] == b"A" * 5
    more = icy.feed(b"B" * 5, "T2")
    assert more[:5] == b"B" * 5
    assert more[5:] == encode_metadata("T2")


def _frame(bitrate_index: int = 9) -> bytes:
    header = bytes([0xFF, 0xFB, (bitrate_index << 4) | 0x00, 0x00])
    size = 144 * 128_000 // 44_100
    return header + b"\x00" * (size - 4)


def test_frame_offset_finds_real_frame() -> None:
    data = b"\x12\xff\x00" + _frame() + _frame()
    assert mp3_frame_offset(data) == 3


def test_frame_offset_rejects_false_sync() -> None:
    assert mp3_frame_offset(b"\xff\xfb\xf0\x00" + b"\x00" * 10) == -1
    assert mp3_frame_offset(b"") == -1
```

`_frame` uses bitrate index 9 (128 kbps) at 44.1 kHz, no padding: 417 bytes per frame.

- [ ] **Step 2: Run to verify failure**

Run: `uv run --frozen pytest tests/unit/test_icy.py -q -o addopts=""`
Expected: FAIL — `ModuleNotFoundError`.

- [ ] **Step 3: Implement `src/autodj/icy.py`**

```python
"""ICY (SHOUTcast) metadata framing and MP3 frame sync helpers."""

from __future__ import annotations

import unicodedata

METAINT = 16000
_MAX_META = 4080
_BITRATES_KBPS = (0, 32, 40, 48, 56, 64, 80, 96, 112, 128, 160, 192, 224, 256, 320, 0)
_SAMPLE_RATES = (44100, 48000, 32000, 0)


def format_stream_title(artist: str, title: str) -> str:
    """Return a safe ``Artist - Title`` string for ICY metadata."""
    parts = [part.strip() for part in (artist, title) if part and part.strip()]
    text = " - ".join(parts)
    text = "".join(ch for ch in text if unicodedata.category(ch)[0] != "C")
    return text.replace("'", "\u2019").replace(";", ",")


def encode_metadata(title: str) -> bytes:
    """Encode one ICY metadata block for *title* (empty → no change)."""
    if not title:
        return b"\x00"
    prefix, suffix = b"StreamTitle='", b"';"
    room = _MAX_META - len(prefix) - len(suffix)
    body = title.encode("utf-8")
    if len(body) > room:
        body = body[:room].decode("utf-8", errors="ignore").encode("utf-8")
    payload = prefix + body + suffix
    padded_len = -(-len(payload) // 16) * 16
    return bytes([padded_len // 16]) + payload.ljust(padded_len, b"\x00")


class IcyInterleaver:
    """Insert ICY metadata blocks into an MP3 byte stream for one listener."""

    def __init__(self, metaint: int = METAINT) -> None:
        """Start a fresh interleaver; the first block always carries the title."""
        self._metaint = metaint
        self._until_meta = metaint
        self._sent_title: str | None = None

    def feed(self, data: bytes, title: str) -> bytes:
        """Return *data* with metadata inserted at every *metaint* boundary."""
        out = bytearray()
        view = memoryview(data)
        while view:
            take = min(self._until_meta, len(view))
            out += view[:take]
            view = view[take:]
            self._until_meta -= take
            if self._until_meta == 0:
                if title != self._sent_title:
                    out += encode_metadata(title)
                    self._sent_title = title
                else:
                    out += b"\x00"
                self._until_meta = self._metaint
        return bytes(out)


def _header_ok(data: bytes, i: int) -> int:
    if i + 4 > len(data):
        return 0
    b1, b2, b3 = data[i + 1], data[i + 2], data[i + 3]
    if data[i] != 0xFF or (b1 & 0xE0) != 0xE0 or (b1 & 0x06) != 0x02:
        return 0
    bitrate = _BITRATES_KBPS[b2 >> 4]
    rate = _SAMPLE_RATES[(b2 >> 2) & 0x03]
    if not bitrate or not rate:
        return 0
    padding = (b2 >> 1) & 0x01
    del b3
    return 144 * bitrate * 1000 // rate + padding


def mp3_frame_offset(data: bytes) -> int:
    """Return the offset of the first MP3 frame confirmed by the next one, or -1."""
    for i in range(len(data) - 3):
        size = _header_ok(data, i)
        if size and (i + size == len(data) or _header_ok(data, i + size)):
            return i
    return -1
```

The test `test_frame_offset_finds_real_frame` has two frames; the second frame ends exactly at `len(data)`, which the `i + size == len(data)` branch accepts.

- [ ] **Step 4: Run tests**

Run: `uv run --frozen pytest tests/unit/test_icy.py -q -o addopts=""`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/autodj/icy.py tests/unit/test_icy.py
git commit -m "feat: add icy metadata framing and mp3 frame sync"
```

---

### Task 10: Stream output (encoder, fan-out, supervisor)

**Files:**
- Create: `src/autodj/stream.py`
- Test: `tests/unit/test_stream_output.py`, `tests/integration/test_stream_ffmpeg.py`

**Interfaces:**
- Consumes: `IcyInterleaver`, `mp3_frame_offset`, `format_stream_title`, `Output` protocol.
- Produces:

```python
class Encoder(Protocol):
    def write(self, pcm: bytes) -> None: ...
    def read(self, n: int) -> bytes: ...       # blocks; b"" on exit
    def close(self) -> None: ...
    @property
    def alive(self) -> bool: ...

def ffmpeg_encoder(bitrate: int) -> Encoder   # real ffmpeg; pragma no cover except via the ffmpeg test

class Listener:
    """One HTTP listener's bounded queue."""
    def __init__(self, icy: bool, max_bytes: int) -> None
    async def chunks(self) -> AsyncIterator[bytes]   # yields until closed
    def close(self) -> None
    closed: bool

class StreamOutput:
    def __init__(self, bitrate: int, max_listeners: int,
                 encoder_factory: Callable[[int], Encoder] = ffmpeg_encoder,
                 clock: Callable[[], float] = time.monotonic,
                 loop: asyncio.AbstractEventLoop | None = None) -> None
    def write(self, block: np.ndarray) -> None          # Output protocol: PCM → encoder
    def close(self) -> None
    def set_title(self, artist: str, title: str) -> None  # takes effect at the current encoder byte position
    def add_listener(self, icy: bool) -> Listener        # raises ListenerLimitError / EncoderUnavailableError
    def remove_listener(self, listener: Listener) -> None
    def disconnect_all(self) -> None
    def set_bitrate(self, bitrate: int) -> None          # restarts encoder, disconnects listeners
    @property
    def listener_count(self) -> int
    on_listener_change: Callable[[int], None] | None     # called with the new count
class ListenerLimitError(Exception): ...
class EncoderUnavailableError(Exception): ...
```

Title timing: the encoder output lags its input by the encoder's buffering. `set_title` records `(input_bytes_written_so_far, title)`; the reader converts input PCM bytes to expected output MP3 bytes with `ratio = (bitrate * 1000 / 8) / (44100 * 2 * 4)` and switches the current title once `output_bytes_read >= input_mark * ratio`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/unit/test_stream_output.py
"""Stream fan-out with a fake encoder."""

from __future__ import annotations

import asyncio
import threading

import numpy as np
import pytest

from autodj.icy import encode_metadata
from autodj.stream import EncoderUnavailableError, ListenerLimitError, StreamOutput


class FakeEncoder:
    """Echo PCM bytes back as 'encoded' bytes, one chunk per write."""

    def __init__(self, bitrate: int) -> None:
        self.bitrate = bitrate
        self._chunks: list[bytes] = []
        self._cond = threading.Condition()
        self._closed = False
        self.alive = True

    def write(self, pcm: bytes) -> None:
        with self._cond:
            self._chunks.append(pcm[:64])
            self._cond.notify_all()

    def read(self, n: int) -> bytes:
        with self._cond:
            while not self._chunks and not self._closed:
                self._cond.wait(0.05)
            return self._chunks.pop(0) if self._chunks else b""

    def close(self) -> None:
        with self._cond:
            self._closed = True
            self.alive = False
            self._cond.notify_all()


def _block(value: float = 0.1) -> np.ndarray:
    return np.full((882, 2), value, np.float32)


async def _collect(listener, n: int) -> bytes:
    got = bytearray()
    async for chunk in listener.chunks():
        got += chunk
        if len(got) >= n:
            break
    return bytes(got)


@pytest.fixture
async def output():
    out = StreamOutput(320, max_listeners=2, encoder_factory=FakeEncoder, loop=asyncio.get_running_loop())
    yield out
    out.close()


async def test_fan_out_same_bytes_to_every_listener(output: StreamOutput) -> None:
    a, b = output.add_listener(icy=False), output.add_listener(icy=False)
    output.write(_block())
    got_a, got_b = await asyncio.gather(_collect(a, 64), _collect(b, 64))
    assert got_a == got_b and len(got_a) >= 64


async def test_listener_limit(output: StreamOutput) -> None:
    output.add_listener(icy=False)
    output.add_listener(icy=False)
    with pytest.raises(ListenerLimitError):
        output.add_listener(icy=False)


async def test_slow_listener_is_dropped_others_continue(output: StreamOutput) -> None:
    slow = output.add_listener(icy=False)
    slow._max_bytes = 100
    fast = output.add_listener(icy=False)
    for _ in range(10):
        output.write(_block())
    await _collect(fast, 128)
    await asyncio.sleep(0.05)
    assert slow.closed
    assert output.listener_count == 1


async def test_icy_listener_gets_title_block(output: StreamOutput) -> None:
    output.set_title("Artist", "Song")
    listener = output.add_listener(icy=True)
    listener._icy._metaint = 32
    listener._icy._until_meta = 32
    output.write(_block())
    data = await _collect(listener, 32 + len(encode_metadata("Artist - Song")))
    assert encode_metadata("Artist - Song") in data


async def test_listener_count_callback_and_disconnect_all(output: StreamOutput) -> None:
    counts: list[int] = []
    output.on_listener_change = counts.append
    first = output.add_listener(icy=False)
    output.remove_listener(first)
    output.add_listener(icy=False)
    output.disconnect_all()
    assert counts == [1, 0, 1, 0]


async def test_connect_disconnect_churn_does_not_leak(output: StreamOutput) -> None:
    for _ in range(50):
        listener = output.add_listener(icy=False)
        output.remove_listener(listener)
    assert output.listener_count == 0
    assert output._listeners == []


async def test_encoder_failures_back_off_then_503() -> None:
    now = [0.0]

    class Dying(FakeEncoder):
        def __init__(self, bitrate: int) -> None:
            super().__init__(bitrate)
            self.alive = False  # reports dead; read() still blocks until close()

    out = StreamOutput(320, 8, encoder_factory=Dying, clock=lambda: now[0], loop=asyncio.get_running_loop())
    for _ in range(5):
        out._restart_encoder()
    with pytest.raises(EncoderUnavailableError):
        out.add_listener(icy=False)
    now[0] = 61.0
    out.add_listener(icy=False)
    out.close()


async def test_set_bitrate_restarts_and_disconnects(output: StreamOutput) -> None:
    listener = output.add_listener(icy=False)
    output.set_bitrate(192)
    assert listener.closed
    assert output._encoder.bitrate == 192
```

`tests/integration/test_stream_ffmpeg.py`:

```python
"""End-to-end encode with a real ffmpeg; skipped when ffmpeg is absent."""

from __future__ import annotations

import shutil

import numpy as np
import pytest

from autodj.icy import mp3_frame_offset
from autodj.stream import ffmpeg_encoder

pytestmark = pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg not installed")


def test_two_seconds_encode_to_valid_mp3() -> None:
    encoder = ffmpeg_encoder(128)
    t = np.arange(88200, dtype=np.float32) / 44100
    pcm = np.stack([np.sin(2 * np.pi * 440 * t)] * 2, axis=1).astype(np.float32) * 0.3
    encoder.write(pcm.tobytes())
    encoder.close_input()
    data = bytearray()
    while chunk := encoder.read(65536):
        data += chunk
    encoder.close()
    assert mp3_frame_offset(bytes(data)) >= 0
    assert len(data) > 128_000 // 8
```

(`close_input()` closes stdin so ffmpeg flushes; add it to the real encoder and to the `Encoder` protocol, and to `FakeEncoder` as a no-op.)

- [ ] **Step 2: Run to verify failure**

Run: `uv run --frozen pytest tests/unit/test_stream_output.py -q -o addopts=""`
Expected: FAIL — `ModuleNotFoundError`.

- [ ] **Step 3: Implement `src/autodj/stream.py`**

```python
"""MP3 radio stream output: one encoder, many HTTP listeners."""

from __future__ import annotations

import asyncio
import collections
import logging
import subprocess
import threading
import time
from collections.abc import AsyncIterator, Callable
from typing import Protocol

import numpy as np

from autodj.icy import IcyInterleaver, format_stream_title, mp3_frame_offset
from autodj.stereo import SAMPLE_RATE

logger = logging.getLogger(__name__)

_BURST_SECONDS = 2.0
_QUEUE_SECONDS = 5.0
_FAILURE_LIMIT = 5
_FAILURE_WINDOW = 60.0
_COOLDOWN = 60.0


class ListenerLimitError(Exception):
    """The listener limit has been reached."""


class EncoderUnavailableError(Exception):
    """The encoder failed repeatedly and is cooling down."""


class Encoder(Protocol):
    """PCM-in, MP3-out encoder."""

    alive: bool

    def write(self, pcm: bytes) -> None:
        """Feed raw f32le stereo PCM."""

    def read(self, n: int) -> bytes:
        """Read up to *n* encoded bytes; ``b""`` once the encoder has exited."""

    def close_input(self) -> None:
        """Close the encoder's input so it flushes."""

    def close(self) -> None:
        """Stop the encoder and release resources."""


class _FfmpegEncoder:  # pragma: no cover -- exercised by tests/integration/test_stream_ffmpeg.py
    """ffmpeg subprocess encoder."""

    def __init__(self, bitrate: int) -> None:
        self.bitrate = bitrate
        self._proc = subprocess.Popen(
            ["ffmpeg", "-hide_banner", "-loglevel", "error", "-f", "f32le", "-ac", "2",
             "-ar", str(SAMPLE_RATE), "-i", "pipe:0", "-c:a", "libmp3lame",
             "-b:a", f"{bitrate}k", "-f", "mp3", "pipe:1"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        )

    @property
    def alive(self) -> bool:
        return self._proc.poll() is None

    def write(self, pcm: bytes) -> None:
        assert self._proc.stdin is not None
        self._proc.stdin.write(pcm)
        self._proc.stdin.flush()

    def read(self, n: int) -> bytes:
        assert self._proc.stdout is not None
        return self._proc.stdout.read1(n)

    def close_input(self) -> None:
        if self._proc.stdin and not self._proc.stdin.closed:
            self._proc.stdin.close()

    def close(self) -> None:
        self.close_input()
        try:
            self._proc.terminate()
            self._proc.wait(timeout=5)
        except Exception:
            self._proc.kill()
            self._proc.wait(timeout=5)


def ffmpeg_encoder(bitrate: int) -> Encoder:
    """Start a real ffmpeg MP3 encoder at *bitrate* kbps."""
    return _FfmpegEncoder(bitrate)  # pragma: no cover


class Listener:
    """One HTTP listener's bounded byte queue."""

    def __init__(self, icy: bool, max_bytes: int, loop: asyncio.AbstractEventLoop) -> None:
        """Create a listener; *icy* adds ICY metadata to its bytes."""
        self._icy = IcyInterleaver() if icy else None
        self._max_bytes = max_bytes
        self._loop = loop
        self._queue: collections.deque[bytes] = collections.deque()
        self._size = 0
        self._event = asyncio.Event()
        self.closed = False

    def push(self, data: bytes, title: str) -> bool:
        """Queue *data*; returns ``False`` when the listener is too far behind."""
        if self.closed:
            return False
        if self._icy is not None:
            data = self._icy.feed(data, title)
        self._queue.append(data)
        self._size += len(data)
        self._loop.call_soon_threadsafe(self._event.set)
        return self._size <= self._max_bytes

    async def chunks(self) -> AsyncIterator[bytes]:
        """Yield queued bytes until the listener is closed."""
        while True:
            while self._queue:
                data = self._queue.popleft()
                self._size -= len(data)
                yield data
            if self.closed:
                return
            self._event.clear()
            await self._event.wait()

    def close(self) -> None:
        """Stop the listener after it drains what it has."""
        self.closed = True
        self._loop.call_soon_threadsafe(self._event.set)


class StreamOutput:
    """Encode mix-bus blocks once and fan the MP3 out to listeners."""

    def __init__(
        self,
        bitrate: int,
        max_listeners: int,
        encoder_factory: Callable[[int], Encoder] = ffmpeg_encoder,
        clock: Callable[[], float] = time.monotonic,
        loop: asyncio.AbstractEventLoop | None = None,
    ) -> None:
        """Start the encoder and its reader thread."""
        self._bitrate = bitrate
        self._max_listeners = max_listeners
        self._factory = encoder_factory
        self._clock = clock
        self._loop = loop or asyncio.get_event_loop()
        self._lock = threading.RLock()
        self._listeners: list[Listener] = []
        self._burst: collections.deque[bytes] = collections.deque()
        self._burst_size = 0
        self._failures: collections.deque[float] = collections.deque()
        self._cooldown_until = 0.0
        self._pcm_in = 0
        self._mp3_out = 0
        self._title = ""
        self._pending_titles: collections.deque[tuple[int, str]] = collections.deque()
        self._closed = False
        self.on_listener_change: Callable[[int], None] | None = None
        self._encoder: Encoder = self._factory(bitrate)
        self._reader = self._start_reader(self._encoder)

    # -- encoder lifecycle -------------------------------------------------

    def _start_reader(self, encoder: Encoder) -> threading.Thread:
        thread = threading.Thread(target=self._read_loop, args=(encoder,), name="autodj-stream-reader", daemon=True)
        thread.start()
        return thread

    def _record_failure(self) -> None:
        now = self._clock()
        self._failures.append(now)
        while self._failures and now - self._failures[0] > _FAILURE_WINDOW:
            self._failures.popleft()
        if len(self._failures) >= _FAILURE_LIMIT:
            self._cooldown_until = now + _COOLDOWN
            self._failures.clear()
            logger.error("Stream encoder failed %d times in a minute; pausing it for 60 s", _FAILURE_LIMIT)

    def _restart_encoder(self) -> None:
        """Replace a dead encoder, counting the failure."""
        with self._lock:
            self._record_failure()
            self.disconnect_all()
            try:
                self._encoder.close()
            except Exception:
                logger.debug("Closing the failed encoder raised", exc_info=True)
            if self._clock() < self._cooldown_until or self._closed:
                return
            self._encoder = self._factory(self._bitrate)
            self._pcm_in = self._mp3_out = 0
            self._reader = self._start_reader(self._encoder)

    def _read_loop(self, encoder: Encoder) -> None:
        while True:
            data = encoder.read(4096)
            if not data:
                break
            self._on_encoded(data)
        if not self._closed and encoder is self._encoder:
            logger.warning("Stream encoder exited; restarting")
            self._restart_encoder()

    # -- data path ---------------------------------------------------------

    def write(self, block: np.ndarray) -> None:
        """Feed one mix-bus block to the encoder."""
        with self._lock:
            encoder = self._encoder
        if not encoder.alive:
            return
        pcm = np.ascontiguousarray(block, dtype=np.float32).tobytes()
        try:
            encoder.write(pcm)
        except (BrokenPipeError, OSError, ValueError):
            logger.warning("Stream encoder input closed")
            return
        with self._lock:
            self._pcm_in += len(pcm)

    def set_title(self, artist: str, title: str) -> None:
        """Switch the ICY title once audio written from now reaches listeners."""
        with self._lock:
            self._pending_titles.append((self._pcm_in, format_stream_title(artist, title)))

    def _on_encoded(self, data: bytes) -> None:
        with self._lock:
            self._mp3_out += len(data)
            ratio = (self._bitrate * 1000 / 8) / (SAMPLE_RATE * 2 * 4)
            while self._pending_titles and self._mp3_out >= self._pending_titles[0][0] * ratio:
                self._title = self._pending_titles.popleft()[1]
            burst_limit = int(self._bitrate * 1000 / 8 * _BURST_SECONDS)
            self._burst.append(data)
            self._burst_size += len(data)
            while self._burst and self._burst_size - len(self._burst[0]) >= burst_limit:
                self._burst_size -= len(self._burst.popleft())
            listeners = list(self._listeners)
            title = self._title
        for listener in listeners:
            if not listener.push(data, title):
                logger.info("Dropping a stream listener that fell behind")
                self.remove_listener(listener)

    # -- listeners ---------------------------------------------------------

    @property
    def listener_count(self) -> int:
        """Current number of listeners."""
        with self._lock:
            return len(self._listeners)

    def _notify(self) -> None:
        if self.on_listener_change is not None:
            self.on_listener_change(len(self._listeners))

    def add_listener(self, icy: bool) -> Listener:
        """Register a listener and give it the recent burst.

        Raises:
            ListenerLimitError: At the listener limit.
            EncoderUnavailableError: While the encoder is cooling down.
        """
        with self._lock:
            if self._clock() < self._cooldown_until:
                raise EncoderUnavailableError("stream encoder failed")
            if not self._encoder.alive:
                self._encoder = self._factory(self._bitrate)
                self._reader = self._start_reader(self._encoder)
            if len(self._listeners) >= self._max_listeners:
                raise ListenerLimitError("listener limit reached")
            max_bytes = int(self._bitrate * 1000 / 8 * _QUEUE_SECONDS)
            listener = Listener(icy, max_bytes, self._loop)
            burst = b"".join(self._burst)
            start = mp3_frame_offset(burst)
            if start >= 0:
                listener.push(burst[start:], self._title)
            self._listeners.append(listener)
            self._notify()
            return listener

    def remove_listener(self, listener: Listener) -> None:
        """Unregister and close *listener*."""
        with self._lock:
            if listener in self._listeners:
                self._listeners.remove(listener)
                listener.close()
                self._notify()

    def disconnect_all(self) -> None:
        """Close every listener."""
        with self._lock:
            for listener in list(self._listeners):
                self._listeners.remove(listener)
                listener.close()
            self._notify()

    def set_bitrate(self, bitrate: int) -> None:
        """Restart the encoder at *bitrate*; listeners reconnect on their own."""
        with self._lock:
            self._bitrate = bitrate
            self.disconnect_all()
            old = self._encoder
            self._encoder = self._factory(bitrate)
            self._pcm_in = self._mp3_out = 0
            self._burst.clear()
            self._burst_size = 0
            self._reader = self._start_reader(self._encoder)
        old.close()

    def close(self) -> None:
        """Disconnect listeners and stop the encoder."""
        with self._lock:
            self._closed = True
            self.disconnect_all()
            encoder = self._encoder
        encoder.close()
```

Fix `test_listener_count_callback_and_disconnect_all`: `disconnect_all` notifies once with 0 even when already empty; the expected list `[1, 0, 1, 0]` matches one notify per add/remove and one for `disconnect_all` with one listener present.

- [ ] **Step 4: Run tests**

Run: `uv run --frozen pytest tests/unit/test_stream_output.py tests/integration/test_stream_ffmpeg.py -q -o addopts=""`
Expected: PASS (ffmpeg test skipped if ffmpeg is absent; run it at least once where ffmpeg exists).

- [ ] **Step 5: Commit**

```bash
git add src/autodj/stream.py tests/unit/test_stream_output.py tests/integration/test_stream_ffmpeg.py
git commit -m "feat: encode the mix once and fan mp3 out to stream listeners"
```

---

### Task 11: Station lifecycle, server wiring and stream routes

**Files:**
- Create: `src/autodj/station.py`
- Modify: `src/autodj/server.py` (`serve` 2019-2177, `create_app` 721, lifespan 768-833, new routes), `src/autodj/security.py` (`_is_public_path` 584), `src/autodj/_bridge.py` (`get_state` 628-654, `skip` 161, `pause` 380, seek methods, history hook, `set_stream_bitrate`), `src/autodj/runtime_state.py` (persist `stream_bitrate`), `src/autodj/player.py` (stream mode: `run` waits for the station)
- Test: `tests/unit/test_station.py`, `tests/integration/test_stream_server.py`

**Interfaces:**
- Consumes: `MixBus`, `StreamOutput`, `StreamSecret`, `LinerScheduler`, `Player._next_rendered`, `Player._pending_entry`.
- Produces:
  - `Station(bus: MixBus, stream: StreamOutput, player: Player, idle_grace: float, clock: Callable[[], float] = time.monotonic, on_event: Callable[[str], None] | None = None)`:
    - `listener_changed(count: int) -> None` — wired to `StreamOutput.on_listener_change`.
    - `tick() -> None` — called once a second; stops the set after the grace period.
    - `start_new_set() -> None` — queue head or shuffle pick, `player._pending_entry = entry`, `player._pending_offset = 0`, `bus.start_set()`.
    - `state -> str` — `"idle"`, `"playing"`, `"paused"`.
    - Events passed to `on_event`: `"set_started"`, `"set_stopped"`.
  - Bridge: `stream_mode: bool` property; `get_state()` adds `stream_mode`, `stream_state`, `stream_listeners`; `browser_playback` is `False` in stream mode.
  - Routes:
    - `GET /stream/{secret}.mp3`
    - `GET /stream/{secret}.m3u`
    - `GET /api/stream` (authenticated) → `{"path": "/stream/<secret>.mp3", "m3u_path": "/stream/<secret>.m3u", "bitrate": int, "listeners": int, "state": str}`
    - `POST /api/stream/rotate` (authenticated) → same body as `GET /api/stream`
    - `POST /api/stream/settings` body `{"bitrate": 128|192|256|320}` (authenticated, `extra="forbid"`) → same body
    - Seek routes return 409 `{"detail": "Seeking is not available while streaming."}` in stream mode.

- [ ] **Step 1: Write the failing tests**

```python
# tests/unit/test_station.py
"""Station lifecycle: idle, first listener, grace period, new set."""

from __future__ import annotations

from unittest.mock import MagicMock

from autodj.player import PlayerState
from autodj.station import Station


def _station(queue=None):
    now = [0.0]
    bus = MagicMock()
    bus.playing = False

    def start():
        bus.playing = True

    def stop():
        bus.playing = False

    bus.start_set.side_effect = start
    bus.stop_set.side_effect = stop
    player = MagicMock()
    player._state = PlayerState()
    player._state.queue = list(queue or [])
    shuffle_pick = MagicMock(name="shuffle")
    player._random_start_entry.return_value = shuffle_pick
    events: list[str] = []
    bridge_history = MagicMock()
    st = Station(bus, MagicMock(), player, idle_grace=30.0, clock=lambda: now[0], on_event=events.append,
                 forget_track=bridge_history)
    return st, bus, player, now, events, shuffle_pick, bridge_history


def test_starts_idle() -> None:
    st, bus, *_ = _station()
    assert st.state == "idle"
    bus.start_set.assert_not_called()


def test_first_listener_starts_set_from_queue_head() -> None:
    head = MagicMock(name="head")
    st, bus, player, _now, events, _shuffle, _h = _station(queue=[head])
    st.listener_changed(1)
    assert player._pending_entry is head
    assert player._state.queue == []
    assert player._pending_offset == 0
    bus.start_set.assert_called_once()
    assert events == ["set_started"]


def test_first_listener_without_queue_uses_shuffle_pick() -> None:
    st, _bus, player, _now, _e, shuffle, _h = _station()
    st.listener_changed(1)
    assert player._pending_entry is shuffle


def test_reconnect_within_grace_keeps_playing() -> None:
    st, bus, _p, now, events, *_ = _station()
    st.listener_changed(1)
    st.listener_changed(0)
    now[0] = 20.0
    st.tick()
    st.listener_changed(1)
    now[0] = 100.0
    st.tick()
    assert bus.stop_set.call_count == 0
    assert events == ["set_started"]


def test_stops_after_grace_and_forgets_cut_short_track() -> None:
    st, bus, player, now, events, _s, forget = _station()
    st.listener_changed(1)
    current = MagicMock(name="current")
    player._state.current_track = current
    st.listener_changed(0)
    now[0] = 30.5
    st.tick()
    bus.stop_set.assert_called_once()
    forget.assert_called_once_with(current)
    assert st.state == "idle"
    assert events == ["set_started", "set_stopped"]


def test_new_set_after_stop() -> None:
    st, bus, _p, now, events, *_ = _station()
    st.listener_changed(1)
    st.listener_changed(0)
    now[0] = 31.0
    st.tick()
    st.listener_changed(1)
    assert bus.start_set.call_count == 2
    assert events == ["set_started", "set_stopped", "set_started"]


def test_paused_set_is_not_stopped_by_idle() -> None:
    st, bus, player, now, *_ = _station()
    st.listener_changed(1)
    player._state.is_paused = True
    st.listener_changed(0)
    now[0] = 500.0
    st.tick()
    bus.stop_set.assert_not_called()
    assert st.state == "paused"
```

`Player._random_start_entry() -> IndexEntry` is new: extract the random seed pick from `run` (927-937) into this method and call it from both places.

`tests/integration/test_stream_server.py` (use the `bridge` fixture and a stream-mode app factory):

```python
"""Stream routes and security."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

from autodj.server import create_app
from autodj.stream_secret import StreamSecret


@pytest.fixture
def stream_app(bridge, tmp_path: Path):
    secret = StreamSecret.load_or_create(tmp_path / ".stream-secret")
    stream = MagicMock()
    stream.listener_count = 0
    listener = MagicMock()

    async def chunks():
        yield b"\xff\xfb\x90\x00" + b"\x00" * 413

    listener.chunks = chunks
    stream.add_listener.return_value = listener
    bridge.player._cfg.stream.enabled = True
    bridge.attach_stream(stream=stream, secret=secret, station=MagicMock(state="idle"))
    app = create_app(bridge)
    return TestClient(app), secret, stream


def test_wrong_secret_is_404(stream_app) -> None:
    client, _secret, stream = stream_app
    assert client.get("/stream/" + "x" * 43 + ".mp3").status_code == 404
    assert client.get("/stream/.mp3").status_code == 404
    stream.add_listener.assert_not_called()


def test_right_secret_streams_mp3(stream_app) -> None:
    client, secret, _stream = stream_app
    with client.stream("GET", f"/stream/{secret.value}.mp3") as response:
        assert response.status_code == 200
        assert response.headers["content-type"] == "audio/mpeg"
        assert response.headers["cache-control"] == "no-store"
        assert "icy-metaint" not in response.headers
        assert next(response.iter_bytes())[:2] == b"\xff\xfb"


def test_icy_headers_when_asked(stream_app) -> None:
    client, secret, stream = stream_app
    with client.stream("GET", f"/stream/{secret.value}.mp3", headers={"Icy-MetaData": "1"}) as response:
        assert response.headers["icy-metaint"] == "16000"
        assert response.headers["icy-name"] == "AutoDJ"
        assert response.headers["icy-br"] == "320"
    stream.add_listener.assert_called_with(icy=True)


def test_m3u_points_at_stream(stream_app) -> None:
    client, secret, _ = stream_app
    body = client.get(f"/stream/{secret.value}.m3u").text
    assert body.strip() == f"http://testserver/stream/{secret.value}.mp3"


def test_rotate_invalidates_old_url(stream_app) -> None:
    client, secret, stream = stream_app
    old = secret.value
    data = client.post("/api/stream/rotate").json()
    assert old not in data["path"]
    assert client.get(f"/stream/{old}.mp3").status_code == 404
    stream.disconnect_all.assert_called_once()


def test_host_check_still_applies(stream_app) -> None:
    client, secret, _ = stream_app
    response = client.get(f"/stream/{secret.value}.mp3", headers={"Host": "evil.example"})
    assert response.status_code in (400, 403, 421)


def test_listener_limit_is_503(stream_app) -> None:
    from autodj.stream import ListenerLimitError
    client, secret, stream = stream_app
    stream.add_listener.side_effect = ListenerLimitError("listener limit reached")
    response = client.get(f"/stream/{secret.value}.mp3")
    assert response.status_code == 503
    assert "limit" in response.text.lower()


def test_encoder_cooldown_is_503(stream_app) -> None:
    from autodj.stream import EncoderUnavailableError
    client, secret, stream = stream_app
    stream.add_listener.side_effect = EncoderUnavailableError("stream encoder failed")
    response = client.get(f"/stream/{secret.value}.mp3")
    assert response.status_code == 503
    assert "stream encoder failed" in response.text


def test_bitrate_setting_validated_and_applied(stream_app) -> None:
    client, _secret, stream = stream_app
    assert client.post("/api/stream/settings", json={"bitrate": 64}).status_code == 422
    assert client.post("/api/stream/settings", json={"bitrate": 192, "x": 1}).status_code == 422
    assert client.post("/api/stream/settings", json={"bitrate": 192}).json()["bitrate"] == 192
    stream.set_bitrate.assert_called_once_with(192)


def test_state_reports_stream_mode(stream_app) -> None:
    client, *_ = stream_app
    state = client.get("/api/status").json()
    assert state["stream_mode"] is True
    assert state["browser_playback"] is False
    assert state["stream_state"] == "idle"


def test_seek_refused_in_stream_mode(stream_app) -> None:
    client, *_ = stream_app
    response = client.post("/api/seek", json={"seconds": 10})
    assert response.status_code == 409
    assert response.json()["detail"] == "Seeking is not available while streaming."


def test_wrong_secret_attempts_are_rate_limited(stream_app) -> None:
    client, *_ = stream_app
    codes = [client.get("/stream/" + "y" * 43 + ".mp3").status_code for _ in range(8)]
    assert 429 in codes
```

Confirm the status route name (`/api/status`) and seek route/body used by the frontend (`/api/seek` with `{seconds}` or `{delta}`) before running; the frontend map says both exist.

- [ ] **Step 2: Run to verify failure**

Run: `uv run --frozen pytest tests/unit/test_station.py tests/integration/test_stream_server.py -q -o addopts=""`
Expected: FAIL — `ModuleNotFoundError: No module named 'autodj.station'`.

- [ ] **Step 3: Implement `src/autodj/station.py`**

```python
"""Stream-mode set lifecycle driven by listener count."""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from typing import Any

logger = logging.getLogger(__name__)


class Station:
    """Start a set on the first listener; stop it after the idle grace period."""

    def __init__(
        self,
        bus: Any,
        stream: Any,
        player: Any,
        idle_grace: float,
        clock: Callable[[], float] = time.monotonic,
        on_event: Callable[[str], None] | None = None,
        forget_track: Callable[[Any], None] | None = None,
    ) -> None:
        """Create an idle station.

        Args:
            bus: The mix bus.
            stream: The stream output.
            player: The player (queue, pending entry, random start pick).
            idle_grace: Seconds without listeners before the set stops.
            clock: Monotonic seconds.
            on_event: Receives ``"set_started"`` and ``"set_stopped"``.
            forget_track: Removes a cut-short track from the session history.
        """
        self._bus = bus
        self._stream = stream
        self._player = player
        self._grace = idle_grace
        self._clock = clock
        self._on_event = on_event or (lambda _event: None)
        self._forget = forget_track or (lambda _entry: None)
        self._listeners = 0
        self._empty_since: float | None = None

    @property
    def state(self) -> str:
        """``"idle"``, ``"playing"`` or ``"paused"``."""
        if not self._bus.playing:
            return "idle"
        return "paused" if self._player._state.is_paused else "playing"

    def start_new_set(self) -> None:
        """Start a set from the queue head, or a fresh shuffle pick."""
        state = self._player._state
        with state.queue_lock:
            entry = state.queue.pop(0) if state.queue else None
        if entry is None:
            entry = self._player._random_start_entry()
        self._player._pending_entry = entry
        self._player._pending_offset = 0
        self._bus.start_set()
        self._on_event("set_started")

    def listener_changed(self, count: int) -> None:
        """React to the stream's listener count changing."""
        self._listeners = count
        if count > 0:
            self._empty_since = None
            if not self._bus.playing:
                self.start_new_set()
        elif self._empty_since is None:
            self._empty_since = self._clock()

    def tick(self) -> None:
        """Stop the set once nobody has listened for the grace period."""
        if (
            self._listeners == 0
            and self._empty_since is not None
            and self._bus.playing
            and not self._player._state.is_paused
            and self._clock() - self._empty_since >= self._grace
        ):
            current = self._player._state.current_track
            self._bus.stop_set()
            if current is not None:
                self._forget(current)
            self._player._state.current_track = None
            self._player._state.next_track = None
            self._empty_since = None
            self._on_event("set_stopped")
```

- [ ] **Step 4: Wire it into the player, bridge and server**

1. `Player.run` in stream mode: add `stream_mode: bool = False` to `Player.__init__` (stored as `self._stream_mode`). In `run`, when `self._stream_mode`, build the `MixBus` exactly as in Task 5 but do not create a `SoundDeviceOutput` unless `self._server_audio_too` (new `__init__` flag set when both `--stream` and `--server-audio` are passed), do not call `start_set()`, and do not pick or record a seed (the station does). Expose `self.bus` before entering `bus.run(stop)`; set a `threading.Event` `self.bus_ready` once `self.bus` exists so the server can wait for it.
2. `PlayerBridge`:
   - `attach_stream(*, stream, secret, station, scheduler=None)` stores the three objects (plus `liner_scheduler`) and sets `stream_mode = True`.
   - `get_state()`: add `"stream_mode": self.stream_mode`, `"stream_state": self.station.state if self.stream_mode else None`, `"stream_listeners": self.stream.listener_count if self.stream_mode else 0`; force `"browser_playback": False` in stream mode.
   - `skip()`: if `player.bus` is set → `player.bus.skip()`.
   - `pause()`: if `player.bus` is set → toggle `is_paused` and call `player.bus.pause(is_paused)`.
   - `seek_to` / `seek_relative`: raise `StreamSeekUnavailable` (new exception in `_bridge.py`) in stream mode; the seek route maps it to 409 with the exact detail string.
   - `forget_track(entry)`: remove the last matching entry from `_play_history` (search from the right by `path`).
   - `on_track_started(entry)`: set as `player.on_track_started`; appends to `_play_history` (the same record shape `advance_now` appends; reuse its helper or factor one out), calls `stream.set_title(artist, title)` in stream mode, and `liner_scheduler.on_track_start()` if set. This also fixes `/api/history` for `--server-audio`.
   - `set_stream_bitrate(bitrate)`: validates against `STREAM_BITRATES`, calls `stream.set_bitrate`, stores it in the persisted settings.
3. `runtime_state.py`: add `stream_bitrate: int` to `PlaybackState`, emit it from `get_settings()["playback"]` as `player._cfg.stream.bitrate`, restore it in `load_into_player` with a validated-int helper that accepts only `STREAM_BITRATES`, and set `cfg.stream.bitrate` on restore.
4. `server.serve(..., stream: bool)`: when `stream` is true:
   - `secret = StreamSecret.load_or_create(stream_secret_path(cfg), forbidden=cfg.server.access_token)`; on `StreamSecretError` raise `SystemExit` with the message.
   - `Player(..., dry_run=False, stream_mode=True, server_audio_too=not no_playback_requested_by_server_audio_flag)` — i.e. when `--server-audio` was also passed.
   - Start the player thread, wait on `player.bus_ready` (timeout 30 s).
   - `stream_out = StreamOutput(cfg.stream.bitrate, cfg.stream.max_listeners, loop=<uvicorn loop>)` — create it in the lifespan startup (so it binds the running loop) rather than in `serve`; register it on the bus with `player.bus.add_output(stream_out)`.
   - `scheduler = LinerScheduler(cfg.playback, _resolve_liner_folder(), player.bus)`.
   - `station = Station(player.bus, stream_out, player, cfg.stream.idle_grace_seconds, on_event=bridge.announce_station_event, forget_track=bridge.forget_track)`; `stream_out.on_listener_change = station.listener_changed`.
   - `bridge.attach_stream(stream=stream_out, secret=secret, station=station, scheduler=scheduler)`.
   - In `_broadcast_loop`, each tick call `bridge.station.tick()` and `bridge.liner_scheduler.tick()` when set (wrap each in `try/except Exception: logger.exception(...)` so one failure does not stop the broadcast).
   - Lifespan shutdown: `stream_out.close()` before stopping the player.
   - `bridge.announce_station_event(event)` stores the latest event with a sequence number in `get_state()["stream_event"] = {"seq": int, "name": str}` so the page can announce each once.
5. `security.py` `_is_public_path`: `or _safe_public_prefix(path, "/stream/")`.
6. Routes in `create_app` (only registered when `bridge.stream_mode` is true at request time — register always, return 404 when not in stream mode):

```python
    _STREAM_NAME = re.compile(r"^(?P<secret>[A-Za-z0-9_-]{1,128})\.(?P<ext>mp3|m3u)$")
    stream_limiter = PairingRateLimiter(per_client_limit=5, global_limit=100, window_seconds=60)

    def _stream_secret_ok(request: Request, name: str) -> str:
        match = _STREAM_NAME.match(name)
        peer = peer_address(request.scope)
        if not bridge.stream_mode or match is None or not bridge.stream_secret.matches(match["secret"]):
            decision = stream_limiter.reserve(peer)
            if not decision.allowed:
                raise HTTPException(status_code=429, detail="Too many attempts",
                                    headers={"Retry-After": str(int(decision.retry_after))})
            raise HTTPException(status_code=404, detail="Not found")
        return match["ext"]

    @app.get("/stream/{name}")
    async def api_stream(name: str, request: Request) -> Response:
        """Serve the MP3 stream or its one-line playlist."""
        ext = _stream_secret_ok(request, name)
        if ext == "m3u":
            host = request.headers.get("host", "")
            scheme = "https" if request.url.scheme == "https" else "http"
            body = f"{scheme}://{host}/stream/{bridge.stream_secret.value}.mp3\n"
            return Response(body, media_type="audio/x-mpegurl", headers={"Cache-Control": "no-store"})
        icy = request.headers.get("icy-metadata") == "1"
        try:
            listener = bridge.stream.add_listener(icy=icy)
        except ListenerLimitError as exc:
            raise HTTPException(status_code=503, detail="Stream listener limit reached") from exc
        except EncoderUnavailableError as exc:
            raise HTTPException(status_code=503, detail="stream encoder failed") from exc
        headers = {"Cache-Control": "no-store"}
        if icy:
            cfg = bridge.player._cfg.stream
            headers.update({"icy-metaint": str(METAINT), "icy-name": cfg.station_name, "icy-br": str(cfg.bitrate)})

        async def body() -> AsyncIterator[bytes]:
            try:
                async for chunk in listener.chunks():
                    yield chunk
            finally:
                bridge.stream.remove_listener(listener)

        return StreamingResponse(
            body(), media_type="audio/mpeg", headers=headers,
            background=BackgroundTask(bridge.stream.remove_listener, listener),
        )
```

   Plus `GET /api/stream`, `POST /api/stream/rotate` (calls `bridge.stream_secret.rotate()`, `bridge.stream.disconnect_all()`, `bridge.announce_station_event("link_changed")` so every open page announces it once, and returns the info body) and `POST /api/stream/settings` (`StreamSettingsBody(BaseModel)` with `model_config = ConfigDict(extra="forbid")` and `bitrate: Literal[128, 192, 256, 320]`; calls `bridge.set_stream_bitrate` then `bridge.save_persistent_state()`). All three return 404 when not in stream mode.

   `remove_listener` is idempotent, so calling it from both the generator `finally` and the background task is safe.

- [ ] **Step 5: Run tests and the whole suite**

Run: `uv run --frozen pytest tests/unit/test_station.py tests/integration/test_stream_server.py -q -o addopts=""`
Expected: PASS.
Run: `uv run --frozen python scripts/ci_pytest.py` and `uv run --frozen pyright src/autodj/` and `uv run --frozen mypy src/autodj/`.
Expected: PASS, coverage gates met.

- [ ] **Step 6: Commit**

```bash
git add src/autodj tests
git commit -m "feat: serve the live mix as a secret-url radio stream"
```

---

### Task 12: Web page in stream mode

**Prerequisite:** the accessibility branch (`review/a11y`) must be merged into this branch first (`git merge review/integration` after it contains the accessibility commits). The frontend line numbers below refer to that merged state.

**Files:**
- Create: `src/autodj/static/modules/stream-mode.js`
- Modify: `src/autodj/static/index.html` (controls row ~168-190; hidden decks 44-45), `src/autodj/static/app.js` (`applyState` 298, progress block 370-403, lyrics call 480, `_seekByDelta` 1272, `installHotkeys` call 1518), `src/autodj/static/modules/hotkeys.js` (options 184, Space/K dispatch 240-244), `src/autodj/static/app.css`
- Test: `tests/jsmodules/stream-mode.test.js`, extend `tests/jsmodules/app-source.test.js`, `tests/jsmodules/hotkeys.test.js`, `tests/jsmodules/a11y-contract.test.js`

**Interfaces:**
- Consumes state keys: `stream_mode`, `stream_state` (`"idle" | "playing" | "paused"`), `stream_listeners`, `stream_event` (`{seq, name}`), `elapsed`, `duration`, `current_track`.
- Produces in `stream-mode.js`:

```js
export const STREAM_DELAY_ESTIMATE_S = 3;
export function createStreamMode({ doc, audio, button, idleNote, srStatus, fetchInfo }) → {
  apply(state),          // show/hide controls, idle note, announce stream_event once
  toggleListen(),        // start/stop <audio> on the stream URL; updates aria-pressed
  delaySeconds(state),   // measured when listening, else STREAM_DELAY_ESTIMATE_S
  isListening(),
}
```

  - `installHotkeys({ ..., togglePlay })` — new optional callback; when given, Space/K call it instead of `btnPause.click()`.

- [ ] **Step 1: Write the failing tests**

```js
// tests/jsmodules/stream-mode.test.js
import { beforeEach, describe, expect, it, vi } from "vitest";
import { createStreamMode, STREAM_DELAY_ESTIMATE_S } from "../../src/autodj/static/modules/stream-mode.js";

function setup() {
  document.body.innerHTML = `
    <audio id="stream-audio" hidden></audio>
    <button id="btn-listen" type="button" aria-pressed="false" hidden><span aria-hidden="true">🔊</span> Listen here</button>
    <p id="stream-idle-note" hidden></p>
    <div id="sr-status" role="status" aria-live="polite" aria-atomic="true"></div>`;
  const audio = document.getElementById("stream-audio");
  audio.play = vi.fn(() => Promise.resolve());
  audio.pause = vi.fn();
  const fetchInfo = vi.fn(async () => ({ path: "/stream/SECRET.mp3" }));
  const mode = createStreamMode({
    doc: document,
    audio,
    button: document.getElementById("btn-listen"),
    idleNote: document.getElementById("stream-idle-note"),
    srStatus: document.getElementById("sr-status"),
    fetchInfo,
  });
  return { mode, audio, fetchInfo };
}

describe("stream mode", () => {
  beforeEach(() => vi.useRealTimers());

  it("stays hidden in browser mode", () => {
    const { mode } = setup();
    mode.apply({ stream_mode: false });
    expect(document.getElementById("btn-listen").hidden).toBe(true);
    expect(document.getElementById("stream-idle-note").hidden).toBe(true);
  });

  it("shows Listen here and the idle note when idle", () => {
    const { mode } = setup();
    mode.apply({ stream_mode: true, stream_state: "idle" });
    const note = document.getElementById("stream-idle-note");
    expect(document.getElementById("btn-listen").hidden).toBe(false);
    expect(note.hidden).toBe(false);
    expect(note.textContent).toBe(
      "Waiting for a listener. Press Listen here, or start the AutoDJ station on a speaker.",
    );
  });

  it("toggles listening with a fixed name and aria-pressed", async () => {
    const { mode, audio, fetchInfo } = setup();
    mode.apply({ stream_mode: true, stream_state: "idle" });
    const button = document.getElementById("btn-listen");
    await mode.toggleListen();
    expect(fetchInfo).toHaveBeenCalledOnce();
    expect(audio.src).toContain("/stream/SECRET.mp3");
    expect(audio.play).toHaveBeenCalled();
    expect(button.getAttribute("aria-pressed")).toBe("true");
    expect(button.textContent.trim()).toContain("Listen here");
    await mode.toggleListen();
    expect(audio.pause).toHaveBeenCalled();
    expect(audio.getAttribute("src")).toBeNull();
    expect(button.getAttribute("aria-pressed")).toBe("false");
  });

  it("announces each station event once", async () => {
    const { mode } = setup();
    const sr = document.getElementById("sr-status");
    mode.apply({ stream_mode: true, stream_state: "idle", stream_event: { seq: 0, name: "set_stopped" } });
    await new Promise((r) => setTimeout(r, 10));
    expect(sr.textContent).toBe("");
    mode.apply({ stream_mode: true, stream_state: "playing", stream_event: { seq: 1, name: "set_started" } });
    await new Promise((r) => setTimeout(r, 10));
    expect(sr.textContent).toBe("New set started.");
    sr.textContent = "";
    mode.apply({ stream_mode: true, stream_state: "playing", stream_event: { seq: 1, name: "set_started" } });
    await new Promise((r) => setTimeout(r, 10));
    expect(sr.textContent).toBe("");
    mode.apply({ stream_mode: true, stream_state: "idle", stream_event: { seq: 2, name: "set_stopped" } });
    await new Promise((r) => setTimeout(r, 10));
    expect(sr.textContent).toBe("Set stopped. Nobody is listening.");
  });

  it("uses the estimate unless listening", () => {
    const { mode } = setup();
    expect(mode.delaySeconds({ elapsed: 50 })).toBe(STREAM_DELAY_ESTIMATE_S);
  });
});
```

Extend `app-source.test.js` using its `setupApp` harness:

```js
it("never starts the deck engine in stream mode and hides seek", async () => {
  const app = await setupApp({ initialState: { ...baseState, stream_mode: true, stream_state: "idle", browser_playback: false } });
  expect(app.unlockAndPlay).not.toHaveBeenCalled();
  expect(document.getElementById("progress-track").hidden).toBe(true);
  expect(document.getElementById("btn-listen").hidden).toBe(false);
});
```

(Name the base state object after what the harness already uses.)

Extend `hotkeys.test.js`:

```js
it("Space calls togglePlay when provided", () => {
  // install a second instance in an isolated document with togglePlay: vi.fn(),
  // press Space on document.body, expect togglePlay called and btnPause.click not called
});
```

Write it fully following the file's `beforeAll` install pattern: create `const togglePlay = vi.fn();` and a `btnPause` with `click = vi.fn()`, call `installHotkeys({ btnPause, togglePlay, ... })`, dispatch `keyEvent(document.body, " ")`, assert `togglePlay` called once and `btnPause.click` not called.

Extend `a11y-contract.test.js`: `#btn-listen` is a `button` with `aria-pressed`, its accessible name is exactly "Listen here" (the glyph span is `aria-hidden`), and `#stream-idle-note` is not a live region.

- [ ] **Step 2: Run to verify failure**

Run: `npx vitest run tests/jsmodules/stream-mode.test.js`
Expected: FAIL — cannot resolve `stream-mode.js`.

- [ ] **Step 3: Implement**

`src/autodj/static/modules/stream-mode.js`:

```js
// Stream mode: the server mixes; this page is a remote that can also listen.
import { announceStatus } from "./live-region.js";

export const STREAM_DELAY_ESTIMATE_S = 3;
const IDLE_TEXT = "Waiting for a listener. Press Listen here, or start the AutoDJ station on a speaker.";
const EVENT_TEXT = {
  set_started: "New set started.",
  set_stopped: "Set stopped. Nobody is listening.",
  link_changed: "Stream link changed. Speakers using the old link have stopped.",
};

export function createStreamMode({ doc, audio, button, idleNote, srStatus, fetchInfo }) {
  let listening = false;
  let lastSeq = null;
  let active = false;

  function setPressed(value) {
    listening = value;
    button.setAttribute("aria-pressed", value ? "true" : "false");
  }

  function apply(state) {
    active = Boolean(state.stream_mode);
    button.hidden = !active;
    const idle = active && state.stream_state === "idle";
    idleNote.hidden = !idle;
    if (idle && idleNote.textContent !== IDLE_TEXT) idleNote.textContent = IDLE_TEXT;
    const event = state.stream_event;
    if (active && event && event.seq !== lastSeq) {
      const first = lastSeq === null;
      lastSeq = event.seq;
      const text = EVENT_TEXT[event.name];
      if (text && !first) announceStatus(srStatus, text, { dwellMs: 4000, force: true });
    }
    if (!active && listening) stop();
  }

  function stop() {
    audio.pause();
    audio.removeAttribute("src");
    audio.load?.();
    setPressed(false);
  }

  async function toggleListen() {
    if (listening) {
      stop();
      return;
    }
    const info = await fetchInfo();
    audio.src = info.path;
    setPressed(true);
    try {
      await audio.play();
    } catch (err) {
      setPressed(false);
      announceStatus(srStatus, `Could not start the stream: ${err.message}`, { force: true, tone: "error" });
    }
  }

  function delaySeconds(state) {
    if (listening && audio.buffered && audio.buffered.length) {
      const ahead = audio.buffered.end(audio.buffered.length - 1) - audio.currentTime;
      if (Number.isFinite(ahead) && ahead >= 0 && ahead < 30) return ahead;
    }
    void state;
    return STREAM_DELAY_ESTIMATE_S;
  }

  void doc;
  return { apply, toggleListen, delaySeconds, isListening: () => listening };
}
```

The first `stream_event` seen after page load is recorded but not announced, so reloading the page does not re-announce an old event; the test's `seq: 0` apply covers that.

`index.html`:
- After `#browser-player-b`: `<audio id="stream-audio" preload="none" hidden></audio>`.
- In `.controls-row` right after `#btn-pause`: `<button type="button" id="btn-listen" aria-pressed="false" hidden><span aria-hidden="true">🔊</span> Listen here</button>`.
- In the Now Playing card, after `#now-playing-announce`: `<p id="stream-idle-note" class="stream-idle-note" hidden></p>`.

`app.js`:
- Import `createStreamMode` and create `const streamMode = createStreamMode({ doc: document, audio: document.getElementById("stream-audio"), button: document.getElementById("btn-listen"), idleNote: document.getElementById("stream-idle-note"), srStatus: document.getElementById("sr-status"), fetchInfo: () => requestJson("/api/stream") });`.
- `document.getElementById("btn-listen").addEventListener("click", () => void streamMode.toggleListen());`
- At the top of `applyState`: `const inStream = Boolean(s.stream_mode); streamMode.apply(s);` and compute `const browserMode = Boolean(s.browser_playback) && !inStream;` — use `browserMode` wherever `s.browser_playback` is read (lines 375, 407, 421, 481, 494 in the map).
- Progress block: when `inStream`, `elapsed = Math.max(0, (s.elapsed || 0) - streamMode.delaySeconds(s))`; set `progressTrack.hidden = inStream` (keep `#progress-bar-label` visible, and remove its `aria-hidden` while in stream mode so the time is readable: `progressLbl.toggleAttribute("aria-hidden", !inStream)`).
- Lyrics: `applyLyricsState(s, _lyricEls, { elapsed, localClock: browserMode || inStream })`.
- `_seekByDelta`: first line `if (_lastStreamMode) { srSpeak("Seeking is not available while streaming."); return; }` where `_lastStreamMode` is updated in `applyState`.
- `installHotkeys({... , togglePlay: () => (_lastStreamMode ? document.getElementById("btn-listen") : btnPause).click() })`.

`hotkeys.js`: add `togglePlay = null` to the options; in the Space/K case: `if (togglePlay) togglePlay(); else if (btnPause) btnPause.click();`. Update the shortcuts dialog text in `index.html` for Space/K: "Play or pause. In stream mode, start or stop listening on this page."

`app.css`: `.stream-idle-note { margin: 0.5rem 0; }` and ensure `#btn-listen[aria-pressed="true"]` picks up the existing pressed-button styles (the forced-colors rule for `button[aria-pressed="true"]` already covers it).

- [ ] **Step 4: Run tests**

Run: `npx vitest run` and `npm run lint`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/autodj/static tests/jsmodules
git commit -m "feat(a11y): make the page a remote with Listen here in stream mode"
```

---

### Task 13: Stream settings section

**Files:**
- Modify: `src/autodj/static/index.html` (Settings card, after the Keyboard fieldset ~729-738), `src/autodj/static/app.js`, `src/autodj/static/modules/stream-mode.js`
- Test: `tests/jsmodules/stream-mode.test.js`, `tests/jsmodules/a11y-contract.test.js`

**Interfaces:**
- Consumes: `GET /api/stream`, `POST /api/stream/rotate`, `POST /api/stream/settings`, `postSettings(url, body, control)`.
- Produces: `createStreamSettings({ doc, fetchInfo, rotate, saveBitrate, srStatus, settingsStatus }) → { apply(state), refresh() }` in `stream-mode.js`.

- [ ] **Step 1: Write the failing tests**

```js
// append to tests/jsmodules/stream-mode.test.js
import { createStreamSettings } from "../../src/autodj/static/modules/stream-mode.js";

function settingsDom() {
  document.body.innerHTML = `
    <fieldset id="stream-settings" hidden>
      <legend>Stream</legend>
      <label for="stream-url">Stream address</label>
      <input id="stream-url" type="text" readonly>
      <button type="button" id="stream-copy">Copy address</button>
      <a id="stream-m3u" href="#">Playlist file (.m3u)</a>
      <label for="stream-bitrate">Quality</label>
      <select id="stream-bitrate"><option value="128">128 kbps</option><option value="192">192 kbps</option><option value="256">256 kbps</option><option value="320">320 kbps</option></select>
      <button type="button" id="stream-rotate">Make new link</button>
      <p id="stream-listeners"></p>
    </fieldset>
    <dialog id="stream-rotate-dialog"><form method="dialog"><p>Make a new link?</p><button value="cancel">Cancel</button><button value="confirm">Make new link</button></form></dialog>
    <div id="sr-status"></div><div id="settings-status"></div>`;
  const dialog = document.getElementById("stream-rotate-dialog");
  dialog.showModal = vi.fn(() => dialog.setAttribute("open", ""));
  return dialog;
}

describe("stream settings", () => {
  it("fills address, playlist link, quality and listener count", async () => {
    settingsDom();
    const info = { path: "/stream/S.mp3", m3u_path: "/stream/S.m3u", bitrate: 256, listeners: 2 };
    const ui = createStreamSettings({ doc: document, fetchInfo: vi.fn(async () => info), rotate: vi.fn(), saveBitrate: vi.fn(), srStatus: document.getElementById("sr-status"), settingsStatus: document.getElementById("settings-status") });
    ui.apply({ stream_mode: true, stream_listeners: 2 });
    await ui.refresh();
    expect(document.getElementById("stream-settings").hidden).toBe(false);
    expect(document.getElementById("stream-url").value).toBe(`${location.origin}/stream/S.mp3`);
    expect(document.getElementById("stream-m3u").getAttribute("href")).toBe("/stream/S.m3u");
    expect(document.getElementById("stream-bitrate").value).toBe("256");
    expect(document.getElementById("stream-listeners").textContent).toBe("2 listeners");
  });

  it("rotates only after confirmation and announces once", async () => {
    const dialog = settingsDom();
    const rotate = vi.fn(async () => ({ path: "/stream/NEW.mp3", m3u_path: "/stream/NEW.m3u", bitrate: 320, listeners: 0 }));
    const ui = createStreamSettings({ doc: document, fetchInfo: vi.fn(async () => ({ path: "/stream/S.mp3", m3u_path: "/stream/S.m3u", bitrate: 320, listeners: 0 })), rotate, saveBitrate: vi.fn(), srStatus: document.getElementById("sr-status"), settingsStatus: document.getElementById("settings-status") });
    ui.apply({ stream_mode: true, stream_listeners: 0 });
    await ui.refresh();
    document.getElementById("stream-rotate").click();
    expect(dialog.showModal).toHaveBeenCalled();
    dialog.returnValue = "cancel";
    dialog.dispatchEvent(new Event("close"));
    expect(rotate).not.toHaveBeenCalled();
    document.getElementById("stream-rotate").click();
    dialog.returnValue = "confirm";
    dialog.dispatchEvent(new Event("close"));
    await new Promise((r) => setTimeout(r, 10));
    expect(rotate).toHaveBeenCalledOnce();
    expect(document.getElementById("stream-url").value).toContain("/stream/NEW.mp3");
    expect(document.getElementById("sr-status").textContent).toBe("New stream link made. The old link no longer works.");
  });

  it("copies the address and reports failure", async () => {
    settingsDom();
    const writeText = vi.fn(async () => {});
    Object.defineProperty(navigator, "clipboard", { value: { writeText }, configurable: true });
    const ui = createStreamSettings({ doc: document, fetchInfo: vi.fn(async () => ({ path: "/stream/S.mp3", m3u_path: "/stream/S.m3u", bitrate: 320, listeners: 1 })), rotate: vi.fn(), saveBitrate: vi.fn(), srStatus: document.getElementById("sr-status"), settingsStatus: document.getElementById("settings-status") });
    ui.apply({ stream_mode: true, stream_listeners: 1 });
    await ui.refresh();
    document.getElementById("stream-copy").click();
    await new Promise((r) => setTimeout(r, 10));
    expect(writeText).toHaveBeenCalledWith(`${location.origin}/stream/S.mp3`);
    expect(document.getElementById("sr-status").textContent).toBe("Stream address copied.");
    writeText.mockRejectedValueOnce(new Error("denied"));
    document.getElementById("stream-copy").click();
    await new Promise((r) => setTimeout(r, 10));
    expect(document.getElementById("sr-status").textContent).toBe("Could not copy. Select the address and copy it yourself.");
  });

  it("saves the quality on change and says 1 listener correctly", async () => {
    settingsDom();
    const saveBitrate = vi.fn(async () => true);
    const ui = createStreamSettings({ doc: document, fetchInfo: vi.fn(async () => ({ path: "/stream/S.mp3", m3u_path: "/stream/S.m3u", bitrate: 320, listeners: 1 })), rotate: vi.fn(), saveBitrate, srStatus: document.getElementById("sr-status"), settingsStatus: document.getElementById("settings-status") });
    ui.apply({ stream_mode: true, stream_listeners: 1 });
    await ui.refresh();
    expect(document.getElementById("stream-listeners").textContent).toBe("1 listener");
    const select = document.getElementById("stream-bitrate");
    select.value = "192";
    select.dispatchEvent(new Event("change"));
    expect(saveBitrate).toHaveBeenCalledWith(192, select);
  });
});
```

a11y-contract additions: `#stream-url`, `#stream-bitrate` have associated labels; `#stream-listeners` is not a live region; `#stream-rotate-dialog` has `aria-labelledby` pointing at a heading inside it.

- [ ] **Step 2: Run to verify failure**

Run: `npx vitest run tests/jsmodules/stream-mode.test.js`
Expected: FAIL — `createStreamSettings` is not exported.

- [ ] **Step 3: Implement**

Add to `stream-mode.js`:

```js
export function createStreamSettings({ doc, fetchInfo, rotate, saveBitrate, srStatus, settingsStatus }) {
  const fieldset = doc.getElementById("stream-settings");
  const url = doc.getElementById("stream-url");
  const copy = doc.getElementById("stream-copy");
  const m3u = doc.getElementById("stream-m3u");
  const bitrate = doc.getElementById("stream-bitrate");
  const rotateBtn = doc.getElementById("stream-rotate");
  const dialog = doc.getElementById("stream-rotate-dialog");
  const listeners = doc.getElementById("stream-listeners");
  let active = false;

  function fill(info) {
    url.value = `${location.origin}${info.path}`;
    m3u.setAttribute("href", info.m3u_path);
    if (doc.activeElement !== bitrate) bitrate.value = String(info.bitrate);
  }

  async function refresh() {
    if (!active) return;
    fill(await fetchInfo());
  }

  function apply(state) {
    const now = Boolean(state.stream_mode);
    fieldset.hidden = !now;
    if (now && !active) {
      active = true;
      void refresh();
    }
    active = now;
    const count = Number(state.stream_listeners || 0);
    const text = `${count} listener${count === 1 ? "" : "s"}`;
    if (listeners.textContent !== text) listeners.textContent = text;
  }

  copy.addEventListener("click", async () => {
    try {
      await navigator.clipboard.writeText(url.value);
      announceStatus(srStatus, "Stream address copied.", { force: true, dwellMs: 3000 });
    } catch (_err) {
      url.focus();
      url.select();
      announceStatus(srStatus, "Could not copy. Select the address and copy it yourself.", { force: true, tone: "error" });
    }
  });

  bitrate.addEventListener("change", () => {
    void saveBitrate(Number(bitrate.value), bitrate);
  });

  rotateBtn.addEventListener("click", () => {
    dialog.returnValue = "";
    dialog.showModal();
  });

  dialog.addEventListener("close", async () => {
    if (dialog.returnValue !== "confirm") {
      rotateBtn.focus();
      return;
    }
    try {
      fill(await rotate());
      announceStatus(srStatus, "New stream link made. The old link no longer works.", { force: true, dwellMs: 5000 });
    } catch (err) {
      announceStatus(settingsStatus, `Could not make a new link: ${err.message}`, { force: true, tone: "error", dwellMs: 6000 });
    }
    rotateBtn.focus();
  });

  return { apply, refresh };
}
```

`index.html`: add the Stream fieldset (the markup from `settingsDom()` with `setting-desc` help text: "Add this address to Sonos or any player as a radio station. Anyone with it can listen." and for the rotate button: "Stops every speaker using the current address.") after the Keyboard fieldset, and the dialog after the hotkey dialog:

```html
<dialog id="stream-rotate-dialog" aria-labelledby="stream-rotate-title">
  <form method="dialog">
    <h2 id="stream-rotate-title">Make a new stream link?</h2>
    <p>Every speaker using the current link stops, and you will need to add the new link to them.</p>
    <button type="submit" value="cancel">Cancel</button>
    <button type="submit" value="confirm">Make new link</button>
  </form>
</dialog>
```

`app.js`: create `streamSettings = createStreamSettings({ doc: document, fetchInfo: () => requestJson("/api/stream"), rotate: () => requestJson("/api/stream/rotate", { method: "POST" }), saveBitrate: (value, control) => postSettings("/api/stream/settings", { bitrate: value }, control), srStatus: document.getElementById("sr-status"), settingsStatus })` and call `streamSettings.apply(s)` in `applyState`.

- [ ] **Step 4: Run tests**

Run: `npx vitest run` and `npm run lint` and `npx knip`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/autodj/static tests/jsmodules
git commit -m "feat(a11y): add stream address, quality and new-link settings"
```

---

### Task 14: Deployment, docs and release notes

**Files:**
- Modify: `compose.yaml`, `scripts/container_smoke.sh`, `docs/operations.md`, `README.md`, `THREAT_MODEL.md`, `CHANGELOG.md`, `tests/unit/test_containerfile.py` (compose profile assertions), `tests/unit/test_config_examples.py` (docs assertions if any list compose profiles)
- Test: existing doc/container contract tests

- [ ] **Step 1: Write the failing tests**

Add to `tests/unit/test_containerfile.py`:

```python
def test_compose_has_stream_profile_on_lan() -> None:
    compose = yaml.safe_load((ROOT / "compose.yaml").read_text(encoding="utf-8"))
    services = compose["services"]
    stream = next(s for s in services.values() if "stream" in s.get("profiles", []))
    command = " ".join(stream["command"]) if isinstance(stream["command"], list) else stream["command"]
    assert "--stream" in command
    assert "AUTODJ_ACCESS_TOKEN" in str(stream.get("environment", {})) or "env_file" in stream


def test_container_smoke_fetches_stream() -> None:
    script = (ROOT / "scripts" / "container_smoke.sh").read_text(encoding="utf-8")
    assert "/stream/" in script
    assert "--stream" in script
```

Reuse the file's existing `ROOT` and `yaml` imports; read how the LAN service is defined in `compose.yaml` first and model the stream service on it.

Add to `tests/unit/test_config_examples.py` inside `test_operator_docs_cover_reproducible_workflows`:

```python
    assert "autodj serve --stream" in operations
    assert "My Radio Stations" in operations
    assert "stream" in threat.lower() and "listen" in threat.lower()
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run --frozen pytest tests/unit/test_containerfile.py tests/unit/test_config_examples.py -q -o addopts="" -k "stream or operator_docs"`
Expected: FAIL.

- [ ] **Step 3: Implement**

1. `compose.yaml`: a `autodj-stream` service with `profiles: [stream]`, the same image, volumes and LAN environment as the LAN service, and `command: ["serve", "--host", "0.0.0.0", "--stream"]`; publish the port the same way the LAN profile does.
2. `scripts/container_smoke.sh`: after the existing LAN checks, start a container with `--stream`, read the secret with `docker exec <c> cat /data/index/.stream-secret` (use the container's configured index path), `curl -s --max-time 3 -H "Host: <allowed host>" http://127.0.0.1:<port>/stream/<secret>.mp3 -o /tmp/stream.mp3 || true`, and assert the file is at least 16 KB and starts with an MP3 frame sync (`head -c 2 /tmp/stream.mp3 | xxd -p | grep -Eq '^fff[bа-f]|^fffb|^fff3'`; use `od -An -tx1` if `xxd` is unavailable). Follow the script's existing helper and cleanup patterns.
3. `docs/operations.md`: new "Radio stream (Sonos, VLC and other players)" section: enabling (`autodj serve --stream`, `[stream] enabled = true`, the compose `stream` profile), the LAN requirement, finding the address (Settings, Stream), adding to Sonos (Sonos app: Browse → TuneIn → My Radio Stations → Add New Radio Station; if your app version lacks it, add the station in the TuneIn app or website, or use the `.m3u` link), adding to VLC (Media → Open Network Stream), quality setting, what Skip/Pause/Seek do, the 30-second idle rule, "Make new link", and troubleshooting (ffmpeg missing, host not allowed, listener limit).
4. `README.md`: a short "Play on Sonos or any network player" section pointing at operations.md, and add `--stream` to the command list.
5. `THREAT_MODEL.md`: stream section as in the spec ("Stream URL, secret and security").
6. `CHANGELOG.md` `[Unreleased]` → `### Added`: a user-facing entry describing stream mode, Listen here, the Stream settings section, and that `--server-audio` is now stereo and gapless; `### Fixed`: server audio replayed the start of each incoming track and its skipped intro; `/api/history` stayed empty with `--server-audio`.

- [ ] **Step 4: Run tests and the full suite**

Run: `uv run --frozen python scripts/ci_pytest.py`, `uv run --frozen pyright src/autodj/`, `npx vitest run`, `npm run lint`, `npm run build`, `npx knip`
Expected: all PASS.

- [ ] **Step 5: Commit**

```bash
git add compose.yaml scripts/container_smoke.sh docs/operations.md README.md THREAT_MODEL.md CHANGELOG.md tests
git commit -m "docs: document stream mode and add its container profile"
```

---

## Manual verification (owner)

1. `uv run autodj serve --stream` on the NAS (with the LAN setup), open Settings → Stream, copy the address.
2. Add it on Sonos; press play; confirm stereo, crossfades and the Sonos app showing "Artist - Title".
3. Skip and queue a track from the page; confirm the speaker follows a few seconds later.
4. Stop the Sonos, wait more than 30 seconds, press play again; confirm a new set starts at the beginning of a track.
5. With NVDA in Firefox and Chrome: Listen here, the idle message, the Stream settings section, "Make new link" dialog, and the Space/K and comma/full-stop shortcuts in stream mode.
