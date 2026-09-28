"""Windows process helpers for the CAD runner (ctypes only; empty results elsewhere).

* :func:`snapshot` — all processes as ``(pid, ppid, image)`` (Toolhelp32; no WMI, no psutil);
* :func:`tree` — a process and its descendants, including orphans whose parent already exited (the crash reporter
  ``senddmp.exe`` outlives a crashed ``accoreconsole.exe``);
* :func:`visible_windows` — visible top-level windows of given processes (a dialog of a headless job);
* :func:`kill` — terminate processes (the job's own tree only).
"""
from __future__ import annotations

import sys
from dataclasses import dataclass
from typing import Iterable


@dataclass(frozen=True)
class Proc:
    pid: int
    ppid: int
    image: str


def snapshot() -> list[Proc]:
    if sys.platform != "win32":
        return []
    import ctypes
    from ctypes import wintypes

    class ProcessEntry32(ctypes.Structure):
        _fields_ = [("dwSize", wintypes.DWORD), ("cntUsage", wintypes.DWORD), ("th32ProcessID", wintypes.DWORD),
                    ("th32DefaultHeapID", ctypes.c_size_t), ("th32ModuleID", wintypes.DWORD),
                    ("cntThreads", wintypes.DWORD), ("th32ParentProcessID", wintypes.DWORD),
                    ("pcPriClassBase", ctypes.c_long), ("dwFlags", wintypes.DWORD),
                    ("szExeFile", ctypes.c_wchar * 260)]

    kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
    kernel32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    handle = kernel32.CreateToolhelp32Snapshot(0x00000002, 0)            # TH32CS_SNAPPROCESS
    if not handle or handle == wintypes.HANDLE(-1).value:
        return []
    out: list[Proc] = []
    try:
        entry = ProcessEntry32()
        entry.dwSize = ctypes.sizeof(ProcessEntry32)
        ok = kernel32.Process32FirstW(handle, ctypes.byref(entry))
        while ok:
            out.append(Proc(int(entry.th32ProcessID), int(entry.th32ParentProcessID), entry.szExeFile.lower()))
            ok = kernel32.Process32NextW(handle, ctypes.byref(entry))
    finally:
        kernel32.CloseHandle(handle)
    return out


def tree(root: int, procs: Iterable[Proc]) -> set[int]:
    """``root`` and every process descending from it (by parent PID, orphans included)."""
    procs = list(procs)
    members = {root}
    changed = True
    while changed:
        changed = False
        for p in procs:
            if p.ppid in members and p.pid not in members and p.pid != p.ppid:
                members.add(p.pid)
                changed = True
    return members


def visible_windows(pids: set[int]) -> list[tuple[int, str]]:
    if sys.platform != "win32" or not pids:
        return []
    import ctypes
    from ctypes import wintypes

    user32 = ctypes.windll.user32  # type: ignore[attr-defined]
    found: list[tuple[int, str]] = []
    proto = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

    def callback(hwnd, _lparam):
        if user32.IsWindowVisible(hwnd):
            pid = wintypes.DWORD()
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            if pid.value in pids:
                buf = ctypes.create_unicode_buffer(256)
                user32.GetWindowTextW(hwnd, buf, 255)
                found.append((int(pid.value), buf.value))
        return True

    user32.EnumWindows(proto(callback), 0)
    return found


def kill(pids: Iterable[int]) -> list[int]:
    """Terminate the given processes; returns the PIDs that were terminated."""
    if sys.platform != "win32":
        return []
    import ctypes

    kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
    done = []
    for pid in sorted(set(pids)):
        handle = kernel32.OpenProcess(0x0001, False, pid)                  # PROCESS_TERMINATE
        if handle:
            try:
                if kernel32.TerminateProcess(handle, 1):
                    done.append(pid)
            finally:
                kernel32.CloseHandle(handle)
    return done
