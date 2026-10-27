"""The GitHub release body comes from the release's CHANGELOG.md section."""

import importlib.util
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
_spec = importlib.util.spec_from_file_location("release_notes", ROOT / "scripts/release_notes.py")
assert _spec is not None and _spec.loader is not None
release_notes = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(release_notes)

BLOB = "https://github.com/blindndangerous/AutoDJ/blob/v1.2.0"
CHANGELOG = """# Changelog

## [Unreleased]

## [1.2.0] - 2026-10-23

Intro for 1.2.0, see [the guide](docs/setup.md#start) and [notes](./docs/a.md "Notes").

### Added

- A thing, like [the site](https://example.com), [mail](mailto:a@example.com),
  [elsewhere](//example.com/x) and [below](#added).
- ![A diagram](docs/diagram.png) next to `[not a link](docs/x.md)`.

---

## [1.1.0] - 2026-10-01 (not released on its own; shipped in 1.2.0)

### Fixed

- Older.

---

## A note on this file

Not part of any release.

[1.2.0]: https://github.com/blindndangerous/AutoDJ/releases/tag/v1.2.0
[1.1.0]: https://github.com/blindndangerous/AutoDJ/compare/v1.0.0...v1.1.0
"""


def test_the_notes_are_the_tags_section_with_links_at_the_tag():
    notes = release_notes.release_notes(CHANGELOG, "v1.2.0")
    assert notes.startswith("Intro for 1.2.0")
    assert f"[the guide]({BLOB}/docs/setup.md#start)" in notes
    assert f'[notes]({BLOB}/docs/a.md "Notes")' in notes
    assert "[the site](https://example.com)" in notes
    assert "[mail](mailto:a@example.com)" in notes
    assert "[elsewhere](//example.com/x)" in notes
    assert f"[below]({BLOB}/CHANGELOG.md#added)" in notes
    assert "1.1.0" not in notes and "Older." not in notes
    assert not notes.rstrip().endswith("---")


def test_images_point_at_the_file_and_code_spans_are_left_alone():
    notes = release_notes.release_notes(CHANGELOG, "v1.2.0")
    raw = "https://github.com/blindndangerous/AutoDJ/raw/v1.2.0"
    assert f"![A diagram]({raw}/docs/diagram.png)" in notes
    assert "`[not a link](docs/x.md)`" in notes


def test_the_last_release_stops_before_notes_and_link_definitions():
    assert release_notes.release_notes(CHANGELOG, "v1.1.0") == "### Fixed\n\n- Older.\n"


@pytest.mark.parametrize("tag", ["v9.9.9", "vUnreleased"])
def test_a_missing_or_empty_section_is_refused(tag):
    with pytest.raises(ValueError, match=r"CHANGELOG\.md"):
        release_notes.release_notes(CHANGELOG, tag)


def test_a_section_too_long_for_a_github_release_is_refused():
    long = CHANGELOG.replace("- Older.", "- Older.\n" + "x" * release_notes.MAX_SECTION_CHARACTERS)
    with pytest.raises(ValueError, match="characters"):
        release_notes.release_notes(long, "v1.1.0")


@pytest.mark.parametrize("tag", ["v0.22.0", "v0.1.3"])
def test_the_real_changelog_gives_publishable_notes(tag):
    notes = release_notes.release_notes((ROOT / "CHANGELOG.md").read_text("utf-8"), tag)
    assert "](docs/" not in notes
    assert "\n## " not in notes
    assert "]: http" not in notes


def test_main_prints_the_notes_and_fails_without_a_section(tmp_path, capsys):
    changelog = tmp_path / "CHANGELOG.md"
    changelog.write_text(CHANGELOG, encoding="utf-8")
    assert release_notes.main(["v1.1.0", "--changelog", str(changelog)]) == 0
    assert capsys.readouterr().out == "### Fixed\n\n- Older.\n"
    assert release_notes.main(["v3.0.0", "--changelog", str(changelog)]) == 1
    assert "no section for 3.0.0" in capsys.readouterr().err
