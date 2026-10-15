"""Getting Stereo Tool and its licence key (autodj.stereotool_setup).

Nothing here downloads the real Stereo Tool: every HTTP answer is a stand-in.
"""

from __future__ import annotations

import io
import logging
import os
import stat
import subprocess
import sys
import urllib.error
from email.message import Message
from pathlib import Path
from types import SimpleNamespace

import pytest

from autodj import stereotool_setup as setup
from autodj.config import DEFAULT_STEREOTOOL_PATH

KEY = "<ABCD-1234-SECRET-KEY-5678>"

LINUX = setup.Build(
    "https://download.thimeo.com/stereo_tool_cmd_64", "Linux, 64-bit PC", "elf", frozenset({62})
)
WINDOWS = setup.Build(
    "https://download.thimeo.com/stereo_tool_cmd.exe", "Windows", "pe", frozenset({0x8664})
)


def _elf(machine: int = 62, size: int = 256) -> bytes:
    head = bytearray(b"\x7fELF" + bytes([2, 1, 1]) + bytes(11))
    head += machine.to_bytes(2, "little")
    return bytes(head) + bytes(size)


def _pe(machine: int = 0x8664) -> bytes:
    data = bytearray(512)
    data[0:2] = b"MZ"
    data[60:64] = (128).to_bytes(4, "little")
    data[128:132] = b"PE\0\0"
    data[132:134] = machine.to_bytes(2, "little")
    return bytes(data)


def _program() -> tuple[setup.Build, bytes]:
    """This platform's build and a file that passes its header check."""
    return (WINDOWS, _pe()) if os.name == "nt" else (LINUX, _elf())


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


def _runs(output: bytes = b"Stereo Tool 11.05\n", code: int = 0):
    calls: list[list[str]] = []

    def run(argv, **kwargs):
        calls.append(argv)
        assert kwargs["stdin"] is subprocess.DEVNULL
        assert kwargs["timeout"] > 0
        assert "shell" not in kwargs
        return SimpleNamespace(returncode=code, stdout=output)

    run.calls = calls  # type: ignore[attr-defined]
    return run


# ---------------------------------------------------------------------------
# Where the program is
# ---------------------------------------------------------------------------


def test_default_path_looks_in_the_index_folder_first(tmp_path: Path) -> None:
    assert setup.resolve_path(DEFAULT_STEREOTOOL_PATH, tmp_path) == DEFAULT_STEREOTOOL_PATH
    assert setup.location(DEFAULT_STEREOTOOL_PATH, tmp_path) == "default"
    managed = setup.managed_path(tmp_path)
    managed.parent.mkdir(parents=True)
    managed.write_bytes(b"x")
    assert managed == tmp_path / "tools" / "stereo-tool" / setup.executable_name()
    assert setup.resolve_path(DEFAULT_STEREOTOOL_PATH, tmp_path) == str(managed)
    assert setup.location(DEFAULT_STEREOTOOL_PATH, tmp_path) == "index"


def test_a_configured_path_is_always_kept(tmp_path: Path) -> None:
    managed = setup.managed_path(tmp_path)
    managed.parent.mkdir(parents=True)
    managed.write_bytes(b"x")
    assert setup.resolve_path("C:/Radio/Night Owl.exe", tmp_path) == "C:/Radio/Night Owl.exe"
    assert setup.location("C:/Radio/Night Owl.exe", tmp_path) == "configured"


def test_a_folder_named_like_the_program_is_not_used(tmp_path: Path) -> None:
    setup.managed_path(tmp_path).mkdir(parents=True)
    assert setup.resolve_path(DEFAULT_STEREOTOOL_PATH, tmp_path) == DEFAULT_STEREOTOOL_PATH


@pytest.mark.parametrize(
    ("system", "machine", "url"),
    [
        ("linux", "x86_64", "https://download.thimeo.com/stereo_tool_cmd_64"),
        ("linux", "aarch64", "https://download.thimeo.com/stereo_tool_pi2_64"),
        ("win32", "AMD64", "https://download.thimeo.com/stereo_tool_cmd.exe"),
        ("win32", "ARM64", "https://download.thimeo.com/stereo_tool_cmd.exe"),
        ("darwin", "arm64", None),
        ("linux", "armv7l", None),
    ],
)
def test_each_platform_gets_thimeos_own_build(system: str, machine: str, url: str | None) -> None:
    build = setup.platform_build(system, machine)
    assert (build.url if build else None) == url
    if build:
        assert setup.allowed_url(build.url)


def test_status_reports_the_version_and_never_a_path(tmp_path: Path) -> None:
    managed = setup.managed_path(tmp_path)
    managed.parent.mkdir(parents=True)
    managed.write_bytes(b"x")
    managed.chmod(0o755)
    run = _runs(b"Stereo Tool version 11.05 (64-bit)\n")
    result = setup.program_status(DEFAULT_STEREOTOOL_PATH, tmp_path, run=run)
    assert result["installed"] is True
    assert result["version"] == "11.05"
    assert result["location"] == "index"
    assert str(tmp_path) not in repr(result)
    # Remembered until the file changes.
    setup.program_status(DEFAULT_STEREOTOOL_PATH, tmp_path, run=run)
    assert len(run.calls) == 1


def test_status_of_a_missing_program(tmp_path: Path) -> None:
    run = _runs()
    result = setup.program_status(DEFAULT_STEREOTOOL_PATH, tmp_path, run=run)
    assert result["installed"] is False
    assert result["version"] is None
    assert run.calls == []


def test_version_tries_the_second_flag_and_gives_up_quietly(tmp_path: Path) -> None:
    program = tmp_path / "stereo_tool_cmd"
    program.write_bytes(b"x")
    answers = iter(
        [
            SimpleNamespace(returncode=1, stdout=b"unknown option"),
            SimpleNamespace(returncode=0, stdout=b"10.92"),
        ]
    )
    seen: list[str] = []

    def run(argv, **_kwargs):
        seen.append(argv[1])
        return next(answers)

    assert setup.read_version(program, run=run) == "10.92"
    assert seen == ["--version", "--full-version"]

    def hangs(argv, **kwargs):
        raise subprocess.TimeoutExpired(argv, kwargs["timeout"])

    assert setup.read_version(program, run=hangs) is None
    assert setup.read_version(program, run=_runs(b"no version here")) is None


# ---------------------------------------------------------------------------
# Downloading
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "url",
    [
        "http://download.thimeo.com/stereo_tool_cmd_64",
        "https://download.thimeo.com.evil.example/stereo_tool_cmd_64",
        "https://evilthimeo.com/stereo_tool_cmd_64",
        "https://example.com/stereo_tool_cmd_64",
        "https://user:pass@download.thimeo.com/x",
        "https://download.thimeo.com:8443/x",
        "ftp://download.thimeo.com/x",
    ],
)
def test_only_https_thimeo_addresses_are_allowed(url: str) -> None:
    assert not setup.allowed_url(url)


def test_thimeo_and_its_subdomains_are_allowed() -> None:
    assert setup.allowed_url("https://thimeo.com/x")
    assert setup.allowed_url("https://download.thimeo.com/stereo_tool_cmd_64")
    assert setup.allowed_url("https://cdn.download.thimeo.com:443/x")


def test_a_redirect_to_another_thimeo_host_is_followed() -> None:
    start = "https://download.thimeo.com/stereo_tool_cmd_64"
    moved = "https://files.thimeo.com/11.05/stereo_tool_cmd_64"
    open_url = _server({start: _redirect(start, moved), moved: _Response(b"program")})
    out = io.BytesIO()
    assert setup.fetch(start, out, open_url=open_url) == 7
    assert out.getvalue() == b"program"
    assert open_url.asked == [start, moved]


def test_a_relative_redirect_stays_on_the_host() -> None:
    start = "https://download.thimeo.com/stereo_tool_cmd_64"
    moved = "https://download.thimeo.com/new/stereo_tool_cmd_64"
    open_url = _server({start: _redirect(start, "/new/stereo_tool_cmd_64"), moved: _Response(b"x")})
    setup.fetch(start, io.BytesIO(), open_url=open_url)
    assert open_url.asked == [start, moved]


def test_a_redirect_to_another_host_is_refused_before_it_is_asked() -> None:
    start = "https://download.thimeo.com/stereo_tool_cmd_64"
    open_url = _server({start: _redirect(start, "https://mirror.example.net/stereo_tool_cmd_64")})
    with pytest.raises(setup.DownloadError, match=r"Refused to download from mirror\.example\.net"):
        setup.fetch(start, io.BytesIO(), open_url=open_url)
    assert open_url.asked == [start]


def test_a_redirect_to_plain_http_is_refused() -> None:
    start = "https://download.thimeo.com/stereo_tool_cmd_64"
    open_url = _server({start: _redirect(start, "http://download.thimeo.com/stereo_tool_cmd_64")})
    with pytest.raises(setup.DownloadError, match=r"only fetched from thimeo\.com over HTTPS"):
        setup.fetch(start, io.BytesIO(), open_url=open_url)


def test_endless_redirects_stop() -> None:
    start = "https://download.thimeo.com/a"
    open_url = _server({start: _redirect(start, start)})
    with pytest.raises(setup.DownloadError, match="too many times"):
        setup.fetch(start, io.BytesIO(), open_url=open_url)
    assert len(open_url.asked) == setup.MAX_REDIRECTS + 1


def test_an_oversized_download_is_refused_by_its_declared_length() -> None:
    start = "https://download.thimeo.com/stereo_tool_cmd_64"
    response = _Response(b"", length=str(setup.MAX_DOWNLOAD_BYTES + 1))
    out = io.BytesIO()
    with pytest.raises(setup.DownloadError, match="larger than 200 MB"):
        setup.fetch(start, out, open_url=_server({start: response}))
    assert out.getvalue() == b""
    assert response.closed


def test_an_oversized_download_is_refused_while_it_streams() -> None:
    start = "https://download.thimeo.com/stereo_tool_cmd_64"
    response = _Response(b"x" * 2048, length="")
    with pytest.raises(setup.DownloadError, match="larger than"):
        setup.fetch(start, io.BytesIO(), open_url=_server({start: response}), limit=1024)


def test_error_answers_and_empty_files_are_refused() -> None:
    start = "https://download.thimeo.com/stereo_tool_cmd_64"
    missing = urllib.error.HTTPError(start, 404, "Not Found", Message(), io.BytesIO(b""))
    with pytest.raises(setup.DownloadError, match="error 404"):
        setup.fetch(start, io.BytesIO(), open_url=_server({start: missing}))
    with pytest.raises(setup.DownloadError, match="empty"):
        setup.fetch(start, io.BytesIO(), open_url=_server({start: _Response(b"")}))
    offline = urllib.error.URLError("no route to host")
    with pytest.raises(setup.DownloadError, match=r"Could not reach thimeo\.com: no route to host"):
        setup.fetch(start, io.BytesIO(), open_url=_server({start: offline}))


def test_install_puts_a_checked_program_in_the_index_folder(tmp_path: Path) -> None:
    build, body = _program()
    before = setup.generation()
    run = _runs(b"11.05\n")
    installed = setup.install(
        tmp_path, build=build, open_url=_server({build.url: _Response(body)}), run=run
    )
    target = setup.managed_path(tmp_path)
    assert installed == setup.Installed(target, "11.05")
    assert target.read_bytes() == body
    assert setup.generation() > before
    # The version check ran the downloaded file before it was put in place.
    assert Path(run.calls[0][0]).parent == target.parent
    assert Path(run.calls[0][0]).name.startswith(".download-")
    assert sorted(path.name for path in target.parent.iterdir()) == [target.name]
    if os.name != "nt":
        assert stat.S_IMODE(target.stat().st_mode) == 0o755
    assert setup.resolve_path(DEFAULT_STEREOTOOL_PATH, tmp_path) == str(target)
    assert setup.installed_version(target, run=_runs(b"never asked")) == "11.05"


def test_update_replaces_the_old_program(tmp_path: Path) -> None:
    build, body = _program()
    target = setup.managed_path(tmp_path)
    target.parent.mkdir(parents=True)
    target.write_bytes(b"old")
    setup.install(
        tmp_path, build=build, open_url=_server({build.url: _Response(body)}), run=_runs()
    )
    assert target.read_bytes() == body


@pytest.mark.parametrize(
    "body",
    [b"#!/bin/sh\nrm -rf /\n", b"<html>not found</html>", "wrong-machine"],
)
def test_a_file_that_is_not_a_program_for_this_computer_is_refused(tmp_path: Path, body) -> None:
    build, _good = _program()
    if body == "wrong-machine":
        body = _elf(machine=183) if build.kind == "elf" else _pe(machine=0x1C0)
    run = _runs()
    old = setup.managed_path(tmp_path)
    old.parent.mkdir(parents=True)
    old.write_bytes(b"working program")
    with pytest.raises(setup.DownloadError, match=r"not a .* program, so it was not installed"):
        setup.install(
            tmp_path, build=build, open_url=_server({build.url: _Response(body)}), run=run
        )
    assert run.calls == []  # never run
    assert old.read_bytes() == b"working program"
    assert sorted(path.name for path in old.parent.iterdir()) == [old.name]


@pytest.mark.parametrize("run", [_runs(code=1), _runs(b"Segmentation fault")])
def test_a_program_that_fails_its_version_check_is_refused(tmp_path: Path, run) -> None:
    build, body = _program()
    with pytest.raises(setup.DownloadError, match="did not run or did not report its version"):
        setup.install(
            tmp_path, build=build, open_url=_server({build.url: _Response(body)}), run=run
        )
    folder = setup.tools_dir(tmp_path)
    assert list(folder.iterdir()) == []


def test_an_oversized_install_leaves_nothing_behind(tmp_path: Path) -> None:
    build, _body = _program()
    with pytest.raises(setup.DownloadError, match="larger than"):
        setup.install(
            tmp_path,
            build=build,
            open_url=_server({build.url: _Response(b"x" * 4096, length="")}),
            run=_runs(),
            limit=1024,
        )
    assert list(setup.tools_dir(tmp_path).iterdir()) == []


def test_no_build_and_a_second_download_are_refused(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(setup, "platform_build", lambda: None)
    with pytest.raises(setup.NoBuildError, match="no command-line Stereo Tool for this computer"):
        setup.install(tmp_path)
    build, _body = _program()
    assert setup._install_lock.acquire(blocking=False)
    try:
        with pytest.raises(setup.DownloadBusy):
            setup.install(tmp_path, build=build, open_url=_server({}), run=_runs())
    finally:
        setup._install_lock.release()


def test_leftovers_of_an_interrupted_download_are_removed(tmp_path: Path) -> None:
    build, body = _program()
    folder = setup.tools_dir(tmp_path)
    folder.mkdir(parents=True)
    (folder / ".download-0123").write_bytes(b"partial")
    (folder / ".stereo_tool_cmd.old-abc").write_bytes(b"old")
    (folder / "README.txt").write_text("kept")
    setup.install(
        tmp_path, build=build, open_url=_server({build.url: _Response(body)}), run=_runs()
    )
    assert sorted(path.name for path in folder.iterdir()) == sorted(
        ["README.txt", setup.executable_name()]
    )


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
    setup.save_license(path, KEY)
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


def _state(index: Path, station: Path, configured: str = "", **environ: str) -> setup.LicenseState:
    return setup.license_state(
        index_dir=index, state_dir=station, configured_file=configured, environ=environ
    )


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
    setup.save_license(setup.station_license_path(station), "STATION-KEY-1")
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
    key_file = tmp_path / "secret-file"
    key_file.write_text("FILE-KEY-1234\n")
    state = _state(index, station, AUTODJ_STEREOTOOL_LICENSE_FILE=str(key_file))
    assert (state.source, state.key) == ("environment", "FILE-KEY-1234")
    state = _state(index, station, AUTODJ_STEREOTOOL_LICENSE_KEY="bad key")
    assert (state.source, state.key) == ("environment", None)
    assert state.problem == "AUTODJ_STEREOTOOL_LICENSE_KEY does not hold a licence key."


def test_a_station_key_file_in_the_configuration_comes_first(tmp_path: Path) -> None:
    index, station = tmp_path, tmp_path / "night"
    setup.save_license(setup.station_license_path(station), KEY)
    key_file = tmp_path / "night.key"
    key_file.write_text("CONFIG-KEY-99\n")
    state = _state(index, station, str(key_file), AUTODJ_STEREOTOOL_LICENSE_KEY="ENV-KEY-1234")
    assert (state.source, state.key, state.station) == ("station-config", "CONFIG-KEY-99", "config")
    missing = _state(index, station, str(tmp_path / "gone.key"))
    assert (missing.source, missing.key) == ("station-config", None)
    assert "is missing" in (missing.problem or "")
    key_file.write_text("not a key at all\n")
    bad = _state(index, station, str(key_file))
    assert bad.key is None
    assert "does not hold a Stereo Tool licence key" in (bad.problem or "")


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
    key_file = tmp_path / "night.key"
    key_file.write_text("bad key with spaces " + KEY)
    with caplog.at_level(logging.WARNING, logger="autodj.stereotool_setup"):
        for _ in range(3):
            assert (
                setup.license_key(
                    index_dir=tmp_path, state_dir=tmp_path / "night", configured_file=str(key_file)
                )
                is None
            )
    messages = [record.getMessage() for record in caplog.records]
    assert len(messages) == 1
    assert KEY not in messages[0]
