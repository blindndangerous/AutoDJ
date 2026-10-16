"""Getting Stereo Tool's plugin library and its licence key (autodj.stereotool_setup).

Nothing here downloads the real Stereo Tool: every HTTP answer is a stand-in,
and the "libraries" in the stand-in zips are only headers, never loaded.
"""

from __future__ import annotations

import io
import json
import logging
import os
import stat
import subprocess
import sys
import threading
import urllib.error
import zipfile
from email.message import Message
from pathlib import Path

import pytest

from autodj import stereotool_setup as setup
from autodj.config import stereotool_library_name

KEY = "<ABCD-1234-SECRET-KEY-5678>"
TOP = "libStereoTool_1105"


def _elf(machine: int = 62, kind: int = 3, size: int = 256) -> bytes:
    head = bytearray(b"\x7fELF" + bytes([2, 1, 1]) + bytes(9))
    head += kind.to_bytes(2, "little") + machine.to_bytes(2, "little")
    return bytes(head) + bytes(size)


def _pe(machine: int = 0x8664, dll: bool = True) -> bytes:
    data = bytearray(512)
    data[0:2] = b"MZ"
    data[60:64] = (128).to_bytes(4, "little")
    data[128:132] = b"PE\0\0"
    data[132:134] = machine.to_bytes(2, "little")
    data[150:152] = (0x2022 if dll else 0x0022).to_bytes(2, "little")
    return bytes(data)


def _build() -> tuple[setup.Build, bytes]:
    """This computer's build (as if it had one) and a library header that passes."""
    if os.name == "nt":
        return setup._WINDOWS_X64, _pe()
    return setup._LINUX_X86_64, _elf()


def _zip(entries: dict[str, bytes], *, links: tuple[str, ...] = ()) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, data in entries.items():
            info = zipfile.ZipInfo(name)
            # Kept as written: ZipInfo turns backslashes into slashes on
            # Windows, and Thimeo's zip has backslashes.
            info.filename = name
            info.compress_type = zipfile.ZIP_DEFLATED
            if name in links:
                info.external_attr = (stat.S_IFLNK | 0o777) << 16
            archive.writestr(info, data)
    return buffer.getvalue()


def _plugin(build: setup.Build, library: bytes, top: str = TOP, **extra: bytes) -> bytes:
    """A stand-in for Thimeo's plugin zip with *library* at *build*'s place."""
    entries = {
        f"{top}/include/libStereoTool.h": b"// header",
        f"{top}/lib/Linux/IntelAMD/64/libStereoToolX11_intel64.so": _elf(),
        f"{top}/lib/Linux/ARM/64/libStereoTool_arm64.so": _elf(183),
        f"{top}/lib/Windows/32/libStereoTool_32.dll": _pe(0x14C),
        f"{top}/lib/{build.member}": library,
    }
    entries.update(extra)
    return _zip(entries)


class _Response:
    def __init__(self, body: bytes, length: str | None = None) -> None:
        self._body = io.BytesIO(body)
        self.headers = {"Content-Length": str(len(body)) if length is None else length}
        self.closed = False

    def read(self, size: int = -1) -> bytes:
        return self._body.read(size)

    def close(self) -> None:
        self.closed = True


def _redirect(url: str, location: str | None, code: int = 302) -> urllib.error.HTTPError:
    headers = Message()
    if location is not None:
        headers["Location"] = location
    return urllib.error.HTTPError(url, code, "Found", headers, io.BytesIO(b""))


def _server(answers: dict[str, object]):
    """open_url that answers each address from *answers*, recording the order."""
    asked: list[str] = []

    def open_url(url: str, _timeout: float):
        asked.append(url)
        answer = answers[url]
        if isinstance(answer, BaseException):
            raise answer
        return answer

    open_url.asked = asked  # type: ignore[attr-defined]
    return open_url


def _thimeo(body: bytes):
    return _server({setup.PLUGIN_URL: _Response(body)})


# ---------------------------------------------------------------------------
# Where the library is
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("system", "machine", "name"),
    [
        ("linux", "x86_64", "libStereoTool_intel64.so"),
        ("linux", "aarch64", "libStereoTool_noX11_arm64.so"),
        ("win32", "AMD64", "libStereoTool_64.dll"),
        ("darwin", "arm64", "libStereoTool_64.dylib"),
    ],
)
def test_each_platform_names_its_library(system: str, machine: str, name: str) -> None:
    assert stereotool_library_name(system, machine) == name


def test_every_station_uses_the_library_in_the_index_folder(tmp_path: Path) -> None:
    managed = setup.managed_path(tmp_path)
    assert managed == tmp_path / "tools" / "stereo-tool" / stereotool_library_name()
    assert setup.library_path(tmp_path) == str(managed)
    assert not setup.library_present(str(managed))
    managed.parent.mkdir(parents=True)
    managed.write_bytes(b"x")
    assert setup.library_present(str(managed))


def test_a_folder_named_like_the_library_is_not_used(tmp_path: Path) -> None:
    setup.managed_path(tmp_path).mkdir(parents=True)
    assert not setup.library_present(str(setup.managed_path(tmp_path)))


@pytest.mark.parametrize(
    ("system", "machine", "member"),
    [
        ("linux", "x86_64", "Linux/IntelAMD/64/libStereoTool_intel64.so"),
        ("linux", "aarch64", "Linux/ARM/64/libStereoTool_noX11_arm64.so"),
        ("win32", "AMD64", "Windows/64/libStereoTool_64.dll"),
        ("win32", "ARM64", None),
        ("darwin", "arm64", None),
        ("linux", "armv7l", None),
    ],
)
def test_each_platform_gets_its_library_from_the_plugin(
    system: str, machine: str, member: str | None
) -> None:
    build = setup.platform_build(system, machine)
    assert (build.member if build else None) == member


def test_a_computer_without_a_library_is_told_why(monkeypatch) -> None:
    note = setup.unsupported_note("darwin", "arm64")
    assert "64-bit Linux (PC or ARM) and 64-bit Windows" in note
    assert "darwin, arm64" in note
    assert "tools/stereo-tool" in note
    monkeypatch.setattr(setup, "platform_build", lambda: None)
    monkeypatch.setattr(setup, "unsupported_note", lambda: note)
    status = setup.program_status(Path("nowhere"))
    assert status["download"] == {"available": False, "platform": None, "note": note, "busy": False}
    with pytest.raises(setup.NoBuildError, match="64-bit Windows"):
        setup.install(Path("nowhere"))


def test_status_reports_the_recorded_version_and_never_a_path(tmp_path: Path) -> None:
    build, library = _build()
    setup.install(tmp_path, build=build, open_url=_thimeo(_plugin(build, library)))
    result = setup.program_status(tmp_path)
    assert result["installed"] is True
    assert result["version"] == "11.05"
    assert str(tmp_path) not in repr(result)


def test_a_hand_placed_library_takes_the_version_stereo_tool_reports(tmp_path: Path) -> None:
    managed = setup.managed_path(tmp_path)
    managed.parent.mkdir(parents=True)
    managed.write_bytes(b"x")
    assert setup.program_status(tmp_path)["version"] is None
    running = {"version": "11.10"}
    status = setup.program_status(tmp_path, running=running)
    assert status["version"] == "11.10"
    # A version recorded for another file is not this one's.
    (managed.parent / setup.VERSION_FILE).write_text("libStereoTool_other.so 11.05\n")
    assert setup.installed_version(managed) is None


def test_status_of_a_missing_library(tmp_path: Path) -> None:
    result = setup.program_status(tmp_path)
    assert result["installed"] is False
    assert result["version"] is None


@pytest.mark.parametrize(
    ("top", "version"),
    [
        ("libStereoTool_1105", "11.05"),
        ("libStereoTool_1001", "10.01"),
        ("libStereoTool_912", "9.12"),
        ("libStereoTool_1106BETA3", "11.06"),
        ("libStereoTool_x", None),
        ("Stereo_Tool_Generic_plugin_1105", None),
    ],
)
def test_the_version_comes_from_the_zip_folder(top: str, version: str | None) -> None:
    assert setup.plugin_version(top) == version


# ---------------------------------------------------------------------------
# Downloading
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "url",
    [
        "http://download.thimeo.com/Stereo_Tool_Generic_plugin.zip",
        "https://download.thimeo.com.evil.example/Stereo_Tool_Generic_plugin.zip",
        "https://evilthimeo.com/Stereo_Tool_Generic_plugin.zip",
        "https://example.com/Stereo_Tool_Generic_plugin.zip",
        "https://user:pass@download.thimeo.com/x",
        "https://download.thimeo.com:8443/x",
        "ftp://download.thimeo.com/x",
    ],
)
def test_only_https_thimeo_addresses_are_allowed(url: str) -> None:
    assert not setup.allowed_url(url)


@pytest.mark.parametrize(
    "url",
    [
        "https://download.thimeo.com%2eevil.example/x",
        "https://download%2ethimeo.com/x",
        "https://evil.example\\@download.thimeo.com/x",
        "https://download.thimeo.com\\.evil.example/x",
        "https://download.thimeo.com /x",
        "https://download.thimeo.com\t.evil.example/x",
        "https://down\nload.thimeo.com/x",
        "https://download.thimeo.com\u3002evil.example/x",
        "https://dównload.thimeo.com/x",
    ],
)
def test_odd_characters_in_the_address_are_refused(url: str) -> None:
    assert not setup.allowed_url(url)


def test_a_download_past_its_deadline_is_stopped() -> None:
    start = "https://download.thimeo.com/Stereo_Tool_Generic_plugin.zip"
    now = [0.0]

    class Slow(_Response):
        def read(self, size: int = -1) -> bytes:
            now[0] += 1000  # each chunk "takes" 1000 seconds
            return super().read(4)

    with pytest.raises(setup.DownloadError, match="took longer than 30 minutes"):
        setup.fetch(
            start,
            io.BytesIO(),
            open_url=_server({start: Slow(b"x" * 64, length="")}),
            clock=lambda: now[0],
        )


def test_thimeo_and_its_subdomains_are_allowed() -> None:
    assert setup.allowed_url("https://thimeo.com/x")
    assert setup.allowed_url("https://download.thimeo.com/Stereo_Tool_Generic_plugin.zip")
    assert setup.allowed_url("https://cdn.download.thimeo.com:443/x")


def test_a_redirect_to_another_thimeo_host_is_followed() -> None:
    start = "https://download.thimeo.com/Stereo_Tool_Generic_plugin.zip"
    moved = "https://files.thimeo.com/11.05/Stereo_Tool_Generic_plugin.zip"
    open_url = _server({start: _redirect(start, moved), moved: _Response(b"program")})
    out = io.BytesIO()
    assert setup.fetch(start, out, open_url=open_url) == 7
    assert out.getvalue() == b"program"
    assert open_url.asked == [start, moved]


def test_a_relative_redirect_stays_on_the_host() -> None:
    start = "https://download.thimeo.com/Stereo_Tool_Generic_plugin.zip"
    moved = "https://download.thimeo.com/new/Stereo_Tool_Generic_plugin.zip"
    open_url = _server(
        {start: _redirect(start, "/new/Stereo_Tool_Generic_plugin.zip"), moved: _Response(b"x")}
    )
    setup.fetch(start, io.BytesIO(), open_url=open_url)
    assert open_url.asked == [start, moved]


def test_a_redirect_to_another_host_is_refused_before_it_is_asked() -> None:
    start = "https://download.thimeo.com/Stereo_Tool_Generic_plugin.zip"
    open_url = _server(
        {start: _redirect(start, "https://mirror.example.net/Stereo_Tool_Generic_plugin.zip")}
    )
    with pytest.raises(setup.DownloadError, match=r"Refused to download from mirror\.example\.net"):
        setup.fetch(start, io.BytesIO(), open_url=open_url)
    assert open_url.asked == [start]


def test_a_redirect_to_plain_http_is_refused() -> None:
    start = "https://download.thimeo.com/Stereo_Tool_Generic_plugin.zip"
    open_url = _server(
        {start: _redirect(start, "http://download.thimeo.com/Stereo_Tool_Generic_plugin.zip")}
    )
    with pytest.raises(setup.DownloadError, match=r"only fetched from thimeo\.com over HTTPS"):
        setup.fetch(start, io.BytesIO(), open_url=open_url)


def test_endless_redirects_stop() -> None:
    start = "https://download.thimeo.com/a"
    open_url = _server({start: _redirect(start, start)})
    with pytest.raises(setup.DownloadError, match="too many times"):
        setup.fetch(start, io.BytesIO(), open_url=open_url)
    assert len(open_url.asked) == setup.MAX_REDIRECTS + 1


def test_an_oversized_download_is_refused_by_its_declared_length() -> None:
    start = "https://download.thimeo.com/Stereo_Tool_Generic_plugin.zip"
    response = _Response(b"", length=str(setup.MAX_DOWNLOAD_BYTES + 1))
    out = io.BytesIO()
    with pytest.raises(setup.DownloadError, match="larger than 768 MB"):
        setup.fetch(start, out, open_url=_server({start: response}))
    assert out.getvalue() == b""
    assert response.closed


def test_an_oversized_download_is_refused_while_it_streams() -> None:
    start = "https://download.thimeo.com/Stereo_Tool_Generic_plugin.zip"
    response = _Response(b"x" * 2048, length="")
    with pytest.raises(setup.DownloadError, match="larger than"):
        setup.fetch(start, io.BytesIO(), open_url=_server({start: response}), limit=1024)


def test_error_answers_and_empty_files_are_refused() -> None:
    start = "https://download.thimeo.com/Stereo_Tool_Generic_plugin.zip"
    missing = urllib.error.HTTPError(start, 404, "Not Found", Message(), io.BytesIO(b""))
    with pytest.raises(setup.DownloadError, match="error 404"):
        setup.fetch(start, io.BytesIO(), open_url=_server({start: missing}))
    with pytest.raises(setup.DownloadError, match="empty"):
        setup.fetch(start, io.BytesIO(), open_url=_server({start: _Response(b"")}))
    offline = urllib.error.URLError("no route to host")
    with pytest.raises(setup.DownloadError, match=r"Could not reach thimeo\.com: no route to host"):
        setup.fetch(start, io.BytesIO(), open_url=_server({start: offline}))


def test_install_takes_only_this_computers_library_from_the_zip(tmp_path: Path) -> None:
    build, library = _build()
    before = setup.generation()
    installed = setup.install(tmp_path, build=build, open_url=_thimeo(_plugin(build, library)))
    target = setup.managed_path(tmp_path)
    assert installed == setup.Installed(target, "11.05")
    assert target.read_bytes() == library
    assert setup.generation() > before
    # Only the library and its version record: no other member, no zip left.
    assert sorted(path.name for path in target.parent.iterdir()) == sorted(
        [target.name, setup.VERSION_FILE]
    )
    assert setup.library_path(tmp_path) == str(target)
    assert setup.installed_version(target) == "11.05"


def test_update_replaces_the_old_library(tmp_path: Path) -> None:
    build, library = _build()
    target = setup.managed_path(tmp_path)
    target.parent.mkdir(parents=True)
    target.write_bytes(b"old")
    setup.install(
        tmp_path, build=build, open_url=_thimeo(_plugin(build, library, top="libStereoTool_1110"))
    )
    assert target.read_bytes() == library
    assert setup.installed_version(target) == "11.10"


@pytest.mark.parametrize(
    "library",
    ["script", "html", "wrong-machine", "program-not-library"],
)
def test_a_file_that_is_not_a_library_for_this_computer_is_refused(
    tmp_path: Path, library: str
) -> None:
    build, _good = _build()
    data = {
        "script": b"#!/bin/sh\nrm -rf /\n",
        "html": b"<html>not found</html>",
        "wrong-machine": _elf(machine=183) if build.kind == "elf" else _pe(machine=0x14C),
        "program-not-library": _elf(kind=2) if build.kind == "elf" else _pe(dll=False),
    }[library]
    old = setup.managed_path(tmp_path)
    old.parent.mkdir(parents=True)
    old.write_bytes(b"working library")
    with pytest.raises(setup.DownloadError, match=r"is not a .* library, so it was not installed"):
        setup.install(tmp_path, build=build, open_url=_thimeo(_plugin(build, data)))
    assert old.read_bytes() == b"working library"
    assert sorted(path.name for path in old.parent.iterdir()) == [old.name]


@pytest.mark.parametrize(
    "name",
    [
        "../evil.so",
        f"{TOP}/../../evil.so",
        "/etc/cron.d/evil",
        # Thimeo's own names use backslashes, so these are traversal too.
        f"{TOP}\\..\\..\\evil.so",
        "\\evil.so",
        "C:\\Windows\\evil.dll",
        "C:/Windows/evil.dll",
    ],
)
def test_a_zip_with_a_name_outside_its_folder_is_refused(tmp_path: Path, name: str) -> None:
    build, library = _build()
    body = _plugin(build, library, **{name: b"evil"})
    with pytest.raises(setup.DownloadError, match="outside its own folder"):
        setup.install(tmp_path, build=build, open_url=_thimeo(body))
    assert not (tmp_path / "evil.so").exists()
    assert list(setup.tools_dir(tmp_path).iterdir()) == []


@pytest.mark.parametrize(
    ("build", "kantar"),
    [
        (setup._LINUX_X86_64, "Linux\\IntelAMD\\64\\Kantar\\libStereoTool_intel64.so"),
        (setup._LINUX_ARM64, "Linux\\ARM\\64\\Kantar\\libStereoTool_noX11_arm64.so"),
        (setup._WINDOWS_X64, "Windows\\64\\Kantar\\libStereoTool_64.dll"),
    ],
)
def test_thimeos_backslash_names_are_read_and_the_kantar_copy_never_picked(
    tmp_path: Path, build: setup.Build, kantar: str
) -> None:
    """The member names exactly as Thimeo's 11.05 zip writes them, on any platform."""
    real = _elf(build.machine) if build.kind == "elf" else _pe()
    member = "libStereoTool_1105\\lib\\" + build.member.replace("/", "\\")
    entries = {
        "libStereoTool_1105\\include\\libStereoTool.h": b"// header",
        "libStereoTool_1105\\lib\\Linux\\IntelAMD\\64\\libStereoToolX11_intel64.so": _elf(),
        "libStereoTool_1105\\lib\\Linux\\IntelAMD\\64\\Kantar\\libStereoToolX11_intel64.so": _elf(),
        f"libStereoTool_1105\\lib\\{kantar}": real + b"KANTAR WATERMARKING BUILD",
        member: real,
    }
    archive = tmp_path / "plugin.zip"
    archive.write_bytes(_zip(entries))
    out = io.BytesIO()
    assert setup.extract_library(archive, build, out) == "11.05"
    assert out.getvalue() == real


def test_a_library_that_is_a_link_is_refused(tmp_path: Path) -> None:
    build, _library = _build()
    member = f"{TOP}/lib/{build.member}"
    body = _zip({member: b"/etc/passwd"}, links=(member,))
    with pytest.raises(setup.DownloadError, match="is a link"):
        setup.install(tmp_path, build=build, open_url=_thimeo(body))
    assert list(setup.tools_dir(tmp_path).iterdir()) == []


@pytest.mark.parametrize(
    ("body", "words"),
    [
        (b"not a zip at all", "not a readable zip"),
        (_zip({f"{TOP}/include/libStereoTool.h": b"x"}), "has no"),
    ],
)
def test_a_zip_without_this_library_is_refused(tmp_path: Path, body: bytes, words: str) -> None:
    build, _library = _build()
    with pytest.raises(setup.DownloadError, match=words):
        setup.install(tmp_path, build=build, open_url=_thimeo(body))
    assert list(setup.tools_dir(tmp_path).iterdir()) == []


def test_a_zip_with_two_libraries_for_this_computer_is_refused(tmp_path: Path) -> None:
    build, library = _build()
    body = _plugin(build, library, **{f"libStereoTool_1110/lib/{build.member}": library})
    with pytest.raises(setup.DownloadError, match="more than one"):
        setup.install(tmp_path, build=build, open_url=_thimeo(body))


def test_a_damaged_library_in_the_zip_is_refused(tmp_path: Path) -> None:
    build, library = _build()
    body = bytearray(_zip({f"{TOP}/lib/{build.member}": library + b"x" * 4096}))
    # Corrupt the stored CRC in the local header and the central directory.
    for marker in (b"PK\x03\x04", b"PK\x01\x02"):
        at = body.find(marker)
        crc = at + (14 if marker == b"PK\x03\x04" else 16)
        body[crc : crc + 4] = b"\0\0\0\0"
    with pytest.raises(setup.DownloadError, match="damaged"):
        setup.install(tmp_path, build=build, open_url=_thimeo(bytes(body)))
    assert list(setup.tools_dir(tmp_path).iterdir()) == []


def test_an_oversized_library_in_the_zip_is_refused(tmp_path: Path, monkeypatch) -> None:
    build, library = _build()
    monkeypatch.setattr(setup, "MAX_LIBRARY_BYTES", 1024)
    body = _plugin(build, library + bytes(4096))
    with pytest.raises(setup.DownloadError, match="too large"):
        setup.install(tmp_path, build=build, open_url=_thimeo(body))
    assert list(setup.tools_dir(tmp_path).iterdir()) == []


def test_an_oversized_download_leaves_nothing_behind(tmp_path: Path) -> None:
    build, _library = _build()
    with pytest.raises(setup.DownloadError, match="larger than"):
        setup.install(
            tmp_path,
            build=build,
            open_url=_server({setup.PLUGIN_URL: _Response(b"x" * 4096, length="")}),
            limit=1024,
        )
    assert list(setup.tools_dir(tmp_path).iterdir()) == []


def test_a_busy_download_is_refused(tmp_path: Path) -> None:
    build, _library = _build()
    assert setup._install_lock.acquire(blocking=False)
    try:
        with pytest.raises(setup.DownloadBusy):
            setup.install(tmp_path, build=build, open_url=_server({}))
    finally:
        setup._install_lock.release()


def test_leftovers_of_an_interrupted_download_are_removed(tmp_path: Path) -> None:
    build, library = _build()
    folder = setup.tools_dir(tmp_path)
    folder.mkdir(parents=True)
    target = setup.managed_path(tmp_path)
    (folder / ".download-0123.zip").write_bytes(b"partial")
    (folder / f".download-0123{target.suffix}").write_bytes(b"partial")
    (folder / "..version.abc").write_bytes(b"partial")
    (folder / f".{target.stem}.old-abc{target.suffix}").write_bytes(b"old")
    (folder / "README.txt").write_text("kept")
    target.write_bytes(b"current")
    setup.install(tmp_path, build=build, open_url=_thimeo(_plugin(build, library)))
    assert sorted(path.name for path in folder.iterdir()) == sorted(
        ["README.txt", target.name, setup.VERSION_FILE]
    )


def test_an_old_library_left_by_a_cut_short_update_is_put_back_not_deleted(
    tmp_path: Path,
) -> None:
    folder = setup.tools_dir(tmp_path)
    folder.mkdir(parents=True)
    target = setup.managed_path(tmp_path)
    older = folder / f".{target.stem}.old-111{target.suffix}"
    newer = folder / f".{target.stem}.old-222{target.suffix}"
    older.write_bytes(b"older")
    newer.write_bytes(b"newest working copy")
    os.utime(older, ns=(1_000_000_000, 1_000_000_000))
    setup._remove_leftovers(folder, target)
    assert target.read_bytes() == b"newest working copy"
    assert sorted(path.name for path in folder.iterdir()) == [target.name]


# ---------------------------------------------------------------------------
# Putting the new library in place on Windows (os.replace stood in for)
# ---------------------------------------------------------------------------


def _replacer(monkeypatch, fails):
    """Make os.replace raise PermissionError for (source name, target name) pairs in *fails*.

    *fails* maps a pair to how many more times it fails (-1: always).
    """
    real = os.replace
    calls: list[tuple[str, str]] = []

    def replace(src, dst):
        pair = (Path(src).name, Path(dst).name)
        calls.append(pair)
        for (source, dest), left in list(fails.items()):
            if Path(src).name.startswith(source) and Path(dst).name.startswith(dest) and left:
                fails[(source, dest)] = left - 1
                raise PermissionError(13, "Access is denied", str(src))
        return real(src, dst)

    monkeypatch.setattr(setup.os, "replace", replace)
    return calls


def _setup_part(tmp_path: Path, old: bytes | None = b"old library") -> tuple[Path, Path]:
    folder = tmp_path / "tools"
    folder.mkdir()
    part = folder / ".download-1.dll"
    part.write_bytes(b"new library")
    target = folder / "libStereoTool_64.dll"
    if old is not None:
        target.write_bytes(old)
    return part, target


def test_a_loaded_library_is_moved_aside_and_replaced(tmp_path: Path, monkeypatch) -> None:
    part, target = _setup_part(tmp_path)
    calls = _replacer(monkeypatch, {(".download", "libStereoTool_64"): 1})
    setup._replace(part, target, windows=True, sleep=lambda _s: None)
    assert target.read_bytes() == b"new library"
    assert sorted(path.name for path in target.parent.iterdir()) == [target.name]
    assert [dest.split("-")[0] for _src, dest in calls] == [
        "libStereoTool_64.dll",
        ".libStereoTool_64.old",
        "libStereoTool_64.dll",
    ]


def test_the_old_library_is_put_back_when_the_new_one_cannot_take_its_place(
    tmp_path: Path, monkeypatch
) -> None:
    part, target = _setup_part(tmp_path)
    _replacer(monkeypatch, {(".download", "libStereoTool_64"): -1})
    with pytest.raises(PermissionError):
        setup._replace(part, target, windows=True, sleep=lambda _s: None)
    assert target.read_bytes() == b"old library"
    assert sorted(path.name for path in target.parent.iterdir()) == sorted([target.name, part.name])


def test_a_locked_download_on_a_first_install_gives_words_not_file_not_found(
    tmp_path: Path, monkeypatch
) -> None:
    part, target = _setup_part(tmp_path, old=None)
    waits: list[float] = []
    _replacer(monkeypatch, {(".download", "libStereoTool_64"): -1})
    with pytest.raises(setup.DownloadError, match="stayed in use"):
        setup._replace(part, target, windows=True, sleep=waits.append)
    assert len(waits) == setup._REPLACE_TRIES
    assert not target.exists()


def test_a_download_locked_for_a_moment_is_retried(tmp_path: Path, monkeypatch) -> None:
    part, target = _setup_part(tmp_path, old=None)
    _replacer(monkeypatch, {(".download", "libStereoTool_64"): 2})
    setup._replace(part, target, windows=True, sleep=lambda _s: None)
    assert target.read_bytes() == b"new library"


def test_elsewhere_a_refused_rename_is_not_retried(tmp_path: Path, monkeypatch) -> None:
    part, target = _setup_part(tmp_path)
    calls = _replacer(monkeypatch, {(".download", "libStereoTool_64"): -1})
    with pytest.raises(PermissionError):
        setup._replace(part, target, windows=False, sleep=lambda _s: None)
    assert len(calls) == 1
    assert target.read_bytes() == b"old library"


# ---------------------------------------------------------------------------
# The licence key
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "value", ["", "   ", "abc", "has a space", "line\nbreak", "é" * 10, "x" * 2049, 42]
)
def test_bad_keys_are_refused_without_repeating_them(value) -> None:
    with pytest.raises(ValueError) as caught:
        setup.validate_key(value)
    if isinstance(value, str) and value.strip():
        assert value.strip() not in str(caught.value)


def test_a_key_is_trimmed() -> None:
    assert setup.validate_key(f"  {KEY}\n") == KEY


def test_a_saved_key_is_private_and_restarts_stereo_tool(tmp_path: Path) -> None:
    path = setup.install_license_path(tmp_path)
    before = setup.generation()
    assert setup.save_license(path, KEY) is True
    assert path.read_text(encoding="ascii") == KEY + "\n"
    assert setup.generation() > before
    if os.name != "nt":
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert [entry.name for entry in tmp_path.iterdir()] == [path.name]
    assert setup.clear_license(path) is True
    assert setup.clear_license(path) is False
    assert not path.exists()


@pytest.mark.skipif(sys.platform != "win32", reason="Windows access lists")
def test_a_saved_key_is_private_on_windows(tmp_path: Path) -> None:
    path = setup.install_license_path(tmp_path)
    setup.save_license(path, KEY)
    listing = subprocess.run(["icacls", str(path)], capture_output=True, text=True, check=True)
    user = subprocess.run(["whoami"], capture_output=True, text=True, check=True).stdout.strip()
    entries = [line.replace(str(path), "").strip() for line in listing.stdout.splitlines()]
    entries = [line for line in entries if line and ":" in line and "processed" not in line]
    assert [line.lower() for line in entries] == [f"{user.lower()}:(f)"]


def _state(index: Path, station: Path, **environ: str) -> setup.LicenseState:
    return setup.license_state(index_dir=index, state_dir=station, environ=environ)


def test_a_station_key_restarts_only_that_station_and_an_unchanged_key_nothing(
    tmp_path: Path,
) -> None:
    night, day = tmp_path / "night", tmp_path / "day"
    every, night_before, day_before = (
        setup.generation(),
        setup.generation(night),
        setup.generation(day),
    )
    assert setup.save_station_license(night, KEY) is True
    assert setup.generation(night) > night_before
    assert (setup.generation(), setup.generation(day)) == (every, day_before)
    night_now = setup.generation(night)
    assert setup.save_station_license(night, f" {KEY} ") is False
    assert setup.generation(night) == night_now
    # The key for every station restarts every station.
    setup.save_license(setup.install_license_path(tmp_path), KEY)
    assert setup.generation(day) > day_before
    assert setup.generation(night) > night_now
    assert setup.clear_station_license(night) is True
    assert setup.generation(day) == setup.generation()


def test_an_unreadable_saved_key_is_reported(tmp_path: Path) -> None:
    index, station = tmp_path, tmp_path / "night"
    station.mkdir()
    setup.save_license(setup.install_license_path(index), KEY)
    setup.old_station_license_path(station).write_bytes(b"\xff\xfe not text")
    state = _state(index, station)
    assert (state.source, state.key, state.station) == ("station", None, "saved")
    assert state.problem == "This station's saved licence does not hold a Stereo Tool licence key."
    setup.old_station_license_path(station).unlink()
    setup.install_license_path(index).write_text("two words")
    state = _state(index, station)
    assert (state.source, state.key, state.install) == ("install", None, "saved")
    assert "does not hold a Stereo Tool licence key" in (state.problem or "")


def test_the_install_key_is_shared_unless_a_station_has_its_own(tmp_path: Path) -> None:
    index, station = tmp_path, tmp_path / "default"
    station.mkdir()
    assert _state(index, station).source is None
    setup.save_license(setup.install_license_path(index), KEY)
    state = _state(index, station)
    assert (state.source, state.key, state.install, state.station) == (
        "install",
        KEY,
        "saved",
        None,
    )
    setup.save_station_license(station, "STATION-KEY-1")
    state = _state(index, station)
    assert (state.source, state.key, state.station) == ("station", "STATION-KEY-1", "saved")


def test_the_environment_overrides_the_saved_install_key(tmp_path: Path) -> None:
    index, station = tmp_path, tmp_path / "default"
    setup.save_license(setup.install_license_path(index), KEY)
    state = _state(index, station, AUTODJ_STEREOTOOL_LICENSE_KEY=" ENV-KEY-1234 ")
    assert (state.source, state.key, state.install) == (
        "environment",
        "ENV-KEY-1234",
        "environment",
    )
    state = _state(index, station, AUTODJ_STEREOTOOL_LICENSE_KEY="bad key")
    assert (state.source, state.key) == ("environment", None)
    assert state.problem == "AUTODJ_STEREOTOOL_LICENSE_KEY does not hold a licence key."


def test_the_key_never_appears_in_the_state_text_or_public_view(tmp_path: Path) -> None:
    setup.save_license(setup.install_license_path(tmp_path), KEY)
    state = _state(tmp_path, tmp_path / "default")
    assert state.key == KEY
    assert KEY not in repr(state)
    assert KEY not in str(state.public())
    assert state.public() == {
        "in_use": "install",
        "install": "saved",
        "station": None,
        "problem": None,
    }


def test_a_licence_problem_is_logged_once_without_any_key(tmp_path: Path, caplog) -> None:
    night = tmp_path / "night"
    night.mkdir()
    setup.old_station_license_path(night).write_text("bad key with spaces " + KEY)
    with caplog.at_level(logging.WARNING, logger="autodj.stereotool_setup"):
        for _ in range(3):
            assert setup.license_key(index_dir=tmp_path, state_dir=night) is None
    messages = [record.getMessage() for record in caplog.records]
    assert len(messages) == 1
    assert KEY not in messages[0]


# ---------------------------------------------------------------------------
# A station's own key, in its secrets.json
# ---------------------------------------------------------------------------

_OTHER_SECRETS = {
    "stream": {"listener_user": "dj", "listener_password": "listen-secret"},
    "webhooks": [{"url": "https://example.com/hook"}],
}


def _write_secrets(station: Path, document: dict) -> Path:
    station.mkdir(parents=True, exist_ok=True)
    path = setup.station_secrets_path(station)
    path.write_text(json.dumps({"version": 1, **document}), encoding="utf-8")
    return path


def _secrets(station: Path) -> dict:
    return json.loads(setup.station_secrets_path(station).read_text(encoding="utf-8"))


def test_a_station_key_is_kept_in_secrets_json_beside_its_other_secrets(tmp_path: Path) -> None:
    station = tmp_path / "night"
    path = _write_secrets(station, _OTHER_SECRETS)
    assert setup.save_station_license(station, KEY) is True
    assert _secrets(station) == {
        "version": 1,
        **_OTHER_SECRETS,
        "stereo_tool": {"license_key": KEY},
    }
    if os.name != "nt":
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
    # Nothing else is left in the folder: no old key file, no temporary file.
    assert [entry.name for entry in station.iterdir()] == [path.name]
    assert _state(tmp_path, station).key == KEY
    assert setup.clear_station_license(station) is True
    assert setup.clear_station_license(station) is False
    assert _secrets(station) == {"version": 1, **_OTHER_SECRETS}
    assert _state(tmp_path, station).station is None


def test_a_secrets_json_holding_only_the_key_is_deleted_when_it_is_cleared(
    tmp_path: Path,
) -> None:
    station = tmp_path / "night"
    assert setup.save_station_license(station, KEY) is True
    assert _secrets(station) == {"version": 1, "stereo_tool": {"license_key": KEY}}
    assert setup.clear_station_license(station) is True
    assert not setup.station_secrets_path(station).exists()


@pytest.mark.skipif(sys.platform != "win32", reason="Windows access lists")
def test_a_station_secrets_json_is_private_on_windows(tmp_path: Path) -> None:
    station = tmp_path / "night"
    _write_secrets(station, _OTHER_SECRETS)
    setup.save_station_license(station, KEY)
    path = setup.station_secrets_path(station)
    listing = subprocess.run(["icacls", str(path)], capture_output=True, text=True, check=True)
    user = subprocess.run(["whoami"], capture_output=True, text=True, check=True).stdout.strip()
    entries = [line.replace(str(path), "").strip() for line in listing.stdout.splitlines()]
    entries = [line for line in entries if line and ":" in line and "processed" not in line]
    assert [line.lower() for line in entries] == [f"{user.lower()}:(f)"]


def test_an_old_station_key_file_moves_into_secrets_json_once(tmp_path: Path, caplog) -> None:
    station = tmp_path / "night"
    _write_secrets(station, _OTHER_SECRETS)
    old = setup.old_station_license_path(station)
    old.write_text(KEY + "\n", encoding="ascii")
    every, before = setup.generation(), setup.generation(station)
    with caplog.at_level(logging.DEBUG, logger="autodj.stereotool_setup"):
        for _ in range(3):
            state = _state(tmp_path, station)
            assert (state.source, state.key, state.station) == ("station", KEY, "saved")
    assert not old.exists()
    assert _secrets(station) == {
        "version": 1,
        **_OTHER_SECRETS,
        "stereo_tool": {"license_key": KEY},
    }
    messages = [record.getMessage() for record in caplog.records]
    assert len(messages) == 1
    assert "Moved the Stereo Tool licence key" in messages[0]
    assert KEY not in messages[0]
    # The same key: Stereo Tool does not restart.
    assert (setup.generation(), setup.generation(station)) == (every, before)


def test_a_key_already_in_secrets_json_wins_over_an_old_key_file(tmp_path: Path) -> None:
    station = tmp_path / "night"
    setup.save_station_license(station, "NEWER-KEY-1")
    old = setup.old_station_license_path(station)
    old.write_text(KEY, encoding="ascii")
    assert _state(tmp_path, station).key == "NEWER-KEY-1"
    assert not old.exists()
    assert _secrets(station)["stereo_tool"] == {"license_key": "NEWER-KEY-1"}


def test_saving_or_clearing_moves_or_deletes_an_old_key_file(tmp_path: Path) -> None:
    night, day = tmp_path / "night", tmp_path / "day"
    for station in (night, day):
        station.mkdir()
        setup.old_station_license_path(station).write_text(KEY, encoding="ascii")
    # Saving the same key moves the old file without restarting Stereo Tool.
    assert setup.save_station_license(night, KEY) is False
    assert not setup.old_station_license_path(night).exists()
    assert _secrets(night)["stereo_tool"] == {"license_key": KEY}
    assert setup.clear_station_license(day) is True
    assert not setup.old_station_license_path(day).exists()
    assert not setup.station_secrets_path(day).exists()


def test_an_old_key_file_without_a_key_stays_until_it_is_replaced_or_cleared(
    tmp_path: Path,
) -> None:
    night, day = tmp_path / "night", tmp_path / "day"
    for station in (night, day):
        station.mkdir()
        setup.old_station_license_path(station).write_text("two words", encoding="ascii")
        state = _state(tmp_path, station)
        assert (state.source, state.key, state.station) == ("station", None, "saved")
        assert setup.old_station_license_path(station).exists()
        assert not setup.station_secrets_path(station).exists()
    assert setup.save_station_license(night, KEY) is True
    assert not setup.old_station_license_path(night).exists()
    assert _state(tmp_path, night).key == KEY
    assert setup.clear_station_license(day) is True
    assert not setup.old_station_license_path(day).exists()
    assert _state(tmp_path, day).station is None


def test_a_damaged_secrets_json_is_reported_and_never_overwritten(tmp_path: Path, caplog) -> None:
    station = tmp_path / "night"
    station.mkdir()
    path = setup.station_secrets_path(station)
    path.write_text("{not json " + KEY, encoding="utf-8")
    state = _state(tmp_path, station)
    assert (state.source, state.key, state.station) == ("station", None, "saved")
    assert (
        state.problem
        == "This station's saved licence cannot be read (secrets.json is not valid JSON)."
    )
    with pytest.raises(ValueError) as caught:
        setup.save_station_license(station, "OTHER-KEY-1")
    assert KEY not in str(caught.value)
    with pytest.raises(ValueError):
        setup.clear_station_license(station)
    assert path.read_text(encoding="utf-8") == "{not json " + KEY
    # An old key file that cannot be moved there is still used, and kept.
    old = setup.old_station_license_path(station)
    old.write_text("OLD-KEY-1234", encoding="ascii")
    with caplog.at_level(logging.WARNING, logger="autodj.stereotool_setup"):
        for _ in range(3):
            assert _state(tmp_path, station).key == "OLD-KEY-1234"
    assert old.exists()
    messages = [record.getMessage() for record in caplog.records]
    assert len(messages) == 1
    assert "OLD-KEY-1234" not in messages[0] and KEY not in messages[0]


def test_a_station_entry_without_a_usable_key_is_reported_and_can_be_cleared(
    tmp_path: Path,
) -> None:
    station = tmp_path / "night"
    _write_secrets(station, {**_OTHER_SECRETS, "stereo_tool": {"license_key": "bad key"}})
    state = _state(tmp_path, station)
    assert (state.source, state.key, state.station) == ("station", None, "saved")
    assert state.problem == "This station's saved licence does not hold a Stereo Tool licence key."
    assert setup.clear_station_license(station) is True
    assert _secrets(station) == {"version": 1, **_OTHER_SECRETS}


def test_station_key_changes_and_other_secret_changes_never_undo_each_other(
    tmp_path: Path,
) -> None:
    from autodj.secret_files import update_private_json

    station = tmp_path / "night"
    path = _write_secrets(station, {})

    def add_hook(number: int) -> None:
        def change(document: dict) -> bool:
            document.setdefault("webhooks", []).append({"url": f"https://example.com/{number}"})
            return True

        update_private_json(path, change)

    workers = [
        threading.Thread(target=add_hook, args=(n,))
        if n % 2
        else threading.Thread(target=setup.save_station_license, args=(station, f"KEY-{n:04}"))
        for n in range(20)
    ]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join()
    document = _secrets(station)
    assert len(document["webhooks"]) == 10
    assert document["stereo_tool"]["license_key"].startswith("KEY-")


@pytest.mark.parametrize(
    ("name", "safe"),
    [
        (r"libStereoTool_1105\lib\Linux\IntelAMD\64\libStereoTool_intel64.so", True),
        ("libStereoTool_1105/lib/Windows/64/libStereoTool_64.dll", True),
        (r"libStereoTool_1105\..\..\evil.so", False),
        (r"\evil.so", False),
        (r"C:\evil.dll", False),
        ("libStereoTool_1105\\lib\\evil\0.so", False),
    ],
)
def test_a_backslash_in_a_member_name_is_a_separator(name: str, safe: bool) -> None:
    """Checked without zipfile, which converts backslashes itself only on Windows."""
    assert setup._safe_name(name) is safe
    if safe:
        assert "\\" not in setup._member_name(name)


def test_the_version_record_is_ignored_once_the_library_changes(tmp_path: Path) -> None:
    build, library = _build()
    setup.install(tmp_path, build=build, open_url=_thimeo(_plugin(build, library)))
    target = setup.managed_path(tmp_path)
    name, version, size, mtime = (target.parent / setup.VERSION_FILE).read_text().split()
    assert (name, version, size) == (target.name, "11.05", str(len(library)))
    assert mtime == str(target.stat().st_mtime_ns)
    # Replaced by hand: the record no longer describes it, so the running report counts.
    target.write_bytes(library + b"another build")
    assert setup.installed_version(target) is None
    status = setup.program_status(tmp_path, running={"version": "11.10"})
    assert status["version"] == "11.10"


def test_a_failed_version_record_still_installs_and_restarts(tmp_path: Path, monkeypatch) -> None:
    build, library = _build()

    def broken(*_args):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(setup, "_write_version", broken)
    before = setup.generation()
    installed = setup.install(tmp_path, build=build, open_url=_thimeo(_plugin(build, library)))
    assert installed.path.read_bytes() == library
    assert setup.generation() > before
    assert not (installed.path.parent / setup.VERSION_FILE).exists()


def test_a_failed_replace_leaves_no_old_version_record(tmp_path: Path, monkeypatch) -> None:
    build, library = _build()
    target = setup.managed_path(tmp_path)
    target.parent.mkdir(parents=True)
    target.write_bytes(b"old library")
    (target.parent / setup.VERSION_FILE).write_text(f"{target.name} 10.92 11 0\n")

    def refuse(*_args, **_kwargs):
        raise OSError(13, "Permission denied")

    monkeypatch.setattr(setup, "_replace", refuse)
    before = setup.generation()
    with pytest.raises(setup.DownloadError, match="Could not put Stereo Tool in place"):
        setup.install(tmp_path, build=build, open_url=_thimeo(_plugin(build, library)))
    assert target.read_bytes() == b"old library"
    assert not (target.parent / setup.VERSION_FILE).exists()
    assert setup.generation() == before
