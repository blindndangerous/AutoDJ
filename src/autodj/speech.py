"""Station speech production, kept separate from eligible playout assets (ADR 13)."""

from __future__ import annotations

import copy
import hashlib
import json
import logging
import re
import shutil
import subprocess  # nosec B404
import tempfile
import threading
import time
import uuid
from collections.abc import Callable, Iterable
from datetime import datetime
from pathlib import Path
from typing import Any

from autodj.fsutil import atomic_write
from autodj.speech_providers import (
    MAX_BYTES,
    ProviderStore,
    cache_key,
    checked_audio,
    identifier,
    prepare,
    synthesize,
)

logger = logging.getLogger(__name__)
CACHE_LIMIT = 1024 * 1024 * 1024
KEEP_SECONDS = 7 * 86400
SET_LIMIT = 512 * 1024 * 1024


def _empty() -> dict:
    return {
        "version": 1,
        "batches": {},
        "saved": {},
        "presets": {},
        "default_preset": None,
        "sets": {},
        "active_set": None,
    }


def load_state(root: Path) -> dict:
    """Read without modifying state; malformed state never becomes empty writable state."""
    try:
        data = json.loads((root / "speech.json").read_text())
    except FileNotFoundError:
        return _empty()
    except (OSError, ValueError):
        raise ValueError("Saved speech state cannot be read; it has been preserved") from None
    if (
        not isinstance(data, dict)
        or data.get("version") != 1
        or any(
            not isinstance(data.get(key), dict) for key in ("batches", "saved", "presets", "sets")
        )
    ):
        raise ValueError("Saved speech state has an unsupported format")
    return data


def export_state(root: Path) -> dict:
    """Saved assets/presets and complete sets only; never resume paid work from a backup."""
    data = load_state(root)
    data["batches"] = {}
    data["sets"] = {
        key: value for key, value in data["sets"].items() if value.get("state") == "complete"
    }
    return data


def backup_files(root: Path) -> set[Path]:
    """Enumerate only durable speech files through validated opaque IDs."""
    data = export_state(root)
    files = {root / "speech.json"} if (root / "speech.json").exists() else set()
    for name in data["saved"]:
        identifier(name)
        files.update(root / "saved" / f"{name}{suffix}" for suffix in (".wav", ".json"))
    for name, item in data["sets"].items():
        identifier(name)
        for key in set(item.get("minutes", {}).values()):
            if not isinstance(key, str) or not re.fullmatch("[0-9a-f]{64}", key):
                raise ValueError("Invalid generated time-check identity")
            files.add(root / "sets" / name / f"{key}.wav")
    return files


def generated_clip(root: Path, local: datetime) -> tuple[bool, Path | None]:
    """Return selected-mode and a local clip; never fall back to synthesis in this mode."""
    data = load_state(root)
    selected = data.get("active_set")
    if not selected:
        return False, None
    identifier(selected)
    item = data["sets"].get(selected)
    if not item or item.get("state") != "complete":
        return True, None
    name = f"{local.hour:02d}_{local.minute:02d}"
    key = item.get("minutes", {}).get(name)
    if not isinstance(key, str) or len(key) != 64 or any(c not in "0123456789abcdef" for c in key):
        return True, None
    path = root / "sets" / selected / f"{key}.wav"
    return True, path if path.is_file() else None


class SpeechService:
    """One bounded speech worker per server; all durable state is station-local."""

    def __init__(
        self,
        providers: ProviderStore,
        roots: Callable[[], Iterable[Path]],
        *,
        command: Callable[[Path], list[str]] = lambda root: [],
        render: Callable[[dict, dict, threading.Event], dict] = synthesize,
    ) -> None:
        self.providers = providers
        self.roots = roots
        self.command = command
        self.render = render
        self.lock = threading.RLock()
        self.worker: threading.Thread | None = None
        self.cancelled = threading.Event()
        self.stopping = False
        self.seen: set[Path] = set()
        self.active_root: Path | None = None

    def _load(self, root: Path) -> dict:
        if root not in self.roots():
            raise ValueError("Unknown speech station")
        data = load_state(root)
        if root not in self.seen:
            for batch in data["batches"].values():
                for candidate in batch["candidates"]:
                    if candidate["state"] in ("pending", "running"):
                        candidate["state"] = "interrupted"
                        candidate["error"] = "Interrupted; check billing before retrying"
            self.seen.add(root)
            if (root / "speech.json").exists():
                self._write(root, data)
        return data

    def _write(self, root: Path, data: dict) -> None:
        atomic_write(root / "speech.json", json.dumps(data, ensure_ascii=False))

    def view(self, root: Path) -> dict:
        with self.lock:
            data = self._load(root)
            data["command_available"] = bool(self.command(root))
            data["busy"] = self.worker is not None and self.worker.is_alive()
            for token, batch in data["batches"].items():
                for index, candidate in enumerate(batch["candidates"]):
                    if (
                        candidate["state"] == "succeeded"
                        and not self._artifact(root, token, index).exists()
                    ):
                        candidate["state"] = "expired"
            return data

    def _connection(self, root: Path | None, name: str) -> dict:
        if name != "station-command":
            return self.providers.connection(name)
        from autodj.config import TimeChecksConfig

        command = list(self.command(root)) if root is not None else []
        TimeChecksConfig(tts_command=command)
        if not command:
            raise ValueError("This station has no configured TTS command")
        revision = hashlib.sha256(json.dumps(command).encode()).hexdigest()
        return {"id": name, "engine": "command", "revision": revision, "command": command}

    def _candidate(self, text: str, request: dict, root: Path | None = None) -> dict:
        if not isinstance(request, dict) or request.keys() - {
            "connection",
            "voice",
            "model",
            "speed",
            "instructions",
        }:
            raise ValueError("Unknown candidate settings")
        connection = self._connection(root, request.get("connection", ""))
        settings = {key: value for key, value in request.items() if key != "connection"}
        if connection["engine"] == "command":
            if not isinstance(text, str) or not text.strip() or len(text) > 5000:
                raise ValueError("Supply a script of at most 5000 characters")
            if (
                settings.get("voice", "") not in ("", "configured")
                or settings.get("model", "")
                or settings.get("instructions", "")
                or settings.get("speed", 1) != 1
            ):
                raise ValueError(
                    "Voice, model and delivery are configured by the station TTS command"
                )
            plan = {
                "text": text,
                "settings": {"voice": "configured"},
                "controls": {"voice/model": "configured by command"},
                "protocol": "station-command-v1",
                "payload": {"input": text},
            }
        else:
            plan = prepare(connection, text, settings)
        snapshot = {key: connection.get(key) for key in ("id", "engine", "url", "revision")}
        return {
            "connection": connection["id"],
            "snapshot": snapshot,
            "plan": plan,
            "key": cache_key(connection, plan),
            "state": "pending",
            "error": None,
            "duration": None,
            "cached": False,
        }

    def preflight(self, text: str, candidates: list, *, root: Path | None = None) -> dict:
        if not isinstance(candidates, list) or not 1 <= len(candidates) <= 8:
            raise ValueError("Choose between one and eight samples")
        plans = []
        for request in candidates:
            try:
                candidate = self._candidate(text, request, root)
                plans.append({"controls": candidate["plan"]["controls"], "error": None})
            except ValueError as exc:
                plans.append({"controls": {}, "error": str(exc)})
        return {"candidates": plans, "cost": None, "cost_text": "Cost unknown"}

    def _space(self, reserve: int = MAX_BYTES, *, disk_reserve: int | None = None) -> None:
        total = 0
        for root in self.roots():
            files = list((root / "cache").glob("*.wav")) + list((root / "samples").glob("*/*.wav"))
            for file in files:
                if time.time() - file.stat().st_mtime > KEEP_SECONDS:
                    file.unlink(missing_ok=True)
                else:
                    total += file.stat().st_size
        if total + reserve > CACHE_LIMIT:
            raise ValueError("Temporary speech storage is full; remove drafts or wait for expiry")
        existing = next((root.parent for root in self.roots() if root.parent.exists()), None)
        if existing is not None and shutil.disk_usage(existing).free < (disk_reserve or reserve):
            raise ValueError("Not enough free disk space for speech generation")

    def _cache(self, root: Path, candidate: dict) -> Path:
        return root / "cache" / f"{candidate['key']}.wav"

    def _artifact(self, root: Path, token: str, index: int) -> Path:
        return root / "samples" / token / f"{index}.wav"

    def submit(
        self,
        root: Path,
        text: str,
        requests: list,
        token: str,
        *,
        fresh: bool = False,
        set_name: str | None = None,
    ) -> str:
        identifier(token)
        if not isinstance(text, str) or not text.strip() or len(text) > 5000:
            raise ValueError("Supply a script of at most 5000 characters")
        if not isinstance(requests, list) or not 1 <= len(requests) <= (1440 if set_name else 8):
            raise ValueError("Choose between one and eight comparison samples")
        fingerprint = hashlib.sha256(
            json.dumps([text, requests, fresh, set_name], sort_keys=True).encode()
        ).hexdigest()
        with self.lock:
            data = self._load(root)
            prior = data["batches"].get(token)
            if prior:
                if prior["fingerprint"] != fingerprint:
                    raise ValueError("That submission ID belongs to different text or settings")
                return token
            if self.stopping or (self.worker is not None and self.worker.is_alive()):
                raise ValueError("Speech generation is busy; try after the current batch finishes")
            if len(data["batches"]) >= 100:
                raise ValueError("Remove an old comparison before creating another")
            if set_name:
                self._space(2 * MAX_BYTES, disk_reserve=3 * SET_LIMIT)
            else:
                self._space(2 * len(requests) * MAX_BYTES)
            candidates = []
            for request in requests:
                try:
                    candidate = self._candidate(
                        request.get("_text", text),
                        {key: value for key, value in request.items() if key != "_text"},
                        root,
                    )
                except ValueError as exc:
                    candidate = {
                        "state": "failed",
                        "error": str(exc),
                        "duration": None,
                        "cached": False,
                        "request": request,
                    }
                candidates.append(candidate)
            data["batches"][token] = {
                "text": text,
                "candidates": candidates,
                "fingerprint": fingerprint,
                "fresh": fresh,
                "set_name": set_name,
                "created": time.time(),
            }
            self._write(root, data)
            self._start(root, token)
            return token

    def _start(self, root: Path, token: str) -> None:
        self.cancelled.clear()
        self.active_root = root
        self.worker = threading.Thread(
            target=self._run, args=(root, token), name="autodj-speech", daemon=True
        )
        self.worker.start()

    def _update(self, root: Path, token: str, index: int, **fields: Any) -> None:
        with self.lock:
            data = self._load(root)
            data["batches"][token]["candidates"][index].update(fields)
            self._write(root, data)

    def _run(self, root: Path, token: str) -> None:
        try:
            with self.lock:
                batch = copy.deepcopy(self._load(root)["batches"][token])
            outcomes: dict[str, dict] = {}
            for index, candidate in enumerate(batch["candidates"]):
                if candidate["state"] != "pending":
                    continue
                if self.cancelled.is_set():
                    self._update(
                        root, token, index, state="cancelled", error="Generation cancelled"
                    )
                    continue
                self._update(root, token, index, state="running")
                try:
                    prior = outcomes.get(candidate["key"])
                    if prior:
                        if prior["state"] == "succeeded":
                            atomic_write(
                                self._artifact(root, token, index),
                                self._artifact(root, token, prior["index"]).read_bytes(),
                            )
                        self._update(
                            root,
                            token,
                            index,
                            **{key: value for key, value in prior.items() if key != "index"},
                        )
                        continue
                    connection = self._connection(root, candidate["connection"])
                    if connection["revision"] != candidate["snapshot"]["revision"]:
                        raise ValueError("Connection settings changed; create a new comparison")
                    cache = self._cache(root, candidate)
                    result = None
                    if cache.exists() and not batch["fresh"]:
                        try:
                            result = checked_audio(cache.read_bytes())
                        except ValueError:
                            cache.unlink(missing_ok=True)
                    cached = result is not None
                    if result is None:
                        self._space(2 * MAX_BYTES)
                        if connection["engine"] == "command":
                            result = self._render_command(root, connection, candidate["plan"])
                        else:
                            result = self.render(connection, candidate["plan"], self.cancelled)
                    if self.cancelled.is_set():
                        self._update(
                            root, token, index, state="cancelled", error="Generation cancelled"
                        )
                        continue
                    atomic_write(cache, result["audio"])
                    atomic_write(self._artifact(root, token, index), result["audio"])
                    if batch["set_name"]:
                        accumulated = sum(
                            path.stat().st_size for path in (root / "samples" / token).glob("*.wav")
                        )
                        if accumulated > SET_LIMIT:
                            self.cancelled.set()
                            raise ValueError(
                                "Generated set exceeds 512 MiB; remaining requests cancelled"
                            )
                    self._update(
                        root,
                        token,
                        index,
                        state="succeeded",
                        duration=result["duration"],
                        cached=cached,
                        error=None,
                    )
                    outcomes[candidate["key"]] = {
                        "index": index,
                        "state": "succeeded",
                        "duration": result["duration"],
                        "cached": True,
                        "error": None,
                    }
                except Exception as exc:
                    # Provider bodies and unexpected exceptions may contain credentials or URLs.
                    safe = str(exc) if isinstance(exc, ValueError) else "Speech generation failed"
                    self._update(root, token, index, state="failed", error=safe)
                    outcomes[candidate["key"]] = {"index": index, "state": "failed", "error": safe}
            if batch["set_name"]:
                self._finish_set(root, token, batch["set_name"])
        except Exception:
            logger.error("Speech worker stopped; in-flight work will not be automatically retried")

    def _render_command(self, root: Path, connection: dict, plan: dict) -> dict:
        from autodj.tts_command import run_command

        with tempfile.TemporaryDirectory(dir=root) as directory:
            output = Path(directory) / "speech.wav"
            try:
                status = run_command(
                    connection["command"],
                    plan["text"],
                    output,
                    cancel=self.cancelled,
                    max_bytes=MAX_BYTES,
                )
                if status:
                    raise ValueError(f"TTS command failed (exit {status})")
                if not output.is_file() or output.stat().st_size > MAX_BYTES:
                    raise ValueError("TTS command did not write audio within the size limit")
                return checked_audio(output.read_bytes())
            except subprocess.TimeoutExpired:
                raise ValueError("TTS command timed out after 20 seconds") from None
            except OSError:
                raise ValueError(
                    "TTS command could not run or its audio could not be read"
                ) from None

    def retry(
        self, root: Path, token: str, indices: list[int], *, attempt: str | None = None
    ) -> None:
        identifier(token)
        attempt = identifier(attempt or uuid.uuid4().hex)
        with self.lock:
            data = self._load(root)
            batch = data["batches"].get(token)
            if batch and attempt in batch.get("retry_tokens", {}):
                if batch["retry_tokens"][attempt] != indices:
                    raise ValueError("Retry ID belongs to a different set of samples")
                return
            if self.stopping or (self.worker and self.worker.is_alive()):
                raise ValueError("Speech generation is busy")
            if not batch or not indices or len(set(indices)) != len(indices):
                raise ValueError("Choose failed samples to retry")
            for index in indices:
                expired = (
                    type(index) is int
                    and 0 <= index < len(batch["candidates"])
                    and batch["candidates"][index]["state"] == "succeeded"
                    and not self._artifact(root, token, index).is_file()
                )
                if (
                    type(index) is not int
                    or not 0 <= index < len(batch["candidates"])
                    or (
                        batch["candidates"][index]["state"]
                        not in ("failed", "interrupted", "cancelled")
                        and not expired
                    )
                ):
                    raise ValueError("Only failed, interrupted or expired samples can be retried")
            for index in indices:
                candidate = batch["candidates"][index]
                if "request" in candidate:
                    request = candidate["request"]
                    candidate = self._candidate(
                        request.get("_text", batch["text"]),
                        {key: value for key, value in request.items() if key != "_text"},
                        root,
                    )
                    batch["candidates"][index] = candidate
                candidate.update(state="pending", error=None)
            batch.setdefault("retry_tokens", {})[attempt] = indices
            self._write(root, data)
            self._start(root, token)

    def cancel(self, root: Path | None = None) -> None:
        if root is not None and self.active_root != root:
            raise ValueError("No speech generation is running on this station")
        self.cancelled.set()

    def wait(self, timeout: float = 5) -> bool:
        worker = self.worker
        if worker:
            worker.join(timeout)
        return worker is None or not worker.is_alive()

    def shutdown(self, timeout: float = 5) -> bool:
        with self.lock:
            self.stopping = True
            self.cancelled.set()
        return self.wait(timeout)

    def sample(self, root: Path, token: str, index: int) -> Path:
        identifier(token)
        with self.lock:
            batch = self._load(root)["batches"].get(token)
            if not batch or type(index) is not int or not 0 <= index < len(batch["candidates"]):
                raise ValueError("Sample not found")
            candidate = batch["candidates"][index]
            if candidate["state"] != "succeeded":
                raise ValueError("This sample has no completed audio")
            cache = self._artifact(root, token, index)
            if not cache.is_file():
                raise ValueError("Sample expired; explicitly generate it again")
            return cache

    def save(self, root: Path, token: str, index: int, label: str) -> str:
        if not isinstance(label, str) or not label.strip() or len(label) > 100:
            raise ValueError("Supply a liner name of at most 100 characters")
        with self.lock:
            path = self.sample(root, token, index)
            checked_audio(path.read_bytes())
            data = self._load(root)
            candidate = data["batches"][token]["candidates"][index]
            name = "liner-" + hashlib.sha256(f"{token}:{index}".encode()).hexdigest()[:16]
            item = {
                "label": label,
                "text": candidate["plan"]["text"],
                "generation": copy.deepcopy(candidate),
                "published": data["saved"].get(name, {}).get("published", False),
            }
            atomic_write(root / "saved" / f"{name}.wav", path.read_bytes())
            atomic_write(root / "saved" / f"{name}.json", json.dumps(item))
            data["saved"][name] = item
            self._write(root, data)
            return name

    def publish(self, root: Path, name: str, target: Path) -> Path:
        identifier(name)
        with self.lock:
            data = self._load(root)
            if name not in data["saved"]:
                raise ValueError("Saved liner not found")
            raw = (root / "saved" / f"{name}.wav").read_bytes()
            checked_audio(raw)
            path = target / f"{name}.wav"
            # Metadata first: a playable file can never appear without its source record.
            atomic_write(path.with_suffix(".json"), json.dumps(data["saved"][name]))
            atomic_write(path, raw)
            data["saved"][name]["published"] = True
            self._write(root, data)
            return path

    def preset(self, root: Path, name: str, candidate: dict) -> None:
        identifier(name)
        self._candidate("Voice preview", candidate, root)
        with self.lock:
            data = self._load(root)
            if len(data["presets"]) >= 100 and name not in data["presets"]:
                raise ValueError("At most 100 voice presets may be saved")
            data["presets"][name] = copy.deepcopy(candidate)
            self._write(root, data)

    def default(self, root: Path, name: str) -> None:
        with self.lock:
            data = self._load(root)
            if name not in data["presets"]:
                raise ValueError("Voice preset not found")
            data["default_preset"] = name
            self._write(root, data)

    def time_inventory(self, root: Path, preset: str, hour_format: str) -> dict:
        from autodj.time_checks import spoken_text

        if hour_format not in ("12", "24"):
            raise ValueError("Choose 12- or 24-hour time")
        with self.lock:
            data = self._load(root)
            if preset not in data["presets"]:
                raise ValueError("Voice preset not found")
            request = data["presets"][preset]
            minutes = {}
            unique = {}
            hits = 0
            for hour in range(24):
                for minute in range(60):
                    text = spoken_text(datetime(2000, 1, 1, hour, minute), hour_format)
                    candidate = self._candidate(text, request, root)
                    minutes[f"{hour:02d}_{minute:02d}"] = candidate["key"]
                    if candidate["key"] not in unique:
                        unique[candidate["key"]] = {**request, "_text": text}
                        cache = self._cache(root, candidate)
                        if cache.is_file():
                            try:
                                checked_audio(cache.read_bytes())
                                hits += 1
                            except ValueError:
                                pass
            fingerprint = hashlib.sha256(
                json.dumps([hour_format, minutes], sort_keys=True).encode()
            ).hexdigest()
            return {
                "minutes": minutes,
                "candidates": list(unique.values()),
                "fingerprint": fingerprint,
                "requests": len(unique),
                "cache_hits": hits,
                "missing": len(unique) - hits,
                "cost": None,
                "preset": preset,
                "hour_format": hour_format,
            }

    def generate_times(
        self, root: Path, preset: str, hour_format: str, token: str, expected: str | None = None
    ) -> str:
        identifier(token)
        with self.lock:
            inventory = self.time_inventory(root, preset, hour_format)
            if expected is not None and expected != inventory["fingerprint"]:
                raise ValueError(
                    "Time-check settings changed; check the inventory again before generating"
                )
            data = self._load(root)
            if token in data["sets"]:
                return token
            # The set remains incomplete and ineligible until every request validates.
            data["sets"][token] = {**inventory, "state": "incomplete", "reviewed": False}
            self._write(root, data)
            try:
                self.submit(
                    root, "Generated time checks", inventory["candidates"], token, set_name=token
                )
            except Exception:
                data["sets"].pop(token)
                self._write(root, data)
                raise
            return token

    def _finish_set(self, root: Path, token: str, name: str) -> None:
        with self.lock:
            data = self._load(root)
            batch = data["batches"][token]
            item = data["sets"][name]
            if any(c["state"] != "succeeded" for c in batch["candidates"]):
                return
            total = sum(
                self._artifact(root, token, i).stat().st_size
                for i in range(len(batch["candidates"]))
            )
            if total > SET_LIMIT:
                item["error"] = "Generated set exceeds 512 MiB; not activated"
            else:
                for index, candidate in enumerate(batch["candidates"]):
                    atomic_write(
                        root / "sets" / name / f"{candidate['key']}.wav",
                        self._artifact(root, token, index).read_bytes(),
                    )
                item["state"] = "complete"
                item["generation"] = [c["snapshot"] for c in batch["candidates"]]
            self._write(root, data)

    def saved_audio(self, root: Path, name: str) -> Path:
        identifier(name)
        with self.lock:
            if name not in self._load(root)["saved"]:
                raise ValueError("Saved liner not found")
            return root / "saved" / f"{name}.wav"

    def set_audio(self, root: Path, name: str, minute: str) -> Path:
        identifier(name)
        if not re.fullmatch(r"(?:[01][0-9]|2[0-3])_[0-5][0-9]", minute):
            raise ValueError("Choose a valid local minute")
        with self.lock:
            item = self._load(root)["sets"].get(name)
            if not item or item["state"] != "complete":
                raise ValueError("Completed time-check set not found")
            key = item["minutes"][minute]
            if not re.fullmatch("[0-9a-f]{64}", key):
                raise ValueError("Invalid generated audio identity")
            return root / "sets" / name / f"{key}.wav"

    def remove(self, root: Path, token: str) -> None:
        identifier(token)
        with self.lock:
            if self.worker and self.worker.is_alive():
                raise ValueError("Wait for speech generation to finish before removing comparisons")
            data = self._load(root)
            if token in data["sets"]:
                raise ValueError("A time-check set cannot be removed as a comparison")
            data["batches"].pop(token, None)
            self._write(root, data)
            shutil.rmtree(root / "samples" / token, ignore_errors=True)

    def activate(self, root: Path, name: str | None, reviewed: bool) -> None:
        with self.lock:
            data = self._load(root)
            if name is not None:
                identifier(name)
                item = data["sets"].get(name)
                if not reviewed or not item or item["state"] != "complete":
                    raise ValueError("Review samples and finish the complete set before activation")
                if len(item["minutes"]) != 1440:
                    raise ValueError("The generated set does not cover every minute")
                for key in set(item["minutes"].values()):
                    checked_audio((root / "sets" / name / f"{key}.wav").read_bytes())
                item["reviewed"] = True
            data["active_set"] = name
            self._write(root, data)
