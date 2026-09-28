"""The job root ``VKM_SIM_ROOT`` and the path jail of the job layer.

``VKM_SIM_ROOT`` is mandatory. It must be an absolute ASCII path without spaces on a fixed local disk, outside the
PUBLIC and PRIVATE clones and outside the canonical data root (MAPDL, ``accoreconsole`` and the MATLAB MCP server
break on Cyrillic and DOS 8.3 paths; the clone path of this project is Cyrillic). Layout below the root::

    jobs/<job_id>/{job.json,status.json,receipt.json,in/,work/,out/,logs/}
    sessions/  locks/  cache/  logs/  tools/  matlab/session/

Paths given by a client are always *relative* to a job directory (or another known root) and pass
:func:`clean_relpath` / :func:`resolve_inside`: no absolute paths, drive letters, UNC shares, ``:``, ``..`` or links
(symlinks and junctions) — otherwise ``PATH_OUTSIDE_ROOT``.
"""
from __future__ import annotations

import os
import re
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping

from vkm_jobs.errors import ToolFailure

LOGICAL_ROOT = "<VKM_SIM_ROOT>"
SUBDIRS = ("jobs", "sessions", "locks", "cache", "logs", "tools", "matlab/session")
LOW_DISK_GB = 50.0            # below this a warning is reported (MAPDL scratch needs room)
RECOMMENDED_FREE_GB = 100.0
_UNSET = re.compile(r"^\s*$|\$\{[^}]*\}")
_DRIVE = re.compile(r"^[A-Za-z]:")


def env_value(env: Mapping[str, str] | None, name: str) -> str | None:
    """Value of ``name``; empty values and unexpanded ``${VAR}`` placeholders count as unset."""
    env = os.environ if env is None else env
    value = env.get(name)
    return None if value is None or _UNSET.search(value) else value.strip()


def _norm(path: Path) -> str:
    return os.path.normcase(os.path.abspath(str(path)))


def is_inside(path: Path, base: Path) -> bool:
    """``path`` equals or lies below ``base`` (case-insensitive on Windows, no filesystem access)."""
    p, b = _norm(path), _norm(base)
    return p == b or p.startswith(b.rstrip("\\/") + os.sep)


def repo_root(env: Mapping[str, str] | None = None) -> Path | None:
    """The PUBLIC clone this code runs from (``VKM_PUBLIC_ROOT`` overrides), or ``None`` for a plain install."""
    explicit = env_value(env, "VKM_PUBLIC_ROOT")
    if explicit:
        return Path(explicit).resolve()
    for candidate in Path(__file__).resolve().parents:
        if (candidate / "pyproject.toml").is_file() and (candidate / "src" / "vkm_jobs").is_dir():
            return candidate
    return None


def _fixed_local_disk(path: Path) -> bool:
    if os.name != "nt":
        return True
    import ctypes

    anchor = Path(path).anchor
    if not anchor:
        return False
    return ctypes.windll.kernel32.GetDriveTypeW(ctypes.c_wchar_p(anchor)) == 3     # DRIVE_FIXED


def check_root(path: Path, env: Mapping[str, str] | None = None) -> str | None:
    """Why ``path`` cannot be the job root, or ``None`` when it is acceptable."""
    text = str(path)
    if not path.is_absolute():
        return "VKM_SIM_ROOT must be an absolute path"
    if not text.isascii():
        return "VKM_SIM_ROOT must be ASCII only (MAPDL, accoreconsole and the MATLAB MCP server need ASCII paths)"
    if any(ch.isspace() for ch in text):
        return "VKM_SIM_ROOT must not contain spaces"
    if text.startswith(("\\\\", "//")):
        return "VKM_SIM_ROOT must be on a local disk, not a network share"
    if not _fixed_local_disk(path):
        return "VKM_SIM_ROOT must be on a fixed local disk"
    repo = repo_root(env)
    if repo is not None and is_inside(path, repo):
        return "VKM_SIM_ROOT lies inside the PUBLIC clone"
    for name in ("VKM_RESOURCES_ROOT",):
        other = env_value(env, name)
        if other and is_inside(path, Path(other)):
            return f"VKM_SIM_ROOT lies inside {name}"
    data = env_value(env, "VKM_DATA_ROOT")
    if data and is_inside(path, Path(data) / "canonical"):
        return "VKM_SIM_ROOT lies inside the canonical data root"
    return None


def free_gb(path: Path) -> float | None:
    probe = Path(path)
    while not probe.exists() and probe.parent != probe:
        probe = probe.parent
    try:
        return round(shutil.disk_usage(probe).free / 1e9, 1)
    except OSError:
        return None


@dataclass
class SimRoot:
    """The validated job root (``path is None`` with a ``reason`` when unavailable)."""

    path: Path | None
    reason: str | None = None
    warnings: list[str] = field(default_factory=list)

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> "SimRoot":
        raw = env_value(env, "VKM_SIM_ROOT")
        if not raw:
            return cls(None, "set VKM_SIM_ROOT (absolute ASCII path on a local data disk, outside the clones)")
        return cls.at(Path(raw).expanduser(), env)

    @classmethod
    def at(cls, path: Path, env: Mapping[str, str] | None = None) -> "SimRoot":
        reason = check_root(Path(path), env)
        if reason:
            return cls(None, reason)
        root = Path(os.path.abspath(path))
        warnings = []
        free = free_gb(root)
        if free is not None and free < LOW_DISK_GB:
            warnings.append(f"SIM_ROOT_LOW_DISK: {free} GB free (< {LOW_DISK_GB:g} GB)")
        return cls(root, None, warnings)

    def require(self) -> Path:
        if self.path is None:
            raise ToolFailure("SIM_ROOT_UNAVAILABLE", f"job root unavailable: {self.reason}")
        for sub in SUBDIRS:
            (self.path / sub).mkdir(parents=True, exist_ok=True)
        return self.path

    @property
    def jobs(self) -> Path:
        return self.require() / "jobs"

    @property
    def locks(self) -> Path:
        return self.require() / "locks"

    @property
    def cache(self) -> Path:
        return self.require() / "cache"

    @property
    def logs(self) -> Path:
        return self.require() / "logs"

    @property
    def tools(self) -> Path:
        return self.require() / "tools"

    def logical(self, path: Path | str) -> str:
        """``<VKM_SIM_ROOT>/…`` for a path below the root (forward slashes); other paths are not rewritten here."""
        if self.path is None:
            return str(path)
        p = Path(path)
        if is_inside(p, self.path):
            rel = os.path.relpath(os.path.abspath(p), os.path.abspath(self.path)).replace("\\", "/")
            return LOGICAL_ROOT if rel == "." else f"{LOGICAL_ROOT}/{rel}"
        return str(path)

    def describe(self) -> dict[str, Any]:
        out: dict[str, Any] = {"logical": LOGICAL_ROOT, "available": self.path is not None, "reason": self.reason,
                               "warnings": list(self.warnings)}
        if self.path is not None:
            out["free_gb"] = free_gb(self.path)
            out["recommended_free_gb"] = RECOMMENDED_FREE_GB
        return out


# --------------------------------------------------------------------------------------------------- path jail
def clean_relpath(rel: str, *, what: str = "path") -> str:
    """Normalised relative POSIX path, or ``PATH_OUTSIDE_ROOT`` for anything that could leave its root."""
    if not isinstance(rel, str) or not rel.strip():
        raise ToolFailure("INVALID_ARGUMENT", f"{what} is empty")
    if "\x00" in rel:
        raise ToolFailure("PATH_OUTSIDE_ROOT", f"{what} contains a NUL character")
    text = rel.strip().replace("\\", "/")
    if text.startswith("/") or _DRIVE.match(text) or ":" in text:
        raise ToolFailure("PATH_OUTSIDE_ROOT", f"{what} must be relative (no drive, UNC, ':' or leading '/')",
                          details={"path": rel[:200]})
    parts = [p for p in text.split("/") if p not in ("", ".")]
    if not parts or ".." in parts:
        raise ToolFailure("PATH_OUTSIDE_ROOT", f"{what} must stay inside its root (no '..')", details={"path": rel[:200]})
    return "/".join(parts)


def _is_link(path: Path) -> bool:
    return path.is_symlink() or bool(getattr(path, "is_junction", lambda: False)())


def resolve_inside(base: Path, rel: str, *, what: str = "path") -> Path:
    """``base / rel`` after :func:`clean_relpath`, refusing links on the way and anything resolving outside."""
    clean = clean_relpath(rel, what=what)
    current = Path(base)
    for part in clean.split("/"):
        current = current / part
        if _is_link(current):
            raise ToolFailure("PATH_OUTSIDE_ROOT", f"{what} passes through a link", details={"path": clean})
    if not is_inside(current.resolve(), Path(base).resolve()):
        raise ToolFailure("PATH_OUTSIDE_ROOT", f"{what} resolves outside its root", details={"path": clean})
    return current
