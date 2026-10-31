"""The app owns rendering; domains own their public refusal details."""

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import BaseModel

from autodj.api_errors import error_headers, install_error_handlers
from autodj.errors import FieldError


@pytest.mark.parametrize("field", ["name", "", None])
def test_field_error_keeps_status_message_and_field(field):
    """ADR-0022-02: domain refusals keep their status, message and field value."""
    app = FastAPI()
    install_error_handlers(app)

    @app.get("/refused")
    def refused():
        raise FieldError("Choose a saved item", field, status=409)

    response = TestClient(app).get("/refused")
    assert response.status_code == 409
    assert response.json() == {"detail": "Choose a saved item", "field": field}
    assert "cache-control" not in response.headers


def test_error_headers_belong_to_the_route():
    """ADR-0022-02: shared rendering preserves a route's cache policy."""
    app = FastAPI()
    install_error_handlers(app)

    @app.get("/private")
    @error_headers({"Cache-Control": "no-store"})
    def private():
        raise FieldError("Choose a spot", "spot", status=422)

    response = TestClient(app).get("/private")
    assert response.headers["cache-control"] == "no-store"
    assert response.json() == {"detail": "Choose a spot", "field": "spot"}


def test_error_without_field_keeps_detail_only():
    app = FastAPI()
    install_error_handlers(app)

    @app.get("/refused")
    def refused():
        raise FieldError("The file is in use", status=409, include_field=False)

    response = TestClient(app).get("/refused")
    assert response.status_code == 409
    assert response.json() == {"detail": "The file is in use"}


def test_request_list_validation_names_the_form_field():
    app = FastAPI()
    install_error_handlers(app)

    class Songs(BaseModel):
        songs: list[str]

    @app.post("/songs")
    def save(body: Songs):
        return body

    response = TestClient(app).post("/songs", json={"songs": [1]})
    assert response.status_code == 422
    assert response.json() == {
        "detail": "songs: Input should be a valid string",
        "field": "songs",
    }


@pytest.mark.parametrize(
    ("payload", "expected"),
    [
        ({}, {"detail": "body: Field required", "field": "body"}),
        ([], {"detail": "Input should be a valid dictionary or object to extract fields from"}),
    ],
)
def test_request_validation_distinguishes_body_field_from_body_location(payload, expected):
    app = FastAPI()
    install_error_handlers(app)

    class Form(BaseModel):
        body: str

    @app.post("/form")
    def save(body: Form):
        return body

    response = TestClient(app).post("/form", json=payload)
    assert response.status_code == 422
    assert response.json() == expected


def test_route_can_keep_distinct_error_cache_policies():
    """ADR-0022-02: one endpoint can retain different refusal cache policies."""
    app = FastAPI()
    install_error_handlers(app)

    class RuleRefused(FieldError):
        pass

    @app.get("/rule")
    @error_headers({"Cache-Control": "no-store"}, error_type=RuleRefused)
    def rule(known: bool):
        if known:
            raise RuleRefused("Rule is full", "clips")
        raise FieldError("Choose a file", "file")

    client = TestClient(app)
    assert client.get("/rule?known=true").headers["cache-control"] == "no-store"
    assert "cache-control" not in client.get("/rule?known=false").headers


def test_malformed_json_does_not_name_its_byte_offset_as_a_form_field():
    app = FastAPI()
    install_error_handlers(app)

    @app.post("/form")
    def save(body: dict):
        return body

    response = TestClient(app).post(
        "/form", content="{", headers={"Content-Type": "application/json"}
    )
    assert response.status_code == 422
    assert response.json() == {"detail": "JSON decode error"}
