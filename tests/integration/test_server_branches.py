"""Branch-coverage tests for autodj.server endpoints.

Targets validate_name 400 paths, profile-not-found 404 paths, liner
upload/delete edge cases, and other small uncovered branches.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient
from httpx2 import Headers
from starlette.websockets import WebSocketDisconnect

from autodj.config import ServerConfig
from autodj.security import COOKIE_NAME, SecurityPolicy
from autodj.server import PlayerBridge, create_app

from ._helpers import _make_player_mock, _make_sim_mock

_TEST_ACCESS_TOKEN = "task10-test-access-token-is-32-bytes"
_TEST_DEVICE_ID = "d" * 32


def _pair(client: TestClient, *, name: str = "Test browser"):
    """Pair client through public API using current short-lived code."""
    code = client.app.state.security_policy.current_pairing_code()
    return client.post("/api/pair", json={"code": code, "device_name": name})


def _security_client(*, secure_cookie: bool = False) -> TestClient:
    scheme = "https" if secure_cookie else "http"
    origin = f"{scheme}://testserver"
    player = _make_player_mock()
    player._cfg.server = ServerConfig(
        access_token=_TEST_ACCESS_TOKEN,
        allowed_hosts=["testserver"],
        allowed_origins=[origin],
    )
    bridge = PlayerBridge(player=player, sim=_make_sim_mock())
    return TestClient(
        create_app(bridge, secure_cookie=secure_cookie),
        base_url=origin,
        headers={"Host": "testserver", "Origin": origin},
    )


def _security_app():
    player = _make_player_mock()
    player._cfg.server = ServerConfig(
        access_token=_TEST_ACCESS_TOKEN,
        allowed_hosts=["testserver"],
        allowed_origins=["http://testserver"],
    )
    return create_app(PlayerBridge(player=player, sim=_make_sim_mock()))


def _security_client_and_bridge(**server: Any) -> tuple[TestClient, PlayerBridge]:
    player = _make_player_mock()
    player._cfg.server = ServerConfig(
        access_token=_TEST_ACCESS_TOKEN,
        allowed_hosts=["testserver"],
        allowed_origins=["http://testserver"],
        **server,
    )
    bridge = PlayerBridge(player=player, sim=_make_sim_mock())
    return (
        TestClient(
            create_app(bridge),
            headers={"Host": "testserver", "Origin": "http://testserver"},
        ),
        bridge,
    )


def _call_http_without_body_read(
    *,
    path: str = "/api/liners/upload",
    raw_path: bytes | None = None,
    headers: list[tuple[bytes, bytes]],
    app: Any | None = None,
) -> list[dict[str, Any]]:
    messages: list[dict[str, Any]] = []

    async def receive() -> dict[str, Any]:
        raise AssertionError("security rejection read request body")

    async def send(message: dict[str, Any]) -> None:
        messages.append(message)

    scope: dict[str, Any] = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": path,
        "raw_path": raw_path or path.encode("ascii"),
        "query_string": b"",
        "root_path": "",
        "headers": headers,
        "client": ("127.0.0.1", 50000),
        "server": ("testserver", 80),
        "state": {},
    }
    asyncio.run((app or _security_app())(scope, receive, send))
    return messages


def _response_status(messages: list[dict[str, Any]]) -> int:
    return next(
        message["status"] for message in messages if message["type"] == "http.response.start"
    )


def test_security_policy_snapshots_mutable_configuration() -> None:
    original = ServerConfig(
        access_token=_TEST_ACCESS_TOKEN,
        allowed_hosts=["testserver"],
        allowed_origins=["http://testserver"],
        session_ttl_seconds=60,
    )
    policy = SecurityPolicy(original, now=lambda: 1000)
    original.access_token = "rotated-task10-access-token-is-32-bytes"
    assert original.allowed_hosts is not None
    assert original.allowed_origins is not None
    original.allowed_hosts[0] = "rotated.local"
    original.allowed_origins[0] = "http://rotated.local"
    original.session_ttl_seconds = 120

    current_code = policy.current_pairing_code()
    assert policy.verify_pairing_code(current_code)
    assert policy.host_allowed("testserver")
    assert not policy.host_allowed("rotated.local")
    assert policy.origin_allowed("http://testserver")
    assert policy.issue_device_session(_TEST_DEVICE_ID).startswith("1060.")
    assert _TEST_ACCESS_TOKEN not in repr(policy)
    assert original.access_token not in repr(policy)

    replacement = SecurityPolicy(original)
    assert replacement.verify_pairing_code(replacement.current_pairing_code())
    assert replacement.host_allowed("rotated.local")


@pytest.mark.parametrize(
    "headers",
    [
        [(b"host", b"testserver"), (b"origin", b"http://testserver"), (b"content-length", b"5000")],
        [
            (b"host", b"testserver"),
            (b"origin", b"http://testserver"),
            (b"content-length", b"12"),
        ],
        [(b"host", b"testserver"), (b"origin", b"http://testserver")],
    ],
)
def test_pairing_body_is_capped_before_json_parsing(
    headers: list[tuple[bytes, bytes]],
    caplog: pytest.LogCaptureFixture,
) -> None:
    calls = 0
    messages: list[dict[str, Any]] = []

    async def receive() -> dict[str, Any]:
        nonlocal calls
        calls += 1
        return {"type": "http.request", "body": b"x" * 2048, "more_body": calls < 3}

    async def send(message: dict[str, Any]) -> None:
        messages.append(message)

    scope: dict[str, Any] = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": "/api/pair",
        "raw_path": b"/api/pair",
        "query_string": b"",
        "root_path": "",
        "headers": headers,
        "client": ("127.0.0.1", 50000),
        "server": ("testserver", 80),
        "state": {},
    }
    with caplog.at_level(logging.WARNING, logger="autodj.audit"):
        asyncio.run(_security_app()(scope, receive, send))

    assert _response_status(messages) == 413
    assert calls == (0 if headers[-1][1] == b"5000" else 3)
    start = next(message for message in messages if message["type"] == "http.response.start")
    assert any(key.lower() == b"x-request-id" for key, _ in start["headers"])
    assert len([record for record in caplog.records if '"status":413' in record.message]) == 1


def test_chunked_liner_upload_past_the_cap_is_413_before_anything_is_stored(
    tmp_path: Path,
) -> None:
    """A body with no Content-Length is counted as it streams into the multipart parser."""
    player = _make_player_mock()
    player._cfg.playback.liners_folder = str(tmp_path / "liners")
    player._cfg.server.liner_upload_max_bytes = 1024
    app = create_app(PlayerBridge(player=player, sim=_make_sim_mock()))
    boundary = b"cap"
    head = (
        b'--cap\r\nContent-Disposition: form-data; name="file"; filename="big.mp3"\r\n'
        b"Content-Type: audio/mpeg\r\n\r\n"
    )
    chunks = [head] + [b"x" * 16384] * 8 + [b"\r\n--cap--\r\n"]
    reads = 0
    messages: list[dict[str, Any]] = []

    async def receive() -> dict[str, Any]:
        nonlocal reads
        reads += 1
        chunk = chunks[reads - 1]
        return {"type": "http.request", "body": chunk, "more_body": reads < len(chunks)}

    async def send(message: dict[str, Any]) -> None:
        messages.append(message)

    scope: dict[str, Any] = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": "/api/liners/upload",
        "raw_path": b"/api/liners/upload",
        "query_string": b"",
        "root_path": "",
        "headers": [
            (b"host", b"testserver"),
            (b"origin", b"http://testserver"),
            (b"content-type", b"multipart/form-data; boundary=" + boundary),
        ],
        "client": ("127.0.0.1", 50000),
        "server": ("testserver", 80),
        "state": {},
    }
    asyncio.run(app(scope, receive, send))

    assert _response_status(messages) == 413
    assert reads < len(chunks)
    assert not (tmp_path / "liners" / "big.mp3").exists()


def test_post_start_exception_audits_actual_status_without_second_response(
    caplog: pytest.LogCaptureFixture,
) -> None:
    app = _security_app()

    async def broken_stream() -> Any:
        yield b"first"
        raise RuntimeError("private-stream-secret")

    from fastapi.responses import StreamingResponse

    @app.get("/api/stream-failure")
    async def stream_failure() -> StreamingResponse:
        return StreamingResponse(broken_stream(), status_code=206)

    policy: SecurityPolicy = app.state.security_policy
    device = app.state.device_registry.pair("Streaming test")
    cookie = policy.issue_device_session(device.device_id)
    messages: list[dict[str, Any]] = []

    async def receive() -> dict[str, Any]:
        await asyncio.sleep(3600)
        return {"type": "http.disconnect"}

    async def send(message: dict[str, Any]) -> None:
        messages.append(message)

    scope: dict[str, Any] = {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.4"},
        "http_version": "1.1",
        "method": "GET",
        "scheme": "http",
        "path": "/api/stream-failure",
        "raw_path": b"/api/stream-failure",
        "query_string": b"",
        "root_path": "",
        "headers": [
            (b"host", b"testserver"),
            (b"cookie", f"{COOKIE_NAME}={cookie}".encode()),
        ],
        "client": ("127.0.0.1", 50000),
        "server": ("testserver", 80),
        "state": {},
    }
    with (
        caplog.at_level(logging.ERROR, logger="autodj.audit"),
        pytest.raises(RuntimeError, match="private-stream-secret"),
    ):
        asyncio.run(app(scope, receive, send))

    starts = [message for message in messages if message["type"] == "http.response.start"]
    assert len(starts) == 1
    assert starts[0]["status"] == 206
    assert any(key.lower() == b"x-request-id" for key, _ in starts[0]["headers"])
    audit = [
        json.loads(record.message) for record in caplog.records if record.name == "autodj.audit"
    ]
    assert audit[-1]["status"] == 206
    assert "private-stream-secret" not in caplog.text


@pytest.mark.parametrize(
    "path",
    [
        "/api/status",
        "/api/audio?path=Z%3A%2FMusic%2Fsong_0.flac",
        "/api/library/job",
        "/api/profiles",
        "/api/liners",
    ],
)
def test_secured_route_categories_require_session(path: str) -> None:
    response = _security_client().get(path)

    assert response.status_code == 401
    assert len(response.headers["X-Request-ID"]) == 32


@pytest.mark.parametrize(
    ("method", "path", "kwargs"),
    [
        ("post", "/api/skip", {}),
        ("post", "/api/profiles", {"json": {}}),
        ("delete", "/api/profiles/default", {}),
        (
            "post",
            "/api/liners/upload",
            {"files": {"file": ("id.mp3", b"x", "audio/mpeg")}},
        ),
        ("delete", "/api/liners/file/id.mp3", {}),
        ("post", "/api/library/run", {"json": {}}),
    ],
)
def test_unsafe_route_categories_reject_before_parsing_body(
    method: str,
    path: str,
    kwargs: dict[str, object],
) -> None:
    response = getattr(_security_client(), method)(path, **kwargs)

    assert response.status_code == 401


def test_pairing_status_logout_cookie_contract() -> None:
    client = _security_client(secure_cookie=True)

    assert client.get("/api/auth/status").json() == {
        "required": True,
        "authenticated": False,
        "pairing": True,
        "device_id": None,
    }
    response = _pair(client)
    assert response.status_code == 200
    cookie = response.headers["set-cookie"]
    assert all(
        flag in cookie
        for flag in (
            "HttpOnly",
            "SameSite=strict",
            "Secure",
            "Max-Age=7776000",
            "Path=/",
        )
    )
    assert client.get("/api/auth/status").json() == {
        "required": True,
        "authenticated": True,
        "pairing": True,
        "device_id": response.json()["device_id"],
    }
    assert client.get("/api/status").status_code == 200

    logout = client.post("/api/logout")
    assert logout.status_code == 200
    deletion = logout.headers["set-cookie"]
    assert all(flag in deletion for flag in ("HttpOnly", "SameSite=strict", "Secure", "Path=/"))
    assert "Max-Age=0" in deletion
    assert client.get("/api/status").status_code == 401


def test_tls_implicit_origin_authenticates_http_and_websocket() -> None:
    player = _make_player_mock()
    player._cfg.server = ServerConfig(
        host="testserver",
        port=8443,
        access_token=_TEST_ACCESS_TOKEN,
    )
    bridge = PlayerBridge(player=player, sim=_make_sim_mock())
    client = TestClient(
        create_app(bridge, secure_cookie=True),
        base_url="https://testserver:8443",
        headers={"Host": "testserver:8443", "Origin": "https://testserver:8443"},
    )

    assert _pair(client).status_code == 200
    assert client.post("/api/skip").status_code == 200
    with client.websocket_connect("wss://testserver:8443/ws") as websocket:
        assert websocket is not None


def test_tls_does_not_override_explicit_allowed_origin() -> None:
    player = _make_player_mock()
    player._cfg.server = ServerConfig(
        host="testserver",
        port=8443,
        access_token=_TEST_ACCESS_TOKEN,
        allowed_origins=["http://trusted.test:8080"],
    )
    bridge = PlayerBridge(player=player, sim=_make_sim_mock())
    client = TestClient(
        create_app(bridge, secure_cookie=True),
        base_url="https://testserver:8443",
        headers={"Host": "testserver", "Origin": "http://trusted.test:8080"},
    )

    assert _pair(client).status_code == 200
    assert client.post("/api/skip").status_code == 200


def test_tampered_cookie_is_rejected() -> None:
    client = _security_client()
    assert _pair(client).status_code == 200
    client.cookies.set(COOKIE_NAME, "tampered")

    assert client.get("/api/status").status_code == 401


def test_anonymous_loopback_fixture_remains_usable(client: TestClient) -> None:
    assert client.get("/api/status").status_code == 200
    assert client.get("/api/auth/status").json() == {
        "required": False,
        "authenticated": True,
        "pairing": False,
        "device_id": None,
    }


def test_public_routes_still_enforce_host_and_unsafe_origin() -> None:
    client = _security_client()

    assert client.get("/api/version").status_code == 200
    assert client.get("/", headers={"Host": "evil.example"}).status_code == 403
    assert client.get("/api/version", headers={"Host": "evil.example"}).status_code == 403
    assert (
        client.post(
            "/api/pair",
            headers={"Origin": "http://evil.example"},
            json={"code": "00000000", "device_name": "Unknown browser"},
        ).status_code
        == 403
    )


@pytest.mark.parametrize(
    "headers",
    [
        [(b"host", b"testserver"), (b"host", b"evil.example"), (b"origin", b"http://testserver")],
        [
            (b"host", b"testserver"),
            (b"origin", b"http://testserver"),
            (b"origin", b"http://evil.example"),
        ],
        [(b"host", b"testserver"), (b"origin", b"http://testserver")],
        [(b"host", b"testserver"), (b"origin", b"http://evil.example")],
    ],
)
def test_security_rejection_never_reads_large_upload_body(
    headers: list[tuple[bytes, bytes]],
) -> None:
    messages = _call_http_without_body_read(
        headers=[
            *headers,
            (b"content-length", str(100 * 1024 * 1024).encode("ascii")),
        ]
    )

    assert _response_status(messages) in {401, 403}


def test_unauthorized_chunked_upload_never_reads_receive() -> None:
    messages = _call_http_without_body_read(
        headers=[(b"host", b"testserver"), (b"origin", b"http://testserver")]
    )

    assert _response_status(messages) == 401


@pytest.mark.parametrize(
    ("path", "raw_path"),
    [
        ("/modules/../api/status", b"/modules/%2e%2e/api/status"),
        ("/modules/\\api/status", b"/modules/%5capi/status"),
        ("/modules//api/status", b"/modules/%2fapi/status"),
    ],
)
def test_public_prefix_cannot_bypass_authentication(path: str, raw_path: bytes) -> None:
    messages = _call_http_without_body_read(
        path=path,
        raw_path=raw_path,
        headers=[(b"host", b"testserver"), (b"origin", b"http://testserver")],
    )

    assert _response_status(messages) == 401


def test_request_id_is_generated_not_accepted_from_client() -> None:
    response = _security_client().get(
        "/api/status",
        headers={"X-Request-ID": "attacker-controlled"},
    )

    assert response.status_code == 401
    assert response.headers["X-Request-ID"] != "attacker-controlled"
    assert len(response.headers["X-Request-ID"]) == 32


def test_request_id_and_template_survive_validation_and_method_errors(
    caplog: pytest.LogCaptureFixture,
) -> None:
    client = _security_client()
    assert _pair(client).status_code == 200
    caplog.clear()

    with caplog.at_level(logging.WARNING, logger="autodj.audit"):
        validation = client.post("/api/pair", json={})
        wrong_method = client.put("/api/status")
    assert validation.status_code == 422
    assert len(validation.headers["X-Request-ID"]) == 32
    assert wrong_method.status_code == 405
    assert len(wrong_method.headers["X-Request-ID"]) == 32
    records = [json.loads(item.message) for item in caplog.records if item.name == "autodj.audit"]
    assert [(record["route"], record["status"]) for record in records] == [
        ("/api/pair", 422),
        ("/api/status", 405),
    ]


def test_unhandled_route_error_gets_generic_request_id_and_redacted_audit(
    caplog: pytest.LogCaptureFixture,
) -> None:
    app = _security_app()
    exception_secret = "private-exception-secret"
    query_secret = "private-query-secret"

    @app.get("/api/explode")
    async def explode() -> None:
        raise RuntimeError(exception_secret)

    client = TestClient(
        app,
        headers={"Host": "testserver", "Origin": "http://testserver"},
    )
    assert _pair(client).status_code == 200
    caplog.clear()

    with caplog.at_level(logging.ERROR, logger="autodj.audit"):
        response = client.get(f"/api/explode?detail={query_secret}")

    assert response.status_code == 500
    assert response.json() == {"detail": "Internal server error"}
    assert len(response.headers["X-Request-ID"]) == 32
    records = [json.loads(item.message) for item in caplog.records if item.name == "autodj.audit"]
    assert records == [
        {
            "action": "/api/explode",
            "method": "GET",
            "outcome": "error",
            "request_id": response.headers["X-Request-ID"],
            "route": "/api/explode",
            "status": 500,
        }
    ]
    assert exception_secret not in caplog.text
    assert query_secret not in caplog.text


def test_options_has_no_cors_bypass() -> None:
    response = _security_client().options("/api/status")

    assert response.status_code == 401
    assert "access-control-allow-origin" not in response.headers


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("post", "/api/logout"),
        ("post", "/api/seek"),
        ("post", "/api/profiles/default/apply"),
        ("post", "/api/pause"),
        ("post", "/api/volume"),
        ("post", "/api/mute"),
        ("post", "/api/play-next"),
        ("post", "/api/queue/add"),
        ("post", "/api/queue/remove"),
        ("post", "/api/queue/reorder"),
        ("post", "/api/advance"),
        ("post", "/api/repick-next"),
        ("post", "/api/random-track"),
        ("post", "/api/preset"),
        ("post", "/api/transition"),
        ("post", "/api/djmix"),
        ("post", "/api/playback-settings"),
        ("post", "/api/bpm-range"),
        ("post", "/api/discovery"),
        ("post", "/api/discovery/toggle"),
        ("post", "/api/eq"),
        ("post", "/api/library/stop"),
    ],
)
def test_every_mutation_route_requires_session_before_body_validation(
    method: str,
    path: str,
) -> None:
    response = getattr(_security_client(), method)(path)

    assert response.status_code == 401


def test_authenticated_mutation_emits_one_success_audit(
    caplog: pytest.LogCaptureFixture,
) -> None:
    client = _security_client()
    assert _pair(client).status_code == 200
    caplog.clear()

    with caplog.at_level(logging.INFO, logger="autodj.audit"):
        response = client.post("/api/skip")

    assert response.status_code == 200
    records = [json.loads(item.message) for item in caplog.records if item.name == "autodj.audit"]
    assert records == [
        {
            "action": "/api/skip",
            "method": "POST",
            "outcome": "success",
            "request_id": response.headers["X-Request-ID"],
            "route": "/api/skip",
            "status": 200,
        }
    ]


def test_audit_rejections_use_route_templates_and_redact_inputs(
    caplog: pytest.LogCaptureFixture,
) -> None:
    client = _security_client()
    private_name = "private-profile-name"
    secret_query = "secret-query-value"
    with caplog.at_level(logging.INFO, logger="autodj.audit"):
        response = client.delete(f"/api/profiles/{private_name}?token={secret_query}")

    assert response.status_code == 401
    audit_messages = [item.message for item in caplog.records if item.name == "autodj.audit"]
    records = [json.loads(message) for message in audit_messages]
    assert len(records) == 1
    assert records[0] == {
        "action": "/api/profiles/{name}",
        "method": "DELETE",
        "outcome": "rejected",
        "request_id": response.headers["X-Request-ID"],
        "route": "/api/profiles/{name}",
        "status": 401,
    }
    assert all(private_name not in message for message in audit_messages)
    assert all(secret_query not in message for message in audit_messages)


def test_unsafe_audit_is_structured_redacted_and_single_event(
    caplog: pytest.LogCaptureFixture,
) -> None:
    client = _security_client()
    private_path = "Z:/Private/Music/secret.flac"
    with caplog.at_level(logging.INFO, logger="autodj.audit"):
        response = client.post(
            "/api/pair",
            json={"code": "00000000", "device_name": private_path},
        )

    assert response.status_code == 401
    records = [json.loads(item.message) for item in caplog.records if item.name == "autodj.audit"]
    assert len(records) == 1
    assert records[0]["request_id"] == response.headers["X-Request-ID"]
    assert records[0]["method"] == "POST"
    assert records[0]["route"] == "/api/pair"
    assert records[0]["status"] == 401
    assert records[0]["outcome"] == "rejected"
    assert "00000000" not in caplog.text
    assert private_path not in caplog.text


def test_websocket_rejects_origin_then_missing_cookie(
    caplog: pytest.LogCaptureFixture,
) -> None:
    client = _security_client()
    with caplog.at_level(logging.WARNING, logger="autodj.audit"):
        with (
            pytest.raises(WebSocketDisconnect) as wrong_origin,
            client.websocket_connect("/ws", headers={"Origin": "http://evil.example"}),
        ):
            raise AssertionError("handshake unexpectedly succeeded")
        assert wrong_origin.value.code == 4403
        with (
            pytest.raises(WebSocketDisconnect) as missing_cookie,
            client.websocket_connect("/ws"),
        ):
            raise AssertionError("handshake unexpectedly succeeded")
        assert missing_cookie.value.code == 4401
    rejected = [
        json.loads(item.message)
        for item in caplog.records
        if item.name == "autodj.audit" and json.loads(item.message)["outcome"] == "rejected"
    ]
    assert [record["status"] for record in rejected[-2:]] == [403, 401]
    assert all(record["route"] == "/ws" for record in rejected[-2:])


def test_websocket_rejects_duplicate_security_headers() -> None:
    client = _security_client()

    with (
        pytest.raises(WebSocketDisconnect) as duplicate_host,
        client.websocket_connect(
            "/ws",
            headers=Headers(
                [
                    ("Host", "testserver"),
                    ("Host", "evil.example"),
                    ("Origin", "http://testserver"),
                ]
            ),
        ),
    ):
        raise AssertionError("handshake unexpectedly succeeded")
    assert duplicate_host.value.code == 4403

    with (
        pytest.raises(WebSocketDisconnect) as duplicate_origin,
        client.websocket_connect(
            "/ws",
            headers=Headers(
                [
                    ("Host", "testserver"),
                    ("Origin", "http://testserver"),
                    ("Origin", "http://evil.example"),
                ]
            ),
        ),
    ):
        raise AssertionError("handshake unexpectedly succeeded")
    assert duplicate_origin.value.code == 4403


def test_websocket_closes_when_an_established_session_expires() -> None:
    now = [1000.0]
    player = _make_player_mock()
    player._cfg.server = ServerConfig(
        access_token=_TEST_ACCESS_TOKEN,
        allowed_hosts=["testserver"],
        allowed_origins=["http://testserver"],
        session_ttl_seconds=60,
    )
    bridge = PlayerBridge(player=player, sim=_make_sim_mock())
    app = create_app(bridge)
    app.state.security_policy.now = lambda: now[0]

    with TestClient(
        app,
        headers={"Host": "testserver", "Origin": "http://testserver"},
    ) as client:
        assert _pair(client).status_code == 200
        with client.websocket_connect("/ws") as websocket:
            websocket.receive_json()
            now[0] = 1061.0
            with pytest.raises(WebSocketDisconnect) as expired:
                websocket.receive_text()

    assert expired.value.code == 4401


def test_websocket_ignores_inbound_frames_and_audits_connect_and_disconnect(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The socket only pushes state: a frame from the page changes nothing."""
    client, bridge = _security_client_and_bridge()
    assert _pair(client).status_code == 200
    initial = bridge.player._state.discovery_enabled
    with (
        caplog.at_level(logging.INFO, logger="autodj.audit"),
        client.websocket_connect("/ws") as websocket,
    ):
        websocket.send_text("not-json")
        websocket.send_bytes(b"binary")
        websocket.send_json({"type": "toggle_discovery"})
        time.sleep(0.05)
    assert bridge.player._state.discovery_enabled is initial
    records = [json.loads(item.message) for item in caplog.records if item.name == "autodj.audit"]
    assert [record["outcome"] for record in records[-2:]] == ["connected", "disconnected"]
    assert all(record["route"] == "/ws" for record in records[-2:])


# ---------------------------------------------------------------------------
# Profile name validation 400 paths
# ---------------------------------------------------------------------------


class TestProfileValidateName:
    def test_save_invalid_name_400(self, client) -> None:
        resp = client.post("/api/profiles", json={"name": "../escape", "preset": None})
        assert resp.status_code == 400

    def test_delete_invalid_name_400(self, client) -> None:
        resp = client.request("DELETE", "/api/profiles/..%2Fbad")
        # Some server stacks normalise %2F so the route may not even match.
        assert resp.status_code in (400, 404)

    def test_delete_unknown_returns_404(self, client) -> None:
        resp = client.request("DELETE", "/api/profiles/no-such-profile-xyz")
        assert resp.status_code == 404

    def test_apply_invalid_name_400(self, client) -> None:
        resp = client.post("/api/profiles/..%2Fbad/apply")
        assert resp.status_code in (400, 404)

    def test_apply_unknown_returns_404(self, client) -> None:
        resp = client.post("/api/profiles/no-such-profile-xyz/apply")
        assert resp.status_code == 404


# ---------------------------------------------------------------------------
# Liner upload / delete branches
# ---------------------------------------------------------------------------


class TestLinerEndpoints:
    def test_upload_bad_extension(self, client) -> None:
        resp = client.post(
            "/api/liners/upload",
            files={"file": ("evil.exe", b"MZ", "application/octet-stream")},
        )
        assert resp.status_code == 400

    def test_upload_no_extension(self, client) -> None:
        resp = client.post(
            "/api/liners/upload",
            files={"file": ("noextension", b"data", "audio/wav")},
        )
        # No extension -> 400
        assert resp.status_code == 400

    def test_delete_unknown_liner_404(self, client) -> None:
        resp = client.delete("/api/liners/file/nope.wav")
        assert resp.status_code == 404

    def test_get_unknown_liner_404(self, client) -> None:
        resp = client.get("/api/liners/file/nope.wav")
        assert resp.status_code == 404

    @pytest.mark.parametrize(
        ("escaped", "status"),
        [
            # A decoded slash matches no route at all.
            ("..%2Fclip.mp3", 404),
            ("sub%2Fclip.mp3", 404),
            ("clip.mp3%3Astream", 400),
            ("clip.mp3.", 400),
            ("clip.mp3%20", 400),
        ],
    )
    @pytest.mark.parametrize("method", ["get", "delete"])
    def test_file_routes_reject_encoded_or_windows_aliases(
        self, client, escaped: str, status: int, method: str
    ) -> None:
        response = getattr(client, method)(f"/api/liners/file/{escaped}")
        assert response.status_code == status

    @pytest.mark.parametrize(
        "name",
        [
            "../clip.mp3",
            r"..\liners-backup\clip.mp3",
            r"sub\clip.mp3",
            "clip.mp3:stream",
            "clip.mp3.",
            "clip.mp3 ",
        ],
    )
    def test_upload_rejects_non_plain_names(self, client, name: str) -> None:
        response = client.post("/api/liners/upload", files={"file": (name, b"audio", "audio/mpeg")})
        assert response.status_code == 400

    def test_missing_plain_liner_is_404(self, client) -> None:
        assert client.get("/api/liners/file/missing.mp3").status_code == 404


# ---------------------------------------------------------------------------
# Cover art 404 paths
# ---------------------------------------------------------------------------


class TestArt:
    def test_unknown_track_404(self, client) -> None:
        resp = client.get("/api/art", params={"path": "Z:/no-such-track.flac"})
        assert resp.status_code == 404

    def test_known_track_no_art_404(self, client) -> None:
        resp = client.get("/api/art", params={"path": "Z:/Music/song_0.flac"})
        # bridge.cover_art_for likely returns None for the mock track
        assert resp.status_code == 404

    def test_known_track_with_art_returns_image(self, monkeypatch) -> None:
        # Mock cover_art_for to return real bytes so the FileResponse path
        # (lines 806-807) is exercised.
        from fastapi.testclient import TestClient

        from autodj.server import PlayerBridge, create_app

        from ._helpers import _make_player_mock, _make_sim_mock

        bridge = PlayerBridge(player=_make_player_mock(), sim=_make_sim_mock())
        bridge.cover_art_for = lambda path: (b"PNG-bytes", "image/png")  # type: ignore[assignment]
        with TestClient(create_app(bridge)) as tc:
            resp = tc.get("/api/art", params={"path": "Z:/Music/song_0.flac"})
            assert resp.status_code == 200
            assert resp.content == b"PNG-bytes"
            assert resp.headers["content-type"] == "image/png"

    def test_cover_art_lookup_runs_off_event_loop(self, monkeypatch) -> None:
        from fastapi.testclient import TestClient

        from autodj.server import PlayerBridge, create_app

        from ._helpers import _make_player_mock, _make_sim_mock

        bridge = PlayerBridge(player=_make_player_mock(), sim=_make_sim_mock())
        called: list[str] = []

        def _cover(path: str):
            called.append(path)
            return (b"JPEG-bytes", "image/jpeg")

        async def _fake_to_thread(fn, *args, **kwargs):
            called.append("to_thread")
            return fn(*args, **kwargs)

        bridge.cover_art_for = _cover  # type: ignore[assignment]
        monkeypatch.setattr("autodj.server.asyncio.to_thread", _fake_to_thread)

        with TestClient(create_app(bridge)) as tc:
            resp = tc.get("/api/art", params={"path": "Z:/Music/song_0.flac"})

        assert resp.status_code == 200
        assert called[-2:] == ["to_thread", "Z:/Music/song_0.flac"]


# ---------------------------------------------------------------------------
# Profile save round-trip
# ---------------------------------------------------------------------------


class TestProfileSaveRoundTrip:
    def test_apply_round_trip(self, client) -> None:
        body = {"name": "apply-rt", "preset": None}
        client.post("/api/profiles", json=body)
        resp = client.post(f"/api/profiles/{body['name']}/apply")
        assert resp.status_code == 200
        assert "applied" in resp.json()
        client.request("DELETE", f"/api/profiles/{body['name']}")


# ---------------------------------------------------------------------------
# Module path traversal 404
# ---------------------------------------------------------------------------


class TestModuleTraversal:
    def test_traversal_attempt_404(self, client) -> None:
        # Try to escape /modules/ directory
        resp = client.get("/modules/..%2F..%2Fetc%2Fpasswd")
        assert resp.status_code in (400, 404)

    def test_unknown_module_404(self, client) -> None:
        resp = client.get("/modules/nonexistent.js")
        assert resp.status_code == 404

    def test_traversal_outside_modules_root_returns_404(self, client) -> None:
        # Use absolute or parent-traversal path that defeats relative_to.
        # On Windows, an absolute drive-letter path triggers the ValueError
        # branch (line 443-444) cleanly.
        resp = client.get("/modules/C:/Windows/System32/cmd.exe")
        assert resp.status_code in (400, 404)

    def test_modules_endpoint_serves_existing_module(self, client) -> None:
        resp = client.get("/modules/dom-helpers.js")
        assert resp.status_code == 200
        assert resp.headers["content-type"].startswith("text/javascript")


# ---------------------------------------------------------------------------
# Profile validate_name 400 paths via direct route (validate_name fires for
# names that survive URL routing but contain disallowed characters like '@').
# ---------------------------------------------------------------------------


class TestProfileBadCharsRouted:
    def test_delete_bad_chars_returns_400(self, client) -> None:
        resp = client.request("DELETE", "/api/profiles/bad@name")
        assert resp.status_code == 400

    def test_apply_bad_chars_returns_400(self, client) -> None:
        resp = client.post("/api/profiles/bad@name/apply")
        assert resp.status_code == 400


# ---------------------------------------------------------------------------
# Profile apply — exercise BPM range, harmonic mode, preset branches
# ---------------------------------------------------------------------------


class TestProfileApplyBranches:
    @pytest.fixture
    def client(self, tmp_path: Path) -> TestClient:
        """Profiles under tmp_path, so parallel workers never see these files."""
        player = _make_player_mock()
        player._cfg.index.active_dir = str(tmp_path / "idx")
        return TestClient(create_app(PlayerBridge(player=player, sim=_make_sim_mock())))

    def test_apply_with_bpm_and_harmonic_and_preset(self, client) -> None:
        body = {
            "name": "branchcov-1",
            "preset": None,
            "bpm_lo": 90,
            "bpm_hi": 130,
            "harmonic_mode": "compatible",
        }
        client.post("/api/profiles", json=body)
        resp = client.post(f"/api/profiles/{body['name']}/apply")
        assert resp.status_code == 200
        applied = resp.json()["applied"]
        assert "bpm_range" in applied
        assert "harmonic_mode" in applied
        client.request("DELETE", f"/api/profiles/{body['name']}")

    def test_apply_with_preset_set(self, client) -> None:
        body = {"name": "branchcov-preset", "preset": "chill"}
        client.post("/api/profiles", json=body)
        resp = client.post(f"/api/profiles/{body['name']}/apply")
        assert resp.status_code == 200
        assert "preset" in resp.json()["applied"]
        client.request("DELETE", f"/api/profiles/{body['name']}")

    def test_apply_with_missing_preset_is_refused_and_changes_nothing(self, client) -> None:
        """A preset the config no longer has used to be dropped without a word."""
        body = {"name": "branchcov-gone", "preset": "warmup", "bpm_lo": 90, "bpm_hi": 130}
        client.post("/api/profiles", json=body)
        before = client.get("/api/status").json()["settings"]["bpm_range"]

        resp = client.post(f"/api/profiles/{body['name']}/apply")

        assert resp.status_code == 400
        assert "warmup" in resp.json()["detail"]
        assert client.get("/api/status").json()["settings"]["bpm_range"] == before
        client.request("DELETE", f"/api/profiles/{body['name']}")


# ---------------------------------------------------------------------------
# Liner upload + delete OSError + ALAC detection
# ---------------------------------------------------------------------------


class TestLinerUploadDelete:
    def test_upload_succeeds_and_delete_works(self, client, tmp_path, monkeypatch) -> None:
        # Point liners folder at tmp_path so writes are isolated.
        # Closure captures bridge.player._cfg.playback.liners_folder;
        # set that to redirect the liner folder.
        from fastapi.testclient import TestClient

        from autodj.server import PlayerBridge, create_app

        from ._helpers import _make_player_mock, _make_sim_mock

        player = _make_player_mock()
        player._cfg.playback.liners_folder = str(tmp_path)
        bridge = PlayerBridge(player=player, sim=_make_sim_mock())
        client = TestClient(create_app(bridge))
        resp = client.post(
            "/api/liners/upload",
            files={"file": ("clip.wav", b"RIFFwavedata", "audio/wav")},
        )
        assert resp.status_code == 200
        # Delete it to exercise the unlink success path.
        resp = client.delete("/api/liners/file/clip.wav")
        assert resp.status_code == 200

    def test_upload_path_with_slash_is_rejected(self, client, tmp_path, monkeypatch) -> None:
        # Closure captures bridge.player._cfg.playback.liners_folder;
        # set that to redirect the liner folder.
        from fastapi.testclient import TestClient

        from autodj.server import PlayerBridge, create_app

        from ._helpers import _make_player_mock, _make_sim_mock

        player = _make_player_mock()
        player._cfg.playback.liners_folder = str(tmp_path)
        bridge = PlayerBridge(player=player, sim=_make_sim_mock())
        client = TestClient(create_app(bridge))
        resp = client.post(
            "/api/liners/upload",
            files={"file": ("subdir/clip.wav", b"data", "audio/wav")},
        )
        assert resp.status_code == 400
        assert not (tmp_path / "clip.wav").exists()

    def test_delete_unlink_raises_oserror_returns_500(self, client, tmp_path, monkeypatch) -> None:
        from fastapi.testclient import TestClient

        # Closure captures bridge.player._cfg.playback.liners_folder;
        # set that to redirect the liner folder.
        from autodj.server import PlayerBridge, create_app

        from ._helpers import _make_player_mock, _make_sim_mock

        player = _make_player_mock()
        player._cfg.playback.liners_folder = str(tmp_path)
        bridge = PlayerBridge(player=player, sim=_make_sim_mock())
        client = TestClient(create_app(bridge))
        from autodj import liner_files

        (tmp_path / "doomed.wav").write_bytes(b"x")

        def _broken_delete(*args, **kwargs):
            raise OSError("locked")

        monkeypatch.setattr(liner_files, "delete_liner_file", _broken_delete)
        resp = client.delete("/api/liners/file/doomed.wav")
        assert resp.status_code == 500

    def test_open_rejects_malformed_range_and_closes_file(self, tmp_path: Path) -> None:
        player = _make_player_mock()
        player._cfg.playback.liners_folder = str(tmp_path)
        client = TestClient(create_app(PlayerBridge(player=player, sim=_make_sim_mock())))
        (tmp_path / "clip.wav").write_bytes(b"audio")

        response = client.get(
            "/api/liners/file/clip.wav",
            headers={"Range": "bytes=bad"},
        )

        assert response.status_code == 400


@pytest.mark.parametrize(
    ("manifests", "entries", "status"),
    [
        (["generation 1", "generation 2"], [object()], 409),
        (["generation 1"], [None], 404),
        (["generation 1", "generation 1"], [object()], 200),
    ],
)
def test_lyrics_refuses_a_result_from_a_replaced_index(
    manifests: list[str],
    entries: list[object | None],
    status: int,
) -> None:
    from unittest.mock import PropertyMock

    player = _make_player_mock()
    sim = _make_sim_mock()
    type(sim).manifest = PropertyMock(side_effect=manifests)
    sim.entry_for_path.side_effect = entries
    bridge = PlayerBridge(player=player, sim=sim)
    bridge.lyrics_for = MagicMock(return_value=[])
    client = TestClient(create_app(bridge))

    assert client.get("/api/lyrics", params={"path": "song.flac"}).status_code == status


# ---------------------------------------------------------------------------
# Library job snapshot success path
# ---------------------------------------------------------------------------


class TestLibraryJobRunSnapshot:
    def test_run_starts_the_job_off_the_event_loop(self, client, monkeypatch) -> None:
        from unittest.mock import MagicMock

        mgr = MagicMock()
        mgr.start.return_value = True
        mgr.snapshot.return_value = {"running": True, "name": "stats"}
        offloaded: list[object] = []

        async def recording_to_thread(function, *args, **kwargs):
            offloaded.append(function)
            return function(*args, **kwargs)

        monkeypatch.setattr("autodj.jobs.get_manager", lambda: mgr)
        monkeypatch.setattr("autodj.server.asyncio.to_thread", recording_to_thread)

        resp = client.post("/api/library/run", json={"name": "stats", "args": []})

        assert resp.status_code == 200
        assert mgr.start in offloaded
        mgr.start.assert_called_once_with("stats", [])

    def test_run_returns_snapshot_when_started(self, client, monkeypatch) -> None:
        from unittest.mock import MagicMock

        from autodj import server as _srv

        mgr = MagicMock()
        mgr.start.return_value = True
        mgr.snapshot.return_value = {"running": True, "name": "stats"}
        monkeypatch.setattr("autodj.jobs.get_manager", lambda: mgr)
        # Need to re-trigger the inner closure import; just call route.
        resp = client.post(
            "/api/library/run",
            json={"name": "stats", "args": []},
        )
        assert resp.status_code == 200
        assert resp.json()["name"] == "stats"
        _ = _srv
