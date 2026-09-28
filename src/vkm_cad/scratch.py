"""Scratch documents of the CAD bridge: ``<scratch root>/<scratch_doc_id>/{in,out,logs}`` + ``manifest.json``.

The scratch root is ``VKM_CAD_SCRATCH`` or ``$VKM_WORK/cad_scratch``. It may not lie inside ``VKM_RESOURCES_ROOT``,
inside the canonical data root, or inside the repository outside the git-ignored ``work/``. Inputs are *copied* in,
with the SHA-256 of the original taken before and after the copy — a changed original aborts the copy. Nothing outside
the scratch root is ever written, and files in a scratch document are not overwritten without ``overwrite=true``.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import shutil
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from vkm_cad import __version__
from vkm_cad.errors import ToolFailure

DOC_ID = re.compile(r"^CADS-\d{8}T\d{6}Z-[0-9a-f]{8}$")
NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,100}$")
_UNSET = re.compile(r"^\s*$|\$\{[^}]*\}")
MANIFEST_SCHEMA = "vkm-cad.derived_manifest/1"
COPY_CHUNK = 1024 * 1024


def env_value(env: Mapping[str, str], name: str) -> str | None:
    value = env.get(name)
    return None if value is None or _UNSET.search(value) else value.strip()


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(COPY_CHUNK), b""):
            h.update(chunk)
    return h.hexdigest()


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _repo_root() -> Path | None:
    for c in Path(__file__).resolve().parents:
        if (c / "pyproject.toml").is_file() and (c / "src" / "vkm_cad").is_dir():
            return c
    return None


@dataclass
class ScratchStore:
    root: Path | None
    reason: str | None = None

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> "ScratchStore":
        env = os.environ if env is None else env
        explicit = env_value(env, "VKM_CAD_SCRATCH")
        work = env_value(env, "VKM_WORK")
        if not explicit and not work:
            return cls(None, "set VKM_CAD_SCRATCH or VKM_WORK")
        root = Path(explicit) if explicit else Path(work) / "cad_scratch"
        root = root.expanduser().resolve()
        resources = env_value(env, "VKM_RESOURCES_ROOT")
        data = env_value(env, "VKM_DATA_ROOT")
        repo = _repo_root()
        if resources and root.is_relative_to(Path(resources).resolve()):
            return cls(None, "the scratch root lies inside VKM_RESOURCES_ROOT")
        if data and root.is_relative_to(Path(data).resolve() / "canonical"):
            return cls(None, "the scratch root lies inside the canonical data root")
        if repo and root.is_relative_to(repo) and not root.is_relative_to(repo / "work"):
            return cls(None, "the scratch root lies inside the repository outside the git-ignored work/")
        return cls(root)

    def require(self) -> Path:
        if self.root is None:
            raise ToolFailure("SCRATCH_UNAVAILABLE", f"scratch root unavailable: {self.reason}")
        self.root.mkdir(parents=True, exist_ok=True)
        return self.root

    def warnings(self) -> list[str]:
        if self.root is not None and not str(self.root).isascii():
            return ["SCRATCH_PATH_NOT_ASCII"]      # AutoCAD scripts (future accoreconsole backend) need ASCII paths
        return []

    # -------------------------------------------------------------------------------------------- documents
    def new_doc(self) -> tuple[str, Path]:
        root = self.require()
        doc_id = f"CADS-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}-{secrets.token_hex(4)}"
        path = root / doc_id
        for sub in ("in", "out", "logs"):
            (path / sub).mkdir(parents=True, exist_ok=False)
        return doc_id, path

    def doc_dir(self, doc_id: str) -> Path:
        if not DOC_ID.match(doc_id or ""):
            raise ToolFailure("INVALID_ARGUMENT", "scratch_doc_id has the form CADS-YYYYMMDDTHHMMSSZ-xxxxxxxx")
        path = self.require() / doc_id
        if not path.is_dir() or path.is_symlink():
            raise ToolFailure("SCRATCH_DOC_NOT_FOUND", f"scratch document {doc_id} does not exist")
        return path

    def logical(self, doc_id: str, rel: str) -> str:
        return f"scratch:{doc_id}/{rel}"

    def out_path(self, doc_id: str, name: str, overwrite: bool) -> Path:
        if not NAME.match(name) or name in (".", ".."):
            raise ToolFailure("INVALID_ARGUMENT", "file names are ASCII letters, digits, '_', '-', '.' (≤ 100)")
        path = self.doc_dir(doc_id) / "out" / name
        if path.exists() and not overwrite:
            raise ToolFailure("WOULD_OVERWRITE", f"out/{name} exists in {doc_id}; pass overwrite=true")
        return path

    def copy_in(self, doc_id: str, source: Path, name: str) -> dict[str, Any]:
        """Copy ``source`` into ``in/`` with SHA-256 before and after (the original is only ever read)."""
        target = self.doc_dir(doc_id) / "in" / name
        before = sha256_file(source)
        with open(source, "rb") as src, open(target, "xb") as dst:
            shutil.copyfileobj(src, dst, COPY_CHUNK)
        after = sha256_file(source)
        copied = sha256_file(target)
        if not before == after == copied:
            target.unlink(missing_ok=True)
            raise ToolFailure("SOURCE_CHANGED_DURING_COPY", "the original changed while it was copied; try again",
                              retryable=True)
        return {"path": f"in/{name}", "sha256": copied, "bytes": target.stat().st_size,
                "source_sha256_before": before, "source_sha256_after": after}

    def write_bytes(self, doc_id: str, rel: str, data: bytes) -> dict[str, Any]:
        path = self.doc_dir(doc_id) / rel
        tmp = path.with_name(f".{path.name}.{secrets.token_hex(4)}.tmp")
        tmp.write_bytes(data)
        os.replace(tmp, path)
        return {"path": rel, "sha256": hashlib.sha256(data).hexdigest(), "bytes": len(data)}

    def manifest(self, doc_id: str) -> dict[str, Any]:
        path = self.doc_dir(doc_id) / "manifest.json"
        return json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}

    def write_manifest(self, doc_id: str, manifest: dict[str, Any]) -> None:
        manifest = {"schema": MANIFEST_SCHEMA, "scratch_doc_id": doc_id, "bridge_version": __version__, **manifest}
        text = json.dumps(manifest, ensure_ascii=False, indent=1, sort_keys=True) + "\n"
        self.write_bytes(doc_id, "manifest.json", text.encode("utf-8"))

    def list_docs(self) -> list[str]:
        root = self.require()
        return sorted(p.name for p in root.iterdir() if p.is_dir() and DOC_ID.match(p.name))
