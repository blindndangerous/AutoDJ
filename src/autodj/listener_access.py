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

from autodj.fsutil import atomic_write, set_aside

logger = logging.getLogger(__name__)
_CLOSED_LISTENER_GRACE = 30.0


class AddressLimitError(Exception):
    """An address already uses its allotted stream connections."""


class BannedAddressError(Exception):
    """An address is banned from the stream."""


class StationFullError(Exception):
    """The station has ``max_listeners`` listeners across all its formats."""


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


# Failed logins in one minute that lock an address out for the rest of it.
_AUTH_FAILURES = 5


class ListenerAuthLimiter:
    """Bound Basic-auth guesses per address in one-minute windows.

    An IPv6 address counts with its whole /64, which one home can use any
    address of, so rotating addresses inside it does not reset the count.
    Only the current minute is kept, and at most ``max_keys`` addresses of
    it, so a flood from many addresses costs bounded memory and constant
    time.  Past that, the address with fewer than five failures that failed
    least recently is forgotten.  A locked address (five failures) is never
    forgotten before the minute ends; when every place holds one, a new
    address counts as locked too, rather than being let in uncounted.
    """

    def __init__(self, clock: Callable[[], float] = time.monotonic, max_keys: int = 4096) -> None:
        self._clock = clock
        self._max_keys = max_keys
        self._lock = threading.Lock()
        self._window = -1
        # key -> failures this window (below the limit), least recently failed first
        self._counting: dict[str, int] = {}
        # keys locked out for the rest of this window
        self._locked: set[str] = set()

    @staticmethod
    def _key(address: str) -> str:
        """*address* itself, or its /64 for IPv6; a key already a network is kept."""
        try:
            parsed = ipaddress.ip_address(address)
        except ValueError:
            return address
        if isinstance(parsed, ipaddress.IPv6Address):
            if parsed.ipv4_mapped is not None:
                return str(parsed.ipv4_mapped)
            return str(ipaddress.IPv6Network((parsed, 64), strict=False))
        return address

    def _roll(self) -> None:
        """Start a new window, forgetting the last one's failures.  Holds the lock."""
        window = int(self._clock() // 60)
        if window != self._window:
            self._window = window
            self._counting.clear()
            self._locked.clear()

    def _full_of_locked(self) -> bool:
        """Every place holds a locked address.  Holds the lock."""
        return len(self._locked) >= self._max_keys

    def blocked(self, address: str) -> bool:
        key = self._key(address)
        with self._lock:
            self._roll()
            if key in self._locked:
                return True
            # Fail closed: an address that cannot be counted is refused.
            return key not in self._counting and self._full_of_locked()

    def failed(self, address: str) -> None:
        key = self._key(address)
        with self._lock:
            self._roll()
            if key in self._locked:
                return
            count = self._counting.pop(key, 0) + 1
            if count >= _AUTH_FAILURES:
                self._locked.add(key)
                return
            if count == 1 and len(self._counting) + len(self._locked) >= self._max_keys:
                if not self._counting:
                    return  # every place is locked: blocked() refuses this address
                del self._counting[next(iter(self._counting))]
            self._counting[key] = count


class ListenerAccess:
    """Own listener admission and persistent address bans."""

    def __init__(
        self,
        path: Path,
        max_per_address: int,
        clock: Callable[[], float] = time.time,
        *,
        max_total: int = 0,
    ) -> None:
        """Load the bans from *path*.

        Args:
            path: The station's ``listener_bans.json``.
            max_per_address: Connections one address may have; 0 for any.
            clock: Wall-clock seconds.
            max_total: Listeners the station may have on all its formats
                (MP3, AAC, Opus) together; 0 for any.
        """
        self.path = path
        self.max_per_address = max_per_address
        self.max_total = max_total
        self._clock = clock
        self._lock = threading.RLock()
        # id -> (address, player, connected at, listener, stream, format)
        self._listeners: dict[str, tuple[str, str, float, Any, Any, str]] = {}
        self._closed_since: dict[str, float] = {}
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
                set_aside(path, "listener bans file", str(exc), naming="timestamp")
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

    def add(
        self, stream: Any, address: str, agent: str, *, icy: bool, fmt: str = "mp3"
    ) -> tuple[str, Any]:
        """Reserve admission before adding a listener outside the registry lock.

        Raises:
            BannedAddressError: The address is banned.
            AddressLimitError: The address has its allotted connections.
            StationFullError: The station has ``max_total`` listeners on
                its formats together.
        """
        address = normalize_address(address)
        self._reclaim_closed()
        with self._lock:
            if self._banned_locked(address):
                raise BannedAddressError(address)
            count = self._pending.get(address, 0) + sum(
                entry[0] == address for entry in self._listeners.values()
            )
            if self.max_per_address and count >= self.max_per_address:
                raise AddressLimitError(address)
            total = sum(self._pending.values()) + len(self._listeners)
            if self.max_total and total >= self.max_total:
                raise StationFullError(address)
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
                        fmt,
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
            self._closed_since.pop(ident, None)
            if entry is not None:
                self.revision += 1
        if entry is not None:
            if immediate:
                entry[3].abort()
            entry[4].remove_listener(entry[3])

    def _reclaim_closed(self) -> None:
        """Release closed listeners whose response did not finish within the grace period."""
        stale = []
        with self._lock:
            now = self._clock()
            for ident, entry in list(self._listeners.items()):
                if not entry[3].closed:
                    self._closed_since.pop(ident, None)
                    continue
                closed_at = getattr(entry[3], "closed_at", None)
                if not isinstance(closed_at, int | float):
                    closed_at = self._closed_since.setdefault(ident, now)
                if now - closed_at >= _CLOSED_LISTENER_GRACE:
                    stale.append(entry)
                    del self._listeners[ident]
                    self._closed_since.pop(ident, None)
                    self.revision += 1
        for entry in stale:
            entry[4].remove_listener(entry[3])

    def listeners(self) -> list[dict[str, Any]]:
        self._reclaim_closed()
        with self._lock:
            return [
                {
                    "id": key,
                    "address": entry[0],
                    "player": entry[1],
                    "connected_at": entry[2],
                    "format": entry[5],
                }
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
