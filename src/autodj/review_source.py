"""Capture planned review inputs through the station's isolated forecast paths."""

from __future__ import annotations

import copy
import hashlib
import random
import time
from collections import deque
from collections.abc import Callable, Mapping, Sequence
from dataclasses import asdict, dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from zoneinfo import ZoneInfo

from autodj import hour_preview
from autodj.fsutil import file_signature
from autodj.hour_preview import Decided, Item
from autodj.review_jobs import ReviewError
from autodj.rotation_sim import SimulationSource, _copy_player, snapshot_player, snapshot_settings


@dataclass(frozen=True)
class ReviewPlan:
    source: SimulationSource
    items: tuple[Item, ...]
    start: float
    fingerprint: str
    request: dict[str, Any]
    notes: tuple[str, ...] = ()


def _start(value: Any, timezone: str) -> float:
    if not value:
        return time.time()
    try:
        parsed = value if isinstance(value, datetime) else datetime.fromisoformat(str(value))
        if parsed.tzinfo is not None:
            return parsed.timestamp()
        zone = ZoneInfo(timezone)
        candidates = {
            local.timestamp()
            for fold in (0, 1)
            if (local := parsed.replace(tzinfo=zone, fold=fold))
            .astimezone(UTC)
            .astimezone(zone)
            .replace(tzinfo=None)
            == parsed
        }
        if len(candidates) != 1:
            raise ReviewError(
                "Start time is ambiguous or does not exist; include an explicit UTC offset"
            )
        return candidates.pop()
    except (ValueError, OverflowError, OSError) as exc:
        raise ReviewError(
            "Start time is invalid, ambiguous or does not exist; include an explicit UTC offset"
        ) from exc


def _show(station: Any, request: Mapping[str, Any]) -> Any:
    store = getattr(station, "shows", None)
    show = store.get(str(request.get("show_id", ""))) if store is not None else None
    if show is None:
        raise ReviewError("That saved show is no longer available")
    return show


def _signature(path: Path) -> Any:
    try:
        return file_signature(path)
    except OSError:
        return "unavailable"


def _fingerprint_base(station: Any) -> str:
    library = station.bridge.library
    selected = {
        entry.path for kind in ("music", "liner") for entry in library.entries_snapshot(kind)
    }
    catalog = library.catalog
    versions = getattr(catalog, "change_versions", lambda: None)()
    cfg = (
        station.cfg.model_dump(mode="json")
        if hasattr(station.cfg, "model_dump")
        else repr(station.cfg)
    )
    schedule = getattr(station, "schedule", None)
    events = list(schedule.events.values()) if schedule is not None else []
    content: list[tuple[str, Any]] = []
    for name in (
        "schedule",
        "liner_dates",
        "matched_intros",
        "ads",
        "time_check_settings",
        "shows",
        "clockwheels",
        "intros",
        "time_checks",
        "speech",
        "liners",
        "ad_spots",
        "cart_uploads",
        "carts",
        "announcements",
        "time_check_clips",
        "time_checks_cache",
    ):
        path = getattr(station.paths, name, None)
        if path is None:
            continue
        path = Path(path)
        if path.is_dir():
            content.extend(
                (str(child), _signature(child))
                for child in sorted(path.rglob("*"))
                if child.is_file()
            )
        else:
            content.append((str(path), _signature(path)))
    rules = Path(station.paths.state_dir) / "intro_rules.json"
    content.append((str(rules), _signature(rules)))
    checks = getattr(station, "time_checks", None)
    for pack in getattr(checks, "packs", {}).values():
        folder = Path(pack.path)
        content.extend(
            (str(child), _signature(child))
            for child in sorted(folder.rglob("*"))
            if child.is_file()
        )
    records = [
        (path, _signature(Path(path)), catalog.record_for_path(path)) for path in sorted(selected)
    ]
    data = (cfg, versions, content, records, [event.model_dump(mode="json") for event in events])
    return hashlib.sha256(repr(data).encode()).hexdigest()


def source_fingerprint(
    station: Any,
    request: Mapping[str, Any],
    paths: Sequence[str] = (),
    *,
    include_live_state: bool = True,
    shared: dict | None = None,
) -> str:
    """Hash inputs, sharing library reads only within one result-list request."""
    if request.get("source") == "edition":
        from autodj.prepared_review import edition_fingerprint

        return edition_fingerprint(station, dict(request))
    if station.bridge.library is None:
        raise ReviewError("The review needs the music library")
    if shared is None:
        shared = {}
    if "base" not in shared:
        shared["base"] = _fingerprint_base(station)
    show = _show(station, request) if request.get("source") == "show" else None
    player = getattr(station, "player", None)
    queue_state = None
    if include_live_state and player is not None and request.get("source") in {"queue", "forecast"}:
        with player._state.queue_lock:
            queue = tuple(entry.path for entry in player._state.queue)
        queue_state = (
            queue,
            getattr(player._state.current_track, "path", None),
            getattr(player._state.next_track, "path", None),
            getattr(getattr(player, "_pending_entry", None), "path", None),
            repr(getattr(player, "_clock", None)),
            getattr(player, "_cursor_serial", None),
        )
    metadata_paths = request.get("_review_dj_paths")
    if metadata_paths is None and show is not None:
        metadata_paths = show.songs
    if metadata_paths is not None:
        from autodj.review_render import _Metadata

        entries = [SimpleNamespace(path=path) for path in sorted(set(metadata_paths))]
        metadata = _Metadata(
            getattr(player, "_dj_cache", None), entries, station.cfg, shared=shared
        )
        dj_values: Any = [(entry.path, asdict(metadata.get(entry.path))) for entry in entries]
    else:
        # Before forecast capture the selected tracks are unknown. Detect edits
        # without decoding every beat grid or warming the live metadata cache.
        database = station.cfg.index.library_dir / "dj_meta.db"
        cache = getattr(player, "_dj_cache", None)
        pending = None
        if cache is not None:
            with cache._lock:
                pending = repr(cache._buf)
        dj_values = (_signature(database), _signature(Path(str(database) + "-wal")), pending)
    data = (
        dict(request),
        shared["base"],
        show.model_dump(mode="json") if show is not None else None,
        queue_state,
        dj_values,
    )
    return hashlib.sha256(repr(data).encode()).hexdigest()


class _ReadOnlyCatalog:
    """Keep the picker's catalog reads while suppressing decode measurements."""

    def __init__(self, real: Any) -> None:
        self._real = real

    def __getattr__(self, name: str) -> Any:
        return getattr(self._real, name)

    def record_for_path(self, path: str) -> Any:
        return copy.deepcopy(self._real.record_for_path(path))

    def points(self, path: str) -> Any:
        return copy.deepcopy(self._real.points(path))

    def set_loudness(self, path: Path, measurement: Any) -> None:
        pass


def _source(station: Any, start: float) -> SimulationSource:
    """Use saved content without constructing a bus, starting a station or booking ads."""
    source = hour_preview._source(station)
    live = getattr(station, "player", None)
    snapshot = (
        snapshot_player(live)
        if live is not None
        else snapshot_settings(station.bridge, station.ledger)
    )
    source.snapshot = snapshot
    shows = getattr(station, "shows", None)
    if shows is not None:
        from autodj.shows import preview_shows

        source.shows = preview_shows(
            shows,
            getattr(station, "show_runner", None),
            source.events,
            source.library.entry_for_path,
        )
    source.library = copy.copy(source.library)
    source.library.catalog = _ReadOnlyCatalog(source.library.catalog)
    player = _copy_player(source, snapshot, start, random.Random(0), lambda *_: None)
    if live is not None:
        player._dj_cache = getattr(live, "_dj_cache", None)
    source.player = player
    # Only a server-mix capability marker; never a bus or output.
    player.bus = cast(Any, object())
    intros = getattr(station, "intros", None) or getattr(live, "matched_intros", None)
    player.matched_intros = intros.snapshot() if intros is not None else None
    from autodj.liner_scheduler import LinerScheduler

    def registered() -> list[tuple[str, Path]]:
        result = []
        for entry in source.library.entries_snapshot("liner"):
            record = source.library.catalog.record_for_path(entry.path)
            if record is not None:
                result.append((str(record["id"]), Path(entry.path)))
        return result

    def decode(path: Path) -> Any:
        from autodj.stereo import SAMPLE_RATE

        entry = source.library.entry_for_path(str(path))
        if entry is None:
            raise ValueError("The liner is no longer registered")
        points = player.saved_points(entry)
        first = round(float(points.get("start") or 0) * SAMPLE_RATE)
        last = round(float(points.get("end") or entry.length or 0) * SAMPLE_RATE)
        return player._load_playout_audio(entry)[first:last]

    live_scheduler = getattr(live, "liner_scheduler", None)
    if live_scheduler is None:
        player.liner_scheduler = LinerScheduler(
            player._cfg.playback,
            station.paths.liners,
            None,
            clock=lambda: start,
            rng=random.Random(0),
            vocal_start=player.vocal_start_s,
            registered_liners=registered,
            path_decoder=decode,
        )
    else:
        player.liner_scheduler = live_scheduler.snapshot(
            clock=lambda: start, wall_clock=lambda: start, cfg=player._cfg
        )
        player.liner_scheduler._path_decoder = decode
        player.liner_scheduler._registered_liners = registered
        player.liner_scheduler._vocal_start = player.vocal_start_s
    if source.carts is not None:
        source.carts = copy.copy(source.carts)
        source.carts.bridge = SimpleNamespace(
            library=source.library, liner_scheduler=player.liner_scheduler
        )
    gate = getattr(station, "liner_gate", None)
    if gate is not None:
        player.liner_scheduler.play_dates = gate.refusal
        player.liner_scheduler.play_dates_at = gate.refusal
    existing_ads = getattr(station, "ads", None)
    if existing_ads is not None:
        from autodj.ads import AdDesk

        source.ads = AdDesk(
            lambda: player._cfg.ads,
            existing_ads.store,
            existing_ads.log,
            source.timezone,
            player=player,
            breaks=None,
            events=lambda: source.events,
            clock=lambda: start,
        )
        source.ads._heard = deque(existing_ads.heard_snapshot())
    return source


def _finite(
    source: SimulationSource, entries: Sequence[Any], start: float, cancelled: Callable[[], bool]
) -> tuple[Item, ...]:
    if not entries:
        raise ReviewError("There are no programme items to review")
    if cancelled():
        raise InterruptedError("Review interrupted")
    liner_paths = {entry.path for entry in source.library.entries_snapshot("liner")}
    queued_entries = list(entries)
    trailing: list[Any] = []
    while queued_entries and queued_entries[-1].path in liner_paths:
        trailing.insert(0, queued_entries.pop())
    waiting = tuple(
        Item("liner" if entry.path in liner_paths else "song", start, 0, "queued", entry)
        for entry in queued_entries
    )
    decided = Decided(now=start, last=None, handoff=start, waiting=waiting)
    end = start + sum(max(1, float(entry.length or 0)) for entry in queued_entries) + 3600
    ad_runs = hour_preview._ad_forecasts(source, start, end)
    assert source.snapshot is not None
    rows, ends, stopped = hour_preview.predict(
        source,
        source.snapshot,
        decided,
        end,
        seed=0,
        budget=120,
        limit=sum(entry.path not in liner_paths for entry in queued_entries),
        ad_runs=ad_runs,
        capture_audio=True,
        cancelled=cancelled,
    )
    if cancelled():
        raise InterruptedError("Review interrupted")
    if stopped and stopped != "it reached its limit of songs":
        raise ReviewError(f"The programme could not be captured: {stopped}")
    from autodj.stereo import SAMPLE_RATE

    for entry in trailing:
        if cancelled():
            raise InterruptedError("Review interrupted")
        points = source.player.saved_points(entry)
        first = round(float(points.get("start") or 0) * SAMPLE_RATE)
        last = round(float(points.get("end") or entry.length or 0) * SAMPLE_RATE)
        try:
            from autodj.stereo import copy_audio

            audio = copy_audio(source.player._load_playout_audio(entry)[first:last])
            length = len(audio) / SAMPLE_RATE
            rows.append(Item("liner", ends, length, "queued", entry, audio=audio))
            ends += length
        except Exception:
            rows.append(
                Item(
                    "liner",
                    ends,
                    0.0,
                    "queued",
                    entry,
                    label="Unavailable queued clip",
                    duration_unknown=True,
                )
            )
    return tuple(rows)


def capture_review(
    station: Any, request: Mapping[str, Any], *, cancelled: Callable[[], bool] = lambda: False
) -> ReviewPlan:
    parameters: dict[str, Any] = copy.deepcopy(dict(request))
    kind = parameters.get("source")
    if kind == "edition":
        from autodj.prepared_review import capture_edition

        return capture_edition(station, parameters, cancelled=cancelled)
    if kind not in {"show", "queue", "forecast"}:
        raise ReviewError("Choose a saved show, queue or forecast")
    if cancelled():
        raise InterruptedError("Review interrupted")
    start = _start(parameters.get("start"), station.timezone)
    before = source_fingerprint(station, parameters)
    source = _source(station, start)
    assert source.snapshot is not None
    notes: list[str] = []
    unavailable: set[str] = set()
    if kind == "show":
        show = _show(station, parameters)
        entries: list[Any] = []
        template = next(iter(source.library.entries_snapshot()), None)
        for path in show.songs:
            entry = source.library.entry_for_path(path)
            if entry is None and template is not None:
                entry = replace(template, path=path, title="Unavailable saved item", length=0)
            if entry is None:
                raise ReviewError("The saved programme has no registered audio to review")
            if source.library.entry_for_path(path) is None or not Path(path).is_file():
                unavailable.add(path)
                notes.append(
                    f"Saved programme item {len(entries) + 1} is unavailable; its duration is unknown"
                )
            entries.append(entry)
        rows = _finite(source, entries, start, cancelled)
    elif kind == "queue":
        if station.player is None:
            raise ReviewError("The current queue requires a running station; choose a saved show")
        decided = hour_preview.read_station(
            station.player, getattr(station, "breaks", None), now=start, capture_audio=True
        )
        rows = decided.items
        if not rows:
            with station.player._state.queue_lock:
                entries = list(station.player._state.queue)
            rows = _finite(source, entries, start, cancelled)
        else:
            # A hard event invalidates rendered successors, but retains the
            # explicitly selected queue sequence for a fresh isolated handoff.
            interrupted = hour_preview._before_interrupt(source, decided, start + 86400)
            if interrupted is not decided:
                fixed = interrupted.items
                tail = tuple(
                    row
                    for row in rows
                    if row.starts >= interrupted.handoff
                    and row.kind in {"song", "liner"}
                    and not row.in_handoff
                )
                pending = interrupted
            else:
                first = next(
                    (i for i, row in enumerate(rows) if row.status in {"queued", "show"}), len(rows)
                )
                fixed, tail = rows[:first], rows[first:]
                previous = next((row for row in reversed(fixed) if row.kind == "song"), None)
                pending = (
                    replace(
                        decided, last=previous.entry, handoff=previous.starts + previous.seconds
                    )
                    if previous is not None
                    else decided
                )
            trailing: list[Item] = []
            pending_rows = list(tail)
            while pending_rows and pending_rows[-1].kind == "liner":
                trailing.insert(0, pending_rows.pop())
            if pending_rows or interrupted is not decided:
                pending = replace(
                    pending,
                    items=tuple(fixed),
                    waiting=tuple(pending_rows),
                    unplayed=tuple(
                        (row.entry, row.starts)
                        for row in fixed
                        if row.entry is not None and row.status != "on_air"
                    ),
                )
                predicted, ends, stopped = hour_preview.predict(
                    source,
                    source.snapshot,
                    pending,
                    start + 86400,
                    seed=0,
                    budget=120,
                    limit=sum(row.kind == "song" for row in pending_rows),
                    ad_runs=hour_preview._ad_forecasts(source, start, start + 86400),
                    capture_audio=True,
                    cancelled=cancelled,
                )
                if cancelled():
                    raise InterruptedError("Review interrupted")
                if stopped and stopped != "it reached its limit of songs":
                    raise ReviewError(f"The queue could not be captured: {stopped}")
                rows = (*fixed, *predicted)
            else:
                rows = tuple(fixed)
                ends = pending.handoff
            if trailing:
                rows = (*rows, *_finite(source, [row.entry for row in trailing], ends, cancelled))
    else:
        if station.player is None:
            raise ReviewError("A forecast requires a running station; choose a saved show")
        rows_list: list[Item] = []
        minutes = int(parameters.get("minutes", 60))
        if not 1 <= minutes <= 180:
            raise ReviewError("Forecast length must be between 1 and 180 minutes")
        # The existing public forecast has a 15-minute minimum; crop its rich rows for shorter reviews.
        view = hour_preview.build_preview(
            source,
            max(15, minutes),
            now=start,
            decided=hour_preview.read_station(
                station.player,
                getattr(station, "breaks", None),
                now=start,
                show=hour_preview._on_air_show(source),
                capture_audio=True,
            ),
            budget=120,
            capture_audio=True,
            on_items=rows_list.extend,
            cancelled=cancelled,
        )
        rows = tuple(row for row in rows_list if row.starts < start + minutes * 60)
        if view.get("reason"):
            raise ReviewError(str(view["reason"]))
        if view.get("stopped"):
            notes.append(view["stopped"])
    if cancelled():
        raise InterruptedError("Review interrupted")
    if source_fingerprint(station, parameters) != before:
        raise ReviewError("Programme inputs changed while capturing; generate the review again")
    named = []
    for number, row in enumerate(rows):
        if row.entry is not None and row.entry.path in unavailable:
            row = replace(
                row,
                kind="break",
                entry=None,
                label="Unavailable saved programme item",
                duration_unknown=True,
                incoming_handoff=None,
            )
        named.append(replace(row, occurrence_id=row.occurrence_id or f"item-{number}"))
    named.sort(key=lambda row: row.starts)
    origin = min((row.starts for row in named), default=start)
    parameters["_review_dj_paths"] = sorted(
        {
            row.entry.path
            for row in named
            if row.entry is not None and not row.in_handoff and row.kind in {"song", "liner"}
        }
    )
    captured_fingerprint = source_fingerprint(station, parameters)
    return ReviewPlan(source, tuple(named), origin, captured_fingerprint, parameters, tuple(notes))


# Public name used by routes when checking retained snapshots.
fingerprint = source_fingerprint
