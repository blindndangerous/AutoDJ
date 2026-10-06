from __future__ import annotations

import json
from pathlib import Path

import pytest

from autodj.index_manifest import (
    MANIFEST_NAME,
    IndexConsistencyError,
    UnsupportedIndexError,
    publish_generation,
    read_manifest,
    require_manifest,
)


def _write_pair(tracks: Path, vectors: Path, text: str = "data") -> None:
    tracks.write_text(f"tracks {text}", encoding="utf-8")
    vectors.write_text(f"vectors {text}", encoding="utf-8")


def _publish(index_dir: Path, base: int, text: str = "data") -> int:
    manifest = publish_generation(
        index_dir,
        base_generation=base,
        vector_count=0,
        write_files=lambda tracks, vectors: _write_pair(tracks, vectors, text),
    )
    return manifest.generation


def _files(index_dir: Path) -> list[str]:
    return sorted(path.name for path in index_dir.iterdir() if not path.name.endswith(".lock"))


def test_publish_keeps_the_new_and_the_replaced_generation_only(tmp_path: Path) -> None:
    assert [_publish(tmp_path, base) for base in (0, 1, 2)] == [1, 2, 3]

    assert _files(tmp_path) == [
        MANIFEST_NAME,
        "tracks.g00000000000000000002.db",
        "tracks.g00000000000000000003.db",
        "vectors.g00000000000000000002.index",
        "vectors.g00000000000000000003.index",
    ]
    manifest = read_manifest(tmp_path)
    assert manifest is not None
    assert manifest.generation == 3
    assert (tmp_path / manifest.tracks_file).read_text(encoding="utf-8") == "tracks data"


def test_publish_from_a_stale_base_is_refused_and_changes_nothing(tmp_path: Path) -> None:
    _publish(tmp_path, 0, "first")
    _publish(tmp_path, 1, "second")
    before = _files(tmp_path)

    with pytest.raises(IndexConsistencyError, match="published generation 2"):
        _publish(tmp_path, 1, "lost update")

    assert _files(tmp_path) == before
    manifest = read_manifest(tmp_path)
    assert manifest is not None and manifest.generation == 2


def test_failed_write_leaves_the_live_generation_and_no_temporary_files(tmp_path: Path) -> None:
    _publish(tmp_path, 0)
    before = _files(tmp_path)

    def fail(tracks: Path, vectors: Path) -> None:
        _write_pair(tracks, vectors)
        raise OSError("disk full")

    with pytest.raises(OSError, match="disk full"):
        publish_generation(tmp_path, base_generation=1, vector_count=0, write_files=fail)

    assert _files(tmp_path) == before


def _manifest_payload(**changes: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "schema_version": 4,
        "generation": 1,
        "vector_count": 2,
        "published_at": "2026-08-02T00:00:00+00:00",
        "tracks_sha256": "0" * 64,
        "vectors_sha256": "1" * 64,
    }
    payload.update(changes)
    return payload


def _write_manifest(index_dir: Path, payload: object) -> None:
    (index_dir / MANIFEST_NAME).write_text(json.dumps(payload), encoding="utf-8")


def test_older_manifest_schema_names_the_rebuild(tmp_path: Path) -> None:
    _write_manifest(tmp_path, _manifest_payload(schema_version=2, tracks_file="x"))

    with pytest.raises(UnsupportedIndexError, match=r"older AutoDJ.*autodj index --force"):
        read_manifest(tmp_path)


def test_schema_3_manifest_points_at_the_conversion_script(tmp_path: Path) -> None:
    _write_manifest(tmp_path, _manifest_payload(schema_version=3, tracks_file="x"))

    with pytest.raises(UnsupportedIndexError, match=r"older AutoDJ.*convert_index_v3_to_v4"):
        read_manifest(tmp_path)


@pytest.mark.parametrize(
    "payload",
    [
        [],
        _manifest_payload(schema_version=99),
        _manifest_payload(extra=1),
        _manifest_payload(generation="1"),
        _manifest_payload(generation=0),
        _manifest_payload(vector_count=-1),
        _manifest_payload(tracks_sha256="not hex"),
    ],
)
def test_malformed_manifest_is_refused(tmp_path: Path, payload: object) -> None:
    _write_manifest(tmp_path, payload)

    with pytest.raises(IndexConsistencyError):
        read_manifest(tmp_path)


def test_unparsable_manifest_is_refused(tmp_path: Path) -> None:
    (tmp_path / MANIFEST_NAME).write_text("{broken", encoding="utf-8")

    with pytest.raises(IndexConsistencyError, match="not valid JSON"):
        read_manifest(tmp_path)


def test_require_manifest_refuses_an_index_from_before_manifests(tmp_path: Path) -> None:
    (tmp_path / "tracks.db").touch()

    with pytest.raises(UnsupportedIndexError, match=r"no index-manifest.json"):
        require_manifest(tmp_path)


def test_require_manifest_refuses_generation_files_without_a_manifest(tmp_path: Path) -> None:
    (tmp_path / "vectors.g00000000000000000004.index").touch()

    with pytest.raises(IndexConsistencyError, match="copy the manifest too"):
        require_manifest(tmp_path)


def test_require_manifest_reports_an_empty_directory_as_missing(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError, match="run `autodj index`"):
        require_manifest(tmp_path)
