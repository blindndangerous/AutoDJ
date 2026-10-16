"""Bounded speech endpoint protocols. See ADR 13; no model runtime or SDK imports."""

from __future__ import annotations

import base64
import hashlib
import html
import http.client
import io
import ipaddress
import json
import math
import os
import re
import socket
import ssl
import subprocess  # nosec B404
import sys
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from autodj.secret_files import write_private_file

ENGINES = ("openai", "speechify", "fish", "chatterbox", "gpt-sovits")
MAX_BYTES = 32 * 1024 * 1024
MAX_SECONDS = 120
DEADLINE_SECONDS = 300
ID = re.compile(r"[a-z0-9][a-z0-9-]{0,63}\Z")
METADATA = {ipaddress.ip_address(value) for value in ("100.100.100.200", "fd00:ec2::254")}


def identifier(value: str) -> str:
    """Validate an opaque resource name before deriving any file or env name."""
    if not isinstance(value, str) or not ID.fullmatch(value):
        raise ValueError("Use a name of up to 64 lowercase letters, numbers and hyphens")
    return value


def validate_url(url: str, trusted_local: bool = False) -> str:
    """Validate configured destinations, not browser-supplied generation URLs."""
    try:
        parts = urlsplit(url)
        port = parts.port
        if (
            parts.scheme not in ("http", "https")
            or not parts.hostname
            or parts.username
            or parts.password
            or parts.fragment
            or parts.query
            or any(ord(c) < 33 for c in url)
            or "\\" in url
        ):
            raise ValueError
        if port is not None and not 1 <= port <= 65535:
            raise ValueError
        try:
            address = ipaddress.ip_address(parts.hostname)
            address = getattr(address, "ipv4_mapped", None) or address
        except ValueError:
            address = None
        if address and (
            address in METADATA
            or address.is_link_local
            or address.is_multicast
            or address.is_unspecified
        ):
            raise ValueError
        if parts.scheme == "http" and not trusted_local:
            raise ValueError
    except (ValueError, TypeError):
        raise ValueError(
            "Use HTTPS, or explicitly allow HTTP for a trusted local endpoint"
        ) from None
    return url


def safe_addresses(host: str, port: int, trusted_local: bool = False) -> list[str]:
    """Resolve once and pin the checked address to prevent a second DNS lookup."""
    addresses = list(
        dict.fromkeys(
            str(row[4][0]) for row in socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
        )
    )
    for value in addresses:
        address = ipaddress.ip_address(value)
        address = getattr(address, "ipv4_mapped", None) or address
        if (
            address in METADATA
            or address.is_link_local
            or address.is_multicast
            or address.is_unspecified
            or (not address.is_global and not trusted_local)
        ):
            raise ValueError("Endpoint resolved to a forbidden or unapproved local address")
    if not addresses:
        raise ValueError("Endpoint has no usable network address")
    return addresses


class ProviderStore:
    """One private server-wide connection registry; env credentials take precedence."""

    def __init__(self, path: Path):
        self.path = path
        self.lock = threading.RLock()

    def _load(self) -> dict:
        try:
            data = json.loads(self.path.read_text())
        except FileNotFoundError:
            return {}
        except (OSError, ValueError):
            raise ValueError(
                "Speech provider settings could not be read; preserved unchanged"
            ) from None
        if (
            not isinstance(data, dict)
            or data.get("version") != 1
            or not isinstance(data.get("connections"), dict)
        ):
            raise ValueError("Speech provider settings have an unsupported format")
        return data["connections"]

    def connection(self, name: str) -> dict:
        identifier(name)
        with self.lock:
            data = self._load()
            if name not in data:
                raise ValueError("Speech connection not found")
            if not isinstance(data[name], dict) or not isinstance(data[name].get("revision"), str):
                raise ValueError("Speech connection has an unsupported format")
            value = dict(data[name])
            variable = "AUTODJ_SPEECH_" + name.upper().replace("-", "_") + "_KEY"
            if variable in os.environ:
                value["api_key"] = os.environ[variable]
            key = value.get("api_key", "")
            if not isinstance(key, str) or len(key) > 4096 or any(ord(c) < 32 for c in key):
                raise ValueError("Configured speech credential contains invalid characters")
            value["revision"] = hashlib.sha256(
                json.dumps([value["revision"], key]).encode()
            ).hexdigest()
            value["credential_environment"] = variable in os.environ
            value["id"] = name
            return value

    def public(self) -> list[dict]:
        with self.lock:
            result = []
            for name in self._load():
                value = self.connection(name)
                value["credential_present"] = bool(value.pop("api_key", ""))
                result.append(value)
            return result

    def save(self, name: str, changes: dict) -> None:
        identifier(name)
        if name == "station-command":
            raise ValueError("station-command is reserved for the station TTS command")
        allowed = {"name", "engine", "url", "api_key", "trusted_local"}
        if not isinstance(changes, dict) or changes.keys() - allowed:
            raise ValueError("Unknown speech connection fields")
        with self.lock:
            data = self._load()
            variable = "AUTODJ_SPEECH_" + name.upper().replace("-", "_") + "_KEY"
            if "api_key" in changes and variable in os.environ:
                raise ValueError("This credential is managed through the environment")
            value = {**data.get(name, {}), **changes}
            if len(data) >= 32 and name not in data:
                raise ValueError("At most 32 speech connections may be configured")
            if value.get("engine") not in ENGINES:
                raise ValueError("Choose a supported speech engine")
            if not isinstance(value.get("trusted_local", False), bool):
                raise ValueError("Trusted local must be true or false")
            validate_url(value.get("url", ""), value.get("trusted_local", False))
            for field in ("name", "api_key"):
                text = value.get(field, "")
                if not isinstance(text, str) or len(text) > (4096 if field == "api_key" else 100):
                    raise ValueError("Invalid connection name or credential")
                if any(ord(c) < 32 for c in text):
                    raise ValueError("Connection fields must not contain control characters")
            # A configuration revision invalidates cached upstream identity without storing keys.
            value["revision"] = os.urandom(8).hex()
            data[name] = value
            write_private_file(self.path, json.dumps({"version": 1, "connections": data}).encode())


def prepare(connection: dict, text: str, settings: dict) -> dict:
    """Lower supported controls and fail preflight before sending supplied text."""
    engine = connection["engine"]
    if engine not in ENGINES or not isinstance(text, str) or not text.strip():
        raise ValueError("Supply text and a supported engine")
    if len(text) > {"openai": 4096, "speechify": 2000}.get(engine, 5000):
        raise ValueError("The script is over this provider's text limit")
    if not isinstance(settings, dict) or settings.keys() - {
        "voice",
        "model",
        "speed",
        "instructions",
    }:
        raise ValueError("Unknown speech settings")
    voice = settings.get("voice", "")
    model = settings.get("model", "")
    instructions = settings.get("instructions", "")
    if not isinstance(voice, str) or not voice.strip() or len(voice) > 256:
        raise ValueError("Supply an explicit voice ID")
    if any(not isinstance(v, str) or len(v) > 4096 for v in (model, instructions)):
        raise ValueError("Invalid model or delivery instructions")
    speed = settings.get("speed", 1.0)
    if isinstance(speed, bool) or not isinstance(speed, (int, float)) or not math.isfinite(speed):
        raise ValueError("Speed must be a finite number")
    payload: dict[str, Any] = {"input": text, "voice": voice}
    controls = {}
    if engine in ("openai", "speechify"):
        if not connection.get("api_key"):
            raise ValueError("Configure this provider's credential before generating")
        if not model:
            raise ValueError("Supply an explicit speech model")
        payload["model"] = model
    elif model or instructions or speed != 1:
        raise ValueError("This endpoint profile supports text and voice only")
    if engine == "openai":
        payload["response_format"] = "wav"
        if not 0.25 <= speed <= 4:
            raise ValueError("OpenAI speed must be between 0.25 and 4")
        payload["speed"] = speed
        controls["speed"] = "applied"
        if instructions:
            if not model.startswith("gpt-4o-mini-tts"):
                raise ValueError("This model does not support delivery instructions")
            payload["instructions"] = instructions
            controls["instructions"] = "approximated"
    if engine == "speechify":
        if instructions:
            raise ValueError("Free-form instructions are unavailable for this Speechify profile")
        if not 0.5 <= speed <= 2:
            raise ValueError("Speechify speed must be between 0.5 and 2")
        payload = {
            "input": html.escape(text),
            "voice_id": voice,
            "model": model,
            "audio_format": "wav",
        }
        if speed != 1:
            payload["input"] = (
                f'<speak><prosody rate="{speed * 100:g}%">{html.escape(text)}</prosody></speak>'
            )
            controls["speed"] = "applied"
    return {
        "payload": payload,
        "controls": controls,
        "protocol": engine + "-v1",
        "text": text,
        "settings": dict(settings),
        "cost": None,
    }


def request_audio(connection: dict, payload: dict, cancel: threading.Event) -> bytes:
    """Bound a direct HTTP request; no redirects, proxies or remote result URLs."""
    validate_url(connection["url"], connection.get("trusted_local", False))
    parts = urlsplit(connection["url"])
    port = parts.port or (443 if parts.scheme == "https" else 80)
    addresses = safe_addresses(parts.hostname, port, connection.get("trusted_local", False))
    if parts.scheme == "http" and any(ipaddress.ip_address(value).is_global for value in addresses):
        raise ValueError("Trusted HTTP endpoints must resolve to local network addresses")
    deadline = time.monotonic() + DEADLINE_SECONDS
    sock = None
    client = http.client.HTTPConnection(parts.hostname, port, timeout=10)
    try:
        if cancel.is_set():
            raise ValueError("Generation cancelled")
        sock = socket.create_connection((addresses[0], port), timeout=10)
        if parts.scheme == "https":
            sock = ssl.create_default_context().wrap_socket(sock, server_hostname=parts.hostname)
        client.sock = sock
        headers = {"Content-Type": "application/json", "Accept": "audio/wav, application/json"}
        if connection.get("api_key"):
            headers["Authorization"] = "Bearer " + connection["api_key"]
        client.request("POST", parts.path or "/", json.dumps(payload).encode(), headers)
        sock.settimeout(max(0.01, deadline - time.monotonic()))
        response = client.getresponse()
        if response.status != 200:
            raise ValueError(f"Speech endpoint returned HTTP {response.status}; no automatic retry")
        pieces = []
        total = 0
        while True:
            if cancel.is_set():
                raise ValueError("Generation cancelled; the provider may already have billed it")
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise ValueError("Speech request deadline exceeded; billing status is unknown")
            sock.settimeout(min(10, remaining))
            chunk = response.read1(min(65536, MAX_BYTES - total + 1))
            if not chunk:
                break
            total += len(chunk)
            if total > MAX_BYTES:
                raise ValueError("Speech response exceeds 32 MiB")
            pieces.append(chunk)
        return b"".join(pieces)
    except (OSError, http.client.HTTPException):
        raise ValueError(
            "Speech endpoint did not complete the request; billing status is unknown"
        ) from None
    finally:
        client.close()
        if sock:
            sock.close()


def checked_audio(raw: bytes) -> dict:
    """Decode bounded provider audio into portable PCM; reject silence and excess duration."""
    import numpy as np
    import soundfile as sf

    if not raw or len(raw) > MAX_BYTES:
        raise ValueError("Speech audio is empty or exceeds 32 MiB")
    try:
        with sf.SoundFile(io.BytesIO(raw)) as source:
            if (
                not 8000 <= source.samplerate <= 192000
                or not 0 < source.frames / source.samplerate <= MAX_SECONDS
                or source.channels not in (1, 2)
                or source.frames * source.channels * 4 > MAX_BYTES
            ):
                raise ValueError("Speech must be at most 120 seconds and at most two channels")
            samples = source.read(dtype="float32", always_2d=True)
            rate = source.samplerate
        if not np.isfinite(samples).all() or np.max(np.abs(samples)) < 1e-5:
            raise ValueError("Speech audio contains no usable sound")
        output = io.BytesIO()
        sf.write(output, samples, rate, format="WAV", subtype="PCM_16")
        if output.tell() > MAX_BYTES:
            raise ValueError("Decoded speech exceeds 32 MiB")
        return {"audio": output.getvalue(), "duration": len(samples) / rate}
    except (RuntimeError, OSError):
        raise ValueError("Speech endpoint returned audio AutoDJ could not decode") from None


def _synthesize(
    connection: dict,
    plan: dict,
    cancel: threading.Event,
    transport: Callable[[dict, dict, threading.Event], bytes],
) -> dict:
    """Execute one candidate without retries, hiding provider error bodies."""
    raw = transport(connection, plan["payload"], cancel or threading.Event())
    if connection["engine"] == "speechify":
        try:
            if len(raw) > MAX_BYTES:
                raise ValueError
            raw = base64.b64decode(json.loads(raw)["audio_data"], validate=True)
        except (ValueError, KeyError, TypeError):
            raise ValueError("Speechify returned an invalid audio response") from None
    return checked_audio(raw)


def synthesize(
    connection: dict,
    plan: dict,
    cancel: threading.Event | None = None,
    *,
    transport: Callable[[dict, dict, threading.Event], bytes] | None = None,
) -> dict:
    """Bound the entire provider operation, including DNS and native audio decoding."""
    from autodj.child_jobs import bind_to_autodj

    cancel = cancel or threading.Event()
    if transport is not None:
        return _synthesize(connection, plan, cancel, transport)
    if cancel.is_set():
        raise ValueError("Generation cancelled")
    # Credentials travel over a private pipe, never command arguments or a file.
    payload = json.dumps({"connection": connection, "plan": plan}).encode()
    # Fixed local Python module, no shell; endpoint input travels only through stdin.
    with subprocess.Popen(  # nosec B603
        [sys.executable, "-m", "autodj.speech_providers"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    ) as child:
        bind_to_autodj(child)
        deadline = time.monotonic() + DEADLINE_SECONDS
        try:
            first = True
            while True:
                remaining = deadline - time.monotonic()
                if cancel.is_set() or remaining <= 0:
                    raise ValueError(
                        "Speech request cancelled or timed out; billing status is unknown"
                    )
                try:
                    raw, error = child.communicate(
                        payload if first else None, timeout=min(0.2, remaining)
                    )
                    break
                except subprocess.TimeoutExpired:
                    first = False
            if child.returncode:
                # Only our fixed error envelope is emitted by the worker, never provider bodies.
                try:
                    message = json.loads(error)["error"]
                except (ValueError, TypeError, KeyError):
                    message = "Speech worker could not complete generation"
                raise ValueError(message)
            return checked_audio(raw)
        finally:
            if child.poll() is None:
                child.kill()
            child.communicate()


if __name__ == "__main__":
    try:
        supplied = json.loads(sys.stdin.buffer.read(128 * 1024))
        result = _synthesize(
            supplied["connection"], supplied["plan"], threading.Event(), request_audio
        )
        sys.stdout.buffer.write(result["audio"])
    except Exception as exc:
        message = str(exc) if isinstance(exc, ValueError) else "Speech worker failed"
        sys.stderr.write(json.dumps({"error": message[:500]}))
        sys.exit(1)


def cache_key(connection: dict, plan: dict) -> str:
    """Hash effective requests, never credentials or mutable preset names."""
    identity = {key: connection.get(key) for key in ("id", "engine", "url", "revision")}
    identity.update(protocol=plan["protocol"], payload=plan["payload"])
    return hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()
