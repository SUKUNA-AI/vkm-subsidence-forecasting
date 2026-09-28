"""Find the draw.io Desktop executable (never cached across processes: the Store install path is versioned).

Order: ``VKM_DRAWIO_EXE`` (ignored when empty or an unexpanded ``${…}`` literal, as Claude Code leaves unset variables)
→ Microsoft Store package ``draw.io.draw.ioDiagrams`` via ``Get-AppxPackage`` (``<InstallLocation>\\app\\draw.io.exe``)
→ ``%ProgramFiles%\\draw.io`` → ``%LOCALAPPDATA%\\Programs\\draw.io`` → ``PATH`` (``draw.io``, ``drawio``).

Reported locations are logical (``<APPX:…>``, ``%ProgramFiles%``, ``$VKM_DRAWIO_EXE``), never machine paths.
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Mapping

APPX_NAME = "draw.io.draw.ioDiagrams"
APPX_QUERY = f"(Get-AppxPackage -Name '{APPX_NAME}').InstallLocation"
_UNSET = re.compile(r"^\s*$|\$\{[^}]*\}")


def env_value(env: Mapping[str, str], name: str) -> str | None:
    """Environment value, or ``None`` when unset, empty or an unexpanded ``${VAR}`` literal."""
    value = env.get(name)
    if value is None or _UNSET.search(value):
        return None
    return value.strip()


@dataclass(frozen=True)
class DrawioLocation:
    found: bool
    discovery: str | None = None          # ENV | APPX | PROGRAM_FILES | LOCALAPPDATA | PATH
    exe: Path | None = None
    exe_logical: str | None = None
    reason: str | None = None

    def public(self) -> dict[str, object]:
        return {"found": self.found, "discovery": self.discovery, "exe_logical": self.exe_logical,
                "reason": self.reason}


def _no_window() -> int:
    return getattr(subprocess, "CREATE_NO_WINDOW", 0) if sys.platform == "win32" else 0


def appx_install_location(timeout: float = 15.0) -> str | None:
    """InstallLocation of the Store package via PowerShell (Windows only)."""
    if sys.platform != "win32":
        return None
    try:
        proc = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", APPX_QUERY],
                              capture_output=True, text=True, timeout=timeout, creationflags=_no_window())
    except (OSError, subprocess.TimeoutExpired):
        return None
    out = (proc.stdout or "").strip().splitlines()
    return out[0].strip() if proc.returncode == 0 and out and out[0].strip() else None


def locate(env: Mapping[str, str] | None = None, *, appx: Callable[[], str | None] = appx_install_location,
           exists: Callable[[Path], bool] = lambda p: p.is_file(),
           which: Callable[[str], str | None] = shutil.which) -> DrawioLocation:
    env = os.environ if env is None else env
    override = env_value(env, "VKM_DRAWIO_EXE")
    if override is not None:
        path = Path(override)
        if exists(path):
            return DrawioLocation(True, "ENV", path, "$VKM_DRAWIO_EXE")
        return DrawioLocation(False, "ENV", None, "$VKM_DRAWIO_EXE", "VKM_DRAWIO_EXE does not point to a file")
    location = appx()
    if location:
        path = Path(location) / "app" / "draw.io.exe"
        if exists(path):
            return DrawioLocation(True, "APPX", path, f"<APPX:{APPX_NAME}>\\app\\draw.io.exe")
    candidates = []
    if env_value(env, "ProgramFiles"):
        candidates.append(("PROGRAM_FILES", Path(env["ProgramFiles"]) / "draw.io" / "draw.io.exe",
                           "%ProgramFiles%\\draw.io\\draw.io.exe"))
    if env_value(env, "LOCALAPPDATA"):
        candidates.append(("LOCALAPPDATA", Path(env["LOCALAPPDATA"]) / "Programs" / "draw.io" / "draw.io.exe",
                           "%LOCALAPPDATA%\\Programs\\draw.io\\draw.io.exe"))
    for discovery, path, logical in candidates:
        if exists(path):
            return DrawioLocation(True, discovery, path, logical)
    for name in ("draw.io", "drawio"):
        hit = which(name)
        if hit:
            return DrawioLocation(True, "PATH", Path(hit), f"PATH:{name}")
    return DrawioLocation(False, reason="draw.io Desktop not found (Store package, Program Files, PATH); "
                                        "set VKM_DRAWIO_EXE to override")
