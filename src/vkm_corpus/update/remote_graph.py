"""Exact combined graph transfer, native observation and drained receiver switch.

No manifest contains executable Cypher. All labels/types and schema definitions
come from the existing DOCUMENT/NAV registries and the narrow EVIDENCE bridge.
No DELETE, DETACH, in-place overwrite, alias/administrative mutation or driver
close occurs here. An operator must protect the qualified standalone database
and the source artifacts from administrative restore; this is not a defence
against a malicious database administrator.
"""
from __future__ import annotations

import re
from collections import defaultdict

from vkm_corpus.graph import schema as S, nav_schema as N
from vkm_corpus.graph.shadow import database_identity
from vkm_corpus.graph.shadow_bundle import CombinedGraphBundle
from vkm_corpus.update.contracts import ComponentIdentity
from vkm_evidence.contracts import canonical_bytes, record_hash

CHECKPOINT = ("SHOW DATABASES YIELD name, databaseID, serverID, address, access, currentStatus, "
              "lastCommittedTxn, lastStartTime, replicationLag WHERE name=$database "
              "RETURN name, databaseID, serverID, address, access, currentStatus, "
              "lastCommittedTxn, lastStartTime, replicationLag")
CONSTRAINTS = "SHOW CONSTRAINTS YIELD name,type,entityType,labelsOrTypes,properties RETURN name,type,entityType,labelsOrTypes,properties"
INDEXES = "SHOW INDEXES YIELD name,state,type,entityType,labelsOrTypes,properties,owningConstraint,indexProvider,options RETURN name,state,type,entityType,labelsOrTypes,properties,owningConstraint,indexProvider,options"
OWNERS = ("MATCH (n) RETURN count(n) AS nodes, count(CASE WHEN n:VkmCombinedNode AND n._vkm_key IS NOT NULL THEN 1 END) AS owned")
EDGE_OWNERS = "MATCH ()-[r]->() RETURN count(r) AS edges, count(r._vkm_key) AS owned"
META = "MATCH (m:VkmCombinedLoad) RETURN labels(m) AS labels, properties(m) AS props"
READ_NODES = ("MATCH (n:VkmCombinedNode) WHERE n._vkm_key > $after RETURN n._vkm_key AS key, "
              "labels(n) AS labels, properties(n) AS props ORDER BY key LIMIT $limit")
READ_EDGES = ("MATCH (a)-[r]->(b) WHERE r._vkm_key > $after RETURN r._vkm_key AS key, "
              "a._vkm_key AS subject, b._vkm_key AS object, type(r) AS type, properties(r) AS props ORDER BY key LIMIT $limit")
FINISH = ("MATCH (m:VkmCombinedLoad {bundle_id:$bundle_id}), (d:ProjectionRun {id:$bundle_id}), "
          "(v:NavMeta {run_id:$bundle_id}) WHERE m.status='LOADING' AND d.status='LOADING' AND v.status='LOADING' "
          "SET m.status='COMPLETE', d.status='COMPLETE', v.status='COMPLETE' RETURN count(m) AS completed")
KEY_DDL = "CREATE CONSTRAINT combined_node_key IF NOT EXISTS FOR (n:VkmCombinedNode) REQUIRE n._vkm_key IS UNIQUE"


class RemoteGraphError(ValueError):
    pass


def _read(driver, db, query, **params):
    from neo4j import Query
    result, _, _ = driver.execute_query(Query(query, timeout=30.0), parameters_=params, database_=db, routing_="r")
    return [dict(r) for r in result]


def _write(driver, database, query, **params):
    from neo4j import Query
    result, _, _ = driver.execute_query(Query(query, timeout=60.0), parameters_=params, database_=database)
    return [dict(r) for r in result]


def native_checkpoint(driver, database):
    """Actual driver, native database/store ID and a standalone transaction fence.

    Cluster routing/replicas are deliberately not qualified by this adapter:
    multiple SHOW rows or missing administrative read privilege fail closed.
    Native db.info id and SHOW databaseID are distinct values, never equated.
    """
    identity = database_identity(driver, database)
    rows = _read(driver, "system", CHECKPOINT, database=database)
    fields = {"name", "databaseID", "serverID", "address", "access", "currentStatus", "lastCommittedTxn", "lastStartTime", "replicationLag"}
    if len(rows) != 1 or set(rows[0]) != fields:
        raise RemoteGraphError("native standalone graph checkpoint unavailable")
    row = rows[0]
    if (row["name"] != database or row["currentStatus"] != "online" or row["access"] not in {"read-only", "read-write"}
            or type(row["lastCommittedTxn"]) is not int or row["lastCommittedTxn"] < 0
            or type(row["replicationLag"]) is not int or row["replicationLag"] != 0
            or any(not row[k] for k in ("databaseID", "serverID", "address", "lastStartTime"))):
        raise RemoteGraphError("native graph is not a qualified available standalone store")
    return {**identity, "native": {k: str(v) if k == "lastStartTime" else v for k, v in row.items()}}


def allowed_labels():
    return ({tuple(sorted(("DocumentLayer", n.label, "VkmCombinedNode"))) for n in S.NODE_TYPES}
        | {tuple(sorted(("NavigationLayer", n.label, "VkmCombinedNode"))) for n in N.NODE_TYPES}
        | {tuple(sorted(("Evidence", "EvidenceLayer", "VkmCombinedNode"))),
           tuple(sorted(("PinnedOriginalRecord", "EvidenceLayer", "VkmCombinedNode"))),
           ("ProjectionRun", "VkmCombinedNode"), ("NavMeta", "VkmCombinedNode"), ("VkmCombinedLoad", "VkmCombinedNode")})


REL_TYPES = frozenset({r.type for r in (*S.REL_TYPES, *N.REL_TYPES)} | {"PINNED_DOCUMENT_RECORD", "VKM_EVIDENCE_REL"})


def node_query(labels):
    labels = tuple(sorted(labels))
    if labels not in allowed_labels():
        raise RemoteGraphError("unregistered combined graph labels")
    return ("UNWIND $rows AS row MERGE (n:" + ":".join(labels) + " {_vkm_key:row.key}) "
            "ON CREATE SET n = row.props RETURN n._vkm_key AS key, labels(n) AS labels, properties(n) AS props")


def edge_query(kind):
    if kind not in REL_TYPES:
        raise RemoteGraphError("unregistered combined graph relationship")
    return ("UNWIND $rows AS row MATCH (a:VkmCombinedNode {_vkm_key:row.subject}), "
            "(b:VkmCombinedNode {_vkm_key:row.object}) MERGE (a)-[r:" + kind + " {_vkm_key:row.key}]->(b) "
            "ON CREATE SET r = row.props RETURN r._vkm_key AS key, a._vkm_key AS subject, "
            "b._vkm_key AS object, type(r) AS type, properties(r) AS props")


def ddl_statements():
    return tuple(dict.fromkeys([KEY_DDL, *(i.statement for i in S.ddl_items()), *(i.statement for i in N.ddl_items()),
        *("CREATE CONSTRAINT combined_rel_" + kind.lower() + " IF NOT EXISTS FOR ()-[r:" + kind + "]-() REQUIRE r._vkm_key IS UNIQUE"
          for kind in sorted(REL_TYPES))]))


def schema_requirements():
    """Derive exact schema expectations from closed, repository-owned DDL."""
    out = {}
    for statement in ddl_statements():
        text = statement.replace("`", "")
        head = re.match(r"CREATE (CONSTRAINT|INDEX) ([A-Za-z0-9_]+) IF NOT EXISTS FOR ", text)
        node = re.search(r"\(n:([A-Za-z0-9]+)\)", text)
        rel = re.search(r"\[r:([A-Z0-9_]+)\]", text)
        if not head or not (node or rel):
            raise RemoteGraphError("unsupported repository graph DDL")
        props = re.findall(r"(?:n|r)\.([A-Za-z0-9_]+)", text)
        out[head[2]] = {"kind": head[1], "entityType": "NODE" if node else "RELATIONSHIP",
                       "labelsOrTypes": [(node or rel)[1]], "properties": props}
    return out


def native_schema(driver, database):
    constraints = _read(driver, database, CONSTRAINTS)
    indexes = _read(driver, database, INDEXES)
    if (len({x.get("name") for x in constraints}) != len(constraints)
            or len({x.get("name") for x in indexes}) != len(indexes)):
        raise RemoteGraphError("ambiguous native graph schema")
    cm, im = {x["name"]: x for x in constraints}, {x["name"]: x for x in indexes}
    for name, expected in schema_requirements().items():
        actual = (cm if expected["kind"] == "CONSTRAINT" else im).get(name)
        if (actual is None or any(actual.get(k) != expected[k] for k in ("entityType", "labelsOrTypes", "properties"))
                or (expected["kind"] == "CONSTRAINT" and not str(actual.get("type", "")).endswith("UNIQUENESS"))
                or (expected["kind"] == "INDEX" and actual.get("type") != "RANGE")):
            raise RemoteGraphError("native graph constraint/index differs from required schema")
        if expected["kind"] == "CONSTRAINT" and not any(x.get("owningConstraint") == name for x in indexes):
            raise RemoteGraphError("native graph constraint backing index unavailable")
    if any(x.get("state") != "ONLINE" for x in indexes):
        raise RemoteGraphError("native graph index is not ONLINE")
    return {"constraints": sorted(constraints, key=canonical_bytes), "indexes": sorted(indexes, key=canonical_bytes)}


def _normal(rows):
    return sorted(({**r, **({"labels": sorted(r["labels"])} if "labels" in r else {})} for r in rows), key=canonical_bytes)


def _loading(row):
    return {**row, "props": {**row["props"], "status": "LOADING"}} if row["key"].startswith("meta:") else row


def _owners(driver, database):
    n, e = _read(driver, database, OWNERS), _read(driver, database, EDGE_OWNERS)
    if len(n) != 1 or len(e) != 1 or n[0].get("nodes") != n[0].get("owned") or e[0].get("edges") != e[0].get("owned"):
        raise RemoteGraphError("foreign graph data present; destructive recovery is forbidden")
    return n[0]["nodes"], e[0]["edges"]


def full_roundtrip(driver, database, bundle, *, loading=False, page_size=500):
    if not 1 <= page_size <= 5000:
        raise RemoteGraphError("invalid graph round-trip budget")
    if _owners(driver, database) != (bundle.manifest["nodes"], bundle.manifest["relationships"]):
        raise RemoteGraphError("combined graph has extra or missing rows")
    for kind, query in (("nodes", READ_NODES), ("edges", READ_EDGES)):
        after = ""
        while True:
            expected = bundle.rows(kind, after, page_size)
            actual = _read(driver, database, query, after=after, limit=page_size)
            if len(actual) > page_size or _normal(actual) != _normal([_loading(r) for r in expected] if loading else expected):
                raise RemoteGraphError("native graph full payload/labels/relationship round-trip differs")
            if not expected:
                break
            after = expected[-1]["key"]


class CombinedShadowPublisher:
    def __init__(self, driver, database, *, serving_driver, serving_database, fence, page_size=500):
        if not 1 <= page_size <= 5000:
            raise ValueError("invalid graph batch budget")
        self.driver, self.database = driver, database
        self.serving_driver, self.serving_database = serving_driver, serving_database
        self.fence, self.page_size = fence, page_size
        self.identity = database_identity(driver, database)
        self._fence()

    def _fence(self):
        self.fence()
        shadow = database_identity(self.driver, self.database)
        serving = database_identity(self.serving_driver, self.serving_database)
        if shadow != self.identity or shadow["database_id"] == serving["database_id"]:
            raise RemoteGraphError("candidate graph changed or is the actual serving database")

    def publish(self, bundle):
        if not isinstance(bundle, CombinedGraphBundle):
            raise TypeError("verified CombinedGraphBundle required")
        self._fence()
        bundle.fence()
        native_checkpoint(self.driver, self.database)  # unsupported proof fails before any write
        count, edges = _owners(self.driver, self.database)
        meta = _read(self.driver, self.database, META)
        wanted = next(r for r in bundle.rows("nodes", "meta:combine", 1) if r["key"] == "meta:combined")
        complete = bool(meta and meta[0]["props"].get("status") == "COMPLETE")
        want_meta = wanted if complete else _loading(wanted)
        if (count and not meta) or len(meta) > 1 or (meta and _normal(meta) != _normal([{k: want_meta[k] for k in ("labels", "props")}])):
            raise RemoteGraphError("candidate graph belongs to another or corrupted generation")
        if not count and edges:
            raise RemoteGraphError("unowned graph relationships")
        if not complete:
            # Ownership first: an interruption during schema creation is resumable.
            if not meta:
                _write(self.driver, self.database, node_query(want_meta["labels"]), rows=[want_meta])
            for ddl in ddl_statements():
                self._fence()
                _write(self.driver, self.database, ddl)
            _read(self.driver, self.database, "CALL db.awaitIndexes($timeout)", timeout=60)
            native_schema(self.driver, self.database)
            for kind in ("nodes", "edges"):
                after = ""
                while batch := bundle.rows(kind, after, self.page_size):
                    self._fence()
                    groups = defaultdict(list)
                    for row in batch:
                        row = _loading(row) if kind == "nodes" else row
                        groups[tuple(row["labels"]) if kind == "nodes" else row["type"]].append(row)
                    for group, expected in groups.items():
                        query = node_query(group) if kind == "nodes" else edge_query(group)
                        actual = _write(self.driver, self.database, query, rows=expected)
                        if _normal(actual) != _normal(expected):
                            raise RemoteGraphError("existing graph row differs; overwrite is forbidden")
                    after = batch[-1]["key"]
            full_roundtrip(self.driver, self.database, bundle, loading=True, page_size=self.page_size)
            bundle.fence()
            self._fence()
            if _write(self.driver, self.database, FINISH, bundle_id=bundle.manifest["bundle_id"]) != [{"completed": 1}]:
                raise RemoteGraphError("combined graph completion conflict")
        self._fence()
        lease = GraphLease.bind(self.driver, self.database, bundle, page_size=self.page_size)
        self._fence()
        return lease


class GraphLease:
    @classmethod
    def bind(cls, driver, database, bundle, *, page_size=500, require_read_only=False):
        bundle.fence()
        before = native_checkpoint(driver, database)
        if require_read_only and before["native"]["access"] != "read-only":
            raise RemoteGraphError("production graph receiver requires actual native read-only access")
        schema = native_schema(driver, database)
        full_roundtrip(driver, database, bundle, page_size=page_size)
        bundle.fence()
        if native_checkpoint(driver, database) != before or native_schema(driver, database) != schema:
            raise RemoteGraphError("graph changed during full native qualification")
        self = cls()
        self.driver, self.database, self.checkpoint, self.schema = driver, database, before, schema
        self.bundle_manifest_sha256 = bundle.manifest_sha256
        self.manifest = json_copy(bundle.manifest)
        self._manifest_digest = record_hash(self.manifest)
        self.identity = {"schema": "vkm-native-combined-graph/1", "checkpoint": before,
                         "scope": "READ_ONLY_RECEIVER" if require_read_only else "SHADOW_TRANSFER_ONLY",
                         "schema_sha256": record_hash(schema), "bundle_manifest_sha256": bundle.manifest_sha256,
                         "content_sha256": self.manifest["content_sha256"]}
        self.sha256 = record_hash(self.identity)
        self._closed = False
        self.observe()
        return self

    def observe(self):
        try:
            if (record_hash(self.identity) != self.sha256 or record_hash(self.manifest) != self._manifest_digest
                    or record_hash(self.schema) != self.identity["schema_sha256"]):
                raise RemoteGraphError("qualified graph in-memory proof changed")
            if self._closed or native_checkpoint(self.driver, self.database) != self.checkpoint:
                raise RemoteGraphError("qualified graph native transaction/store fence changed")
            if native_schema(self.driver, self.database) != self.schema:
                raise RemoteGraphError("qualified graph native schema changed")
            if native_checkpoint(self.driver, self.database) != self.checkpoint:
                raise RemoteGraphError("qualified graph changed during observation")
        except Exception:
            self._closed = True
            raise
        return json_copy(self.identity)

    def component(self):
        self.observe()
        return ComponentIdentity(component="GRAPH", revision=self.manifest["bundle_id"],
            manifest_sha256=self.sha256, policy_sha256=self.manifest["policy_sha256"],
            built_from={k: self.manifest["inputs"][k]["revision"] for k in ("DOCUMENT", "NAV", "EVIDENCE")}).model_dump(mode="json")


def json_copy(value):
    import json
    return json.loads(canonical_bytes(value))


class NativeGraphObserver:
    """Bound to the very backend object/driver that serves this receiver."""
    def __init__(self, deps, bundle, *, page_size=500):
        from vkm_corpus.api.backends import Neo4jBackend
        if not isinstance(deps.graph, Neo4jBackend):
            raise RemoteGraphError("actual Neo4j serving backend required")
        self.deps, self.backend = deps, deps.graph
        self.driver, self.database = self.backend._connect(), self.backend.settings.neo4j_database
        self.lease = GraphLease.bind(self.driver, self.database, bundle, page_size=page_size, require_read_only=True)

    def observe(self):
        if (self.deps.graph is not self.backend or self.backend._connect() is not self.driver
                or self.backend.settings.neo4j_database != self.database):
            raise RemoteGraphError("actual serving graph backend binding changed")
        return {"GRAPH": self.lease.component()}

    def document_origin(self):
        self.observe()
        document = self.lease.manifest["inputs"]["DOCUMENT"]
        return document["revision"], document["manifest_sha256"], self.lease.manifest["policy_sha256"]


class GraphReceiverSwitch:
    """Exact process-local backend handoff under a caller-owned drained barrier.

    The root coordinator owns durable intent, restart reconstruction and global
    generation ordering. This adapter neither changes a remote server selector
    nor guesses recovery from a healthy endpoint. Previously qualified backend
    objects remain alive for exact restore; unqualified legacy targets are blocked.
    """
    def __init__(self, deps, previous, candidate, *, fence):
        from vkm_corpus.api.backends import Neo4jBackend
        if not isinstance(previous, NativeGraphObserver) or not isinstance(candidate, NativeGraphObserver):
            raise TypeError("two actual qualified graph observers required")
        if not all(isinstance(o.backend, Neo4jBackend) for o in (previous, candidate)):
            raise RemoteGraphError("actual graph backends required")
        if previous.backend is candidate.backend:
            raise RemoteGraphError("candidate and previous graph receivers must be distinct")
        self.deps, self.previous, self.candidate, self.fence = deps, previous, candidate, fence

    def _set(self, wanted, other, *, require_other):
        self.fence()
        wanted.lease.observe()
        if require_other:
            other.lease.observe()
        if (wanted.backend._connect() is not wanted.driver or wanted.backend.settings.neo4j_database != wanted.database
                or (require_other and (other.backend._connect() is not other.driver
                    or other.backend.settings.neo4j_database != other.database))):
            raise RemoteGraphError("qualified graph receiver binding changed")
        if self.deps.graph is not wanted.backend and self.deps.graph is not other.backend:
            raise RemoteGraphError("graph receiver is neither pinned previous nor candidate")
        self.fence()
        self.deps.graph = wanted.backend
        wanted.lease.observe()
        self.fence()
        return {"GRAPH": wanted.lease.component()}

    def apply(self): return self._set(self.candidate, self.previous, require_other=True)
    def restore(self): return self._set(self.previous, self.candidate, require_other=False)
