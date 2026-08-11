from __future__ import annotations

import copy
import hashlib
import hmac
import ipaddress
import json
import logging
import math
import re
import secrets
import threading
import time
import uuid
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass

from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Match
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from autodj.config import ServerConfig, canonicalize_allowed_origin

COOKIE_NAME = "autodj_session"

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
        "/api/version",
        "/api/auth/status",
        "/api/login",
    }
)

_HEX_32 = re.compile(r"[0-9a-f]{32}\Z")
_HEX_64 = re.compile(r"[0-9a-f]{64}\Z")
_CANONICAL_EXPIRY = re.compile(r"(?:0|[1-9][0-9]{0,18})\Z")
_MAX_EXPIRY = 2**63 - 1
_DNS_LABEL = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\Z")
_BRACKETED_HOST = re.compile(r"\[([^\]]+)\](?::([0-9]+))?\Z")
LOGIN_BODY_MAX_BYTES = 4096


@dataclass(frozen=True)
class LoginLimitDecision:
    allowed: bool
    retry_after: int = 0
    audit: bool = False


@dataclass
class _LoginClientState:
    window_started: float
    attempts: int = 0
    blocked_audited: bool = False


class LoginRateLimiter:
    """Fixed-window login limiter with bounded peer state."""

    def __init__(
        self,
        *,
        now: Callable[[], float] = time.monotonic,
        per_client_limit: int = 5,
        global_limit: int = 100,
        window_seconds: int = 60,
        max_clients: int = 1024,
    ) -> None:
        if min(per_client_limit, global_limit, window_seconds, max_clients) < 1:
            raise ValueError("login rate-limit settings must be positive")
        self._now = now
        self._per_client_limit = per_client_limit
        self._global_limit = global_limit
        self._window_seconds = window_seconds
        self._max_clients = max_clients
        self._clients: OrderedDict[str, _LoginClientState] = OrderedDict()
        self._global_started = self._time()
        self._global_attempts = 0
        self._global_blocked_audited = False
        self._lock = threading.Lock()

    def _time(self) -> float:
        value = float(self._now())
        if not math.isfinite(value):
            raise ValueError("login limiter clock returned an invalid value")
        return value

    def _reset_global_if_expired(self, current: float) -> None:
        if current - self._global_started >= self._window_seconds:
            self._global_started = current
            self._global_attempts = 0
            self._global_blocked_audited = False
            self._clients.clear()

    def _retry_after(self, current: float, started: float) -> int:
        return max(1, math.ceil(self._window_seconds - (current - started)))

    def reserve(self, peer: str) -> LoginLimitDecision:
        """Atomically admit and count one login attempt before any body work."""
        with self._lock:
            current = self._time()
            self._reset_global_if_expired(current)
            if self._global_attempts >= self._global_limit:
                audit = not self._global_blocked_audited
                self._global_blocked_audited = True
                return LoginLimitDecision(
                    False, self._retry_after(current, self._global_started), audit
                )

            state = self._clients.get(peer)
            if state is not None and current - state.window_started >= self._window_seconds:
                del self._clients[peer]
                state = None
            if state is not None and state.attempts >= self._per_client_limit:
                self._clients.move_to_end(peer)
                audit = not state.blocked_audited
                state.blocked_audited = True
                return LoginLimitDecision(
                    False, self._retry_after(current, state.window_started), audit
                )

            if state is None:
                state = _LoginClientState(current)
                self._clients[peer] = state
            state.attempts += 1
            self._global_attempts += 1
            self._clients.move_to_end(peer)
            while len(self._clients) > self._max_clients:
                self._clients.popitem(last=False)
            return LoginLimitDecision(True)

    def record_success(self, peer: str) -> None:
        with self._lock:
            self._clients.pop(peer, None)

    @property
    def tracked_clients(self) -> int:
        with self._lock:
            return len(self._clients)


def _parse_host_header(value: object) -> str | None:
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


def _clock_timestamp(now: Callable[[], float]) -> int:
    try:
        value = now()
        if type(value) not in {int, float}:
            raise TypeError
        timestamp = int(value)
    except (OverflowError, TypeError, ValueError):
        raise ValueError("clock returned an invalid timestamp") from None
    if not 0 <= timestamp <= _MAX_EXPIRY:
        raise ValueError("clock returned an invalid timestamp")
    return timestamp


def _is_unspecified_host(host: str) -> bool:
    try:
        return ipaddress.ip_address(host).is_unspecified
    except ValueError:
        return False


@dataclass
class SecurityPolicy:
    config: ServerConfig
    secure_cookie: bool = False
    now: Callable[[], float] = time.time

    def __post_init__(self) -> None:
        """Detach policy decisions from caller-owned mutable configuration."""
        self.config = copy.deepcopy(self.config)

    @property
    def authentication_required(self) -> bool:
        return self.config.access_token is not None

    def verify_access_token(self, candidate: str) -> bool:
        expected = self.config.access_token
        if expected is None:
            return False
        try:
            candidate_bytes = candidate.encode("utf-8")
            expected_bytes = expected.encode("utf-8")
        except (AttributeError, UnicodeEncodeError):
            return False
        return secrets.compare_digest(candidate_bytes, expected_bytes)

    def issue_session(self) -> str:
        token = self.config.access_token
        if token is None:
            raise RuntimeError("access token is not configured")
        expires = _clock_timestamp(self.now) + self.config.session_ttl_seconds
        if not 0 <= expires <= _MAX_EXPIRY:
            raise RuntimeError("session expiry is outside the supported range")
        nonce = secrets.token_hex(16)
        payload = f"{expires}.{nonce}"
        signature = hmac.new(
            token.encode("utf-8"), payload.encode("ascii"), hashlib.sha256
        ).hexdigest()
        return f"{payload}.{signature}"

    def verify_session(self, value: str | None) -> bool:
        token = self.config.access_token
        if token is None or not isinstance(value, str):
            return False
        parts = value.split(".")
        if len(parts) != 3:
            return False
        expires_text, nonce, signature = parts
        if (
            _CANONICAL_EXPIRY.fullmatch(expires_text) is None
            or _HEX_32.fullmatch(nonce) is None
            or _HEX_64.fullmatch(signature) is None
        ):
            return False
        expires = int(expires_text)
        if expires > _MAX_EXPIRY:
            return False
        payload = f"{expires_text}.{nonce}"
        expected = hmac.new(
            token.encode("utf-8"), payload.encode("ascii"), hashlib.sha256
        ).hexdigest()
        signature_valid = secrets.compare_digest(
            signature.encode("ascii"), expected.encode("ascii")
        )
        try:
            current_time = _clock_timestamp(self.now)
        except ValueError:
            return False
        return signature_valid and expires >= current_time

    def host_allowed(self, host_header: str | None) -> bool:
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
        try:
            canonical = canonicalize_allowed_origin(origin)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            return False
        return canonical in set(self.effective_allowed_origins())


def new_request_id() -> str:
    return uuid.uuid4().hex


def audit_record(
    request_id: str,
    action: str,
    outcome: str,
    method: str | None = None,
    route: str | None = None,
    status: int | None = None,
) -> str:
    for field_name, required_value in (
        ("request_id", request_id),
        ("action", action),
        ("outcome", outcome),
    ):
        if type(required_value) is not str:
            raise TypeError(f"{field_name} must be a string")
    for field_name, optional_value in (("method", method), ("route", route)):
        if optional_value is not None and type(optional_value) is not str:
            raise TypeError(f"{field_name} must be a string or None")
    if status is not None:
        if type(status) is not int:
            raise TypeError("status must be an integer or None")
        if not (100 <= status <= 599 or 1000 <= status <= 4999):
            raise ValueError("status must be a valid HTTP status or WebSocket code")

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
    if not path.startswith(prefix):
        return False
    suffix = path[len(prefix) :]
    if not suffix or "\\" in suffix:
        return False
    return all(part not in {"", ".", ".."} for part in suffix.split("/"))


def _is_public_path(path: str) -> bool:
    return (
        path in _PUBLIC_FILES
        or _safe_public_prefix(path, "/static/")
        or _safe_public_prefix(path, "/modules/")
    )


def _route_template(scope: Scope) -> str:
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
    return [
        value.decode("latin-1") for key, value in scope.get("headers", ()) if key.lower() == name
    ]


class SecurityMiddleware:
    """Enforce HTTP request policy before any downstream body consumer."""

    def __init__(self, app: ASGIApp, policy: SecurityPolicy) -> None:
        self.app = app
        self._policy = policy

    def _current_policy(self, scope: Scope) -> SecurityPolicy:
        app = scope.get("app")
        state = getattr(app, "state", None)
        return getattr(state, "security_policy", self._policy)

    @staticmethod
    def _login_limiter(scope: Scope) -> LoginRateLimiter | None:
        app = scope.get("app")
        state = getattr(app, "state", None)
        limiter = getattr(state, "login_rate_limiter", None)
        return limiter if isinstance(limiter, LoginRateLimiter) else None

    @staticmethod
    def _peer(scope: Scope) -> str:
        client = scope.get("client")
        if isinstance(client, (tuple, list)) and client:
            return str(client[0])[:128]
        return "<unknown>"

    @staticmethod
    def _declared_login_body_too_large(scope: Scope) -> bool:
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
        return int(values[0]) > LOGIN_BODY_MAX_BYTES

    @staticmethod
    async def _buffer_login_body(receive: Receive) -> tuple[bytes, bool]:
        body = bytearray()
        while True:
            message = await receive()
            if message["type"] != "http.request":
                return bytes(body), True
            chunk = message.get("body", b"")
            if not isinstance(chunk, bytes):
                raise ValueError("invalid ASGI request body")
            if len(chunk) > LOGIN_BODY_MAX_BYTES - len(body):
                raise _LoginBodyTooLarge
            body.extend(chunk)
            if not message.get("more_body", False):
                return bytes(body), False

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        request_id = new_request_id()
        state = scope.setdefault("state", {})
        state["request_id"] = request_id
        method = str(scope.get("method", "")).upper()
        path = str(scope.get("path", ""))
        is_login = method == "POST" and path == "/api/login"
        route = _route_template(scope)
        policy = self._current_policy(scope)
        host_values = _raw_header_values(scope, b"host")
        origin_values = _raw_header_values(scope, b"origin")

        rejection: tuple[int, str] | None = None
        if len(host_values) != 1 or not policy.host_allowed(host_values[0]):
            rejection = (403, "Disallowed Host")
        elif len(origin_values) > 1 or (
            method in _UNSAFE_METHODS
            and (len(origin_values) != 1 or not policy.origin_allowed(origin_values[0]))
        ):
            rejection = (403, "Disallowed Origin")
        elif policy.authentication_required and not _is_public_path(path):
            cookie = Request(scope).cookies.get(COOKIE_NAME)
            if not policy.verify_session(cookie):
                rejection = (401, "Authentication required")

        if rejection is not None:
            status, detail = rejection
            response = JSONResponse(
                {"detail": detail},
                status_code=status,
                headers={"X-Request-ID": request_id},
            )
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

        login_limiter = self._login_limiter(scope) if is_login else None
        peer = self._peer(scope)
        if login_limiter is not None:
            decision = login_limiter.reserve(peer)
            if not decision.allowed:
                response = JSONResponse(
                    {"detail": "Too many login attempts"},
                    status_code=429,
                    headers={
                        "X-Request-ID": request_id,
                        "Retry-After": str(decision.retry_after),
                    },
                )
                await response(scope, receive, send)
                if decision.audit:
                    emit_audit(
                        request_id,
                        route,
                        "rejected",
                        method=method,
                        route=route,
                        status=429,
                        level=logging.WARNING,
                    )
                return

        if is_login:
            if self._declared_login_body_too_large(scope):
                await self._reject_login_body(scope, receive, send, request_id, method, route)
                return
            try:
                body, disconnected = await self._buffer_login_body(receive)
            except (_LoginBodyTooLarge, ValueError):
                await self._reject_login_body(scope, receive, send, request_id, method, route)
                return
            replayed = False

            async def receive_login_body() -> Message:
                nonlocal replayed
                if replayed:
                    return await receive()
                replayed = True
                if disconnected:
                    return {"type": "http.disconnect"}
                return {"type": "http.request", "body": body, "more_body": False}

            downstream_receive: Receive = receive_login_body
        else:
            downstream_receive = receive

        response_status: int | None = None

        async def send_with_request_id(message: Message) -> None:
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
        if (
            login_limiter is not None
            and response_status is not None
            and 200 <= response_status < 300
        ):
            login_limiter.record_success(peer)
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
    async def _reject_login_body(
        scope: Scope,
        receive: Receive,
        send: Send,
        request_id: str,
        method: str,
        route: str,
    ) -> None:
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


class _LoginBodyTooLarge(Exception):
    pass
