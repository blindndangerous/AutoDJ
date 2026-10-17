"""Machine tokens: shown once, stored hashed, named, scoped, revocable, last use kept."""

import sqlite3

import pytest

from autodj.machine_tokens import (
    KNOWN_SCOPES,
    LAST_USED_RESOLUTION_SECONDS,
    SCOPE_ANALYSIS_WORKER,
    SCOPE_ANNOUNCER,
    STATION_SCOPES,
    MachineTokenStore,
    token_id_of,
    validate_name,
    validate_scopes,
    validate_station,
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
    token, secret = tokens.create("  Studio PC  ", [SCOPE_ANALYSIS_WORKER])
    assert token.name == "Studio PC"
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


def test_tokens_command_creates_lists_and_revokes(tmp_path):
    from click.testing import CliRunner

    from autodj.analysis_queue import AnalysisQueue
    from autodj.cli import cli

    config = tmp_path / "config.toml"
    config.write_text(f'[index]\nindex_dir = "{(tmp_path / "index").as_posix()}"\n')
    runner = CliRunner()
    base = ["--config", str(config), "tokens"]
    assert "No machine tokens." in runner.invoke(cli, [*base, "list"]).output
    created = runner.invoke(cli, [*base, "create", "Studio PC", "--scope", SCOPE_ANALYSIS_WORKER])
    assert created.exit_code == 0, created.output
    secret = created.stdout.strip()
    assert secret.startswith("adj1.")
    assert "shown only now and cannot be shown again" in created.stderr
    token_id = token_id_of(secret)
    listed = runner.invoke(cli, [*base, "list"])
    assert token_id in listed.output and "Studio PC" in listed.output
    assert secret.split(".")[2] not in listed.output
    store = MachineTokenStore(tmp_path / "index" / ".machine-tokens.sqlite3")
    assert store.authenticate(secret, SCOPE_ANALYSIS_WORKER) is not None
    library_db = tmp_path / "index" / "libraries" / "main" / "library.db"
    library_db.parent.mkdir(parents=True)
    queue = AnalysisQueue(library_db)
    queue.seen(token_id, "Studio PC")
    queue.close()
    revoked = runner.invoke(cli, [*base, "revoke", token_id])
    assert revoked.exit_code == 0, revoked.output
    assert store.authenticate(secret, SCOPE_ANALYSIS_WORKER) is None
    queue = AnalysisQueue(library_db)
    try:
        assert queue.workers() == []
    finally:
        queue.close()
    again = runner.invoke(cli, [*base, "revoke", token_id])
    assert again.exit_code == 1
    assert "not found" in again.output
    bad = runner.invoke(cli, [*base, "create", "x", "--scope", "admin"])
    assert bad.exit_code == 2
    blank = runner.invoke(cli, [*base, "create", " ", "--scope", SCOPE_ANALYSIS_WORKER])
    assert blank.exit_code == 1


def test_an_announcer_token_is_bound_to_one_station(store):
    """ADR-0007-02."""
    tokens, _clock = store
    token, secret = tokens.create("news-bot", [SCOPE_ANNOUNCER], "nightowl")
    assert token.station == "nightowl"
    found = tokens.authenticate(secret, SCOPE_ANNOUNCER)
    assert found is not None and found.station == "nightowl"
    assert tokens.list_tokens()[0].station == "nightowl"
    assert tokens.get(token.token_id).station == "nightowl"
    # It is no worker token, and a worker token is no announcer.
    assert tokens.authenticate(secret, SCOPE_ANALYSIS_WORKER) is None
    _worker, worker_secret = tokens.create("box", [SCOPE_ANALYSIS_WORKER])
    assert tokens.authenticate(worker_secret, SCOPE_ANNOUNCER) is None
    assert {SCOPE_ANNOUNCER} == STATION_SCOPES


@pytest.mark.parametrize(
    ("scopes", "station"),
    [
        ([SCOPE_ANNOUNCER], None),
        ([SCOPE_ANNOUNCER], ""),
        ([SCOPE_ANNOUNCER], "Bad Name"),
        ([SCOPE_ANNOUNCER], "../x"),
        ([SCOPE_ANALYSIS_WORKER], "default"),
    ],
)
def test_station_binding_is_validated(store, scopes, station):
    tokens, _clock = store
    with pytest.raises(ValueError):
        tokens.create("news-bot", scopes, station)
    assert validate_station([SCOPE_ANALYSIS_WORKER], None) is None


def test_an_older_token_file_gains_the_station_column(tmp_path):
    path = tmp_path / ".machine-tokens.sqlite3"
    with sqlite3.connect(path) as db:
        db.execute(
            "CREATE TABLE machine_tokens (token_id TEXT PRIMARY KEY, name TEXT NOT NULL, "
            "scopes TEXT NOT NULL, secret_sha256 TEXT NOT NULL, created_at INTEGER NOT NULL, "
            "last_used_at INTEGER, revoked_at INTEGER)"
        )
        db.execute(
            "INSERT INTO machine_tokens VALUES (?, 'old', ?, ?, 1, NULL, NULL)",
            ("a" * 32, SCOPE_ANALYSIS_WORKER, "0" * 64),
        )
    db.close()
    tokens = MachineTokenStore(path)
    assert [(t.name, t.station) for t in tokens.list_tokens()] == [("old", None)]
    made, _secret = tokens.create("news-bot", [SCOPE_ANNOUNCER], "default")
    assert tokens.get(made.token_id).station == "default"


def test_tokens_command_binds_an_announcer_token(tmp_path):
    from click.testing import CliRunner

    from autodj.cli import cli

    config = tmp_path / "config.toml"
    config.write_text(f'[index]\nindex_dir = "{(tmp_path / "index").as_posix()}"\n')
    runner = CliRunner()
    base = ["--config", str(config), "tokens"]
    made = runner.invoke(
        cli, [*base, "create", "news-bot", "--scope", SCOPE_ANNOUNCER, "--station", "default"]
    )
    assert made.exit_code == 0, made.output
    secret = made.stdout.strip()
    listed = runner.invoke(cli, [*base, "list"])
    assert "station: default" in listed.output
    assert secret.split(".")[2] not in listed.output
    unknown = runner.invoke(
        cli, [*base, "create", "x", "--scope", SCOPE_ANNOUNCER, "--station", "nightowl"]
    )
    assert unknown.exit_code == 1
    assert "No station is named" in unknown.output
    unbound = runner.invoke(cli, [*base, "create", "x", "--scope", SCOPE_ANNOUNCER])
    assert unbound.exit_code == 1
    assert "needs the station" in unbound.output


def test_adding_the_station_column_twice_is_harmless(tmp_path):
    from autodj.machine_tokens import add_station_column

    path = tmp_path / ".machine-tokens.sqlite3"
    MachineTokenStore(path)  # already has the column
    with sqlite3.connect(path) as db:
        add_station_column(db)  # the slower of two openers finds it there
    db.close()
    # Any other failure still stops the store from opening.
    with (
        sqlite3.connect(tmp_path / "empty.sqlite3") as empty,
        pytest.raises(sqlite3.OperationalError),
    ):
        add_station_column(empty)
    empty.close()
