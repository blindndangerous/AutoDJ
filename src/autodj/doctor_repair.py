"""Conservative, reversible repairs for config errors found by ``doctor``."""

from __future__ import annotations

import os
import re
import tomllib
import uuid
from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path

from autodj.config import unknown_config_keys
from autodj.fsutil import atomic_write


@dataclass
class ConfigRepair:
    """A proposed removal of obsolete keys from one TOML file."""

    path: Path
    keys_by_section: dict[str, tuple[str, ...]]
    original: bytes
    replacement: bytes

    def apply(self) -> Path:
        """Back up the original, then atomically write the repaired file."""
        current = self.path.read_bytes()
        if current != self.original:
            raise RuntimeError(f"Config changed since repair was planned: {self.path}")

        backup = self._create_backup()
        # Detect edits made while the backup was being created.
        if self.path.read_bytes() != self.original:
            backup.unlink(missing_ok=True)
            raise RuntimeError(f"Config changed since repair was planned: {self.path}")
        mode = self.path.stat().st_mode & 0o777
        atomic_write(self.path, self.replacement, mode=mode)
        return backup

    def _create_backup(self) -> Path:
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0)
        for _ in range(100):
            backup = self.path.with_name(f"{self.path.name}.doctor-{uuid.uuid4().hex}.bak")
            try:
                fd = os.open(backup, flags, 0o600)
            except FileExistsError:
                continue
            try:
                with os.fdopen(fd, "wb") as handle:
                    handle.write(self.original)
                    handle.flush()
                    os.fsync(handle.fileno())
            except BaseException:
                backup.unlink(missing_ok=True)
                raise
            return backup
        raise FileExistsError(f"Could not create a unique config backup beside {self.path}")


_HEADER = re.compile(r"^\s*\[([^\[\]]+)\]\s*(?:#.*)?(?:\r?\n)?$")
_ASSIGNMENT = re.compile(r"^(\s*)([A-Za-z0-9_-]+)(\s*=\s*.*?)(\r?\n)?$")


def plan_unknown_key_repairs(config_path: str | None) -> list[ConfigRepair]:
    """Plan safe key removals from the same config files used by ``load_config``.

    The planner only removes simple, single-line assignments in the exact
    section. Any syntax it cannot prove safe is left for manual repair.
    """
    candidate = Path(config_path) if config_path is not None else Path("config.toml")
    paths = [candidate]
    if candidate.exists():
        local = candidate.parent / "config.local.toml"
        if local.exists():
            paths.append(local)

    unique_paths: list[Path] = []
    seen: set[str] = set()
    for path in paths:
        identity = os.path.normcase(os.path.abspath(path))
        if identity not in seen and not path.is_symlink():
            seen.add(identity)
            unique_paths.append(path)

    repairs: list[ConfigRepair] = []
    for path in unique_paths:
        if not path.exists():
            continue
        original = path.read_bytes()
        try:
            text = original.decode("utf-8")
            parsed = tomllib.loads(text)
        except (UnicodeDecodeError, tomllib.TOMLDecodeError):
            continue
        keys_by_section = unknown_config_keys(parsed)
        if not keys_by_section:
            continue

        lines = text.splitlines(keepends=True)
        current_section: str | None = None
        removed: set[str] = set()
        output: list[str] = []
        unsafe = False
        for line in lines:
            stripped = line.lstrip()
            if stripped.startswith("[["):
                current_section = None
                output.append(line)
                continue
            header = _HEADER.match(line)
            if header:
                current_section = header.group(1).strip()
                output.append(line)
                continue
            match = _ASSIGNMENT.match(line)
            if (
                current_section in keys_by_section
                and match
                and match.group(2) in keys_by_section[current_section]
            ):
                # Parse the assignment on its own. This rejects multiline
                # values, dotted keys, and syntax we cannot safely remove.
                try:
                    tomllib.loads(f"[{current_section}]\n{line}")
                except tomllib.TOMLDecodeError:
                    unsafe = True
                    break
                removed.add(f"{current_section}.{match.group(2)}")
                continue
            output.append(line)
        expected_removed = {
            f"{section}.{key}" for section, keys in keys_by_section.items() for key in keys
        }
        if unsafe or removed != expected_removed:
            continue
        replacement = "".join(output).encode("utf-8")
        try:
            after = tomllib.loads(replacement.decode("utf-8"))
        except (UnicodeDecodeError, tomllib.TOMLDecodeError):
            continue
        expected = deepcopy(parsed)
        for section, keys in keys_by_section.items():
            for key in keys:
                expected[section].pop(key, None)
        if after != expected:
            continue
        repairs.append(ConfigRepair(path, keys_by_section, original, replacement))
    return repairs
