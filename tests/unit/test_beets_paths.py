"""Beets' paths matched against the library: the check, the found folder and its messages (#386)."""

from __future__ import annotations

import logging
import sqlite3
from pathlib import Path
from types import SimpleNamespace

import pytest

import autodj.beets_paths as beets_paths
from autodj import doctor
from autodj._bridge import _library_problem
from autodj.beets_paths import check_library, latest, match, page_view, remember
from autodj.catalog import normalize_path
from autodj.categories import Category, CategoryIndex
from autodj.doctor import CheckStatus
from tests._layout import layout_index


@pytest.fixture(autouse=True)
def _fresh_checks():
    beets_paths._latest.clear()
    yield
    beets_paths._latest.clear()


def song(path: Path, genre: str = "Rock"):
    return SimpleNamespace(
        path=str(path),
        title=path.stem,
        artist="Artist",
        album="Album",
        genre=genre,
        year=0,
        bpm=120.0,
        length=200.0,
        is_silent=False,
    )


def make_beets(path: Path, paths: list[str]) -> Path:
    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE items (id INTEGER PRIMARY KEY, path BLOB, title TEXT, genre TEXT, "
        "year INTEGER, original_year INTEGER)"
    )
    conn.executemany(
        "INSERT INTO items (path, title, genre, year, original_year) VALUES (?, 't', ?, 2001, ?)",
        [(item.encode(), "Rock", 2001) for item in paths],
    )
    conn.commit()
    conn.close()
    return path


RELATIVE = ["Artist A/Album/01 One.flac", "Artist A/Album/02 Two.flac", "Artist B/Single.flac"]


def library_under(root: Path) -> list[str]:
    return [str(root / item) for item in RELATIVE]


def rows(paths: list[str]):
    return [(path, {"genre": "Rock"}) for path in paths]


def test_a_wrong_existing_music_dir_is_reported_with_the_folder_that_holds_the_paths(tmp_path):
    music = tmp_path / "music"
    wrong = tmp_path / "elsewhere"
    wrong.mkdir()

    items, check = match(rows(RELATIVE), library_under(music), wrong)

    assert check.mismatch and check.relative
    assert check.matched == 0 and check.items == 3 and check.library == 3
    # An existing music_dir is never overridden; the folder is only suggested.
    assert check.root == wrong and check.found == music
    assert not (set(items) & {beets_paths.library_key(p, music) for p in RELATIVE})
    assert check.problem is not None
    assert "No song in the beets database matches a song in AutoDJ's library" in check.problem
    assert "Artist A/Album/01 One.flac" in check.problem
    assert f"[library] music_dir, which is {wrong}." in check.problem
    assert check.fix is not None
    assert f'Set music_dir = "{music.as_posix()}" under [library]' in check.fix
    assert check.note is None
    assert "check [library] music_dir" in (check.cause or "")


@pytest.mark.parametrize("missing", [True, False])
def test_an_unset_or_missing_music_dir_uses_the_one_folder_found(tmp_path, missing):
    music = tmp_path / "music"
    configured = tmp_path / "Z" / "Music" if missing else None

    items, check = match(rows(RELATIVE), library_under(music), configured)

    assert not check.mismatch and check.problem is None
    assert check.matched == 3 and check.root == music and check.detected
    assert set(items) == {beets_paths.library_key(p, music) for p in RELATIVE}
    assert check.note is not None
    assert (
        f"Beets queries and Beets conditions read relative paths from {music.as_posix()}"
        in check.note
    )
    setting = "a folder that does not exist here" if missing else "is not set"
    assert setting in check.note


def test_two_folders_holding_the_same_paths_are_not_guessed_between(tmp_path):
    first, second = tmp_path / "a", tmp_path / "b"

    _, check = match(rows(RELATIVE), library_under(first) + library_under(second), None)

    assert check.mismatch and not check.detected
    assert check.found in {first, second}
    assert check.fix is not None and "Set music_dir" in check.fix


def test_too_few_agreeing_paths_are_not_used(tmp_path):
    music = tmp_path / "music"
    library = [str(music / RELATIVE[0])]

    _, check = match(rows(RELATIVE), library, None)

    # One of three sampled paths found: suggested, never used.
    assert check.mismatch and check.found == music and check.root is None


def test_fewer_than_three_beets_paths_are_not_enough_to_choose_a_folder(tmp_path):
    music = tmp_path / "music"
    _, check = match(rows(RELATIVE[:2]), library_under(music)[:2], None)

    assert check.mismatch and check.found == music and check.root is None


def test_relative_paths_found_nowhere_say_which_setting_to_change(tmp_path):
    _, check = match(rows(RELATIVE), [str(tmp_path / "other.flac")], None)

    assert check.mismatch and check.found is None
    assert "music_dir, which is not set" in (check.problem or "")
    assert "the folder on this machine that holds beets' music" in (check.fix or "")


def test_full_beets_paths_that_are_not_library_songs_name_the_mount(tmp_path):
    beets_side = [f"/srv/media/{item}" for item in RELATIVE]

    _, check = match(rows(beets_side), library_under(tmp_path / "music"), None)

    assert check.mismatch and not check.relative
    assert "Beets stores full paths, such as /srv/media/Artist A" in (check.problem or "")
    assert "mount the music at the folder beets uses" in (check.fix or "")


def test_matching_paths_report_only_the_count(tmp_path):
    music = tmp_path / "music"
    music.mkdir()
    library = [*library_under(music), str(music / "Not in beets.flac")]

    _, check = match(rows(RELATIVE), library, music)

    assert check.message() is None and not check.mismatch
    assert check.summary == "3 of 4 library songs have a beets entry"
    assert page_view(check) == {
        "summary": "3 of 4 library songs have a beets entry",
        "problem": None,
        "fix": None,
        "note": None,
        "mismatch": False,
    }


def test_an_empty_beets_database_or_library_is_no_mismatch(tmp_path):
    assert not match([], library_under(tmp_path), None)[1].mismatch
    assert not match(rows(RELATIVE), [], None)[1].mismatch


def test_a_problem_is_logged_once_per_change(tmp_path, caplog):
    db = tmp_path / "library.db"
    music = tmp_path / "music"
    wrong = tmp_path / "wrong"
    wrong.mkdir()
    bad = match(rows(RELATIVE), library_under(music), wrong)[1]
    music.mkdir()
    good = match(rows(RELATIVE), library_under(music), music)[1]

    with caplog.at_level(logging.INFO, logger="autodj.beets_paths"):
        for _ in range(3):
            remember(db, wrong, bad, 3)
        assert [r.levelno for r in caplog.records] == [logging.ERROR]
        assert "Beets paths: No song in the beets database" in caplog.records[0].getMessage()
        remember(db, wrong, good, 3)
        remember(db, wrong, good, 3)
        remember(db, wrong, bad, 3)
    assert [r.levelno for r in caplog.records] == [logging.ERROR, logging.INFO, logging.ERROR]


def test_check_library_reads_beets_again_only_when_something_changed(tmp_path, monkeypatch):
    music = tmp_path / "music"
    db = make_beets(tmp_path / "library.db", RELATIVE)
    cfg = SimpleNamespace(library=SimpleNamespace(beets_db=db, music_dir=tmp_path / "Z"))
    songs = [song(Path(path)) for path in library_under(music)]
    import autodj.beets as beets

    reads = []
    real = beets.read_items
    monkeypatch.setattr(beets, "read_items", lambda *a: reads.append(1) or real(*a))

    first = check_library(cfg, songs)
    assert first is not None and first.detected
    assert check_library(cfg, songs) is first
    assert len(reads) == 1
    assert check_library(cfg, songs[:2]) is not first  # the library changed
    assert len(reads) == 2
    assert latest(cfg) is not None
    assert check_library(SimpleNamespace(library=SimpleNamespace(beets_db=None)), songs) is None


def test_check_library_rechecks_when_missing_music_dir_appears(tmp_path, monkeypatch):
    music = tmp_path / "music"
    configured = tmp_path / "mounted"
    db = make_beets(tmp_path / "library.db", RELATIVE)
    cfg = SimpleNamespace(library=SimpleNamespace(beets_db=db, music_dir=configured))
    songs = [song(Path(path)) for path in library_under(music)]
    import autodj.beets as beets

    reads = []
    real = beets.read_items
    monkeypatch.setattr(beets, "read_items", lambda *a: reads.append(1) or real(*a))
    first = check_library(cfg, songs)
    assert first is not None and first.detected

    configured.mkdir()
    second = check_library(cfg, songs)
    assert second is not None and second.mismatch and not second.detected
    assert len(reads) == 2


def test_the_station_state_names_a_mismatch_and_its_fix(tmp_path):
    db = tmp_path / "library.db"
    wrong = tmp_path / "wrong"
    wrong.mkdir()
    cfg = SimpleNamespace(library=SimpleNamespace(beets_db=db, music_dir=wrong))
    assert _library_problem(cfg) is None

    remember(db, wrong, match(rows(RELATIVE), library_under(tmp_path / "music"), wrong)[1], 3)

    text = _library_problem(cfg)
    assert text is not None
    assert 'Set music_dir = "' in text
    assert "Clockwheel steps that need those songs are skipped" not in text


# ---------------------------------------------------------------------------
# Categories
# ---------------------------------------------------------------------------


def category_config(tmp_path: Path, db: Path, music_dir: Path | None):
    return SimpleNamespace(
        index=layout_index(tmp_path / "index"),
        library=SimpleNamespace(beets_db=db, music_dir=music_dir, playlists_folder=None),
    )


def test_an_empty_beets_category_logs_the_likely_cause(tmp_path, caplog):
    music = tmp_path / "music"
    wrong = tmp_path / "wrong"
    wrong.mkdir()
    db = make_beets(tmp_path / "library.db", RELATIVE)
    songs = tuple(song(Path(path)) for path in library_under(music))
    cfg = category_config(tmp_path, db, wrong)
    index = CategoryIndex(cfg, lambda: songs)
    index.save([Category(id="c1", name="Gold", source="query", query="original_year:2001")])

    with caplog.at_level(logging.WARNING):
        assert index.members() == {"c1": ("Gold", frozenset())}
        index.members()
    messages = [r.getMessage() for r in caplog.records]
    assert (
        "Category Gold has no songs in the library: no beets song path matches the library; "
        "check [library] music_dir" in messages
    )
    assert sum("Beets paths: No song in the beets database" in m for m in messages) == 1
    check = latest(cfg)
    assert check is not None and check.mismatch and check.found == music


def test_categories_use_the_found_folder_when_music_dir_is_missing(tmp_path):
    music = tmp_path / "music"
    db = make_beets(tmp_path / "library.db", RELATIVE)
    songs = tuple(song(Path(path)) for path in library_under(music))
    cfg = category_config(tmp_path, db, tmp_path / "Z" / "Music")
    index = CategoryIndex(cfg, lambda: songs)
    index.save([Category(id="c1", name="Gold", source="query", query="original_year:2001")])

    assert index.members()["c1"][1] == frozenset(s.path for s in songs)
    check = latest(cfg)
    assert check is not None and check.detected


# ---------------------------------------------------------------------------
# Doctor
# ---------------------------------------------------------------------------


def make_catalog(cfg, paths: list[str]) -> None:
    from autodj.catalog import LibraryCatalog
    from autodj.indexer import IndexEntry

    cfg.index.library_dir.mkdir(parents=True, exist_ok=True)
    with LibraryCatalog(cfg.index.library_dir / "library.db") as catalog:
        for path in paths:
            catalog.put_entry(IndexEntry(path, "t", "a", "b", "Rock", 0, 0, 0, 0, -1, -1, 0))


def test_doctor_reports_a_mismatch_with_its_fix(tmp_path, caplog):
    music = tmp_path / "music"
    wrong = tmp_path / "wrong"
    wrong.mkdir()
    cfg = category_config(tmp_path, make_beets(tmp_path / "library.db", RELATIVE), wrong)
    make_catalog(cfg, library_under(music))

    [check] = doctor._beets_paths_check(cfg)

    assert check.name == "beets-paths" and check.status is CheckStatus.WARN
    assert "No song in the beets database matches" in check.summary
    catalog_music = Path(normalize_path(music)).as_posix()
    assert check.repair is not None and f'music_dir = "{catalog_music}"' in check.repair
    assert isinstance(check.detail, dict) and check.detail["matched"] == 0

    categories = tmp_path / "index" / "stations" / "default"
    categories.mkdir(parents=True, exist_ok=True)
    from autodj.categories import CategoryStore, store_path

    CategoryStore(store_path(cfg)).save(
        [Category(id="c1", name="Gold", source="query", query="original_year:2001")]
    )
    report = doctor._categories_check(cfg)
    assert report.status is CheckStatus.WARN
    assert "Gold: no songs match (no beets song path matches the library" in report.summary
    assert report.repair is not None and report.repair.startswith("Set music_dir = ")
    assert not any("Beets paths:" in record.getMessage() for record in caplog.records)
    assert latest(cfg) is None


def test_doctor_passes_a_found_folder_when_music_dir_is_unset(tmp_path):
    music = tmp_path / "music"
    cfg = category_config(tmp_path, make_beets(tmp_path / "library.db", RELATIVE), None)
    make_catalog(cfg, library_under(music))

    [check] = doctor._beets_paths_check(cfg)

    assert check.status is CheckStatus.PASS
    assert f"read relative paths from {Path(normalize_path(music)).as_posix()}" in check.summary


def test_doctor_has_no_beets_paths_check_without_a_beets_database(tmp_path):
    cfg = category_config(tmp_path, tmp_path / "absent.db", None)
    assert doctor._beets_paths_check(cfg) == []


def test_doctor_labels_the_configured_beets_relative_path_base(tmp_path):
    music = tmp_path / "music"
    music.mkdir()
    cfg = category_config(tmp_path, make_beets(tmp_path / "library.db", RELATIVE), music)
    make_catalog(cfg, [*library_under(music), str(tmp_path / "other" / "song.flac")])

    [check] = doctor._beets_paths_check(cfg)

    assert check.status is CheckStatus.PASS
    assert check.detail["matched"] == 3
    assert check.detail["library_songs"] == 4
    assert check.detail["music_dir"] == str(music)
    report = doctor.DoctorReport((check,))
    for rendered in (doctor.render_text(report), report.to_json()):
        rendered = " ".join(rendered.split())
        assert "Base for relative Beets paths" in rendered
        assert "does not register music folders" in rendered


def test_clockwheel_beets_conditions_use_the_found_folder(tmp_path):
    from autodj.catalog import normalize_path
    from autodj.clock_condition_data import BeetsConditions

    music = tmp_path / "music"
    db = make_beets(tmp_path / "library.db", RELATIVE)
    songs = tuple(song(Path(path)) for path in library_under(music))
    cfg = SimpleNamespace(library=SimpleNamespace(beets_db=db, music_dir=None))

    _, items, error = BeetsConditions(cfg, lambda: songs).snapshot(["genre"])
    _, unmatched, _ = BeetsConditions(cfg).snapshot(["genre"])

    assert error is None
    assert set(items) == {normalize_path(s.path) for s in songs}
    assert not set(unmatched) & set(items)  # without the library nothing is guessed
