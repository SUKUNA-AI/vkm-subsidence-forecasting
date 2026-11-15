"""Bounded, noninteractive process supervision.

Windows uses CREATE_NO_WINDOW and a kill-on-close Job Object. The process is
created suspended and assigned before its first instruction; this is lifecycle
containment, not a sandbox.
The native qualification must test descendant cleanup on the actual host.
"""
from __future__ import annotations
import ctypes
import os
import queue
import signal
import subprocess
import threading
import time
from pathlib import Path
from vkm_worker_core import WorkerError, need


def creation_kwargs(*, suspended: bool = False) -> dict:
    if os.name == "nt":
        return {"creationflags": subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP | (0x4 if suspended else 0)}
    return {"start_new_session": True}


class WindowsJob:
    def __init__(self, proc):
        self.handle = None
        if os.name != "nt":
            return
        from ctypes import wintypes as W
        class BASIC(ctypes.Structure):
            _fields_ = [("PerProcessUserTimeLimit", ctypes.c_int64), ("PerJobUserTimeLimit", ctypes.c_int64),
                        ("LimitFlags", W.DWORD), ("MinimumWorkingSetSize", ctypes.c_size_t),
                        ("MaximumWorkingSetSize", ctypes.c_size_t), ("ActiveProcessLimit", W.DWORD),
                        ("Affinity", ctypes.c_size_t), ("PriorityClass", W.DWORD), ("SchedulingClass", W.DWORD)]
        class IO(ctypes.Structure):
            _fields_ = [(n, ctypes.c_uint64) for n in ("ReadOperationCount", "WriteOperationCount", "OtherOperationCount", "ReadTransferCount", "WriteTransferCount", "OtherTransferCount")]
        class EXTENDED(ctypes.Structure):
            _fields_ = [("BasicLimitInformation", BASIC), ("IoInfo", IO), ("ProcessMemoryLimit", ctypes.c_size_t),
                        ("JobMemoryLimit", ctypes.c_size_t), ("PeakProcessMemoryUsed", ctypes.c_size_t), ("PeakJobMemoryUsed", ctypes.c_size_t)]
        k = ctypes.WinDLL("kernel32", use_last_error=True)
        k.CreateJobObjectW.argtypes = [ctypes.c_void_p, W.LPCWSTR]; k.CreateJobObjectW.restype = W.HANDLE
        k.SetInformationJobObject.argtypes = [W.HANDLE, ctypes.c_int, ctypes.c_void_p, W.DWORD]; k.SetInformationJobObject.restype = W.BOOL
        k.AssignProcessToJobObject.argtypes = [W.HANDLE, W.HANDLE]; k.AssignProcessToJobObject.restype = W.BOOL
        k.CloseHandle.argtypes = [W.HANDLE]; k.CloseHandle.restype = W.BOOL
        h = k.CreateJobObjectW(None, None)
        if not h:
            raise WorkerError("CreateJobObject failed")
        info = EXTENDED(); info.BasicLimitInformation.LimitFlags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if not k.SetInformationJobObject(h, 9, ctypes.byref(info), ctypes.sizeof(info)) or not k.AssignProcessToJobObject(h, W.HANDLE(int(proc._handle))):
            k.CloseHandle(h)
            raise WorkerError("cannot contain Windows worker process tree")
        self.handle = h; self.kernel = k

    def terminate_and_verify_empty(self, timeout=15.0):
        """Explicit owned-job completion proof; handle stays valid until query0."""
        proof = {"method": "WINDOWS_OWNED_JOB_TERMINATE_QUERY_ACTIVE_PROCESSES",
                 "tree_cleanup_proven": False, "active_processes": None,
                 "termination_succeeded": False, "query_succeeded": False}
        if os.name != "nt" or not self.handle:
            return proof
        from ctypes import wintypes as W
        class ACCOUNTING(ctypes.Structure):
            _fields_ = [("TotalUserTime", ctypes.c_int64), ("TotalKernelTime", ctypes.c_int64),
                        ("ThisPeriodTotalUserTime", ctypes.c_int64), ("ThisPeriodTotalKernelTime", ctypes.c_int64),
                        ("TotalPageFaultCount", W.DWORD), ("TotalProcesses", W.DWORD),
                        ("ActiveProcesses", W.DWORD), ("TotalTerminatedProcesses", W.DWORD)]
        k = self.kernel
        k.TerminateJobObject.argtypes = [W.HANDLE, W.UINT]; k.TerminateJobObject.restype = W.BOOL
        k.QueryInformationJobObject.argtypes = [W.HANDLE, ctypes.c_int, ctypes.c_void_p, W.DWORD, ctypes.c_void_p]
        k.QueryInformationJobObject.restype = W.BOOL
        proof["termination_succeeded"] = bool(k.TerminateJobObject(self.handle, 1))
        if not proof["termination_succeeded"]:
            return proof
        deadline = time.monotonic() + timeout
        while True:
            info = ACCOUNTING()
            if not k.QueryInformationJobObject(self.handle, 1, ctypes.byref(info), ctypes.sizeof(info), None):
                return proof
            proof["query_succeeded"] = True
            proof["active_processes"] = int(info.ActiveProcesses)
            if info.ActiveProcesses == 0:
                proof["tree_cleanup_proven"] = True
                return proof
            if time.monotonic() >= deadline:
                return proof
            time.sleep(0.02)

    def close(self):
        if self.handle:
            self.kernel.CloseHandle(self.handle)
            self.handle = None


def resume_suspended(proc) -> None:
    """Resume the sole initial thread only after Job assignment succeeded.

    Popen closes the CreateProcess thread handle, so enumerate this still
    suspended process's thread. No child instruction can run during this gap.
    """
    if os.name != "nt":
        return
    from ctypes import wintypes as W
    class THREADENTRY32(ctypes.Structure):
        _fields_ = [("dwSize", W.DWORD), ("cntUsage", W.DWORD),
                    ("th32ThreadID", W.DWORD), ("th32OwnerProcessID", W.DWORD),
                    ("tpBasePri", W.LONG), ("tpDeltaPri", W.LONG), ("dwFlags", W.DWORD)]
    k = ctypes.WinDLL("kernel32", use_last_error=True)
    k.CreateToolhelp32Snapshot.argtypes = [W.DWORD, W.DWORD]
    k.CreateToolhelp32Snapshot.restype = W.HANDLE
    k.Thread32First.argtypes = [W.HANDLE, ctypes.POINTER(THREADENTRY32)]
    k.Thread32First.restype = W.BOOL
    k.Thread32Next.argtypes = k.Thread32First.argtypes
    k.Thread32Next.restype = W.BOOL
    k.OpenThread.argtypes = [W.DWORD, W.BOOL, W.DWORD]; k.OpenThread.restype = W.HANDLE
    k.ResumeThread.argtypes = [W.HANDLE]; k.ResumeThread.restype = W.DWORD
    k.CloseHandle.argtypes = [W.HANDLE]; k.CloseHandle.restype = W.BOOL
    snapshot = k.CreateToolhelp32Snapshot(0x4, 0)  # TH32CS_SNAPTHREAD
    need(snapshot and snapshot != W.HANDLE(-1).value, "cannot enumerate suspended worker thread")
    tids = []
    try:
        row = THREADENTRY32(); row.dwSize = ctypes.sizeof(row)
        ok = k.Thread32First(snapshot, ctypes.byref(row))
        while ok:
            if row.th32OwnerProcessID == proc.pid:
                tids.append(row.th32ThreadID)
            ok = k.Thread32Next(snapshot, ctypes.byref(row))
    finally:
        k.CloseHandle(snapshot)
    need(len(tids) == 1, "suspended worker must have exactly one initial thread")
    thread = k.OpenThread(0x2, False, tids[0])  # THREAD_SUSPEND_RESUME
    need(bool(thread), "cannot open suspended worker thread")
    try:
        need(k.ResumeThread(thread) == 1, "unexpected worker suspend count")
    finally:
        k.CloseHandle(thread)


def stop_tree(proc, job: WindowsJob | None) -> dict:
    proof = {"method": "NO_EXPLICIT_WINDOWS_JOB_QUERY_PROOF", "tree_cleanup_proven": False}
    if os.name == "nt":
        if job and job.handle:
            try:
                proof = job.terminate_and_verify_empty()
            finally:
                job.close()
        elif proc.poll() is None:
            subprocess.run(["taskkill.exe", "/T", "/F", "/PID", str(proc.pid)],
                           stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                           creationflags=subprocess.CREATE_NO_WINDOW, timeout=15)
    else:
        # Even if the direct child exited, inherited pipe holders must be killed.
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    if proc.poll() is None:
        proc.kill()
    try:
        proc.wait(timeout=15)
    except subprocess.TimeoutExpired as exc:
        raise WorkerError("process tree did not stop") from exc
    return proof


def supervise(argv: list[str], *, cwd: Path, env: dict, timeout: int, stdout_line,
              stderr_line, max_bytes: int = 32 * 1024 * 1024, max_line: int = 1024 * 1024,
              stdin_bytes: bytes | None = None) -> dict:
    """Drain both pipes; limit bytes/line/time; always reap children, including errors."""
    started = time.monotonic()
    need(stdin_bytes is None or isinstance(stdin_bytes, bytes), "stdin payload must be bytes")
    proc = subprocess.Popen(argv, cwd=cwd, env=env, stdin=subprocess.PIPE if stdin_bytes is not None else subprocess.DEVNULL,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, shell=False,
                            **creation_kwargs(suspended=True))
    job = None
    q = queue.Queue(maxsize=64)
    stopped = threading.Event()
    threads = []
    def emit(item):
        while not stopped.is_set():
            try:
                q.put(item, timeout=0.1); return
            except queue.Full:
                continue
    def pump(stream, name):
        try:
            while not stopped.is_set():
                raw = stream.readline(max_line + 1)
                if not raw:
                    break
                emit((name, raw))
                if len(raw) > max_line:
                    break
        finally:
            emit((name, None))
    def feed():
        try:
            proc.stdin.write(stdin_bytes); proc.stdin.close()
        except (BrokenPipeError, OSError, ValueError):
            pass
    status = "EXITED"
    size = 0
    try:
        job = WindowsJob(proc)
        resume_suspended(proc)
        for stream, name in ((proc.stdout, "stdout"), (proc.stderr, "stderr")):
            thread = threading.Thread(target=pump, args=(stream, name), daemon=True)
            thread.start(); threads.append(thread)
        if stdin_bytes is not None:
            thread = threading.Thread(target=feed, daemon=True)
            thread.start(); threads.append(thread)
        eof = set()
        exit_seen = None
        while len(eof) != 2:
            if time.monotonic() - started > timeout:
                status = "TIMED_OUT"; break
            if proc.poll() is not None:
                exit_seen = exit_seen or time.monotonic()
                if time.monotonic() - exit_seen > 2:
                    status = "DESCENDANTS_HELD_PIPES"; break
            try:
                source, raw = q.get(timeout=0.1)
            except queue.Empty:
                continue
            if raw is None:
                eof.add(source); continue
            size += len(raw)
            if len(raw) > max_line or size > max_bytes:
                status = "OUTPUT_LIMIT"; break
            try:
                text = raw.decode("utf-8")
            except UnicodeDecodeError:
                status = "INVALID_UTF8"; break
            (stdout_line if source == "stdout" else stderr_line)(text)
        if status == "EXITED":
            try:
                proc.wait(timeout=max(0.1, timeout - (time.monotonic() - started)))
            except subprocess.TimeoutExpired:
                status = "TIMED_OUT"
    finally:
        stopped.set()
        cleanup = stop_tree(proc, job)
        for stream in (proc.stdout, proc.stderr):
            if stream:
                stream.close()
        for thread in threads:
            thread.join(timeout=2)
    return {"lifecycle": status, "returncode": proc.returncode, "bytes_observed": size,
            "tree_cleanup_proven": cleanup.get("tree_cleanup_proven") is True, "tree_cleanup": cleanup,
            "duration_sec": round(time.monotonic() - started, 3)}
