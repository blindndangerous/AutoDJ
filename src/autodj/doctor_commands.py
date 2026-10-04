"""Run doctor's explicit recovery actions with consent and fresh diagnostics."""

from __future__ import annotations

import subprocess  # nosec B404 -- explicit argv, no shell or parsed error-message commands
import sys
from typing import TYPE_CHECKING

import click

if TYPE_CHECKING:
    from autodj.config import AutoDJConfig
    from autodj.doctor import DoctorReport


# Commands are defined here, never extracted from exception messages or file contents.
_COMMANDS = {
    "rebuild-index": ("index", "--force"),
    "index": ("index",),
    "rebuild-dj-meta": ("analyse",),
    "analyse": ("analyse",),
    "serve-lan": ("serve", "--lan"),
}
_EFFECTS = {
    "rebuild-index": "Re-embeds the full library from scratch; may download models and take a long time.",
    "index": "Indexes the library and downloads missing models; this may take a long time.",
    "rebuild-dj-meta": "Stop AutoDJ serving/indexing first. Backs up the DJ metadata cache and rebuilds it.",
    "analyse": "Analyses indexed audio to fill missing DJ metadata; this may take a long time.",
    "serve-lan": "Starts the server on your network until you stop it; does not change your saved config.",
}


def run_recovery_commands(
    cfg: AutoDJConfig, report: DoctorReport, *, auto_fix: bool
) -> tuple[DoctorReport, bool]:
    """Offer each recovery once, retaining config and interpreter; recheck after execution."""
    from autodj.doctor import CheckStatus, render_text, run_doctor
    from autodj.doctor_cache import backup_dj_meta_cache

    attempted: set[str] = set()
    failed = False
    while True:
        recommended = {
            check.repair for check in report.checks if check.status is not CheckStatus.PASS
        }
        action = next(
            (name for name in _COMMANDS if name in recommended and name not in attempted), None
        )
        if action is None:
            return report, failed
        # A forced rebuild also covers ordinary indexing/model downloads. A
        # metadata rebuild covers the ordinary analysis recommendation.
        attempted.add(action)
        if action == "rebuild-index":
            attempted.add("index")
        elif action == "rebuild-dj-meta":
            attempted.add("analyse")
        args: list[str] = []
        if cfg.config_path is not None:
            args.extend(("--config", str(cfg.config_path)))
        args.extend(_COMMANDS[action])
        args.extend(("--name", cfg.index.name))
        display = "autodj " + subprocess.list2cmdline(args)
        click.echo(f"Suggested recovery: {display}")
        click.echo(_EFFECTS[action])
        if not auto_fix and not click.confirm("Run this command now?", default=False):
            continue
        try:
            if action == "rebuild-dj-meta":
                backup = backup_dj_meta_cache(cfg.index.active_dir)
                if backup is not None:
                    click.echo(f"DJ metadata backup: {backup}")
            # Inherit the terminal for progress and Ctrl+C; no shell interprets paths.
            result = subprocess.run(  # nosec B603 -- allowlisted command, argv only
                [sys.executable, "-m", "autodj", *args], check=False
            )
            if result.returncode:
                click.echo(
                    f"Recovery command failed (exit {result.returncode}): {display}", err=True
                )
                failed = True
        except (OSError, ValueError) as exc:
            click.echo(f"Could not run recovery: {exc}", err=True)
            failed = True
        click.echo("Rechecking after recovery...")
        report = run_doctor(cfg)
        click.echo(render_text(report))
