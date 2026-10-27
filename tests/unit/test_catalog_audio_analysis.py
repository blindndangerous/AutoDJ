import numpy as np
import pytest

from autodj.audio_analysis import ANALYSIS_VERSION, AudioAnalysis
from autodj.catalog import LibraryCatalog
from autodj.indexer import FEATURE_DIM, IndexEntry, file_fingerprint


def _result():
    return AudioAnalysis(
        version=ANALYSIS_VERSION,
        features=[float(i) for i in range(16)],
        energy=0.0,
        key=3,
        mode=1,
        bpm=120.0,
        tempo_confidence=0.8,
    )


def _add(catalog, path, *, bpm=0, key=-1):
    size, fingerprint = file_fingerprint(path)
    entry = IndexEntry(
        str(path),
        path.stem,
        "",
        "",
        "",
        bpm,
        0,
        10,
        0,
        key,
        -1,
        0,
        size=size,
        fingerprint=fingerprint,
    )
    catalog.put_entry(entry)
    return size, fingerprint


def test_audio_analysis_cache_is_identity_checked_and_survives_source_changes(tmp_path):
    """ADR-0006-06. Cached features follow exact source identity."""
    music = tmp_path / "music"
    music.mkdir()
    path = music / "track.flac"
    path.write_bytes(b"audio")
    catalog = LibraryCatalog(tmp_path / "library.db")
    try:
        source = catalog.add_source(music, analysis="audio")
        size, fingerprint = _add(catalog, path)
        result = _result()
        assert catalog.analysis_for_path(path) == "audio"
        assert catalog.store_audio_analysis(path, size, fingerprint, result)
        assert catalog.get_audio_analysis(path) == result.model_dump(mode="json")
        assert catalog.update_source(source["id"], True, False, analysis="basic")
        assert catalog.analysis_for_path(path) == "basic"
        assert catalog.update_source(source["id"], False, False)
        assert catalog.sources()[0]["analysis"] == "basic"
        assert catalog.get_audio_analysis(path) == result.model_dump(mode="json")
        path.write_bytes(b"changed")
        assert catalog.get_audio_analysis(path) is None
    finally:
        catalog.close()


def test_liner_policy_wins_and_invalid_audio_result_is_rejected(tmp_path):
    root = tmp_path / "music"
    liner_root = root / "liners"
    liner_root.mkdir(parents=True)
    path = liner_root / "id.wav"
    path.write_bytes(b"liner")
    catalog = LibraryCatalog(tmp_path / "library.db")
    try:
        catalog.add_source(root, analysis="similarity")
        catalog.add_source(liner_root, kind="liner", analysis="similarity")
        assert catalog.analysis_for_path(path) == "basic"
        size, fingerprint = _add(catalog, path)
        with pytest.raises(ValueError):
            catalog.store_audio_analysis(path, size, fingerprint, {"version": "wrong"})
        assert catalog.get_audio_analysis(path) is None
    finally:
        catalog.close()


def test_audio_estimates_preserve_manual_tags_and_existing_source_bpm(tmp_path):
    """ADR-0006-06. Computed estimates preserve operator metadata."""
    root = tmp_path / "music"
    root.mkdir()
    manual_path = root / "manual.flac"
    manual_path.write_bytes(b"manual audio")
    source_path = root / "source.flac"
    source_path.write_bytes(b"tagged audio")
    catalog = LibraryCatalog(tmp_path / "library.db")
    try:
        catalog.add_source(root)
        _add(catalog, manual_path)
        source_size, source_fingerprint = _add(catalog, source_path, bpm=99, key=1)
        catalog.retag(manual_path, {"bpm": 140, "key": 8})
        manual_size, manual_fingerprint = file_fingerprint(manual_path)
        result = _result()
        assert catalog.store_analysis(
            manual_path,
            manual_size,
            manual_fingerprint,
            {"energy": 0.1, "key": 2, "mode": 0, "bpm": 110, "tempo_confidence": 0.4},
            np.zeros(FEATURE_DIM, dtype=np.float32),
            audio_analysis=result,
        )
        manual = catalog.record_for_path(manual_path)
        assert (manual["bpm"], manual["key"]) == (140, 8)
        assert catalog.store_audio_analysis(source_path, source_size, source_fingerprint, result)
        source = catalog.record_for_path(source_path)
        assert (source["bpm"], source["key"]) == (99, 3)
    finally:
        catalog.close()
