"""Root conftest — mocks hardware-dependent libraries so tests run anywhere.

sounddevice requires PortAudio to be installed at the OS level. We mock it at
the sys.modules level before any test module imports autodj.player, so the
import succeeds without audio hardware.
"""

import atexit
import os
import shutil
import sys
import tempfile
from unittest.mock import MagicMock

import pytest


def _private_numba_cache() -> str:
    """Give this test process a numba cache of its own, empty at the start.

    librosa and ``autodj.kernels`` compile with ``cache=True``.  xdist workers
    sharing one cache, the default beside the installed sources or a shared
    ``NUMBA_CACHE_DIR``, race on its index files and leave entries that make a
    later load segfault (#558).  One fresh directory per process means no two
    processes write the same cache and no run reads another run's entries.  It
    has to be set before numba is imported, so it is set here, at conftest
    import; tests' subprocesses inherit it.
    """
    worker = os.environ.get("PYTEST_XDIST_WORKER", "main")
    folder = tempfile.mkdtemp(prefix=f"autodj-numba-{worker}-")
    atexit.register(shutil.rmtree, folder, ignore_errors=True)
    os.environ["NUMBA_CACHE_DIR"] = folder
    numba = sys.modules.get("numba")
    if numba is not None:  # imported early by a plugin: point its config here too
        numba.config.CACHE_DIR = folder
    return folder


_private_numba_cache()

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
