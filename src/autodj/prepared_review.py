"""Adapt prepared editions to the shared isolated review engine."""

from __future__ import annotations

import hashlib
from collections.abc import Sequence
from dataclasses import replace
from typing import Any

from autodj.prepared_editions import edition_store
from autodj.review_jobs import ReviewError


def edition_fingerprint(
    station: Any,
    request: dict[str, Any],
    *,
    paths: Sequence[str] = (),
    shared: dict | None = None,
) -> str:
    from autodj.review_source import _signature, source_fingerprint

    edition = edition_store(station).get(request["edition_id"])
    inputs = {**request, "source": "forecast"}
    inputs.setdefault(
        "_review_dj_paths", sorted({row.path for row in edition.occurrences if row.kind == "song"})
    )
    base = source_fingerprint(station, inputs, paths, include_live_state=False, shared=shared)
    assets = [
        (
            take.asset_id,
            _signature(station.paths.edition_audio / edition.assets[take.asset_id].file),
        )
        for take in edition.takes
    ]
    return hashlib.sha256((base + edition.model_dump_json() + repr(assets)).encode()).hexdigest()


def capture_edition(station: Any, request: dict[str, Any], *, cancelled: Any) -> Any:
    from autodj.review_source import (
        ReviewPlan,
        _check_programme_length,
        _finite,
        _freeze_review_inputs,
        _source,
        _span,
        _start,
    )
    from autodj.shows import prepared_break, prepared_entry

    parameters = dict(request)
    capture_paths = tuple(entry.path for entry in station.bridge.library.entries_snapshot("liner"))
    before = edition_fingerprint(station, parameters, paths=capture_paths)
    plan = edition_store(station).runtime_plan(
        parameters["edition_id"], station, include_pending=parameters.get("include_pending", False)
    )
    start = _start(parameters.get("start"), station.timezone)
    source = _source(station, start)
    entries = [prepared_entry(item, source.library) for item in plan.items]
    if any(entry is None for entry in entries):
        raise ReviewError("An edition item is unavailable; repair it before contextual review")
    _check_programme_length(
        sum(max(0, float(entry.length or 0)) for entry in entries)
        + sum(take.seconds for group in plan.handoffs.values() for take in group)
    )
    identities = {id(entry): item for entry, item in zip(entries, plan.items, strict=True)}

    def handoff(current: Any, upcoming: Any) -> tuple[Any, ...]:
        pair = (
            getattr(current, "_prepared_occurrence_id", None),
            getattr(upcoming, "_prepared_occurrence_id", None),
        )
        if pair[0] is None or pair[1] is None:
            return ()
        return tuple(prepared_break(take) for take in plan.handoffs.get((pair[0], pair[1]), ()))

    source.player.prepared_handoff = handoff
    rows = []
    for row in _finite(source, entries, start, cancelled):
        item = identities.get(id(row.entry))
        if item is None or row.in_handoff:
            rows.append(row)
            continue
        rows.append(
            replace(
                row,
                kind=item.kind,
                occurrence_id=item.take_id or item.occurrence_id,
                playback_rate=1 if item.kind == "clip" else row.playback_rate,
            )
        )
    if cancelled():
        raise ReviewError("Review cancelled")
    _check_programme_length(_span(rows))
    _freeze_review_inputs(station, source, rows, parameters)
    target = parameters.get("target_take_id")
    if target:
        from autodj.prepared_editions import relevant_signature

        edition = edition_store(station).get(plan.edition_id)
        take = next((take for take in edition.takes if take.id == target), None)
        if take is None:
            raise ReviewError("Choose a take in this edition")
        parameters["_prepared_target"] = {
            "edition_id": edition.id,
            "take_id": take.id,
            "asset_id": take.asset_id,
            "dependency_hash": relevant_signature(edition, take, station),
            "reviewed_revision": edition.revision,
        }
        targets: list[Any] = [item for item in plan.items if item.take_id == target]
        targets.extend(
            take for group in plan.handoffs.values() for take in group if take.take_id == target
        )
        parameters["_prepared_target_seconds"] = targets[0].seconds if targets else None
    if edition_fingerprint(station, request, paths=capture_paths) != before:
        raise ReviewError("Edition inputs changed while capturing; generate again")
    captured = edition_fingerprint(station, parameters)
    return ReviewPlan(source, tuple(rows), start, captured, parameters, plan.omissions)


def review_receipts(
    request: dict[str, Any], metadata: dict[str, Any], review_id: str
) -> list[dict]:
    """Issue proof only for an identifiable, completely retained real target."""
    from autodj.prepared_editions import ReviewReceipt

    target = request.get("target_take_id")
    if not target:
        return []
    captured = request["_prepared_target"]
    row = next((row for row in metadata["items"] if row["id"] == target), None)
    omitted = any(item["id"] == target for item in metadata.get("omissions", ()))
    expected = request.get("_prepared_target_seconds")
    complete = (
        row is not None
        and row.get("complete", False)
        and expected is not None
        and row.get("programme_start") is not None
        and row.get("programme_end") is not None
        and (
            row.get("source_complete", False) and abs(row["source_seconds"] - expected) <= 2 / 44100
            if row.get("kind") == "clip"
            else abs(row["programme_end"] - row["programme_start"] - expected) <= 2 / 44100
        )
        and row["preview_end"] - row["preview_start"] >= expected - 2 / 44100
        and target in request.get("keep_complete", ())
        and not omitted
        and not metadata.get("incomplete", False)
    )
    return [
        ReviewReceipt(
            **captured,
            review_id=review_id,
            keep_complete=complete,
            omitted=omitted,
        ).model_dump(mode="json")
    ]


def prepared_review_receipt(station: Any, review_id: str) -> dict:
    """Read a private receipt from station-owned immutable result metadata."""
    from autodj.review_api import _check_id
    from autodj.review_jobs import ReviewJobs

    _check_id(review_id)
    try:
        # This reader creates no worker and performs no recovery or mutation.
        result = ReviewJobs().result(station.paths.reviews, review_id)
        receipts = result["metadata"]["_prepared_receipts"]
    except (OSError, ValueError, KeyError) as exc:
        raise ValueError("Choose a completed full-take edition review") from exc
    return {"receipts": [{**receipt, "review_id": review_id} for receipt in receipts]}
