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
from autodj.catalog import LibraryCatalog
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
from autodj.index_manifest import sha256_file
from autodj.indexer import FEATURE_DIM, IndexEntry


def _config(tmp_path: Path, *, host: str = "127.0.0.2") -> AutoDJConfig:
    music = tmp_path / "music"
    index = tmp_path / "index"
    models = tmp_path / "models"
    music.mkdir(parents=True, exist_ok=True)
    index.mkdir(parents=True, exist_ok=True)
    models.mkdir(parents=True, exist_ok=True)
    return AutoDJConfig(
        library=LibraryConfig(music, None, ["flac"]),
        index=IndexConfig(index, models),
        playback=PlaybackConfig(),
        model=ModelConfig(),
        huggingface=HuggingFaceConfig("hf_secret_value"),
        config_path=tmp_path / "config.toml",
        server=ServerConfig(host=host, access_token="s" * 32),
        config_sources=("defaults", str(tmp_path / "config.toml")),
    )


def _write_index(cfg: AutoDJConfig) -> None:
    cfg.index.library_dir.mkdir(parents=True, exist_ok=True)
    cfg.index.shared_dir.mkdir(parents=True, exist_ok=True)
    (cfg.index.stations_dir / "default").mkdir(parents=True, exist_ok=True)
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
    with LibraryCatalog(cfg.index.library_dir / "library.db") as catalog:
        catalog.seed_sources(cfg.library.music_dir, (cfg.index.stations_dir / "default") / "liners")
        catalog.seed_entries([entry])
        catalog.seed_vectors([entry], vector)


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


def _tree_snapshot(root: Path) -> dict[Path, tuple[bool, int, int, str | None]]:
    paths = [root, *root.rglob("*")]
    return {
        path.relative_to(root): (
            path.is_dir(),
            # Transient SQLite WAL sidecars can also change directory size.
            None if path.is_dir() else path.stat().st_size,
            # Read-only SQLite connections may create transient WAL sidecars.
            None if path.is_dir() else path.stat().st_mtime_ns,
            None if path.is_dir() else sha256_file(path),
        )
        for path in paths
        if path.exists()
        and path.name != ".index-publication.lock"
        and not path.name.endswith(("-wal", "-shm"))
    }


def test_healthy_report_is_read_only_and_redacts_tokens(tmp_path: Path) -> None:
    cfg = _config(tmp_path)
    _write_index(cfg)
    _write_dj_meta(cfg.index.library_dir / "dj_meta.db")
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
    """ADR-0012-03."""
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
    cfg.index.library_dir.mkdir(parents=True, exist_ok=True)
    cfg.index.shared_dir.mkdir(parents=True, exist_ok=True)
    (cfg.index.stations_dir / "default").mkdir(parents=True, exist_ok=True)
    (cfg.index.library_dir / remaining).write_bytes(b"partial")

    check = doctor._index_check(cfg)

    assert check.status is doctor.CheckStatus.FAIL
    assert "autodj migrate-index --apply" in check.detail
    assert check.repair == "migrate-index"


def test_corrupt_catalog_fails_without_rebuild_offer(tmp_path: Path) -> None:
    cfg = _config(tmp_path)
    cfg.index.library_dir.mkdir(parents=True, exist_ok=True)
    cfg.index.shared_dir.mkdir(parents=True, exist_ok=True)
    (cfg.index.stations_dir / "default").mkdir(parents=True, exist_ok=True)
    (cfg.index.library_dir / "library.db").write_bytes(b"corrupt")
    check = doctor._index_check(cfg)
    assert check.status is doctor.CheckStatus.FAIL
    assert check.repair is None


@pytest.mark.parametrize("schema", [2, 3, 4])
def test_legacy_library_requires_explicit_migration(tmp_path: Path, schema: int) -> None:
    cfg = _config(tmp_path)
    cfg.index.library_dir.mkdir(parents=True, exist_ok=True)
    cfg.index.shared_dir.mkdir(parents=True, exist_ok=True)
    (cfg.index.stations_dir / "default").mkdir(parents=True, exist_ok=True)
    path = cfg.index.library_dir / "index-manifest.json"
    path.write_text(json.dumps({"schema_version": schema}))
    check = doctor._index_check(cfg)
    assert check.status is doctor.CheckStatus.FAIL
    assert check.repair == "migrate-index"
    assert "migrate-index --apply" in check.detail


def test_corrupt_dj_meta_fails_without_touching_file(tmp_path: Path) -> None:
    cfg = _config(tmp_path)
    cfg.index.library_dir.mkdir(parents=True, exist_ok=True)
    cfg.index.shared_dir.mkdir(parents=True, exist_ok=True)
    (cfg.index.stations_dir / "default").mkdir(parents=True, exist_ok=True)
    db = cfg.index.library_dir / "dj_meta.db"
    db.write_bytes(b"not sqlite")
    before = db.stat().st_mtime_ns

    check = doctor._dj_meta_database_check(cfg)

    assert check.status is doctor.CheckStatus.FAIL
    assert "rebuild" in check.detail.lower()
    assert check.repair == "rebuild-dj-meta"
    assert db.stat().st_mtime_ns == before


def test_dj_meta_with_absolute_key_is_valid(tmp_path: Path) -> None:
    """Absolute keys are the current canonical format and pass Doctor checks."""
    cfg = _config(tmp_path)
    cfg.index.library_dir.mkdir(parents=True, exist_ok=True)
    cfg.index.shared_dir.mkdir(parents=True, exist_ok=True)
    (cfg.index.stations_dir / "default").mkdir(parents=True, exist_ok=True)
    db = cfg.index.library_dir / "dj_meta.db"
    _write_dj_meta(db)
    with closing(sqlite3.connect(db)) as conn:
        conn.execute("INSERT INTO dj_meta (path) VALUES ('\\\\nas\\music\\song.flac')")
        conn.commit()

    check = doctor._dj_meta_database_check(cfg)

    assert check.status is doctor.CheckStatus.PASS
    assert check.summary == "integrity valid"
    assert check.repair is None


def test_music_path_check_uses_registered_roots_when_catalog_exists(tmp_path: Path) -> None:
    from autodj.catalog import LibraryCatalog

    cfg = _config(tmp_path)
    cfg.library.music_dir.rmdir()  # configured fallback is unused after sources were saved
    registered = tmp_path / "registered music"
    registered.mkdir()
    cfg.index.library_dir.mkdir(parents=True, exist_ok=True)
    cfg.index.shared_dir.mkdir(parents=True, exist_ok=True)
    (cfg.index.stations_dir / "default").mkdir(parents=True, exist_ok=True)
    with LibraryCatalog(cfg.index.library_dir / "library.db") as catalog:
        catalog.add_source(registered, kind="music")

    check = doctor._music_paths_check(cfg)

    assert check.status is CheckStatus.PASS
    assert [Path(path) for path in check.detail["registered"]] == [registered]


def test_music_path_check_reports_missing_registered_root(tmp_path: Path) -> None:
    from autodj.catalog import LibraryCatalog

    cfg = _config(tmp_path)
    registered = tmp_path / "registered music"
    registered.mkdir()
    cfg.index.library_dir.mkdir(parents=True, exist_ok=True)
    cfg.index.shared_dir.mkdir(parents=True, exist_ok=True)
    (cfg.index.stations_dir / "default").mkdir(parents=True, exist_ok=True)
    with LibraryCatalog(cfg.index.library_dir / "library.db") as catalog:
        catalog.add_source(registered, kind="music")
    registered.rmdir()

    check = doctor._music_paths_check(cfg)

    assert check.status is CheckStatus.FAIL
    assert check.summary == "registered music folders unavailable"
    assert [Path(path) for path in check.detail["unavailable"]] == [registered]


def test_sqlite_checks_close_read_only_connections(tmp_path: Path) -> None:
    cfg = _config(tmp_path)
    cfg.index.library_dir.mkdir(parents=True, exist_ok=True)
    cfg.index.shared_dir.mkdir(parents=True, exist_ok=True)
    (cfg.index.stations_dir / "default").mkdir(parents=True, exist_ok=True)
    _write_dj_meta(cfg.index.library_dir / "dj_meta.db")

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always", ResourceWarning)
        doctor._dj_meta_database_check(cfg)
        gc.collect()

    assert not [warning for warning in caught if "unclosed database" in str(warning.message)]


def test_dj_meta_failed_integrity_check_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = _config(tmp_path)
    cfg.index.library_dir.mkdir(parents=True, exist_ok=True)
    cfg.index.shared_dir.mkdir(parents=True, exist_ok=True)
    (cfg.index.stations_dir / "default").mkdir(parents=True, exist_ok=True)
    _write_dj_meta(cfg.index.library_dir / "dj_meta.db")
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

    monkeypatch.setattr(doctor, "_connect", FailedIntegrityConnection)

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
        "speech",
        "dmca",
        "python",
        "music-path",
        "beets-db",
        "index-path",
        "model-path",
        "index",
        "saved-settings",
        "dj-meta-db",
        "dependencies",
        "model-cache",
        "network-safety",
        "stream",
        "related-artists",
        "related-tracks",
        "time-checks",
        "categories",
        "config-keys",
        "recording",
        "liners",
    ]


def test_malformed_speech_state_is_reported_by_time_checks(tmp_path: Path) -> None:
    from autodj.stations import StationPaths

    cfg = _config(tmp_path)
    cfg.time_checks.enabled = True
    root = StationPaths.default(cfg).speech
    root.mkdir(parents=True)
    (root / "speech.json").write_text("{broken")
    assert doctor._speech_check(cfg).status is doctor.CheckStatus.FAIL
    assert doctor._time_checks_check(cfg).status is doctor.CheckStatus.FAIL


@pytest.mark.parametrize(
    "content,status",
    [
        (None, CheckStatus.PASS),
        ("{bad", CheckStatus.WARN),
        ('{"schema_version": 99}', CheckStatus.WARN),
        ('{"schema_version": 99, "playback": {"no_repeat_minutes": 40}}', CheckStatus.WARN),
        (
            '{"schema_version": 2, "playback": {"related_artists_file": "custom.toml"}}',
            CheckStatus.WARN,
        ),
    ],
)
def test_saved_settings_check(tmp_path: Path, content: str | None, status: CheckStatus) -> None:
    cfg = _config(tmp_path)
    path = (cfg.index.stations_dir / "default") / "web_state.json"
    if content is not None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")

    check = doctor._saved_settings_check(cfg)

    assert check.name == "saved-settings"
    assert check.status is status
    if check.repair == "repair-saved-settings":
        assert "no_repeat_minutes" in check.detail or "related_artists_file" in check.detail
        if "related_artists_file" in content:
            assert "Preserve the old related-artists data" in check.detail


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
    cfg.index.library_dir.parent.mkdir(parents=True, exist_ok=True)
    cfg.index.library_dir.write_text("not a directory", encoding="utf-8")

    assert doctor._index_check(cfg).status is doctor.CheckStatus.FAIL


def test_generation_artifacts_without_manifest_fail(tmp_path: Path) -> None:
    cfg = _config(tmp_path)
    cfg.index.library_dir.mkdir(parents=True, exist_ok=True)
    cfg.index.shared_dir.mkdir(parents=True, exist_ok=True)
    (cfg.index.stations_dir / "default").mkdir(parents=True, exist_ok=True)
    (cfg.index.library_dir / "tracks.g00000000000000000001.db").write_bytes(b"stale")

    check = doctor._index_check(cfg)

    assert check.status is doctor.CheckStatus.FAIL
    assert "migrate-index" in check.detail


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
    monkeypatch.setattr("autodj.model_identity.model_analysis_available", lambda: True)
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

    monkeypatch.setattr("autodj.model_identity.model_analysis_available", lambda: True)
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

    monkeypatch.setattr("autodj.model_identity.model_analysis_available", lambda: True)
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
    assert index.summary == "library is empty"
    assert dj_meta.summary == "database absent"


def test_stream_check_passes_when_disabled(tmp_path: Path) -> None:
    cfg = _config(tmp_path)

    assert doctor._stream_check(cfg).status is doctor.CheckStatus.PASS


def test_stream_check_fails_without_ffmpeg(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = _config(tmp_path)
    cfg.station.output = "stream"
    monkeypatch.setattr(doctor.shutil, "which", lambda _n: None)

    result = doctor._stream_check(cfg)

    assert result.status is doctor.CheckStatus.FAIL
    assert "ffmpeg" in result.summary.lower()


def test_stream_check_warns_on_loopback_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from dataclasses import replace

    cfg = _config(tmp_path)
    cfg.station.output = "stream"
    monkeypatch.setattr(doctor.shutil, "which", lambda _n: "/usr/bin/ffmpeg")
    cfg.server = replace(cfg.server, host="127.0.0.1")

    check = doctor._stream_check(cfg)
    assert check.status is doctor.CheckStatus.WARN
    assert check.repair == "serve-lan"


def test_stream_check_passes_enabled_with_ffmpeg_and_non_loopback_bind(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = _config(tmp_path, host="192.168.1.30")
    cfg.station.output = "stream"
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
    cfg.station.output = "stream"
    monkeypatch.setattr(doctor.shutil, "which", lambda _n: "/usr/bin/ffmpeg")

    assert doctor._stream_check(cfg).status is doctor.CheckStatus.PASS


def test_configuration_check_shows_lan_setting(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    check = doctor._configuration_check(_lan_config(tmp_path, monkeypatch))

    assert isinstance(check.detail, dict)
    assert check.detail["lan"] is True


def test_empty_catalog_warns_instead_of_passing(tmp_path: Path) -> None:
    cfg = _config(tmp_path)
    with LibraryCatalog(cfg.index.library_dir / "library.db") as catalog:
        catalog.set_setting("sources_seeded", True)
    check = doctor._index_check(cfg)
    assert check.status is doctor.CheckStatus.WARN
    assert check.summary == "0 songs; 0 analyzed"


def test_locked_metadata_is_not_offered_a_destructive_rebuild(tmp_path: Path) -> None:
    cfg = _config(tmp_path)
    cfg.index.library_dir.mkdir(parents=True, exist_ok=True)
    cfg.index.shared_dir.mkdir(parents=True, exist_ok=True)
    (cfg.index.stations_dir / "default").mkdir(parents=True, exist_ok=True)
    (cfg.index.library_dir / "dj_meta.db").write_bytes(b"cache")
    error = sqlite3.OperationalError("database is locked")
    error.sqlite_errorcode = sqlite3.SQLITE_BUSY
    with patch("autodj.doctor._connect", side_effect=error):
        check = doctor._dj_meta_database_check(cfg)
    assert check.status is CheckStatus.FAIL
    assert check.repair is None
    assert "permissions" in check.detail


def _relay_cfg(tmp_path: Path, **relays: dict) -> AutoDJConfig:
    from autodj.config import StreamConfig

    cfg = _config(tmp_path)
    cfg.station.output = "stream"
    cfg.stream = StreamConfig(relays=relays)
    return cfg


def test_relay_checks_probe_without_credentials_and_show_last_state(tmp_path: Path) -> None:
    import socket

    from autodj.relay import relay_status_path

    server = socket.socket()
    server.bind(("127.0.0.1", 0))
    server.listen()
    port = server.getsockname()[1]
    secret = "relay-password-value"
    cfg = _relay_cfg(
        tmp_path,
        up={
            "type": "icecast",
            "host": "127.0.0.1",
            "port": port,
            "mount": "/l",
            "password": secret,
        },
        sc={"type": "shoutcast1", "host": "127.0.0.1", "port": port - 1, "password": secret},
        off={"type": "shoutcast2", "host": "h", "password": secret, "enabled": False},
    )
    relay_status_path(cfg).parent.mkdir(parents=True, exist_ok=True)
    relay_status_path(cfg).write_bytes(
        json.dumps(
            {
                "updated": "2026-10-11T01:00:00+00:00",
                "relays": [
                    {"name": "up", "state": "live", "error": None, "title_error": "no"},
                    {
                        "name": "sc",
                        "state": "retrying",
                        "error": "the server rejected the password",
                    },
                ],
            }
        ).encode()
    )
    with closing(server):
        report = doctor.run_doctor(cfg, python_version=(3, 14))
    checks = {check.name: check for check in report.checks}
    assert checks["relay-up"].status is CheckStatus.PASS
    assert "the server answers" in checks["relay-up"].summary
    assert "last reported it live at 2026-10-11" in checks["relay-up"].detail
    assert "title updates failing: no" in checks["relay-up"].detail
    assert checks["relay-sc"].status is CheckStatus.WARN  # reachable, but last seen retrying
    assert "rejected the password" in checks["relay-sc"].detail
    assert checks["relay-off"].summary.endswith("off (enabled = false)")
    assert secret not in doctor.render_text(report)
    assert secret not in report.to_json()


def test_unreachable_relay_warns(tmp_path: Path) -> None:
    def refuse(address, timeout):
        raise ConnectionRefusedError(111, "refused")

    cfg = _relay_cfg(
        tmp_path, main={"type": "icecast", "host": "h.example", "mount": "/l", "password": "p"}
    )
    (check,) = doctor._relay_checks(cfg, connect=refuse)
    assert check.status is CheckStatus.WARN
    assert check.summary == (
        "Icecast at h.example:8000/l: cannot reach the server: the server refused the connection"
    )
    assert check.detail == ""


def test_no_relays_no_relay_checks(tmp_path: Path) -> None:
    assert doctor._relay_checks(_config(tmp_path)) == ()


def test_related_artists_check(tmp_path: Path) -> None:
    cfg = _config(tmp_path)
    check = doctor._related_artists_check(cfg)
    assert check.status == doctor.CheckStatus.PASS
    assert "Sample will be copied" in check.summary

    path = cfg.index.library_dir / "related-artists.toml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b'[related]\n"Bruno Mars" = ["The Hooligans"]\n')
    check = doctor._related_artists_check(cfg)
    assert check.status is doctor.CheckStatus.PASS
    assert str(path) in check.summary


def test_related_tracks_check(tmp_path: Path) -> None:
    cfg = _config(tmp_path)
    check = doctor._related_tracks_check(cfg)
    assert check.status is doctor.CheckStatus.PASS
    assert "Sample will be copied" in check.summary

    path = cfg.index.library_dir / "related-tracks.toml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b'[related]\n"Snow - Informer" = ["Daddy Yankee - Con Calma"]\n')
    check = doctor._related_tracks_check(cfg)
    assert check.status is doctor.CheckStatus.PASS
    assert check.summary.startswith("2 tracks with related tracks")

    path.write_bytes(b'[related]\n"Informer" = ["Con Calma"]\n')
    check = doctor._related_tracks_check(cfg)
    assert check.status is doctor.CheckStatus.WARN
    assert str(path) in check.detail


@pytest.mark.parametrize("content", [b"[related\n", b"related = 3\n", None])
def test_related_artists_check_warns_about_a_bad_file(tmp_path: Path, content) -> None:
    cfg = _config(tmp_path)
    path = cfg.index.library_dir / "related-artists.toml"
    path.parent.mkdir(parents=True, exist_ok=True)
    if content is not None:
        path.write_bytes(content)
    check = doctor._related_artists_check(cfg)
    assert check.status is (
        doctor.CheckStatus.WARN if content is not None else doctor.CheckStatus.PASS
    )
    if content is not None:
        assert str(path) in check.detail
    assert check.repair is None


def test_doctor_names_empty_categories_and_missing_playlist_tracks(tmp_path: Path) -> None:
    from autodj.catalog import LibraryCatalog
    from autodj.categories import Category, CategoryStore

    cfg = _config(tmp_path)
    check = {c.name: c for c in run_doctor(cfg).checks}["categories"]
    assert (check.status, check.summary) == (CheckStatus.PASS, "no categories saved")

    active = cfg.index.stations_dir / "default"
    active.mkdir(parents=True, exist_ok=True)
    music = cfg.library.music_dir
    with LibraryCatalog(cfg.index.library_dir / "library.db") as catalog:
        for name, year in (("one", 2004), ("two", 1990)):
            entry = IndexEntry(
                str(music / f"{name}.flac"),
                name,
                "A",
                "B",
                "Rock",
                120,
                year,
                180.0,
                0.2,
                -1,
                -1,
                0.0,
            )
            catalog.put_entry(entry, points={"start": 0, "end": 180})
    playlists = cfg.index.shared_dir / "playlists"
    playlists.mkdir(parents=True)
    (playlists / "A.m3u").write_text(f"{music / 'one.flac'}\nlost.flac\n", encoding="utf-8")
    store = CategoryStore(cfg.index.stations_dir / "default" / "categories.json")
    store.save(
        [
            Category(id="a", name="2000s", source="query", query="original_year:2000..2009"),
            Category(id="b", name="A", source="m3u", playlist="A.m3u"),
        ]
    )
    check = {c.name: c for c in run_doctor(cfg).checks}["categories"]
    assert check.status is CheckStatus.WARN
    assert check.summary == (
        "2 saved, 2 songs in all; A: 1 playlist line matches no library track: lost.flac"
    )
    assert check.detail["2000s"]["release_year_fallbacks"] == 1

    store.save([Category(id="c", name="Jazz", source="query", query="genre:jazz")])
    check = {c.name: c for c in run_doctor(cfg).checks}["categories"]
    assert check.summary == "1 saved, 0 songs in all; Jazz: no songs match"

    store.save([Category(id="c", name="Jazz", source="query", query="genre:rock")])
    check = {c.name: c for c in run_doctor(cfg).checks}["categories"]
    assert (check.status, check.summary) == (CheckStatus.PASS, "1 saved, 2 songs in all")

    (cfg.index.stations_dir / "default" / "categories.json").parent.mkdir(
        parents=True, exist_ok=True
    )
    (cfg.index.stations_dir / "default" / "categories.json").write_text("{", encoding="utf-8")
    check = {c.name: c for c in run_doctor(cfg).checks}["categories"]
    assert check.status is CheckStatus.WARN and "cannot be read" in check.summary
    assert (
        cfg.index.stations_dir / "default" / "categories.json"
    ).exists()  # doctor never sets the file aside


def test_new_library_needs_no_music_root(tmp_path: Path) -> None:
    cfg = _config(tmp_path)
    cfg.library.music_dir = None
    check = doctor._music_paths_check(cfg)
    assert check.status is CheckStatus.PASS
    assert check.summary == "no music folders registered"


@pytest.mark.parametrize("disabled", [True, False])
def test_unavailable_model_analysis_warns_without_cache_inspection(tmp_path, monkeypatch, disabled):
    monkeypatch.setenv("AUTODJ_NO_MODEL", "1" if disabled else "0")
    if not disabled:
        monkeypatch.setattr("autodj.model_identity.importlib.util.find_spec", lambda name: None)
    monkeypatch.setattr(
        doctor, "inspect_model_cache", lambda *_: pytest.fail("unavailable model inspected")
    )
    check = doctor._model_cache_check(_config(tmp_path))
    assert check.status is doctor.CheckStatus.WARN
    assert "autodj setup --backend cpu" in check.detail
    assert check.repair is None
