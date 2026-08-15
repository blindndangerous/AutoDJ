# Delivery and Maintenance Remediation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make no-config startup, containers, releases, dependency maintenance, CI diagnosis, browser audits, and backups reproducible and consistent with documented behavior.

**Architecture:** Configuration is assembled once through a typed merge pipeline and consumed by CLI, doctor, container, and backup code. Build and release identity comes only from `pyproject.toml`; CI recreates Python, Node, browser, and container artifacts from committed locks and promotes only artifacts that pass tag/version/runtime checks. Operational checks remain read-only, while backup/restore uses a versioned ZIP manifest and SQLite's online backup API for coherent WAL snapshots.

**Tech Stack:** Python 3.14, Click, dataclasses, TOML, FastAPI/Uvicorn, FAISS, SQLite, Docker/Podman Compose, uv, Node 24, npm, Vite 8, Vitest 4.1, ESLint 10, Playwright, GitHub Actions, Trivy, OSV-Scanner, pytest/coverage, mypy, Pyright.

---

## Execution prerequisites and file map

This plan runs **after** `docs/superpowers/specs/2026-08-01-security-data-integrity-remediation-design.md` is implemented. It consumes, and must not redefine, these security-owned APIs:

- `autodj.config.ServerConfig`, available as `AutoDJConfig.server`, with `host`, `port`, `access_token`, and `insecure_lan` fields plus the security plan's validators.
- The security plan's LAN authentication enforcement in `autodj serve` and its loopback-anonymous behavior.
- `autodj.model.ModelCacheStatus(path: Path, complete: bool, reason: str)` and `inspect_model_cache(model_cfg: ModelConfig, index_cfg: IndexConfig) -> ModelCacheStatus`.
- `autodj.index_manifest.copy_published_snapshot(index_dir: Path, destination: Path, *, expected_generation: int | None = None) -> IndexManifest`. The security plan owns generation locking, immutable manifest-referenced artifact selection, digest/count validation, and the final generation re-read; backup code consumes this API and must not reproduce it.
- The security plan's `--insecure-lan`, `--allowed-host`, and `--allowed-origin` CLI gates. This delivery plan owns Compose and invokes those gates explicitly.

If those symbols are absent, stop before Task 1 and finish the security plan. Start execution in an
isolated worktree created with `superpowers:using-git-worktrees`; the tooling-file removal is
committed in `d245c9d`, so do not recreate or restore `CLAUDE.md`.

Files created by this plan:

- `src/autodj/version.py` — authoritative installed/source version accessor.
- `src/autodj/doctor.py` — typed, read-only operational checks and rendering.
- `src/autodj/backup.py` — versioned backup/restore archive implementation.
- `tests/unit/test_version.py`, `tests/unit/test_doctor.py`, `tests/unit/test_backup.py` — focused TDD coverage.
- `tests/integration/test_empty_serve.py` — empty-library serving and health contract.
- `tests/unit/test_config_examples.py` — shipped-config synchronization.
- `tests/playwright/audit_helpers.mjs` — browser selection, assertions, report/exit handling.
- `scripts/container_smoke.sh` — deterministic container startup/UID/health checks.
- `tests/unit/test_containerfile.py` — reviewed external-image digest source contract.
- `scripts/verify_release.py` and `tests/unit/test_verify_release.py` — release identity checks.
- `scripts/check_coverage_policy.py` and `tests/unit/test_coverage_policy.py` — exclusion-policy guard.
- `.dockerignore`, `config.local.toml.example`, `docs/operations.md` — safe build context and operator guidance.

Existing files modified together:

- Configuration/CLI: `src/autodj/config.py`, `src/autodj/cli.py`, `config.toml.example`, `.gitignore`.
- Empty serving/health: `src/autodj/similarity.py`, `src/autodj/player.py`, `src/autodj/server.py`.
- Container/reproducibility: `Containerfile`, `compose.yaml`, `renovate.json`, `.github/workflows/ci.yml`.
- Version/release: `pyproject.toml`, `uv.lock`, `src/autodj/__init__.py`, `src/autodj/server.py`, `package.json`, `vite.config.js`, `.github/workflows/release.yml`.
- Frontend gates: `package.json`, `package-lock.json`, `.gitignore`, `.pre-commit-config.yaml`, `eslint.config.js`, four `tests/playwright/*_audit.mjs` files.
- Python gates: `pyproject.toml`, `.github/workflows/ci.yml`, and the coverage-policy script/test.
- Documentation: `README.md`, `SECURITY.md`, `THREAT_MODEL.md`, `CONTRIBUTING.md`, `CHANGELOG.md`.

### Task 1: Optional configuration and typed environment precedence

**Files:**
- Modify: `src/autodj/config.py:622-771`
- Modify: `src/autodj/cli.py:144-152,367-400,1358-1370,1658-1700`
- Modify: `tests/unit/test_config.py:69-90,320-378`
- Modify: `tests/unit/test_cli.py:172-215,526-570`

- [ ] **Step 1: Write failing configuration and CLI tests**

Add these imports and tests to `tests/unit/test_config.py`:

```python
import os

from autodj.config import ENVIRONMENT_OVERLAY, load_config


class TestDefaultAndEnvironmentConfig:
    def test_missing_implicit_config_uses_validated_defaults(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.chdir(tmp_path)
        cfg = load_config(None, environ={})
        assert cfg.library.music_dir == Path("music")
        assert cfg.index.index_dir == Path("index")
        assert cfg.index.model_dir == Path("models")
        assert cfg.server.host == "127.0.0.1"
        assert cfg.server.port == 8080
        assert cfg.config_path is None
        assert cfg.config_sources == ("defaults",)

    def test_explicit_missing_config_remains_strict(self, tmp_path: Path) -> None:
        with pytest.raises(FileNotFoundError, match="Config file not found"):
            load_config(tmp_path / "missing.toml", environ={})

    def test_precedence_defaults_toml_local_environment(self, tmp_path: Path) -> None:
        base = tmp_path / "config.toml"
        base.write_text(
            '[library]\nmusic_dir = "toml-music"\n'
            '[index]\nindex_dir = "toml-index"\nmodel_dir = "toml-models"\n'
            '[server]\nhost = "127.0.0.2"\nport = 8081\n'
            'access_token = "test-token-0123456789abcdef0123456789"\n',
            encoding="utf-8",
        )
        (tmp_path / "config.local.toml").write_text(
            '[library]\nmusic_dir = "local-music"\n[server]\nport = 8082\n',
            encoding="utf-8",
        )
        cfg = load_config(
            base,
            environ={
                "AUTODJ_LIBRARY_MUSIC_DIR": "env-music",
                "AUTODJ_INDEX_DIR": "env-index",
                "AUTODJ_MODEL_DIR": "env-models",
                "AUTODJ_HOST": "127.0.0.3",
                "AUTODJ_PORT": "8083",
                "AUTODJ_ACCESS_TOKEN": "env-token-0123456789abcdef0123456789",
                "AUTODJ_HUGGINGFACE_TOKEN": "hf-token-0123456789abcdef0123456789",
            },
        )
        assert cfg.library.music_dir == Path("env-music")
        assert cfg.index.index_dir == Path("env-index")
        assert cfg.index.model_dir == Path("env-models")
        assert (cfg.server.host, cfg.server.port) == ("127.0.0.3", 8083)
        assert cfg.server.access_token == "env-token-0123456789abcdef0123456789"
        assert cfg.huggingface.token == "hf-token-0123456789abcdef0123456789"
        assert cfg.config_sources == (
            "defaults",
            str(base),
            str(tmp_path / "config.local.toml"),
            "environment",
        )

    @pytest.mark.parametrize("value", ["", "eight", "0", "65536"])
    def test_invalid_environment_port_uses_server_validator(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, value: str
    ) -> None:
        monkeypatch.chdir(tmp_path)
        with pytest.raises(ValueError, match="AUTODJ_PORT|port"):
            load_config(None, environ={"AUTODJ_PORT": value})

    def test_environment_contract_is_complete(self) -> None:
        assert set(ENVIRONMENT_OVERLAY) == {
            "AUTODJ_LIBRARY_MUSIC_DIR",
            "AUTODJ_INDEX_DIR",
            "AUTODJ_MODEL_DIR",
            "AUTODJ_HOST",
            "AUTODJ_PORT",
            "AUTODJ_ACCESS_TOKEN",
            "AUTODJ_HUGGINGFACE_TOKEN",
        }
```

Replace the old implicit-missing assertions in `tests/unit/test_cli.py` with:

```python
class TestCliConfigSelection:
    def test_omitted_config_passes_none_to_loader(self) -> None:
        cfg = _make_cfg()
        with (
            patch("autodj.config.load_config", return_value=cfg) as loader,
            patch("autodj.similarity.SimilarityIndex.from_index_dir", return_value=_make_sim()),
            patch("autodj.server.serve"),
        ):
            result = CliRunner().invoke(cli, ["serve"])
        assert result.exit_code == 0
        loader.assert_called_once_with(None)

    def test_explicit_missing_config_exits_one(self, tmp_path: Path) -> None:
        result = CliRunner().invoke(
            cli, ["--config", str(tmp_path / "missing.toml"), "serve"]
        )
        assert result.exit_code == 1
        assert "Config not found" in result.output

    def test_explicit_cli_host_port_override_effective_config(self) -> None:
        cfg = _make_cfg()
        cfg.server.host = "127.0.0.2"
        cfg.server.port = 8082
        with (
            patch("autodj.config.load_config", return_value=cfg),
            patch("autodj.similarity.SimilarityIndex.from_index_dir", return_value=_make_sim()),
            patch("autodj.server.serve") as serve_mock,
        ):
            result = CliRunner().invoke(cli, ["serve", "--host", "127.0.0.3", "--port", "8083"])
        assert result.exit_code == 0
        assert serve_mock.call_args.kwargs["host"] == "127.0.0.3"
        assert serve_mock.call_args.kwargs["port"] == 8083
        assert cfg.config_sources[-1] == "cli"

    def test_no_cli_override_does_not_claim_cli_config_source(self) -> None:
        cfg = _make_cfg()
        original_sources = cfg.config_sources
        with (
            patch("autodj.config.load_config", return_value=cfg),
            patch("autodj.similarity.SimilarityIndex.from_index_dir", return_value=_make_sim()),
            patch("autodj.server.serve"),
        ):
            result = CliRunner().invoke(cli, ["serve"])
        assert result.exit_code == 0
        assert cfg.config_sources == original_sources
```

- [ ] **Step 2: Run the focused tests and verify the new contract is red**

Run: `uv run pytest tests/unit/test_config.py::TestDefaultAndEnvironmentConfig tests/unit/test_cli.py::TestCliConfigSelection -q`

Expected: FAIL because `ENVIRONMENT_OVERLAY`, `config_sources`, and `load_config(None, environ=...)` do not exist and Click still substitutes `config.toml`, `127.0.0.1`, and `8080` before configuration is loaded.

- [ ] **Step 3: Implement one typed merge pipeline and preserve security validators**

Add the following near the public loader in `src/autodj/config.py`; retain all existing dataclass constructors and add the security-owned `server=ServerConfig.from_dict(...)` in `_build_config`:

```python
from collections.abc import Mapping

ENVIRONMENT_OVERLAY: dict[str, tuple[str, str, type[str] | type[int]]] = {
    "AUTODJ_LIBRARY_MUSIC_DIR": ("library", "music_dir", str),
    "AUTODJ_INDEX_DIR": ("index", "index_dir", str),
    "AUTODJ_MODEL_DIR": ("index", "model_dir", str),
    "AUTODJ_HOST": ("server", "host", str),
    "AUTODJ_PORT": ("server", "port", int),
    "AUTODJ_ACCESS_TOKEN": ("server", "access_token", str),
    "AUTODJ_HUGGINGFACE_TOKEN": ("huggingface", "token", str),
}


def _default_raw() -> dict[str, Any]:
    return {
        "library": {"music_dir": "music"},
        "index": {"index_dir": "index", "model_dir": "models"},
        "server": {"host": "127.0.0.1", "port": 8080},
    }


def _environment_overlay(environ: Mapping[str, str]) -> dict[str, Any]:
    overlay: dict[str, Any] = {}
    for variable, (section, key, converter) in ENVIRONMENT_OVERLAY.items():
        if variable not in environ:
            continue
        raw_value = environ[variable]
        try:
            value = converter(raw_value)
        except ValueError as exc:
            raise ValueError(f"{variable} has invalid value {raw_value!r}") from exc
        overlay.setdefault(section, {})[key] = value
    return overlay


def _build_config(
    raw: dict[str, Any], *, config_path: Path | None, sources: list[str], presets_raw: dict[str, Any]
) -> AutoDJConfig:
    from autodj.presets import load_user_presets

    return AutoDJConfig(
        library=LibraryConfig.from_dict(raw.get("library", {})),
        index=IndexConfig.from_dict(raw.get("index", {})),
        playback=PlaybackConfig.from_dict(raw.get("playback", {})),
        model=ModelConfig.from_dict(raw.get("model", {})),
        huggingface=HuggingFaceConfig.from_dict(raw.get("huggingface", {})),
        replaygain=ReplayGainConfig.from_dict(raw.get("replaygain", {})),
        djmix=DjMixConfig.from_dict(raw.get("djmix", {})),
        transitions=TransitionsConfig.from_dict(raw.get("transitions", {})),
        server=ServerConfig.from_dict(raw.get("server", {})),
        presets=load_user_presets(presets_raw),
        config_path=config_path,
        config_sources=tuple(sources),
    )


def load_config(
    path: str | Path | None = None, *, environ: Mapping[str, str] | None = None
) -> AutoDJConfig:
    """Load defaults, optional TOML/local TOML, then environment overrides."""
    environment = os.environ if environ is None else environ
    explicit = path is not None
    candidate = Path(path) if explicit else Path("config.toml")
    raw = _default_raw()
    sources = ["defaults"]
    loaded_path: Path | None = None

    if candidate.exists():
        with candidate.open("rb") as fh:
            raw = _deep_merge(raw, tomllib.load(fh))
        loaded_path = candidate
        sources.append(str(candidate))
    elif explicit:
        raise FileNotFoundError(f"Config file not found: {candidate}")

    if loaded_path is not None:
        local_path = loaded_path.parent / "config.local.toml"
        if local_path.exists():
            with local_path.open("rb") as fh:
                raw = _deep_merge(raw, tomllib.load(fh))
            sources.append(str(local_path))

    env_raw = _environment_overlay(environment)
    if env_raw:
        raw = _deep_merge(raw, env_raw)
        sources.append("environment")

    sidecar_root = loaded_path.parent if loaded_path is not None else Path.cwd()
    presets_path = sidecar_root / "presets.toml"
    if presets_path.exists():
        with presets_path.open("rb") as fh:
            presets_raw = tomllib.load(fh)
    else:
        presets_raw = {"presets": raw["presets"]} if "presets" in raw else {}
    return _build_config(
        raw, config_path=loaded_path, sources=sources, presets_raw=presets_raw
    )
```

Change `AutoDJConfig.config_path` to `Path | None` and add `config_sources: tuple[str, ...] = ("defaults",)`. Add `import os` at the top of `config.py`.

In `src/autodj/cli.py`, make the root option default `None`, change the helper to accept `str | None`, and defer host/port defaults until after loading:

```python
def _load_cfg_or_exit(config_path: str | None) -> AutoDJConfig:
    from autodj.config import load_config

    try:
        return load_config(config_path)
    except (FileNotFoundError, ValueError) as exc:
        console.print(f"[bold red]Config not found or invalid:[/] {exc}")
        raise click.exceptions.Exit(1) from exc
```

```python
@click.option(
    "--config",
    "config_path",
    default=None,
    type=click.Path(dir_okay=False),
    help="Optional TOML config. An explicitly supplied missing path is an error.",
)
```

For `serve`, set `--host` and `--port` defaults to `None`, type them as optional in `cmd_serve`, then immediately after `_apply_serve_overrides` resolve:

```python
host = host if host is not None else cfg.server.host
port = port if port is not None else cfg.server.port
```

Make `_apply_serve_overrides` return whether it actually wrote any configuration field. Aggregate
that result with explicit host, port, access-token, and security/LAN configuration overrides
without short-circuiting the helper call. If any effective configuration field came from Click,
append `"cli"` exactly once so doctor reports the true final precedence:

```python
general_cli_override = _apply_serve_overrides(cfg, locals())
server_cli_override = _apply_security_serve_overrides(cfg, locals())
host_or_port_override = host is not None or port is not None
if general_cli_override or server_cli_override or host_or_port_override:
    cfg.config_sources = (*cfg.config_sources, "cli")
```

Use the security plan's actual helper name if it differs from the illustrative
`_apply_security_serve_overrides`; the contract is that every explicit CLI value which mutates the
effective config contributes one final `cli` source, while operational-only flags do not.

Keep the security plan's `access_token`/`insecure_lan` enforcement after this resolution so environment-derived server values pass through the same validation as TOML and CLI values.

- [ ] **Step 4: Run focused and regression configuration tests**

Run: `uv run pytest tests/unit/test_config.py tests/unit/test_cli.py tests/smoke/test_cli_smoke.py -q`

Expected: PASS. Explicit `--config missing.toml` tests remain strict; implicit calls now reach later
index/music validation instead of failing on configuration; doctor provenance ends with `cli` only
when an explicit Click override changed effective configuration.

- [ ] **Step 5: Commit the configuration behavior**

```bash
git add src/autodj/config.py src/autodj/cli.py tests/unit/test_config.py tests/unit/test_cli.py tests/smoke/test_cli_smoke.py
git commit -m "feat: add optional layered configuration"
```

### Task 2: Synchronized example configuration

**Files:**
- Create: `config.local.toml.example`
- Create: `tests/unit/test_config_examples.py`
- Modify: `config.toml.example:1-64,189-202`

- [ ] **Step 1: Write failing example-contract tests**

Create `tests/unit/test_config_examples.py`:

```python
from pathlib import Path

from autodj.config import ENVIRONMENT_OVERLAY, load_config

ROOT = Path(__file__).resolve().parents[2]


def test_base_example_loads_without_environment() -> None:
    cfg = load_config(ROOT / "config.toml.example", environ={})
    assert cfg.library.music_dir == Path("Z:/Music")
    assert cfg.index.index_dir == Path("index")
    assert cfg.index.model_dir == Path("models")
    assert cfg.server.host == "127.0.0.1"
    assert cfg.server.port == 8080


def test_local_example_is_a_valid_overlay(tmp_path: Path) -> None:
    base = tmp_path / "config.toml"
    base.write_bytes((ROOT / "config.toml.example").read_bytes())
    (tmp_path / "config.local.toml").write_bytes(
        (ROOT / "config.local.toml.example").read_bytes()
    )
    cfg = load_config(base, environ={})
    assert cfg.library.music_dir == Path("/srv/music")
    assert cfg.index.index_dir == Path("/srv/autodj/index")


def test_examples_name_current_sqlite_stores() -> None:
    text = (ROOT / "config.toml.example").read_text(encoding="utf-8")
    assert "tracks.db (SQLite)" in text
    assert "dj_meta.db (SQLite)" in text
    assert "metadata.json" not in text


def test_documented_environment_variables_are_listed() -> None:
    text = (ROOT / "config.toml.example").read_text(encoding="utf-8")
    for variable in ENVIRONMENT_OVERLAY:
        assert variable in text
```

- [ ] **Step 2: Run the tests and verify examples are incomplete**

Run: `uv run pytest tests/unit/test_config_examples.py -q`

Expected: FAIL because `config.local.toml.example`, `[server]`, environment-variable documentation, and current SQLite comments are absent.

- [ ] **Step 3: Add the local overlay and authoritative comments**

Create `config.local.toml.example` with this complete content:

```toml
# Per-machine overrides, loaded only when a sibling config.toml exists.
# Copy to config.local.toml; this example remains safe to commit.

[library]
music_dir = "/srv/music"
beets_db = "/srv/beets/library.db"

[index]
# Each named subdirectory contains vectors.index, tracks.db (SQLite), dj_meta.db (SQLite),
# web_state.json, and the default liners directory.
index_dir = "/srv/autodj/index"
model_dir = "/srv/autodj/models"

[server]
host = "127.0.0.1"
port = 8080
# Supply access_token through AUTODJ_ACCESS_TOKEN; never commit it here.
```

Add `[server]` with loopback/port to `config.toml.example`. Replace obsolete metadata-store comments with the exact `vectors.index`, `tracks.db (SQLite)`, and `dj_meta.db (SQLite)` names. Add one comment block listing all seven `ENVIRONMENT_OVERLAY` names and the precedence `defaults < config.toml < config.local.toml < environment < CLI`.

- [ ] **Step 4: Run example and configuration tests**

Run: `uv run pytest tests/unit/test_config_examples.py tests/unit/test_config.py -q`

Expected: PASS.

- [ ] **Step 5: Commit shipped configuration examples**

```bash
git add config.toml.example config.local.toml.example tests/unit/test_config_examples.py
git commit -m "docs: synchronize configuration examples"
```

### Task 3: Empty-library serve mode and health endpoint

**Files:**
- Create: `tests/integration/test_empty_serve.py`
- Modify: `src/autodj/similarity.py:117-220`
- Modify: `src/autodj/player.py:872-914`
- Modify: `src/autodj/cli.py:169-181,1658-1662`
- Modify: `src/autodj/server.py:373-376`

- [ ] **Step 1: Write failing empty-index and health tests**

Create `tests/integration/test_empty_serve.py`:

```python
import threading
from pathlib import Path
from unittest.mock import patch

import numpy as np
from click.testing import CliRunner
from fastapi.testclient import TestClient

from autodj.cli import cli
from autodj.config import load_config
from autodj.index_manifest import read_manifest
from autodj.indexer import FEATURE_DIM, IndexEntry, save_index
from autodj.player import Player
from autodj.server import PlayerBridge, create_app
from autodj.similarity import SimilarityIndex


def test_empty_similarity_index_has_feature_dimension() -> None:
    sim = SimilarityIndex.empty()
    assert sim.ntotal == 0
    assert sim.faiss_index.d == 1040


def test_empty_similarity_index_reloads_a_published_generation(tmp_path: Path) -> None:
    sim = SimilarityIndex.empty()
    entry = IndexEntry(
        path=str(tmp_path / "song.flac"), title="Song", artist="Artist", album="",
        genre="", bpm=0.0, year=0, length=1.0, energy=0.0, key=-1, mode=-1,
        tempo_confidence=0.0,
    )
    vectors = np.zeros((1, FEATURE_DIM), dtype=np.float32)
    vectors[0, 0] = 1.0
    save_index([entry], vectors, tmp_path)
    manifest = read_manifest(tmp_path)
    assert manifest is not None
    assert sim.reload_from_disk(tmp_path, expected_generation=manifest.generation) == 1
    assert sim.ntotal == 1
    assert sim.entries_snapshot() == (entry,)


def test_player_waits_safely_for_first_index_generation(tmp_path: Path) -> None:
    cfg = load_config(None, environ={})
    sim = SimilarityIndex.empty()
    player = Player(cfg, sim, dry_run=True, no_keyboard=True)
    thread = threading.Thread(target=player.run, kwargs={"seed_entry": None})
    thread.start()
    assert thread.is_alive()
    player.stop()
    thread.join(timeout=2)
    assert not thread.is_alive()


def test_healthz_is_minimal_and_reports_empty_library(bridge: PlayerBridge) -> None:
    bridge.sim = SimilarityIndex.empty()
    response = TestClient(create_app(bridge)).get("/healthz")
    assert response.status_code == 200
    assert response.json() == {"status": "ok", "tracks": 0}


def test_serve_uses_empty_index_when_files_are_absent(tmp_path: Path) -> None:
    with (
        CliRunner().isolated_filesystem(temp_dir=tmp_path),
        patch("autodj.server.serve") as serve_mock,
    ):
        result = CliRunner().invoke(cli, ["serve", "--no-playback"])
    assert result.exit_code == 0, result.output
    assert serve_mock.call_args.kwargs["sim"].ntotal == 0
```

- [ ] **Step 2: Verify empty serving is red**

Run: `uv run pytest tests/integration/test_empty_serve.py -q`

Expected: FAIL because `SimilarityIndex.empty()` and `/healthz` are absent, `Player.run()` calls `random.choice([])`, and `serve` exits on missing index files.

- [ ] **Step 3: Implement the empty-index lifecycle**

Add to `SimilarityIndex` in `src/autodj/similarity.py`:

```python
@classmethod
def empty(cls) -> SimilarityIndex:
    """Return a valid zero-track index for first-run web serving."""
    from autodj.indexer import FEATURE_DIM

    return cls(faiss_index=faiss.IndexFlatIP(FEATURE_DIM), entries=[])
```

This method runs after the security plan's reload/read-lock task. Let `__post_init__` initialize the
same generation state used by `reload_from_disk` (generation zero for the empty instance); do not
introduce a second empty-mode generation counter or a separate reload path.

Add a serve-specific loader in `src/autodj/cli.py` and use it only from `cmd_serve`; keep play/playlist/stats strict:

```python
def _load_index_for_serve(cfg: AutoDJConfig) -> SimilarityIndex:
    from autodj.similarity import SimilarityIndex as _SI

    try:
        return _SI.from_index_dir(
            cfg.index.active_dir,
            music_dir=cfg.library.music_dir,
            path_remap=cfg.library.path_remap,
        )
    except FileNotFoundError:
        console.print(
            "[yellow]Index is empty; the web UI will stay ready while you run autodj index.[/]"
        )
        return _SI.empty()
```

Add `Player.stop` immediately before `Player.run`, then replace the unconditional random selection at the start of `run`:

```python
def stop(self) -> None:
    """Wake and stop the playback loop, including empty-library waiting."""
    self._state.should_stop = True
    self._skip_event.set()


if seed_entry is None:
    while not self._state.should_stop:
        entries = self._sim.entries_snapshot()
        if entries:
            seed_entry = random.choice(entries)  # nosec B311
            break
        self._skip_event.wait(timeout=0.25)
        self._skip_event.clear()
    if self._state.should_stop:
        return
```

Do not add any direct `self._sim.entries` read here. The security plan has already made
`entries_snapshot()`/`ntotal` the read boundary; the local tuple keeps the emptiness check and
random selection on the same immutable snapshot even if a generation reload happens concurrently.

Add the route beside `/api/version` in `create_app`:

```python
@app.get("/healthz")
async def healthz() -> dict[str, str | int]:
    """Return process readiness without exposing library metadata."""
    return {"status": "ok", "tracks": bridge.sim.ntotal}
```

- [ ] **Step 4: Run empty-mode, player, and server tests**

Run: `uv run pytest tests/integration/test_empty_serve.py tests/unit/test_player.py tests/integration/test_server.py -q`

Expected: PASS; the empty instance reloads through the security-owned expected-generation API,
the player reads one immutable entry snapshot, and existing strict missing-index tests for
play/playlist/stats remain PASS.

- [ ] **Step 5: Commit first-run serving**

```bash
git add src/autodj/similarity.py src/autodj/player.py src/autodj/cli.py src/autodj/server.py tests/integration/test_empty_serve.py
git commit -m "feat: serve safely with an empty library"
```

### Task 4: Reproducible unprivileged container and image smoke test

**Files:**
- Create: `.dockerignore`
- Create: `scripts/container_smoke.sh`
- Create: `tests/unit/test_containerfile.py`
- Modify: `Containerfile:17-71`
- Modify: `compose.yaml:21-45`
- Modify: `renovate.json`
- Modify: `.github/workflows/ci.yml`

- [ ] **Step 1: Write the container smoke contract**

Create executable `scripts/container_smoke.sh`:

```bash
#!/usr/bin/env bash
set -euo pipefail

export AUTODJ_MUSIC_DIR="${RUNNER_TEMP:-/tmp}/autodj-music"
export AUTODJ_INDEX_DIR="${RUNNER_TEMP:-/tmp}/autodj-index"
export AUTODJ_MODEL_DIR="${RUNNER_TEMP:-/tmp}/autodj-models"
mkdir -p "$AUTODJ_MUSIC_DIR" "$AUTODJ_INDEX_DIR" "$AUTODJ_MODEL_DIR"
sudo chown 10001:10001 "$AUTODJ_MUSIC_DIR" "$AUTODJ_INDEX_DIR" "$AUTODJ_MODEL_DIR"
chmod 0755 "$AUTODJ_MUSIC_DIR" "$AUTODJ_INDEX_DIR" "$AUTODJ_MODEL_DIR"

cleanup() { docker compose down --volumes --remove-orphans; }
trap cleanup EXIT

unset AUTODJ_ACCESS_TOKEN AUTODJ_LAN_HOST AUTODJ_LAN_ORIGIN
docker compose config >/dev/null
docker compose --profile lan config >/dev/null
docker compose build --pull
if docker compose --profile lan run --rm --no-deps autodj-lan \
    >"${RUNNER_TEMP:-/tmp}/autodj-lan-negative.log" 2>&1; then
  echo "LAN service unexpectedly started without authentication/origin inputs" >&2
  exit 1
fi
grep -Eiq 'requires|allowed[ _-](host|origin)|invalid.*origin' \
  "${RUNNER_TEMP:-/tmp}/autodj-lan-negative.log"
docker compose up -d
curl --fail --retry 30 --retry-delay 1 --retry-connrefused \
  http://127.0.0.1:8080/healthz | grep '"status":"ok"'

test "$(docker compose exec -T autodj id -u)" = "10001"
test "$(docker compose exec -T autodj id -g)" = "10001"
test "$(docker compose exec -T autodj stat -c '%u:%g:%a' /music)" = "10001:10001:755"
test "$(docker compose exec -T autodj stat -c '%u:%g:%a' /index)" = "10001:10001:755"
test "$(docker compose exec -T autodj stat -c '%u:%g:%a' /models)" = "10001:10001:755"
docker compose exec -T autodj sh -ceu 'touch /index/.write-test; rm /index/.write-test'
docker compose exec -T autodj sh -ceu 'touch /models/.write-test; rm /models/.write-test'

host_ip="$(docker inspect autodj --format '{{(index (index .NetworkSettings.Ports "8080/tcp") 0).HostIp}}')"
test "$host_ip" = "127.0.0.1"
```

Create `tests/unit/test_containerfile.py` so a mutable tag cannot silently return:

```python
import re
from pathlib import Path

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


def test_external_container_images_use_reviewed_manifest_digests() -> None:
    content = (ROOT / "Containerfile").read_text(encoding="utf-8")
    references = set(re.findall(r"^(?:FROM|COPY --from=)(\S+)", content, re.MULTILINE))
    external = {
        ref for ref in references
        if ref.startswith(("python:", "node:", "ghcr.io/"))
    }
    assert external == REVIEWED_IMAGES
    assert all(re.search(r"@sha256:[0-9a-f]{64}$", ref) for ref in external)
```

- [ ] **Step 2: Run the current container smoke test and capture the expected failure**

Run:

```bash
uv run pytest tests/unit/test_containerfile.py -q
bash scripts/container_smoke.sh
```

Expected: the new source-contract test FAILS because the external `FROM`/`COPY --from` references
are mutable tags. The smoke test FAILS during `uv sync --frozen` because `python:3.13-slim`
conflicts with `uv.lock`'s `==3.14.*`; after changing only that locally, it would next fail because
the image lacks config/environment overlay and runs as UID 0. The 0755 ownership assertions also
prevent a world-writable `0777` setup from masking an image ownership defect.

- [ ] **Step 3: Replace the container recipe and build-context policy**

Replace `Containerfile` with:

```dockerfile
# Manifest-list digests reviewed 2026-08-02 via registry Docker-Content-Digest headers.
FROM python:3.14.6-slim-bookworm@sha256:86f975aca15cf04a40b399eebede9aea7c82eae084d1f1a0a6ef6bcaae871a30 AS python-base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_LINK_MODE=copy \
    UV_PROJECT_ENVIRONMENT=/opt/venv

COPY --from=ghcr.io/astral-sh/uv:0.11.26@sha256:3d868e555f8f1dbc324afa005066cd11e1053fc4743b9808ca8025283e65efa5 /uv /usr/local/bin/uv
RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates ffmpeg libsndfile1 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY pyproject.toml uv.lock README.md ./
RUN uv sync --frozen --no-dev --no-install-project

FROM node:24.6.0-bookworm-slim@sha256:9b741b28148b0195d62fa456ed84dd6c953c1f17a3761f3e6e6797a754d9edff AS frontend
WORKDIR /build
COPY package.json package-lock.json pyproject.toml vite.config.js ./
COPY src/autodj/static ./src/autodj/static
RUN npm ci --ignore-scripts --no-audit --no-fund && npm run build

FROM python-base AS runtime
WORKDIR /app
COPY src ./src
COPY --from=frontend /build/src/autodj/static_dist ./src/autodj/static_dist
RUN uv sync --frozen --no-dev \
    && groupadd --gid 10001 autodj \
    && useradd --uid 10001 --gid 10001 --create-home --shell /usr/sbin/nologin autodj \
    && install -d -o 10001 -g 10001 /app/.cache /index /models

ENV HOME=/home/autodj \
    XDG_CACHE_HOME=/app/.cache \
    HF_HOME=/models/huggingface \
    AUTODJ_HOST=0.0.0.0 \
    AUTODJ_PORT=8080 \
    AUTODJ_LIBRARY_MUSIC_DIR=/music \
    AUTODJ_INDEX_DIR=/index \
    AUTODJ_MODEL_DIR=/models

USER 10001:10001
EXPOSE 8080
HEALTHCHECK --interval=10s --timeout=3s --start-period=20s --retries=6 \
    CMD ["/opt/venv/bin/python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/healthz', timeout=2).read()"]
ENTRYPOINT ["/opt/venv/bin/autodj"]
CMD ["serve", "--no-playback"]
```

Create `.dockerignore`:

```gitignore
.git
.github
.venv
.venvs
.uv
node_modules
src/autodj/static_dist
config.toml
config.local.toml*
*.pem
*.key
.env*
music
index
models
liners
profiles
dayparts
tmp
debug
.benchmarks
.hypothesis
.mypy_cache
.pytest_cache
.ruff_cache
.playwright-mcp
htmlcov
.coverage
coverage.json
coverage.xml
*_audit.json
*_audit_report.json
served.html
tests
docs
*.log
*.tmp
```

In `compose.yaml`, parameterize bind sources, keep hardening, publish the default only on loopback, and make LAN startup an explicitly targeted profile with required auth/origin inputs:

```yaml
x-autodj-common: &autodj-common
  build:
    context: .
    dockerfile: Containerfile
  image: autodj:local
  restart: unless-stopped
  volumes:
    - ${AUTODJ_MUSIC_DIR:-./music}:/music:ro
    - ${AUTODJ_INDEX_DIR:-./index}:/index
    - ${AUTODJ_MODEL_DIR:-./models}:/models
  cap_drop: [ALL]
  security_opt: [no-new-privileges:true]

services:
  autodj:
    <<: *autodj-common
    container_name: autodj
    ports:
      - "127.0.0.1:8080:8080"
    # Acknowledge only the container-internal 0.0.0.0 bind; host publication stays loopback-only.
    command: ["serve", "--no-playback", "--insecure-lan"]

  autodj-lan:
    <<: *autodj-common
    profiles: [lan]
    container_name: autodj-lan
    ports:
      - "${AUTODJ_LAN_BIND_ADDRESS:-0.0.0.0}:8080:8080"
    environment:
      # Empty defaults keep inactive-profile interpolation safe. Application validation below
      # rejects the selected wildcard service until real auth/host/origin values are supplied.
      AUTODJ_ACCESS_TOKEN: ${AUTODJ_ACCESS_TOKEN:-}
    command:
      - serve
      - --no-playback
      - --allowed-host
      - "${AUTODJ_LAN_HOST:-}"
      - --allowed-origin
      - "${AUTODJ_LAN_ORIGIN:-}"
```

Document that the default service's `--insecure-lan` acknowledges Uvicorn's necessary
container-internal `0.0.0.0` listener, while Compose publishes that port only on host
`127.0.0.1`; it does **not** authorize a host LAN exposure. Document the exact authenticated
startup as `docker compose --profile lan up autodj-lan`; targeting the service prevents the
default loopback service from starting on the same port.
Do not use Compose's `${VAR:?message}` form inside an inactive profile: Compose interpolates the
whole model before profile selection, which would break default config/build/start. Empty-safe
interpolation above lets the default service resolve, while the security plan's wildcard-bind,
token, allowed-host, and allowed-origin validation makes an explicitly selected incomplete LAN
service exit nonzero. The smoke test proves both halves.

Extend Renovate with Docker digest/version management while retaining existing dependency rules:

```json
{
  "$schema": "https://docs.renovatebot.com/renovate-schema.json",
  "extends": ["config:best-practices", "docker:pinDigests"],
  "packageRules": [
    {"matchDepTypes": ["devDependencies"], "automerge": true},
    {"matchDatasources": ["docker"], "groupName": "container base images"}
  ],
  "lockFileMaintenance": {"enabled": true, "schedule": ["before 5am on Monday"]}
}
```

- [ ] **Step 4: Add and run the container CI gate**

Add this job to `.github/workflows/ci.yml`:

```yaml
  container:
    name: Container build, smoke, and scan
    runs-on: ubuntu-latest
    permissions:
      contents: read
    steps:
      - uses: actions/checkout@v6
      - name: Build and smoke-test unprivileged image
        run: bash scripts/container_smoke.sh
      - name: Generate image SBOM
        uses: anchore/sbom-action@v0
        with:
          image: autodj:local
          format: cyclonedx-json
          output-file: container-sbom.cdx.json
      - name: Scan built image
        uses: aquasecurity/trivy-action@v0.36.0
        with:
          image-ref: autodj:local
          severity: HIGH,CRITICAL
          ignore-unfixed: true
          exit-code: "1"
      - uses: actions/upload-artifact@v7
        if: always()
        with:
          name: container-sbom
          path: container-sbom.cdx.json
```

Run:

```bash
uv run pytest tests/unit/test_containerfile.py -q
bash scripts/container_smoke.sh
```

Expected: PASS; every external build image matches the reviewed manifest-list digest, health
reports zero tracks, UID/GID and all bind-directory owners are 10001, bind-directory modes are
0755, `/index` and `/models` are writable by that owner, default Compose config/build/start resolves
with all LAN variables unset, explicit incomplete LAN startup exits nonzero in application
validation, and published host IP is loopback.

- [ ] **Step 5: Commit container reproducibility**

```bash
git add .dockerignore Containerfile compose.yaml renovate.json scripts/container_smoke.sh tests/unit/test_containerfile.py .github/workflows/ci.yml
git commit -m "build: make container reproducible and unprivileged"
```

### Task 5: Single version source and build metadata

**Files:**
- Create: `src/autodj/version.py`
- Create: `tests/unit/test_version.py`
- Create: `tests/integration/test_version_contract.py`
- Modify: `pyproject.toml:1-5`
- Modify: `uv.lock:139-140`
- Modify: `src/autodj/__init__.py:1-7`
- Modify: `src/autodj/server.py:68-86,333`
- Modify: `package.json:1-6`
- Modify: `vite.config.js:22-80`

- [ ] **Step 1: Write failing version-consistency tests**

Create `tests/unit/test_version.py`:

```python
import json
import tomllib
from pathlib import Path
from unittest.mock import patch

import autodj
from autodj.server import _version_info
from autodj.version import current_version

ROOT = Path(__file__).resolve().parents[2]


def project_version() -> str:
    with (ROOT / "pyproject.toml").open("rb") as fh:
        return str(tomllib.load(fh)["project"]["version"])


def test_python_version_derives_from_project_metadata() -> None:
    expected = project_version()
    assert current_version() == expected
    assert autodj.__version__ == expected


def test_version_endpoint_uses_same_accessor() -> None:
    _version_info.cache_clear()
    with patch("autodj.server.current_version", return_value="9.8.7"):
        assert _version_info()["version"] == "9.8.7"


def test_frontend_has_no_independent_product_version() -> None:
    package = json.loads((ROOT / "package.json").read_text(encoding="utf-8"))
    assert "version" not in package
```

Create `tests/integration/test_version_contract.py`, where the integration-local `bridge` fixture is available:

```python
from autodj.server import create_app
from autodj.version import current_version


def test_api_version_derives_from_project_metadata(bridge) -> None:
    assert create_app(bridge).version == current_version()
```

- [ ] **Step 2: Run version tests and verify drift is detected**

Run: `uv run pytest tests/unit/test_version.py tests/integration/test_version_contract.py -q`

Expected: FAIL because `version.py` is absent, `__version__` is `0.2.0`, FastAPI is `0.1.0`, Python metadata is `0.14.0`, frontend is `0.15.0`, and the latest changelog heading is `0.15.0`.

- [ ] **Step 3: Implement the authoritative accessor and frontend build stamp**

Set `pyproject.toml` project version to `0.15.0`, then run `uv lock` so the local package entry becomes `0.15.0`.

Create `src/autodj/version.py`:

```python
from __future__ import annotations

import importlib.metadata
import tomllib
from functools import cache
from pathlib import Path


@cache
def current_version() -> str:
    """Return installed metadata, falling back to source-tree pyproject metadata."""
    try:
        return importlib.metadata.version("autodj")
    except importlib.metadata.PackageNotFoundError:
        root = Path(__file__).resolve().parents[2]
        with (root / "pyproject.toml").open("rb") as fh:
            return str(tomllib.load(fh)["project"]["version"])
```

Replace `src/autodj/__init__.py`'s literal with:

```python
from autodj.version import current_version

__version__ = current_version()
```

Import `current_version` in `server.py`, replace its metadata try/except with `version = current_version()`, and construct FastAPI as:

```python
app = FastAPI(title="AutoDJ", version=current_version(), lifespan=lifespan)
```

Remove the `version` member from private `package.json`. In `vite.config.js`, import `readFileSync`/`writeFileSync`, derive the version from `pyproject.toml`, and write the bundle stamp in `closeBundle`:

```javascript
const pyproject = readFileSync(resolve(here, "pyproject.toml"), "utf8");
const versionMatch = pyproject.match(/^version\s*=\s*"([^"]+)"/m);
if (!versionMatch) throw new Error("project.version missing from pyproject.toml");
const PRODUCT_VERSION = versionMatch[1];
```

```javascript
writeFileSync(
  resolve(OUT, "build-info.json"),
  JSON.stringify({ version: PRODUCT_VERSION }, null, 2) + "\n",
);
```

- [ ] **Step 4: Verify Python, lock, and frontend stamps**

Run: `uv run pytest tests/unit/test_version.py tests/integration/test_version_contract.py tests/integration/test_server_recent.py -q && uv lock --check && npm run build && node -e "const p=require('./src/autodj/static_dist/build-info.json'); if(p.version!=='0.15.0') process.exit(1)"`

Expected: PASS; generated `build-info.json` reports `0.15.0` and remains ignored with `static_dist`.

- [ ] **Step 5: Commit version unification**

```bash
git add pyproject.toml uv.lock src/autodj/version.py src/autodj/__init__.py src/autodj/server.py package.json vite.config.js tests/unit/test_version.py tests/integration/test_version_contract.py
git commit -m "fix: derive product version from pyproject"
```

### Task 6: Tracked Node/Python locks, patched dependencies, and frontend CI

**Files:**
- Modify: `.gitignore:51-53`
- Modify: `package.json:7-29`
- Add: `package-lock.json`
- Modify: `uv.lock`
- Modify: `.pre-commit-config.yaml:139-148`
- Modify: `eslint.config.js:87-103`
- Modify: `.github/workflows/ci.yml`

- [ ] **Step 1: Prove the current lock and advisory contract is red**

Run before changing dependency files:

```bash
test "$(git ls-files package-lock.json)" = "package-lock.json"
npm audit --audit-level=high
```

Expected: FAIL because the existing `package-lock.json` is ignored/untracked and the locked Vitest 2.1.9 tree is affected by a HIGH advisory.

- [ ] **Step 2: Tighten package scripts and declare patched supported versions**

Replace the scripts/dependencies portion of `package.json` with:

```json
"scripts": {
  "build": "vite build",
  "dev": "vite",
  "preview": "vite preview",
  "test": "vitest run",
  "test:watch": "vitest",
  "lint": "eslint --max-warnings 0 src/autodj/static tests/jsmodules tests/playwright vite.config.js eslint.config.js",
  "deadcode": "knip",
  "audit:transitions": "node tests/playwright/transition_audit.mjs",
  "audit:health": "node tests/playwright/health_audit.mjs",
  "audit:hotkeys": "node tests/playwright/hotkey_audit.mjs",
  "audit:regression": "node tests/playwright/regression_audit.mjs",
  "audit:ci": "npm run audit:health && npm run audit:hotkeys && npm run audit:regression && npm run audit:transitions"
},
"devDependencies": {
  "@eslint/js": "10.0.1",
  "@playwright/test": "1.62.1",
  "eslint": "10.8.0",
  "happy-dom": "20.11.1",
  "jsdom": "30.0.1",
  "knip": "6.31.0",
  "vite": "8.2.0",
  "vitest": "4.1.10"
}
```

Remove `package-lock.json` from `.gitignore`. Run `npm install --package-lock-only --ignore-scripts` to regenerate the lock from these exact direct versions.

Run `uv lock --upgrade` to refresh all Python transitive dependencies within the reviewed `pyproject.toml` constraints, then retain this narrowly scoped CI suppression with evidence and an expiry:

```yaml
      - name: pip-audit — dependency CVE scan
        # PYSEC-2022-42969 affects Subversion metadata parsing in dev-only py,
        # reached through xenon -> radon; AutoDJ does not accept SVN metadata.
        # Source: https://osv.dev/vulnerability/PYSEC-2022-42969
        # Reviewed: 2026-08-02. Expiry/re-review: 2026-11-02.
        run: uv run pip-audit --ignore-vuln PYSEC-2022-42969
```

- [ ] **Step 3: Run clean installs and vulnerability gates before CI wiring**

Run: `git clean -ndx node_modules && uv lock --check && uv sync --frozen --all-extras && uv run pip-audit --ignore-vuln PYSEC-2022-42969 && npm ci --ignore-scripts && npm run lint && npm test && npm run build && npm run deadcode && npm audit --audit-level=high`

Expected: PASS after regeneration with a frozen Python environment, no unsuppressed Python advisory, zero ESLint warnings, and no HIGH/CRITICAL npm audit result. Review `git clean -ndx` output only; do not run destructive clean in a shared worktree.

- [ ] **Step 4: Make pre-commit use the committed installation**

Replace the ESLint local hook with:

```yaml
      - id: eslint
        name: eslint
        entry: npm run lint
        language: system
        pass_filenames: false
        files: \.(js|mjs)$
```

Change production-source warning rules in `eslint.config.js` from `warn` to `error` for `no-unused-vars`, `prefer-const`, and `eqeqeq`; keep test-only underscore exemptions.

- [ ] **Step 5: Add clean lock-verification jobs**

Add to `.github/workflows/ci.yml`:

```yaml
  frontend:
    name: Frontend lint, test, build, audit
    runs-on: ubuntu-latest
    permissions:
      contents: read
    steps:
      - uses: actions/checkout@v6
      - uses: actions/setup-node@v6
        with:
          node-version: "24.6.0"
          cache: npm
      - run: npm ci --ignore-scripts
      - run: npm run lint
      - run: npm test
      - run: npm run build
      - run: npm run deadcode
      - run: npm audit --audit-level=high
```

Add `uv lock --check` immediately after Python setup in the existing `quality` job. Keep its existing frozen sync and replace the pip-audit comment with the evidence/expiry block from Step 2.

Run: `npm ci --ignore-scripts && npm run lint && npm test && npm run build && npm run deadcode && npm audit --audit-level=high`

Expected: PASS from a lock-faithful install.

- [ ] **Step 6: Commit dependency reproducibility**

```bash
git add .gitignore package.json package-lock.json uv.lock .pre-commit-config.yaml eslint.config.js .github/workflows/ci.yml
git commit -m "build: gate dependencies from committed locks"
```

### Task 7: Assertion-based Playwright audits

**Files:**
- Create: `tests/playwright/audit_helpers.mjs`
- Modify: `tests/playwright/health_audit.mjs:5-88`
- Modify: `tests/playwright/hotkey_audit.mjs:14-212`
- Modify: `tests/playwright/regression_audit.mjs:14-148`
- Modify: `tests/playwright/transition_audit.mjs:8-106`
- Modify: `.github/workflows/ci.yml`
- Create: `.github/workflows/browser-audit.yml`

- [ ] **Step 1: Write the shared assertion/engine runner**

Create `tests/playwright/audit_helpers.mjs`:

```javascript
import { chromium, firefox, webkit } from "playwright";
import { writeFileSync } from "node:fs";

const launchers = { chromium, firefox, webkit };

export function selectedBrowsers() {
  const names = (process.env.AUTODJ_BROWSERS || "chromium,firefox,webkit")
    .split(",").map((value) => value.trim()).filter(Boolean);
  for (const name of names) {
    if (!launchers[name]) throw new Error(`Unsupported browser: ${name}`);
  }
  return names.map((name) => [name, launchers[name]]);
}

export function check(condition, message) {
  if (!condition) throw new Error(message);
}

export function equal(actual, expected, message) {
  if (actual !== expected) {
    throw new Error(`${message}: expected ${JSON.stringify(expected)}, got ${JSON.stringify(actual)}`);
  }
}

export async function runAudit({ audit, validate, report }) {
  const results = {};
  const failures = [];
  for (const [name, launcher] of selectedBrowsers()) {
    try {
      const result = await audit(name, launcher);
      validate(name, result);
      results[name] = result;
    } catch (error) {
      results[name] = { error: String(error) };
      failures.push(`${name}: ${error}`);
    }
  }
  writeFileSync(report, JSON.stringify(results, null, 2) + "\n");
  if (failures.length) {
    console.error(failures.join("\n"));
    process.exitCode = 1;
  }
}
```

- [ ] **Step 2: Run an existing audit against a deliberately unreachable URL**

Run: `AUTODJ_URL=http://127.0.0.1:1 AUTODJ_BROWSERS=chromium node tests/playwright/health_audit.mjs`

Expected before conversion: exit 0 despite a report containing an error. This proves reports currently are not gates.

- [ ] **Step 3: Export each audit and add explicit success predicates**

Replace direct Playwright launcher imports with `check`, `equal`, and `runAudit`. Export each existing `audit` function and replace each file's bottom loop with the matching complete block.

For `health_audit.mjs`:

```javascript
await runAudit({
  audit,
  report: "health_audit.json",
  validate(name, result) {
    equal(result.pageerrors.length, 0, `${name} page errors`);
    equal(result.requestfailed.length, 0, `${name} failed requests`);
    equal(result.status_4xx_5xx.length, 0, `${name} HTTP errors`);
    equal(result.unhandled.length, 0, `${name} interaction errors`);
    check(result.probe && !result.probe.error, `${name} page probe failed`);
    check(result.probe.title.includes("AutoDJ"), `${name} title missing AutoDJ`);
    check(result.probe.hasAudio, `${name} audio element missing`);
  },
});
```

For `hotkey_audit.mjs`:

```javascript
await runAudit({
  audit,
  report: "hotkey_audit_report.json",
  validate(name, result) {
    for (const [key, value] of Object.entries(result.source)) check(value, `${name} source.${key}`);
    equal(result.dom.modalExists, true, `${name} modal exists`);
    equal(result.dom.legacyDetailsGone, true, `${name} legacy details gone`);
    equal(result.dom.btnShortcutsExists, true, `${name} shortcut trigger exists`);
    equal(result.behaviour.shuffleClicksLatched, 1, `${name} press latch`);
    equal(result.behaviour.shuffleClicksAfterRelease, 2, `${name} release latch`);
    equal(result.behaviour.muteClicksFromSliderFocus, 1, `${name} slider hotkey`);
    equal(result.behaviour.pauseClicksFromSearchInput, 0, `${name} input suppression`);
    equal(result.behaviour.effectDurationUnified, true, `${name} transition duration`);
    equal(result.errors.length, 0, `${name} browser errors`);
  },
});
```

For `regression_audit.mjs`:

```javascript
await runAudit({
  audit,
  report: "regression_audit_report.json",
  validate(name, result) {
    equal(result.dom.lyricsCardInNow, true, `${name} lyrics location`);
    equal(result.dom.lyricsCardInSettings, false, `${name} lyrics absent from settings`);
    equal(result.dom.cueListSummaryGone, true, `${name} legacy cue list`);
    equal(result.hotkeys.volUnchangedOnSettingsTab, true, `${name} settings hotkey gate`);
    equal(result.hotkeys.shortcutsDialogOpensFromAnyTab, true, `${name} help shortcut`);
    equal(result.hotkeys.volChangedOnNowTab, true, `${name} now-playing hotkey`);
    check(Boolean(result.liveRegion.announced), `${name} live region did not announce`);
    equal(result.liveRegion.cleared, true, `${name} live region did not clear`);
    equal(result.errors.length, 0, `${name} browser errors`);
  },
});
```

For `transition_audit.mjs`:

```javascript
await runAudit({
  audit,
  report: "transition_audit.json",
  validate(name, result) {
    check(result.workletReady, `${name} audio element/worklet readiness failed`);
    equal(result.probe.errors.length, 0, `${name} probe errors`);
    for (const [worklet, status] of Object.entries(result.probe.worklets)) {
      equal(status, "ok", `${name} ${worklet} worklet`);
    }
    for (const [effect, response] of Object.entries(result.transitions)) {
      equal(response.status, 200, `${name} ${effect} transition response`);
    }
    equal(result.logs.filter((entry) => entry.type === "pageerror").length, 0, `${name} page errors`);
  },
});
```

- [ ] **Step 4: Add the Chromium pull-request gate and scheduled full-engine gate**

Add a `browser` job to `ci.yml` that starts empty-library AutoDJ, installs Chromium, runs all assertions, and uploads reports only as diagnostics:

```yaml
  browser:
    name: Browser assertions (Chromium)
    needs: [test, frontend]
    runs-on: ubuntu-latest
    permissions:
      contents: read
    steps:
      - uses: actions/checkout@v6
      - uses: astral-sh/setup-uv@v8.1.0
      - uses: actions/setup-python@v6
        with: {python-version: "3.14"}
      - uses: actions/setup-node@v6
        with: {node-version: "24.6.0", cache: npm}
      - run: uv sync --frozen --all-extras
      - run: npm ci --ignore-scripts
      - run: npx playwright install --with-deps chromium
      - name: Start empty-library server
        run: |
          mkdir -p "$RUNNER_TEMP/music" "$RUNNER_TEMP/index" "$RUNNER_TEMP/models"
          AUTODJ_LIBRARY_MUSIC_DIR="$RUNNER_TEMP/music" \
          AUTODJ_INDEX_DIR="$RUNNER_TEMP/index" \
          AUTODJ_MODEL_DIR="$RUNNER_TEMP/models" \
          uv run autodj serve --no-playback >"$RUNNER_TEMP/autodj.log" 2>&1 &
          curl --fail --retry 30 --retry-delay 1 --retry-connrefused http://127.0.0.1:8080/healthz
      - run: AUTODJ_BROWSERS=chromium npm run audit:ci
      - uses: actions/upload-artifact@v7
        if: always()
        with:
          name: playwright-audit-reports
          path: "*_audit*.json"
```

Create `.github/workflows/browser-audit.yml` for the stable full-engine gate:

```yaml
name: Full browser audit
on:
  schedule:
    - cron: "17 6 * * 1"
  workflow_dispatch:
permissions:
  contents: read

jobs:
  audit:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v6
      - uses: astral-sh/setup-uv@v8.1.0
      - uses: actions/setup-python@v6
        with: {python-version: "3.14"}
      - uses: actions/setup-node@v6
        with: {node-version: "24.6.0", cache: npm}
      - run: uv sync --frozen --all-extras
      - run: npm ci --ignore-scripts
      - run: npx playwright install --with-deps firefox webkit
      - name: Start empty-library server
        run: |
          mkdir -p "$RUNNER_TEMP/music" "$RUNNER_TEMP/index" "$RUNNER_TEMP/models"
          AUTODJ_LIBRARY_MUSIC_DIR="$RUNNER_TEMP/music" \
          AUTODJ_INDEX_DIR="$RUNNER_TEMP/index" \
          AUTODJ_MODEL_DIR="$RUNNER_TEMP/models" \
          uv run autodj serve --no-playback >"$RUNNER_TEMP/autodj.log" 2>&1 &
          curl --fail --retry 30 --retry-delay 1 --retry-connrefused http://127.0.0.1:8080/healthz
      - run: AUTODJ_BROWSERS=firefox,webkit npm run audit:ci
      - uses: actions/upload-artifact@v7
        if: always()
        with:
          name: full-browser-audit-reports
          path: "*_audit*.json"
```

The audit command, not report upload, determines job success.

Run against a local server: `AUTODJ_BROWSERS=chromium npm run audit:ci`

Expected: PASS. Re-run the unreachable URL command; expected exit is now nonzero.

- [ ] **Step 5: Commit browser assertions**

```bash
git add tests/playwright package.json .github/workflows/ci.yml .github/workflows/browser-audit.yml
git commit -m "test: make browser audits assertion based"
```

### Task 8: Read-only doctor command

**Files:**
- Create: `src/autodj/doctor.py`
- Create: `tests/unit/test_doctor.py`
- Modify: `src/autodj/cli.py:1843-1942`

- [ ] **Step 1: Write failing doctor result/redaction/coherence tests**

Create `tests/unit/test_doctor.py`:

```python
import json
import sqlite3
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

import numpy as np
import pytest
from click.testing import CliRunner

from autodj.cli import cli
from autodj.config import load_config
from autodj.doctor import (
    CheckStatus,
    DoctorCheck,
    DoctorReport,
    _bundle_check,
    _dependency_check,
    _python_check,
    render_text,
    run_doctor,
)
from autodj.indexer import FEATURE_DIM, IndexEntry, save_index
from autodj.model import ModelCacheStatus


def _entry(path: str) -> IndexEntry:
    return IndexEntry(
        path=path, title="Song", artist="Artist", album="", genre="", bpm=0.0,
        year=0, length=1.0, energy=0.0, key=-1, mode=-1,
        tempo_confidence=0.0,
    )


def _write_dj_meta(path: Path) -> None:
    with closing(sqlite3.connect(path)) as conn:
        conn.execute(
            "CREATE TABLE dj_meta (path TEXT PRIMARY KEY, intro_end_s REAL, "
            "outro_start_s REAL, analysed INTEGER, beats TEXT, cues TEXT)"
        )
        conn.commit()


def test_healthy_doctor_is_read_only_and_redacts_tokens(tmp_path: Path) -> None:
    music = tmp_path / "music"
    music.mkdir()
    server_token = "test-token-0123456789abcdef0123456789"
    huggingface_token = "hf-token-0123456789abcdef0123456789"
    cfg = load_config(
        None,
        environ={
            "AUTODJ_LIBRARY_MUSIC_DIR": str(music),
            "AUTODJ_INDEX_DIR": str(tmp_path / "index"),
            "AUTODJ_MODEL_DIR": str(tmp_path / "models"),
            "AUTODJ_ACCESS_TOKEN": server_token,
            "AUTODJ_HUGGINGFACE_TOKEN": huggingface_token,
        },
    )
    cfg.index.active_dir.mkdir(parents=True)
    save_index(
        [_entry(str(music / "song.flac"))],
        np.ones((1, FEATURE_DIM), dtype=np.float32) / np.sqrt(FEATURE_DIM),
        cfg.index.active_dir,
    )
    _write_dj_meta(cfg.index.active_dir / "dj_meta.db")
    before = {p: p.stat().st_mtime_ns for p in cfg.index.active_dir.iterdir()}
    with (
        patch("autodj.doctor.inspect_model_cache", return_value=ModelCacheStatus(tmp_path / "models", True, "complete")),
        patch("autodj.doctor.shutil.which", return_value="/usr/bin/ffmpeg"),
    ):
        report = run_doctor(cfg)
    assert report.exit_code == 0
    assert server_token not in report.to_json()
    assert huggingface_token not in report.to_json()
    assert '"host": "127.0.0.1"' in report.to_json()
    assert '"access_token": "<redacted>"' in report.to_json()
    assert {p: p.stat().st_mtime_ns for p in cfg.index.active_dir.iterdir()} == before


def test_corrupt_index_is_required_failure(tmp_path: Path) -> None:
    cfg = load_config(None, environ={"AUTODJ_INDEX_DIR": str(tmp_path / "index")})
    cfg.index.active_dir.mkdir(parents=True)
    (cfg.index.active_dir / "vectors.index").write_bytes(b"broken")
    (cfg.index.active_dir / "tracks.db").write_bytes(b"broken")
    report = run_doctor(cfg)
    index_check = next(check for check in report.checks if check.name == "index-coherence")
    assert index_check.status is CheckStatus.FAIL
    assert report.exit_code == 1


def test_partial_index_is_required_failure(tmp_path: Path) -> None:
    cfg = load_config(None, environ={"AUTODJ_INDEX_DIR": str(tmp_path / "index")})
    cfg.index.active_dir.mkdir(parents=True)
    (cfg.index.active_dir / "vectors.index").write_bytes(b"vectors")
    report = run_doctor(cfg)
    index_check = next(check for check in report.checks if check.name == "index-coherence")
    assert index_check.status is CheckStatus.FAIL


def test_corrupt_dj_meta_is_required_read_only_failure(tmp_path: Path) -> None:
    cfg = load_config(None, environ={"AUTODJ_INDEX_DIR": str(tmp_path / "index")})
    cfg.index.active_dir.mkdir(parents=True)
    metadata = cfg.index.active_dir / "dj_meta.db"
    metadata.write_bytes(b"not sqlite")
    before = metadata.stat().st_mtime_ns
    report = run_doctor(cfg)
    check = next(check for check in report.checks if check.name == "dj-meta-db")
    assert check.status is CheckStatus.FAIL
    assert "integrity/schema" in check.summary
    assert metadata.stat().st_mtime_ns == before


def test_missing_ffmpeg_warns_with_fallback_detail() -> None:
    with patch("autodj.doctor.shutil.which", return_value=None):
        check = _dependency_check()
    assert check.status is CheckStatus.WARN
    assert "ALAC" in check.detail


def test_invalid_bundle_stamp_is_required_failure(tmp_path: Path) -> None:
    bundle_root = tmp_path / "autodj"
    (bundle_root / "static_dist").mkdir(parents=True)
    (bundle_root / "static_dist" / "build-info.json").write_text("not-json", encoding="utf-8")
    assert _bundle_check(bundle_root).status is CheckStatus.FAIL


def test_unsafe_bind_without_token_is_required_failure(tmp_path: Path) -> None:
    cfg = load_config(None, environ={"AUTODJ_HOST": "0.0.0.0"})
    cfg.server.access_token = None
    cfg.server.insecure_lan = False
    report = run_doctor(cfg)
    network = next(check for check in report.checks if check.name == "network-safety")
    assert network.status is CheckStatus.FAIL


@pytest.mark.parametrize("host", ["127.0.0.2", "::1"])
def test_all_loopback_forms_are_safe_without_token(host: str) -> None:
    cfg = load_config(None, environ={"AUTODJ_HOST": host})
    cfg.server.access_token = None
    report = run_doctor(cfg)
    network = next(check for check in report.checks if check.name == "network-safety")
    assert network.status is CheckStatus.PASS


def test_unsupported_python_is_required_failure() -> None:
    check = _python_check((3, 13))
    assert check.status is CheckStatus.FAIL
    assert "3.14" in check.detail


def test_python_315_is_outside_exact_project_constraint() -> None:
    check = _python_check((3, 15))
    assert check.status is CheckStatus.FAIL
    assert "==3.14.*" in check.detail


def test_text_output_includes_actionable_detail() -> None:
    report = DoctorReport((
        DoctorCheck(
            "index-coherence",
            CheckStatus.FAIL,
            "unreadable published generation",
            "run `autodj index` to republish the index",
        ),
    ))
    output = render_text(report)
    assert "unreadable published generation" in output
    assert "run `autodj index`" in output


def test_cli_json_output_and_nonzero_failure(tmp_path: Path) -> None:
    result = CliRunner().invoke(
        cli,
        ["doctor", "--json"],
        env={"AUTODJ_INDEX_DIR": str(tmp_path / "missing")},
    )
    assert result.exit_code == 1
    payload = json.loads(result.output)
    assert payload["exit_code"] == 1
```

- [ ] **Step 2: Verify doctor tests fail before implementation**

Run: `uv run pytest tests/unit/test_doctor.py -q`

Expected: FAIL because `autodj.doctor` and the CLI command do not exist.

- [ ] **Step 3: Implement typed checks and CLI rendering**

Create `src/autodj/doctor.py`:

```python
from __future__ import annotations

import importlib.util
import json
import os
import shutil
import sqlite3
import sys
from contextlib import closing
from dataclasses import asdict, dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any

from autodj.config import AutoDJConfig, is_loopback_bind
from autodj.index_manifest import IndexConsistencyError, read_manifest
from autodj.indexer import load_index
from autodj.model import inspect_model_cache
from autodj.version import current_version


class CheckStatus(StrEnum):
    """Severity and process-exit meaning for one diagnostic."""

    PASS = "pass"
    WARN = "warn"
    FAIL = "fail"


@dataclass(frozen=True)
class DoctorCheck:
    """One named, read-only diagnostic result."""

    name: str
    status: CheckStatus
    summary: str
    detail: str | dict[str, Any] = ""


@dataclass(frozen=True)
class DoctorReport:
    """Aggregate diagnostics with stable text and JSON serialization."""

    checks: tuple[DoctorCheck, ...]

    @property
    def exit_code(self) -> int:
        """Return one when any required check failed, otherwise zero."""

        return int(any(check.status is CheckStatus.FAIL for check in self.checks))

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serializable report."""

        return {"exit_code": self.exit_code, "checks": [asdict(check) for check in self.checks]}

    def to_json(self) -> str:
        """Serialize the report without exposing configuration secrets."""

        return json.dumps(self.to_dict(), indent=2, default=str)


def _path_check(name: str, path: Path, *, writable: bool) -> DoctorCheck:
    if path.exists():
        readable = os.access(path, os.R_OK)
        write_ok = not writable or os.access(path, os.W_OK)
        if readable and write_ok:
            return DoctorCheck(name, CheckStatus.PASS, str(path))
        return DoctorCheck(name, CheckStatus.FAIL, str(path), "required permissions missing")
    parent = path.parent if path.parent != path else Path.cwd()
    if writable and parent.exists() and os.access(parent, os.W_OK):
        return DoctorCheck(name, CheckStatus.WARN, str(path), "missing; writable parent can create it")
    return DoctorCheck(name, CheckStatus.FAIL, str(path), "path does not exist")


_TRACKS_COLUMNS = frozenset({
    "vec_row", "path", "title", "artist", "album", "genre", "bpm", "year",
    "length", "energy", "key", "mode", "tempo_confidence", "embedded_at",
})
_DJ_META_COLUMNS = frozenset({
    "path", "intro_end_s", "outro_start_s", "analysed", "beats", "cues",
})


def _database_check(
    name: str,
    path: Path,
    *,
    table: str,
    required_columns: frozenset[str],
) -> DoctorCheck:
    if not path.exists():
        return DoctorCheck(name, CheckStatus.WARN, "database absent", str(path))
    try:
        uri = f"{path.resolve().as_uri()}?mode=ro"
        with closing(sqlite3.connect(uri, uri=True)) as conn:
            conn.execute("PRAGMA query_only=ON")
            integrity = [str(row[0]) for row in conn.execute("PRAGMA integrity_check")]
            columns = {
                str(row[1]) for row in conn.execute(f'PRAGMA table_info("{table}")')
            }
    except (OSError, sqlite3.DatabaseError) as exc:
        return DoctorCheck(
            name, CheckStatus.FAIL, "integrity/schema check failed", str(exc)
        )
    missing = sorted(required_columns - columns)
    if integrity != ["ok"] or missing:
        return DoctorCheck(
            name,
            CheckStatus.FAIL,
            "integrity/schema check failed",
            f"integrity={integrity}; missing_columns={missing}",
        )
    return DoctorCheck(name, CheckStatus.PASS, "integrity and schema valid", str(path))


def _tracks_database_check(cfg: AutoDJConfig) -> DoctorCheck:
    try:
        manifest = read_manifest(cfg.index.active_dir)
    except IndexConsistencyError as exc:
        return DoctorCheck(
            "tracks-db", CheckStatus.FAIL, "integrity/schema check failed", str(exc)
        )
    path = cfg.index.active_dir / (
        manifest.tracks_file if manifest is not None else "tracks.db"
    )
    return _database_check(
        "tracks-db", path, table="tracks", required_columns=_TRACKS_COLUMNS
    )


def _dj_meta_database_check(cfg: AutoDJConfig) -> DoctorCheck:
    return _database_check(
        "dj-meta-db",
        cfg.index.active_dir / "dj_meta.db",
        table="dj_meta",
        required_columns=_DJ_META_COLUMNS,
    )


def _index_check(cfg: AutoDJConfig) -> DoctorCheck:
    active = cfg.index.active_dir
    try:
        manifest = read_manifest(active)
        entries, faiss_index = load_index(
            active,
            music_dir=cfg.library.music_dir,
            path_remap=cfg.library.path_remap,
            expected_generation=None if manifest is None else manifest.generation,
        )
    except FileNotFoundError as exc:
        remnants = (
            manifest is not None
            or any(active.glob("vectors*.index"))
            or any(active.glob("tracks*.db"))
        )
        if not remnants:
            return DoctorCheck(
                "index-coherence", CheckStatus.WARN, "empty index",
                f"{active}; run `autodj index` before playback",
            )
        return DoctorCheck(
            "index-coherence", CheckStatus.FAIL, "partial published index",
            f"{exc}; run `autodj index` to republish one complete generation",
        )
    except (IndexConsistencyError, OSError, ValueError, RuntimeError) as exc:
        return DoctorCheck(
            "index-coherence", CheckStatus.FAIL, "unreadable published generation",
            f"{exc}; run `autodj index` to republish the index",
        )
    count = len(entries)
    if int(faiss_index.ntotal) != count:
        return DoctorCheck(
            "index-coherence", CheckStatus.FAIL, "index count mismatch",
            f"rows={count}, vectors={faiss_index.ntotal}; run `autodj index`",
        )
    if manifest is None:
        return DoctorCheck(
            "index-coherence", CheckStatus.WARN, f"{count} legacy vectors and rows",
            "no generation manifest; run `autodj index` to publish one",
        )
    return DoctorCheck(
        "index-coherence", CheckStatus.PASS,
        f"generation {manifest.generation}: {count} vectors and rows",
    )


def _dependency_check() -> DoctorCheck:
    missing = [
        name for name in ("soundfile", "sounddevice")
        if importlib.util.find_spec(name) is None
    ]
    ffmpeg = shutil.which("ffmpeg")
    if ffmpeg is None:
        return DoctorCheck(
            "dependencies",
            CheckStatus.WARN,
            "ffmpeg missing",
            "optional ALAC browser transcoding is unavailable; raw ALAC fallback remains enabled",
        )
    detail = "optional audio modules missing: " + ", ".join(missing) if missing else "audio modules present"
    status = CheckStatus.WARN if missing else CheckStatus.PASS
    return DoctorCheck("dependencies", status, ffmpeg, detail)


def _configuration_check(cfg: AutoDJConfig) -> DoctorCheck:
    effective = {
        "sources": list(cfg.config_sources),
        "host": cfg.server.host,
        "port": cfg.server.port,
        "music_dir": str(cfg.library.music_dir),
        "index_dir": str(cfg.index.index_dir),
        "model_dir": str(cfg.index.model_dir),
        "access_token": "<redacted>" if cfg.server.access_token else None,
        "huggingface_token": "<redacted>" if cfg.huggingface.token else None,
    }
    return DoctorCheck(
        "configuration",
        CheckStatus.PASS,
        " < ".join(cfg.config_sources),
        effective,
    )


def _python_check(version: tuple[int, int] | None = None) -> DoctorCheck:
    active = version or (sys.version_info.major, sys.version_info.minor)
    status = CheckStatus.PASS if active == (3, 14) else CheckStatus.FAIL
    return DoctorCheck(
        "python",
        status,
        f"{active[0]}.{active[1]}",
        "AutoDJ requires Python ==3.14.*; use the project-managed interpreter",
    )


def _network_check(cfg: AutoDJConfig) -> DoctorCheck:
    if is_loopback_bind(cfg.server.host):
        return DoctorCheck("network-safety", CheckStatus.PASS, "loopback-only")
    if cfg.server.access_token:
        return DoctorCheck("network-safety", CheckStatus.PASS, "authenticated non-loopback bind")
    if cfg.server.insecure_lan:
        return DoctorCheck("network-safety", CheckStatus.WARN, "explicit insecure LAN acknowledgement")
    return DoctorCheck("network-safety", CheckStatus.FAIL, "non-loopback bind lacks authentication")


def _bundle_check(root: Path | None = None) -> DoctorCheck:
    root = root or Path(__file__).parent
    stamp = root / "static_dist" / "build-info.json"
    if not stamp.exists():
        return DoctorCheck("frontend-bundle", CheckStatus.WARN, "source assets in use")
    try:
        version = str(json.loads(stamp.read_text(encoding="utf-8"))["version"])
    except (OSError, KeyError, ValueError, TypeError) as exc:
        return DoctorCheck("frontend-bundle", CheckStatus.FAIL, "invalid build-info.json", str(exc))
    status = CheckStatus.PASS if version == current_version() else CheckStatus.FAIL
    return DoctorCheck("frontend-bundle", status, f"bundle {version}; runtime {current_version()}")


def run_doctor(cfg: AutoDJConfig) -> DoctorReport:
    """Run diagnostics without mutating configuration, indexes, or caches."""

    cache = inspect_model_cache(cfg.model, cfg.index)
    cache_status = CheckStatus.PASS if cache.complete else CheckStatus.WARN
    checks = (
        _configuration_check(cfg),
        _python_check(),
        _path_check("music-path", cfg.library.music_dir, writable=False),
        _path_check("index-path", cfg.index.index_dir, writable=True),
        _path_check("model-path", cfg.index.model_dir, writable=True),
        _index_check(cfg),
        _tracks_database_check(cfg),
        _dj_meta_database_check(cfg),
        _dependency_check(),
        DoctorCheck("model-cache", cache_status, str(cache.path), cache.reason),
        _network_check(cfg),
        _bundle_check(),
    )
    return DoctorReport(checks)


def render_text(report: DoctorReport) -> str:
    """Render a concise screen-reader-friendly diagnostic list."""

    lines = []
    for check in report.checks:
        line = f"[{check.status.upper()}] {check.name}: {check.summary}"
        if check.detail:
            detail = (
                json.dumps(check.detail, sort_keys=True, default=str)
                if isinstance(check.detail, dict)
                else check.detail
            )
            line += f" — {detail}"
        lines.append(line)
    return "\n".join(lines)
```

The index check intentionally reads the security-owned manifest first and calls `load_index` with
that exact expected generation. It does not infer the active snapshot from fixed canonical
filenames; the globs above only distinguish an empty directory from a partial legacy layout after
the manifest API reported no publication. The generic configuration currently has no capability that makes ffmpeg mandatory, and
the server retains raw ALAC fallback, so a missing executable is a warning; promote it to FAIL only
if a future explicit configured capability truly requires transcoding.

The two database checks open SQLite with `mode=ro`, enable `query_only`, run full
`PRAGMA integrity_check`, and validate required columns without creating or migrating anything.
For `tracks-db`, select `manifest.tracks_file` rather than assuming the working `tracks.db` is the
published generation. Missing re-derivable databases warn; an existing corrupt or wrong-schema
database fails with actionable detail.

Add to `src/autodj/cli.py`:

```python
@cli.command("doctor")
@click.option("--json", "as_json", is_flag=True, help="Emit machine-readable JSON.")
@click.pass_context
def cmd_doctor(ctx: click.Context, as_json: bool) -> None:
    """Check configuration, storage, dependencies, model, network, and bundle health."""
    from autodj.doctor import render_text, run_doctor

    cfg = _load_cfg_or_exit(ctx.obj["config_path"])
    report = run_doctor(cfg)
    click.echo(report.to_json() if as_json else render_text(report))
    if report.exit_code:
        raise click.exceptions.Exit(report.exit_code)
```

- [ ] **Step 4: Run doctor and CLI tests**

Run: `uv run pytest tests/unit/test_doctor.py tests/unit/test_cli.py tests/smoke/test_cli_smoke.py -q`

Expected: PASS; failure cases exit 1, Python 3.15 is rejected by the exact `==3.14.*` project
constraint, alternate IPv4/IPv6 loopback binds use the security validator, missing optional ffmpeg
warns with the ALAC fallback explanation, text output includes actionable detail, secrets never
appear, corrupt `dj_meta.db` fails integrity/schema validation, the manifest-selected tracks DB is
checked, and the mtime snapshot proves checks are read-only.

- [ ] **Step 5: Commit doctor diagnostics**

```bash
git add src/autodj/doctor.py src/autodj/cli.py tests/unit/test_doctor.py
git commit -m "feat: add read-only doctor command"
```

### Task 9: Versioned backup and restore commands

**Files:**
- Create: `src/autodj/backup.py`
- Create: `tests/unit/test_backup.py`
- Modify: `src/autodj/cli.py`

- [ ] **Step 1: Write failing coherence, durable-publication, and all-or-nothing restore tests**

Create `tests/unit/test_backup.py`:

```python
import hashlib
import json
import sqlite3
import stat
import zipfile
from contextlib import closing
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from zipfile import ZipInfo

import numpy as np
import pytest
from click.testing import CliRunner

from autodj.backup import (
    BackupError,
    _required_free_space,
    _validate_member_info,
    create_backup,
    restore_backup,
)
from autodj.cli import cli
from autodj.config import load_config
from autodj.index_manifest import IndexConsistencyError, IndexManifest, read_manifest
from autodj.indexer import FEATURE_DIM, IndexEntry, save_index
from autodj.version import current_version


def _sqlite(path: Path, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(path)) as conn:
        conn.execute("CREATE TABLE data(value TEXT)")
        conn.execute("INSERT INTO data VALUES (?)", (value,))
        conn.commit()


def _config(tmp_path: Path):
    cfg = load_config(None, environ={"AUTODJ_INDEX_DIR": str(tmp_path / "index")})
    cfg.index.active_dir.mkdir(parents=True)
    return cfg


def _published_index(cfg, *, title: str = "Song") -> None:
    entry = IndexEntry(
        path=str(cfg.library.music_dir / "song.flac"),
        title=title,
        artist="Artist",
        album="",
        genre="",
        bpm=0.0,
        year=0,
        length=1.0,
        energy=0.0,
        key=-1,
        mode=-1,
        tempo_confidence=0.0,
    )
    vectors = np.zeros((1, FEATURE_DIM), dtype=np.float32)
    vectors[0, 0] = 1.0
    save_index([entry], vectors, cfg.index.active_dir, cfg.library.music_dir)


def test_stopped_backup_contains_derived_and_unique_data(tmp_path: Path) -> None:
    cfg = _config(tmp_path)
    _published_index(cfg)
    _sqlite(cfg.index.active_dir / "dj_meta.db", "cue")
    (cfg.index.active_dir / "web_state.json").write_text("{}", encoding="utf-8")
    liners = cfg.index.active_dir / "liners"
    liners.mkdir()
    (liners / "station.wav").write_bytes(b"liner")
    archive = create_backup(cfg, tmp_path / "backup.zip", online=False)
    with zipfile.ZipFile(archive) as zf:
        manifest = json.loads(zf.read("manifest.json"))
        assert manifest["schema_version"] == 1
        assert {item["classification"] for item in manifest["items"]} == {"derived", "unique"}
        assert "derived/tracks.db" in zf.namelist()
        assert "derived/index-manifest.json" in zf.namelist()
        assert "unique/liners/station.wav" in zf.namelist()


def test_stopped_backup_refuses_active_wal(tmp_path: Path) -> None:
    cfg = _config(tmp_path)
    _published_index(cfg)
    (cfg.index.active_dir / "tracks.db-wal").write_bytes(b"active")
    with pytest.raises(BackupError, match="--online"):
        create_backup(cfg, tmp_path / "backup.zip", online=False)


def test_stopped_backup_refuses_sidecar_that_appears_during_copy(tmp_path: Path) -> None:
    cfg = _config(tmp_path)
    _published_index(cfg)
    metadata = cfg.index.active_dir / "dj_meta.db"
    _sqlite(metadata, "cue")
    from shutil import copy2 as real_copy2

    def copy_then_activate(source: Path, target: Path) -> None:
        real_copy2(source, target)
        if source == metadata:
            (cfg.index.active_dir / "dj_meta.db-wal").write_bytes(b"active")

    with (
        patch("autodj.backup.shutil.copy2", side_effect=copy_then_activate),
        pytest.raises(BackupError, match="changed during stopped-mode snapshot"),
    ):
        create_backup(cfg, tmp_path / "backup.zip", online=False)
    assert not (tmp_path / "backup.zip").exists()


def test_online_backup_includes_committed_wal_state(tmp_path: Path) -> None:
    cfg = _config(tmp_path)
    _published_index(cfg)
    db = cfg.index.active_dir / "dj_meta.db"
    writer = sqlite3.connect(db)
    writer.execute("PRAGMA journal_mode=WAL")
    writer.execute("CREATE TABLE data(value TEXT)")
    writer.execute("INSERT INTO data VALUES ('committed')")
    writer.commit()
    try:
        archive = create_backup(cfg, tmp_path / "backup.zip", online=True)
    finally:
        writer.close()
    target = tmp_path / "restored.db"
    with zipfile.ZipFile(archive) as zf:
        target.write_bytes(zf.read("derived/dj_meta.db"))
    with closing(sqlite3.connect(target)) as conn:
        assert conn.execute("SELECT value FROM data").fetchone() == ("committed",)


@pytest.mark.parametrize("target_is_directory", [False, True])
def test_backup_rejects_symlinks_in_unique_trees(
    tmp_path: Path, target_is_directory: bool
) -> None:
    cfg = _config(tmp_path)
    _published_index(cfg)
    liners = cfg.index.active_dir / "liners"
    liners.mkdir()
    outside = tmp_path / ("outside-dir" if target_is_directory else "outside.wav")
    if target_is_directory:
        outside.mkdir()
        (outside / "secret.wav").write_bytes(b"secret")
    else:
        outside.write_bytes(b"secret")
    link = liners / ("linked-dir" if target_is_directory else "linked.wav")
    try:
        link.symlink_to(outside, target_is_directory=target_is_directory)
    except OSError as exc:
        pytest.skip(f"symlinks unavailable: {exc}")
    destination = tmp_path / "backup.zip"
    with pytest.raises(BackupError, match="symbolic link"):
        create_backup(cfg, destination, online=False)
    assert not destination.exists()


def test_failed_backup_leaves_existing_destination_untouched(tmp_path: Path) -> None:
    cfg = _config(tmp_path)
    _published_index(cfg)
    destination = tmp_path / "backup.zip"
    destination.write_bytes(b"prior archive")

    def fail_after_partial_write(_cfg: object, archive: Path, *, online: bool) -> None:
        assert online is False
        archive.write_bytes(b"partial new archive")
        raise OSError("injected")

    with (
        patch("autodj.backup._write_backup_archive", side_effect=fail_after_partial_write),
        pytest.raises(BackupError, match="backup creation failed"),
    ):
        create_backup(cfg, destination, online=False, force=True)
    assert destination.read_bytes() == b"prior archive"
    assert not list(tmp_path.glob(".backup.zip.backup-*.tmp"))


def test_backup_refuses_existing_destination_without_force(tmp_path: Path) -> None:
    cfg = _config(tmp_path)
    _published_index(cfg)
    destination = tmp_path / "backup.zip"
    destination.write_bytes(b"prior archive")
    with (
        patch("autodj.backup._write_backup_archive") as writer,
        pytest.raises(BackupError, match="--force"),
    ):
        create_backup(cfg, destination, online=False)
    writer.assert_not_called()
    assert destination.read_bytes() == b"prior archive"


def test_backup_force_replaces_existing_destination(tmp_path: Path) -> None:
    cfg = _config(tmp_path)
    _published_index(cfg)
    destination = tmp_path / "backup.zip"
    destination.write_bytes(b"prior archive")
    create_backup(cfg, destination, online=False, force=True)
    with zipfile.ZipFile(destination) as zf:
        assert "manifest.json" in zf.namelist()


def test_backup_cli_passes_force_explicitly(tmp_path: Path) -> None:
    destination = tmp_path / "backup.zip"
    with patch("autodj.backup.create_backup", return_value=destination) as create:
        result = CliRunner().invoke(cli, ["backup", str(destination), "--force"])
    assert result.exit_code == 0, result.output
    assert create.call_args.kwargs == {"online": False, "force": True}


def test_online_backup_retries_one_generation_change(tmp_path: Path) -> None:
    cfg = _config(tmp_path)
    _published_index(cfg)
    actual = read_manifest(cfg.index.active_dir)
    assert actual is not None
    changed = replace(actual, generation=actual.generation + 1)
    from autodj.index_manifest import copy_published_snapshot as real_copy

    with patch("autodj.backup.copy_published_snapshot") as copied:
        calls = 0

        def flaky_copy(
            source: Path,
            destination: Path,
            *,
            expected_generation: int | None = None,
        ) -> IndexManifest:
            nonlocal calls
            calls += 1
            if calls == 1:
                # Simulate the manifest advancing between the initial read and copy.
                assert expected_generation == actual.generation
                raise IndexConsistencyError(f"generation changed to {changed.generation}")
            return real_copy(
                source,
                destination,
                expected_generation=actual.generation,
            )

        copied.side_effect = flaky_copy
        create_backup(cfg, tmp_path / "backup.zip", online=True)
    assert copied.call_count == 2


def test_online_backup_refuses_continuous_generation_churn(tmp_path: Path) -> None:
    cfg = _config(tmp_path)
    _published_index(cfg)
    destination = tmp_path / "backup.zip"
    destination.write_bytes(b"prior archive")
    with (
        patch(
            "autodj.backup.copy_published_snapshot",
            side_effect=IndexConsistencyError("generation changed"),
        ) as copied,
        pytest.raises(BackupError, match="changed during 3 snapshot attempts"),
    ):
        create_backup(cfg, destination, online=True, force=True)
    assert copied.call_count == 3
    assert destination.read_bytes() == b"prior archive"


def test_restore_refuses_newer_schema_before_writing(tmp_path: Path) -> None:
    cfg = _config(tmp_path)
    archive = tmp_path / "future.zip"
    with zipfile.ZipFile(archive, "w") as zf:
        zf.writestr("manifest.json", json.dumps({"schema_version": 999, "items": []}))
    with pytest.raises(BackupError, match="schema 999"):
        restore_backup(cfg, archive, force=False)
    assert not (cfg.index.active_dir / "vectors.index").exists()


def test_restore_refuses_incompatible_autodj_version_before_writing(tmp_path: Path) -> None:
    cfg = _config(tmp_path)
    archive = tmp_path / "future-version.zip"
    manifest = {"schema_version": 1, "autodj_version": "0.16.0", "items": []}
    with zipfile.ZipFile(archive, "w") as zf:
        zf.writestr("manifest.json", json.dumps(manifest))
    with pytest.raises(BackupError, match=r"AutoDJ version 0\.16\.0"):
        restore_backup(cfg, archive, force=False)
    assert not (cfg.index.active_dir / "vectors.index").exists()


def test_restore_refuses_destination_traversal_before_writing(tmp_path: Path) -> None:
    cfg = _config(tmp_path)
    archive = tmp_path / "traversal.zip"
    manifest = {
        "schema_version": 1,
        "autodj_version": current_version(),
        "items": [
            {
                "archive_path": "derived/tracks.db",
                "classification": "derived",
                "destination": "active/../escaped.db",
                "size": 7,
                "sha256": "239f59ed55e737c77147cf55ad0c1b030b6d7ee748a7426952f9b852d5a935e5",
            }
        ],
    }
    with zipfile.ZipFile(archive, "w") as zf:
        zf.writestr("manifest.json", json.dumps(manifest))
        zf.writestr("derived/tracks.db", b"payload")
    with pytest.raises(BackupError, match="unsafe restore path"):
        restore_backup(cfg, archive, force=False)
    assert not (cfg.index.index_dir / "escaped.db").exists()


def test_restore_rejects_central_directory_size_mismatch_before_staging(
    tmp_path: Path,
) -> None:
    cfg = _config(tmp_path)
    payload = b"payload"
    archive = tmp_path / "size-mismatch.zip"
    manifest = {
        "schema_version": 1,
        "autodj_version": current_version(),
        "items": [
            {
                "archive_path": "derived/tracks.db",
                "classification": "derived",
                "destination": "active/tracks.db",
                "size": len(payload) + 1,
                "sha256": hashlib.sha256(payload).hexdigest(),
            }
        ],
    }
    with zipfile.ZipFile(archive, "w") as zf:
        zf.writestr("manifest.json", json.dumps(manifest))
        zf.writestr("derived/tracks.db", payload)
    with (
        patch("autodj.backup._stage_payloads") as stage,
        pytest.raises(BackupError, match="central-directory size"),
    ):
        restore_backup(cfg, archive, force=True)
    stage.assert_not_called()
    assert not (cfg.index.active_dir / "tracks.db").exists()


def test_restore_rejects_compressed_manifest_bomb_before_reading_items(
    tmp_path: Path,
) -> None:
    cfg = _config(tmp_path)
    archive = tmp_path / "manifest-bomb.zip"
    with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("manifest.json", b" " * (16 * 1024**2 + 1))
    with (
        patch("autodj.backup._parse_items") as parse_items,
        pytest.raises(BackupError, match="manifest exceeds 16 MiB"),
    ):
        restore_backup(cfg, archive, force=True)
    parse_items.assert_not_called()


def test_restore_rejects_encrypted_and_nonregular_members() -> None:
    encrypted = ZipInfo("derived/encrypted.db")
    encrypted.flag_bits |= 0x1
    symlink = ZipInfo("derived/link.db")
    symlink.create_system = 3
    symlink.external_attr = (stat.S_IFLNK | 0o777) << 16
    directory = ZipInfo("derived/directory/")
    for info, message in (
        (encrypted, "encrypted"),
        (symlink, "regular file"),
        (directory, "regular file"),
    ):
        with pytest.raises(BackupError, match=message):
            _validate_member_info(info)


def test_restore_preflights_free_space_before_staging(tmp_path: Path) -> None:
    source = _config(tmp_path / "source")
    _published_index(source)
    archive = create_backup(source, tmp_path / "backup.zip", online=False)
    target = _config(tmp_path / "target")
    with (
        patch("autodj.backup.shutil.disk_usage", return_value=SimpleNamespace(free=0)),
        patch("autodj.backup._stage_payloads") as stage,
        pytest.raises(BackupError, match="insufficient free space"),
    ):
        restore_backup(target, archive, force=True)
    stage.assert_not_called()
    assert not (target.index.active_dir / "tracks.db").exists()


def test_restore_space_margin_has_explicit_floor_and_cap() -> None:
    mib = 1024**2
    gib = 1024**3
    assert _required_free_space(1) == 1 + 64 * mib
    assert _required_free_space(100 * gib) == 101 * gib


@pytest.mark.parametrize(
    "items",
    [
        "not-a-list",
        [
            {
                "archive_path": "derived/tracks.db",
                "classification": "derived",
                "destination": "active/tracks.db",
                "size": 7,
                "sha256": "239f59ed55e737c77147cf55ad0c1b030b6d7ee748a7426952f9b852d5a935e5",
            },
            {
                "archive_path": "derived/tracks.db",
                "classification": "derived",
                "destination": "active/other.db",
                "size": 7,
                "sha256": "239f59ed55e737c77147cf55ad0c1b030b6d7ee748a7426952f9b852d5a935e5",
            },
        ],
    ],
)
def test_restore_rejects_invalid_or_duplicate_manifest_items_before_writing(
    tmp_path: Path, items: object
) -> None:
    cfg = _config(tmp_path)
    archive = tmp_path / "invalid.zip"
    manifest = {
        "schema_version": 1,
        "autodj_version": current_version(),
        "items": items,
    }
    with zipfile.ZipFile(archive, "w") as zf:
        zf.writestr("manifest.json", json.dumps(manifest))
        zf.writestr("derived/tracks.db", b"payload")
    with pytest.raises(BackupError, match=r"manifest items|duplicate archive member"):
        restore_backup(cfg, archive, force=True)
    assert not (cfg.index.active_dir / "tracks.db").exists()


@pytest.mark.parametrize("failure", ["missing", "corrupt"])
def test_invalid_later_member_leaves_every_target_unchanged(
    tmp_path: Path, failure: str
) -> None:
    source = _config(tmp_path / "source")
    _published_index(source, title="New")
    good = create_backup(source, tmp_path / "good.zip", online=False)
    broken = tmp_path / "broken.zip"
    with zipfile.ZipFile(good) as src, zipfile.ZipFile(broken, "w") as dst:
        names = src.namelist()
        assert names.index("derived/tracks.db") > names.index("derived/vectors.index")
        for name in names:
            if name == "derived/tracks.db" and failure == "missing":
                continue
            data = b"corrupt" if name == "derived/tracks.db" else src.read(name)
            dst.writestr(name, data)

    target = _config(tmp_path / "target")
    _published_index(target, title="Old")
    before_vectors = (target.index.active_dir / "vectors.index").read_bytes()
    before_tracks = (target.index.active_dir / "tracks.db").read_bytes()
    with pytest.raises(BackupError, match=r"missing|checksum|size"):
        restore_backup(target, broken, force=True)
    assert (target.index.active_dir / "vectors.index").read_bytes() == before_vectors
    assert (target.index.active_dir / "tracks.db").read_bytes() == before_tracks


def test_replace_failure_rolls_back_every_target(tmp_path: Path) -> None:
    source = _config(tmp_path / "source")
    _published_index(source, title="New")
    archive = create_backup(source, tmp_path / "backup.zip", online=False)
    target = _config(tmp_path / "target")
    _published_index(target, title="Old")
    active = target.index.active_dir
    before = {
        name: (active / name).read_bytes()
        for name in ("vectors.index", "tracks.db", "index-manifest.json")
    }
    real_replace = __import__("os").replace

    def fail_tracks_install(source_path, destination_path) -> None:
        source_name = Path(source_path).name
        if ".restore-stage-" in source_name and Path(destination_path).name == "tracks.db":
            raise OSError("injected install failure")
        real_replace(source_path, destination_path)

    with (
        patch("autodj.backup.os.replace", side_effect=fail_tracks_install),
        pytest.raises(BackupError, match="previous files restored"),
    ):
        restore_backup(target, archive, force=True)
    assert {name: (active / name).read_bytes() for name in before} == before


def test_recovery_cleanup_failure_is_success_with_warning(tmp_path: Path) -> None:
    source = _config(tmp_path / "source")
    _published_index(source, title="New")
    archive = create_backup(source, tmp_path / "backup.zip", online=False)
    target = _config(tmp_path / "target")
    _published_index(target, title="Old")
    expected = (source.index.active_dir / "tracks.db").read_bytes()
    real_unlink = Path.unlink

    def retain_recovery(path: Path, *args, **kwargs) -> None:
        if ".restore-old-" in path.name:
            raise OSError("injected cleanup failure")
        real_unlink(path, *args, **kwargs)

    with patch("autodj.backup.Path.unlink", new=retain_recovery):
        result = restore_backup(target, archive, force=True)
    assert result.restored >= 3
    assert any("recovery copy retained" in warning for warning in result.warnings)
    assert (target.index.active_dir / "tracks.db").read_bytes() == expected
    assert list(target.index.active_dir.glob(".*.restore-old-*"))


def test_post_install_directory_fsync_failure_is_success_with_warning(
    tmp_path: Path,
) -> None:
    source = _config(tmp_path / "source")
    _published_index(source, title="New")
    archive = create_backup(source, tmp_path / "backup.zip", online=False)
    target = _config(tmp_path / "target")
    _published_index(target, title="Old")
    expected = (source.index.active_dir / "tracks.db").read_bytes()
    with patch(
        "autodj.backup._fsync_directory", side_effect=OSError("injected sync failure")
    ):
        result = restore_backup(target, archive, force=True)
    assert (target.index.active_dir / "tracks.db").read_bytes() == expected
    assert any("directory sync" in warning for warning in result.warnings)


def test_restore_requires_force_then_recreates_files(tmp_path: Path) -> None:
    source = _config(tmp_path / "source")
    _published_index(source, title="New")
    archive = create_backup(source, tmp_path / "backup.zip", online=False)
    target = _config(tmp_path / "target")
    _published_index(target, title="Old")
    expected = (source.index.active_dir / "vectors.index").read_bytes()
    with pytest.raises(BackupError, match="--force"):
        restore_backup(target, archive, force=False)
    restore_backup(target, archive, force=True)
    assert (target.index.active_dir / "vectors.index").read_bytes() == expected
```

- [ ] **Step 2: Verify backup tests are red**

Run: `uv run pytest tests/unit/test_backup.py -q`

Expected: FAIL because `autodj.backup` does not exist. Once the initial module exists, the new
tests remain red until archive creation is atomic, manifest entries are checksummed and unique,
existing backup destinations require explicit force, symlinks are refused, ZIP member type/size
and per-filesystem capacity are preflighted before staging, all restore payloads are staged before
any target changes, rollback is implemented, committed cleanup failures become warnings, and
online backup retries the security-owned coherent snapshot operation.

- [ ] **Step 3: Implement durable archive publication and transactional restore**

Create `src/autodj/backup.py`:

```python
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import sqlite3
import stat
import tempfile
import zipfile
from contextlib import closing
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Any

from autodj.config import AutoDJConfig
from autodj.index_manifest import (
    IndexConsistencyError,
    copy_published_snapshot,
    read_manifest,
)
from autodj.version import current_version

SCHEMA_VERSION = 1
MAX_MANIFEST_BYTES = 16 * 1024**2


class BackupError(RuntimeError):
    """Raised when a backup cannot be created or safely restored."""


@dataclass(frozen=True)
class BackupItem:
    """Manifest entry mapping one archive member to a restore target."""

    archive_path: str
    classification: str
    destination: str
    size: int
    sha256: str


@dataclass(frozen=True)
class RestoreResult:
    """Successful restore count plus non-fatal post-commit cleanup warnings."""

    restored: int
    warnings: tuple[str, ...] = ()


@dataclass
class _StagedRestore:
    item: BackupItem
    target: Path
    stage: Path
    previous: Path | None = None
    installed: bool = False


def _unique_roots(cfg: AutoDJConfig) -> list[tuple[Path, str]]:
    active = cfg.index.active_dir
    roots = [
        (active / "web_state.json", "web_state"),
        (Path(cfg.playback.liners_folder) if cfg.playback.liners_folder else active / "liners", "liners"),
        (active.parent / "profiles", "profiles"),
    ]
    if cfg.playback.dayparts_dir:
        roots.append((Path(cfg.playback.dayparts_dir), "dayparts"))
    if cfg.playback.history_file:
        roots.append((cfg.playback.history_file, "history"))
    return roots


def _add_file(
    zf: zipfile.ZipFile,
    source: Path,
    archive_path: str,
    classification: str,
    destination: str,
    items: list[BackupItem],
) -> None:
    digest = hashlib.sha256()
    size = 0
    with source.open("rb") as src, zf.open(archive_path, "w") as dst:
        while chunk := src.read(1024 * 1024):
            dst.write(chunk)
            digest.update(chunk)
            size += len(chunk)
    items.append(BackupItem(archive_path, classification, destination, size, digest.hexdigest()))


def _add_path(
    zf: zipfile.ZipFile,
    source: Path,
    prefix: str,
    destination: str,
    items: list[BackupItem],
) -> None:
    if source.is_symlink():
        raise BackupError(f"refusing symbolic link in backup source: {source}")
    if not source.exists():
        return
    if source.is_file():
        files = [source]
    elif source.is_dir():
        candidates = sorted(source.rglob("*"))
        link = next((path for path in candidates if path.is_symlink()), None)
        if link is not None:
            raise BackupError(f"refusing symbolic link in backup source: {link}")
        files = [path for path in candidates if path.is_file()]
    else:
        raise BackupError(f"backup source is not a regular file or directory: {source}")
    for file in files:
        relative = file.name if source.is_file() else file.relative_to(source).as_posix()
        archive_path = f"unique/{prefix}/{relative}"
        _add_file(zf, file, archive_path, "unique", f"{destination}/{relative}", items)


def _sqlite_snapshot(source: Path, target: Path) -> None:
    with closing(sqlite3.connect(source)) as src, closing(sqlite3.connect(target)) as dst:
        src.backup(dst)


def _fsync_directory(path: Path) -> None:
    if os.name == "nt":
        return
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _remove_snapshot(path: Path) -> None:
    if path.exists():
        shutil.rmtree(path)


_SQLITE_MAIN_NAMES = ("tracks.db", "dj_meta.db")
_SQLITE_SIDECAR_SUFFIXES = ("-wal", "-shm", "-journal")


def _capture_stopped_state(active: Path) -> dict[str, tuple[int, int]]:
    names = [Path(name) for name in _SQLITE_MAIN_NAMES]
    names.extend(
        Path(f"{name}{suffix}")
        for name in _SQLITE_MAIN_NAMES
        for suffix in _SQLITE_SIDECAR_SUFFIXES
    )
    state: dict[str, tuple[int, int]] = {}
    for relative in names:
        path = active / relative
        try:
            metadata = path.stat()
        except FileNotFoundError:
            continue
        state[relative.as_posix()] = (metadata.st_size, metadata.st_mtime_ns)
    return state


def _begin_stopped_snapshot(active: Path) -> dict[str, tuple[int, int]]:
    state = _capture_stopped_state(active)
    sidecars = [
        name
        for name in state
        if name.endswith(_SQLITE_SIDECAR_SUFFIXES)
    ]
    if sidecars:
        raise BackupError(
            f"SQLite state {', '.join(sidecars)} exists; stop the service or use --online"
        )
    return state


def _verify_stopped_state(
    expected: dict[str, tuple[int, int]], active: Path, destination: Path, *, phase: str
) -> None:
    current = _capture_stopped_state(active)
    if current == expected:
        return
    changed = sorted(
        name for name in set(expected) | set(current) if expected.get(name) != current.get(name)
    )
    _remove_snapshot(destination)
    raise BackupError(
        f"SQLite state changed during stopped-mode snapshot ({phase}: {changed}); "
        "stop the service or use --online"
    )


def _snapshot_derived(cfg: AutoDJConfig, destination: Path, *, online: bool) -> None:
    active = cfg.index.active_dir
    stopped_state = None if online else _begin_stopped_snapshot(active)

    manifest = read_manifest(active)
    has_index = any((active / name).exists() for name in ("vectors.index", "tracks.db"))
    if manifest is None and has_index:
        raise BackupError(
            "index has no published manifest; rebuild it before backup so one coherent "
            "generation can be selected"
        )
    if manifest is not None:
        attempts = 3 if online else 1
        expected_generation = manifest.generation
        for attempt in range(attempts):
            # Security's API publishes the copied directory atomically and requires it absent.
            _remove_snapshot(destination)
            try:
                copy_published_snapshot(
                    active,
                    destination,
                    expected_generation=expected_generation,
                )
                break
            except IndexConsistencyError as exc:
                if attempt + 1 == attempts:
                    raise BackupError(
                        f"published index changed during {attempts} snapshot attempts; retry later"
                    ) from exc
                latest = read_manifest(active)
                if latest is None:
                    raise BackupError("published index disappeared during backup") from exc
                expected_generation = latest.generation
        if stopped_state is not None:
            _verify_stopped_state(
                stopped_state, active, destination, phase="published index copy"
            )

    metadata = active / "dj_meta.db"
    if metadata.exists():
        destination.mkdir(parents=True, exist_ok=True)
        target = destination / metadata.name
        if online:
            _sqlite_snapshot(metadata, target)
        else:
            shutil.copy2(metadata, target)
    if stopped_state is not None:
        _verify_stopped_state(stopped_state, active, destination, phase="DJ metadata copy")


def _write_backup_archive(cfg: AutoDJConfig, archive: Path, *, online: bool) -> None:
    """Write a complete archive to a new, unpublished path."""

    items: list[BackupItem] = []
    with tempfile.TemporaryDirectory(prefix="autodj-backup-snapshot-") as temp_name:
        # The child is deliberately absent for copy_published_snapshot's atomic promotion.
        snapshot = Path(temp_name) / "published"
        _snapshot_derived(cfg, snapshot, online=online)
        with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED) as zf:
            for name in ("vectors.index", "tracks.db", "index-manifest.json", "dj_meta.db"):
                source = snapshot / name
                if source.exists():
                    _add_file(zf, source, f"derived/{name}", "derived", f"active/{name}", items)
            for source, label in _unique_roots(cfg):
                _add_path(zf, source, label, label, items)
            manifest = {
                "schema_version": SCHEMA_VERSION,
                "autodj_version": current_version(),
                "created_at": datetime.now(UTC).isoformat(),
                "index_name": cfg.index.name,
                "mode": "online" if online else "stopped",
                "items": [asdict(item) for item in items],
            }
            zf.writestr("manifest.json", json.dumps(manifest, indent=2) + "\n")


def create_backup(
    cfg: AutoDJConfig,
    destination: Path,
    *,
    online: bool,
    force: bool = False,
) -> Path:
    """Create an archive without replacing an existing destination by default."""

    destination = destination.resolve()
    destination.parent.mkdir(parents=True, exist_ok=True)
    if os.path.lexists(destination):
        if not force:
            raise BackupError(f"{destination} exists; pass --force to replace it")
        if not destination.is_file():
            raise BackupError(f"backup destination is not a regular file: {destination}")
    descriptor, temp_name = tempfile.mkstemp(
        prefix=f".{destination.name}.backup-",
        suffix=".tmp",
        dir=destination.parent,
    )
    os.close(descriptor)
    unpublished = Path(temp_name)
    try:
        _write_backup_archive(cfg, unpublished, online=online)
        with unpublished.open("rb+") as handle:
            handle.flush()
            os.fsync(handle.fileno())
        if force:
            os.replace(unpublished, destination)
        else:
            # Same-directory hard-link publication is atomic and fails if a racer created
            # destination after the preflight; os.replace would silently clobber that file.
            try:
                os.link(unpublished, destination)
            except FileExistsError as exc:
                raise BackupError(
                    f"{destination} appeared during backup; pass --force to replace it"
                ) from exc
            except OSError as exc:
                raise BackupError(
                    "filesystem cannot atomically publish a no-clobber backup; "
                    "choose a local destination or pass --force"
                ) from exc
            unpublished.unlink(missing_ok=True)
        _fsync_directory(destination.parent)
    except Exception as exc:
        unpublished.unlink(missing_ok=True)
        if isinstance(exc, BackupError):
            raise
        raise BackupError(f"backup creation failed: {exc}") from exc
    return destination


def _destination(cfg: AutoDJConfig, label: str, relative: str) -> Path:
    active = cfg.index.active_dir
    roots = {
        "active": active,
        "web_state": active,
        "liners": Path(cfg.playback.liners_folder) if cfg.playback.liners_folder else active / "liners",
        "profiles": active.parent / "profiles",
        "dayparts": Path(cfg.playback.dayparts_dir) if cfg.playback.dayparts_dir else active.parent / "dayparts",
        "history": cfg.playback.history_file.parent if cfg.playback.history_file else active.parent,
    }
    if label not in roots:
        raise BackupError(f"unsupported restore destination {label!r}")
    target = (roots[label] / relative).resolve()
    root = roots[label].resolve()
    if target != root and root not in target.parents:
        raise BackupError(f"unsafe restore path {target}")
    return target


def _compatibility_line(version: str) -> tuple[int, int]:
    match = re.match(r"^(\d+)\.(\d+)(?:\.|$)", version)
    if match is None:
        raise BackupError(f"invalid AutoDJ version in backup: {version!r}")
    return int(match.group(1)), int(match.group(2))


def _safe_relative(value: str, *, field: str) -> PurePosixPath:
    path = PurePosixPath(value)
    if (
        not value
        or "\\" in value
        or "\x00" in value
        or path.is_absolute()
        or any(part in ("", ".", "..") for part in path.parts)
    ):
        raise BackupError(f"unsafe restore path in {field}: {value!r}")
    return path


def _validate_member_info(info: zipfile.ZipInfo) -> None:
    _safe_relative(info.filename, field="archive member")
    if info.flag_bits & 0x1:
        raise BackupError(f"encrypted archive member is unsupported: {info.filename}")
    unix_mode = (info.external_attr >> 16) & 0xFFFF
    file_type = stat.S_IFMT(unix_mode)
    if info.is_dir() or (info.create_system == 3 and file_type not in (0, stat.S_IFREG)):
        raise BackupError(f"archive member is not a regular file: {info.filename}")


def _member_map(zf: zipfile.ZipFile) -> dict[str, zipfile.ZipInfo]:
    members: dict[str, zipfile.ZipInfo] = {}
    for info in zf.infolist():
        _validate_member_info(info)
        if info.filename in members:
            raise BackupError(f"backup contains a duplicate archive member: {info.filename}")
        members[info.filename] = info
    return members


def _parse_items(
    members: dict[str, zipfile.ZipInfo], raw_manifest: Any
) -> list[BackupItem]:
    if not isinstance(raw_manifest, dict):
        raise BackupError("backup manifest must be an object")
    raw_items = raw_manifest.get("items")
    if not isinstance(raw_items, list):
        raise BackupError("backup manifest items must be a list")
    items: list[BackupItem] = []
    archive_paths: set[str] = set()
    destinations: set[str] = set()
    for raw in raw_items:
        if not isinstance(raw, dict):
            raise BackupError("backup manifest items must be objects")
        try:
            item = BackupItem(
                archive_path=raw["archive_path"],
                classification=raw["classification"],
                destination=raw["destination"],
                size=raw["size"],
                sha256=raw["sha256"],
            )
        except (KeyError, TypeError) as exc:
            raise BackupError("backup manifest item is missing or has invalid fields") from exc
        if not all(
            isinstance(value, str)
            for value in (item.archive_path, item.classification, item.destination, item.sha256)
        ):
            raise BackupError("backup manifest item fields have invalid types")
        archive_path = _safe_relative(item.archive_path, field="archive_path")
        _safe_relative(item.destination, field="destination")
        if item.classification not in {"derived", "unique"}:
            raise BackupError(f"unsupported backup classification {item.classification!r}")
        if archive_path.parts[0] != item.classification:
            raise BackupError("archive member classification does not match its path")
        if isinstance(item.size, bool) or not isinstance(item.size, int) or item.size < 0:
            raise BackupError("backup manifest item size must be a non-negative integer")
        if re.fullmatch(r"[0-9a-f]{64}", item.sha256) is None:
            raise BackupError("backup manifest item checksum is invalid")
        if item.archive_path in archive_paths:
            raise BackupError(f"duplicate archive member {item.archive_path!r}")
        if item.destination in destinations:
            raise BackupError(f"duplicate restore destination {item.destination!r}")
        archive_paths.add(item.archive_path)
        destinations.add(item.destination)
        info = members.get(item.archive_path)
        if info is not None and info.file_size != item.size:
            raise BackupError(
                f"central-directory size for {item.archive_path} is {info.file_size}; "
                f"manifest declares {item.size}"
            )
        items.append(item)

    member_names = set(members)
    missing = archive_paths - member_names
    unexpected = member_names - archive_paths - {"manifest.json"}
    if missing:
        raise BackupError(f"backup member is missing: {sorted(missing)[0]}")
    if unexpected:
        raise BackupError(f"backup contains unmanifested member: {sorted(unexpected)[0]}")
    return items


def _resolve_targets(
    cfg: AutoDJConfig, items: list[BackupItem], *, force: bool
) -> list[tuple[BackupItem, Path]]:
    targets: list[tuple[BackupItem, Path]] = []
    resolved: set[Path] = set()
    for item in items:
        parts = PurePosixPath(item.destination).parts
        target = _destination(cfg, parts[0], PurePosixPath(*parts[1:]).as_posix())
        if target in resolved:
            raise BackupError(f"duplicate restore target {target}")
        if target.exists() and not target.is_file():
            raise BackupError(f"restore target is not a file: {target}")
        if target.exists() and not force:
            raise BackupError(f"{target} exists; pass --force to replace it")
        resolved.add(target)
        targets.append((item, target))
    return targets


_SPACE_MARGIN_FLOOR = 64 * 1024**2
_SPACE_MARGIN_CAP = 1024**3


def _required_free_space(payload_bytes: int) -> int:
    """Return payload plus a 5% margin bounded from 64 MiB to 1 GiB."""

    margin = min(max(payload_bytes // 20, _SPACE_MARGIN_FLOOR), _SPACE_MARGIN_CAP)
    return payload_bytes + margin


def _existing_ancestor(path: Path) -> Path:
    candidate = path
    while not candidate.exists():
        parent = candidate.parent
        if parent == candidate:
            raise BackupError(f"no existing filesystem ancestor for restore target {path}")
        candidate = parent
    return candidate


def _preflight_free_space(targets: list[tuple[BackupItem, Path]]) -> None:
    by_device: dict[int, tuple[Path, int]] = {}
    for item, target in targets:
        anchor = _existing_ancestor(target.parent)
        device = os.stat(anchor).st_dev
        previous_anchor, total = by_device.get(device, (anchor, 0))
        by_device[device] = (previous_anchor, total + item.size)
    for anchor, payload_bytes in by_device.values():
        required = _required_free_space(payload_bytes)
        free = shutil.disk_usage(anchor).free
        if free < required:
            raise BackupError(
                f"insufficient free space on {anchor}: need {required} bytes "
                f"for staged payload plus bounded safety margin; {free} available"
            )


def _stage_payloads(
    zf: zipfile.ZipFile, targets: list[tuple[BackupItem, Path]]
) -> list[_StagedRestore]:
    staged: list[_StagedRestore] = []
    try:
        for item, target in targets:
            target.parent.mkdir(parents=True, exist_ok=True)
            descriptor, stage_name = tempfile.mkstemp(
                prefix=f".{target.name}.restore-stage-", dir=target.parent
            )
            record = _StagedRestore(item, target, Path(stage_name))
            staged.append(record)
            digest = hashlib.sha256()
            size = 0
            with os.fdopen(descriptor, "wb") as dst, zf.open(item.archive_path) as src:
                while chunk := src.read(1024 * 1024):
                    dst.write(chunk)
                    digest.update(chunk)
                    size += len(chunk)
                dst.flush()
                os.fsync(dst.fileno())
            if size != item.size:
                raise BackupError(f"backup member size mismatch: {item.archive_path}")
            if digest.hexdigest() != item.sha256:
                raise BackupError(f"backup member checksum mismatch: {item.archive_path}")
        return staged
    except Exception as exc:
        for record in staged:
            record.stage.unlink(missing_ok=True)
        if isinstance(exc, BackupError):
            raise
        raise BackupError(f"backup member extraction failed: {exc}") from exc


def _commit_staged(staged: list[_StagedRestore]) -> RestoreResult:
    parents = {record.target.parent for record in staged}
    try:
        for record in staged:
            if record.target.exists():
                descriptor, previous_name = tempfile.mkstemp(
                    prefix=f".{record.target.name}.restore-old-",
                    dir=record.target.parent,
                )
                os.close(descriptor)
                previous = Path(previous_name)
                try:
                    os.replace(record.target, previous)
                except Exception:
                    previous.unlink(missing_ok=True)
                    raise
                record.previous = previous
            os.replace(record.stage, record.target)
            record.installed = True
    except Exception as exc:
        rollback_errors: list[str] = []
        for record in reversed(staged):
            try:
                if record.installed:
                    record.target.unlink(missing_ok=True)
                if record.previous is not None and record.previous.exists():
                    os.replace(record.previous, record.target)
            except OSError as rollback_exc:
                rollback_errors.append(f"{record.target}: {rollback_exc}")
        for record in staged:
            record.stage.unlink(missing_ok=True)
        if rollback_errors:
            retained = [
                str(record.previous)
                for record in staged
                if record.previous is not None and record.previous.exists()
            ]
            raise BackupError(
                "restore failed and rollback was incomplete; inspect targets; retained "
                f"recovery copies: {retained or ['none']}; errors: "
                + "; ".join(rollback_errors)
            ) from exc
        raise BackupError("restore failed; previous files restored") from exc

    # All target renames succeeded: this is the commit point. Cleanup must never turn an
    # installed restore into a reported failure or claim that rollback occurred.
    warnings: list[str] = []
    for record in staged:
        if record.previous is not None:
            try:
                record.previous.unlink(missing_ok=True)
            except OSError as exc:
                warnings.append(
                    f"recovery copy retained at {record.previous}: {exc}"
                )
    for parent in parents:
        try:
            _fsync_directory(parent)
        except OSError as exc:
            warnings.append(f"directory sync failed for installed restore at {parent}: {exc}")
    return RestoreResult(restored=len(staged), warnings=tuple(warnings))


def restore_backup(cfg: AutoDJConfig, archive: Path, *, force: bool) -> RestoreResult:
    """Validate and stage every payload, then install all targets with rollback."""

    try:
        zf = zipfile.ZipFile(archive)
    except (OSError, zipfile.BadZipFile) as exc:
        raise BackupError(f"backup archive is unreadable: {exc}") from exc
    with zf:
        members = _member_map(zf)
        manifest_info = members.get("manifest.json")
        if manifest_info is None:
            raise BackupError("backup manifest is missing")
        if manifest_info.file_size > MAX_MANIFEST_BYTES:
            raise BackupError(
                f"backup manifest exceeds 16 MiB metadata limit: {manifest_info.file_size} bytes"
            )
        try:
            manifest = json.loads(zf.read(manifest_info))
        except (OSError, RuntimeError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise BackupError("backup manifest is missing or invalid") from exc
        if not isinstance(manifest, dict):
            raise BackupError("backup manifest must be an object")
        schema = manifest.get("schema_version")
        if isinstance(schema, bool) or not isinstance(schema, int):
            raise BackupError("backup manifest schema_version must be an integer")
        if schema != SCHEMA_VERSION:
            raise BackupError(f"unsupported backup schema {schema}; expected {SCHEMA_VERSION}")
        backup_version = manifest.get("autodj_version")
        if not isinstance(backup_version, str):
            raise BackupError("backup manifest autodj_version must be a string")
        if _compatibility_line(backup_version) != _compatibility_line(current_version()):
            raise BackupError(
                f"backup AutoDJ version {backup_version} is incompatible with {current_version()}"
            )
        items = _parse_items(members, manifest)
        targets = _resolve_targets(cfg, items, force=force)
        _preflight_free_space(targets)
        staged = _stage_payloads(zf, targets)
    return _commit_staged(staged)
```

`copy_published_snapshot(index_dir: Path, destination: Path, *,
expected_generation: int | None = None) -> IndexManifest` is owned by the security/data-integrity
plan. It copies canonical `tracks.db`, `vectors.index`, and `index-manifest.json` from one immutable
manifest-referenced generation, validates digests/count, then re-reads the generation. Do not
redefine its locking or digest logic here. Backup calls it into a private staging directory and
archives only those staged files. A generation race is retried at most three times for `--online`;
continuous churn is refused without publishing a destination archive.

Archive publication never silently replaces a destination. Without `force`, the early existence
check gives a clear error and same-directory `os.link` is the final race-safe no-clobber publish
operation. With explicit `force`, the already-fsynced temporary archive is promoted with
`os.replace`. The Click command must always pass its `--force` value into the API.

Restore validates all central-directory records before extraction: names are contained POSIX
paths, entries are unique unencrypted regular files, every payload's `ZipInfo.file_size` equals its
manifest size, and the member set exactly matches the manifest. Before calling `zf.read`, it limits
the uncompressed `manifest.json` metadata document to 16 MiB so the manifest itself cannot be a
memory zip bomb. It then groups payload sizes by target `st_dev` and requires the full staged
payload plus a 5% safety margin bounded from 64 MiB to 1 GiB on every target filesystem. The 16 MiB
limit applies only to metadata; payloads have no arbitrary maximum, so valid large libraries remain
supported subject to actual free capacity.

The restore transaction stages and hashes **every** member before the first target rename. The
commit uses same-directory `os.replace` operations and retains one rollback file per pre-existing
target until every install succeeds. If an install fails, rollback errors report retained recovery
paths. Once every install rename succeeds, installed data is success: failure to unlink an old
recovery copy or fsync a directory is returned in `RestoreResult.warnings`, printed by the CLI, and
must not trigger or claim rollback. There is no honest way to promise one cross-directory atomic
rename.

`online=False` is an operator assertion that the service has been stopped, not something WAL
absence can prove. The stopped path checks `-wal`, `-shm`, and rollback-journal sidecars both before
and after copying and refuses if they appear, but documentation must still require `docker compose
down` (or equivalent process shutdown). Use `--online` whenever exclusive shutdown is uncertain.

Add Click commands that run doctor after restore:

```python
@cli.command("backup")
@click.argument("destination", type=click.Path(path_type=Path, dir_okay=False))
@click.option("--online", is_flag=True, help="Use SQLite online backup while AutoDJ is running.")
@click.option("--force", is_flag=True, help="Atomically replace an existing destination archive.")
@click.pass_context
def cmd_backup(ctx: click.Context, destination: Path, online: bool, force: bool) -> None:
    """Create a versioned backup of derived and unique AutoDJ state."""
    from autodj.backup import BackupError, create_backup
    cfg = _load_cfg_or_exit(ctx.obj["config_path"])
    try:
        path = create_backup(cfg, destination, online=online, force=force)
    except BackupError as exc:
        raise click.ClickException(str(exc)) from exc
    click.echo(f"Backup written: {path}")


@cli.command("restore")
@click.argument("archive", type=click.Path(path_type=Path, exists=True, dir_okay=False))
@click.option("--force", is_flag=True, help="Replace files already present at restore targets.")
@click.pass_context
def cmd_restore(ctx: click.Context, archive: Path, force: bool) -> None:
    """Restore a compatible backup, then require doctor validation."""
    from autodj.backup import BackupError, restore_backup
    from autodj.doctor import render_text, run_doctor
    cfg = _load_cfg_or_exit(ctx.obj["config_path"])
    try:
        result = restore_backup(cfg, archive, force=force)
    except BackupError as exc:
        raise click.ClickException(str(exc)) from exc
    click.echo(f"Restored {result.restored} files.")
    for warning in result.warnings:
        click.echo(f"WARNING: {warning}", err=True)
    report = run_doctor(cfg)
    click.echo(render_text(report))
    if report.exit_code:
        raise click.ClickException("Restore completed, but doctor found required failures; do not serve yet.")
```

- [ ] **Step 4: Run backup, restore, doctor, and CLI tests**

Run: `uv run pytest tests/unit/test_backup.py tests/unit/test_doctor.py tests/unit/test_cli.py -q`

Expected: PASS. Stopped mode rejects WAL, online mode captures committed metadata rows from WAL and
uses one security-published index generation, a transient generation change retries, continuous
churn refuses, stopped mode detects sidecars that appear during copying, existing backup
destinations require explicit `--force`, a failed forced create preserves the prior destination,
unique-data symlinks are refused, malformed/duplicate/encrypted/non-regular/size-mismatched members
and insufficient target-filesystem capacity fail before staging, missing/corrupt later members leave
all targets unchanged, an injected install failure rolls every target back, post-commit cleanup and
directory-sync failures return success warnings, incompatible schema or AutoDJ release line writes
nothing, existing restore targets require `--force`, and restore invokes doctor.

- [ ] **Step 5: Commit backup and restore support**

```bash
git add src/autodj/backup.py src/autodj/cli.py tests/unit/test_backup.py
git commit -m "feat: add coherent backup and restore"
```

### Task 10: Python quality, branch coverage, and resource-warning gates

**Files:**
- Create: `scripts/check_coverage_policy.py`
- Create: `tests/unit/test_coverage_policy.py`
- Modify: `pyproject.toml:93-156,199-219`
- Modify: `.github/workflows/ci.yml:44-49`

- [ ] **Step 1: Add a test that rejects broad coverage exclusions**

Create `scripts/check_coverage_policy.py`:

```python
from __future__ import annotations

import sys
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FORBIDDEN = (
    "except Exception",
    "except OSError",
    "except FileNotFoundError",
    "sys\\.exit",
    "if open_browser",
)


def main() -> int:
    """Return nonzero when coverage configuration hides executable branches."""

    with (ROOT / "pyproject.toml").open("rb") as fh:
        exclusions = tomllib.load(fh)["tool"]["coverage"]["report"]["exclude_lines"]
    bad = [pattern for pattern in exclusions if any(token in pattern for token in FORBIDDEN)]
    if bad:
        print("Broad coverage exclusions are forbidden: " + ", ".join(bad), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
```

Create `tests/unit/test_coverage_policy.py`:

```python
from scripts.check_coverage_policy import main


def test_coverage_exclusions_are_narrow() -> None:
    assert main() == 0
```

- [ ] **Step 2: Run policy, resource-warning, Pyright, and coverage gates to establish red state**

Run:

```bash
uv run pytest tests/unit/test_coverage_policy.py -q
uv run pytest tests/unit/test_beets.py tests/unit/test_dj_cues.py tests/unit/test_indexer.py -W error::ResourceWarning -q
uv run pyright src/autodj
uv run python scripts/ci_pytest.py
```

Expected: the policy test FAILS on broad exception/CLI patterns. The focused ResourceWarning run PASSES because the current helpers explicitly close their connections. Pyright must be treated as red if it prints any error; coverage may fall below the existing 99.1% line/94.7% branch floors after exclusions are narrowed.

- [ ] **Step 3: Narrow exclusions, target SQLite warnings, and make Pyright blocking**

In `pyproject.toml`, retain only structural and hardware-specific exclusions:

```toml
exclude_lines = [
    "pragma: no cover",
    "raise NotImplementedError",
    "if TYPE_CHECKING:",
    "@overload",
    "if torch.cuda.is_available\\(\\):",
    "if not torch.cuda.is_available\\(\\):",
]
```

Do not lower `[tool.autodj.coverage]` floors. Add a targeted warning rule without changing unrelated third-party warnings:

```toml
"error:unclosed database.*:ResourceWarning",
```

Retain the explicit `conn.close()`/`con.close()` calls already present in the focused SQLite test helpers. The new backup tests use `contextlib.closing`, so the targeted warning gate covers every newly introduced connection without a broad `error::ResourceWarning` policy that would promote unrelated third-party warnings.

Remove `continue-on-error: true` from the Pyright CI step:

```yaml
      - name: Pyright — cross-check type checker
        run: uv run pyright src/autodj/
```

Add the policy check to CI after Ruff:

```yaml
      - name: Coverage exclusion policy
        run: uv run python scripts/check_coverage_policy.py
```

The implementations in Tasks 1-9 use declared return types, optional-value checks, and typed dataclasses. Do not add file-wide ignores or make Pyright advisory.

- [ ] **Step 4: Run the complete branch, resource, and type gates**

Tasks 1-9 already add named assertions for absent/explicit/env-invalid configuration, empty player stop, invalid bundle JSON, corrupt/partial indexes, missing ffmpeg, unsupported Python, unsafe bind, stopped WAL refusal, online SQLite backup, incompatible schema/release restore, and doctor redaction. Do not replace those branches with exclusions.

Run:

```bash
uv run python scripts/check_coverage_policy.py
uv run pytest tests/unit/test_beets.py tests/unit/test_dj_cues.py tests/unit/test_indexer.py -W error::ResourceWarning -q
uv run mypy src/autodj
uv run pyright src/autodj
uv run python scripts/ci_pytest.py
```

Expected: PASS; final output includes `Coverage gates passed` with lines at least 99.1% and branches at least 94.7%, and both type checkers report zero errors.

- [ ] **Step 5: Commit Python quality enforcement**

```bash
git add pyproject.toml .github/workflows/ci.yml scripts/check_coverage_policy.py tests/unit/test_coverage_policy.py
git commit -m "test: enforce Python quality gates"
```

### Task 11: Release identity and exact-artifact verification

**Files:**
- Create: `scripts/verify_release.py`
- Create: `tests/unit/test_verify_release.py`
- Modify: `.github/workflows/ci.yml:3-8`
- Modify: `.github/workflows/security.yml:3-10`
- Modify: `.github/workflows/release.yml`

- [ ] **Step 1: Write failing release-verifier tests**

Create `tests/unit/test_verify_release.py`:

```python
import zipfile
from pathlib import Path

import pytest

from scripts.verify_release import ReleaseVerificationError, verify_release


def _wheel(path: Path, version: str) -> None:
    dist_info = f"autodj-{version}.dist-info"
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr(f"{dist_info}/METADATA", f"Metadata-Version: 2.3\nName: autodj\nVersion: {version}\n")


def test_matching_tag_project_changelog_and_wheel_pass(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text('[project]\nname="autodj"\nversion="1.2.3"\n', encoding="utf-8")
    (tmp_path / "CHANGELOG.md").write_text("## [1.2.3] - 2026-08-02\n", encoding="utf-8")
    wheel = tmp_path / "autodj-1.2.3-py3-none-any.whl"
    _wheel(wheel, "1.2.3")
    assert verify_release("v1.2.3", tmp_path / "pyproject.toml", tmp_path / "CHANGELOG.md", wheel) == "1.2.3"


@pytest.mark.parametrize(
    ("tag", "project", "heading", "wheel_version"),
    [
        ("v1.2.4", "1.2.3", "1.2.3", "1.2.3"),
        ("v1.2.3", "1.2.4", "1.2.3", "1.2.3"),
        ("v1.2.3", "1.2.3", "1.2.4", "1.2.3"),
        ("v1.2.3", "1.2.3", "1.2.3", "1.2.4"),
    ],
)
def test_any_identity_mismatch_fails(
    tmp_path: Path, tag: str, project: str, heading: str, wheel_version: str
) -> None:
    (tmp_path / "pyproject.toml").write_text(f'[project]\nname="autodj"\nversion="{project}"\n', encoding="utf-8")
    (tmp_path / "CHANGELOG.md").write_text(f"## [{heading}] - 2026-08-02\n", encoding="utf-8")
    wheel = tmp_path / "artifact.whl"
    _wheel(wheel, wheel_version)
    with pytest.raises(ReleaseVerificationError):
        verify_release(tag, tmp_path / "pyproject.toml", tmp_path / "CHANGELOG.md", wheel)


def test_release_must_be_latest_released_changelog_heading(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname="autodj"\nversion="1.2.3"\n', encoding="utf-8"
    )
    (tmp_path / "CHANGELOG.md").write_text(
        "## [Unreleased]\n## [1.2.4] - 2026-08-02\n## [1.2.3] - 2026-08-01\n",
        encoding="utf-8",
    )
    wheel = tmp_path / "autodj-1.2.3-py3-none-any.whl"
    _wheel(wheel, "1.2.3")
    with pytest.raises(ReleaseVerificationError, match="latest released CHANGELOG"):
        verify_release("v1.2.3", tmp_path / "pyproject.toml", tmp_path / "CHANGELOG.md", wheel)
```

- [ ] **Step 2: Run verifier tests and confirm the module is absent**

Run: `uv run pytest tests/unit/test_verify_release.py -q`

Expected: FAIL because `scripts.verify_release` does not exist.

- [ ] **Step 3: Implement metadata verification**

Create `scripts/verify_release.py`:

```python
from __future__ import annotations

import argparse
import email.parser
import re
import tomllib
import zipfile
from pathlib import Path


class ReleaseVerificationError(RuntimeError):
    """Raised when tag, source, changelog, and wheel identity disagree."""


def _wheel_version(wheel: Path) -> str:
    with zipfile.ZipFile(wheel) as zf:
        metadata_names = [name for name in zf.namelist() if name.endswith(".dist-info/METADATA")]
        if len(metadata_names) != 1:
            raise ReleaseVerificationError("wheel must contain exactly one METADATA file")
        message = email.parser.Parser().parsestr(zf.read(metadata_names[0]).decode("utf-8"))
    version = message.get("Version")
    if not version:
        raise ReleaseVerificationError("wheel METADATA has no Version")
    return version


def verify_release(tag: str, pyproject: Path, changelog: Path, wheel: Path) -> str:
    """Verify all release identity sources and return the common version."""

    if not tag.startswith("v"):
        raise ReleaseVerificationError(f"release tag must start with v: {tag}")
    tag_version = tag[1:]
    with pyproject.open("rb") as fh:
        project_version = str(tomllib.load(fh)["project"]["version"])
    changelog_text = changelog.read_text(encoding="utf-8")
    wheel_version = _wheel_version(wheel)
    values = {"tag": tag_version, "project": project_version, "wheel": wheel_version}
    if len(set(values.values())) != 1:
        raise ReleaseVerificationError(f"release versions differ: {values}")
    latest = re.search(
        r"^## \[(?!Unreleased\])([^]]+)\]",
        changelog_text,
        flags=re.IGNORECASE | re.MULTILINE,
    )
    if latest is None or latest.group(1) != tag_version:
        found = latest.group(1) if latest else "none"
        raise ReleaseVerificationError(
            f"latest released CHANGELOG heading is {found}; expected {tag_version}"
        )
    return tag_version


def main() -> int:
    """Validate one built wheel against the current release checkout."""

    parser = argparse.ArgumentParser()
    parser.add_argument("--tag", required=True)
    parser.add_argument("--wheel", type=Path, required=True)
    args = parser.parse_args()
    version = verify_release(args.tag, Path("pyproject.toml"), Path("CHANGELOG.md"), args.wheel)
    print(version)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
```

- [ ] **Step 4: Make CI/security reusable and replace release workflow**

Replace only the trigger maps in `ci.yml` and `security.yml` with these exact merged forms, retaining their existing branch, schedule, and manual triggers:

```yaml
# .github/workflows/ci.yml
on:
  push:
    branches: [main, master]
  pull_request:
    branches: [main, master]
  workflow_call:
```

```yaml
# .github/workflows/security.yml
on:
  pull_request:
  push:
    branches: [main, master]
  schedule:
    - cron: "0 7 * * *"
  workflow_dispatch:
  workflow_call:
```

Replace `release.yml` so tag publication reruns exact-commit CI/security, builds once, verifies/install-runs the exact wheel, queries the API, and attaches an artifact-specific SBOM:

```yaml
name: Release
on:
  push:
    tags: ["v*"]
permissions:
  contents: read

jobs:
  ci:
    uses: ./.github/workflows/ci.yml
    permissions:
      contents: read
      id-token: write
  security:
    uses: ./.github/workflows/security.yml
    permissions:
      contents: read

  build-and-verify:
    needs: [ci, security]
    runs-on: ubuntu-latest
    permissions:
      contents: read
    steps:
      - uses: actions/checkout@v6
        with: {fetch-depth: 0}
      - uses: astral-sh/setup-uv@v8.1.0
      - uses: actions/setup-python@v6
        with: {python-version: "3.14"}
      - run: uv build --sdist --wheel
      - name: Verify tag, project, changelog, and wheel metadata
        run: uv run python scripts/verify_release.py --tag "$GITHUB_REF_NAME" --wheel dist/*.whl
      - name: Smoke-install exact wheel
        run: |
          uv venv .release-venv --python 3.14
          uv pip install --python .release-venv/bin/python dist/*.whl
          test "$(.release-venv/bin/python -c 'import autodj; print(autodj.__version__)')" = "${GITHUB_REF_NAME#v}"
      - name: Verify runtime API version
        run: |
          mkdir -p "$RUNNER_TEMP/music" "$RUNNER_TEMP/index" "$RUNNER_TEMP/models"
          AUTODJ_LIBRARY_MUSIC_DIR="$RUNNER_TEMP/music" AUTODJ_INDEX_DIR="$RUNNER_TEMP/index" AUTODJ_MODEL_DIR="$RUNNER_TEMP/models" \
            .release-venv/bin/autodj serve --no-playback >"$RUNNER_TEMP/release-server.log" 2>&1 &
          response="$(curl --fail --retry 30 --retry-delay 1 --retry-connrefused http://127.0.0.1:8080/api/version)"
          test "$(jq -r .version <<<"$response")" = "${GITHUB_REF_NAME#v}"
      - uses: anchore/sbom-action@v0
        with:
          path: dist
          format: cyclonedx-json
          output-file: dist/sbom.cdx.json
      - uses: actions/upload-artifact@v7
        with:
          name: release-artifacts
          path: dist/

  publish:
    needs: build-and-verify
    runs-on: ubuntu-latest
    permissions:
      contents: write
      id-token: write
    steps:
      - uses: actions/checkout@v6
      - uses: actions/download-artifact@v8
        with: {name: release-artifacts, path: dist}
      - uses: sigstore/cosign-installer@v4.1.1
      - name: Sign exact artifacts
        run: |
          for file in dist/*; do cosign sign-blob --yes --bundle "${file}.bundle" "$file"; done
      - name: Publish GitHub release
        env:
          GH_TOKEN: ${{ secrets.GITHUB_TOKEN }}
        run: gh release create "$GITHUB_REF_NAME" --verify-tag --generate-notes dist/*
```

Run: `uv run pytest tests/unit/test_verify_release.py tests/unit/test_version.py -q && uv build && uv run python scripts/verify_release.py --tag v0.15.0 --wheel dist/*.whl`

Expected: PASS and print `0.15.0`.

- [ ] **Step 5: Commit release enforcement**

```bash
git add scripts/verify_release.py tests/unit/test_verify_release.py .github/workflows/ci.yml .github/workflows/security.yml .github/workflows/release.yml
git commit -m "build: verify exact release identity"
```

### Task 12: Authoritative operations and maintenance documentation

**Files:**
- Create: `docs/operations.md`
- Modify: `README.md:21-58,116-156,184-210`
- Modify: `SECURITY.md:3-11,30-43`
- Modify: `THREAT_MODEL.md:3-76`
- Modify: `CONTRIBUTING.md:5-24,43-51`
- Modify: `CHANGELOG.md:1-9`

- [ ] **Step 1: Write documentation assertions**

Extend `tests/unit/test_config_examples.py`:

```python
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
    assert "0.15.x" in security
    assert "AUTODJ_ACCESS_TOKEN" in threat
    assert "SQLite online backup" in operations
    assert "tracks.db-wal" in operations
    assert "container-internal `0.0.0.0`" in operations
    assert "host `127.0.0.1`" in operations
    assert "Windows PowerShell" in operations
    assert "Get-Date -Format yyyy-MM-dd" in operations
```

- [ ] **Step 2: Run the documentation contract and verify it fails**

Run: `uv run pytest tests/unit/test_config_examples.py::test_operator_docs_cover_reproducible_workflows -q`

Expected: FAIL because operational commands/procedures and authoritative thresholds are not documented and `docs/operations.md` is absent.

- [ ] **Step 3: Write exact setup, diagnosis, backup, and restore procedures**

Create `docs/operations.md` with these sections and commands:

````markdown
# AutoDJ operations

Commands labeled Bash require a Linux host or WSL2. Native Windows operators should use the
PowerShell equivalents below; run Linux container ownership/smoke commands inside WSL2 with the
repository on a WSL filesystem so UID 10001 and POSIX modes have their documented meaning.

## Configuration precedence

AutoDJ resolves defaults, `config.toml`, sibling `config.local.toml`, environment, then explicit CLI flags. Omitting `--config` is valid; explicitly naming a missing file is an error. Put access tokens only in ignored local config or `AUTODJ_ACCESS_TOKEN`.

## Diagnose before serving

Run `uv run autodj doctor`. Use `uv run autodj doctor --json` for automation. A required failed check returns exit 1. Doctor never writes the index and redacts both server and Hugging Face tokens.

## Container ownership and exposure

Create bind sources before startup:

```bash
# Linux/WSL2 Bash
mkdir -p music index models
sudo chown 10001:10001 music index models
chmod 0755 music index models
docker compose up --build
```

The default process listens on container-internal `0.0.0.0` so Docker networking can reach it, and
the Compose `--insecure-lan` flag acknowledges only that internal wildcard bind. Compose publishes
the port solely on host `127.0.0.1` (`127.0.0.1:8080:8080`), so the default is not a host LAN
exposure. Start the authenticated LAN service explicitly, substituting the DNS name clients
actually use:

```bash
AUTODJ_ACCESS_TOKEN="$(openssl rand -hex 32)" \
AUTODJ_LAN_HOST=radio.local \
AUTODJ_LAN_ORIGIN=http://radio.local:8080 \
docker compose --profile lan up autodj-lan
```

Store the generated token in a secret manager before startup. Do not publish the loopback service directly to the internet.

## Windows PowerShell setup

For native no-container operation, create paths and run diagnostics as follows:

```powershell
New-Item -ItemType Directory -Force music, index, models, backups | Out-Null
uv run autodj doctor
$stamp = Get-Date -Format yyyy-MM-dd
uv run autodj backup "backups\autodj-$stamp.zip"
```

Generate an authenticated LAN token without placing it on a command line:

```powershell
$tokenBytes = [byte[]]::new(32)
[Security.Cryptography.RandomNumberGenerator]::Fill($tokenBytes)
$env:AUTODJ_ACCESS_TOKEN = [Convert]::ToHexString($tokenBytes).ToLowerInvariant()
$env:AUTODJ_LAN_HOST = "radio.local"
$env:AUTODJ_LAN_ORIGIN = "http://radio.local:8080"
docker compose --profile lan up autodj-lan
```

Docker Desktop bind-mount ownership depends on its WSL2/Linux filesystem mapping. Run
`bash scripts/container_smoke.sh` inside WSL2 for the authoritative UID/mode gate; do not replace
the 0755/UID 10001 contract with world-writable Windows mounts.

## Backup classifications

Re-derivable data: `vectors.index`, `tracks.db`, and `dj_meta.db`. Unique data: profiles, liners, dayparts, optional history, and `web_state.json`. A full archive contains both and labels every item in `manifest.json`.

## Stopped-service backup

```bash
# Linux/WSL2 Bash
docker compose down
uv run autodj backup backups/autodj-$(date +%F).zip
```

Stopped mode refuses `tracks.db-wal`, `tracks.db-shm`, `dj_meta.db-wal`, or `dj_meta.db-shm`; do not copy a live SQLite main file by itself.
It also refuses SQLite rollback journals and rechecks sidecars after copying. These checks can
detect activity but cannot prove the process is stopped; stopping the service is the operator
contract. Backup refuses an existing destination unless `--force` is explicitly supplied.

## SQLite online backup

```bash
# Linux/WSL2 Bash
uv run autodj backup --online backups/autodj-live-$(date +%F).zip
```

SQLite online backup includes committed metadata WAL state consistently while serving and archives
one security-published index generation. It retries a bounded generation race and refuses continuous
index churn instead of mixing generations.

## Restore and validate

```bash
# Linux/WSL2 Bash
docker compose down
uv run autodj restore --force backups/autodj-2026-08-02.zip
uv run autodj doctor
docker compose up
```

Restore refuses unknown archive schema versions and existing destinations without `--force`. It
rejects encrypted, non-regular, unsafe, or symlink-derived content; preflights declared sizes and
target-filesystem free space; checks every member size/digest; and stages every payload before
replacing any target. An install failure rolls prior targets back. Cleanup warnings after a
successful install name any retained recovery files and do not mean rollback occurred. Do not serve
until doctor exits 0. Keep an untouched archive until playback and profile/liner inventory are
confirmed.

Native PowerShell stopped backup and restore equivalents are:

```powershell
docker compose down
$stamp = Get-Date -Format yyyy-MM-dd
uv run autodj backup "backups\autodj-$stamp.zip"
uv run autodj restore --force "backups\autodj-$stamp.zip"
uv run autodj doctor
docker compose up
```

## Upgrade checklist

1. Create and retain a backup.
2. Run `uv sync --frozen --all-extras` and `npm ci` from committed locks.
3. Run `uv run autodj doctor`.
4. Run Python, frontend, and container gates from `CONTRIBUTING.md`.
5. Start loopback-only and verify `/api/version` before enabling LAN access.
````

Update README quickstarts to show no-config defaults, explicit config behavior, `npm ci`, unprivileged bind-mount ownership, doctor, and the operations link. Update SECURITY to supported `0.15.x`; update THREAT_MODEL with environment token/authenticated LAN, request/audit behavior from the security plan, image scanning, and backup boundaries. Update CONTRIBUTING to the actual 99.1% line/94.7% branch gates and list `npm ci`, Playwright Chromium, doctor, and container smoke commands. Add an Unreleased changelog entry summarizing these operator-visible changes; preserve historical contributor/AI credit.

- [ ] **Step 4: Run documentation, link, and help checks**

Run:

```bash
uv run pytest tests/unit/test_config_examples.py tests/smoke/test_cli_smoke.py -q
uv run autodj --help
uv run autodj doctor --help
uv run autodj backup --help
uv run autodj restore --help
```

Expected: PASS; root help lists doctor/backup/restore and every documented flag exists.

- [ ] **Step 5: Commit operator documentation**

```bash
git add docs/operations.md README.md SECURITY.md THREAT_MODEL.md CONTRIBUTING.md CHANGELOG.md tests/unit/test_config_examples.py
git commit -m "docs: add delivery and recovery operations"
```

### Task 13: Full lock-to-release verification

**Files:**
- Verify only; modify a focused file only if a gate identifies a concrete defect in a preceding task.

- [ ] **Step 1: Verify repository and lock consistency**

Run in Bash or PowerShell:

```bash
git status --short
uv lock --check
npm ci --ignore-scripts
npm audit --audit-level=high
```

Expected: only intentional plan implementation changes before final commits; lock check and npm audit PASS. Confirm `git ls-files package-lock.json` prints `package-lock.json`.

- [ ] **Step 2: Run all Python static and security gates**

Run in Bash or PowerShell:

```bash
uv run ruff check src tests scripts
uv run ruff format --check src tests scripts
uv run mypy src/autodj
uv run pyright src/autodj
uv run bandit -r src -c pyproject.toml
uv run deptry src/autodj
uv run vulture
uv run python scripts/check_coverage_policy.py
```

Expected: every command exits 0; Pyright is no longer advisory.

- [ ] **Step 3: Run all Python and frontend tests**

Run in Bash or PowerShell:

```bash
uv run python scripts/ci_pytest.py
npm run lint
npm test
npm run build
npm run deadcode
```

Expected: PASS, coverage reports at least 99.1% lines and 94.7% branches, ESLint emits zero warnings, and build produces `build-info.json` matching `pyproject.toml`.

- [ ] **Step 4: Run browser, container, doctor, backup, and release smoke checks**

Run on Linux or in WSL2 Bash:

```bash
mkdir -p tmp/final-music tmp/final-index tmp/final-models
AUTODJ_LIBRARY_MUSIC_DIR="$PWD/tmp/final-music" \
AUTODJ_INDEX_DIR="$PWD/tmp/final-index" \
AUTODJ_MODEL_DIR="$PWD/tmp/final-models" \
uv run autodj serve --no-playback >tmp/final-server.log 2>&1 &
AUTODJ_FINAL_PID=$!
trap 'kill "$AUTODJ_FINAL_PID" 2>/dev/null || true' EXIT
curl --fail --retry 30 --retry-delay 1 --retry-connrefused http://127.0.0.1:8080/healthz
AUTODJ_BROWSERS=chromium npm run audit:ci
kill "$AUTODJ_FINAL_PID"
wait "$AUTODJ_FINAL_PID" || true
trap - EXIT
bash scripts/container_smoke.sh
AUTODJ_LIBRARY_MUSIC_DIR="$PWD/tmp/final-music" \
AUTODJ_INDEX_DIR="$PWD/tmp/final-index" \
AUTODJ_MODEL_DIR="$PWD/tmp/final-models" \
uv run autodj doctor --json
AUTODJ_LIBRARY_MUSIC_DIR="$PWD/tmp/final-music" \
AUTODJ_INDEX_DIR="$PWD/tmp/final-index" \
AUTODJ_MODEL_DIR="$PWD/tmp/final-models" \
uv run autodj backup --online --force tmp/final-verification.zip
uv build
uv run python scripts/verify_release.py --tag v0.15.0 --wheel dist/*.whl
```

Run the equivalent native portions in Windows PowerShell; the container gate remains the WSL2
command explicitly shown below:

```powershell
New-Item -ItemType Directory -Force tmp/final-music, tmp/final-index, tmp/final-models | Out-Null
$env:AUTODJ_LIBRARY_MUSIC_DIR = (Resolve-Path tmp/final-music).Path
$env:AUTODJ_INDEX_DIR = (Resolve-Path tmp/final-index).Path
$env:AUTODJ_MODEL_DIR = (Resolve-Path tmp/final-models).Path
$server = Start-Process -FilePath "uv" `
    -ArgumentList "run", "autodj", "serve", "--no-playback" `
    -RedirectStandardOutput "tmp/final-server.log" `
    -RedirectStandardError "tmp/final-server-error.log" `
    -WindowStyle Hidden -PassThru
try {
    curl.exe --fail --retry 30 --retry-delay 1 --retry-connrefused `
        http://127.0.0.1:8080/healthz
    $env:AUTODJ_BROWSERS = "chromium"
    npm run audit:ci
} finally {
    Stop-Process -Id $server.Id -ErrorAction SilentlyContinue
    $server.WaitForExit()
}
wsl.exe --cd . bash -lc "bash scripts/container_smoke.sh"
uv run autodj doctor --json
uv run autodj backup --online --force tmp/final-verification.zip
uv build
$wheel = (Get-ChildItem dist -Filter *.whl -File).FullName
uv run python scripts/verify_release.py --tag v0.15.0 --wheel $wheel
```

Expected: all commands exit 0; the doctor JSON contains no token values, the archive contains schema 1 manifest, container runs UID/GID 10001 on loopback, and release verifier prints `0.15.0`.

- [ ] **Step 5: Inspect history and preserve a clean handoff**

If a gate failed, return to the task that owns that gate, add a named regression test there, and repeat that task's red/green/commit steps before rerunning Task 13. Do not create an undifferentiated final-fixes commit. When every gate passes, run:

The following commands are identical in Bash and PowerShell:

```bash
git log --oneline --decorate -12
git status --short
```

Expected: one focused commit for each implementation Task 1-12, no unrelated files, no generated
`dist`, `static_dist`, coverage, audit-report, temp backup, or local config files staged, and the
committed `CLAUDE.md` removal from `d245c9d` remains intact.
