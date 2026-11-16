"""Read-only checks before a station changes music library."""

from __future__ import annotations

import json
from typing import Any

from autodj.clockwheel_store import (
    ClockwheelStore,
    resolve_conditions,
    source_folder,
    step_categories,
    step_source,
)


def library_warnings(station: Any, library: Any) -> list[str]:
    """Describe unavailable queue and programming references without rewriting them."""
    warnings: list[str] = []
    snapshot = getattr(station.bridge, "_queue_snapshot", None)
    if snapshot is not None:
        missing = sum(library.entry_for_path(entry.path) is None for entry in snapshot())
        if missing:
            warnings.append(
                f"{missing} queued {'track is' if missing == 1 else 'tracks are'} absent from this library."
            )
    from autodj.categories import CategoryIndex, store_path

    category_index = CategoryIndex(
        library.cfg,
        library.entries_snapshot,
        library.catalog.sources,
        path=store_path(library.cfg, station.paths),
        beets=library.clock_beets,
        repair_invalid=False,
    )
    try:
        categories, results = category_index.resolutions()
    except (OSError, ValueError) as exc:
        warnings.append(f"Could not check categories: {exc}")
        categories, results = (), {}
    known = {category.id for category in categories}
    for category in categories:
        result = results[category.id]
        if result.error:
            warnings.append(f"{category.name}: {result.error}")
        elif not result.count:
            warnings.append(f"{category.name}: no matching tracks.")
    sources = {item["id"]: item for item in library.catalog.sources()}
    store = ClockwheelStore(station.paths.clockwheels)
    clock_names = store.list_names()
    for name in clock_names:
        try:
            steps = store.load(name)["steps"]
        except ValueError as exc:
            warnings.append(str(exc))
            continue
        for number, step in enumerate(steps, 1):
            try:
                if step["type"] == "track":
                    source = step_source(step, sources, number)
                    if library.entry_for_path(source_folder(source["path"], step["path"])) is None:
                        raise ValueError("track is absent from this library")
                elif step["type"] in {"music", "liner"}:
                    resolve_conditions(step, sources, number)
                if step_categories(step) - known:
                    raise ValueError("category is unavailable")
            except ValueError as exc:
                warnings.append(f"{name}, step {number}: {exc}")

    from autodj.shows import ShowStore

    shows = ShowStore(station.paths.shows)
    try:
        saved_shows = shows.all(repair_invalid=False)
    except (OSError, ValueError) as exc:
        warnings.append(f"Could not check saved shows: {exc}")
    else:
        for show in saved_shows.values():
            if show.source == "category" and show.category not in known:
                warnings.append(f"{show.name}: category is unavailable.")
            if show.source == "clockwheel" and show.clockwheel.casefold() not in {
                name.casefold() for name in clock_names
            }:
                warnings.append(f"{show.name}: clockwheel is unavailable.")
            missing = sum(library.entry_for_path(path) is None for path in show.songs)
            if missing:
                warnings.append(
                    f"{show.name}: {missing} saved {'track is' if missing == 1 else 'tracks are'} absent from this library."
                )
        if problem := shows.problem():
            warnings.append(problem)

    from autodj.listener_requests import PENDING, STATE_VERSION, ListenerRequest

    try:
        desk = getattr(station, "requests", None)
        if desk is not None:
            pending = desk.book.pending()
        elif station.paths.listener_requests.exists():
            raw = json.loads(station.paths.listener_requests.read_text(encoding="utf-8"))
            if not isinstance(raw, dict) or raw.get("version") != STATE_VERSION:
                raise ValueError("unknown listener request file version")
            pending = [ListenerRequest(**item) for item in raw.get("requests", [])]
        else:
            pending = []
        missing = sum(
            item.status == PENDING and library.entry_for_path(item.path) is None for item in pending
        )
        if missing:
            warnings.append(
                f"{missing} pending {'request refers' if missing == 1 else 'requests refer'} to tracks absent from this library."
            )
    except (OSError, TypeError, ValueError) as exc:
        warnings.append(f"Could not check pending requests: {exc}")
    return warnings
