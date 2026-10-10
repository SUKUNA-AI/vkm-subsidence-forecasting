"""Linux local-filesystem mutation fence independent of timestamp resolution.

Install before reading/hashing/loading. Any content/metadata/identity event or
queue overflow invalidates the lease permanently. This is an operational fence
for operator-owned immutable files, not protection against a privileged attacker.
Network/FUSE/DrvFS filesystems are deliberately unqualified.
"""
from __future__ import annotations

import ctypes
import errno
import os
import platform
import threading
from pathlib import Path

from vkm_corpus.update.remote_retrieval import file_signature

LOCAL_FILESYSTEMS = {0xEF53, 0x58465342, 0x9123683E, 0x01021994, 0x794C7630}  # ext*, XFS, btrfs, tmpfs, overlayfs
MUTATIONS = 0x00000002 | 0x00000004 | 0x00000008 | 0x00000400 | 0x00000800  # MODIFY ATTRIB CLOSE_WRITE DELETE_SELF MOVE_SELF


class NativeFileWatch:
    def __init__(self, paths):
        self._fd = -1
        self._invalid = False
        self._lock = threading.Lock()
        if platform.system() != "Linux":
            raise ValueError("native file mutation fence requires Linux")
        selected = tuple(dict.fromkeys(str(Path(p).absolute()) for p in paths))
        if not selected or len(selected) > 8192:
            raise ValueError("bounded nonempty native file inventory required")
        libc = ctypes.CDLL(None, use_errno=True)
        libc.statfs.argtypes = (ctypes.c_char_p, ctypes.c_void_p)
        libc.statfs.restype = ctypes.c_int
        libc.inotify_init1.argtypes = (ctypes.c_int,)
        libc.inotify_init1.restype = ctypes.c_int
        libc.inotify_add_watch.argtypes = (ctypes.c_int, ctypes.c_char_p, ctypes.c_uint32)
        libc.inotify_add_watch.restype = ctypes.c_int
        self._fd = libc.inotify_init1(os.O_NONBLOCK | os.O_CLOEXEC)
        if self._fd < 0:
            raise ValueError("native inotify unavailable")
        try:
            for path in selected:
                before = file_signature(path)
                info = ctypes.create_string_buffer(4096)
                if libc.statfs(os.fsencode(path), info) != 0:
                    raise ValueError("native filesystem identity unavailable")
                if ctypes.c_long.from_buffer(info).value & 0xFFFFFFFF not in LOCAL_FILESYSTEMS:
                    raise ValueError("native file filesystem is not qualified for mutation events")
                if libc.inotify_add_watch(self._fd, os.fsencode(path), MUTATIONS) < 0:
                    raise ValueError("native file watch unavailable")
                if file_signature(path) != before:
                    raise ValueError("native file replaced while installing watch")
            self.check()
        except BaseException:
            self.close()
            raise

    def check(self):
        with self._lock:
            self._check()

    def _check(self):
        if self._fd < 0 or self._invalid:
            raise ValueError("native file mutation lease is closed or invalid")
        try:
            events = os.read(self._fd, 65536)
        except BlockingIOError:
            return
        except OSError as exc:
            if exc.errno in {errno.EAGAIN, errno.EWOULDBLOCK}:
                return
            self._invalid = True
            raise ValueError("native file watch failed") from exc
        # All subscribed events mean changed/removed resources. IN_IGNORED,
        # IN_UNMOUNT and IN_Q_OVERFLOW are delivered independently of the mask.
        self._invalid = True
        raise ValueError("native file changed or event stream overflowed" if events else "native file event stream closed")

    def close(self):
        if self._fd >= 0:
            os.close(self._fd)
            self._fd = -1

    def __del__(self):
        self.close()


def optional_watch(paths):
    """Legacy readers may load anywhere; only captured native watches qualify."""
    try:
        return NativeFileWatch(paths)
    except (OSError, ValueError):
        return None
