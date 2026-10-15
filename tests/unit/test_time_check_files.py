"""Voice pack zips and clips added from the page: safe unpacking and limits (#388)."""

from __future__ import annotations

import io
import zipfile
from pathlib import Path

import numpy as np
import pytest
import soundfile as sf

from autodj import time_check_files
from autodj.time_check_files import (
    PackConflict,
    PackError,
    PackNotFound,
    PackTooLarge,
    check_pack_name,
    clip_files,
    install_clip,
    install_pack,
    remove_pack,
    subfolders,
)


def _wav(seconds: float = 0.1) -> bytes:
    buffer = io.BytesIO()
    sf.write(buffer, np.zeros((int(44100 * seconds), 2)), 44100, format="WAV")
    return buffer.getvalue()


def _zip(path: Path, entries: dict[str, bytes] | list[tuple[zipfile.ZipInfo | str, bytes]]) -> Path:
    items = entries.items() if isinstance(entries, dict) else entries
    with zipfile.ZipFile(path, "w") as archive:
        for name, data in items:
            archive.writestr(name, data)
    return path


def _left_over(root: Path) -> list[str]:
    """Hidden staging files or folders an upload left behind."""
    return sorted(entry.name for entry in root.iterdir() if entry.name.startswith("."))


def test_a_pack_is_unpacked_into_place_with_its_one_top_folder_taken_off(tmp_path: Path) -> None:
    root = tmp_path / "time_checks"
    upload = _zip(
        tmp_path / "pack.zip",
        {
            "Studio/hr_03.wav": _wav(),
            "Studio/m_05.wav": _wav(),
            "__MACOSX/Studio/._hr_03.wav": b"resource fork",
            "Studio/Thumbs.db": b"thumbnail cache",
        },
    )
    folder = install_pack(root, "Studio", upload, replace=False)
    assert folder == root / "Studio"
    assert sorted(path.name for path in folder.iterdir()) == ["hr_03.wav", "m_05.wav"]
    assert _left_over(root) == []
    assert [path.name for path in subfolders(root)] == ["Studio"]


def test_files_at_the_top_of_the_zip_are_a_pack_too(tmp_path: Path) -> None:
    upload = _zip(tmp_path / "pack.zip", {"hr_03.wav": _wav(), "intro.wav": _wav()})
    folder = install_pack(tmp_path / "root", "Night", upload, replace=False)
    assert sorted(path.name for path in folder.iterdir()) == ["hr_03.wav", "intro.wav"]


@pytest.mark.parametrize(
    ("name", "reason"),
    [
        ("../evil.wav", "outside the pack"),
        ("Studio/../../evil.wav", "outside the pack"),
        ("Studio\\..\\..\\evil.wav", "outside the pack"),
        ("/etc/evil.wav", "absolute path"),
        ("C:/Windows/evil.wav", "absolute path"),
        ("\\\\server\\share\\evil.wav", "absolute path"),
        ("./hr_01.wav", "outside the pack"),
    ],
)
def test_a_zip_slip_entry_refuses_the_whole_zip(tmp_path: Path, name: str, reason: str) -> None:
    root = tmp_path / "time_checks"
    upload = _zip(tmp_path / "pack.zip", [("hr_03.wav", _wav()), (name, _wav())])
    with pytest.raises(PackError, match=reason):
        install_pack(root, "Studio", upload, replace=False)
    assert not (root / "Studio").exists()
    assert not (tmp_path / "evil.wav").exists()
    assert _left_over(root) == []


def test_a_link_in_the_zip_is_refused(tmp_path: Path) -> None:
    link = zipfile.ZipInfo("hr_04.wav")
    link.create_system = 3
    link.external_attr = 0o120777 << 16
    upload = _zip(tmp_path / "pack.zip", [("hr_03.wav", _wav()), (link, b"/etc/passwd")])
    with pytest.raises(PackError, match="is a link"):
        install_pack(tmp_path / "root", "Studio", upload, replace=False)
    assert not (tmp_path / "root" / "Studio").exists()


@pytest.mark.parametrize(
    ("entries", "reason"),
    [
        ({"hr_03.wav": b"", "readme.txt": b"hello"}, "readme.txt is not an audio file"),
        ({"run.exe": b"MZ"}, "run.exe is not an audio file"),
        ({"hr_03.wav": b"this is not audio"}, "hr_03.wav: "),
        ({"Studio/hr_03.wav": b"", "Studio/more/m_05.wav": b""}, "not in subfolders"),
        ({"A/hr_03.wav": b"", "B/m_05.wav": b""}, "not in subfolders"),
        ({"Studio/": b""}, "no audio files"),
        ({"hr_03.wav": b"", "HR_03.WAV": b""}, "in the zip twice"),
        ({"con.wav": b""}, "Reserved device"),
    ],
)
def test_anything_but_audio_files_refuses_the_zip(
    tmp_path: Path, entries: dict[str, bytes], reason: str
) -> None:
    root = tmp_path / "root"
    with pytest.raises(PackError, match=reason):
        install_pack(root, "Studio", _zip(tmp_path / "pack.zip", entries), replace=False)
    assert not (root / "Studio").exists()
    assert _left_over(root) == []


def test_an_encrypted_entry_is_refused(tmp_path: Path) -> None:
    upload = _zip(tmp_path / "pack.zip", {"hr_03.wav": _wav()})
    data = bytearray(upload.read_bytes())
    # Set the "encrypted" flag in the local header and the central directory.
    for signature, offset in ((b"PK\x03\x04", 6), (b"PK\x01\x02", 8)):
        at = data.find(signature)
        data[at + offset] |= 0x1
    upload.write_bytes(bytes(data))
    with pytest.raises(PackError, match="encrypted"):
        install_pack(tmp_path / "root", "Studio", upload, replace=False)


def test_a_file_that_is_not_a_zip_is_refused(tmp_path: Path) -> None:
    upload = tmp_path / "pack.zip"
    upload.write_bytes(b"not a zip at all")
    with pytest.raises(PackError, match="not a zip"):
        install_pack(tmp_path / "root", "Studio", upload, replace=False)


def test_too_many_files_or_bytes_are_refused_before_unpacking(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "root"
    entries = {f"m_{minute:02d}.wav": _wav() for minute in range(5)}
    upload = _zip(tmp_path / "pack.zip", entries)
    monkeypatch.setattr(time_check_files, "MAX_PACK_FILES", 4)
    with pytest.raises(PackTooLarge, match="at most 4 files"):
        install_pack(root, "Studio", upload, replace=False)
    monkeypatch.setattr(time_check_files, "MAX_PACK_FILES", 2000)
    monkeypatch.setattr(time_check_files, "MAX_PACK_BYTES", len(_wav()) * 4)
    with pytest.raises(PackTooLarge, match="at most"):
        install_pack(root, "Studio", upload, replace=False)
    assert not (root / "Studio").exists()
    assert _left_over(root) == []


def test_unpacking_counts_the_bytes_written_not_only_the_header() -> None:
    """A header that understates a file's size cannot unpack more than it says."""
    out = io.BytesIO()
    with pytest.raises(PackTooLarge):
        time_check_files._copy_bounded(io.BytesIO(b"x" * 100), out, 10, [1000])
    with pytest.raises(PackTooLarge):
        time_check_files._copy_bounded(io.BytesIO(b"x" * 100), io.BytesIO(), 1000, [10])
    assert len(out.getvalue()) < 100


def test_a_too_long_file_is_refused(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(time_check_files, "MAX_PIECE_SECONDS", 0.05)
    upload = _zip(tmp_path / "pack.zip", {"hr_03.wav": _wav(0.2)})
    with pytest.raises(PackError, match="longer than"):
        install_pack(tmp_path / "root", "Studio", upload, replace=False)


def test_a_taken_name_needs_replace_and_replacing_swaps_the_folder(tmp_path: Path) -> None:
    root = tmp_path / "root"
    install_pack(root, "Studio", _zip(tmp_path / "a.zip", {"hr_03.wav": _wav()}), replace=False)
    second = _zip(tmp_path / "b.zip", {"m_05.wav": _wav()})
    with pytest.raises(PackConflict, match="already exists"):
        install_pack(root, "Studio", second, replace=False)
    assert [path.name for path in (root / "Studio").iterdir()] == ["hr_03.wav"]
    install_pack(root, "Studio", second, replace=True)
    assert [path.name for path in (root / "Studio").iterdir()] == ["m_05.wav"]
    assert _left_over(root) == []


def test_a_refused_replacement_keeps_the_old_pack(tmp_path: Path) -> None:
    root = tmp_path / "root"
    install_pack(root, "Studio", _zip(tmp_path / "a.zip", {"hr_03.wav": _wav()}), replace=False)
    bad = _zip(tmp_path / "b.zip", {"notes.txt": b"x"})
    with pytest.raises(PackError):
        install_pack(root, "Studio", bad, replace=True)
    assert [path.name for path in (root / "Studio").iterdir()] == ["hr_03.wav"]
    assert _left_over(root) == []


@pytest.mark.parametrize("name", ["", "..", ".hidden", "a/b", "a\\b", "C:", "nul", "x" * 101, "a."])
def test_a_pack_name_must_be_one_plain_folder_name(name: str) -> None:
    with pytest.raises(PackError):
        check_pack_name(name)


def test_removing_a_pack_deletes_only_that_folder(tmp_path: Path) -> None:
    root = tmp_path / "root"
    install_pack(root, "Studio", _zip(tmp_path / "a.zip", {"hr_03.wav": _wav()}), replace=False)
    install_pack(root, "Night", _zip(tmp_path / "b.zip", {"hr_03.wav": _wav()}), replace=False)
    remove_pack(root, "Studio")
    assert [path.name for path in subfolders(root)] == ["Night"]
    assert _left_over(root) == []
    with pytest.raises(PackNotFound):
        remove_pack(root, "Studio")
    with pytest.raises(PackError):
        remove_pack(root, "../root")


def test_a_linked_pack_folder_is_not_removed(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    real = tmp_path / "elsewhere"
    real.mkdir()
    (real / "hr_03.wav").write_bytes(_wav())
    try:
        (root / "Linked").symlink_to(real, target_is_directory=True)
    except OSError:
        pytest.skip("this account cannot make symbolic links")
    with pytest.raises(PackConflict, match="is a link"):
        remove_pack(root, "Linked")
    assert (real / "hr_03.wav").is_file()


def test_a_clip_is_checked_then_moved_into_place(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    upload = root / ".clip-upload-1.wav"
    upload.write_bytes(_wav())
    install_clip(root, "intro.wav", upload, replace=False)
    assert (root / "intro.wav").is_file() and not upload.exists()
    again = root / ".clip-upload-2.wav"
    again.write_bytes(_wav())
    with pytest.raises(PackConflict):
        install_clip(root, "intro.wav", again, replace=False)
    assert not again.exists()
    bad = root / ".clip-upload-3.wav"
    bad.write_bytes(b"not audio")
    with pytest.raises(PackError):
        install_clip(root, "outro.wav", bad, replace=False)
    assert not (root / "outro.wav").exists() and not bad.exists()
    for name in ("notes.txt", "../x.wav", ".hidden.wav"):
        with pytest.raises(PackError):
            install_clip(root, name, root / "missing.wav", replace=False)


def test_clip_choices_leave_out_minute_clips_and_hidden_files(tmp_path: Path) -> None:
    for name in ("intro.wav", "03_05.wav", "Outro.MP3", ".part.wav", "notes.txt"):
        (tmp_path / name).write_bytes(b"x")
    (tmp_path / "Studio").mkdir()
    (tmp_path / ".pack-upload-1").mkdir()
    assert clip_files(tmp_path) == ["intro.wav", "Outro.MP3"]
    assert [path.name for path in subfolders(tmp_path)] == ["Studio"]
    assert clip_files(tmp_path / "missing") == [] and subfolders(tmp_path / "missing") == []
