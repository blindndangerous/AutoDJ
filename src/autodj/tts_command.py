"""Execute the operator-configured TTS argv contract, never through a shell."""

from __future__ import annotations

import os
import signal
import subprocess  # nosec B404
import sys
import tempfile
import threading
import time
from contextlib import suppress
from pathlib import Path
from typing import IO

from autodj.child_jobs import bind_to_autodj

TTS_TIMEOUT_S = 20


def run_command(
    command: list[str],
    text: str,
    output: Path,
    *,
    timeout: float = TTS_TIMEOUT_S,
    cancel: threading.Event | None = None,
    max_bytes: int | None = None,
) -> int:
    """Send plain text on stdin and substitute {text}/{out}; reap on every exit."""
    if cancel is not None and cancel.is_set():
        raise ValueError("Generation cancelled")
    argv = [part.replace("{out}", str(output)).replace("{text}", text) for part in command]
    with tempfile.TemporaryFile(dir=output.parent) as source:
        source.write(text.encode())
        source.seek(0)
        return _run(argv, source, output, timeout, cancel, max_bytes)


def _run(
    argv: list[str],
    source: IO[bytes],
    output: Path,
    timeout: float,
    cancel: threading.Event | None,
    max_bytes: int | None,
) -> int:
    with subprocess.Popen(  # nosec B603 -- operator-configured argv, no shell
        argv,
        stdin=source,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=os.name != "nt",
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    ) as child:
        deadline = time.monotonic() + timeout
        try:
            bind_to_autodj(child)
            while True:
                if cancel is not None and cancel.is_set():
                    raise ValueError("Generation cancelled")
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise subprocess.TimeoutExpired("TTS command", timeout)
                if max_bytes is not None and output.exists() and output.stat().st_size > max_bytes:
                    raise ValueError("TTS command output exceeds the audio size limit")
                try:
                    child.communicate(timeout=min(0.1, remaining))
                    return child.returncode
                except subprocess.TimeoutExpired:
                    continue
        finally:
            if sys.platform != "win32":
                with suppress(ProcessLookupError):
                    os.killpg(child.pid, signal.SIGKILL)
            elif child.poll() is None:
                # The server-wide Windows job survives this request; stop its live tree too.
                with suppress(OSError, subprocess.TimeoutExpired):
                    subprocess.run(  # nosec B603 B607 -- fixed OS tool, numeric PID
                        ["taskkill", "/PID", str(child.pid), "/T", "/F"],
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                        timeout=2,
                        check=False,
                        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                    )
                if child.poll() is None:
                    child.kill()
            child.communicate()
