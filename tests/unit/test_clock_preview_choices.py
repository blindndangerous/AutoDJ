"""Remembered clockwheel choices still pass through selection and fitting (#604)."""

from random import Random
from types import SimpleNamespace

import pytest

from autodj.clockwheel import ClockSlot, pick_slot
from autodj.hour_fit import FIT_CANDIDATES, fit_chooser
from autodj.repeat_rules import SongVersions


def _entry(number, *, title=None, length=200):
    return SimpleNamespace(
        path=f"/music/{number}.flac",
        artist="Artist",
        title=title or str(number),
        album="",
        length=length,
    )


@pytest.mark.parametrize("selection", ["random", "least_recent"])
@pytest.mark.parametrize("group_versions", [False, True])
def test_a_remembered_choice_is_retained_only_while_eligible(selection, group_versions):
    entries = [_entry(1, title="Song"), _entry(2, title="Song"), _entry(3)]
    preferred = entries[1]
    options = {"versions": SongVersions(entries) if group_versions else None}
    slot = ClockSlot("/music", selection=selection)

    def pick(allowed):
        return pick_slot(
            [slot],
            0,
            entries,
            {},
            allowed,
            Random(1),
            preferred_path=preferred.path.replace("/music/", "/music/./"),
            **options,
        ).entry

    assert pick(lambda _: True) is preferred
    assert pick(lambda entry: entry is not preferred) is not preferred


@pytest.mark.parametrize("selection", ["random", "least_recent"])
@pytest.mark.parametrize("group_versions", [False, True])
def test_a_remembered_choice_wins_fit_ties_but_a_better_fit_replaces_it(selection, group_versions):
    entries = [_entry(1, title="Song"), _entry(2, title="Song"), _entry(3)]
    preferred = entries[1]
    options = {"versions": SongVersions(entries) if group_versions else None}
    slot = ClockSlot("/music", selection=selection)

    def pick():
        return pick_slot(
            [slot],
            0,
            entries,
            {},
            lambda _: True,
            Random(1),
            preferred_path=preferred.path,
            fit=fit_chooser(200, lambda entry: entry.length),
            **options,
        ).entry

    assert pick() is preferred
    preferred.length = 230
    assert pick() is not preferred


@pytest.mark.parametrize("group_versions", [False, True])
def test_random_fit_keeps_the_preferred_version_in_a_bounded_candidate_set(group_versions):
    entries = [_entry(number, title=str(number // 2)) for number in range(60)]
    preferred = entries[-1]
    versions = SongVersions(entries) if group_versions else None
    candidates = []

    def fit(sampled):
        candidates.extend(sampled)
        return sampled[0]

    picked = pick_slot(
        [ClockSlot("/music", selection="random")],
        0,
        entries,
        {},
        lambda _: True,
        Random(1),
        versions=versions,
        preferred_path=preferred.path,
        fit=fit,
    )
    assert picked.entry is preferred
    assert len(candidates) == FIT_CANDIDATES
    if versions:
        assert len({versions.song(entry.path) for entry in candidates}) == len(candidates)


@pytest.mark.parametrize("group_versions", [False, True])
def test_random_without_a_preference_keeps_its_seeded_selection(group_versions):
    entries = [_entry(1, title="Song"), _entry(2, title="Song"), _entry(3)]
    versions = SongVersions(entries) if group_versions else None
    expected = versions.choice(entries, Random(3)) if versions else Random(3).choice(entries)
    picked = pick_slot(
        [ClockSlot("/music", selection="random")],
        0,
        entries,
        {},
        lambda _: True,
        Random(3),
        versions=versions,
    )
    assert picked.entry is expected
