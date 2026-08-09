from __future__ import annotations

import hashlib
import hmac
import ipaddress
import json
import re
import secrets
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass

from autodj.config import ServerConfig, canonicalize_allowed_origin

COOKIE_NAME = "autodj_session"

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
        if not port_text or not port_text.isascii() or not port_text.isdecimal():
            return None
        port = int(port_text)
        if not 1 <= port <= 65535:
            return None
    return canonical


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
        expires = int(self.now()) + self.config.session_ttl_seconds
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
        return signature_valid and expires >= int(self.now())

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
    return json.dumps(record, separators=(",", ":"), sort_keys=True)
