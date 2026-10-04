"""Read-only diagnostics for the ``autodj doctor`` command."""

from __future__ import annotations

import gc
import json
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
    _dependency_check,
    _python_check,
    render_text,
    run_doctor,
)
from autodj.index_manifest import read_manifest, sha256_file
from autodj.indexer import FEATURE_DIM, IndexEntry, build_faiss_index, save_index


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
    save_index(
        [entry], vector, cfg.index.active_dir, music_dir=cfg.library.music_dir, base_generation=0
    )


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
            # The index check takes the index lock, as serve does, which
            # touches the lock file and its folder.
            None if path.is_dir() else path.stat().st_mtime_ns,
            None if path.is_dir() else sha256_file(path),
        )
        for path in paths
        if path.exists() and path.name != ".index-publication.lock"
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


def test_import_and_empty_index_check_do_not_import_heavy_runtimes(tmp_path: Path) -> None:
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
        cwd=tmp_path,
    )

    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize("remaining", ["tracks.db", "vectors.index"])
def test_index_without_manifest_fails_actionably(tmp_path: Path, remaining: str) -> None:
    cfg = _config(tmp_path)
    cfg.index.active_dir.mkdir()
    (cfg.index.active_dir / remaining).write_bytes(b"partial")

    check = doctor._index_check(cfg)

    assert check.status is doctor.CheckStatus.FAIL
    assert "autodj index --force" in check.detail
    assert check.repair == "rebuild-index"


def test_corrupt_published_index_fails(tmp_path: Path) -> None:
    cfg = _config(tmp_path)
    _write_index(cfg)
    manifest = read_manifest(cfg.index.active_dir)
    assert manifest is not None
    (cfg.index.active_dir / manifest.vectors_file).write_bytes(b"corrupt")

    check = doctor._index_check(cfg)

    assert check.status is doctor.CheckStatus.FAIL
    assert "autodj index" in check.detail
    assert check.repair == "rebuild-index"


def test_corrupt_dj_meta_fails_without_touching_file(tmp_path: Path) -> None:
    cfg = _config(tmp_path)
    cfg.index.active_dir.mkdir()
    db = cfg.index.active_dir / "dj_meta.db"
    db.write_bytes(b"not sqlite")
    before = db.stat().st_mtime_ns

    check = doctor._dj_meta_database_check(cfg)

    assert check.status is doctor.CheckStatus.FAIL
    assert "rebuild" in check.detail.lower()
    assert check.repair == "rebuild-dj-meta"
    assert db.stat().st_mtime_ns == before


def test_published_absolute_track_path_fails_like_serve(tmp_path: Path) -> None:
    """Serve refuses an index with absolute paths, so doctor reports the same error."""
    from autodj.index_manifest import publish_generation
    from autodj.indexer import _entry_to_row, _write_faiss_chunked, _write_tracks_file

    cfg = _config(tmp_path)
    entry = IndexEntry("song.flac", "Song", "", "", "", 0, 0, 0, 0, -1, -1, 0)
    vector = np.ones((1, FEATURE_DIM), dtype=np.float32)

    def write(tracks: Path, vectors: Path) -> None:
        row = _entry_to_row(entry, None, 0) | {"path": "C:/Music/song.flac"}
        _write_tracks_file([row], tracks)
        _write_faiss_chunked(build_faiss_index(vector), vectors)

    publish_generation(cfg.index.active_dir, base_generation=0, vector_count=1, write_files=write)

    check = doctor._index_check(cfg)

    assert check.status is doctor.CheckStatus.FAIL
    assert check.summary == "UnsupportedIndexError"
    assert "absolute path C:/Music/song.flac" in check.detail
    assert "autodj index --force" in check.detail


def test_dj_meta_with_absolute_key_fails_like_serve(tmp_path: Path) -> None:
    """A dj_meta.db keyed by absolute paths is refused at load, so doctor must fail it."""
    cfg = _config(tmp_path)
    cfg.index.active_dir.mkdir()
    db = cfg.index.active_dir / "dj_meta.db"
    _write_dj_meta(db)
    with closing(sqlite3.connect(db)) as conn:
        conn.execute("INSERT INTO dj_meta (path) VALUES ('\\\\nas\\music\\song.flac')")
        conn.commit()

    check = doctor._dj_meta_database_check(cfg)

    assert check.status is doctor.CheckStatus.FAIL
    assert check.summary == "old DJ metadata cache"
    assert "Delete it and run `autodj analyse`" in check.detail
    assert check.repair == "rebuild-dj-meta"


def test_unknown_index_exception_does_not_select_repair(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = _config(tmp_path)
    cfg.index.active_dir.mkdir()

    def fail(*_args, **_kwargs):
        raise RuntimeError("untrusted text says autodj index --force")

    monkeypatch.setattr("autodj.similarity.SimilarityIndex.from_index_dir", fail)

    check = doctor._index_check(cfg)

    assert check.status is doctor.CheckStatus.FAIL
    assert check.repair is None


def test_sqlite_checks_close_read_only_connections(tmp_path: Path) -> None:
    cfg = _config(tmp_path)
    cfg.index.active_dir.mkdir()
    _write_dj_meta(cfg.index.active_dir / "dj_meta.db")

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always", ResourceWarning)
        doctor._dj_meta_database_check(cfg)
        gc.collect()

    assert not [warning for warning in caught if "unclosed database" in str(warning.message)]


def test_dj_meta_failed_integrity_check_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = _config(tmp_path)
    cfg.index.active_dir.mkdir()
    _write_dj_meta(cfg.index.active_dir / "dj_meta.db")
    real_connect = sqlite3.connect

    class FailedIntegrityConnection:
        def __init__(self, *args: object, **kwargs: object) -> None:
            self._conn = real_connect(*args, **kwargs)

        def execute(self, query: str):
            if query == "PRAGMA integrity_check":
                return [("corrupt page",)]
            return self._conn.execute(query)

        def close(self) -> None:
            self._conn.close()

    monkeypatch.setattr(doctor.sqlite3, "connect", FailedIntegrityConnection)

    check = doctor._dj_meta_database_check(cfg)

    assert check.status is doctor.CheckStatus.FAIL
    assert "integrity_check: corrupt page" in check.detail


def test_missing_ffmpeg_warns_with_alac_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(doctor, "_module_available", lambda _name: True)
    monkeypatch.setattr(doctor.shutil, "which", lambda _name: None)

    check = doctor._dependency_check()

    assert check.status is doctor.CheckStatus.WARN
    assert "raw ALAC fallback" in check.detail
    # The indexer decodes these through FFmpeg and skips them without it.
    assert ".m4a, .mp4 and .aac files, which are skipped" in check.detail


def test_missing_beets_db_warns_and_unset_passes(tmp_path: Path) -> None:
    cfg = _config(tmp_path)
    assert doctor._beets_db_check(cfg).status is doctor.CheckStatus.PASS

    cfg.library.beets_db = tmp_path / "missing" / "library.db"
    check = doctor._beets_db_check(cfg)
    assert check.status is doctor.CheckStatus.WARN
    assert "enrich fails" in check.detail

    cfg.library.beets_db = tmp_path / "library.db"
    cfg.library.beets_db.write_bytes(b"")
    assert doctor._beets_db_check(cfg).status is doctor.CheckStatus.PASS


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


def test_explicit_insecure_lan_is_warning(tmp_path: Path) -> None:
    cfg = _config(tmp_path, host="192.168.1.21")
    cfg.server.access_token = None
    cfg.server.insecure_lan = True

    assert doctor._network_check(cfg).status is doctor.CheckStatus.WARN


def test_run_doctor_check_order(tmp_path: Path) -> None:
    cfg = _config(tmp_path)

    report = doctor.run_doctor(cfg, python_version=(3, 14))

    assert [check.name for check in report.checks] == [
        "configuration",
        "python",
        "music-path",
        "beets-db",
        "index-path",
        "model-path",
        "index",
        "dj-meta-db",
        "dependencies",
        "model-cache",
        "network-safety",
        "stream",
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


def test_literal_planned_api_skeleton_has_failure_exit() -> None:
    assert all(callable(item) for item in (_dependency_check, _python_check))
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


def test_repair_ids_are_structured_and_omitted_when_absent(tmp_path: Path) -> None:
    cfg = _config(tmp_path)
    missing_index = doctor._index_check(cfg)
    absent_meta = doctor._dj_meta_database_check(cfg)
    ordinary = doctor.DoctorCheck("ordinary", doctor.CheckStatus.PASS, "ok")

    report = doctor.DoctorReport((missing_index, absent_meta, ordinary))
    checks = report.to_dict()["checks"]

    assert missing_index.repair == "index"
    assert absent_meta.repair == "analyse"
    assert checks[0]["repair"] == "index"
    assert checks[1]["repair"] == "analyse"
    assert "repair" not in checks[2]


def test_configuration_detail_is_structured_and_redacted(tmp_path: Path) -> None:
    cfg = _config(tmp_path)

    check = doctor._configuration_check(cfg)

    assert check.name == "configuration"
    assert check.detail == {
        "sources": list(cfg.config_sources),
        "host": cfg.server.host,
        "port": cfg.server.port,
        "lan": False,
        "music_dir": str(cfg.library.music_dir),
        "index_dir": str(cfg.index.index_dir),
        "model_dir": str(cfg.index.model_dir),
        "access_token": "<redacted>",
        "huggingface_token": "<redacted>",
    }
    assert cfg.server.access_token not in check.summary
    assert cfg.huggingface.token not in check.summary


def test_missing_index_and_dj_meta_warn_explicitly(tmp_path: Path) -> None:
    cfg = _config(tmp_path)

    index = doctor._index_check(cfg)
    dj_meta = doctor._dj_meta_database_check(cfg)

    assert index.status is doctor.CheckStatus.WARN
    assert dj_meta.status is doctor.CheckStatus.WARN
    assert index.summary == "no index"
    assert dj_meta.summary == "database absent"


def test_stream_check_passes_when_disabled(tmp_path: Path) -> None:
    cfg = _config(tmp_path)

    assert doctor._stream_check(cfg).status is doctor.CheckStatus.PASS


def test_stream_check_fails_without_ffmpeg(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = _config(tmp_path)
    cfg.stream.enabled = True
    monkeypatch.setattr(doctor.shutil, "which", lambda _n: None)

    result = doctor._stream_check(cfg)

    assert result.status is doctor.CheckStatus.FAIL
    assert "ffmpeg" in result.summary.lower()


def test_stream_check_warns_on_loopback_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from dataclasses import replace

    cfg = _config(tmp_path)
    cfg.stream.enabled = True
    monkeypatch.setattr(doctor.shutil, "which", lambda _n: "/usr/bin/ffmpeg")
    cfg.server = replace(cfg.server, host="127.0.0.1")

    check = doctor._stream_check(cfg)
    assert check.status is doctor.CheckStatus.WARN
    assert check.repair == "serve-lan"


def test_stream_check_passes_enabled_with_ffmpeg_and_non_loopback_bind(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = _config(tmp_path, host="192.168.1.30")
    cfg.stream.enabled = True
    monkeypatch.setattr(doctor.shutil, "which", lambda _n: "/usr/bin/ffmpeg")

    result = doctor._stream_check(cfg)

    assert result.status is doctor.CheckStatus.PASS
    assert str(cfg.stream.bitrate) in result.summary


# ---------------------------------------------------------------------------
# LAN mode
# ---------------------------------------------------------------------------

_LAN_HOSTS = ["127.0.0.1", "192.168.1.20", "::1", "localhost", "nas"]


def _lan_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, **server: object) -> AutoDJConfig:
    from dataclasses import replace

    monkeypatch.setattr("autodj.lan.detect_lan_hosts", lambda: list(_LAN_HOSTS))
    cfg = _config(tmp_path, host="127.0.0.1")
    cfg.server = replace(cfg.server, lan=True, **server)  # type: ignore[arg-type]
    return cfg


def test_lan_network_check_reports_hosts_and_token_to_create(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = _lan_config(tmp_path, monkeypatch, access_token=None)
    before = _tree_snapshot(tmp_path)

    check = doctor._network_check(cfg)

    assert check.status is doctor.CheckStatus.PASS
    assert check.summary == "LAN mode"
    assert isinstance(check.detail, dict)
    assert check.detail["bind"] == "0.0.0.0:8080"
    assert check.detail["detected_hosts"] == _LAN_HOSTS
    assert "created on first start" in check.detail["access_token"]
    assert before == _tree_snapshot(tmp_path)  # doctor never creates the token


def test_lan_network_check_reports_saved_token_without_revealing_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from autodj.lan import load_or_create_access_token

    cfg = _lan_config(tmp_path, monkeypatch, access_token=None)
    token = load_or_create_access_token(cfg.index.index_dir / ".access-token")

    check = doctor._network_check(cfg)
    text = doctor.render_text(doctor.DoctorReport((check,)))

    assert check.status is doctor.CheckStatus.PASS
    assert isinstance(check.detail, dict)
    assert check.detail["access_token"].startswith("saved in ")
    assert token not in text


def test_lan_network_check_with_configured_token(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = _lan_config(tmp_path, monkeypatch, host="192.168.1.20")

    check = doctor._network_check(cfg)

    assert check.status is doctor.CheckStatus.PASS
    assert isinstance(check.detail, dict)
    assert check.detail["bind"] == "192.168.1.20:8080"
    assert check.detail["access_token"] == "configured"
    assert "s" * 32 not in doctor.render_text(doctor.DoctorReport((check,)))


def test_lan_network_check_warns_for_insecure_lan(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = _lan_config(tmp_path, monkeypatch, access_token=None, insecure_lan=True)

    check = doctor._network_check(cfg)

    assert check.status is doctor.CheckStatus.WARN
    assert "without pairing" in check.summary
    assert isinstance(check.detail, dict)
    assert check.detail["access_token"] is None


def test_lan_network_check_fails_for_unreadable_token(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = _lan_config(tmp_path, monkeypatch, access_token=None)
    (cfg.index.index_dir / ".access-token").mkdir()

    check = doctor._network_check(cfg)

    assert check.status is doctor.CheckStatus.FAIL
    assert isinstance(check.detail, dict)
    assert "cannot read access token" in check.detail["access_token"]


def test_stream_check_accepts_loopback_config_in_lan_mode(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = _lan_config(tmp_path, monkeypatch)
    cfg.stream.enabled = True
    monkeypatch.setattr(doctor.shutil, "which", lambda _n: "/usr/bin/ffmpeg")

    assert doctor._stream_check(cfg).status is doctor.CheckStatus.PASS


def test_configuration_check_shows_lan_setting(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    check = doctor._configuration_check(_lan_config(tmp_path, monkeypatch))

    assert isinstance(check.detail, dict)
    assert check.detail["lan"] is True


def test_published_empty_index_warns_instead_of_passing(tmp_path: Path) -> None:
    cfg = _config(tmp_path)
    save_index(
        [], np.zeros((0, FEATURE_DIM), dtype=np.float32), cfg.index.active_dir, base_generation=0
    )

    check = doctor._index_check(cfg)

    assert check.status is doctor.CheckStatus.WARN
    assert check.summary == "generation 1: 0 tracks"
    assert "empty" in check.detail


def test_locked_metadata_is_not_offered_a_destructive_rebuild(tmp_path: Path) -> None:
    cfg = _config(tmp_path)
    cfg.index.active_dir.mkdir()
    (cfg.index.active_dir / "dj_meta.db").write_bytes(b"cache")
    error = sqlite3.OperationalError("database is locked")
    error.sqlite_errorcode = sqlite3.SQLITE_BUSY
    with patch("autodj.doctor.sqlite3.connect", side_effect=error):
        check = doctor._dj_meta_database_check(cfg)
    assert check.status is CheckStatus.FAIL
    assert check.repair is None
    assert "permissions" in check.detail
