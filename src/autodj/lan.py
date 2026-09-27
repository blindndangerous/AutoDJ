"""Local-network mode for ``autodj serve --lan``.

LAN serving needs a Host allow-list, an Origin allow-list and an access token.
This module works them out so nobody has to type them: it finds the names and
addresses of this machine, allows exactly those plus anything configured, and
loads or creates a private access token in the index directory.
"""

from __future__ import annotations

import secrets
import socket
from dataclasses import replace
from typing import TYPE_CHECKING

from autodj.config import (
    canonicalize_allowed_host,
    canonicalize_allowed_origin,
    is_loopback_bind,
    validate_access_token,
)
from autodj.stream_secret import write_private_file

if TYPE_CHECKING:
    from collections.abc import Iterable
    from pathlib import Path

    from autodj.config import ServerConfig

_LOOPBACK_HOSTS = ("localhost", "127.0.0.1", "::1")
# TEST-NET-1 (RFC 5737). Connecting a UDP socket only chooses the outbound
# route; nothing is sent, so this address never has to exist.
_ROUTE_PROBE = ("192.0.2.1", 9)
_WILDCARD_BINDS = frozenset({"0.0.0.0", "::"})  # nosec B104 - comparison only


class AccessTokenError(Exception):
    """The saved access token file exists but cannot be read or written."""


def _canonical(value: str) -> str | None:
    """Return *value* as the Host check compares it, or ``None`` if it is not a valid host."""
    try:
        return canonicalize_allowed_host(value)
    except (TypeError, ValueError):
        return None


def _machine_addresses(hostname: str) -> list[str]:
    """Return the addresses the resolver gives *hostname*, plus the outbound IPv4 address."""
    addresses: list[str] = []
    if hostname:
        try:
            infos = socket.getaddrinfo(hostname, None)
        except (OSError, UnicodeError):
            infos = []
        # Link-local IPv6 comes back with a zone ("fe80::1%eth0"); browsers never send one.
        addresses.extend(str(info[4][0]).partition("%")[0] for info in infos)
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
            probe.connect(_ROUTE_PROBE)
            addresses.append(str(probe.getsockname()[0]))
    except OSError:
        pass
    return addresses


def detect_lan_hosts() -> list[str]:
    """Return the names and addresses other devices may use to reach this machine.

    Covers the hostname, ``<hostname>.local`` for a hostname without a dot, the
    fully qualified name, every non-loopback address the resolver gives the
    hostname, the outbound IPv4 address, and ``localhost``/``127.0.0.1``/``::1``.
    Lookup failures are skipped, and names that are not valid DNS names (for
    example Windows hostnames with underscores) are left out.

    Returns:
        Canonical hosts, sorted and without duplicates.
    """
    try:
        hostname = socket.gethostname().strip()
    except OSError:
        hostname = ""
    candidates: list[str] = []
    if hostname:
        candidates.append(hostname)
        if "." not in hostname:
            candidates.append(f"{hostname}.local")
        candidates.append(socket.getfqdn(hostname))
    candidates.extend(_machine_addresses(hostname))
    detected = {
        host
        for host in map(_canonical, candidates)
        if host is not None and not is_loopback_bind(host)
    }
    return sorted(detected.union(_LOOPBACK_HOSTS))


def lan_origins(hosts: Iterable[str], port: int, *, tls: bool) -> list[str]:
    """Return the browser origins for *hosts* on *port*.

    Args:
        hosts: Canonical or raw host names and addresses.
        port: The port AutoDJ listens on.
        tls: Also allow ``https://`` origins.

    Returns:
        Canonical origins, sorted and without duplicates; IPv6 hosts are bracketed.
    """
    schemes = ("http", "https") if tls else ("http",)
    origins: set[str] = set()
    for host in hosts:
        canonical = canonicalize_allowed_host(host)
        rendered = f"[{canonical}]" if ":" in canonical else canonical
        origins.update(
            canonicalize_allowed_origin(f"{scheme}://{rendered}:{port}") for scheme in schemes
        )
    return sorted(origins)


def read_access_token(path: Path) -> str | None:
    """Return the saved access token, or ``None`` if it is missing or not valid.

    Args:
        path: The token file.

    Raises:
        AccessTokenError: The file exists but cannot be read.
    """
    try:
        text = path.read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        return None
    except (OSError, UnicodeDecodeError) as exc:
        raise AccessTokenError(f"cannot read access token at {path}: {exc}") from exc
    try:
        validate_access_token(text)
    except ValueError:
        return None
    return text


def load_or_create_access_token(path: Path) -> str:
    """Return the saved access token, creating or replacing it when missing or invalid.

    The file is written atomically and readable only by its owner.

    Args:
        path: The token file.

    Raises:
        AccessTokenError: The file cannot be read or written.
    """
    token = read_access_token(path)
    if token is not None:
        return token
    token = secrets.token_urlsafe(32)
    try:
        write_private_file(path, token + "\n")
    except OSError as exc:
        raise AccessTokenError(f"cannot write access token at {path}: {exc}") from exc
    return token


def lan_server_config(
    server: ServerConfig,
    *,
    tls: bool,
    token_path: Path,
    hosts: list[str] | None = None,
) -> ServerConfig:
    """Return *server* set up for local-network use.

    Binds ``0.0.0.0`` unless a non-loopback host was configured, adds the
    detected hosts and their origins to the configured allow-lists, and uses the
    configured access token or else the saved one (none with ``insecure_lan``).

    Args:
        server: The staged server settings.
        tls: TLS is on, so ``https://`` origins are allowed too.
        token_path: Where the saved access token lives.
        hosts: Detected hosts; ``None`` detects them now.

    Raises:
        AccessTokenError: The saved token cannot be read or written.
    """
    detected = detect_lan_hosts() if hosts is None else hosts
    bind = "0.0.0.0" if is_loopback_bind(server.host) else server.host  # nosec B104
    allowed_hosts: list[str] = sorted({*(server.allowed_hosts or []), *detected})
    allowed_origins: list[str] = sorted(
        {*(server.allowed_origins or []), *lan_origins(allowed_hosts, server.port, tls=tls)}
    )
    token = server.access_token
    if token is None and not server.insecure_lan:
        token = load_or_create_access_token(token_path)
    return replace(
        server,
        host=bind,
        lan=True,
        allowed_hosts=allowed_hosts,
        allowed_origins=allowed_origins,
        access_token=token,
    )


def lan_urls(server: ServerConfig, *, tls: bool) -> list[str]:
    """Return the addresses to open from another device.

    Args:
        server: Server settings produced by :func:`lan_server_config`.
        tls: TLS is on.

    Returns:
        One URL per allowed name and IPv4 address, or just the bind address
        when AutoDJ listens on one specific address.
    """
    scheme = "https" if tls else "http"
    if server.host in _WILDCARD_BINDS:
        names = sorted(
            host
            for host in server.effective_allowed_hosts()
            if ":" not in host and not is_loopback_bind(host)
        )
    else:
        names = [server.host]
    return [f"{scheme}://{f'[{name}]' if ':' in name else name}:{server.port}" for name in names]


def format_lan_banner(urls: list[str], pairing: tuple[str, int] | None) -> str:
    """Return the start-up block: where to open AutoDJ and how to pair.

    Args:
        urls: Addresses from :func:`lan_urls`.
        pairing: The current pairing code and the seconds it stays valid, or
            ``None`` when no pairing is needed.

    Returns:
        Multi-line text. It never contains the access token.
    """
    lines = ["Open AutoDJ from another device on your network:"]
    lines.extend(f"  {url}" for url in urls)
    if not urls:
        lines.append("  (no address was detected; use this machine's name or IP address)")
    if pairing is None:
        lines.append(
            "No pairing needed (--insecure-lan): any device on your network can control AutoDJ."
        )
    else:
        code, valid_for = pairing
        lines.append(
            f"Pairing code: {code} (valid for about {valid_for} more seconds). "
            "Enter it once in each browser; `autodj devices pairing-code` prints a fresh one."
        )
    return "\n".join(lines)
