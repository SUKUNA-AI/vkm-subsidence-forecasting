"""Process trees of applications, detached runners and process listing (Windows and POSIX).

Windows: :class:`ProcessTree` starts the application *suspended* (``CREATE_SUSPENDED | CREATE_NO_WINDOW |
CREATE_NEW_PROCESS_GROUP``), assigns it to a fresh Job Object with ``KILL_ON_JOB_CLOSE | BREAKAWAY_OK`` and only then
resumes it, so every descendant is inside the job before it can run. ``kill()`` is ``TerminateJobObject``; if the
runner itself dies, the OS closes the job handle and kills the whole tree. Processes that explicitly ask to break
away (``CREATE_BREAKAWAY_FROM_JOB``, e.g. per-user service hosts shared with other sessions) are allowed to.
POSIX: a new session (process group); ``kill()`` is ``killpg(SIGKILL)``.

:func:`spawn_detached` starts a runner outside the caller's process tree (``DETACHED_PROCESS``, and
``CREATE_BREAKAWAY_FROM_JOB`` when the parent job allows it) so that a restart of Claude Code or of the MCP server
does not kill a long computation.
"""
from __future__ import annotations

import os
import signal
import subprocess
import time
from pathlib import Path
from typing import Any, Mapping, Sequence

IS_WINDOWS = os.name == "nt"
KILL_EXIT_CODE = 0xC0DE           # exit code given to processes killed through the Job Object
_KEEP: list[subprocess.Popen] = []  # detached runners (POSIX: reaped lazily; Windows: no ResourceWarning)

if IS_WINDOWS:
    import ctypes
    from ctypes import wintypes

    _k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _INVALID = ctypes.c_void_p(-1).value
    CREATE_SUSPENDED = 0x00000004
    CREATE_NEW_PROCESS_GROUP = 0x00000200
    CREATE_NO_WINDOW = 0x08000000
    DETACHED_PROCESS = 0x00000008
    CREATE_BREAKAWAY_FROM_JOB = 0x01000000
    JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000
    JOB_OBJECT_LIMIT_BREAKAWAY_OK = 0x00000800
    _BASIC_ACCOUNTING, _PID_LIST, _EXTENDED_LIMIT = 1, 3, 9
    _TH32CS_SNAPPROCESS, _TH32CS_SNAPTHREAD = 0x2, 0x4
    _THREAD_SUSPEND_RESUME = 0x0002
    _PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    _STILL_ACTIVE = 259

    class _IO_COUNTERS(ctypes.Structure):
        _fields_ = [(n, ctypes.c_ulonglong) for n in ("ReadOperationCount", "WriteOperationCount",
                                                      "OtherOperationCount", "ReadTransferCount",
                                                      "WriteTransferCount", "OtherTransferCount")]

    class _BASIC_LIMIT(ctypes.Structure):
        _fields_ = [("PerProcessUserTimeLimit", ctypes.c_longlong), ("PerJobUserTimeLimit", ctypes.c_longlong),
                    ("LimitFlags", wintypes.DWORD), ("MinimumWorkingSetSize", ctypes.c_size_t),
                    ("MaximumWorkingSetSize", ctypes.c_size_t), ("ActiveProcessLimit", wintypes.DWORD),
                    ("Affinity", ctypes.c_size_t), ("PriorityClass", wintypes.DWORD),
                    ("SchedulingClass", wintypes.DWORD)]

    class _EXTENDED(ctypes.Structure):
        _fields_ = [("BasicLimitInformation", _BASIC_LIMIT), ("IoInfo", _IO_COUNTERS),
                    ("ProcessMemoryLimit", ctypes.c_size_t), ("JobMemoryLimit", ctypes.c_size_t),
                    ("PeakProcessMemoryUsed", ctypes.c_size_t), ("PeakJobMemoryUsed", ctypes.c_size_t)]

    class _ACCOUNTING(ctypes.Structure):
        _fields_ = [("TotalUserTime", ctypes.c_longlong), ("TotalKernelTime", ctypes.c_longlong),
                    ("ThisPeriodTotalUserTime", ctypes.c_longlong), ("ThisPeriodTotalKernelTime", ctypes.c_longlong),
                    ("TotalPageFaultCount", wintypes.DWORD), ("TotalProcesses", wintypes.DWORD),
                    ("ActiveProcesses", wintypes.DWORD), ("TotalTerminatedProcesses", wintypes.DWORD)]

    class _PID_LIST_BUF(ctypes.Structure):
        _fields_ = [("Assigned", wintypes.DWORD), ("InList", wintypes.DWORD), ("Ids", ctypes.c_size_t * 2048)]

    class _THREADENTRY32(ctypes.Structure):
        _fields_ = [("dwSize", wintypes.DWORD), ("cntUsage", wintypes.DWORD), ("th32ThreadID", wintypes.DWORD),
                    ("th32OwnerProcessID", wintypes.DWORD), ("tpBasePri", wintypes.LONG),
                    ("tpDeltaPri", wintypes.LONG), ("dwFlags", wintypes.DWORD)]

    class _PROCESSENTRY32W(ctypes.Structure):
        _fields_ = [("dwSize", wintypes.DWORD), ("cntUsage", wintypes.DWORD), ("th32ProcessID", wintypes.DWORD),
                    ("th32DefaultHeapID", ctypes.c_size_t), ("th32ModuleID", wintypes.DWORD),
                    ("cntThreads", wintypes.DWORD), ("th32ParentProcessID", wintypes.DWORD),
                    ("pcPriClassBase", wintypes.LONG), ("dwFlags", wintypes.DWORD),
                    ("szExeFile", wintypes.WCHAR * 260)]

    _k32.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
    _k32.CreateJobObjectW.restype = wintypes.HANDLE
    _k32.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
    _k32.SetInformationJobObject.restype = wintypes.BOOL
    _k32.QueryInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD,
                                               ctypes.POINTER(wintypes.DWORD)]
    _k32.QueryInformationJobObject.restype = wintypes.BOOL
    _k32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
    _k32.AssignProcessToJobObject.restype = wintypes.BOOL
    _k32.TerminateJobObject.argtypes = [wintypes.HANDLE, wintypes.UINT]
    _k32.TerminateJobObject.restype = wintypes.BOOL
    _k32.CloseHandle.argtypes = [wintypes.HANDLE]
    _k32.CloseHandle.restype = wintypes.BOOL
    _k32.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
    _k32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    _k32.Thread32First.argtypes = [wintypes.HANDLE, ctypes.POINTER(_THREADENTRY32)]
    _k32.Thread32First.restype = wintypes.BOOL
    _k32.Thread32Next.argtypes = [wintypes.HANDLE, ctypes.POINTER(_THREADENTRY32)]
    _k32.Thread32Next.restype = wintypes.BOOL
    _k32.Process32FirstW.argtypes = [wintypes.HANDLE, ctypes.POINTER(_PROCESSENTRY32W)]
    _k32.Process32FirstW.restype = wintypes.BOOL
    _k32.Process32NextW.argtypes = [wintypes.HANDLE, ctypes.POINTER(_PROCESSENTRY32W)]
    _k32.Process32NextW.restype = wintypes.BOOL
    _k32.OpenThread.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    _k32.OpenThread.restype = wintypes.HANDLE
    _k32.ResumeThread.argtypes = [wintypes.HANDLE]
    _k32.ResumeThread.restype = wintypes.DWORD
    _k32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    _k32.OpenProcess.restype = wintypes.HANDLE
    _k32.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
    _k32.GetExitCodeProcess.restype = wintypes.BOOL
    _k32.QueryFullProcessImageNameW.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR,
                                                ctypes.POINTER(wintypes.DWORD)]
    _k32.QueryFullProcessImageNameW.restype = wintypes.BOOL

    def _last_error(what: str) -> OSError:
        code = ctypes.get_last_error()
        return OSError(code, f"{what} failed", None, code)

    def _snapshot(flags: int):
        snap = _k32.CreateToolhelp32Snapshot(flags, 0)
        if snap in (None, 0, _INVALID):
            raise _last_error("CreateToolhelp32Snapshot")
        return snap

    def _thread_ids(pid: int) -> list[int]:
        snap = _snapshot(_TH32CS_SNAPTHREAD)
        try:
            entry = _THREADENTRY32()
            entry.dwSize = ctypes.sizeof(entry)
            ids = []
            ok = _k32.Thread32First(snap, ctypes.byref(entry))
            while ok:
                if entry.th32OwnerProcessID == pid:
                    ids.append(entry.th32ThreadID)
                ok = _k32.Thread32Next(snap, ctypes.byref(entry))
            return ids
        finally:
            _k32.CloseHandle(snap)

    def _resume_process(pid: int) -> int:
        resumed = 0
        for tid in _thread_ids(pid):
            handle = _k32.OpenThread(_THREAD_SUSPEND_RESUME, False, tid)
            if handle:
                try:
                    if _k32.ResumeThread(handle) != 0xFFFFFFFF:
                        resumed += 1
                finally:
                    _k32.CloseHandle(handle)
        return resumed


def list_processes() -> list[dict[str, Any]]:
    """``[{pid, ppid, name}]`` of all processes visible to this user (no command lines, no owners)."""
    if IS_WINDOWS:
        snap = _snapshot(_TH32CS_SNAPPROCESS)
        try:
            entry = _PROCESSENTRY32W()
            entry.dwSize = ctypes.sizeof(entry)
            out = []
            ok = _k32.Process32FirstW(snap, ctypes.byref(entry))
            while ok:
                out.append({"pid": int(entry.th32ProcessID), "ppid": int(entry.th32ParentProcessID),
                            "name": entry.szExeFile})
                ok = _k32.Process32NextW(snap, ctypes.byref(entry))
            return out
        finally:
            _k32.CloseHandle(snap)
    out = []
    proc = Path("/proc")
    if not proc.is_dir():
        return out
    for d in proc.iterdir():
        if not d.name.isdigit():
            continue
        try:
            stat = (d / "stat").read_text()
        except OSError:
            continue
        name = stat[stat.find("(") + 1:stat.rfind(")")]
        rest = stat[stat.rfind(")") + 2:].split()
        if rest and rest[0] != "Z":
            out.append({"pid": int(d.name), "ppid": int(rest[1]), "name": name})
    return out


def pid_alive(pid: int) -> bool:
    if not pid or pid <= 0:
        return False
    if IS_WINDOWS:
        handle = _k32.OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not handle:
            return False
        try:
            code = wintypes.DWORD()
            return bool(_k32.GetExitCodeProcess(handle, ctypes.byref(code))) and code.value == _STILL_ACTIVE
        finally:
            _k32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    try:
        state = Path(f"/proc/{pid}/stat").read_text()
        return state[state.rfind(")") + 2:].split()[0] != "Z"
    except OSError:
        return True


def process_image(pid: int) -> str | None:
    """Executable file name (no directory) of a live process, or ``None``."""
    if IS_WINDOWS:
        handle = _k32.OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not handle:
            return None
        try:
            size = wintypes.DWORD(1024)
            buf = ctypes.create_unicode_buffer(1024)
            if _k32.QueryFullProcessImageNameW(handle, 0, buf, ctypes.byref(size)):
                return Path(buf.value).name
            return None
        finally:
            _k32.CloseHandle(handle)
    try:
        return Path(f"/proc/{pid}/comm").read_text().strip() or None
    except OSError:
        return None


def _posix_group_pids(pgid: int) -> list[int]:
    out = []
    proc = Path("/proc")
    if not proc.is_dir():
        try:
            os.killpg(pgid, 0)
            return [pgid]
        except OSError:
            return []
    for d in proc.iterdir():
        if not d.name.isdigit():
            continue
        try:
            stat = (d / "stat").read_text()
        except OSError:
            continue
        rest = stat[stat.rfind(")") + 2:].split()
        if len(rest) > 2 and rest[0] != "Z" and int(rest[2]) == pgid:
            out.append(int(d.name))
    return out


class ProcessTree:
    """One application process tree (see the module docstring)."""

    def __init__(self) -> None:
        self.proc: subprocess.Popen | None = None
        self.isolation = "NONE"
        self._job = None
        self.killed = False

    def start(self, argv: Sequence[str], *, cwd: Path, env: Mapping[str, str], stdout, stderr) -> subprocess.Popen:
        if IS_WINDOWS:
            job = _k32.CreateJobObjectW(None, None)
            if not job:
                raise _last_error("CreateJobObjectW")
            info = _EXTENDED()
            info.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE | JOB_OBJECT_LIMIT_BREAKAWAY_OK
            if not _k32.SetInformationJobObject(job, _EXTENDED_LIMIT, ctypes.byref(info), ctypes.sizeof(info)):
                err = _last_error("SetInformationJobObject")
                _k32.CloseHandle(job)
                raise err
            self._job = job
            flags = CREATE_SUSPENDED | CREATE_NO_WINDOW | CREATE_NEW_PROCESS_GROUP
            self.proc = subprocess.Popen(list(argv), cwd=str(cwd), env=dict(env), stdin=subprocess.DEVNULL,
                                         stdout=stdout, stderr=stderr, creationflags=flags)
            if _k32.AssignProcessToJobObject(job, int(self.proc._handle)):     # noqa: SLF001 - Popen handle
                self.isolation = "WIN_JOB_OBJECT"
            else:
                self.isolation = "WIN_TASKKILL_FALLBACK"
            if _resume_process(self.proc.pid) == 0:
                self.proc.kill()
                raise OSError("could not resume the suspended application process")
            return self.proc
        self.proc = subprocess.Popen(list(argv), cwd=str(cwd), env=dict(env), stdin=subprocess.DEVNULL, stdout=stdout,
                                     stderr=stderr, start_new_session=True)
        self.isolation = "POSIX_PROCESS_GROUP"
        return self.proc

    def active_pids(self) -> list[int]:
        if self.proc is None:
            return []
        if self.isolation == "WIN_JOB_OBJECT":
            buf = _PID_LIST_BUF()
            if _k32.QueryInformationJobObject(self._job, _PID_LIST, ctypes.byref(buf), ctypes.sizeof(buf), None):
                return [int(buf.Ids[i]) for i in range(min(buf.InList, 2048))]
            return []
        if self.isolation == "POSIX_PROCESS_GROUP":
            return _posix_group_pids(self.proc.pid)
        return [self.proc.pid] if self.proc.poll() is None else []

    def active_count(self) -> int:
        if self.isolation == "WIN_JOB_OBJECT":
            acct = _ACCOUNTING()
            if _k32.QueryInformationJobObject(self._job, _BASIC_ACCOUNTING, ctypes.byref(acct),
                                              ctypes.sizeof(acct), None):
                return int(acct.ActiveProcesses)
        return len(self.active_pids())

    def kill(self) -> None:
        """Kill the whole tree (idempotent)."""
        self.killed = True
        if self.proc is None:
            return
        if self.isolation == "WIN_JOB_OBJECT":
            _k32.TerminateJobObject(self._job, KILL_EXIT_CODE)
        elif self.isolation == "POSIX_PROCESS_GROUP":
            try:
                os.killpg(self.proc.pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass
        elif IS_WINDOWS:
            subprocess.run(["taskkill", "/T", "/F", "/PID", str(self.proc.pid)], capture_output=True, check=False)
        else:
            self.proc.kill()

    def wait_empty(self, timeout_s: float, poll_s: float = 0.05) -> bool:
        """Wait until no process of the tree is alive; ``False`` if some still run after ``timeout_s``."""
        deadline = time.monotonic() + timeout_s
        while True:
            if self.proc is not None:
                self.proc.poll()
            if self.active_count() == 0:
                return True
            if time.monotonic() >= deadline:
                return False
            time.sleep(poll_s)

    def accounting(self) -> dict[str, Any]:
        if self.isolation != "WIN_JOB_OBJECT":
            return {}
        out: dict[str, Any] = {}
        acct = _ACCOUNTING()
        if _k32.QueryInformationJobObject(self._job, _BASIC_ACCOUNTING, ctypes.byref(acct), ctypes.sizeof(acct), None):
            out.update(total_processes=int(acct.TotalProcesses), cpu_user_s=round(acct.TotalUserTime / 1e7, 2),
                       cpu_kernel_s=round(acct.TotalKernelTime / 1e7, 2))
        ext = _EXTENDED()
        if _k32.QueryInformationJobObject(self._job, _EXTENDED_LIMIT, ctypes.byref(ext), ctypes.sizeof(ext), None):
            out["peak_job_memory_mb"] = round(ext.PeakJobMemoryUsed / 2**20, 1)
        return out

    def close(self) -> None:
        if self._job is not None:
            _k32.CloseHandle(self._job)            # KILL_ON_JOB_CLOSE: anything left dies here
            self._job = None


def reap() -> None:
    """Collect finished detached runners (POSIX zombies)."""
    for p in list(_KEEP):
        if p.poll() is not None:
            _KEEP.remove(p)


def spawn_detached(argv: Sequence[str], *, cwd: Path, env: Mapping[str, str], log_path: Path) -> tuple[int, str]:
    """Start ``argv`` outside the caller's tree; returns ``(pid, mode)`` with mode BREAKAWAY / NO_BREAKAWAY / SESSION."""
    reap()
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with open(log_path, "ab") as log:
        if IS_WINDOWS:
            base = DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP
            for flags, mode in ((base | CREATE_BREAKAWAY_FROM_JOB, "BREAKAWAY"), (base, "NO_BREAKAWAY")):
                try:
                    proc = subprocess.Popen(list(argv), cwd=str(cwd), env=dict(env), stdin=subprocess.DEVNULL,
                                            stdout=log, stderr=log, creationflags=flags, close_fds=True)
                except OSError as exc:
                    if getattr(exc, "winerror", None) == 5 and mode == "BREAKAWAY":
                        continue               # the parent job forbids breakaway: stay in it
                    raise
                _KEEP.append(proc)
                return proc.pid, mode
        proc = subprocess.Popen(list(argv), cwd=str(cwd), env=dict(env), stdin=subprocess.DEVNULL, stdout=log,
                                stderr=log, start_new_session=True, close_fds=True)
        _KEEP.append(proc)
        return proc.pid, "SESSION"
