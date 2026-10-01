"""Shadow EVIDENCE transfer tests with actual projections and a fake driver."""
import copy
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from vkm_corpus.contracts.access import AccessContext, ResourcePolicy
from vkm_corpus.graph import shadow as s
from vkm_corpus.parquet.atomic import sha256_of
from vkm_evidence.contracts import Entity, EvidenceBatch, ObjectRef
from vkm_evidence.journal import EvidenceJournal
from vkm_evidence.objects import ObjectCatalogue, OriginalObject
from vkm_evidence.projections import build_projection
from vkm_evidence.query import EvidenceReader


class Driver:
    def __init__(self, identity):
        self.identity = identity
        self.nodes, self.edges, self.meta = {}, {}, None
        self.foreign = 0
        self.writes = []
        self.corrupt_roundtrip = False
        self.interrupt = None

    def get_server_info(self):
        return SimpleNamespace(address=(self.identity, 7687))

    def execute_query(self, query, *, parameters_, database_, **kwargs):
        query = str(query)
        p = parameters_
        if query == s.DB_INFO:
            rows = [{"id": self.identity, "name": database_}]
        elif query == s.OWNERS:
            n = len(self.nodes) + bool(self.meta)
            rows = [{"total": n + self.foreign, "owned": n}]
        elif query == s.META:
            rows = [{"props": self.meta}] if self.meta else []
        elif query == s.READ_NODES:
            rows = [{"props": n} for n in self.nodes.values()]
            if self.corrupt_roundtrip and rows:
                rows = copy.deepcopy(rows)
                rows[0]["props"]["payload_json"] = "corruption"
        elif query == s.READ_EDGES:
            rows = [{"type": "VKM_EVIDENCE_REL", **e} for e in self.edges.values()]
        else:
            self.writes.append(query)
            if self.interrupt == query:
                self.interrupt = None
                raise RuntimeError("synthetic interrupted remote transaction")
            if query == s.DDL:
                rows = []
            elif query == s.START:
                if self.meta is None:
                    self.meta = {**p, "status": "LOADING"}
                rows = [{"props": self.meta}]
            elif query == s.NODES:
                rows = []
                for row in p["rows"]:
                    self.nodes.setdefault(row["node_key"], copy.deepcopy(row))
                    rows.append({"props": self.nodes[row["node_key"]]})
            elif query == s.EDGES:
                rows = []
                for row in p["rows"]:
                    if row["subject"] in self.nodes and row["object"] in self.nodes:
                        key = row["props"]["edge_key"]
                        self.edges.setdefault(key, copy.deepcopy(row))
                        rows.append(self.edges[key])
            elif query == s.FINISH:
                self.meta.update(p, status="COMPLETE")
                rows = [{"props": self.meta}]
            else:
                raise AssertionError(query)
        return copy.deepcopy(rows), None, None


def projection(tmp_path):
    ctx = AccessContext(principal="synthetic", execution="CLOUD")
    policy = ResourcePolicy(access_class="PUBLIC", policy_version="1", authority="synthetic")
    ref = ObjectRef(source_id="VKM-SRC-001", source_sha256="a" * 64, snapshot_id="synthetic",
        object_id="object", object_version="1", content_sha256="a" * 64, locator="page:1", extraction_generation="1")
    catalogue = ObjectCatalogue((OriginalObject(ref, policy),))
    journal = EvidenceJournal(tmp_path / "journal", object_validator=catalogue.validate)
    e = Entity(record_id="entity", recorded_at=datetime(2026, 10, 1, tzinfo=timezone.utc),
        actor=ctx.principal, policy=policy, supports=(ref,), entity_type="MINE", label="synthetic")
    journal.publish("publish", journal.revision, EvidenceBatch(records=(e,)), ctx)
    result = build_projection(EvidenceReader(journal), tmp_path / "projection", ctx)
    return tmp_path / "projection" / result["projection_id"]


def publisher(shadow, serving=None, fence=lambda: None):
    return s.ShadowEvidencePublisher(shadow, "neo4j", serving_driver=serving or Driver("serving"),
                                    serving_database="neo4j", fence=fence, batch_size=1)


def test_roundtrip_all_prepared_records_and_origins_without_serving_mutations(tmp_path):
    root = projection(tmp_path)
    remote, serving = Driver("shadow"), Driver("serving")
    pub = publisher(remote, serving)
    result = pub.publish(root, manifest_sha256=sha256_of(root / "manifest.json"))
    assert result["status"] == "PASS" and result["scope"] == "EVIDENCE_GRAPH_ONLY"
    assert result["serving_switch"] == result["scientific_qualification"] == "NOT_RUN"
    assert result["nodes"] == len(remote.nodes) >= 2 and result["relationships"] == len(remote.edges) >= 1
    assert not serving.writes
    previous = list(remote.writes)
    assert pub.publish(root, manifest_sha256=sha256_of(root / "manifest.json")) == result
    assert remote.writes == previous  # completed retry is read-only and reverified


def test_same_native_database_is_refused_even_through_another_driver():
    shadow, serving = Driver("same-id"), Driver("same-id")
    with pytest.raises(ValueError, match="serving"):
        publisher(shadow, serving)
    assert not shadow.writes


@pytest.mark.parametrize("fault", ["foreign", "tamper", "budget", "fence", "other_manifest"])
def test_rejection_before_any_write(tmp_path, fault):
    root = projection(tmp_path)
    digest = sha256_of(root / "manifest.json")
    remote = Driver("shadow")
    pub = publisher(remote)
    if fault == "foreign":
        remote.foreign = 1
    elif fault == "tamper":
        (root / "neo4j_nodes.jsonl").write_bytes(b"tampered")
    elif fault == "budget":
        pub.max_records = 1
    elif fault == "fence":
        pub.fence = lambda: (_ for _ in ()).throw(ValueError("policy revoked"))
    else:
        remote.meta = {"projection_id": "other", "manifest_sha256": digest, "status": "LOADING"}
    with pytest.raises(ValueError):
        pub.publish(root, manifest_sha256=digest)
    assert not remote.writes


def test_interrupted_shadow_load_resumes_exactly_without_duplicate_edges(tmp_path):
    root = projection(tmp_path)
    remote = Driver("shadow")
    remote.interrupt = s.EDGES
    pub = publisher(remote)
    with pytest.raises(RuntimeError, match="interrupted"):
        pub.publish(root, manifest_sha256=sha256_of(root / "manifest.json"))
    assert remote.meta["status"] == "LOADING"
    result = pub.publish(root, manifest_sha256=sha256_of(root / "manifest.json"))
    assert result["status"] == "PASS" and result["relationships"] == len(remote.edges)


def test_roundtrip_corruption_never_publishes_complete(tmp_path):
    root = projection(tmp_path)
    remote = Driver("shadow")
    remote.corrupt_roundtrip = True
    with pytest.raises(ValueError, match="content differs"):
        publisher(remote).publish(root, manifest_sha256=sha256_of(root / "manifest.json"))
    assert remote.meta["status"] == "LOADING"
    assert s.FINISH not in remote.writes


def test_shadow_endpoint_replacement_after_bind_is_refused(tmp_path):
    root = projection(tmp_path)
    remote = Driver("shadow")
    pub = publisher(remote)
    remote.identity = "replaced"
    with pytest.raises(ValueError, match="identity changed"):
        pub.publish(root, manifest_sha256=sha256_of(root / "manifest.json"))
    assert not remote.writes
