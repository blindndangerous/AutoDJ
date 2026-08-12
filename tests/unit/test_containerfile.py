import json
import os
import re
import shutil
import socket
import stat
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
import yaml

from autodj.config import ServerConfig
from autodj.security import SecurityPolicy

ROOT = Path(__file__).resolve().parents[2]
REVIEWED_IMAGES = {
    (
        "python:3.14.6-slim-bookworm@sha256:"
        "86f975aca15cf04a40b399eebede9aea7c82eae084d1f1a0a6ef6bcaae871a30"
    ),
    (
        "node:24.6.0-bookworm-slim@sha256:"
        "9b741b28148b0195d62fa456ed84dd6c953c1f17a3761f3e6e6797a754d9edff"
    ),
    (
        "ghcr.io/astral-sh/uv:0.11.26@sha256:"
        "3d868e555f8f1dbc324afa005066cd11e1053fc4743b9808ca8025283e65efa5"
    ),
}
SENSITIVE_CONTEXT_EXCLUSIONS = {
    ".env*",
    "*.key",
    "*.pem",
    "config.toml",
    "config.local.toml*",
    "music",
    "index",
    "models",
}


def _assert_reviewed_images(content: str) -> None:
    stages: set[str] = set()
    external: set[str] = set()
    stage_index = 0
    logical_content = re.sub(r"\\\s*\n\s*", " ", content)
    for line in logical_content.splitlines():
        from_match = re.match(
            r"^\s*from\s+(?:--platform=\S+\s+)?(\S+)(?:\s+as\s+(\S+))?\s*$",
            line,
            re.IGNORECASE,
        )
        if from_match:
            reference, alias = from_match.groups()
            if reference.lower() not in stages and reference.lower() != "scratch":
                external.add(reference)
            stages.add(str(stage_index))
            stage_index += 1
            if alias:
                stages.add(alias.lower())
            continue
        copy_match = re.match(r"^\s*copy\b.*?--from\s*=\s*(\S+)", line, re.IGNORECASE)
        if copy_match:
            reference = copy_match.group(1)
            if reference.lower() not in stages:
                external.add(reference)
        if re.match(r"^\s*run\b", line, re.IGNORECASE):
            for mount in re.findall(r"--mount\s*=\s*([^\s]+)", line, re.IGNORECASE):
                source = next(
                    (
                        value
                        for key, value in (
                            part.split("=", 1) for part in mount.split(",") if "=" in part
                        )
                        if key.lower() == "from"
                    ),
                    None,
                )
                if source and source.lower() not in stages and not source.isdigit():
                    external.add(source)

    assert external == REVIEWED_IMAGES
    assert all(re.fullmatch(r"\S+@sha256:[0-9a-f]{64}", ref) for ref in external)


def _ignore_entries(content: str) -> set[str]:
    return {
        line.strip()
        for line in content.splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    }


def _healthcheck_command(content: str) -> str:
    match = re.search(r"^\s+CMD (\[.*\])$", content, re.MULTILINE)
    assert match is not None
    command = json.loads(match.group(1))
    assert command[:2] == ["/opt/venv/bin/python", "-c"]
    return command[2]


def _compose_command(service: str, environment: dict[str, str]) -> list[str]:
    compose = yaml.safe_load((ROOT / "compose.yaml").read_text(encoding="utf-8"))

    def resolve(value: str) -> str:
        return re.sub(
            r"\$\{([A-Z0-9_]+):-\}",
            lambda match: environment.get(match.group(1), ""),
            value,
        )

    return [resolve(value) for value in compose["services"][service]["command"]]


def _option_values(command: list[str], option: str) -> list[str]:
    return [command[index + 1] for index, value in enumerate(command[:-1]) if value == option]


def _health_validator_source() -> str:
    content = (ROOT / "scripts" / "container_smoke.sh").read_text(encoding="utf-8")
    match = re.search(
        r"^# HEALTH_VALIDATOR_START\n(?P<source>.*?)^# HEALTH_VALIDATOR_END$",
        content,
        re.MULTILINE | re.DOTALL,
    )
    assert match is not None
    return match.group("source")


def _validate_health(payload: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-c", _health_validator_source(), payload],
        check=False,
        capture_output=True,
        text=True,
    )


def _shell_function(content: str, name: str) -> str:
    match = re.search(
        rf"^{re.escape(name)}\(\) \{{\n(?P<body>.*?)^\}}$",
        content,
        re.MULTILINE | re.DOTALL,
    )
    assert match is not None
    return match.group("body")


def _assert_bounded_smoke_lifecycle(content: str) -> None:
    cleanup = _shell_function(content, "cleanup")
    logs = _shell_function(content, "emit_lan_failure_logs")
    down = _shell_function(content, "bounded_compose_down")

    assert cleanup.index("exit_code=$?") < cleanup.index("emit_lan_failure_logs")
    assert cleanup.index("emit_lan_failure_logs") < cleanup.index("bounded_compose_down")
    assert cleanup.index("bounded_compose_down") < cleanup.index('exit "$exit_code"')
    assert '"$lan_phase_active" == true && "$exit_code" -ne 0' in cleanup
    assert "timeout --signal=TERM --kill-after=5s 15s" in logs
    assert "docker compose --profile lan logs --no-color --tail 200 autodj-lan" in logs
    assert "|| true" in logs
    assert "timeout --signal=TERM --kill-after=5s 30s" in down
    assert "docker compose --profile lan down --volumes --remove-orphans" in down
    assert "bounded_compose_down || cleanup_exit_code=$?" in cleanup
    assert (
        len(
            re.findall(
                r"^\s*bounded_compose_down(?: \|\| cleanup_exit_code=\$\?)?\s*$",
                content,
                re.MULTILINE,
            )
        )
        == 2
    )

    lan_phase = content.index("lan_phase_active=true")
    login = content.index("http://127.0.0.1:8080/api/login", lan_phase)
    status = content.index("http://127.0.0.1:8080/api/status", login)
    login_command = content[
        content.rfind("curl --fail", lan_phase, login) : content.index("\nJSON", login)
    ]
    status_command = content[
        content.rfind("curl --fail", login, status) : content.index("\n", status)
    ]
    assert "|| true" not in login_command
    assert "|| true" not in status_command


def _git_bash() -> str:
    if os.name != "nt":
        executable = shutil.which("bash")
    else:
        git = shutil.which("git")
        executable = str(Path(git).resolve().parents[1] / "bin" / "bash.exe") if git else None
    assert executable is not None and Path(executable).is_file()
    return executable


def _bash_path(bash: str, path: Path) -> str:
    if os.name != "nt":
        return str(path)
    result = subprocess.run(
        [bash, "-lc", 'cygpath -u "$1"', "bash", str(path)],
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def _write_executable(path: Path, source: str) -> None:
    path.write_text(source, encoding="utf-8", newline="\n")
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


def _run_fake_container_smoke(
    tmp_path: Path,
    failing_endpoint: str,
    *,
    fail_rm: bool = False,
) -> subprocess.CompletedProcess[str]:
    bash = _git_bash()
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    log = tmp_path / "commands.log"
    _write_executable(fake_bin / "sudo", "#!/usr/bin/env bash\nexit 0\n")
    _write_executable(
        fake_bin / "rm",
        """#!/usr/bin/env bash
printf 'rm %s\\n' "$*" >> "$FAKE_SMOKE_LOG"
if [[ "$FAKE_RM_FAILURE" == true ]]; then exit 73; fi
exit 0
""",
    )
    _write_executable(
        fake_bin / "timeout",
        """#!/usr/bin/env bash
printf 'timeout %s\\n' "$*" >> "$FAKE_SMOKE_LOG"
if [[ "$1" == --signal=* ]]; then shift 3; else shift; fi
"$@"
""",
    )
    _write_executable(
        fake_bin / "docker",
        """#!/usr/bin/env bash
printf 'docker %s\\n' "$*" >> "$FAKE_SMOKE_LOG"
case "$*" in
  "compose --profile lan run --rm --no-deps autodj-lan")
    echo 'LAN binding requires allowed host, origin, and access token' >&2
    exit 2
    ;;
  "compose exec -T autodj id -u") echo 10001 ;;
  "compose exec -T autodj id -g") echo 10001 ;;
  "compose exec -T autodj stat -c %u:%g:%a "*) echo 10001:10001:755 ;;
  "compose --profile lan up -d autodj-lan") printf running > "$FAKE_LAN_STATE" ;;
  "compose --profile lan down --volumes --remove-orphans") printf stopped > "$FAKE_LAN_STATE" ;;
  "inspect autodj-lan --format "*) echo healthy ;;
  "inspect autodj --format "*) echo 127.0.0.1 ;;
esac
exit 0
""",
    )
    _write_executable(
        fake_bin / "curl",
        """#!/usr/bin/env bash
printf 'curl %s\\n' "$*" >> "$FAKE_SMOKE_LOG"
if [[ "$FAKE_CURL_FAILURE" == login && "$*" == *'/api/login'* ]]; then exit 22; fi
if [[ "$FAKE_CURL_FAILURE" == status && "$*" == *'/api/status'* ]]; then exit 22; fi
if [[ "$*" == *'/healthz'* ]]; then printf '{"status":"ok","tracks":0}'; fi
exit 0
""",
    )
    environment = os.environ.copy()
    environment.update(
        {
            "FAKE_CURL_FAILURE": failing_endpoint,
            "FAKE_LAN_STATE": _bash_path(bash, tmp_path / "lan.state"),
            "FAKE_RM_FAILURE": str(fail_rm).lower(),
            "FAKE_SMOKE_LOG": _bash_path(bash, log),
            "RUNNER_TEMP": _bash_path(bash, tmp_path),
        }
    )
    fake_path = _bash_path(bash, fake_bin)
    script = _bash_path(bash, ROOT / "scripts" / "container_smoke.sh")
    return subprocess.run(
        [bash, "-lc", 'export PATH="$1:$PATH"; exec bash "$2"', "bash", fake_path, script],
        check=False,
        capture_output=True,
        text=True,
        env=environment,
    )


def test_external_container_images_use_reviewed_manifest_digests() -> None:
    content = (ROOT / "Containerfile").read_text(encoding="utf-8")
    _assert_reviewed_images(content)


@pytest.mark.parametrize(
    "instruction",
    [
        "  fRoM registry.example/unreviewed:latest AS injected",
        "cOpY   --chown=10001 --FrOm = registry.example/unreviewed:latest /x /x",
        "COPY \\\n          --from=registry.example/unreviewed:latest /x /x",
        "rUn --Mount = type=bind,FrOm=registry.example/unreviewed:latest,target=/x true",
        "RUN \\\n          --mount=type=bind,from=registry.example/unreviewed:latest,target=/x true",
    ],
)
def test_image_contract_rejects_any_unreviewed_external_reference(
    instruction: str,
) -> None:
    content = (ROOT / "Containerfile").read_text(encoding="utf-8")

    with pytest.raises(AssertionError):
        _assert_reviewed_images(f"{content}\n{instruction}\n")


def test_image_contract_accepts_internal_run_mount_stage() -> None:
    content = (ROOT / "Containerfile").read_text(encoding="utf-8")

    _assert_reviewed_images(f"{content}\nRUN --mount=type=bind,from=python-base,target=/x true\n")


def test_docker_and_podman_exclude_the_same_sensitive_build_context() -> None:
    dockerignore = (ROOT / ".dockerignore").read_text(encoding="utf-8")
    containerignore = (ROOT / ".containerignore").read_text(encoding="utf-8")

    assert containerignore == dockerignore
    assert SENSITIVE_CONTEXT_EXCLUSIONS.issubset(_ignore_entries(dockerignore))


@pytest.mark.parametrize("bind_host", ["0.0.0.0", "127.0.0.1", "localhost"])
def test_container_healthcheck_uses_ipv4_loopback_and_effective_port(
    bind_host: str,
) -> None:
    content = (ROOT / "Containerfile").read_text(encoding="utf-8")
    command = _healthcheck_command(content)

    requests: list[str] = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            requests.append(self.path)
            self.send_response(200)
            self.end_headers()

        def log_message(self, format: str, *args: object) -> None:
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    environment = os.environ.copy()
    environment.update({"AUTODJ_HOST": bind_host, "AUTODJ_PORT": str(server.server_port)})
    try:
        result = subprocess.run(
            [sys.executable, "-c", command],
            check=False,
            capture_output=True,
            text=True,
            env=environment,
        )
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)

    assert "AUTODJ_HOST" in command
    assert "AUTODJ_PORT" in command
    assert "127.0.0.1" in command
    assert "127.0.0.1:8080" not in command
    assert result.returncode == 0, result.stderr
    assert requests == ["/healthz"]


@pytest.mark.parametrize("bind_host", ["::", "::1", "[::1]"])
def test_container_healthcheck_uses_ipv6_loopback_and_effective_port(
    bind_host: str,
) -> None:
    content = (ROOT / "Containerfile").read_text(encoding="utf-8")
    command = _healthcheck_command(content)
    requests: list[str] = []

    class IPv6Server(ThreadingHTTPServer):
        address_family = socket.AF_INET6

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            requests.append(self.path)
            self.send_response(200)
            self.end_headers()

        def log_message(self, format: str, *args: object) -> None:
            pass

    try:
        server = IPv6Server(("::1", 0), Handler)
    except OSError as exc:
        pytest.skip(f"IPv6 loopback unavailable: {exc}")
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    environment = os.environ.copy()
    environment.update({"AUTODJ_HOST": bind_host, "AUTODJ_PORT": str(server.server_port)})
    try:
        result = subprocess.run(
            [sys.executable, "-c", command],
            check=False,
            capture_output=True,
            text=True,
            env=environment,
        )
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)

    assert "[::1]" in command
    assert result.returncode == 0, result.stderr
    assert requests == ["/healthz"]


def test_container_healthcheck_rejects_unexpected_probe_host() -> None:
    content = (ROOT / "Containerfile").read_text(encoding="utf-8")
    command = _healthcheck_command(content)
    environment = os.environ.copy()
    environment.update({"AUTODJ_HOST": "attacker.example/redirect", "AUTODJ_PORT": "8080"})

    result = subprocess.run(
        [sys.executable, "-c", command],
        check=False,
        capture_output=True,
        text=True,
        env=environment,
    )

    assert result.returncode != 0


def test_authenticated_lan_healthcheck_is_allowed_by_server_policy() -> None:
    command = _healthcheck_command((ROOT / "Containerfile").read_text(encoding="utf-8"))
    compose_command = _compose_command(
        "autodj-lan",
        {
            "AUTODJ_LAN_HOST": "radio.local",
            "AUTODJ_LAN_ORIGIN": "http://radio.local:8080",
        },
    )
    policy = SecurityPolicy(
        ServerConfig(
            host="0.0.0.0",
            access_token="s" * 32,
            allowed_hosts=_option_values(compose_command, "--allowed-host"),
            allowed_origins=_option_values(compose_command, "--allowed-origin"),
        )
    )
    requests: list[tuple[str, str | None, int]] = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            host = self.headers.get("Host")
            status = 200 if policy.host_allowed(host) else 403
            requests.append((self.path, host, status))
            self.send_response(status)
            self.end_headers()
            self.wfile.write(b'{"status":"ok","tracks":0}')

        def log_message(self, format: str, *args: object) -> None:
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    environment = os.environ.copy()
    environment.update({"AUTODJ_HOST": "0.0.0.0", "AUTODJ_PORT": str(server.server_port)})
    try:
        result = subprocess.run(
            [sys.executable, "-c", command],
            check=False,
            capture_output=True,
            text=True,
            env=environment,
        )
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)

    assert policy.authentication_required is True
    assert policy.host_allowed("radio.local") is True
    assert policy.host_allowed("evil.example") is False
    assert policy.origin_allowed("http://radio.local:8080") is True
    assert policy.origin_allowed("http://evil.example:8080") is False
    assert result.returncode == 0, result.stderr
    assert requests == [("/healthz", f"127.0.0.1:{server.server_port}", 200)]


def test_compose_defaults_to_named_writable_volumes_with_bind_overrides() -> None:
    compose = yaml.safe_load((ROOT / "compose.yaml").read_text(encoding="utf-8"))

    assert set(compose["volumes"]) == {"autodj-index", "autodj-models"}
    assert compose["x-autodj-common"]["volumes"] == [
        "${AUTODJ_MUSIC_DIR:-./music}:/music:ro",
        "${AUTODJ_INDEX_DIR:-autodj-index}:/index",
        "${AUTODJ_MODEL_DIR:-autodj-models}:/models",
    ]


def test_container_smoke_uses_private_unique_workspace_and_guarded_cleanup() -> None:
    content = (ROOT / "scripts" / "container_smoke.sh").read_text(encoding="utf-8")

    assert 'mktemp -d -- "$temp_parent/autodj-smoke.XXXXXXXX"' in content
    assert 'chmod 0700 "$smoke_root"' in content
    assert '"$smoke_root/autodj-lan-negative.log"' in content
    assert 'rm -rf -- "$smoke_root"' in content
    assert "${RUNNER_TEMP:-/tmp}/autodj-index" not in content
    assert "timeout 15s docker compose --profile lan run" in content
    assert "lan_exit_code=$?" in content
    assert '"$lan_exit_code" -eq 124' in content
    assert "LAN validation timed out" in content


def test_container_smoke_bounds_failure_logs_and_both_teardowns() -> None:
    content = (ROOT / "scripts" / "container_smoke.sh").read_text(encoding="utf-8")

    _assert_bounded_smoke_lifecycle(content)


@pytest.mark.parametrize(
    ("old", "new"),
    [
        ("timeout --signal=TERM --kill-after=5s 15s", "timeout 15s"),
        ("--tail 200 autodj-lan", "autodj-lan"),
        ("timeout --signal=TERM --kill-after=5s 30s", "timeout 30s"),
        (
            "docker compose --profile lan down --volumes --remove-orphans",
            "docker compose down --volumes --remove-orphans",
        ),
        ("    bounded_compose_down || cleanup_exit_code=$?\n", ""),
        (
            "\nbounded_compose_down\nexport AUTODJ_LAN_BIND_ADDRESS",
            "\nexport AUTODJ_LAN_BIND_ADDRESS",
        ),
        ("lan_phase_active=true", "lan_phase_active=false"),
        (
            "http://127.0.0.1:8080/api/login >/dev/null <<JSON",
            "http://127.0.0.1:8080/api/login >/dev/null || true <<JSON",
        ),
        (
            "http://127.0.0.1:8080/api/status >/dev/null",
            "http://127.0.0.1:8080/api/status >/dev/null || true",
        ),
    ],
)
def test_container_smoke_lifecycle_contract_rejects_mutation(old: str, new: str) -> None:
    content = (ROOT / "scripts" / "container_smoke.sh").read_text(encoding="utf-8")
    assert old in content

    with pytest.raises((AssertionError, ValueError)):
        _assert_bounded_smoke_lifecycle(content.replace(old, new, 1))


@pytest.mark.parametrize("failing_endpoint", ["login", "status"])
def test_container_smoke_routes_lan_request_failure_through_bounded_trap(
    tmp_path: Path,
    failing_endpoint: str,
) -> None:
    result = _run_fake_container_smoke(tmp_path, failing_endpoint)
    commands = (tmp_path / "commands.log").read_text(encoding="utf-8")

    assert result.returncode == 22, result.stderr
    failure = commands.index(f"/api/{failing_endpoint}")
    logs = commands.index(
        "timeout --signal=TERM --kill-after=5s 15s "
        "docker compose --profile lan logs --no-color --tail 200 autodj-lan",
        failure,
    )
    cleanup = commands.index(
        "timeout --signal=TERM --kill-after=5s 30s "
        "docker compose --profile lan down --volumes --remove-orphans",
        logs,
    )
    assert failure < logs < cleanup
    assert (
        len(
            re.findall(
                r"^docker compose --profile lan down --volumes --remove-orphans$",
                commands,
                re.MULTILINE,
            )
        )
        == 2
    )
    assert (tmp_path / "lan.state").read_text(encoding="utf-8") == "stopped"


@pytest.mark.parametrize(
    ("failing_endpoint", "expected_exit"),
    [("login", 22), ("", 73)],
)
def test_container_smoke_handles_workspace_removal_failure_without_losing_exit_policy(
    tmp_path: Path,
    failing_endpoint: str,
    expected_exit: int,
) -> None:
    result = _run_fake_container_smoke(tmp_path, failing_endpoint, fail_rm=True)

    assert result.returncode == expected_exit, result.stderr


@pytest.mark.parametrize(
    "payload",
    [
        '{"status":"ok","tracks":0}',
        '{ "tracks" : 0, "extra" : true, "status" : "ok" }',
    ],
)
def test_container_health_validator_accepts_semantic_empty_health(payload: str) -> None:
    result = _validate_health(payload)

    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize(
    "payload",
    [
        '{"status":"ok"}',
        '{"status":"ok","tracks":1}',
        '{"status":"error","tracks":0}',
        '{"status":"ok","tracks":false}',
        "not-json",
    ],
)
def test_container_health_validator_rejects_nonempty_or_invalid_health(
    payload: str,
) -> None:
    result = _validate_health(payload)

    assert result.returncode != 0
