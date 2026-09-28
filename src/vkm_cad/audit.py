"""Side-effect audit of AutoCAD runs: what changed in the user's AutoCAD registry branch and profile files.

The audit only reads. Before and after a run it takes a snapshot of ``HKCU\\Software\\Autodesk\\AutoCAD`` (value
names and a digest of the data; licence/serial/password-like keys and values are skipped — never read) and of the
files under the product's roaming/local profile folders (relative name, size, mtime). The receipt lists *what*
changed (key or file name and the change type), never the values. Nothing is restored automatically: headless runs
(``/isolate``) leave only small traces (e.g. ``LastLaunchedProduct``, AEC object defaults, plot components), the hidden
full instance also rewrites workspace files — that is why it is gated (AGENT_CAD_V1.md).
"""
from __future__ import annotations

import hashlib
import os
import sys
from pathlib import Path
from typing import Any, Mapping

from vkm_cad.detect import DENIED_FRAGMENTS

REG_ROOT = r"Software\Autodesk\AutoCAD"
SKIP_DIRS = {".webview2", "graphicscache", "webservices", "cache"}
MAX_FILES = 20_000


def _denied(name: str) -> bool:
    lowered = name.lower()
    return any(f in lowered for f in DENIED_FRAGMENTS)


def _value_names(handle: int) -> list[str]:
    """Value names only (``RegEnumValueW`` without data): denied values are never read."""
    import ctypes
    from ctypes import wintypes

    advapi = ctypes.windll.advapi32  # type: ignore[attr-defined]
    names: list[str] = []
    buf = ctypes.create_unicode_buffer(16384)
    index = 0
    while True:
        size = wintypes.DWORD(16384)
        rc = advapi.RegEnumValueW(wintypes.HKEY(handle), index, buf, ctypes.byref(size), None, None, None, None)
        if rc != 0:
            return names
        names.append(buf.value)
        index += 1


def registry_snapshot(root: str = REG_ROOT) -> dict[tuple[str, str | None], str]:
    """``{(key path relative to HKCU\\Software\\Autodesk, value name or None for the key itself): digest}``."""
    if sys.platform != "win32":
        return {}
    import winreg

    out: dict[tuple[str, str | None], str] = {}
    base_prefix = "Software\\Autodesk\\"

    def walk(path: str, depth: int) -> None:
        if depth > 12 or _denied(path):
            return
        try:
            key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, path, 0, winreg.KEY_READ)
        except OSError:
            return
        with key:
            rel = path[len(base_prefix):] if path.startswith(base_prefix) else path
            out[(rel, None)] = ""
            for name in _value_names(int(key)):
                if _denied(name):
                    continue
                try:
                    data, kind = winreg.QueryValueEx(key, name)
                except OSError:
                    continue
                out[(rel, name)] = hashlib.sha256(f"{kind}:{data!r}".encode("utf-8", "replace")).hexdigest()[:16]
            j = 0
            subs = []
            while True:
                try:
                    subs.append(winreg.EnumKey(key, j))
                except OSError:
                    break
                j += 1
        for sub in subs:
            walk(f"{path}\\{sub}", depth + 1)

    walk(root, 0)
    return out


def registry_diff(before: dict, after: dict) -> list[dict[str, Any]]:
    changes = []
    for key in sorted(set(before) | set(after), key=lambda k: (k[0], k[1] or "")):
        if before.get(key) == after.get(key):
            continue
        change = "ADDED" if key not in before else "REMOVED" if key not in after else "CHANGED"
        changes.append({"key": key[0], "value": key[1], "change": change})
    return changes


def profile_roots(year: int | None, env: Mapping[str, str] | None = None) -> list[tuple[str, Path]]:
    env = os.environ if env is None else env
    if not year:
        return []
    roots = []
    for var in ("APPDATA", "LOCALAPPDATA"):
        base = env.get(var)
        if not base:
            continue
        for product in (f"AutoCAD {year}", f"C3D {year}"):
            path = Path(base) / "Autodesk" / product
            roots.append((f"<{var}>/Autodesk/{product}", path))
    return roots


def files_snapshot(roots: list[tuple[str, Path]]) -> dict[str, tuple[int, int]]:
    out: dict[str, tuple[int, int]] = {}
    for label, root in roots:
        if not root.is_dir():
            continue
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = [d for d in dirnames if d.lower() not in SKIP_DIRS]
            for name in filenames:
                path = Path(dirpath) / name
                try:
                    st = path.stat()
                except OSError:
                    continue
                out[f"{label}/{path.relative_to(root).as_posix()}"] = (st.st_size, st.st_mtime_ns)
                if len(out) >= MAX_FILES:
                    return out
    return out


def files_diff(before: dict, after: dict) -> list[dict[str, str]]:
    changes = []
    for name in sorted(set(before) | set(after)):
        if before.get(name) == after.get(name):
            continue
        changes.append({"file": name, "change": "ADDED" if name not in before else "REMOVED" if name not in after
                        else "CHANGED"})
    return changes


class Audit:
    """``with Audit(year) as audit: …`` then ``audit.result()`` for the run record."""

    def __init__(self, year: int | None, enabled: bool = True, env: Mapping[str, str] | None = None) -> None:
        self.enabled = enabled
        self.roots = profile_roots(year, env)
        self._reg_before: dict = {}
        self._files_before: dict = {}
        self._result: dict[str, Any] = {"status": "DISABLED"}

    def __enter__(self) -> "Audit":
        if self.enabled:
            self._reg_before = registry_snapshot()
            self._files_before = files_snapshot(self.roots)
        return self

    def __exit__(self, *exc: Any) -> None:
        if not self.enabled:
            return
        reg = registry_diff(self._reg_before, registry_snapshot())
        files = files_diff(self._files_before, files_snapshot(self.roots))
        self._result = {"status": "AUDITED", "registry_root": "HKCU\\" + REG_ROOT, "registry_changes": reg[:200],
                        "registry_changes_count": len(reg), "profile_file_changes": files[:200],
                        "profile_file_changes_count": len(files), "restored": False}

    def result(self) -> dict[str, Any]:
        return self._result
