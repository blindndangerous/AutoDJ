"""Windows: tie mastering child processes to AutoDJ's own life.

Stereo Tool and FFmpeg run as child processes.  On a normal shutdown
AutoDJ stops them itself, but if AutoDJ is killed outright (Task Manager,
a crash) Windows would leave them running. So every mastering child is put in one Job Object
created with ``JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE``: when AutoDJ's process
ends for any reason, Windows closes the job's last handle and kills every
process in it.

On other systems this does nothing; there, AutoDJ stops its children on a
normal shutdown and at interpreter exit (:mod:`autodj.mastering`).
"""

from __future__ import annotations

import logging
import subprocess  # nosec B404 -- only handles of processes started elsewhere
import sys
import threading

logger = logging.getLogger(__name__)

_lock = threading.Lock()
_job: int | None = None
_unavailable = False


if sys.platform == "win32":
    import ctypes
    from ctypes import wintypes

    _JOB_OBJECT_EXTENDED_LIMIT_INFORMATION_CLASS = 9
    _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x2000

    class _BasicLimits(ctypes.Structure):
        _fields_ = [
            ("PerProcessUserTimeLimit", ctypes.c_int64),
            ("PerJobUserTimeLimit", ctypes.c_int64),
            ("LimitFlags", wintypes.DWORD),
            ("MinimumWorkingSetSize", ctypes.c_size_t),
            ("MaximumWorkingSetSize", ctypes.c_size_t),
            ("ActiveProcessLimit", wintypes.DWORD),
            ("Affinity", ctypes.c_size_t),
            ("PriorityClass", wintypes.DWORD),
            ("SchedulingClass", wintypes.DWORD),
        ]

    class _IoCounters(ctypes.Structure):
        _fields_ = [
            (name, ctypes.c_uint64)
            for name in (
                "ReadOperationCount",
                "WriteOperationCount",
                "OtherOperationCount",
                "ReadTransferCount",
                "WriteTransferCount",
                "OtherTransferCount",
            )
        ]

    class _ExtendedLimits(ctypes.Structure):
        _fields_ = [
            ("BasicLimitInformation", _BasicLimits),
            ("IoInfo", _IoCounters),
            ("ProcessMemoryLimit", ctypes.c_size_t),
            ("JobMemoryLimit", ctypes.c_size_t),
            ("PeakProcessMemoryUsed", ctypes.c_size_t),
            ("PeakJobMemoryUsed", ctypes.c_size_t),
        ]

    def _kernel32() -> ctypes.WinDLL:
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CreateJobObjectW.restype = wintypes.HANDLE
        kernel32.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
        kernel32.SetInformationJobObject.restype = wintypes.BOOL
        kernel32.SetInformationJobObject.argtypes = [
            wintypes.HANDLE,
            ctypes.c_int,
            ctypes.c_void_p,
            wintypes.DWORD,
        ]
        kernel32.AssignProcessToJobObject.restype = wintypes.BOOL
        kernel32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
        return kernel32

    def _create_job() -> int:
        kernel32 = _kernel32()
        job = kernel32.CreateJobObjectW(None, None)
        if not job:
            raise ctypes.WinError(ctypes.get_last_error())
        limits = _ExtendedLimits()
        limits.BasicLimitInformation.LimitFlags = _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if not kernel32.SetInformationJobObject(
            job,
            _JOB_OBJECT_EXTENDED_LIMIT_INFORMATION_CLASS,
            ctypes.byref(limits),
            ctypes.sizeof(limits),
        ):
            raise ctypes.WinError(ctypes.get_last_error())
        # Never closed: the handle must live as long as AutoDJ does, and
        # Windows closes it, killing the job's processes, when AutoDJ ends.
        return int(job)

    def _assign(job: int, proc: subprocess.Popen[bytes]) -> None:
        handle = proc._handle  # type: ignore[attr-defined]
        if not _kernel32().AssignProcessToJobObject(job, int(handle)):
            raise ctypes.WinError(ctypes.get_last_error())


else:  # pragma: no cover -- other systems never call these

    def _create_job() -> int:
        raise OSError("job objects are Windows only")

    def _assign(job: int, proc: subprocess.Popen[bytes]) -> None:
        raise OSError("job objects are Windows only")


def bind_to_autodj(proc: subprocess.Popen[bytes]) -> bool:
    """Make *proc* die when AutoDJ's process ends, however it ends (Windows only).

    Returns:
        Whether *proc* is now in the job: ``False`` on other systems, for
        a stand-in process without a handle, or when Windows refuses (it is
        then logged once, and AutoDJ still stops the process itself on a
        normal shutdown).
    """
    global _job, _unavailable
    if sys.platform != "win32" or getattr(proc, "_handle", None) is None:
        return False
    with _lock:
        if _unavailable:
            return False
        try:
            if _job is None:
                _job = _create_job()
            _assign(_job, proc)
        except OSError:
            _unavailable = True
            logger.warning(
                "Could not tie mastering processes to AutoDJ (Windows job object); they are "
                "still stopped on a normal shutdown",
                exc_info=True,
            )
            return False
    return True
