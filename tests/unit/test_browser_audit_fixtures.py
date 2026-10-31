"""Audit pages retain production markup and substitute only their startup module."""

from __future__ import annotations

import importlib
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from autodj import review_source


@pytest.mark.parametrize(
    ("module_name", "factory_name", "page_path", "startup", "other_startup"),
    [
        (
            "programme_review_fixture",
            "build",
            "/review-audit",
            "window.reviewAudit =",
            "window.editionAudit =",
        ),
        (
            "prepared_edition_fixture",
            "application",
            "/edition-audit",
            "window.editionAudit =",
            "window.reviewAudit =",
        ),
    ],
)
def test_audit_preserves_page_and_serves_its_own_startup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    module_name: str,
    factory_name: str,
    page_path: str,
    startup: str,
    other_startup: str,
) -> None:
    root = Path(__file__).resolve().parents[2]
    monkeypatch.syspath_prepend(str(root / "tests/playwright"))
    # Restore the global capture function replaced by the standalone fixture.
    original_capture = review_source.capture_review
    try:
        module = importlib.import_module(module_name)
        app = getattr(module, factory_name)(tmp_path, 8893)

        with TestClient(app, base_url="http://127.0.0.1:8893") as client:
            page = client.get(page_path)
            assert page.status_code == 200
            assert page.text == (root / "src/autodj/static/index.html").read_text(encoding="utf-8")

            script = client.get("/app.js")
            assert script.status_code == 200
            assert script.headers["content-type"].startswith("text/javascript")
            assert startup in script.text
            assert other_startup not in script.text
            assert client.get("/modules/theme-init.js").status_code == 200
    finally:
        review_source.capture_review = original_capture
