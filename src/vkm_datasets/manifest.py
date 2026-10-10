"""Byte manifests and immutable versions. No copy, OCR, indexing or current-pointer side effects."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import tempfile

from .policy import Policy

SCHEMA = "vkm-dataset-version-v1"
ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}\Z")
SHA = re.compile(r"[a-f0-9]{64}\Z")


def canonical_bytes(value: dict) -> bytes:
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False) + "\n").encode()


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def fsync_directory(path: Path) -> bool:
    """Persist directory entries on POSIX; Windows directory durability is unqualified.

    Errors on a supported filesystem propagate. File flushes alone are never
    reported as a completed power-loss qualification.
    """
    if os.name != "posix":
        return False
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)
    return True


def durable_mkdir(path: Path) -> None:
    missing, current = [], Path(path)
    while not current.exists():
        missing.append(current)
        current = current.parent
    for directory in reversed(missing):
        directory.mkdir(exist_ok=True)
        fsync_directory(directory)
        fsync_directory(directory.parent)


def fsync_file(path: Path) -> None:
    # These are newly produced, owned files, never original inputs. Windows
    # requires a writable handle for FlushFileBuffers through os.fsync.
    with path.open("r+b") as stream:
        stream.flush()
        os.fsync(stream.fileno())


def durable_write_new(path: Path, raw: bytes) -> None:
    with path.open("xb") as stream:
        stream.write(raw)
        stream.flush()
        os.fsync(stream.fileno())


def relative_name(value: str) -> str:
    p = PurePosixPath(value)
    if not value or "\\" in value or ":" in value or p.is_absolute() or \
            any(x in {"", ".", ".."} for x in value.split("/")):
        raise ValueError("member paths must be relative POSIX paths without traversal")
    # Reject names that become aliases on Windows (also keeps manifests portable on Linux).
    for part in p.parts:
        if part.endswith((".", " ")) or any(c in part for c in '<>"|?*') or any(ord(c) < 32 for c in part) or \
                re.fullmatch(r"(?i)(CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\..*)?", part):
            raise ValueError("non-portable member name")
    return value


def confined(root: Path, relative: str, *, must_exist: bool = True) -> Path:
    relative_name(relative)
    root = Path(root).absolute()
    # Check parents as well as the leaf: a symlinked root is not a quarantine boundary.
    for part in (root, *root.parents):
        if part.is_symlink() or getattr(part, "is_junction", lambda: False)():
            raise ValueError("symlinks/junctions are not dataset members or roots")
    path = root
    for part in PurePosixPath(relative).parts:
        path /= part
        if path.is_symlink() or getattr(path, "is_junction", lambda: False)():
            raise ValueError("symlinks/junctions are not dataset members")
    if must_exist and not path.is_file():
        raise FileNotFoundError(relative)
    return path


@dataclass(frozen=True)
class FileMember:
    path: str
    size_bytes: int
    sha256: str
    role: str = "ORIGINAL"

    def __post_init__(self):
        relative_name(self.path)
        if type(self.size_bytes) is not int or self.size_bytes < 0 or not SHA.fullmatch(self.sha256):
            raise ValueError("invalid size/hash")
        if self.role not in {"ORIGINAL", "COMPANION", "METADATA"}:
            raise ValueError("invalid file role")

    def as_dict(self):
        return {"path": self.path, "size_bytes": self.size_bytes, "sha256": self.sha256, "role": self.role}


def discover(root: Path, entrypoints: tuple[str, ...], *, additional: tuple[str, ...] = (),
             allow_nonspatial_tab: bool = False) -> tuple[FileMember, ...]:
    """Hash explicitly selected native inputs and their sibling companions; never traverse archives.

    Native spatial TAB requires DAT/MAP/ID; IND is optional. Nonspatial TAB is opt-in.
    MIF always requires MID. Other TAB variants (raster/seamless/external tables) are refused.
    Additional project/style/metadata members must be named explicitly.
    """
    if not entrypoints:
        raise ValueError("at least one entrypoint is required")
    selected = {relative_name(p): "METADATA" for p in additional}
    for name in entrypoints:
        p = confined(root, name)
        selected[name] = "ORIGINAL"
        suffix = p.suffix.lower()
        if suffix not in {".tab", ".mif", ".gpkg", ".xlsx", ".xls", ".qgs", ".qgz"}:
            raise ValueError("unsupported dataset entrypoint")
        if suffix in {".tab", ".mif"}:
            siblings = {}
            for sibling in p.parent.iterdir():
                if sibling.stem.casefold() == p.stem.casefold():
                    key = sibling.suffix.casefold()
                    if key in siblings:
                        raise ValueError("case-ambiguous companion names")
                    siblings[key] = sibling
            if suffix == ".tab":
                with p.open("rb") as stream:
                    header = stream.read(65536).decode("latin1")
                if not re.search(r"\bType\s+NATIVE\b", header, re.IGNORECASE):
                    raise ValueError("only native MapInfo TAB is supported")
                if re.search(r"\bFile\s+[\"']", header, re.IGNORECASE):
                    raise ValueError("external TAB file reference needs explicit adapter")
                required = {".dat"} | (set() if allow_nonspatial_tab else {".map", ".id"})
                if (".map" in siblings) != (".id" in siblings):
                    raise ValueError("MAP and ID must occur together")
                known = required | {".map", ".id", ".ind"}
            else:
                required, known = {".mid"}, {".mid"}
            missing = required - siblings.keys()
            if missing:
                raise ValueError("missing MapInfo companions: " + ",".join(sorted(missing)))
            for ext in known & siblings.keys():
                rel = siblings[ext].relative_to(Path(root)).as_posix()
                confined(root, rel)
                selected[rel] = "COMPANION"
    if len({p.casefold() for p in selected}) != len(selected):
        raise ValueError("case-ambiguous dataset paths")
    result = []
    for name, role in sorted(selected.items()):
        p = confined(root, name)
        before = p.stat()
        digest = sha256(p)
        after = p.stat()
        if (before.st_size, before.st_mtime_ns, before.st_ino) != (after.st_size, after.st_mtime_ns, after.st_ino):
            raise ValueError("source changed while hashing")
        result.append(FileMember(name, after.st_size, digest, role))
    return tuple(result)


def verify_members(root: Path, members: tuple[FileMember, ...]) -> None:
    """Fresh hashes at trust boundaries. No size/mtime hash cache."""
    for member in members:
        p = confined(root, member.path)
        if p.stat().st_size != member.size_bytes or sha256(p) != member.sha256:
            raise ValueError("dataset member hash mismatch: " + member.path)


@dataclass(frozen=True)
class DatasetVersion:
    dataset_id: str
    files: tuple[FileMember, ...]
    entrypoints: tuple[str, ...]
    policy: Policy
    owner: str
    licence: str
    received_at: str
    data_valid_at: str | None = None
    available_from: str | None = None
    source_ids: tuple[str, ...] = ()
    parents: tuple[str, ...] = ()
    restrictions: str = "UNKNOWN"

    def __post_init__(self):
        if not ID.fullmatch(self.dataset_id) or not self.owner.strip() or not self.licence.strip():
            raise ValueError("dataset identity, owner and licence (or explicit UNKNOWN) are required")
        object.__setattr__(self, "files", tuple(sorted(self.files, key=lambda f: f.path)))
        for name in ("entrypoints", "source_ids", "parents"):
            object.__setattr__(self, name, tuple(getattr(self, name)))
        names = [f.path for f in self.files]
        if not names or len(set(p.casefold() for p in names)) != len(names):
            raise ValueError("manifest is empty or contains duplicate/ambiguous paths")
        if not self.entrypoints or len(set(self.entrypoints)) != len(self.entrypoints) or \
                not set(self.entrypoints) <= set(names):
            raise ValueError("entrypoints must name unique manifest members")
        if datetime.fromisoformat(self.received_at.replace("Z", "+00:00")).tzinfo is None:
            raise ValueError("received_at needs a timezone")
        for value in (self.data_valid_at, self.available_from):
            if value is not None:
                date.fromisoformat(value)
        if any(not re.fullmatch(r"VKM-SRC-\d+", sid) for sid in self.source_ids):
            raise ValueError("source_ids must refer to SOURCE_REGISTER identities")
        if any(not SHA.fullmatch(p) for p in self.parents):
            raise ValueError("parents must be full dataset manifest digests")

    def as_dict(self):
        return {"schema": SCHEMA, "dataset_id": self.dataset_id, "files": [f.as_dict() for f in self.files],
                "entrypoints": list(self.entrypoints), "policy": self.policy.as_dict(), "owner": self.owner,
                "licence": self.licence, "received_at": self.received_at, "data_valid_at": self.data_valid_at,
                "available_from": self.available_from, "source_ids": list(self.source_ids),
                "parents": list(self.parents), "restrictions": self.restrictions}

    @property
    def digest(self):
        return hashlib.sha256(canonical_bytes(self.as_dict())).hexdigest()

    @classmethod
    def from_dict(cls, obj):
        obj = dict(obj)
        if obj.pop("schema", None) != SCHEMA:
            raise ValueError("unsupported dataset manifest schema")
        obj["files"] = tuple(FileMember(**f) for f in obj["files"])
        obj["policy"] = Policy(**obj["policy"])
        return cls(**obj)


class Registry:
    """Append-only manifests grouped by logical dataset ID; indexes are rebuildable projections.

    A hard-link publishes a complete fsynced temporary file with no-overwrite semantics on NTFS/POSIX.
    No mutable CURRENT pointer, silent replacement, or destructive rollback exists.
    """
    def __init__(self, root: Path):
        self.root = Path(root)

    def register(self, version: DatasetVersion) -> Path:
        path = confined(self.root, f"{version.dataset_id}/{version.digest}.json", must_exist=False)
        raw = canonical_bytes(version.as_dict())
        durable_mkdir(path.parent)
        confined(self.root, f"{version.dataset_id}/{version.digest}.json", must_exist=False)
        if path.exists():
            if path.read_bytes() != raw:
                raise ValueError("immutable registry conflict")
            fsync_file(path)
            fsync_directory(path.parent)
            return path
        for parent in version.parents:
            if self.load(version.dataset_id, parent).digest != parent:
                raise ValueError("parent manifest hash mismatch")
        fd, tmp = tempfile.mkstemp(prefix=".pending-", dir=path.parent)
        with os.fdopen(fd, "wb") as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        try:
            os.link(tmp, path)
        except FileExistsError:
            if path.read_bytes() != raw:
                raise ValueError("immutable registry conflict")
        fsync_directory(path.parent)
        # Pending files are retained for recovery/inspection; no automatic cleanup.
        return path

    def load(self, dataset_id: str, digest: str) -> DatasetVersion:
        if not ID.fullmatch(dataset_id) or not SHA.fullmatch(digest):
            raise ValueError("invalid dataset identity")
        path = confined(self.root, f"{dataset_id}/{digest}.json")
        obj = DatasetVersion.from_dict(json.loads(path.read_text(encoding="utf-8")))
        if obj.dataset_id != dataset_id or obj.digest != digest:
            raise ValueError("registry content does not match its identity")
        return obj
