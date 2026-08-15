import json
import re
import tomllib
from datetime import timedelta
from pathlib import Path

import yaml

from autodj.config import ENVIRONMENT_OVERLAY, load_config
from scripts.check_pip_audit_suppressions import SUPPRESSIONS

ROOT = Path(__file__).resolve().parents[2]


def _precommit_hook_ids(text: str) -> set[str]:
    config = yaml.safe_load(text)
    return {hook["id"] for repository in config["repos"] for hook in repository.get("hooks", ())}


def _documented_precommit_hook_ids(text: str) -> set[str]:
    normalized = " ".join(text.split())
    match = re.search(
        r"Pre-commit runs these hooks: (?P<hooks>.+?)\. Install them with",
        normalized,
    )
    assert match is not None
    return set(re.findall(r"`([^`]+)`", match.group("hooks")))


def _documented_non_hook_gates(text: str) -> set[str]:
    normalized = " ".join(text.split())
    match = re.search(
        r"Gates outside pre-commit include (?P<gates>.+?)\. Run their commands",
        normalized,
    )
    assert match is not None
    rendered = match.group("gates").replace(", and ", ", ")
    return {item.strip() for item in rendered.split(",")}


def _required_non_hook_gates(
    ci: str,
    release: str,
    package_scripts: dict[str, str],
) -> set[str]:
    evidence = {
        "lock checks": (
            "uv lock --check" in ci
            and "npm run audit:lock" in ci
            and "audit:lock" in package_scripts
        ),
        "the coverage-exclusion policy": ("uv run python scripts/check_coverage_policy.py" in ci),
        "Pyright": "uv run pyright src/autodj/" in ci,
        "Vitest": "vitest" in package_scripts.get("test", "") and "npm test" in ci,
        "the Vite build": (
            "vite build" in package_scripts.get("build", "") and "npm run build" in ci
        ),
        "the frontend dead-code scan": (
            bool(package_scripts.get("deadcode")) and "npm run deadcode" in ci
        ),
        "npm audit": "npm audit --audit-level=high" in ci,
        "Playwright audits": (
            "playwright" in package_scripts.get("audit:ci", "")
            or (bool(package_scripts.get("audit:ci")) and "npm run audit:ci" in ci)
        ),
        "container smoke": "bash scripts/container_smoke.sh" in ci,
        "release verification": (
            "uses: ./.github/workflows/ci.yml" in release
            and "uses: ./.github/workflows/security.yml" in release
            and "scripts/verify_release.py" in release
        ),
    }
    return {name for name, present in evidence.items() if present}


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


def test_operator_docs_cover_reproducible_workflows() -> None:
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    operations = (ROOT / "docs" / "operations.md").read_text(encoding="utf-8")
    contributing = (ROOT / "CONTRIBUTING.md").read_text(encoding="utf-8")
    security = (ROOT / "SECURITY.md").read_text(encoding="utf-8")
    threat = (ROOT / "THREAT_MODEL.md").read_text(encoding="utf-8")
    for command in ("autodj doctor", "autodj backup", "autodj restore"):
        assert command in readme
        assert command in operations
    assert "99.1%" in contributing
    assert "94.7%" in contributing
    assert "`uv run pyright src/autodj/`" in contributing
    assert "pre-commit does not run Pyright" in contributing
    assert "0.15.x" in security
    assert "AUTODJ_ACCESS_TOKEN" in threat
    assert "SQLite online backup" in operations
    assert "tracks.db-wal" in operations
    assert "container-internal `0.0.0.0`" in operations
    assert "host `127.0.0.1`" in operations
    assert "Windows PowerShell" in operations
    assert "Get-Date -Format yyyy-MM-dd" in operations


def test_threat_model_describes_device_pairing_not_removed_login() -> None:
    threat = (ROOT / "THREAT_MODEL.md").read_text(encoding="utf-8")

    assert "/api/login" not in threat
    assert "/api/pair" in threat
    assert "The browser never receives or sends the token" in threat
    assert "autodj devices revoke" in threat


def test_operator_docs_require_end_to_end_tls_for_untrusted_networks() -> None:
    operations = (ROOT / "docs" / "operations.md").read_text(encoding="utf-8")
    security = (ROOT / "SECURITY.md").read_text(encoding="utf-8")
    normalized_operations = " ".join(operations.replace("\\\n", " ").split())

    assert (
        "end-to-end TLS: run `autodj serve` directly with both `--ssl-certfile` and "
        "`--ssl-keyfile`" in normalized_operations
    )
    assert (
        "uv run autodj serve --host 0.0.0.0 --allowed-host radio.local "
        "--allowed-origin https://radio.local:8080 --ssl-certfile radio.pem "
        "--ssl-keyfile radio-key.pem" in normalized_operations
    )
    assert "leave `AUTODJ_ACCESS_TOKEN` exported" in operations
    assert "AutoDJ does not support TLS termination in front of its server" in normalized_operations
    assert "This supports private LAN access, not public Internet hosting" in normalized_operations
    assert "Public Internet hosting, including end-to-end TLS deployments" in security


def test_operator_docs_keep_security_and_quality_commands_exact() -> None:
    operations = (ROOT / "docs" / "operations.md").read_text(encoding="utf-8")
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    threat = (ROOT / "THREAT_MODEL.md").read_text(encoding="utf-8")
    changelog = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    precommit_text = (ROOT / ".pre-commit-config.yaml").read_text(encoding="utf-8")
    ci = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    release = (ROOT / ".github" / "workflows" / "release.yml").read_text(encoding="utf-8")
    package_scripts = json.loads((ROOT / "package.json").read_text(encoding="utf-8"))["scripts"]

    lan_setup = "uv run autodj setup-lan --host-name radio.local"
    lan_start = "docker compose --profile lan up autodj-lan"
    assert operations.index(lan_setup) < operations.index(lan_start)
    assert "gitignored `.env`" in operations
    assert "8-digit code" in operations
    assert "paired-device cookie" in operations

    assert operations.count("docker compose --profile lan down") == 3
    assert "docker compose down" not in operations

    assert operations.count(lan_setup) == 2

    configured_hooks = _precommit_hook_ids(precommit_text)
    documented_hooks = _documented_precommit_hook_ids(readme)
    assert documented_hooks == configured_hooks

    mutated_config = precommit_text.replace(
        "      - id: ruff",
        "      - id: future-hook\n      - id: ruff",
        1,
    )
    assert _precommit_hook_ids(mutated_config) != documented_hooks
    mutated_readme = readme.replace("`ruff`,", "", 1)
    assert _documented_precommit_hook_ids(mutated_readme) != configured_hooks

    required_non_hooks = _required_non_hook_gates(ci, release, package_scripts)
    documented_non_hooks = _documented_non_hook_gates(readme)
    assert documented_non_hooks == required_non_hooks
    mutated_scripts = dict(package_scripts)
    mutated_scripts["test"] = "jest run"
    assert _required_non_hook_gates(ci, release, mutated_scripts) != documented_non_hooks
    assert "all of these run as `pre-commit` hooks" not in readme.lower()

    osv_config = tomllib.loads((ROOT / "osv-scanner.toml").read_text(encoding="utf-8"))
    ignored = osv_config.get("IgnoredVulns", [])
    ignored_ids = {item["id"] for item in ignored}
    assert ignored_ids == set(SUPPRESSIONS)

    suppression_docs = threat.split("`uv.lock`", 1)[1].split("## Reporting", 1)[0]
    count_match = re.search(
        r"its\s+(?P<count>\w+)\s+ignored\s+advis(?:ory|ories)", suppression_docs
    )
    assert count_match is not None
    number_words = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5}
    assert number_words[count_match.group("count")] == len(ignored)
    assert ("records the rationale" in suppression_docs) == all(
        bool(item.get("reason", "").strip()) for item in ignored
    )
    documented_expiries = set(re.findall(r"\b\d{4}-\d{2}-\d{2}\b", suppression_docs))
    source_expiries = {expiry.isoformat() for expiry in SUPPRESSIONS.values()}
    assert documented_expiries == source_expiries
    mutated_expiries = {
        (expiry + timedelta(days=1)).isoformat() for expiry in SUPPRESSIONS.values()
    }
    assert documented_expiries != mutated_expiries
    assert "Suppressions require a documented rationale and review date." not in threat

    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    coverage = project["tool"]["autodj"]["coverage"]
    unreleased = changelog.split("## [Unreleased]", 1)[1].split("## [0.15.0]", 1)[0]
    assert f"{coverage['line_fail_under']}% line coverage" in unreleased
    assert f"{coverage['branch_fail_under']}% branch coverage" in unreleased
    assert "CI enforces" in unreleased
    release_entry = unreleased.split("The release workflow", 1)[1]
    verify_step = next(line for line in release.splitlines() if "Verify tag" in line)
    for identity in ("tag", "project", "changelog", "wheel"):
        assert identity in release_entry.lower()
        assert identity in verify_step.lower()
