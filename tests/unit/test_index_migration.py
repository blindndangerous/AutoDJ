"""Focused coverage for the schema-3 index converter and serve prompt."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import faiss
import numpy as np
import pytest
from click.testing import CliRunner

from autodj import index_migration
from autodj.catalog import LibraryCatalog
from autodj.cli import _load_index_for_serve, cli
from autodj.config import (
    AutoDJConfig,
    HuggingFaceConfig,
    IndexConfig,
    LibraryConfig,
    ModelConfig,
    PlaybackConfig,
)
from autodj.dj_meta import Cue, DjMeta, DjMetaCache
from autodj.index_manifest import MANIFEST_NAME
from autodj.indexer import FEATURE_DIM, _write_tracks_file


def _config(root: Path) -> AutoDJConfig:
    music = root / "music"
    music.mkdir(parents=True)
    index = root / "indexes"
    active = index / "default"
    active.mkdir(parents=True)
    return AutoDJConfig(
        library=LibraryConfig(music, None, ["flac"]),
        index=IndexConfig(index, root / "models", "default"),
        playback=PlaybackConfig(),
        model=ModelConfig(),
        huggingface=HuggingFaceConfig(None),
        config_path=root / "config.toml",
    )


def _fixture(
    root: Path,
    *,
    paths: tuple[str, ...] = ("old.flac", "missing.flac"),
    index_dir: Path | None = None,
    music_dir: Path | None = None,
) -> tuple[Path, Path]:
    index_dir = root / "index" if index_dir is None else index_dir
    music_dir = root / "music" if music_dir is None else music_dir
    index_dir.mkdir(parents=True, exist_ok=True)
    music_dir.mkdir(parents=True, exist_ok=True)
    rows = [
        {
            "vec_row": i,
            "path": name,
            "title": f"Track {i}",
            "artist": "Artist",
            "album": "Album",
            "genre": "",
            "bpm": 120.0 + i,
            "year": 2000 + i,
            "length": 180.0,
            "energy": 0.5,
            "key": 1,
            "mode": 1,
            "tempo_confidence": 0.9,
            "embedded_at": 123.0 + i,
            "size": 0,
            "fingerprint": "",
        }
        for i, name in enumerate(paths)
    ]
    tracks = index_dir / "tracks.g00000000000000000001.db"
    _write_tracks_file(rows, tracks)
    with closing(sqlite3.connect(tracks)) as conn:
        conn.execute("ALTER TABLE tracks DROP COLUMN size")
        conn.execute("ALTER TABLE tracks DROP COLUMN fingerprint")
    vectors = np.zeros((len(rows), FEATURE_DIM), dtype=np.float32)
    vectors[:, 0] = np.arange(1, len(rows) + 1, dtype=np.float32)
    vectors /= np.linalg.norm(vectors, axis=1, keepdims=True)
    faiss_index = faiss.IndexFlatIP(FEATURE_DIM)
    faiss_index.add(vectors)
    vector_path = index_dir / "vectors.g00000000000000000001.index"
    faiss.write_index(faiss_index, str(vector_path))
    manifest = {
        "schema_version": 3,
        "generation": 1,
        "vector_count": len(rows),
        "published_at": "2024-01-01T00:00:00+00:00",
        "tracks_sha256": hashlib.sha256(tracks.read_bytes()).hexdigest(),
        "vectors_sha256": hashlib.sha256(vector_path.read_bytes()).hexdigest(),
    }
    (index_dir / MANIFEST_NAME).write_text(json.dumps(manifest), encoding="utf-8")
    return index_dir, music_dir


def _tree(path: Path) -> dict[str, bytes]:
    return {
        file.relative_to(path).as_posix(): file.read_bytes()
        for file in path.rglob("*")
        if file.is_file() and file.name != ".index-publication.lock"
    }


def _write_cue(path: Path, key: str = "old.flac") -> None:
    with DjMetaCache(path) as cache:
        cache.set(key, DjMeta(intro_end_s=12.5, analysed=True, cues=[Cue(12.5, "drop", "Hook")]))
        cache.flush(force=True)


def _cues(path: Path) -> list[tuple[str, str]]:
    with closing(sqlite3.connect(path)) as conn:
        return conn.execute("SELECT path, cues FROM dj_meta ORDER BY path").fetchall()


def test_dry_run_writes_nothing(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    index_dir, music_dir = _fixture(tmp_path)
    (music_dir / "old.flac").write_bytes(b"audio bytes")
    before = _tree(index_dir)

    assert index_migration.convert(index_dir, music_dir, {}, apply=False) == 0

    assert _tree(index_dir) == before
    assert "Dry run" in capsys.readouterr().out


def test_apply_preserves_vectors_metadata_and_reports_missing_file(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    index_dir, music_dir = _fixture(tmp_path)
    audio = b"audio bytes"
    (music_dir / "old.flac").write_bytes(audio)
    old_vectors = (index_dir / "vectors.g00000000000000000001.index").read_bytes()

    assert index_migration.convert(index_dir, music_dir, {}, apply=True) == 0

    manifest = json.loads((index_dir / MANIFEST_NAME).read_text(encoding="utf-8"))
    assert manifest["schema_version"] == 4
    assert manifest["generation"] == 2
    assert (index_dir / "vectors.g00000000000000000002.index").read_bytes() == old_vectors
    with closing(sqlite3.connect(index_dir / "tracks.g00000000000000000002.db")) as conn:
        rows = conn.execute(
            "SELECT path, title, artist, bpm, size, fingerprint FROM tracks ORDER BY vec_row"
        ).fetchall()
    assert rows[0] == (
        "old.flac",
        "Track 0",
        "Artist",
        120.0,
        len(audio),
        hashlib.sha256(audio).hexdigest(),
    )
    assert rows[1][:4] == ("missing.flac", "Track 1", "Artist", 121.0)
    assert rows[1][4:] == (0, "")
    assert "unreadable: missing.flac" in capsys.readouterr().out


def test_apply_rekeys_cues_and_recovery_copy_contains_wal_cues_and_history(tmp_path: Path) -> None:
    index_dir, music_dir = _fixture(tmp_path, paths=("old.flac",))
    (music_dir / "old.flac").write_bytes(b"audio")  # fallback when the move is not complete
    dj_meta_path = index_dir / "dj_meta.db"
    _write_cue(dj_meta_path)
    with closing(sqlite3.connect(dj_meta_path)) as conn:
        assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    library = sqlite3.connect(index_dir / "library.db")
    library.execute("PRAGMA journal_mode=WAL")
    library.execute("CREATE TABLE play_history(event_id TEXT PRIMARY KEY, title TEXT)")
    library.execute("INSERT INTO play_history VALUES ('play-1', 'Heard')")
    library.commit()
    assert Path(str(index_dir / "library.db") + "-wal").exists()
    recovery_output = []
    with patch(
        "builtins.print",
        side_effect=lambda *args, **kwargs: recovery_output.append(" ".join(map(str, args))),
    ):
        index_migration.convert(index_dir, music_dir, {"old.flac": "new.flac"}, apply=True)

    assert _cues(dj_meta_path)[0][0] == "new.flac"
    assert json.loads(_cues(dj_meta_path)[0][1])[0]["label"] == "Hook"
    backup_path = Path(
        next(line.split(" to ", 1)[1] for line in recovery_output if "recovery copy to" in line)
    )
    assert backup_path.is_dir()
    assert _cues(backup_path / "dj_meta.db")[0][0] == "old.flac"
    with closing(sqlite3.connect(backup_path / "library.db")) as conn:
        assert conn.execute("SELECT title FROM play_history").fetchall() == [("Heard",)]
    library.close()


@pytest.mark.parametrize(
    ("moves", "message"),
    [
        ({"old.flac": "../escape.flac"}, "relative paths"),
        ({"not-indexed.flac": "new.flac"}, "not in this index"),
        ({"old.flac": "missing.flac", "missing.flac": "old.flac"}, "existing indexed paths"),
        ({"old.flac": "same.flac", "missing.flac": "same.flac"}, "more than one track"),
    ],
)
def test_invalid_move_maps_are_refused(tmp_path: Path, moves: dict[str, str], message: str) -> None:
    index_dir, music_dir = _fixture(tmp_path)
    with pytest.raises(ValueError, match=message):
        index_migration.convert(index_dir, music_dir, moves, apply=False)


def test_corrupt_index_is_rejected(tmp_path: Path) -> None:
    index_dir, music_dir = _fixture(tmp_path)
    vectors = index_dir / "vectors.g00000000000000000001.index"
    vectors.write_bytes(b"corrupt")
    manifest_path = index_dir / MANIFEST_NAME
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["vectors_sha256"] = hashlib.sha256(vectors.read_bytes()).hexdigest()
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    with pytest.raises(RuntimeError):
        index_migration.convert(index_dir, music_dir, {}, apply=False)


def test_recovery_copy_failure_leaves_original_cues_and_generation(tmp_path: Path) -> None:
    index_dir, music_dir = _fixture(tmp_path, paths=("old.flac",))
    _write_cue(index_dir / "dj_meta.db")
    before = _tree(index_dir)
    with (
        patch("autodj.index_migration._recovery_copy", side_effect=OSError("disk full")),
        pytest.raises(OSError, match="disk full"),
    ):
        index_migration.convert(index_dir, music_dir, {"old.flac": "new.flac"}, apply=True)
    assert _tree(index_dir) == before
    assert _cues(index_dir / "dj_meta.db")[0][0] == "old.flac"


def test_manifest_publish_failure_restores_original_cue_keys(tmp_path: Path) -> None:
    index_dir, music_dir = _fixture(tmp_path, paths=("old.flac",))
    _write_cue(index_dir / "dj_meta.db")
    atomic_write = index_migration.atomic_write

    def fail_manifest(path: Path, data: str | bytes, **kwargs: object) -> None:
        if Path(path) == index_dir / MANIFEST_NAME:
            raise OSError("manifest write failed")
        atomic_write(path, data, **kwargs)

    with (
        patch("autodj.index_migration.atomic_write", side_effect=fail_manifest),
        pytest.raises(OSError, match="manifest write failed"),
    ):
        index_migration.convert(index_dir, music_dir, {"old.flac": "new.flac"}, apply=True)
    assert (
        json.loads((index_dir / MANIFEST_NAME).read_text(encoding="utf-8"))["schema_version"] == 3
    )
    assert _cues(index_dir / "dj_meta.db")[0][0] == "old.flac"


def test_manifest_fsync_failure_keeps_cues_with_published_generation(tmp_path: Path) -> None:
    index_dir, music_dir = _fixture(tmp_path, paths=("old.flac",))
    _write_cue(index_dir / "dj_meta.db")
    atomic_write = index_migration.atomic_write

    def fail_after_manifest(path: Path, data: str | bytes, **kwargs: object) -> None:
        atomic_write(path, data, **kwargs)
        if Path(path) == index_dir / MANIFEST_NAME:
            raise OSError("directory sync failed")

    with (
        patch("autodj.index_migration.atomic_write", side_effect=fail_after_manifest),
        pytest.raises(OSError, match="directory sync failed"),
    ):
        index_migration.convert(index_dir, music_dir, {"old.flac": "new.flac"}, apply=True)
    assert (
        json.loads((index_dir / MANIFEST_NAME).read_text(encoding="utf-8"))["schema_version"] == 4
    )
    assert _cues(index_dir / "dj_meta.db")[0][0] == "new.flac"


def test_cli_migrate_default_is_dry_run(tmp_path: Path) -> None:
    cfg = _config(tmp_path)
    index_dir, music_dir = _fixture(
        tmp_path, index_dir=cfg.index.active_dir, music_dir=cfg.library.music_dir
    )
    (music_dir / "old.flac").write_bytes(b"audio")
    before = _tree(index_dir)
    with patch("autodj.cli._load_cfg_or_exit", return_value=cfg):
        result = CliRunner().invoke(cli, ["migrate-index"])
    assert result.exit_code == 0, result.output
    assert "Dry run" in result.output
    assert _tree(index_dir) == before


def test_accepted_startup_converts_and_loads_real_named_index(tmp_path: Path) -> None:
    cfg = _config(tmp_path)
    named_dir = cfg.index.index_dir / "gold"
    _fixture(tmp_path, index_dir=named_dir, music_dir=cfg.library.music_dir)
    with (
        patch("autodj.cli.sys.stdin", SimpleNamespace(isatty=lambda: True)),
        patch("autodj.cli.click.confirm", return_value=True),
    ):
        sim = _load_index_for_serve(cfg, active_dir=named_dir)
    assert sim.ntotal == 2
    assert json.loads((named_dir / MANIFEST_NAME).read_text())["schema_version"] == 4
    assert not (cfg.index.active_dir / MANIFEST_NAME).exists()


@pytest.mark.parametrize("accept", [True, False])
def test_serve_prompt_accepts_or_declines_for_named_index(tmp_path: Path, accept: bool) -> None:
    cfg = _config(tmp_path)
    named_dir = cfg.index.index_dir / "named"
    named_dir.mkdir()
    manifest = {"schema_version": 3}
    (named_dir / MANIFEST_NAME).write_text(json.dumps(manifest), encoding="utf-8")
    from autodj.index_manifest import UnsupportedIndexError
    from autodj.similarity import SimilarityIndex

    unsupported = UnsupportedIndexError(named_dir, "schema 3", schema_version=3)
    loads = iter((unsupported, object()))

    def load_index(*args: object, **kwargs: object) -> object:
        result = next(loads)
        if isinstance(result, UnsupportedIndexError):
            raise result
        return result

    with (
        patch("autodj.cli.sys.stdin", SimpleNamespace(isatty=lambda: True)),
        patch("autodj.cli.click.confirm", return_value=accept) as confirm,
        patch("autodj.cli._convert_index") as convert,
        patch.object(SimilarityIndex, "from_index_dir", side_effect=load_index),
    ):
        if accept:
            value = _load_index_for_serve(cfg, active_dir=named_dir)
            assert value is not None
            convert.assert_called_once_with(named_dir, cfg.library.music_dir, {}, apply=True)
        else:
            with pytest.raises(UnsupportedIndexError):
                _load_index_for_serve(cfg, active_dir=named_dir)
            convert.assert_not_called()
    confirm.assert_called_once()


def test_non_tty_serve_does_not_prompt_and_uses_named_active_dir(tmp_path: Path) -> None:
    cfg = _config(tmp_path)
    named_dir = cfg.index.index_dir / "named"
    named_dir.mkdir()
    (named_dir / MANIFEST_NAME).write_text('{"schema_version": 3}', encoding="utf-8")
    from autodj.index_manifest import UnsupportedIndexError
    from autodj.similarity import SimilarityIndex

    with (
        patch("autodj.cli.sys.stdin", SimpleNamespace(isatty=lambda: False)),
        patch("autodj.cli.click.confirm") as confirm,
        patch("autodj.cli._convert_index") as convert,
        patch.object(
            SimilarityIndex,
            "from_index_dir",
            side_effect=UnsupportedIndexError(named_dir, "schema 3", schema_version=3),
        ) as load,
        pytest.raises(UnsupportedIndexError),
    ):
        _load_index_for_serve(cfg, active_dir=named_dir)
    confirm.assert_not_called()
    convert.assert_not_called()
    assert load.call_args.kwargs["music_dir"] == cfg.library.music_dir
    assert load.call_args.args[0] == named_dir


def test_migrate_imports_schema3_vectors_and_catalog_state_without_analysis(tmp_path: Path) -> None:
    cfg = _config(tmp_path)
    index_dir, music_dir = _fixture(
        tmp_path,
        paths=("old.flac",),
        index_dir=cfg.index.active_dir,
        music_dir=cfg.library.music_dir,
    )
    audio = music_dir / "old.flac"
    audio.write_bytes(b"audio bytes")
    _write_cue(index_dir / "dj_meta.db")
    original = faiss.read_index(str(index_dir / "vectors.g00000000000000000001.index"))
    with closing(sqlite3.connect(index_dir / "library.db")) as db:
        db.execute("CREATE TABLE catalog_settings (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
        db.execute(
            "INSERT INTO catalog_settings VALUES ('play_history', ?)",
            (
                json.dumps(
                    [
                        {
                            "title": "Old song",
                            "artist": "Singer",
                            "duration": 120,
                            "played_at": "2024-01-01T00:00:00+00:00",
                        }
                    ]
                ),
            ),
        )
        db.commit()
    state = {
        "schema_version": 1,
        "djmix": {"harmonic_mode": "compatible", "harmonic_mixing": False},
    }
    (index_dir / "web_state.json").write_text(json.dumps(state), encoding="utf-8")

    assert index_migration.migration_required(index_dir)[0]
    assert (
        index_migration.migrate_catalog(index_dir, music_dir, tmp_path / "liners", {}, apply=True)
        == 0
    )

    with closing(sqlite3.connect(index_dir / "library.db")) as db:
        row = db.execute(
            "SELECT e.path,e.title,e.artist,e.points,e.point_sources,v.vector "
            "FROM catalog_entries e JOIN catalog_vectors v ON v.entry_id=e.id"
        ).fetchone()
        history = db.execute("SELECT title,artist,duration,played_at FROM play_history").fetchall()
        settings = dict(db.execute("SELECT key,value FROM catalog_settings"))
        sources = db.execute("SELECT path,kind,recursive,advanced FROM catalog_sources").fetchall()
    assert row[:3] == (str(audio.resolve()).casefold(), "Track 0", "Artist")
    assert json.loads(row[3]) == {"intro": 12.5}
    assert json.loads(row[4]) == {"intro": "analysis"}
    np.testing.assert_array_equal(np.frombuffer(row[5], dtype=np.float32), original.reconstruct(0))
    assert history == [("Old song", "Singer", 120.0, "2024-01-01T00:00:00+00:00")]
    assert json.loads(settings["play_history_migrated"]) is True
    assert json.loads(settings["imported_index_generation"])["generation"] == 2
    assert (str(music_dir.resolve()).casefold(), "music", 1, 1) in sources
    assert json.loads((index_dir / "web_state.json").read_text())["djmix"]["harmonic_mode"] == "off"
    assert (index_dir / "web_state.json.v1.bak").exists()
    assert not index_migration.migration_required(index_dir)[0]

    # Reruns fill missing import work, but never replace current catalog values.
    with closing(sqlite3.connect(index_dir / "library.db")) as db:
        db.execute("UPDATE catalog_entries SET title='Current title'")
        db.commit()
    index_migration.migrate_catalog(index_dir, music_dir, tmp_path / "liners", {}, apply=True)
    with closing(sqlite3.connect(index_dir / "library.db")) as db:
        assert db.execute("SELECT title FROM catalog_entries").fetchone() == ("Current title",)
        assert db.execute("SELECT COUNT(*) FROM play_history").fetchone() == (1,)


def test_migrate_preview_reports_catalog_only_conversions_without_writing(tmp_path: Path) -> None:
    index_dir = tmp_path / "index"
    index_dir.mkdir()
    with closing(sqlite3.connect(index_dir / "library.db")) as db:
        db.execute("CREATE TABLE catalog_settings (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
        db.execute("INSERT INTO catalog_settings VALUES ('sources_seeded','false')")
        db.execute("INSERT INTO catalog_settings VALUES ('play_history','[]')")
        db.commit()
    (index_dir / "web_state.json").write_text(
        '{"schema_version":1,"djmix":{"harmonic_mixing":false}}', encoding="utf-8"
    )
    before = _tree(index_dir)
    assert index_migration.migration_required(index_dir) == (
        True,
        "web_state.json schema 1; legacy play-history JSON; initial folder registration is incomplete",
    )

    index_migration.migrate_catalog(
        index_dir, tmp_path / "music", tmp_path / "liners", {}, apply=False
    )
    assert _tree(index_dir) == before


def test_migrate_upgrades_catalog_only_legacy_state(tmp_path: Path) -> None:
    index_dir = tmp_path / "index"
    index_dir.mkdir()
    with closing(sqlite3.connect(index_dir / "library.db")) as db:
        db.execute("CREATE TABLE catalog_settings (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
        db.execute("INSERT INTO catalog_settings VALUES ('sources_seeded','false')")
        db.execute(
            "INSERT INTO catalog_settings VALUES ('play_history', ?)",
            (
                json.dumps(
                    [
                        {
                            "title": "Old song",
                            "artist": "Singer",
                            "duration": 120,
                            "played_at": "2024-01-01T00:00:00+00:00",
                        }
                    ]
                ),
            ),
        )
        db.commit()
    (index_dir / "web_state.json").write_text(
        '{"schema_version":1,"djmix":{"harmonic_mode":"compatible","harmonic_mixing":false}}',
        encoding="utf-8",
    )

    index_migration.migrate_catalog(
        index_dir, tmp_path / "music", tmp_path / "liners", {}, apply=True
    )
    assert not index_migration.migration_required(index_dir)[0]
    with closing(sqlite3.connect(index_dir / "library.db")) as db:
        assert db.execute("SELECT title,artist,duration FROM play_history").fetchall() == [
            ("Old song", "Singer", 120.0)
        ]
        assert db.execute("SELECT COUNT(*) FROM catalog_sources").fetchone() == (2,)
    assert json.loads((index_dir / "web_state.json").read_text())["djmix"]["harmonic_mode"] == "off"


def test_settings_migration_preserves_existing_catalog_source_roots(tmp_path: Path) -> None:
    index_dir = tmp_path / "index"
    index_dir.mkdir()
    configured_music = tmp_path / "configured-music"
    configured_liners = tmp_path / "configured-liners"
    chosen_music = tmp_path / "chosen-music"
    chosen_liners = tmp_path / "chosen-liners"
    for folder in (configured_music, configured_liners, chosen_music, chosen_liners):
        folder.mkdir()
    with LibraryCatalog(index_dir / "library.db") as catalog:
        catalog.add_source(chosen_music, "music", recursive=False, advanced=False)
        catalog.add_source(chosen_liners, "liner", recursive=False, advanced=False)
    state = index_dir / "web_state.json"
    state.write_text('{"schema_version":1,"djmix":{"harmonic_mixing":false}}')

    index_migration.migrate_catalog(index_dir, configured_music, configured_liners, {}, apply=True)

    with LibraryCatalog(index_dir / "library.db") as catalog:
        assert {(source["path"], source["kind"]) for source in catalog.sources()} == {
            (str(chosen_music.resolve()).casefold(), "music"),
            (str(chosen_liners.resolve()).casefold(), "liner"),
        }


def test_migrate_imports_schema4_and_resumes_after_interruption(tmp_path: Path) -> None:
    index_dir, music_dir = _fixture(tmp_path, paths=("old.flac",))
    (music_dir / "old.flac").write_bytes(b"audio bytes")
    index_migration.convert(index_dir, music_dir, {}, apply=True)
    with (
        patch("autodj.catalog.LibraryCatalog.seed_vectors", side_effect=OSError("interrupted")),
        pytest.raises(OSError, match="interrupted"),
    ):
        index_migration.migrate_catalog(index_dir, music_dir, tmp_path / "liners", {}, apply=True)
    assert index_migration.migration_required(index_dir) == (
        True,
        "published-index import was interrupted",
    )

    index_migration.migrate_catalog(index_dir, music_dir, tmp_path / "liners", {}, apply=True)
    assert not index_migration.migration_required(index_dir)[0]
    with closing(sqlite3.connect(index_dir / "library.db")) as db:
        assert db.execute("SELECT COUNT(*) FROM catalog_entries").fetchone() == (1,)
        assert db.execute("SELECT COUNT(*) FROM catalog_vectors").fetchone() == (1,)


def test_current_catalog_ignores_unimported_stale_manifest(tmp_path: Path) -> None:
    index_dir, music_dir = _fixture(tmp_path, paths=("old.flac",))
    (index_dir / "index-manifest.json").write_text("stale corrupt manifest", encoding="utf-8")
    station_state = tmp_path / "stations" / "secondary" / "web_state.json"
    station_state.parent.mkdir(parents=True)
    station_state.write_text(
        '{"schema_version":1,"djmix":{"harmonic_mixing":false}}', encoding="utf-8"
    )
    with LibraryCatalog(index_dir / "library.db") as catalog:
        catalog.set_setting("sources_seeded", True)
    assert index_migration.migration_required(index_dir, [station_state]) == (
        True,
        "web_state.json schema 1",
    )
    index_migration.migrate_catalog(
        index_dir,
        music_dir,
        tmp_path / "liners",
        {},
        apply=True,
        station_web_states=[station_state],
    )
    assert json.loads(station_state.read_text())["djmix"]["harmonic_mode"] == "off"
    assert station_state.with_suffix(".json.v1.bak").exists()
    assert not index_migration.migration_required(index_dir, [station_state])[0]
    with closing(sqlite3.connect(index_dir / "library.db")) as db:
        assert db.execute("SELECT COUNT(*) FROM catalog_entries").fetchone() == (0,)


@pytest.mark.parametrize("legacy_history", ["{broken", "[null]"])
def test_invalid_legacy_history_blocks_migration_before_writes(
    tmp_path: Path, legacy_history: str
) -> None:
    index_dir = tmp_path / "index"
    index_dir.mkdir()
    with closing(sqlite3.connect(index_dir / "library.db")) as db:
        db.execute("CREATE TABLE catalog_settings (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
        db.execute("INSERT INTO catalog_settings VALUES ('play_history', ?)", (legacy_history,))
        db.commit()
    before = _tree(index_dir)

    with pytest.raises(ValueError, match="legacy play-history"):
        index_migration.migrate_catalog(
            index_dir, tmp_path / "music", tmp_path / "liners", {}, apply=False
        )
    with pytest.raises(ValueError, match="legacy play-history"):
        index_migration.migrate_catalog(
            index_dir, tmp_path / "music", tmp_path / "liners", {}, apply=True
        )
    assert _tree(index_dir) == before


@pytest.mark.parametrize("apply", [False, True])
def test_invalid_legacy_history_blocks_schema3_conversion_before_writes(
    tmp_path: Path, apply: bool
) -> None:
    index_dir, music_dir = _fixture(tmp_path, paths=("old.flac",))
    with closing(sqlite3.connect(index_dir / "library.db")) as db:
        db.execute("CREATE TABLE catalog_settings (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
        db.execute("INSERT INTO catalog_settings VALUES ('play_history','[null]')")
        db.commit()
    before = _tree(index_dir)

    with pytest.raises(ValueError, match="legacy play-history"):
        index_migration.migrate_catalog(index_dir, music_dir, tmp_path / "liners", {}, apply=apply)

    assert _tree(index_dir) == before
