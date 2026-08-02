"""Root conftest — mocks hardware-dependent libraries so tests run anywhere.

sounddevice requires PortAudio to be installed at the OS level. We mock it at
the sys.modules level before any test module imports autodj.player, so the
import succeeds without audio hardware.
"""

import sys
from unittest.mock import MagicMock

import pytest

# Mock sounddevice before any module-level import can trigger PortAudio lookup
if "sounddevice" not in sys.modules:
    sys.modules["sounddevice"] = MagicMock()


@pytest.fixture(autouse=True)
def _close_global_dj_cache_between_tests():
    from autodj.dj_meta import close_cache

    close_cache()
    yield
    close_cache()
