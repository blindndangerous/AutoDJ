import threading
from pathlib import Path

import pytest

from autodj.catalog import LibraryCatalog
from autodj.cue_rescan import CueRescan
from autodj.indexer import IndexEntry


def _entry(path: Path) -> IndexEntry:
    return IndexEntry(str(path), "Title", "Artist", "Album", "", 120, 2000, 20, 0.2, -1, -1, 0.0)


@pytest.fixture
def catalog_audio(tmp_path):
    audio = tmp_path / "song.wav"
    audio.write_bytes(b"audio bytes")
    with LibraryCatalog(tmp_path / "catalog.db") as catalog:
        yield catalog, audio


def test_rescan_proposal_is_server_owned_and_apply_preserves_unselected_cues(
    catalog_audio, monkeypatch
):
    from autodj import cue_rescan

    catalog, audio = catalog_audio
    catalog.put_entry(
        _entry(audio),
        points={"start": 0.2, "intro": 2.0, "segue": 18.0, "end": 20.0},
        point_source="user",
    )
    monkeypatch.setattr(
        cue_rescan,
        "analyze_basic",
        lambda *_a, **_k: (
            _entry(audio),
            {"start": 0.1, "intro": 3.0, "outro": None, "segue": 17.0, "end": 20.0},
        ),
    )
    proposal = CueRescan()
    result = proposal.scan(catalog, str(audio), "main")
    assert result["points"]["intro"] == 3.0
    assert "token" in result
    proposal.apply(catalog, str(audio), "main", result["token"], ["intro"])
    record = catalog.record_for_path(audio)
    assert record["points"]["intro"] == 3.0
    assert record["point_sources"]["intro"] == "analysis"
    assert record["points"]["start"] == 0.2


def test_rescan_rejects_stale_saved_cues_and_modified_file(catalog_audio, monkeypatch):
    from autodj import cue_rescan

    catalog, audio = catalog_audio
    catalog.put_entry(
        _entry(audio), points={"start": 0.0, "segue": 18.0, "end": 20.0}, point_source="analysis"
    )
    monkeypatch.setattr(
        cue_rescan,
        "analyze_basic",
        lambda *_a, **_k: (_entry(audio), {"start": 0.1, "segue": 17.0, "end": 20.0}),
    )
    proposal = CueRescan()
    result = proposal.scan(catalog, str(audio), "main")
    catalog.set_points(audio, {"start": 0.3})
    with pytest.raises(RuntimeError, match="Saved cues changed"):
        proposal.apply(catalog, str(audio), "main", result["token"], ["segue"])
    result = proposal.scan(catalog, str(audio), "main")
    audio.write_bytes(b"changed bytes")
    with pytest.raises(RuntimeError, match="Recording changed"):
        proposal.apply(catalog, str(audio), "main", result["token"], ["segue"])


def test_liner_scan_limits_fields_and_refuses_to_clamp_unselected_overlap(
    catalog_audio, monkeypatch
):
    from autodj import cue_rescan

    catalog, audio = catalog_audio
    catalog.put_entry(
        _entry(audio),
        kind="liner",
        points={"start": 0, "segue": 8, "end": 10, "overlap_start": 7},
        point_source="analysis",
    )
    monkeypatch.setattr(
        cue_rescan,
        "analyze_basic",
        lambda *_a, **_k: (
            _entry(audio),
            {"start": 0.1, "segue": 5, "end": 10, "intro": 2, "outro": 9},
        ),
    )
    proposal = CueRescan()
    result = proposal.scan(catalog, str(audio), "main")
    assert set(result["points"]) == {"start", "segue", "end"}
    before = catalog.record_for_path(audio)
    with pytest.raises(ValueError, match="overlap_start"):
        proposal.apply(catalog, str(audio), "main", result["token"], ["segue"])
    after = catalog.record_for_path(audio)
    assert after["points"] == before["points"]
    assert after["point_sources"] == before["point_sources"]


def test_scan_detects_lyrics_provenance_and_stale_sidecar(catalog_audio, monkeypatch):
    from autodj import cue_rescan

    catalog, audio = catalog_audio
    catalog.put_entry(
        _entry(audio), points={"start": 0, "segue": 18, "end": 20}, point_source="analysis"
    )
    audio.with_suffix(".lrc").write_text(
        "[00:03.00]First lyric\n[00:04.00]Second lyric\n", encoding="utf-8"
    )
    monkeypatch.setattr(
        cue_rescan,
        "analyze_basic",
        lambda *_a, **_k: (
            _entry(audio),
            {"start": 0, "intro": 3, "outro": 4, "segue": 18, "end": 20},
        ),
    )
    proposal = CueRescan()
    result = proposal.scan(catalog, str(audio), "main")
    assert result["point_sources"]["intro"] == "lyrics"
    assert result["point_sources"]["outro"] == "lyrics"
    audio.with_suffix(".lrc").write_text("[00:05.00]Changed lyric\n", encoding="utf-8")
    with pytest.raises(RuntimeError, match="Recording changed"):
        proposal.apply(catalog, str(audio), "main", result["token"], ["intro"])


def test_catalog_snapshot_is_rechecked_across_connections(catalog_audio, monkeypatch):
    from autodj import cue_rescan

    catalog, audio = catalog_audio
    catalog.put_entry(
        _entry(audio), points={"start": 0, "segue": 18, "end": 20}, point_source="analysis"
    )
    monkeypatch.setattr(
        cue_rescan,
        "analyze_basic",
        lambda *_a, **_k: (_entry(audio), {"start": 0.1, "segue": 17, "end": 20}),
    )
    proposal = CueRescan()
    result = proposal.scan(catalog, str(audio), "main")
    with LibraryCatalog(catalog.path) as other:
        other.set_points(audio, {"start": 0.2})
    with pytest.raises(RuntimeError, match="Saved cues changed"):
        proposal.apply(catalog, str(audio), "main", result["token"], ["segue"])
    assert catalog.record_for_path(audio)["points"]["segue"] == 18


def test_apply_preserves_liner_settings_and_play_history(catalog_audio, monkeypatch):
    from autodj import cue_rescan

    catalog, audio = catalog_audio
    catalog.put_entry(
        _entry(audio),
        kind="liner",
        points={"start": 0, "segue": 8, "end": 10},
        point_source="analysis",
    )
    catalog.set_points(audio, {}, allow_outro=True)
    catalog.set_intro_lift(audio, 1.25)
    catalog.remember_play(audio, timestamp=123)
    monkeypatch.setattr(
        cue_rescan,
        "analyze_basic",
        lambda *_a, **_k: (_entry(audio), {"start": 0.1, "segue": 7, "end": 10}),
    )
    proposal = CueRescan()
    result = proposal.scan(catalog, str(audio), "main")
    proposal.apply(catalog, str(audio), "main", result["token"], ["segue"])
    record = catalog.record_for_path(audio)
    assert record["points"]["segue"] == 7
    assert record["allow_outro"] is True
    assert record["intro_lift_db"] == 1.25
    assert record["last_played"] == 123


def test_scan_failure_and_busy_shutdown_leave_saved_state_intact(catalog_audio, monkeypatch):
    from autodj import cue_rescan

    catalog, audio = catalog_audio
    catalog.put_entry(
        _entry(audio), points={"start": 0, "segue": 18, "end": 20}, point_source="analysis"
    )
    before = catalog.record_for_path(audio)
    entered, release = threading.Event(), threading.Event()

    def blocked_scan(*_args, **_kwargs):
        entered.set()
        release.wait()
        raise ValueError("decode failed")

    monkeypatch.setattr(cue_rescan, "analyze_basic", blocked_scan)
    proposal = CueRescan()

    def run_scan():
        with pytest.raises(ValueError):
            proposal.scan(catalog, str(audio), "main")

    worker = threading.Thread(target=run_scan)
    worker.start()
    assert entered.wait(2)
    with pytest.raises(RuntimeError, match="already running"):
        proposal.scan(catalog, str(audio), "main")
    assert not proposal.shutdown(0.01)
    release.set()
    worker.join(2)
    assert not worker.is_alive()
    with pytest.raises(RuntimeError, match="shutting down"):
        proposal.scan(catalog, str(audio), "main")
    assert catalog.record_for_path(audio)["points"] == before["points"]


@pytest.mark.parametrize("fields", [[], ["overlap_start"], ["start", "start"]])
def test_apply_rejects_fields_outside_the_scan(catalog_audio, monkeypatch, fields):
    from autodj import cue_rescan

    catalog, audio = catalog_audio
    catalog.put_entry(_entry(audio), points={"start": 0, "end": 20})
    before = catalog.record_for_path(audio)
    monkeypatch.setattr(
        cue_rescan, "analyze_basic", lambda *_a, **_k: (_entry(audio), {"start": 1, "end": 20})
    )
    scanner = CueRescan()
    result = scanner.scan(catalog, str(audio), "main")
    with pytest.raises(ValueError, match="Select one or more"):
        scanner.apply(catalog, str(audio), "main", result["token"], fields)
    assert catalog.record_for_path(audio) == before


@pytest.mark.parametrize("change", ["cues", "audio"])
def test_changes_during_analysis_reject_the_proposal(catalog_audio, monkeypatch, change):
    from autodj import cue_rescan

    catalog, audio = catalog_audio
    catalog.put_entry(_entry(audio), points={"start": 0, "end": 20})

    def analyze(*_args, **_kwargs):
        if change == "cues":
            catalog.set_points(audio, {"start": 2})
        else:
            audio.write_bytes(b"replacement audio")
        return _entry(audio), {"start": 1, "end": 20}

    monkeypatch.setattr(cue_rescan, "analyze_basic", analyze)
    with pytest.raises(RuntimeError, match="changed during scan"):
        CueRescan().scan(catalog, str(audio), "main")
    assert catalog.points(audio)["start"] == (2 if change == "cues" else 0)
