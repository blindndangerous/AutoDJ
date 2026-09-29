import threading
from pathlib import Path
from unittest.mock import patch

import numpy as np
import pytest
from click.testing import CliRunner
from fastapi.testclient import TestClient

from autodj.cli import _load_index_for_serve, cli
from autodj.config import ServerConfig, load_config
from autodj.index_manifest import IndexConsistencyError, UnsupportedIndexError, read_manifest
from autodj.indexer import FEATURE_DIM, IndexEntry, save_index
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
    vectors = np.zeros((1, FEATURE_DIM), dtype=np.float32)
    vectors[0, 0] = 1.0
    manifest = save_index([entry], vectors, tmp_path, base_generation=0)
    assert sim.reload_from_disk(tmp_path) == 1
    assert sim.manifest == manifest
    assert sim.ntotal == 1
    assert sim.entries_snapshot() == (entry,)


def test_serve_loader_refuses_index_without_manifest(tmp_path: Path) -> None:
    cfg = load_config(None, environ={})
    index_dir = tmp_path / "old-index"
    index_dir.mkdir()
    (index_dir / "tracks.db").touch()

    with pytest.raises(UnsupportedIndexError, match=r"Rebuild it with `autodj index --force`"):
        _load_index_for_serve(cfg, active_dir=index_dir)


def test_serve_loader_refuses_a_manifest_whose_file_is_missing(tmp_path: Path) -> None:
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
    save_index([entry], np.zeros((1, FEATURE_DIM), dtype=np.float32), tmp_path, base_generation=0)
    manifest = read_manifest(tmp_path)
    assert manifest is not None
    (tmp_path / manifest.vectors_file).unlink()

    with pytest.raises(IndexConsistencyError, match="is missing; copy the index again"):
        _load_index_for_serve(cfg, active_dir=tmp_path)


@pytest.mark.parametrize(
    "orphan_name",
    ["tracks.g00000000000000000001.db", "vectors.g00000000000000000001.index"],
)
def test_serve_loader_names_generation_files_without_manifest(
    tmp_path: Path,
    orphan_name: str,
) -> None:
    """A lost manifest is not an empty index: say so and name the rebuild."""
    cfg = load_config(None, environ={})
    (tmp_path / orphan_name).touch()

    with pytest.raises(IndexConsistencyError, match=r"no index-manifest\.json.*index --force"):
        _load_index_for_serve(cfg, active_dir=tmp_path)


def test_player_waits_safely_for_first_index_generation() -> None:
    cfg = load_config(None, environ={})
    sim = SimilarityIndex.empty()
    player = Player(cfg, sim, dry_run=True)
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
    player = Player(cfg, sim, dry_run=True)
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
        save_index(entries, vectors, tmp_path, base_generation=0)
        assert sim.reload_from_disk(tmp_path) == 2
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
        result = CliRunner().invoke(cli, ["serve"])
    assert result.exit_code == 0, result.output
    assert serve_mock.call_args.kwargs["sim"].ntotal == 0
