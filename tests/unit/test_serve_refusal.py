"""Serve refuses unsafe startup before persistent changes, locks, or listeners."""

import json
import shutil
import sqlite3
from contextlib import closing
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import patch

import pytest
from click.testing import CliRunner

from autodj.certificates import CertificateStore
from autodj.cli import cli
from autodj.config import load_config
from autodj.doctor import CheckStatus, _index_check, _network_check


def configuration(tmp_path, server=""):
    path = tmp_path / "config.toml"
    path.write_text(f"[index]\nindex_dir = '{tmp_path.as_posix()}/index'\n" + server)
    return path


def snapshot(root):
    # SQLite readers update existing shared-memory read marks, never database/WAL data.
    return {
        str(p.relative_to(root)): (
            b"SQLite shared memory" if p.name.endswith("-shm") else p.read_bytes()
        )
        if p.is_file()
        else None
        for p in root.rglob("*")
    }


def refused_without_starting(path, message, *arguments):
    before = snapshot(path.parent)
    with (
        patch("autodj.server.serve") as serve,
        patch("autodj.cli._claim_index_for_serve") as claim,
        patch("autodj.library.LibraryManager") as library,
        patch("socket.socket.bind", side_effect=AssertionError("bound before validation")),
        patch("autodj.lan.detect_lan_hosts", return_value=["localhost", "127.0.0.1"]),
    ):
        result = CliRunner().invoke(cli, ["--config", str(path), "serve", *arguments])
    assert result.exit_code != 0, result.output
    assert message in result.output, (result.output, result.exception)
    serve.assert_not_called()
    claim.assert_not_called()
    library.assert_not_called()
    assert snapshot(path.parent) == before
    return result


def test_invalid_config_refuses_before_any_startup_write(tmp_path):
    path = configuration(tmp_path, "[server]\nport = 'invalid'\n")
    refused_without_starting(path, "Config not found or invalid")


def test_unsafe_exposure_refuses_before_any_startup_write(tmp_path):
    path = configuration(
        tmp_path,
        "[server]\nhost = '0.0.0.0'\nallowed_hosts = ['radio.local']\nallowed_origins = ['http://radio.local:8080']\n",
    )
    cfg = load_config(path, environ={})
    assert _network_check(cfg).status is CheckStatus.FAIL
    refused_without_starting(path, "access_token")


def test_unusable_advertised_origin_refuses_before_any_startup_write(tmp_path):
    path = configuration(
        tmp_path,
        "[server]\nhost = '0.0.0.0'\naccess_token = '"
        + "x" * 32
        + "'\nallowed_hosts = ['radio.local']\nallowed_origins = ['http://other.local:8080']\n",
    )
    assert _network_check(load_config(path, environ={})).status is CheckStatus.FAIL
    refused_without_starting(path, "allowed origin")


@pytest.mark.parametrize("saved", [False, True])
@pytest.mark.parametrize("problem", ["missing", "malformed", "mismatch", "encrypted"])
def test_unusable_https_refuses_before_writes_and_doctor_agrees(tmp_path, saved, problem):
    """ADR-0019-07."""
    path = configuration(tmp_path)
    cfg = load_config(path, environ={})
    store = CertificateStore(cfg.index.index_dir / ".tls")
    first = store.create("Local", ["localhost"])
    cert, key = store.paths(first["id"])
    if saved:
        store.activate(first["id"])
    else:
        path.write_text(
            path.read_text()
            + f"[server]\nssl_certfile = '{cert.as_posix()}'\nssl_keyfile = '{key.as_posix()}'\n"
        )
    if problem == "missing":
        key.unlink()
    elif problem == "malformed":
        cert.write_bytes(b"not a certificate")
    elif problem == "mismatch":
        second = store.create("Other", ["localhost"])
        key.write_bytes(store.paths(second["id"])[1].read_bytes())
    else:
        from cryptography import x509
        from cryptography.hazmat.primitives import hashes, serialization

        private = serialization.load_pem_private_key(key.read_bytes(), password=None)
        if problem == "encrypted":
            key.write_bytes(
                private.private_bytes(
                    serialization.Encoding.PEM,
                    serialization.PrivateFormat.PKCS8,
                    serialization.BestAvailableEncryption(b"test-password"),
                )
            )
        else:
            original = x509.load_pem_x509_certificate(cert.read_bytes())
            now = datetime.now(UTC)
            expired = (
                x509.CertificateBuilder()
                .subject_name(original.subject)
                .issuer_name(original.issuer)
                .public_key(private.public_key())
                .serial_number(1)
                .not_valid_before(now - timedelta(days=2))
                .not_valid_after(now - timedelta(days=1))
                .sign(private, hashes.SHA256())
            )
            cert.write_bytes(expired.public_bytes(serialization.Encoding.PEM))
    refused_without_starting(path, "ssl_keyfile" if problem == "missing" else "HTTPS", "--lan")
    from autodj.doctor import _tls_checks

    checks = _tls_checks(load_config(path, environ={}))
    assert checks[0].status is CheckStatus.FAIL
    assert checks[0].repair is None


@pytest.mark.parametrize("old_layout", [False, True])
@pytest.mark.parametrize("kind", ["legacy", "newer-manifest", "newer-catalog"])
def test_library_refusal_is_read_only_and_matches_doctor(tmp_path, old_layout, kind):
    """ADR-0006-09."""
    path = configuration(tmp_path)
    cfg = load_config(path, environ={})
    folder = cfg.index.index_dir / "default" if old_layout else cfg.index.library_dir
    folder.mkdir(parents=True)
    if kind == "newer-catalog":
        with closing(sqlite3.connect(folder / "library.db")) as db:
            db.execute("PRAGMA user_version=999")
    else:
        (folder / "index-manifest.json").write_text(
            json.dumps({"schema_version": 999 if kind == "newer-manifest" else 4})
        )
    check = _index_check(cfg)
    assert check.status is CheckStatus.FAIL
    if kind == "legacy":
        assert check.repair == "migrate-index"
        refused_without_starting(path, "autodj doctor --fix", "--lan")
    else:
        assert "newer" in str(check.detail)
        assert check.repair is None
        refused_without_starting(path, "newer", "--lan")


def test_doctor_uses_full_exposure_validation_even_with_a_token(tmp_path):
    path = configuration(
        tmp_path, "[server]\nhost = '0.0.0.0'\naccess_token = '" + "x" * 32 + "'\n"
    )
    cfg = load_config(path, environ={})
    assert _network_check(cfg).status is CheckStatus.FAIL
    refused_without_starting(path, "allowed_hosts")


def test_warning_only_conditions_do_not_gate_browser_serving(tmp_path):
    path = configuration(tmp_path)
    with (
        patch("autodj.server.serve", return_value=None) as serve,
        patch(
            "autodj.doctor.run_doctor",
            side_effect=AssertionError("Doctor severity is not a startup gate"),
        ),
        patch("autodj.model_identity.model_analysis_available", return_value=False),
        patch("autodj.doctor._module_available", return_value=False),
        patch("shutil.which", return_value=None),
    ):
        result = CliRunner().invoke(cli, ["--config", str(path), "serve"])
    assert result.exit_code == 0, (result.output, result.exception)
    serve.assert_called_once()
    assert serve.call_args.kwargs["sim"].ntotal == 0


def test_doctor_previews_implicit_lan_without_saving_a_token(tmp_path):
    path = configuration(tmp_path, "[server]\nhost = '192.168.1.20'\n")
    cfg = load_config(path, environ={})
    before = snapshot(tmp_path)
    with patch("autodj.lan.detect_lan_hosts", return_value=["localhost", "192.168.1.20"]):
        assert _network_check(cfg).status is CheckStatus.PASS
    assert snapshot(tmp_path) == before


def test_explicit_valid_tls_overrides_an_unusable_saved_selection(tmp_path):
    """ADR-0019-07."""
    path = configuration(tmp_path)
    cfg = load_config(path, environ={})
    store = CertificateStore(cfg.index.index_dir / ".tls")
    saved = store.create("Saved", ["localhost"])
    store.activate(saved["id"])
    store.paths(saved["id"])[1].unlink()
    chosen = store.create("Explicit", ["localhost"])
    cert, key = store.paths(chosen["id"])
    with patch("autodj.server.serve", return_value=None) as serve:
        result = CliRunner().invoke(
            cli,
            [
                "--config",
                str(path),
                "serve",
                "--ssl-certfile",
                str(cert),
                "--ssl-keyfile",
                str(key),
            ],
        )
    assert result.exit_code == 0, (result.output, result.exception)
    assert Path(serve.call_args.kwargs["cfg"].server.ssl_certfile) == cert
    assert store.active_id == saved["id"]


def test_direct_serve_checks_catalog_before_spill_cleanup_or_station_creation(tmp_path):
    from autodj.server import serve

    cfg = load_config(configuration(tmp_path), environ={})
    cfg.index.library_dir.mkdir(parents=True)
    with closing(sqlite3.connect(cfg.index.library_dir / "library.db")) as db:
        db.execute("PRAGMA user_version=999")
    spill = cfg.index.index_dir / "cache/spill/autodj-audio-old"
    spill.parent.mkdir(parents=True)
    spill.write_bytes(b"keep")
    before = snapshot(tmp_path)
    with (
        patch("autodj.library.LibraryManager") as library,
        patch("autodj.server._extra_station") as station,
        patch("autodj.server.create_app") as app,
        patch("uvicorn.Server"),
        pytest.raises(RuntimeError, match="newer"),
    ):
        serve(cfg, None, None)
    library.assert_not_called()
    station.assert_not_called()
    app.assert_not_called()
    assert snapshot(tmp_path) == before


@pytest.mark.parametrize("wal_state", ["checkpointed", "pending", "orphan"])
@pytest.mark.parametrize("kind", ["legacy", "newer"])
def test_catalog_refusal_never_writes_sqlite_sidecars(tmp_path, wal_state, kind):
    """ADR-0006-09: read-only SQLite connections must not change live sidecars."""
    path = configuration(tmp_path)
    cfg = load_config(path, environ={})
    cfg.index.library_dir.mkdir(parents=True)
    target = cfg.index.library_dir / "library.db"
    source = tmp_path / "source.db" if wal_state == "orphan" else target
    db = sqlite3.connect(source)
    try:
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("CREATE TABLE catalog_settings (key TEXT PRIMARY KEY, value TEXT)")
        db.execute("INSERT INTO catalog_settings VALUES ('play_history', '[]')")
        if kind == "newer":
            db.execute("PRAGMA user_version=999")
        db.commit()
        if wal_state == "orphan":
            shutil.copyfile(source, target)
            shutil.copyfile(str(source) + "-wal", str(target) + "-wal")
        if wal_state != "pending":
            db.close()
        before = snapshot(tmp_path)
        check = _index_check(cfg)
        assert check.status is CheckStatus.FAIL
        assert (check.repair == "migrate-index") == (kind == "legacy")
        assert snapshot(tmp_path) == before
        refused_without_starting(path, "needs migration" if kind == "legacy" else "newer")
    finally:
        db.close()


@pytest.mark.parametrize("saved", [False, True])
@pytest.mark.parametrize("dates", ["expired", "future"])
def test_certificate_dates_warn_without_disabling_https(tmp_path, saved, dates):
    """ADR-0019-07: renewal remains reachable over configured HTTPS."""
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization

    from autodj.doctor import _tls_checks
    from autodj.tls import validate_tls_pair

    path = configuration(tmp_path)
    cfg = load_config(path, environ={})
    store = CertificateStore(cfg.index.index_dir / ".tls")
    item = store.create("Local", ["localhost"])
    cert, key = store.paths(item["id"])
    if saved:
        store.activate(item["id"])
    else:
        path.write_text(
            path.read_text()
            + f"[server]\nssl_certfile = '{cert.as_posix()}'\nssl_keyfile = '{key.as_posix()}'\n"
        )
    original = x509.load_pem_x509_certificate(cert.read_bytes())
    private = serialization.load_pem_private_key(key.read_bytes(), password=None)
    now = datetime.now(UTC)
    before, after = (-2, -1) if dates == "expired" else (1, 2)
    replacement = (
        x509.CertificateBuilder()
        .subject_name(original.subject)
        .issuer_name(original.issuer)
        .public_key(private.public_key())
        .serial_number(2)
        .not_valid_before(now + timedelta(days=before))
        .not_valid_after(now + timedelta(days=after))
        .sign(private, hashes.SHA256())
    )
    cert.write_bytes(replacement.public_bytes(serialization.Encoding.PEM))
    cfg = load_config(path, environ={})
    assert validate_tls_pair(cfg.server) is not None
    check = _tls_checks(cfg)[0]
    assert check.status is CheckStatus.WARN
    expected = "expired" if dates == "expired" else "not yet valid"
    assert expected in check.summary
    assert expected in store.describe(replacement, "Configured", "configured")["warning"]
    with patch("autodj.server.serve", return_value=None) as serve:
        result = CliRunner().invoke(cli, ["--config", str(path), "serve"])
    assert result.exit_code == 0, (result.output, result.exception)
    assert Path(serve.call_args.kwargs["cfg"].server.ssl_certfile) == cert


@pytest.mark.parametrize("old", [False, True])
def test_pending_restore_refuses_before_any_writes(tmp_path, old):
    path = configuration(tmp_path)
    cfg = load_config(path, environ={})
    folder = cfg.index.index_dir / "default" if old else cfg.index.library_dir
    folder.mkdir(parents=True)
    (folder / ".autodj-restore.json").write_text('{"state":"installing","steps":[]}')
    refused_without_starting(path, "autodj doctor --fix", "--lan")


def test_failed_claim_precedes_logs_layout_spill_and_token_writes(tmp_path):
    path = configuration(tmp_path)
    before = snapshot(tmp_path)
    from autodj.backup import BackupError

    with patch("autodj.backup.hold_serve_lock", side_effect=BackupError("already serving")):
        result = CliRunner().invoke(cli, ["--config", str(path), "serve", "--lan"])
    assert result.exit_code != 0
    assert "already serving" in result.output
    assert snapshot(tmp_path) == before


def test_large_checkpointed_catalog_is_inspected_without_copying(tmp_path):
    """ADR-0006-09: ordinary schema checks need no proportional temporary space."""
    from autodj.index_migration import migration_required

    path = tmp_path / "library.db"
    with closing(sqlite3.connect(path)) as db:
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("CREATE TABLE padding (value BLOB)")
        db.execute("INSERT INTO padding VALUES (zeroblob(16777216))")
        db.execute("PRAGMA user_version=999")
        db.commit()
    before = snapshot(tmp_path)
    with (
        patch(
            "autodj.index_migration.shutil.copyfile", side_effect=AssertionError("catalog copied")
        ),
        patch(
            "autodj.index_migration.tempfile.TemporaryDirectory",
            side_effect=OSError("temp disk full"),
        ),
        pytest.raises(RuntimeError, match="newer"),
    ):
        migration_required(tmp_path)
    assert snapshot(tmp_path) == before


def test_cli_passes_successful_library_preflight_to_server_and_manager(tmp_path):
    """ADR-0006-09: one compatibility check covers the entire serve startup."""
    from autodj.index_migration import migration_required
    from autodj.library import require_configured_library

    path = configuration(tmp_path)
    with (
        patch(
            "autodj.library.require_configured_library", wraps=require_configured_library
        ) as check,
        patch("autodj.index_migration.migration_required", wraps=migration_required) as inspect,
        patch("uvicorn.Server"),
    ):
        result = CliRunner().invoke(cli, ["--config", str(path), "serve"])
    assert result.exit_code == 0, (result.output, result.exception)
    assert check.call_count == 1
    assert inspect.call_count == 1


def test_direct_server_refuses_pending_restore_before_writes(tmp_path):
    from autodj.server import serve

    cfg = load_config(configuration(tmp_path), environ={})
    cfg.index.library_dir.mkdir(parents=True)
    (cfg.index.library_dir / ".autodj-restore.json").write_text('{"state":"installing","steps":[]}')
    before = snapshot(tmp_path)
    with (
        patch("autodj.library.LibraryManager") as library,
        pytest.raises(RuntimeError, match="autodj doctor --fix"),
    ):
        serve(cfg, None, None)
    library.assert_not_called()
    assert snapshot(tmp_path) == before
