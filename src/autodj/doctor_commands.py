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
    "sync-packages": ("setup", "--yes"),
    "migrate-index": ("migrate-index", "--apply"),
    "index": ("index",),
    "rebuild-dj-meta": ("analyse",),
    "analyse": ("analyse",),
    "serve-lan": ("serve", "--lan"),
    "download-model": ("download-model",),
}
_EFFECTS = {
    "sync-packages": "Installs the Python packages this AutoDJ version needs into its environment, and may download packages.",
    "migrate-index": "Stop AutoDJ serving/indexing first. Saves a recovery copy and imports older library data without re-embedding.",
    "index": "Indexes the library and downloads missing models; this may take a long time.",
    "rebuild-dj-meta": "Stop AutoDJ serving/indexing first. Backs up the DJ metadata cache and rebuilds it.",
    "analyse": "Analyses indexed audio to fill missing DJ metadata; this may take a long time.",
    "serve-lan": "Starts the server on your network until you stop it; does not change your saved config.",
    "download-model": "Downloads or validates the configured model cache without analyzing songs.",
}


def run_recovery_commands(
    cfg: AutoDJConfig, report: DoctorReport, *, auto_fix: bool, station: str | None = None
) -> tuple[DoctorReport, bool]:
    """Offer each recovery once, retaining config and interpreter; recheck after execution."""
    from autodj.doctor import CheckStatus, render_text, run_doctor
    from autodj.doctor_cache import backup_dj_meta_cache
    from autodj.host_tools import ensure_ffmpeg

    attempted: set[str] = set()
    failed = False
    if any(check.repair == "install-ffmpeg" for check in report.checks):
        try:
            if ensure_ffmpeg(auto_install=auto_fix):
                report = run_doctor(cfg, station=station, check_updates=False)
                click.echo(render_text(report))
        except (OSError, RuntimeError) as exc:
            click.echo(f"FFmpeg installation failed: {exc}", err=True)
            failed = True
    while True:
        recommended = {
            check.repair for check in report.checks if check.status is not CheckStatus.PASS
        }
        action = next(
            (name for name in _COMMANDS if name in recommended and name not in attempted), None
        )
        if action is None:
            return report, failed
        attempted.add(action)
        if action == "rebuild-dj-meta":
            attempted.add("analyse")
        args: list[str] = []
        if action == "sync-packages":
            from autodj.runtime_packages import (
                missing_runtime_packages,
                package_repair,
                runtime_selection,
            )

            setup_args, note, runnable = package_repair()
            if note:
                click.echo(note)
            selection = runtime_selection()
            if auto_fix and selection and selection[1]:
                if not any(p.installed is None for p in missing_runtime_packages()):
                    runnable = False
                    click.echo(
                        "--fix does not take ownership of an adopted environment unless a required core package is missing."
                    )
                else:
                    click.echo(
                        "--fix is taking ownership of this adopted environment to install missing required packages."
                    )
            args = ["setup", *setup_args]
        elif cfg.config_path is not None:
            args.extend(("--config", str(cfg.config_path)))
        if action != "sync-packages":
            args.extend(_COMMANDS[action])
        display = "autodj " + subprocess.list2cmdline(args)
        click.echo(f"Suggested recovery: {display}")
        click.echo(_EFFECTS[action])
        if action == "sync-packages" and not runnable:
            failed = failed or auto_fix
            continue
        if not auto_fix and not click.confirm("Run this command now?", default=False):
            continue
        try:
            if action == "rebuild-dj-meta":
                backup = backup_dj_meta_cache(cfg.index.library_dir)
                if backup is not None:
                    click.echo(f"DJ metadata backup: {backup}")
            # Inherit the terminal for progress and Ctrl+C; no shell interprets paths.
            if action == "sync-packages":
                from autodj.launcher import run_setup

                returncode = run_setup(setup_args)
            else:
                result = subprocess.run(  # nosec B603 -- allowlisted command, argv only
                    [sys.executable, "-m", "autodj", *args], check=False
                )
                returncode = result.returncode
            if returncode:
                click.echo(f"Recovery command failed (exit {returncode}): {display}", err=True)
                failed = True
        except (OSError, ValueError) as exc:
            click.echo(f"Could not run recovery: {exc}", err=True)
            failed = True
        click.echo("Rechecking after recovery...")
        report = run_doctor(cfg, station=station, check_updates=False)
        click.echo(render_text(report))
