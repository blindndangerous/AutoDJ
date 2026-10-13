"""Admission, identity and operator controls for direct radio listeners."""

from __future__ import annotations

import ipaddress
import json
import logging
import math
import secrets
import threading
import time
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

from autodj.fsutil import atomic_write

logger = logging.getLogger(__name__)


class AddressLimitError(Exception):
    """An address already uses its allotted stream connections."""


class BannedAddressError(Exception):
    """An address is banned from the stream."""


def normalize_address(value: str) -> str:
    """Canonicalize a socket or uvicorn-forwarded client address."""
    address = ipaddress.ip_address(value)
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped:
        address = address.ipv4_mapped
    return str(address)


def _network(value: str) -> str:
    network = ipaddress.ip_network(value, strict=False)
    if (
        isinstance(network, ipaddress.IPv6Network)
        and network.network_address.ipv4_mapped
        and network.prefixlen >= 96
    ):
        return str(
            ipaddress.IPv4Network(
                (network.network_address.ipv4_mapped, network.prefixlen - 96), strict=False
            )
        )
    return str(network)


def proxy_allowlist(trusted: list[str]) -> list[str]:
    """Include mapped IPv6 peers for configured IPv4 proxy networks."""
    result: list[str] = []
    for item in trusted:
        network = ipaddress.ip_network(_network(item), strict=False)
        result.append(str(network))
        if isinstance(network, ipaddress.IPv4Network):
            mapped = int(ipaddress.IPv6Address("::ffff:0:0")) + int(network.network_address)
            result.append(str(ipaddress.IPv6Network((mapped, network.prefixlen + 96))))
    return result


#: The proxies trusted when ``[stream] trusted_proxies`` is empty: one on
#: this machine, as Uvicorn trusts by default.  A TLS reverse proxy on the
#: same host then still makes its requests HTTPS (#88).
LOOPBACK_PROXIES: tuple[str, ...] = ("127.0.0.1/32", "::1/128")


def trusted_proxy_setting(
    trusted: list[str], environ: Mapping[str, str]
) -> tuple[list[str] | str, list[str]]:
    """What Uvicorn trusts forwarded headers from, and warnings to log at startup.

    ``[stream] trusted_proxies`` wins; without it, an existing
    ``FORWARDED_ALLOW_IPS`` environment variable (Uvicorn's own setting) is
    honoured as it is; without either, only a proxy on this machine.  Each
    trusted peer that is not this machine gets a warning: AutoDJ believes
    the client address and scheme it reports, so it must be a proxy that
    appends or overwrites ``X-Forwarded-For``, and it must terminate TLS
    (and say so with ``X-Forwarded-Proto: https``) before machine tokens
    are accepted through it.
    """
    warnings: list[str] = []
    from_env = environ.get("FORWARDED_ALLOW_IPS", "").strip()
    allow: list[str] | str
    if trusted:
        allow = forwarded_allow_ips(trusted)
        entries = list(trusted)
        if from_env:
            warnings.append(
                "Ignoring FORWARDED_ALLOW_IPS: [stream] trusted_proxies is set and is used instead."
            )
    elif from_env:
        allow = from_env
        entries = [item.strip() for item in from_env.split(",") if item.strip()]
    else:
        return forwarded_allow_ips([]), warnings
    for entry in entries:
        try:
            loopback = ipaddress.ip_network(entry, strict=False).is_loopback
        except ValueError:
            loopback = False  # "*" or a name: not provably this machine
        if not loopback:
            warnings.append(
                f"Trusting X-Forwarded-For and X-Forwarded-Proto from {entry}. It must append or "
                "overwrite X-Forwarded-For (never pass a client's own header through), and it "
                "must terminate TLS and send X-Forwarded-Proto: https before machine tokens are "
                "accepted through it."
            )
    return allow, warnings


def forwarded_allow_ips(trusted: list[str]) -> list[str]:
    """The peers whose ``X-Forwarded-*`` headers the server believes.

    The configured ``[stream] trusted_proxies``, or this machine when the
    list is empty.
    """
    return proxy_allowlist(list(trusted) or list(LOOPBACK_PROXIES))


def player_name(user_agent: str) -> str:
    """Give the operator a short printable player label."""
    clean = "".join(char if char.isprintable() else " " for char in user_agent)
    clean = " ".join(clean.split())[:80]
    return clean or "Unknown player"


class ListenerAuthLimiter:
    """Bound Basic-auth guesses per address in one-minute windows."""

    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self._lock = threading.Lock()
        self._failures: dict[str, tuple[int, int]] = {}

    def blocked(self, address: str) -> bool:
        with self._lock:
            window, count = self._failures.get(address, (-1, 0))
            return window == int(self._clock() // 60) and count >= 5

    def failed(self, address: str) -> None:
        with self._lock:
            window = int(self._clock() // 60)
            if len(self._failures) > 4096:
                self._failures = {
                    key: value for key, value in self._failures.items() if value[0] == window
                }
            old_window, count = self._failures.get(address, (-1, 0))
            self._failures[address] = (window, count + 1 if old_window == window else 1)


class ListenerAccess:
    """Own listener admission and persistent address bans."""

    def __init__(
        self,
        path: Path,
        max_per_address: int,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.path = path
        self.max_per_address = max_per_address
        self._clock = clock
        self._lock = threading.RLock()
        self._listeners: dict[str, tuple[str, str, float, Any, Any]] = {}
        self._pending: dict[str, int] = {}
        self._bans: dict[str, dict[str, Any]] = {}
        self.revision = 0
        if path.exists():
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                if not isinstance(data, dict) or not isinstance(data.get("bans"), list):
                    raise ValueError("invalid bans file")
                for item in data["bans"]:
                    if not isinstance(item, dict):
                        raise TypeError("ban must be an object")
                    network = _network(item["address"])
                    expires = item["expires_at"]
                    note = item["note"]
                    invalid_expiry = expires is not None and (
                        isinstance(expires, bool)
                        or not isinstance(expires, int | float)
                        or not math.isfinite(expires)
                    )
                    if invalid_expiry or not isinstance(note, str):
                        raise TypeError("invalid ban fields")
                    self._bans[network] = {"address": network, "expires_at": expires, "note": note}
            except (OSError, ValueError, TypeError, KeyError) as exc:
                logger.error(
                    "Cannot load listener bans from %s: %s; starting with no bans", path, exc
                )
                self._bans.clear()
                bad = path.with_name(f"{path.name}.{time.time_ns()}.bad")
                try:
                    path.replace(bad)
                except OSError:
                    logger.error(
                        "Cannot move corrupt listener bans file %s aside", path, exc_info=True
                    )
        self.prune_expired()

    def _save(self) -> None:
        atomic_write(self.path, json.dumps({"bans": list(self._bans.values())}, indent=2) + "\n")

    def prune_expired(self) -> None:
        """Persist expired-ban removal; call from a worker thread."""
        with self._lock:
            self._prune_locked()

    def _prune_locked(self) -> None:
        expired = [
            key
            for key, ban in self._bans.items()
            if ban["expires_at"] is not None and ban["expires_at"] <= self._clock()
        ]
        for key in expired:
            del self._bans[key]
        if expired:
            self.revision += 1
            try:
                self._save()
            except OSError:
                logger.warning("Could not persist expired listener bans", exc_info=True)

    def banned(self, address: str) -> bool:
        with self._lock:
            return self._banned_locked(address)

    def _banned_locked(self, address: str) -> bool:
        try:
            ip = ipaddress.ip_address(normalize_address(address))
        except ValueError:
            return False
        now = self._clock()
        return any(
            (ban["expires_at"] is None or ban["expires_at"] > now)
            and ip in ipaddress.ip_network(key)
            for key, ban in self._bans.items()
        )

    def add(self, stream: Any, address: str, agent: str, *, icy: bool) -> tuple[str, Any]:
        """Reserve admission before adding a listener outside the registry lock."""
        address = normalize_address(address)
        with self._lock:
            if self._banned_locked(address):
                raise BannedAddressError(address)
            count = self._pending.get(address, 0) + sum(
                entry[0] == address and not entry[3].closed for entry in self._listeners.values()
            )
            if self.max_per_address and count >= self.max_per_address:
                raise AddressLimitError(address)
            self._pending[address] = self._pending.get(address, 0) + 1
        try:
            listener = stream.add_listener(icy=icy)
            banned = False
            with self._lock:
                banned = self._banned_locked(address)
                if not banned:
                    ident = secrets.token_urlsafe(12)
                    self._listeners[ident] = (
                        address,
                        player_name(agent),
                        self._clock(),
                        listener,
                        stream,
                    )
                    self.revision += 1
            if banned:
                listener.abort()
                stream.remove_listener(listener)
                raise BannedAddressError(address)
            return ident, listener
        finally:
            with self._lock:
                self._pending[address] -= 1
                if not self._pending[address]:
                    del self._pending[address]

    def remove(self, ident: str, *, immediate: bool = False) -> None:
        with self._lock:
            entry = self._listeners.pop(ident, None)
            if entry is not None:
                self.revision += 1
        if entry is not None:
            if immediate:
                entry[3].abort()
            entry[4].remove_listener(entry[3])

    def listeners(self) -> list[dict[str, Any]]:
        with self._lock:
            stale = [key for key, entry in self._listeners.items() if entry[3].closed]
            for key in stale:
                del self._listeners[key]
            if stale:
                self.revision += 1
            return [
                {"id": key, "address": entry[0], "player": entry[1], "connected_at": entry[2]}
                for key, entry in self._listeners.items()
            ]

    def kick(self, ident: str) -> str | None:
        with self._lock:
            entry = self._listeners.get(ident)
        if entry is None:
            return None
        self.remove(ident, immediate=True)
        return entry[0]

    def bans(self) -> list[dict[str, Any]]:
        with self._lock:
            now = self._clock()
            return [
                ban
                for ban in self._bans.values()
                if ban["expires_at"] is None or ban["expires_at"] > now
            ]

    def ban(self, address: str, minutes: int | None, note: str = "") -> dict[str, Any]:
        network = _network(address)
        if minutes is not None and minutes <= 0:
            raise ValueError("ban duration must be positive")
        with self._lock:
            ban = {
                "address": network,
                "expires_at": None if minutes is None else self._clock() + minutes * 60,
                "note": "".join(char if char.isprintable() else " " for char in note.strip())[:200],
            }
            previous = self._bans.get(network)
            self._bans[network] = ban
            try:
                self._save()
            except OSError:
                self._bans.pop(network)
                if previous is not None:
                    self._bans[network] = previous
                raise
            self.revision += 1
            victims = [
                key for key, entry in self._listeners.items() if self._banned_locked(entry[0])
            ]
        for key in victims:
            self.remove(key, immediate=True)
        return ban

    def unban(self, address: str) -> bool:
        network = _network(address)
        with self._lock:
            if network not in self._bans:
                return False
            previous = self._bans.pop(network)
            try:
                self._save()
            except OSError:
                self._bans[network] = previous
                raise
            self.revision += 1
            return True
