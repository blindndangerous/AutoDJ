"""Review audio uses the real playout renderer and remains private."""

import shutil
from dataclasses import replace

import numpy as np
import pytest
import soundfile as sf

from autodj.hour_preview import IncomingHandoff, Item
from autodj.review_audio import ReviewSettings
from autodj.stereo import SAMPLE_RATE as SR
from tests.unit._rotation_station import make_station, song


def station(tmp_path, *, mode="fixed"):
    entries = []
    for number, frequency in enumerate((220, 660)):
        path = tmp_path / f"song-{number}.wav"
        time = np.arange(2 * SR) / SR
        audio = np.repeat((0.2 * np.sin(2 * np.pi * frequency * time))[:, None], 2, axis=1)
        sf.write(path, audio, SR, subtype="FLOAT")
        entries.append(replace(song(number, "Artist", length=2), path=str(path)))
    source = make_station(
        tmp_path / "station",
        entries,
        playback={
            "transition_mode": mode,
            "crossfade_seconds": 0.25,
            "loudness_processing": "off",
            "liners_duck_db": -12,
        },
    )
    rows = [
        Item("song", 100, 1.75, "show", entries[0]),
        Item("song", 101.75, 2, "show", entries[1]),
    ]
    return source, rows


@pytest.mark.parametrize(
    ("song_seconds", "scratch_limit"),
    [(2, 48 * 1024**2), (120, 2 * 1024**3)],
    ids=["small-budget", "sixty-minutes"],
)
def test_programme_renders_with_reusable_scratch_budget(tmp_path, song_seconds, scratch_limit):
    """ADR-0014-08: freed decodes must not accumulate against the scratch cap."""
    import time

    from autodj.review_render import render_review
    from autodj.stereo import audio_workspace

    if shutil.disk_usage(tmp_path).free < scratch_limit + 64 * 1024**2:
        pytest.skip("Programme render needs its scratch budget plus 64 MiB free")
    path = tmp_path / "silence.wav"
    # Keep real decoding and encoding; the small case must also release buffers
    # to fit its tighter budget, without allocating an hour of PCM.
    with sf.SoundFile(path, "w", samplerate=SR, channels=2, subtype="FLOAT") as source_file:
        source_file.seek(song_seconds * SR - 1)
        source_file.write(np.zeros((1, 2), np.float32))
    entry = replace(song(0, "Artist", length=song_seconds), path=str(path))
    source = make_station(
        tmp_path / "station",
        [entry],
        playback={
            "transition_mode": "fixed",
            "crossfade_seconds": 0,
            "loudness_processing": "off",
        },
    )
    rows = [
        Item("song", 100 + number * song_seconds, song_seconds, "show", entry)
        for number in range(30)
    ]
    output = tmp_path / "result"
    output.mkdir()
    deadline = time.monotonic() + 120
    with audio_workspace(tmp_path / "scratch", max_bytes=scratch_limit):
        metadata = render_review(
            source, rows, output, cancelled=lambda: time.monotonic() >= deadline
        )
    assert metadata["incomplete"] is False
    assert metadata["effective_range"][1] == pytest.approx(30 * song_seconds, abs=0.1)
    assert len(metadata["items"]) == 30
    assert sf.info(output / "review.wav").duration < 700
    assert (output / "review.mp3").stat().st_size > 100


def test_real_render_exports_matching_wav_mp3_and_does_not_touch_live_state(tmp_path):
    """ADR-0014-01: review uses isolated state and matching real audio exports."""
    from autodj.review_render import render_review
    from autodj.stereo import load_stereo

    source, rows = station(tmp_path)
    before = source.player._state.queue.copy()
    output = tmp_path / "result"
    output.mkdir()
    metadata = render_review(
        source,
        rows,
        output,
        settings=ReviewSettings(head_seconds=0.2, tail_seconds=0.2, tone_seconds=0.05),
    )
    wav, rate = sf.read(output / "review.wav", dtype="float32", always_2d=True)
    mp3 = load_stereo(str(output / "review.mp3"), SR)
    assert rate == SR
    # .2 s head, .25 s real overlap, .2 s tail, two .05 s skip tones.
    assert len(wav) / SR == pytest.approx(0.75, abs=2 / SR)
    assert abs(len(mp3) - len(wav)) < 0.1 * SR
    assert np.mean((mp3[: len(wav)] - wav[: len(mp3)]) ** 2) < 0.003
    assert metadata["pre_mastering"] is True
    assert metadata["incomplete"] is False
    assert len(metadata["items"]) == 2
    assert metadata["items"][1]["programme_start"] == pytest.approx(1.75, abs=0.02)
    assert source.library.catalog.writes == 0
    assert source.ledger.writes == 0
    assert source.player._state.queue == before
    assert sorted(path.name for path in output.iterdir()) == ["review.mp3", "review.wav"]
    assert not (source.player._cfg.index.library_dir / "dj_meta.db").exists()


def test_complete_target_and_repeated_occurrences_have_distinct_navigation(tmp_path):
    from autodj.review_render import render_review

    source, rows = station(tmp_path)
    rows[1] = replace(rows[1], entry=rows[0].entry)
    output = tmp_path / "result"
    output.mkdir()
    metadata = render_review(
        source,
        rows,
        output,
        keep_complete=("0",),
        settings=ReviewSettings(head_seconds=0.1, tail_seconds=0.1),
    )
    assert len({row["id"] for row in metadata["items"]}) == 2
    assert metadata["items"][0]["preview_end"] - metadata["items"][0]["preview_start"] >= 1.99


def test_unresolved_audio_uses_placeholder_and_marks_unknown_programme_times(tmp_path):
    """ADR-0014-03: unknown content is audible and downstream time stays unknown."""
    from autodj.review_render import render_review

    source, rows = station(tmp_path)
    rows.insert(1, Item("break", 101, 0, "predicted", label="Time check", duration_unknown=True))
    calls = []

    def placeholder(text, output):
        calls.append(text)
        return np.full((SR, 2), 0.1, dtype=np.float32)

    output = tmp_path / "result"
    output.mkdir()
    metadata = render_review(source, rows, output, placeholder=placeholder)
    assert len(calls) == 1
    assert metadata["incomplete"] is True
    assert len(metadata["omissions"]) == 1
    later_song = next(row for row in metadata["items"] if row["id"] == "2")
    assert later_song["programme_start"] is None
    assert later_song["programme_end"] is None
    assert len(sf.read(output / "review.wav")[0]) > SR


@pytest.mark.parametrize("configured", [False, True])
def test_unresolved_audio_without_tts_fails_actionably(tmp_path, configured):
    """ADR-0014-03: missing default TTS cannot silently omit unknown content."""
    from autodj.review_jobs import ReviewError
    from autodj.review_render import render_review

    source, rows = station(tmp_path)
    if configured:
        import sys

        source.player._cfg.time_checks.tts_command = [sys.executable, "-c", "raise SystemExit(1)"]
    rows.append(Item("break", 104, 0, "predicted", label="Missing clip", duration_unknown=True))
    output = tmp_path / "result"
    output.mkdir()
    with pytest.raises(ReviewError, match="TTS") as failure:
        render_review(source, rows, output)
    assert "Missing clip" in str(failure.value)
    assert not (output / "review.mp3").exists()


def test_cancelled_render_never_exports(tmp_path):
    from autodj.review_render import render_review

    source, rows = station(tmp_path)
    output = tmp_path / "result"
    output.mkdir()
    with pytest.raises(InterruptedError):
        render_review(source, rows, output, cancelled=lambda: True)
    assert not list(output.iterdir())


def test_private_overlay_uses_bus_ducking_and_preserves_original_audio(tmp_path):
    from autodj.review_render import render_review

    source, rows = station(tmp_path)
    voice = np.full((SR // 2, 2), 0.1, dtype=np.float32)
    rows.insert(1, Item("break", 100.5, 0.5, "predicted", label="ID", audio=voice))
    output = tmp_path / "result"
    output.mkdir()
    metadata = render_review(source, rows, output)
    mixed, _ = sf.read(output / "review.wav", dtype="float32", always_2d=True)
    # With a constant voice and sinusoidal music, the mean identifies the voice;
    # variance identifies the ducked music bed independently.
    during = mixed[round(0.8 * SR) : round(0.9 * SR)]
    after = mixed[round(1.4 * SR) : round(1.5 * SR)]
    assert np.mean(during) == pytest.approx(0.1, abs=0.004)
    assert np.std(during) < np.std(after) / 2
    assert np.all(voice == np.float32(0.1))
    assert any(row["label"] == "ID" for row in metadata["items"])


def test_saved_handoff_clip_plays_once_and_keeps_full_clip(tmp_path):
    from autodj.review_render import render_review
    from tests.unit._rotation_station import liner

    source, rows = station(tmp_path, mode="saved_points")
    voice_path = tmp_path / "voice.wav"
    voice = np.full((SR, 2), 0.3, dtype=np.float32)
    sf.write(voice_path, voice, SR, subtype="FLOAT")
    entry = replace(liner(1, length=1), path=str(voice_path))
    source.library._liners = (entry,)
    rows[1] = replace(rows[1], incoming_handoff=IncomingHandoff(liners=((entry, False),)))
    output = tmp_path / "result"
    output.mkdir()
    metadata = render_review(
        source, rows, output, settings=ReviewSettings(head_seconds=0.1, tail_seconds=0.1)
    )
    clips = [row for row in metadata["items"] if row["kind"] == "liner"]
    assert len(clips) == 1
    assert clips[0]["preview_end"] - clips[0]["preview_start"] == pytest.approx(1, abs=0.01)
    assert metadata["incomplete"] is False


def test_opening_handoff_is_rendered_before_first_song(tmp_path):
    from autodj.review_render import render_review
    from tests.unit._rotation_station import liner

    source, rows = station(tmp_path, mode="saved_points")
    path = tmp_path / "opening.wav"
    sf.write(path, np.full((SR, 2), 0.3, np.float32), SR, subtype="FLOAT")
    entry = replace(liner(1, length=1), path=str(path))
    source.library._liners = (entry,)
    rows[0] = replace(rows[0], incoming_handoff=IncomingHandoff(liners=((entry, False),)))
    output = tmp_path / "result"
    output.mkdir()
    metadata = render_review(source, rows, output)
    clips = [row for row in metadata["items"] if row["kind"] == "liner"]
    assert len(clips) == 1
    assert clips[0]["programme_start"] == pytest.approx(0, abs=1 / SR)
    assert clips[0]["programme_end"] == pytest.approx(1, abs=1 / SR)
    first = next(row for row in metadata["items"] if row["id"] == "0")
    assert first["programme_start"] >= 1


def test_captured_overlap_continues_once_without_repeating_incoming_head(tmp_path):
    from autodj.review_render import render_review

    source, rows = station(tmp_path)
    first = sf.read(rows[0].entry.path, dtype="float32", always_2d=True)[0]
    second = sf.read(rows[1].entry.path, dtype="float32", always_2d=True)[0]
    frames = round(0.25 * SR)
    fade = np.linspace(0, 1, frames, dtype=np.float32)[:, None]
    first[-frames:] = first[-frames:] * (1 - fade) + second[:frames] * fade
    rows[0] = replace(rows[0], audio=first.copy(), audio_starts=100)
    rows[1] = replace(rows[1], incoming_offset=frames)
    output = tmp_path / "result"
    output.mkdir()
    metadata = render_review(source, rows, output)
    rendered = sf.read(output / "review.wav", dtype="float32", always_2d=True)[0]
    assert len(rendered) == 4 * SR - frames
    assert np.max(np.abs(rendered[: 2 * SR] - first)) < 0.000001
    assert np.max(np.abs(rendered[2 * SR :] - second[frames:])) < 0.000001
    assert np.array_equal(rows[0].audio, first)
    assert metadata["items"][1]["programme_start"] == pytest.approx(1.75)


def test_selection_expands_to_preserve_complete_actual_transition(tmp_path):
    from autodj.review_render import render_review

    source, rows = station(tmp_path)
    output = tmp_path / "result"
    output.mkdir()
    metadata = render_review(source, rows, output, selection=(1.8, 1.9))
    assert metadata["effective_range"] == pytest.approx([1.75, 2])
    assert metadata["duration"] == pytest.approx(0.25)


def test_long_clip_is_shortened_unless_explicitly_kept_complete(tmp_path):
    from autodj.review_render import render_review

    source, _rows = station(tmp_path)
    voice = np.full((6 * SR, 2), 0.1, dtype=np.float32)
    rows = [Item("break", 100, 6, "show", label="Interview", audio=voice)]
    settings = ReviewSettings(head_seconds=0.1, tail_seconds=0.1, clip_threshold_seconds=1)
    short, complete = tmp_path / "short", tmp_path / "complete"
    short.mkdir()
    complete.mkdir()
    summary = render_review(source, rows, short, settings=settings)
    full = render_review(source, rows, complete, settings=settings, keep_complete=("0",))
    assert summary["duration"] < 1
    assert full["duration"] == pytest.approx(6)
    assert voice.shape == (6 * SR, 2)


def test_configured_default_tts_generates_real_placeholder_audio(tmp_path):
    import sys

    from autodj.review_render import render_review

    source, rows = station(tmp_path)
    script = (
        "import sys,wave; "
        "text=sys.stdin.read(); assert 'Missing review audio' in text; "
        "out=wave.open(sys.argv[1],'wb'); out.setnchannels(2); out.setsampwidth(2); "
        "out.setframerate(44100); out.writeframes(b'\\x00\\x10\\x00\\x10'*44100); out.close()"
    )
    source.player._cfg.time_checks.tts_command = [sys.executable, "-c", script, "{out}"]
    rows.insert(1, Item("break", 100.5, 0, "predicted", label="Time check", duration_unknown=True))
    output = tmp_path / "result"
    output.mkdir()
    metadata = render_review(source, rows, output)
    assert metadata["incomplete"] is True
    assert len(metadata["omissions"]) == 1
    assert sorted(path.name for path in output.iterdir()) == ["review.mp3", "review.wav"]


def test_encoder_failure_is_actionable_and_does_not_publish_mp3(tmp_path, monkeypatch):
    import sys

    from autodj import review_render
    from autodj.review_jobs import ReviewError

    source, rows = station(tmp_path)
    # Python rejects the FFmpeg-only argv on every supported platform.
    monkeypatch.setattr(review_render.shutil, "which", lambda _: sys.executable)
    output = tmp_path / "result"
    output.mkdir()
    with pytest.raises(ReviewError, match="MP3 export failed"):
        review_render.render_review(source, rows, output)
    assert not (output / "review.mp3").exists()


def test_render_uses_frozen_catalog_cues_without_mutating_live_points(tmp_path):
    from autodj.review_render import render_review

    source, rows = station(tmp_path, mode="saved_points")
    cues = {"start": 0.5, "end": 1.5, "segue": 1.25}
    source.library.catalog.points = lambda path: cues if path == rows[0].entry.path else {}
    output = tmp_path / "result"
    output.mkdir()
    metadata = render_review(source, rows[:1], output)
    audio = sf.read(output / "review.wav", dtype="float32", always_2d=True)[0]
    original = sf.read(rows[0].entry.path, dtype="float32", always_2d=True)[0]
    assert len(audio) == SR
    assert np.max(np.abs(audio - original[SR // 2 : 3 * SR // 2])) < 0.000001
    assert cues == {"start": 0.5, "end": 1.5, "segue": 1.25}
    assert metadata["duration"] == pytest.approx(1)


def test_stopped_station_reuses_persisted_dj_markers_read_only(tmp_path, monkeypatch):
    from autodj.dj_meta import Cue, DjMeta, DjMetaCache
    from autodj.player import Player
    from autodj.review_render import render_review

    source, rows = station(tmp_path, mode="fixed_skip_silence")
    sidecar = source.player._cfg.index.library_dir / "dj_meta.db"
    cache = DjMetaCache(sidecar, music_dir=source.player._cfg.library.music_dir)
    for row in rows:
        cache.set(
            row.entry.path,
            DjMeta(
                analysed=True,
                intro_start_s=0.5,
                intro_end_s=1,
                outro_start_s=1.75,
                cues=[Cue(0.5, "intro_start", source="user")],
            ),
        )
    cache.flush(force=True)
    cache.close()
    before = sidecar.read_bytes()

    def unexpected_analysis(*args):
        pytest.fail("Persisted analysed DJ markers must be reused")

    monkeypatch.setattr(Player, "_analyse", unexpected_analysis)
    output = tmp_path / "result"
    output.mkdir()
    metadata = render_review(source, rows, output)
    assert metadata["duration"] == pytest.approx(3.25, abs=2 / SR)
    assert sidecar.read_bytes() == before
    assert source.library.catalog.writes == 0


def test_encoder_failure_closes_mapped_pcm_before_scratch_cleanup(tmp_path, monkeypatch):
    from autodj import review_render
    from autodj.review_jobs import ReviewError

    source, rows = station(tmp_path)
    mappings = []
    original_shorten = review_render.shorten_review
    original_directory = review_render.tempfile.TemporaryDirectory

    def capture_mapping(*args, **kwargs):
        rendered = original_shorten(*args, **kwargs)
        mappings.append(rendered.audio)
        return rendered

    class CheckedDirectory(original_directory):
        def __exit__(self, *args):
            assert all(mapping._mmap.closed for mapping in mappings)
            return super().__exit__(*args)

    def failed_encoder(*args):
        raise ReviewError("MP3 export failed")

    monkeypatch.setattr(review_render, "shorten_review", capture_mapping)
    monkeypatch.setattr(review_render.tempfile, "TemporaryDirectory", CheckedDirectory)
    monkeypatch.setattr(review_render, "_encode", failed_encoder)
    output = tmp_path / "result"
    output.mkdir()
    with pytest.raises(ReviewError, match="MP3 export failed"):
        review_render.render_review(source, rows, output)
    assert mappings[0]._mmap.closed


def test_continuous_piece_cannot_claim_known_endpoint_after_unknown_duration(tmp_path):
    from autodj.review_render import render_review

    source, rows = station(tmp_path)
    rows.insert(1, Item("break", 100.5, 0, "predicted", label="Time check", duration_unknown=True))
    output = tmp_path / "result"
    output.mkdir()
    metadata = render_review(
        source, rows, output, placeholder=lambda *_: np.full((SR, 2), 0.1, np.float32)
    )
    assert metadata["effective_range"] == [0, None]
    for piece in metadata["pieces"]:
        if piece["kind"] == "audio" and piece["programme_start"] is not None:
            assert piece["programme_end"] is None or piece["programme_end"] <= 0.5


def test_hard_event_uses_actual_skip_fade_and_resets_incoming_offset(tmp_path):
    from autodj.review_render import render_review

    source, rows = station(tmp_path)
    rows[0] = replace(rows[0], seconds=1, cut_at=101)
    rows[1] = replace(rows[1], starts=101)
    output = tmp_path / "result"
    output.mkdir()
    metadata = render_review(source, rows, output)
    rendered = sf.read(output / "review.wav", dtype="float32", always_2d=True)[0]
    first = sf.read(rows[0].entry.path, dtype="float32", always_2d=True)[0]
    second = sf.read(rows[1].entry.path, dtype="float32", always_2d=True)[0]
    fade_frames = round(0.15 * SR)
    after = SR + fade_frames
    assert len(rendered) == after + 2 * SR
    assert np.max(np.abs(rendered[:SR] - first[:SR])) < 0.000001
    expected_fade = (
        first[SR:after] * ((fade_frames - np.arange(fade_frames)) / fade_frames)[:, None]
    )
    assert np.max(np.abs(rendered[SR:after] - expected_fade)) < 0.000001
    assert np.max(np.abs(rendered[after:] - second)) < 0.000001
    assert metadata["items"][1]["programme_start"] == pytest.approx(1.15)


@pytest.mark.parametrize("seconds,threshold,shortened", [(1, 5, False), (6, 1, True)])
def test_standalone_clip_entry_uses_same_joins_and_threshold(
    tmp_path, seconds, threshold, shortened
):
    from autodj.review_render import render_review
    from tests.unit._rotation_station import liner

    source, rows = station(tmp_path)
    path = tmp_path / "interview.wav"
    voice = np.full((seconds * SR, 2), 0.3, dtype=np.float32)
    sf.write(path, voice, SR, subtype="FLOAT")
    entry = replace(liner(1, length=seconds), path=str(path))
    clip = Item(
        "clip", 101.75, seconds - 0.25, "show", entry, label="Interview", occurrence_id="interview"
    )
    rows.insert(1, clip)
    rows[2] = replace(rows[2], starts=101.5 + seconds)
    output = tmp_path / "result"
    output.mkdir()
    metadata = render_review(
        source,
        rows,
        output,
        settings=ReviewSettings(
            head_seconds=0.1, tail_seconds=0.1, clip_threshold_seconds=threshold
        ),
    )
    result = next(row for row in metadata["items"] if row["id"] == "interview")
    assert metadata["incomplete"] is False
    if shortened:
        assert result["preview_end"] - result["preview_start"] < seconds
    else:
        assert result["preview_end"] - result["preview_start"] >= seconds
    assert len(metadata["items"]) == 3


def test_configured_skip_effect_receives_actual_speed_and_consumed_source_offset(
    tmp_path, monkeypatch
):
    from autodj.player import Player
    from autodj.review_render import render_review

    source, rows = station(tmp_path)
    source.player._cfg.playback.music_speed_percent = 10
    source.player._cfg.playback.skip_style = "echo_out"
    rows[1] = replace(rows[1], cut_at=102.5)
    rows.append(replace(rows[0], starts=102.5))
    observed = []
    original_skip = Player._plan_skip

    def capture_skip(player, track, position):
        observed.append((track.playback_rate, track.start_offset))
        return original_skip(player, track, position)

    monkeypatch.setattr(Player, "_plan_skip", capture_skip)
    output = tmp_path / "result"
    output.mkdir()
    render_review(source, rows, output)
    assert observed == [(1.1, 11025)]
