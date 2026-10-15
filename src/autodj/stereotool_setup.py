"""Getting Stereo Tool onto this machine and giving it a licence key.

AutoDJ may not redistribute Thimeo's Stereo Tool, so each install gets the
command-line build itself:

- :func:`install` downloads Thimeo's official build for this platform from
  ``download.thimeo.com`` over HTTPS (redirects only to thimeo.com hosts, size
  capped), checks that the file is a program for this computer and that it
  runs and reports its version, and only then moves it into
  ``<index_dir>/tools/stereo-tool/``, the writable data folder (``/index`` in
  the container).  The page never uploads a program: an upload route would
  let anyone who can change settings run any program on the server.
- :func:`resolve_path` makes the default ``[playback] stereotool_path`` look
  in that folder first, so a binary downloaded there, or dropped there by
  hand, is used with no configuration change.

The licence key is a secret.  Thimeo documents one way to give the
command-line build its key: ``-k <key>`` on its command line (see
``THREAT_MODEL.md`` for what that exposes).  AutoDJ stores the key in a file
only its own user can read (:func:`autodj.secret_files.write_private_file`),
outside backups, and never returns it to the page or writes it to a log.
Where it comes from, first match wins, for one station:

1. ``[playback] stereotool_license_file`` of that station: a key file named in
   the configuration;
2. the station's own key saved from Settings, Sound (``<state_dir>/.stereo-tool-license``);
3. ``AUTODJ_STEREOTOOL_LICENSE_KEY`` or ``AUTODJ_STEREOTOOL_LICENSE_FILE``,
   for the whole install;
4. the install's key saved from Settings, Sound (``<index_dir>/.stereo-tool-license``).
"""

from __future__ import annotations

import contextlib
import logging
import os
import platform
import re
import secrets
import shutil
import stat
import subprocess  # nosec B404 -- fixed argv, never a shell
import sys
import threading
import urllib.error
import urllib.request
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, BinaryIO, Literal
from urllib.parse import urljoin, urlsplit

from autodj.config import DEFAULT_STEREOTOOL_PATH
from autodj.secret_files import write_private_file

logger = logging.getLogger(__name__)

TERMS_URL = "https://www.thimeo.com/terms-and-conditions/"
LICENSING_URL = "https://www.thimeo.com/stereo-tool/licenses/"
LICENSE_ENV = "AUTODJ_STEREOTOOL_LICENSE_KEY"
LICENSE_FILE_ENV = "AUTODJ_STEREOTOOL_LICENSE_FILE"
LICENSE_FILE_NAME = ".stereo-tool-license"
#: Thimeo's builds are 30 to 65 MB (September 2026); anything far bigger is not one.
MAX_DOWNLOAD_BYTES = 200 * 1024 * 1024
MAX_REDIRECTS = 5
_CHUNK_BYTES = 1024 * 1024
_HTTP_TIMEOUT_SECONDS = 60.0
_VERSION_TIMEOUT_SECONDS = 15.0
# Printable ASCII without spaces: Thimeo's keys are long runs of these,
# sometimes in angle brackets.
_KEY = re.compile(r"[\x21-\x7e]{4,2048}")
_MAX_KEY_FILE_BYTES = 8192
_VERSION = re.compile(r"(?<![\d.])(\d{1,3}\.\d{1,3}(?:\.\d{1,4})?)(?![\d.])")
# Linux builds have had --version since 10.11; --full-version came in 10.30.
_VERSION_FLAGS = ("--version", "--full-version")

LicenseSource = Literal["station-config", "station", "environment", "install"]


class DownloadError(RuntimeError):
    """Stereo Tool could not be downloaded or installed; the message is for the page."""


class DownloadBusy(DownloadError):
    """Another download is already running."""


class NoBuildError(DownloadError):
    """Thimeo offers no command-line build for this computer."""


class LicenseLocked(RuntimeError):
    """The licence comes from the configuration or environment, not the page."""


# ---------------------------------------------------------------------------
# Changes the running masters must notice
# ---------------------------------------------------------------------------

_generation = 0
_generation_lock = threading.Lock()


def generation() -> int:
    """A number that changes whenever a licence or the downloaded program changes.

    Masters compare it on every mix block, so it is only an integer read.
    """
    return _generation


def _bump() -> None:
    global _generation
    with _generation_lock:
        _generation += 1


# ---------------------------------------------------------------------------
# Where the program lives
# ---------------------------------------------------------------------------


def executable_name() -> str:
    """The file name AutoDJ gives the command-line build."""
    return "stereo_tool_cmd.exe" if os.name == "nt" else "stereo_tool_cmd"


def tools_dir(index_dir: Path | str) -> Path:
    """The folder in the writable index folder that holds the program."""
    return Path(index_dir) / "tools" / "stereo-tool"


def managed_path(index_dir: Path | str) -> Path:
    """Where a downloaded (or dropped-in) program is looked for."""
    return tools_dir(index_dir) / executable_name()


def resolve_path(configured: str, index_dir: Path | str) -> str:
    """The program a station runs: its own setting, else the index folder's copy.

    A station that keeps the default ``stereotool_path`` uses
    ``<index_dir>/tools/stereo-tool/`` when the program is there, and the
    old ``./tools/stereo-tool/`` beside the working folder otherwise.
    """
    if configured != DEFAULT_STEREOTOOL_PATH:
        return configured
    managed = managed_path(index_dir)
    try:
        if managed.is_file():
            return str(managed)
    except OSError:
        pass
    return configured


def location(configured: str, index_dir: Path | str) -> Literal["index", "default", "configured"]:
    """Which program a station uses, in words the page can choose from."""
    if configured != DEFAULT_STEREOTOOL_PATH:
        return "configured"
    return "index" if resolve_path(configured, index_dir) != configured else "default"


_version_lock = threading.Lock()
_versions: dict[tuple[str, int, int], str | None] = {}


def read_version(
    path: Path,
    *,
    run: Callable[..., Any] | None = None,
    timeout: float = _VERSION_TIMEOUT_SECONDS,
) -> str | None:
    """Run the program once with a version flag; return the version, or ``None``.

    Nothing is fed to it (stdin is empty) and it is stopped after *timeout*
    seconds, so a file that is not Stereo Tool cannot hang the server.
    """
    run = run or subprocess.run
    for flag in _VERSION_FLAGS:
        try:
            result = run(  # nosec B603 -- the program the operator installed, fixed flag, no shell
                [str(path), flag],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                timeout=timeout,
                check=False,
                cwd=str(path.parent),
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except (OSError, subprocess.SubprocessError, ValueError):
            continue
        if result.returncode != 0:
            continue
        output = result.stdout or b""
        text = output[:4096].decode("utf-8", "replace") if isinstance(output, bytes) else ""
        match = _VERSION.search(text)
        if match:
            return match.group(1)
    return None


def installed_version(path: Path, *, run: Callable[..., Any] | None = None) -> str | None:
    """:func:`read_version`, remembered until the file changes."""
    try:
        info = path.stat()
    except OSError:
        return None
    if not stat.S_ISREG(info.st_mode):
        return None
    key = (str(path), info.st_size, info.st_mtime_ns)
    with _version_lock:
        if key in _versions:
            return _versions[key]
    version = read_version(path, run=run)
    with _version_lock:
        _versions[key] = version
    return version


def program_status(
    configured: str, index_dir: Path | str, *, run: Callable[..., Any] | None = None
) -> dict[str, Any]:
    """Whether a station's program is there, its version and where it is.

    Never a path: the page only needs to know which of the three it is.
    """
    resolved = resolve_path(configured, index_dir)
    found = shutil.which(resolved)
    version = installed_version(Path(found), run=run) if found else None
    build = platform_build()
    return {
        "installed": found is not None,
        "version": version,
        "location": location(configured, index_dir),
        "download": {
            "available": build is not None,
            "platform": build.label if build is not None else None,
            "busy": _install_lock.locked(),
        },
    }


# ---------------------------------------------------------------------------
# Downloading the official build
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Build:
    """Thimeo's command-line build for one platform."""

    url: str
    label: str
    # "elf" or "pe", and the machine types such a file may name.
    kind: Literal["elf", "pe"]
    machines: frozenset[int]


_ELF_X86_64 = 62
_ELF_AARCH64 = 183
_PE_AMD64 = 0x8664
_PE_I386 = 0x14C

# From https://www.thimeo.com/stereo-tool/download/ ("Command line version").
_LINUX_X86_64 = Build(
    "https://download.thimeo.com/stereo_tool_cmd_64",
    "Linux, 64-bit PC",
    "elf",
    frozenset({_ELF_X86_64}),
)
_LINUX_ARM64 = Build(
    "https://download.thimeo.com/stereo_tool_pi2_64",
    "Linux, 64-bit ARM (Thimeo's Raspberry Pi build)",
    "elf",
    frozenset({_ELF_AARCH64}),
)
# Windows on ARM runs the x64 build through its emulation.
_WINDOWS = Build(
    "https://download.thimeo.com/stereo_tool_cmd.exe",
    "Windows",
    "pe",
    frozenset({_PE_AMD64, _PE_I386}),
)


def platform_build(system: str | None = None, machine: str | None = None) -> Build | None:
    """Thimeo's command-line build for this computer, or ``None`` if there is none."""
    system = sys.platform if system is None else system
    machine = (platform.machine() if machine is None else machine).lower()
    if system.startswith("linux"):
        if machine in {"x86_64", "amd64"}:
            return _LINUX_X86_64
        if machine in {"aarch64", "arm64"}:
            return _LINUX_ARM64
    if system == "win32" and machine in {"amd64", "x86_64", "arm64"}:
        return _WINDOWS
    return None


def allowed_url(url: str) -> bool:
    """Whether *url* is HTTPS to thimeo.com or one of its subdomains, on the usual port."""
    try:
        parts = urlsplit(url)
        port = parts.port
    except ValueError:
        return False
    host = (parts.hostname or "").lower()
    return (
        parts.scheme == "https"
        and port in {None, 443}
        and parts.username is None
        and parts.password is None
        and (host == "thimeo.com" or host.endswith(".thimeo.com"))
    )


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Hand every redirect back, so each hop's address is checked first."""

    def redirect_request(
        self, _request: Any, _fp: Any, _code: int, _msg: str, _headers: Any, _newurl: str
    ) -> None:
        return None


_OPENER = urllib.request.build_opener(_NoRedirect)


def _open(url: str, timeout: float) -> Any:
    from autodj.version import current_version

    request = urllib.request.Request(url, headers={"User-Agent": f"AutoDJ/{current_version()}"})
    return _OPENER.open(request, timeout=timeout)  # nosec B310 -- allowed_url checked https first


def _host(url: str) -> str:
    try:
        return urlsplit(url).hostname or url
    except ValueError:
        return "an address that cannot be read"


def fetch(
    url: str,
    out: BinaryIO,
    *,
    open_url: Callable[[str, float], Any] | None = None,
    limit: int = MAX_DOWNLOAD_BYTES,
) -> int:
    """Copy *url* to *out*, following redirects only to thimeo.com; return the bytes written.

    Raises:
        DownloadError: An address that is not thimeo.com over HTTPS, an
            error answer, too many redirects, or a file over *limit*.
    """
    open_url = open_url or _open
    for _ in range(MAX_REDIRECTS + 1):
        if not allowed_url(url):
            raise DownloadError(
                f"Refused to download from {_host(url)}: Stereo Tool is only fetched from "
                "thimeo.com over HTTPS."
            )
        try:
            response = open_url(url, _HTTP_TIMEOUT_SECONDS)
        except urllib.error.HTTPError as exc:
            with contextlib.closing(exc):
                if exc.code in {301, 302, 303, 307, 308}:
                    target = exc.headers.get("Location") if exc.headers else None
                    if not target:
                        raise DownloadError(
                            "thimeo.com sent a redirect without an address."
                        ) from None
                    url = urljoin(url, target)
                    continue
                raise DownloadError(f"thimeo.com answered with error {exc.code}.") from None
        except (OSError, ValueError) as exc:
            reason = getattr(exc, "reason", None) or exc
            raise DownloadError(f"Could not reach thimeo.com: {reason}.") from None
        with contextlib.closing(response):
            declared = (response.headers.get("Content-Length") or "").strip()
            if declared.isdigit() and int(declared) > limit:
                raise DownloadError(_too_big(limit))
            total = 0
            while True:
                try:
                    chunk = response.read(_CHUNK_BYTES)
                except (OSError, ValueError) as exc:
                    raise DownloadError(f"The download from thimeo.com broke off: {exc}.") from None
                if not chunk:
                    break
                total += len(chunk)
                if total > limit:
                    raise DownloadError(_too_big(limit))
                out.write(chunk)
        if total == 0:
            raise DownloadError("thimeo.com sent an empty file.")
        return total
    raise DownloadError("thimeo.com redirected too many times.")


def _too_big(limit: int) -> str:
    return f"The download is larger than {limit // (1024 * 1024)} MB, so it is not Stereo Tool."


def check_program(path: Path, build: Build) -> None:
    """Refuse a file that is not a program for this computer.

    Raises:
        DownloadError: Not an ELF (Linux) or PE (Windows) program for one of
            *build*'s machine types.
    """
    with path.open("rb") as handle:
        head = handle.read(4096)
    machine: int | None = None
    if build.kind == "elf":
        # 64-bit, little-endian ELF; e_machine is at byte 18.
        if head[:4] == b"\x7fELF" and len(head) >= 20 and head[4] == 2 and head[5] == 1:
            machine = int.from_bytes(head[18:20], "little")
    elif head[:2] == b"MZ" and len(head) >= 64:
        offset = int.from_bytes(head[60:64], "little")
        if offset + 6 <= len(head) and head[offset : offset + 4] == b"PE\0\0":
            machine = int.from_bytes(head[offset + 4 : offset + 6], "little")
    if machine is None or machine not in build.machines:
        raise DownloadError(
            f"The download is not a {build.label} program, so it was not installed."
        )


@dataclass(frozen=True)
class Installed:
    """A program :func:`install` put in place."""

    path: Path
    version: str


_install_lock = threading.Lock()


def install(
    index_dir: Path | str,
    *,
    build: Build | None = None,
    open_url: Callable[[str, float], Any] | None = None,
    run: Callable[..., Any] | None = None,
    limit: int = MAX_DOWNLOAD_BYTES,
) -> Installed:
    """Download Thimeo's build for this computer and put it in the index folder.

    The file is written beside its final place under a hidden name, checked,
    run once for its version, and only then renamed over the old program,
    so a failed or partial download never replaces a working one.

    Raises:
        NoBuildError: Thimeo has no command-line build for this computer.
        DownloadBusy: Another download is running.
        DownloadError: The download or one of its checks failed.
    """
    build = build if build is not None else platform_build()
    if build is None:
        raise NoBuildError(
            f"Thimeo offers no command-line Stereo Tool for this computer "
            f"({sys.platform}, {platform.machine() or 'unknown processor'}). Put one you got "
            f"from thimeo.com in tools/stereo-tool in AutoDJ's index folder instead."
        )
    if not _install_lock.acquire(blocking=False):
        raise DownloadBusy("Stereo Tool is already downloading.")
    try:
        folder = tools_dir(index_dir)
        target = managed_path(index_dir)
        try:
            folder.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise DownloadError(f"Could not make the Stereo Tool folder: {exc.strerror}.") from None
        _remove_leftovers(folder)
        # Windows runs only a file whose name ends in .exe.
        part = folder / f".download-{secrets.token_hex(8)}{target.suffix}"
        try:
            try:
                with part.open("xb") as out:
                    fetch(build.url, out, open_url=open_url, limit=limit)
                    out.flush()
                    os.fsync(out.fileno())
            except OSError as exc:
                raise DownloadError(f"Could not save the download: {exc.strerror}.") from None
            check_program(part, build)
            part.chmod(0o755)
            version = read_version(part, run=run)
            if version is None:
                raise DownloadError(
                    "The downloaded Stereo Tool did not run or did not report its version, "
                    "so it was not installed."
                )
            try:
                _replace(part, target)
            except OSError as exc:
                raise DownloadError(
                    f"Could not put Stereo Tool in place: {exc.strerror or exc}."
                ) from None
        finally:
            with contextlib.suppress(OSError):
                part.unlink(missing_ok=True)
        info = target.stat()
        with _version_lock:
            _versions[(str(target), info.st_size, info.st_mtime_ns)] = version
        _bump()
        logger.info("Stereo Tool %s installed from %s", version, build.url)
        return Installed(target, version)
    finally:
        _install_lock.release()


def _replace(part: Path, target: Path) -> None:
    """Rename *part* over *target*, even while *target* runs on Windows."""
    try:
        os.replace(part, target)
    except PermissionError:
        if os.name != "nt":
            raise
        # Windows will not replace a running program's file, but renames it.
        aside = target.with_name(f".{target.stem}.old-{secrets.token_hex(6)}{target.suffix}")
        os.replace(target, aside)
        os.replace(part, target)
        with contextlib.suppress(OSError):
            aside.unlink()


def _remove_leftovers(folder: Path) -> None:
    """Delete parts of interrupted downloads and old programs that have stopped."""
    for pattern in (".download-*", ".stereo_tool_cmd.old-*"):
        for path in folder.glob(pattern):
            with contextlib.suppress(OSError):
                if path.is_file() and not path.is_symlink():
                    path.unlink()


# ---------------------------------------------------------------------------
# The licence key
# ---------------------------------------------------------------------------


def validate_key(value: object) -> str:
    """Return the key without surrounding spaces; the error never repeats it.

    Raises:
        ValueError: Not a plausible Stereo Tool key.
    """
    if not isinstance(value, str):
        raise ValueError("The licence key must be text.")
    key = value.strip()
    if not key:
        raise ValueError("Enter the licence key first.")
    if not _KEY.fullmatch(key):
        raise ValueError(
            "A Stereo Tool licence key is 4 to 2048 letters, digits and symbols, "
            "with no spaces or line breaks."
        )
    return key


def install_license_path(index_dir: Path | str) -> Path:
    """The key saved for every station."""
    return Path(index_dir) / LICENSE_FILE_NAME


def station_license_path(state_dir: Path | str) -> Path:
    """The key saved for one station only."""
    return Path(state_dir) / LICENSE_FILE_NAME


class _Unreadable(Exception):
    """A key file that is named but cannot be used; the message names the file, not the key."""


def _read_key(path: Path, what: str) -> str | None:
    """The key in *path*, ``None`` when there is no file.

    Raises:
        _Unreadable: The file is there but unreadable or holds no key.
    """
    try:
        with path.open("rb") as handle:
            data = handle.read(_MAX_KEY_FILE_BYTES + 1)
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise _Unreadable(
            f"{what} cannot be read ({exc.strerror or type(exc).__name__})."
        ) from None
    try:
        return validate_key(data[:_MAX_KEY_FILE_BYTES].decode("ascii"))
    except (UnicodeDecodeError, ValueError):
        raise _Unreadable(f"{what} does not hold a Stereo Tool licence key.") from None


def _saved(path: Path) -> bool:
    try:
        return _read_key(path, "saved") is not None
    except _Unreadable:
        return False


@dataclass(frozen=True)
class LicenseState:
    """Which licence a station uses; the key itself never prints."""

    source: LicenseSource | None
    key: str | None = field(default=None, repr=False)
    problem: str | None = None
    install: Literal["environment", "saved"] | None = None
    station: Literal["config", "saved"] | None = None

    def public(self) -> dict[str, Any]:
        """Everything but the key, for the page."""
        return {
            "in_use": self.source,
            "install": self.install,
            "station": self.station,
            "problem": self.problem,
        }


def license_state(
    *,
    index_dir: Path | str,
    state_dir: Path | str,
    configured_file: str = "",
    environ: Mapping[str, str] | None = None,
) -> LicenseState:
    """Find the licence key one station uses, in the order the module describes."""
    env = os.environ if environ is None else environ
    env_key = (env.get(LICENSE_ENV) or "").strip()
    env_file = (env.get(LICENSE_FILE_ENV) or "").strip()
    install: Literal["environment", "saved"] | None
    if env_key or env_file:
        install = "environment"
    else:
        install = "saved" if _saved(install_license_path(index_dir)) else None
    station: Literal["config", "saved"] | None
    if configured_file:
        station = "config"
    else:
        station = "saved" if _saved(station_license_path(state_dir)) else None

    def found(
        source: LicenseSource | None, key: str | None, problem: str | None = None
    ) -> LicenseState:
        return LicenseState(source, key, problem, install, station)

    if configured_file:
        what = "The licence file named by [playback] stereotool_license_file"
        try:
            key = _read_key(Path(configured_file), what)
        except _Unreadable as exc:
            return found("station-config", None, str(exc))
        return found("station-config", key, None if key else f"{what} is missing.")
    try:
        key = _read_key(station_license_path(state_dir), "This station's saved licence")
    except _Unreadable:
        key = None
    if key:
        return found("station", key)
    if env_key:
        try:
            return found("environment", validate_key(env_key))
        except ValueError:
            return found("environment", None, f"{LICENSE_ENV} does not hold a licence key.")
    if env_file:
        what = f"The licence file named by {LICENSE_FILE_ENV}"
        try:
            key = _read_key(Path(env_file), what)
        except _Unreadable as exc:
            return found("environment", None, str(exc))
        return found("environment", key, None if key else f"{what} is missing.")
    try:
        key = _read_key(install_license_path(index_dir), "The saved licence")
    except _Unreadable:
        key = None
    if key:
        return found("install", key)
    return found(None, None)


def license_key(
    *, index_dir: Path | str, state_dir: Path | str, configured_file: str = ""
) -> str | None:
    """The key to give Stereo Tool when it starts, or ``None`` to run unlicensed."""
    state = license_state(index_dir=index_dir, state_dir=state_dir, configured_file=configured_file)
    # Said once per problem: Stereo Tool may be retried every half second.
    with _warned_lock:
        repeat = _warned.get(str(state_dir)) == state.problem
        _warned[str(state_dir)] = state.problem
    if state.problem and not repeat:
        logger.warning("Stereo Tool runs without a licence: %s", state.problem)
    return state.key


_warned_lock = threading.Lock()
_warned: dict[str, str | None] = {}


def save_license(path: Path, key: str) -> None:
    """Save *key* readable only by AutoDJ's user, and restart Stereo Tool with it."""
    write_private_file(path, validate_key(key) + "\n")
    _bump()


def clear_license(path: Path) -> bool:
    """Delete a saved key; return whether there was one."""
    try:
        path.unlink()
    except FileNotFoundError:
        return False
    _bump()
    return True
