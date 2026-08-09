from __future__ import annotations

import hashlib
import hmac
import ipaddress
import json
import logging
import re
import secrets
import time
import uuid
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


@dataclass
class SecurityPolicy:
    config: ServerConfig
    secure_cookie: bool = False
    now: Callable[[], float] = time.time

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

    def origin_allowed(self, origin: str | None) -> bool:
        try:
            canonical = canonicalize_allowed_origin(origin)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            return False
        return canonical in set(self.config.effective_allowed_origins())


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

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        request_id = new_request_id()
        state = scope.setdefault("state", {})
        state["request_id"] = request_id
        method = str(scope.get("method", "")).upper()
        path = str(scope.get("path", ""))
        route = _route_template(scope)
        host_values = _raw_header_values(scope, b"host")
        origin_values = _raw_header_values(scope, b"origin")

        rejection: tuple[int, str] | None = None
        if len(host_values) != 1 or not self._policy.host_allowed(host_values[0]):
            rejection = (403, "Disallowed Host")
        elif len(origin_values) > 1 or (
            method in _UNSAFE_METHODS
            and (len(origin_values) != 1 or not self._policy.origin_allowed(origin_values[0]))
        ):
            rejection = (403, "Disallowed Origin")
        elif self._policy.authentication_required and not _is_public_path(path):
            cookie = Request(scope).cookies.get(COOKIE_NAME)
            if not self._policy.verify_session(cookie):
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

        await self.app(scope, receive, send_with_request_id)
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
