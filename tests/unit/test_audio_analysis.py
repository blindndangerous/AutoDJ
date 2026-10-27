"""Focused contracts for shared, validated audio features."""

import numpy as np
import pytest

from autodj.audio_analysis import ANALYSIS_VERSION, AudioAnalysis, analyse_audio_features


def test_audio_analysis_validates_version_ranges_and_vector_length():
    base = {
        "version": ANALYSIS_VERSION,
        "features": [0.0] * 16,
        "energy": 0.0,
        "key": -1,
        "mode": -1,
        "bpm": 0.0,
        "tempo_confidence": 0.0,
    }
    assert AudioAnalysis.model_validate(base).version == ANALYSIS_VERSION
    with pytest.raises(ValueError):
        AudioAnalysis.model_validate({**base, "features": [0.0] * 15})
    with pytest.raises(ValueError):
        AudioAnalysis.model_validate({**base, "version": "old"})
    with pytest.raises(ValueError):
        AudioAnalysis.model_validate({**base, "bpm": float("nan")})


def test_analyse_audio_features_shares_one_beat_track(monkeypatch):
    import autodj.audio_analysis as shared
    import autodj.dj_meta as dj_meta

    calls = []
    monkeypatch.setattr(shared.librosa.feature, "rms", lambda **kw: np.array([[0.2]]))
    monkeypatch.setattr(
        shared.librosa.feature, "spectral_centroid", lambda **kw: np.array([[12.0]])
    )
    monkeypatch.setattr(
        shared.librosa.feature, "zero_crossing_rate", lambda **kw: np.array([[0.1]])
    )
    monkeypatch.setattr(shared.librosa.feature, "chroma_stft", lambda **kw: np.ones((12, 1)) / 12)
    monkeypatch.setattr(shared.librosa.onset, "onset_strength", lambda **kw: np.array([0.3]))

    def beat_track(**kwargs):
        calls.append(kwargs)
        return 120.0, np.array([1, 2])

    monkeypatch.setattr(shared.librosa.beat, "beat_track", beat_track)
    monkeypatch.setattr(
        shared.librosa, "frames_to_time", lambda frames, **kw: np.asarray(frames) / 10
    )
    captured = {}

    def fake_analyse(audio, sr, intro, outro, beats=None):
        captured["beats"] = beats
        return object()

    monkeypatch.setattr(dj_meta, "analyse_audio", fake_analyse)
    model, meta = analyse_audio_features(np.ones(16000, dtype=np.float32), 8000)
    assert len(calls) == 1
    assert np.isclose(model.features[0], model.energy)
    assert model.bpm == 120.0
    assert captured["beats"] == [0.1, 0.2]
    assert meta is not None


@pytest.mark.parametrize("windowed", [False, True])
def test_long_audio_uses_only_head_and_tail_five_minutes(monkeypatch, windowed):
    import autodj.audio_analysis as shared
    import autodj.dj_meta as dj_meta

    monkeypatch.setattr(shared, "SAMPLE_RATE", 1)
    monkeypatch.setattr(shared.librosa.feature, "rms", lambda **kw: np.array([[0.2]]))
    monkeypatch.setattr(
        shared.librosa.feature, "spectral_centroid", lambda **kw: np.array([[12.0]])
    )
    monkeypatch.setattr(
        shared.librosa.feature, "zero_crossing_rate", lambda **kw: np.array([[0.1]])
    )
    monkeypatch.setattr(shared.librosa.feature, "chroma_stft", lambda **kw: np.ones((12, 1)) / 12)
    monkeypatch.setattr(shared.librosa.onset, "onset_strength", lambda **kw: np.array([0.3]))
    lengths = []

    def beat_track(**kwargs):
        lengths.append(len(kwargs["y"]))
        return 120.0, np.array([1])

    monkeypatch.setattr(shared.librosa.beat, "beat_track", beat_track)
    monkeypatch.setattr(shared.librosa, "frames_to_time", lambda frames, **kw: np.asarray(frames))
    captured = {}

    def fake_analyse_windows(head, tail, tail_start, sr, intro, outro, beats=None):
        captured["beats"] = beats
        return object()

    monkeypatch.setattr(dj_meta, "analyse_windows", fake_analyse_windows)
    audio = np.ones(1000, dtype=np.float32)
    if windowed:
        from autodj.processing import TrackWindows

        audio = TrackWindows(audio[:300], audio[-300:], 700, 1000, 1.0, 0, 1000)
    analysis, _ = analyse_audio_features(audio, 1)
    assert lengths == [300, 300]
    assert captured["beats"] == ([1.0], [1.0])
    assert analysis.version == ANALYSIS_VERSION
