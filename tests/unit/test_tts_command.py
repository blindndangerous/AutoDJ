"""Cancellation, timeout and bounded output of the shared operator command runner."""

import subprocess
import sys
import threading
from unittest.mock import patch

import pytest

from autodj.tts_command import run_command


def test_timeout_cancel_and_output_limit_reap_the_command(tmp_path):
    output = tmp_path / "voice.wav"
    command = [sys.executable, "-c", "import time; time.sleep(30)", "{out}"]
    with pytest.raises(subprocess.TimeoutExpired):
        run_command(command, "hello", output, timeout=0.1)
    cancel = threading.Event()
    timer = threading.Timer(0.1, cancel.set)
    timer.start()
    try:
        with pytest.raises(ValueError, match="cancelled"):
            run_command(command, "hello", output, cancel=cancel)
    finally:
        timer.join()
    with (
        patch("autodj.tts_command.subprocess.Popen", side_effect=AssertionError("must not spawn")),
        pytest.raises(ValueError, match="cancelled"),
    ):
        run_command([sys.executable, "-c", "raise SystemExit(99)"], "hello", output, cancel=cancel)
    command = [
        sys.executable,
        "-c",
        "import sys,time; open(sys.argv[1],'wb').write(b'x'*200); time.sleep(30)",
        "{out}",
    ]
    with pytest.raises(ValueError, match="size limit"):
        run_command(command, "hello", output, max_bytes=100)
