"""Read-only diagnostics for the ``autodj doctor`` command."""

from __future__ import annotations

import builtins
import gc
import json
import shutil
import sqlite3
import subprocess
import sys
import warnings
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import numpy as np
import pytest
from click.testing import CliRunner

import autodj.doctor as doctor
from autodj.cli import _load_cfg_or_exit, cli
from autodj.config import (
    AutoDJConfig,
    HuggingFaceConfig,
    IndexConfig,
    LibraryConfig,
    ModelConfig,
    PlaybackConfig,
    ServerConfig,
)
from autodj.doctor import (
    CheckStatus,
    DoctorCheck,
    DoctorReport,
    _bundle_check,
    _dependency_check,
    _python_check,
    render_text,
    run_doctor,
)
from autodj.index_manifest import sha256_file, tombstone_publication
from autodj.indexer import FEATURE_DIM, IndexEntry, save_index


def _config(tmp_path: Path, *, host: str = "127.0.0.2") -> AutoDJConfig:
    music = tmp_path / "music"
    index = tmp_path / "index"
    models = tmp_path / "models"
    music.mkdir(parents=True, exist_ok=True)
    index.mkdir(parents=True, exist_ok=True)
    models.mkdir(parents=True, exist_ok=True)
    return AutoDJConfig(
        library=LibraryConfig(music, None, ["flac"]),
        index=IndexConfig(index, models, "default"),
        playback=PlaybackConfig(),
        model=ModelConfig(),
        huggingface=HuggingFaceConfig("hf_secret_value"),
        config_path=tmp_path / "config.toml",
        server=ServerConfig(host=host, access_token="s" * 32),
        config_sources=("defaults", str(tmp_path / "config.toml")),
    )


def _write_index(cfg: AutoDJConfig) -> None:
    cfg.index.active_dir.mkdir(parents=True, exist_ok=True)
    track = cfg.library.music_dir / "song.flac"
    track.write_bytes(b"audio")
    entry = IndexEntry(
        path=str(track),
        title="Song",
        artist="Artist",
        album="Album",
        genre="Rock",
        bpm=120.0,
        year=2025,
        length=180.0,
        energy=0.5,
        key=0,
        mode=1,
        tempo_confidence=0.9,
    )
    vector = np.ones((1, FEATURE_DIM), dtype=np.float32)
    vector /= np.linalg.norm(vector, axis=1, keepdims=True)
    save_index([entry], vector, cfg.index.active_dir)


def _mutate_published_track(cfg: AutoDJConfig, column: str, value: object) -> None:
    manifest_path = cfg.index.active_dir / "index-manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    tracks = cfg.index.active_dir / manifest["tracks_file"]
    with closing(sqlite3.connect(tracks)) as conn:
        conn.execute("PRAGMA journal_mode=DELETE")
        conn.execute(f'UPDATE tracks SET "{column}" = ?', (value,))
        conn.commit()
    manifest["tracks_sha256"] = sha256_file(tracks)
    manifest_path.write_text(json.dumps(manifest, sort_keys=True) + "\n", encoding="utf-8")


def _replace_published_vectors(cfg: AutoDJConfig, index: object) -> None:
    import faiss

    manifest_path = cfg.index.active_dir / "index-manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    vectors = cfg.index.active_dir / manifest["vectors_file"]
    faiss.write_index(index, str(vectors))
    manifest["vectors_sha256"] = sha256_file(vectors)
    manifest_path.write_text(json.dumps(manifest, sort_keys=True) + "\n", encoding="utf-8")


def _write_dj_meta(path: Path) -> None:
    with closing(sqlite3.connect(path)) as conn:
        conn.execute(
            """CREATE TABLE dj_meta (
                path TEXT PRIMARY KEY,
                intro_end_s REAL NOT NULL DEFAULT 0,
                outro_start_s REAL NOT NULL DEFAULT 0,
                analysed INTEGER NOT NULL DEFAULT 0,
                beats TEXT,
                cues TEXT
            )"""
        )
        conn.commit()


def _check(report, name: str):
    return next(check for check in report.checks if check.name == name)


def _tree_snapshot(root: Path) -> dict[Path, tuple[bool, int, int, str | None]]:
    paths = [root, *root.rglob("*")]
    return {
        path.relative_to(root): (
            path.is_dir(),
            path.stat().st_size,
            path.stat().st_mtime_ns,
            None if path.is_dir() else sha256_file(path),
        )
        for path in paths
        if path.exists()
    }


def test_healthy_report_is_read_only_and_redacts_tokens(tmp_path: Path) -> None:
    cfg = _config(tmp_path)
    _write_index(cfg)
    _write_dj_meta(cfg.index.active_dir / "dj_meta.db")
    before = _tree_snapshot(tmp_path)

    report = doctor.run_doctor(cfg, python_version=(3, 14))
    text = doctor.render_text(report)
    payload = report.to_json()

    assert report.exit_code == 0
    assert cfg.server.host in text
    assert cfg.server.access_token not in text
    assert cfg.server.access_token not in payload
    assert cfg.huggingface.token not in text
    assert cfg.huggingface.token not in payload
    assert before == _tree_snapshot(tmp_path)


def test_import_and_empty_index_check_do_not_import_heavy_runtimes() -> None:
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import sys, tempfile; from pathlib import Path; "
            "from autodj.config import load_config; "
            "from autodj.doctor import _index_check; "
            "root=tempfile.TemporaryDirectory(); base=Path(root.name); "
            "cfg=load_config(None, environ={"
            "'AUTODJ_LIBRARY_MUSIC_DIR': str(base/'music'), "
            "'AUTODJ_INDEX_DIR': str(base/'index'), "
            "'AUTODJ_MODEL_DIR': str(base/'models')}); "
            "_index_check(cfg); "
            "heavy={'autodj.indexer','autodj.model','faiss','numpy','librosa','soundfile'}; "
            "loaded=sorted(heavy.intersection(sys.modules)); assert not loaded, loaded",
        ],
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize("remaining", ["tracks.db", "vectors.index"])
def test_partial_legacy_index_fails_actionably(tmp_path: Path, remaining: str) -> None:
    cfg = _config(tmp_path)
    cfg.index.active_dir.mkdir()
    (cfg.index.active_dir / remaining).write_bytes(b"partial")

    check = doctor._index_check(cfg)

    assert check.status is doctor.CheckStatus.FAIL
    assert "autodj index" in check.detail


def test_corrupt_published_index_fails(tmp_path: Path) -> None:
    cfg = _config(tmp_path)
    _write_index(cfg)
    manifest = json.loads(
        (cfg.index.active_dir / "index-manifest.json").read_text(encoding="utf-8")
    )
    (cfg.index.active_dir / manifest["vectors_file"]).write_bytes(b"corrupt")

    check = doctor._index_check(cfg)

    assert check.status is doctor.CheckStatus.FAIL
    assert "autodj index" in check.detail


def test_malformed_published_numeric_metadata_fails_actionably(tmp_path: Path) -> None:
    cfg = _config(tmp_path)
    _write_index(cfg)
    manifest_path = cfg.index.active_dir / "index-manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    tracks = cfg.index.active_dir / manifest["tracks_file"]
    with closing(sqlite3.connect(tracks)) as conn:
        conn.execute("PRAGMA journal_mode=DELETE")
        conn.execute("UPDATE tracks SET bpm = 'not-a-number'")
        conn.commit()
    manifest["tracks_sha256"] = sha256_file(tracks)
    manifest_path.write_text(json.dumps(manifest, sort_keys=True) + "\n", encoding="utf-8")

    check = doctor._index_check(cfg)

    assert check.status is doctor.CheckStatus.FAIL
    assert "not-a-number" in check.detail
    assert "autodj index" in check.detail


@pytest.mark.parametrize(
    ("column", "value"),
    [
        ("path", sqlite3.Binary(b"blob-path")),
        ("title", sqlite3.Binary(b"blob-title")),
        ("artist", sqlite3.Binary(b"blob-artist")),
        ("album", sqlite3.Binary(b"blob-album")),
        ("genre", sqlite3.Binary(b"blob-genre")),
        ("vec_row", 0.5),
        ("year", 0.5),
        ("key", 0.5),
        ("mode", 0.5),
    ],
    ids=[
        "blob-path",
        "blob-title",
        "blob-artist",
        "blob-album",
        "blob-genre",
        "fractional-vec-row",
        "fractional-year",
        "fractional-key",
        "fractional-mode",
    ],
)
def test_published_rows_require_exact_runtime_storage_classes(
    tmp_path: Path,
    column: str,
    value: object,
) -> None:
    cfg = _config(tmp_path)
    _write_index(cfg)
    _mutate_published_track(cfg, column, value)

    check = doctor._index_check(cfg)

    assert check.status is doctor.CheckStatus.FAIL
    assert column in check.detail


@pytest.mark.parametrize(
    "column",
    ["bpm", "length", "energy", "tempo_confidence", "embedded_at"],
)
def test_published_rows_reject_infinite_runtime_numerics(
    tmp_path: Path,
    column: str,
) -> None:
    cfg = _config(tmp_path)
    _write_index(cfg)
    _mutate_published_track(cfg, column, float("inf"))

    check = doctor._index_check(cfg)

    assert check.status is doctor.CheckStatus.FAIL
    assert column in check.detail


def test_published_rows_reject_nan_runtime_numeric(tmp_path: Path) -> None:
    cfg = _config(tmp_path)
    _write_index(cfg)
    _mutate_published_track(cfg, "bpm", "NaN")

    check = doctor._index_check(cfg)

    assert check.status is doctor.CheckStatus.FAIL
    assert "bpm" in check.detail


@pytest.mark.parametrize(
    "kind",
    ["wrong-dimension", "wrong-metric", "unsupported-index-type"],
)
def test_published_vectors_require_runtime_faiss_contract(
    tmp_path: Path,
    kind: str,
) -> None:
    import faiss

    cfg = _config(tmp_path)
    _write_index(cfg)
    if kind == "wrong-dimension":
        index = faiss.IndexFlatIP(FEATURE_DIM - 1)
        index.add(np.ones((1, FEATURE_DIM - 1), dtype=np.float32))
    elif kind == "wrong-metric":
        index = faiss.IndexFlatIP(FEATURE_DIM)
        index.add(np.ones((1, FEATURE_DIM), dtype=np.float32))
        index.metric_type = faiss.METRIC_L2
    else:
        index = faiss.IndexIDMap(faiss.IndexFlatIP(FEATURE_DIM))
        index.add_with_ids(
            np.ones((1, FEATURE_DIM), dtype=np.float32),
            np.array([0], dtype=np.int64),
        )
    _replace_published_vectors(cfg, index)

    check = doctor._index_check(cfg)

    assert check.status is doctor.CheckStatus.FAIL
    assert "FAISS" in check.detail


@pytest.mark.parametrize("unavailable", ["faiss", "autodj.indexer"])
def test_published_index_import_errors_are_actionable_failures(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    unavailable: str,
) -> None:
    cfg = _config(tmp_path)
    _write_index(cfg)
    original_import = builtins.__import__

    def guarded_import(name: str, *args: object, **kwargs: object):
        if name == unavailable:
            raise ImportError(f"{unavailable} unavailable")
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", guarded_import)

    check = doctor._index_check(cfg)

    assert check.status is doctor.CheckStatus.FAIL
    assert check.name == "index-coherence"
    assert "install" in check.detail.lower()


def test_published_index_import_does_not_catch_system_exit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cfg = _config(tmp_path)
    _write_index(cfg)
    original_import = builtins.__import__

    def guarded_import(name: str, *args: object, **kwargs: object):
        if name == "faiss":
            raise SystemExit(7)
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", guarded_import)

    with pytest.raises(SystemExit, match="7"):
        doctor._index_check(cfg)


def test_corrupt_dj_meta_fails_without_touching_file(tmp_path: Path) -> None:
    cfg = _config(tmp_path)
    cfg.index.active_dir.mkdir()
    db = cfg.index.active_dir / "dj_meta.db"
    db.write_bytes(b"not sqlite")
    before = db.stat().st_mtime_ns

    check = doctor._dj_meta_database_check(cfg)

    assert check.status is doctor.CheckStatus.FAIL
    assert "rebuild" in check.detail.lower()
    assert db.stat().st_mtime_ns == before


def test_sqlite_checks_close_read_only_connections(tmp_path: Path) -> None:
    cfg = _config(tmp_path)
    cfg.index.active_dir.mkdir()
    _write_dj_meta(cfg.index.active_dir / "dj_meta.db")

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always", ResourceWarning)
        doctor._dj_meta_database_check(cfg)
        gc.collect()

    assert not [warning for warning in caught if "unclosed database" in str(warning.message)]


def test_active_wal_database_is_refused_without_mutation(tmp_path: Path) -> None:
    cfg = _config(tmp_path)
    cfg.index.active_dir.mkdir()
    db = cfg.index.active_dir / "dj_meta.db"
    with closing(sqlite3.connect(db)) as writer:
        assert writer.execute("PRAGMA journal_mode=WAL").fetchone()[0] == "wal"
        writer.execute(
            """CREATE TABLE dj_meta (
                path TEXT PRIMARY KEY,
                intro_end_s REAL NOT NULL DEFAULT 0,
                outro_start_s REAL NOT NULL DEFAULT 0,
                analysed INTEGER NOT NULL DEFAULT 0,
                beats TEXT,
                cues TEXT
            )"""
        )
        writer.execute("INSERT INTO dj_meta (path) VALUES ('current.flac')")
        writer.commit()
        before = _tree_snapshot(tmp_path)

        check = doctor._dj_meta_database_check(cfg)

        assert check.status is doctor.CheckStatus.FAIL
        assert "WAL" in check.detail
        assert before == _tree_snapshot(tmp_path)


def test_wal_without_shm_refuses_without_creating_sidecars(tmp_path: Path) -> None:
    cfg = _config(tmp_path)
    cfg.index.active_dir.mkdir()
    source = tmp_path / "source.db"
    with closing(sqlite3.connect(source)) as writer:
        assert writer.execute("PRAGMA journal_mode=WAL").fetchone()[0] == "wal"
        writer.execute(
            """CREATE TABLE dj_meta (
                path TEXT PRIMARY KEY,
                intro_end_s REAL NOT NULL DEFAULT 0,
                outro_start_s REAL NOT NULL DEFAULT 0,
                analysed INTEGER NOT NULL DEFAULT 0,
                beats TEXT,
                cues TEXT
            )"""
        )
        writer.execute("INSERT INTO dj_meta (path) VALUES ('current.flac')")
        writer.commit()
        source_wal = Path(f"{source}-wal")
        assert source_wal.stat().st_size > 0
        db = cfg.index.active_dir / "dj_meta.db"
        shutil.copyfile(source, db)
        shutil.copyfile(source_wal, Path(f"{db}-wal"))
        before = _tree_snapshot(tmp_path)

        check = doctor._dj_meta_database_check(cfg)

        assert check.status is doctor.CheckStatus.FAIL
        assert "WAL" in check.detail
        assert not Path(f"{db}-shm").exists()
        assert before == _tree_snapshot(tmp_path)


def test_sqlite_validation_rejects_concurrent_sidecar_change(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db = tmp_path / "dj_meta.db"
    _write_dj_meta(db)
    wal = Path(f"{db}-wal")
    original_open = doctor._open_readonly_sqlite
    original_hash = doctor.sha256_file
    original_path_open = Path.open

    class TouchingConnection:
        def __init__(self, *, immutable: bool):
            self._conn = original_open(db, immutable=immutable)
            self._touched = False

        def execute(self, query: str):
            if not self._touched:
                wal.write_bytes(b"appeared concurrently")
                self._touched = True
            return self._conn.execute(query)

        def close(self) -> None:
            self._conn.close()

    monkeypatch.setattr(
        doctor,
        "_open_readonly_sqlite",
        lambda _path, *, immutable=False: TouchingConnection(immutable=immutable),
    )

    def guarded_hash(path: Path) -> str:
        if path == wal:
            raise AssertionError("doctor hashed a concurrently created WAL")
        return original_hash(path)

    def guarded_path_open(path: Path, *args: object, **kwargs: object):
        mode = str(args[0] if args else kwargs.get("mode", "r"))
        if path == wal and "r" in mode:
            raise AssertionError("doctor opened a concurrently created WAL for reading")
        return original_path_open(path, *args, **kwargs)

    monkeypatch.setattr(doctor, "sha256_file", guarded_hash)
    monkeypatch.setattr(Path, "open", guarded_path_open)

    with pytest.raises(sqlite3.DatabaseError, match="changed during validation"):
        doctor._validate_sqlite(db, "dj_meta", doctor._DJ_META_COLUMNS)


@pytest.mark.parametrize(
    "error",
    [PermissionError("sidecar permission denied"), OSError("sidecar stat I/O failed")],
)
def test_sqlite_sidecar_stat_errors_are_actionable_failures(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    error: OSError,
) -> None:
    cfg = _config(tmp_path)
    cfg.index.active_dir.mkdir()
    db = cfg.index.active_dir / "dj_meta.db"
    _write_dj_meta(db)
    blocked_sidecar = Path(f"{db}-wal")
    original_lstat = Path.lstat

    def guarded_lstat(path: Path, *args: object, **kwargs: object):
        if path == blocked_sidecar:
            raise error
        return original_lstat(path, *args, **kwargs)

    monkeypatch.setattr(Path, "lstat", guarded_lstat)

    check = doctor._dj_meta_database_check(cfg)

    assert check.status is doctor.CheckStatus.FAIL
    assert str(error) in check.detail


def test_existing_sqlite_sidecar_is_rejected_without_opening_or_hashing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cfg = _config(tmp_path)
    cfg.index.active_dir.mkdir()
    db = cfg.index.active_dir / "dj_meta.db"
    _write_dj_meta(db)
    wal = Path(f"{db}-wal")
    wal.write_bytes(b"existing WAL contents")
    original_hash = doctor.sha256_file
    original_open = Path.open

    def guarded_hash(path: Path) -> str:
        if path == wal:
            raise AssertionError("doctor hashed an existing WAL")
        return original_hash(path)

    def guarded_open(path: Path, *args: object, **kwargs: object):
        if path == wal:
            raise AssertionError("doctor opened an existing WAL")
        return original_open(path, *args, **kwargs)

    monkeypatch.setattr(doctor, "sha256_file", guarded_hash)
    monkeypatch.setattr(Path, "open", guarded_open)

    check = doctor._dj_meta_database_check(cfg)

    assert check.status is doctor.CheckStatus.FAIL
    assert "WAL" in check.detail


@pytest.mark.parametrize("dangling", [False, True], ids=["target", "dangling"])
def test_sqlite_sidecar_symlink_is_rejected_without_following(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    dangling: bool,
) -> None:
    cfg = _config(tmp_path)
    cfg.index.active_dir.mkdir()
    db = cfg.index.active_dir / "dj_meta.db"
    _write_dj_meta(db)
    target = tmp_path / "sidecar-target"
    if not dangling:
        target.write_bytes(b"target contents")
    wal = Path(f"{db}-wal")
    try:
        wal.symlink_to(target)
    except (NotImplementedError, OSError) as exc:
        pytest.skip(f"symlinks unavailable: {exc}")
    original_hash = doctor.sha256_file
    original_open = Path.open

    def guarded_hash(path: Path) -> str:
        if path == wal:
            raise AssertionError("doctor followed or hashed a WAL symlink")
        return original_hash(path)

    def guarded_open(path: Path, *args: object, **kwargs: object):
        if path == wal:
            raise AssertionError("doctor followed or opened a WAL symlink")
        return original_open(path, *args, **kwargs)

    monkeypatch.setattr(doctor, "sha256_file", guarded_hash)
    monkeypatch.setattr(Path, "open", guarded_open)

    check = doctor._dj_meta_database_check(cfg)

    assert check.status is doctor.CheckStatus.FAIL
    assert "WAL" in check.detail


def test_published_sidecar_stat_error_is_an_actionable_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cfg = _config(tmp_path)
    _write_index(cfg)
    manifest = json.loads(
        (cfg.index.active_dir / "index-manifest.json").read_text(encoding="utf-8")
    )
    blocked_sidecar = Path(f"{cfg.index.active_dir / manifest['tracks_file']}-shm")
    original_lstat = Path.lstat

    def guarded_lstat(path: Path, *args: object, **kwargs: object):
        if path == blocked_sidecar:
            raise PermissionError("published sidecar permission denied")
        return original_lstat(path, *args, **kwargs)

    monkeypatch.setattr(Path, "lstat", guarded_lstat)

    check = doctor._index_check(cfg)

    assert check.status is doctor.CheckStatus.FAIL
    assert "published sidecar permission denied" in check.detail


def test_published_sidecar_is_rejected_without_hashing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cfg = _config(tmp_path)
    _write_index(cfg)
    manifest = json.loads(
        (cfg.index.active_dir / "index-manifest.json").read_text(encoding="utf-8")
    )
    wal = Path(f"{cfg.index.active_dir / manifest['tracks_file']}-wal")
    wal.write_bytes(b"existing unpublished WAL")
    original_hash = doctor.sha256_file

    def guarded_hash(path: Path) -> str:
        if path == wal:
            raise AssertionError("doctor hashed a published-index WAL")
        return original_hash(path)

    monkeypatch.setattr(doctor, "sha256_file", guarded_hash)

    check = doctor._index_check(cfg)

    assert check.status is doctor.CheckStatus.FAIL
    assert "WAL" in check.detail


def test_published_tracks_wal_is_not_ignored_or_mutated(tmp_path: Path) -> None:
    cfg = _config(tmp_path)
    _write_index(cfg)
    manifest = json.loads(
        (cfg.index.active_dir / "index-manifest.json").read_text(encoding="utf-8")
    )
    db = cfg.index.active_dir / manifest["tracks_file"]
    with closing(sqlite3.connect(db)) as writer:
        assert writer.execute("PRAGMA journal_mode=WAL").fetchone()[0] == "wal"
        writer.execute("PRAGMA wal_autocheckpoint=0")
        writer.execute("DROP TABLE tracks")
        writer.execute("CREATE TABLE tracks (path TEXT PRIMARY KEY)")
        writer.commit()
        before = _tree_snapshot(tmp_path)

        check = doctor._tracks_database_check(cfg)

        assert check.status is doctor.CheckStatus.FAIL
        assert "WAL" in check.detail
        assert before == _tree_snapshot(tmp_path)


def test_unpublished_metadata_wal_never_passes_published_checks(tmp_path: Path) -> None:
    cfg = _config(tmp_path)
    _write_index(cfg)
    manifest = json.loads(
        (cfg.index.active_dir / "index-manifest.json").read_text(encoding="utf-8")
    )
    db = cfg.index.active_dir / manifest["tracks_file"]
    with closing(sqlite3.connect(db)) as writer:
        assert writer.execute("PRAGMA journal_mode=WAL").fetchone()[0] == "wal"
        writer.execute("PRAGMA wal_autocheckpoint=0")
        writer.execute("UPDATE tracks SET title = 'unpublished metadata'")
        writer.commit()
        assert Path(f"{db}-wal").stat().st_size > 0
        before = _tree_snapshot(tmp_path)

        index_check = doctor._index_check(cfg)
        tracks_check = doctor._tracks_database_check(cfg)

        assert index_check.status is doctor.CheckStatus.FAIL
        assert tracks_check.status is doctor.CheckStatus.FAIL
        assert "WAL" in index_check.detail
        assert "WAL" in tracks_check.detail
        assert before == _tree_snapshot(tmp_path)


def test_missing_ffmpeg_warns_with_alac_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(doctor, "_module_available", lambda _name: True)
    monkeypatch.setattr(doctor.shutil, "which", lambda _name: None)

    check = doctor._dependency_check()

    assert check.status is doctor.CheckStatus.WARN
    assert "ALAC" in check.detail
    assert "raw ALAC fallback" in check.detail


def test_invalid_bundle_stamp_fails(tmp_path: Path) -> None:
    bundle = tmp_path / "static_dist"
    bundle.mkdir()
    (bundle / "build-info.json").write_text("not json", encoding="utf-8")

    check = doctor._bundle_check(tmp_path)

    assert check.status is doctor.CheckStatus.FAIL
    assert "build-info.json" in check.summary


def test_network_rejects_unsafe_bind_and_accepts_loopback_alternates(tmp_path: Path) -> None:
    unsafe = _config(tmp_path / "unsafe", host="192.168.1.20")
    unsafe.server.access_token = None
    assert doctor._network_check(unsafe).status is doctor.CheckStatus.FAIL

    for host in ("127.0.0.2", "::1"):
        cfg = _config(tmp_path / host.replace(":", "_"), host=host)
        cfg.server.access_token = None
        assert doctor._network_check(cfg).status is doctor.CheckStatus.PASS


@pytest.mark.parametrize("version", [(3, 13), (3, 15)])
def test_wrong_python_versions_fail_with_exact_constraint(version: tuple[int, int]) -> None:
    check = doctor._python_check(version)
    assert check.status is doctor.CheckStatus.FAIL
    assert "==3.14.*" in check.detail


def test_render_text_has_one_summary_and_action_line_per_check() -> None:
    report = doctor.DoctorReport(
        (
            doctor.DoctorCheck(
                "index",
                doctor.CheckStatus.FAIL,
                "Index is corrupt.",
                "Action: run autodj index --force.",
            ),
        )
    )

    rendered = doctor.render_text(report)

    assert rendered.splitlines() == [
        "[FAIL] index: Index is corrupt. Action: run autodj index --force."
    ]


def test_doctor_cli_json_is_parseable_and_failure_exits_one(tmp_path: Path) -> None:
    cfg = _config(tmp_path)
    report = doctor.DoctorReport(
        (doctor.DoctorCheck("index", doctor.CheckStatus.FAIL, "broken", "Action: rebuild."),)
    )

    with (
        patch("autodj.cli._load_cfg_or_exit", return_value=cfg),
        patch("autodj.doctor.run_doctor", return_value=report),
    ):
        result = CliRunner().invoke(cli, ["doctor", "--json"])

    assert result.exit_code == 1
    assert json.loads(result.output) == {
        "checks": [
            {
                "detail": "Action: rebuild.",
                "name": "index",
                "status": "fail",
                "summary": "broken",
            }
        ],
        "exit_code": 1,
    }


def test_doctor_cli_json_uses_report_method(tmp_path: Path) -> None:
    cfg = _config(tmp_path)

    class JsonOnlyReport:
        exit_code = 0

        def to_json(self) -> str:
            return '{"source":"report.to_json"}'

    with (
        patch("autodj.cli._load_cfg_or_exit", return_value=cfg),
        patch("autodj.doctor.run_doctor", return_value=JsonOnlyReport()),
    ):
        result = CliRunner().invoke(cli, ["doctor", "--json"])

    assert result.exit_code == 0
    assert json.loads(result.output) == {"source": "report.to_json"}


def test_doctor_cli_json_invalid_config_is_structured_and_redacted() -> None:
    secret = "server-secret-from-invalid-config"
    with patch(
        "autodj.config.load_config",
        side_effect=ValueError(f"invalid access token {secret}"),
    ):
        result = CliRunner().invoke(cli, ["doctor", "--json"])

    assert result.exit_code == 1
    payload = json.loads(result.output)
    assert payload["exit_code"] == 1
    assert payload["checks"] == [
        {
            "name": "configuration",
            "status": "fail",
            "summary": "configuration invalid",
            "detail": "fix the configuration and retry",
        }
    ]
    assert secret not in result.output


def test_doctor_cli_json_config_permission_error_is_structured_and_redacted() -> None:
    secret = "permission-error-secret"
    with patch(
        "autodj.config.load_config",
        side_effect=PermissionError(secret),
    ):
        result = CliRunner().invoke(cli, ["doctor", "--json"])

    assert result.exit_code == 1
    payload = json.loads(result.output)
    assert payload["checks"][0]["name"] == "configuration"
    assert payload["checks"][0]["status"] == "fail"
    assert secret not in result.output


@pytest.mark.parametrize("fatal", [SystemExit(7), KeyboardInterrupt()])
def test_config_loader_does_not_catch_process_control_exceptions(fatal: BaseException) -> None:
    with (
        patch("autodj.config.load_config", side_effect=fatal),
        pytest.raises(type(fatal)),
    ):
        _load_cfg_or_exit(None, show_error=False)


def test_missing_writable_child_warns_when_parent_is_writable(tmp_path: Path) -> None:
    cfg = _config(tmp_path)
    cfg.index.index_dir = tmp_path / "new-index-parent" / "index"
    (tmp_path / "new-index-parent").mkdir()

    check = doctor._path_check(
        "index-path",
        cfg.index.index_dir,
        writable=True,
    )

    assert check.status is doctor.CheckStatus.WARN
    assert "can create it" in check.detail


def test_missing_index_and_model_paths_are_not_created(tmp_path: Path) -> None:
    cfg = _config(tmp_path)
    cfg.index.index_dir = tmp_path / "missing" / "indexes"
    cfg.index.model_dir = tmp_path / "missing" / "models"

    doctor.run_doctor(cfg, python_version=(3, 14))

    assert not cfg.index.index_dir.exists()
    assert not cfg.index.model_dir.exists()


def test_empty_active_index_directory_remains_byte_for_byte_empty(tmp_path: Path) -> None:
    cfg = _config(tmp_path)
    cfg.index.active_dir.mkdir()

    doctor.run_doctor(cfg, python_version=(3, 14))

    assert list(cfg.index.active_dir.iterdir()) == []


def test_missing_publication_lock_fails_without_recreating_it(tmp_path: Path) -> None:
    cfg = _config(tmp_path)
    _write_index(cfg)
    lock = cfg.index.active_dir / ".index-publication.lock"
    lock.unlink()

    check = doctor._index_check(cfg)

    assert check.status is doctor.CheckStatus.FAIL
    assert "lock" in check.detail.lower()
    assert not lock.exists()


def test_published_index_check_never_acquires_publication_lock(tmp_path: Path) -> None:
    cfg = _config(tmp_path)
    _write_index(cfg)

    with patch(
        "autodj.indexer.publication_lock",
        side_effect=AssertionError("doctor acquired publication lock"),
    ):
        check = doctor._index_check(cfg)

    assert check.status is doctor.CheckStatus.PASS


def test_tracks_db_uses_manifest_generation_file(tmp_path: Path) -> None:
    cfg = _config(tmp_path)
    _write_index(cfg)
    # The mutable working DB is not the security-owned published generation.
    (cfg.index.active_dir / "tracks.db").write_bytes(b"corrupt working copy")

    check = doctor._tracks_database_check(cfg)

    assert check.status is doctor.CheckStatus.PASS


def test_tombstoned_publication_is_logically_empty_not_stale_legacy(tmp_path: Path) -> None:
    cfg = _config(tmp_path)
    _write_index(cfg)
    tombstone_publication(cfg.index.active_dir)

    assert doctor._index_check(cfg).status is doctor.CheckStatus.WARN
    assert doctor._tracks_database_check(cfg).status is doctor.CheckStatus.WARN


def test_manifest_free_artifacts_with_publication_history_are_not_legacy(tmp_path: Path) -> None:
    cfg = _config(tmp_path)
    _write_index(cfg)
    (cfg.index.active_dir / "index-manifest.json").unlink()
    for pattern in ("tracks.g*.db", "vectors.g*.index"):
        for generated in cfg.index.active_dir.glob(pattern):
            generated.unlink()

    assert doctor._index_check(cfg).status is doctor.CheckStatus.FAIL
    assert doctor._tracks_database_check(cfg).status is doctor.CheckStatus.FAIL


def test_explicit_insecure_lan_is_warning(tmp_path: Path) -> None:
    cfg = _config(tmp_path, host="192.168.1.21")
    cfg.server.access_token = None
    cfg.server.insecure_lan = True

    assert doctor._network_check(cfg).status is doctor.CheckStatus.WARN


def test_bundle_version_mismatch_fails(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    bundle = tmp_path / "static_dist"
    bundle.mkdir()
    for name in doctor.REQUIRED_BUILT_ASSETS:
        (bundle / name).write_text("asset", encoding="utf-8")
    (bundle / "build-info.json").write_text('{"version":"0.1.0"}', encoding="utf-8")
    monkeypatch.setattr(doctor, "current_version", lambda: "9.9.9")

    check = doctor._bundle_check(tmp_path)

    assert check.status is doctor.CheckStatus.FAIL
    assert "0.1.0" in check.summary
    assert "9.9.9" in check.summary


def test_run_doctor_check_order(tmp_path: Path) -> None:
    cfg = _config(tmp_path)

    report = doctor.run_doctor(cfg, python_version=(3, 14))

    assert [check.name for check in report.checks] == [
        "configuration",
        "python",
        "music-path",
        "index-path",
        "model-path",
        "index-coherence",
        "tracks-db",
        "dj-meta-db",
        "dependencies",
        "model-cache",
        "network-safety",
        "frontend-bundle",
    ]


def test_unreadable_or_non_directory_paths_fail(tmp_path: Path) -> None:
    cfg = _config(tmp_path)
    cfg.library.music_dir = tmp_path / "missing-music"
    cfg.index.index_dir = tmp_path / "index-file"
    cfg.index.index_dir.write_text("not a directory", encoding="utf-8")

    music_check = doctor._path_check(
        "music-path",
        cfg.library.music_dir,
        writable=False,
    )
    index_check = doctor._path_check(
        "index-path",
        cfg.index.index_dir,
        writable=True,
    )

    assert music_check.status is doctor.CheckStatus.FAIL
    assert index_check.status is doctor.CheckStatus.FAIL
    assert "does not exist" in music_check.detail
    assert "permissions" in index_check.detail


def test_path_without_existing_parent_fails_safely() -> None:

    class RootlessPath:
        @property
        def parent(self):
            return self

        def exists(self) -> bool:
            return False

    assert doctor._nearest_existing_parent(RootlessPath()) is None


def test_missing_path_with_unwritable_parent_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "missing"
    monkeypatch.setattr(doctor.os, "access", lambda *_args: False)

    check = doctor._path_check("index-path", path, writable=True)

    assert check.status is doctor.CheckStatus.FAIL


def test_index_path_that_is_a_file_fails(tmp_path: Path) -> None:
    cfg = _config(tmp_path)
    cfg.index.active_dir.write_text("not a directory", encoding="utf-8")

    assert doctor._index_check(cfg).status is doctor.CheckStatus.FAIL


def test_generation_artifacts_without_manifest_fail(tmp_path: Path) -> None:
    cfg = _config(tmp_path)
    cfg.index.active_dir.mkdir()
    (cfg.index.active_dir / "tracks.g00000000000000000001.db").write_bytes(b"stale")

    check = doctor._index_check(cfg)

    assert check.status is doctor.CheckStatus.FAIL
    assert "manifest" in check.detail


@pytest.mark.parametrize("count", [0, 1])
def test_coherent_legacy_index_is_inspected_read_only(tmp_path: Path, count: int) -> None:
    cfg = _config(tmp_path)
    cfg.index.active_dir.mkdir()
    if count:
        _write_index(cfg)
    else:
        save_index([], np.empty((0, FEATURE_DIM), dtype=np.float32), cfg.index.active_dir)
    for name in ("index-manifest.json", ".index-publication-state.json"):
        (cfg.index.active_dir / name).unlink(missing_ok=True)
    for pattern in ("tracks.g*.db", "vectors.g*.index"):
        for generated in cfg.index.active_dir.glob(pattern):
            generated.unlink()

    check = doctor._index_check(cfg)

    assert check.status is doctor.CheckStatus.WARN


def test_coherent_legacy_index_does_not_create_sqlite_sidecars(tmp_path: Path) -> None:
    cfg = _config(tmp_path)
    _write_index(cfg)
    for name in ("index-manifest.json", ".index-publication-state.json"):
        (cfg.index.active_dir / name).unlink(missing_ok=True)
    for pattern in ("tracks.g*.db", "vectors.g*.index"):
        for generated in cfg.index.active_dir.glob(pattern):
            generated.unlink()
    before = _tree_snapshot(tmp_path)

    check = doctor._index_check(cfg)

    assert check.status is doctor.CheckStatus.WARN
    assert before == _tree_snapshot(tmp_path)


def test_legacy_entry_vector_count_mismatch_fails(tmp_path: Path) -> None:
    import faiss

    cfg = _config(tmp_path)
    _write_index(cfg)
    for name in ("index-manifest.json", ".index-publication-state.json"):
        (cfg.index.active_dir / name).unlink(missing_ok=True)
    for pattern in ("tracks.g*.db", "vectors.g*.index"):
        for generated in cfg.index.active_dir.glob(pattern):
            generated.unlink()
    faiss.write_index(faiss.IndexFlatIP(FEATURE_DIM), str(cfg.index.active_dir / "vectors.index"))

    check = doctor._index_check(cfg)

    assert check.status is doctor.CheckStatus.FAIL
    assert "tracks=1, vectors=0" in check.detail


def test_readonly_sqlite_closes_connection_when_query_only_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:

    class BrokenConnection:
        closed = False

        def execute(self, _query: str):
            raise RuntimeError("query-only unavailable")

        def close(self) -> None:
            self.closed = True

    connection = BrokenConnection()
    monkeypatch.setattr(doctor.sqlite3, "connect", lambda *_args, **_kwargs: connection)

    with pytest.raises(RuntimeError, match="query-only unavailable"):
        doctor._open_readonly_sqlite(Path("unused.db"))
    assert connection.closed


def test_dj_meta_schema_mismatch_fails(tmp_path: Path) -> None:
    cfg = _config(tmp_path)
    cfg.index.active_dir.mkdir()
    db = cfg.index.active_dir / "dj_meta.db"
    with closing(sqlite3.connect(db)) as conn:
        conn.execute("CREATE TABLE dj_meta (path TEXT PRIMARY KEY)")
        conn.commit()

    check = doctor._dj_meta_database_check(cfg)

    assert check.status is doctor.CheckStatus.FAIL
    assert "missing required columns" in check.detail


@pytest.mark.parametrize(
    ("original", "replacement"),
    [
        ("vec_row INTEGER NOT NULL UNIQUE", "vec_row TEXT NOT NULL UNIQUE"),
        ("title TEXT NOT NULL DEFAULT ''", "title TEXT DEFAULT ''"),
        ("path TEXT NOT NULL UNIQUE", "path TEXT NOT NULL"),
    ],
    ids=["wrong-type", "nullable", "missing-unique"],
)
def test_tracks_db_rejects_runtime_schema_contract_drift(
    tmp_path: Path,
    original: str,
    replacement: str,
) -> None:
    cfg = _config(tmp_path)
    cfg.index.active_dir.mkdir()
    schema = """CREATE TABLE tracks (
        vec_row INTEGER NOT NULL UNIQUE,
        path TEXT NOT NULL UNIQUE,
        title TEXT NOT NULL DEFAULT '', artist TEXT NOT NULL DEFAULT '',
        album TEXT NOT NULL DEFAULT '', genre TEXT NOT NULL DEFAULT '',
        bpm REAL NOT NULL DEFAULT 0, year INTEGER NOT NULL DEFAULT 0,
        length REAL NOT NULL DEFAULT 0, energy REAL NOT NULL DEFAULT 0,
        key INTEGER NOT NULL DEFAULT -1, mode INTEGER NOT NULL DEFAULT -1,
        tempo_confidence REAL NOT NULL DEFAULT 0,
        embedded_at REAL NOT NULL DEFAULT 0
    )""".replace(original, replacement)
    with closing(sqlite3.connect(cfg.index.active_dir / "tracks.db")) as conn:
        conn.execute(schema)
        conn.commit()

    check = doctor._tracks_database_check(cfg)

    assert check.status is doctor.CheckStatus.FAIL
    assert "schema" in check.summary


@pytest.mark.parametrize(
    ("original", "replacement"),
    [
        ("path TEXT PRIMARY KEY", "path INTEGER PRIMARY KEY"),
        ("path TEXT PRIMARY KEY", "path TEXT"),
        ("intro_end_s REAL NOT NULL DEFAULT 0", "intro_end_s REAL DEFAULT 0"),
    ],
    ids=["wrong-type", "missing-primary-key", "nullable"],
)
def test_dj_meta_db_rejects_runtime_schema_contract_drift(
    tmp_path: Path,
    original: str,
    replacement: str,
) -> None:
    cfg = _config(tmp_path)
    cfg.index.active_dir.mkdir()
    schema = """CREATE TABLE dj_meta (
        path TEXT PRIMARY KEY,
        intro_end_s REAL NOT NULL DEFAULT 0,
        outro_start_s REAL NOT NULL DEFAULT 0,
        analysed INTEGER NOT NULL DEFAULT 0,
        beats TEXT,
        cues TEXT
    )""".replace(original, replacement)
    with closing(sqlite3.connect(cfg.index.active_dir / "dj_meta.db")) as conn:
        conn.execute(schema)
        conn.commit()

    check = doctor._dj_meta_database_check(cfg)

    assert check.status is doctor.CheckStatus.FAIL
    assert "schema" in check.summary


def test_tracks_db_rejects_invalid_manifest_before_opening_db(tmp_path: Path) -> None:
    cfg = _config(tmp_path)
    cfg.index.active_dir.mkdir()
    (cfg.index.active_dir / "index-manifest.json").write_text("not json", encoding="utf-8")

    check = doctor._tracks_database_check(cfg)

    assert check.status is doctor.CheckStatus.FAIL
    assert "invalid index manifest" in check.detail


def test_module_probe_handles_normal_and_invalid_specs(monkeypatch: pytest.MonkeyPatch) -> None:
    probe = "autodj_test_probe_dependency"
    monkeypatch.delitem(doctor.sys.modules, probe, raising=False)
    monkeypatch.setattr(doctor.importlib.util, "find_spec", lambda _name: object())
    assert doctor._module_available(probe)

    def invalid_spec(_name: str):
        raise ValueError("invalid spec")

    monkeypatch.setattr(doctor.importlib.util, "find_spec", invalid_spec)
    assert not doctor._module_available(probe)


def test_missing_audio_modules_warn(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(doctor, "_module_available", lambda _name: False)
    monkeypatch.setattr(doctor.shutil, "which", lambda _name: "ffmpeg")

    check = doctor._dependency_check()

    assert check.status is doctor.CheckStatus.WARN
    assert "soundfile" in check.summary
    assert "play or all extra" in check.detail


def test_complete_model_cache_passes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = _config(tmp_path)
    status = SimpleNamespace(path=tmp_path / "model", complete=True, reason="complete")
    monkeypatch.setattr(doctor, "inspect_model_cache", lambda *_args: status)

    assert doctor._model_cache_check(cfg).status is doctor.CheckStatus.PASS


@pytest.mark.parametrize("error_type", [PermissionError, ImportError, RuntimeError])
def test_model_cache_inspection_errors_fail_safely_and_redact(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    error_type: type[Exception],
) -> None:
    cfg = _config(tmp_path)
    secret = "model-error-secret"

    def fail_inspection(*_args):
        raise error_type(secret)

    monkeypatch.setattr(doctor, "inspect_model_cache", fail_inspection)

    check = doctor._model_cache_check(cfg)
    payload = doctor.DoctorReport((check,)).to_json()

    assert check.status is doctor.CheckStatus.FAIL
    assert "inspect" in check.detail
    assert secret not in payload


def test_model_cache_check_does_not_catch_system_exit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cfg = _config(tmp_path)

    def stop(*_args):
        raise SystemExit(2)

    monkeypatch.setattr(doctor, "inspect_model_cache", stop)

    with pytest.raises(SystemExit, match="2"):
        doctor._model_cache_check(cfg)


def test_non_loopback_token_authentication_passes(tmp_path: Path) -> None:
    cfg = _config(tmp_path, host="192.168.1.30")

    assert doctor._network_check(cfg).status is doctor.CheckStatus.PASS


@pytest.mark.parametrize("stamp", [None, '{"version":""}'])
def test_missing_or_invalid_bundle_metadata_is_reported(tmp_path: Path, stamp: str | None) -> None:
    bundle = tmp_path / "static_dist"
    bundle.mkdir()
    if stamp is not None:
        (bundle / "build-info.json").write_text(stamp, encoding="utf-8")

    check = doctor._bundle_check(tmp_path)

    expected = doctor.CheckStatus.WARN if stamp is None else doctor.CheckStatus.FAIL
    assert check.status is expected


def test_incomplete_built_assets_warn_even_with_valid_stamp(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bundle = tmp_path / "static_dist"
    bundle.mkdir()
    (bundle / "build-info.json").write_text('{"version":"1.2.3"}', encoding="utf-8")
    monkeypatch.setattr(doctor, "current_version", lambda: "1.2.3")

    check = doctor._bundle_check(tmp_path)

    assert check.status is doctor.CheckStatus.WARN
    assert "incomplete built assets" in check.detail


def test_bundle_version_inspection_errors_fail_safely_and_redact(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bundle = tmp_path / "static_dist"
    bundle.mkdir()
    for name in doctor.REQUIRED_BUILT_ASSETS:
        (bundle / name).write_text("asset", encoding="utf-8")
    (bundle / "build-info.json").write_text('{"version":"1.2.3"}', encoding="utf-8")
    secret = "version-error-secret"

    def fail_version() -> str:
        raise RuntimeError(secret)

    monkeypatch.setattr(doctor, "current_version", fail_version)

    check = doctor._bundle_check(tmp_path)
    payload = doctor.DoctorReport((check,)).to_json()

    assert check.status is doctor.CheckStatus.FAIL
    assert "version" in check.detail
    assert secret not in payload


def test_bundle_check_does_not_catch_system_exit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bundle = tmp_path / "static_dist"
    bundle.mkdir()
    (bundle / "build-info.json").write_text('{"version":"1.2.3"}', encoding="utf-8")

    def stop() -> str:
        raise SystemExit(2)

    monkeypatch.setattr(doctor, "current_version", stop)

    with pytest.raises(SystemExit, match="2"):
        doctor._bundle_check(tmp_path)


def test_literal_planned_api_skeleton_has_failure_exit() -> None:
    assert all(callable(item) for item in (_bundle_check, _dependency_check, _python_check))
    assert callable(render_text)
    assert callable(run_doctor)
    report = DoctorReport(
        (
            DoctorCheck(
                "index-coherence",
                CheckStatus.FAIL,
                "unreadable published generation",
                "run `autodj index` to republish the index",
            ),
        )
    )
    assert report.exit_code == 1


def test_check_status_values_are_stable_lowercase() -> None:

    assert [status.value for status in doctor.CheckStatus] == ["pass", "warn", "fail"]


def test_report_structured_serialization_is_stable() -> None:
    report = doctor.DoctorReport(
        (doctor.DoctorCheck("python", doctor.CheckStatus.FAIL, "3.15", "==3.14.*"),)
    )
    expected = {
        "exit_code": 1,
        "checks": [{"name": "python", "status": "fail", "summary": "3.15", "detail": "==3.14.*"}],
    }

    assert report.to_dict() == expected
    assert json.loads(report.to_json()) == expected


def test_configuration_detail_is_structured_and_redacted(tmp_path: Path) -> None:
    cfg = _config(tmp_path)

    check = doctor._configuration_check(cfg)

    assert check.name == "configuration"
    assert check.detail == {
        "sources": list(cfg.config_sources),
        "host": cfg.server.host,
        "port": cfg.server.port,
        "music_dir": str(cfg.library.music_dir),
        "index_dir": str(cfg.index.index_dir),
        "model_dir": str(cfg.index.model_dir),
        "access_token": "<redacted>",
        "huggingface_token": "<redacted>",
    }
    assert cfg.server.access_token not in check.summary
    assert cfg.huggingface.token not in check.summary


def test_run_doctor_uses_planned_stable_identifiers(tmp_path: Path) -> None:
    cfg = _config(tmp_path)

    report = doctor.run_doctor(cfg, python_version=(3, 14))

    assert [check.name for check in report.checks] == [
        "configuration",
        "python",
        "music-path",
        "index-path",
        "model-path",
        "index-coherence",
        "tracks-db",
        "dj-meta-db",
        "dependencies",
        "model-cache",
        "network-safety",
        "frontend-bundle",
    ]


def test_missing_rederivable_databases_warn_explicitly(tmp_path: Path) -> None:
    cfg = _config(tmp_path)

    tracks = doctor._tracks_database_check(cfg)
    dj_meta = doctor._dj_meta_database_check(cfg)

    assert tracks.status is doctor.CheckStatus.WARN
    assert dj_meta.status is doctor.CheckStatus.WARN
    assert tracks.summary == "database absent"
    assert dj_meta.summary == "database absent"
