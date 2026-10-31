"""Consumption, rather than receipt of browser JSON, commits a play."""

import pytest

from autodj.local_programme_events import ProgrammeEvents


def event(sequence=0, kind="started", occurrence="one", frame=441):
    return {"sequence": sequence, "kind": kind, "occurrence": occurrence, "frame": frame}


def test_consumption_receipts_survive_revocation_and_retry():
    """ADR-0020-06: unheard markers never count, consumed markers survive loss."""
    events = ProgrammeEvents()
    events.reserve("one")
    events.submit(event(), 0)
    assert events.receipts() == []
    events.consume([(0, 441)])
    assert events.receipts() == []
    events.consume([(441, 441)])
    events.cancel()
    assert events.receipts() == [event()]
    events.complete(event())
    assert events.submit(event(), 882) == "completed"
    with pytest.raises(ValueError, match="Conflicting"):
        events.submit(event(frame=442), 882)


def test_only_reserved_occurrences_ordered_future_frames_and_bounded_pending():
    """ADR-0020-06: occurrence identities and bounded ordered markers fence reports."""
    events = ProgrammeEvents()
    with pytest.raises(ValueError):
        events.submit(event(), 0)
    events.reserve("one")
    events.reserve("two")
    with pytest.raises(ValueError):
        events.reserve("three")
    with pytest.raises(ValueError):
        events.submit(event(frame=0), 441)
    events.submit(event(), 0)
    for sequence in range(1, 64):
        events.submit(
            event(sequence, "paused" if sequence % 2 else "resumed", frame=441 + sequence), 0
        )
    with pytest.raises(ValueError, match="budget"):
        events.submit(event(64, "paused", frame=600), 0)


def test_same_song_occurrences_are_distinct_and_ended_releases_slot():
    """ADR-0020-06: A/B/A uses occurrence IDs, never path-based deduplication."""
    events = ProgrammeEvents()
    for occurrence in ("a-first", "b", "a-again"):
        events.reserve(occurrence)
        sequence = events.next_sequence
        start = event(sequence, occurrence=occurrence, frame=sequence * 441)
        end = event(sequence + 1, "ended", occurrence, (sequence + 1) * 441)
        events.submit(start, sequence * 441)
        events.submit(end, sequence * 441)
        events.consume([(sequence * 441, 882)])
        assert events.receipts() == [start, end]
        events.complete(start)
        events.complete(end)
    assert events.next_sequence == 6


def test_skipped_marker_fails_without_silently_losing_receipt():
    """ADR-0020-06: a missing audio span cannot erase an accepted start."""
    events = ProgrammeEvents()
    events.reserve("one")
    marker = event(frame=0)
    events.submit(marker, 0)
    with pytest.raises(ValueError, match="span"):
        events.consume([(441, 441)])
    assert events.submit(marker, 882) == "pending"
    events.consume([(0, 441)])
    assert events.receipts() == [marker]
