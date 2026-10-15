"""Getting Stereo Tool and its licence key (autodj.stereotool_setup).

Nothing here downloads the real Stereo Tool: every HTTP answer is a stand-in.
"""

from __future__ import annotations

import io
import json
import logging
import os
import stat
import subprocess
import sys
import urllib.error
from email.message import Message
from pathlib import Path

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


def _runs(output: bytes = b"Stereo Tool 11.05\n", code: int | None = 0):
    """A stand-in runner: (argv, folder, timeout) to (exit code, output)."""
    calls: list[list[str]] = []

    def run(argv, cwd, timeout):
        calls.append(argv)
        assert cwd == str(Path(argv[0]).parent)
        assert timeout > 0
        return code, output

    run.calls = calls  # type: ignore[attr-defined]
    return run


def _will_not_run(argv, _cwd, _timeout):
    raise PermissionError(13, "Permission denied", argv[0])


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
        # Thimeo's ARM build takes 16-bit audio only; AutoDJ sends 32-bit float.
        ("linux", "aarch64", None),
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


def test_arm_gets_no_download_and_is_told_why(monkeypatch) -> None:
    note = setup.unsupported_note("linux", "aarch64")
    assert "16-bit audio only" in note
    assert "stereotool_path" in note
    assert "tools/stereo-tool" in note
    assert "no command-line Stereo Tool for this computer (darwin, arm64)" in (
        setup.unsupported_note("darwin", "arm64")
    )
    monkeypatch.setattr(setup, "platform_build", lambda: None)
    monkeypatch.setattr(setup, "unsupported_note", lambda: note)
    status = setup.program_status(DEFAULT_STEREOTOOL_PATH, Path("nowhere"))
    assert status["download"] == {"available": False, "platform": None, "note": note, "busy": False}
    with pytest.raises(setup.NoBuildError, match="16-bit audio only"):
        setup.install(Path("nowhere"))


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


def test_version_tries_each_flag_and_takes_a_version_whatever_the_exit_code(
    tmp_path: Path,
) -> None:
    program = tmp_path / "stereo_tool_cmd"
    program.write_bytes(b"x")
    answers = iter(
        [
            (1, b"unknown option --version"),
            (2, b"unknown option"),
            (1, b"Stereo Tool 10.92\nUsage: ..."),
        ]
    )
    seen: list[str] = []

    def run(argv, _cwd, _timeout):
        seen.append(argv[1])
        return next(answers)

    assert setup.read_version(program, run=run) == "10.92"
    assert seen == ["--version", "--full-version", "-h"]
    # Ran, but named no version (or was stopped at its timeout): unknown, not refused.
    assert setup.read_version(program, run=_runs(b"no version here")) is None
    assert setup.read_version(program, run=_runs(b"", code=None)) is None


def test_a_program_that_cannot_start_raises_will_not_run(tmp_path: Path) -> None:
    program = tmp_path / "stereo_tool_cmd"
    program.write_bytes(b"x")
    with pytest.raises(setup.WillNotRun):
        setup.read_version(program, run=_will_not_run)
    assert setup.installed_version(program, run=_will_not_run) is None


def _python(code: str) -> list[str]:
    return [sys.executable, "-I", "-c", code]


def test_the_version_check_gets_a_minimal_environment(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("AUTODJ_STEREOTOOL_LICENSE_KEY", KEY)
    monkeypatch.setenv("SOME_API_TOKEN", "not-for-stereo-tool")
    code, output = setup._run_bounded(
        _python("import json, os; print(json.dumps(sorted(os.environ)))"), str(tmp_path), 20
    )
    assert code == 0
    names = set(json.loads(output.decode()))
    assert "AUTODJ_STEREOTOOL_LICENSE_KEY" not in names
    assert "SOME_API_TOKEN" not in names
    assert KEY.encode() not in output
    assert names <= {"PATH", "HOME", "SYSTEMROOT", "WINDIR", "TEMP", "TMP", "USERPROFILE"} | {
        # Set by the interpreter itself on some platforms.
        "__CF_USER_TEXT_ENCODING",
        "LC_CTYPE",
    }


def test_the_version_check_reads_at_most_64_kb_and_stops_a_slow_program(tmp_path: Path) -> None:
    import time

    code, output = setup._run_bounded(
        _python(
            "import sys, time\nsys.stdout.write('x' * 1000000)\nsys.stdout.flush()\ntime.sleep(30)"
        ),
        str(tmp_path),
        20,
    )
    assert len(output) == 64 * 1024
    assert code is None
    started = time.monotonic()
    code, _output = setup._run_bounded(_python("import time; time.sleep(30)"), str(tmp_path), 0.5)
    assert code is None
    assert time.monotonic() - started < 10


def test_the_version_check_refuses_a_file_that_will_not_start(tmp_path: Path) -> None:
    with pytest.raises(OSError):
        setup._run_bounded([str(tmp_path / "missing-program")], str(tmp_path), 5)


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
    start = "https://download.thimeo.com/stereo_tool_cmd_64"
    now = [0.0]

    class Slow(_Response):
        def read(self, size: int = -1) -> bytes:
            now[0] += 400  # each chunk "takes" 400 seconds
            return super().read(4)

    with pytest.raises(setup.DownloadError, match="took longer than 10 minutes"):
        setup.fetch(
            start,
            io.BytesIO(),
            open_url=_server({start: Slow(b"x" * 64, length="")}),
            clock=lambda: now[0],
        )


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


def test_a_program_that_runs_without_naming_a_version_is_installed_as_unknown(
    tmp_path: Path,
) -> None:
    build, body = _program()
    run = _runs(b"Usage: stereo_tool_cmd [options] in out", code=1)
    installed = setup.install(
        tmp_path, build=build, open_url=_server({build.url: _Response(body)}), run=run
    )
    assert installed.version is None
    assert len(run.calls) == 3
    assert setup.managed_path(tmp_path).read_bytes() == body
    assert setup.installed_version(setup.managed_path(tmp_path), run=_runs(b"never")) is None


def test_a_program_that_cannot_start_is_refused_and_noexec_is_named(tmp_path: Path) -> None:
    build, body = _program()
    old = setup.managed_path(tmp_path)
    old.parent.mkdir(parents=True)
    old.write_bytes(b"working program")
    with pytest.raises(setup.DownloadError, match="noexec") as caught:
        setup.install(
            tmp_path, build=build, open_url=_server({build.url: _Response(body)}), run=_will_not_run
        )
    assert "could not be started (Permission denied)" in str(caught.value)
    assert old.read_bytes() == b"working program"
    assert sorted(path.name for path in old.parent.iterdir()) == [old.name]


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
    monkeypatch.undo()
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


def test_an_old_program_left_by_a_cut_short_update_is_put_back_not_deleted(tmp_path: Path) -> None:
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
# Putting the new program in place on Windows (os.replace stood in for)
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


def _setup_part(tmp_path: Path, old: bytes | None = b"old program") -> tuple[Path, Path]:
    folder = tmp_path / "tools"
    folder.mkdir()
    part = folder / ".download-1.exe"
    part.write_bytes(b"new program")
    target = folder / "stereo_tool_cmd.exe"
    if old is not None:
        target.write_bytes(old)
    return part, target


def test_a_running_program_is_moved_aside_and_replaced(tmp_path: Path, monkeypatch) -> None:
    part, target = _setup_part(tmp_path)
    calls = _replacer(monkeypatch, {(".download", "stereo_tool_cmd"): 1})
    setup._replace(part, target, windows=True, sleep=lambda _s: None)
    assert target.read_bytes() == b"new program"
    assert sorted(path.name for path in target.parent.iterdir()) == [target.name]
    assert [dest.split("-")[0] for _src, dest in calls] == [
        "stereo_tool_cmd.exe",
        ".stereo_tool_cmd.old",
        "stereo_tool_cmd.exe",
    ]


def test_the_old_program_is_put_back_when_the_new_one_cannot_take_its_place(
    tmp_path: Path, monkeypatch
) -> None:
    part, target = _setup_part(tmp_path)
    _replacer(monkeypatch, {(".download", "stereo_tool_cmd"): -1})
    with pytest.raises(PermissionError):
        setup._replace(part, target, windows=True, sleep=lambda _s: None)
    assert target.read_bytes() == b"old program"
    assert sorted(path.name for path in target.parent.iterdir()) == sorted([target.name, part.name])


def test_a_locked_download_on_a_first_install_gives_words_not_file_not_found(
    tmp_path: Path, monkeypatch
) -> None:
    part, target = _setup_part(tmp_path, old=None)
    waits: list[float] = []
    _replacer(monkeypatch, {(".download", "stereo_tool_cmd"): -1})
    with pytest.raises(setup.DownloadError, match="stayed in use"):
        setup._replace(part, target, windows=True, sleep=waits.append)
    assert len(waits) == setup._REPLACE_TRIES
    assert not target.exists()


def test_a_download_locked_for_a_moment_is_retried(tmp_path: Path, monkeypatch) -> None:
    part, target = _setup_part(tmp_path, old=None)
    _replacer(monkeypatch, {(".download", "stereo_tool_cmd"): 2})
    setup._replace(part, target, windows=True, sleep=lambda _s: None)
    assert target.read_bytes() == b"new program"


def test_elsewhere_a_refused_rename_is_not_retried(tmp_path: Path, monkeypatch) -> None:
    part, target = _setup_part(tmp_path)
    calls = _replacer(monkeypatch, {(".download", "stereo_tool_cmd"): -1})
    with pytest.raises(PermissionError):
        setup._replace(part, target, windows=False, sleep=lambda _s: None)
    assert len(calls) == 1
    assert target.read_bytes() == b"old program"


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


def _state(index: Path, station: Path, configured: str = "", **environ: str) -> setup.LicenseState:
    return setup.license_state(
        index_dir=index, state_dir=station, configured_file=configured, environ=environ
    )


def test_a_station_key_restarts_only_that_station_and_an_unchanged_key_nothing(
    tmp_path: Path,
) -> None:
    night, day = tmp_path / "night", tmp_path / "day"
    every, night_before, day_before = (
        setup.generation(),
        setup.generation(night),
        setup.generation(day),
    )
    assert setup.save_license(setup.station_license_path(night), KEY, station_dir=night) is True
    assert setup.generation(night) > night_before
    assert (setup.generation(), setup.generation(day)) == (every, day_before)
    night_now = setup.generation(night)
    assert (
        setup.save_license(setup.station_license_path(night), f" {KEY} ", station_dir=night)
        is False
    )
    assert setup.generation(night) == night_now
    # The key for every station restarts every station.
    setup.save_license(setup.install_license_path(tmp_path), KEY)
    assert setup.generation(day) > day_before
    assert setup.generation(night) > night_now
    assert setup.clear_license(setup.station_license_path(night), station_dir=night) is True
    assert setup.generation(day) == setup.generation()


def test_an_unreadable_saved_key_is_reported(tmp_path: Path) -> None:
    index, station = tmp_path, tmp_path / "night"
    station.mkdir()
    setup.save_license(setup.install_license_path(index), KEY)
    setup.station_license_path(station).write_bytes(b"\xff\xfe not text")
    state = _state(index, station)
    assert (state.source, state.key, state.station) == ("station", None, "saved")
    assert state.problem == "This station's saved licence does not hold a Stereo Tool licence key."
    setup.station_license_path(station).unlink()
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
