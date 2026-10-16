"""Saved speech is durable; provider keys and temporary auditions are not archives."""

import json
import shutil
import zipfile
from datetime import datetime

import pytest

from autodj.backup import BackupError, _roots, _target, create_backup, restore_backup
from autodj.config import load_config
from autodj.speech import _empty, export_state
from autodj.stations import StationPaths
from tests.unit.test_backup import _config
from tests.unit.test_speech_providers import audio_bytes


def test_speech_export_omits_unfinished_work_and_retains_active_set(tmp_path):
    """ADR-0013-01: backup keeps local playback state without secrets or paid retries."""
    data = _empty()
    data["batches"]["pending"] = {"candidates": [{"state": "running"}]}
    data["sets"] = {"ready": {"state": "complete"}, "unfinished": {"state": "incomplete"}}
    data["active_set"] = "ready"
    (tmp_path / "speech.json").write_text(json.dumps(data))
    exported = export_state(tmp_path)
    assert exported["batches"] == {}
    assert set(exported["sets"]) == {"ready"}
    assert exported["active_set"] == "ready"


def test_restore_rejects_speech_cache_and_credentials(tmp_path):
    cfg = load_config(environ={"AUTODJ_INDEX_DIR": str(tmp_path)})
    roots = _roots(cfg)
    assert _target(roots, "speech/speech.json") == roots["speech"] / "speech.json"
    for name in [
        "speech/cache/sample.wav",
        "speech/speech_providers.json",
        "speech/sets/../../escape.wav",
    ]:
        with pytest.raises(BackupError):
            _target(roots, name)


def test_speech_round_trip_keeps_playable_saved_assets_and_excludes_keys(tmp_path):
    """ADR-0013-01: archive restore keeps local audio without any provider credentials."""
    cfg = _config(tmp_path)
    root = StationPaths.default(cfg).speech
    (root / "saved").mkdir(parents=True)
    (root / "sets" / "ready").mkdir(parents=True)
    (root / "cache").mkdir()
    key = "a" * 64
    data = _empty()
    data["saved"]["liner-one"] = {"label": "Station ID", "text": "Hello", "published": False}
    data["sets"]["ready"] = {
        "state": "complete",
        "minutes": {f"{h:02d}_{m:02d}": key for h in range(24) for m in range(60)},
    }
    data["active_set"] = "ready"
    data["batches"]["unfinished"] = {"candidates": [{"state": "running"}]}
    (root / "speech.json").write_text(json.dumps(data))
    (root / "saved" / "liner-one.wav").write_bytes(audio_bytes())
    (root / "saved" / "liner-one.json").write_text(json.dumps(data["saved"]["liner-one"]))
    (root / "sets" / "ready" / f"{key}.wav").write_bytes(audio_bytes())
    (root / "cache" / "temporary.wav").write_bytes(b"not backed up")
    (cfg.index.index_dir / "speech_providers.json").write_text("private-provider-key")
    archive = create_backup(cfg, tmp_path / "speech.zip", force=False)
    with zipfile.ZipFile(archive) as saved:
        assert "speech/saved/liner-one.wav" in saved.namelist()
        assert not any("cache/" in name or "speech_providers" in name for name in saved.namelist())
        assert json.loads(saved.read("speech/speech.json"))["batches"] == {}
    shutil.rmtree(root)
    restore_backup(cfg, archive, force=True)
    from autodj.speech import generated_clip

    assert generated_clip(root, datetime(2000, 1, 1, 15, 5))[1].read_bytes() == audio_bytes()
