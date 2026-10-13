"""Play log export: range filtering, columns, CSV safety and the missing-field report."""

from __future__ import annotations

import csv
import io
import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path

import pytest

from autodj import audio_meta
from autodj import catalog as catalog_module
from autodj.catalog import LibraryCatalog
from autodj.indexer import IndexEntry
from autodj.playlog import (
    COLUMNS,
    MISSING_COLUMNS,
    SOUNDEXCHANGE_COLUMNS,
    Play,
    missing_csv,
    missing_fields,
    parse_bound,
    plays_between,
    plays_csv,
    soundexchange_filename,
    soundexchange_report,
    summary,
    unmatchable_count,
)

T0 = datetime(2026, 9, 1, 10, 0, tzinfo=UTC)


def _entry(path: Path, title: str, album: str = "Album") -> IndexEntry:
    return IndexEntry(str(path), title, "Artist", album, "", 120, 2000, 180, 0.2, -1, -1, 0.0)


def _row(event_id: str, path: Path | str, title: str, at: datetime, duration: float = 180.0):
    return {
        "event_id": event_id,
        "path": str(path),
        "title": title,
        "artist": "Artist",
        "duration": duration,
        "played_at": at.isoformat(),
    }


def _tags(values: dict[str, tuple[str, str]] | None = None):
    calls: list[str] = []

    def read(path: str) -> tuple[str, str]:
        calls.append(path)
        return (values or {}).get(Path(path).name.lower(), ("", ""))

    return read, calls


@pytest.fixture
def library(tmp_path: Path):
    music, liners = tmp_path / "Music", tmp_path / "Liners"
    (music / "Rock").mkdir(parents=True)
    liners.mkdir()
    song = music / "Rock" / "song.flac"
    other = music / "other.mp3"
    liner = liners / "id.wav"
    loose = liners / "unindexed.wav"
    for path in (song, other, liner, loose):
        path.write_bytes(b"audio")
    catalog = LibraryCatalog(tmp_path / "library.db")
    catalog.add_source(music)
    catalog.add_source(liners, "liner", recursive=False, advanced=False)
    catalog.put_entry(_entry(song, "Song", album="First Album"))
    catalog.put_entry(_entry(other, "Other", album=""))
    catalog.put_entry(_entry(liner, "ID"), "liner")
    try:
        yield catalog, {"song": song, "other": other, "liner": liner, "loose": loose}
    finally:
        catalog.close()


class TestParseBound:
    def test_date_start_is_local_midnight_and_end_is_next_midnight(self) -> None:
        start = parse_bound("2026-09-01")
        end = parse_bound(" 2026-09-01 ", end=True)
        assert start.tzinfo is not None
        assert (start.year, start.month, start.day, start.hour) == (2026, 9, 1, 0)
        assert (end.month, end.day, end.hour) == (9, 2, 0)

    def test_datetime_keeps_its_offset_and_naive_is_local(self) -> None:
        assert parse_bound("2026-09-01T10:30:00+02:00").utcoffset() == timedelta(hours=2)
        naive = parse_bound("2026-09-01T10:30", end=True)
        assert naive.tzinfo is not None
        assert (naive.hour, naive.minute) == (10, 30)

    @pytest.mark.parametrize("text", ["", "yesterday", "2026-13-01", "01/09/2026"])
    def test_rejects_other_text(self, text: str) -> None:
        with pytest.raises(ValueError, match="YYYY-MM-DD"):
            parse_bound(text)


class TestPlaysBetween:
    def test_filters_by_start_time_and_measures_play_to_next_start(self, library) -> None:
        catalog, paths = library
        catalog.write_history(
            [
                _row("before", paths["other"], "Before", T0 - timedelta(minutes=10)),
                _row("a", paths["song"], "Song", T0),
                # Skipped after 60 s: the next play starts first.
                _row("b", paths["other"], "Other", T0 + timedelta(seconds=170)),
                _row("c", paths["song"], "Song", T0 + timedelta(seconds=230)),
                _row("after", paths["other"], "After", T0 + timedelta(hours=2)),
            ]
        )
        read, calls = _tags()
        plays = plays_between(catalog, T0, T0 + timedelta(hours=1), read_tags=read)
        assert [p.title for p in plays] == ["Song", "Other", "Song"]
        assert [p.duration for p in plays] == [170, 60, 180]
        assert plays[0].start == T0
        assert plays[0].end == T0 + timedelta(seconds=170)
        assert len(calls) == 2  # each file's tags are read once

    def test_end_bound_is_exclusive(self, library) -> None:
        catalog, paths = library
        catalog.write_history([_row("a", paths["song"], "Song", T0)])
        read, _ = _tags()
        assert plays_between(catalog, T0 - timedelta(hours=1), T0, read_tags=read) == []
        assert len(plays_between(catalog, T0, T0 + timedelta(seconds=1), read_tags=read)) == 1

    def test_whole_local_day_from_dates(self, library) -> None:
        catalog, paths = library
        late = datetime(2026, 9, 1, 23, 59).astimezone()
        catalog.write_history(
            [
                _row("a", paths["song"], "Late", late),
                _row("b", paths["song"], "Next day", late + timedelta(minutes=2)),
            ]
        )
        read, _ = _tags()
        plays = plays_between(
            catalog, parse_bound("2026-09-01"), parse_bound("2026-09-01", end=True), read_tags=read
        )
        assert [p.title for p in plays] == ["Late"]
        assert plays[0].duration == 120

    def test_range_returns_only_matches_with_following_play_outside_range(self, library):
        catalog, paths = library
        catalog.write_history(
            [
                _row("old", paths["song"], "Old", T0 - timedelta(days=100)),
                _row("in", paths["song"], "In", T0),
                _row("cut", paths["song"], "Interrupted", T0 + timedelta(seconds=10)),
                _row("next", paths["other"], "Next", T0 + timedelta(seconds=30)),
                _row("future", paths["song"], "Future", T0 + timedelta(days=100)),
            ]
        )
        catalog.write_history([], interrupted=["cut"])
        rows = catalog.play_log(start=T0, end=T0 + timedelta(seconds=20))
        assert [row["title"] for row in rows] == ["In"]
        assert datetime.fromisoformat(rows[0]["following_at"]) == T0 + timedelta(seconds=30)
        read, _ = _tags()
        assert (
            plays_between(catalog, T0, T0 + timedelta(seconds=20), read_tags=read)[0].duration == 30
        )
        assert len(catalog.play_log()) == 4  # stored history was not trimmed

    def test_range_preserves_offsets_precision_and_insertion_order(self, library):
        catalog, paths = library
        offset = timezone(timedelta(hours=-4))
        catalog.write_history(
            [
                _row("offset", paths["song"], "Offset", T0.astimezone(offset)),
                _row("earlier", paths["song"], "Earlier", T0 - timedelta(seconds=1)),
                _row("micro", paths["song"], "Micro", T0 + timedelta(microseconds=1)),
                _row("end", paths["song"], "End", T0 + timedelta(microseconds=2)),
            ]
        )
        rows = catalog.play_log(start=T0, end=T0 + timedelta(microseconds=2))
        assert [row["title"] for row in rows] == ["Offset", "Micro"]
        # The following play is by insertion order, not a timestamp sort.
        assert datetime.fromisoformat(rows[0]["following_at"]) == T0 - timedelta(seconds=1)

    def test_range_accepts_legacy_naive_dates_and_keeps_stations_separate(self, library):
        catalog, paths = library
        local = datetime(2026, 9, 1, 12, 0)
        catalog.write_history([_row("legacy", paths["song"], "Local", local)])
        catalog.write_history(
            [_row("other", paths["song"], "Other station", local)], station="other"
        )
        start = local.astimezone()
        rows = catalog.play_log(start=start, end=start + timedelta(seconds=1))
        assert [row["title"] for row in rows] == ["Local"]
        assert rows[0]["following_at"] is None

    def test_report_snapshot_does_not_block_play_history_writes(self, library, monkeypatch):
        catalog, paths = library
        catalog.write_history([_row("old", paths["song"], "Old", T0)])
        entered, release = threading.Event(), threading.Event()
        parse = catalog_module.history_start

        def slow_parse(value):
            entered.set()
            assert release.wait(5)
            return parse(value)

        monkeypatch.setattr(catalog_module, "history_start", slow_parse)
        with ThreadPoolExecutor(max_workers=2) as workers:
            report = workers.submit(catalog.play_log, start=T0, end=T0 + timedelta(hours=1))
            try:
                assert entered.wait(5)
                write = workers.submit(
                    catalog.write_history,
                    [_row("new", paths["other"], "New", T0 + timedelta(seconds=30))],
                )
                write.result(timeout=5)
            finally:
                release.set()
            rows = report.result(timeout=5)
        assert [row["title"] for row in rows] == ["Old"]
        assert rows[0]["following_at"] is None  # same snapshot, before the concurrent write

    def test_range_requires_both_bounds(self, library):
        catalog, _ = library
        with pytest.raises(ValueError, match="Both"):
            catalog.play_log(start=T0)
        with pytest.raises(ValueError, match="after its start"):
            catalog.play_log(start=T0, end=T0)

    def test_columns_come_from_history_catalog_and_tags(self, library) -> None:
        catalog, paths = library
        catalog.write_history(
            [
                _row("a", paths["song"], "Song", T0),
                _row("b", paths["liner"], "ID", T0 + timedelta(minutes=5), duration=4),
                _row("c", paths["loose"], "Loose", T0 + timedelta(minutes=6), duration=3),
                _row("d", paths["other"], "Other", T0 + timedelta(minutes=7)),
            ]
        )
        read, _ = _tags({"song.flac": ("USRC17607839", "Label One")})
        song, liner, loose, other = plays_between(
            catalog, T0, T0 + timedelta(hours=1), read_tags=read
        )
        assert (song.artist, song.title, song.album) == ("Artist", "Song", "First Album")
        assert (song.isrc, song.label, song.kind) == ("USRC17607839", "Label One", "song")
        assert song.path == "Rock/song.flac"
        assert (liner.kind, liner.path, liner.duration) == ("liner", "id.wav", 4)
        assert (loose.kind, loose.album) == ("liner", "")  # no record, in a liner folder
        assert (other.kind, other.path) == ("song", "other.mp3")

    def test_moved_file_gives_its_new_path(self, library, tmp_path: Path) -> None:
        catalog, paths = library
        catalog.write_history([_row("a", paths["song"], "Song", T0)])
        moved = tmp_path / "Music" / "Moved.flac"
        paths["song"].rename(moved)
        assert catalog.rekey(paths["song"], moved)
        read, calls = _tags()
        (play,) = plays_between(catalog, T0, T0 + timedelta(hours=1), read_tags=read)
        assert play.path == "Moved.flac"
        assert Path(calls[0]).name.lower() == "moved.flac"

    def test_interrupted_and_migrated_rows(self, library, tmp_path: Path) -> None:
        catalog, paths = library
        catalog.write_history(
            [
                _row("cut", paths["song"], "Cut short", T0),
                _row("kept", paths["song"], "Kept", T0 + timedelta(minutes=10)),
            ],
            interrupted=["cut"],
        )
        with closing(sqlite3.connect(tmp_path / "library.db")) as db, db:
            db.execute(
                "INSERT INTO play_history (event_id,title,artist,duration,played_at) "
                "VALUES ('old','Old','Someone',200,?)",
                ((T0 + timedelta(minutes=20)).isoformat(),),
            )
            db.execute(
                "INSERT INTO play_history (event_id,title,artist,duration,played_at) "
                "VALUES ('bad','Bad','Someone',200,'not a time')"
            )
        read, calls = _tags()
        kept, old = plays_between(catalog, T0, T0 + timedelta(hours=1), read_tags=read)
        assert kept.title == "Kept"
        assert (old.title, old.path, old.album, old.kind, old.duration) == (
            "Old",
            "",
            "",
            "song",
            200,
        )
        assert len(calls) == 1  # no path, no tag read

    def test_path_outside_every_folder_stays_absolute(self, library, tmp_path: Path) -> None:
        catalog, _ = library
        stray = tmp_path / "elsewhere" / "stray.mp3"
        catalog.write_history([_row("a", stray, "Stray", T0)])
        read, _ = _tags()
        (play,) = plays_between(catalog, T0, T0 + timedelta(hours=1), read_tags=read)
        assert play.path.endswith("elsewhere/stray.mp3")
        assert Path(play.path).is_absolute()

    def test_range_must_move_forward(self, library) -> None:
        catalog, _ = library
        with pytest.raises(ValueError, match="after its start"):
            plays_between(catalog, T0, T0)


def _play(**fields) -> Play:
    values = {
        "start": T0,
        "end": T0 + timedelta(seconds=200),
        "artist": "Artist",
        "title": "Title",
        "album": "Album",
        "isrc": "USRC17607839",
        "label": "Label",
        "kind": "song",
        "path": "a/b.flac",
        "performances": 1,
    }
    values.update(fields)
    return Play(**values)


class TestCsv:
    def test_header_and_row_content(self) -> None:
        text = plays_csv([_play(artist="Beyoncé, Jay-Z", title='Say "Hi"')])
        rows = list(csv.reader(io.StringIO(text, newline="")))
        assert tuple(rows[0]) == COLUMNS
        start, end, seconds, artist, title, album, isrc, label, kind, path, heard = rows[1]
        assert datetime.fromisoformat(start) == T0
        assert datetime.fromisoformat(start).utcoffset() is not None
        assert datetime.fromisoformat(end) == T0 + timedelta(seconds=200)
        assert seconds == "200"
        assert (artist, title, album) == ("Beyoncé, Jay-Z", 'Say "Hi"', "Album")
        assert (isrc, label, kind, path, heard) == (
            "USRC17607839",
            "Label",
            "song",
            "a/b.flac",
            "1",
        )
        assert text.endswith("\r\n")
        assert not text.startswith("﻿")

    @pytest.mark.parametrize("value", ["=HYPERLINK(1)", "+1", "-1+2", "@SUM(A1)", "\tx", "\rx"])
    def test_formula_like_cells_become_text(self, value: str) -> None:
        text = plays_csv([_play(title=value, artist=value)])
        row = list(csv.reader(io.StringIO(text, newline="")))[1]
        assert row[3] == row[4] == "'" + value

    def test_ordinary_cells_are_unchanged(self) -> None:
        row = list(csv.reader(io.StringIO(plays_csv([_play(title="a=b")]), newline="")))[1]
        assert row[4] == "a=b"

    def test_empty_export_has_only_the_header(self) -> None:
        assert plays_csv([]) == ",".join(COLUMNS) + "\r\n"


class TestMissingFields:
    def test_lists_each_incomplete_song_once_sorted(self) -> None:
        plays = [
            _play(artist="zed", title="Last", isrc="", path="z.flac"),
            _play(artist="Abba", title="Song", label="", album="", path="a.flac"),
            _play(artist="Abba", title="Song", label="", album="", path="a.flac"),
            _play(artist="Abba", title="Complete", path="c.flac"),
            _play(artist="", title="ID", isrc="", kind="liner", path="id.wav"),
        ]
        assert missing_fields(plays) == [
            ("Abba", "Song", "a.flac", "album; label", 2, "yes"),
            ("zed", "Last", "z.flac", "isrc", 1, "yes"),
        ]
        rows = list(csv.reader(io.StringIO(missing_csv(plays), newline="")))
        assert tuple(rows[0]) == MISSING_COLUMNS
        assert rows[1] == ["Abba", "Song", "a.flac", "album; label", "2", "yes"]

    def test_flags_songs_soundexchange_cannot_match(self) -> None:
        plays = [
            _play(title="No ISRC, no label", isrc="", label="", path="a"),
            _play(title="No ISRC, no album", isrc="", album="", path="b"),
            _play(title="Album and label", isrc="", path="c"),
        ]
        assert [p.matchable for p in plays] == [False, False, True]
        assert [item[5] for item in missing_fields(plays)] == ["yes", "no", "no"]
        assert unmatchable_count(plays) == 2
        assert summary(plays).endswith(
            " Warning: 2 songs have neither an ISRC nor both an album and a label,"
            " so SoundExchange may not be able to match them."
        )
        assert summary(plays[:1]).endswith("may not be able to match it.")

    def test_summary_counts_plays_and_incomplete_songs(self) -> None:
        assert summary([]) == "0 plays exported."
        assert summary([_play()]) == "1 play exported."
        assert summary([_play(isrc="")]) == (
            "1 play exported. 1 song lacks ISRC, label, listener count or other reporting fields."
        )
        assert summary([_play(isrc="", path="a"), _play(label="", path="b")]).endswith(
            "2 songs lack ISRC, label, listener count or other reporting fields."
        )


class _Frame:
    def __init__(self, *text: str) -> None:
        self.text = list(text)


class _Tags:
    def __init__(self, values: dict) -> None:
        self.values = values

    def get(self, key: str):
        if key == "boom":
            raise ValueError(key)
        return self.values.get(key)


class TestReadReportingTags:
    @pytest.mark.parametrize(
        ("values", "expected"),
        [
            (
                {"isrc": ["usrc17607839"], "organization": ["Vorbis Label"]},
                ("USRC17607839", "Vorbis Label"),
            ),
            (
                {"TSRC": _Frame("GBAYE0000001"), "TPUB": _Frame("ID3 Label")},
                ("GBAYE0000001", "ID3 Label"),
            ),
            (
                {
                    "----:com.apple.iTunes:ISRC": [b"USAAA0000003"],
                    "----:com.apple.iTunes:LABEL": [b"MP4 Label"],
                },
                ("USAAA0000003", "MP4 Label"),
            ),
            ({"label": ["  "], "publisher": ["Fallback"], "TSRC": _Frame()}, ("", "Fallback")),
            ({"isrc": []}, ("", "")),
            # Hyphens and spaces go; a malformed ISRC counts as missing, so
            # the next tag that holds a valid one is used.
            ({"isrc": ["us-rc1-76 07839"]}, ("USRC17607839", "")),
            ({"isrc": ["not an isrc"], "TSRC": _Frame("GB-AYE-00-00001")}, ("GBAYE0000001", "")),
            ({"isrc": ["USRC1760783"]}, ("", "")),
            ({"isrc": ["USRC176078390"]}, ("", "")),
            ({"isrc": ["1SRC17607839"]}, ("", "")),
            ({"isrc": ["USRC1A607839"]}, ("", "")),
        ],
    )
    def test_reads_each_tag_container(self, monkeypatch, values, expected) -> None:
        monkeypatch.setattr(audio_meta, "_open_mutagen", lambda _path: _Tags(values))
        assert audio_meta.read_reporting_tags("song.flac") == expected

    def test_unreadable_files_and_odd_tag_objects(self, monkeypatch) -> None:
        monkeypatch.setattr(audio_meta, "_open_mutagen", lambda _path: None)
        assert audio_meta.read_reporting_tags("missing.mp3") == ("", "")
        monkeypatch.setattr(audio_meta, "_open_mutagen", lambda _path: object())
        assert audio_meta.read_reporting_tags("odd.mp3") == ("", "")
        assert audio_meta._tag_text(_Tags({}), "boom") == ""

    @pytest.mark.parametrize(
        ("text", "expected"),
        [
            ("USRC17607839", "USRC17607839"),
            ("us-rc1-76-07839", "USRC17607839"),
            (" GB AYE 00 00001 ", "GBAYE0000001"),
            ("QZ1A22300001", "QZ1A22300001"),
            ("", ""),
            ("US_RC17607839", ""),
            ("USRC17607839X", ""),
            ("USR!17607839", ""),
        ],
    )
    def test_normalize_isrc(self, text: str, expected: str) -> None:
        assert audio_meta.normalize_isrc(text) == expected


class TestExportPlaysCli:
    @pytest.fixture
    def config(self, tmp_path: Path) -> Path:
        music = tmp_path / "Music"
        music.mkdir()
        song = music / "song.flac"
        song.write_bytes(b"audio")
        index_dir = tmp_path / "index"
        with LibraryCatalog(index_dir / "default" / "library.db") as catalog:
            catalog.add_source(music)
            catalog.put_entry(_entry(song, "Song"))
            catalog.write_history(
                [
                    _row("a", song, "=cmd", datetime(2026, 9, 1, 12).astimezone()),
                    _row("b", song, "Later", datetime(2026, 9, 3, 12).astimezone()),
                ]
            )
        path = tmp_path / "config.toml"
        path.write_text(
            f'[library]\nmusic_dir = "{music.as_posix()}"\n'
            f'[index]\nindex_dir = "{index_dir.as_posix()}"\n'
            f'model_dir = "{(tmp_path / "models").as_posix()}"\n',
            encoding="utf-8",
        )
        return path

    def _run(self, config: Path, *args: str):
        from click.testing import CliRunner

        from autodj.cli import cli

        return CliRunner().invoke(cli, ["--config", str(config), "export-plays", *args])

    def test_writes_csv_to_stdout_and_summary_to_stderr(self, config: Path) -> None:
        result = self._run(config, "--from", "2026-09-01", "--to", "2026-09-02")
        assert result.exit_code == 0, result.output
        rows = list(csv.reader(io.StringIO(result.stdout, newline="")))
        assert tuple(rows[0]) == COLUMNS
        assert [row[4] for row in rows[1:]] == ["'=cmd"]
        assert rows[1][9] == "song.flac"
        assert result.stderr.strip() == (
            "1 play exported. 1 song lacks ISRC, label, listener count or other reporting fields."
            " Warning: 1 song has neither an ISRC nor both an album and a label,"
            " so SoundExchange may not be able to match it."
        )

    def test_writes_file_and_missing_report(self, config: Path, tmp_path: Path) -> None:
        out = tmp_path / "plays.csv"
        result = self._run(config, "--from", "2026-09-01", "--to", "2026-09-30", "--out", str(out))
        assert result.exit_code == 0, result.output
        assert out.read_bytes().count(b"\r\n") == 3
        assert result.stdout == ""
        result = self._run(config, "--from", "2026-09-01", "--to", "2026-09-30", "--missing")
        rows = list(csv.reader(io.StringIO(result.stdout, newline="")))
        assert tuple(rows[0]) == MISSING_COLUMNS
        assert rows[1:] == [
            ["Artist", "'=cmd", "song.flac", "isrc; label; performances", "1", "no"],
            ["Artist", "Later", "song.flac", "isrc; label; performances", "1", "no"],
        ]

    def test_exports_one_station_at_a_time(self, config: Path, tmp_path: Path) -> None:
        song = tmp_path / "Music" / "song.flac"
        with LibraryCatalog(tmp_path / "index" / "default" / "library.db") as catalog:
            played = datetime(2026, 9, 1, 13).astimezone()
            catalog.write_history([_row("owl", song, "Owl play", played)], station="nightowl")
        dates = ("--from", "2026-09-01", "--to", "2026-09-01")

        def titles(*args: str) -> list[str]:
            result = self._run(config, *dates, *args)
            assert result.exit_code == 0, result.output
            return [row[4] for row in csv.reader(io.StringIO(result.stdout, newline=""))][1:]

        assert titles() == ["'=cmd"]
        assert titles("--station", "default") == ["'=cmd"]
        assert titles("--station", "nightowl") == ["Owl play"]
        assert titles("--station", "nobody") == []
        refused = self._run(config, *dates, "--station", "../x")
        assert refused.exit_code == 2
        assert "--station" in refused.stderr

    @pytest.mark.parametrize(
        ("args", "message"),
        [
            (["--from", "soon", "--to", "2026-09-01"], "YYYY-MM-DD"),
            (["--from", "2026-09-02", "--to", "2026-09-01"], "after its start"),
        ],
    )
    def test_rejects_bad_ranges(self, config: Path, args: list[str], message: str) -> None:
        result = self._run(config, *args)
        assert result.exit_code == 2
        assert message in result.stderr

    def test_reports_missing_log_and_unwritable_output(self, config: Path, tmp_path: Path) -> None:
        result = self._run(
            config, "--from", "2026-09-01", "--to", "2026-09-01", "--out", str(tmp_path)
        )
        assert result.exit_code != 0
        result = self._run(
            config,
            "--from",
            "2026-09-01",
            "--to",
            "2026-09-01",
            "--out",
            str(tmp_path / "no" / "x.csv"),
        )
        assert result.exit_code == 1
        assert "Could not write" in result.stderr
        (tmp_path / "index" / "default" / "library.db").unlink()
        result = self._run(config, "--from", "2026-09-01", "--to", "2026-09-01")
        assert result.exit_code == 1
        assert "No play log found" in result.stderr

    def test_reports_unreadable_log(self, config: Path, tmp_path: Path) -> None:
        (tmp_path / "index" / "default" / "library.db").write_bytes(b"not a database" * 100)
        result = self._run(config, "--from", "2026-09-01", "--to", "2026-09-01")
        assert result.exit_code == 1
        assert "Could not read the play log" in result.stderr

    def test_writes_a_soundexchange_report(self, config: Path) -> None:
        args = ("--from", "2026-09-01", "--to", "2026-09-30", "--format", "soundexchange")
        result = self._run(config, *args, "--service", "My Station", "--category", "B")
        assert result.exit_code == 0, result.output
        assert result.stdout.splitlines() == [
            "^My Station^|^B^|^Artist^|^=cmd^||^Album^|||||1",
            "^My Station^|^B^|^Artist^|^Later^||^Album^|||||1",
        ]
        result = self._run(config, *args)
        assert result.exit_code == 2
        assert "name of the service" in result.stderr


def _old_play_log(path: Path) -> None:
    """Write a play log from before AutoDJ counted listeners."""
    with closing(sqlite3.connect(path)) as db, db:
        db.execute(
            "CREATE TABLE play_history (event_id TEXT PRIMARY KEY NOT NULL, entry_id TEXT,"
            " path TEXT, title TEXT NOT NULL, artist TEXT NOT NULL, duration REAL NOT NULL,"
            " played_at TEXT NOT NULL, interrupted INTEGER NOT NULL DEFAULT 0)"
        )
        db.execute(
            "INSERT INTO play_history (event_id,title,artist,duration,played_at)"
            " VALUES ('old','Old','A',100,?)",
            (T0.isoformat(),),
        )


class TestPerformanceCounts:
    def test_old_play_log_gains_the_column(self, tmp_path: Path) -> None:
        path = tmp_path / "old.db"
        _old_play_log(path)
        with LibraryCatalog(path) as catalog:
            assert catalog.play_log()[0]["performances"] is None
            # The repeat rules read the same table: a migrated play with no
            # path is left out, and new plays with counts reach both readers.
            assert catalog.recent_plays(10) == []
            catalog.write_history(
                [_row("new", tmp_path / "a.flac", "New", T0) | {"performances": 3}]
            )
            assert [p["title"] for p in catalog.recent_plays(10)] == ["New"]
            assert catalog.play_log()[-1]["performances"] == 3
        with LibraryCatalog(path) as catalog:  # reopening finds the column already there
            assert len(catalog.play_log()) == 2

    def test_two_processes_upgrading_at_once_both_open(self, tmp_path, monkeypatch) -> None:
        """Another process cannot add the column between our check and our change."""
        path = tmp_path / "old.db"
        _old_play_log(path)
        connect = sqlite3.connect
        outcome: list[str] = []

        class Racing(sqlite3.Connection):
            def execute(self, sql, *args):  # type: ignore[override]
                cursor = super().execute(sql, *args)
                if not sql.startswith("PRAGMA table_info(play_history)") or outcome:
                    return cursor
                rows = cursor.fetchall()
                # Right after our check, a second process tries the same upgrade.
                with closing(connect(path, timeout=0, isolation_level=None)) as other:
                    try:
                        other.execute("ALTER TABLE play_history ADD COLUMN performances INTEGER")
                        outcome.append("other process upgraded first")
                    except sqlite3.OperationalError as exc:
                        outcome.append(str(exc))
                return iter(rows)

        monkeypatch.setattr(
            catalog_module, "_connect", lambda *a, **k: connect(*a, factory=Racing, **k)
        )
        with LibraryCatalog(path) as catalog:  # no "duplicate column name"
            assert catalog.play_log()[0]["performances"] is None
        # The other process had to wait for our write lock; once it gets it,
        # it finds the column and opens too.
        assert outcome == ["database is locked"]
        with LibraryCatalog(path) as catalog:
            assert len(catalog.play_log()) == 1

    def test_repeated_event_only_raises_the_count(self, library) -> None:
        catalog, paths = library
        row = _row("a", paths["song"], "Song", T0) | {"performances": 2}
        catalog.write_history([row])
        catalog.write_history([row | {"performances": 5, "title": "Changed"}])
        catalog.write_history([row | {"performances": 3}])
        catalog.write_history([row | {"performances": None}])
        (logged,) = catalog.play_log()
        assert (logged["title"], logged["performances"]) == ("Song", 5)

    @pytest.mark.parametrize("bad", [-1, True, 1.5, "3"])
    def test_invalid_counts_are_unknown(self, library, bad) -> None:
        catalog, paths = library
        catalog.write_history([_row("a", paths["song"], "Song", T0) | {"performances": bad}])
        assert catalog.play_log()[0]["performances"] is None

    def test_counts_reach_the_export_and_zero_is_known(self, library) -> None:
        catalog, paths = library
        catalog.write_history(
            [
                _row("a", paths["song"], "Song", T0) | {"performances": 0},
                _row("b", paths["song"], "Song", T0 + timedelta(minutes=5)) | {"performances": 4},
                _row("c", paths["song"], "Song", T0 + timedelta(minutes=9)),
            ]
        )
        read, _ = _tags({"song.flac": ("USRC17607839", "Label")})
        plays = plays_between(catalog, T0, T0 + timedelta(hours=1), read_tags=read)
        assert [p.performances for p in plays] == [0, 4, None]
        assert [p.missing() for p in plays] == [[], [], ["performances"]]
        assert list(csv.reader(io.StringIO(plays_csv(plays), newline="")))[3][10] == ""


class TestSoundExchange:
    def test_reports_songs_once_per_recording(self) -> None:
        plays = [
            _play(artist="Björk", title="Jóga", performances=3),
            # The same recording once written as ASCII: one line, not two.
            _play(artist="Bjork", title="Joga", performances=4),
            _play(
                artist="AC|DC", title="T.N.T. ^live^", isrc="", album="", label="", performances=0
            ),
            _play(artist="Station", title="Top of the hour", kind="liner", performances=9),
            _play(artist="Old", title="Unknown count", performances=None),
            _play(artist="Old", title="Unknown count", performances=2),
        ]
        text = soundexchange_report(plays, "Ünïcode Radio", "a", channel="Main")
        assert text.endswith("\r\n")
        assert text.isascii()
        assert text.splitlines() == [
            "^Unicode Radio^|^A^|^AC DC^|^T.N.T. live^||||0||^Main^|1",
            "^Unicode Radio^|^A^|^Bjork^|^Joga^|^USRC17607839^|^Album^|^Label^|7||^Main^|2",
            "^Unicode Radio^|^A^|^Old^|^Unknown count^|^USRC17607839^|^Album^|^Label^|||^Main^|2",
        ]
        assert len(SOUNDEXCHANGE_COLUMNS) == len(text.splitlines()[0].split("|"))

    def test_other_scripts_become_question_marks(self) -> None:
        line = soundexchange_report([_play(artist="坂本龍一")], "S")
        assert "^????^" in line

    def test_needs_a_service_and_a_known_category(self) -> None:
        with pytest.raises(ValueError, match="name of the service"):
            soundexchange_report([], " | ")
        for bad in ("D", "AB", "", "z"):
            with pytest.raises(ValueError, match="transmission category"):
                soundexchange_report([], "S", bad)
        assert soundexchange_report([], "S", " b ") == ""

    def test_file_name_follows_the_regulation(self) -> None:
        assert soundexchange_filename("Acme Music Co.", T0, T0 + timedelta(days=29)) == (
            "AcmeMusicCo20260901-20260930.txt"
        )
        assert soundexchange_filename("坂本", T0, T0).startswith("Service20260901")
