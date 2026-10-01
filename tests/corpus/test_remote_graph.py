"""Actual synthetic canonical/NAV/EVIDENCE files; adversarial fake Neo4j only."""
from __future__ import annotations

import copy
import json
import sqlite3
from contextlib import closing
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
import pyarrow.parquet as pq

from vkm_corpus.api.backends import Neo4jBackend
from vkm_corpus.contracts.access import AccessContext, ResourcePolicy
from vkm_corpus.contracts.policy_store import SourcePolicyStore
from vkm_corpus.graph import shadow
from vkm_corpus.graph.shadow_bundle import CombinedGraphBundle, SnapshotPin
from vkm_corpus.graph.canon import ProjectionInput
from vkm_corpus.graph.common import load_snapshot
from vkm_corpus.graph.synthetic import write_synthetic_canonical_root, synthetic_canonical_rows
from vkm_corpus.navigation import sections
from vkm_corpus.parquet.atomic import sha256_of
from vkm_corpus.update import remote_graph as g
from vkm_evidence.contracts import Entity, EvidenceBatch, ObjectRef, canonical_bytes, record_hash
from vkm_evidence.journal import EvidenceJournal
from vkm_evidence.objects import canonical_resolver, canonical_locator
from vkm_evidence.projections import build_projection
from vkm_evidence.query import EvidenceReader

CTX = AccessContext(principal="synthetic", execution="CLOUD")
POLICY = ResourcePolicy(access_class="PUBLIC", policy_version="synthetic-1", authority="synthetic")


class Driver:
    def __init__(self, native_id="shadow"):
        self.native_id = native_id
        self.nodes, self.edges, self.schema, self.writes = {}, {}, set(), []
        self.txn = 1
        self.start = "2026-10-01T00:00:00Z"
        self.access = "read-write"
        self.interrupt = None
        self.on_read = None
        self.bad_schema = False
        self.index_state = "ONLINE"
        self.checkpoint_unavailable = False
        self.foreign = 0

    def get_server_info(self):
        return SimpleNamespace(address=(self.native_id, 7687))

    def execute_query(self, query, *, parameters_, database_, **kwargs):
        query, p = str(query), parameters_
        if query == shadow.DB_INFO:
            result = [{"id": self.native_id, "name": database_}]
        elif query == g.CHECKPOINT:
            assert database_ == "system"
            result = [] if self.checkpoint_unavailable else [{"name": p["database"], "databaseID": "store-" + self.native_id,
                "serverID": "server-" + self.native_id, "address": self.native_id + ":7687", "access": self.access,
                "currentStatus": "online", "lastCommittedTxn": self.txn, "lastStartTime": self.start, "replicationLag": 0}]
        elif query in (g.CONSTRAINTS, g.INDEXES):
            result = []
            for name, e in g.schema_requirements().items():
                if name not in self.schema:
                    continue
                base = {"name": name, **{k: e[k] for k in ("entityType", "labelsOrTypes", "properties")}}
                if self.bad_schema and name == "combined_node_key":
                    base["properties"] = ["wrong"]
                if query == g.CONSTRAINTS and e["kind"] == "CONSTRAINT":
                    result.append({**base, "type": "UNIQUENESS"})
                if query == g.INDEXES:
                    result.append({**base, "state": self.index_state, "type": "RANGE", "indexProvider": "range-1.0",
                        "options": {}, "owningConstraint": name if e["kind"] == "CONSTRAINT" else None})
        elif query == g.OWNERS:
            result = [{"nodes": len(self.nodes) + self.foreign, "owned": len(self.nodes)}]
        elif query == g.EDGE_OWNERS:
            result = [{"edges": len(self.edges), "owned": sum(r.get("key") is not None for r in self.edges.values())}]
        elif query == g.META:
            result = [{k: r[k] for k in ("labels", "props")} for r in self.nodes.values() if "VkmCombinedLoad" in r["labels"]]
        elif query in (g.READ_NODES, g.READ_EDGES):
            store = self.nodes if query == g.READ_NODES else self.edges
            result = [copy.deepcopy(store[k]) for k in sorted(store) if k > p["after"]][:p["limit"]]
            if self.on_read:
                self.on_read(self, query)
        elif query == "CALL db.awaitIndexes($timeout)":
            result = []
        else:
            self.writes.append(query)
            if self.interrupt and self.interrupt(query):
                self.interrupt = None
                raise RuntimeError("synthetic interrupted transaction")
            self.txn += 1
            if query in g.ddl_statements():
                name = query.replace("`", "").split()[2]
                self.schema.add(name)
                result = []
            elif query == g.FINISH:
                meta = [self.nodes[k] for k in ("meta:combined", "meta:document", "meta:nav")]
                if all(r["props"]["status"] == "LOADING" and r["props"]["bundle_id"] == p["bundle_id"] for r in meta):
                    for r in meta: r["props"]["status"] = "COMPLETE"
                    result = [{"completed": 1}]
                else: result = [{"completed": 0}]
            elif query.startswith("UNWIND $rows AS row MERGE (n:"):
                assert query == g.node_query(p["rows"][0]["labels"])
                result = []
                for r in p["rows"]:
                    self.nodes.setdefault(r["key"], copy.deepcopy(r))
                    result.append(self.nodes[r["key"]])
            elif query.startswith("UNWIND $rows AS row MATCH (a:"):
                assert query == g.edge_query(p["rows"][0]["type"])
                result = []
                for r in p["rows"]:
                    if r["subject"] in self.nodes and r["object"] in self.nodes:
                        self.edges.setdefault(r["key"], copy.deepcopy(r))
                        result.append(self.edges[r["key"]])
            else:
                raise AssertionError(query)
        return copy.deepcopy(result), None, None


def assets(root, *, historical=False):
    rows = synthetic_canonical_rows()
    rows["blocks"] = [r.model_copy(update={"extraction_signature": "a" * 64, "extraction_generation": 1}) for r in rows["blocks"]]
    canonical = write_synthetic_canonical_root(root / "canonical", rows=rows)
    snap = load_snapshot(canonical)
    navdir = root / "nav"
    navdir.mkdir()
    with closing(ProjectionInput.from_snapshot(snap)) as inp:
        tables = sections.build(inp.con)
        source_ids = [r["source_id"] for r in inp.fetch("SELECT source_id FROM sources")]
    navm = {"format": "vkm-nav-manifest-v1", "snapshot": snap.info.as_dict(), "datasets": {}}
    for name, table in tables.items():
        path = navdir / (name + ".parquet")
        pq.write_table(table, path)
        navm["datasets"][name] = {"path": path.name, "sha256": sha256_of(path), "rows": table.num_rows, "columns": table.schema.names}
    (navdir / "manifest.json").write_bytes(canonical_bytes(navm))
    policy_path = root / "policy.json"
    policy_path.write_bytes(canonical_bytes({"schema": "vkm-source-policy/1", "policies": {s: POLICY.model_dump(mode="json") for s in source_ids}}))
    store = SourcePolicyStore(policy_path, lambda: source_ids)
    original = snap
    oldpins = ()
    if historical:
        oldroot = write_synthetic_canonical_root(root / "historical", rows=rows, snapshot_id="snap-historical")
        original = load_snapshot(oldroot)
        oldpins = (SnapshotPin(oldroot, original.snapshot_id, original.manifest_sha256),)
    with closing(ProjectionInput.from_snapshot(original)) as inp:
        row = inp.fetch("SELECT * FROM blocks ORDER BY object_id LIMIT 1")[0]
    ref = ObjectRef(source_id=row["source_id"], source_sha256=row["source_sha256"], snapshot_id=original.snapshot_id,
        object_id=row["object_id"], object_version=row["extraction_signature"], content_sha256=row["content_sha256"],
        extraction_generation=str(row["extraction_generation"]), locator=canonical_locator(row))
    from vkm_evidence.objects import ObjectCatalogue, OriginalObject
    cat = ObjectCatalogue((OriginalObject(ref, POLICY, row["text"]),))
    journal = EvidenceJournal(root / "evidence", object_validator=cat.validate)
    entity = Entity(record_id="synthetic-mine", actor=CTX.principal, recorded_at=datetime(2026, 10, 1, tzinfo=timezone.utc),
                    policy=POLICY, supports=(ref,), entity_type="MINE", label="Synthetic mine")
    journal.publish("first", journal.revision, EvidenceBatch(records=(entity,)), CTX)
    result = build_projection(EvidenceReader(journal), root / "projection", CTX)
    evdir = root / "projection" / result["projection_id"]
    return dict(canonical_root=canonical, snapshot_id=snap.snapshot_id, canonical_manifest_sha256=snap.manifest_sha256,
                nav_dir=navdir, nav_manifest_sha256=sha256_of(navdir / "manifest.json"), evidence_dir=evdir,
                evidence_manifest_sha256=sha256_of(evdir / "manifest.json"), policy_store=store, context=CTX,
                historical_snapshots=oldpins)


@pytest.fixture(scope="module")
def prepared(tmp_path_factory):
    root = tmp_path_factory.mktemp("combined-graph")
    args = assets(root)
    return CombinedGraphBundle.prepare(root / "bundle", **args)


def publisher(driver=None, **kwargs):
    return g.CombinedShadowPublisher(driver or Driver(), "neo4j", serving_driver=Driver("serving"),
        serving_database="neo4j", fence=kwargs.pop("fence", lambda: None), page_size=3, **kwargs)


def test_combined_publisher_full_payload_resume_and_reader_compatible_labels(prepared):
    driver = Driver()
    lease = publisher(driver).publish(prepared)
    assert lease.component()["built_from"] == {k: prepared.manifest["inputs"][k]["revision"] for k in ("DOCUMENT", "NAV", "EVIDENCE")}
    labels = {label for row in driver.nodes.values() for label in row["labels"]}
    assert {"Source", "Page", "Block", "Table", "Formula", "NavSection", "Evidence", "PinnedOriginalRecord", "ProjectionRun", "NavMeta"} <= labels
    assert driver.nodes["meta:document"]["props"]["status"] == driver.nodes["meta:nav"]["props"]["status"] == "COMPLETE"
    assert any(e["type"] == "PINNED_DOCUMENT_RECORD" for e in driver.edges.values())
    previous = list(driver.writes)
    assert publisher(driver).publish(prepared).identity == lease.identity
    assert driver.writes == previous
    assert not any("DELETE" in q or "SET n =" in q and "ON CREATE SET n =" not in q for q in driver.writes)


@pytest.mark.parametrize("point", ["ddl", "node", "edge", "finish"])
def test_interrupted_load_resumes_without_wipe_or_duplicate(prepared, point):
    driver = Driver()
    pred = {"ddl": lambda q: q.startswith("CREATE"), "node": lambda q: q == g.node_query(["DocumentLayer", "Source", "VkmCombinedNode"]),
            "edge": lambda q: q.startswith("UNWIND $rows AS row MATCH"), "finish": lambda q: q == g.FINISH}[point]
    driver.interrupt = pred
    with pytest.raises(RuntimeError, match="interrupted"):
        publisher(driver).publish(prepared)
    assert driver.nodes["meta:combined"]["props"]["status"] == "LOADING"
    publisher(driver).publish(prepared).observe()
    assert len(driver.nodes) == prepared.manifest["nodes"]
    assert len(driver.edges) == prepared.manifest["relationships"]


@pytest.mark.parametrize("fault", ["property", "label", "edge", "extra_node", "extra_edge", "schema"])
def test_full_observer_does_not_trust_complete_marker_or_expected_json(prepared, fault):
    driver = Driver()
    publisher(driver).publish(prepared)
    if fault == "property": next(r for r in driver.nodes.values() if "Block" in r["labels"])["props"]["text_sha256"] = "0" * 64
    if fault == "label": next(r for r in driver.nodes.values() if "Block" in r["labels"])["labels"].append("Claim")
    if fault == "edge": next(iter(driver.edges.values()))["object"] = "wrong"
    if fault == "extra_node": driver.foreign = 1
    if fault == "extra_edge": driver.edges["foreign"] = {"key": None}
    if fault == "schema": driver.bad_schema = True
    before = list(driver.writes)
    with pytest.raises(ValueError): g.GraphLease.bind(driver, "neo4j", prepared, page_size=7)
    assert driver.writes == before


@pytest.mark.parametrize("fault", ["transaction", "store", "restart", "access", "index"])
def test_native_lease_closes_after_change_even_if_payload_is_restored(prepared, fault):
    driver = Driver()
    lease = publisher(driver).publish(prepared)
    if fault == "transaction": driver.txn += 2
    if fault == "store": driver.native_id = "replaced"
    if fault == "restart": driver.start = "2026-10-02T00:00:00Z"
    if fault == "access": driver.access = "read-only"
    if fault == "index": driver.index_state = "FAILED"
    with pytest.raises(ValueError): lease.observe()
    driver.native_id, driver.start, driver.access, driver.index_state = "shadow", "2026-10-01T00:00:00Z", "read-write", "ONLINE"
    driver.txn = lease.checkpoint["native"]["lastCommittedTxn"]
    with pytest.raises(ValueError): lease.observe()  # no self-healing a revoked proof


def test_transaction_during_payload_scan_never_qualifies(prepared):
    driver = Driver()
    publisher(driver).publish(prepared)
    driver.on_read = lambda d, q: setattr(d, "txn", d.txn + 1)
    with pytest.raises(ValueError, match="changed during"):
        g.GraphLease.bind(driver, "neo4j", prepared)


def test_missing_native_checkpoint_cannot_return_ready(prepared):
    driver = Driver()
    publisher(driver).publish(prepared)
    driver.checkpoint_unavailable = True
    with pytest.raises(ValueError, match="checkpoint unavailable"):
        g.GraphLease.bind(driver, "neo4j", prepared)


@pytest.mark.parametrize("fault", ["serving", "foreign", "policy_fence"])
def test_publisher_refuses_before_first_write(prepared, fault):
    driver = Driver("serving" if fault == "serving" else "shadow")
    if fault == "foreign": driver.foreign = 1
    def fence():
        if fault == "policy_fence": raise PermissionError("policy revoked")
    with pytest.raises((ValueError, PermissionError)):
        publisher(driver, fence=fence).publish(prepared)
    assert driver.writes == []


def test_exact_historical_original_payload_never_retargets_current_id(tmp_path):
    args = assets(tmp_path, historical=True)
    with pytest.raises(ValueError, match="historical original snapshot"):
        CombinedGraphBundle.prepare(tmp_path / "missing", **{**args, "historical_snapshots": ()})
    bundle = CombinedGraphBundle.prepare(tmp_path / "accepted", **args)
    driver = Driver()
    publisher(driver).publish(bundle)
    originals = [r["props"] for r in driver.nodes.values() if "PinnedOriginalRecord" in r["labels"]]
    assert len(originals) == 1 and originals[0]["snapshot_id"] == "snap-historical"
    assert json.loads(originals[0]["reference_json"])["snapshot_id"] == "snap-historical"
    assert json.loads(originals[0]["payload_json"])["object_id"] == originals[0]["object_id"]
    assert any(r["props"].get("id") == originals[0]["object_id"] and "Block" in r["labels"] for r in driver.nodes.values())
    edge = next(r for r in driver.edges.values() if r["type"] == "PINNED_DOCUMENT_RECORD")
    assert edge["object"].startswith("PinnedOriginalRecord:")


@pytest.mark.parametrize("fault", ["nav_snapshot", "canonical_hash", "policy", "budget"])
def test_real_input_bytes_policy_and_budget_are_gates(tmp_path, fault):
    args = assets(tmp_path)
    if fault == "nav_snapshot":
        p = args["nav_dir"] / "manifest.json"
        m = json.loads(p.read_bytes()); m["snapshot"]["manifest_sha256"] = "0" * 64
        p.write_bytes(canonical_bytes(m)); args["nav_manifest_sha256"] = sha256_of(p)
    if fault == "canonical_hash": args["canonical_manifest_sha256"] = "0" * 64
    if fault == "policy":
        p = args["policy_store"].path
        p.write_bytes(canonical_bytes({"schema": "vkm-source-policy/1", "policies": {}}))
    if fault == "budget": args["max_records"] = 2
    with pytest.raises((ValueError, PermissionError)):
        CombinedGraphBundle.prepare(tmp_path / "rejected", **args)
    assert not (tmp_path / "rejected" / "manifest.json").exists()


def backend(driver):
    value = Neo4jBackend(SimpleNamespace(neo4j_database="neo4j"))
    value._driver = driver
    return value


def test_actual_backend_observer_and_exact_switch_restore(prepared):
    old, new = Driver("old"), Driver("new")
    publisher(old).publish(prepared)
    publisher(new).publish(prepared)
    old.access = new.access = "read-only"
    deps = SimpleNamespace(graph=backend(old))
    candidate_deps = SimpleNamespace(graph=backend(new))
    previous = g.NativeGraphObserver(deps, prepared)
    candidate = g.NativeGraphObserver(candidate_deps, prepared)
    assert previous.document_origin() == (prepared.manifest["inputs"]["DOCUMENT"]["revision"],
        prepared.manifest["inputs"]["DOCUMENT"]["manifest_sha256"], prepared.manifest["policy_sha256"])
    calls = []
    switch = g.GraphReceiverSwitch(deps, previous, candidate, fence=lambda: calls.append("drained"))
    switch.apply(); switch.apply()
    assert deps.graph is candidate.backend
    with pytest.raises(ValueError, match="binding changed"): previous.observe()
    new.txn += 1  # failed candidate must not prevent restoring still-qualified previous
    switch.restore(); switch.restore()
    assert deps.graph is previous.backend and previous.observe()
    assert len(calls) >= 8


def test_read_bundle_rehashes_actual_sqlite_and_rejects_forged_counts(tmp_path, prepared):
    import shutil
    target = tmp_path / "bundle"
    shutil.copytree(prepared.root, target)
    manifest = json.loads((target / "manifest.json").read_bytes())
    manifest["nodes"] += 1
    (target / "manifest.json").write_bytes(canonical_bytes(manifest))
    with pytest.raises(ValueError, match="counts"):
        CombinedGraphBundle.read(target, manifest_sha256=sha256_of(target / "manifest.json"))
    with sqlite3.connect(target / "graph.sqlite") as con:
        con.execute("UPDATE nodes SET props='{}' WHERE key='meta:combined'")
    with pytest.raises(ValueError, match="byte identity"):
        CombinedGraphBundle.read(target, manifest_sha256=sha256_of(target / "manifest.json"))


def test_in_memory_manifest_cannot_relabel_native_policy_or_document_origin(prepared):
    expected = prepared.manifest["policy_sha256"]
    prepared.manifest["policy_sha256"] = "0" * 64
    assert prepared.manifest["policy_sha256"] == expected
    lease = publisher().publish(prepared)
    lease.manifest["policy_sha256"] = "0" * 64
    with pytest.raises(ValueError, match="in-memory proof"):
        lease.component()


def test_missing_native_proof_permission_blocks_before_remote_writes(prepared):
    driver = Driver()
    driver.checkpoint_unavailable = True
    with pytest.raises(ValueError, match="checkpoint unavailable"):
        publisher(driver).publish(prepared)
    assert driver.writes == []


def test_partial_generation_conflicting_existing_record_is_not_overwritten(prepared):
    driver = Driver()
    driver.interrupt = lambda q: q.startswith("UNWIND $rows AS row MATCH")
    with pytest.raises(RuntimeError): publisher(driver).publish(prepared)
    victim = next(r for r in driver.nodes.values() if "Block" in r["labels"])
    victim["props"]["content_sha256"] = "0" * 64
    with pytest.raises(ValueError, match="overwrite is forbidden"):
        publisher(driver).publish(prepared)
    assert victim["props"]["content_sha256"] == "0" * 64
    assert driver.nodes["meta:combined"]["props"]["status"] == "LOADING"


def test_switch_fence_failure_leaves_original_receiver_selected(prepared):
    old, new = Driver("old"), Driver("new")
    publisher(old).publish(prepared); publisher(new).publish(prepared)
    old.access = new.access = "read-only"
    deps, candidate_deps = SimpleNamespace(graph=backend(old)), SimpleNamespace(graph=backend(new))
    previous, candidate = g.NativeGraphObserver(deps, prepared), g.NativeGraphObserver(candidate_deps, prepared)
    def closed(): raise ValueError("active requests not drained")
    switch = g.GraphReceiverSwitch(deps, previous, candidate, fence=closed)
    with pytest.raises(ValueError, match="not drained"): switch.apply()
    assert deps.graph is previous.backend


def test_read_write_transfer_is_never_qualified_as_production_receiver(prepared):
    driver = Driver()
    transfer = publisher(driver).publish(prepared)
    assert transfer.identity["scope"] == "SHADOW_TRANSFER_ONLY"
    deps = SimpleNamespace(graph=backend(driver))
    with pytest.raises(ValueError, match="native read-only"):
        g.NativeGraphObserver(deps, prepared)
    driver.access = "read-only"  # synthetic operator freeze, not an adapter write
    receiver = g.NativeGraphObserver(deps, prepared)
    assert receiver.lease.identity["scope"] == "READ_ONLY_RECEIVER"
    before = list(driver.writes)
    receiver.observe()
    assert driver.writes == before
