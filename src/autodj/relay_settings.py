"""Relays added and changed in Settings, Stream, and their passwords (#185).

Each station keeps two files in its state folder (:class:`~autodj.stations.StationPaths`):

- ``relays.json``: the relays added or changed on the web page, by name,
  with every setting except the password.  It sits beside
  ``web_state.json`` and goes into backups like it.
- ``relay-passwords.json``: the passwords typed on the page, readable by
  the owner only (``0600``), like the other secrets AutoDJ saves.  It is
  never sent to a browser, logged or backed up.

Where a relay's settings come from, for each name:

- The settings: ``relays.json`` when the page saved that relay, otherwise
  its ``[stream.relays.<name>]`` table from ``config.toml`` and
  ``config.local.toml``.  A relay the page saved replaces the TOML
  table of the same name as a whole; Revert goes back to the TOML table.
- The password: the environment variable
  (``AUTODJ_STREAM_RELAY_<NAME>_PASSWORD``, or
  ``AUTODJ_STATION_<SLUG>_RELAY_<NAME>_PASSWORD`` for another station)
  when it is set, then a password saved from the page, then the TOML
  files.  The page refuses a password while the variable is set, since
  saving it would change nothing.

Nothing is migrated: the TOML files are never written, and a relay
defined there stays defined there.  A change is checked, then saved, and
only then put on the air through :class:`~autodj.relay.RelayManager`,
which restarts that relay alone.  A save that fails leaves both files and
the running relay as they were.
"""

from __future__ import annotations

import contextlib
import ipaddress
import json
import logging
import os
import re
import socket
import sys
import tempfile
import threading
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from autodj.config import (
    MAX_RELAYS,
    RELAY_TYPES,
    STREAM_BITRATES,
    RelayConfig,
    relay_password_variable,
    relay_target,
    station_relay_password_variable,
)
from autodj.fsutil import atomic_write
from autodj.relay import RelayError, describe_target
from autodj.station_log import DEFAULT_SLUG

if TYPE_CHECKING:
    from autodj.config import StreamConfig
    from autodj.relay import RelayManager
    from autodj.stations import StationPaths

logger = logging.getLogger(__name__)

FILE_VERSION = 1
#: A relay's settings besides its name (the key) and password (kept apart).
TABLE_KEYS: tuple[str, ...] = (
    "type",
    "host",
    "port",
    "mount",
    "stream_id",
    "user",
    "bitrate",
    "public",
    "enabled",
)
_NAME = re.compile(r"[A-Za-z0-9_-]{1,32}\Z")
# Held, across every station, while a station checks its relays against
# the others' and publishes its own (a save's check and write, a load), so
# two stations cannot both take one relay target.  Never held while a relay
# restarts.  A station's own lock is taken inside it, and only a holder of
# this lock may then take another station's.
_TARGETS_LOCK = threading.Lock()
# A host name label that is a number in some base (decimal, 0x hex or 0
# octal): resolvers read "2852039166" or "0xA9.0xFE.0xA9.0xFE" as an IPv4
# address, so a host made only of such labels must be a plain address.
_NUMERIC_LABEL = re.compile(r"(?:0[xX][0-9a-fA-F]*|[0-9]+)\Z")
# The IPv6 address some clouds serve instance metadata on; not link-local.
_METADATA_V6 = ipaddress.ip_address("fd00:ec2::254")

#: What the page says when a field is refused, by field.
FIELD_MESSAGES: dict[str, str] = {
    "name": "Enter a name of 1 to 32 letters, digits, hyphens or underscores.",
    "type": "Choose Icecast, Shoutcast 1 or Shoutcast 2.",
    "host": "Enter the server's host name or IP address, without http://, a port or a path.",
    "port": "Enter the listener port, a whole number from 1 to 65535 "
    "(to 65534 for Shoutcast, whose sources use the port above it).",
    "mount": "Enter the mount point: a / followed by letters, digits and . _ ~ - /, "
    "for example /live.",
    "user": "Enter the source user name, up to 255 characters, without a colon.",
    "stream_id": "Enter the stream ID, a whole number of 1 or more.",
    "bitrate": "Choose the station's quality, or 128, 192, 256 or 320 kbps.",
    "password": "Enter the source password: up to 255 characters, without control characters.",  # nosec B105 -- words for the page
    "public": "Public directory listing must be on or off.",
    "enabled": "On must be on or off.",
}
_BODY_KEYS = frozenset({"name", "password", *TABLE_KEYS})


class RelaySettingsError(Exception):
    """A refused relay change, with the HTTP status and the field to blame.

    The message is plain wording for the page; it never holds a password.
    """

    def __init__(self, status: int, message: str, field: str | None = None) -> None:
        super().__init__(message)
        self.status = status
        self.message = message
        self.field = field


@dataclass
class Saved:
    """What the two files hold: relay tables and passwords, by name."""

    definitions: dict[str, Any] = field(default_factory=dict)
    passwords: dict[str, str] = field(default_factory=dict)


@dataclass
class RelayEntry:
    """One relay as the station has it now.

    Attributes:
        name: The relay's name.
        table: Its settings without the password, as checked; the raw
            saved values when they could not be used.
        source: ``"config"`` (the TOML files), ``"saved"`` (only the page)
            or ``"config_saved"`` (a TOML relay changed on the page).
        password_source: ``"environment"``, ``"saved"``, ``"config"`` or ``None``.
        config: The relay as it runs; ``None`` when it cannot run.
        problem: Why it cannot run, in words.
        valid: Whether *table* passed the checks.
    """

    name: str
    table: dict[str, Any]
    source: str
    password_source: str | None
    config: RelayConfig | None
    problem: str | None = None
    valid: bool = True


def password_variable(slug: str, name: str) -> str:
    """The environment variable that sets the password of station *slug*'s relay *name*."""
    if slug == DEFAULT_SLUG:
        return relay_password_variable(name)
    return station_relay_password_variable(slug, name)


def table_of(cfg: RelayConfig) -> dict[str, Any]:
    """*cfg*'s settings without its name and password."""
    return {key: getattr(cfg, key) for key in TABLE_KEYS}


def _field_of(message: str) -> str | None:
    """The setting a :class:`RelayConfig` error names, if any."""
    match = re.search(r"stream\.relays\.[A-Za-z0-9_-]+\.(\w+)", message) or re.search(
        r"is missing '(\w+)'", message
    )
    return match.group(1) if match else None


def build_config(name: str, table: Mapping[str, Any], password: str) -> RelayConfig:
    """Check *table* as relay *name* and return the relay.

    Raises:
        RelaySettingsError: 422 naming the field refused.
    """
    values: dict[str, Any] = {key: table[key] for key in TABLE_KEYS if key in table}
    try:
        return RelayConfig(name=name, password=password, **values)
    except (TypeError, ValueError, KeyError) as exc:
        message = str(exc.args[0]) if exc.args else str(exc)
        key = _field_of(message)
        raise RelaySettingsError(422, FIELD_MESSAGES.get(key or "", message), key) from None


def _checked_table(name: str, raw: Any) -> dict[str, Any]:
    """A saved table, checked and with the per-type defaults filled.

    Raises:
        RelaySettingsError: It is not usable.
    """
    if not isinstance(raw, Mapping):
        raise RelaySettingsError(422, "it is not a table of settings")
    unknown = sorted(set(raw) - set(TABLE_KEYS))
    if unknown:
        logger.warning("Ignoring unknown keys of relay %s in relays.json: %s", name, unknown)
    return table_of(build_config(name, raw, "placeholder"))


def _target_text(table: Mapping[str, Any]) -> str:
    """Where a relay that cannot be checked points, for the page."""
    host, port = table.get("host"), table.get("port", 8000)
    if not isinstance(host, str):
        return ""
    return f"{host}:{port}{table.get('mount') or ''}"


def build_entry(
    name: str,
    toml: RelayConfig | None,
    saved_table: Any,
    saved_password: str | None,
    *,
    slug: str,
    environ: Mapping[str, str],
) -> RelayEntry:
    """Work out relay *name* from the TOML files, the saved files and the environment."""
    problem = None
    table: dict[str, Any] | None = None
    valid = True
    if saved_table is not None:
        try:
            table = _checked_table(name, saved_table)
        except RelaySettingsError as exc:
            logger.warning("Relay %s saved in Settings is not usable: %s", name, exc.message)
            problem = f"Its saved settings are not valid: {exc.message}"
    from_saved = table is not None
    if table is None and toml is not None:
        table = table_of(toml)
    if table is None:
        valid = False
        raw = saved_table if isinstance(saved_table, Mapping) else {}
        table = {key: raw.get(key) for key in TABLE_KEYS}
    if toml is None:
        source = "saved"
    else:
        source = "config_saved" if from_saved else "config"
    variable = password_variable(slug, name)
    password: str | None
    if variable in environ:
        password, password_source = environ[variable], "environment"
    elif saved_password:
        password, password_source = saved_password, "saved"
    elif toml is not None:
        password, password_source = toml.password, "config"
    else:
        password, password_source = None, None
    config = None
    if valid and problem is None:
        if password is None:
            problem = "No password is set. Edit the relay and enter its source password."
        else:
            try:
                config = build_config(name, table, password)
            except RelaySettingsError as exc:
                problem = exc.message
    elif valid and toml is not None and password is not None:
        # The saved settings are not usable: the TOML table still runs.
        with contextlib.suppress(RelaySettingsError):
            config = build_config(name, table, password)
    return RelayEntry(name, table, source, password_source, config, problem, valid)


class RelayStore:
    """A station's ``relays.json`` and ``relay-passwords.json``."""

    def __init__(self, path: Path, passwords_path: Path) -> None:
        self.path = path
        self.passwords_path = passwords_path
        #: Why a file could not be read; saving is refused until it is fixed,
        #: so a damaged file is never overwritten with less than it held.
        self.problem: str | None = None
        # relays.json had keys beside version and relays: rewrite it.
        self._extra_keys = False

    def _scrub(self, found: dict[str, Any], *, rewrite: bool = False) -> dict[str, Any]:
        """Drop what a relay table may not hold, such as a password typed into the file.

        ``relays.json`` goes into backups, so anything but the relay
        settings is removed from the file at once, with a warning naming the
        relay and the key (never its value).
        """
        clean: dict[str, Any] = {}
        dropped = rewrite
        for name, table in found.items():
            if not isinstance(table, Mapping):
                logger.warning("Removing relay %r from relays.json: it is not a table", name)
                dropped = True
                continue
            unknown = sorted(set(table) - set(TABLE_KEYS))
            if unknown:
                logger.warning(
                    "Removing unknown keys of relay %s from relays.json: %s (passwords belong "
                    "in Settings, Stream or config.local.toml)",
                    name,
                    unknown,
                )
                dropped = True
            clean[name] = {key: value for key, value in table.items() if key in TABLE_KEYS}
        if dropped:
            try:
                self._write_definitions(clean)
            except OSError:
                logger.warning("Could not rewrite relays.json without them", exc_info=True)
        return clean

    def _read(self, path: Path, key: str) -> dict[str, Any]:
        try:
            text = path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return {}
        except (OSError, UnicodeError) as exc:
            raise ValueError(f"cannot read it ({type(exc).__name__})") from None
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            raise ValueError("it is not valid JSON") from None
        if not isinstance(data, dict) or data.get("version") != FILE_VERSION:
            raise ValueError(f"it is not a version {FILE_VERSION} file")
        unknown = sorted(set(data) - {"version", key})
        if unknown:
            logger.warning("Ignoring unknown keys in %s: %s", path.name, unknown)
        found = data.get(key, {})
        if not isinstance(found, dict):
            raise ValueError(f"its {key} are not a table")
        if unknown and key == "relays":
            # Rewritten without them, as they could hold anything.
            self._extra_keys = True
        return found

    def load(self) -> Saved:
        """Read both files; a missing file is empty, a damaged one is ignored with a warning."""
        self.problem = None
        saved = Saved()
        for path, key in ((self.path, "relays"), (self.passwords_path, "passwords")):
            try:
                found = self._read(path, key)
            except ValueError as exc:
                logger.warning(
                    "Ignoring %s: %s. Relays cannot be saved from the web page until it "
                    "is fixed or removed.",
                    path,
                    exc,
                )
                self.problem = (
                    f"The saved relay settings ({path.name}) could not be read: {exc}. Fix or "
                    "remove that file in the station's folder, then restart AutoDJ."
                )
                continue
            if key == "relays":
                rewrite, self._extra_keys = self._extra_keys, False
                saved.definitions = self._scrub(found, rewrite=rewrite)
            else:
                saved.passwords = {
                    name: value
                    for name, value in found.items()
                    if isinstance(name, str) and isinstance(value, str) and value
                }
        return saved

    def write(self, new: Saved, old: Saved) -> None:
        """Save *new*: the passwords first, then the settings.

        When the settings cannot be written the old passwords are put back,
        so a failed save never leaves a password that does not match its
        settings.

        Raises:
            OSError: Nothing was saved.
        """
        passwords_changed = new.passwords != old.passwords
        if passwords_changed:
            self._write_passwords(new.passwords)
        try:
            self._write_definitions(new.definitions)
        except OSError:
            if passwords_changed:
                with contextlib.suppress(OSError):
                    self._write_passwords(old.passwords)
            raise

    def _write_definitions(self, definitions: Mapping[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        text = json.dumps(
            {"version": FILE_VERSION, "relays": dict(definitions)}, indent=2, ensure_ascii=False
        )
        atomic_write(self.path, text + "\n")

    def _write_passwords(self, passwords: Mapping[str, str]) -> None:
        text = json.dumps({"version": FILE_VERSION, "passwords": dict(passwords)}, indent=2)
        write_owner_only(self.passwords_path, text + "\n")


def write_owner_only(path: Path, text: str) -> None:
    """Replace *path* with *text*, readable and writable by this user alone.

    On POSIX the file is ``0600``.  On Windows the new file's access list is
    set to this user only, with nothing inherited from the folder, before
    the text is written, and the rename keeps it.  If that cannot be done
    nothing is written.

    Raises:
        OSError: Nothing was saved.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    if sys.platform != "win32":
        atomic_write(path, text, mode=0o600)
        os.chmod(path, 0o600)
        return
    handle, temporary = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        os.close(handle)
        restrict_to_current_user(Path(temporary))
        with open(temporary, "w", encoding="utf-8", newline="") as stream:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(temporary)
        raise


def restrict_to_current_user(path: Path) -> None:  # pragma: no cover - Windows only
    """Give *path* a protected access list allowing only the current user (Windows).

    Raises:
        OSError: The access list could not be set.
    """
    if sys.platform != "win32":
        raise OSError("access lists are set on Windows only")
    import ctypes
    from ctypes import wintypes

    advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.GetCurrentProcess.restype = wintypes.HANDLE
    kernel32.LocalFree.argtypes = [ctypes.c_void_p]
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    advapi32.OpenProcessToken.argtypes = [
        wintypes.HANDLE,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.HANDLE),
    ]
    advapi32.GetTokenInformation.argtypes = [
        wintypes.HANDLE,
        ctypes.c_int,
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD),
    ]
    advapi32.ConvertSidToStringSidW.argtypes = [
        ctypes.c_void_p,
        ctypes.POINTER(wintypes.LPWSTR),
    ]
    advapi32.ConvertStringSecurityDescriptorToSecurityDescriptorW.argtypes = [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        ctypes.POINTER(ctypes.c_void_p),
        ctypes.c_void_p,
    ]
    advapi32.SetFileSecurityW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, ctypes.c_void_p]

    def fail(what: str) -> OSError:
        return OSError(ctypes.get_last_error(), f"could not {what} for {path.name}")

    token_query, token_user = 0x0008, 1
    token = wintypes.HANDLE()
    if not advapi32.OpenProcessToken(
        kernel32.GetCurrentProcess(), token_query, ctypes.byref(token)
    ):
        raise fail("read this user's token")
    try:
        size = wintypes.DWORD()
        advapi32.GetTokenInformation(token, token_user, None, 0, ctypes.byref(size))
        buffer = ctypes.create_string_buffer(size.value)
        if not advapi32.GetTokenInformation(token, token_user, buffer, size, ctypes.byref(size)):
            raise fail("read this user's identity")
        # TOKEN_USER starts with a pointer to the user's SID.
        sid = ctypes.cast(buffer, ctypes.POINTER(ctypes.c_void_p))[0]
        text = wintypes.LPWSTR()
        if not advapi32.ConvertSidToStringSidW(sid, ctypes.byref(text)):
            raise fail("name this user")
        try:
            user = text.value
        finally:
            kernel32.LocalFree(ctypes.cast(text, ctypes.c_void_p))
    finally:
        kernel32.CloseHandle(token)
    descriptor = ctypes.c_void_p()
    # Protected (P): nothing inherited; one entry: full access for the user.
    if not advapi32.ConvertStringSecurityDescriptorToSecurityDescriptorW(
        f"D:P(A;;FA;;;{user})", 1, ctypes.byref(descriptor), None
    ):
        raise fail("build an access list")
    try:
        dacl, protected = 0x00000004, 0x80000000
        if not advapi32.SetFileSecurityW(str(path), dacl | protected, descriptor):
            raise fail("set the access list")
    finally:
        kernel32.LocalFree(descriptor)


#: Another station's relay: its station, name, target and password variable.
OtherRelay = tuple[str, str, tuple[object, ...], str]


def station_relays(
    toml: Mapping[str, RelayConfig],
    paths: StationPaths,
    slug: str,
    environ: Mapping[str, str] | None = None,
) -> dict[str, RelayEntry]:
    """The relays a station that is not running would have, from its files."""
    store = RelayStore(paths.relays, paths.relay_passwords)
    return merge(toml, store.load(), slug=slug, environ=os.environ if environ is None else environ)


def merge(
    toml: Mapping[str, RelayConfig], saved: Saved, *, slug: str, environ: Mapping[str, str]
) -> dict[str, RelayEntry]:
    """Every relay of a station: the TOML ones in their order, then the ones only saved."""
    names = [*toml, *(name for name in saved.definitions if name not in toml)]
    entries = {}
    for name in names:
        if not isinstance(name, str) or not _NAME.match(name):
            logger.warning("Ignoring a relay saved in Settings with the name %r", name)
            continue
        entries[name] = build_entry(
            name,
            toml.get(name),
            saved.definitions.get(name),
            saved.passwords.get(name),
            slug=slug,
            environ=environ,
        )
    return entries


def entry_targets(slug: str, entries: Iterable[RelayEntry]) -> list[OtherRelay]:
    """Where each of *entries* sends, with its password variable."""
    found = []
    for entry in entries:
        if not entry.valid:
            continue
        with contextlib.suppress(RelaySettingsError):
            target = relay_target(build_config(entry.name, entry.table, "placeholder"))
            found.append((slug, entry.name, target, password_variable(slug, entry.name)))
    return found


def _text(data: Mapping[str, Any], key: str, *, required: bool) -> str | None:
    value = data.get(key)
    if value is None or (isinstance(value, str) and not value.strip() and not required):
        if required:
            raise RelaySettingsError(422, FIELD_MESSAGES[key], key)
        return None
    if not isinstance(value, str):
        raise RelaySettingsError(422, FIELD_MESSAGES[key], key)
    value = value.strip()
    if required and not value:
        raise RelaySettingsError(422, FIELD_MESSAGES[key], key)
    return value


def _whole(data: Mapping[str, Any], key: str, default: int | None) -> int | None:
    value = data.get(key, default)
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        raise RelaySettingsError(422, FIELD_MESSAGES[key], key)
    return value


def _switch(data: Mapping[str, Any], key: str, default: bool) -> bool:
    value = data.get(key, default)
    if not isinstance(value, bool):
        raise RelaySettingsError(422, FIELD_MESSAGES[key], key)
    return value


def parse_body(data: Any) -> tuple[str, dict[str, Any], str | None]:
    """Read a relay sent by the page: its name, its settings and a new password, if typed.

    Only what a relay may hold is accepted, by the rules of
    ``[stream.relays.<name>]``.  A new or changed host is checked further
    by :func:`check_host` when it is saved.

    Raises:
        RelaySettingsError: 422 naming the field refused.
    """
    if not isinstance(data, Mapping):
        raise RelaySettingsError(422, "Send the relay as a JSON object.")
    unknown = sorted(set(data) - _BODY_KEYS)
    if unknown:
        raise RelaySettingsError(422, f"Unknown relay setting: {unknown[0]}")
    name = _text(data, "name", required=True)
    assert name is not None
    if not _NAME.match(name):
        raise RelaySettingsError(422, FIELD_MESSAGES["name"], "name")
    kind = data.get("type")
    if kind not in RELAY_TYPES:
        raise RelaySettingsError(422, FIELD_MESSAGES["type"], "type")
    host = _text(data, "host", required=True)
    assert host is not None
    bitrate = data.get("bitrate")
    if bitrate is not None and (isinstance(bitrate, bool) or bitrate not in STREAM_BITRATES):
        raise RelaySettingsError(422, FIELD_MESSAGES["bitrate"], "bitrate")
    table: dict[str, Any] = {
        "type": kind,
        "host": host,
        "port": _whole(data, "port", 8000),
        "mount": _text(data, "mount", required=kind == "icecast") if kind == "icecast" else None,
        "stream_id": _whole(data, "stream_id", 1) if kind == "shoutcast2" else None,
        "user": _text(data, "user", required=False) if kind == "icecast" else None,
        "bitrate": bitrate,
        "public": _switch(data, "public", False),
        "enabled": _switch(data, "enabled", True),
    }
    password = data.get("password")
    if password is not None and not isinstance(password, str):
        raise RelaySettingsError(422, FIELD_MESSAGES["password"], "password")
    return name, table, password or None


_NEVER_A_SERVER = (
    "That address is never a streaming server (it is unspecified, multicast or link-local, "
    "such as a cloud metadata address). Enter the server's host name or IP address."
)


def _never_a_server(address: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    if isinstance(address, ipaddress.IPv6Address):
        if address == _METADATA_V6:
            return True
        mapped = address.ipv4_mapped or address.sixtofour
        if mapped is not None and _never_a_server(mapped):
            return True
    return address.is_unspecified or address.is_multicast or address.is_link_local


def check_host(host: str, port: int, *, resolve: Callable[..., Any] = socket.getaddrinfo) -> None:
    """Refuse a host typed on the page that is, or resolves to, an address that is never a server.

    The checks: a host made only of numeric labels must be a plain address
    (``2852039166`` and ``0xA9.0xFE.0xA9.0xFE`` are refused, as resolvers
    read them as ``169.254.169.254``); the address, IPv4 mapped into IPv6
    included, may not be unspecified, multicast or link-local, nor the
    IPv6 metadata address some clouds use; and every address the name
    resolves to now is checked the same way.  A name that does not resolve
    is refused.  What the name resolves to later is not checked again (see
    the threat model).

    Raises:
        RelaySettingsError: 422 naming the host.
    """
    bare = host.strip("[]").rstrip(".")
    try:
        address = ipaddress.ip_address(bare)
    except ValueError:
        address = None
    if address is None and all(_NUMERIC_LABEL.match(label) for label in bare.split(".")):
        raise RelaySettingsError(
            422,
            "Write an IPv4 address as four decimal numbers from 0 to 255, such as 192.0.2.10.",
            "host",
        )
    if address is not None and _never_a_server(address):
        raise RelaySettingsError(422, _NEVER_A_SERVER, "host")
    try:
        found = resolve(bare, port, type=socket.SOCK_STREAM)
    except (OSError, UnicodeError):
        raise RelaySettingsError(
            422,
            "The server name could not be found. Check it, or enter the server's IP address.",
            "host",
        ) from None
    for item in found:
        try:
            resolved = ipaddress.ip_address(str(item[4][0]).split("%", 1)[0])
        except (ValueError, IndexError, TypeError):
            continue
        if _never_a_server(resolved):
            raise RelaySettingsError(422, _NEVER_A_SERVER, "host")


class RelayControl:
    """One running station's relays: what the page lists, adds, changes and removes.

    Built when the station starts streaming; :meth:`load` reads the saved
    files before the relays start, and :meth:`attach` hands it the
    :class:`~autodj.relay.RelayManager` that runs them.
    """

    def __init__(
        self,
        *,
        slug: str,
        stream: StreamConfig,
        paths: StationPaths,
        environ: Mapping[str, str] | None = None,
        elsewhere: Callable[[], Iterable[OtherRelay]] = tuple,
        resolve: Callable[..., Any] = socket.getaddrinfo,
    ) -> None:
        """Remember the station's TOML relays; read nothing yet.

        Args:
            slug: The station's name.
            stream: The station's ``[stream]`` settings, whose ``relays``
                this keeps current.
            paths: Where the station keeps its files.
            environ: The process environment, for the password variables.
            elsewhere: Every other station's relays, for the duplicate checks.
            resolve: ``socket.getaddrinfo``, replaceable in tests.
        """
        self.slug = slug
        self._stream = stream
        self._toml = dict(stream.relays)
        self._store = RelayStore(paths.relays, paths.relay_passwords)
        self._environ = os.environ if environ is None else environ
        self._elsewhere = elsewhere
        self._resolve = resolve
        # Held briefly around the state below.
        self._lock = threading.Lock()
        # This station's changes, one at a time, including restarting the
        # relay; other stations' changes do not wait for it.
        self._change_lock = threading.Lock()
        self._manager: RelayManager | None = None
        self._saved = Saved()
        self._entries: dict[str, RelayEntry] = {}

    def load(self) -> None:
        """Read the saved files and set the station's relays to what should run.

        A saved relay that would push to a target another station already
        uses is kept but not started, and says why.
        """
        with _TARGETS_LOCK:
            self._load_locked()

    def _load_locked(self) -> None:
        saved = self._store.load()
        entries = merge(self._toml, saved, slug=self.slug, environ=self._environ)
        taken = {target: slug for slug, _name, target, _variable in self._elsewhere()}
        for entry in entries.values():
            if entry.config is None or entry.source == "config":
                continue
            other = taken.get(relay_target(entry.config))
            if other is not None:
                logger.warning(
                    "Relay %s saved in Settings is not started: station %s relays to %s too",
                    entry.name,
                    other,
                    describe_target(entry.config),
                )
                entry.problem = (
                    f"Station {other} already relays to {describe_target(entry.config)}."
                )
                entry.config = None
        with self._lock:
            self._saved, self._entries = saved, entries
            self._stream.relays = {
                name: entry.config for name, entry in entries.items() if entry.config is not None
            }

    def attach(self, manager: RelayManager | None) -> None:
        """Run changes through *manager* from now on (``None`` once the stream stops)."""
        with self._lock:
            self._manager = manager

    def targets(self) -> list[OtherRelay]:
        """This station's relays, for another station's duplicate checks."""
        with self._lock:
            entries = list(self._entries.values())
        return entry_targets(self.slug, entries)

    def reconnect(self) -> None:
        """Make the live relays log in again with the station's new details."""
        with self._lock:
            manager = self._manager
        if manager is not None:
            manager.reconnect()

    # -- what the page sees --------------------------------------------------------

    def describe(self) -> dict[str, Any]:
        """Every relay for the page: settings, where the password comes from, and state.

        Never a password, only whether one is set and from where.
        """
        with self._lock:
            entries = list(self._entries.values())
            manager = self._manager
            saved = self._saved
        live = {status["name"]: status for status in manager.status()} if manager else {}
        return {
            "relays": [self._describe(entry, live.get(entry.name), saved) for entry in entries],
            "max_relays": MAX_RELAYS,
            "problem": self._store.problem,
        }

    def _describe(
        self, entry: RelayEntry, status: Mapping[str, Any] | None, saved: Saved
    ) -> dict[str, Any]:
        table = entry.table
        has_saved = entry.name in saved.definitions or entry.name in saved.passwords
        if entry.config is None or status is None:
            state = "off"
            error = entry.problem
            title_error = None
        else:
            state = status.get("state", "off")
            error = status.get("error")
            title_error = status.get("title_error")
        return {
            "name": entry.name,
            **{key: table.get(key) for key in TABLE_KEYS},
            "target": describe_target(entry.config) if entry.config else _target_text(table),
            "source": entry.source,
            "password_set": entry.password_source is not None,
            "password_source": entry.password_source,
            "password_variable": password_variable(self.slug, entry.name),
            "removable": entry.name not in self._toml,
            "revertable": entry.name in self._toml and has_saved,
            "state": state,
            "error": error,
            "title_error": title_error,
        }

    def entry(self, name: str) -> dict[str, Any]:
        """One relay for the page.

        Raises:
            RelaySettingsError: 404, no relay has that name.
        """
        with self._lock:
            entry = self._entries.get(name)
            manager = self._manager
            saved = self._saved
        if entry is None:
            raise RelaySettingsError(404, "No relay has that name.")
        status = None
        if manager is not None:
            status = next((item for item in manager.status() if item["name"] == name), None)
        return self._describe(entry, status, saved)

    # -- changes ---------------------------------------------------------------------

    def save(self, data: Any, *, create: bool) -> dict[str, Any]:
        """Add a relay (*create*) or change one, save it, then put it on the air.

        Raises:
            RelaySettingsError: The change was refused or could not be
                saved; nothing changed.
        """
        name, table, password = parse_body(data)
        with self._lock:
            before = self._entries.get(name)
        if before is None or before.table.get("host") != table["host"]:
            # Only a new or changed host: turning a relay on or off never
            # judges a host config.toml already gave it.  Outside the locks,
            # as a name lookup can take seconds.
            check_host(table["host"], int(table["port"] or 8000), resolve=self._resolve)
        with self._change_lock:
            with _TARGETS_LOCK, self._lock:
                self._refuse_while_stopped()
                self._refuse_while_damaged()
                existing = self._entries.get(name)
                if create and existing is not None:
                    raise RelaySettingsError(
                        409,
                        f"A relay named {name} already exists. Choose another name, or edit "
                        "that relay.",
                        "name",
                    )
                if not create and existing is None:
                    raise RelaySettingsError(404, "No relay has that name.")
                if create and len(self._entries) >= MAX_RELAYS:
                    raise RelaySettingsError(
                        409, f"A station can have at most {MAX_RELAYS} relays."
                    )
                variable = password_variable(self.slug, name)
                from_environment = variable in self._environ
                if password is not None and from_environment:
                    raise RelaySettingsError(
                        409,
                        f"The environment variable {variable} sets this relay's password, and it "
                        "wins over a password saved here. Change it there, or leave this field "
                        "empty.",
                        "password",
                    )
                toml = self._toml.get(name)
                effective = (
                    self._environ[variable]
                    if from_environment
                    else password
                    or self._saved.passwords.get(name)
                    or (toml.password if toml is not None else None)
                )
                if not effective:
                    raise RelaySettingsError(422, "Enter the source password.", "password")
                cfg = build_config(name, table, effective)
                self._refuse_duplicates(cfg)
                new = Saved(dict(self._saved.definitions), dict(self._saved.passwords))
                new.definitions[name] = table_of(cfg)
                if password is not None:
                    new.passwords[name] = password
                self._write(new)
                if from_environment:
                    origin = "environment"
                elif password is not None or name in new.passwords:
                    origin = "saved"
                else:
                    origin = "config"
                entry = RelayEntry(
                    name,
                    table_of(cfg),
                    "saved" if toml is None else "config_saved",
                    origin,
                    cfg,
                )
                self._entries[name] = entry
                self._stream.relays[name] = cfg
                manager = self._manager
            warning = self._put_on_air(manager, cfg)
        logger.info(
            "Relay %s %s in Settings (%s)",
            name,
            "added" if create else "changed",
            "on" if cfg.enabled else "off",
        )
        return {**self.entry(name), "warning": warning}

    def set_enabled(self, name: str, enabled: Any) -> dict[str, Any]:
        """Turn relay *name* on or off, keeping its other settings.

        Raises:
            RelaySettingsError: Refused or not saved; nothing changed.
        """
        if not isinstance(enabled, bool):
            raise RelaySettingsError(422, FIELD_MESSAGES["enabled"], "enabled")
        with self._lock:
            entry = self._entries.get(name)
        if entry is None:
            raise RelaySettingsError(404, "No relay has that name.")
        if enabled and entry.config is None:
            raise RelaySettingsError(
                409, f"{name} cannot be turned on: {entry.problem or 'its settings are not valid.'}"
            )
        return self.save({**entry.table, "name": name, "enabled": enabled}, create=False)

    def remove(self, name: str) -> tuple[str, str | None]:
        """Remove a relay added on the page, or revert a TOML relay to its TOML table.

        Returns:
            ``"removed"`` or ``"reverted"``, and what went wrong putting
            the change on the air after it was saved, if anything.

        Raises:
            RelaySettingsError: 404 for an unknown name, 409 for a relay
                only the TOML files define, or the files could not be saved.
        """
        with self._change_lock:
            with _TARGETS_LOCK, self._lock:
                self._refuse_while_stopped()
                self._refuse_while_damaged()
                if name not in self._entries:
                    raise RelaySettingsError(404, "No relay has that name.")
                toml = self._toml.get(name)
                if toml is not None and not (
                    name in self._saved.definitions or name in self._saved.passwords
                ):
                    raise RelaySettingsError(
                        409,
                        f"{name} is set in config.toml, so it cannot be removed here. Turn it "
                        "off here, or delete it from config.toml and restart AutoDJ.",
                    )
                new = Saved(
                    {key: value for key, value in self._saved.definitions.items() if key != name},
                    {key: value for key, value in self._saved.passwords.items() if key != name},
                )
                self._write(new)
                config: RelayConfig | None = None
                if toml is not None:
                    entry = build_entry(
                        name, toml, None, None, slug=self.slug, environ=self._environ
                    )
                    self._entries[name] = entry
                    config = entry.config
                else:
                    del self._entries[name]
                if config is not None:
                    self._stream.relays[name] = config
                else:
                    self._stream.relays.pop(name, None)
                manager = self._manager
            warning = None
            if config is not None:
                warning = self._put_on_air(manager, config)
            elif manager is not None:
                try:
                    manager.remove(name)
                except RelayError as exc:
                    warning = f"Saved, but {exc}."
        result = "reverted" if toml is not None else "removed"
        logger.info("Relay %s %s in Settings", name, result)
        return result, warning

    def _refuse_while_stopped(self) -> None:
        if self._manager is None:
            raise RelaySettingsError(
                409,
                "The stream is stopped, so its relays cannot be changed now. Nothing was saved.",
            )

    def _refuse_while_damaged(self) -> None:
        if self._store.problem is not None:
            raise RelaySettingsError(503, self._store.problem)

    def _write(self, new: Saved) -> None:
        try:
            self._store.write(new, self._saved)
        except OSError:
            logger.error("Could not save the relay settings", exc_info=True)
            raise RelaySettingsError(
                503, "Could not save the relay settings. Nothing was changed."
            ) from None
        self._saved = new

    def _refuse_duplicates(self, cfg: RelayConfig) -> None:
        """Refuse a relay that would share a target or a password variable with another."""
        target = relay_target(cfg)
        variable = password_variable(self.slug, cfg.name)
        mine = entry_targets(self.slug, (e for e in self._entries.values() if e.name != cfg.name))
        for slug, name, other_target, other_variable in [*mine, *self._elsewhere()]:
            where = "This station's relay" if slug == self.slug else f"Station {slug}'s relay"
            if other_target == target:
                raise RelaySettingsError(
                    409,
                    f"{where} {name} already sends to {describe_target(cfg)}. Each relay needs "
                    "its own server and mount point or stream ID.",
                    "host",
                )
            if other_variable == variable:
                raise RelaySettingsError(
                    409,
                    f"{where} {name} reads its password from {variable} too. Choose another name.",
                    "name",
                )

    def _put_on_air(self, manager: RelayManager | None, cfg: RelayConfig) -> str | None:
        """Start, restart or stop the relay as *cfg* says; the others carry on.

        Returns:
            ``None``, or what went wrong, in words: the change is saved
            either way, so the page shows it as saved and says this.
        """
        if manager is None:
            return None
        try:
            manager.apply(cfg)
        except RelayError as exc:
            return f"Saved, but {exc}."
        except Exception:
            logger.exception("Relay %s could not start its encoder", cfg.name)
            return (
                f"Saved, but the encoder for {cfg.name} could not start, so the relay is not "
                "running yet. It starts with the station next time."
            )
        return None
