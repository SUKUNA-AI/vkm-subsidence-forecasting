"""Write roots, path jail, format policy and atomic writes for ``vkm-drawio``.

Roots (other roots only through configuration, never as a tool argument):

* ``public`` — ``<PUBLIC>/docs/diagrams`` (committed). The repository is found from ``CLAUDE_PROJECT_DIR``, the current
  directory or the package location (``pyproject.toml`` + ``src/vkm_drawio``). Formats: ``.drawio``, ``.svg``, ``.png``;
  the leakage policy (:mod:`vkm_drawio.policy`) applies to everything written here.
* ``work`` — ``$VKM_WORK/diagrams`` (drafts; PDF only here). Refused when ``VKM_WORK`` lies inside
  ``VKM_RESOURCES_ROOT`` or inside the repository outside the git-ignored ``work/``.

A path argument is relative, at most 200 characters, without ``..``, drive letters, UNC prefixes, ``:`` (alternate data
streams), Windows device names, trailing dots/spaces or reparse points (junctions, symlinks) under the root.
"""
from __future__ import annotations

import os
import re
import secrets
import stat
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

from vkm_drawio.errors import ToolFailure
from vkm_drawio.locate import env_value

ROOT_NAMES = ("public", "work")
PUBLIC_SUFFIXES = frozenset({".drawio", ".svg", ".png"})
WORK_SUFFIXES = frozenset({".drawio", ".svg", ".png", ".pdf", ".jpg", ".jpeg", ".xml"})
READABLE_SUFFIXES = frozenset({".drawio", ".xml", ".svg", ".png"})
MAX_REL_PATH = 200
MAX_LIST = 1000
_DEVICE = re.compile(r"^(con|prn|aux|nul|com[0-9¹²³]|lpt[0-9¹²³]|conin\$|conout\$)(\..*)?$", re.IGNORECASE)
_BAD_CHARS = re.compile(r'[<>"|?*\x00-\x1f:]')
_GLOB_BAD = re.compile(r'[<>"|\x00-\x1f:]')


def _is_reparse(p: Path) -> bool:
    try:
        st = os.lstat(p)
    except FileNotFoundError:
        return False
    if stat.S_ISLNK(st.st_mode):
        return True
    return bool(getattr(st, "st_file_attributes", 0) & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400))


def check_relative(rel: str, *, allow_glob: bool = False) -> list[str]:
    """Validate a relative path (or glob) and return its parts."""
    if not isinstance(rel, str) or not rel.strip():
        raise ToolFailure("PATH_OUTSIDE_WORKSPACE", "empty path")
    if len(rel) > MAX_REL_PATH:
        raise ToolFailure("PATH_OUTSIDE_WORKSPACE", f"path longer than {MAX_REL_PATH} characters")
    if rel.startswith(("/", "\\")) or re.match(r"^[A-Za-z]:", rel):
        raise ToolFailure("PATH_OUTSIDE_WORKSPACE", "absolute paths are not accepted; use a path relative to the root")
    bad = _GLOB_BAD if allow_glob else _BAD_CHARS
    if bad.search(rel):
        raise ToolFailure("PATH_OUTSIDE_WORKSPACE", "path contains a forbidden character (':', '<', '>', '|', …)")
    parts = re.split(r"[\\/]", rel)
    for part in parts:
        if part in ("", ".", ".."):
            raise ToolFailure("PATH_OUTSIDE_WORKSPACE", "empty, '.' or '..' path components are not accepted")
        if part != part.rstrip(" .") and not (allow_glob and part == "**"):
            raise ToolFailure("PATH_OUTSIDE_WORKSPACE", "path components may not end with a dot or a space")
        if _DEVICE.match(part):
            raise ToolFailure("PATH_OUTSIDE_WORKSPACE", f"reserved device name {part!r}")
    return parts


def suffix_of(path: str | Path) -> str:
    return Path(str(path)).suffix.lower()


def find_repo_root(env: Mapping[str, str]) -> Path | None:
    candidates: list[Path] = []
    project = env_value(env, "CLAUDE_PROJECT_DIR")
    if project:
        candidates.append(Path(project))
    cwd = Path.cwd()
    candidates += [cwd, *cwd.parents]
    candidates += list(Path(__file__).resolve().parents)
    for c in candidates:
        if (c / "pyproject.toml").is_file() and (c / "src" / "vkm_drawio").is_dir():
            return c.resolve()
    return None


@dataclass
class Workspace:
    public_root: Path | None
    work_root: Path | None
    public_reason: str | None = None
    work_reason: str | None = None

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> "Workspace":
        env = os.environ if env is None else env
        repo = find_repo_root(env)
        public = repo / "docs" / "diagrams" if repo else None
        work_root, work_reason = None, "VKM_WORK is not set"
        work = env_value(env, "VKM_WORK")
        if work:
            from vkm_corpus.config import ConfigError, load_settings   # configuration only through load_settings

            try:
                settings = load_settings({k: v for k, v in env.items()})
            except ConfigError as exc:
                return cls(public_root=public, work_root=None, work_reason=f"configuration error: {exc}",
                           public_reason=None if public else "repository root not found (set CLAUDE_PROJECT_DIR)")
            base = (settings.work_root or Path(work)).expanduser().resolve()
            resources = settings.resources_root.resolve() if settings.resources_root and env_value(
                env, "VKM_RESOURCES_ROOT") else None
            if resources is not None and base.is_relative_to(resources):
                work_reason = "VKM_WORK lies inside VKM_RESOURCES_ROOT"
            elif repo is not None and base.is_relative_to(repo) and not base.is_relative_to(repo / "work"):
                work_reason = "VKM_WORK lies inside the repository outside the git-ignored work/"
            else:
                work_root, work_reason = base / "diagrams", None
        return cls(public_root=public, work_root=work_root,
                   public_reason=None if public else "repository root not found (set CLAUDE_PROJECT_DIR)",
                   work_reason=work_reason)

    # -------------------------------------------------------------------------------------------- roots and paths
    def root(self, name: str) -> Path:
        if name not in ROOT_NAMES:
            raise ToolFailure("PATH_OUTSIDE_WORKSPACE", f"unknown root {name!r}; use 'public' or 'work'")
        path = self.public_root if name == "public" else self.work_root
        if path is None:
            reason = self.public_reason if name == "public" else self.work_reason
            raise ToolFailure("ROOT_UNAVAILABLE", f"root {name!r} is not available: {reason}")
        return path

    def resolve(self, root_name: str, rel: str) -> Path:
        root = self.root(root_name)
        parts = check_relative(rel)
        root_resolved = root.resolve()
        target = root_resolved.joinpath(*parts)
        cur = root_resolved
        for part in parts:
            cur = cur / part
            if _is_reparse(cur):
                raise ToolFailure("PATH_OUTSIDE_WORKSPACE", "the path crosses a junction or symbolic link")
        resolved = target.resolve()
        if not resolved.is_relative_to(root_resolved):
            raise ToolFailure("PATH_OUTSIDE_WORKSPACE", "the path resolves outside the root")
        return resolved

    def logical(self, root_name: str, path: Path) -> str:
        rel = path.resolve().relative_to(self.root(root_name).resolve()).as_posix()
        return f"{root_name}:{rel}"

    def relative(self, root_name: str, path: Path) -> str:
        return path.resolve().relative_to(self.root(root_name).resolve()).as_posix()

    def check_format(self, root_name: str, path: Path) -> str:
        suffix = suffix_of(path)
        allowed = PUBLIC_SUFFIXES if root_name == "public" else WORK_SUFFIXES
        if suffix not in allowed:
            code = "FORMAT_NOT_ALLOWED_IN_ROOT" if suffix in WORK_SUFFIXES else "FORMAT_NOT_SUPPORTED"
            raise ToolFailure(code, f"'{suffix or path.name}' files are not allowed in root {root_name!r}",
                              details={"allowed": sorted(allowed)})
        return suffix

    def list_files(self, root_name: str, pattern: str) -> list[Path]:
        root = self.root(root_name).resolve()
        check_relative(pattern, allow_glob=True)
        if not root.is_dir():
            return []
        out: list[Path] = []
        for p in sorted(root.glob(pattern), key=lambda q: q.as_posix()):
            if not p.is_file() or p.name.startswith(".") or suffix_of(p) not in (PUBLIC_SUFFIXES | WORK_SUFFIXES):
                continue
            try:
                rel = p.relative_to(root).as_posix()
                if self.resolve(root_name, rel) != p.resolve():
                    continue
            except ToolFailure:
                continue
            out.append(p)
            if len(out) >= MAX_LIST:
                break
        return out


# ------------------------------------------------------------------------------------------------ atomic write
def atomic_write(path: Path, data: bytes, *, overwrite: bool) -> None:
    """Write via a temporary file in the same directory; without ``overwrite`` an existing target is never replaced
    (also not by a concurrent writer: the final step fails instead)."""
    if path.exists() and not overwrite:
        raise ToolFailure("WOULD_OVERWRITE", f"{path.name} exists; pass overwrite=true to replace it")
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.parent / f".{path.name}.vkm-{secrets.token_hex(4)}.tmp"
    try:
        with open(tmp, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        if overwrite:
            os.replace(tmp, path)
        elif sys.platform == "win32":
            os.rename(tmp, path)                      # fails if the target appeared meanwhile
        else:
            os.link(tmp, path)                        # fails if the target exists
            os.unlink(tmp)
    except FileExistsError as exc:
        raise ToolFailure("WOULD_OVERWRITE", f"{path.name} appeared while writing; nothing replaced") from exc
    finally:
        if tmp.exists():
            tmp.unlink()
