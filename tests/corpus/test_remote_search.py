"""Adversarial native-generation and exact selector tests; no remote services."""
import copy
from types import SimpleNamespace

import pytest

from vkm_corpus.update.remote_search import (SearchBundleSpec, SearchBundleLease, SearchBundleSwitch,
    RemoteSearchError, NativeSearchObserver, content_digest, validate_prepare_options)
from vkm_corpus.search.mappings import INDEX_TYPES


class Missing(Exception):
    status_code = 404


class Cluster:
    def __init__(self):
        self.transport = SimpleNamespace(hosts=[{"host": "synthetic", "port": 9200}])
        self.cluster_uuid = "cluster-a"
        self.indices = self
        self.data, self.aliases, self.writes, self.scrolls = {}, {}, [], {}
        self.partial = False
        self.on_count = None
        self.ack = True

    def add(self, tag):
        spec = SearchBundleSpec(prefix="vkm", indices={k: f"vkm-{k}-m1-{tag}" for k in (*INDEX_TYPES, "vectors", "pagevis")})
        for k, name in spec.indices.items():
            self.data[name] = {"uuid": name + "-uuid", "seq": 0, "docs": {"id-1": {"kind": k, "value": tag}},
                "settings": {"index.uuid": name + "-uuid", "index.number_of_shards": "1", "index.blocks.write": "true"},
                "mapping": {"_meta": {"build_status": "COMPLETE", "built_from_snapshot_id": "snap-" + tag,
                    "canonical_manifest_sha256": ("a" if tag == "old" else "b") * 64, "policy_sha256": "c" * 64},
                    "properties": {"value": {"type": "keyword"}}}}
        return spec

    def select(self, spec):
        self.aliases = {k: list(v) for k, v in spec.aliases().items()}

    def info(self):
        return {"cluster_uuid": self.cluster_uuid}

    def get_alias(self, name):
        if name not in self.aliases:
            raise Missing()
        return {n: {"aliases": {name: {}}} for n in self.aliases[name]}

    def get_mapping(self, index):
        return {index: {"mappings": copy.deepcopy(self.data[index]["mapping"])}}

    def get_settings(self, index, params):
        return {index: {"settings": copy.deepcopy(self.data[index]["settings"])}}

    def stats(self, index, params):
        d = self.data[index]
        return {"_shards": {"failed": 0}, "indices": {index: {"uuid": d["uuid"], "shards": {
            "0": [{"routing": {"primary": True, "state": "STARTED"}, "seq_no": {
                "max_seq_no": d["seq"], "local_checkpoint": d["seq"], "global_checkpoint": d["seq"]}}]}}}}

    def count(self, index):
        if self.on_count:
            f, self.on_count = self.on_count, None
            f()
        return {"_shards": {"failed": 0}, "count": len(self.data[index]["docs"])}

    def search(self, index, body, params):
        hits = [{"_id": k, "_index": index, "_source": copy.deepcopy(v)} for k, v in self.data[index]["docs"].items()]
        self.scrolls[index] = []
        return {"_scroll_id": index, "timed_out": False, "_shards": {"failed": int(self.partial)},
                "hits": {"total": {"relation": "eq", "value": len(hits)}, "hits": hits}}

    def scroll(self, body, params):
        return {"_scroll_id": body["scroll_id"], "timed_out": False, "_shards": {"failed": 0}, "hits": {"hits": []}}

    def clear_scroll(self, body):
        for token in body["scroll_id"]:
            self.scrolls.pop(token)

    def update_aliases(self, body):
        changed = copy.deepcopy(self.aliases)
        for action in body["actions"]:
            op, value = next(iter(action.items()))
            targets = changed.setdefault(value["alias"], [])
            if op == "remove":
                assert value["must_exist"]
                targets.remove(value["index"])
            else:
                targets.append(value["index"])
        self.aliases = changed
        self.writes.append(body)
        return {"acknowledged": self.ack}


def lease(c, spec):
    return SearchBundleLease(c, spec, max_documents=20, timeout_seconds=10)


def pair():
    c = Cluster()
    a, b = c.add("old"), c.add("new")
    c.select(a)
    return c, lease(c, a), lease(c, b)


@pytest.mark.parametrize("change", ["cache", "backend", "hybrid_client", "prefix", "alias"])
def test_native_search_observer_uses_actual_clients_and_generation_cache(change):
    from vkm_corpus.api.backends import OpenSearchBackend, HybridBackend
    cluster = Cluster()
    spec = cluster.add("old")
    cluster.select(spec)
    settings = SimpleNamespace(opensearch_index_prefix="vkm")
    search = OpenSearchBackend(settings)
    search._client = cluster
    hybrid = object.__new__(HybridBackend)
    hybrid._search, hybrid.settings, hybrid._meta, hybrid._vmeta = search, settings, None, None
    deps = SimpleNamespace(search=search, hybrid=hybrid)
    observer = NativeSearchObserver(deps, spec, max_documents=20, timeout_seconds=10)
    assert set(observer.observe()) == {"SEARCH", "DENSE", "VISUAL"}
    hybrid._meta = (1.0, dict(observer.meta["vectors"]))
    assert observer.observe()["DENSE"]["revision"] == cluster.data[spec.indices["vectors"]]["uuid"]
    if change == "cache": hybrid._meta[1]["index"] = "vkm-vectors-m1-other"
    if change == "backend": deps.search = OpenSearchBackend(settings)
    if change == "hybrid_client":
        hybrid._search = OpenSearchBackend(settings)
        hybrid._search._client = Cluster()
    if change == "prefix": settings.opensearch_index_prefix = "other"
    if change == "alias": cluster.aliases["vkm-vectors"] = ["vkm-vectors-m1-other"]
    with pytest.raises(RemoteSearchError):
        observer.observe()
    assert cluster.writes == []


def test_exact_bundle_apply_restore_is_atomic_and_idempotent():
    c, a, b = pair()
    calls = []
    op = SearchBundleSwitch(a, b, fence=lambda: calls.append("fenced"))
    assert a.observe() == a.identity
    assert op.apply()["changed"]
    assert c.aliases == {k: list(v) for k, v in b.identity.spec.aliases().items()}
    assert not op.apply()["changed"]
    assert op.restore()["changed"]
    assert not op.restore()["changed"]
    assert len(c.writes) == 2 and len(calls) >= 8
    assert len(c.data) == 14
    assert not c.scrolls


@pytest.mark.parametrize("fault", ["uuid", "metadata", "content", "mapping", "write_enabled", "endpoint", "cluster"])
def test_native_lease_rejects_changes_not_just_expected_json(fault):
    c, a, b = pair()
    d = c.data[a.identity.spec.indices["pages"]]
    if fault == "uuid":
        d["uuid"] += "-replacement"
        d["settings"]["index.uuid"] = d["uuid"]
    elif fault == "metadata":
        d["mapping"]["_meta"]["policy_sha256"] = "d" * 64
    elif fault == "content":
        d["docs"]["id-1"]["value"] = "mutated same count"
        d["seq"] += 1
    elif fault == "mapping":
        d["mapping"]["properties"]["value"]["type"] = "text"
    elif fault == "write_enabled":
        d["settings"]["index.blocks.write"] = "false"
    elif fault == "endpoint":
        c.transport.hosts[0]["port"] = 9201
    else:
        c.cluster_uuid = "another-cluster"
    with pytest.raises(RemoteSearchError):
        a.observe()
    with pytest.raises(RemoteSearchError):
        SearchBundleSwitch(a, b, fence=lambda: None).apply()
    assert not c.writes


def test_actual_content_digest_changes_even_if_metadata_and_count_do_not():
    c = Cluster()
    spec = c.add("old")
    a = lease(c, spec)
    c.data[spec.indices["pages"]]["docs"]["id-1"]["value"] = "different actual source"
    b = lease(c, spec)
    assert a.identity.indices == b.identity.indices  # fake bypasses native write sequence
    assert a.identity.content_sha256 != b.identity.content_sha256


def test_alias_move_during_observation_is_rejected():
    c, a, b = pair()
    c.on_count = lambda: c.select(b.identity.spec)
    with pytest.raises(RemoteSearchError, match="changed during"):
        a.observe()


def test_foreign_or_partial_selector_never_overwritten_by_apply_or_restore():
    c, a, b = pair()
    c.aliases["vkm-pages"] = ["foreign-index"]
    op = SearchBundleSwitch(a, b, fence=lambda: None)
    for fn in (op.apply, op.restore):
        with pytest.raises(RemoteSearchError, match="selectors differ"):
            fn()
    assert not c.writes


def test_lost_drain_writer_lease_has_no_mutations():
    c, a, b = pair()
    def denied():
        raise RuntimeError("lease lost")
    with pytest.raises(RuntimeError, match="lease lost"):
        SearchBundleSwitch(a, b, fence=denied).apply()
    assert not c.writes


def test_uncertain_response_can_be_recovered_from_actual_exact_selectors():
    c, a, b = pair()
    c.ack = False
    op = SearchBundleSwitch(a, b, fence=lambda: None)
    with pytest.raises(RemoteSearchError, match="not acknowledged"):
        op.apply()
    c.ack = True
    assert op.restore()["changed"]
    assert a.observe()


@pytest.mark.parametrize("fault", ["partial", "budget", "missing_policy", "sequence"])
def test_unqualified_candidate_never_becomes_lease(fault):
    c = Cluster()
    spec = c.add("old")
    d = c.data[spec.indices["pages"]]
    if fault == "partial":
        c.partial = True
    elif fault == "missing_policy":
        del d["mapping"]["_meta"]["policy_sha256"]
    elif fault == "sequence":
        d["seq"] = None
    with pytest.raises(ValueError):
        SearchBundleLease(c, spec, max_documents=0 if fault == "budget" else 20, timeout_seconds=10)
    assert not c.writes
    assert not c.scrolls


def test_prepare_requires_policy_and_names_cannot_embed_queries():
    for policy in (None, "PUBLIC", "z" * 64):
        with pytest.raises(ValueError):
            validate_prepare_options(False, policy)
    with pytest.raises(ValueError):
        SearchBundleSpec(prefix="vkm", indices={k: "*" for k in INDEX_TYPES})


def test_bm25_prepare_freezes_indices_without_touching_aliases_or_pruning(tmp_path, monkeypatch):
    from vkm_corpus.search import indexer
    from vkm_corpus.search.fakes import FakeOpenSearch
    from vkm_corpus.config import load_settings
    from vkm_corpus.graph.common import check
    client = FakeOpenSearch()
    client.cluster = SimpleNamespace(health=lambda: {"status": "green"})
    client.info = lambda: {"version": {"number": "synthetic"}}
    client.count = lambda index: {"count": 0}
    monkeypatch.setattr(indexer, "iter_documents", lambda inp, kind: iter(()))
    monkeypatch.setattr(indexer, "analyzer_checks", lambda c, i: check("S4", "synthetic empty fixture", [], code="E_ANALYZER"))
    from opensearchpy import helpers
    monkeypatch.setattr(helpers, "streaming_bulk", lambda c, actions, **kw: iter(list(actions)))
    receipt = {}
    inp = SimpleNamespace(info=SimpleNamespace(snapshot_id="synthetic", manifest_sha256="a" * 64))
    import time
    indexer._build_indices(load_settings({}), indexer.BuildOptions(publish=False, policy_sha256="c" * 64, prune=True),
        "vkm", inp, tmp_path, receipt, dict.fromkeys(INDEX_TYPES, 0), {}, time.monotonic(), client)
    assert receipt["status"] == "COMPLETE" and receipt["publication"] == "PREPARED_NOT_PUBLISHED"
    assert not client.aliases and not client.deleted
    assert all(i["meta"]["policy_sha256"] == "c" * 64 for i in client.indices_.values())
    assert not any(call[0] == "update_aliases" for call in client.calls)
