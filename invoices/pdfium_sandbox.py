"""Running `pdfium_worker` for one document, bounded by the operating system.

The child gets a Job Object on Windows (`_Job`: a cap on what it may commit,
killed with the job's handle - so with the server too), an rlimit it sets
itself elsewhere, and a wall clock everywhere (`run`): past either it is
DocumentTooBig, « trop lourd à afficher ». A Job Object that cannot be made
or given the child is a warning in the server's log, and the clock still
bounds it; PDFium never runs in the server's process instead.

Production is Windows; the rest is only so the tests run anywhere.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
import threading

logger = logging.getLogger(__name__)

WORKER = os.path.join(os.path.dirname(os.path.abspath(__file__)), "pdfium_worker.py")
#: What the child is given of the server's environment: enough to start an
#: interpreter, never the server's settings or keys.
ENVIRONMENT = ("PATH", "SYSTEMROOT", "SYSTEMDRIVE", "WINDIR", "TEMP", "TMP", "TMPDIR", "LANG", "LC_ALL")
#: The longest line the child may say: its pid, or one result.
LINE_BYTES = 64 * 1024


class Outcome:
    """What one run gave: `result` (the child's last word, or None), and
    whether the clock or the memory cap stopped it."""

    def __init__(self):
        self.result: dict | None = None
        self.expired = False
        self.memory = False
        self.capped = False
        self.returncode: int | None = None


def run(path: str, folder: str, limits: dict, memory: int, seconds: float) -> Outcome:
    """Run the worker on `path`, writing into `folder`; never longer than
    `seconds`, never more than `memory` bytes where it can be capped."""
    outcome = Outcome()
    job = _Job.make(memory) if os.name == "nt" else None
    flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
    environment = {name: os.environ[name] for name in ENVIRONMENT if name in os.environ}
    try:
        process = subprocess.Popen(
            [sys.executable, "-I", WORKER],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            env=environment,
            cwd=folder,
            creationflags=flags,
        )
    except BaseException:
        if job is not None:
            job.close()
        raise
    interpreter = [process.pid]

    def expire():
        outcome.expired = True
        _kill(process, job, interpreter[0])

    clock = threading.Timer(seconds, expire)
    clock.daemon = True
    clock.start()
    try:
        hello = _said(process)
        if hello is not None and isinstance(hello.get("pid"), int):
            interpreter[0] = hello["pid"]
            # The interpreter, not the venv's launcher: that one put it in a
            # job of its own, killed with the launcher, and an interpreter
            # already in a job may join only an empty one.
            if job is not None and not job.take(interpreter[0]):
                job = job.drop()
            outcome.capped = job is not None or os.name != "nt"
            request = {"path": path, "folder": folder, "limits": limits, "memory": memory}
            try:
                process.stdin.write(json.dumps(request).encode() + b"\n")
                process.stdin.close()
            except OSError:
                pass
            else:
                outcome.result = _said(process)
        process.wait()
    finally:
        clock.cancel()
        # A clock that went off is done with the job before it is closed.
        clock.join()
        if process.poll() is None:
            _kill(process, job, interpreter[0])
            process.wait()
        for pipe in (process.stdin, process.stdout):
            try:
                pipe.close()
            except OSError:
                pass
        if job is not None:
            outcome.memory = job.hit_memory_cap()
            job.close()
    outcome.returncode = process.returncode
    return outcome


def _said(process) -> dict | None:
    """The child's next line, as JSON - None once it is gone or says nonsense."""
    line = process.stdout.readline(LINE_BYTES)
    try:
        message = json.loads(line)
    except ValueError:
        return None
    return message if isinstance(message, dict) else None


def _kill(process, job, interpreter: int) -> None:
    if job is not None:
        job.terminate()
    if process.poll() is None:
        # With no job: the interpreter the venv's launcher started, then the
        # launcher (it waits for the interpreter, so that one is still ours).
        for pid in {interpreter, process.pid}:
            try:
                os.kill(pid, 9)
            except OSError:
                pass


class _Job:
    """A Windows Job Object: per-process commit capped, every process in it
    killed when its last handle closes, no crash dialog left waiting; and a
    completion port, on which Windows says when a process hit the cap."""

    LIMIT_PROCESS_MEMORY = 0x100
    LIMIT_DIE_ON_UNHANDLED_EXCEPTION = 0x400
    LIMIT_KILL_ON_JOB_CLOSE = 0x2000
    EXTENDED_LIMIT_INFORMATION = 9
    ASSOCIATE_COMPLETION_PORT = 7
    MESSAGE_PROCESS_MEMORY_LIMIT = 9
    PROCESS_SET_QUOTA = 0x0100
    PROCESS_TERMINATE = 0x0001

    def __init__(self, kernel32, handle, port):
        self.kernel32, self.handle, self.port = kernel32, handle, port

    @classmethod
    def make(cls, memory: int) -> _Job | None:
        try:
            return cls._make(memory)
        except Exception:  # logged; the clock still bounds the child
            logger.warning(
                "PDFium : pas d'objet de tâche Windows, le rendu n'est borné que par le temps.", exc_info=True
            )
            return None

    @classmethod
    def _make(cls, memory: int) -> _Job:
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CreateJobObjectW.restype = wintypes.HANDLE
        kernel32.CreateJobObjectW.argtypes = (ctypes.c_void_p, wintypes.LPCWSTR)
        kernel32.SetInformationJobObject.argtypes = (wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD)
        kernel32.CreateIoCompletionPort.restype = wintypes.HANDLE
        kernel32.CreateIoCompletionPort.argtypes = (wintypes.HANDLE, wintypes.HANDLE, ctypes.c_size_t, wintypes.DWORD)
        kernel32.OpenProcess.restype = wintypes.HANDLE
        kernel32.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
        kernel32.AssignProcessToJobObject.argtypes = (wintypes.HANDLE, wintypes.HANDLE)
        kernel32.TerminateJobObject.argtypes = (wintypes.HANDLE, wintypes.UINT)
        kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
        kernel32.GetQueuedCompletionStatus.argtypes = (
            wintypes.HANDLE,
            ctypes.POINTER(wintypes.DWORD),
            ctypes.POINTER(ctypes.c_size_t),
            ctypes.POINTER(ctypes.c_void_p),
            wintypes.DWORD,
        )

        class BasicLimits(ctypes.Structure):
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

        class ExtendedLimits(ctypes.Structure):
            _fields_ = [
                ("BasicLimitInformation", BasicLimits),
                ("IoInfo", ctypes.c_uint64 * 6),
                ("ProcessMemoryLimit", ctypes.c_size_t),
                ("JobMemoryLimit", ctypes.c_size_t),
                ("PeakProcessMemoryUsed", ctypes.c_size_t),
                ("PeakJobMemoryUsed", ctypes.c_size_t),
            ]

        class CompletionPort(ctypes.Structure):
            _fields_ = [("CompletionKey", ctypes.c_void_p), ("CompletionPort", wintypes.HANDLE)]

        handle = kernel32.CreateJobObjectW(None, None)
        if not handle:
            raise ctypes.WinError(ctypes.get_last_error())
        job = cls(kernel32, handle, None)
        try:
            limits = ExtendedLimits()
            limits.BasicLimitInformation.LimitFlags = (
                cls.LIMIT_PROCESS_MEMORY | cls.LIMIT_DIE_ON_UNHANDLED_EXCEPTION | cls.LIMIT_KILL_ON_JOB_CLOSE
            )
            limits.ProcessMemoryLimit = memory
            if not kernel32.SetInformationJobObject(
                handle, cls.EXTENDED_LIMIT_INFORMATION, ctypes.byref(limits), ctypes.sizeof(limits)
            ):
                raise ctypes.WinError(ctypes.get_last_error())
            job.port = kernel32.CreateIoCompletionPort(wintypes.HANDLE(-1), None, 0, 1)
            if not job.port:
                raise ctypes.WinError(ctypes.get_last_error())
            port = CompletionPort(None, job.port)
            if not kernel32.SetInformationJobObject(
                handle, cls.ASSOCIATE_COMPLETION_PORT, ctypes.byref(port), ctypes.sizeof(port)
            ):
                raise ctypes.WinError(ctypes.get_last_error())
        except BaseException:
            job.close()
            raise
        return job

    def take(self, pid: int) -> bool:
        """Put process `pid` in the job."""
        import ctypes

        process = self.kernel32.OpenProcess(self.PROCESS_SET_QUOTA | self.PROCESS_TERMINATE, False, pid)
        if not process:
            logger.warning("PDFium : processus %s introuvable pour l'objet de tâche.", pid)
            return False
        try:
            if self.kernel32.AssignProcessToJobObject(self.handle, process):
                return True
            logger.warning("PDFium : l'objet de tâche refuse le processus %s (%s).", pid, ctypes.get_last_error())
            return False
        finally:
            self.kernel32.CloseHandle(process)

    def drop(self) -> None:
        """Given up (the child could not be put in it): the clock alone bounds it."""
        logger.warning("PDFium : le rendu n'est borné que par le temps.")
        self.close()

    def terminate(self) -> None:
        if self.handle:
            self.kernel32.TerminateJobObject(self.handle, 1)

    def hit_memory_cap(self) -> bool:
        """Whether Windows said a process of the job hit its commit cap."""
        import ctypes
        from ctypes import wintypes

        hit = False
        code, key, overlapped = wintypes.DWORD(), ctypes.c_size_t(), ctypes.c_void_p()
        while self.port and self.kernel32.GetQueuedCompletionStatus(
            self.port, ctypes.byref(code), ctypes.byref(key), ctypes.byref(overlapped), 0
        ):
            hit = hit or code.value == self.MESSAGE_PROCESS_MEMORY_LIMIT
        return hit

    def close(self) -> None:
        for name in ("handle", "port"):
            handle = getattr(self, name)
            if handle:
                self.kernel32.CloseHandle(handle)
                setattr(self, name, None)
