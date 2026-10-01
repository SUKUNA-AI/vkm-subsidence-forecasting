"""Adversarial native service identities; fake transports, never real servers."""
import asyncio
import copy
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from vkm_corpus.api.backends import GatewayRerankBackend, HybridBackend, PgControlPlane
from vkm_corpus.retrieval.client import RerankClient
from vkm_corpus.search.hybrid import EmbedClient
from vkm_corpus.update import remote_control as rc
from vkm_corpus.update import remote_models as rm
from vkm_corpus.update.remote_services import NativeServiceObserver
from vkm_evidence.contracts import record_hash

H = "a" * 64


class Database:
    def __init__(self):
        self.native = dict(system_identifier="7102345678", database_oid=16384, database_name="vkm",
            server_version="170006", server_address="127.0.0.1", server_port=5432, postmaster_started="2026-10-01T00:00:00Z",
            current_role="vkm", session_role="vkm", search_path="pg_catalog, public")
        self.schema = {"columns": [{"relation": t, "position": 1, "name": "id", "type": "text", "not_null": True,
            "default_expression": None} for t in sorted(rc.REQUIRED_TABLES)], "constraints": [], "indexes": [],
            "versions": [{"version": "ops-0.1.0"}]}
        self.workers = [dict(worker_id="w1", host_role="WORKSTATION", kind="PIPELINE", state="IDLE", age_seconds=2.0)]
        self.queries, self.rollbacks, self.closed = [], 0, 0
        self.denied = False

    def cursor(self):
        database = self
        class Cursor:
            def __enter__(self): return self
            def __exit__(self, *args): pass
            def execute(self, sql):
                database.queries.append(sql)
                self.sql = sql
                if database.denied and sql == rc.NATIVE:
                    raise PermissionError("native system read unavailable")
                assert sql in {rc.BEGIN, rc.TIMEOUT, rc.NATIVE, rc.COLUMNS, rc.CONSTRAINTS, rc.INDEXES, rc.VERSIONS, rc.WORKERS}
            def fetchone(self): return dict(database.native)
            def fetchall(self):
                mapping = {rc.COLUMNS: database.schema["columns"], rc.CONSTRAINTS: database.schema["constraints"],
                    rc.INDEXES: database.schema["indexes"], rc.VERSIONS: database.schema["versions"], rc.WORKERS: database.workers}
                return copy.deepcopy(mapping[self.sql])
        return Cursor()
    def rollback(self): self.rollbacks += 1
    def close(self): self.closed += 1
    def spec(self):
        return rc.ControlSpec(schema_sha256=record_hash(self.schema),
            workers=(rc.WorkerRequirement(host_role="WORKSTATION", kind="PIPELINE"),))


def test_control_native_read_only_schema_and_liveness():
    db = Database()
    result = rc.read_native(db, db.spec())
    assert result["native"] == db.native and result["workers"] == [["WORKSTATION", "PIPELINE"]]
    assert db.queries[:2] == [rc.BEGIN, rc.TIMEOUT] and db.rollbacks == 1


@pytest.mark.parametrize("change", ["schema", "missing", "stale", "future", "stopped", "permission", "native"])
def test_control_no_false_ready_and_transaction_always_closed(change):
    db = Database()
    spec = db.spec()
    if change == "schema": db.schema["columns"][0]["type"] = "jsonb"
    if change == "missing": db.workers = []
    if change == "stale": db.workers[0]["age_seconds"] = 181
    if change == "future": db.workers[0]["age_seconds"] = -1
    if change == "stopped": db.workers[0]["state"] = "STOPPED"
    if change == "permission": db.denied = True
    if change == "native": db.native["system_identifier"] = None
    with pytest.raises((ValueError, PermissionError)):
        rc.read_native(db, spec)
    assert db.rollbacks == 1


def test_control_worker_profile_cannot_be_empty_or_inferred():
    with pytest.raises(ValueError, match="nonempty"):
        rc.ControlSpec(schema_sha256=H, workers=())


def native_models():
    return {role: {"role": role, "model_id": "synthetic", "model_revision": "test", "query_signature": H,
        "query_config_sha256": H, "weights_sha256": H, "tokenizer_sha256": H, "resources_sha256": H,
        "process": {"pid": i + 1, "start_ticks": 100, "command_sha256": H, "executable_signature": [1,2,3,4,5],
                    "endpoint_sha256": H}} for i, role in enumerate(("dense", "late", "visual"))}


def payloads():
    models = {kind: rm.NativeModelProof(kind=kind, instance_sha256=H, code_sha256=H, dependencies_sha256=H,
        config_sha256=H, resources={"weights": H, "tokenizer": H, **({"mmproj": H} if kind == "visual" else {})}
        ).model_dump(mode="json") for kind in ("text", "visual")}
    return {"rerank": {"schema": "vkm-rerank-native-identity/1", "status": "READY", "instance_sha256": H,
        "code_sha256": H, "dependencies_sha256": H, "config_sha256": H,
        "resources": {"text_tokenizer": H, "visual_tokenizer": H, "visual_head": H},
        "models": models, "functional_qualification": "NOT_RUN"},
        "retrieval": {"schema": "vkm-retrieval-native-identity/1", "status": "READY", "scope": "NATIVE_LOADED_IDENTITY",
            "code_sha256": H, "dependencies_sha256": H,
            "pack": {"schema": "vkm-loaded-pack/1", "manifest_sha256": H,
                "pack_id": "synthetic-pack", "snapshot_id": "snap-synthetic", "config_signature": H,
                "config_sha256": H, "files": {"tokens.f16": H, "index.parquet": H}, "count": 1, "total_tokens": 1},
            "models": native_models(), "functional_qualification": "NOT_RUN"}}


def environment(monkeypatch):
    from vkm_corpus.api import production
    monkeypatch.setattr(production, "serving_code_identity", lambda: H)
    monkeypatch.setattr(production, "serving_dependencies_identity", lambda: H)
    db, bodies, calls = Database(), payloads(), []
    def transport(request):
        calls.append(request)
        assert request.method == "GET" and request.url.path == "/identity"
        role = "rerank" if request.url.host == "rerank.test" else "retrieval"
        assert request.headers.get("X-VKM-Rerank-Token") == "rr" if role == "rerank" else request.headers.get("Authorization") == "Bearer ee"
        return httpx.Response(200, json=bodies[role])
    rr = object.__new__(GatewayRerankBackend)
    rr._client = RerankClient("http://rerank.test", "rr", transport=httpx.MockTransport(transport))
    hybrid = object.__new__(HybridBackend)
    hybrid._embed = EmbedClient("http://retrieval.test", "ee", transport=httpx.MockTransport(transport))
    control = PgControlPlane(SimpleNamespace())
    control._conn = lambda: db
    return SimpleNamespace(rerank=rr, hybrid=hybrid, control=control), db, bodies, calls


def test_native_factory_binds_actual_clients_and_no_model_calls(monkeypatch):
    deps, db, bodies, calls = environment(monkeypatch)
    async def run():
        observer = await NativeServiceObserver.bind(deps, control_spec=db.spec())
        result = await observer.observe()
        assert set(result) == {"RETRIEVAL", "RERANK", "CONTROL"}
        assert result["CONTROL"]["resources"] == {"ops_schema": record_hash(db.schema)}
        assert len(calls) == 4 and db.rollbacks == 3 and db.closed == 3
        await deps.rerank._client.aclose()
        deps.hybrid._embed._http.close()
    asyncio.run(run())


@pytest.mark.parametrize("change", ["model", "missing_role", "legacy_health", "degraded", "client", "endpoint", "database", "pg_role", "stale_worker", "fake_qualification", "pack_file", "pack_empty", "pack_snapshot"])
def test_native_factory_refuses_changed_or_unproved_services(monkeypatch, change):
    deps, db, bodies, calls = environment(monkeypatch)
    async def run():
        observer = await NativeServiceObserver.bind(deps, control_spec=db.spec())
        if change == "model": bodies["rerank"]["models"]["text"]["resources"]["weights"] = "b" * 64
        if change == "missing_role": del bodies["retrieval"]["models"]["visual"]
        if change == "legacy_health": bodies["rerank"] = {"status": "ready", "runtime": {"commit": H}}
        if change == "degraded": bodies["rerank"]["status"] = "DEGRADED"
        if change == "client": deps.hybrid._embed = SimpleNamespace(_http=observer.clients[1])
        if change == "endpoint": observer.clients[1].base_url = "http://other.test"
        if change == "database": db.native["system_identifier"] = "1234"
        if change == "pg_role": db.native["current_role"] = "different_role"
        if change == "stale_worker": db.workers = []
        if change == "fake_qualification": bodies["retrieval"]["functional_qualification"] = "PASS"
        if change == "pack_file": del bodies["retrieval"]["pack"]["files"]["tokens.f16"]
        if change == "pack_empty": bodies["retrieval"]["pack"]["count"] = 0
        if change == "pack_snapshot": bodies["retrieval"]["pack"]["snapshot_id"] = "other-snapshot"
        from vkm_corpus.api.errors import ApiFailure
        with pytest.raises((ValueError, ApiFailure)):
            await observer.observe()
        await observer.rerank.aclose()
        observer.clients[1].close()
    asyncio.run(run())


def test_gateway_legacy_ready_is_not_native_identity_or_model_call():
    from test_rerank_gateway import make_client, _hdr
    client, resources = make_client()
    with client:
        assert client.get("/identity").status_code == 401
        response = client.get("/identity", headers=_hdr())
        assert response.status_code == 503 and response.json()["reason"] == "NATIVE_MODEL_PROVIDERS_NOT_BOUND"
        assert resources.text.calls == [] and resources.visual.calls == []


class SyntheticModel:
    pass


@pytest.mark.parametrize("change", ["object", "resources", "config", "during_load"])
def test_real_loader_attestation_refuses_replacement_and_mutation(tmp_path, monkeypatch, change):
    # Filesystem semantics are separately Linux-qualified; this unit isolates
    # real loader/getter/process and bytes binding on synthetic resources.
    monkeypatch.setattr(rm, "own_process", lambda: {"pid": 1, "start_ticks": 2})
    from vkm_corpus.update import native_files
    monkeypatch.setattr(native_files, "NativeFileWatch", lambda paths: SimpleNamespace(check=lambda: None))
    weights, tokenizer = tmp_path / "weights", tmp_path / "tokenizer"
    weights.write_bytes(b"1234"); tokenizer.write_bytes(b"abcd")
    holder, config = {}, {"mode": "synthetic"}
    def loader():
        if change == "during_load": weights.write_bytes(b"CHANGED")
        return SyntheticModel()
    kwargs = dict(kind="text", resources={"weights": weights, "tokenizer": tokenizer},
        implementation_files=[Path(__file__)], dependency_packages=["pydantic"], config=config,
        loader=loader, serving_getter=lambda: holder.get("model"), install=lambda m: holder.update(model=m))
    if change == "during_load":
        with pytest.raises(ValueError, match="during loading"):
            rm.LoadedModelLease.load(**kwargs)
        assert not holder
        return
    lease = rm.LoadedModelLease.load(**kwargs)
    assert lease.observe().resources["weights"] != H
    if change == "object": holder["model"] = SyntheticModel()
    if change == "resources": weights.write_bytes(b"CHANGED")
    if change == "config": config["mode"] = "replaced"
    with pytest.raises(ValueError, match="changed"):
        lease.observe()


@pytest.mark.parametrize("change", ["model", "tokenizer", "head_object", "client", "native_missing"])
def test_gateway_qualifies_actual_downstream_clients_and_fences_local_objects(tmp_path, monkeypatch, change):
    from vkm_corpus.api import production
    from vkm_corpus.parquet.atomic import sha256_of
    from vkm_corpus.retrieval.backends import TextBackend, VisualBackend
    from vkm_corpus.retrieval.gateway import GatewayConfig, Resources
    from vkm_corpus.retrieval import pins
    from vkm_corpus.update import remote_rerank as rr
    from vkm_corpus.update.remote_retrieval import capture_load_files
    monkeypatch.setattr(production, "serving_code_identity", lambda: H)
    monkeypatch.setattr(production, "serving_dependencies_identity", lambda: H)
    monkeypatch.setattr(rr, "own_process", lambda: {"pid": 1, "start_ticks": 2})
    paths = [tmp_path / n for n in ("text.json", "visual.json", "head.npz")]
    for p in paths: p.write_bytes(p.name.encode())
    cfg = GatewayConfig(token="gateway", qualified_identity=True, text_native_token="native-text",
        visual_native_token="native-visual", warmup=False, text_url="http://text.test", visual_url="http://visual.test",
        v35_tokenizer=str(paths[0]), m0_tokenizer=str(paths[1]), m0_head=str(paths[2]))
    text_hash, visual_hash, head_hash = [sha256_of(p) for p in paths]
    models = payloads()["rerank"]["models"]
    models["text"]["resources"] = {"weights": pins.TEXT["weights_sha256"], "tokenizer": text_hash}
    models["visual"]["resources"] = {"weights": cfg.m0_weights_sha256, "tokenizer": visual_hash,
                                     "mmproj": cfg.m0_mmproj_sha256}
    called = []
    def response(req):
        kind = "text" if req.url.host == "text.test" else "visual"
        assert req.url.path == "/identity" and req.method == "GET"
        assert req.headers["Authorization"] == "Bearer native-" + kind
        called.append(kind)
        return httpx.Response(200, json=models[kind])
    text, visual = TextBackend(cfg.text_url), VisualBackend(cfg.visual_url)
    text._client = httpx.AsyncClient(base_url=cfg.text_url, transport=httpx.MockTransport(response))
    visual._client = httpx.AsyncClient(base_url=cfg.visual_url, transport=httpx.MockTransport(response))
    res = Resources(text=text, visual=visual, v35_tokens=SimpleNamespace(sha256=text_hash),
        m0_tokens=SimpleNamespace(sha256=visual_hash), head=SimpleNamespace(sha256=head_hash),
        load_files=capture_load_files(paths))
    res.load_watch = SimpleNamespace(check=lambda: None)
    async def run():
        lease = await rr.RerankServiceLease.bind(cfg, res)
        assert (await lease.observe())["status"] == "READY"
        if change == "model": models["visual"]["resources"]["weights"] = "f" * 64
        if change == "tokenizer": paths[0].write_bytes(b"OTHER-SIZE")
        if change == "head_object": res.head = SimpleNamespace(sha256=head_hash)
        if change == "client": text._client.base_url = "http://sidecar.test"
        if change == "native_missing": models["text"] = {"status": "ready", "runtime": {"commit": H}}
        with pytest.raises(ValueError):
            await lease.observe()
        await text.aclose(); await visual.aclose()
    asyncio.run(run())
    assert called[:4] == ["text", "visual", "text", "visual"]


def test_qualified_gateway_refuses_legacy_before_starting_warmup(monkeypatch):
    from test_rerank_gateway import make_client
    from vkm_corpus.retrieval import gateway
    warmed = []
    async def warm(*args): warmed.append(True)
    monkeypatch.setattr(gateway, "_warmup_loop", warm)
    client, resources = make_client(qualified_identity=True)
    with pytest.raises(ValueError, match="load-time"):
        with client: pass
    assert warmed == [] and resources.text.calls == [] and resources.visual.calls == []
