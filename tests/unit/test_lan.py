"""Local-network mode: host detection, origins, the saved token and staging."""

from __future__ import annotations

import os
import socket
import stat
from pathlib import Path
from typing import Any, ClassVar

import pytest

from autodj import lan
from autodj.config import ServerConfig, validate_server_exposure
from autodj.lan import (
    AccessTokenError,
    detect_lan_hosts,
    format_lan_banner,
    lan_origins,
    lan_server_config,
    lan_urls,
    load_or_create_access_token,
    read_access_token,
)

_LOOPBACK = ["127.0.0.1", "::1", "localhost"]


class _FakeUdpSocket:
    """Stand-in for the UDP socket; records the connect target, sends nothing."""

    connected: ClassVar[list[tuple[str, int]]] = []

    def __init__(self, address: str | None = "192.168.1.20", error: bool = False) -> None:
        self._address = address
        self._error = error

    def __call__(self, *args: Any) -> _FakeUdpSocket:
        return self

    def __enter__(self) -> _FakeUdpSocket:
        return self

    def __exit__(self, *exc: object) -> None:
        return None

    def connect(self, target: tuple[str, int]) -> None:
        if self._error:
            raise OSError("network unreachable")
        type(self).connected.append(target)

    def getsockname(self) -> tuple[str, int]:
        return (self._address or "0.0.0.0", 40000)


def _addrinfo(*addresses: str) -> list[tuple[Any, ...]]:
    out: list[tuple[Any, ...]] = []
    for address in addresses:
        if ":" in address:
            out.append((socket.AF_INET6, socket.SOCK_STREAM, 6, "", (address, 0, 0, 0)))
        else:
            out.append((socket.AF_INET, socket.SOCK_STREAM, 6, "", (address, 0)))
    return out


def _patch_socket(
    monkeypatch: pytest.MonkeyPatch,
    *,
    hostname: str | Exception = "NAS",
    fqdn: str = "NAS",
    addresses: tuple[str, ...] | Exception = (),
    udp: _FakeUdpSocket | None = None,
) -> None:
    def gethostname() -> str:
        if isinstance(hostname, Exception):
            raise hostname
        return hostname

    def getaddrinfo(host: str, port: object) -> list[tuple[Any, ...]]:
        if isinstance(addresses, Exception):
            raise addresses
        return _addrinfo(*addresses)

    monkeypatch.setattr(socket, "gethostname", gethostname)
    monkeypatch.setattr(socket, "getfqdn", lambda name="": fqdn)
    monkeypatch.setattr(socket, "getaddrinfo", getaddrinfo)
    monkeypatch.setattr(socket, "socket", udp or _FakeUdpSocket(error=True))


# ---------------------------------------------------------------------------
# detect_lan_hosts
# ---------------------------------------------------------------------------


def test_detects_hostname_mdns_name_and_addresses(monkeypatch: pytest.MonkeyPatch) -> None:
    _FakeUdpSocket.connected = []
    _patch_socket(
        monkeypatch,
        hostname="NAS",
        addresses=("192.168.1.20", "127.0.1.1", "FE80::1%eth0", "2001:DB8::5"),
        udp=_FakeUdpSocket("10.0.0.7"),
    )

    hosts = detect_lan_hosts()

    assert hosts == sorted(
        [
            "10.0.0.7",
            "192.168.1.20",
            "2001:db8::5",
            "fe80::1",
            "nas",
            "nas.local",
            *_LOOPBACK,
        ]
    )
    # The UDP trick only picks a route; it never sends to a real host.
    assert _FakeUdpSocket.connected == [("192.0.2.1", 9)]


def test_dotted_hostname_gets_no_mdns_suffix(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_socket(monkeypatch, hostname="nas.home.arpa", fqdn="nas.home.arpa")

    assert detect_lan_hosts() == sorted(["nas.home.arpa", *_LOOPBACK])


def test_distinct_fqdn_is_added(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_socket(monkeypatch, hostname="nas", fqdn="nas.example.lan")

    assert detect_lan_hosts() == sorted(["nas", "nas.local", "nas.example.lan", *_LOOPBACK])


def test_fqdn_of_localhost_is_ignored(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_socket(monkeypatch, hostname="nas", fqdn="localhost")

    assert detect_lan_hosts() == sorted(["nas", "nas.local", *_LOOPBACK])


def test_failures_fall_back_to_loopback_names(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_socket(
        monkeypatch,
        hostname=OSError("no hostname"),
        fqdn="",
        addresses=socket.gaierror("no resolver"),
    )

    assert detect_lan_hosts() == sorted(_LOOPBACK)


def test_invalid_names_and_wildcard_route_are_skipped(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_socket(
        monkeypatch,
        hostname="DESKTOP_1",  # underscores are not valid DNS labels
        fqdn="DESKTOP_1",
        addresses=("0.0.0.0", "10.1.2.3"),
        udp=_FakeUdpSocket("0.0.0.0"),
    )

    assert detect_lan_hosts() == sorted(["10.1.2.3", *_LOOPBACK])


def test_resolver_unicode_error_is_swallowed(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_socket(monkeypatch, hostname="nas", addresses=UnicodeError("idna"))

    assert detect_lan_hosts() == sorted(["nas", "nas.local", *_LOOPBACK])


# ---------------------------------------------------------------------------
# lan_origins
# ---------------------------------------------------------------------------


def test_origins_cover_each_host_with_brackets_for_ipv6() -> None:
    assert lan_origins(["nas", "192.168.1.20", "fe80::1"], 8080, tls=False) == [
        "http://192.168.1.20:8080",
        "http://[fe80::1]:8080",
        "http://nas:8080",
    ]


def test_tls_adds_https_origins() -> None:
    assert lan_origins(["nas"], 8443, tls=True) == [
        "http://nas:8443",
        "https://nas:8443",
    ]


def test_default_ports_are_canonicalised_without_duplicates() -> None:
    assert lan_origins(["nas", "NAS"], 80, tls=False) == ["http://nas"]


# ---------------------------------------------------------------------------
# Saved access token
# ---------------------------------------------------------------------------


def test_token_is_created_then_reused(tmp_path: Path) -> None:
    path = tmp_path / "index" / ".access-token"

    first = load_or_create_access_token(path)

    assert len(first.encode("utf-8")) >= 32
    assert path.read_text(encoding="utf-8").strip() == first
    assert load_or_create_access_token(path) == first
    assert read_access_token(path) == first


@pytest.mark.skipif(os.name == "nt", reason="POSIX permissions")
def test_token_file_is_private(tmp_path: Path) -> None:
    path = tmp_path / ".access-token"
    load_or_create_access_token(path)
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


@pytest.mark.parametrize("content", ["", "  \n", "too-short"])
def test_invalid_token_file_is_regenerated(tmp_path: Path, content: str) -> None:
    path = tmp_path / ".access-token"
    path.write_text(content, encoding="utf-8")

    assert read_access_token(path) is None
    token = load_or_create_access_token(path)

    assert len(token.encode("utf-8")) >= 32
    assert path.read_text(encoding="utf-8").strip() == token


def test_missing_token_file_reads_as_none(tmp_path: Path) -> None:
    assert read_access_token(tmp_path / ".access-token") is None


def test_unreadable_token_file_fails_clearly(tmp_path: Path) -> None:
    path = tmp_path / ".access-token"
    path.mkdir()

    with pytest.raises(AccessTokenError, match="cannot read access token"):
        load_or_create_access_token(path)


def test_undecodable_token_file_fails_clearly(tmp_path: Path) -> None:
    path = tmp_path / ".access-token"
    path.write_bytes(b"\xff\xfe" * 40)

    with pytest.raises(AccessTokenError, match="cannot read access token"):
        read_access_token(path)


def test_unwritable_token_file_fails_clearly(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def refuse(path: Path, text: str) -> None:
        raise PermissionError("read-only volume")

    monkeypatch.setattr("autodj.stream_secret.write_private_file", refuse)

    with pytest.raises(AccessTokenError, match="cannot write access token"):
        load_or_create_access_token(tmp_path / ".access-token")


# ---------------------------------------------------------------------------
# lan_server_config
# ---------------------------------------------------------------------------

_DETECTED = ["127.0.0.1", "192.168.1.20", "::1", "localhost", "nas", "nas.local"]


def test_lan_binds_wildcard_and_merges_lists(tmp_path: Path) -> None:
    server = ServerConfig(
        allowed_hosts=["radio.example"],
        allowed_origins=["https://radio.example"],
    )

    staged = lan_server_config(
        server, tls=False, token_path=tmp_path / ".access-token", hosts=_DETECTED
    )

    assert staged.host == "0.0.0.0"
    assert staged.lan is True
    assert staged.allowed_hosts == sorted([*_DETECTED, "radio.example"])
    assert staged.allowed_origins is not None
    assert "https://radio.example" in staged.allowed_origins
    assert "http://radio.example:8080" in staged.allowed_origins
    assert "http://192.168.1.20:8080" in staged.allowed_origins
    assert "http://[::1]:8080" in staged.allowed_origins
    assert staged.access_token == (tmp_path / ".access-token").read_text().strip()
    validate_server_exposure(staged)


def test_lan_keeps_an_explicit_non_loopback_bind(tmp_path: Path) -> None:
    server = ServerConfig(host="192.168.1.20", port=9000)

    staged = lan_server_config(
        server, tls=True, token_path=tmp_path / ".access-token", hosts=_DETECTED
    )

    assert staged.host == "192.168.1.20"
    assert staged.port == 9000
    assert "https://nas:9000" in (staged.allowed_origins or [])
    validate_server_exposure(staged)


def test_explicit_token_wins_over_saved_file(tmp_path: Path) -> None:
    token_path = tmp_path / ".access-token"
    token_path.write_text("f" * 40, encoding="utf-8")

    staged = lan_server_config(
        ServerConfig(access_token="e" * 32), tls=False, token_path=token_path, hosts=_DETECTED
    )

    assert staged.access_token == "e" * 32


def test_insecure_lan_skips_the_token(tmp_path: Path) -> None:
    token_path = tmp_path / ".access-token"

    staged = lan_server_config(
        ServerConfig(insecure_lan=True), tls=False, token_path=token_path, hosts=_DETECTED
    )

    assert staged.access_token is None
    assert not token_path.exists()
    validate_server_exposure(staged)


def test_lan_detects_hosts_when_none_are_given(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(lan, "detect_lan_hosts", lambda: ["nas", *_LOOPBACK])

    staged = lan_server_config(ServerConfig(), tls=False, token_path=tmp_path / ".access-token")

    assert staged.allowed_hosts == sorted(["nas", *_LOOPBACK])


# ---------------------------------------------------------------------------
# Start-up block
# ---------------------------------------------------------------------------


def test_urls_list_names_and_ipv4_but_not_loopback_or_ipv6() -> None:
    server = ServerConfig(
        host="0.0.0.0",
        lan=True,
        access_token="t" * 32,
        allowed_hosts=[*_DETECTED, "fe80::1"],
        allowed_origins=["http://nas:8080"],
    )

    assert lan_urls(server, tls=False) == [
        "http://192.168.1.20:8080",
        "http://nas:8080",
        "http://nas.local:8080",
    ]
    assert lan_urls(server, tls=True)[0] == "https://192.168.1.20:8080"


def test_urls_use_only_the_bind_address_when_it_is_specific() -> None:
    server = ServerConfig(
        host="192.168.1.20",
        lan=True,
        access_token="t" * 32,
        allowed_hosts=_DETECTED,
        allowed_origins=["http://nas:8080"],
    )

    assert lan_urls(server, tls=False) == ["http://192.168.1.20:8080"]


def test_banner_lists_addresses_and_pairing_code() -> None:
    text = format_lan_banner(["http://nas:8080", "http://192.168.1.20:8080"], ("12345678", 540))

    assert "http://nas:8080" in text
    assert "http://192.168.1.20:8080" in text
    assert "12345678" in text
    assert "540 more seconds" in text


def test_banner_without_pairing_says_no_code_is_needed() -> None:
    text = format_lan_banner(["http://nas:8080"], None)

    assert "http://nas:8080" in text
    assert "no pairing" in text.lower()


def test_banner_handles_no_detected_address() -> None:
    text = format_lan_banner([], ("12345678", 60))

    assert "this machine's name or IP address" in text


# ---------------------------------------------------------------------------
# Config field and path helper
# ---------------------------------------------------------------------------


def test_server_lan_setting_is_read_and_type_checked() -> None:
    assert ServerConfig.from_dict({}).lan is False
    assert ServerConfig.from_dict({"lan": True}).lan is True
    with pytest.raises(TypeError, match=r"server.lan must be a boolean"):
        ServerConfig(lan="yes")  # type: ignore[arg-type]


def test_access_token_path_sits_beside_paired_devices(tmp_path: Path) -> None:
    from types import SimpleNamespace

    from autodj.stream_secret import access_token_path, paired_devices_path

    cfg: Any = SimpleNamespace(index=SimpleNamespace(index_dir=tmp_path))

    assert access_token_path(cfg) == tmp_path / ".access-token"
    assert access_token_path(cfg).parent == paired_devices_path(cfg).parent


# ---------------------------------------------------------------------------
# Review fixes: bounded lookups, FQDN filter, containers, bind rule
# ---------------------------------------------------------------------------


def test_isp_reverse_name_is_not_taken_as_fqdn(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_socket(monkeypatch, hostname="nas", fqdn="host-10-0-0-7.isp.example")

    assert detect_lan_hosts() == sorted(["nas", "nas.local", *_LOOPBACK])


@pytest.mark.parametrize(
    "fqdn",
    [
        "4.3.2.1.in-addr.arpa",
        "nas.7.0.0.10.in-addr.arpa.",
        "nas.1.0.0.0.0.0.0.0.8.b.d.0.1.0.0.2.IP6.ARPA",
    ],
)
def test_reverse_dns_fqdn_is_ignored(monkeypatch: pytest.MonkeyPatch, fqdn: str) -> None:
    _patch_socket(monkeypatch, hostname="nas", fqdn=fqdn)

    assert detect_lan_hosts() == sorted(["nas", "nas.local", *_LOOPBACK])


def test_home_arpa_fqdn_is_kept(monkeypatch: pytest.MonkeyPatch) -> None:
    # RFC 8375: home.arpa is the standard home-network domain many routers use.
    _patch_socket(monkeypatch, hostname="nas", fqdn="NAS.home.arpa.")

    assert detect_lan_hosts() == sorted(["nas", "nas.local", "nas.home.arpa", *_LOOPBACK])


def test_valid_fqdn_with_trailing_dot_is_accepted(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_socket(monkeypatch, hostname="Host", fqdn="host.example.lan.")

    assert detect_lan_hosts() == sorted(["host", "host.local", "host.example.lan", *_LOOPBACK])


@pytest.mark.parametrize("hostname", ["localhost.localdomain", "localhost"])
def test_names_starting_with_localhost_are_skipped(
    monkeypatch: pytest.MonkeyPatch, hostname: str
) -> None:
    _patch_socket(monkeypatch, hostname=hostname, fqdn="localhost.localdomain")

    assert detect_lan_hosts() == sorted(_LOOPBACK)


@pytest.mark.parametrize("slow", ["getaddrinfo", "getfqdn"])
def test_slow_lookups_are_abandoned_after_the_timeout(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, slow: str
) -> None:
    import logging
    import threading
    import time

    release = threading.Event()
    _patch_socket(
        monkeypatch,
        hostname="nas",
        fqdn="nas.example.lan",
        addresses=("192.168.1.20",),
        udp=_FakeUdpSocket("10.0.0.7"),
    )
    original = getattr(socket, slow)

    def stuck(*args: Any) -> Any:
        release.wait(10)
        return original(*args)

    monkeypatch.setattr(socket, slow, stuck)
    started = time.monotonic()
    try:
        with caplog.at_level(logging.INFO, logger="autodj.lan"):
            hosts = detect_lan_hosts(timeout=0.05)
    finally:
        release.set()
    elapsed = time.monotonic() - started

    assert elapsed < 2
    # What is known without the resolver is kept.
    assert hosts == sorted(["10.0.0.7", "nas", "nas.local", *_LOOPBACK])
    assert caplog.text.count("took longer than") == 1


def test_fast_lookups_finish_within_the_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_socket(monkeypatch, hostname="nas", addresses=("192.168.1.20",))

    assert "192.168.1.20" in detect_lan_hosts(timeout=5)


def test_container_markers_are_checked(tmp_path: Path) -> None:
    from autodj.lan import CONTAINER_MARKERS, running_in_container

    marker = tmp_path / ".dockerenv"
    assert running_in_container([marker]) is False
    marker.write_text("", encoding="utf-8")
    assert running_in_container([tmp_path / "absent", marker]) is True
    assert Path("/.dockerenv") in CONTAINER_MARKERS
    assert Path("/run/.containerenv") in CONTAINER_MARKERS


def test_lan_bind_host_rule() -> None:
    from autodj.lan import lan_bind_host

    assert lan_bind_host("127.0.0.1") == "0.0.0.0"
    assert lan_bind_host("localhost") == "0.0.0.0"
    assert lan_bind_host("::1") == "0.0.0.0"
    assert lan_bind_host("192.168.1.20") == "192.168.1.20"
    assert lan_bind_host("0.0.0.0") == "0.0.0.0"


def test_container_urls_list_only_configured_hosts_in_order() -> None:
    server = ServerConfig(
        host="0.0.0.0",
        lan=True,
        access_token="t" * 32,
        allowed_hosts=[*_DETECTED, "radio.local", "172.17.0.2"],
        allowed_origins=["http://radio.local:8080"],
    )

    urls = lan_urls(
        server,
        tls=False,
        in_container=True,
        configured_hosts=["127.0.0.1", "Radio.Local", "radio.local", "fe80::1", "bad_name"],
    )

    assert urls == ["http://radio.local:8080", "http://[fe80::1]:8080"]
    assert lan_urls(server, tls=False, in_container=True) == []


def test_container_banner_puts_the_working_address_first() -> None:
    text = format_lan_banner(["http://radio.local:8080"], ("12345678", 60), in_container=True)
    lines = text.splitlines()

    assert lines[1] == "  http://radio.local:8080"
    assert lines[2] == "Container addresses are not reachable from your network."
    assert "Container addresses" not in format_lan_banner(["http://nas:8080"], None)
