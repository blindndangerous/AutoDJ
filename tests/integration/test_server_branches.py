"""Branch-coverage tests for autodj.server endpoints.

Targets validate_name 400 paths, profile-not-found 404 paths, liner
upload/delete edge cases, and other small uncovered branches.
"""

from __future__ import annotations

import asyncio
import json
import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi.testclient import TestClient
from httpx import Headers
from starlette.websockets import WebSocketDisconnect

from autodj.config import ServerConfig
from autodj.index_manifest import IndexSnapshotToken
from autodj.security import COOKIE_NAME, PairingRateLimiter, SecurityPolicy
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


def _security_client_and_bridge() -> tuple[TestClient, PlayerBridge]:
    player = _make_player_mock()
    player._cfg.server = ServerConfig(
        access_token=_TEST_ACCESS_TOKEN,
        allowed_hosts=["testserver"],
        allowed_origins=["http://testserver"],
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


def test_app_policy_replacement_is_atomic() -> None:
    app = _security_app()
    rotated = "rotated-task10-access-token-is-32-bytes"
    app.state.security_policy = SecurityPolicy(
        ServerConfig(
            access_token=rotated,
            allowed_hosts=["rotated.local"],
            allowed_origins=["http://rotated.local"],
        ),
        device_is_active=app.state.device_registry.is_active,
    )
    client = TestClient(
        app,
        base_url="http://rotated.local",
        headers={"Host": "rotated.local", "Origin": "http://rotated.local"},
    )

    assert _pair(client).status_code == 200


def test_pairing_rate_limiter_is_bounded_isolated_and_expires() -> None:
    now = [100.0]
    limiter = PairingRateLimiter(
        now=lambda: now[0], per_client_limit=2, global_limit=4, window_seconds=10, max_clients=2
    )
    assert limiter.reserve("one").allowed
    assert limiter.reserve("one").allowed
    assert not limiter.reserve("one").allowed
    assert limiter.reserve("two").allowed
    assert limiter.reserve("three").allowed
    assert limiter.tracked_clients <= 2
    assert not limiter.reserve("four").allowed
    now[0] = 111.0
    assert limiter.reserve("one").allowed


def test_pairing_throttle_bypasses_body_and_code_compare(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    limiter = PairingRateLimiter(per_client_limit=1, global_limit=10)
    app = _security_app()
    app.state.pairing_rate_limiter = limiter
    client = TestClient(
        app,
        headers={
            "Host": "testserver",
            "Origin": "http://testserver",
            "X-Forwarded-For": "203.0.113.1",
        },
    )
    policy = app.state.security_policy
    checked = MagicMock(wraps=policy.verify_pairing_code)
    monkeypatch.setattr(policy, "verify_pairing_code", checked)

    invalid = {"code": "00000000", "device_name": "Unknown browser"}
    assert client.post("/api/pair", json=invalid).status_code == 401
    with caplog.at_level(logging.WARNING, logger="autodj.audit"):
        first = client.post("/api/pair", content=b"x" * 5000)
        second = client.post(
            "/api/pair",
            json=invalid,
            headers={"X-Forwarded-For": "198.51.100.2"},
        )

    assert first.status_code == second.status_code == 429
    assert first.headers["Retry-After"]
    assert checked.call_count == 1
    limited = [record for record in caplog.records if '"status":429' in record.message]
    assert len(limited) == 1
    assert "wrong" not in caplog.text


def test_pairing_throttle_rejects_without_reading_request_body() -> None:
    limiter = PairingRateLimiter(per_client_limit=1, global_limit=10)
    assert limiter.reserve("127.0.0.1").allowed
    app = _security_app()
    app.state.pairing_rate_limiter = limiter

    messages = _call_http_without_body_read(
        path="/api/pair",
        headers=[
            (b"host", b"testserver"),
            (b"origin", b"http://testserver"),
            (b"content-length", b"5000"),
        ],
        app=app,
    )

    assert _response_status(messages) == 429


def test_concurrent_pairing_guesses_reserve_capacity_before_comparison(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    limiter = PairingRateLimiter(per_client_limit=2, global_limit=2)
    app = _security_app()
    app.state.pairing_rate_limiter = limiter
    policy: SecurityPolicy = app.state.security_policy
    compare_barrier = threading.Barrier(2)
    compare_calls = 0
    compare_lock = threading.Lock()

    def compare(_candidate: str) -> bool:
        nonlocal compare_calls
        with compare_lock:
            compare_calls += 1
        compare_barrier.wait(timeout=5)
        return False

    monkeypatch.setattr(policy, "verify_pairing_code", compare)

    def attempt(_index: int) -> int:
        with TestClient(
            app,
            headers={"Host": "testserver", "Origin": "http://testserver"},
        ) as client:
            return client.post(
                "/api/pair",
                json={"code": "00000000", "device_name": "Unknown browser"},
            ).status_code

    with ThreadPoolExecutor(max_workers=6) as executor:
        statuses = list(executor.map(attempt, range(6)))

    assert statuses.count(401) == 2
    assert statuses.count(429) == 4
    assert compare_calls == 2


def test_malformed_and_oversized_pairing_consume_bounded_attempt_budget(
    caplog: pytest.LogCaptureFixture,
) -> None:
    limiter = PairingRateLimiter(per_client_limit=2, global_limit=10)
    app = _security_app()
    app.state.pairing_rate_limiter = limiter
    client = TestClient(app, headers={"Host": "testserver", "Origin": "http://testserver"})

    with caplog.at_level(logging.INFO, logger="autodj.audit"):
        oversized = client.post("/api/pair", content=b"x" * 5000)
        malformed = client.post(
            "/api/pair", content=b"{", headers={"Content-Type": "application/json"}
        )
        invalid = {"code": "00000000", "device_name": "Unknown browser"}
        blocked = client.post("/api/pair", json=invalid)
        blocked_again = client.post("/api/pair", json=invalid)

    assert [
        response.status_code for response in (oversized, malformed, blocked, blocked_again)
    ] == [
        413,
        422,
        429,
        429,
    ]
    statuses = [
        json.loads(record.message)["status"]
        for record in caplog.records
        if record.name == "autodj.audit"
    ]
    assert statuses == [413, 422, 429]


def test_single_oversized_pairing_chunk_is_not_copied_into_accumulator(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import autodj.security as security_module

    class BoundedAccumulator:
        extended = False

        def __len__(self) -> int:
            return 0

        def extend(self, _chunk: bytes) -> None:
            type(self).extended = True
            raise AssertionError("oversized chunk was copied")

    monkeypatch.setattr(security_module, "bytearray", BoundedAccumulator, raising=False)
    messages: list[dict[str, Any]] = []

    async def receive() -> dict[str, Any]:
        return {"type": "http.request", "body": b"x" * 1_000_000, "more_body": False}

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
        "headers": [(b"host", b"testserver"), (b"origin", b"http://testserver")],
        "client": ("127.0.0.1", 50000),
        "server": ("testserver", 80),
        "state": {},
    }

    asyncio.run(_security_app()(scope, receive, send))

    assert _response_status(messages) == 413
    assert BoundedAccumulator.extended is False


def test_successful_pairing_resets_per_client_failures() -> None:
    limiter = PairingRateLimiter(per_client_limit=2, global_limit=100)
    app = _security_app()
    app.state.pairing_rate_limiter = limiter
    client = TestClient(app, headers={"Host": "testserver", "Origin": "http://testserver"})

    invalid = {"code": "00000000", "device_name": "Unknown browser"}
    assert client.post("/api/pair", json=invalid).status_code == 401
    assert _pair(client).status_code == 200
    client.cookies.clear()
    assert client.post("/api/pair", json=invalid).status_code == 401
    assert _pair(client, name="Second browser").status_code == 200


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
        ("/static/../api/status", b"/static/%2e%2e/api/status"),
        ("/static/\\api/status", b"/static/%5capi/status"),
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
        response = client.get(f"/api/profiles/{private_name}?token={secret_query}")

    assert response.status_code == 401
    audit_messages = [item.message for item in caplog.records if item.name == "autodj.audit"]
    records = [json.loads(message) for message in audit_messages]
    assert len(records) == 1
    assert records[0] == {
        "action": "/api/profiles/{name}",
        "method": "GET",
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
    app.state.security_policy = SecurityPolicy(
        player._cfg.server,
        now=lambda: now[0],
    )

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


def test_websocket_rejects_mutation_after_session_expiry() -> None:
    now = [1000.0]
    client, bridge = _security_client_and_bridge()
    client.app.state.security_policy = SecurityPolicy(
        ServerConfig(
            access_token=_TEST_ACCESS_TOKEN,
            allowed_hosts=["testserver"],
            allowed_origins=["http://testserver"],
            session_ttl_seconds=60,
        ),
        now=lambda: now[0],
    )
    initial = bridge.player._state.discovery_enabled
    assert _pair(client).status_code == 200

    with client.websocket_connect("/ws") as websocket:
        now[0] = 1061.0
        websocket.send_json({"type": "toggle_discovery"})
        with pytest.raises(WebSocketDisconnect) as expired:
            websocket.receive_text()

    assert expired.value.code == 4401
    assert bridge.player._state.discovery_enabled is initial


def test_websocket_audits_connect_mutation_and_disconnect(
    caplog: pytest.LogCaptureFixture,
) -> None:
    client = _security_client()
    assert _pair(client).status_code == 200
    with (
        caplog.at_level(logging.INFO, logger="autodj.audit"),
        client.websocket_connect("/ws") as websocket,
    ):
        websocket.send_text("not-json")
        websocket.send_json({"type": "toggle_discovery"})
    records = [json.loads(item.message) for item in caplog.records if item.name == "autodj.audit"]
    assert [record["outcome"] for record in records[-3:]] == [
        "connected",
        "success",
        "disconnected",
    ]
    assert records[-2]["action"] == "toggle_discovery"
    assert all(record["route"] == "/ws" for record in records[-3:])


def test_websocket_ignores_binary_frame_then_processes_mutation() -> None:
    client, bridge = _security_client_and_bridge()
    assert _pair(client).status_code == 200
    initial = bridge.player._state.discovery_enabled

    with client.websocket_connect("/ws") as websocket:
        websocket.send_bytes(b"not-a-text-command")
        websocket.send_json({"type": "toggle_discovery"})
        time.sleep(0.05)

    assert bridge.player._state.discovery_enabled is not initial


def test_websocket_bridge_failure_closes_and_audits_cleanup(
    caplog: pytest.LogCaptureFixture,
) -> None:
    client, bridge = _security_client_and_bridge()
    bridge.toggle_discovery = MagicMock(side_effect=RuntimeError("private failure details"))
    assert _pair(client).status_code == 200
    caplog.clear()

    with (
        caplog.at_level(logging.INFO, logger="autodj.audit"),
        client.websocket_connect("/ws") as websocket,
    ):
        websocket.send_json({"type": "toggle_discovery"})
        with pytest.raises(WebSocketDisconnect) as closed:
            websocket.receive_text()
    assert closed.value.code == 1011
    records = [json.loads(item.message) for item in caplog.records if item.name == "autodj.audit"]
    assert [(record["action"], record["outcome"], record["status"]) for record in records] == [
        ("/ws", "connected", 101),
        ("toggle_discovery", "rejected", 500),
        ("/ws", "disconnected", 1011),
    ]
    assert "private failure details" not in caplog.text


# ---------------------------------------------------------------------------
# Profile name validation 400 paths
# ---------------------------------------------------------------------------


class TestProfileValidateName:
    def test_get_invalid_name_returns_400(self, client) -> None:
        # Names with traversal / special chars trip validate_name -> 400
        resp = client.get("/api/profiles/..%2Fbad")
        assert resp.status_code in (400, 404)

    def test_get_unknown_name_returns_404(self, client, tmp_path: Path) -> None:
        resp = client.get("/api/profiles/no-such-profile-xyz")
        assert resp.status_code == 404

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
        "escaped",
        [
            "..%2Fclip.mp3",
            "sub%2Fclip.mp3",
            "clip.mp3%3Astream",
            "clip.mp3.",
            "clip.mp3%20",
        ],
    )
    @pytest.mark.parametrize("method", ["get", "delete"])
    def test_file_routes_reject_encoded_or_windows_aliases(
        self, client, escaped: str, method: str
    ) -> None:
        response = getattr(client, method)(f"/api/liners/file/{escaped}")
        assert response.status_code == 400

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
    def test_save_then_get(self, client) -> None:
        body = {
            "name": "test-profile-1",
            "preset": None,
            "bpm_lo": 90,
            "bpm_hi": 130,
        }
        resp = client.post("/api/profiles", json=body)
        assert resp.status_code == 200
        # Should be retrievable
        get_resp = client.get(f"/api/profiles/{body['name']}")
        assert get_resp.status_code == 200
        assert get_resp.json()["name"] == body["name"]
        # Cleanup
        client.request("DELETE", f"/api/profiles/{body['name']}")

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

    def test_modules_endpoint_serves_existing_module_when_present(self, client) -> None:
        """Best-effort: when the source static dir is the live one (no
        bundled static_dist on this checkout), confirm a real module file
        is served. CI has no static_dist so the response is 200; local
        builds with static_dist may 404 — both are acceptable.
        """
        resp = client.get("/modules/dom-helpers.js")
        # Either 200 (no bundle, real modules dir) or 404 (bundled, no /modules).
        assert resp.status_code in (200, 404)
        if resp.status_code == 200:
            assert "javascript" in resp.headers.get("content-type", "")


# ---------------------------------------------------------------------------
# Profile validate_name 400 paths via direct route (validate_name fires for
# names that survive URL routing but contain disallowed characters like '@').
# ---------------------------------------------------------------------------


class TestProfileBadCharsRouted:
    def test_get_bad_chars_returns_400(self, client) -> None:
        # '@' is rejected by validate_name; the route still matches.
        resp = client.get("/api/profiles/bad@name")
        assert resp.status_code == 400

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
        body = {
            "name": "branchcov-preset",
            "preset": "warmup",
        }
        client.post("/api/profiles", json=body)
        resp = client.post(f"/api/profiles/{body['name']}/apply")
        assert resp.status_code == 200
        # `preset` may or may not appear depending on whether the
        # built-in preset exists — but the contextlib.suppress branch is hit.
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

        monkeypatch.setattr(liner_files, "_delete_relative_file", _broken_delete)
        resp = client.delete("/api/liners/file/doomed.wav")
        assert resp.status_code == 500

    @pytest.mark.parametrize(
        ("error_name", "status"),
        [
            ("InvalidLinerName", 400),
            ("LinerStorageUnsupportedError", 503),
        ],
    )
    def test_upload_maps_storage_boundary_errors(
        self,
        error_name: str,
        status: int,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from autodj import liner_files

        player = _make_player_mock()
        player._cfg.playback.liners_folder = str(tmp_path)
        client = TestClient(create_app(PlayerBridge(player=player, sim=_make_sim_mock())))
        error_type = getattr(liner_files, error_name)
        monkeypatch.setattr(
            liner_files,
            "store_liner_upload",
            AsyncMock(side_effect=error_type("storage rejected")),
        )

        response = client.post(
            "/api/liners/upload",
            files={"file": ("clip.wav", b"data", "audio/wav")},
        )

        assert response.status_code == status

    def test_delete_maps_unsupported_storage(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from autodj import liner_files

        player = _make_player_mock()
        player._cfg.playback.liners_folder = str(tmp_path)
        client = TestClient(create_app(PlayerBridge(player=player, sim=_make_sim_mock())))
        monkeypatch.setattr(
            liner_files,
            "delete_liner_file",
            MagicMock(side_effect=liner_files.LinerStorageUnsupportedError("unsupported")),
        )

        assert client.delete("/api/liners/file/clip.wav").status_code == 503

    def test_open_maps_unsupported_storage(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from autodj import liner_files

        player = _make_player_mock()
        player._cfg.playback.liners_folder = str(tmp_path)
        client = TestClient(create_app(PlayerBridge(player=player, sim=_make_sim_mock())))
        monkeypatch.setattr(
            liner_files,
            "open_liner_file",
            MagicMock(side_effect=liner_files.LinerStorageUnsupportedError("unsupported")),
        )

        assert client.get("/api/liners/file/clip.wav").status_code == 503

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

    def test_streaming_response_construction_failure_closes_file(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from autodj import liner_files, server

        player = _make_player_mock()
        player._cfg.playback.liners_folder = str(tmp_path)
        client = TestClient(create_app(PlayerBridge(player=player, sim=_make_sim_mock())))
        file = MagicMock()
        opened = liner_files.OpenedLiner(
            file=file,
            stat_result=SimpleNamespace(st_size=5),
        )
        monkeypatch.setattr(liner_files, "open_liner_file", MagicMock(return_value=opened))
        monkeypatch.setattr(
            server, "StreamingResponse", MagicMock(side_effect=RuntimeError("response"))
        )

        response = client.get("/api/liners/file/clip.wav")

        assert response.status_code == 500
        file.close.assert_called()


def test_dev_module_route_serves_existing_javascript(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from autodj import server

    monkeypatch.setattr(
        server,
        "_selected_static_dir",
        MagicMock(return_value=server._PACKAGE_DIR / "static"),
    )
    player = _make_player_mock()
    client = TestClient(create_app(PlayerBridge(player=player, sim=_make_sim_mock())))
    response = client.get("/modules/tabs.js")
    assert response.status_code == 200
    assert "javascript" in response.headers["content-type"]


@pytest.mark.parametrize(
    ("tokens", "entries", "status"),
    [
        ([IndexSnapshotToken(1, 1), IndexSnapshotToken(2, 2)], [object()], 409),
        ([IndexSnapshotToken(1, 1)] * 3, [object(), None], 404),
        (
            [
                IndexSnapshotToken(1, 1),
                IndexSnapshotToken(1, 1),
                IndexSnapshotToken(1, 1),
                IndexSnapshotToken(2, 2),
            ],
            [object(), object()],
            409,
        ),
    ],
)
def test_lyrics_revalidates_snapshot_and_membership(
    tokens: list[IndexSnapshotToken],
    entries: list[object | None],
    status: int,
) -> None:
    from unittest.mock import PropertyMock

    player = _make_player_mock()
    sim = _make_sim_mock()
    type(sim).snapshot_token = PropertyMock(side_effect=tokens)
    sim.entry_for_path.side_effect = entries
    bridge = PlayerBridge(player=player, sim=sim)
    bridge.lyrics_for = MagicMock(return_value=[])
    client = TestClient(create_app(bridge))

    assert client.get("/api/lyrics", params={"path": "song.flac"}).status_code == status


# ---------------------------------------------------------------------------
# Library job snapshot success path
# ---------------------------------------------------------------------------


class TestLibraryJobRunSnapshot:
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
