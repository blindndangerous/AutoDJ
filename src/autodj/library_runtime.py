"""Server-owned libraries and station bindings, independent of browser selection."""

from __future__ import annotations

import threading
from dataclasses import asdict, replace
from pathlib import Path
from typing import Any

from autodj import index_manifest as im
from autodj._bridge import LibraryServices
from autodj.config import AutoDJConfig, LibraryConfig
from autodj.library_duplicate import snapshot_library
from autodj.library_registry import LibraryDefinition, LibraryRegistry, validate_library_id


class Libraries:
    """Keep each catalog, similarity view and scan alive for the server's lifetime."""

    def __init__(self, main: LibraryServices) -> None:
        self.root = main.cfg
        self.registry = LibraryRegistry(main.cfg.index.index_dir / "libraries")
        self.registry.ensure_main()
        self._lock = threading.RLock()
        self._services = {"main": main}
        self._bindings: dict[str, str] = {}
        self._owners: dict[str, Any] = {}
        self._stations: Any = None
        for definition in self.registry.list():
            self.get(definition.id)
        definitions = [
            (
                "default",
                (
                    self.root.default_station.cfg
                    if self.root.default_station is not None
                    else self.root
                ),
            )
        ]
        definitions.extend((spec.slug, spec.cfg) for spec in self.root.stations)
        for library_id, services in self._services.items():
            services.library.set_station_ids(
                {slug for slug, cfg in definitions if cfg.station.library_id == library_id}
            )

    def get(self, library_id: str) -> LibraryServices:
        """Resolve an existing ID; never substitute a different library."""
        library_id = validate_library_id(library_id)
        with self._lock:
            if library_id in self._services:
                return self._services[library_id]
            definition = self.registry.get(library_id)
            from autodj.library import LibraryManager
            from autodj.similarity import SimilarityIndex

            cfg = replace(
                self.root,
                index=replace(self.root.index, library_id=library_id),
                library=LibraryConfig.from_dict(definition.settings),
            )
            sim = SimilarityIndex.empty()
            manager = LibraryManager(cfg, sim)
            self._services[library_id] = LibraryServices(cfg, sim, manager)
            return self._services[library_id]

    def duplicate(self, source_id: str, library_id: str, name: str) -> LibraryDefinition:
        """Create a complete independent catalog copy from an existing library."""
        source_id = validate_library_id(source_id)
        source = self.get(source_id)
        library = source.library
        library_root = Path(library.cfg.index.library_dir)
        settings = {
            key: str(value) if isinstance(value, Path) else value
            for key, value in asdict(library.cfg.library).items()
        }

        def initialize(stage: Path) -> None:
            cache = library._dj_cache()
            if cache is not None:
                cache.flush(force=True)
            snapshot_library(library_root, stage)

        # Match backup's lock order. The source manager lock is intentionally
        # not held while SQLite snapshots the catalog and DJ metadata.
        with im.publication_lock(self.root.index.index_dir), im.publication_lock(library_root):
            return self.registry.create(
                library_id,
                name,
                settings,
                initializer=initialize,
            )

    def all(self) -> list[LibraryServices]:
        with self._lock:
            return list(self._services.values())

    def bind(self, slug: str, cfg: AutoDJConfig) -> LibraryServices:
        """Bind a stopped station before building its next engine or saved editors.

        Move its ledger object, retaining buffered history and clock settings.
        The station registry serializes starts, stops and saved-editor creation.
        """
        with self._lock:
            services = self.get(cfg.station.library_id)
            destination = services.library
            previous = self._bindings.get(slug)
            if previous != cfg.station.library_id:
                attached = [
                    (source.library, source.library.attached_ledger(slug))
                    for source in self._services.values()
                ]
                found = [(manager, ledger) for manager, ledger in attached if ledger is not None]
                if len(found) > 1:
                    raise RuntimeError(f"Station {slug} has multiple library owners")
                if found:
                    owner, ledger = found[0]
                    if owner is not destination:
                        # Flush can fail. Keep the previous owner until the new
                        # one has accepted the same ledger successfully.
                        destination.attach_station(ledger)
                else:
                    destination.station_ledger(slug)
                for manager, _ledger in attached:
                    if manager is not destination:
                        manager.detach_station(slug)
                self._bindings[slug] = cfg.station.library_id
            cfg.index = services.cfg.index
            cfg.library = services.cfg.library
            return services

    async def start(self, stations: Any = None) -> None:
        from autodj.stations import SharedLibrary

        if stations is not None:
            self._stations = stations
        for library_id, services in self._services.copy().items():
            if library_id not in self._owners:
                owner = SharedLibrary(services.library, self._stations)
                self._owners[library_id] = owner
                await owner.start()

    def stop_scan(self, timeout_s: float) -> None:
        for owner in self._owners.values():
            owner.stop_scan(timeout_s)

    async def close(
        self, writers_quiesced: bool, *, timeout_s: float, readers_stopped: bool = True
    ) -> bool:
        results = []
        for owner in self._owners.values():
            results.append(
                await owner.close(
                    writers_quiesced, timeout_s=timeout_s, readers_stopped=readers_stopped
                )
            )
        return all(results)
