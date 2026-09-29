"""Windows process helpers for the CAD runner (ctypes only; empty results elsewhere).

* :func:`snapshot` — all processes as ``(pid, ppid, image, created)`` (Toolhelp32 + ``GetProcessTimes``; no WMI, no
  psutil);
* :func:`tree` — a process and its descendants, including orphans whose parent already exited (the crash reporter
  ``senddmp.exe`` outlives a crashed ``accoreconsole.exe``);
* :func:`visible_windows` — visible top-level windows of given processes (a dialog of a headless job);
* :func:`kill` — terminate processes (the job's own tree only; the creation time is checked again right before).

Windows keeps the PID of a dead parent in its children and reuses PIDs. A parent PID alone therefore does not prove
descent: on 29.09.2026 a new ``accoreconsole.exe`` received the PID of a long-gone process whose children (a user's
Discord) were still running; they were taken for the job's processes, their window for a dialog, and killed. A process
now belongs to the tree only when its creation time is known and not earlier than the tree's root (``not_before``)
nor than its parent, if that parent is alive in the snapshot.
"""
from __future__ import annotations

import sys
from dataclasses import dataclass
from typing import Iterable, Mapping

PROCESS_TERMINATE = 0x0001
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000


@dataclass(frozen=True)
class Proc:
    pid: int
    ppid: int
    image: str
    created: int | None = None           # creation time, FILETIME (100 ns since 1601, UTC); None: not readable


def _kernel32():
    import ctypes

    return ctypes.windll.kernel32  # type: ignore[attr-defined]


def _handle_created(kernel32, handle) -> int | None:
    import ctypes
    from ctypes import wintypes

    times = [wintypes.FILETIME() for _ in range(4)]
    if not kernel32.GetProcessTimes(handle, *[ctypes.byref(t) for t in times]):
        return None
    value = (times[0].dwHighDateTime << 32) | times[0].dwLowDateTime
    return value or None


def creation_time(pid: int) -> int | None:
    """Creation time of a process (FILETIME), or None. A process the caller holds a handle to (``Popen``) keeps its PID
    and stays readable after it exited."""
    if sys.platform != "win32" or pid <= 0:
        return None
    kernel32 = _kernel32()
    handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return None
    try:
        return _handle_created(kernel32, handle)
    finally:
        kernel32.CloseHandle(handle)


def filetime_now() -> int:
    """The current system time as FILETIME (the clock of process creation times)."""
    if sys.platform != "win32":
        return 0
    import ctypes
    from ctypes import wintypes

    ft = wintypes.FILETIME()
    _kernel32().GetSystemTimeAsFileTime(ctypes.byref(ft))
    return (ft.dwHighDateTime << 32) | ft.dwLowDateTime


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

    kernel32 = _kernel32()
    kernel32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    handle = kernel32.CreateToolhelp32Snapshot(0x00000002, 0)            # TH32CS_SNAPPROCESS
    if not handle or handle == wintypes.HANDLE(-1).value:
        return []
    entries: list[tuple[int, int, str]] = []
    try:
        entry = ProcessEntry32()
        entry.dwSize = ctypes.sizeof(ProcessEntry32)
        ok = kernel32.Process32FirstW(handle, ctypes.byref(entry))
        while ok:
            entries.append((int(entry.th32ProcessID), int(entry.th32ParentProcessID), entry.szExeFile.lower()))
            ok = kernel32.Process32NextW(handle, ctypes.byref(entry))
    finally:
        kernel32.CloseHandle(handle)
    return [Proc(pid, ppid, image, creation_time(pid)) for pid, ppid, image in entries]


def tree(root: int, procs: Iterable[Proc], *, not_before: int | None) -> set[int]:
    """``root`` and every process descending from it (by parent PID, orphans included) that was created at or after
    ``not_before`` (the root's creation time) and not before its parent when the parent is alive in ``procs``. A
    process whose creation time is unknown never joins; with ``not_before=None`` (the root's creation time unknown)
    only ``root`` is returned."""
    members = {root}
    if not_before is None:
        return members
    procs = list(procs)
    created = {p.pid: p.created for p in procs}
    changed = True
    while changed:
        changed = False
        for p in procs:
            if p.ppid not in members or p.pid in members or p.pid == p.ppid:
                continue
            if p.created is None or p.created < not_before:
                continue
            parent = created.get(p.ppid)
            if parent is not None and parent > p.created:          # the parent PID now names a newer process
                continue
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


def kill(pids: Iterable[int], created: Mapping[int, int | None] | None = None) -> list[int]:
    """Terminate the given processes; returns the PIDs that were terminated. With ``created`` (PID → creation time from
    the snapshot the PIDs came from) a process is terminated only if its creation time is still the same, so a PID
    reused in between is left alone."""
    if sys.platform != "win32":
        return []
    kernel32 = _kernel32()
    done = []
    for pid in list(dict.fromkeys(pids)):                          # the caller's order (children first)
        handle = kernel32.OpenProcess(PROCESS_TERMINATE | PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if handle:
            try:
                if created is not None and _handle_created(kernel32, handle) != created.get(pid):
                    continue
                if kernel32.TerminateProcess(handle, 1):
                    done.append(pid)
            finally:
                kernel32.CloseHandle(handle)
    return done
