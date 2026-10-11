"""First-use installation of the editable related-artists file."""

from pathlib import Path

from autodj.related_artist_file import (
    ensure_related_artists_file,
    load_configured_related_artists,
    migrate_legacy_related_file,
)
from autodj.repeat_rules import load_related_artists


def test_copies_bundled_sample_on_first_use(tmp_path: Path) -> None:
    destination = ensure_related_artists_file(tmp_path)

    assert destination == tmp_path / "related-artists.toml"
    assert "Bruno Mars" in destination.read_text(encoding="utf-8")
    assert "single_acts = []" in destination.read_text(encoding="utf-8")


def test_custom_path_is_returned_without_creating_it(tmp_path: Path) -> None:
    custom = tmp_path / "custom.toml"

    assert ensure_related_artists_file(tmp_path, str(custom)) == custom
    assert not custom.exists()


def test_repeated_call_preserves_user_edits(tmp_path: Path) -> None:
    destination = ensure_related_artists_file(tmp_path)
    destination.write_text("user edit", encoding="utf-8")

    assert ensure_related_artists_file(tmp_path).read_text(encoding="utf-8") == "user edit"


def test_malformed_existing_file_is_left_untouched(tmp_path: Path) -> None:
    destination = tmp_path / "related-artists.toml"
    destination.write_text("invalid = [", encoding="utf-8")

    assert ensure_related_artists_file(tmp_path).read_text(encoding="utf-8") == "invalid = ["


def test_loader_without_an_active_directory_is_empty() -> None:
    assert not load_configured_related_artists(None).links


def test_legacy_starter_migration_copies_union_and_preserves_source(tmp_path: Path) -> None:
    source = tmp_path / "custom.toml"
    original = b'single_acts = ["Custom Band"]\n[related]\n"Custom Artist" = ["Custom Project"]\n'
    source.write_bytes(original)

    migrated = migrate_legacy_related_file(
        tmp_path / "index", {"related_artists_file": str(source), "related_artists_starter": True}
    )

    assert migrated is not None
    assert source.read_bytes() == original
    related = load_related_artists(migrated)
    assert related.related_to("bruno mars") == {"silk sonic"}
    assert related.related_to("custom artist") == {"custom project"}
    assert "Custom Band" in related.single_acts


def test_legacy_starter_migration_reuses_copy_without_source(tmp_path: Path) -> None:
    source = tmp_path / "custom.toml"
    source.write_text('[related]\n"Custom Artist" = ["First Project"]\n', encoding="utf-8")
    legacy = {"related_artists_file": str(source), "related_artists_starter": True}
    migrated = Path(migrate_legacy_related_file(tmp_path / "index", legacy))
    migrated.write_text('[related]\n"User Edit" = ["Kept"]\n', encoding="utf-8")
    source.unlink()

    assert migrate_legacy_related_file(tmp_path / "index", legacy) == str(migrated)
    assert "User Edit" in migrated.read_text(encoding="utf-8")


def test_legacy_starter_migration_uses_different_copy_for_each_source(tmp_path: Path) -> None:
    one, two = tmp_path / "one.toml", tmp_path / "two.toml"
    one.write_text("", encoding="utf-8")
    two.write_text("", encoding="utf-8")

    first = migrate_legacy_related_file(
        tmp_path / "index", {"related_artists_file": str(one), "related_artists_starter": True}
    )
    second = migrate_legacy_related_file(
        tmp_path / "index", {"related_artists_file": str(two), "related_artists_starter": True}
    )

    assert first != second
