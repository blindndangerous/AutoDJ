"""Request authentication, origin checks, audit logging, and the pairing lockout."""

from __future__ import annotations

import asyncio
import copy
import hashlib
import hmac
import ipaddress
import json
import logging
import re
import secrets
import threading
import time
import uuid
from collections.abc import Callable, Mapping
from collections.abc import Set as AbstractSet
from dataclasses import dataclass, field

from starlette.exceptions import HTTPException
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Match
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from autodj.config import ServerConfig, canonicalize_allowed_origin
from autodj.pairing import DEVICE_ID

COOKIE_NAME = "autodj_session"

logger = logging.getLogger(__name__)
_AUDIT_LOGGER = logging.getLogger("autodj.audit")
_UNSAFE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})
_PUBLIC_FILES = frozenset(
    {
        "/",
        "/app.css",
        "/app.js",
        "/bitcrusher-worklet.js",
        "/stutter-worklet.js",
        "/freeze-worklet.js",
        "/glitch-worklet.js",
        "/healthz",
        "/api/version",
        "/api/auth/status",
        "/api/nowplaying",
        # The public station status page; see status_page.py.
        "/status",
        # The player page and embeddable player; each answers 404 unless
        # [player_page] turns it on.  See player_page.py.
        "/player",
        "/embed",
        "/api/pair",
    }
)

_HEX_64 = re.compile(r"[0-9a-f]{64}\Z")
_CANONICAL_EXPIRY = re.compile(r"(?:0|[1-9][0-9]{0,18})\Z")
_MAX_EXPIRY = 2**63 - 1
_DNS_LABEL = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\Z")
_BRACKETED_HOST = re.compile(r"\[([^\]]+)\](?::([0-9]+))?\Z")
PAIRING_BODY_MAX_BYTES = 4096
PAIRING_CODE_WINDOW_SECONDS = 300
# Wrong well-formed codes tolerated per code window.  One client address that
# reaches the per-client limit is locked out until the window ends; only when
# all clients together reach the global limit are the live codes burned for
# everyone, as a last resort against many addresses.
PAIRING_MAX_FAILURES_PER_CLIENT = 10
PAIRING_MAX_FAILURES_PER_WINDOW = 50


@dataclass(frozen=True)
class PairingBlock:
    """Why pairing is refused before a code is even compared."""

    retry_after: int
    detail: str


def _parse_host_header(value: object) -> str | None:
    """Return a normalized host header name or ``None`` when invalid."""
    if (
        not isinstance(value, str)
        or not value
        or len(value) > 512
        or value != value.strip()
        or not value.isascii()
        or any(char.isspace() or ord(char) < 32 or ord(char) == 127 for char in value)
        or any(marker in value for marker in ("@", "/", "\\", "?", "#", "%", ","))
    ):
        return None

    bracketed = _BRACKETED_HOST.fullmatch(value)
    if bracketed is not None:
        hostname, port_text = bracketed.groups()
        try:
            canonical = ipaddress.IPv6Address(hostname).compressed.lower()
        except ValueError:
            return None
    else:
        if "[" in value or "]" in value or value.count(":") > 1:
            return None
        hostname, separator, port_text = value.partition(":")
        if not separator:
            port_text = None
        hostname = hostname.removesuffix(".").lower()
        if not hostname or len(hostname) > 253:
            return None
        try:
            address = ipaddress.IPv4Address(hostname)
        except ValueError:
            if any(not _DNS_LABEL.fullmatch(label) for label in hostname.split(".")):
                return None
            canonical = hostname
        else:
            canonical = address.compressed

    if port_text is not None:
        if (
            not port_text
            or len(port_text) > 5
            or not port_text.isascii()
            or not port_text.isdecimal()
        ):
            return None
        port = int(port_text)
        if not 1 <= port <= 65535:
            return None
    return canonical


def _is_unspecified_host(host: str) -> bool:
    """Return whether host is an unspecified IP address."""
    try:
        return ipaddress.ip_address(host).is_unspecified
    except ValueError:
        return False


@dataclass
class SecurityPolicy:
    """Evaluate host, origin, and session requirements for server requests."""

    config: ServerConfig
    secure_cookie: bool = False
    now: Callable[[], float] = time.time
    device_is_active: Callable[[str], bool] | None = field(default=None, repr=False)
    _pairing_lock: threading.Lock = field(
        default_factory=threading.Lock, init=False, repr=False, compare=False
    )
    _pairing_failure_window: int = field(default=-1, init=False, repr=False, compare=False)
    _pairing_failures: int = field(default=0, init=False, repr=False, compare=False)
    # Bounded in practice: after PAIRING_MAX_FAILURES_PER_WINDOW failures the
    # window is paused and nothing more is recorded until it resets.
    _pairing_client_failures: dict[str, int] = field(
        default_factory=dict, init=False, repr=False, compare=False
    )
    # Codes for windows below this number are refused even while still fresh.
    _pairing_first_valid_window: int = field(default=0, init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        """Detach policy decisions from caller-owned mutable configuration."""
        self.config = copy.deepcopy(self.config)

    @property
    def authentication_required(self) -> bool:
        """Return whether the policy has an access token configured."""
        return self.config.access_token is not None

    def pairing_code_now(self) -> tuple[str, int, int]:
        """Return the current code, seconds until it expires, and seconds until the next one.

        All three come from one clock reading, so the code and its times
        never straddle a window boundary.  A code is accepted for its own
        window and the one after it, so it outlives the start of its
        successor by one full window.

        Raises:
            RuntimeError: No access token is configured.
        """
        token = self.config.access_token
        if token is None:
            raise RuntimeError("access token is not configured")
        timestamp = int(self.now())
        window = timestamp // PAIRING_CODE_WINDOW_SECONDS
        next_start = (window + 1) * PAIRING_CODE_WINDOW_SECONDS
        return (
            self._pairing_code(token, window),
            next_start + PAIRING_CODE_WINDOW_SECONDS - timestamp,
            next_start - timestamp,
        )

    def current_pairing_code(self) -> str:
        """Return short code authorizing browsers during current time window."""
        return self.pairing_code_now()[0]

    def pairing_code_seconds_left(self) -> tuple[int, int]:
        """Return seconds until the current code expires and until the next one starts."""
        _code, valid_for, next_code_in = self.pairing_code_now()
        return valid_for, next_code_in

    def pairing_paused(self) -> PairingBlock | None:
        """Return why no code works right now after too many wrong ones, or ``None``."""
        timestamp = int(self.now())
        window = timestamp // PAIRING_CODE_WINDOW_SECONDS
        retry_after = (window + 1) * PAIRING_CODE_WINDOW_SECONDS - timestamp
        with self._pairing_lock:
            self._reset_pairing_failures_if_new_window(window)
            if self._pairing_first_valid_window > window:
                return PairingBlock(
                    retry_after,
                    "Pairing is paused after too many wrong codes. "
                    f"Try again in {retry_after} seconds with a new code.",
                )
        return None

    def pairing_block(self, client: str) -> PairingBlock | None:
        """Return why *client* may not try a code right now, or ``None``.

        Checked before comparing, so a locked-out client or a paused server
        answers the same way for right and wrong codes.
        """
        paused = self.pairing_paused()
        if paused is not None:
            return paused
        timestamp = int(self.now())
        window = timestamp // PAIRING_CODE_WINDOW_SECONDS
        retry_after = (window + 1) * PAIRING_CODE_WINDOW_SECONDS - timestamp
        with self._pairing_lock:
            self._reset_pairing_failures_if_new_window(window)
            if self._pairing_client_failures.get(client, 0) >= PAIRING_MAX_FAILURES_PER_CLIENT:
                return PairingBlock(
                    retry_after,
                    "Too many wrong pairing codes from this device. "
                    f"Try again in {retry_after} seconds.",
                )
        return None

    def _reset_pairing_failures_if_new_window(self, window: int) -> None:
        """Start fresh failure counts when the code window changes.

        Caller holds ``_pairing_lock``.
        """
        if self._pairing_failure_window != window:
            self._pairing_failure_window = window
            self._pairing_failures = 0
            self._pairing_client_failures.clear()

    @staticmethod
    def _pairing_code(token: str, window: int) -> str:
        """Derive one pairing code from server secret and numbered time window."""
        digest = hmac.new(
            token.encode("utf-8"), f"pair:{window}".encode("ascii"), hashlib.sha256
        ).digest()
        return f"{int.from_bytes(digest[:8], 'big') % 100_000_000:08d}"

    def verify_pairing_code(self, candidate: str, client: str = "<unknown>") -> bool:
        """Compare a candidate with current short-lived pairing code safely.

        A wrong, well-formed code counts against *client* and toward the
        server-wide limit; see :meth:`pairing_block`.
        """
        if (
            not isinstance(candidate, str)
            or len(candidate) != 8
            or not candidate.isascii()
            or not candidate.isdecimal()
        ):
            return False
        token = self.config.access_token
        if token is None:
            return False
        timestamp = int(self.now())
        window = timestamp // PAIRING_CODE_WINDOW_SECONDS
        candidate_bytes = candidate.encode("ascii")
        previous_window = max(0, window - 1)
        current = self._pairing_code(token, window).encode("ascii")
        previous = self._pairing_code(token, previous_window).encode("ascii")
        current_valid = secrets.compare_digest(candidate_bytes, current)
        previous_valid = secrets.compare_digest(candidate_bytes, previous)
        with self._pairing_lock:
            first_valid = self._pairing_first_valid_window
            if (current_valid and window >= first_valid) or (
                previous_valid and previous_window >= first_valid
            ):
                return True
            self._record_pairing_failure(timestamp, client)
        return False

    def _record_pairing_failure(self, timestamp: int, client: str) -> None:
        """Count one wrong code against *client* and the server-wide total.

        Caller holds ``_pairing_lock``.  A client reaching its limit is locked
        out for the rest of the window.  Reaching the server-wide limit burns
        every live code: the first accepted window moves past the current
        one, so the next working code is the one ``autodj devices
        pairing-code`` prints once the window turns over.
        """
        window = timestamp // PAIRING_CODE_WINDOW_SECONDS
        seconds_left = (window + 1) * PAIRING_CODE_WINDOW_SECONDS - timestamp
        self._reset_pairing_failures_if_new_window(window)
        client_failures = self._pairing_client_failures.get(client, 0) + 1
        self._pairing_client_failures[client] = client_failures
        self._pairing_failures += 1
        if client_failures == PAIRING_MAX_FAILURES_PER_CLIENT:
            logger.warning(
                "Pairing locked for client %s for %d seconds after %d wrong codes.",
                client,
                seconds_left,
                PAIRING_MAX_FAILURES_PER_CLIENT,
            )
        if self._pairing_failures < PAIRING_MAX_FAILURES_PER_WINDOW:
            return
        self._pairing_first_valid_window = window + 1
        logger.warning(
            "Pairing codes invalidated after %d wrong attempts; someone may be guessing. "
            "Run `autodj devices pairing-code` again in about %d seconds for a fresh code.",
            PAIRING_MAX_FAILURES_PER_WINDOW,
            seconds_left,
        )

    def issue_device_session(self, device_id: str) -> str:
        """Create signed session bound to one active paired device."""
        token = self.config.access_token
        if token is None:
            raise RuntimeError("access token is not configured")
        if DEVICE_ID.fullmatch(device_id) is None:
            raise ValueError("device ID is invalid")
        if self.device_is_active is not None and not self.device_is_active(device_id):
            raise ValueError("device is not active")
        expires = int(self.now()) + self.config.session_ttl_seconds
        nonce = secrets.token_hex(16)
        payload = f"{expires}.{device_id}.{nonce}"
        signature = hmac.new(
            token.encode("utf-8"), payload.encode("ascii"), hashlib.sha256
        ).hexdigest()
        return f"{payload}.{signature}"

    def _verified_session(self, value: str | None) -> tuple[bool, str | None]:
        """Validate session and return optional paired-device identity."""
        token = self.config.access_token
        if token is None or not isinstance(value, str):
            return False, None
        parts = value.split(".")
        if len(parts) == 4:
            expires_text, device_id, nonce, signature = parts
            if DEVICE_ID.fullmatch(device_id) is None:
                return False, None
            payload = f"{expires_text}.{device_id}.{nonce}"
        else:
            return False, None
        if (
            _CANONICAL_EXPIRY.fullmatch(expires_text) is None
            or DEVICE_ID.fullmatch(nonce) is None  # 32 hex digits, like a device ID
            or _HEX_64.fullmatch(signature) is None
        ):
            return False, None
        expires = int(expires_text)
        if expires > _MAX_EXPIRY:
            return False, None
        expected = hmac.new(
            token.encode("utf-8"), payload.encode("ascii"), hashlib.sha256
        ).hexdigest()
        signature_valid = secrets.compare_digest(
            signature.encode("ascii"), expected.encode("ascii")
        )
        if not signature_valid or expires < int(self.now()):
            return False, None
        if (
            device_id is not None
            and self.device_is_active is not None
            and not self.device_is_active(device_id)
        ):
            return False, None
        return True, device_id

    def verify_session(self, value: str | None) -> bool:
        """Return whether a session token is well formed, signed, and unexpired."""
        valid, _device_id = self._verified_session(value)
        return valid

    def session_device_id(self, value: str | None) -> str | None:
        """Return active paired-device identity carried by a valid session."""
        valid, device_id = self._verified_session(value)
        return device_id if valid else None

    def host_allowed(self, host_header: str | None) -> bool:
        """Return whether a normalized Host header appears in the configured allowlist."""
        hostname = _parse_host_header(host_header)
        return hostname is not None and hostname in set(self.config.effective_allowed_hosts())

    def effective_allowed_origins(self) -> list[str]:
        """Return transport-aware canonical origins without mutating config."""
        if self.config.allowed_origins is not None or not self.secure_cookie:
            return self.config.effective_allowed_origins()
        if _is_unspecified_host(self.config.host):
            return []
        rendered_host = f"[{self.config.host}]" if ":" in self.config.host else self.config.host
        return [canonicalize_allowed_origin(f"https://{rendered_host}:{self.config.port}")]

    def origin_allowed(self, origin: str | None) -> bool:
        """Return whether a valid origin appears in the effective allowlist."""
        try:
            canonical = canonicalize_allowed_origin(origin)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            return False
        return canonical in set(self.effective_allowed_origins())


def new_request_id() -> str:
    """Return a new hexadecimal request identifier."""
    return uuid.uuid4().hex


def audit_record(
    request_id: str,
    action: str,
    outcome: str,
    method: str | None = None,
    route: str | None = None,
    status: int | None = None,
) -> str:
    """Serialize a closed-schema audit event as JSON."""
    record: dict[str, str | int] = {
        "action": action,
        "outcome": outcome,
        "request_id": request_id,
    }
    if method is not None:
        record["method"] = method
    if route is not None:
        record["route"] = route
    if status is not None:
        record["status"] = status
    return json.dumps(record, allow_nan=False, separators=(",", ":"), sort_keys=True)


def emit_audit(
    request_id: str,
    action: str,
    outcome: str,
    *,
    method: str | None = None,
    route: str | None = None,
    status: int | None = None,
    level: int = logging.INFO,
) -> None:
    """Write one closed-schema audit event without request-controlled details."""
    _AUDIT_LOGGER.log(
        level,
        audit_record(request_id, action, outcome, method, route, status),
    )


def _safe_public_prefix(path: str, prefix: str) -> bool:
    """Return whether path contains a plain public filename under prefix."""
    if not path.startswith(prefix):
        return False
    suffix = path[len(prefix) :]
    if not suffix or "\\" in suffix:
        return False
    return all(part not in {"", ".", ".."} for part in suffix.split("/"))


# The routes under /api/stations/<slug>/ that need no session, as their
# default-station paths.  Everything else a station serves is paired.
_PUBLIC_STATION_ROUTES = frozenset({"/api/nowplaying"})
_STATION_PATH = re.compile(r"/api/stations/([^/]+)(/.*)\Z", re.DOTALL)


def station_relative(path: str, slugs: AbstractSet[str] | None) -> str | None:
    """Map ``/api/stations/<slug>/X`` to ``/api/X`` when *slug* is a configured station.

    Returns ``None`` for any other path, including one naming a station
    that is not configured, so an unknown name is never treated as a
    known route.  With *slugs* ``None`` any name maps: only for limits
    that must hold whatever the name.
    """
    match = _STATION_PATH.match(path)
    if match is None or (slugs is not None and match.group(1) not in slugs):
        return None
    return "/api" + match.group(2)


def _is_public_path(path: str, slugs: AbstractSet[str] = frozenset()) -> bool:
    """Return whether a request path is accessible without authentication.

    Listening (``/stream.mp3`` and ``/stream.m3u``, and
    ``/stations/<slug>.mp3`` and ``.m3u`` for a configured station) does
    not require pairing; control APIs require a session.  A configured
    station's public now-playing feed is public under its station path too,
    and so are its pages, ``/stations/<slug>/status``, ``/player`` and
    ``/embed``.
    """
    relative = station_relative(path, slugs)
    if relative is not None:
        return relative in _PUBLIC_STATION_ROUTES
    return (
        path in _PUBLIC_FILES
        or path in {"/stream.mp3", "/stream.m3u"}
        or _is_station_stream(path, slugs)
        or _is_station_page(path, slugs)
        or _safe_public_prefix(path, "/modules/")
    )


#: The public pages each station has under ``/stations/<slug>/``.
_STATION_PAGES = frozenset({"status", "player", "embed"})


def _is_station_page(path: str, slugs: AbstractSet[str]) -> bool:
    """Whether *path* is a public page of a configured *slug*: ``/stations/<slug>/status``..."""
    name = path.removeprefix("/stations/")
    slug, slash, rest = name.partition("/")
    return name != path and bool(slash) and rest in _STATION_PAGES and slug in slugs


def _is_station_stream(path: str, slugs: AbstractSet[str]) -> bool:
    """Whether *path* is ``/stations/<slug>.mp3`` or ``.m3u`` for a configured *slug*."""
    name = path.removeprefix("/stations/")
    if name == path:
        return False
    slug, dot, ext = name.rpartition(".")
    return bool(dot) and ext in {"mp3", "m3u"} and slug in slugs


def _route_template(scope: Scope) -> str:
    """Return the matching route template or the first partial match."""
    app = scope.get("app")
    partial = "<unmatched>"
    for route in getattr(app, "routes", ()):
        match, _child_scope = route.matches(scope)
        if match is Match.FULL:
            return str(getattr(route, "path", "<mounted>"))
        if match is Match.PARTIAL and partial == "<unmatched>":
            partial = str(getattr(route, "path", "<mounted>"))
    return partial


def _raw_header_values(scope: Scope, name: bytes) -> list[str]:
    """Return all raw header values matching name without combining them."""
    return [
        value.decode("latin-1") for key, value in scope.get("headers", ()) if key.lower() == name
    ]


def _is_loopback(address: str) -> bool:
    """Return whether *address* is a loopback IP address."""
    try:
        return ipaddress.ip_address(address).is_loopback
    except ValueError:
        return False


#: Headers a reverse proxy adds.  On plain HTTP they mean the request may
#: have crossed the network before it reached this machine's loopback.
_PROXY_HEADERS = (
    b"forwarded",
    b"x-forwarded-for",
    b"x-forwarded-proto",
    b"x-forwarded-host",
    b"x-real-ip",
)


def machine_transport_allowed(scope: Scope) -> bool:
    """Whether a machine token may travel on this connection: HTTPS, or loopback.

    A bearer token sent over plain HTTP can be read by anyone on the network
    path, so it is refused unless the client is this machine.  A request
    on plain HTTP that carries any proxy header (``Forwarded``,
    ``X-Forwarded-*``, ``X-Real-IP``) is refused even from loopback: a
    reverse proxy on this machine may have carried it in from the network
    in the clear.  Through a proxy, a machine token needs TLS up to the
    proxy, and the server must trust the proxy's ``X-Forwarded-Proto:
    https`` (``[stream] trusted_proxies``, or a proxy on this machine when
    that list is empty; see
    :func:`~autodj.listener_access.forwarded_allow_ips`), so the request
    counts as HTTPS here.
    """
    if scope.get("scheme") in {"https", "wss"}:
        return True
    names = {key.lower() for key, _value in scope.get("headers", ())}
    if any(name in names for name in _PROXY_HEADERS) or any(
        name.startswith(b"x-forwarded-") for name in names
    ):
        return False
    return _is_loopback(peer_address(scope))


def bearer_token(scope: Scope) -> str | None:
    """The token of a single ``Authorization: Bearer`` header, or ``None``."""
    values = _raw_header_values(scope, b"authorization")
    if len(values) != 1:
        return None
    scheme, _, token = values[0].partition(" ")
    token = token.strip()
    if scheme.lower() != "bearer" or not token or len(token) > 256:
        return None
    return token


def peer_address(scope: Scope) -> str:
    """Return a bounded client address string for pairing attempt tracking."""
    client = scope.get("client")
    if isinstance(client, (tuple, list)) and client:
        return str(client[0])[:128]
    return "<unknown>"


#: What every response says about frames unless it chose for itself.
NO_FRAMES = "frame-ancestors 'none'"


def frame_guard(send: Send) -> Send:
    """Wrap *send* so no response can be shown in another page's frame (clickjacking).

    A response without a Content-Security-Policy gets ``frame-ancestors
    'none'``; one with a policy that does not mention frames has it added.
    Either way, and for a policy that already says ``frame-ancestors
    'none'``, ``X-Frame-Options: DENY`` is added for older browsers.  Only a
    response whose own policy lets some site frame it (the embeddable
    player) is left as it is.
    """

    async def guarded(message: Message) -> None:
        if message["type"] == "http.response.start":
            headers = list(message.get("headers", []))
            index = next(
                (
                    i
                    for i, (key, _value) in enumerate(headers)
                    if key.lower() == b"content-security-policy"
                ),
                None,
            )
            framed = False
            if index is None:
                headers.append((b"content-security-policy", NO_FRAMES.encode("ascii")))
            else:
                policy = headers[index][1].decode("latin-1")
                if "frame-ancestors" not in policy:
                    headers[index] = (
                        b"content-security-policy",
                        f"{policy}; {NO_FRAMES}".encode("latin-1"),
                    )
                else:
                    framed = NO_FRAMES not in policy
            if not framed and not any(key.lower() == b"x-frame-options" for key, _ in headers):
                headers.append((b"x-frame-options", b"DENY"))
            message["headers"] = headers
        await send(message)

    return guarded


class SecurityMiddleware:
    """Enforce HTTP request policy before any downstream body consumer."""

    def __init__(
        self,
        app: ASGIApp,
        policy: SecurityPolicy,
        body_limits: Mapping[str, Callable[[], int]],
        machine_scopes: Mapping[str, str] | None = None,
        machine_auth: Callable[[str, str], object | None] | None = None,
        station_slugs: Callable[[], AbstractSet[str]] | None = None,
    ) -> None:
        """Wrap *app*; *body_limits* maps a POST path to its request-body cap in bytes.

        *station_slugs* names the configured stations.  A path under
        ``/api/stations/<slug>/`` for one of them is checked as the default
        station's path: the same body limit, and public only where the
        station's feed is.

        *machine_scopes* maps a path prefix to the machine-token scope its
        routes need.  Those routes take only ``Authorization: Bearer``
        tokens, checked by *machine_auth(token, scope)*, which returns who
        the token belongs to or ``None``; a browser session does not work
        there, and a machine token works nowhere else.  A prefix under
        ``/api/`` also covers the same path under ``/api/stations/<slug>/``,
        whatever the name, so a station route is never a browser route by
        another address.
        """
        self.app = app
        self._policy = policy
        self._body_limits = body_limits
        self._machine_scopes = dict(machine_scopes or {})
        self._machine_auth = machine_auth
        self._station_slugs = station_slugs or frozenset

    def _machine_scope(self, path: str) -> str | None:
        """The token scope *path* needs, or ``None`` for a browser route."""
        relative = station_relative(path, None)
        for prefix, scope_name in self._machine_scopes.items():
            if path.startswith(prefix) or (relative is not None and relative.startswith(prefix)):
                return scope_name
        return None

    async def _machine_rejection(
        self, scope: Scope, needed: str, origin_values: list[str]
    ) -> tuple[int, str] | None:
        """Check a machine request; store its token's owner in the request state."""
        if origin_values:
            # Programs send no Origin; a page in a browser always does.
            return (403, "Machine routes do not accept browser requests")
        if not machine_transport_allowed(scope):
            return (403, "Machine tokens need HTTPS")
        token = bearer_token(scope)
        if token is None or self._machine_auth is None:
            return (401, "Machine token required")
        owner = await asyncio.to_thread(self._machine_auth, token, needed)
        if owner is None:
            return (401, "Invalid machine token")
        scope.setdefault("state", {})["machine_token"] = owner
        return None

    @staticmethod
    def _declared_body_too_large(scope: Scope, cap: int) -> bool:
        """Return whether Content-Length is malformed or exceeds *cap*."""
        values = _raw_header_values(scope, b"content-length")
        if not values:
            return False
        if (
            len(values) != 1
            or len(values[0]) > 20
            or not values[0].isascii()
            or not values[0].isdecimal()
        ):
            return True
        return int(values[0]) > cap

    @staticmethod
    def _capped_receive(receive: Receive, cap: int) -> Receive:
        """Return *receive* raising 413 once the body passes *cap* bytes.

        FastAPI re-raises an HTTPException from body parsing, so the route
        answers 413 before it parses or spools anything past the cap.
        """
        consumed = 0

        async def capped() -> Message:
            """Receive one message and count its body bytes against the cap."""
            nonlocal consumed
            message = await receive()
            if message["type"] == "http.request":
                consumed += len(message.get("body", b""))
                if consumed > cap:
                    raise HTTPException(status_code=413, detail="Request body too large")
            return message

        return capped

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        """Enforce request policy, attach request IDs, and audit unsafe requests."""
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        send = frame_guard(send)
        request_id = new_request_id()
        state = scope.setdefault("state", {})
        state["request_id"] = request_id
        method = str(scope.get("method", "")).upper()
        path = str(scope.get("path", ""))
        route = _route_template(scope)
        policy = self._policy
        slugs = self._station_slugs()
        host_values = _raw_header_values(scope, b"host")
        origin_values = _raw_header_values(scope, b"origin")

        rejection: tuple[int, str] | None = None
        machine_scope = self._machine_scope(path)
        if len(host_values) != 1 or not policy.host_allowed(host_values[0]):
            rejection = (403, "Disallowed Host")
        elif machine_scope is not None:
            rejection = await self._machine_rejection(scope, machine_scope, origin_values)
        elif len(origin_values) > 1 or (
            method in _UNSAFE_METHODS
            and (len(origin_values) != 1 or not policy.origin_allowed(origin_values[0]))
        ):
            rejection = (403, "Disallowed Origin")
        elif policy.authentication_required and not _is_public_path(path, slugs):
            cookie = Request(scope).cookies.get(COOKIE_NAME)
            if not policy.verify_session(cookie):
                rejection = (401, "Authentication required")

        if rejection is not None:
            status, detail = rejection
            headers = {"X-Request-ID": request_id}
            if status == 401 and machine_scope is not None:
                headers["WWW-Authenticate"] = "Bearer"
            response = JSONResponse({"detail": detail}, status_code=status, headers=headers)
            await response(scope, receive, send)
            emit_audit(
                request_id,
                route,
                "rejected",
                method=method,
                route=route,
                status=status,
                level=logging.WARNING,
            )
            return

        # Capped like the default station's route even for a name that is no
        # station: that request answers 404, but only after reading its body.
        limited = station_relative(path, None) or path
        body_limit = self._body_limits.get(limited) if method == "POST" else None
        downstream_receive = receive
        if body_limit is not None:
            cap = body_limit()
            if self._declared_body_too_large(scope, cap):
                await self._reject_body_too_large(scope, receive, send, request_id, method, route)
                return
            downstream_receive = self._capped_receive(receive, cap)

        response_status: int | None = None

        async def send_with_request_id(message: Message) -> None:
            """Record response status and add the request ID response header."""
            nonlocal response_status
            if message["type"] == "http.response.start":
                response_status = int(message["status"])
                headers = [
                    (key, value)
                    for key, value in message.get("headers", [])
                    if key.lower() != b"x-request-id"
                ]
                headers.append((b"x-request-id", request_id.encode("ascii")))
                message["headers"] = headers
            await send(message)

        try:
            await self.app(scope, downstream_receive, send_with_request_id)
        except Exception:
            emit_audit(
                request_id,
                route,
                "error",
                method=method,
                route=route,
                status=response_status if response_status is not None else 500,
                level=logging.ERROR,
            )
            if response_status is not None:
                raise
            response = JSONResponse(
                {"detail": "Internal server error"},
                status_code=500,
                headers={"X-Request-ID": request_id},
            )
            await response(scope, receive, send)
            return
        if method in _UNSAFE_METHODS and response_status is not None:
            emit_audit(
                request_id,
                route,
                "success" if response_status < 400 else "rejected",
                method=method,
                route=route,
                status=response_status,
                level=logging.INFO if response_status < 400 else logging.WARNING,
            )

    @staticmethod
    async def _reject_body_too_large(
        scope: Scope,
        receive: Receive,
        send: Send,
        request_id: str,
        method: str,
        route: str,
    ) -> None:
        """Send and audit a 413 response for a declared oversized request body."""
        response = JSONResponse(
            {"detail": "Request body too large"},
            status_code=413,
            headers={"X-Request-ID": request_id},
        )
        await response(scope, receive, send)
        emit_audit(
            request_id,
            route,
            "rejected",
            method=method,
            route=route,
            status=413,
            level=logging.WARNING,
        )
