"""A small station on disk for the rotation tester's tests (#125).

The library is a stand-in (no SQLite catalog, no audio), but the
categories, their M3U playlists, the clockwheels and the configuration
are the real ones, read from a temporary index folder.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Sequence
from pathlib import Path
from typing import Any

import faiss
import numpy as np

from autodj.categories import Category, CategoryIndex, CategoryStore, store_path
from autodj.clockwheel_store import ClockwheelStore
from autodj.config import load_config
from autodj.indexer import FEATURE_DIM, IndexEntry
from autodj.player import Player
from autodj.rotation_sim import SimulationSource
from autodj.similarity import SimilarityIndex
from autodj.stations import StationPaths

MUSIC = "/music"
LINERS = "/liners"


def song(
    number: int,
    artist: str,
    *,
    album: str = "",
    title: str = "",
    length: float = 200.0,
    folder: str = "main",
) -> IndexEntry:
    """A song at ``/music/<folder>/<number>.flac``."""
    return IndexEntry(
        path=f"{MUSIC}/{folder}/{number:06d}.flac",
        title=title or f"Song {number}",
        artist=artist,
        album=album or f"{artist} album",
        genre="Pop",
        bpm=120.0,
        year=2000,
        length=length,
        energy=0.1,
        key=0,
        mode=1,
        tempo_confidence=0.8,
    )


def liner(number: int, length: float = 6.0) -> IndexEntry:
    """A station ID at ``/liners/<number>.mp3``."""
    return IndexEntry(
        path=f"{LINERS}/{number:03d}.mp3",
        title=f"ID {number}",
        artist="",
        album="",
        genre="",
        bpm=0.0,
        year=0,
        length=length,
        energy=0.1,
        key=-1,
        mode=-1,
        tempo_confidence=0.0,
    )


class FakeCatalog:
    """The catalog calls the picker makes: settings, sources and saved points."""

    def __init__(self) -> None:
        self.settings: dict[str, Any] = {}
        self.writes = 0

    def get_setting(self, key: str, default: Any = None) -> Any:
        return self.settings.get(key, default)

    def set_setting(self, key: str, value: Any) -> None:
        self.writes += 1
        self.settings[key] = value

    def sources(self) -> list[dict[str, str]]:
        return [
            {"id": "music", "path": MUSIC, "kind": "music"},
            {"id": "liners", "path": LINERS, "kind": "liner"},
        ]

    def points(self, path: str) -> dict[str, Any]:
        return {}

    def record_for_path(self, path: str) -> None:
        return None


class FakeLedger:
    """A station's last-played times; counts any write so a test can say there was none."""

    def __init__(self, catalog: Any = None, played: dict[str, float] | None = None) -> None:
        self._catalog = catalog
        self.played = dict(played or {})
        self.writes = 0

    def get_setting(self, name: str, default: Any = None) -> Any:
        if self._catalog is None:
            return default
        return self._catalog.get_setting(self.setting_key(name), default)

    def setting_key(self, name: str) -> str:
        return name

    def last_played(self) -> dict[str, float]:
        return dict(self.played)

    def remember_play(self, path: str, timestamp: float | None = None) -> None:
        self.writes += 1

    def remember_setting(self, name: str, value: Any) -> None:
        self.writes += 1

    def recent_plays(self, limit: int) -> list[dict]:
        return []

    def pending_history(self) -> list[dict]:
        return []


class FakeLibrary:
    """The shared library calls the picker makes, over fixed entries."""

    def __init__(self, cfg: Any, music: Sequence[IndexEntry], liners: Sequence[IndexEntry]):
        self._music = tuple(music)
        self._liners = tuple(liners)
        self._by_path = {entry.path: entry for entry in (*self._music, *self._liners)}
        self.catalog = FakeCatalog()
        self.categories = CategoryIndex(cfg, lambda: self._music)
        self.ledger = FakeLedger(self.catalog)

    def entries_snapshot(self, kind: str = "music") -> tuple[IndexEntry, ...]:
        if kind == "music":
            return self._music
        return self._liners if kind == "liner" else ()

    def entry_for_path(self, path: str) -> IndexEntry | None:
        return self._by_path.get(path)

    def record_for_id(self, record_id: str) -> None:
        return None


def _toml(value: Any) -> str:
    """*value* as a TOML value; a dict becomes an inline table."""
    if isinstance(value, dict):
        return "{ " + ", ".join(f"{key} = {_toml(item)}" for key, item in value.items()) + " }"
    return json.dumps(value)


def similarity_index(entries: Sequence[IndexEntry]) -> SimilarityIndex:
    """A FAISS index over *entries* with deterministic vectors."""
    rng = np.random.default_rng(7)
    vectors = rng.standard_normal((len(entries), FEATURE_DIM)).astype(np.float32)
    vectors /= np.linalg.norm(vectors, axis=1, keepdims=True)
    index = faiss.IndexFlatIP(FEATURE_DIM)
    index.add(vectors)
    return SimilarityIndex(faiss_index=index, entries=list(entries))


def make_station(
    root: Path,
    music: Sequence[IndexEntry],
    *,
    liners: Sequence[IndexEntry] = (),
    categories: Iterable[dict[str, Any]] = (),
    clockwheels: Iterable[dict[str, Any]] = (),
    active_clock: str | None = None,
    playback: dict[str, Any] | None = None,
    dmca: bool = False,
    events: Sequence[Any] = (),
    timezone: str = "UTC",
    similarity: bool = False,
    related: dict[str, list[str]] | None = None,
    related_tracks: dict[str, list[str]] | None = None,
) -> SimulationSource:
    """A station in *root*: config, categories (each with ``members``), clockwheels.

    Each category dict holds the :class:`~autodj.categories.Category`
    fields plus ``members``, the songs its M3U playlist lists.
    """
    playback = {"transition_mode": "saved_points", **(playback or {})}
    text = "\n".join(
        [
            "[index]",
            f'index_dir = "{root.as_posix()}"',
            "[server]",
            f'station_timezone = "{timezone}"',
            "[playback]",
            *(f"{key} = {_toml(value)}" for key, value in playback.items()),
            "[dmca]",
            f"enabled = {_toml(dmca)}",
            "",
        ]
    )
    root.mkdir(parents=True, exist_ok=True)
    config_file = root / "config.toml"
    config_file.write_text(text, encoding="utf-8")
    cfg = load_config(config_file, environ={})
    cfg.index.active_dir.mkdir(parents=True, exist_ok=True)
    if related_tracks is not None:
        groups = [f"{json.dumps(name)} = {json.dumps(to)}" for name, to in related_tracks.items()]
        (cfg.index.active_dir / "related-tracks.toml").write_text(
            "\n".join(["[related]", *groups, ""]), encoding="utf-8"
        )
    if related is not None:
        links = [f"{json.dumps(name)} = {json.dumps(to)}" for name, to in related.items()]
        (cfg.index.active_dir / "related-artists.toml").write_text(
            "\n".join(["single_acts = []", "[related]", *links, ""]), encoding="utf-8"
        )
    saved = []
    playlists = cfg.index.active_dir / "playlists"
    playlists.mkdir(exist_ok=True)
    for spec in categories:
        spec = dict(spec)
        members = spec.pop("members")
        name = f"{spec['id']}.m3u"
        (playlists / name).write_text(
            "\n".join(entry.path for entry in members) + "\n", encoding="utf-8"
        )
        saved.append(Category.model_validate({"source": "m3u", "playlist": name, **spec}))
    if saved:
        CategoryStore(store_path(cfg)).save(saved)
    store = ClockwheelStore(StationPaths.default(cfg).clockwheels)
    for clock in clockwheels:
        store.save({"version": 1, "resync_minutes": [0], **clock})
    library = FakeLibrary(cfg, music, liners)
    if active_clock:
        library.catalog.settings["clock"] = {"name": active_clock, "cursor": 0}
    sim = similarity_index(music) if similarity else SimilarityIndex.empty()
    player = Player(cfg, sim, dry_run=True)
    player._library = library
    player._pure_shuffle = cfg.playback.pick_mode == "random"
    player.configure_clock()
    return SimulationSource(
        player=player,
        library=library,
        station="default",
        ledger=library.ledger,
        events=events,
        timezone=timezone,
    )


def category_step(category_id: str, *, enforce_rules: bool = True) -> dict[str, Any]:
    return {
        "type": "category",
        "category": category_id,
        "selection": "least_recent",
        "enforce_rules": enforce_rules,
    }


def liner_step() -> dict[str, Any]:
    return {
        "type": "liner",
        "source": "liners",
        "folder": "",
        "recursive": True,
        "selection": "random",
        "enforce_rules": False,
    }
