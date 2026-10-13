"""Private web-managed relay definitions, layered over TOML until first save."""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any

from autodj.config import RelayConfig, relay_password_variable, station_relay_password_variable
from autodj.secret_files import write_private_file

logger = logging.getLogger(__name__)

_FIELDS = ("type", "host", "port", "mount", "stream_id", "user", "bitrate", "public", "enabled")
_MISSING_CREDENTIAL = "autodj-relay-credential-unconfigured"


def credential_is_missing(value: str) -> bool:
    """Whether *value* marks a disabled relay that has no source credential."""
    return value == _MISSING_CREDENTIAL


def password_variable(slug: str, name: str) -> str:
    return (
        relay_password_variable(name)
        if slug == "default"
        else station_relay_password_variable(slug, name)
    )


def load_relays(path: Path, baseline: dict[str, RelayConfig], slug: str) -> dict[str, RelayConfig]:
    """Load the authoritative web relay list, or return the TOML baseline."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return baseline
    except (OSError, UnicodeError, json.JSONDecodeError):
        logger.error("Could not read saved relay settings; refusing to reactivate TOML relays")
        return {}
    if (
        not isinstance(data, dict)
        or data.get("version") != 1
        or not isinstance(data.get("relays"), list)
    ):
        logger.error("Invalid saved relay settings; refusing to reactivate TOML relays")
        return {}
    loaded: dict[str, RelayConfig] = {}
    try:
        for item in data["relays"]:
            if not isinstance(item, dict):
                raise ValueError("relay entry must be an object")
            name = item.get("name")
            if not isinstance(name, str) or name in loaded:
                raise ValueError("relay name must be a unique string")
            password = item.get("password")
            variable = password_variable(slug, name) if isinstance(name, str) else ""
            if variable in os.environ:
                password = os.environ[variable]
            elif password is None and name in baseline:
                password = baseline[name].password
            if password is None:
                password = _MISSING_CREDENTIAL
            if not isinstance(password, str):
                raise ValueError("relay password is missing")
            values = {key: item[key] for key in _FIELDS if key in item}
            if credential_is_missing(password):
                values["enabled"] = False
            loaded[name] = RelayConfig.from_table(name, {**values, "password": password})
    except (KeyError, TypeError, ValueError) as exc:
        logger.error(
            "Invalid saved relay settings (%s); refusing to reactivate TOML relays",
            type(exc).__name__,
        )
        return {}
    return loaded


def save_relays(path: Path, relays: dict[str, RelayConfig], slug: str) -> None:
    """Atomically save definitions with env-managed passwords kept out of the file."""
    old: dict[str, Any] = {}
    try:
        prior = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        prior = None
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError("saved relay settings are unreadable") from exc
    if prior is not None:
        if (
            not isinstance(prior, dict)
            or prior.get("version") != 1
            or not isinstance(prior.get("relays"), list)
            or any(not isinstance(item, dict) or not isinstance(item.get("name"), str)
                   for item in prior["relays"])
        ):
            raise ValueError("saved relay settings are invalid")
        try:
            names = set()
            for item in prior["relays"]:
                name = item["name"]
                if name in names:
                    raise ValueError("duplicate relay name")
                names.add(name)
                password = item.get("password")
                variable = password_variable(slug, name)
                if variable in os.environ:
                    password = os.environ[variable]
                elif password is None:
                    password = _MISSING_CREDENTIAL
                RelayConfig.from_table(
                    name,
                    {**{key: item[key] for key in _FIELDS if key in item}, "password": password},
                )
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError("saved relay settings are invalid") from exc
        old = {item["name"]: item for item in prior["relays"]}
    entries = []
    for name, cfg in relays.items():
        item: dict[str, Any] = {"name": name, **{key: getattr(cfg, key) for key in _FIELDS}}
        variable = password_variable(slug, name)
        if variable in os.environ:
            if isinstance(old.get(name), dict) and "password" in old[name]:
                item["password"] = old[name]["password"]
        else:
            item["password"] = cfg.password
        entries.append(item)
    try:
        write_private_file(
            path, json.dumps({"version": 1, "relays": entries}, ensure_ascii=True, indent=2) + "\n"
        )
    except OSError as exc:
        raise OSError("Could not save relay settings") from exc


def public_relay(cfg: RelayConfig, status: dict[str, Any], slug: str) -> dict[str, Any]:
    """Return editable relay fields and a password presence flag, never its value."""
    return {
        "name": cfg.name,
        **{key: getattr(cfg, key) for key in _FIELDS},
        "password_configured": bool(
            os.environ.get(password_variable(slug, cfg.name))
            or (cfg.password and not credential_is_missing(cfg.password))
        ),
        "password_managed": password_variable(slug, cfg.name) in os.environ,
        **{key: status.get(key) for key in ("state", "error", "title_error", "target")},
    }
