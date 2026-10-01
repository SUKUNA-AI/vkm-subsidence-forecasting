"""CPU-only issuer for a legacy late pack's exact canonical/policy binding.

The issuer replays deterministic text-unit rules over pinned canonical bytes;
it never encodes, loads a model, changes a pack, or qualifies model quality.
The public entry point always uses the existing hard-bounded child runner.
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
import sys
import time
from pathlib import Path
from typing import Literal

from pydantic import ConfigDict, Field, model_validator

from vkm_corpus.contracts.access import AccessContext
from vkm_corpus.graph.shadow_bundle import ordinary, checked_file
from vkm_corpus.parquet.atomic import create_exclusive, sha256_of
from vkm_corpus.update.runtime import BoundFile, bounded_subprocess
from vkm_evidence.contracts import Sha256, StrictModel, canonical_bytes, record_hash

KINDS = ("BLOCK_GROUP", "FIGURE", "TABLE", "FORMULA", "BIB_ENTRY")
CHECKS = ("canonical_files", "pack_files", "config", "unit_identity", "unit_text", "complete_selected_scope",
          "token_layout", "source_policy", "input_fences")


class PackPolicyRequest(StrictModel):
    canonical_root: str
    snapshot_id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{0,119}$")
    canonical_manifest_sha256: Sha256
    pack_manifest: BoundFile
    policy: BoundFile
    context: BoundFile
    selected_kinds: tuple[Literal["BLOCK_GROUP", "FIGURE", "TABLE", "FORMULA", "BIB_ENTRY"], ...] = KINDS
    max_units: int = Field(gt=0, le=100_000_000)
    max_source_rows: int = Field(gt=0, le=10_000_000)
    max_canonical_bytes: int = Field(gt=0)
    max_pack_bytes: int = Field(gt=0)
    max_spool_bytes: int = Field(gt=0)
    memory_gib: float = Field(gt=0, le=128)
    min_free_disk_gib: float = Field(gt=0)
    timeout_seconds: int = Field(gt=0, le=24 * 3600)

    @model_validator(mode="after")
    def _scope(self):
        if not self.selected_kinds or len(set(self.selected_kinds)) != len(self.selected_kinds):
            raise ValueError("exact nonempty unique unit-kind scope required")
        return self


class PackPolicyBinding(StrictModel):
    status: Literal["PASS"] = "PASS"
    component_manifest_sha256: Sha256
    policy_sha256: Sha256
    snapshot_id: str
    canonical_manifest_sha256: Sha256


class LoadedPackIdentity(StrictModel):
    model_config = ConfigDict(serialize_by_alias=True, validate_by_name=True)
    schema_version: Literal["vkm-loaded-pack/1"] = Field(default="vkm-loaded-pack/1", alias="schema")
    pack_id: str = Field(min_length=1)
    snapshot_id: str = Field(min_length=1)
    manifest_sha256: Sha256
    config_signature: Sha256
    config_sha256: Sha256
    files: dict[str, Sha256]
    count: int = Field(gt=0)
    total_tokens: int = Field(gt=0)

    @model_validator(mode="after")
    def _files(self):
        if set(self.files) != {"tokens.f16", "index.parquet"} or self.total_tokens < self.count:
            raise ValueError("complete loaded pack inventory required")
        return self


class PackPolicyQualification(StrictModel):
    model_config = ConfigDict(serialize_by_alias=True, validate_by_name=True)
    schema_version: Literal["vkm-late-policy-qualification/1"] = Field(default="vkm-late-policy-qualification/1", alias="schema")
    status: Literal["PASS"] = "PASS"
    scope: Literal["CANONICAL_UNIT_POLICY_BINDING"] = "CANONICAL_UNIT_POLICY_BINDING"
    binding: PackPolicyBinding
    binding_sha256: Sha256
    loaded_pack: LoadedPackIdentity
    context_sha256: Sha256
    issuer: dict
    unit_rule: str
    context_variant: Literal["A", "B", "C", "D"]
    unit_config: dict
    selected_kinds: tuple[Literal["BLOCK_GROUP", "FIGURE", "TABLE", "FORMULA", "BIB_ENTRY"], ...]
    units_sha256: Sha256
    unit_count: int = Field(gt=0)
    by_kind: dict[str, int]
    source_count: int = Field(gt=0)
    canonical_files_sha256: Sha256
    checks: dict[str, Literal["PASS"]]
    model_quality: Literal["NOT_RUN"] = "NOT_RUN"
    scientific_qualification: Literal["NOT_RUN"] = "NOT_RUN"

    @model_validator(mode="after")
    def _coherent(self):
        from vkm_corpus.retrieval_lab.units import UNIT_RULE, UnitConfig
        if (set(self.checks) != set(CHECKS) or self.unit_count != self.loaded_pack.count
                or self.binding.component_manifest_sha256 != self.loaded_pack.manifest_sha256
                or self.binding.snapshot_id != self.loaded_pack.snapshot_id
                or self.binding_sha256 != record_hash(self.binding)
                or self.unit_rule != UNIT_RULE or self.unit_config != UnitConfig().as_dict()
                or not self.selected_kinds or len(set(self.selected_kinds)) != len(self.selected_kinds)
                or set(self.by_kind) - set(self.selected_kinds)
                or any(type(n) is not int or n <= 0 for n in self.by_kind.values())
                or sum(self.by_kind.values()) != self.unit_count):
            raise ValueError("incoherent late policy qualification")
        return self


def issuer_identity():
    from importlib.metadata import version
    from vkm_corpus.retrieval_lab import units, canon, textproc
    from vkm_corpus.duckdb import build
    from vkm_corpus.embeddings import signature
    from vkm_corpus.contracts import access, arrow, datasets, models
    from vkm_corpus.graph import common
    from vkm_evidence import contracts
    from vkm_corpus.graph.canon import derived_sql_files
    modules = (units, canon, textproc, build, signature, access, arrow, datasets, models, common, contracts)
    return {"schema": "vkm-late-policy-issuer/1",
        "code": {**{m.__name__: sha256_of(Path(m.__file__)) for m in modules},
                 "vkm_corpus.update.pack_policy": sha256_of(Path(__file__))},
        "sql": {name: hashlib.sha256(data).hexdigest() for name, data in derived_sql_files()},
        "dependencies": {name: version(name) for name in ("duckdb", "pyarrow", "pydantic")}}


def verify_pack_policy_receipt(receipt_bytes: bytes, expected_sha256: str, binding: dict, loaded_pack: dict):
    """Pinned durable issuer output + actual authenticated load-time native pack.

    Receipt trust comes from its operator-approved SHA, never a caller's PASS.
    Requalify after changing issuer/unit rules; no embedding/model execution here.
    """
    if len(receipt_bytes) > 2 * 1024**2 or hashlib.sha256(receipt_bytes).hexdigest() != expected_sha256:
        raise ValueError("late policy qualification bytes changed")
    receipt = PackPolicyQualification.model_validate_json(receipt_bytes)
    if receipt.binding != PackPolicyBinding.model_validate(binding) or receipt.loaded_pack != LoadedPackIdentity.model_validate(loaded_pack):
        raise ValueError("late policy receipt differs from actual native pack or binding")
    if receipt.issuer != issuer_identity():
        raise ValueError("late policy issuer/unit rule code or dependencies changed")
    return receipt


def _qualify(request: PackPolicyRequest, output: Path):
    """Fixed worker implementation. Public callers use issue_pack_policy."""
    import pyarrow.parquet as pq
    from vkm_corpus.graph.common import load_snapshot, verify_snapshot_files
    from vkm_corpus.contracts.policy_store import SourcePolicyStore
    from vkm_corpus.parquet.layout import CanonLayout
    from vkm_corpus.retrieval_lab.canon import CanonReader
    from vkm_corpus.retrieval_lab.units import UNIT_RULE, UnitConfig, build_units, render
    from vkm_corpus.embeddings.signature import EmbeddingConfig
    from vkm_corpus.embeddings.pack import PACK_SCHEMA

    start = time.monotonic()
    code = issuer_identity()
    artifacts = {}
    def budget():
        if time.monotonic() - start > request.timeout_seconds:
            raise TimeoutError("pack policy issuer deadline exceeded")
        if shutil.disk_usage(output).free < request.min_free_disk_gib * 1024**3:
            raise ValueError("pack policy issuer free disk guard")
        db = output / "units.sqlite"
        if db.exists() and db.stat().st_size > request.max_spool_bytes:
            raise ValueError("pack policy issuer spool budget exceeded")
    def pin(path, digest):
        budget()
        path = checked_file(path, digest)
        if path in artifacts and artifacts[path] != digest:
            raise ValueError("conflicting pack policy input pin")
        artifacts[path] = digest
        return path
    def fence():
        for path, digest in artifacts.items():
            budget(); checked_file(path, digest)
        if issuer_identity() != code:
            raise ValueError("pack policy issuer changed during qualification")
    policy = pin(request.policy.path, request.policy.sha256)
    context_path = pin(request.context.path, request.context.sha256)
    context = AccessContext.model_validate_json(context_path.read_bytes())
    source_policies = SourcePolicyStore(policy, lambda: ()).read()
    def require_source(sid):
        if sid not in source_policies:
            raise PermissionError("RESOURCE_POLICY_UNCLASSIFIED")
        source_policies[sid].require(context)
    root = Path(request.canonical_root)
    marker = ordinary(root / ".vkm_root.json")
    if json.loads(marker.read_bytes()).get("root_kind") != "CANONICAL":
        raise ValueError("canonical root required for pack qualification")
    pin(marker, sha256_of(marker))
    snapshot = load_snapshot(root, request.snapshot_id)
    pin(root / "canonical" / "_snapshots" / (request.snapshot_id + ".json"), request.canonical_manifest_sha256)
    if snapshot.manifest_sha256 != request.canonical_manifest_sha256:
        raise ValueError("exact canonical snapshot unavailable")
    sizes = 0
    canonical_files = {}
    for ds in snapshot.datasets.values():
        if len(ds.paths) != len(ds.expected): raise ValueError("canonical files lack exact pins")
        for path, entry in zip(ds.paths, ds.expected):
            sizes += ordinary(path).stat().st_size
            if sizes > request.max_canonical_bytes: raise ValueError("canonical hashing byte budget exceeded")
            pin(path, entry.get("sha256"))
            canonical_files[path.relative_to(root).as_posix()] = entry["sha256"]
    verify_snapshot_files(snapshot)
    for path in snapshot.datasets["sources"].paths:
        for batch in pq.ParquetFile(path).iter_batches(batch_size=500, columns=["source_id"]):
            for row in batch.to_pylist(): require_source(row["source_id"])

    manifest_path = pin(request.pack_manifest.path, request.pack_manifest.sha256)
    pack = json.loads(manifest_path.read_bytes())
    if (pack.get("schema") != PACK_SCHEMA or pack.get("snapshot_id") != request.snapshot_id
            or set(pack.get("files", {})) != {"tokens.f16", "index.parquet"}
            or pack.get("dtype") != "float16" or pack.get("byte_order") != "little"):
        raise ValueError("unsupported or different-snapshot late pack")
    cfg = EmbeddingConfig(**pack["config"])
    if (cfg.mode != "multivector" or type(cfg.dimension) is not int or not 1 <= cfg.dimension <= 8192
            or cfg.signature() != pack.get("config_signature") or cfg.dimension != pack.get("dimension")
            or cfg.text_rule != pack.get("text_rule") or cfg.text_rule not in {UNIT_RULE + "/" + v for v in "ABCD"}
            or cfg.model_id != pack.get("model_id") or cfg.model_revision != pack.get("model_revision")):
        raise ValueError("unsupported or internally inconsistent late pack config")
    variant = cfg.text_rule.rsplit("/", 1)[1]
    files, packed_bytes = {}, 0
    for name, meta in pack["files"].items():
        path = ordinary(manifest_path.parent / name)
        packed_bytes += path.stat().st_size
        if packed_bytes > request.max_pack_bytes or path.stat().st_size != meta.get("bytes"):
            raise ValueError("pack hashing byte budget or native file size mismatch")
        pin(path, meta.get("sha256")); files[name] = meta["sha256"]
    count, total = pack.get("count"), pack.get("total_tokens")
    if (type(count) is not int or not 0 < count <= request.max_units or type(total) is not int or total < count
            or pack["files"]["tokens.f16"]["bytes"] != total * cfg.dimension * 2):
        raise ValueError("pack token count/dimension/size mismatch")

    dbpath = output / "units.sqlite"
    fd = os.open(dbpath, os.O_CREAT | os.O_EXCL | os.O_RDWR, 0o600); os.close(fd)
    con = sqlite3.connect(dbpath)
    con.execute("PRAGMA cache_size=-8192")
    con.execute("CREATE TABLE units (id TEXT PRIMARY KEY, payload TEXT NOT NULL, matched INTEGER NOT NULL DEFAULT 0)")
    reader = None
    try:
        reader = CanonReader.from_layout(CanonLayout(root), request.snapshot_id)
        reader.con.execute("SET threads=1")
        reader.con.execute("SET memory_limit=?", [str(max(64, int(request.memory_gib * 512))) + "MB"])
        # Per-source bounds are checked before load_all; unchanged production
        # rules preserve cross-page section/neighbor context within each source.
        meta = reader.source_meta()
        labels = reader.page_labels() if variant == "D" else {}
        unit_count, by_kind, sources = 0, {}, set()
        for sid in reader.source_ids():
            require_source(sid); budget()
            nrows = 0
            for table in ("pages", "blocks", "figures", "tables", "formulas", "bibliography_entries"):
                nrows += reader.con.execute('SELECT count(*) FROM canonical."' + table + '" WHERE source_id=?', [sid]).fetchone()[0]
            if nrows > request.max_source_rows: raise ValueError("single source exceeds row memory guard")
            data = reader.load_all([sid])
            units = build_units(data["pages"], data["blocks"], data["figures"], data["tables"], data["formulas"], data["bibliography"], UnitConfig())
            for u in units:
                if u.kind not in request.selected_kinds: continue
                require_source(u.source_id)
                if u.source_id != sid: raise ValueError("unit crosses source identity")
                text = render(u, variant, meta.get(sid), labels.get(u.page_id or ""))
                row = {"unit_id": u.unit_id, "kind": u.kind, "source_id": u.source_id, "page_id": u.page_id,
                       "object_ids": list(u.object_ids), "text_hash": hashlib.sha256(text.encode()).hexdigest()}
                try: con.execute("INSERT INTO units (id,payload) VALUES (?,?)", (u.unit_id, canonical_bytes(row).decode()))
                except sqlite3.IntegrityError as exc: raise ValueError("duplicate regenerated unit identity") from exc
                unit_count += 1
                if unit_count > request.max_units: raise ValueError("regenerated unit budget exceeded")
                sources.add(sid); by_kind[u.kind] = by_kind.get(u.kind, 0) + 1
            con.commit(); budget()
        if unit_count != count or pack.get("by_kind") != by_kind:
            raise ValueError("pack is not complete for the declared canonical unit-kind scope")
        parquet = pq.ParquetFile(manifest_path.parent / "index.parquet")
        cols = {"unit_id", "kind", "source_id", "page_id", "object_ids", "text_hash", "token_offset", "n_tokens"}
        if set(parquet.schema_arrow.names) != cols or parquet.metadata.num_rows != count:
            raise ValueError("pack index schema/count differs")
        offset, actual_count = 0, 0
        for batch in parquet.iter_batches(batch_size=500):
            budget()
            for row in batch.to_pylist():
                expected = con.execute("SELECT payload,matched FROM units WHERE id=?", (row["unit_id"],)).fetchone()
                canonical_row = {k: row[k] for k in cols - {"token_offset", "n_tokens"}}
                if expected is None or expected[1] or json.loads(expected[0]) != canonical_row:
                    raise ValueError("pack unit identity/text differs, is extra, or is duplicated")
                if type(row["n_tokens"]) is not int or row["n_tokens"] <= 0 or row["token_offset"] != offset:
                    raise ValueError("pack token offsets are not exact contiguous native layout")
                offset += row["n_tokens"]; actual_count += 1
                con.execute("UPDATE units SET matched=1 WHERE id=?", (row["unit_id"],))
            con.commit()
        if offset != total or actual_count != count or con.execute("SELECT count(*) FROM units WHERE matched=0").fetchone()[0]:
            raise ValueError("pack token layout or canonical unit coverage incomplete")
        digest = hashlib.sha256()
        for (payload,) in con.execute("SELECT payload FROM units ORDER BY id"):
            digest.update(payload.encode() + b"\n")
        loaded = LoadedPackIdentity(pack_id=pack["pack_id"], snapshot_id=request.snapshot_id,
            manifest_sha256=request.pack_manifest.sha256, config_signature=cfg.signature(), config_sha256=record_hash(pack["config"]),
            files=files, count=count, total_tokens=total)
        binding = PackPolicyBinding(component_manifest_sha256=request.pack_manifest.sha256, policy_sha256=request.policy.sha256,
            snapshot_id=request.snapshot_id, canonical_manifest_sha256=request.canonical_manifest_sha256)
        fence()
        receipt = PackPolicyQualification(binding=binding, binding_sha256=record_hash(binding), loaded_pack=loaded,
            context_sha256=request.context.sha256, issuer=code, unit_rule=UNIT_RULE, context_variant=variant,
            unit_config=UnitConfig().as_dict(), selected_kinds=request.selected_kinds, units_sha256=digest.hexdigest(),
            unit_count=unit_count, by_kind=by_kind, source_count=len(sources), canonical_files_sha256=record_hash(canonical_files),
            checks=dict.fromkeys(CHECKS, "PASS"))
        fence(); budget()
        if not create_exclusive(output / "binding.json", canonical_bytes(binding).decode()):
            raise ValueError("pack binding publication conflict")
        if not create_exclusive(output / "qualification.json", canonical_bytes(receipt).decode()):
            raise ValueError("pack qualification publication conflict")
        return receipt
    finally:
        if reader is not None: reader.close()
        con.close()


def issue_pack_policy(request: PackPolicyRequest, output_dir: Path):
    """Run the fixed CPU-only issuer under hard process-tree timeout/RLIMIT_AS."""
    output = Path(output_dir).absolute()
    roots = (Path(request.canonical_root), Path(request.pack_manifest.path).parent)
    if output.exists() or any(output.resolve().is_relative_to(p.resolve()) or p.resolve().is_relative_to(output.resolve()) for p in roots):
        raise ValueError("new issuer output must be disjoint from protected input roots")
    if any(p.is_symlink() or (hasattr(p, "is_junction") and p.is_junction()) for p in output.parents):
        raise ValueError("indirect pack issuer output")
    if sys.platform != "linux":
        raise ValueError("qualified pack-policy child resource enforcement requires Linux")
    output.mkdir(parents=True, mode=0o700)
    request_bytes = canonical_bytes(request)
    create_exclusive(output / "request.json", request_bytes.decode())
    request_sha = hashlib.sha256(request_bytes).hexdigest()
    code = bounded_subprocess([sys.executable, "-m", "vkm_corpus.update.pack_policy", "--worker",
        str(output / "request.json"), request_sha], timeout=request.timeout_seconds, memory_gib=request.memory_gib, cwd=output)
    if code != 0:
        raise ValueError("pack policy issuer blocked or failed; no qualification accepted")
    checked_file(output / "request.json", request_sha)
    proof = ordinary(output / "qualification.json").read_bytes()
    receipt = PackPolicyQualification.model_validate_json(proof)
    binding = json.loads(ordinary(output / "binding.json").read_bytes())
    verify_pack_policy_receipt(proof, hashlib.sha256(proof).hexdigest(), binding, receipt.loaded_pack.model_dump(mode="json"))
    return {"binding": BoundFile(path=str(output / "binding.json"), sha256=sha256_of(output / "binding.json")),
            "qualification": BoundFile(path=str(output / "qualification.json"), sha256=hashlib.sha256(proof).hexdigest())}


if __name__ == "__main__":
    if len(sys.argv) != 4 or sys.argv[1] != "--worker": raise SystemExit(2)
    path = checked_file(sys.argv[2], sys.argv[3])
    _qualify(PackPolicyRequest.model_validate_json(path.read_bytes()), path.parent)
