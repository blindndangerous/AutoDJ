"""Machine tokens: shown once, stored hashed, named, scoped, revocable, last use kept."""

import sqlite3

import pytest

from autodj.machine_tokens import (
    KNOWN_SCOPES,
    LAST_USED_RESOLUTION_SECONDS,
    SCOPE_ANALYSIS_WORKER,
    MachineTokenStore,
    token_id_of,
    validate_name,
    validate_scopes,
)


class Clock:
    def __init__(self, now: float = 1_000_000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def store(tmp_path):
    clock = Clock()
    return MachineTokenStore(tmp_path / ".machine-tokens.sqlite3", now=clock), clock


def test_created_token_is_shown_once_and_only_its_digest_is_stored(store, tmp_path):
    tokens, _clock = store
    token, secret = tokens.create("  Alienware  ", [SCOPE_ANALYSIS_WORKER])
    assert token.name == "Alienware"
    assert token.scopes == (SCOPE_ANALYSIS_WORKER,)
    assert secret.startswith(f"adj1.{token.token_id}.")
    assert token_id_of(secret) == token.token_id
    with sqlite3.connect(tmp_path / ".machine-tokens.sqlite3") as db:
        dump = "\n".join(db.iterdump())
    db.close()
    assert secret.split(".")[2] not in dump
    # Listing never gives the secret back.
    listed = tokens.list_tokens()
    assert [t.token_id for t in listed] == [token.token_id]
    assert all(secret not in repr(t) for t in listed)


def test_authenticate_needs_the_right_secret_scope_and_an_active_token(store):
    tokens, _clock = store
    token, secret = tokens.create("GPU box", [SCOPE_ANALYSIS_WORKER])
    assert tokens.authenticate(secret, SCOPE_ANALYSIS_WORKER).token_id == token.token_id
    wrong = secret[:-1] + ("A" if secret[-1] != "A" else "B")
    assert tokens.authenticate(wrong, SCOPE_ANALYSIS_WORKER) is None
    assert tokens.authenticate(secret, "liner-upload") is None
    assert tokens.authenticate("not a token", SCOPE_ANALYSIS_WORKER) is None
    assert tokens.authenticate(None, SCOPE_ANALYSIS_WORKER) is None  # type: ignore[arg-type]
    other_id = "f" * 32
    assert (
        tokens.authenticate(f"adj1.{other_id}.{secret.split('.')[2]}", SCOPE_ANALYSIS_WORKER)
        is None
    )
    assert tokens.revoke(token.token_id)
    assert not tokens.revoke(token.token_id)
    assert tokens.authenticate(secret, SCOPE_ANALYSIS_WORKER) is None
    assert tokens.list_tokens() == []
    assert tokens.list_tokens(include_revoked=True)[0].revoked_at is not None
    assert tokens.get(token.token_id).active is False


def test_last_use_is_recorded_at_most_once_a_minute(store):
    tokens, clock = store
    token, secret = tokens.create("GPU box", [SCOPE_ANALYSIS_WORKER])
    assert tokens.get(token.token_id).last_used_at is None
    tokens.authenticate(secret, SCOPE_ANALYSIS_WORKER)
    first = tokens.get(token.token_id).last_used_at
    assert first == int(clock.now)
    clock.now += LAST_USED_RESOLUTION_SECONDS - 1
    tokens.authenticate(secret, SCOPE_ANALYSIS_WORKER)
    assert tokens.get(token.token_id).last_used_at == first
    clock.now += 1
    tokens.authenticate(secret, SCOPE_ANALYSIS_WORKER)
    assert tokens.get(token.token_id).last_used_at == int(clock.now)


def test_names_and_scopes_are_validated(store):
    tokens, _clock = store
    with pytest.raises(ValueError):
        tokens.create("", [SCOPE_ANALYSIS_WORKER])
    with pytest.raises(ValueError):
        tokens.create("x" * 65, [SCOPE_ANALYSIS_WORKER])
    with pytest.raises(ValueError):
        tokens.create("bad\nname", [SCOPE_ANALYSIS_WORKER])
    with pytest.raises(ValueError):
        tokens.create("box", ["admin"])
    with pytest.raises(ValueError):
        tokens.create("box", [])
    with pytest.raises(ValueError):
        validate_name(7)
    assert validate_scopes([SCOPE_ANALYSIS_WORKER, SCOPE_ANALYSIS_WORKER]) == (
        SCOPE_ANALYSIS_WORKER,
    )
    assert SCOPE_ANALYSIS_WORKER in KNOWN_SCOPES
    assert tokens.get("nope") is None
    assert tokens.revoke("nope") is False


def test_another_process_sees_tokens_and_revocations(store, tmp_path):
    tokens, _clock = store
    token, secret = tokens.create("box", [SCOPE_ANALYSIS_WORKER])
    other = MachineTokenStore(tmp_path / ".machine-tokens.sqlite3")
    assert other.authenticate(secret, SCOPE_ANALYSIS_WORKER) is not None
    other.revoke(token.token_id)
    assert tokens.authenticate(secret, SCOPE_ANALYSIS_WORKER) is None
