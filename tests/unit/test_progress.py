"""Terminal progress redraws; redirected progress stays plain and bounded."""

import io
import logging
from unittest.mock import Mock, patch

import pytest
from rich.console import Console
from rich.logging import RichHandler
from rich.progress import Progress

from autodj.progress import TrackProgress


def test_redirected_progress_keeps_log_cadence() -> None:
    logger = Mock()
    output = io.StringIO()
    with patch("sys.stderr", output), TrackProgress("Indexing", 27, logger) as progress:
        for done in range(1, 28):
            progress.update(done)
    assert logger.info.call_count == 2
    assert logger.info.call_args_list[0].args[-2:] == (25, 27)
    assert logger.info.call_args_list[1].args[-2:] == (27, 27)
    assert "\x1b" not in output.getvalue()


@pytest.mark.parametrize("interrupt", [False, True])
def test_terminal_refreshes_each_track_and_cleans_up(interrupt: bool, monkeypatch) -> None:
    monkeypatch.setenv("TERM", "xterm")
    output = io.StringIO()
    console = Console(file=output, force_terminal=True, color_system=None, width=80)
    stream = Mock()
    stream.isatty.return_value = True
    with (
        patch("sys.stderr", stream),
        patch("autodj.progress.get_progress_console", return_value=console),
        patch("autodj.progress.Progress", wraps=Progress) as progress_type,
    ):
        try:
            with TrackProgress("Indexing", 3, logging.getLogger(__name__)) as progress:
                progress.update(1)
                first = output.getvalue()
                progress.update(2)
                second = output.getvalue()
                if interrupt:
                    raise KeyboardInterrupt
                progress.update(3)
        except KeyboardInterrupt:
            pass
    assert "Indexing" in first
    assert len(second) > len(first)
    assert "\r" in second or "\x1b[" in second
    assert output.getvalue().endswith("\n") or "\x1b[?25h" in output.getvalue()
    assert console._live_stack == []
    assert progress_type.call_args.kwargs["auto_refresh"] is False


def test_rich_warning_shares_progress_console_and_updates_continue(monkeypatch) -> None:
    monkeypatch.setenv("TERM", "xterm")
    output = io.StringIO()
    console = Console(file=output, force_terminal=True, color_system=None, width=100)
    stream = Mock()
    stream.isatty.return_value = True
    logger = logging.getLogger("test.progress.decode-warning")
    handler = RichHandler(console=console, show_path=False, show_time=False)
    logger.addHandler(handler)
    logger.setLevel(logging.WARNING)
    try:
        with (
            patch("sys.stderr", stream),
            patch("autodj.progress.get_progress_console", return_value=console),
            TrackProgress("Indexing", 3, logger) as progress,
        ):
            progress.update(1)
            logger.warning("Audio decode warning for track.mp3:\nrecovered frame damage")
            after_warning = output.getvalue()
            progress.update(2)
            progress.update(3)
    finally:
        logger.removeHandler(handler)
        handler.close()

    rendered = output.getvalue()
    assert "track.mp3" in after_warning
    assert "recovered frame damage" in after_warning
    assert "3/3" in rendered
    assert len(rendered) > len(after_warning)
    assert console._live_stack == []
