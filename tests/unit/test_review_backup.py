"""Derived reviews do not become backed-up programme source assets."""

import zipfile

from autodj.backup import create_backup
from tests.unit.test_station_backup import _config


def test_backup_keeps_saved_shows_but_excludes_derived_reviews(tmp_path):
    """ADR-0014-06: review artifacts are private derived data, excluded from backups."""
    cfg = _config(tmp_path)
    for slug in ["default", "nightowl"]:
        station = cfg.index.stations_dir / slug
        station.mkdir(parents=True, exist_ok=True)
        (station / "shows.json").write_text('{"schema_version":1,"shows":{}}')
        review = station / "reviews" / ("a" * 32)
        review.mkdir(parents=True)
        (review / "review.wav").write_bytes(b"private derived audio")
        (review / "manifest.json").write_text('{"id":"private"}')
    archive = create_backup(cfg, tmp_path / "backup.zip", force=False)
    with zipfile.ZipFile(archive) as bundle:
        names = bundle.namelist()
    assert sum(name.endswith("/shows.json") for name in names) == 2
    assert not any("/reviews/" in name or name.endswith("/review.wav") for name in names)
