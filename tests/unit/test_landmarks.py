"""Spoken track landmarks (#60): extraction, naming, merging and the route."""

from __future__ import annotations

import asyncio
import os
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from autodj.dj_meta import Cue, DjMeta
from autodj.landmarks import (
    LandmarkCache,
    TrackFacts,
    bar_number,
    build_landmarks,
    station_landmarks,
)
from autodj.library import LibraryManager
from autodj.server import create_app
from tests.unit.test_library import Similarity, make_config, make_entry

# 120 BPM from 0.5 s: a beat every half second, a bar every 2 s.
BEATS = [0.5 + 0.5 * i for i in range(480)]


def _meta(*cues: Cue, beats=BEATS, intro=0.0, outro=0.0, start=None) -> DjMeta:
    return DjMeta(
        intro_end_s=intro,
        outro_start_s=outro,
        beats=list(beats),
        analysed=True,
        cues=list(cues),
        intro_start_s=start,
    )


def _summary(found):
    return [(lm.name, lm.time_s, lm.bar) for lm in found]


def test_bar_number_counts_four_beats_a_bar_from_the_first_beat():
    assert bar_number(BEATS, 0.5) == 1
    assert bar_number(BEATS, 2.4) == 1
    assert bar_number(BEATS, 2.5) == 2
    # A moment a hair before a beat is on it, not in the bar before.
    assert bar_number(BEATS, 96.48) == 49
    # Just before the grid starts (less than a bar) is bar 1; long before, no bar.
    assert bar_number(BEATS, 0.0) == 1
    assert bar_number([10 + 0.5 * i for i in range(16)], 0.0) is None
    # No usable grid, no bar numbers.
    assert bar_number(BEATS[:4], 1.0) is None
    assert bar_number([], 1.0) is None


def test_a_song_reads_as_intro_vocals_drop_breakdown_outro():
    meta = _meta(
        Cue(32.5, "first_downbeat"),
        Cue(32.5, "phrase"),
        Cue(48.5, "phrase"),
        Cue(96.5, "drop"),
        Cue(96.5, "phrase"),
        Cue(160.0, "breakdown"),
        Cue(229.0, "outro_downbeat"),
    )
    found = build_landmarks(
        TrackFacts(
            length=250.0,
            points={"start": 0.0, "intro": 32.5, "outro": 230.5, "segue": 240.0, "end": 250.0},
            point_sources={"intro": "analysis", "outro": "user", "segue": "analysis"},
            meta=meta,
            sung=((45.0, 49.0), (50.0, 54.0)),
        )
    )
    assert _summary(found) == [
        ("Intro", 0.0, 1),
        ("Intro ends", 32.5, 17),
        ("Vocals", 45.0, 23),
        ("Phrase", 48.5, 25),
        ("Drop", 96.5, 49),
        ("Breakdown", 160.0, 80),
        ("Outro", 230.5, 116),
        ("Segue", 240.0, 120),
    ]
    intro, _end, vocals, phrase, drop, _breakdown, outro, _segue = found
    assert (intro.end_s, intro.end_bar, intro.source) == (32.5, 17, "analysis")
    assert (outro.end_s, outro.source) == (250.0, "user")
    assert vocals.source == "lyrics"
    # Phrases are stops for the keys, not part of the structure.
    assert not phrase.major and drop.major
    assert drop.as_dict() == {
        "time_s": 96.5,
        "kind": "drop",
        "kinds": ["drop"],
        "name": "Drop",
        "source": "analysis",
        "major": True,
        "bar": 49,
        "end_s": None,
        "end_bar": None,
    }


def test_a_mark_set_by_hand_wins_and_names_the_landmark():
    meta = _meta(
        Cue(96.5, "drop"),
        Cue(96.9, "user", label="  Drop\n2 ", source="rekordbox"),
        Cue(120.0, "hotcue", label="", source="serato"),
        Cue(140.0, "user", label="Verse", source="user"),
        Cue(160.0, "breakdown"),
        Cue(160.3, "user", label="", source="mixxx"),
        Cue(200.0, "loop", label="", source="traktor"),
        Cue(210.0, "user", label="build up", source="user"),
        Cue(5.0, "user", label="Broken", source="user", color="#fff"),
    )
    found = build_landmarks(TrackFacts(length=250.0, points={}, meta=meta))
    by_time = {lm.time_s: lm for lm in found}
    drop = by_time[96.9]
    assert (drop.name, drop.kind, drop.source) == ("Drop 2", "drop", "imported")
    # An unnamed DJ-software cue is a stop of its own, never part of the structure.
    assert (by_time[120.0].name, by_time[120.0].major) == ("Cue", False)
    assert (by_time[140.0].name, by_time[140.0].kind, by_time[140.0].major) == (
        "Verse",
        "cue",
        True,
    )
    # An unnamed cue on the breakdown adds nothing.
    assert by_time[160.0].name == "Breakdown"
    assert by_time[200.0].name == "Loop"
    assert (by_time[210.0].kind, by_time[210.0].name) == ("build", "build up")
    assert by_time[5.0].name == "Broken"


def test_marks_that_fall_together_merge_into_one_named_landmark():
    meta = _meta(Cue(96.5, "drop"), Cue(96.5, "phrase"))
    found = build_landmarks(
        TrackFacts(
            length=250.0,
            points={"intro": 30.0, "outro": 96.7},
            meta=meta,
            sung=((30.2, 33.0), (96.6, 100.0)),
        )
    )
    # The singing at the intro's end says the intro is over.
    assert [lm.name for lm in found] == ["Intro", "Vocals", "Drop and outro"]
    merged = found[-1]
    assert merged.kinds == ["drop", "outro"]
    assert merged.end_s == 250.0


def test_without_cue_points_the_analysis_gives_intro_and_outro():
    meta = _meta(
        Cue(3.0, "intro_start", source="mixxx"),
        Cue(250.0, "outro_end", source="mixxx"),
        intro=16.5,
        outro=200.5,
        start=0.4,
    )
    found = build_landmarks(TrackFacts(length=250.0, points=None, meta=meta))
    assert _summary(found) == [
        ("Intro", 0.4, 1),
        ("Intro ends", 16.5, 9),
        ("Outro", 200.5, 101),
    ]
    # An analysis with no outro (outro at the very end) gives no outro.
    flat = build_landmarks(TrackFacts(length=250.0, meta=_meta(intro=0.0, outro=250.0, start=3.0)))
    assert _summary(flat) == [("Start", 3.0, 2)]


def test_nothing_known_or_out_of_range_gives_nothing():
    assert build_landmarks(TrackFacts(length=200.0)) == []
    assert build_landmarks(TrackFacts(length=200.0, meta=DjMeta(cues=[Cue(5.0, "drop")]))) == []
    bad = _meta(Cue(-1.0, "drop"), Cue(500.0, "drop"), Cue(float("nan"), "drop"))
    points = {"intro": True, "outro": "x", "segue": 250.0, "end": 250.0}
    assert build_landmarks(TrackFacts(length=250.0, points=points, meta=bad)) == []


def test_cache_keeps_an_answer_only_while_its_facts_are_the_same():
    cache = LandmarkCache(size=2)
    meta = _meta()
    answer = {"landmarks": []}
    cache.put("a", (1, 200.0, "music", meta, ()), answer)
    assert cache.get("a", (1, 200.0, "music", meta, ())) is answer
    # Another analysis object, or another records counter, is a miss.
    assert cache.get("a", (1, 200.0, "music", _meta(), ())) is None
    assert cache.get("a", (2, 200.0, "music", meta, ())) is None
    assert cache.get("a", (1, 200.0, "music", meta)) is None
    cache.put("b", (None,), {})
    cache.put("c", (None,), {})
    assert cache.get("a", (1, 200.0, "music", meta, ())) is None


@pytest.fixture
def station(tmp_path):
    cfg = make_config(tmp_path)
    manager = LibraryManager(cfg, Similarity())
    manager.catalog.add_source(cfg.library.music_dir)
    manager.start_scan = lambda *a, **k: True
    music = cfg.library.music_dir / "song.flac"
    music.touch()
    manager.catalog.put_entry(make_entry(music), points={"start": 0, "intro": 16.5, "end": 180})
    liner_dir = tmp_path / "Liners"
    liner_dir.mkdir()
    manager.catalog.add_source(liner_dir, kind="liner")
    liner = liner_dir / "id.flac"
    liner.touch()
    manager.catalog.put_entry(make_entry(liner), kind="liner")
    manager.refresh()
    # Keyed as the DJ-meta cache keys paths: case does not count on Windows.
    metas = {os.path.normcase(str(music)): _meta(Cue(96.5, "drop"))}
    reads: list[str] = []

    class Cache:
        def get(self, path):
            return metas.get(os.path.normcase(path), DjMeta())

    def sung(path):
        reads.append(os.path.normcase(path))
        return ((20.0, 24.0),)

    player = SimpleNamespace(_ensure_dj_cache=lambda: None, _dj_cache=Cache(), _sung_spans=sung)
    bridge = SimpleNamespace(player=player, library=manager)
    yield SimpleNamespace(bridge=bridge, music=music, liner=liner, reads=reads, metas=metas)
    manager.close()


def test_station_answer_names_the_song_structure_and_is_cached(station):
    answer = station_landmarks(station.bridge, str(station.music))
    assert answer["kind"] == "music"
    assert (answer["length"], answer["bpm"]) == (180.0, 120.0)
    assert (answer["analysed"], answer["bars"], answer["lyrics"]) == (True, True, True)
    assert [lm["name"] for lm in answer["landmarks"]] == ["Intro", "Intro ends", "Vocals", "Drop"]
    assert station_landmarks(station.bridge, str(station.music)) is answer
    # Saving a cue point anywhere changes the catalog's records counter.
    station.bridge.library.catalog.set_points(station.music, {"outro": 150.0})
    again = station_landmarks(station.bridge, str(station.music))
    assert again is not answer
    assert [lm["name"] for lm in again["landmarks"]][-1] == "Outro"
    # New analysis is a new answer too.
    station.metas[os.path.normcase(str(station.music))] = _meta(Cue(60.0, "breakdown"))
    names = [
        lm["name"] for lm in station_landmarks(station.bridge, str(station.music))["landmarks"]
    ]
    assert "Breakdown" in names and "Drop" not in names


def test_station_answer_for_a_liner_is_empty_and_reads_no_lyrics(station):
    answer = station_landmarks(station.bridge, str(station.liner))
    assert (answer["kind"], answer["landmarks"], answer["analysed"]) == ("liner", [], False)
    assert os.path.normcase(str(station.liner)) not in station.reads


def test_station_answer_survives_a_broken_analysis_cache_and_lyrics(station):
    def broken(*_args):
        raise OSError("gone")

    station.bridge.player._dj_cache = SimpleNamespace(get=broken)
    station.bridge.player._sung_spans = broken
    answer = station_landmarks(station.bridge, str(station.music))
    assert (answer["analysed"], answer["lyrics"]) == (False, False)
    assert [lm["name"] for lm in answer["landmarks"]] == ["Intro", "Intro ends"]


def test_station_answer_without_a_catalog_uses_the_index(station):
    entry = make_entry(station.music)
    bridge = SimpleNamespace(
        player=station.bridge.player,
        library=None,
        entry_for_path=lambda path: entry if path == entry.path else None,
    )
    answer = station_landmarks(bridge, entry.path)
    assert [lm["name"] for lm in answer["landmarks"]] == ["Vocals", "Drop"]
    with pytest.raises(LookupError):
        station_landmarks(bridge, "elsewhere.flac")


def test_route_answers_404_for_a_path_outside_the_library(station, tmp_path):
    outside = tmp_path / "secret.flac"
    outside.touch()
    with pytest.raises(LookupError):
        station_landmarks(station.bridge, str(outside))
    app = create_app(
        SimpleNamespace(
            player=SimpleNamespace(_cfg=station.bridge.library.cfg), library=station.bridge.library
        )
    )
    endpoint = next(
        route.endpoint for route in app.routes if getattr(route, "path", "") == "/api/landmarks"
    )
    with pytest.raises(HTTPException) as missing:
        asyncio.run(endpoint(SimpleNamespace(bridge=station.bridge), str(outside)))
    assert missing.value.status_code == 404
    # Nothing outside the library is ever read.
    assert os.path.normcase(str(outside)) not in station.reads
    found = asyncio.run(endpoint(SimpleNamespace(bridge=station.bridge), str(station.music)))
    assert found["landmarks"]
