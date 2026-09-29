#!/usr/bin/env python3
"""VKM backup manifests (agent OPS, 29.09.2026): sha256 of every file of the CORE backup set, comparison of two
manifests, verification of a restored tree and retention of hard-link snapshots. Standard library only; runs with the
host ``python3`` of CORE (source manifest, ``backup_prepare.sh``) and of EDGE (snapshot manifest, ``vkm_backup.sh``).

Manifest file (``*.manifest.jsonl.gz``): line 1 is a JSON header (schema ``vkm.backup_manifest/1``: the backup set, the
resolved late packs, counts, bytes, digest), then one JSON array per file, sorted by path::

    ["canonical/pages/source_id=VKM-SRC-001/run=RUN-…/part-00000.parquet", size, mtime_ns, inode, "<sha256>"]

The digest is the sha256 of ``path\\tsize\\tsha256\\n`` over all entries: equal digests mean equal file sets. Hashes of
unchanged files are reused from a previous manifest (same path, size, mtime and inode: files of the data root are
immutable, and the hard links of a snapshot keep the inode of the file they share); ``--rehash-all`` re-reads every file
and reports reused hashes that no longer match (silent corruption, bit rot).

Subcommands (``--help`` of each): ``build``, ``compare``, ``verify``, ``files``, ``estimate``, ``plan``, ``prune``,
``header``. Exit codes: 0 PASS (or done), 1 FAIL, 2 usage error; ``compare`` exits 0 on WARN as well.
"""
from __future__ import annotations

import argparse
import concurrent.futures
import datetime as dt
import fnmatch
import gzip
import hashlib
import json
import os
import shutil
import stat
import sys
import time
from pathlib import Path
from typing import Any, Callable, Iterable, Iterator

SCHEMA = "vkm.backup_manifest/1"
COMPARE_SCHEMA = "vkm.backup_compare/1"
VERIFY_SCHEMA = "vkm.backup_verify/1"
TOOL = "vkm_manifest.py/1"
CHUNK = 1 << 20
EXAMPLES = 20

# The backup set of the CORE data root ($VKM_DATA_ROOT_HOST). Everything else there is rebuildable or transient:
# neo4j/ and opensearch/ (projections, rebuilt by `core reconcile`, `nav graph-load`, `search build-vectors`), tmp/,
# cache/, locks/. Patterns: without "/" they match the file name, with "/" the whole relative path (fnmatch: "*"
# crosses "/").
CORE_SET: dict[str, Any] = {
    "name": "vkm-core-data/1",
    "include": [".vkm_root.json", "canonical", "artifacts", "duckdb", "derived", "receipts", "logs"],
    "exclude": [".lock", "*.tmp", "*.tmp-*", "*/.rsync-partial/*", "receipts/backup/*", "derived/dossiers/.tmp-*",
                "derived/dossiers/.old-*"],
    # files that may change between the source manifest and the copy (pointers, the DuckDB file replaced by a
    # reconcile, receipts and logs being written): a difference there is a warning, never corruption
    "volatile": ["CURRENT", "STATUS", "latest.json", "LATEST", "duckdb/*", "logs/*", "receipts/*",
                 "canonical/_snapshots/candidates/*", "derived/dossiers/*"],
    # late-interaction token packs (derived/embeddings/**/packs/<pack_id>/, ~6 GB each) are rebuildable from the
    # multivector Parquet parts (`embed pack`, CPU, minutes): "current" = only the pack named by packs/CURRENT,
    # "none" = no pack (and no packs/CURRENT), "all" = every pack kept on CORE
    "packs": "current",
}
PACK_MODES = ("current", "none", "all")


# ============================================================================================================= paths
def matches(rel: str, pattern: str) -> bool:
    """``pattern`` without "/" matches the file name, with "/" the whole POSIX path relative to the root."""
    if "/" in pattern:
        return fnmatch.fnmatchcase(rel, pattern)
    return fnmatch.fnmatchcase(rel.rsplit("/", 1)[-1], pattern)


def matches_any(rel: str, patterns: Iterable[str]) -> bool:
    return any(matches(rel, p) for p in patterns)


def _packs_split(rel: str) -> tuple[str, str] | None:
    """``derived/embeddings/<…>/packs/<rest>`` → (``<…>/packs`` directory, ``<rest>``); None outside packs."""
    if not rel.startswith("derived/embeddings/") or "/packs/" not in rel:
        return None
    head, _, rest = rel.rpartition("/packs/")
    return head + "/packs", rest


def read_current_pack(root: Path, packs_dir: str) -> str | None:
    """pack_id named by ``<packs_dir>/CURRENT`` (JSON with ``pack_id``, or a bare id), None if absent/unreadable."""
    try:
        text = (root / packs_dir / "CURRENT").read_text(encoding="utf-8").strip()
    except OSError:
        return None
    if not text:
        return None
    try:
        obj = json.loads(text)
    except ValueError:
        return text.split()[0]
    if isinstance(obj, dict) and isinstance(obj.get("pack_id"), str):
        return obj["pack_id"]
    return None


class Selector:
    """Decides which files of a data root belong to the backup set (``spec`` like :data:`CORE_SET`)."""

    def __init__(self, root: Path, spec: dict[str, Any]) -> None:
        self.root = root
        self.spec = spec
        self.mode = spec.get("packs", "current")
        if self.mode not in PACK_MODES:
            raise ValueError(f"packs must be one of {PACK_MODES}, got {self.mode!r}")
        self.current_packs: dict[str, str | None] = {}

    def pack_ok(self, rel: str) -> bool:
        split = _packs_split(rel)
        if split is None:
            return True
        packs_dir, rest = split
        if rest == ".lock" or self.mode == "none":
            return False
        if self.mode == "all" or rest == "CURRENT":
            return True
        if packs_dir not in self.current_packs:
            self.current_packs[packs_dir] = read_current_pack(self.root, packs_dir)
        current = self.current_packs[packs_dir]
        return current is not None and rest.split("/", 1)[0] == current

    def wanted(self, rel: str) -> bool:
        return not matches_any(rel, self.spec.get("exclude") or ()) and self.pack_ok(rel)

    def resolved(self) -> dict[str, Any]:
        return {"packs_mode": self.mode,
                "current_packs": {k: v for k, v in sorted(self.current_packs.items())}}


def iter_files(root: Path, spec: dict[str, Any], selector: Selector | None = None,
               stats: dict[str, int] | None = None) -> Iterator[tuple[str, os.stat_result]]:
    """Regular files of the backup set under ``root`` (sorted walk; symlinks are never followed and never included —
    they are counted in ``stats['symlinks']``)."""
    selector = selector or Selector(root, spec)
    stats = stats if stats is not None else {}
    stats.setdefault("symlinks", 0)
    stats.setdefault("excluded", 0)
    for top in sorted(spec.get("include") or ()):
        base = root / top
        if base.is_symlink():
            stats["symlinks"] += 1
            continue
        if base.is_file():
            if selector.wanted(top):
                yield top, base.stat()
            else:
                stats["excluded"] += 1
            continue
        if not base.is_dir():
            continue
        for dirpath, dirnames, filenames in os.walk(base, followlinks=False):
            dirnames.sort()
            rel_dir = Path(dirpath).relative_to(root).as_posix()
            for name in sorted(filenames):
                rel = f"{rel_dir}/{name}"
                st = os.lstat(os.path.join(dirpath, name))          # one syscall; a symlink is never followed
                if stat.S_ISLNK(st.st_mode):
                    stats["symlinks"] += 1
                    continue
                if not selector.wanted(rel):
                    stats["excluded"] += 1
                    continue
                if stat.S_ISREG(st.st_mode):
                    yield rel, st
            # symlinked directories are listed in dirnames but os.walk does not descend into them
            for d in list(dirnames):
                if Path(dirpath, d).is_symlink():
                    stats["symlinks"] += 1


def iter_tree(root: Path, skip_prefixes: Iterable[str] = ()) -> Iterator[tuple[str, os.stat_result]]:
    """Every regular file under ``root`` (a snapshot or a restored tree), except under ``skip_prefixes``."""
    skip = tuple(p.strip("/") + "/" for p in skip_prefixes if p.strip("/"))
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        dirnames.sort()
        rel_dir = Path(dirpath).relative_to(root).as_posix()
        for name in sorted(filenames):
            rel = name if rel_dir == "." else f"{rel_dir}/{name}"
            if skip and rel.startswith(skip):
                continue
            st = os.lstat(os.path.join(dirpath, name))
            if stat.S_ISREG(st.st_mode):                            # symlinks and special files are skipped
                yield rel, st


# ============================================================================================================ hashing
def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        while True:
            block = fh.read(CHUNK)
            if not block:
                break
            h.update(block)
    return h.hexdigest()


def digest_of(entries: dict[str, tuple[int, int, int, str]]) -> str:
    h = hashlib.sha256()
    for path in sorted(entries):
        size, _mtime, _ino, sha = entries[path]
        h.update(f"{path}\t{size}\t{sha}\n".encode("utf-8"))
    return h.hexdigest()


def now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


def build(root: Path, files: Iterable[tuple[str, os.stat_result]], *, cache: dict[str, tuple] | None = None,
          rehash_all: bool = False, jobs: int = 1, header_extra: dict[str, Any] | None = None,
          hasher: Callable[[Path], str] = sha256_file) -> tuple[dict[str, Any], dict[str, tuple[int, int, int, str]]]:
    """Hash ``files`` (relative path, stat) under ``root``; a cache entry is reused when size, mtime_ns and inode are
    equal (unless ``rehash_all``). Returns (header, entries)."""
    t0 = time.monotonic()
    cache = cache or {}
    entries: dict[str, tuple[int, int, int, str]] = {}
    todo: list[tuple[str, int, int, int]] = []
    reused = 0
    for rel, st in files:
        size, mtime, ino = int(st.st_size), int(st.st_mtime_ns), int(st.st_ino)
        hit = cache.get(rel)
        if hit and not rehash_all and hit[0] == size and hit[1] == mtime and hit[2] == ino:
            entries[rel] = (size, mtime, ino, hit[3])
            reused += 1
        else:
            todo.append((rel, size, mtime, ino))
    conflicts: list[str] = []
    hashed_bytes = 0
    with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, jobs)) as pool:
        futures = {pool.submit(hasher, root / rel): (rel, size, mtime, ino) for rel, size, mtime, ino in todo}
        for fut in concurrent.futures.as_completed(futures):
            rel, size, mtime, ino = futures[fut]
            sha = fut.result()
            entries[rel] = (size, mtime, ino, sha)
            hashed_bytes += size
            hit = cache.get(rel)
            if rehash_all and hit and hit[0] == size and hit[1] == mtime and hit[2] == ino and hit[3] != sha:
                conflicts.append(rel)
    header = {"schema": SCHEMA, "tool": TOOL, "created_at": now_iso(), "files": len(entries),
              "bytes": sum(e[0] for e in entries.values()), "digest": digest_of(entries), "hashed": len(todo),
              "hashed_bytes": hashed_bytes, "reused": reused, "rehash_all": bool(rehash_all),
              "cache_conflicts": {"count": len(conflicts), "examples": sorted(conflicts)[:EXAMPLES]},
              "seconds": round(time.monotonic() - t0, 1)}
    header.update(header_extra or {})
    return header, entries


# ============================================================================================================== files
def write_manifest(path: Path, header: dict[str, Any], entries: dict[str, tuple[int, int, int, str]]) -> None:
    """Atomic write (temporary file in the same directory, then rename)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    with gzip.open(tmp, "wt", encoding="utf-8", newline="\n", compresslevel=6) as fh:
        fh.write(json.dumps(header, ensure_ascii=False, sort_keys=True) + "\n")
        for rel in sorted(entries):
            size, mtime, ino, sha = entries[rel]
            fh.write(json.dumps([rel, size, mtime, ino, sha], ensure_ascii=False) + "\n")
    os.replace(tmp, path)


def read_manifest(path: Path) -> tuple[dict[str, Any], dict[str, tuple[int, int, int, str]]]:
    opener = gzip.open if str(path).endswith(".gz") else open
    entries: dict[str, tuple[int, int, int, str]] = {}
    with opener(path, "rt", encoding="utf-8") as fh:
        header = json.loads(fh.readline())
        if header.get("schema") != SCHEMA:
            raise ValueError(f"{path}: not a {SCHEMA} manifest")
        for line in fh:
            if line.strip():
                rel, size, mtime, ino, sha = json.loads(line)
                entries[rel] = (int(size), int(mtime), int(ino), sha)
    return header, entries


def read_header(path: Path) -> dict[str, Any]:
    opener = gzip.open if str(path).endswith(".gz") else open
    with opener(path, "rt", encoding="utf-8") as fh:
        return json.loads(fh.readline())


# ============================================================================================================ compare
def _examples(items: list[str]) -> list[str]:
    return sorted(items)[:EXAMPLES]


def compare(src_header: dict[str, Any], src: dict[str, tuple], dst_header: dict[str, Any], dst: dict[str, tuple],
            volatile: Iterable[str] | None = None) -> dict[str, Any]:
    """Source manifest (CORE, taken before the copy) against the manifest of the copy.

    * missing — in the source, not in the copy (volatile: warning; otherwise FAIL);
    * corrupt — same path, same modification second, other size or sha256, not volatile (FAIL);
    * changed_after_manifest — other content and another modification time: the file changed on CORE between the
      manifest and the copy (warning; ``stable_changed`` counts the non-volatile ones: an immutable file rewritten);
    * extra — in the copy only (warning).
    """
    volatile = list(volatile if volatile is not None else (src_header.get("set") or {}).get("volatile") or [])
    missing, missing_volatile, corrupt, changed, stable_changed = [], [], [], [], []
    equal = 0
    for rel, (size, mtime, _ino, sha) in src.items():
        other = dst.get(rel)
        vol = matches_any(rel, volatile)
        if other is None:
            (missing_volatile if vol else missing).append(rel)
            continue
        if other[0] == size and other[3] == sha:
            equal += 1
            continue
        if other[1] // 1_000_000_000 != mtime // 1_000_000_000 or vol:
            changed.append(rel)
            if not vol:
                stable_changed.append(rel)
            continue
        corrupt.append(rel)
    extra = [rel for rel in dst if rel not in src]
    verdict = "FAIL" if (missing or corrupt) else ("WARN" if (missing_volatile or changed or extra) else "PASS")

    def side(h: dict[str, Any], entries: dict[str, tuple]) -> dict[str, Any]:
        return {"created_at": h.get("created_at"), "label": h.get("label"), "files": len(entries),
                "bytes": sum(e[0] for e in entries.values()), "digest": h.get("digest")}

    return {"schema": COMPARE_SCHEMA, "verdict": verdict, "compared_at": now_iso(),
            "source": side(src_header, src), "target": side(dst_header, dst),
            "digest_equal": src_header.get("digest") == dst_header.get("digest"), "equal": equal,
            "missing": {"count": len(missing), "examples": _examples(missing)},
            "missing_volatile": {"count": len(missing_volatile), "examples": _examples(missing_volatile)},
            "corrupt": {"count": len(corrupt), "examples": _examples(corrupt)},
            "changed_after_manifest": {"count": len(changed), "stable": len(stable_changed),
                                       "examples": _examples(changed)},
            "extra": {"count": len(extra), "examples": _examples(extra)}}


def verify(root: Path, manifest: dict[str, tuple], prefixes: Iterable[str] = (), *, jobs: int = 1,
           hasher: Callable[[Path], str] = sha256_file) -> dict[str, Any]:
    """Re-hash the files of ``manifest`` found under ``root`` (a restored tree or a snapshot), limited to the path
    ``prefixes`` (all when empty); size and sha256 must match."""
    prefixes = [p.strip("/") for p in prefixes if p.strip("/")]

    def wanted(rel: str) -> bool:
        return not prefixes or any(rel == p or rel.startswith(p + "/") for p in prefixes)

    selected = {rel: e for rel, e in manifest.items() if wanted(rel)}
    missing, corrupt, ok = [], [], 0
    t0 = time.monotonic()
    present: list[str] = []
    for rel in selected:
        path = root / rel
        if not path.is_file():
            missing.append(rel)
        else:
            present.append(rel)
    with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, jobs)) as pool:
        results = dict(zip(present, pool.map(lambda r: (os.path.getsize(root / r), hasher(root / r)), present)))
    for rel in present:
        size, sha = results[rel]
        if size == selected[rel][0] and sha == selected[rel][3]:
            ok += 1
        else:
            corrupt.append(rel)
    return {"schema": VERIFY_SCHEMA, "verdict": "FAIL" if (missing or corrupt or not selected) else "PASS",
            "prefixes": prefixes, "files": len(selected), "bytes": sum(e[0] for e in selected.values()), "ok": ok,
            "missing": {"count": len(missing), "examples": _examples(missing)},
            "corrupt": {"count": len(corrupt), "examples": _examples(corrupt)},
            "seconds": round(time.monotonic() - t0, 1), "verified_at": now_iso()}


def new_bytes(src: dict[str, tuple], previous: dict[str, tuple]) -> dict[str, int]:
    """What a copy with ``--link-dest`` of the previous snapshot has to transfer: files of ``src`` whose path, size
    and sha256 are not already in ``previous``."""
    files = size = 0
    for rel, (s, _m, _i, sha) in src.items():
        prev = previous.get(rel)
        if prev is None or prev[0] != s or prev[3] != sha:
            files += 1
            size += s
    return {"files": files, "bytes": size}


# ========================================================================================================== retention
def snapshot_date(name: str) -> dt.date | None:
    """``YYYY-MM-DD`` or ``YYYY-MM-DDTHHMM`` → date; anything else (``latest``, ``*.partial``, ``*.failed``) → None."""
    if len(name) not in (10, 15) or (len(name) == 15 and name[10] != "T"):
        return None
    try:
        day = dt.date.fromisoformat(name[:10])
    except ValueError:
        return None
    if len(name) == 15 and not name[11:].isdigit():
        return None
    return day


def plan_retention(names: Iterable[str], *, daily: int = 7, weekly: int = 4, monthly: int = 6, minimum: int = 3,
                   protect: Iterable[str] = ()) -> dict[str, Any]:
    """Grandfather-father-son retention of snapshot directory names. Kept: the newest snapshot of each of the newest
    ``daily`` days, ``weekly`` ISO weeks and ``monthly`` months, the newest ``minimum`` snapshots and ``protect``."""
    snaps = sorted((n for n in names if snapshot_date(n) is not None), reverse=True)   # newest first (ISO names)
    reasons: dict[str, list[str]] = {n: [] for n in snaps}

    def newest_per(key: Callable[[dt.date], Any], limit: int, label: str) -> None:
        seen: list[Any] = []
        for n in snaps:
            k = key(snapshot_date(n))            # type: ignore[arg-type]
            if k in seen:
                continue
            if len(seen) >= limit:
                break
            seen.append(k)
            reasons[n].append(label)

    newest_per(lambda d: d, daily, "daily")
    newest_per(lambda d: tuple(d.isocalendar())[:2], weekly, "weekly")
    newest_per(lambda d: (d.year, d.month), monthly, "monthly")
    for n in snaps[:max(0, minimum)]:
        reasons[n].append("minimum")
    for n in protect:
        if n in reasons:
            reasons[n].append("protected")
    keep = [n for n in snaps if reasons[n]]
    delete = [n for n in snaps if not reasons[n]]
    return {"keep": keep, "delete": sorted(delete), "reasons": {n: r for n, r in reasons.items() if r}}


def _existing(path: Path) -> Path:
    """``path`` or its nearest existing parent (free space of a directory that does not exist yet)."""
    path = Path(path).absolute()
    while not path.exists() and path.parent != path:
        path = path.parent
    return path


def prune(snapshots_dir: Path, *, daily: int, weekly: int, monthly: int, minimum: int, min_free_bytes: int = 0,
          need_bytes: int = 0, protect: Iterable[str] = (), keep_failed: int = 1, dry_run: bool = False,
          free_bytes: Callable[[Path], int] | None = None,
          remove: Callable[[Path], None] = shutil.rmtree) -> dict[str, Any]:
    """Apply :func:`plan_retention`; then, while the free space is below ``min_free_bytes + need_bytes``, delete the
    oldest snapshot that is not protected and not among the newest ``minimum``; keep the newest ``keep_failed`` failed
    or partial runs for diagnosis. Returns what was (or, with ``dry_run``, would be) deleted."""
    free_bytes = free_bytes or (lambda p: shutil.disk_usage(_existing(p)).free)
    names = sorted(p.name for p in snapshots_dir.iterdir() if p.is_dir() and not p.is_symlink()) \
        if snapshots_dir.is_dir() else []
    protect = set(protect)
    latest = snapshots_dir / "latest"
    if latest.is_symlink():
        protect.add(os.readlink(latest).rstrip("/").rsplit("/", 1)[-1])
    plan = plan_retention(names, daily=daily, weekly=weekly, monthly=monthly, minimum=minimum, protect=protect)
    deleted: list[str] = []
    budget: list[str] = []

    def drop(name: str) -> None:
        if not dry_run:
            remove(snapshots_dir / name)
        deleted.append(name)

    for name in plan["delete"]:
        drop(name)
    failed = sorted((n for n in names if n.endswith((".failed", ".partial"))), reverse=True)
    for name in failed[max(0, keep_failed):]:
        drop(name)
    # disk guard: the oldest kept snapshots beyond the protected minimum go first (a dry run only reports)
    keep = [n for n in plan["keep"] if n not in deleted]
    protected_min = set(keep[:max(0, minimum)]) | protect
    candidates = [n for n in reversed(keep) if n not in protected_min]         # oldest first
    want = int(min_free_bytes) + int(need_bytes)
    if not dry_run:
        while want and candidates and free_bytes(snapshots_dir) < want:
            name = candidates.pop(0)
            drop(name)
            budget.append(name)
    free = free_bytes(snapshots_dir)
    return {"plan": plan, "deleted": deleted, "deleted_for_space": budget, "dry_run": dry_run,
            "free_bytes": free, "wanted_free_bytes": want, "space_ok": free >= want,
            "space_candidates": candidates if dry_run else []}


# ================================================================================================================ CLI
def _load_cache(path: str | None) -> dict[str, tuple]:
    if not path or not Path(path).is_file():
        return {}
    try:
        return read_manifest(Path(path))[1]
    except (OSError, ValueError, json.JSONDecodeError):
        return {}


def _spec(args: argparse.Namespace) -> dict[str, Any]:
    spec = json.loads(json.dumps(CORE_SET))
    if getattr(args, "packs", None):
        spec["packs"] = args.packs
    return spec


def _dump(obj: Any, out: str | None) -> None:
    text = json.dumps(obj, ensure_ascii=False, indent=1, sort_keys=True) + "\n"
    if out:
        Path(out).parent.mkdir(parents=True, exist_ok=True)
        tmp = Path(out + ".tmp")
        tmp.write_text(text, encoding="utf-8", newline="\n")
        os.replace(tmp, out)
    else:
        sys.stdout.write(text)


def cmd_build(args: argparse.Namespace) -> int:
    root = Path(args.root)
    if not root.is_dir():
        print(f"not a directory: {root}", file=sys.stderr)
        return 2
    stats: dict[str, int] = {}
    extra: dict[str, Any] = {"label": args.label}
    if args.tree:
        files = list(iter_tree(root, skip_prefixes=args.skip or ()))
        extra["set"] = {"name": "tree", "skip": args.skip or [], "volatile": CORE_SET["volatile"]}
    else:
        spec = _spec(args)
        selector = Selector(root, spec)
        files = list(iter_files(root, spec, selector, stats))
        extra["set"] = spec
        extra["resolved"] = selector.resolved()
    extra["walk"] = stats
    if args.dry_run:
        cache = _load_cache(args.cache)
        todo = [(r, st) for r, st in files if not (cache.get(r) and not args.rehash_all and
                                                   cache[r][0] == st.st_size and cache[r][1] == st.st_mtime_ns and
                                                   cache[r][2] == st.st_ino)]
        _dump({"dry_run": True, "files": len(files), "bytes": sum(st.st_size for _r, st in files),
               "to_hash": len(todo), "to_hash_bytes": sum(st.st_size for _r, st in todo), **extra}, None)
        return 0
    if not args.out:
        print("--out is required (or --dry-run)", file=sys.stderr)
        return 2
    header, entries = build(root, files, cache=_load_cache(args.cache), rehash_all=args.rehash_all, jobs=args.jobs,
                            header_extra=extra)
    write_manifest(Path(args.out), header, entries)
    summary = {k: header[k] for k in ("files", "bytes", "digest", "hashed", "hashed_bytes", "reused",
                                      "cache_conflicts", "seconds", "created_at")}
    _dump({"manifest": args.out, **summary}, None)
    return 1 if header["cache_conflicts"]["count"] else 0


def cmd_compare(args: argparse.Namespace) -> int:
    sh, se = read_manifest(Path(args.source))
    th, te = read_manifest(Path(args.target))
    report = compare(sh, se, th, te)
    _dump(report, args.out)
    if args.out:
        print(f"compare: {report['verdict']} equal={report['equal']} missing={report['missing']['count']} "
              f"corrupt={report['corrupt']['count']} changed={report['changed_after_manifest']['count']} "
              f"extra={report['extra']['count']}")
    return 1 if report["verdict"] == "FAIL" else 0


def cmd_verify(args: argparse.Namespace) -> int:
    _h, entries = read_manifest(Path(args.manifest))
    report = verify(Path(args.root), entries, args.path or (), jobs=args.jobs)
    _dump(report, args.out)
    return 0 if report["verdict"] == "PASS" else 1


def cmd_files(args: argparse.Namespace) -> int:
    _h, entries = read_manifest(Path(args.manifest))
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    with open(out, "w", encoding="utf-8", newline="\n") as fh:
        for rel in sorted(entries):
            if "\n" in rel or "\r" in rel:
                raise SystemExit(f"path with a line break cannot go into a --files-from list: {rel!r}")
            fh.write(rel + "\n")
    print(json.dumps({"files": len(entries), "list": str(out)}))
    return 0


def cmd_estimate(args: argparse.Namespace) -> int:
    _h, src = read_manifest(Path(args.manifest))
    prev = _load_cache(args.previous)
    _dump({"previous": bool(prev), **new_bytes(src, prev), "total_files": len(src),
           "total_bytes": sum(e[0] for e in src.values())}, None)
    return 0


def cmd_plan(args: argparse.Namespace) -> int:
    snaps = Path(args.snapshots_dir)
    names = [p.name for p in snaps.iterdir() if p.is_dir() and not p.is_symlink()] if snaps.is_dir() else []
    _dump(plan_retention(names, daily=args.daily, weekly=args.weekly, monthly=args.monthly, minimum=args.minimum,
                         protect=args.protect or ()), None)
    return 0


def cmd_prune(args: argparse.Namespace) -> int:
    res = prune(Path(args.snapshots_dir), daily=args.daily, weekly=args.weekly, monthly=args.monthly,
                minimum=args.minimum, min_free_bytes=int(args.min_free_gb * 1e9), need_bytes=args.need_bytes,
                protect=args.protect or (), keep_failed=args.keep_failed, dry_run=args.dry_run)
    _dump(res, args.out)
    return 0 if res["space_ok"] or args.dry_run else 1


def cmd_header(args: argparse.Namespace) -> int:
    _dump(read_header(Path(args.manifest)), None)
    return 0


def parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="vkm_manifest.py", description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    b = sub.add_parser("build", help="sha256 manifest of the backup set of a data root (or of a whole tree)")
    b.add_argument("--root", required=True)
    b.add_argument("--out", help="manifest file (*.manifest.jsonl.gz)")
    b.add_argument("--cache", help="previous manifest: reuse hashes of unchanged files (path, size, mtime, inode)")
    b.add_argument("--rehash-all", action="store_true", help="re-read every file; report cache conflicts (bit rot)")
    b.add_argument("--packs", choices=PACK_MODES, help=f"late packs in the set (default {CORE_SET['packs']})")
    b.add_argument("--tree", action="store_true", help="every file under --root (a snapshot), not the CORE set")
    b.add_argument("--skip", action="append", help="--tree: skip this relative prefix (repeatable)")
    b.add_argument("--label", default="", help="free label kept in the header (e.g. core-source, edge-snapshot)")
    b.add_argument("--jobs", type=int, default=1)
    b.add_argument("--dry-run", action="store_true", help="walk and count only: files, bytes, what would be hashed")
    b.set_defaults(func=cmd_build)
    c = sub.add_parser("compare", help="source manifest against the manifest of a copy")
    c.add_argument("source")
    c.add_argument("target")
    c.add_argument("--out")
    c.set_defaults(func=cmd_compare)
    v = sub.add_parser("verify", help="re-hash a restored tree against a manifest")
    v.add_argument("--root", required=True)
    v.add_argument("--manifest", required=True)
    v.add_argument("--path", action="append", help="only this relative prefix (repeatable)")
    v.add_argument("--jobs", type=int, default=1)
    v.add_argument("--out")
    v.set_defaults(func=cmd_verify)
    f = sub.add_parser("files", help="rsync --files-from list of a manifest")
    f.add_argument("--manifest", required=True)
    f.add_argument("--out", required=True)
    f.set_defaults(func=cmd_files)
    e = sub.add_parser("estimate", help="files and bytes a --link-dest copy has to transfer")
    e.add_argument("--manifest", required=True)
    e.add_argument("--previous", help="manifest of the previous snapshot")
    e.set_defaults(func=cmd_estimate)
    for name, fn, hlp in (("plan", cmd_plan, "retention plan of a snapshots directory (no changes)"),
                          ("prune", cmd_prune, "apply retention and the free-space guard")):
        p = sub.add_parser(name, help=hlp)
        p.add_argument("--snapshots-dir", required=True)
        p.add_argument("--daily", type=int, default=7)
        p.add_argument("--weekly", type=int, default=4)
        p.add_argument("--monthly", type=int, default=6)
        p.add_argument("--minimum", type=int, default=3)
        p.add_argument("--protect", action="append", help="snapshot name never deleted (repeatable)")
        p.set_defaults(func=fn)
        if name == "prune":
            p.add_argument("--min-free-gb", type=float, default=0.0)
            p.add_argument("--need-bytes", type=int, default=0)
            p.add_argument("--keep-failed", type=int, default=1)
            p.add_argument("--dry-run", action="store_true")
            p.add_argument("--out")
    h = sub.add_parser("header", help="print the header of a manifest")
    h.add_argument("manifest")
    h.set_defaults(func=cmd_header)
    return ap


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    return int(args.func(args) or 0)


if __name__ == "__main__":
    raise SystemExit(main())
