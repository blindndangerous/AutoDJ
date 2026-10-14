"""The playout timing the queue checks share with the hour preview (#217)."""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import numpy as np
import pytest

from autodj._bridge import PlayerBridge
from autodj.dmca import complement
from autodj.hour_preview import _remaining, history_at, read_head, time_queue
from autodj.mixbus import RenderedTrack
from autodj.repeat_rules import PlayRecord
from autodj.stereo import SAMPLE_RATE as SR
from tests.unit._rotation_station import liner, make_station, song

NOW = 1_800_000_000.0


def _station(tmp_path: Path, **changes: Any):
    songs = [song(n, f"Artist {n}") for n in range(8)]
    source = make_station(tmp_path, songs, liners=[liner(1, 10.0)], **changes)
    return source, songs


def _playing(source: Any, entry: Any, started: float, position: float) -> None:
    state = source.player._state
    state.current_track = entry
    state.record_played(entry)
    state.play_history[-1] = PlayRecord.of(entry, started)
    source.player._current_sr = SR
    source.player._playback_pos[0] = round(position * SR)


def _points(monkeypatch, source: Any, points: dict[str, dict[str, Any]]) -> None:
    monkeypatch.setattr(source.library.catalog, "points", lambda path: points.get(path, {}))


def _render(entry: Any, seconds: float, following: Any, overlap: float, **fields: Any):
    return RenderedTrack(
        entry=entry,
        audio=np.empty((round(seconds * SR), 0), dtype=np.float32),
        next_entry=following,
        next_start_offset=round(overlap * SR),
        transition_fx="none",
        overlap_frames=round(overlap * SR),
        **fields,
    )


def test_a_forward_seek_moves_the_queue_earlier(tmp_path: Path) -> None:
    source, songs = _station(tmp_path)
    player = source.player
    # Started 30 s ago, but seeked to 150 s of its 200: 50 s are left, not 170.
    _playing(source, songs[0], NOW - 30, 150)
    head = read_head(player, now=NOW)
    timed = time_queue(player, head, [songs[1]], insert=(1, songs[2]))
    assert timed.starts[0] == pytest.approx(NOW + 50)
    assert timed.starts[1] == pytest.approx(NOW + 250)


def test_a_segue_before_the_end_keeps_a_dmca_window_open(tmp_path: Path, monkeypatch) -> None:
    """A 300 s file that segues at 180 s hands over two minutes before its end."""
    source, songs = _station(tmp_path)
    player = source.player
    long_song = song(20, "Artist 20", length=300.0)
    _points(monkeypatch, source, {long_song.path: {"segue": 180.0}})
    _playing(source, songs[0], NOW - 10, 10)
    head = read_head(player, now=NOW)
    request = song(30, "Artist 30", album="Hits")
    timed = time_queue(player, head, [long_song], insert=(1, request))
    when = timed.starts[1]
    assert when == pytest.approx(NOW + 190 + 180)
    # Three plays from Hits, the first 3 hours before the request's real
    # start less 60 s: still counted then, though gone by the file's end.
    hits = [song(40 + n, f"Hits {n}", album="Hits") for n in range(3)]
    history = [PlayRecord.of(entry, when - 3 * 3600 + 60 + 600 * n) for n, entry in enumerate(hits)]
    records = history_at([*history, *player._state.play_history], head, timed.unplayed, when)
    assert complement(records, when, player._track_keys()).broken(request)
    later = complement(records, NOW + 190 + 300, player._track_keys())
    assert not later.broken(request)


def test_an_overlapping_liner_brings_the_next_song_in_sooner(tmp_path: Path, monkeypatch) -> None:
    source, songs = _station(tmp_path)
    player = source.player
    ident = source.library.entries_snapshot("liner")[0]
    _points(
        monkeypatch,
        source,
        {
            ident.path: {"start": 0.0, "end": 10.0, "overlap_start": 0},
            songs[1].path: {"start": 0.0, "intro": 6.0},
        },
    )
    monkeypatch.setattr(
        source.library.catalog, "record_for_path", lambda _path: {"allow_outro": True}
    )
    _playing(source, songs[0], NOW, 0)
    head = read_head(player, now=NOW)
    timed = time_queue(player, head, [ident, songs[1]])
    liner_starts, song_starts = timed.starts
    # The song starts under the liner's last 6 s, over its intro.
    assert song_starts - liner_starts < ident.length
    assert song_starts == pytest.approx(liner_starts + 4.0, abs=0.01)


@pytest.mark.parametrize("percent", [-10.0, 10.0])
def test_music_speed_scales_queued_songs_but_not_liners(tmp_path: Path, percent: float) -> None:
    source, songs = _station(tmp_path, playback={"music_speed_percent": percent})
    player = source.player
    ident = source.library.entries_snapshot("liner")[0]
    _playing(source, songs[0], NOW, 0)
    head = read_head(player, now=NOW)
    rate = 1 + percent / 100
    timed = time_queue(player, head, [songs[1], ident, songs[2]], insert=(3, songs[3]))
    first, voice, second, request = timed.starts
    assert voice - first == pytest.approx(200 / rate)
    assert second - voice == pytest.approx(ident.length)
    assert request - second == pytest.approx(200 / rate)


def test_a_render_holding_a_later_queue_song_picks_again(tmp_path: Path) -> None:
    """A request put ahead of the rendered queue song takes its place, at its handoff."""
    source, songs = _station(tmp_path)
    player = source.player
    _playing(source, songs[0], NOW - 50, 50)
    player.bus = object()
    player._playing_render = _render(songs[0], 200, songs[1], 5, next_from_queue=True)
    player._playback_pos[0] = round(50 * SR)
    player._pending_entry = songs[1]
    player._pending_offset = round(5 * SR)
    player._state.queue.extend([songs[1], songs[2]])
    head = read_head(player, now=NOW)
    as_is = time_queue(player, head)
    assert as_is.starts == (pytest.approx(NOW + 145), pytest.approx(NOW + 345))
    first = time_queue(player, head, insert=(0, songs[5]))
    assert first.starts[0] == pytest.approx(NOW + 145)
    assert first.starts[1] == pytest.approx(NOW + 345)
    # After it: the render stays and the request follows.
    second = time_queue(player, head, insert=(1, songs[5]))
    assert second.starts == (
        pytest.approx(NOW + 145),
        pytest.approx(NOW + 345),
        pytest.approx(NOW + 545),
    )


def test_a_render_that_picked_from_an_empty_queue_yields_to_a_queued_song(
    tmp_path: Path,
) -> None:
    source, songs = _station(tmp_path)
    player = source.player
    _playing(source, songs[0], NOW - 50, 50)
    player.bus = object()
    player._playing_render = _render(songs[0], 200, songs[1], 5)
    player._playback_pos[0] = round(50 * SR)
    player._pending_entry = songs[1]
    player._pending_offset = round(5 * SR)
    head = read_head(player, now=NOW)
    timed = time_queue(player, head, [], insert=(0, songs[5]))
    assert timed.starts == (pytest.approx(NOW + 145),)
    assert [entry for entry, _at in timed.unplayed] == [songs[5]]


def test_a_fixed_render_keeps_its_own_speed(tmp_path: Path) -> None:
    """A new speed setting applies to unrendered songs, not the render on the air."""
    source, songs = _station(tmp_path, playback={"music_speed_percent": 10})
    player = source.player
    _playing(source, songs[0], NOW - 50, 50)
    player.bus = object()
    player._playing_render = _render(songs[0], 200, None, 0, playback_rate=0.9)
    player._playback_pos[0] = round(50 * 0.9 * SR)
    head = read_head(player, now=NOW)
    timed = time_queue(player, head, [songs[1]], insert=(1, songs[2]))
    assert timed.starts[0] == pytest.approx(NOW + 150)
    assert timed.starts[1] == pytest.approx(NOW + 150 + 200 / 1.1)


def test_the_bridge_times_queue_checks_like_the_preview(tmp_path: Path) -> None:
    source, songs = _station(tmp_path)
    player = source.player
    _playing(source, songs[0], NOW - 30, 150)
    player._state.clock = lambda: NOW
    bridge = PlayerBridge(player=player, sim=MagicMock())
    records, when = bridge.history_ahead([songs[1]], 1, songs[2])
    assert when == pytest.approx(NOW + 50 + _remaining(player, songs[1], 0.0, 200.0))
    assert [record.path for record in records][-2:] == [songs[0].path, songs[1].path]
    assert records[-1].started == pytest.approx(NOW + 50)
