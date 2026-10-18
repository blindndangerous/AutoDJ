"""Prepared editions consume the real isolated renderer and explicit receipts."""

import numpy as np
import pytest
import soundfile as sf

from autodj.prepared_editions import edition_store
from autodj.prepared_review import edition_fingerprint, review_receipts
from autodj.review_audio import ReviewSettings
from autodj.review_render import render_review
from autodj.review_source import capture_review
from tests.unit.test_review_source import station_content


def test_pending_review_retains_real_take_and_never_approves_it(tmp_path):
    """ADR-0015-03: actual contextual audio and a receipt do not approve a take."""
    station, show_id, _ = station_content(tmp_path, seconds=2)
    store = edition_store(station)
    edition = store.create(show_id, station.shows.get(show_id))
    voice = tmp_path / "take.wav"
    padded = np.concatenate((np.zeros(4410), np.full(44100, 0.15), np.zeros(8820)))
    sf.write(voice, padded, 44100)
    edition = store.import_take(
        edition.id,
        edition.revision,
        voice,
        position="transition",
        before_id=edition.occurrences[0].id,
        after_id=edition.occurrences[1].id,
        placement="standalone",
    )
    take = edition.takes[0]
    request = {
        "source": "edition",
        "edition_id": edition.id,
        "include_pending": True,
        "target_take_id": take.id,
        "keep_complete": [take.id],
    }
    plan = capture_review(station, request)
    output = tmp_path / "review"
    output.mkdir()
    metadata = render_review(
        plan.source,
        plan.items,
        output,
        settings=ReviewSettings(head_seconds=0.01, tail_seconds=0.01),
        keep_complete=[take.id],
    )
    receipts = review_receipts(plan.request, metadata, "f" * 32)
    assert receipts[0]["keep_complete"] is True
    assert store.get(edition.id).takes[0].approval is None
    assert station.player is None and station.ledger.writes == 0
    target = next(row for row in metadata["items"] if row["id"] == take.id)
    assert target["preview_end"] - target["preview_start"] == pytest.approx(1.3)
    with sf.SoundFile(output / "review.wav") as rendered:
        rendered.seek(round(target["preview_start"] * rendered.samplerate))
        assert np.max(np.abs(rendered.read(4410))) < 1e-6
        assert np.max(np.abs(rendered.read(1000))) > 0.01


def test_edition_review_becomes_stale_when_its_source_asset_changes(tmp_path):
    station, show_id, _ = station_content(tmp_path, seconds=2)
    store = edition_store(station)
    edition = store.create(show_id, station.shows.get(show_id))
    source = tmp_path / "speech.wav"
    sf.write(source, np.full(44100, 0.15, np.float32), 44100)
    edition = store.import_take(
        edition.id, edition.revision, source, position="opening", after_id=edition.occurrences[0].id
    )
    request = {"source": "edition", "edition_id": edition.id}
    before = edition_fingerprint(station, request)
    asset = edition.assets[edition.takes[0].asset_id]
    path = station.paths.edition_audio / asset.file
    path.write_bytes(path.read_bytes() + b"changed")
    assert edition_fingerprint(station, request) != before


@pytest.mark.parametrize("change", ["shortened", "placeholder", "missing", "wrong_duration"])
def test_shortened_or_diagnostic_target_cannot_supply_approval_proof(change):
    target = "a" * 32
    request = {
        "target_take_id": target,
        "keep_complete": [target],
        "_prepared_target_seconds": 301,
        "_prepared_target": {
            "edition_id": "b" * 32,
            "take_id": target,
            "asset_id": "c" * 32,
            "dependency_hash": "d" * 64,
            "reviewed_revision": 1,
        },
    }
    row = {
        "id": target,
        "complete": True,
        "programme_start": 10,
        "programme_end": 311,
        "preview_start": 1,
        "preview_end": 302,
    }
    metadata = {"items": [row], "omissions": [], "incomplete": False}
    if change == "shortened":
        row["complete"] = False
    elif change == "placeholder":
        metadata["incomplete"] = True
        metadata["omissions"] = [{"id": target}]
    elif change == "missing":
        metadata["items"] = []
    else:
        row["programme_end"] = 300
    assert review_receipts(request, metadata, "f" * 32)[0]["keep_complete"] is False
