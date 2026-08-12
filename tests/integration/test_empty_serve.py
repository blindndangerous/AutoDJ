import threading
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

import numpy as np
import pytest
from click.testing import CliRunner
from fastapi.testclient import TestClient

from autodj.cli import _load_index_for_serve, cli
from autodj.config import ServerConfig, load_config
from autodj.index_manifest import IndexConsistencyError, read_manifest, tombstone_publication
from autodj.indexer import FEATURE_DIM, IndexEntry, _save_tracks_metadata, _save_vectors, save_index
from autodj.player import Player
from autodj.server import PlayerBridge, create_app
from autodj.similarity import SimilarityIndex


class _WaitSignallingEvent:
    """Threading event that exposes when the player enters its wait loop."""

    def __init__(self) -> None:
        self._event = threading.Event()
        self.wait_started = threading.Event()

    def wait(self, timeout: float | None = None) -> bool:
        self.wait_started.set()
        return self._event.wait(timeout)

    def set(self) -> None:
        self._event.set()

    def clear(self) -> None:
        self._event.clear()


def test_empty_similarity_index_has_feature_dimension() -> None:
    sim = SimilarityIndex.empty()
    assert sim.ntotal == 0
    assert sim.faiss_index.d == 1040


def test_empty_similarity_index_reloads_a_published_generation(tmp_path: Path) -> None:
    sim = SimilarityIndex.empty()
    entry = IndexEntry(
        path=(tmp_path / "song.flac").as_posix(),
        title="Song",
        artist="Artist",
        album="",
        genre="",
        bpm=0.0,
        year=0,
        length=1.0,
        energy=0.0,
        key=-1,
        mode=-1,
        tempo_confidence=0.0,
    )
    vectors = np.zeros((1, FEATURE_DIM), dtype=np.float32)
    vectors[0, 0] = 1.0
    save_index([entry], vectors, tmp_path)
    manifest = read_manifest(tmp_path)
    assert manifest is not None
    assert sim.reload_from_disk(tmp_path, expected_generation=manifest.generation) == 1
    assert sim.ntotal == 1
    assert sim.entries_snapshot() == (entry,)


def test_serve_loader_propagates_partial_legacy_index(tmp_path: Path) -> None:
    cfg = load_config(None, environ={})
    index_dir = tmp_path / "partial-index"
    index_dir.mkdir()
    (index_dir / "tracks.db").touch()

    with pytest.raises(FileNotFoundError):
        _load_index_for_serve(cfg, active_dir=index_dir)


def test_serve_loader_propagates_missing_published_artifact(tmp_path: Path) -> None:
    cfg = load_config(None, environ={})
    entry = IndexEntry(
        path="song.flac",
        title="Song",
        artist="Artist",
        album="",
        genre="",
        bpm=0.0,
        year=0,
        length=1.0,
        energy=0.0,
        key=-1,
        mode=-1,
        tempo_confidence=0.0,
    )
    save_index([entry], np.zeros((1, FEATURE_DIM), dtype=np.float32), tmp_path)
    manifest = read_manifest(tmp_path)
    assert manifest is not None
    (tmp_path / manifest.vectors_file).unlink()

    with pytest.raises(FileNotFoundError):
        _load_index_for_serve(cfg, active_dir=tmp_path)


def test_serve_loader_accepts_tombstoned_index(tmp_path: Path) -> None:
    cfg = load_config(None, environ={})
    tombstone_publication(tmp_path)
    (tmp_path / "tracks.db").write_bytes(b"stale")
    (tmp_path / "vectors.index").write_bytes(b"stale")

    with patch(
        "autodj.similarity.SimilarityIndex.from_index_dir",
        side_effect=AssertionError("tombstoned cores must not be loaded"),
    ) as load_index:
        assert _load_index_for_serve(cfg, active_dir=tmp_path).ntotal == 0
    load_index.assert_not_called()


@pytest.mark.parametrize(
    "orphan_name",
    [
        "tracks.g00000000000000000001.db",
        "vectors.g00000000000000000001.index",
        ".index-manifest.json.0123456789abcdef0123456789abcdef.tmp",
        "..index-publication-state.json.0123456789abcdef0123456789abcdef.tmp",
        ".tracks.g00000000000000000001.db.0123456789abcdef0123456789abcdef.tmp",
        ".flat-migration-0123456789abcdef0123456789abcdef",
        "tracks.db-wal",
        "vectors.index.tmp",
    ],
)
def test_serve_loader_rejects_orphan_publication_artifact(
    tmp_path: Path,
    orphan_name: str,
) -> None:
    cfg = load_config(None, environ={})
    (tmp_path / orphan_name).touch()

    with pytest.raises(FileNotFoundError):
        _load_index_for_serve(cfg, active_dir=tmp_path)


def test_serve_loader_ignores_near_match_flat_migration_name(tmp_path: Path) -> None:
    cfg = load_config(None, environ={})
    (tmp_path / ".flat-migration-0123456789abcdef0123456789abcdeg").mkdir()

    assert _load_index_for_serve(cfg, active_dir=tmp_path).ntotal == 0


def test_serve_loader_rejects_uncommitted_generation_reservation(tmp_path: Path) -> None:
    cfg = load_config(None, environ={})
    (tmp_path / ".index-publication-state.json").write_text(
        '{"high_water": 1, "tombstone_revision": 0}',
        encoding="utf-8",
    )

    with pytest.raises(IndexConsistencyError, match="publication history"):
        _load_index_for_serve(cfg, active_dir=tmp_path)


def test_serve_loader_preserves_flat_legacy_migration(tmp_path: Path) -> None:
    cfg = load_config(None, environ={})
    entry = IndexEntry(
        path="song.flac",
        title="Song",
        artist="Artist",
        album="",
        genre="",
        bpm=0.0,
        year=0,
        length=1.0,
        energy=0.0,
        key=-1,
        mode=-1,
        tempo_confidence=0.0,
    )
    _save_vectors(np.zeros((1, FEATURE_DIM), dtype=np.float32), tmp_path)
    _save_tracks_metadata([entry], tmp_path, music_dir=None)

    sim = _load_index_for_serve(cfg, active_dir=tmp_path / "default")

    assert sim.ntotal == 1
    assert sim.entries_snapshot()[0].title == entry.title
    assert read_manifest(tmp_path / "default") is not None


def test_serve_loader_serializes_tombstone_between_check_and_load(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import autodj.index_manifest as manifest_module

    cfg = load_config(None, environ={})
    entry = IndexEntry(
        path="song.flac",
        title="Song",
        artist="Artist",
        album="",
        genre="",
        bpm=0.0,
        year=0,
        length=1.0,
        energy=0.0,
        key=-1,
        mode=-1,
        tempo_confidence=0.0,
    )
    save_index([entry], np.zeros((1, FEATURE_DIM), dtype=np.float32), tmp_path)
    original_factory = SimilarityIndex.from_index_dir
    original_lock = manifest_module.publication_lock
    loader_entered = threading.Event()
    release_loader = threading.Event()
    tombstone_attempted = threading.Event()
    tombstone_acquired = threading.Event()
    tombstone_done = threading.Event()
    loaded: list[SimilarityIndex] = []
    errors: list[BaseException] = []

    def gated_factory(*args, **kwargs) -> SimilarityIndex:
        loader_entered.set()
        assert release_loader.wait(timeout=2)
        return original_factory(*args, **kwargs)

    @contextmanager
    def observed_lock(index_dir: Path):
        if threading.current_thread().name == "test-tombstone":
            tombstone_attempted.set()
        with original_lock(index_dir):
            if threading.current_thread().name == "test-tombstone":
                tombstone_acquired.set()
            yield

    def load_for_serve() -> None:
        try:
            loaded.append(_load_index_for_serve(cfg, active_dir=tmp_path))
        except BaseException as exc:
            errors.append(exc)

    def tombstone() -> None:
        try:
            tombstone_publication(tmp_path)
        except BaseException as exc:
            errors.append(exc)
        finally:
            tombstone_done.set()

    monkeypatch.setattr(manifest_module, "publication_lock", observed_lock)
    with patch.object(SimilarityIndex, "from_index_dir", side_effect=gated_factory):
        loader_thread = threading.Thread(target=load_for_serve, name="test-serve-loader")
        tombstone_thread = threading.Thread(target=tombstone, name="test-tombstone")
        loader_thread.start()
        try:
            assert loader_entered.wait(timeout=2)
            tombstone_thread.start()
            assert tombstone_attempted.wait(timeout=2)
            assert not tombstone_acquired.wait(timeout=0.2)
        finally:
            release_loader.set()
            loader_thread.join(timeout=2)
            tombstone_thread.join(timeout=2)

    if errors:
        raise AssertionError("concurrent index operation failed") from errors[0]
    assert tombstone_done.is_set()
    assert len(loaded) == 1
    assert loaded[0].ntotal == 1
    assert not loader_thread.is_alive()
    assert not tombstone_thread.is_alive()


def test_serve_loader_classifies_failed_load_before_concurrent_publish(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import autodj.index_manifest as manifest_module

    cfg = load_config(None, environ={})
    original_lock = manifest_module.publication_lock
    load_failed = threading.Event()
    publisher_acquired = threading.Event()
    publish_done = threading.Event()
    lock_depth = threading.local()
    loaded: list[SimilarityIndex] = []
    errors: list[BaseException] = []
    entry = IndexEntry(
        path="song.flac",
        title="Song",
        artist="Artist",
        album="",
        genre="",
        bpm=0.0,
        year=0,
        length=1.0,
        energy=0.0,
        key=-1,
        mode=-1,
        tempo_confidence=0.0,
    )

    def failing_factory(*_args, **_kwargs) -> SimilarityIndex:
        load_failed.set()
        raise FileNotFoundError("simulated missing cores")

    @contextmanager
    def ordered_lock(index_dir: Path):
        is_loader = threading.current_thread().name == "test-failed-loader"
        depth = getattr(lock_depth, "value", 0)
        with original_lock(index_dir):
            lock_depth.value = depth + 1
            try:
                yield
            finally:
                lock_depth.value = depth
        if is_loader and depth == 0:
            assert publisher_acquired.wait(timeout=2)

    def load_for_serve() -> None:
        try:
            loaded.append(_load_index_for_serve(cfg, active_dir=tmp_path))
        except BaseException as exc:
            errors.append(exc)

    def publish() -> None:
        try:
            assert load_failed.wait(timeout=2)
            with ordered_lock(tmp_path):
                publisher_acquired.set()
                save_index(
                    [entry],
                    np.zeros((1, FEATURE_DIM), dtype=np.float32),
                    tmp_path,
                )
        except BaseException as exc:
            errors.append(exc)
        finally:
            publish_done.set()

    monkeypatch.setattr(manifest_module, "publication_lock", ordered_lock)
    with patch.object(SimilarityIndex, "from_index_dir", side_effect=failing_factory):
        loader_thread = threading.Thread(target=load_for_serve, name="test-failed-loader")
        publisher_thread = threading.Thread(target=publish, name="test-publisher")
        publisher_thread.start()
        loader_thread.start()
        loader_thread.join(timeout=3)
        publisher_thread.join(timeout=3)

    if errors:
        raise AssertionError("concurrent index operation failed") from errors[0]
    assert len(loaded) == 1
    assert loaded[0].ntotal == 0
    assert publish_done.is_set()
    assert SimilarityIndex.from_index_dir(tmp_path).ntotal == 1
    assert not loader_thread.is_alive()
    assert not publisher_thread.is_alive()


def test_player_waits_safely_for_first_index_generation() -> None:
    cfg = load_config(None, environ={})
    sim = SimilarityIndex.empty()
    player = Player(cfg, sim, dry_run=True, no_keyboard=True)
    wait_event = _WaitSignallingEvent()
    player._skip_event = wait_event  # type: ignore[assignment]
    errors: list[BaseException] = []

    def run_player() -> None:
        try:
            player.run(seed_entry=None)
        except BaseException as exc:
            errors.append(exc)

    thread = threading.Thread(target=run_player)
    thread.start()
    entered_wait = wait_event.wait_started.wait(timeout=2)
    try:
        player.stop()
    finally:
        thread.join(timeout=2)

    if errors:
        raise AssertionError("player thread failed") from errors[0]
    assert entered_wait
    assert not thread.is_alive()


def test_player_uses_first_published_generation_after_waiting(tmp_path: Path) -> None:
    cfg = load_config(None, environ={})
    sim = SimilarityIndex.empty()
    player = Player(cfg, sim, dry_run=True, no_keyboard=True)
    wait_event = _WaitSignallingEvent()
    player._skip_event = wait_event  # type: ignore[assignment]
    progressed = threading.Event()
    errors: list[BaseException] = []

    player.load_lyrics_in_background = lambda _path: progressed.set()  # type: ignore[method-assign]
    player.analyse_track_in_background = lambda _path: None  # type: ignore[method-assign]

    def run_player() -> None:
        try:
            player.run(seed_entry=None)
        except BaseException as exc:
            errors.append(exc)

    thread = threading.Thread(target=run_player)
    thread.start()
    main_error: BaseException | None = None
    advanced = False
    entries: list[IndexEntry] = []
    try:
        assert wait_event.wait_started.wait(timeout=2)
        entries = [
            IndexEntry(
                path=f"song-{index}.flac",
                title=f"Song {index}",
                artist="Artist",
                album="",
                genre="",
                bpm=0.0,
                year=0,
                length=1.0,
                energy=0.0,
                key=-1,
                mode=-1,
                tempo_confidence=0.0,
            )
            for index in range(2)
        ]
        vectors = np.zeros((2, FEATURE_DIM), dtype=np.float32)
        vectors[:, 0] = 1.0
        save_index(entries, vectors, tmp_path)
        manifest = read_manifest(tmp_path)
        assert manifest is not None
        assert sim.reload_from_disk(tmp_path, expected_generation=manifest.generation) == 2
        wait_event.set()
        advanced = progressed.wait(timeout=2)
    except BaseException as exc:
        main_error = exc
    finally:
        player.stop()
        thread.join(timeout=2)

    if errors:
        raise AssertionError("player thread failed") from errors[0]
    if main_error is not None:
        raise main_error
    assert advanced
    assert player._state.current_track is not None
    assert player._state.next_track is not None
    assert {player._state.current_track.path, player._state.next_track.path} == {
        entry.path for entry in entries
    }
    assert not thread.is_alive()


def test_healthz_is_minimal_and_reports_empty_library(bridge: PlayerBridge) -> None:
    bridge.sim = SimilarityIndex.empty()
    with TestClient(create_app(bridge)) as client:
        response = client.get("/healthz")
        assert response.status_code == 200
        assert response.json() == {"status": "ok", "tracks": 0}


def test_healthz_is_public_but_still_enforces_raw_host_and_origin(
    bridge: PlayerBridge,
) -> None:
    bridge.sim = SimilarityIndex.empty()
    bridge.player._cfg.server = ServerConfig(
        host="0.0.0.0",
        access_token="s" * 32,
        allowed_hosts=["radio.local"],
        allowed_origins=["http://radio.local:8080"],
    )
    with TestClient(
        create_app(bridge),
        base_url="http://radio.local:8080",
        headers={"Host": "radio.local"},
    ) as client:
        assert client.get("/healthz").status_code == 200
        duplicate_host = client.get(
            "/healthz",
            headers=[("Host", "radio.local"), ("Host", "evil.example")],
        )
        duplicate_origin = client.get(
            "/healthz",
            headers=[
                ("Origin", "http://radio.local:8080"),
                ("Origin", "http://evil.example"),
            ],
        )
        assert duplicate_host.status_code == 403
        assert duplicate_origin.status_code == 403


def test_serve_uses_empty_index_when_files_are_absent(tmp_path: Path) -> None:
    with (
        CliRunner().isolated_filesystem(temp_dir=tmp_path),
        patch("autodj.server.serve") as serve_mock,
    ):
        result = CliRunner().invoke(cli, ["serve", "--no-playback"])
    assert result.exit_code == 0, result.output
    assert serve_mock.call_args.kwargs["sim"].ntotal == 0
