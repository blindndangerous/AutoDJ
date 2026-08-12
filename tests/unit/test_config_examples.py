from pathlib import Path

from autodj.config import ENVIRONMENT_OVERLAY, load_config

ROOT = Path(__file__).resolve().parents[2]


def test_base_example_loads_without_environment(tmp_path: Path) -> None:
    base = tmp_path / "renamed-base.toml"
    base.write_bytes((ROOT / "config.toml.example").read_bytes())

    cfg = load_config(base, environ={})
    assert cfg.library.music_dir == Path("Z:/Music")
    assert cfg.index.index_dir == Path("index")
    assert cfg.index.model_dir == Path("models")
    assert cfg.server.host == "127.0.0.1"
    assert cfg.server.port == 8080


def test_local_example_is_a_valid_overlay(tmp_path: Path) -> None:
    base = tmp_path / "config.toml"
    base.write_bytes((ROOT / "config.toml.example").read_bytes())
    (tmp_path / "config.local.toml").write_bytes((ROOT / "config.local.toml.example").read_bytes())
    cfg = load_config(base, environ={})
    assert cfg.library.music_dir == Path("/srv/music")
    assert cfg.index.index_dir == Path("/srv/autodj/index")


def test_examples_describe_optional_current_index_artifacts() -> None:
    for filename in ("config.toml.example", "config.local.toml.example"):
        text = (ROOT / filename).read_text(encoding="utf-8")
        assert "may contain" in text
        assert "vectors.index" in text
        assert "tracks.db (SQLite)" in text
        assert "dj_meta.db (SQLite)" in text
        assert "web_state.json" in text
        assert "<index_dir>/<name>/liners/" in text
        assert "metadata.json" not in text


def test_local_example_describes_overlay_for_any_loaded_base_filename() -> None:
    text = (ROOT / "config.local.toml.example").read_text(encoding="utf-8")
    assert "sibling base configuration file is loaded" in text
    assert "loaded only when a sibling config.toml exists" not in text


def test_documented_environment_variables_exactly_match_loader_contract() -> None:
    text = (ROOT / "config.toml.example").read_text(encoding="utf-8")
    start = "# BEGIN ENVIRONMENT OVERRIDES"
    end = "# END ENVIRONMENT OVERRIDES"
    assert text.count(start) == 1
    assert text.count(end) == 1
    block = text.split(start, 1)[1].split(end, 1)[0]
    documented = {
        line.removeprefix("#").strip()
        for line in block.splitlines()
        if line.removeprefix("#").strip()
    }
    assert documented == set(ENVIRONMENT_OVERLAY)
