"""Non-destructive EVIDENCE publisher into an isolated shadow database.

The caller supplies actual serving and shadow drivers, plus the coordinator's
exclusive writer/source-policy fence. Native database IDs must differ. This
publisher owns only a previously empty database (or its own interrupted load),
never DOCUMENT wipe/cascade or serving selectors. Round-trip equality is exact;
the result qualifies this projection transfer, not full serving/scientific use.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Callable

from vkm_corpus.graph import client
from vkm_corpus.parquet.atomic import sha256_of
from vkm_evidence.contracts import canonical_bytes, record_hash
from vkm_evidence.projections import FILES, MANIFEST_KEYS, PROJECTION_SCHEMA, _prepared_summary
from vkm_corpus.graph.shadow_bundle import CombinedGraphBundle, SnapshotPin  # noqa: F401 - public preparation API

# The combined publisher is intentionally separate from the legacy in-place
# loaders. It reuses their pure row projectors, never their wipe/sweep paths.

DB_INFO = "CALL db.info() YIELD id, name RETURN id, name"
OWNERS = "MATCH (n) RETURN count(n) AS total, count(CASE WHEN n:VkmEvidenceNode OR n:VkmEvidenceLoad THEN 1 END) AS owned"
META = "MATCH (m:VkmEvidenceLoad) RETURN properties(m) AS props"
START = ("MERGE (m:VkmEvidenceLoad {projection_id:$projection_id}) "
         "ON CREATE SET m.manifest_sha256=$manifest_sha256, m.status='LOADING' RETURN properties(m) AS props")
NODES = ("UNWIND $rows AS row MERGE (n:Evidence:VkmEvidenceNode {node_key:row.node_key}) "
         "ON CREATE SET n = row RETURN properties(n) AS props")
EDGES = ("UNWIND $rows AS row MATCH (a:VkmEvidenceNode {node_key:row.subject}), "
         "(b:VkmEvidenceNode {node_key:row.object}) "
         "MERGE (a)-[r:VKM_EVIDENCE_REL {edge_key:row.props.edge_key}]->(b) "
         "ON CREATE SET r = row.props RETURN properties(r) AS props, a.node_key AS subject, b.node_key AS object")
READ_NODES = "MATCH (n:VkmEvidenceNode) RETURN properties(n) AS props ORDER BY n.node_key"
READ_EDGES = ("MATCH (a)-[r]->(b) RETURN type(r) AS type, properties(r) AS props, "
              "a.node_key AS subject, b.node_key AS object ORDER BY r.edge_key")
FINISH = ("MATCH (m:VkmEvidenceLoad {projection_id:$projection_id, manifest_sha256:$manifest_sha256}) "
          "SET m.status='COMPLETE', m.content_sha256=$content_sha256 RETURN properties(m) AS props")
DDL = "CREATE CONSTRAINT ev_shadow_key IF NOT EXISTS FOR (n:VkmEvidenceNode) REQUIRE n.node_key IS UNIQUE"


def database_identity(driver, database: str) -> dict:
    rows = client.read(driver, database, DB_INFO)
    if len(rows) != 1 or rows[0].get("name") != database or not isinstance(rows[0].get("id"), str) or not rows[0]["id"]:
        raise ValueError("native database identity unavailable")
    info = driver.get_server_info()
    address = str(info.address)
    if not address:
        raise ValueError("actual driver endpoint unavailable")
    return {"database_id": rows[0]["id"], "database": database, "endpoint_sha256": record_hash(address)}


class ShadowEvidencePublisher:
    def __init__(self, driver, database: str, *, serving_driver, serving_database: str,
                 fence: Callable[[], None], batch_size: int = 500, max_records: int = 1_000_000):
        if not 1 <= batch_size <= 5000 or not 1 <= max_records <= 10_000_000:
            raise ValueError("invalid shadow load budget")
        self.driver, self.database = driver, database
        self.serving_driver, self.serving_database = serving_driver, serving_database
        self.fence, self.batch_size, self.max_records = fence, batch_size, max_records
        self.identity = database_identity(driver, database)
        self._fence()

    def _fence(self):
        self.fence()
        native = database_identity(self.driver, self.database)
        serving = database_identity(self.serving_driver, self.serving_database)
        if native != self.identity or native["database_id"] == serving["database_id"]:
            raise ValueError("shadow database identity changed or selects serving database")

    def publish(self, directory: Path, *, manifest_sha256: str) -> dict:
        root = Path(directory).absolute()
        if any(p.is_symlink() for p in (root, *root.parents)):
            raise ValueError("indirect projection root")
        manifest_path = root / "manifest.json"
        raw = manifest_path.read_bytes()
        import hashlib
        if hashlib.sha256(raw).hexdigest() != manifest_sha256:
            raise ValueError("projection manifest bytes changed")
        manifest = json.loads(raw)
        if set(manifest) != MANIFEST_KEYS or manifest["schema"] != PROJECTION_SCHEMA or set(manifest["files"]) != set(FILES):
            raise ValueError("unsupported projection manifest")

        def input_fence():
            if manifest_path.read_bytes() != raw:
                raise ValueError("projection manifest changed during load")
            for name in FILES:
                path = root / name
                if path.is_symlink() or not path.is_file() or sha256_of(path) != manifest["files"][name]:
                    raise ValueError("projection artifact bytes changed")
            self._fence()  # source/access decision is supplied by the trusted coordinator

        input_fence()
        summary = _prepared_summary(root)
        if any(manifest.get(k) != v for k, v in summary.items()) or summary["load_readiness"] != "PINNED_INPUTS_PREPARED":
            raise ValueError("projection has stale references or forged readiness/counts")
        total = sum(summary[k] for k in ("record_count", "occurrence_count", "primary_origin_count", "relation_count"))
        if total > self.max_records:
            raise ValueError("shadow projection exceeds record budget")
        pid = manifest["projection_id"]

        def lines(name):
            with (root / name).open(encoding="utf-8") as stream:
                for line in stream:
                    if line.strip():
                        yield json.loads(line)

        # Prepared JSONL must equal the canonical Parquet projection, not merely
        # have hashes repeated in an untrusted manifest.
        import pyarrow.parquet as pq
        canonical_nodes = ([{"layer": "EVIDENCE", **r} for r in pq.read_table(root / "evidence.parquet").to_pylist()]
            + [{"layer": "ORIGINAL_OCCURRENCE", "node_key": r["occurrence_id"], **r}
               for r in pq.read_table(root / "occurrences.parquet").to_pylist()]
            + [{"layer": "PRIMARY_ORIGIN", "node_key": r["origin_version_id"], **r}
               for r in pq.read_table(root / "origins.parquet").to_pylist()])
        canonical_edges = pq.read_table(root / "relations.parquet").to_pylist()
        if (list(lines("neo4j_nodes.jsonl")) != canonical_nodes or list(lines("neo4j_relations.jsonl")) != canonical_edges):
            raise ValueError("prepared graph rows differ from canonical Parquet")
        nodes = [{"node_key": r["node_key"], "layer": r["layer"], "projection_id": pid,
                  "payload_json": canonical_bytes(r).decode("utf-8")} for r in canonical_nodes]
        edges = [{"subject": r["subject_node_key"], "object": r["object_node_key"], "props": {
            "edge_key": record_hash(r), "projection_id": pid, "predicate": r["predicate"],
            "payload_json": canonical_bytes(r).decode("utf-8")}} for r in canonical_edges]
        keys = {n["node_key"] for n in nodes}
        if len(keys) != len(nodes) or len({e["props"]["edge_key"] for e in edges}) != len(edges):
            raise ValueError("duplicate shadow projection identity")
        if any(e["subject"] not in keys or e["object"] not in keys for e in edges):
            raise ValueError("shadow projection endpoint missing")
        digest = record_hash({"nodes": sorted(nodes, key=lambda r: r["node_key"]),
                              "edges": sorted(edges, key=lambda r: r["props"]["edge_key"])})

        def check_target():
            self._fence()
            owners = client.read(self.driver, self.database, OWNERS)
            meta = client.read(self.driver, self.database, META)
            if len(owners) != 1 or owners[0].get("total") != owners[0].get("owned"):
                raise ValueError("shadow database contains foreign nodes; no wipe is allowed")
            if owners[0].get("total") and not meta:
                raise ValueError("unowned interrupted shadow data")
            if meta and (len(meta) != 1 or meta[0]["props"].get("projection_id") != pid
                         or meta[0]["props"].get("manifest_sha256") != manifest_sha256):
                raise ValueError("shadow database belongs to another projection")
            return meta

        before = check_target()
        complete = before and before[0]["props"].get("status") == "COMPLETE"
        if not complete:
            input_fence()
            client.write(self.driver, self.database, DDL)
            started = client.write(self.driver, self.database, START, projection_id=pid, manifest_sha256=manifest_sha256)
            if len(started) != 1 or started[0]["props"].get("manifest_sha256") != manifest_sha256:
                raise ValueError("shadow load ownership conflict")
            for expected, query in ((nodes, NODES), (edges, EDGES)):
                for offset in range(0, len(expected), self.batch_size):
                    self._fence()
                    batch = expected[offset:offset + self.batch_size]
                    got = client.write(self.driver, self.database, query, rows=batch)
                    want = [{"props": n} for n in batch] if query == NODES else batch
                    if sorted(got, key=canonical_bytes) != sorted(want, key=canonical_bytes):
                        raise ValueError("shadow batch round-trip mismatch; generation remains unpublished")

        self._fence()
        got_nodes = client.read(self.driver, self.database, READ_NODES)
        got_edges = client.read(self.driver, self.database, READ_EDGES)
        want_edges = [{"type": "VKM_EVIDENCE_REL", **e} for e in edges]
        if (sorted(got_nodes, key=canonical_bytes) != sorted(({"props": n} for n in nodes), key=canonical_bytes)
                or sorted(got_edges, key=canonical_bytes) != sorted(want_edges, key=canonical_bytes)):
            raise ValueError("shadow graph content differs; no COMPLETE identity issued")
        check_target()
        input_fence()
        result = client.write(self.driver, self.database, FINISH, projection_id=pid,
                              manifest_sha256=manifest_sha256, content_sha256=digest) if not complete else before
        if (len(result) != 1 or result[0]["props"].get("status") != "COMPLETE"
                or result[0]["props"].get("content_sha256") != digest):
            raise ValueError("shadow completion identity mismatch")
        self._fence()
        return {"schema": "vkm-shadow-evidence-transfer/1", "status": "PASS", "scope": "EVIDENCE_GRAPH_ONLY",
                **self.identity, "projection_id": pid, "manifest_sha256": manifest_sha256,
                "evidence_revision": manifest["evidence_revision"], "policy_sha256": manifest["source_policy_sha256"],
                "context_sha256": manifest["context_sha256"], "content_sha256": digest,
                "nodes": len(nodes), "relationships": len(edges), "serving_switch": "NOT_RUN",
                "scientific_qualification": "NOT_RUN"}
