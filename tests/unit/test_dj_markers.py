"""Intro and outro markers from DJ software move the mix points.

The importers turn each program's markers into ``intro_start``,
``intro_end``, ``outro_start`` and ``outro_end`` cues, and
:func:`autodj.dj_meta.apply_imported_markers` copies them onto the
track's ``intro_start_s``, ``intro_end_s`` and ``outro_start_s``.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from autodj.dj_cues_import import (
    import_from_mixxx,
    import_from_traktor_nml,
    merge_imported_cues,
    remerge_library_cues,
)
from autodj.dj_meta import (
    Cue,
    DjMeta,
    apply_imported_markers,
    imported_markers,
    merge_cues,
)

_SR = 44100


def _mixxx_db(path: Path, rows: list[tuple[float, int, float, str]]) -> Path:
    """A Mixxx library with a ``length`` column: rows of (start s, type, length s, label).

    A start of -1 is Mixxx's unset position.
    """
    con = sqlite3.connect(path)
    con.executescript(
        """
        CREATE TABLE library (id INTEGER PRIMARY KEY, location INTEGER, samplerate INTEGER);
        CREATE TABLE track_locations (id INTEGER PRIMARY KEY, location TEXT);
        CREATE TABLE cues (
            id INTEGER PRIMARY KEY, track_id INTEGER, type INTEGER, position REAL,
            length REAL, hotcue INTEGER, label TEXT
        );
        INSERT INTO track_locations VALUES (1, '/music/a.flac');
        INSERT INTO library VALUES (1, 1, 44100);
        """
    )
    for start, ctype, length, label in rows:
        position = -1.0 if start < 0 else start * _SR * 2
        con.execute(
            "INSERT INTO cues (track_id, type, position, length, hotcue, label) "
            "VALUES (1, ?, ?, ?, -1, ?)",
            (ctype, position, length * _SR * 2, label),
        )
    con.commit()
    con.close()
    return path


def _cues_by_type(cues: list[Cue]) -> dict[str, float]:
    return {c.type: pytest.approx(c.time_s) for c in cues}


class TestMixxxRanges:
    def test_intro_and_outro_ranges_give_start_and_end(self, tmp_path: Path) -> None:
        db = _mixxx_db(tmp_path / "m.db", [(4.0, 6, 12.0, ""), (200.0, 7, 16.0, "")])
        cues = import_from_mixxx(db)[str(Path("/music/a.flac"))]
        assert _cues_by_type(cues) == {
            "intro_start": 4.0,
            "intro_end": 16.0,
            "outro_start": 200.0,
            "outro_end": 216.0,
        }
        assert {c.source for c in cues} == {"mixxx"}

    def test_intro_with_only_an_end(self, tmp_path: Path) -> None:
        # Mixxx stores an unset start as -1 and the end as the length alone.
        db = _mixxx_db(tmp_path / "m.db", [(-1, 6, 30.0, "")])
        cues = import_from_mixxx(db)[str(Path("/music/a.flac"))]
        assert _cues_by_type(cues) == {"intro_end": 30.0}

    def test_intro_without_an_end(self, tmp_path: Path) -> None:
        db = _mixxx_db(tmp_path / "m.db", [(4.0, 6, 0.0, "")])
        assert _cues_by_type(import_from_mixxx(db)[str(Path("/music/a.flac"))]) == {
            "intro_start": 4.0
        }

    def test_main_cue_and_loops_stay_single_points(self, tmp_path: Path) -> None:
        db = _mixxx_db(tmp_path / "m.db", [(1.0, 2, 0.0, ""), (60.0, 4, 8.0, "Loop")])
        cues = import_from_mixxx(db)[str(Path("/music/a.flac"))]
        assert [(c.type, c.time_s) for c in cues] == [
            ("first_downbeat", pytest.approx(1.0)),
            ("user", pytest.approx(60.0)),
        ]


def test_traktor_fade_points_are_intro_start_and_outro_start(tmp_path: Path) -> None:
    nml = tmp_path / "collection.nml"
    nml.write_text(
        '<?xml version="1.0"?><NML VERSION="19"><COLLECTION>'
        '<ENTRY><LOCATION DIR="/:music/:" FILE="a.mp3" VOLUME=""/>'
        '<CUE_V2 NAME="in" TYPE="1" START="8000"/>'
        '<CUE_V2 NAME="out" TYPE="2" START="190000"/>'
        '<CUE_V2 NAME="load" TYPE="3" START="500"/>'
        "</ENTRY></COLLECTION></NML>",
        encoding="utf-8",
    )
    cues = next(iter(import_from_traktor_nml(nml).values()))
    assert _cues_by_type(cues) == {
        "first_downbeat": 0.5,
        "intro_start": 8.0,
        "outro_start": 190.0,
    }


class TestImportedMarkers:
    def test_detected_cues_are_not_markers(self) -> None:
        assert imported_markers([Cue(5.0, "intro_start", source="auto")]) == {}

    def test_user_beats_dj_software(self) -> None:
        cues = [Cue(5.0, "intro_start", source="mixxx"), Cue(7.0, "intro_start", source="user")]
        assert imported_markers(cues) == {"intro_start": 7.0}

    def test_earliest_wins_between_equal_sources(self) -> None:
        cues = [
            Cue(9.0, "outro_start", source="traktor"),
            Cue(8.0, "outro_start", source="rekordbox"),
        ]
        assert imported_markers(cues) == {"outro_start": 8.0}

    def test_other_cue_types_are_ignored(self) -> None:
        assert imported_markers([Cue(5.0, "first_downbeat", source="mixxx")]) == {}


class TestApplyImportedMarkers:
    def test_markers_replace_the_detected_mix_points(self) -> None:
        meta = DjMeta(
            intro_start_s=0.4,
            intro_end_s=10.0,
            outro_start_s=170.0,
            analysed=True,
            cues=[
                Cue(2.0, "intro_start", source="mixxx"),
                Cue(18.0, "intro_end", source="mixxx"),
                Cue(185.0, "outro_start", source="mixxx"),
                Cue(200.0, "outro_end", source="mixxx"),
            ],
        )
        apply_imported_markers(meta)
        assert (meta.intro_start_s, meta.intro_end_s, meta.outro_start_s) == (2.0, 18.0, 185.0)

    def test_fields_without_a_marker_keep_the_detected_value(self) -> None:
        meta = DjMeta(intro_start_s=0.4, intro_end_s=10.0, outro_start_s=170.0)
        meta.cues = [Cue(185.0, "outro_start", source="rekordbox")]
        apply_imported_markers(meta)
        assert (meta.intro_start_s, meta.intro_end_s, meta.outro_start_s) == (0.4, 10.0, 185.0)

    def test_detected_intro_end_before_the_marked_start_is_dropped(self) -> None:
        meta = DjMeta(intro_start_s=0.4, intro_end_s=10.0, outro_start_s=170.0)
        meta.cues = [Cue(30.0, "intro_start", source="traktor")]
        apply_imported_markers(meta)
        assert (meta.intro_start_s, meta.intro_end_s) == (30.0, 0.0)

    def test_detected_intro_start_after_the_marked_end_is_dropped(self) -> None:
        meta = DjMeta(intro_start_s=6.0, intro_end_s=10.0)
        meta.cues = [Cue(5.0, "intro_end", source="mixxx")]
        apply_imported_markers(meta)
        assert (meta.intro_start_s, meta.intro_end_s) == (0.0, 5.0)

    def test_no_markers_changes_nothing(self) -> None:
        meta = DjMeta(intro_start_s=0.4, intro_end_s=10.0, outro_start_s=170.0)
        meta.cues = [Cue(30.0, "drop", source="auto"), Cue(60.0, "user", source="serato")]
        apply_imported_markers(meta)
        assert (meta.intro_start_s, meta.intro_end_s, meta.outro_start_s) == (0.4, 10.0, 170.0)


def test_merge_keeps_a_marker_over_a_main_cue_at_the_same_time() -> None:
    # Mixxx puts the main cue and the intro start on the same first sound.
    merged = merge_cues(
        [Cue(1.0, "first_downbeat", source="mixxx")], [Cue(1.0, "intro_start", source="mixxx")]
    )
    assert [c.type for c in merged] == ["intro_start"]
    merged = merge_cues(
        [Cue(1.0, "intro_start", source="mixxx")], [Cue(1.1, "first_downbeat", source="mixxx")]
    )
    assert [c.type for c in merged] == ["intro_start"]


def test_merge_imported_cues_moves_the_mix_points(tmp_path: Path) -> None:
    path = str(tmp_path / "a.flac")  # no file: no Serato tags
    meta = DjMeta(intro_start_s=0.0, intro_end_s=8.0, outro_start_s=150.0, analysed=True)
    library = {
        path: [
            Cue(3.0, "intro_start", source="mixxx"),
            Cue(20.0, "intro_end", source="mixxx"),
            Cue(160.0, "outro_start", source="mixxx"),
        ]
    }
    merge_imported_cues(meta, path, library)
    assert (meta.intro_start_s, meta.intro_end_s, meta.outro_start_s) == (3.0, 20.0, 160.0)


class TestRemergeLibraryCues:
    def test_old_library_cues_are_replaced_and_markers_applied(self) -> None:
        # Stored before the readers knew intro markers: the intro came in as
        # a first_downbeat and did not move the intro.
        stored = DjMeta(
            intro_start_s=0.2,
            intro_end_s=8.0,
            outro_start_s=150.0,
            analysed=True,
            cues=[
                Cue(4.0, "first_downbeat", source="mixxx"),
                Cue(30.0, "drop", source="auto"),
                Cue(60.0, "user", source="serato"),
            ],
        )
        fresh = [Cue(4.0, "intro_start", source="mixxx"), Cue(16.0, "intro_end", source="mixxx")]
        updated = remerge_library_cues(stored, fresh)
        assert updated is not None
        assert [(c.type, c.source) for c in updated.cues] == [
            ("intro_start", "mixxx"),
            ("intro_end", "mixxx"),
            ("drop", "auto"),
            ("user", "serato"),
        ]
        assert (updated.intro_start_s, updated.intro_end_s) == (4.0, 16.0)
        assert stored.intro_end_s == 8.0  # the stored meta is left alone

    def test_unchanged_track_gives_none(self) -> None:
        cues = [Cue(4.0, "intro_start", source="mixxx")]
        stored = DjMeta(intro_start_s=4.0, intro_end_s=8.0, analysed=True, cues=list(cues))
        assert remerge_library_cues(stored, cues) is None
