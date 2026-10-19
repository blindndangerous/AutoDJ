"""Durable edition source identity, review dependencies and atomic approval."""

import numpy as np
import pytest
import soundfile as sf

from autodj.prepared_editions import EditionError, EditionStore, ReviewReceipt, relevant_signature
from tests.unit.test_review_source import station_content


def edition_store(tmp_path):
    station, show_id, entries = station_content(tmp_path)
    store = EditionStore(station.paths.editions, station.paths.edition_audio)
    edition = store.create(show_id, station.shows.get(show_id))
    return station, store, edition, entries


def voice(tmp_path, seconds=0.1):
    path = tmp_path / "voice.wav"
    sf.write(path, np.ones((round(seconds * 44100), 2), np.float32) * 0.25, 44100, subtype="PCM_16")
    return path


def add_take(station, store, edition, tmp_path, **options):
    return store.import_take(
        edition.id,
        edition.revision,
        voice(tmp_path),
        position="transition",
        before_id=edition.occurrences[0].id,
        after_id=edition.occurrences[1].id,
        **options,
    )


def receipt(station, store, edition, take, **change):
    return ReviewReceipt(
        edition_id=edition.id,
        take_id=take.id,
        asset_id=take.asset_id,
        dependency_hash=relevant_signature(edition, take, station),
        review_id="a" * 32,
        reviewed_revision=edition.revision,
        keep_complete=True,
        **change,
    )


def test_repeated_song_ids_survive_reorder_and_stale_edit_is_rejected(tmp_path):
    """ADR-0015-01: each occurrence persists independently and stale edits write nothing."""
    _station, store, edition, entries = edition_store(tmp_path)
    assert len({item.id for item in edition.occurrences}) == 3
    assert edition.occurrences[0].path == edition.occurrences[2].path == entries[0].path
    changed = store.edit(
        edition.id, edition.revision, occurrences=list(reversed(edition.occurrences))
    )
    assert changed.occurrences[0].id == edition.occurrences[2].id
    with pytest.raises(EditionError, match="changed"):
        store.edit(edition.id, edition.revision, name="Stale")
    assert store.get(edition.id).name == edition.name


def test_intact_pair_move_and_unrelated_edits_preserve_approval_but_changed_song_does_not(tmp_path):
    """ADR-0015-02: approval follows relevant context rather than whole-edition revision."""
    station, store, edition, entries = edition_store(tmp_path)
    edition = add_take(station, store, edition, tmp_path)
    take = edition.takes[0]
    edition = store.approve(edition.id, take.id, receipt(station, store, edition, take), station)
    assert store.take_status(edition, edition.takes[0], station) == "ready"
    edition = store.edit(
        edition.id,
        edition.revision,
        occurrences=[edition.occurrences[2], *edition.occurrences[:2]],
        name="Renamed",
    )
    assert store.take_status(edition, edition.takes[0], station) == "ready"
    changed = [*edition.occurrences]
    changed[1] = changed[1].model_copy(update={"path": entries[2].path})
    edition = store.edit(edition.id, edition.revision, occurrences=changed)
    assert store.take_status(edition, edition.takes[0], station) == "needs_review"
    assert store.asset_path(take.asset_id).is_file()


def test_approval_rejects_shortened_placeholder_and_relevant_change_but_not_unrelated_revision(
    tmp_path,
):
    station, store, edition, _ = edition_store(tmp_path)
    edition = add_take(station, store, edition, tmp_path)
    take = edition.takes[0]
    proof = receipt(station, store, edition, take)
    with pytest.raises(EditionError, match="complete"):
        store.approve(
            edition.id, take.id, proof.model_copy(update={"keep_complete": False}), station
        )
    with pytest.raises(EditionError, match=r"missing|omitted|placeholder"):
        store.approve(edition.id, take.id, proof.model_copy(update={"omitted": True}), station)
    edition = store.edit(edition.id, edition.revision, name="Unrelated rename")
    assert store.approve(edition.id, take.id, proof, station).takes[0].approval is not None
    station.cfg.playback.crossfade_seconds += 1
    with pytest.raises(EditionError, match="changed"):
        store.approve(edition.id, take.id, proof, station)


def test_rerecord_preserves_old_asset_and_runtime_skips_pending_speech(tmp_path):
    station, store, edition, _ = edition_store(tmp_path)
    edition = add_take(station, store, edition, tmp_path)
    first = edition.takes[0]
    plan = store.runtime_plan(edition.id, station)
    assert not plan.handoffs and plan.omissions
    pending = store.runtime_plan(edition.id, station, include_pending=True)
    assert len(pending.handoffs[(first.before_id, first.after_id)]) == 1
    edition = store.import_take(
        edition.id,
        edition.revision,
        voice(tmp_path, 0.2),
        position="transition",
        before_id=first.before_id,
        after_id=first.after_id,
        take_id=first.id,
    )
    assert len(edition.assets) == 2 and len(edition.takes) == 1
    assert store.asset_path(first.asset_id).is_file()
    assert edition.takes[0].asset_id != first.asset_id


def test_failed_manifest_write_keeps_original_asset_and_edition(tmp_path, monkeypatch):
    import autodj.prepared_editions as module

    station, store, edition, _ = edition_store(tmp_path)
    original = store.get(edition.id)
    monkeypatch.setattr(
        module, "atomic_write", lambda *args, **kwargs: (_ for _ in ()).throw(OSError("diskfull"))
    )
    with pytest.raises(OSError):
        add_take(station, store, edition, tmp_path)
    assert store.get(edition.id) == original
    assert not list(store.audio.glob("*.wav"))


def test_segment_has_own_approval_and_start_warning_is_persisted(tmp_path):
    station, store, edition, _ = edition_store(tmp_path)
    edition = store.import_take(
        edition.id, edition.revision, voice(tmp_path), position="segment", index=1
    )
    segment = edition.occurrences[1]
    take = next(take for take in edition.takes if take.id == segment.take_id)
    assert take.occurrence_id == segment.id
    assert not any(item.kind == "clip" for item in store.runtime_plan(edition.id, station).items)
    edition = store.approve(edition.id, take.id, receipt(station, store, edition, take), station)
    assert any(
        item.kind == "clip" and item.occurrence_id == segment.id
        for item in store.runtime_plan(edition.id, station).items
    )
    store.mark_started(edition.id)
    restored = EditionStore(store.path, store.audio)
    assert restored.get(edition.id).previously_started
    with pytest.raises(EditionError, match="confirmation"):
        restored.mark_started(edition.id)
    restored.mark_started(edition.id, confirm_reair=True)


def test_relevant_duck_gain_and_cues_invalidate_without_unrelated_name_changes(tmp_path):
    station, store, edition, _ = edition_store(tmp_path)
    edition = add_take(station, store, edition, tmp_path)
    take = edition.takes[0]
    original = relevant_signature(edition, take, station)
    station.cfg.breaks.duck_db = -6
    assert relevant_signature(edition, take, station) != original
    station.cfg.breaks.duck_db = None
    assert relevant_signature(edition, take, station) == original
    station.bridge.library.catalog.points = lambda path: {"intro": 0.05}
    assert relevant_signature(edition, take, station) != original


def test_changed_adjacent_segment_audio_invalidates_transition_review(tmp_path):
    station, store, edition, _ = edition_store(tmp_path)
    edition = store.import_take(
        edition.id, edition.revision, voice(tmp_path), position="segment", index=1
    )
    edition = store.import_take(
        edition.id,
        edition.revision,
        voice(tmp_path),
        position="transition",
        before_id=edition.occurrences[0].id,
        after_id=edition.occurrences[1].id,
    )
    take = edition.takes[-1]
    assert relevant_signature(edition, take, station)
    segment = edition.assets[edition.occurrences[1].asset_id]
    with (store.audio / segment.file).open("ab") as handle:
        handle.write(b"changed")
    with pytest.raises(EditionError, match=r"segment|invalid|changed"):
        relevant_signature(edition, take, station)


def test_duplicate_take_is_rejected_before_decode_without_changing_edition(tmp_path, monkeypatch):
    station, store, edition, _ = edition_store(tmp_path)
    edition = add_take(station, store, edition, tmp_path)
    original = store.path.read_bytes()
    assets = set(store.audio.iterdir())
    monkeypatch.setattr(
        "autodj.prepared_editions.load_stereo",
        lambda *args: pytest.fail("A duplicate binding must be rejected before decoding"),
    )
    with pytest.raises(EditionError, match="Record again"):
        add_take(station, store, edition, tmp_path, label="Second take")
    assert store.path.read_bytes() == original
    assert set(store.audio.iterdir()) == assets


def test_rerecord_replaces_active_take_keeps_old_asset_and_requires_fresh_review(tmp_path):
    station, store, edition, _ = edition_store(tmp_path)
    edition = add_take(station, store, edition, tmp_path)
    take = edition.takes[0]
    edition = store.approve(edition.id, take.id, receipt(station, store, edition, take), station)
    edition = add_take(station, store, edition, tmp_path, take_id=take.id)
    assert len(edition.takes) == 1 and edition.takes[0].id == take.id
    assert edition.takes[0].asset_id != take.asset_id
    assert store.asset_path(take.asset_id).is_file()
    assert store.take_status(edition, edition.takes[0], station) == "needs_review"


def test_persisted_duplicate_binding_is_invalid_and_never_overwritten(tmp_path):
    import json

    station, store, edition, _ = edition_store(tmp_path)
    edition = add_take(station, store, edition, tmp_path)
    document = json.loads(store.path.read_text())
    takes = document["editions"][edition.id]["takes"]
    takes.append({**takes[0], "id": "f" * 32})
    store.path.write_text(json.dumps(document))
    original = store.path.read_bytes()
    with pytest.raises(EditionError, match="state is invalid"):
        store.get(edition.id)
    assert store.path.read_bytes() == original


def test_manifest_is_utf8_even_when_platform_default_is_cp1252(tmp_path, monkeypatch):
    from pathlib import Path

    _station, store, edition, _ = edition_store(tmp_path)
    edition = store.edit(edition.id, edition.revision, name="Ángel Café")
    read_text = Path.read_text

    def local_read(path, encoding=None, *args, **kwargs):
        return read_text(path, encoding or "cp1252", *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", local_read)
    assert store.get(edition.id).name == "Ángel Café"
    assert store.edit(edition.id, edition.revision, name="Édition").name == "Édition"


def test_runtime_validation_reuses_unchanged_hashes_and_rechecks_changed_files(
    tmp_path, monkeypatch
):
    import os
    from collections import Counter

    import autodj.prepared_editions as module

    station, store, edition, _ = edition_store(tmp_path)
    edition = add_take(station, store, edition, tmp_path)
    take = edition.takes[0]
    edition = store.approve(edition.id, take.id, receipt(station, store, edition, take), station)
    calls = Counter()
    original = module._hash

    def counted(path):
        calls[path] += 1
        return original(path)

    monkeypatch.setattr(module, "_hash", counted)
    assert store.runtime_plan(edition.id, station).handoffs
    calls.clear()
    assert store.runtime_plan(edition.id, station).handoffs
    assert not calls
    path = store.audio / edition.assets[take.asset_id].file
    stamp = path.stat()
    data = bytearray(path.read_bytes())
    data[-1] ^= 1
    path.write_bytes(data)
    os.utime(path, ns=(stamp.st_atime_ns, stamp.st_mtime_ns + 1_000_000))
    assert not store.runtime_plan(edition.id, station).handoffs


@pytest.mark.parametrize("edit", ["append", "move", "remove_neighbour"])
def test_segment_follows_current_neighbours_and_can_be_reapproved(tmp_path, edit):
    """ADR-0015-01 ADR-0015-02: edits retain the segment and invalidate changed context."""
    from autodj.prepared_editions import Occurrence

    station, store, edition, entries = edition_store(tmp_path)
    edition = store.import_take(edition.id, edition.revision, voice(tmp_path), position="segment")
    take = edition.takes[0]
    edition = store.approve(edition.id, take.id, receipt(station, store, edition, take), station)
    rows = list(edition.occurrences)
    if edit == "append":
        rows.append(Occurrence(path=entries[1].path))
    elif edit == "move":
        rows.insert(1, rows.pop())
    else:
        rows.pop(-2)
    edition = store.edit(edition.id, edition.revision, occurrences=rows)
    take = edition.takes[0]
    at = next(index for index, row in enumerate(rows) if row.id == take.occurrence_id)
    assert take.before_id == (rows[at - 1].id if at else None)
    assert take.after_id == (rows[at + 1].id if at + 1 < len(rows) else None)
    assert store.take_status(edition, take, station) == "needs_review"
    assert store.asset_path(take.asset_id).is_file()
    pending = store.runtime_plan(edition.id, station, include_pending=True)
    assert any(row.take_id == take.id for row in pending.items)
    assert not pending.omissions
    edition = store.approve(edition.id, take.id, receipt(station, store, edition, take), station)
    assert any(row.take_id == take.id for row in store.runtime_plan(edition.id, station).items)


def test_broken_pair_is_unattached_and_reattach_keeps_asset(tmp_path):
    """ADR-0015-01 ADR-0015-02: broken pairs expose recovery without re-recording."""
    station, store, edition, _ = edition_store(tmp_path)
    edition = add_take(station, store, edition, tmp_path)
    take = edition.takes[0]
    edition = store.edit(
        edition.id,
        edition.revision,
        occurrences=[edition.occurrences[0], edition.occurrences[2], edition.occurrences[1]],
    )
    assert store.take_status(edition, take, station) == "unattached"
    edition = store.reattach(
        edition.id,
        take.id,
        edition.revision,
        before_id=edition.occurrences[0].id,
        after_id=edition.occurrences[1].id,
    )
    moved = edition.takes[0]
    assert moved.asset_id == take.asset_id and moved.id == take.id
    assert store.take_status(edition, moved, station) == "needs_review"
    edition = store.approve(edition.id, moved.id, receipt(station, store, edition, moved), station)
    assert store.runtime_plan(edition.id, station).handoffs


def test_removing_adjacent_segment_keeps_reapproved_take_valid_on_rename(tmp_path):
    """ADR-0015-02: segment removal updates bindings before the next unrelated edit."""
    station, store, edition, _ = edition_store(tmp_path)
    for _ in range(2):
        edition = store.import_take(
            edition.id, edition.revision, voice(tmp_path), position="segment"
        )
    removed, kept = edition.takes
    edition = store.remove_take(edition.id, removed.id, edition.revision)
    kept = edition.takes[0]
    edition = store.approve(edition.id, kept.id, receipt(station, store, edition, kept), station)
    edition = store.edit(edition.id, edition.revision, name="Unrelated name")
    assert store.take_status(edition, edition.takes[0], station) == "ready"


@pytest.mark.parametrize("position", ["opening", "closing"])
def test_resume_take_id_maps_to_real_occurrence(tmp_path, position):
    """ADR-0015-05: primary edge speech never leaves an unusable resume choice."""
    station, store, edition, _ = edition_store(tmp_path)
    edition = store.import_take(
        edition.id,
        edition.revision,
        voice(tmp_path),
        position=position,
        **(
            {"after_id": edition.occurrences[0].id}
            if position == "opening"
            else {"before_id": edition.occurrences[-1].id}
        ),
    )
    take = edition.takes[0]
    plan = store.runtime_plan(edition.id, station, from_occurrence=take.id)
    expected = edition.occurrences[0 if position == "opening" else -1].id
    assert plan.items[0].occurrence_id == expected


def test_import_passes_duration_bound_to_decoder(tmp_path, monkeypatch):
    from autodj import prepared_editions as module

    _station, store, edition, _ = edition_store(tmp_path)
    decode = module.load_stereo
    limits = []

    def bounded(*args, max_seconds=None):
        limits.append(max_seconds)
        return decode(*args, max_seconds=max_seconds)

    monkeypatch.setattr(module, "load_stereo", bounded)
    store.import_take(edition.id, edition.revision, voice(tmp_path), position="segment")
    assert limits == [1800]


def test_station_store_startup_sweeps_only_partial_imports(tmp_path):
    from autodj.prepared_editions import edition_store as station_store

    station, store, _edition, _ = edition_store(tmp_path)
    for name in (".upload-crashed", ".import-crashed", "retained"):
        directory = store.audio / name
        directory.mkdir(parents=True)
        (directory / "source.wav").write_bytes(b"partial")
    source = store.audio / "retained.wav"
    source.write_bytes(b"published")
    station_store(station)
    assert not (store.audio / ".upload-crashed").exists()
    assert not (store.audio / ".import-crashed").exists()
    assert source.read_bytes() == b"published"
    assert (store.audio / "retained/source.wav").exists()
    # Ordinary requests reuse the store and cannot sweep an active upload.
    active = store.audio / ".upload-active"
    active.mkdir()
    station_store(station)
    assert active.is_dir()
    from types import SimpleNamespace

    restarted = SimpleNamespace(paths=station.paths)
    station_store(restarted)
    assert active.is_dir()


def test_partial_import_cleanup_failure_does_not_prevent_station_store(
    tmp_path, monkeypatch, caplog
):
    from autodj import prepared_editions as module

    station, store, _, _ = edition_store(tmp_path)
    partial = store.audio / ".import-locked"
    partial.mkdir(parents=True)
    monkeypatch.setattr(
        module.shutil, "rmtree", lambda *_: (_ for _ in ()).throw(PermissionError("locked"))
    )
    assert module.edition_store(station).audio == store.audio
    assert partial.is_dir()
    assert "locked" in caplog.text


def test_removing_segment_row_removes_take_and_frees_binding_without_losing_asset(tmp_path):
    """ADR-0015-01 ADR-0015-02: row removal retains audio without orphaning its take."""
    _station, store, edition, _ = edition_store(tmp_path)
    for index in (1, 4):
        edition = store.import_take(
            edition.id,
            edition.revision,
            voice(tmp_path),
            position="segment",
            index=index,
        )
    removed, kept = edition.takes
    edition = store.edit(
        edition.id,
        edition.revision,
        occurrences=[row for row in edition.occurrences if row.id != removed.occurrence_id],
    )
    assert [take.id for take in edition.takes] == [kept.id]
    assert store.asset_path(removed.asset_id).is_file()
    rows = list(edition.occurrences)
    rows.insert(1, rows.pop())
    edition = store.edit(edition.id, edition.revision, occurrences=rows)
    assert store.get(edition.id) == edition
    assert edition.takes[0].before_id == rows[0].id
    assert edition.takes[0].after_id == rows[2].id
