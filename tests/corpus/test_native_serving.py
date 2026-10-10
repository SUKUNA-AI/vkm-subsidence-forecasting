"""Integrated real native factories with fake Neo4j/OpenSearch/HTTP/PG.

No observer is replaced by expected identities; no remote calls/model execution.
The synthetic LATE binding exercises the trust boundary, not corpus qualification.
"""
import asyncio
import json
from types import SimpleNamespace

import pytest

from test_remote_graph import Driver, assets, publisher, backend, CombinedGraphBundle
from test_remote_search import Cluster
from test_remote_services import environment
from test_pack_policy import pack_assets
from vkm_corpus.api.backends import OpenSearchBackend
from vkm_corpus.parquet.atomic import sha256_of
from vkm_corpus.update.runtime import BoundFile
from vkm_corpus.update.serving import NativeServingBindings, NativeServingProfile
from vkm_corpus.update import pack_policy
from vkm_evidence.contracts import canonical_bytes


@pytest.fixture(scope="module")
def graph_bundle(tmp_path_factory):
    root = tmp_path_factory.mktemp("native-serving")
    args = assets(root)
    graph = CombinedGraphBundle.prepare(root / "bundle", **args)
    request, _ = pack_assets(root / "pack-inputs", canonical=args["canonical_root"], policy_path=args["policy_store"].path)
    output = root / "pack-qualification"
    output.mkdir(mode=0o700)
    pack_policy._qualify(request, output)
    return graph


def write(path, value):
    path.write_bytes(canonical_bytes(value))
    return BoundFile(path=str(path), sha256=sha256_of(path))


def configured(tmp_path, monkeypatch, graph_bundle, *, fault=None):
    deps, db, bodies, calls = environment(monkeypatch)
    cluster = Cluster()
    spec = cluster.add("candidate")
    cluster.select(spec)
    document = graph_bundle.manifest["inputs"]["DOCUMENT"]
    policy_sha = graph_bundle.manifest["policy_sha256"]
    for index in cluster.data.values():
        index["mapping"]["_meta"].update(built_from_snapshot_id=document["revision"],
            canonical_manifest_sha256=document["manifest_sha256"], policy_sha256=policy_sha)
    if fault == "search_document":
        for index in cluster.data.values(): index["mapping"]["_meta"]["canonical_manifest_sha256"] = "0" * 64
    settings = SimpleNamespace(opensearch_index_prefix="vkm")
    deps.search = OpenSearchBackend(settings)
    deps.search._client = cluster
    deps.hybrid._search, deps.hybrid.settings = deps.search, settings
    deps.hybrid._meta = deps.hybrid._vmeta = None
    native_graph = Driver()
    publisher(native_graph).publish(graph_bundle)
    native_graph.access = "read-write" if fault == "graph_rw" else "read-only"
    deps.graph = backend(native_graph)
    qualification_path = graph_bundle.root.parent / "pack-qualification" / "qualification.json"
    qualification = json.loads(qualification_path.read_bytes())
    bodies["retrieval"]["pack"] = qualification["loaded_pack"]
    binding = dict(qualification["binding"])
    if fault == "late_document": binding["canonical_manifest_sha256"] = "0" * 64
    if fault == "late_snapshot": binding["snapshot_id"] = "other-snapshot"
    if fault == "late_pack": binding["component_manifest_sha256"] = "0" * 64
    if fault == "late_policy": binding["policy_sha256"] = "0" * 64
    binding_file = write(tmp_path / "late.json", binding)
    profile = NativeServingProfile(search=spec,
        graph_manifest=BoundFile(path=str(graph_bundle.root / "manifest.json"), sha256=graph_bundle.manifest_sha256),
        late_policy_binding=binding_file,
        late_qualification=BoundFile(path=str(qualification_path), sha256=sha256_of(qualification_path)),
        control=db.spec(), max_search_documents=100,
        search_timeout_seconds=10, graph_page_size=11)
    path = write(tmp_path / "profile.json", profile)
    expected_document = {"revision": document["revision"], "manifest_sha256": document["manifest_sha256"], "policy_sha256": policy_sha}
    return deps, db, bodies, calls, cluster, native_graph, path, expected_document


async def close(deps):
    await deps.rerank._client.aclose()
    deps.hybrid._embed._http.close()


def test_complete_native_factory_binds_actual_graph_search_pack_and_services(tmp_path, monkeypatch, graph_bundle):
    deps, db, bodies, calls, cluster, graph, profile, document = configured(tmp_path, monkeypatch, graph_bundle)
    previous_writes = list(graph.writes)
    async def run():
        try:
            native = await NativeServingBindings.bind(deps, profile)
            assert set(native.observe_components()) == {"GRAPH", "SEARCH", "DENSE", "LATE", "VISUAL"}
            native.verify_document(document)
            assert set(await native.observe_services()) == {"RETRIEVAL", "RERANK", "CONTROL"}
            assert native.graph.driver is deps.graph._driver
            assert native.search.client is deps.hybrid._search._client
            assert native.services.clients == (deps.rerank._client._http, deps.hybrid._embed._http)
            assert graph.writes == previous_writes and not cluster.writes
            assert all(r.url.path == "/identity" and r.method == "GET" for r in calls)
        finally: await close(deps)
    asyncio.run(run())


@pytest.mark.parametrize("fault", ["late_document", "late_policy", "late_snapshot", "late_pack", "search_document", "graph_rw"])
def test_native_factory_rejects_mixed_store_origin_or_unqualified_graph(tmp_path, monkeypatch, graph_bundle, fault):
    deps, db, bodies, calls, cluster, graph, profile, document = configured(tmp_path, monkeypatch, graph_bundle, fault=fault)
    async def run():
        try:
            with pytest.raises(ValueError):
                native = await NativeServingBindings.bind(deps, profile)
                native.verify_document(document)
        finally: await close(deps)
    asyncio.run(run())


@pytest.mark.parametrize("fault", ["graph_restart", "graph_access", "search_alias", "search_sequence", "retrieval_model", "late_loaded_pack", "stale_worker", "profile"])
def test_native_integrated_observation_closes_after_actual_backend_change(tmp_path, monkeypatch, graph_bundle, fault):
    deps, db, bodies, calls, cluster, graph, profile, document = configured(tmp_path, monkeypatch, graph_bundle)
    async def run():
        try:
            native = await NativeServingBindings.bind(deps, profile)
            native.verify_document(document)
            if fault == "graph_restart": graph.start = "new-start"
            if fault == "graph_access": graph.access = "read-write"
            if fault == "search_alias": cluster.aliases[next(iter(cluster.aliases))] = []
            if fault == "search_sequence": next(iter(cluster.data.values()))["seq"] += 1
            if fault == "retrieval_model": bodies["retrieval"]["models"]["dense"]["weights_sha256"] = "0" * 64
            if fault == "late_loaded_pack": bodies["retrieval"]["pack"]["files"]["tokens.f16"] = "0" * 64
            if fault == "stale_worker": db.workers[0]["age_seconds"] = 1000
            if fault == "profile":
                from pathlib import Path
                path = Path(profile.path)
                data = json.loads(path.read_bytes()); data["max_search_documents"] += 1
                path.write_bytes(canonical_bytes(data))
            with pytest.raises(ValueError):
                await native.observe_services()
                native.observe_components()
                native.verify_document(document)
        finally: await close(deps)
    asyncio.run(run())
