"""First-use installation of the editable related-artists file."""

from pathlib import Path

import pytest

from autodj.related_artist_file import (
    ensure_related_artists_file,
    invalid_related_artists_message,
    load_configured_related_artists,
)
from autodj.repeat_rules import RelatedArtistsFileTooLargeError, load_related_artists


def test_copies_bundled_sample_on_first_use(tmp_path: Path) -> None:
    destination = ensure_related_artists_file(tmp_path)

    assert destination == tmp_path / "related-artists.toml"
    assert "Bruno Mars" in destination.read_text(encoding="utf-8")
    assert "single_acts = []" in destination.read_text(encoding="utf-8")


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


def test_related_artists_file_over_one_mib_is_rejected_unread(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "related-artists.toml"
    path.write_bytes(b" " * (1024 * 1024 + 1))

    def no_read(*_args, **_kwargs):
        raise AssertionError("the file was opened")

    monkeypatch.setattr(Path, "open", no_read)
    with pytest.raises(RelatedArtistsFileTooLargeError) as error:
        load_related_artists(path)
    assert invalid_related_artists_message(error.value) == (
        "The related artists file is larger than 1 MiB."
    )


@pytest.mark.parametrize(
    ("content", "message"),
    [
        (b'secret = "x"\n[related\n', "The related artists file is not valid: line 2."),
        (b'secret_token = "private"\n', "The related artists file is not valid."),
    ],
    ids=["toml syntax", "unexpected key"],
)
def test_browser_message_names_no_file_content(
    tmp_path: Path, content: bytes, message: str
) -> None:
    path = tmp_path / "related-artists.toml"
    path.write_bytes(content)
    with pytest.raises(ValueError) as error:
        load_related_artists(path)
    assert invalid_related_artists_message(error.value) == message


def test_browser_message_for_an_unreadable_file() -> None:
    assert invalid_related_artists_message(PermissionError("C:/secret")) == (
        "The related artists file could not be read."
    )
