"""Root conftest — mocks hardware-dependent libraries so tests run anywhere.

sounddevice requires PortAudio to be installed at the OS level. We mock it at
the sys.modules level before any test module imports autodj.player, so the
import succeeds without audio hardware.
"""

import os
import sys
from unittest.mock import MagicMock

import pytest

# Mock sounddevice before any module-level import can trigger PortAudio lookup
if "sounddevice" not in sys.modules:
    sys.modules["sounddevice"] = MagicMock()


@pytest.fixture(autouse=True)
def _no_hook_git_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep every test's git calls in its own temporary repo.

    A git hook (pre-push runs this suite) exports GIT_DIR, GIT_INDEX_FILE and
    friends. Inherited, they make ``git init`` and ``git add`` in tmp_path act on
    the real repository: its index fills with fixture files and core.bare turns on.
    Clearing them for all tests, not per file, covers tests added later too.
    """
    for name in list(os.environ):
        if name.startswith("GIT_"):
            monkeypatch.delenv(name)


@pytest.fixture(autouse=True)
def _no_update_check_network(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep Doctor's and the web page's update check off the network.

    Tests of the check itself remove this variable and pass stand-ins for
    git and GitHub.
    """
    monkeypatch.setenv("AUTODJ_UPDATE_CHECK", "0")


@pytest.fixture(autouse=True)
def _close_global_dj_cache_between_tests():
    from autodj.dj_meta import close_cache

    close_cache()
    yield
    close_cache()


@pytest.fixture(autouse=True)
def _no_scan_dj_analysis(request, monkeypatch):
    """Scans in tests skip the beat and cue analysis, unless marked ``dj_analysis``.

    Test songs are not real audio, so decoding them would fail.
    """
    if request.node.get_closest_marker("dj_analysis"):
        return
    from autodj.library import LibraryManager

    monkeypatch.setattr(LibraryManager, "dj_analysis_enabled", lambda self: False)
