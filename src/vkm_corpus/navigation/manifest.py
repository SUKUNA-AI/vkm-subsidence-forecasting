"""File and origin checks for NAV reuse/packing; never scientific admission.

Legacy or manifest-less directories can be explored, but their identity remains
AD_HOC_UNVERIFIED. Versioned manifests supply the dataset whitelist. A valid
subset of NAV parts is enough: completeness is exposed through capabilities.
"""
from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any

MANIFEST_FORMAT = "vkm-nav-manifest-v1"
VERIFIED = "SNAPSHOT_VERIFIED"
UNVERIFIED = "AD_HOC_UNVERIFIED"


class DatasetReference(dict):
    """Serializable receipt with an in-memory origin for pre-publication checks.

    The directory is an attribute, never serialized as a machine path.
    """

    def __init__(self, values: dict[str, Any], directory: Path, tables: dict[str, Any]):
        super().__init__(values)
        self.directory = directory
        self.tables = dict(tables)

    def verify_tables(self, tables: dict[str, Any]) -> None:
        if set(tables) != set(self.tables) or any(self.tables[k] is not tables[k] and
                                                not self.tables[k].equals(tables[k]) for k in self.tables):
            raise ValueError("NAV consumed tables differ from the checked inputs")

    def verify_unchanged(self) -> None:
        manifest = self.directory / "manifest.json"
        digest = sha256_file(manifest) if manifest.is_file() else None
        if digest != self["manifest_sha256"]:
            raise ValueError("NAV input manifest changed after loading")
        for name, entry in self["datasets"].items():
            path = self.directory / entry["path"]
            if not path.is_file() or path.is_symlink() or sha256_file(path) != entry["sha256"]:
                raise ValueError(f"NAV input {name} changed after loading")


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def snapshot_identified(snapshot: dict[str, Any]) -> bool:
    sid = snapshot.get("snapshot_id")
    digest = snapshot.get("manifest_sha256")
    return bool(isinstance(sid, str) and sid and "/" not in sid and "\\" not in sid and not sid.startswith(".") and
                isinstance(digest, str) and re.fullmatch(r"[0-9a-f]{64}", digest))


def canonical_identity_match(meta: dict[str, Any], canonical: dict[str, Any]) -> bool | None:
    """File/origin integrity is separate from matching the serving canon."""
    if meta.get("identity_status") != VERIFIED:
        return None
    snapshot = meta.get("snapshot") or {}
    if not isinstance(snapshot, dict) or not snapshot_identified(snapshot):
        return None
    if snapshot["snapshot_id"] != canonical.get("snapshot_id"):
        return False
    digest = canonical.get("manifest_sha256")
    if not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest):
        return None
    return snapshot["manifest_sha256"] == digest


def load_datasets(directory: Path, snapshot_id: str | None = None, skip: tuple[str, ...] = (), *,
                  manifest_sha256: str | None = None, require_verified: bool = False):
    """Return Arrow tables, an inspectable identity receipt and the source manifest.

    Declared contradictions always fail, including in exploratory mode. Missing
    declarations remain explicitly unverified; unlisted files are never read
    when a dataset map exists. ``skip`` excludes outputs about to be rebuilt.
    """
    import pyarrow.parquet as pq

    directory = Path(directory)
    mpath = directory / "manifest.json"
    raw = mpath.read_bytes() if mpath.is_file() else None
    manifest = json.loads(raw) if raw is not None else {}
    if not isinstance(manifest, dict):
        raise ValueError("NAV manifest must be an object")
    snapshot = manifest.get("snapshot") or {}
    if not isinstance(snapshot, dict):
        raise ValueError("NAV snapshot must be an object")
    claimed = snapshot.get("snapshot_id") or manifest.get("snapshot_id")
    if (snapshot.get("snapshot_id") and manifest.get("snapshot_id")
            and snapshot["snapshot_id"] != manifest["snapshot_id"]):
        raise ValueError("NAV manifest contains conflicting snapshot IDs")
    if snapshot_id and claimed and claimed != snapshot_id:
        raise ValueError(f"--inputs is a build of {claimed}, the canon is {snapshot_id}")
    if manifest_sha256 and snapshot.get("manifest_sha256") and snapshot["manifest_sha256"] != manifest_sha256:
        raise ValueError("NAV source canonical manifest SHA-256 differs from the canon")
    declared = manifest.get("datasets")
    if declared is not None and not isinstance(declared, dict):
        raise ValueError("NAV datasets must be an object")
    listed = declared is not None
    entries = declared if listed else {p.stem: {"path": p.name} for p in sorted(directory.glob("*.parquet"))}
    verified = (manifest.get("format") == MANIFEST_FORMAT and listed and snapshot_identified(snapshot)
                and ("identity_status" not in manifest or manifest["identity_status"] == VERIFIED))
    input_ref = manifest.get("inputs") or {}
    if not isinstance(input_ref, dict):
        raise ValueError("NAV inputs reference must be an object")
    if input_ref and input_ref.get("identity_status") != VERIFIED:
        verified = False
    parts = manifest.get("parts") or {}
    if not isinstance(parts, dict) or any(not isinstance(part, dict) for part in parts.values()):
        raise ValueError("NAV parts must contain objects")
    def names(value: Any, label: str) -> set[str]:
        if (not isinstance(value, list) or any(not isinstance(x, str) for x in value)
                or len(value) != len(set(value))):
            raise ValueError(f"NAV {label} must be a unique dataset-name list")
        return set(value) - set(skip)

    selected = set(entries) - set(skip)
    if "capabilities" in manifest and names(manifest["capabilities"], "capabilities") != selected:
        raise ValueError("NAV capabilities differ from the declared datasets")
    for part_name, part in parts.items():
        if "datasets" in part:
            part_datasets = names(part["datasets"], "part datasets")
            if not part_datasets <= selected:
                raise ValueError("NAV part claims datasets absent from the dataset whitelist")
            owned = {name for name, entry in entries.items() if isinstance(entry, dict)
                     and entry.get("part") == part_name} - set(skip)
            if not owned <= part_datasets:
                raise ValueError("NAV part dataset list omits explicitly owned datasets")
            if any(isinstance(entries[name], dict) and "part" in entries[name]
                   and entries[name]["part"] != part_name for name in part_datasets):
                raise ValueError("NAV part dataset list conflicts with dataset ownership")
        imported = part.get("imported")
        if imported and (not isinstance(imported, dict) or imported.get("bundle_identity_status") != VERIFIED):
            verified = False
    tables, datasets = {}, {}
    for name, entry in sorted(entries.items()):
        if name in skip:
            continue
        if not isinstance(name, str) or not name.replace("_", "").isalnum() or not isinstance(entry, dict):
            raise ValueError("bad NAV dataset entry")
        filename = entry.get("path") or f"{name}.parquet"
        if (not isinstance(filename, str) or Path(filename).name != filename or "/" in filename
                or "\\" in filename or filename.startswith(".") or not filename.endswith(".parquet")):
            raise ValueError(f"bad NAV dataset path for {name}")
        path = directory / filename
        if not path.is_file() or path.is_symlink():
            raise ValueError(f"missing or indirect NAV dataset {name}")
        digest = sha256_file(path)
        if "sha256" in entry:
            declared_hash = entry["sha256"]
            if (not isinstance(declared_hash, str) or not re.fullmatch(r"[0-9a-f]{64}", declared_hash)
                    or declared_hash != digest):
                raise ValueError(f"NAV dataset {name}: sha256 mismatch or malformed hash")
        table = pq.read_table(path)
        if sha256_file(path) != digest:
            raise ValueError(f"NAV dataset {name}: changed while reading")
        rows, columns = table.num_rows, table.schema.names
        if "rows" in entry and (type(entry["rows"]) is not int or entry["rows"] != rows):
            raise ValueError(f"NAV dataset {name}: row count mismatch")
        if "columns" in entry and entry["columns"] != columns:
            raise ValueError(f"NAV dataset {name}: columns mismatch")
        verified = verified and all(k in entry for k in ("sha256", "rows", "columns"))
        tables[name] = table
        datasets[name] = {"path": filename, "rows": rows, "columns": columns, "sha256": digest}
    listed_files = {v.get("path") or f"{k}.parquet" for k, v in entries.items() if isinstance(v, dict)}
    ref = DatasetReference({
        "dir": directory.name, "snapshot_id": claimed, "claimed_snapshot_id": claimed, "snapshot": snapshot,
        "manifest_sha256": hashlib.sha256(raw).hexdigest() if raw is not None else None,
        "identity_status": VERIFIED if verified else UNVERIFIED,
        "navigation_only": True, "scientific_decision": "NOT_CHECKED", "datasets": datasets,
        "capabilities": sorted(tables),
        "excluded_unmanifested": sorted(p.name for p in directory.glob("*.parquet")
                                        if listed and p.name not in listed_files)}, directory, tables)
    if require_verified and not verified:
        raise ValueError("NAV input identity is AD_HOC_UNVERIFIED; a verified snapshot manifest is required")
    return tables, ref, manifest
