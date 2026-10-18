"""Station-owned prepared editions: immutable audio and occurrence-bound approval."""

from __future__ import annotations

import copy
import hashlib
import json
import os
import tempfile
import threading
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import asdict, dataclass, is_dataclass
from pathlib import Path
from types import MappingProxyType, SimpleNamespace
from typing import Annotated, Any, Literal

import numpy as np
import soundfile as sf
from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictInt, StrictStr, model_validator

from autodj.break_audio import check_break_audio
from autodj.fsutil import atomic_write
from autodj.stereo import SAMPLE_RATE, audio_workspace, load_stereo

ID = Annotated[StrictStr, Field(pattern=r"^[0-9a-f]{32}$")]
MAX_UPLOAD_BYTES = 512 * 1024**2
MAX_ASSET_BYTES = 4 * 1024**3
MAX_SHORT_SECONDS = 300
MAX_SEGMENT_SECONDS = 1800


def _id() -> str:
    return uuid.uuid4().hex


class EditionError(ValueError):
    def __init__(self, message: str, status: int = 400) -> None:
        super().__init__(message)
        self.status = status


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class SourceAsset(_Model):
    id: ID
    file: Annotated[StrictStr, Field(pattern=r"^[0-9a-f]{32}\.wav$")]
    sha256: Annotated[StrictStr, Field(pattern=r"^[0-9a-f]{64}$")]
    seconds: float = Field(gt=0, le=MAX_SEGMENT_SECONDS, allow_inf_nan=False)
    bytes: StrictInt = Field(gt=0, le=MAX_UPLOAD_BYTES)
    mtime_ns: StrictInt
    label: StrictStr = Field(default="Speech", max_length=200)


class Occurrence(_Model):
    id: ID = Field(default_factory=_id)
    kind: Literal["song", "segment"] = "song"
    path: StrictStr = Field(default="", max_length=4096)
    asset_id: ID | None = None
    take_id: ID | None = None

    @model_validator(mode="after")
    def source(self) -> Occurrence:
        if self.kind == "song" and (not self.path or self.asset_id or self.take_id):
            raise ValueError("A song occurrence needs its library path")
        if self.kind == "segment" and (self.path or not self.asset_id or not self.take_id):
            raise ValueError("A segment occurrence needs its asset and take identities")
        return self


class ReviewReceipt(_Model):
    edition_id: ID
    take_id: ID
    asset_id: ID
    dependency_hash: Annotated[StrictStr, Field(pattern=r"^[0-9a-f]{64}$")]
    review_id: ID
    reviewed_revision: StrictInt = Field(ge=1)
    keep_complete: StrictBool
    omitted: StrictBool = False


class Take(_Model):
    id: ID = Field(default_factory=_id)
    asset_id: ID
    position: Literal["opening", "transition", "closing", "segment"]
    before_id: ID | None = None
    after_id: ID | None = None
    occurrence_id: ID | None = None
    placement: Literal["auto", "talk_up", "standalone"] = "auto"
    allow_outro: StrictBool = False
    label: StrictStr = Field(default="Speech", max_length=200)
    approval: ReviewReceipt | None = None


class Edition(_Model):
    id: ID = Field(default_factory=_id)
    show_id: ID
    name: StrictStr = Field(min_length=1, max_length=100)
    revision: StrictInt = Field(default=1, ge=1)
    occurrences: tuple[Occurrence, ...] = Field(default=(), max_length=500)
    takes: tuple[Take, ...] = Field(default=(), max_length=1000)
    assets: dict[str, SourceAsset] = Field(default_factory=dict)
    previously_started: StrictBool = False

    @model_validator(mode="after")
    def identities(self) -> Edition:
        for values in (self.occurrences, self.takes):
            if len({value.id for value in values}) != len(values):
                raise ValueError("Occurrence and take identities must be unique")
        bindings = [(take.position, take.before_id, take.after_id) for take in self.takes]
        if len(set(bindings)) != len(bindings):
            raise ValueError("Only one active take is allowed at each bound position")
        if any(key != asset.id or asset.file != f"{key}.wav" for key, asset in self.assets.items()):
            raise ValueError("Invalid edition asset identity")
        if any(take.asset_id not in self.assets for take in self.takes):
            raise ValueError("A take must reference an edition source asset")
        return self


class _File(_Model):
    schema_version: Literal[1] = 1
    editions: dict[str, Edition] = Field(default_factory=dict)


@dataclass(frozen=True)
class PreparedItem:
    occurrence_id: str
    path: Path
    kind: Literal["song", "clip"]
    seconds: float
    take_id: str | None = None
    label: str = ""


@dataclass(frozen=True)
class PreparedSpeech:
    take_id: str
    asset_id: str
    path: Path
    seconds: float
    placement: str
    allow_outro: bool
    label: str = ""


@dataclass(frozen=True)
class PreparedPlan:
    edition_id: str
    name: str
    revision: int
    items: tuple[PreparedItem, ...]
    handoffs: Mapping[tuple[str, str], tuple[PreparedSpeech, ...]]
    omissions: tuple[str, ...]
    estimated_seconds: float
    previously_started: bool


def _hash(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _content_hash(path: Path, hashes: dict[Path, str]) -> str:
    if path not in hashes:
        hashes[path] = _hash(path)
    return hashes[path]


def _bound(edition: Edition, take: Take) -> tuple[Occurrence, ...]:
    rows = edition.occurrences
    by_id = {row.id: index for index, row in enumerate(rows)}
    if take.position == "opening":
        valid = bool(rows) and take.before_id is None and take.after_id == rows[0].id
        selected = rows[:1]
    elif take.position == "closing":
        valid = bool(rows) and take.after_id is None and take.before_id == rows[-1].id
        selected = rows[-1:]
    elif take.position == "segment":
        index = by_id.get(take.occurrence_id or "", -1)
        valid = (
            index >= 0
            and rows[index].kind == "segment"
            and rows[index].take_id == take.id
            and rows[index].asset_id == take.asset_id
        )
        left = rows[index - 1].id if index > 0 else None
        right = rows[index + 1].id if 0 <= index < len(rows) - 1 else None
        valid = valid and left == take.before_id and right == take.after_id
        selected = rows[max(0, index - 1) : index + 2] if index >= 0 else ()
    else:
        left_index = by_id.get(take.before_id or "", -1)
        right_index = by_id.get(take.after_id or "", -1)
        valid = left_index >= 0 and right_index == left_index + 1
        selected = rows[left_index : right_index + 1] if valid else ()
    if not valid:
        raise EditionError("The take's occurrence placement changed; review or reattach it")
    return tuple(selected)


def relevant_signature(
    edition: Edition, take: Take, station: Any, *, hashes: dict[Path, str] | None = None
) -> str:
    """Only the reviewed take, its neighbours and effective rendering inputs."""
    from autodj.review_render import _Metadata

    if hashes is None:
        hashes = {}
    rows = _bound(edition, take)
    library = station.bridge.library
    entries = [SimpleNamespace(path=row.path) for row in rows if row.kind == "song"]
    metadata = _Metadata(
        getattr(getattr(station, "player", None), "_dj_cache", None), entries, station.cfg
    )
    dependencies: list[tuple[Any, ...]] = []
    for row in rows:
        if row.kind == "segment":
            asset = edition.assets.get(row.asset_id or "")
            if asset is None:
                raise EditionError("Adjacent segment audio is missing or invalid")
            source = Path(station.paths.edition_audio) / asset.file
            if (
                not source.is_file()
                or source.is_symlink()
                or _content_hash(source, hashes) != asset.sha256
            ):
                raise EditionError("Adjacent segment audio is missing or invalid")
            dependencies.append((row.id, "segment", asset.sha256))
            continue
        path = Path(row.path)
        if not path.is_file():
            raise EditionError("An adjacent song's audio is missing")
        entry = library.entry_for_path(row.path)
        if entry is None:
            raise EditionError("An adjacent song is no longer in the library")
        record = library.catalog.record_for_path(row.path) or {}
        lyrics = path.with_suffix(".lrc")
        dependencies.append(
            (
                row.id,
                row.path,
                _content_hash(path, hashes),
                library.catalog.points(row.path),
                asdict(metadata.get(row.path)),
                tuple(record.get(name) for name in ("kind", "loudness", "intro_lift_db")),
                tuple(getattr(entry, name, None) for name in ("length", "bpm", "key", "mode")),
                _content_hash(lyrics, hashes) if lyrics.is_file() else None,
            )
        )
    playback = station.cfg.playback
    mix = {
        name: getattr(playback, name, None)
        for name in (
            "transition_mode",
            "crossfade_seconds",
            "music_speed_percent",
            "loudness_processing",
            "liners_duck_db",
            "intro_lift_max_db",
            "import_external_cues",
            "beat_sync_fx",
            "key_sync_fx",
        )
    }
    for name in ("djmix", "transitions", "replaygain"):
        value = getattr(station.cfg, name, None)
        mix[name] = (
            asdict(value) if is_dataclass(value) and not isinstance(value, type) else repr(value)
        )
    config = station.cfg.breaks
    mix["speech_placement"] = (
        getattr(config, "dj_placement", None),
        getattr(config, "min_intro_seconds", None),
        getattr(config, "duck_db", None),
    )
    asset = edition.assets[take.asset_id]
    source = Path(station.paths.edition_audio) / asset.file
    if not source.is_file() or source.is_symlink() or _content_hash(source, hashes) != asset.sha256:
        raise EditionError("The take audio is missing or invalid")
    value = (
        take.asset_id,
        asset.sha256,
        take.position,
        take.before_id,
        take.after_id,
        take.occurrence_id,
        take.placement,
        take.allow_outro,
        dependencies,
        mix,
    )
    return hashlib.sha256(json.dumps(value, sort_keys=True, default=str).encode()).hexdigest()


class EditionStore:
    def __init__(self, path: Path, audio: Path) -> None:
        self.path, self.audio = Path(path), Path(audio)
        self._lock = threading.RLock()

    def _read(self) -> dict[str, Edition]:
        try:
            data = _File.model_validate_json(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {}
        except ValueError as exc:
            raise EditionError(
                "Prepared edition state is invalid; nothing was overwritten", 503
            ) from exc
        if any(key != edition.id for key, edition in data.editions.items()):
            raise EditionError("Prepared edition identities are invalid", 503)
        return dict(data.editions)

    def _save(self, editions: Mapping[str, Edition]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write(self.path, _File(editions=dict(editions)).model_dump_json(indent=2) + "\n")

    def all(self) -> dict[str, Edition]:
        with self._lock:
            return copy.deepcopy(self._read())

    def get(self, edition_id: str) -> Edition:
        with self._lock:
            edition = self._read().get(edition_id)
            if edition is None:
                raise EditionError("That prepared edition no longer exists", 404)
            return edition.model_copy(deep=True)

    def _current(self, editions: Mapping[str, Edition], identity: str, revision: int) -> Edition:
        edition = editions.get(identity)
        if edition is None:
            raise EditionError("That prepared edition no longer exists", 404)
        if edition.revision != revision:
            raise EditionError("Edition changed; reload before saving", 409)
        return edition

    def create(self, show_id: str, show: Any, *, name: str | None = None) -> Edition:
        if show is None:
            raise EditionError("That saved show no longer exists", 404)
        edition = Edition(
            show_id=show_id,
            name=name or show.name,
            occurrences=tuple(Occurrence(path=path) for path in show.songs),
        )
        with self._lock:
            editions = self._read()
            if len(editions) >= 200:
                raise EditionError("A station keeps at most 200 prepared editions")
            editions[edition.id] = edition
            self._save(editions)
        return edition

    def edit(
        self,
        edition_id: str,
        expected_revision: int,
        *,
        name: str | None = None,
        occurrences: Sequence[Occurrence] | None = None,
    ) -> Edition:
        with self._lock:
            editions = self._read()
            edition = self._current(editions, edition_id, expected_revision)
            values = edition.model_dump()
            values.update(revision=edition.revision + 1)
            if name is not None:
                values["name"] = name
            if occurrences is not None:
                known = {item.id: item for item in edition.occurrences if item.kind == "segment"}
                if any(
                    item.kind == "segment" and known.get(item.id) != item for item in occurrences
                ):
                    raise EditionError("Import segment audio before adding it to an edition")
                values["occurrences"] = tuple(occurrences)
            changed = Edition.model_validate(values)
            editions[edition_id] = changed
            self._save(editions)
            return changed

    def asset(self, asset_id: str) -> SourceAsset | None:
        for edition in self.all().values():
            if asset_id in edition.assets:
                return edition.assets[asset_id]
        return None

    def asset_path(self, asset_id: str) -> Path:
        asset = self.asset(asset_id)
        if asset is None:
            raise EditionError("That edition audio no longer exists", 404)
        path = self.audio / asset.file
        if path.is_symlink() or not path.is_file():
            raise EditionError("That edition audio is missing", 404)
        return path

    @staticmethod
    def _check_binding(
        edition: Edition,
        position: str,
        before_id: str | None,
        after_id: str | None,
        take_id: str | None,
    ) -> None:
        if any(
            (item.position, item.before_id, item.after_id) == (position, before_id, after_id)
            and item.id != take_id
            for item in edition.takes
        ):
            raise EditionError(
                "This position already has a take. Use Record again or replace the existing take",
                409,
            )

    def import_take(
        self,
        edition_id: str,
        expected_revision: int,
        source: Path,
        *,
        position: Literal["opening", "transition", "closing", "segment"],
        before_id: str | None = None,
        after_id: str | None = None,
        placement: Literal["auto", "talk_up", "standalone"] = "auto",
        allow_outro: bool = False,
        take_id: str | None = None,
        index: int | None = None,
        label: str = "Speech",
        cancelled: Callable[[], bool] = lambda: False,
    ) -> Edition:
        if cancelled():
            raise EditionError("Recording import cancelled")
        with self._lock:
            current = self._current(self._read(), edition_id, expected_revision)
            if position != "segment":
                self._check_binding(current, position, before_id, after_id, take_id)
        limit = MAX_SEGMENT_SECONDS if position == "segment" else MAX_SHORT_SECONDS
        if source.stat().st_size > MAX_UPLOAD_BYTES:
            raise EditionError("The audio upload exceeds 512 MiB", 413)
        try:
            check_break_audio(source, limit)
        except ValueError as exc:
            if position != "segment":
                raise EditionError(
                    "Use a recording up to five minutes, or import a longer standalone segment"
                ) from exc
            raise EditionError(str(exc)) from exc
        self.audio.mkdir(parents=True, exist_ok=True)
        if self.audio.is_symlink():
            raise EditionError("Edition audio storage must be a real directory")
        identifier = _id()
        destination = self.audio / f"{identifier}.wav"
        published = False
        with tempfile.TemporaryDirectory(prefix=".import-", dir=self.audio) as scratch_name:
            scratch = Path(scratch_name)
            temporary = scratch / "normalized.wav"
            with audio_workspace(scratch, max_bytes=1024**3):
                audio = load_stereo(str(source), SAMPLE_RATE)
                if not 0 < len(audio) <= round(limit * SAMPLE_RATE):
                    raise EditionError(f"This recording must be at most {limit} seconds")
                audible = False
                with sf.SoundFile(
                    temporary, "w", samplerate=SAMPLE_RATE, channels=2, subtype="PCM_16"
                ) as output:
                    for at in range(0, len(audio), 10 * SAMPLE_RATE):
                        if cancelled():
                            raise EditionError("Recording import cancelled")
                        block = audio[at : at + 10 * SAMPLE_RATE]
                        if not np.isfinite(block).all():
                            raise EditionError("The audio contains invalid samples")
                        audible = audible or bool(np.max(np.abs(block)) > 1e-5)
                        output.write(block)
                if not audible:
                    raise EditionError("The recording contains no audible speech")
                seconds = len(audio) / SAMPLE_RATE
            stat = temporary.stat()
            asset = SourceAsset(
                id=identifier,
                file=destination.name,
                sha256=_hash(temporary),
                seconds=seconds,
                bytes=stat.st_size,
                mtime_ns=stat.st_mtime_ns,
                label=label,
            )
            try:
                with self._lock:
                    editions = self._read()
                    edition = self._current(editions, edition_id, expected_revision)
                    if (
                        sum(
                            item.bytes
                            for saved in editions.values()
                            for item in saved.assets.values()
                        )
                        + asset.bytes
                        > MAX_ASSET_BYTES
                    ):
                        raise EditionError("Prepared edition audio storage is full")
                    takes = list(edition.takes)
                    old = next((take for take in takes if take.id == take_id), None)
                    if take_id and old is None:
                        raise EditionError("That take no longer exists", 404)
                    if old and old.position != position:
                        raise EditionError("Re-record this take at its existing position")
                    identity = old.id if old else _id()
                    occurrences = list(edition.occurrences)
                    own = old.occurrence_id if old else None
                    if position == "segment":
                        if old and old.position != "segment":
                            raise EditionError("A transition take cannot be changed into a segment")
                        at = next(
                            (n for n, row in enumerate(occurrences) if row.id == own),
                            len(occurrences) if index is None else index,
                        )
                        if not 0 <= at <= len(occurrences):
                            raise EditionError("Choose a valid segment position")
                        own = own or _id()
                        occurrence = Occurrence(
                            id=own, kind="segment", asset_id=identifier, take_id=identity
                        )
                        if old:
                            occurrences[at] = occurrence
                        else:
                            occurrences.insert(at, occurrence)
                        before_id = occurrences[at - 1].id if at else None
                        after_id = occurrences[at + 1].id if at + 1 < len(occurrences) else None
                        placement = "standalone"
                    self._check_binding(edition, position, before_id, after_id, identity)
                    take = Take(
                        id=identity,
                        asset_id=identifier,
                        position=position,
                        before_id=before_id,
                        after_id=after_id,
                        occurrence_id=own,
                        placement=placement,
                        allow_outro=allow_outro,
                        label=label,
                    )
                    takes = (
                        [take if item.id == identity else item for item in takes]
                        if old
                        else [*takes, take]
                    )
                    changed = Edition.model_validate(
                        {
                            **edition.model_dump(),
                            "revision": edition.revision + 1,
                            "occurrences": occurrences,
                            "takes": takes,
                            "assets": {**edition.assets, identifier: asset},
                        }
                    )
                    _bound(changed, take)
                    if cancelled():
                        raise EditionError("Recording import cancelled")
                    os.replace(temporary, destination)
                    published = True
                    editions[edition_id] = changed
                    self._save(editions)
                    return changed
            except BaseException:
                if published:
                    destination.unlink(missing_ok=True)
                raise

    def take_status(
        self, edition: Edition, take: Take, station: Any, *, hashes: dict[Path, str] | None = None
    ) -> str:
        if hashes is None:
            hashes = {}
        asset = edition.assets.get(take.asset_id)
        if asset is None or not (self.audio / asset.file).is_file():
            return "missing"
        try:
            if (self.audio / asset.file).is_symlink() or _content_hash(
                self.audio / asset.file, hashes
            ) != asset.sha256:
                return "invalid"
            signature = relevant_signature(edition, take, station, hashes=hashes)
        except (EditionError, OSError, ValueError):
            return "needs_review"
        return (
            "ready"
            if take.approval and take.approval.dependency_hash == signature
            else "needs_review"
        )

    def approve(
        self, edition_id: str, take_id: str, receipt: ReviewReceipt, station: Any
    ) -> Edition:
        if not receipt.keep_complete:
            raise EditionError("Approval requires a complete review of the take")
        if receipt.omitted:
            raise EditionError("Missing, omitted or placeholder audio cannot establish approval")
        with self._lock:
            editions = self._read()
            edition = editions.get(edition_id)
            if edition is None:
                raise EditionError("That prepared edition no longer exists", 404)
            take = next((item for item in edition.takes if item.id == take_id), None)
            if take is None or (receipt.edition_id, receipt.take_id, receipt.asset_id) != (
                edition_id,
                take_id,
                take.asset_id,
            ):
                raise EditionError("The reviewed take changed; review it again", 409)
            if relevant_signature(edition, take, station) != receipt.dependency_hash:
                raise EditionError("The take's relevant inputs changed; review it again", 409)
            approved = take.model_copy(update={"approval": receipt})
            changed = edition.model_copy(
                update={
                    "revision": edition.revision + 1,
                    "takes": tuple(
                        approved if item.id == take_id else item for item in edition.takes
                    ),
                }
            )
            editions[edition_id] = changed
            self._save(editions)
            return changed

    def remove_take(self, edition_id: str, take_id: str, expected_revision: int) -> Edition:
        with self._lock:
            editions = self._read()
            edition = self._current(editions, edition_id, expected_revision)
            take = next((item for item in edition.takes if item.id == take_id), None)
            if take is None:
                raise EditionError("That take no longer exists", 404)
            changed = edition.model_copy(
                update={
                    "revision": edition.revision + 1,
                    "takes": tuple(item for item in edition.takes if item.id != take_id),
                    "occurrences": tuple(
                        item for item in edition.occurrences if item.id != take.occurrence_id
                    ),
                }
            )
            editions[edition_id] = changed
            self._save(editions)
            return changed

    def mark_started(self, edition_id: str, *, confirm_reair: bool = False) -> Edition:
        with self._lock:
            editions = self._read()
            edition = editions.get(edition_id)
            if edition is None:
                raise EditionError("That prepared edition no longer exists", 404)
            if edition.previously_started and not confirm_reair:
                raise EditionError(
                    "This edition's speech may have aired; confirmation is required", 409
                )
            changed = edition.model_copy(
                update={"previously_started": True, "revision": edition.revision + 1}
            )
            editions[edition_id] = changed
            self._save(editions)
            return changed

    def delete(self, edition_id: str, expected_revision: int) -> None:
        with self._lock:
            editions = self._read()
            self._current(editions, edition_id, expected_revision)
            removed = editions.pop(edition_id)
            self._save(editions)
            kept = {key for edition in editions.values() for key in edition.assets}
            for identity, asset in removed.assets.items():
                if identity not in kept:
                    (self.audio / asset.file).unlink(missing_ok=True)

    def runtime_plan(
        self,
        edition_id: str,
        station: Any,
        *,
        include_pending: bool = False,
        from_occurrence: str | None = None,
    ) -> PreparedPlan:
        edition = self.get(edition_id)
        selected = edition.occurrences
        if from_occurrence is not None:
            at = next((n for n, row in enumerate(selected) if row.id == from_occurrence), None)
            if at is None:
                raise EditionError("Choose an occurrence in this edition")
            selected = selected[at:]
        valid_ids = {row.id for row in selected}
        omissions: list[str] = []
        available: dict[str, PreparedSpeech] = {}
        hashes: dict[Path, str] = {}
        for take in edition.takes:
            status = self.take_status(edition, take, station, hashes=hashes)
            try:
                _bound(edition, take)
            except EditionError:
                status = "unattached"
            if status == "ready" or (include_pending and status == "needs_review"):
                asset = edition.assets[take.asset_id]
                available[take.id] = PreparedSpeech(
                    take.id,
                    asset.id,
                    self.audio / asset.file,
                    asset.seconds,
                    take.placement,
                    take.allow_outro,
                    take.label,
                )
            else:
                omissions.append(f"{take.label}: {status}; speech skipped")
        items: list[PreparedItem] = []
        handoffs: dict[tuple[str, str], tuple[PreparedSpeech, ...]] = {}
        for take in edition.takes:
            speech = available.get(take.id)
            if speech is None:
                continue
            if (
                take.position == "opening"
                and selected
                and selected[0].id == edition.occurrences[0].id
            ):
                items.append(
                    PreparedItem(take.id, speech.path, "clip", speech.seconds, take.id, take.label)
                )
            elif (
                take.position == "transition"
                and take.before_id in valid_ids
                and take.after_id in valid_ids
            ):
                pair = (take.before_id or "", take.after_id or "")
                handoffs[pair] = (*handoffs.get(pair, ()), speech)
        for row in selected:
            if row.kind == "song":
                entry = station.bridge.library.entry_for_path(row.path)
                if entry is None or not Path(row.path).is_file():
                    omissions.append("Missing song skipped")
                    continue
                points = station.bridge.library.catalog.points(row.path)
                length = float(entry.length or 0)
                seconds = max(
                    0,
                    float(points.get("segue") or points.get("outro") or points.get("end") or length)
                    - float(points.get("start") or 0),
                )
                rate = 1 + float(getattr(station.cfg.playback, "music_speed_percent", 0)) / 100
                items.append(
                    PreparedItem(
                        row.id,
                        Path(row.path),
                        "song",
                        seconds / max(0.01, rate),
                        label=entry.display_name,
                    )
                )
            elif row.take_id in available:
                speech = available[row.take_id]
                items.append(
                    PreparedItem(
                        row.id, speech.path, "clip", speech.seconds, row.take_id, speech.label
                    )
                )
        for take in edition.takes:
            if take.position == "closing" and take.id in available and selected:
                speech = available[take.id]
                items.append(
                    PreparedItem(take.id, speech.path, "clip", speech.seconds, take.id, take.label)
                )
        estimate = sum(item.seconds for item in items) + sum(
            speech.seconds for group in handoffs.values() for speech in group
        )
        return PreparedPlan(
            edition.id,
            edition.name,
            edition.revision,
            tuple(items),
            MappingProxyType(handoffs),
            tuple(omissions),
            estimate,
            edition.previously_started,
        )


_STORE_LOCK = threading.Lock()


def edition_store(station: Any) -> EditionStore:
    """One lock owner for a station's editors, review adapter and runner."""
    with _STORE_LOCK:
        store = getattr(station, "edition_store", None)
        if store is None:
            store = EditionStore(station.paths.editions, station.paths.edition_audio)
            station.edition_store = store
        return store


def backup_snapshot(audio: Path) -> tuple[str | None, set[Path]]:
    """Freeze the manifest and its immutable source membership together."""
    store = EditionStore(audio.parent / "editions.json", audio)
    if not store.path.is_file():
        return None, set()
    editions = store.all()
    document = _File(editions=editions).model_dump_json(indent=2) + "\n"
    files = {
        audio / asset.file for edition in editions.values() for asset in edition.assets.values()
    }
    return document, files
