"""The update contract: the files a server and its updater exchange (version 1).

One contract, whatever updates AutoDJ: the source checkout's launcher now,
and a helper on a container's host later.  The server never changes its
own files; it writes a request and exits with
:data:`autodj.updates.UPDATE_EXIT_CODE`.  The updater does the work while
no server runs and starts a server again.  ADR 16 documents every field.

The files, JSON objects of at most 64 KiB, each written atomically (a
temporary file renamed over the old one), in one update folder:

- ``request.json``: the server, when someone presses Update or Go back;
- ``state.json``: the updater, while it works, after each step;
- ``result.json``: the updater, once, when it is done; the page reads it;
- ``go-back.json``: the updater, when it moves a stable release to
  development, so the page can offer to go back with that backup.

Every file has ``"contract": 1``.  A reader ignores a file with another
version, or one that is damaged, as if it were missing.

The update folder is named by the updater in ``AUTODJ_UPDATE_DIR`` for the
server it starts: the checkout's launcher uses ``.uv/update`` inside the
checkout.  Without the variable, the server uses ``updates`` in the index
folder, which a container mounts, so a helper on the host finds it there.
Backups the updater makes go in its ``backups`` folder.

The steps are generic, so any updater maps onto them; a step that does not
apply is ``skipped``, never missing: ``check``, ``backup``, ``fetch`` (a
git fetch, or an image pull), ``switch``, ``setup``, ``restore`` (Go back
only), ``migration-check``, ``start``, ``health``, ``rollback``.

The health check is plain HTTP that a shell script can make too:
``GET /healthz`` at the address in the request, with its Host header,
answers 200 and ``{"status": "ok"}`` when the server is up.

Standard library only, so the launcher can use it before any dependency.
"""

from __future__ import annotations

import contextlib
import http.client
import ipaddress
import json
import os
import re
import ssl
import time
import urllib.request
import zipfile
from collections.abc import Callable, Mapping
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

#: The version of this contract; a reader ignores any other.
CONTRACT_VERSION = 1
#: Set by an updater for the server it starts: the update folder.
UPDATE_DIR_ENV = "AUTODJ_UPDATE_DIR"
#: The update folder in the index folder, when no updater names one.
INDEX_UPDATE_DIR = "updates"
REQUEST = "request.json"
STATE = "state.json"
RESULT = "result.json"
GO_BACK = "go-back.json"
#: Where an updater keeps the backups it makes, inside the update folder.
BACKUPS = "backups"
#: The name of the backup an updater keeps for Go back.
GO_BACK_BACKUP = re.compile(r"autodj-go-back-[0-9A-Za-z.-]{1,80}\.zip\Z")

#: What a request asks for.
ACTIONS = ("update", "go-back")
#: Every step, in order.
STEPS = (
    "check",
    "backup",
    "fetch",
    "switch",
    "setup",
    "restore",
    "migration-check",
    "start",
    "health",
    "rollback",
)
STEP_STATUSES = ("pending", "running", "done", "skipped", "failed")
#: How an update ended.
OUTCOMES = ("updated", "rolled-back", "refused", "failed", "stopped")
#: The outcomes that come with recovery commands, when there are any.
FAILURES = frozenset({"rolled-back", "refused", "failed", "stopped"})

_MAX_BYTES = 64 * 1024
_ID = re.compile(r"[0-9a-f]{8,64}\Z")
_HOST = re.compile(r"[A-Za-z0-9.:\[\]-]{1,255}\Z")
_SHA = re.compile(r"[0-9a-f]{40}\Z")
_TAG = re.compile(r"v\d{1,6}\.\d{1,6}\.\d{1,6}\Z")


def update_dir(index_dir: Path | None, environ: Mapping[str, str] | None = None) -> Path | None:
    """The update folder: the one the updater names, else ``updates`` in *index_dir*."""
    environment = os.environ if environ is None else environ
    named = (environment.get(UPDATE_DIR_ENV) or "").strip()
    if named:
        return Path(named)
    return Path(index_dir) / INDEX_UPDATE_DIR if index_dir is not None else None


def valid_id(value: object) -> str | None:
    """*value* when it is an update's id (8 to 64 lowercase hex digits), else ``None``."""
    return value if isinstance(value, str) and _ID.fullmatch(value) else None


def read(path: Path) -> dict[str, Any] | None:
    """A contract file, or ``None`` when it is missing, damaged or another version."""
    try:
        with Path(path).open("rb") as handle:
            data = handle.read(_MAX_BYTES + 1)
    except OSError:
        return None
    if len(data) > _MAX_BYTES:
        return None
    try:
        record = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return None
    if not isinstance(record, dict) or record.get("contract") != CONTRACT_VERSION:
        return None
    if record.get("id") is not None and valid_id(record["id"]) is None:
        return None
    return record


def write(path: Path, record: Mapping[str, Any]) -> None:
    """Replace a contract file atomically, with this contract's version.

    Raises:
        OSError: It could not be written.
    """
    from autodj.fsutil import atomic_write

    atomic_write(Path(path), json.dumps({**record, "contract": CONTRACT_VERSION}, indent=2) + "\n")


def remove(path: Path) -> None:
    """Delete a contract file if it is there."""
    with contextlib.suppress(OSError):
        Path(path).unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# The health check
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class HealthProbe:
    """Where an updater asks a new server whether it is up: ``GET /healthz``.

    ``address`` is the IP address to connect to (loopback for a server bound
    to every interface); ``host`` is the Host header, one the server's own
    allowed hosts accept.  A shell script can make the same request, for
    example with curl's ``--header`` and ``--insecure`` over HTTPS.
    """

    scheme: str
    address: str
    port: int
    host: str
    path: str = "/healthz"

    @property
    def url(self) -> str:
        """The address of ``/healthz``."""
        address = f"[{self.address}]" if ":" in self.address else self.address
        return f"{self.scheme}://{address}:{self.port}{self.path}"

    def to_dict(self) -> dict[str, Any]:
        """The JSON form in the request."""
        return asdict(self)


def parse_health(raw: object) -> HealthProbe | None:
    """A probe from a request, or ``None`` unless every part is valid.

    The address must be an IP address or ``localhost``, so an updater only
    ever asks this computer or the address the server was bound to.
    """
    if not isinstance(raw, dict):
        return None
    scheme, address, port, host = (raw.get(key) for key in ("scheme", "address", "port", "host"))
    if scheme not in {"http", "https"} or not isinstance(address, str):
        return None
    if address != "localhost":
        try:
            ipaddress.ip_address(address)
        except ValueError:
            return None
    if not isinstance(port, int) or isinstance(port, bool) or not 1 <= port <= 65535:
        return None
    if not isinstance(host, str) or not _HOST.fullmatch(host):
        return None
    if raw.get("path", "/healthz") != "/healthz":
        return None
    return HealthProbe(scheme, address, port, host)


def health_for(
    host: str, port: int, *, https: bool, host_allowed: Callable[[str], bool]
) -> HealthProbe:
    """How to reach a server bound to *host*:*port* from this computer.

    A server bound to every interface is asked on loopback.  The Host header
    is the first of the address, ``localhost``, the loopback addresses and
    *host* that *host_allowed* accepts, so the server's own Host check lets
    the probe through; with none, the address itself.
    """
    bound = host.strip("[]")
    if bound in {"", "0.0.0.0"}:  # nosec B104 -- compares the bind address, binds nothing
        address = "127.0.0.1"
    elif bound == "::":
        address = "::1"
    else:
        address = bound
    candidates = [address, "localhost", "127.0.0.1", "::1", bound]
    header = next((name for name in candidates if name and host_allowed(name)), address)
    rendered = f"[{header}]" if ":" in header else header
    return HealthProbe("https" if https else "http", address, port, f"{rendered}:{port}")


def probe_health(probe: HealthProbe, timeout: float = 2.0) -> bool:
    """Whether the server answers ``GET /healthz`` with 200 and ``"ok"``.

    No proxy is used.  Over HTTPS the certificate is not checked: the probe
    asks this computer (or the address the server is bound to), sends
    nothing secret, and the certificate names the public host name.
    """
    handlers: list[urllib.request.BaseHandler] = [urllib.request.ProxyHandler({})]
    if probe.scheme == "https":
        context = ssl.create_default_context()
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE  # nosec B501 -- local readiness probe, see above
        handlers.append(urllib.request.HTTPSHandler(context=context))
    opener = urllib.request.build_opener(*handlers)
    request = urllib.request.Request(probe.url, headers={"Host": probe.host}, method="GET")
    try:
        with opener.open(request, timeout=timeout) as response:  # nosec B310 -- http(s) only
            if response.status != 200:
                return False
            body = json.loads(response.read(4096).decode("utf-8"))
    except (OSError, ValueError, http.client.HTTPException):
        return False
    return isinstance(body, dict) and body.get("status") == "ok"


# ---------------------------------------------------------------------------
# The files
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Request:
    """What the server asks for: ``request.json``.

    Fields: ``id`` (16 hex digits), ``action`` (``update`` or ``go-back``),
    ``channel`` (the channel the server follows), ``requested_at`` (seconds
    since 1970), ``health`` (:class:`HealthProbe`).  The server also writes
    ``target`` and ``from`` for the record; an updater works out the target
    itself and never trusts them.
    """

    id: str
    action: str
    channel: str
    requested_at: float
    health: HealthProbe


def read_request(folder: Path) -> Request | None:
    """The server's request, or ``None`` unless it is complete and valid."""
    record = read(Path(folder) / REQUEST)
    if record is None:
        return None
    update_id = valid_id(record.get("id"))
    health = parse_health(record.get("health"))
    requested = record.get("requested_at")
    if (
        update_id is None
        or health is None
        or record.get("action") not in ACTIONS
        or record.get("channel") not in ("stable", "development")
        or not isinstance(requested, (int, float))
        or isinstance(requested, bool)
    ):
        return None
    return Request(update_id, record["action"], record["channel"], float(requested), health)


def new_steps() -> list[dict[str, str]]:
    """Every step, pending: the start of ``state.json`` and ``result.json``'s ``steps``."""
    return [{"name": name, "status": "pending"} for name in STEPS]


def read_result(folder: Path) -> dict[str, Any] | None:
    """The updater's result, or ``None`` unless it is valid.

    Fields: ``id`` (the request's), ``action``, ``outcome`` (one of
    OUTCOMES), ``message`` (one plain sentence or more for the page),
    ``reason``, ``from`` and ``to`` (``version``, ``commit``, ``label``),
    ``steps`` (``name`` and ``status`` for every step), ``commands`` (the
    recovery commands, only when the outcome is a failure) and
    ``finished_at``.
    """
    record = read(Path(folder) / RESULT)
    if record is None or record.get("outcome") not in OUTCOMES:
        return None
    if record.get("action") not in ACTIONS or not isinstance(record.get("message"), str):
        return None
    commands = record.get("commands")
    if not isinstance(commands, list) or not all(isinstance(line, str) for line in commands):
        return None
    if commands and record["outcome"] not in FAILURES:
        return None
    return record


@dataclass(frozen=True)
class GoBack:
    """A stable release an updater moved to development from: ``go-back.json``.

    Fields: ``tag`` and ``version`` of that release, ``commit`` its commit,
    ``backup`` the path of the backup made just before the move (in the
    update folder's ``backups``, named ``autodj-go-back-*.zip``), and
    ``made_at`` when it was made.
    """

    tag: str
    version: str
    commit: str
    backup: Path
    made_at: float

    @property
    def date(self) -> str:
        """The day the backup was made, ``YYYY-MM-DD`` in local time."""
        return time.strftime("%Y-%m-%d", time.localtime(self.made_at))

    def to_dict(self) -> dict[str, Any]:
        """The JSON form in ``go-back.json``."""
        return {**asdict(self), "backup": str(self.backup)}


def read_go_back(folder: Path) -> GoBack | None:
    """The go-back record, or ``None`` unless it is valid and names this folder's backup."""
    record = read(Path(folder) / GO_BACK)
    if record is None:
        return None
    tag, version, commit, backup, made = (
        record.get(key) for key in ("tag", "version", "commit", "backup", "made_at")
    )
    if not (isinstance(tag, str) and _TAG.fullmatch(tag)) or not isinstance(version, str):
        return None
    if not (isinstance(commit, str) and _SHA.fullmatch(commit)) or not isinstance(backup, str):
        return None
    if not isinstance(made, (int, float)) or isinstance(made, bool):
        return None
    path = Path(backup)
    # Only a backup this flow made, in this folder: never one from anywhere else.
    try:
        inside = path.resolve().parent == (Path(folder) / BACKUPS).resolve()
    except OSError:
        return None
    if not inside or not GO_BACK_BACKUP.fullmatch(path.name):
        return None
    return GoBack(tag, version, commit, path, float(made))


def go_back_problem(record: GoBack) -> str | None:
    """Why the backup for Go back cannot be used, in one sentence; ``None`` when it can."""
    if not record.backup.is_file():
        return (
            f"Going back to {record.version} is not offered, because the backup AutoDJ "
            f"made on {record.date} is missing."
        )
    try:
        readable = zipfile.is_zipfile(record.backup)
    except OSError:
        readable = False
    if not readable:
        return (
            f"Going back to {record.version} is not offered, because the backup AutoDJ "
            f"made on {record.date} cannot be read."
        )
    return None
