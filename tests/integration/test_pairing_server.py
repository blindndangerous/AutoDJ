from __future__ import annotations

from unittest.mock import MagicMock

from fastapi.testclient import TestClient

from autodj.pairing import DeviceRegistry
from autodj.server import create_app

_SECRET = "pairing-secret-that-is-at-least-32-bytes"


def _paired_client(bridge, tmp_path) -> tuple[TestClient, DeviceRegistry]:
    bridge.player._cfg.server.access_token = _SECRET
    bridge.player._cfg.index.active_dir = tmp_path
    bridge.player._cfg.index.index_dir = tmp_path
    app = create_app(bridge)
    app.state.security_policy.now = lambda: 1_000
    return TestClient(app), app.state.device_registry


def test_browser_pairs_once_and_reuses_device_session(bridge, tmp_path) -> None:
    client, registry = _paired_client(bridge, tmp_path)
    code = client.app.state.security_policy.current_pairing_code()

    response = client.post(
        "/api/pair",
        json={"code": code, "device_name": "Kitchen tablet"},
    )

    assert response.status_code == 200
    device_id = response.json()["device_id"]
    assert registry.is_active(device_id)
    assert client.get("/api/status").status_code == 200
    assert client.get("/api/auth/status").json() == {
        "required": True,
        "authenticated": True,
        "pairing": True,
        "device_id": device_id,
    }


def test_invalid_pairing_code_cannot_create_device(bridge, tmp_path) -> None:
    client, registry = _paired_client(bridge, tmp_path)

    response = client.post(
        "/api/pair",
        json={"code": "00000000", "device_name": "Unknown browser"},
    )

    assert response.status_code == 401
    assert registry.list_devices() == []


def test_revoked_device_loses_api_access_without_server_restart(bridge, tmp_path) -> None:
    client, registry = _paired_client(bridge, tmp_path)
    code = client.app.state.security_policy.current_pairing_code()
    paired = client.post(
        "/api/pair",
        json={"code": code, "device_name": "Old phone"},
    ).json()

    assert registry.revoke(paired["device_id"])
    assert client.get("/api/status").status_code == 401
    status = client.get("/api/auth/status").json()
    assert status["authenticated"] is False
    assert status["device_id"] is None


def test_pairing_rejects_oversized_body_before_json_parsing(bridge, tmp_path) -> None:
    client, registry = _paired_client(bridge, tmp_path)

    response = client.post(
        "/api/pair",
        content=b"x" * 5_000,
        headers={"Content-Type": "application/json"},
    )

    assert response.status_code == 413
    assert registry.list_devices() == []


def test_pairing_rejects_untrusted_browser_origin(bridge, tmp_path) -> None:
    client, registry = _paired_client(bridge, tmp_path)

    response = client.post(
        "/api/pair",
        json={
            "code": client.app.state.security_policy.current_pairing_code(),
            "device_name": "Unknown browser",
        },
        headers={"Origin": "http://evil.example"},
    )

    assert response.status_code == 403
    assert registry.list_devices() == []


def test_pairing_is_disabled_for_anonymous_loopback_server(bridge) -> None:
    client = TestClient(create_app(bridge))

    response = client.post(
        "/api/pair",
        json={"code": "12345678", "device_name": "Unused browser"},
    )

    assert response.status_code == 409


def test_mock_secret_does_not_create_registry_paths(bridge, tmp_path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    bridge.player._cfg.server.access_token = MagicMock()
    bridge.player._cfg.index.index_dir = MagicMock()

    app = create_app(bridge)

    assert app.state.device_registry is None
    assert list(tmp_path.iterdir()) == []


def test_pairing_rejects_unsafe_device_name_without_creating_identity(bridge, tmp_path) -> None:
    client, registry = _paired_client(bridge, tmp_path)

    response = client.post(
        "/api/pair",
        json={
            "code": client.app.state.security_policy.current_pairing_code(),
            "device_name": "hidden\u202ename",
        },
    )

    assert response.status_code == 422
    assert registry.list_devices() == []


def test_legacy_login_endpoint_is_removed(bridge, tmp_path) -> None:
    client, registry = _paired_client(bridge, tmp_path)
    code = client.app.state.security_policy.current_pairing_code()
    paired = client.post(
        "/api/pair",
        json={"code": code, "device_name": "Current browser"},
    )

    response = client.post("/api/login", json={"token": _SECRET})

    assert paired.status_code == 200
    assert response.status_code == 404
    assert [device.name for device in registry.list_devices()] == ["Current browser"]


def test_pairing_and_auth_status_keep_sqlite_off_the_event_loop(
    bridge, tmp_path, monkeypatch
) -> None:
    import asyncio

    client, _registry = _paired_client(bridge, tmp_path)
    offloaded: list[str] = []
    real_to_thread = asyncio.to_thread

    async def recording_to_thread(function, *args, **kwargs):
        offloaded.append(getattr(function, "__name__", repr(function)))
        return await real_to_thread(function, *args, **kwargs)

    monkeypatch.setattr("autodj.server.asyncio.to_thread", recording_to_thread)
    code = client.app.state.security_policy.current_pairing_code()

    assert client.post("/api/pair", json={"code": code, "device_name": "Den"}).status_code == 200
    assert client.get("/api/auth/status").json()["authenticated"] is True
    # registry.pair, the active-device check behind issuing the session, and
    # the touch + verify pair in /api/auth/status all hit SQLite.
    assert {"pair", "issue_device_session", "_status"} <= set(offloaded)


def test_locked_out_client_gets_429_with_retry_after_even_for_the_right_code(
    bridge, tmp_path
) -> None:
    from autodj.security import PAIRING_MAX_FAILURES_PER_CLIENT

    client, registry = _paired_client(bridge, tmp_path)
    policy = client.app.state.security_policy
    code = policy.current_pairing_code()
    wrong = "00000000" if code != "00000000" else "11111111"

    for _ in range(PAIRING_MAX_FAILURES_PER_CLIENT):
        assert client.post("/api/pair", json={"code": wrong, "device_name": "x"}).status_code == 401
    response = client.post("/api/pair", json={"code": code, "device_name": "Real"})

    assert response.status_code == 429
    assert response.headers["Retry-After"] == "200"
    assert "Try again in 200 seconds" in response.json()["detail"]
    assert registry.list_devices() == []


def test_paused_pairing_tells_the_user_to_wait_for_a_new_code(bridge, tmp_path) -> None:
    from autodj.security import PAIRING_MAX_FAILURES_PER_WINDOW

    client, _registry = _paired_client(bridge, tmp_path)
    policy = client.app.state.security_policy
    for attempt in range(PAIRING_MAX_FAILURES_PER_WINDOW):
        policy.verify_pairing_code("00000000", f"10.9.0.{attempt}")

    response = client.post(
        "/api/pair",
        json={"code": policy.current_pairing_code(), "device_name": "Real"},
    )

    assert response.status_code == 429
    assert response.headers["Retry-After"] == "200"
    assert response.json()["detail"] == (
        "Pairing is paused after too many wrong codes. Try again in 200 seconds with a new code."
    )


def test_healthz_and_version_hide_details_until_paired(bridge, tmp_path) -> None:
    """Under pairing, the track count, commit and build time need a session."""
    client, _registry = _paired_client(bridge, tmp_path)

    assert client.get("/healthz").json() == {"status": "ok"}
    assert set(client.get("/api/version").json()) == {"version"}

    code = client.app.state.security_policy.current_pairing_code()
    paired = client.post("/api/pair", json={"code": code, "device_name": "Kitchen tablet"})
    assert paired.status_code == 200

    assert set(client.get("/healthz").json()) == {"status", "tracks"}
    assert set(client.get("/api/version").json()) == {"version", "commit", "built_at"}


def _pair(client: TestClient, name: str) -> str:
    code = client.app.state.security_policy.current_pairing_code()
    response = client.post("/api/pair", json={"code": code, "device_name": name})
    assert response.status_code == 200
    return response.json()["device_id"]


def test_devices_list_marks_this_browser_and_needs_a_session(bridge, tmp_path) -> None:
    client, registry = _paired_client(bridge, tmp_path)
    other = registry.pair("Old phone")
    assert client.get("/api/devices").status_code == 401

    this = _pair(client, "Kitchen tablet")
    body = client.get("/api/devices").json()

    assert body["pairing"] is True
    assert [(d["name"], d["current"]) for d in body["devices"]] == [
        ("Old phone", False),
        ("Kitchen tablet", True),
    ]
    assert {d["device_id"] for d in body["devices"]} == {other.device_id, this}


def test_revoking_another_device_keeps_this_session(bridge, tmp_path) -> None:
    client, registry = _paired_client(bridge, tmp_path)
    other = registry.pair("Old phone")
    _pair(client, "Kitchen tablet")

    response = client.delete(f"/api/devices/{other.device_id}")

    assert response.json() == {"revoked": other.device_id, "signed_out": False}
    assert not registry.is_active(other.device_id)
    assert client.get("/api/status").status_code == 200
    assert [d["name"] for d in client.get("/api/devices").json()["devices"]] == ["Kitchen tablet"]
    assert client.delete(f"/api/devices/{other.device_id}").status_code == 404


def test_revoking_this_device_signs_this_browser_out(bridge, tmp_path) -> None:
    client, _registry = _paired_client(bridge, tmp_path)
    this = _pair(client, "Kitchen tablet")

    response = client.delete(f"/api/devices/{this}")

    assert response.json()["signed_out"] is True
    assert "autodj_session" not in client.cookies
    assert client.get("/api/status").status_code == 401


def test_sign_out_revokes_this_device(bridge, tmp_path) -> None:
    """Deleting only the cookie left the device paired for 90 more days."""
    client, registry = _paired_client(bridge, tmp_path)
    this = _pair(client, "Kitchen tablet")

    assert client.post("/api/logout").status_code == 200

    assert not registry.is_active(this)
    assert client.get("/api/status").status_code == 401


def test_device_routes_without_pairing_say_so(client) -> None:
    assert client.get("/api/devices").json() == {"pairing": False, "devices": []}
    assert client.delete(f"/api/devices/{'a' * 32}").status_code == 409
    assert client.patch(f"/api/devices/{'a' * 32}", json={"name": "Phone"}).status_code == 409
    assert client.get("/api/pairing-code").status_code == 409


def test_rename_device_needs_a_session_and_validates_the_name(bridge, tmp_path) -> None:
    client, registry = _paired_client(bridge, tmp_path)
    other = registry.pair("Old phone")
    url = f"/api/devices/{other.device_id}"
    assert client.patch(url, json={"name": "Hall speaker"}).status_code == 401

    _pair(client, "Kitchen tablet")
    renamed = client.patch(url, json={"name": "  Hall speaker "})

    assert renamed.json() == {"device_id": other.device_id, "name": "Hall speaker"}
    assert [d["name"] for d in client.get("/api/devices").json()["devices"]] == [
        "Hall speaker",
        "Kitchen tablet",
    ]
    for bad in ("", "   ", "x" * 65, "two\nlines"):
        assert client.patch(url, json={"name": bad}).status_code == 422
    registry.revoke(other.device_id)
    assert client.patch(url, json={"name": "Back again"}).status_code == 404
    assert client.patch(f"/api/devices/{'f' * 32}", json={"name": "Nobody"}).status_code == 404
    assert client.patch("/api/devices/not-a-device", json={"name": "Nobody"}).status_code == 404


def test_paired_browser_can_show_the_pairing_code(bridge, tmp_path) -> None:
    client, _registry = _paired_client(bridge, tmp_path)
    policy = client.app.state.security_policy
    assert client.get("/api/pairing-code").status_code == 401

    _pair(client, "Kitchen tablet")
    response = client.get("/api/pairing-code")

    assert response.headers["cache-control"] == "no-store"
    # now() is 1000: window 900..1200, then the grace window to 1500.
    assert response.json() == {
        "code": policy.current_pairing_code(),
        "valid_seconds": 500,
        "next_code_seconds": 200,
    }


def test_pairing_code_while_paused_says_to_wait(bridge, tmp_path) -> None:
    from autodj.security import PAIRING_MAX_FAILURES_PER_WINDOW

    client, _registry = _paired_client(bridge, tmp_path)
    _pair(client, "Kitchen tablet")
    policy = client.app.state.security_policy
    for index in range(PAIRING_MAX_FAILURES_PER_WINDOW):
        policy.verify_pairing_code("00000000", f"10.0.0.{index}")

    response = client.get("/api/pairing-code")

    assert response.status_code == 429
    assert response.headers["retry-after"] == "200"
    assert "paused" in response.json()["detail"]
