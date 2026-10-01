"""Rebuildable evidence projections, written to a new immutable directory.

Production must use an exact, qualified read-only source/dependency profile and
operator-owned immutable inputs/outputs. Boundary fences detect operational
changes; they do not make publication atomic with arbitrary edits to a mutable
host, protect against root, or establish remote/scientific qualification.
"""
from __future__ import annotations

import json
import stat
from copy import deepcopy
from pathlib import Path

from vkm_corpus.contracts.access import AccessContext
from vkm_corpus.parquet.atomic import create_exclusive, sha256_of, write_bytes, write_with
from vkm_evidence.contracts import canonical_bytes, record_hash
from vkm_evidence.query import EvidenceReader

PROJECTION_SCHEMA = "vkm-evidence-projection/2"
FILES = ("evidence.parquet", "relations.parquet", "occurrences.parquet", "origins.parquet",
         "neo4j_nodes.jsonl", "neo4j_relations.jsonl", "opensearch.jsonl", "evidence.duckdb")
COUNT_FILES = {"record_count": "evidence.parquet", "relation_count": "relations.parquet",
               "occurrence_count": "occurrences.parquet", "primary_origin_count": "origins.parquet"}
MANIFEST_KEYS = {"schema", "projection_id", "evidence_revision", "builder_identity", "source_policy_sha256",
                 "context_sha256", *COUNT_FILES, "missing_referenced_versions", "load_readiness", "remote_load", "files"}


def _node_key(record_id, digest):
    return "evidence:" + record_hash({"record_id": record_id, "record_sha256": digest})


def _artifact_stamps(root: Path, *, manifest=False):
    result = {}
    for name in (*FILES, *(("manifest.json",) if manifest else ())):
        path = root / name
        value = path.stat(follow_symlinks=False)
        if not stat.S_ISREG(value.st_mode):
            raise ValueError("projection artifact is not an ordinary file")
        result[name] = (value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns, value.st_ctime_ns)
    return result


def _prepared_summary(root: Path) -> dict:
    """Counts come from Parquet footers; exact missing pins from prepared edges.

    Read only key/state columns in bounded batches. Source text and numeric
    payloads are neither re-extracted nor interpreted by this integrity check.
    """
    import pyarrow.parquet as pq

    result = {key: pq.read_metadata(root / name).num_rows for key, name in COUNT_FILES.items()}

    def rows(name, columns):
        for batch in pq.ParquetFile(root / name).iter_batches(batch_size=16_384, columns=columns):
            yield from batch.to_pylist()

    record_keys = set()
    for row in rows("evidence.parquet", ["node_key", "record_id", "record_sha256"]):
        key = row["node_key"]
        if key != _node_key(row["record_id"], row["record_sha256"]) or key in record_keys:
            raise ValueError("projection manifest has invalid or duplicate record identities")
        record_keys.add(key)
    occurrence_keys = {r["occurrence_id"] for r in rows("occurrences.parquet", ["occurrence_id"])}
    origin_state = {r["origin_version_id"]: r["verified"] for r in rows("origins.parquet", ["origin_version_id", "verified"])}
    if len(occurrence_keys) != result["occurrence_count"] or len(origin_state) != result["primary_origin_count"]:
        raise ValueError("projection manifest has duplicate occurrence/origin identities")
    missing = set()
    columns = ["subject", "subject_sha256", "subject_node_key", "object", "object_sha256",
               "object_node_key", "object_kind", "pin_state"]
    for row in rows("relations.parquet", columns):
        subject, target = row["subject_node_key"], row["object_node_key"]
        if subject != _node_key(row["subject"], row["subject_sha256"]):
            raise ValueError("projection manifest has invalid relation subject identity")
        if row["object_kind"] == "RECORD":
            if target != _node_key(row["object"], row["object_sha256"]):
                raise ValueError("projection manifest has invalid relation target identity")
            absent = {key for key in (subject, target) if key not in record_keys}
            if row["pin_state"] != ("STALE" if absent else "CURRENT"):
                raise ValueError("projection manifest disagrees with actual relation pin state")
            missing.update(absent)
        elif row["object_kind"] == "ORIGINAL_OCCURRENCE":
            if (subject not in record_keys or target != row["object"] or target not in occurrence_keys
                    or row["pin_state"] != "PINNED_ORIGINAL"):
                raise ValueError("projection manifest has unresolved original occurrence")
        elif row["object_kind"] == "PRIMARY_ORIGIN":
            if (subject not in record_keys or target != row["object"] or target not in origin_state
                    or row["pin_state"] != ("VERIFIED" if origin_state[target] else "UNKNOWN")):
                raise ValueError("projection manifest has unresolved primary origin")
        else:
            raise ValueError("projection manifest has an unsupported relation endpoint")
    return {**result, "missing_referenced_versions": sorted(missing),
            "load_readiness": "BLOCKED_MISSING_REFERENCED_VERSIONS" if missing else "PINNED_INPUTS_PREPARED",
            "remote_load": "NOT_RUN"}


def projection_builder_identity(producer_identity=None):
    """All local mapping/rule bytes and relevant dependency versions key reuse.

    A production caller additionally pins its already qualified producer identity.
    Source paths are logical; no machine path enters the published manifest.
    """
    from importlib.metadata import version
    import vkm_corpus.contracts.access as access
    import vkm_world.core.provenance as provenance
    import vkm_evidence.contracts as contracts
    import vkm_evidence.query as query
    import vkm_evidence.validation as validation
    import vkm_evidence.temporal as temporal
    modules = (access, provenance, contracts, query, validation, temporal)
    code = {m.__name__: sha256_of(Path(m.__file__)) for m in modules}
    code[__name__] = sha256_of(Path(__file__))
    return {"contract": "vkm-evidence-projection-rules/1", "code": code,
            "dependencies": {name: version(name) for name in ("duckdb", "pyarrow", "pydantic")},
            "producer_identity": producer_identity}


def build_projection(reader: EvidenceReader, output: Path, context: AccessContext, *, producer_identity=None) -> dict:
    output = Path(output).resolve()
    output.mkdir(parents=True, exist_ok=True)
    lock = output / "publisher.lock"
    import uuid
    token = uuid.uuid4().hex
    if lock.is_symlink() or not create_exclusive(lock, token):
        raise ValueError("projection publisher busy; recovery required for stale lock")
    try:
        pinned_producer = deepcopy(producer_identity)
        builder = deepcopy(projection_builder_identity(pinned_producer))
        return _build_projection(reader, output, context, builder, pinned_producer)
    finally:
        if lock.read_text(encoding="ascii") != token:
            raise ValueError("projection publisher lock owner changed")
        lock.unlink()


def _build_projection(reader: EvidenceReader, output: Path, context: AccessContext, builder: dict,
                      pinned_producer=None) -> dict:
    import duckdb
    import pyarrow as pa
    import pyarrow.parquet as pq

    revision, records = reader.view()
    policy_sha256 = reader.policy_fingerprint(records, revision)
    visible = reader._visible(records, context, revision)
    projection_id = record_hash({"schema": PROJECTION_SCHEMA, "evidence": revision,
                                 "context": context.model_dump(mode="json"), "builder": builder,
                                 "source_policy_sha256": policy_sha256})
    root = Path(output) / projection_id
    if root.is_symlink() or not root.resolve().is_relative_to(Path(output).resolve()):
        raise ValueError("projection path escapes root")
    manifest = root / "manifest.json"

    def final_fence(stamps, *, cached=False):
        # The exact initial qualified producer identity remains the input to the
        # second observation; never silently adopt a new identity at the end.
        if projection_builder_identity(deepcopy(pinned_producer)) != builder:
            raise ValueError("projection builder changed during projection")
        if reader.policy_fingerprint(records, revision) != policy_sha256 or reader.journal.revision != revision:
            raise ValueError("evidence or source policy changed during projection")
        if _artifact_stamps(root, manifest=cached) != stamps:
            raise ValueError("projection artifacts changed during verification")

    if manifest.exists():
        stamps = _artifact_stamps(root, manifest=True)
        result = json.loads(manifest.read_bytes())
        if (not isinstance(result, dict) or set(result) != MANIFEST_KEYS
                or result.get("schema") != PROJECTION_SCHEMA or result.get("projection_id") != projection_id
                or result.get("evidence_revision") != revision or result.get("context_sha256") != record_hash(context)
                or result.get("builder_identity") != builder or result.get("source_policy_sha256") != policy_sha256
                or not isinstance(result.get("files"), dict) or set(result["files"]) != set(FILES)
                or any(type(result.get(name)) is not int or result[name] < 0 for name in COUNT_FILES)):
            raise ValueError("existing projection manifest identity mismatch")
        hashes = {name: sha256_of(root / name) for name in FILES}
        if hashes != result["files"]:
            raise ValueError("existing projection has changed bytes")
        summary = _prepared_summary(root)
        if any(result[name] != value for name, value in summary.items()):
            raise ValueError("existing projection manifest summary differs from prepared files")
        final_fence(stamps, cached=True)
        return result
    root.mkdir(parents=True, exist_ok=True)
    from vkm_evidence.contracts import ReviewDecision, ScientificUseAdmission
    rows, edges, occurrences, origins = [], [], {}, {}

    def edge(record, predicate, ref):
        current = visible.get(ref.record_id)
        edges.append({"subject": record.record_id, "subject_sha256": record_hash(record), "predicate": predicate,
            "object": ref.record_id, "object_sha256": ref.record_sha256, "object_kind": "RECORD",
            "pin_state": "CURRENT" if current is not None and current.version_ref == ref else "STALE",
            "asserted_by": record.record_id})

    for rid, r in sorted(visible.items()):
        from vkm_evidence.temporal import unique_date, available_latest, temporal_conflicts
        rows.append({"record_id": rid, "record_sha256": record_hash(r), "kind": r.kind,
                     "revision": r.revision, "recorded_at": r.recorded_at.isoformat(),
                     "event_date": str(unique_date(r, "event_date")) if unique_date(r, "event_date") else None,
                     "measurement_date": str(unique_date(r, "measurement_date")) if unique_date(r, "measurement_date") else None,
                     "publication_date": str(unique_date(r, "publication_date")) if unique_date(r, "publication_date") else None,
                     "available_latest": str(available_latest(r)) if available_latest(r) else None,
                     "temporal_state": "CONFLICT" if temporal_conflicts(r) else "STATED" if available_latest(r) else "UNKNOWN",
                     "access_class": r.policy.access_class.value, "experimental_role": r.policy.experimental_role.value,
                     "policy_version": r.policy.policy_version, "payload_json": canonical_bytes(r).decode("utf-8")})
        for ref in r.depends_on:
            edge(r, "DEPENDS_ON", ref)
        for ref in r.references:
            edge(r, "REFERENCES", ref)
        if isinstance(r, ReviewDecision):
            edge(r, "REVIEWS", r.target)
        if isinstance(r, ScientificUseAdmission):
            for predicate, refs in (("ADMITS", r.targets), ("ADMISSION_DEPENDENCY", r.dependency_versions),
                                     ("ADMISSION_REVIEW", r.review_versions)):
                for ref in refs:
                    edge(r, predicate, ref)
        if r.kind == "EVIDENCE_RELATION":
            ref = next(d for d in (*r.depends_on, *r.references) if d.record_id == r.object)
            subject = next(d for d in (*r.depends_on, *r.references) if d.record_id == r.subject)
            edges.append({"subject": r.subject, "predicate": r.predicate, "object": r.object,
                "subject_sha256": subject.record_sha256, "object_sha256": ref.record_sha256,
                "object_kind": "RECORD", "pin_state": "CURRENT" if visible[r.object].version_ref == ref
                    and visible[r.subject].version_ref == subject else "STALE", "asserted_by": rid})
        supports = [("SUPPORTED_BY", support) for support in r.supports]
        supports.extend(("SYMBOL_DEFINED_BY", support) for binding in getattr(r, "symbols", ()) for support in binding.supports)
        for predicate, support in supports:
            oid = "occurrence:" + record_hash(support)
            occurrences[oid] = {"occurrence_id": oid, "source_id": support.source_id,
                "source_sha256": support.source_sha256, "snapshot_id": support.snapshot_id,
                "object_id": support.object_id, "object_version": support.object_version,
                "content_sha256": support.content_sha256, "extraction_generation": support.extraction_generation,
                "locator": support.locator, "payload_json": canonical_bytes(support).decode("utf-8")}
            edges.append({"subject": rid, "subject_sha256": record_hash(r), "predicate": predicate, "object": oid,
                "object_sha256": record_hash(support), "object_kind": "ORIGINAL_OCCURRENCE",
                "pin_state": "PINNED_ORIGINAL", "asserted_by": rid})
        for origin in getattr(r, "origins", ()):
            # A primary set identifier and a publication are different objects.
            # Exact subsets and verification are retained; no independence is inferred.
            oid = "origin:" + record_hash(origin)
            origins[oid] = {"origin_version_id": oid, "origin_id": origin.origin_id,
                "verified": origin.verified, "payload_json": canonical_bytes(origin).decode("utf-8")}
            edges.append({"subject": rid, "subject_sha256": record_hash(r), "predicate": "USES_PRIMARY_ORIGIN",
                "object": oid, "object_sha256": record_hash(origin), "object_kind": "PRIMARY_ORIGIN",
                "pin_state": "VERIFIED" if origin.verified else "UNKNOWN", "asserted_by": rid})
    for item in rows:
        item["node_key"] = "evidence:" + record_hash({"record_id": item["record_id"], "record_sha256": item["record_sha256"]})
    for item in edges:
        item["subject_node_key"] = "evidence:" + record_hash({"record_id": item["subject"], "record_sha256": item["subject_sha256"]})
        item["object_node_key"] = ("evidence:" + record_hash({"record_id": item["object"], "record_sha256": item["object_sha256"]})
            if item["object_kind"] == "RECORD" else item["object"])
    schema = pa.schema([(k, pa.string()) for k in ("node_key", "record_id", "record_sha256", "kind", "recorded_at", "event_date",
                        "measurement_date", "publication_date", "available_latest", "access_class", "experimental_role",
                        "policy_version", "payload_json", "temporal_state")] + [("revision", pa.int64())])
    edge_schema = pa.schema([(k, pa.string()) for k in ("subject", "subject_sha256", "predicate", "object",
        "object_sha256", "object_kind", "pin_state", "asserted_by", "subject_node_key", "object_node_key")])
    occurrence_schema = pa.schema([(k, pa.string()) for k in ("occurrence_id", "source_id", "source_sha256",
        "snapshot_id", "object_id", "object_version", "content_sha256", "extraction_generation", "locator", "payload_json")])
    origin_schema = pa.schema([(k, pa.string()) for k in ("origin_version_id", "origin_id", "payload_json")]
                             + [("verified", pa.bool_())])
    write_with(root / "tmp", root / "evidence.parquet", lambda p: pq.write_table(pa.Table.from_pylist(rows, schema=schema), p))
    write_with(root / "tmp", root / "relations.parquet", lambda p: pq.write_table(pa.Table.from_pylist(edges, schema=edge_schema), p))
    occurrence_rows = [occurrences[k] for k in sorted(occurrences)]
    origin_rows = [origins[k] for k in sorted(origins)]
    write_with(root / "tmp", root / "occurrences.parquet", lambda p: pq.write_table(pa.Table.from_pylist(occurrence_rows, schema=occurrence_schema), p))
    write_with(root / "tmp", root / "origins.parquet", lambda p: pq.write_table(pa.Table.from_pylist(origin_rows, schema=origin_schema), p))
    # Neo4j and OpenSearch use the same prepared rows. Loading these files is a
    # deployment stage; DOCUMENT cascade is not used to replace EVIDENCE.
    node_rows = ([{"layer": "EVIDENCE", **row} for row in rows]
        + [{"layer": "ORIGINAL_OCCURRENCE", "node_key": row["occurrence_id"], **row} for row in occurrence_rows]
        + [{"layer": "PRIMARY_ORIGIN", "node_key": row["origin_version_id"], **row} for row in origin_rows])
    write_bytes(root / "tmp", root / "neo4j_nodes.jsonl", b"".join(canonical_bytes(row) + b"\n" for row in node_rows))
    write_bytes(root / "tmp", root / "neo4j_relations.jsonl", b"".join(canonical_bytes(row) + b"\n" for row in edges))
    write_bytes(root / "tmp", root / "opensearch.jsonl", b"".join(canonical_bytes(
        {"id": row["record_id"], "generation": revision, **row}) + b"\n" for row in rows))
    def database(path):
        con = duckdb.connect(str(path))
        try:
            con.execute("CREATE TABLE evidence AS SELECT * FROM read_parquet(?)", [str(root / "evidence.parquet")])
            con.execute("CREATE TABLE relations AS SELECT * FROM read_parquet(?)", [str(root / "relations.parquet")])
            con.execute("CREATE TABLE occurrences AS SELECT * FROM read_parquet(?)", [str(root / "occurrences.parquet")])
            con.execute("CREATE TABLE origins AS SELECT * FROM read_parquet(?)", [str(root / "origins.parquet")])
            con.execute("CREATE TABLE generation AS SELECT ? AS evidence_revision, ? AS projection_id", [revision, projection_id])
            con.execute("CREATE INDEX evidence_id ON evidence(record_id)")
            con.execute("CHECKPOINT")
        finally:
            con.close()
    db = root / "evidence.duckdb"
    if db.exists():
        if db.is_symlink():
            raise ValueError("unpublished projection database is indirect")
        # DuckDB physical bytes are not deterministic across rebuilds. After a
        # crash before manifest, reuse only a database proven equivalent to the
        # freshly verified immutable Parquet inputs and generation metadata.
        con = duckdb.connect(str(db), read_only=True, config={"autoload_known_extensions": "false",
            "autoinstall_known_extensions": "false", "allow_unsigned_extensions": "false"})
        try:
            tables = con.execute("SELECT table_name, table_type FROM information_schema.tables WHERE table_schema='main'").fetchall()
            if set(tables) != {(name, "BASE TABLE") for name in ("evidence", "relations", "occurrences", "origins", "generation")}:
                raise ValueError("unpublished database has unexpected tables/views")
            if con.execute("SELECT evidence_revision, projection_id FROM generation").fetchall() != [(revision, projection_id)]:
                raise ValueError("unpublished database belongs to another generation")
            for name in ("evidence", "relations", "occurrences", "origins"):
                sql = (f"SELECT count(*) FROM ((SELECT * FROM {name} EXCEPT ALL SELECT * FROM read_parquet(?)) "
                       f"UNION ALL (SELECT * FROM read_parquet(?) EXCEPT ALL SELECT * FROM {name}))")
                path = str(root / (name + ".parquet"))
                if con.execute(sql, [path, path]).fetchone()[0]:
                    raise ValueError("unpublished database differs from canonical projection")
        finally:
            con.close()
    else:
        write_with(root / "tmp", db, database)
    stamps = _artifact_stamps(root)
    hashes = {name: sha256_of(root / name) for name in FILES}
    summary = _prepared_summary(root)
    result = {"schema": PROJECTION_SCHEMA, "projection_id": projection_id, "evidence_revision": revision,
              "builder_identity": builder, "source_policy_sha256": policy_sha256,
              "context_sha256": record_hash(context), **summary, "files": hashes}
    data = canonical_bytes(result)
    final_fence(stamps)
    write_bytes(root / "tmp", manifest, data)
    return result
