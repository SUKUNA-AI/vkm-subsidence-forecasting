"""NAV part dependencies and inspectable, content-bound reuse identities.

These are navigation provenance, never scientific admission. Physical orphan
files are deliberately retained; only the manifest whitelist is consumable.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

CONTRACT = "vkm-nav-dependencies/1"
UPSTREAM = {
    "sections": (),
    "formulas": ("sections", "section_pages"),
    "tables": ("sections", "section_pages", "formula_symbols"),
    "parameters": ("sections", "section_pages", "formula_parameters", "formula_symbols", "table_structure", "table_cells"),
    "duplicates": (),
    "object_duplicates": ("source_overlap", "dup_members", "formula_context", "formula_symbols"),
    "concepts": ("section_pages",),
    "translations": ("terms", "term_edges", "term_mentions", "formula_symbols"),
    "figure_series": (),
    "topics": ("sections", "section_pages", "terms", "term_mentions"),
}


def digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"),
                                     ensure_ascii=False, allow_nan=False).encode("utf-8")).hexdigest()


def upstream(part: str, options: dict, offered=()) -> tuple[str, ...]:
    # Extension/test builders conservatively bind every offered dataset.
    names = UPSTREAM.get(part, tuple(sorted(offered)))
    if part == "concepts" and options.get("drop_duplicate_blocks"):
        names += ("dup_members",)
    return names


def table_digest(table) -> str:
    import pyarrow as pa
    sink = pa.BufferOutputStream()
    with pa.ipc.new_stream(sink, table.schema) as stream:
        stream.write_table(table.combine_chunks())
    return hashlib.sha256(sink.getvalue().to_pybytes()).hexdigest()


def binding(manifest: dict, name: str) -> dict | None:
    entry = manifest.get("datasets", {}).get(name)
    origin = manifest
    if entry is None:
        # A tombstone must beat an older --inputs or historical external origin.
        if name in manifest.get("invalidated_datasets", {}):
            return None
        origin = manifest.get("inputs") or {}
        entry = origin.get("datasets", {}).get(name)
    if entry is None:
        return None
    owner = entry.get("part")
    part = origin.get("parts", {}).get(owner, {})
    if part.get("status", "BUILT") != "BUILT":
        return None
    return {"sha256": entry["sha256"], "part": owner,
            "rule_version": part.get("rule_version"),
            "producer_input_sha256": part.get("input_sha256") if origin.get("dependency_contract") == CONTRACT else None,
            "snapshot": origin.get("snapshot", manifest.get("snapshot")),
            "identity_status": entry.get("identity_status", origin.get("identity_status", "AD_HOC_UNVERIFIED"))}


def file_identity(path: Path) -> dict:
    from vkm_corpus.navigation.manifest import sha256_file
    if path.is_symlink() or not path.is_file():
        raise ValueError("NAV dependency file is missing or indirect")
    before = path.stat()
    result = {"name": path.name, "sha256": sha256_file(path), "bytes": before.st_size}
    after = path.stat()
    if (before.st_size, before.st_mtime_ns, before.st_ctime_ns, before.st_ino) != (
            after.st_size, after.st_mtime_ns, after.st_ctime_ns, after.st_ino):
        raise ValueError("NAV dependency changed while hashing")
    return result


def value_identity(value: Any) -> Any:
    """Bind file-valued options to bytes, not a machine-specific spelling."""
    if isinstance(value, (Path, str)):
        path = Path(value)
        try:
            exists = path.exists()
        except OSError:
            exists = False  # ordinary, possibly long option text
        if exists:
            if path.is_dir():
                return {"directory": path.name, "qualification": "UNVERIFIED_EXTERNAL_DIRECTORY"}
            return file_identity(path)
        if isinstance(value, Path):
            raise ValueError("NAV dependency path does not exist")
    if isinstance(value, dict):
        return {str(k): value_identity(v) for k, v in sorted(value.items())}
    if isinstance(value, (tuple, list)):
        return [value_identity(v) for v in value]
    return value


def external_identity(part: str, *, outlines=None, vectors=None, artifacts=None) -> dict:
    out = {}
    if part == "sections":
        out["outlines_sha256"] = digest(outlines) if outlines is not None else None
    if part in {"duplicates", "topics"}:
        if vectors is None:
            out["vectors"] = None
        else:
            directory = Path(vectors)
            if directory.is_symlink() or not directory.is_dir():
                raise ValueError("NAV vector directory is missing or indirect")
            files = sorted(directory.glob("part-*.parquet"))
            if not files:
                raise ValueError("NAV vectors have no part files")
            config = directory / "config.json"
            out["vectors"] = [file_identity(p) for p in ([config] if config.exists() else []) + files]
    if part == "object_duplicates" and artifacts is not None:
        # No recursive artifact scan. Native artifact qualification is separate.
        out["artifacts"] = {"qualification": "UNVERIFIED_EXTERNAL_DIRECTORY"}
    return out


def recipe(part: str, manifest: dict, options: dict, external: dict, offered=()) -> dict:
    from vkm_corpus.navigation.ids import RULE_VERSIONS
    return {"contract": CONTRACT, "snapshot": manifest["snapshot"],
            "rule_version": RULE_VERSIONS.get(part), "options": value_identity(options),
            "declared_options_sha256": digest(value_identity(options)),
            "upstream": {name: binding(manifest, name) for name in upstream(part, options, offered)},
            "external": external}


def invalidate(manifest: dict, part: str, reason: str) -> None:
    record = manifest["parts"][part]
    removed = {name: entry for name, entry in manifest["datasets"].items()
               if entry.get("part") == part or name in record.get("datasets", [])}
    manifest.setdefault("invalidated_datasets", {}).update({name: {"part": part, "reason": reason,
                                                                    "sha256": entry.get("sha256")}
                                                             for name, entry in removed.items()})
    for name in removed:
        del manifest["datasets"][name]
    record.update(status="STALE_UPSTREAM", datasets=[], stale_reason=reason, stale_datasets=removed)


def reconcile(manifest: dict, *, current_rules: bool = False, rebuilding=()) -> set[str]:
    """Invalidate the transitive affected closure; absent optional inputs are bound too."""
    from vkm_corpus.navigation.ids import RULE_VERSIONS
    invalid = set()
    while True:
        changed = False
        for part, record in list(manifest.get("parts", {}).items()):
            if record.get("status") != "BUILT" or part in rebuilding:
                continue
            proof = record.get("input_identity")
            if not proof or record.get("input_sha256") != digest(proof):
                reason = "DEPENDENCY_PROOF_MISSING_OR_INVALID"
            elif proof.get("snapshot") != manifest.get("snapshot"):
                reason = "CANONICAL_SNAPSHOT_CHANGED"
            elif current_rules and proof.get("rule_version") != RULE_VERSIONS.get(part):
                reason = "RULE_CHANGED"
            elif any(value != binding(manifest, name) for name, value in proof.get("upstream", {}).items()):
                reason = "UPSTREAM_CHANGED"
            else:
                continue
            invalidate(manifest, part, reason)
            invalid.add(part)
            changed = True
        if not changed:
            return invalid


def validate(manifest: dict) -> None:
    """Reject forged/mixed new-contract manifests, including re-added stale files."""
    if "dependency_contract" not in manifest:
        return  # legacy identity remains a separate, explicitly named contract
    if manifest["dependency_contract"] != CONTRACT:
        raise ValueError("unknown NAV dependency contract")
    for name, entry in manifest.get("datasets", {}).items():
        record = manifest.get("parts", {}).get(entry.get("part"), {})
        if record.get("status") != "BUILT" or name not in record.get("datasets", []):
            raise ValueError("NAV dataset belongs to a stale or missing part")
        if entry.get("identity_status") != record.get("identity_status"):
            raise ValueError("NAV dataset qualification differs from its producer")
        if manifest.get("identity_status") == "SNAPSHOT_VERIFIED" and entry.get("identity_status") != "SNAPSHOT_VERIFIED":
            raise ValueError("NAV manifest promotes an unqualified dependency")
        if name in manifest.get("invalidated_datasets", {}):
            raise ValueError("NAV stale dataset was reintroduced")
    for part, record in manifest.get("parts", {}).items():
        if record.get("status") != "BUILT":
            continue
        proof = record.get("input_identity")
        if (not isinstance(proof, dict) or proof.get("contract") != CONTRACT
                or record.get("input_sha256") != digest(proof)
                or proof.get("snapshot") != manifest.get("snapshot")
                or proof.get("rule_version") != record.get("rule_version")
                or proof.get("options") != record.get("options", {})
                or proof.get("declared_options_sha256") != digest(record.get("options", {}))
                or not isinstance(proof.get("upstream"), dict)
                or (part in UPSTREAM and set(proof["upstream"]) != set(upstream(part, proof.get("options", {}))))
                or any(value != binding(manifest, name) for name, value in proof["upstream"].items())):
            raise ValueError(f"NAV part {part}: dependency identity mismatch")
