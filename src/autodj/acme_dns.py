"""Bounded Cloudflare DNS-01 operations; never replace a TXT RRset."""

from __future__ import annotations

import http.client
import ipaddress
import json
import re
import socket
from collections.abc import Callable
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import Any
from urllib.parse import urlencode

import dns.resolver

API_HOST = "api.cloudflare.com"
MAX_RESPONSE = 1024 * 1024
_ID = re.compile(r"[0-9a-f]{32}")
_LABEL = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?")


class AcmeError(ValueError):
    """An allowlisted operator message, never a remote response or secret."""

    def __init__(self, message: str, *, retry_at: float = 0, permanent: bool = True) -> None:
        super().__init__(message)
        self.retry_at = retry_at
        self.permanent = permanent


def retry_after(value: str | None, now: float) -> float:
    if not value:
        return 0
    try:
        return now + max(0, int(value))
    except ValueError:
        try:
            return parsedate_to_datetime(value).timestamp()
        except (TypeError, ValueError, OverflowError):
            return 0


def validate_domain(name: str, zone: str) -> str:
    name = name.lower().rstrip(".")
    zone = zone.lower().rstrip(".")
    try:
        ipaddress.ip_address(name)
    except ValueError:
        pass
    else:
        raise AcmeError("Use a public DNS name, not an IP address.")
    labels = name.split(".")
    if (
        len(name) > 253
        or len(labels) < 2
        or any(not _LABEL.fullmatch(p) for p in labels)
        or labels[-1]
        in {
            "local",
            "localhost",
            "lan",
            "internal",
            "home",
            "corp",
            "intranet",
            "private",
            "test",
            "invalid",
            "example",
            "onion",
        }
        or name.endswith(".home.arpa")
        or labels[-1] == "arpa"
    ):
        raise AcmeError(
            "Use an ordinary public DNS name; private names and wildcards cannot be issued."
        )
    if name != zone and not name.endswith("." + zone):
        raise AcmeError("The domain is outside the selected Cloudflare zone.")
    return name


def resolve_host(name: str) -> list[str]:
    try:
        return sorted(
            {
                str(ipaddress.ip_address(info[4][0]))
                for info in socket.getaddrinfo(name, None, type=socket.SOCK_STREAM)
            }
        )
    except OSError:
        return []


def _on_host(address: str) -> bool:
    family = socket.AF_INET6 if ":" in address else socket.AF_INET
    try:
        with socket.socket(family, socket.SOCK_STREAM) as sock:
            sock.bind((address, 0))
        return not ipaddress.ip_address(address).is_unspecified
    except OSError:
        return False


def preflight(
    name: str,
    *,
    resolve: Callable[[str], list[str]] = resolve_host,
    local_addresses: set[str] | None = None,
) -> dict:
    addresses = resolve(name)
    confirmed = bool(addresses) and all(
        a in local_addresses if local_addresses is not None else _on_host(a) for a in addresses
    )
    return {"addresses": addresses, "confirmed": confirmed}


def has_cname(name: str) -> bool:
    try:
        return bool(dns.resolver.resolve(name, "CNAME", lifetime=15, search=False))
    except (dns.resolver.NXDOMAIN, dns.resolver.NoAnswer):
        return False
    except dns.exception.DNSException:
        raise AcmeError("DNS could not be checked. Try again later.", permanent=False) from None


class CloudflareDNS:
    def __init__(
        self,
        zone_id: str,
        token: str,
        *,
        request: Callable[..., Any] | None = None,
        cname: Callable[[str], bool] = has_cname,
    ) -> None:
        if (
            not _ID.fullmatch(zone_id)
            or not token
            or len(token) > 1024
            or not token.isascii()
            or any(c.isspace() for c in token)
        ):
            raise AcmeError("Enter a valid zone ID and zone-scoped API token.")
        self.zone_id = zone_id
        self._token = token
        self._request = request or self._call
        self._cname = cname
        self._zone: str | None = None

    def _call(
        self, method: str, path: str, *, body: dict | None = None, params: dict | None = None
    ) -> Any:
        connection = http.client.HTTPSConnection(API_HOST, timeout=5)
        try:
            connection.connect()
            assert connection.sock is not None
            connection.sock.settimeout(15)
            target = "/client/v4" + path + ("?" + urlencode(params) if params else "")
            connection.request(
                method,
                target,
                json.dumps(body) if body is not None else None,
                {"Authorization": "Bearer " + self._token, "Content-Type": "application/json"},
            )
            response = connection.getresponse()
            payload = response.read(MAX_RESPONSE + 1)
            if len(payload) > MAX_RESPONSE:
                raise AcmeError("Cloudflare returned an oversized response.")
            if not 200 <= response.status < 300:
                raise AcmeError(
                    "Cloudflare refused the request. Check the zone-scoped token and zone ID.",
                    retry_at=retry_after(
                        response.getheader("Retry-After"), datetime.now(UTC).timestamp()
                    ),
                    permanent=response.status not in {429, 500, 502, 503, 504},
                )
            result = json.loads(payload)
            if not isinstance(result, dict) or not result.get("success"):
                raise AcmeError("Cloudflare refused the DNS operation.")
            if result.get("result_info", {}).get("total_pages", 1) > 1:
                raise AcmeError("Too many matching DNS records to safely check ownership.")
            return result["result"]
        except (OSError, ValueError, KeyError, http.client.HTTPException) as exc:
            if isinstance(exc, AcmeError):
                raise
            raise AcmeError(
                "Cloudflare could not be reached or returned an invalid response.", permanent=False
            ) from None
        finally:
            connection.close()

    @property
    def path(self) -> str:
        return f"/zones/{self.zone_id}"

    def zone(self) -> str:
        if self._zone is None:
            result = self._request("GET", self.path)
            if result.get("id") != self.zone_id or result.get("status") != "active":
                raise AcmeError("The selected Cloudflare zone is not active.")
            self._zone = validate_domain(result["name"], result["name"])
        return self._zone

    def present(self, domain: str, value: str, marker: str) -> dict:
        name = "_acme-challenge." + validate_domain(domain, self.zone())
        records = self._request("GET", self.path + "/dns_records", params={"name": name})
        if any(r.get("type") == "CNAME" for r in records) or self._cname(name):
            raise AcmeError(
                "A CNAME at the challenge name is unsupported. Use external renewal and import."
            )
        comment = "AutoDJ ACME " + marker
        owned = [
            r
            for r in records
            if r.get("type") == "TXT" and r.get("content") == value and r.get("comment") == comment
        ]
        if len(owned) > 1:
            raise AcmeError("Multiple challenge receipts found; inspect the zone before retrying.")
        record = (
            owned[0]
            if owned
            else self._request(
                "POST",
                self.path + "/dns_records",
                body={
                    "type": "TXT",
                    "name": name,
                    "content": value,
                    "ttl": 60,
                    "comment": comment,
                },
            )
        )
        if not _ID.fullmatch(record.get("id", "")):
            raise AcmeError("Cloudflare returned an invalid record receipt.")
        return {"id": record["id"], "name": name, "content": value, "comment": comment}

    def cleanup(self, receipt: dict) -> None:
        ident = receipt["id"]
        if not _ID.fullmatch(ident):
            raise AcmeError("Invalid challenge cleanup receipt.")
        records = self._request("GET", self.path + "/dns_records", params={"name": receipt["name"]})
        record = next((r for r in records if r.get("id") == ident), None)
        if record is None:
            return
        if (
            any(record.get(k) != receipt[k] for k in ("name", "content", "comment"))
            or record.get("type") != "TXT"
        ):
            raise AcmeError("The challenge record changed; it was not deleted.")
        self._request("DELETE", self.path + "/dns_records/" + ident)

    def recover(self, domain: str, value: str, marker: str) -> dict | None:
        """Recover a lost creation response without creating any new DNS record."""
        name = "_acme-challenge." + validate_domain(domain, self.zone())
        comment = "AutoDJ ACME " + marker
        records = self._request("GET", self.path + "/dns_records", params={"name": name})
        owned = [
            r
            for r in records
            if r.get("type") == "TXT"
            and r.get("content") == value
            and r.get("comment") == comment
            and r.get("name") == name
        ]
        if len(owned) > 1:
            raise AcmeError("Multiple challenge receipts found; inspect the zone before retrying.")
        if not owned:
            return None
        if not _ID.fullmatch(owned[0].get("id", "")):
            raise AcmeError("Cloudflare returned an invalid record receipt.")
        return {"id": owned[0]["id"], "name": name, "content": value, "comment": comment}
