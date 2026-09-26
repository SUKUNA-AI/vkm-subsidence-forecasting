#!/usr/bin/env python3
"""Canonical repository verifier v2 (read-only: no models, no evaluator-truth parsing, no network).

Checks; every check emits structured entries ``{id, group, status, blocking, summary, details}``:

1. ``frozen_references``  FROZEN_REFERENCE registry (``scripts/frozen_references.json``) verified from git
                          objects: plain blobs by sha256 of ``git cat-file blob <ref>:<path>``, Git LFS pointers
                          by their ``oid sha256`` (the LFS oid is the content sha256). Manifest closures are walked.
2. ``retired_references`` retired v3.2 inputs referenced by frozen manifests, verified at archive commit 10452b0.
3. ``private_sources``    only with ``VKM_RESOURCES_ROOT``/``--resources-root``: PRIVATE SOURCE_REGISTER readable,
                          sha256 of registered files on disk, PUBLIC catalogue rows (VKM-SRC id + sha256) match it.
                          The duplicate copies under ``08_data_archives/main_repo_snapshots`` are never read.
4. ``markdown_links``     local Markdown links resolve inside the repository (URLs and anchors ignored).
5. ``leakage``            ``vkm_world.governance.leakage.scan()`` reports no problems.
6. ``host_paths``         no absolute/Windows host paths in executable code/config outside documented exemptions.
7. ``worldspec_schema``   ``schemas/worldspec_vnext.schema.json`` equals the schema generated from code.

A missing git ref (shallow clone, tags not fetched) gives ``SKIPPED_REF_UNAVAILABLE`` (non-blocking) with
instructions. Exit code 0 only if no blocking check FAILs. The JSON report goes to stdout and, with
``--output``, to a file (default ``work/verification/canonical_verification.json``).

Usage::

    python scripts/verify_canonical_repository.py
    VKM_RESOURCES_ROOT=<private checkout> python scripts/verify_canonical_repository.py --output
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
import os
import posixpath
import re
import subprocess
import sys
import time
import traceback
from dataclasses import asdict, dataclass, field
from pathlib import Path
from urllib.parse import unquote

VERSION = "2.0.0"
DEFAULT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_REGISTRY = Path(__file__).resolve().with_name("frozen_references.json")
DEFAULT_OUTPUT = "work/verification/canonical_verification.json"

PASS, FAIL, WARN = "PASS", "FAIL", "WARN"
PASS_WITH_NONBLOCKING = "PASS_WITH_NONBLOCKING"   # overall: no blocking FAIL, but SKIPPED/WARN entries exist
SKIPPED, SKIPPED_REF = "SKIPPED", "SKIPPED_REF_UNAVAILABLE"
GROUPS = ("registry", "frozen_references", "retired_references", "private_sources", "markdown_links", "leakage",
          "host_paths", "worldspec_schema")

REF_HELP = ("ref not available locally (shallow clone or tags/branches not fetched): run "
            "'git fetch --unshallow origin' (if shallow), 'git fetch origin legacy:legacy' and "
            "'git fetch origin --tags', then re-run; see docs/legacy/LEGACY_INDEX_RU.md")
LFS_MAGIC = b"version https://git-lfs.github.com/spec/v1"
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
COMMIT_RE = re.compile(r"^[0-9a-f]{40}$")
VKM_SRC_RE = re.compile(r"^VKM-SRC-\d+$")
MAX_LIST = 50                                    # cap of error/hit lists in the report
MAX_SCAN_BYTES = 5_000_000                       # host-path scan skips larger text files

# private sources
REGISTER_REL = "00_registry/SOURCE_REGISTER.csv"
REGISTER_REQUIRED = ("resource_id", "canonical_path", "sha256")
DUPLICATE_SNAPSHOT_PREFIX = "08_data_archives/main_repo_snapshots/"   # never read (byte duplicates)
REGISTER_ABSENCE_RE = re.compile(r"DELETED|RETIRED", re.I)   # migration_status/evidence_scope: absence expected
CATALOGUE_DIRS = ("evidence", "catalogues")
CATALOGUE_ID_COLUMNS = {"source_id", "resource_id", "vkm_src_id", "vkm_source_id", "src_id"}
CATALOGUE_SHA_COLUMNS = {"sha256", "source_sha256", "file_sha256", "register_sha256"}

# markdown
FENCE_RE = re.compile(r"^[ \t]{0,3}(```|~~~)[^\n]*\n.*?^[ \t]{0,3}\1[^\n]*$", re.M | re.S)
INLINE_CODE_RE = re.compile(r"(`+)(?:(?!\1).)+?\1", re.S)
HTML_COMMENT_RE = re.compile(r"<!--.*?-->", re.S)
INLINE_LINK_RE = re.compile(
    r"!?\[(?:[^\[\]\n]|\[[^\[\]\n]*\])*\]\(\s*(<[^>\n]+>|(?:[^\s()]|\([^\s()]*\))+)(?:\s+(?:\"[^\"\n]*\"|'[^'\n]*'))?\s*\)")
REF_DEF_RE = re.compile(r"^[ \t]{0,3}\[[^\]\n]+\]:[ \t]*(<[^>\n]+>|\S+)", re.M)
SCHEME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9+.\-]+:")      # http:, mailto:, data:, app: ... (not 'C:')

# host paths; patterns are written so that this file does not match itself
HOST_PATH_SUFFIXES = {".py", ".yaml", ".yml", ".json", ".toml"}
# Documented exemptions (reported in every run, never extended silently): prefix -> reason.
HOST_PATH_EXEMPTIONS = {
    "docs/reset_2026_09/run_kit/": "historical cloud run kit, rewritten by localize_paths.sh (DATA_AND_PATH_POLICY_RU §3)",
    "tests/": "negative fixtures of path/leakage guards; a real host-path dependency breaks the tests elsewhere",
    "src/vkm_world/governance/leakage.py": "forbidden-fragment patterns of the leakage guard itself",
}
HOST_PATH_PRAGMA = "host-path-ok"            # a line carrying this marker (with a reason) is exempt
HOST_PATH_PATTERNS = (
    ("windows_drive", re.compile(r"(?<![A-Za-z0-9_])[A-Z]:(?:\\{1,2}|/)(?=\w)")),
    ("windows_venv", re.compile(r"\.venv(?:\\{1,2}|/)Scripts(?:\\{1,2}|/)python", re.I)),
    ("posix_host_dir", re.compile(r"(?<![\w.~/\-])/(?:home|Users|root|mnt|media)/[\w.\-]")),
)
WALK_SKIP_DIRS = {".git", "work", "__pycache__", ".pytest_cache", "node_modules"}


@dataclass
class Check:
    id: str
    group: str
    status: str
    blocking: bool
    summary: str
    details: dict = field(default_factory=dict)


def _capped(items: list, limit: int = MAX_LIST) -> dict:
    return {"count": len(items), "items": items[:limit], "truncated": max(0, len(items) - limit)}


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def parse_lfs_pointer(data: bytes) -> tuple[str, int | None] | None:
    """Return (oid, size) if ``data`` is a Git LFS v1 pointer, else None."""
    if not data.startswith(LFS_MAGIC) or len(data) > 1024:
        return None
    oid, size = None, None
    for line in data.decode("utf-8", "replace").splitlines():
        if line.startswith("oid sha256:"):
            oid = line.split(":", 1)[1].strip()
        elif line.startswith("size "):
            try:
                size = int(line[5:].strip())
            except ValueError:
                size = None
    return (oid, size) if oid else None


def content_identity(data: bytes) -> tuple[str, str, int]:
    """(sha256, method, size) of a git blob; an LFS pointer is identified by its oid and declared size."""
    pointer = parse_lfs_pointer(data)
    if pointer:
        return pointer[0], "lfs_oid", pointer[1] if pointer[1] is not None else -1
    return sha256_bytes(data), "git_blob", len(data)


# --------------------------------------------------------------------------- git access (read-only)
class Git:
    """Read-only git access: rev-parse and one persistent ``git cat-file --batch`` process."""

    def __init__(self, root: Path):
        self.root = root
        self._batch: subprocess.Popen | None = None
        self._resolved: dict[str, str | None] = {}
        self.available = self.run("rev-parse", "--is-inside-work-tree") == "true"

    def run(self, *args: str) -> str | None:
        try:
            done = subprocess.run(["git", "-C", str(self.root), *args], capture_output=True, text=True, timeout=120)
        except (OSError, subprocess.SubprocessError):
            return None
        return done.stdout.strip() if done.returncode == 0 else None

    def resolve(self, ref: str) -> str | None:
        """Full commit SHA for a branch/tag/SHA, or None if not available locally."""
        if ref not in self._resolved:
            self._resolved[ref] = self.run("rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}") if self.available else None
        return self._resolved[ref]

    def resolve_ref(self, ref: str) -> str | None:
        """Like ``resolve`` but a branch name also falls back to ``origin/<name>`` (fresh clones)."""
        commit = self.resolve(ref)
        if commit is None and not COMMIT_RE.match(ref):
            commit = self.resolve(f"refs/remotes/origin/{ref}")
        return commit

    def resolve_named(self, name: str, kind: str) -> str | None:
        refs = [f"refs/tags/{name}"] if kind == "tag" else [f"refs/heads/{name}", f"refs/remotes/origin/{name}"]
        for ref in refs:
            commit = self.resolve(ref)
            if commit:
                return commit
        return None

    def is_ancestor(self, commit: str, of: str) -> bool:
        try:
            done = subprocess.run(["git", "-C", str(self.root), "merge-base", "--is-ancestor", commit, of],
                                  capture_output=True, timeout=120)
        except (OSError, subprocess.SubprocessError):
            return False
        return done.returncode == 0

    def read(self, commit: str, path: str) -> bytes | None:
        """Blob bytes of ``<commit>:<path>``; None if the path is absent or not a blob."""
        if "\n" in path:
            return None
        if self._batch is None:
            self._batch = subprocess.Popen(["git", "-C", str(self.root), "cat-file", "--batch"],
                                           stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        proc = self._batch
        assert proc.stdin is not None and proc.stdout is not None
        proc.stdin.write(f"{commit}:{path}\n".encode("utf-8"))
        proc.stdin.flush()
        header = proc.stdout.readline().decode("utf-8", "replace").rstrip("\n").split(" ")
        # found: "<oid> <type> <size>"; otherwise "<name> missing|ambiguous" (the name may contain spaces)
        if len(header) != 3 or not re.fullmatch(r"[0-9a-f]{40,64}", header[0]) or not header[2].isdigit():
            return None
        _, kind, size = header
        data = proc.stdout.read(int(size))
        proc.stdout.read(1)                        # trailing LF
        return data if kind == "blob" else None

    def close(self) -> None:
        if self._batch is not None:
            try:
                self._batch.stdin.close()          # type: ignore[union-attr]
                self._batch.wait(timeout=10)
            except Exception:  # noqa: BLE001
                self._batch.kill()
            self._batch = None


# --------------------------------------------------------------------------- context
@dataclass
class Context:
    root: Path
    registry_path: Path
    resources_root: Path | None
    resources_source: str | None
    git: Git
    registry: dict | None = None
    registry_error: str | None = None
    register: dict[str, dict] | None = None          # resource_id -> row
    register_by_sha: dict[str, str] | None = None     # sha256 -> resource_id
    register_error: str | None = None
    register_duplicates: list[str] = field(default_factory=list)
    retired_seen: dict[str, dict] = field(default_factory=dict)   # path -> {sha256s, via}
    repo_files_cache: list[str] | None = None

    def repo_files(self) -> list[str]:
        """Repository files that exist in the working tree (tracked + untracked-not-ignored)."""
        if self.repo_files_cache is None:
            names: list[str] | None = None
            if self.git.available:
                out = self.git.run("ls-files", "-z", "--cached", "--others", "--exclude-standard")
                if out is not None:
                    names = [n for n in out.split("\0") if n]
            if names is None:
                names = []
                for dirpath, dirnames, filenames in os.walk(self.root):
                    dirnames[:] = [d for d in dirnames if d not in WALK_SKIP_DIRS and not d.startswith(".venv")]
                    rel = Path(dirpath).relative_to(self.root)
                    names.extend((rel / f).as_posix() for f in filenames)
            self.repo_files_cache = sorted({n for n in names if (self.root / n).is_file()})
        return self.repo_files_cache


def load_registry(ctx: Context) -> None:
    try:
        ctx.registry = json.loads(ctx.registry_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        ctx.registry_error = f"{type(exc).__name__}: {exc}"


def load_private_register(ctx: Context) -> None:
    if ctx.resources_root is None:
        return
    path = ctx.resources_root / REGISTER_REL
    try:
        with path.open(encoding="utf-8-sig", newline="") as stream:
            reader = csv.DictReader(stream)
            header = reader.fieldnames or []
            rows = list(reader)
    except (OSError, UnicodeDecodeError, csv.Error) as exc:
        ctx.register_error = f"cannot read {REGISTER_REL}: {type(exc).__name__}: {exc}"
        return
    missing = [c for c in REGISTER_REQUIRED if c not in header]
    if missing:
        ctx.register_error = f"{REGISTER_REL} lacks columns {missing}"
        return
    ctx.register, ctx.register_by_sha = {}, {}
    for row in rows:
        rid = (row.get("resource_id") or "").strip()
        sha = (row.get("sha256") or "").strip().lower()
        if rid in ctx.register:
            ctx.register_duplicates.append(rid)
        elif rid:
            ctx.register[rid] = row
        if SHA256_RE.match(sha):
            ctx.register_by_sha.setdefault(sha, rid)


# --------------------------------------------------------------------------- 0. registry
def check_registry(ctx: Context) -> list[Check]:
    rel = _display_path(ctx.registry_path, ctx.root)
    if ctx.registry is None:
        return [Check("registry:load", "registry", FAIL, True, f"cannot load registry {rel}",
                      {"error": ctx.registry_error})]
    reg, problems = ctx.registry, []
    refs = reg.get("references")
    if not isinstance(refs, list) or not refs:
        problems.append("'references' must be a non-empty list")
        refs = []
    ids = [r.get("id") for r in refs if isinstance(r, dict)]
    if len(ids) != len(set(ids)):
        problems.append("duplicate reference ids")
    known_rules = set((reg.get("closure_rules") or {}).keys()) or {"none", "json_manifest", "csv_inventory"}
    for r in refs:
        if not isinstance(r, dict):
            problems.append("reference entry is not an object")
            continue
        rid = r.get("id", "?")
        if not isinstance(r.get("path"), str) or not r["path"] or r["path"].startswith("/"):
            problems.append(f"{rid}: invalid path")
        if not SHA256_RE.match(str(r.get("sha256", ""))):
            problems.append(f"{rid}: sha256 is not 64 lowercase hex")
        if not COMMIT_RE.match(str(r.get("commit", ""))):
            problems.append(f"{rid}: commit is not a full 40-hex SHA")
        if not isinstance(r.get("verify_at"), list) or not r["verify_at"]:
            problems.append(f"{rid}: verify_at must be a non-empty list")
        rule = (r.get("closure") or {}).get("rule")
        if rule not in known_rules:
            problems.append(f"{rid}: unknown closure rule {rule!r}")
        for cc in r.get("cross_checks", []) or []:
            if cc.get("equals_reference") not in ids:
                problems.append(f"{rid}: cross-check target {cc.get('equals_reference')!r} is not a registered id")
    for a in reg.get("anchors", []) or []:
        if a.get("kind") not in {"branch", "tag"} or not COMMIT_RE.match(str(a.get("commit", ""))):
            problems.append(f"anchor {a.get('name')!r}: kind must be branch/tag and commit a full SHA")
    retired = reg.get("retired_references") or {}
    if retired:
        if not COMMIT_RE.match(str(retired.get("commit", ""))):
            problems.append("retired_references.commit is not a full SHA")
        for f in retired.get("files", []) or []:
            if not SHA256_RE.match(str(f.get("sha256", ""))):
                problems.append(f"retired {f.get('path')}: invalid sha256")
    if problems:
        return [Check("registry:schema", "registry", FAIL, True, f"registry {rel} is malformed",
                      {"problems": _capped(problems)})]
    return [Check("registry:schema", "registry", PASS, True,
                  f"registry {rel}: {len(refs)} references, {len(reg.get('anchors', []) or [])} anchors, "
                  f"{len(retired.get('files', []) or [])} retired files",
                  {"registry_sha256": sha256_file(ctx.registry_path)})]


# --------------------------------------------------------------------------- 1. frozen references
def _ref_label(ref: str) -> str:
    return ref[:7] if COMMIT_RE.match(ref) else ref


def _verify_closure_item(ctx: Context, commit: str, path: str, expected: str, size, via: str, acc: dict) -> bool:
    """Verify one closure record at ``commit``. Returns True if the bytes are verified (for recursion)."""
    reg = ctx.registry or {}
    retired_prefixes = tuple(reg.get("retired_prefixes", []))
    external_prefixes = tuple(reg.get("externalized_prefixes", []))
    expected = str(expected).lower()
    if (path, expected) in acc["seen"]:           # already verified through another manifest
        return acc["seen"][(path, expected)]
    acc["seen"][(path, expected)] = False
    if retired_prefixes and path.startswith(retired_prefixes):
        seen = ctx.retired_seen.setdefault(path, {"sha256": set(), "via": set()})
        seen["sha256"].add(expected)
        seen["via"].add(via)
        acc["retired_deferred"] += 1
        return False
    is_external = bool(external_prefixes) and path.startswith(external_prefixes)
    data = ctx.git.read(commit, path)
    if data is None:
        if not is_external:
            acc["errors"].append({"kind": "missing_at_ref", "path": path, "via": via})
            return False
        if ctx.register_by_sha is None:
            acc["externalized_unchecked"] += 1
            return False
        rid = ctx.register_by_sha.get(expected)
        if rid is None:
            acc["errors"].append({"kind": "externalized_not_in_private_register", "path": path, "expected": expected})
            return False
        acc["externalized_matched"][path] = rid
        return False
    actual, method, actual_size = content_identity(data)
    acc["by_method"][method] = acc["by_method"].get(method, 0) + 1
    acc["files"] += 1
    ok = actual == expected
    if ok and size not in (None, "") and actual_size >= 0:
        try:
            ok = int(size) == actual_size
        except (TypeError, ValueError):
            ok = False
    if not ok:
        acc["errors"].append({"kind": "hash_or_size", "path": path, "via": via, "method": method,
                              "expected_sha256": expected, "actual_sha256": actual,
                              "expected_size": size, "actual_size": actual_size})
        return False
    if is_external and ctx.register_by_sha is not None:
        rid = ctx.register_by_sha.get(expected)
        if rid is None:
            acc["errors"].append({"kind": "externalized_not_in_private_register", "path": path, "expected": expected})
        else:
            acc["externalized_matched"][path] = rid
    acc["seen"][(path, expected)] = True
    return True


def _walk_json_manifest(ctx: Context, commit: str, path: str, closure: dict, acc: dict, visited: set) -> None:
    if path in visited:
        return
    visited.add(path)
    data = ctx.git.read(commit, path)
    if data is None:
        acc["errors"].append({"kind": "manifest_missing_at_ref", "path": path})
        return
    try:
        payload = json.loads(data.decode("utf-8-sig"))
    except (UnicodeDecodeError, ValueError) as exc:
        acc["errors"].append({"kind": "manifest_not_json", "path": path, "error": str(exc)})
        return
    acc["manifests"] += 1
    nested = set((ctx.registry or {}).get("nested_manifest_names", []))
    local = set(closure.get("relative_to_manifest_dir", ["outputs"]))
    for section in closure.get("sections", ["inputs", "sources", "outputs"]):
        records = payload.get(section, []) if isinstance(payload, dict) else []
        for record in records if isinstance(records, list) else []:
            if not isinstance(record, dict) or not {"path", "sha256"} <= record.keys():
                continue
            child = str(record["path"]).replace("\\", "/")
            if section in local:
                child = posixpath.join(posixpath.dirname(path), child)
            child = posixpath.normpath(child)
            if child.startswith("../") or child.startswith("/"):
                acc["errors"].append({"kind": "unsafe_path", "path": child, "via": path})
                continue
            if _verify_closure_item(ctx, commit, child, record["sha256"], record.get("size_bytes"), path, acc) \
                    and posixpath.basename(child) in nested:
                _walk_json_manifest(ctx, commit, child, closure, acc, visited)


def _walk_csv_inventory(ctx: Context, commit: str, path: str, data: bytes, closure: dict, acc: dict) -> None:
    try:
        rows = list(csv.DictReader(io.StringIO(data.decode("utf-8-sig"))))
    except (UnicodeDecodeError, csv.Error) as exc:
        acc["errors"].append({"kind": "inventory_not_csv", "path": path, "error": str(exc)})
        return
    acc["manifests"] += 1
    pcol, scol = closure.get("path_column", "path"), closure.get("sha256_column", "sha256")
    zcol = closure.get("size_column")
    for row in rows:
        child, expected = (row.get(pcol) or "").strip(), (row.get(scol) or "").strip()
        if not child or not expected:
            acc["errors"].append({"kind": "inventory_row_incomplete", "path": path, "row": row})
            continue
        _verify_closure_item(ctx, commit, posixpath.normpath(child), expected, row.get(zcol) if zcol else None,
                             path, acc)


def _verify_reference_at(ctx: Context, entry: dict, ref: str, commit: str, sha_by_id: dict) -> Check:
    acc = {"files": 0, "manifests": 0, "by_method": {}, "errors": [], "externalized_matched": {},
           "externalized_unchecked": 0, "retired_deferred": 0, "seen": {}}
    check_id = f"frozen:{entry['id']}@{_ref_label(ref)}"
    data = ctx.git.read(commit, entry["path"])
    base = {"reference": entry["id"], "path": entry["path"], "ref": ref, "commit": commit}
    if data is None:
        return Check(check_id, "frozen_references", FAIL, True, f"pinned object absent at {ref}", base)
    actual, method, _ = content_identity(data)
    base.update({"expected_sha256": entry["sha256"], "actual_sha256": actual, "method": method})
    if actual != entry["sha256"]:
        return Check(check_id, "frozen_references", FAIL, True, f"pinned object hash mismatch at {ref}", base)
    closure = entry.get("closure") or {"rule": "none"}
    rule = closure.get("rule", "none")
    if rule == "json_manifest":
        _walk_json_manifest(ctx, commit, entry["path"], closure, acc, set())
    elif rule == "csv_inventory":
        _walk_csv_inventory(ctx, commit, entry["path"], data, closure, acc)
    cross = []
    if entry.get("cross_checks"):
        try:
            payload = json.loads(data.decode("utf-8-sig"))
        except (UnicodeDecodeError, ValueError):
            payload = {}
        for cc in entry["cross_checks"]:
            value = payload.get(cc["json_field"]) if isinstance(payload, dict) else None
            want = sha_by_id.get(cc["equals_reference"])
            ok = value == want
            cross.append({"field": cc["json_field"], "equals": cc["equals_reference"], "ok": ok})
            if not ok:
                acc["errors"].append({"kind": "cross_check", "field": cc["json_field"], "value": value,
                                      "expected": want, "reference": cc["equals_reference"]})
    base.update({"closure_rule": rule, "closure_files_hash_checked": acc["files"], "by_method": acc["by_method"],
                 "manifests_walked": acc["manifests"], "cross_checks": cross,
                 "externalized_matched_private_register": dict(sorted(acc["externalized_matched"].items())),
                 "externalized_unchecked_no_private_register": acc["externalized_unchecked"],
                 "retired_references_deferred": acc["retired_deferred"], "errors": _capped(acc["errors"])})
    if acc["errors"]:
        return Check(check_id, "frozen_references", FAIL, True,
                     f"{len(acc['errors'])} closure error(s) at {ref}", base)
    note = f"pinned + {acc['files']} closure files verified from git objects at {_ref_label(ref)}"
    if acc["externalized_unchecked"]:
        note += f"; {acc['externalized_unchecked']} externalized sources unchecked (set VKM_RESOURCES_ROOT)"
    return Check(check_id, "frozen_references", PASS, True, note, base)


def check_frozen_references(ctx: Context) -> list[Check]:
    if ctx.registry is None:
        return [Check("frozen:registry", "frozen_references", SKIPPED, False, "registry unavailable")]
    if not ctx.git.available:
        return [Check("frozen:git", "frozen_references", SKIPPED_REF, False,
                      "not a git work tree: frozen references cannot be read from git objects", {"how_to_fix": REF_HELP})]
    out: list[Check] = []
    head = ctx.git.resolve("HEAD")
    for anchor in ctx.registry.get("anchors", []) or []:
        name, kind, want = anchor["name"], anchor["kind"], anchor["commit"]
        got = ctx.git.resolve_named(name, kind)
        cid = f"frozen:anchor:{kind}:{name}"
        details = {"name": name, "kind": kind, "expected_commit": want, "actual_commit": got}
        if got is None:
            fix = (f"git tag -a {name} {want} -m '<see docs/legacy/LEGACY_INDEX_RU.md>' "
                   f"(or 'git fetch origin --tags')") if kind == "tag" else REF_HELP
            out.append(Check(cid, "frozen_references", SKIPPED_REF, False, f"{kind} '{name}' not available locally",
                             {**details, "how_to_fix": fix}))
        elif got != want:
            # a moved tag breaks an immutable pin; a moved legacy branch is a governance warning
            status, blocking = (FAIL, True) if kind == "tag" else (WARN, False)
            out.append(Check(cid, "frozen_references", status, blocking,
                             f"{kind} '{name}' points to {got[:7]}, registry expects {want[:7]}", details))
        else:
            details["ancestor_of_head"] = bool(head) and ctx.git.is_ancestor(want, head)
            out.append(Check(cid, "frozen_references", PASS, kind == "tag", f"{kind} '{name}' -> {want[:7]}", details))
    sha_by_id = {r["id"]: r["sha256"] for r in ctx.registry.get("references", [])}
    for entry in ctx.registry.get("references", []):
        verified_any = False
        for ref in entry["verify_at"]:
            commit = ctx.git.resolve_ref(ref)
            if commit is None:
                out.append(Check(f"frozen:{entry['id']}@{_ref_label(ref)}", "frozen_references", SKIPPED_REF, False,
                                 f"ref '{ref}' not available locally", {"reference": entry["id"], "ref": ref,
                                                                         "how_to_fix": REF_HELP}))
                continue
            check = _verify_reference_at(ctx, entry, ref, commit, sha_by_id)
            verified_any |= check.status == PASS
            out.append(check)
        if not verified_any and not any(c.status == FAIL for c in out if c.details.get("reference") == entry["id"]):
            out.append(Check(f"frozen:{entry['id']}:unverified", "frozen_references", WARN, False,
                             "no listed ref available: identity not verified in this clone",
                             {"reference": entry["id"], "how_to_fix": REF_HELP}))
    return out


# --------------------------------------------------------------------------- 2. retired references
def check_retired_references(ctx: Context) -> list[Check]:
    spec = (ctx.registry or {}).get("retired_references") or {}
    if not spec:
        return [Check("retired:registry", "retired_references", SKIPPED, False, "no retired_references in registry")]
    commit_want, files = spec["commit"], spec.get("files", [])
    commit = ctx.git.resolve_ref(commit_want) if ctx.git.available else None
    out: list[Check] = []
    registered = {f["path"]: f["sha256"] for f in files}
    # consistency: every retired path met while walking frozen closures is registered with the same sha256
    if ctx.retired_seen:
        problems = []
        for path, seen in sorted(ctx.retired_seen.items()):
            if path not in registered:
                problems.append({"path": path, "problem": "not in retired_references", "via": sorted(seen["via"])})
            elif seen["sha256"] != {registered[path]}:
                problems.append({"path": path, "problem": "sha256 differs from registry",
                                 "closure_sha256": sorted(seen["sha256"]), "registry_sha256": registered[path]})
        out.append(Check("retired:registry_consistency", "retired_references", FAIL if problems else PASS, True,
                         f"{len(ctx.retired_seen)} retired paths met in frozen closures; {len(problems)} not consistent "
                         f"with the registry", {"problems": _capped(problems)}))
    if commit is None:
        out.append(Check(f"retired:{spec.get('id', 'retired')}@{commit_want[:7]}", "retired_references", SKIPPED_REF,
                         False, f"archive commit {commit_want[:7]} not available locally",
                         {"tag": spec.get("tag"), "how_to_fix": REF_HELP}))
        return out
    errors, methods = [], {}
    for f in files:
        data = ctx.git.read(commit, f["path"])
        if data is None:
            errors.append({"kind": "missing_at_ref", "path": f["path"]})
            continue
        actual, method, _ = content_identity(data)
        methods[method] = methods.get(method, 0) + 1
        if actual != f["sha256"]:
            errors.append({"kind": "hash", "path": f["path"], "expected_sha256": f["sha256"], "actual_sha256": actual})
    details = {"commit": commit, "tag": spec.get("tag"), "files": len(files), "by_method": methods,
               "errors": _capped(errors), "status_note": "LEGACY_RETIRED: historical provenance only"}
    out.append(Check(f"retired:{spec.get('id', 'retired')}@{commit_want[:7]}", "retired_references",
                     FAIL if errors else PASS, True,
                     f"{len(files) - len(errors)}/{len(files)} retired v3.2 files verified at {commit_want[:7]}", details))
    return out


# --------------------------------------------------------------------------- 3. private sources
def _hash_registered_file(res: Path, rel: str) -> tuple[str, str | None, dict]:
    path = res / rel
    if not path.is_file():
        return "missing", None, {}
    with path.open("rb") as stream:
        head = stream.read(1025)
    pointer = parse_lfs_pointer(head) if len(head) <= 1024 else None
    if pointer:
        return "lfs_pointer", pointer[0], {"pointer_size": pointer[1]}
    return "file", sha256_file(path), {"size": path.stat().st_size}


def _public_catalogue_rows(ctx: Context) -> tuple[list[dict], list[str]]:
    rows, files = [], []
    for name in ctx.repo_files():
        if not name.lower().endswith(".csv") or not name.startswith(tuple(d + "/" for d in CATALOGUE_DIRS)):
            continue
        try:
            with (ctx.root / name).open(encoding="utf-8-sig", newline="") as stream:
                reader = csv.DictReader(stream)
                header = [h.strip().lower() for h in (reader.fieldnames or [])]
                id_cols = [c for c, h in zip(reader.fieldnames or [], header) if h in CATALOGUE_ID_COLUMNS]
                sha_cols = [c for c, h in zip(reader.fieldnames or [], header) if h in CATALOGUE_SHA_COLUMNS]
                if not id_cols or not sha_cols:
                    continue
                found = False
                for lineno, row in enumerate(reader, start=2):
                    ids = [(row.get(c) or "").strip() for c in id_cols]
                    ids = [i for i in ids if VKM_SRC_RE.match(i)]
                    shas = [(row.get(c) or "").strip().lower() for c in sha_cols]
                    shas = [s for s in shas if SHA256_RE.match(s)]
                    if ids and shas:
                        found = True
                        rows.append({"file": name, "line": lineno, "id": ids[0], "sha256": shas[0]})
                if found:
                    files.append(name)
        except (OSError, UnicodeDecodeError, csv.Error):
            continue
    return rows, files


def check_private_sources(ctx: Context) -> list[Check]:
    if ctx.resources_root is None:
        return [Check("private:configured", "private_sources", SKIPPED, False,
                      "VKM_RESOURCES_ROOT not set: PRIVATE SOURCE_REGISTER checks skipped",
                      {"how_to_enable": "VKM_RESOURCES_ROOT=<private checkout> python scripts/verify_canonical_repository.py"})]
    if ctx.register is None:
        return [Check("private:register", "private_sources", FAIL, True, "PRIVATE SOURCE_REGISTER unreadable",
                      {"error": ctx.register_error, "source": ctx.resources_source})]
    out = [Check("private:register", "private_sources", PASS, True,
                 f"{REGISTER_REL} readable: {len(ctx.register)} resource ids",
                 {"register_sha256": sha256_file(ctx.resources_root / REGISTER_REL), "source": ctx.resources_source,
                  "duplicate_snapshot_copies_read": False})]
    if ctx.register_duplicates:
        out[0] = Check("private:register", "private_sources", FAIL, True, "duplicate resource_id in PRIVATE register",
                       {**out[0].details, "duplicates": sorted(set(ctx.register_duplicates))})
    bad_sha = [rid for rid, row in ctx.register.items() if not SHA256_RE.match((row.get("sha256") or "").strip().lower())]
    verified, missing, expected_absent, pointers, mismatches, snapshot_rows = [], [], [], [], [], []
    for rid, row in sorted(ctx.register.items()):
        rel = (row.get("canonical_path") or "").strip().replace("\\", "/")
        want = (row.get("sha256") or "").strip().lower()
        if not rel or rel.startswith("/") or ".." in rel.split("/"):
            mismatches.append({"id": rid, "problem": "unsafe or empty canonical_path", "canonical_path": rel})
            continue
        if rel.startswith(DUPLICATE_SNAPSHOT_PREFIX):
            snapshot_rows.append({"id": rid, "canonical_path": rel})
            continue
        kind, actual, extra = _hash_registered_file(ctx.resources_root, rel)
        if kind == "missing":
            marker = f"{row.get('migration_status') or ''} {row.get('evidence_scope') or ''}"
            (expected_absent if REGISTER_ABSENCE_RE.search(marker) else missing).append(
                {"id": rid, "canonical_path": rel, "migration_status": row.get("migration_status")})
        elif kind == "lfs_pointer":
            entry = {"id": rid, "canonical_path": rel, "status": "SKIPPED_LFS_POINTER",
                     "pointer_oid_matches_register": actual == want}
            pointers.append(entry)
            if actual != want:
                mismatches.append({"id": rid, "problem": "LFS pointer oid differs from register sha256",
                                   "canonical_path": rel, "oid": actual, "register_sha256": want})
        elif actual != want:
            mismatches.append({"id": rid, "problem": "sha256 mismatch", "canonical_path": rel, "actual": actual,
                               "register_sha256": want})
        else:
            verified.append(rid)
    status = FAIL if (mismatches or bad_sha) else (WARN if (missing or pointers or snapshot_rows) else PASS)
    out.append(Check("private:registered_files", "private_sources", status, True,
                     f"{len(verified)} registered files sha256-verified on disk; {len(pointers)} LFS pointers skipped "
                     f"(not materialized); {len(missing)} unexpectedly not on disk; {len(expected_absent)} absent by "
                     f"register status; {len(mismatches)} mismatches",
                     {"verified": len(verified), "lfs_pointers_skipped": _capped(pointers), "missing_on_disk": _capped(missing),
                      "absent_by_register_status": expected_absent,
                      "mismatches": _capped(mismatches), "invalid_register_sha256": bad_sha,
                      "rows_pointing_to_duplicate_snapshots_not_read": snapshot_rows}))
    rows, files = _public_catalogue_rows(ctx)
    if not rows:
        out.append(Check("private:public_catalogues", "private_sources", SKIPPED, False,
                         "no PUBLIC source catalogue with VKM-SRC id + sha256 columns found yet",
                         {"searched": [d + "/**/*.csv" for d in CATALOGUE_DIRS]}))
        return out
    problems = []
    for r in rows:
        reg = ctx.register.get(r["id"])
        if reg is None:
            problems.append({**r, "problem": "VKM-SRC id not in PRIVATE register"})
        elif (reg.get("sha256") or "").strip().lower() != r["sha256"]:
            problems.append({**r, "problem": "sha256 differs from PRIVATE register",
                             "register_sha256": (reg.get("sha256") or "").strip().lower()})
    out.append(Check("private:public_catalogues", "private_sources", FAIL if problems else PASS, True,
                     f"{len(rows)} PUBLIC catalogue rows in {len(files)} files checked against the PRIVATE register; "
                     f"{len(problems)} problems", {"files": files, "problems": _capped(problems)}))
    return out


# --------------------------------------------------------------------------- 4. markdown links
def _blank_keep_lines(match: re.Match) -> str:
    return re.sub(r"[^\n]", " ", match.group(0))


def markdown_targets(text: str) -> list[tuple[int, str]]:
    """(line, target) of inline links/images and reference definitions outside code."""
    clean = FENCE_RE.sub(_blank_keep_lines, text)
    clean = HTML_COMMENT_RE.sub(_blank_keep_lines, clean)
    clean = INLINE_CODE_RE.sub(_blank_keep_lines, clean)
    found = []
    for regex in (INLINE_LINK_RE, REF_DEF_RE):
        for m in regex.finditer(clean):
            found.append((clean.count("\n", 0, m.start()) + 1, m.group(1).strip("<>").strip()))
    return sorted(found)


def check_markdown_links(ctx: Context) -> list[Check]:
    files = ctx.repo_files()
    existing = set(files)
    dirs = {posixpath.dirname(f) for f in files}
    dirs |= {d for f in files for d in _parents(f)}
    root_resolved = ctx.root.resolve()
    reg = ctx.registry or {}
    offloaded = tuple(reg.get("externalized_prefixes", [])) + tuple(reg.get("retired_prefixes", []))
    broken, offloaded_links, checked_links, md_files = [], [], 0, 0
    for name in files:
        if not name.lower().endswith(".md"):
            continue
        md_files += 1
        try:
            text = (ctx.root / name).read_text(encoding="utf-8-sig", errors="replace")
        except OSError as exc:
            broken.append({"file": name, "line": 0, "target": "", "reason": f"unreadable: {exc}"})
            continue
        for line, raw in markdown_targets(text):
            target = unquote(raw)
            if not target or target.startswith("#") or SCHEME_RE.match(target) or target.startswith("//"):
                continue
            local = re.split(r"[#?]", target, maxsplit=1)[0]
            if not local:
                continue
            checked_links += 1
            if re.match(r"^[A-Za-z]:[\\/]", local) or local.startswith("\\"):
                broken.append({"file": name, "line": line, "target": raw, "reason": "absolute host path"})
                continue
            joined = local.lstrip("/") if local.startswith("/") else posixpath.join(posixpath.dirname(name), local)
            rel = posixpath.normpath(joined.replace("\\", "/"))
            if rel == ".":
                rel = ""
            if rel.startswith("../") or rel == "..":
                broken.append({"file": name, "line": line, "target": raw, "reason": "points outside the repository"})
                continue
            if rel in existing or rel in dirs or rel == "":
                continue
            if offloaded and rel.startswith(offloaded):
                offloaded_links.append({"file": name, "line": line, "target": raw})
                continue
            reason = "target does not exist"
            if (root_resolved / rel).exists():
                reason = "target exists only as an ignored/untracked local file (e.g. work/)"
            broken.append({"file": name, "line": line, "target": raw, "reason": reason})
    out = [Check("markdown:links", "markdown_links", FAIL if broken else PASS, True,
                 f"{md_files} Markdown files, {checked_links} local links, {len(broken)} broken",
                 {"broken": _capped(broken, 200),
                  "scope": "inline links/images and reference definitions outside code; URLs and anchors not fetched"})]
    if offloaded_links:
        out.append(Check("markdown:offloaded_links", "markdown_links", WARN, False,
                         f"{len(offloaded_links)} links into externalized/retired trees (bytes live in PRIVATE or in "
                         f"git history; cite by VKM-SRC id / commit instead)",
                         {"prefixes": list(offloaded), "links": _capped(offloaded_links, 200)}))
    return out


def _parents(path: str) -> list[str]:
    parts = path.split("/")[:-1]
    return ["/".join(parts[:i]) for i in range(1, len(parts) + 1)]


# --------------------------------------------------------------------------- 5. leakage
def _import_vkm_world(ctx: Context):
    src = str(ctx.root / "src")
    if (ctx.root / "src" / "vkm_world").is_dir() and src not in sys.path:
        sys.path.insert(0, src)
    import vkm_world.governance.leakage as leakage  # noqa: PLC0415
    return leakage


def check_leakage(ctx: Context) -> list[Check]:
    leakage = _import_vkm_world(ctx)
    problems = leakage.scan(ctx.root)
    return [Check("leakage:scan", "leakage", FAIL if problems else PASS, True,
                  f"vkm_world.governance.leakage.scan: {len(problems)} problems", {"problems": _capped(problems)})]


# --------------------------------------------------------------------------- 6. host paths
def _host_path_exemptions(ctx: Context) -> dict[str, str]:
    exemptions = dict(HOST_PATH_EXEMPTIONS)
    try:
        for prefix in _import_vkm_world(ctx).PATH_CHECK_EXEMPT_PREFIXES:
            exemptions.setdefault(prefix, "PATH_CHECK_EXEMPT_PREFIXES of vkm_world.governance.leakage")
    except Exception:  # noqa: BLE001 - the leakage guard's exemptions are optional here
        pass
    return exemptions


def scan_host_paths(text: str) -> list[tuple[int, str, str]]:
    """(line, kind, excerpt) of absolute/Windows host paths in ``text``; pragma lines are skipped."""
    hits, lines = [], None
    for kind, regex in HOST_PATH_PATTERNS:
        for m in regex.finditer(text):
            lines = lines if lines is not None else text.split("\n")
            lineno = text.count("\n", 0, m.start()) + 1
            if HOST_PATH_PRAGMA in lines[lineno - 1]:
                continue
            hits.append((lineno, kind, lines[lineno - 1].strip()[:160]))
    return sorted(set(hits))


def check_host_paths(ctx: Context) -> list[Check]:
    exemptions = _host_path_exemptions(ctx)
    prefixes = tuple(exemptions)
    hits, exempt_hits, scanned, skipped_large = [], {}, 0, []
    for name in ctx.repo_files():
        if Path(name).suffix.lower() not in HOST_PATH_SUFFIXES:
            continue
        path = ctx.root / name
        if path.stat().st_size > MAX_SCAN_BYTES:
            skipped_large.append(name)
            continue
        data = path.read_bytes()
        if parse_lfs_pointer(data):
            continue
        found = scan_host_paths(data.decode("utf-8", "replace"))
        if name.startswith(prefixes):
            if found:
                prefix = next(p for p in prefixes if name.startswith(p))
                exempt_hits[prefix] = exempt_hits.get(prefix, 0) + len(found)
            continue
        scanned += 1
        hits.extend({"file": name, "line": ln, "kind": kind, "excerpt": excerpt} for ln, kind, excerpt in found)
    return [Check("host_paths:code_config", "host_paths", FAIL if hits else PASS, True,
                  f"{scanned} code/config files scanned; {len(hits)} absolute/Windows host paths outside exemptions",
                  {"suffixes": sorted(HOST_PATH_SUFFIXES), "exemptions": exemptions, "pragma": HOST_PATH_PRAGMA,
                   "hits_in_exemptions": dict(sorted(exempt_hits.items())),
                   "skipped_larger_than_bytes": {str(MAX_SCAN_BYTES): skipped_large}, "hits": _capped(hits, 200)})]


# --------------------------------------------------------------------------- 7. worldspec schema
def check_worldspec_schema(ctx: Context) -> list[Check]:
    committed = ctx.root / "schemas" / "worldspec_vnext.schema.json"
    if not committed.is_file():
        return [Check("schema:worldspec_vnext", "worldspec_schema", FAIL, True,
                      "schemas/worldspec_vnext.schema.json is missing (run scripts/export_worldspec_schema.py)")]
    _import_vkm_world(ctx)
    from vkm_world.worldspec.io import json_schema  # noqa: PLC0415
    generated, text = json_schema(), committed.read_text(encoding="utf-8")
    if generated == text:
        return [Check("schema:worldspec_vnext", "worldspec_schema", PASS, True,
                      "committed WorldSpec schema equals the schema generated from code",
                      {"sha256": sha256_bytes(text.encode("utf-8"))})]
    a, b = text.splitlines(), generated.splitlines()
    first = next((i + 1 for i, (x, y) in enumerate(zip(a, b)) if x != y), min(len(a), len(b)) + 1)
    return [Check("schema:worldspec_vnext", "worldspec_schema", FAIL, True,
                  "committed schema differs from code (run scripts/export_worldspec_schema.py)",
                  {"first_differing_line": first, "committed_sha256": sha256_bytes(text.encode("utf-8")),
                   "generated_sha256": sha256_bytes(generated.encode("utf-8"))})]


CHECKS = {
    "registry": check_registry,
    "frozen_references": check_frozen_references,
    "retired_references": check_retired_references,
    "private_sources": check_private_sources,
    "markdown_links": check_markdown_links,
    "leakage": check_leakage,
    "host_paths": check_host_paths,
    "worldspec_schema": check_worldspec_schema,
}


# --------------------------------------------------------------------------- driver
def _display_path(path: Path, root: Path) -> str:
    try:
        return path.resolve().relative_to(root.resolve()).as_posix()
    except ValueError:
        return f"<outside root>/{path.name}"


def verify(root: str | Path = DEFAULT_ROOT, registry_path: str | Path | None = None,
           resources_root: str | Path | None = None, groups: list[str] | tuple[str, ...] | None = None,
           use_env: bool = True) -> dict:
    """Run the selected check groups and return the JSON-serialisable report (never raises for check errors)."""
    started = time.monotonic()
    root = Path(root)
    source = None
    if resources_root is not None:
        source = "argument"
    elif use_env and os.environ.get("VKM_RESOURCES_ROOT"):
        resources_root, source = os.environ["VKM_RESOURCES_ROOT"], "env VKM_RESOURCES_ROOT"
    ctx = Context(root=root, registry_path=Path(registry_path) if registry_path else DEFAULT_REGISTRY,
                  resources_root=Path(resources_root) if resources_root else None, resources_source=source,
                  git=Git(root))
    selected = [g for g in GROUPS if groups is None or g in groups]
    checks: list[Check] = []
    try:
        if not root.is_dir():
            checks.append(Check("root", "registry", FAIL, True, "repository root does not exist"))
            selected = []
        load_registry(ctx)
        load_private_register(ctx)
        for group in selected:
            t0 = time.monotonic()
            try:
                produced = CHECKS[group](ctx)
            except Exception as exc:  # noqa: BLE001 - a crashing check becomes a structured FAIL
                produced = [Check(f"{group}:error", group, FAIL, True, f"check raised {type(exc).__name__}: {exc}",
                                  {"traceback_tail": traceback.format_exc().splitlines()[-6:]})]
            for c in produced:
                c.details.setdefault("elapsed_s", round(time.monotonic() - t0, 3))
            checks.extend(produced)
    finally:
        ctx.git.close()
    by_status: dict[str, int] = {}
    for c in checks:
        by_status[c.status] = by_status.get(c.status, 0) + 1
    blocking = [c.id for c in checks if c.status == FAIL and c.blocking]
    status = FAIL if blocking else (PASS if all(c.status == PASS for c in checks) else PASS_WITH_NONBLOCKING)
    head = ctx.git.resolve("HEAD") if ctx.git.available else None
    return {
        "status": status,
        "exit_code": 1 if blocking else 0,
        "blocking_failures": blocking,
        "summary": {"checks": len(checks), "by_status": dict(sorted(by_status.items())), "groups_run": selected,
                    "elapsed_s": round(time.monotonic() - started, 3)},
        "verifier": {"script": "scripts/verify_canonical_repository.py", "version": VERSION,
                     "registry": _display_path(ctx.registry_path, root)},
        "repository": {"root_name": root.resolve().name, "git_work_tree": ctx.git.available, "head": head,
                       "branch": ctx.git.run("rev-parse", "--abbrev-ref", "HEAD") if ctx.git.available else None,
                       "shallow": (ctx.git.run("rev-parse", "--is-shallow-repository") == "true")
                       if ctx.git.available else None},
        "private_resources": {"configured": ctx.resources_root is not None, "source": source,
                              "register_readable": ctx.register is not None},
        "guarantees": {"models_executed": 0, "evaluator_truth_parsed": False, "network_access": False,
                       "git_mutations": False, "duplicate_snapshot_copies_read": False},
        "checks": [asdict(c) for c in checks],
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--root", default=str(DEFAULT_ROOT), help="repository root (default: parent of scripts/)")
    parser.add_argument("--registry", default=None, help="FROZEN_REFERENCE registry (default: scripts/frozen_references.json)")
    parser.add_argument("--resources-root", default=None, help="PRIVATE checkout (default: $VKM_RESOURCES_ROOT)")
    parser.add_argument("--output", nargs="?", const=DEFAULT_OUTPUT, default=None,
                        help=f"also write the report to this path (relative to --root; default {DEFAULT_OUTPUT})")
    parser.add_argument("--only", nargs="+", choices=GROUPS, default=None, help="run only these check groups")
    args = parser.parse_args(argv)
    root = Path(args.root)
    report = verify(root, registry_path=args.registry, resources_root=args.resources_root, groups=args.only)
    text = json.dumps(report, ensure_ascii=False, indent=1) + "\n"
    if args.output:
        destination = Path(args.output)
        destination = destination if destination.is_absolute() else root / destination
        try:
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_text(text, encoding="utf-8", newline="\n")
        except OSError as exc:
            print(f"cannot write report: {exc}", file=sys.stderr)
    sys.stdout.write(text)
    return int(report["exit_code"])


if __name__ == "__main__":
    raise SystemExit(main())
