"""Intro rules reuse clockwheel conditions and the matched-intro lifecycle."""

from __future__ import annotations

import json
import random
from datetime import date
from pathlib import Path
from types import SimpleNamespace

import pytest

from autodj.catalog import normalize_path
from autodj.intro_rules import IntroRule, IntroRules
from autodj.matched_intros import Intro, MatchedIntros, file_name
from tests.unit._break_station import write_voice

NOW = 1_800_000_000.0


def library(tmp_path: Path):
    clip = write_voice(tmp_path / "liner.wav", 1)
    liner = SimpleNamespace(path=str(clip))
    record = {"id": "liner-1", "kind": "liner", "path": str(clip)}
    return SimpleNamespace(
        record_for_id=lambda key: record if key == "liner-1" else None,
        entry_for_path=lambda path: liner if path == str(clip) else None,
        entries_snapshot=lambda kind: (liner,),
        catalog=SimpleNamespace(
            sources=lambda *args: [
                {"id": "music", "path": str(tmp_path), "recursive": True, "kind": "music"}
            ]
        ),
    )


def rule(**changes):
    return IntroRule(
        conditions=[{"field": "genre", "operator": "is", "value": "Rock"}],
        clip_ids=["liner-1"],
        **changes,
    )


def test_frozen_rule_replays_draws_only_when_the_chosen_clip_is_heard(tmp_path: Path) -> None:
    def day_at(_at):
        return date(2026, 10, 13)

    live_rng = random.Random(3)
    matched = MatchedIntros(
        tmp_path / "matched_intros.json",
        tmp_path / "intros",
        day_at=day_at,
        rng=live_rng,
    )
    rules = IntroRules(tmp_path / "intro_rules.json", day_at, live_rng)
    key = "a" * 32
    rules.save(key, rule(chance=100, cooldown_minutes=0), create=True)
    matched.rules = rules
    initial = live_rng.getstate()
    snapshot = matched.snapshot(record_rng=True)
    mark = snapshot._rng.mark()
    chosen = snapshot.rules.choose(
        SimpleNamespace(path=str(tmp_path / "song.flac"), genre="Rock", year=1995),
        NOW,
        library(tmp_path),
    )
    assert chosen is not None and chosen.intro_id == f"rule:{key}"
    draws = snapshot._rng.since(mark)
    assert draws
    assert live_rng.getstate() == initial

    expected = random.Random()
    expected.setstate(initial)
    for draw in draws:
        expected.random() if draw is None else expected.getrandbits(draw)
    matched.heard(chosen.intro_id, draws)
    assert live_rng.getstate() == initial
    matched.upkeep()
    committed = live_rng.getstate()
    assert committed == expected.getstate()
    matched.upkeep()
    assert live_rng.getstate() == committed


def test_rule_matches_genre_year_and_beets_with_cooldown(tmp_path: Path) -> None:
    store = IntroRules(
        tmp_path / "intro_rules.json", lambda at: date(2026, 10, 13), random.Random(1)
    )
    key = "a" * 32
    store.save(
        key,
        IntroRule(
            conditions=[
                {"field": "genre", "operator": "is", "value": "Rock"},
                {"field": "year", "operator": "between", "value": 1990, "upper": 1999},
                {
                    "field": "beets",
                    "name": "original_year",
                    "operator": "between",
                    "value": 1980,
                    "upper": 1989,
                },
            ],
            clip_ids=["liner-1"],
            cooldown_minutes=60,
        ),
        create=True,
    )
    lib = library(tmp_path)
    lib.clock_beets = SimpleNamespace(
        snapshot=lambda fields: (
            fields,
            {normalize_path(str(tmp_path / "song.flac")): {"original_year": 1984}},
            None,
        )
    )
    entry = SimpleNamespace(path=str(tmp_path / "song.flac"), genre="Rock", year=1995)
    # original_year uses first_release_year, which accepts original_year in a Beets row.
    chosen = store.choose(entry, NOW, lib)
    assert chosen is not None and chosen.intro_id == f"rule:{key}"
    assert store.choose(entry, NOW + 10, lib) is None
    store.heard(key)
    store.upkeep()
    assert store.all()[key].last_aired is not None
    assert (
        store.choose(SimpleNamespace(path=entry.path, genre="Pop", year=1995), NOW + 4000, lib)
        is None
    )


def test_rule_dates_chance_and_priority_after_song_and_artist(tmp_path: Path) -> None:
    lib = library(tmp_path)
    entry = SimpleNamespace(path=str(tmp_path / "song.flac"), genre="Rock", year=1995)
    store = MatchedIntros(
        tmp_path / "matched_intros.json",
        tmp_path / "intros",
        day_at=lambda at: date(2026, 10, 13),
        rng=random.Random(1),
    )
    store.rules = IntroRules(tmp_path / "intro_rules.json", store._day_at, store._rng)
    store.rules.save("a" * 32, rule(not_before=date(2026, 10, 14)), create=True)
    assert store.rules.choose(entry, NOW, lib) is None
    store.rules.save("a" * 32, rule(), create=False)
    assert store.rules.choose(entry, NOW, lib) is not None
    store.release((f"rule:{'a' * 32}",))
    store.heard(f"rule:{'a' * 32}")
    store.upkeep()
    assert store.rules.all()["a" * 32].last_aired is not None
    clip_id = "b" * 32
    store.folder.mkdir()
    write_voice(store.folder / file_name(clip_id, ".wav"), 1)
    store.add(clip_id, Intro(file=file_name(clip_id, ".wav"), track_id="song"))
    assert store.choose("song", "", NOW).intro_id == clip_id


def test_folder_and_category_conditions_use_clockwheel_membership(tmp_path: Path) -> None:
    lib = library(tmp_path)
    entry = SimpleNamespace(path=str(tmp_path / "song.flac"), genre="Rock")
    lib.categories = SimpleNamespace(members=lambda: {"rock": ("Rock", frozenset({entry.path}))})
    store = IntroRules(
        tmp_path / "intro_rules.json", lambda at: date(2026, 10, 13), random.Random(1)
    )
    store.save(
        "a" * 32,
        IntroRule(
            conditions=[
                {"field": "folder", "source": "music", "folder": "", "recursive": True},
                {"field": "category", "category": "rock"},
            ],
            clip_ids=["liner-1"],
            cooldown_minutes=0,
        ),
        create=True,
    )
    assert store.choose(entry, NOW, lib) is not None
    lib.categories = SimpleNamespace(members=lambda: {"rock": ("Rock", frozenset())})
    assert store.choose(entry, NOW + 3600, lib) is None


def test_category_rules_use_station_paths(tmp_path: Path) -> None:
    entry = SimpleNamespace(path=str(tmp_path / "song.flac"), genre="Rock")
    lib = library(tmp_path)
    requested = []
    lib.categories_for = lambda paths: (
        requested.append(paths)
        or SimpleNamespace(members=lambda: {"rock": ("Rock", frozenset({entry.path}))})
    )
    lib.categories = SimpleNamespace(members=lambda: {})
    paths = ("station",)
    store = IntroRules(
        tmp_path / "intro_rules.json", date.fromtimestamp, random.Random(1), paths=paths
    )
    store.save(
        "a" * 32,
        IntroRule(
            conditions=[{"field": "category", "category": "rock"}],
            clip_ids=["liner-1"],
            cooldown_minutes=0,
        ),
        create=True,
    )
    assert store.choose(entry, NOW, lib) is not None
    assert requested == [paths]


def test_snapshot_copies_rule_choice_state_without_touching_the_source(tmp_path: Path) -> None:
    lib = library(tmp_path)
    entry = SimpleNamespace(path=str(tmp_path / "song.flac"), genre="Rock", year=1995)
    store = IntroRules(tmp_path / "intro_rules.json", date.fromtimestamp, random.Random(1))
    key = "a" * 32
    store.save(key, rule(cooldown_minutes=0), create=True)
    before = store.path.read_bytes()
    source_rng = store._rng.getstate()
    store._reserved[key] = NOW
    store._heard.append((key, NOW - 5))
    clone = store.snapshot()
    clone._stat = lambda: (_ for _ in ()).throw(AssertionError("snapshot read the file"))

    assert clone._reserved == {key: NOW}
    assert list(clone._heard) == [(key, NOW - 5)]
    assert clone.choose(entry, NOW + 100, lib) is not None
    assert store._reserved == {key: NOW}
    assert list(store._heard) == [(key, NOW - 5)]
    assert store._rng.getstate() == source_rng
    clone.upkeep()
    assert store.path.read_bytes() == before


def test_snapshot_reads_unopened_rules_without_setting_aside_invalid_data(tmp_path: Path) -> None:
    written = IntroRules(tmp_path / "intro_rules.json", date.fromtimestamp, random.Random(1))
    key = "a" * 32
    written.save(key, rule(), create=True)
    before = written.path.read_bytes()
    snapshot = IntroRules(written.path, date.fromtimestamp, random.Random(2)).snapshot()
    assert key in snapshot.all()
    assert written.path.read_bytes() == before

    written.path.write_text("not json", encoding="utf-8")
    before = written.path.read_bytes()
    snapshot = IntroRules(written.path, date.fromtimestamp, random.Random(3)).snapshot()
    assert snapshot.all() == {}
    assert written.path.read_bytes() == before
    assert not list(tmp_path.glob("intro_rules.json.bad-*"))


def test_bad_entries_retained_and_unreadable_file_blocks_saves(tmp_path: Path, monkeypatch) -> None:
    store = IntroRules(
        tmp_path / "intro_rules.json", lambda at: date(2026, 10, 13), random.Random(1)
    )
    key = "a" * 32
    store.save(key, rule(), create=True)
    data = json.loads(store.path.read_text())
    data["rules"]["bad"] = {"chance": 500}
    store.path.write_text(json.dumps(data))
    assert list(store.all()) == [key]
    assert store.problem() is not None
    store.save("b" * 32, rule(), create=True)
    assert json.loads(store.path.read_text())["rules"]["bad"] == {"chance": 500}
    original = store.path.read_bytes()
    store._stamp = None
    monkeypatch.setattr(
        Path, "read_bytes", lambda self: (_ for _ in ()).throw(PermissionError("busy"))
    )
    assert key in store.all()
    with pytest.raises(PermissionError):
        store.remove(key)
    monkeypatch.undo()
    assert store.path.read_bytes() == original


def test_invalid_document_is_set_aside(tmp_path: Path) -> None:
    store = IntroRules(tmp_path / "intro_rules.json", lambda at: date.today(), random.Random(1))
    store.path.write_text("not json")
    assert store.all() == {}
    assert not store.path.exists()
    assert len(list(tmp_path.glob("intro_rules.json.bad-*"))) == 1


def test_first_rule_creates_a_new_station_state_folder(tmp_path: Path) -> None:
    store = IntroRules(
        tmp_path / "nightowl" / "intro_rules.json", date.fromtimestamp, random.Random(1)
    )
    store.save("a" * 32, rule(), create=True)
    assert store.path.is_file()


def test_match_any_and_chance_roll(tmp_path: Path) -> None:
    store = IntroRules(tmp_path / "intro_rules.json", date.fromtimestamp, random.Random(1))
    store.save(
        "a" * 32,
        IntroRule(
            conditions=[
                {"field": "genre", "operator": "is", "value": "Rock"},
                {"field": "year", "operator": "eq", "value": 1995},
            ],
            match="any",
            clip_ids=["liner-1"],
            chance=1,
        ),
        create=True,
    )
    entry = SimpleNamespace(path=str(tmp_path / "song.flac"), genre="Rock", year=2000)
    assert store.choose(entry, NOW, library(tmp_path)) is None


def folder_rule(tmp_path: Path, **changes) -> IntroRule:
    return IntroRule(
        conditions=[{"field": "genre", "operator": "is", "value": "Rock"}],
        liner_folder=str(tmp_path),
        cooldown_minutes=0,
        **changes,
    )


def test_liner_folder_pool_is_warmed_on_the_tick_not_the_render(
    tmp_path: Path, monkeypatch
) -> None:
    lib = library(tmp_path)
    liner = lib.entries_snapshot("liner")[0]
    snapshot = (liner,)
    lib.entries_snapshot = lambda kind: snapshot
    store = IntroRules(
        tmp_path / "intro_rules.json", date.fromtimestamp, random.Random(1), library=lambda: lib
    )
    store.save("a" * 32, folder_rule(tmp_path), create=True)
    # The save leaves the pool to the station's tick, which builds it.
    assert store.needs_upkeep()
    store.upkeep()
    assert not store.needs_upkeep()
    built = []
    real = IntroRules._pool
    monkeypatch.setattr(
        IntroRules, "_pool", staticmethod(lambda *args: built.append(args) or real(*args))
    )
    entry = SimpleNamespace(path=str(tmp_path / "song.flac"), genre="Rock")
    chosen = store.choose(entry, NOW, lib)
    assert chosen is not None and chosen.path == Path(liner.path)
    assert built == []  # the render found the pool ready
    store.release("a" * 32)

    # A rescan publishes new liners: until the tick, the old pool serves,
    # but only liners the library still has.
    snapshot = ()
    lib.entry_for_path = lambda path: None
    assert store.choose(entry, NOW + 3600, lib) is None
    assert built == []
    monkeypatch.setattr(store, "_warmed_at", float("-inf"))
    store.upkeep()
    assert built and store._folder_pools == {str(tmp_path): ()}


def test_a_render_before_the_first_tick_builds_the_pool_itself(tmp_path: Path) -> None:
    lib = library(tmp_path)
    store = IntroRules(tmp_path / "intro_rules.json", date.fromtimestamp, random.Random(1))
    store.save("a" * 32, folder_rule(tmp_path), create=True)
    entry = SimpleNamespace(path=str(tmp_path / "song.flac"), genre="Rock")
    assert store.choose(entry, NOW, lib) is not None


def test_rule_clips_pass_the_station_liner_filter(tmp_path: Path) -> None:
    """Another station's liners, and liners outside their play dates, never air."""
    lib = library(tmp_path)
    store = IntroRules(tmp_path / "intro_rules.json", date.fromtimestamp, random.Random(1))
    store.save("a" * 32, rule(cooldown_minutes=0), create=True)
    store.save("b" * 32, folder_rule(tmp_path), create=True)
    entry = SimpleNamespace(path=str(tmp_path / "song.flac"), genre="Rock")
    asked = []

    def refuse(entries, at):
        asked.append(at)
        return ()

    assert store.choose(entry, NOW, lib, allowed=refuse) is None
    assert asked and set(asked) == {NOW}
    assert store.choose(entry, NOW, lib, allowed=lambda entries, at: entries) is not None


def test_an_edit_between_reads_never_breaks_the_render(tmp_path: Path) -> None:
    lib = library(tmp_path)
    store = IntroRules(tmp_path / "intro_rules.json", date.fromtimestamp, random.Random(1))
    store.save("a" * 32, rule(cooldown_minutes=0), create=True)
    store.all()
    # Conditions compiled for an older version of the rule are passed over.
    store._compiled["a" * 32] = []
    store._stamp = store._stat()
    entry = SimpleNamespace(path=str(tmp_path / "song.flac"), genre="Rock")
    assert store.choose(entry, NOW, lib) is None


def test_cooldown_survives_a_restart(tmp_path: Path) -> None:
    lib = library(tmp_path)
    path = tmp_path / "intro_rules.json"
    store = IntroRules(path, date.fromtimestamp, random.Random(1))
    store.save("a" * 32, rule(cooldown_minutes=60), create=True)
    entry = SimpleNamespace(path=str(tmp_path / "song.flac"), genre="Rock")
    assert store.choose(entry, NOW, lib) is not None
    store.heard("a" * 32)
    store.upkeep()
    later = IntroRules(path, date.fromtimestamp, random.Random(1))
    aired = later.all()["a" * 32].last_aired
    assert aired is not None
    assert later.choose(entry, aired + 60, lib) is None
    assert later.choose(entry, aired + 3601, lib) is not None


def test_an_unheard_rule_is_released_for_the_next_change(tmp_path: Path) -> None:
    """#209: a render that ended before its intro aired gives the rule back."""
    lib = library(tmp_path)
    store = MatchedIntros(
        tmp_path / "matched_intros.json",
        tmp_path / "intros",
        day_at=date.fromtimestamp,
        rng=random.Random(1),
    )
    store.rules = IntroRules(tmp_path / "intro_rules.json", store._day_at, store._rng)
    store.rules.save("a" * 32, rule(cooldown_minutes=60), create=True)
    entry = SimpleNamespace(path=str(tmp_path / "song.flac"), genre="Rock")
    chosen = store.rules.choose(entry, NOW, lib)
    assert chosen is not None
    assert store.rules.choose(entry, NOW + 30, lib) is None  # held for its change
    store.release_later((chosen.intro_id,))
    assert store.needs_upkeep()
    store.upkeep()
    assert store.rules.choose(entry, NOW + 30, lib) is not None
    assert store.rules.all()["a" * 32].last_aired is None


def test_a_large_folder_of_missing_files_cannot_hold_up_the_render(
    tmp_path: Path, monkeypatch
) -> None:
    from autodj import intro_rules

    lib = library(tmp_path)
    gone = tuple(SimpleNamespace(path=str(tmp_path / f"gone-{i}.wav")) for i in range(1000))
    lib.entries_snapshot = lambda kind: gone
    store = IntroRules(tmp_path / "intro_rules.json", date.fromtimestamp, random.Random(1))
    store.save("a" * 32, folder_rule(tmp_path, clip_ids=[]), create=True)
    looked = []
    real = Path.is_file
    monkeypatch.setattr(Path, "is_file", lambda self: looked.append(self) or real(self))
    entry = SimpleNamespace(path=str(tmp_path / "song.flac"), genre="Rock")
    assert store.choose(entry, NOW, lib) is None
    assert len(set(looked)) == len(looked) == intro_rules.MAX_TRIES


@pytest.mark.parametrize(
    "field,value,expanded",
    [
        ("year", 1988, "1988"),
        ("bpm", 119.5, "120"),
        ("bpm", 120.1, "120"),
        ("artist", "Rick Astley", "Rick Astley"),
        ("title", "Never Gonna Give You Up", "Never Gonna Give You Up"),
        ("album", "Whenever You Need Somebody", "Whenever You Need Somebody"),
        ("genre", "Pop", "Pop"),
    ],
)
def test_filename_pattern_expands_song_fields(field, value, expanded):
    from autodj.intro_rules import resolve_filename_pattern

    text, matcher, missing = resolve_filename_pattern(
        "intro-{" + field + "}-?.wav", SimpleNamespace(**{field: value})
    )
    assert text == f"intro-{expanded}-?.wav"
    assert not missing
    assert matcher.fullmatch(f"INTRO-{expanded.upper()}-1.WAV")
    assert not matcher.fullmatch(f"intro-{expanded}-12.wav")


def test_metadata_wildcards_and_brackets_are_literal():
    from autodj.intro_rules import resolve_filename_pattern

    text, matcher, missing = resolve_filename_pattern(
        "[intro]-{artist}-*.wav", SimpleNamespace(artist="A*[B]?")
    )
    assert text == "[intro]-A*[B]?-*.wav" and not missing
    assert matcher.fullmatch("[intro]-A*[B]?-one.wav")
    assert not matcher.fullmatch("[intro]-AnythingBz-one.wav")


@pytest.mark.parametrize(
    "values,missing",
    [
        ({}, ("year", "bpm")),
        ({"year": 0, "bpm": float("nan")}, ("year", "bpm")),
        ({"year": 1988, "bpm": -1}, ("bpm",)),
    ],
)
def test_missing_numeric_fields_never_become_wildcards(values, missing):
    from autodj.intro_rules import resolve_filename_pattern

    assert resolve_filename_pattern("{year}-{bpm}-{year}-*.wav", SimpleNamespace(**values)) == (
        None,
        None,
        missing,
    )


@pytest.mark.parametrize(
    "pattern",
    [
        "",
        "{unknown}-*",
        "{year",
        "{artist.name}",
        "{year:04d}",
        "{artist!r}",
        "../*.wav",
        "folder\\*.wav",
        "a\n*.wav",
        "x" * 241,
    ],
)
def test_invalid_filename_patterns_are_refused(pattern):
    with pytest.raises(ValueError):
        IntroRule(
            conditions=[{"field": "genre", "operator": "is", "value": "Rock"}],
            liner_folder="/liners",
            filename_pattern=pattern,
        )


def test_filename_patterns_require_a_folder_and_cannot_mix_specific_clips():
    with pytest.raises(ValueError, match="registered liner folder"):
        rule(filename_pattern="{year}-*.wav")
    with pytest.raises(ValueError, match="specific liners"):
        rule(liner_folder="/liners", filename_pattern="{year}-*.wav")


def test_pattern_rule_selects_matching_pool_and_keeps_whole_rule_cooldown(tmp_path):
    root = tmp_path / "liners"
    root.mkdir()
    clips = tuple(
        SimpleNamespace(path=str(root / name))
        for name in ("year-1982-a.wav", "YEAR-1988-b.WAV", "generic.wav")
    )
    for clip in clips:
        Path(clip.path).touch()
    lib = SimpleNamespace(
        entries_snapshot=lambda kind: clips,
        entry_for_path=lambda path: next((clip for clip in clips if clip.path == path), None),
    )
    store = IntroRules(
        tmp_path / "rules.json", lambda at: date(2026, 10, 16), random.Random(1), lambda: lib
    )
    key = "a" * 32
    store.save(
        key,
        IntroRule(
            conditions=[{"field": "genre", "operator": "is", "value": "Rock"}],
            liner_folder=str(root),
            filename_pattern="year-{year}-*.wav",
            cooldown_minutes=60,
        ),
        create=True,
    )
    song = SimpleNamespace(path="song.flac", genre="Rock", year=1982)
    chosen = store.choose(song, NOW, lib)
    assert chosen.path.name == "year-1982-a.wav"
    song.year = 1988
    assert store.choose(song, NOW + 30, lib) is None
    chosen = store.choose(song, NOW + 3601, lib)
    assert chosen.path.name == "YEAR-1988-b.WAV"
    # The same gate used by playback still excludes station-ineligible or expired liners.
    assert store.choose(song, NOW + 7202, lib, allowed=lambda entries, at: ()) is None
    song.year = 0
    assert store.choose(song, NOW + 10803, lib) is None
    assert (
        IntroRules(store.path, store._day_at, random.Random()).all()[key].filename_pattern
        == "year-{year}-*.wav"
    )


def test_pattern_filtering_happens_before_bounded_random_sampling(tmp_path):
    store = IntroRules(tmp_path / "rules.json", lambda at: date(2026, 10, 16), random.Random(1))
    clip = tmp_path / "year-1988.wav"
    clip.touch()
    pool = (
        *(SimpleNamespace(path=str(tmp_path / f"other-{n}.wav")) for n in range(1000)),
        SimpleNamespace(path=str(clip)),
    )
    lib = SimpleNamespace(entries_snapshot=lambda kind: pool)
    store.save(
        "b" * 32,
        IntroRule(
            conditions=[{"field": "year", "operator": "eq", "value": 1988}],
            liner_folder=str(tmp_path),
            filename_pattern="year-{year}.wav",
        ),
        create=True,
    )
    chosen = store.choose(SimpleNamespace(year=1988), NOW, lib)
    assert chosen.path == clip
    # A refreshed pool must not reuse an older set of filename matches.
    first = store._pattern_pool("year-{year}.wav", SimpleNamespace(year=1988), pool)
    assert store._pattern_pool("year-{year}.wav", SimpleNamespace(year=1988), pool) is first
    assert store._pattern_pool("year-{year}.wav", SimpleNamespace(year=1988), pool[:-1]) == ()
