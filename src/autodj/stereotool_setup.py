"""Getting Thimeo's Stereo Tool plugin library and giving it a licence key.

AutoDJ may not redistribute Stereo Tool, so each install gets it itself.
AutoDJ uses Thimeo's plugin library (``libStereoTool``), as AzuraCast and
Liquidsoap do, run in a child process of its own
(:mod:`autodj.stereotool_host`):

- :func:`install` downloads Thimeo's Generic plugin zip from
  ``download.thimeo.com`` over HTTPS (redirects only to thimeo.com hosts,
  size capped and time-limited), takes out only the one library for this
  computer (no other member is written, and names that could leave the
  folder or links are refused), checks that it is a shared library for
  this processor, and only then moves it into ``<index_dir>/tools/stereo-tool/``,
  the writable data folder (``/index`` in the container), with its version
  from the zip's folder name.  The page never uploads a file: an upload
  route would let anyone who can change settings run any code on the server.
- Every station uses the library in that folder (:func:`library_path`),
  downloaded there or put there by hand.  There is no setting for it.

The licence key is a secret.  It is handed to the library's
``stereoTool_Create3`` inside the host process, over that process's stdin,
never on a command line or in an environment.  AutoDJ stores the key in a
file only its own user can read (:func:`autodj.secret_files.write_private_file`),
outside backups, and never returns it to the page or writes it to a log.
Where it comes from, first match wins, for one station:

1. the station's own key saved from Settings, Sound, in the station's
   ``secrets.json`` (``<state_dir>/secrets.json``, with its other secrets);
2. ``AUTODJ_STEREOTOOL_LICENSE_KEY``, for the whole install;
3. the install's key saved from Settings, Sound (``<index_dir>/.stereo-tool-license``).

A station's own key used to be kept in ``<state_dir>/.stereo-tool-license``.  The
first read after an upgrade moves it into ``secrets.json`` and deletes that file.
"""

from __future__ import annotations

import contextlib
import logging
import os
import platform
import re
import secrets
import stat
import sys
import threading
import time
import urllib.error
import urllib.request
import zipfile
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any, BinaryIO, Literal
from urllib.parse import urljoin, urlsplit

from autodj.config import stereotool_library_name
from autodj.secret_files import (
    private_json_lock,
    read_private_json,
    update_private_json,
    write_private_file,
)

logger = logging.getLogger(__name__)

TERMS_URL = "https://www.thimeo.com/terms-and-conditions/"
LICENSING_URL = "https://www.thimeo.com/stereo-tool/licenses/"
#: Thimeo's "Plugin SDK with binaries for all OS's" (https://www.thimeo.com/stereo-tool/download/).
PLUGIN_URL = "https://download.thimeo.com/Stereo_Tool_Generic_plugin.zip"
LICENSE_ENV = "AUTODJ_STEREOTOOL_LICENSE_KEY"
LICENSE_FILE_NAME = ".stereo-tool-license"
#: A station's private secrets file (#394); its own key is kept under SECRETS_SECTION.
SECRETS_FILE_NAME = "secrets.json"
SECRETS_SECTION = "stereo_tool"
#: The 11.05 zip is 318 MB (October 2026); anything far bigger is not it.
MAX_DOWNLOAD_BYTES = 768 * 1024 * 1024
#: The one library taken out of it; Thimeo's are 33 to 117 MB.
MAX_LIBRARY_BYTES = 256 * 1024 * 1024
MAX_REDIRECTS = 5
_CHUNK_BYTES = 1024 * 1024
_HTTP_TIMEOUT_SECONDS = 60.0
#: The whole download, redirects included, must end within this.
DOWNLOAD_DEADLINE_SECONDS = 1800.0
_REPLACE_TRIES = 5
VERSION_FILE = ".version"
# Printable ASCII without spaces: Thimeo's keys are long runs of these,
# in angle brackets.
_KEY = re.compile(r"[\x21-\x7e]{4,2048}")
_MAX_KEY_FILE_BYTES = 8192
# The zip's top folder: libStereoTool_1105, or with BETA and a number.
_TOP = re.compile(r"libStereoTool_(\d{3,4})(?:BETA\w{0,8})?")

LicenseSource = Literal["station", "environment", "install"]


class DownloadError(RuntimeError):
    """Stereo Tool could not be downloaded or installed; the message is for the page."""


class DownloadBusy(DownloadError):
    """Another download is already running."""


class NoBuildError(DownloadError):
    """Thimeo's plugin has no library AutoDJ can use on this computer."""


# ---------------------------------------------------------------------------
# Changes the running masters must notice
# ---------------------------------------------------------------------------

_INSTALL_SCOPE = ""
_generations: dict[str, int] = {}
_generation_lock = threading.Lock()


def _scope(state_dir: Path | str | None) -> str:
    return _INSTALL_SCOPE if state_dir is None else str(Path(state_dir))


def generation(state_dir: Path | str | None = None) -> int:
    """A number that grows whenever what Stereo Tool starts with changes for a station.

    It counts changes for the whole install (the library, the key for every
    station) plus changes to the key of the station whose folder is
    *state_dir*, so a key saved for one station restarts only that one.
    Masters compare it on every mix block, so it is two dictionary reads.
    """
    every = _generations.get(_INSTALL_SCOPE, 0)
    return every if state_dir is None else every + _generations.get(_scope(state_dir), 0)


def _bump(state_dir: Path | str | None = None) -> None:
    key = _scope(state_dir)
    with _generation_lock:
        _generations[key] = _generations.get(key, 0) + 1


# ---------------------------------------------------------------------------
# Where the library lives
# ---------------------------------------------------------------------------


def tools_dir(index_dir: Path | str) -> Path:
    """The folder in the writable index folder that holds the library."""
    return Path(index_dir) / "tools" / "stereo-tool"


def managed_path(index_dir: Path | str) -> Path:
    """Where a downloaded (or hand-placed) library is looked for."""
    return tools_dir(index_dir) / stereotool_library_name()


def library_path(index_dir: Path | str) -> str:
    """The library every station uses."""
    return str(managed_path(index_dir))


def library_present(path: str) -> bool:
    """Whether a library file is there to start Stereo Tool with."""
    try:
        return os.path.isfile(path)
    except OSError:
        return False


def installed_version(library: Path) -> str | None:
    """The version recorded beside a downloaded library, if it is still that file.

    The record names the library, its size and its modification time; a
    library replaced by hand since does not match, and its version then
    comes from what the running Stereo Tool reports.
    """
    try:
        text = (library.parent / VERSION_FILE).read_text(encoding="ascii").strip()
        info = library.stat()
    except (OSError, UnicodeDecodeError):
        return None
    parts = text.split(" ")
    if len(parts) != 4 or not re.fullmatch(r"\d{1,2}\.\d{2,3}", parts[1]):
        return None
    name, version, size, mtime = parts
    if name != library.name or size != str(info.st_size) or mtime != str(info.st_mtime_ns):
        return None
    return version


def program_status(
    index_dir: Path | str, *, running: Mapping[str, object] | None = None
) -> dict[str, Any]:
    """Whether the library is there and its version; never a path.

    *running* is what the station's running Stereo Tool reported, whose
    version counts for a library put there by hand.
    """
    resolved = managed_path(index_dir)
    installed = library_present(str(resolved))
    version = installed_version(resolved) if installed else None
    reported = (running or {}).get("version")
    if installed and version is None and isinstance(reported, str):
        version = reported
    build = platform_build()
    return {
        "installed": installed,
        "version": version,
        "download": {
            "available": build is not None,
            "platform": build.label if build is not None else None,
            # Why there is nothing to download, in words for the page.
            "note": None if build is not None else unsupported_note(),
            "busy": _install_lock.locked(),
        },
    }


# ---------------------------------------------------------------------------
# Downloading Thimeo's plugin
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Build:
    """The library in Thimeo's plugin zip for one platform."""

    #: Its path in the zip below ``libStereoTool_<version>/lib/``.
    member: str
    label: str
    # "elf" or "pe", and the machine type such a file must name.
    kind: Literal["elf", "pe"]
    machine: int


_ELF_X86_64 = 62
_ELF_AARCH64 = 183
_PE_AMD64 = 0x8664

# The zip's Linux builds without X11: AutoDJ shows no Stereo Tool window,
# and a slim container has no X11 libraries to load.
_LINUX_X86_64 = Build(
    "Linux/IntelAMD/64/libStereoTool_intel64.so", "Linux, 64-bit PC", "elf", _ELF_X86_64
)
_LINUX_ARM64 = Build(
    "Linux/ARM/64/libStereoTool_noX11_arm64.so", "Linux, 64-bit ARM", "elf", _ELF_AARCH64
)
_WINDOWS_X64 = Build("Windows/64/libStereoTool_64.dll", "Windows, 64-bit", "pe", _PE_AMD64)


def platform_build(system: str | None = None, machine: str | None = None) -> Build | None:
    """The library for this computer, or ``None`` if AutoDJ downloads none here.

    The Python running AutoDJ must be 64-bit, like the library it loads.
    """
    system = sys.platform if system is None else system
    machine = (platform.machine() if machine is None else machine).lower()
    if sys.maxsize <= 2**32:
        return None
    if system.startswith("linux"):
        if machine in {"x86_64", "amd64"}:
            return _LINUX_X86_64
        if machine in {"aarch64", "arm64"}:
            return _LINUX_ARM64
    if system == "win32" and machine in {"amd64", "x86_64"}:
        return _WINDOWS_X64
    return None


def unsupported_note(system: str | None = None, machine: str | None = None) -> str:
    """Why this computer gets no download, and how to use Stereo Tool anyway."""
    system = sys.platform if system is None else system
    machine = (platform.machine() if machine is None else machine).lower()
    return (
        f"AutoDJ downloads Thimeo's Stereo Tool plugin library for 64-bit Linux (PC or ARM) "
        f"and 64-bit Windows, and this computer is {system}, {machine or 'unknown processor'}. "
        "To use another build of the library, put it in tools/stereo-tool in AutoDJ's index "
        "folder."
    )


def allowed_url(url: str) -> bool:
    """Whether *url* is HTTPS to thimeo.com or one of its subdomains, on the usual port.

    Whitespace, control characters or non-ASCII anywhere, or ``%`` or a
    backslash in the address part, refuse it outright, so no parser can
    read another host there.
    """
    if any(ch.isspace() or ord(ch) < 32 or ord(ch) > 126 for ch in url):
        return False
    try:
        parts = urlsplit(url)
        port = parts.port
    except ValueError:
        return False
    if "%" in parts.netloc or "\\" in parts.netloc:
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
    deadline_seconds: float = DOWNLOAD_DEADLINE_SECONDS,
    clock: Callable[[], float] = time.monotonic,
) -> int:
    """Copy *url* to *out*, following redirects only to thimeo.com; return the bytes written.

    Raises:
        DownloadError: An address that is not thimeo.com over HTTPS, an
            error answer, too many redirects, a file over *limit*, or a
            download still running after *deadline_seconds*.
    """
    open_url = open_url or _open
    deadline = clock() + deadline_seconds

    def check_deadline() -> None:
        if clock() > deadline:
            raise DownloadError(
                f"The download from thimeo.com took longer than {deadline_seconds / 60:g} "
                "minutes, so it was stopped."
            )

    for _ in range(MAX_REDIRECTS + 1):
        check_deadline()
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
                check_deadline()
        if total == 0:
            raise DownloadError("thimeo.com sent an empty file.")
        return total
    raise DownloadError("thimeo.com redirected too many times.")


def _too_big(limit: int) -> str:
    return f"The download is larger than {limit // (1024 * 1024)} MB, so it is not Stereo Tool."


def check_library(path: Path, build: Build) -> None:
    """Refuse a file that is not a shared library for this computer.

    Raises:
        DownloadError: Not a 64-bit little-endian ELF shared object for the
            build's machine (Linux), or not a PE DLL for x64 (Windows).
    """
    with path.open("rb") as handle:
        head = handle.read(4096)
    ok = False
    if build.kind == "elf":
        # e_type 3 is a shared object; e_machine is at byte 18.
        ok = (
            head[:4] == b"\x7fELF"
            and len(head) >= 20
            and head[4] == 2
            and head[5] == 1
            and int.from_bytes(head[16:18], "little") == 3
            and int.from_bytes(head[18:20], "little") == build.machine
        )
    elif head[:2] == b"MZ" and len(head) >= 64:
        offset = int.from_bytes(head[60:64], "little")
        if offset + 24 <= len(head) and head[offset : offset + 4] == b"PE\0\0":
            machine = int.from_bytes(head[offset + 4 : offset + 6], "little")
            characteristics = int.from_bytes(head[offset + 22 : offset + 24], "little")
            # 0x2000: IMAGE_FILE_DLL.
            ok = machine == build.machine and bool(characteristics & 0x2000)
    if not ok:
        raise DownloadError(
            f"The Stereo Tool library in Thimeo's download is not a {build.label} library, "
            "so it was not installed."
        )


def plugin_version(top: str) -> str | None:
    """``11.05`` from the zip's top folder ``libStereoTool_1105``."""
    match = _TOP.fullmatch(top)
    if match is None:
        return None
    digits = match.group(1)
    return f"{int(digits[:-2])}.{digits[-2:]}"


def _member_name(name: str) -> str:
    """A zip member name with ``/`` separators.

    Thimeo's zip names its members with backslashes
    (``libStereoTool_1105\\lib\\Linux\\...``), which Python's zipfile keeps
    as they are except on Windows, so a backslash counts as a separator.
    """
    return name.replace("\\", "/")


def _safe_name(name: str) -> bool:
    """Whether a zip member name stays inside the folder it is unpacked in."""
    name = _member_name(name)
    if not name or "\0" in name or name.startswith("/") or ":" in name:
        return False
    return ".." not in PurePosixPath(name).parts


def extract_library(archive: Path, build: Build, out: BinaryIO) -> str | None:
    """Copy this computer's library from Thimeo's zip to *out*; return its version.

    Only the one member is read.  Every name in the zip must be safe to
    unpack and the member must not be a link, or the zip is refused.

    Raises:
        DownloadError: Not a zip, an unsafe name, no or several matching
            libraries, a link, a library over the size cap, or a damaged member.
    """
    try:
        opened = zipfile.ZipFile(archive)
    except (zipfile.BadZipFile, OSError) as exc:
        raise DownloadError(f"Thimeo's download is not a readable zip file ({exc}).") from None
    with opened:
        found = []
        for info in opened.infolist():
            if not _safe_name(info.filename):
                raise DownloadError(
                    "Thimeo's download names a file outside its own folder, so nothing was "
                    "installed."
                )
            # Only the library straight in the build's folder: never the
            # copy in a Kantar subfolder (Thimeo's watermarking build).
            top, _, rest = _member_name(info.filename).partition("/")
            if rest == f"lib/{build.member}" and _TOP.fullmatch(top):
                found.append((top, info))
        if len(found) != 1:
            raise DownloadError(
                f"Thimeo's download has {'no' if not found else 'more than one'} "
                f"{build.label} Stereo Tool library, so nothing was installed."
            )
        top, info = found[0]
        if stat.S_ISLNK(info.external_attr >> 16) or info.is_dir():
            raise DownloadError("The Stereo Tool library in Thimeo's download is a link.")
        if info.file_size > MAX_LIBRARY_BYTES:
            raise DownloadError("The Stereo Tool library in Thimeo's download is too large.")
        copied = 0
        try:
            with opened.open(info) as member:
                while chunk := member.read(_CHUNK_BYTES):
                    copied += len(chunk)
                    if copied > MAX_LIBRARY_BYTES:
                        raise DownloadError(
                            "The Stereo Tool library in Thimeo's download is too large."
                        )
                    out.write(chunk)
        except (zipfile.BadZipFile, OSError, EOFError, ValueError) as exc:
            # A bad CRC is a BadZipFile; a damaged stream a zlib error (ValueError kin).
            raise DownloadError(f"Thimeo's download is damaged ({exc}).") from None
        return plugin_version(top)


@dataclass(frozen=True)
class Installed:
    """A library :func:`install` put in place."""

    path: Path
    #: ``None`` when the zip's folder name gives none.
    version: str | None


_install_lock = threading.Lock()


def install(
    index_dir: Path | str,
    *,
    build: Build | None = None,
    open_url: Callable[[str, float], Any] | None = None,
    limit: int = MAX_DOWNLOAD_BYTES,
    clock: Callable[[], float] = time.monotonic,
) -> Installed:
    """Download Thimeo's plugin zip and put this computer's library in the index folder.

    The zip is saved beside the library under a hidden name, the one
    library is taken out to another hidden name and checked, and only then
    renamed over the old one, so a failed or partial download never
    replaces a working library.  The zip is deleted afterwards.  Nothing
    downloaded is run or loaded here.

    Raises:
        NoBuildError: AutoDJ downloads no library for this computer.
        DownloadBusy: Another download is running.
        DownloadError: The download or one of its checks failed.
    """
    build = build if build is not None else platform_build()
    if build is None:
        raise NoBuildError(unsupported_note())
    if not _install_lock.acquire(blocking=False):
        raise DownloadBusy("Stereo Tool is already downloading.")
    try:
        folder = tools_dir(index_dir)
        target = managed_path(index_dir)
        try:
            folder.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise DownloadError(f"Could not make the Stereo Tool folder: {exc.strerror}.") from None
        _remove_leftovers(folder, target)
        token = secrets.token_hex(8)
        archive = folder / f".download-{token}.zip"
        part = folder / f".download-{token}{target.suffix}"
        try:
            try:
                with archive.open("xb") as out:
                    fetch(PLUGIN_URL, out, open_url=open_url, limit=limit, clock=clock)
                    out.flush()
                with part.open("xb") as out:
                    version = extract_library(archive, build, out)
                    out.flush()
                    os.fsync(out.fileno())
            except OSError as exc:
                raise DownloadError(
                    f"Could not save the download: {exc.strerror or exc}. Stereo Tool needs "
                    "about 400 MB free in AutoDJ's index folder while it downloads."
                ) from None
            check_library(part, build)
            record = folder / VERSION_FILE
            try:
                # Gone first, so a cut-short update leaves no record that
                # names the old version beside the new library.
                record.unlink(missing_ok=True)
                _replace(part, target)
            except OSError as exc:
                raise DownloadError(
                    f"Could not put Stereo Tool in place: {exc.strerror or exc}."
                ) from None
            # In place: running Stereo Tools restart with it, whatever follows.
            _bump()
            try:
                _write_version(folder, target, version)
            except OSError as exc:
                # The library works; only the page's version line is missing
                # until Stereo Tool reports its own.
                logger.warning(
                    "Stereo Tool was installed, but its version could not be recorded: %s",
                    exc.strerror or exc,
                )
        finally:
            for leftover in (archive, part):
                with contextlib.suppress(OSError):
                    leftover.unlink(missing_ok=True)
        logger.info(
            "Stereo Tool %s plugin library installed from %s",
            version or "(version unknown)",
            PLUGIN_URL,
        )
        return Installed(target, version)
    finally:
        _install_lock.release()


def _write_version(folder: Path, target: Path, version: str | None) -> None:
    """Record which library this version is for, beside it."""
    record = folder / VERSION_FILE
    if version is None:
        record.unlink(missing_ok=True)
        return
    info = target.stat()
    temporary = folder / f".{VERSION_FILE}.{secrets.token_hex(6)}"
    temporary.write_text(
        f"{target.name} {version} {info.st_size} {info.st_mtime_ns}\n", encoding="ascii"
    )
    os.replace(temporary, record)


def _replace(
    part: Path,
    target: Path,
    *,
    windows: bool = os.name == "nt",
    sleep: Callable[[float], None] = time.sleep,
) -> None:
    """Rename *part* over *target*, even while *target* is loaded on Windows.

    Windows will not replace a loaded library's file but will rename it,
    so the old library is moved aside first; if the new one then cannot
    take its place, the old one is put back.  A *part* that stays locked
    (a virus scanner reading the new file) is retried a few times.

    Raises:
        DownloadError: The new file stayed in use.
        OSError: The rename failed; *target* is as it was.
    """
    for attempt in range(_REPLACE_TRIES):
        try:
            os.replace(part, target)
            return
        except PermissionError:
            if not windows:
                raise
            if target.exists():
                break  # most likely the old library, loaded by Stereo Tool
            sleep(0.2 * (attempt + 1))
    else:
        raise DownloadError(
            "Could not put Stereo Tool in place: the downloaded file stayed in use, perhaps "
            "while a virus scanner checked it. Try again in a minute."
        )
    aside = target.with_name(f".{target.stem}.old-{secrets.token_hex(6)}{target.suffix}")
    os.replace(target, aside)
    try:
        for attempt in range(_REPLACE_TRIES):
            try:
                os.replace(part, target)
                break
            except PermissionError:
                if attempt == _REPLACE_TRIES - 1:
                    raise
                sleep(0.2 * (attempt + 1))
    except OSError:
        try:
            os.replace(aside, target)
        except OSError:
            # _remove_leftovers puts it back before the next download.
            logger.exception("The old Stereo Tool was left as %s", aside.name)
        raise
    with contextlib.suppress(OSError):
        aside.unlink()


def _remove_leftovers(folder: Path, target: Path) -> None:
    """Delete parts of interrupted downloads and old libraries no longer loaded.

    An old library set aside by an update that was cut short is the only
    working copy when *target* is missing: the newest one is put back
    instead of deleted.
    """
    olds = []
    for path in folder.glob(f".{target.stem}.old-*"):
        with contextlib.suppress(OSError):
            if path.is_file() and not path.is_symlink():
                olds.append((path.stat().st_mtime_ns, path))
    olds.sort()
    if olds and not target.exists() and not target.is_symlink():
        _mtime, newest = olds.pop()
        try:
            os.replace(newest, target)
        except OSError:
            logger.warning("Could not put back the old Stereo Tool %s", newest.name)
    stray = [*folder.glob(".download-*"), *folder.glob(f".{VERSION_FILE}.*")]
    for path in [*stray, *(path for _mtime, path in olds)]:
        with contextlib.suppress(OSError):
            if path.is_file() and not path.is_symlink():
                path.unlink()


# ---------------------------------------------------------------------------
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


def station_secrets_path(state_dir: Path | str) -> Path:
    """The station's ``secrets.json``, which holds the key saved for that station only."""
    return Path(state_dir) / SECRETS_FILE_NAME


def old_station_license_path(state_dir: Path | str) -> Path:
    """Where a station's own key was kept before it moved into ``secrets.json``."""
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
    """Whether a saved key file is there, readable or not (so Clear can remove it)."""
    try:
        return path.is_file()
    except OSError:
        return False


def _set_station_key(key: str | None) -> Callable[[dict[str, Any]], bool]:
    """A change to a station's secrets that sets its key, or removes it when *key* is ``None``."""

    def change(document: dict[str, Any]) -> bool:
        if key is None:
            if SECRETS_SECTION not in document:
                return False
            del document[SECRETS_SECTION]
            return True
        wanted = {"license_key": key}
        if document.get(SECRETS_SECTION) == wanted:
            return False
        document[SECRETS_SECTION] = wanted
        return True

    return change


def _move_old_station_key(state_dir: Path | str) -> None:
    """Move a station's key from its old ``.stereo-tool-license`` into its ``secrets.json``.

    Done once, on the first read after an upgrade.  A key already in
    ``secrets.json`` wins, and the old file is only deleted.  An old file
    that holds no key, or a ``secrets.json`` that cannot be changed, leaves
    the old file where it is: :func:`license_state` reports it and Clear
    deletes it.  The caller holds :data:`private_json_lock`.
    """
    old = old_station_license_path(state_dir)
    if not _saved(old):
        return
    try:
        key = _read_key(old, "saved")
    except _Unreadable:
        return
    if key is None:
        return
    path = station_secrets_path(state_dir)

    def add(document: dict[str, Any]) -> bool:
        return SECRETS_SECTION not in document and _set_station_key(key)(document)

    try:
        moved = update_private_json(path, add)
        old.unlink(missing_ok=True)
    except (OSError, ValueError) as exc:
        reason = exc.strerror if isinstance(exc, OSError) and exc.strerror else str(exc)
        with _warned_lock:
            repeat = _move_warned.get(str(old)) == reason
            _move_warned[str(old)] = reason
        if not repeat:
            # Names the files, never the key.
            logger.warning(
                "Could not move the Stereo Tool licence key in %s into %s: %s",
                old,
                path.name,
                reason,
            )
        return
    if moved:
        logger.info("Moved the Stereo Tool licence key in %s into %s.", old, path)
    else:
        logger.info("Deleted %s: this station's own key is already in %s.", old, path)


def _read_station_key(state_dir: Path | str, what: str) -> tuple[bool, str | None]:
    """Whether the station has a key of its own saved, and that key.

    Raises:
        _Unreadable: A saved key that cannot be used; the message names it, never the key.
    """
    with private_json_lock:
        _move_old_station_key(state_dir)
        old = old_station_license_path(state_dir)
        if _saved(old):
            # Not moved: it holds no key, or secrets.json could not be changed.
            return True, _read_key(old, what)
        path = station_secrets_path(state_dir)
        try:
            document = read_private_json(path)
        except OSError as exc:
            raise _Unreadable(
                f"{what} cannot be read ({exc.strerror or type(exc).__name__})."
            ) from None
        except ValueError as exc:
            raise _Unreadable(f"{what} cannot be read ({exc}).") from None
    if SECRETS_SECTION not in document:
        return False, None
    section = document[SECRETS_SECTION]
    value = section.get("license_key") if isinstance(section, dict) else None
    try:
        return True, validate_key(value)
    except ValueError:
        raise _Unreadable(f"{what} does not hold a Stereo Tool licence key.") from None


@dataclass(frozen=True)
class LicenseState:
    """Which licence a station uses; the key itself never prints."""

    source: LicenseSource | None
    key: str | None = field(default=None, repr=False)
    problem: str | None = None
    install: Literal["environment", "saved"] | None = None
    station: Literal["saved"] | None = None

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
    environ: Mapping[str, str] | None = None,
) -> LicenseState:
    """Find the licence key one station uses, in the order the module describes."""
    env = os.environ if environ is None else environ
    env_key = (env.get(LICENSE_ENV) or "").strip()
    install: Literal["environment", "saved"] | None
    if env_key:
        install = "environment"
    else:
        install = "saved" if _saved(install_license_path(index_dir)) else None
    problem: str | None = None
    try:
        saved, key = _read_station_key(state_dir, "This station's saved licence")
    except _Unreadable as exc:
        saved, key, problem = True, None, str(exc)
    station: Literal["saved"] | None = "saved" if saved else None

    def found(
        source: LicenseSource | None, key: str | None, problem: str | None = None
    ) -> LicenseState:
        return LicenseState(source, key, problem, install, station)

    if problem:
        return found("station", None, problem)
    if key:
        return found("station", key)
    if env_key:
        try:
            return found("environment", validate_key(env_key))
        except ValueError:
            return found("environment", None, f"{LICENSE_ENV} does not hold a licence key.")
    try:
        key = _read_key(install_license_path(index_dir), "The licence saved for every station")
    except _Unreadable as exc:
        return found("install", None, str(exc))
    if key:
        return found("install", key)
    return found(None, None)


def license_key(*, index_dir: Path | str, state_dir: Path | str) -> str | None:
    """The key to give Stereo Tool when it starts, or ``None`` to run unlicensed."""
    state = license_state(index_dir=index_dir, state_dir=state_dir)
    # Said once per problem: Stereo Tool may be retried every half second.
    with _warned_lock:
        repeat = _warned.get(str(state_dir)) == state.problem
        _warned[str(state_dir)] = state.problem
    if state.problem and not repeat:
        logger.warning("Stereo Tool runs without a licence: %s", state.problem)
    return state.key


_warned_lock = threading.Lock()
_warned: dict[str, str | None] = {}
_move_warned: dict[str, str] = {}


def save_license(path: Path, key: str) -> bool:
    """Save the key for every station, readable only by AutoDJ's user; return whether it changed.

    Every station restarts Stereo Tool, and only when the key is new.
    """
    key = validate_key(key)
    try:
        unchanged = _read_key(path, "saved") == key
    except _Unreadable:
        unchanged = False
    write_private_file(path, key + "\n")
    if unchanged:
        return False
    _bump()
    return True


def clear_license(path: Path) -> bool:
    """Delete the key saved for every station; return whether there was one."""
    try:
        path.unlink()
    except FileNotFoundError:
        return False
    _bump()
    return True


def save_station_license(state_dir: Path | str, key: str) -> bool:
    """Save *key* for one station in its ``secrets.json``; return whether it changed.

    The station's other secrets are kept.  Only that station restarts
    Stereo Tool, and only when the key is new.

    Raises:
        OSError: ``secrets.json`` cannot be read or written.
        ValueError: ``secrets.json`` is damaged; it is left as it is.
    """
    key = validate_key(key)
    with private_json_lock:
        _move_old_station_key(state_dir)
        changed = update_private_json(station_secrets_path(state_dir), _set_station_key(key))
        old = old_station_license_path(state_dir)
        # Still there only when it held no key worth moving.
        if _saved(old):
            old.unlink()
            changed = True
    if changed:
        _bump(state_dir)
    return changed


def clear_station_license(state_dir: Path | str) -> bool:
    """Delete one station's own key, keeping its other secrets; return whether there was one.

    Raises:
        OSError: ``secrets.json`` cannot be read or written.
        ValueError: ``secrets.json`` is damaged; it is left as it is.
    """
    with private_json_lock:
        cleared = update_private_json(station_secrets_path(state_dir), _set_station_key(None))
        old = old_station_license_path(state_dir)
        if _saved(old):
            old.unlink()
            cleared = True
    if cleared:
        _bump(state_dir)
    return cleared
