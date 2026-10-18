"""Real long audio checks; explicitly report hosts without scratch capacity."""

import mmap
import shutil

import numpy as np
import pytest
import soundfile as sf

from autodj.prepared_editions import edition_store
from autodj.prepared_review import review_receipts
from autodj.review_audio import ReviewSettings
from autodj.review_render import render_review
from autodj.review_source import _source, capture_review
from autodj.shows import prepared_entry
from autodj.stereo import SAMPLE_RATE, audio_workspace
from tests.unit.test_review_source import station_content


def source_clip(path, seconds):
    block = (0.15 * np.sin(2 * np.pi * 220 * np.arange(8000) / 8000)).astype(np.float32)
    with sf.SoundFile(path, "w", samplerate=8000, channels=1, subtype="PCM_16") as output:
        for _ in range(seconds):
            output.write(block)


def edition_with_segment(tmp_path, seconds):
    station, show_id, _ = station_content(tmp_path, seconds=2)
    station.cfg.playback.loudness_processing = "off"
    store = edition_store(station)
    edition = store.create(show_id, station.shows.get(show_id))
    path = tmp_path / "interview.flac"
    source_clip(path, seconds)
    edition = store.import_take(edition.id, edition.revision, path, position="segment", index=1)
    return station, edition


def test_real_thirty_minute_primary_decode_is_scoped_and_disk_backed(tmp_path):
    """ADR-0015-04: decode the real supported bound without a break decoder mock."""
    if shutil.disk_usage(tmp_path).free < 1536 * 1024**2:
        pytest.skip("Real 30-minute decode needs 1.5 GiB free scratch; run on a larger test host")
    station, edition = edition_with_segment(tmp_path, 1800)
    plan = edition_store(station).runtime_plan(edition.id, station, include_pending=True)
    source = _source(station, 0)
    item = next(item for item in plan.items if item.kind == "clip")
    entry = prepared_entry(item, source.library)
    with audio_workspace(tmp_path / "decode", max_bytes=1024**3) as workspace:
        audio = source.player._load_playout_audio(entry)
        assert audio.shape == (1800 * SAMPLE_RATE, 2)
        base = audio
        while isinstance(base, np.ndarray):
            base = base.base
        if isinstance(base, memoryview):
            base = base.obj
        assert isinstance(base, mmap.mmap) and not base.closed
        assert workspace.allocated >= audio.nbytes
        assert source.player.music_playback_rate(entry) == 1
        assert np.max(np.abs(audio[:SAMPLE_RATE])) > 0.01
    assert base.closed


def test_real_above_threshold_segment_gets_complete_target_receipt(tmp_path):
    """ADR-0015-03: actual >5-minute PCM remains complete through MP3/WAV review."""
    if shutil.disk_usage(tmp_path).free < 768 * 1024**2:
        pytest.skip("Real long review needs 768 MiB free scratch; run on a larger test host")
    station, edition = edition_with_segment(tmp_path, 301)
    take = edition.takes[0]
    request = {
        "source": "edition",
        "edition_id": edition.id,
        "target_take_id": take.id,
        "include_pending": True,
        "keep_complete": [take.id],
    }
    plan = capture_review(station, request)
    output = tmp_path / "review"
    output.mkdir()
    metadata = render_review(
        plan.source,
        plan.items,
        output,
        settings=ReviewSettings(head_seconds=0.1, tail_seconds=0.1),
        keep_complete=[take.id],
    )
    target = next(row for row in metadata["items"] if row["id"] == take.id)
    assert target["source_seconds"] == 301
    assert target["source_complete"] is True
    # Context can include transition padding; every source frame must remain.
    assert target["preview_end"] - target["preview_start"] >= 301 - 2 / SAMPLE_RATE
    assert not any(
        piece["kind"] == "tone"
        and piece["preview_start"] < target["preview_end"]
        and piece["preview_end"] > target["preview_start"]
        for piece in metadata["pieces"]
    )
    assert review_receipts(plan.request, metadata, "f" * 32)[0]["keep_complete"] is True
    assert (output / "review.mp3").stat().st_size > 1000
    with sf.SoundFile(output / "review.wav") as audio:
        assert audio.frames >= 301 * SAMPLE_RATE


@pytest.mark.parametrize("covered", [False, True])
def test_primary_receipt_requires_verified_source_coverage(covered):
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
        "kind": "clip",
        "complete": True,
        "programme_start": 2,
        "programme_end": 303.25,
        "preview_start": 2,
        "preview_end": 303.25,
        "source_seconds": 301,
        "source_complete": covered,
    }
    metadata = {"items": [row], "omissions": [], "incomplete": False}
    assert review_receipts(request, metadata, "f" * 32)[0]["keep_complete"] is covered
