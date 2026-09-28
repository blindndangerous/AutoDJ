"""Local-network mode for ``autodj serve --lan``.

LAN serving needs a Host allow-list, an Origin allow-list and an access token.
This module works them out so nobody has to type them: it finds the names and
addresses of this machine, allows exactly those plus anything configured, and
loads or creates a private access token in the index directory.
"""

from __future__ import annotations

import logging
import socket
import threading
from dataclasses import replace
from pathlib import Path
from typing import TYPE_CHECKING

from autodj.config import (
    canonicalize_allowed_host,
    canonicalize_allowed_origin,
    is_loopback_bind,
    validate_access_token,
)
from autodj.stream_secret import load_or_create_secret, read_secret

if TYPE_CHECKING:
    from collections.abc import Iterable

    from autodj.config import ServerConfig

logger = logging.getLogger(__name__)

_LOOPBACK_HOSTS = ("localhost", "127.0.0.1", "::1")
# TEST-NET-1 (RFC 5737). Connecting a UDP socket only chooses the outbound
# route; nothing is sent, so this address never has to exist.
_ROUTE_PROBE = ("192.0.2.1", 9)
_WILDCARD_BINDS = frozenset({"0.0.0.0", "::"})  # nosec B104 - comparison only
# Resolver calls can each hang for ~30 s on broken DNS; start-up waits this long at most.
LOOKUP_TIMEOUT_SECONDS = 2.0
# Reverse-DNS zones: names here describe addresses, not hosts browsers use.
_REVERSE_DNS_ZONES = (".in-addr.arpa", ".ip6.arpa")
# Files Docker and Podman create inside every container.
CONTAINER_MARKERS = (Path("/.dockerenv"), Path("/run/.containerenv"))


class AccessTokenError(Exception):
    """The saved access token file exists but cannot be read or written."""


def _canonical(value: str) -> str | None:
    """Return *value* as the Host check compares it, or ``None`` if it is not a valid host."""
    try:
        return canonicalize_allowed_host(value)
    except (TypeError, ValueError):
        return None


def _is_own_fqdn(fqdn: str, hostname: str) -> bool:
    """Return whether *fqdn* is a longer form of *hostname*, not a reverse-DNS name.

    A resolver may answer with an ISP name ("host-1-2-3-4.isp.example") or a
    reverse zone ("4.3.2.1.in-addr.arpa", "...ip6.arpa"); neither is a name
    browsers use. Other ``.arpa`` names stay: ``home.arpa`` is the standard
    home-network domain (RFC 8375).
    """
    name = fqdn.strip().lower().removesuffix(".")
    return name.startswith(f"{hostname.lower()}.") and not name.endswith(_REVERSE_DNS_ZONES)


def _resolver_names(hostname: str) -> list[str]:
    """Return the fully qualified name and resolver addresses of *hostname* (may block)."""
    names: list[str] = []
    fqdn = socket.getfqdn(hostname)
    if _is_own_fqdn(fqdn, hostname):
        names.append(fqdn)
    try:
        infos = socket.getaddrinfo(hostname, None)
    except (OSError, UnicodeError):
        infos = []
    # Link-local IPv6 comes back with a zone ("fe80::1%eth0"); browsers never send one.
    names.extend(str(info[4][0]).partition("%")[0] for info in infos)
    return names


def _bounded_resolver_names(hostname: str, timeout: float) -> list[str]:
    """Run :func:`_resolver_names` for at most *timeout* seconds.

    A daemon thread is used rather than an executor, whose shutdown would wait
    for the stuck lookup; the thread is abandoned if it overruns.
    """
    found: list[str] = []
    worker = threading.Thread(
        target=lambda: found.extend(_resolver_names(hostname)),
        name="autodj-lan-lookup",
        daemon=True,
    )
    worker.start()
    worker.join(timeout)
    if worker.is_alive():
        logger.info(
            "Looking up the network names of %s took longer than %g seconds; "
            "using the hostname and route address only.",
            hostname,
            timeout,
        )
        return []
    return list(found)


def _route_address() -> list[str]:
    """Return the IPv4 address of the outbound route, or nothing when there is none."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
            probe.connect(_ROUTE_PROBE)
            return [str(probe.getsockname()[0])]
    except OSError:
        return []


def detect_lan_hosts(*, timeout: float = LOOKUP_TIMEOUT_SECONDS) -> list[str]:
    """Return the names and addresses other devices may use to reach this machine.

    Covers the hostname, ``<hostname>.local`` for a hostname without a dot, the
    fully qualified name when it extends the hostname (not a reverse-DNS
    ``in-addr.arpa``/``ip6.arpa`` name), every non-loopback address the resolver gives the
    hostname, the outbound IPv4 address, and ``localhost``/``127.0.0.1``/``::1``.
    Lookup failures are skipped, and names that are not valid DNS names (for
    example Windows hostnames with underscores) or whose first label is
    ``localhost`` are left out.

    Args:
        timeout: Seconds to wait for the resolver before keeping only the
            hostname, ``.local`` name and route address.

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
        candidates.extend(_bounded_resolver_names(hostname, timeout))
    candidates.extend(_route_address())
    detected = {
        host
        for host in map(_canonical, candidates)
        if host is not None and not is_loopback_bind(host) and host.split(".", 1)[0] != "localhost"
    }
    return sorted(detected.union(_LOOPBACK_HOSTS))


def running_in_container(markers: Iterable[Path] = CONTAINER_MARKERS) -> bool:
    """Return whether AutoDJ runs inside a Docker or Podman container.

    Args:
        markers: Files whose presence means a container.
    """
    return any(marker.exists() for marker in markers)


def lan_bind_host(host: str) -> str:
    """Return the address LAN mode binds: all interfaces unless *host* is a specific LAN address.

    Args:
        host: The configured bind host.
    """
    return "0.0.0.0" if is_loopback_bind(host) else host  # nosec B104 - LAN mode binds all


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


def _is_valid_token(text: str) -> bool:
    """Return whether *text* passes the access-token rules."""
    try:
        validate_access_token(text)
    except ValueError:
        return False
    return True


def read_access_token(path: Path) -> str | None:
    """Return the saved access token, or ``None`` if it is missing or not valid.

    Raises:
        AccessTokenError: The file exists but cannot be read.
    """
    return read_secret(path, valid=_is_valid_token, error=AccessTokenError, what="access token")


def load_or_create_access_token(path: Path) -> str:
    """Return the saved access token, creating or replacing it when missing or invalid.

    Raises:
        AccessTokenError: The file cannot be read or written.
    """
    return load_or_create_secret(
        path, valid=_is_valid_token, error=AccessTokenError, what="access token"
    )


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
    bind = lan_bind_host(server.host)
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


def lan_urls(
    server: ServerConfig,
    *,
    tls: bool,
    in_container: bool = False,
    configured_hosts: Iterable[str] = (),
) -> list[str]:
    """Return the addresses to open from another device.

    Args:
        server: Server settings produced by :func:`lan_server_config`.
        tls: TLS is on.
        in_container: AutoDJ runs in a container, whose own addresses other
            devices cannot reach.
        configured_hosts: The allowed hosts configured before detection was
            merged in (``--allowed-host`` or ``[server] allowed_hosts``).

    Returns:
        In a container, one URL per configured non-loopback host in the order
        given, so the working address comes first. Otherwise one URL per allowed
        name and IPv4 address, or just the bind address when AutoDJ listens on
        one specific address.
    """
    scheme = "https" if tls else "http"
    if in_container:
        canonical = (_canonical(host) for host in configured_hosts)
        names = list(
            dict.fromkeys(
                host for host in canonical if host is not None and not is_loopback_bind(host)
            )
        )
    elif server.host in _WILDCARD_BINDS:
        names = sorted(
            host
            for host in server.effective_allowed_hosts()
            if ":" not in host and not is_loopback_bind(host)
        )
    else:
        names = [server.host]
    return [f"{scheme}://{f'[{name}]' if ':' in name else name}:{server.port}" for name in names]


def format_lan_banner(
    urls: list[str], pairing: tuple[str, int] | None, *, in_container: bool = False
) -> str:
    """Return the start-up block: where to open AutoDJ and how to pair.

    Args:
        urls: Addresses from :func:`lan_urls`, the working one first.
        pairing: The current pairing code and the seconds it stays valid, or
            ``None`` when no pairing is needed.
        in_container: Add a line saying container addresses are unreachable.

    Returns:
        Multi-line text. It never contains the access token.
    """
    lines = ["Open AutoDJ from another device on your network:"]
    lines.extend(f"  {url}" for url in urls)
    if not urls:
        lines.append("  (no address was detected; use this machine's name or IP address)")
    if in_container:
        lines.append("Container addresses are not reachable from your network.")
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
