"""rsync transfer STAGING (WORKSTATION) → CANONICAL (CORE) and the reverse backup (H-09, H-10).

Rules: files in a data root are immutable, so every copy uses ``--ignore-existing`` (an existing file is never
rewritten; rsync writes to a temporary file and renames, never ``--inplace``). Order of a publication: artifact blobs →
canonical partitions and logs → run markers → commit markers; a commit marker therefore never arrives before the files
it lists. Leases, admission records, snapshots, ``CURRENT``, the root marker, caches and DuckDB never leave a root.
"""
from __future__ import annotations

import hashlib
import json
import re
import subprocess
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Sequence

NEVER_COPIED = ("/_leases/", "/_admission/", "/_snapshots/", "/.lock", "/CURRENT")
PUBLISH_STEPS: tuple[tuple[str, str, tuple[str, ...]], ...] = (
    ("artifact-blobs", "artifacts/", ()),
    ("canonical-files", "canonical/", ("/_commits/", "/_runs/") + NEVER_COPIED),
    ("run-markers", "canonical/_runs/", ()),
    ("commit-markers", "canonical/_commits/", ()),
)
BACKUP_STEPS: tuple[tuple[str, str, tuple[str, ...]], ...] = (
    ("artifact-blobs", "artifacts/", ()),
    ("canonical-files", "canonical/", ("/.lock",)),
)
_STAT = re.compile(r"Number of regular files transferred: ([\d,]+)|Total transferred file size: ([\d,]+)")


class TransferError(RuntimeError):
    """rsync or ssh failed; the receipt of the failed step is attached."""


@dataclass
class StepResult:
    name: str
    command: list[str]
    returncode: int
    files_transferred: int = 0
    bytes_transferred: int = 0
    stderr_tail: str = ""


@dataclass
class TransferReceipt:
    kind: str                       # PUBLISH | BACKUP
    source: str
    target: str
    dry_run: bool
    started_at: str
    finished_at: str = ""
    steps: list[StepResult] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {"kind": self.kind, "source": self.source, "target": self.target, "dry_run": self.dry_run,
                "started_at": self.started_at, "finished_at": self.finished_at,
                "files_transferred": sum(s.files_transferred for s in self.steps),
                "bytes_transferred": sum(s.bytes_transferred for s in self.steps),
                "steps": [s.__dict__ for s in self.steps]}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def rsync_command(src: str, dst: str, excludes: Sequence[str], *, dry_run: bool, rsync: str = "rsync") -> list[str]:
    cmd = [rsync, "-a", "--ignore-existing", "--mkpath", "--stats", "--no-human-readable"]
    cmd += [f"--exclude={pattern}" for pattern in excludes]
    if dry_run:
        cmd.append("--dry-run")
    return cmd + [src, dst]


def _run(name: str, cmd: list[str], runner=subprocess.run) -> StepResult:
    proc = runner(cmd, capture_output=True, text=True)
    res = StepResult(name=name, command=cmd, returncode=proc.returncode, stderr_tail=(proc.stderr or "")[-2000:])
    for m in _STAT.finditer(proc.stdout or ""):
        if m.group(1):
            res.files_transferred = int(m.group(1).replace(",", ""))
        if m.group(2):
            res.bytes_transferred = int(m.group(2).replace(",", ""))
    return res


def _split_remote(target: str) -> tuple[str | None, str]:
    host, sep, path = target.partition(":")
    return (host, path) if sep and "/" not in host else (None, target)


def publish(staging_root: Path, target: str, *, dry_run: bool = False, rsync: str = "rsync",
            runner=subprocess.run) -> TransferReceipt:
    """Copy a STAGING root to ``target`` (``host:/path`` of the CANONICAL root, or a local path in tests)."""
    from vkm_corpus.contracts.vocab import RootKind
    from vkm_corpus.parquet.layout import open_root

    layout = open_root(staging_root, RootKind.STAGING)
    receipt = TransferReceipt("PUBLISH", str(layout.root), target, dry_run, _now())
    base = target.rstrip("/")
    for name, rel, excludes in PUBLISH_STEPS:
        src = (layout.root / rel).as_posix().rstrip("/") + "/"
        if not Path(src).is_dir():
            continue
        step = _run(name, rsync_command(src, f"{base}/{rel}", excludes, dry_run=dry_run, rsync=rsync), runner)
        receipt.steps.append(step)
        if step.returncode != 0:
            receipt.finished_at = _now()
            raise TransferError(json.dumps(receipt.to_dict(), ensure_ascii=False))
    host, _ = _split_remote(target)
    if host and not dry_run:
        runner(["ssh", host, "sync"], capture_output=True, text=True)
    receipt.finished_at = _now()
    return receipt


def backup(source: str, archive_dir: Path, *, dry_run: bool = False, rsync: str = "rsync",
           runner=subprocess.run) -> TransferReceipt:
    """Reverse copy CORE → archive: canonical layer (incl. snapshots and admission records) and all blobs (H-10)."""
    receipt = TransferReceipt("BACKUP", source, str(archive_dir), dry_run, _now())
    base = source.rstrip("/")
    archive_dir.mkdir(parents=True, exist_ok=True)
    for name, rel, excludes in BACKUP_STEPS:
        dst = (archive_dir / rel).as_posix().rstrip("/") + "/"
        step = _run(name, rsync_command(f"{base}/{rel}", dst, excludes, dry_run=dry_run, rsync=rsync), runner)
        receipt.steps.append(step)
        if step.returncode != 0:
            receipt.finished_at = _now()
            raise TransferError(json.dumps(receipt.to_dict(), ensure_ascii=False))
    receipt.finished_at = _now()
    return receipt


def sha_manifest(root: Path, rels: Sequence[str] = ("canonical", "artifacts")) -> dict[str, Any]:
    """Sorted ``{relpath: sha256}`` of every file under ``rels`` plus a digest of the whole listing."""
    entries: dict[str, str] = {}
    for rel in rels:
        base = root / rel
        if not base.exists():
            continue
        for path in sorted(p for p in base.rglob("*") if p.is_file()):
            h = hashlib.sha256()
            with open(path, "rb") as f:
                for chunk in iter(lambda: f.read(1 << 20), b""):
                    h.update(chunk)
            entries[path.relative_to(root).as_posix()] = h.hexdigest()
    listing = "\n".join(f"{k} {v}" for k, v in entries.items()).encode("utf-8")
    return {"files": len(entries), "digest": hashlib.sha256(listing).hexdigest(), "entries": entries}
