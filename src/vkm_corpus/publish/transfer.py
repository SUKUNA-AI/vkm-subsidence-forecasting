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
    ("accounting", "accounting/", ("/tmp/",)),
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
    accounting: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {"kind": self.kind, "source": self.source, "target": self.target, "dry_run": self.dry_run,
                "started_at": self.started_at, "finished_at": self.finished_at,
                "files_transferred": sum(s.files_transferred for s in self.steps),
                "bytes_transferred": sum(s.bytes_transferred for s in self.steps),
                "steps": [s.__dict__ for s in self.steps], **({"accounting": self.accounting} if self.accounting else {})}


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
    if re.match(r"^[A-Za-z]:[\\/]", target):
        return None, target  # Windows drive prefix is not an SSH host.
    host, sep, path = target.partition(":")
    return (host, path) if sep and "/" not in host else (None, target)


def publish(staging_root: Path, target: str, *, dry_run: bool = False, rsync: str = "rsync",
            runner=subprocess.run, publication_approval=None, policy_path=None) -> TransferReceipt:
    """Copy a STAGING root to ``target`` (``host:/path`` of the CANONICAL root, or a local path in tests)."""
    from vkm_corpus.contracts.vocab import RootKind
    from vkm_corpus.parquet.layout import open_root

    layout = open_root(staging_root, RootKind.STAGING)
    if publication_approval is not None:
        return publish_accounted(layout.root, target, publication_approval, policy_path=policy_path,
                                 dry_run=dry_run, rsync=rsync, runner=runner)
    from vkm_corpus.parquet.commits import list_markers
    if any(m.get("accounting_required") for m in list_markers(layout)) or \
            (layout.root / "accounting" / "publications").exists():
        raise TransferError("PUBLICATION_OPERATOR_APPROVAL_REQUIRED")
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
        step = _run("remote-sync", ["ssh", host, "sync"], runner)
        receipt.steps.append(step)
        if step.returncode:
            raise TransferError("PUBLICATION_REMOTE_SYNC_FAILED")
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


def sha_manifest(root: Path, rels: Sequence[str] = ("canonical", "artifacts", "accounting")) -> dict[str, Any]:
    """Sorted ``{relpath: sha256}`` of every file under ``rels`` plus a digest of the whole listing."""
    entries: dict[str, str] = {}
    for rel in rels:
        base = root / rel
        if not base.exists():
            continue
        for path in sorted(p for p in base.rglob("*") if p.is_file()):
            if path.relative_to(root).as_posix().startswith("accounting/tmp/"):
                continue
            h = hashlib.sha256()
            with open(path, "rb") as f:
                for chunk in iter(lambda: f.read(1 << 20), b""):
                    h.update(chunk)
            entries[path.relative_to(root).as_posix()] = h.hexdigest()
    listing = "\n".join(f"{k} {v}" for k, v in entries.items()).encode("utf-8")
    return {"files": len(entries), "digest": hashlib.sha256(listing).hexdigest(), "entries": entries}


def publish_accounted(root, target, approval, *, policy_path, dry_run=False, rsync="rsync", runner=subprocess.run):
    from vkm_corpus.coverage.publication import verify_publication, descriptor_path, file_ref, read_ref
    from vkm_corpus.coverage.accounting import _path
    from vkm_corpus.parquet.atomic import write_bytes
    descriptor = verify_publication(root, approval, policy_path=policy_path)
    ref = file_ref(root, descriptor_path(approval.descriptor_sha256))
    groups = {k: [] for k in ("content", "run-markers", "commit-markers", "publication-marker")}
    for item in descriptor.files:
        key = "commit-markers" if item.path.startswith("canonical/_commits/") else \
            "run-markers" if item.path.startswith("canonical/_runs/") else "content"
        groups[key].append(item)
    groups["publication-marker"] = [ref]
    receipt = TransferReceipt("PUBLISH", str(root), target, dry_run, _now())
    for name, files in groups.items():
        if not files:
            continue
        # Recheck policy and pinned bytes immediately before each transport step.
        from vkm_corpus.coverage.publication import authorize
        authorize(policy_path, approval.policy_sha256, approval.source_ids, approval.context)
        for item in files:
            read_ref(root, item)
        data = b"".join(item.path.encode("utf-8") + b"\0" for item in files)
        listing = _path(root, "accounting/tmp/" + hashlib.sha256(data).hexdigest() + ".files")
        write_bytes(_path(root, "accounting/tmp"), listing, data)
        cmd = [rsync, "-a", "--ignore-existing", "--mkpath", "--stats", "--no-human-readable",
               "--from0", "--files-from=" + str(listing)]
        if dry_run:
            cmd.append("--dry-run")
        step = _run(name, cmd + [Path(root).as_posix().rstrip("/") + "/", target.rstrip("/") + "/"], runner)
        receipt.steps.append(step)
        if step.returncode:
            raise TransferError("PUBLICATION_TRANSFER_FAILED:" + name)
    host, _ = _split_remote(target)
    if host and not dry_run:
        step = _run("remote-sync", ["ssh", host, "sync"], runner)
        receipt.steps.append(step)
        if step.returncode:
            raise TransferError("PUBLICATION_REMOTE_SYNC_FAILED")
    if not host and not dry_run:
        verify_publication(Path(target), approval, policy_path=policy_path, receiving=True)
    receipt.accounting = {"descriptor_sha256": approval.descriptor_sha256,
        "campaign_sha256": approval.campaign_sha256,
        "receiving_status": "VERIFIED" if not host and not dry_run else "NOT_RUN",
        "admission": "NOT_RUN", "physical_durability": "NOT_QUALIFIED"}
    # Remote transfer success means bytes sent; only CORE admission verifies it.
    receipt.finished_at = _now()
    return receipt
