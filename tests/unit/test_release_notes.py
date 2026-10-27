"""The GitHub release body comes from the release's CHANGELOG.md section."""

import importlib.util
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
_spec = importlib.util.spec_from_file_location("release_notes", ROOT / "scripts/release_notes.py")
assert _spec is not None and _spec.loader is not None
release_notes = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(release_notes)

CHANGELOG = """# Changelog

## [Unreleased]

## [1.2.0] - 2026-10-23

Intro for 1.2.0, see [the guide](docs/setup.md#start) and [notes](./docs/a.md).

### Added

- A thing, like [the site](https://example.com) and [below](#added).

---

## [1.1.0] - 2026-10-01

### Fixed

- Older.
"""


def test_the_notes_are_the_tags_section_with_links_at_the_tag():
    notes = release_notes.release_notes(CHANGELOG, "v1.2.0")
    base = "https://github.com/blindndangerous/AutoDJ/blob/v1.2.0"
    assert notes.startswith("Intro for 1.2.0")
    assert f"[the guide]({base}/docs/setup.md#start)" in notes
    assert f"[notes]({base}/docs/a.md)" in notes
    assert "[the site](https://example.com)" in notes
    assert "[below](#added)" in notes
    assert "1.1.0" not in notes and "Older." not in notes
    assert not notes.rstrip().endswith("---")


def test_the_last_section_runs_to_the_end_of_the_file():
    assert release_notes.release_notes(CHANGELOG, "v1.1.0") == "### Fixed\n\n- Older.\n"


@pytest.mark.parametrize("tag", ["v9.9.9", "vUnreleased"])
def test_a_missing_or_empty_section_is_refused(tag):
    with pytest.raises(ValueError, match=r"CHANGELOG\.md"):
        release_notes.release_notes(CHANGELOG, tag)


def test_the_real_changelog_has_notes_for_the_latest_release():
    notes = release_notes.release_notes((ROOT / "CHANGELOG.md").read_text("utf-8"), "v0.22.0")
    assert "### Added" in notes
    assert "](docs/" not in notes


def test_main_prints_the_notes_and_fails_without_a_section(tmp_path, capsys):
    changelog = tmp_path / "CHANGELOG.md"
    changelog.write_text(CHANGELOG, encoding="utf-8")
    assert release_notes.main(["v1.1.0", "--changelog", str(changelog)]) == 0
    assert capsys.readouterr().out == "### Fixed\n\n- Older.\n"
    assert release_notes.main(["v3.0.0", "--changelog", str(changelog)]) == 1
    assert "no section for 3.0.0" in capsys.readouterr().err
