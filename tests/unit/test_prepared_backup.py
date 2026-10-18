"""Edition manifests and their immutable source assets form one backup snapshot."""

import json
import zipfile

import pytest

from autodj.backup import BackupError, _roots, _target, create_backup, restore_backup
from autodj.prepared_editions import EditionStore
from autodj.shows import Show
from autodj.stations import StationPaths
from tests.unit.test_backup import _config
from tests.unit.test_prepared_editions import voice


def prepared(tmp_path):
    cfg = _config(tmp_path)
    paths = StationPaths.default(cfg)
    store = EditionStore(paths.editions, paths.edition_audio)
    edition = store.create(
        "b" * 32, Show(name="Future", source="category", category="test", songs=["/music/a.wav"])
    )
    edition = store.import_take(
        edition.id,
        edition.revision,
        voice(tmp_path),
        position="opening",
        after_id=edition.occurrences[0].id,
    )
    return cfg, store, edition


def test_backup_restores_editions_and_source_audio_but_not_partial_imports(tmp_path):
    """ADR-0015-06: editions and immutable source audio survive native restore."""
    cfg, store, edition = prepared(tmp_path)
    (store.audio / ".upload-incomplete").mkdir()
    (store.audio / ".upload-incomplete" / "source.wav").write_bytes(b"partial")
    archive = create_backup(cfg, tmp_path / "edition.zip", force=False)
    with zipfile.ZipFile(archive) as bundle:
        names = bundle.namelist()
        assert "index/editions.json" in names
        assert any(name.startswith("edition_audio/") and name.endswith(".wav") for name in names)
        assert not any(".upload" in name for name in names)
    store.delete(edition.id, edition.revision)
    restore_backup(cfg, archive, force=True)
    assert store.get(edition.id) == edition
    assert store.asset_path(edition.takes[0].asset_id).is_file()


def test_restore_only_accepts_flat_opaque_edition_wav_assets(tmp_path):
    cfg, store, edition = prepared(tmp_path)
    roots = _roots(cfg)
    asset = edition.assets[edition.takes[0].asset_id]
    assert _target(roots, f"edition_audio/{asset.file}") == store.audio / asset.file
    for invalid in [
        "edition_audio/script.py",
        "edition_audio/.upload/source.wav",
        "edition_audio/../../outside.wav",
    ]:
        with pytest.raises(BackupError):
            _target(roots, invalid)


def test_backup_freezes_manifest_before_concurrent_import(tmp_path, monkeypatch):
    import autodj.backup as module

    cfg, store, edition = prepared(tmp_path)
    original = module._snapshot

    def snapshot(*args, **kwargs):
        result = original(*args, **kwargs)
        store.import_take(
            edition.id,
            edition.revision,
            voice(tmp_path, 0.2),
            position="closing",
            before_id=edition.occurrences[-1].id,
        )
        return result

    monkeypatch.setattr(module, "_snapshot", snapshot)
    archive = create_backup(cfg, tmp_path / "snapshot.zip", force=False)
    with zipfile.ZipFile(archive) as bundle:
        saved = json.loads(bundle.read("index/editions.json"))["editions"][edition.id]
        assert saved["revision"] == edition.revision
        assert len(saved["assets"]) == 1
        assert len([name for name in bundle.namelist() if name.startswith("edition_audio/")]) == 1
