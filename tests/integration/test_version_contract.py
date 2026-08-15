import pytest

import autodj.server as server
from autodj.server import create_app
from autodj.version import current_version

BUILT_FILES = (
    "index.html",
    "app.js",
    "app.css",
    "bitcrusher-worklet.js",
    "stutter-worklet.js",
    "freeze-worklet.js",
    "glitch-worklet.js",
    "build-info.json",
)


def write_bundle(directory, prefix: str) -> None:
    directory.mkdir()
    for name in BUILT_FILES:
        contents = '{"version":"0.15.0"}\n' if name == "build-info.json" else f"{prefix}:{name}"
        (directory / name).write_text(contents, encoding="utf-8")


def test_api_version_derives_from_project_metadata(bridge) -> None:
    assert create_app(bridge).version == current_version()


def test_api_rejects_a_stale_built_bundle(bridge, tmp_path, monkeypatch) -> None:
    static_dist = tmp_path / "static_dist"
    write_bundle(static_dist, "built")
    (static_dist / "build-info.json").write_text('{"version":"0.14.0"}\n', encoding="utf-8")
    monkeypatch.setattr(server, "_PACKAGE_DIR", tmp_path)

    with pytest.raises(RuntimeError, match=r"bundle version 0\.14\.0.*runtime version 0\.15\.0"):
        create_app(bridge)


@pytest.mark.parametrize("missing", BUILT_FILES)
async def test_api_falls_back_to_source_when_built_bundle_is_partial(
    bridge, tmp_path, monkeypatch, missing
) -> None:
    static = tmp_path / "static"
    write_bundle(static, "source")
    static_dist = tmp_path / "static_dist"
    write_bundle(static_dist, "built")
    (static_dist / missing).unlink()
    monkeypatch.setattr(server, "_PACKAGE_DIR", tmp_path)

    app = create_app(bridge)

    index_route = next(route for route in app.routes if getattr(route, "path", None) == "/")
    response = await index_route.endpoint()
    assert response.body == b"source:index.html"
    for path in (
        "/app.js",
        "/app.css",
        "/bitcrusher-worklet.js",
        "/stutter-worklet.js",
        "/freeze-worklet.js",
        "/glitch-worklet.js",
    ):
        route = next(route for route in app.routes if getattr(route, "path", None) == path)
        asset_response = await route.endpoint()
        assert asset_response.path == static / path.removeprefix("/")
