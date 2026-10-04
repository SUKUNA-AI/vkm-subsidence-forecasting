"""Optional disabled text is not a missing proof or an enabled model.

All clients and resources are synthetic; no model, service or GPU execution.
"""
import asyncio
import copy
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from vkm_corpus.retrieval import gateway
from vkm_corpus.api.service import ApiService
from vkm_corpus.update import remote_rerank, remote_services
from vkm_corpus.update.remote_retrieval import capture_load_files
from vkm_corpus.parquet.atomic import sha256_of
from test_remote_services import H, environment, payloads


PROFILE = "VISUAL_ONLY_TEXT_DISABLED_V2"


def visual_body():
    value = payloads()["rerank"]
    value.update(schema="vkm-rerank-native-identity/2", profile=PROFILE,
                 text_route="DISABLED_UNAVAILABLE")
    del value["models"]["text"]
    del value["resources"]["text_tokenizer"]
    return value


def retrieval():
    return remote_services.retrieval_identity(payloads()["retrieval"], H)


def test_explicit_profile_preserves_legacy_default_and_rejects_unknown():
    assert gateway.GatewayConfig().native_profile == "BOTH_NATIVE_V1"
    assert gateway.GatewayConfig.from_env({"VKM_RERANK_NATIVE_PROFILE": PROFILE}).text_disabled
    with pytest.raises(ValueError):
        gateway.GatewayConfig(native_profile="omit-text")


def test_disabled_gateway_route_is_unavailable_without_body_or_text_calls():
    from test_rerank_gateway import make_client, _hdr
    client, resources = make_client(native_profile=PROFILE)
    async def forbidden(*args, **kwargs):
        raise AssertionError("disabled text must not be contacted, even by health/status")
    resources.text.health = resources.text.identity = forbidden
    with client:
        # The fixed rejection precedes JSON parsing and any native/text call.
        response = client.post("/v1/rerank/text", content=b"not json", headers=_hdr())
        assert response.status_code == 503
        assert response.json()["error"]["code"] == "RERANK_BACKEND_UNAVAILABLE"
        assert resources.text.calls == []
        health = client.get("/health").json()
        assert health["backends"]["text"] == "unavailable"
        status = client.get("/status", headers=_hdr()).json()
        assert status["backends"]["text"]["status"] == "unavailable"
        assert status["backends"]["text"]["detail"] == "DISABLED_BY_QUALIFIED_PROFILE"
        assert resources.text.calls == []


def test_direct_disabled_text_flow_is_rejected_before_using_resources():
    cfg = gateway.GatewayConfig(native_profile=PROFILE)
    with pytest.raises(gateway.RerankError) as caught:
        asyncio.run(gateway.rerank_text(None, None, cfg, "synthetic"))
    assert caught.value.code == "RERANK_BACKEND_UNAVAILABLE"


def test_visual_only_identity_binds_same_qualified_late_service():
    observed = retrieval()
    identity = remote_services.rerank_identity(visual_body(), H, text_backend="late", retrieval=observed)
    assert identity.capabilities == ("rerank_visual",)
    assert "text_model_weights" not in identity.resources
    assert identity.resources["text_fallback_late"]
    changed = observed.model_copy(update={"instance_sha256": "b"*64})
    assert identity.runtime_sha256 != remote_services.rerank_identity(
        visual_body(), H, text_backend="late", retrieval=changed).runtime_sha256


@pytest.mark.parametrize("change", ["missing_retrieval", "wrong_route", "missing_late_scores", "wrong_service",
    "wrong_profile", "enabled_text", "text_model", "text_resource", "missing_visual", "v1_omission", "extra"])
def test_optional_identity_cannot_hide_a_missing_required_model(change):
    body, native, route = visual_body(), retrieval(), "late"
    if change == "missing_retrieval": native = None
    elif change == "wrong_route": route = "gateway"
    elif change == "missing_late_scores": native = native.model_copy(update={"capabilities": ("dense_query",)})
    elif change == "wrong_service": native = native.model_copy(update={"service": "CONTROL"})
    elif change == "wrong_profile": body["profile"] = "BOTH_NATIVE_V1"
    elif change == "enabled_text": body["text_route"] = "READY"
    elif change == "text_model": body["models"]["text"] = payloads()["rerank"]["models"]["text"]
    elif change == "text_resource": body["resources"]["text_tokenizer"] = H
    elif change == "missing_visual": body["models"] = {}
    elif change == "v1_omission":
        body["schema"] = "vkm-rerank-native-identity/1"
        del body["profile"]; del body["text_route"]
    else: body["allow_missing"] = True
    with pytest.raises(ValueError):
        remote_services.rerank_identity(body, H, text_backend=route, retrieval=native)


def test_strict_v1_still_requires_both_proofs():
    value = payloads()["rerank"]
    assert remote_services.rerank_identity(value, H).capabilities == ("rerank_text", "rerank_visual")
    del value["models"]["text"]
    with pytest.raises(ValueError): remote_services.rerank_identity(value, H)


@pytest.mark.parametrize("change", ["route", "retrieval", "profile"])
def test_native_observer_joins_actual_api_route_and_rejects_drift(monkeypatch, change):
    deps, db, bodies, calls = environment(monkeypatch)
    bodies["rerank"] = visual_body()
    monkeypatch.setenv("VKM_RERANK_TEXT_BACKEND", "late")
    async def run():
        try:
            observer = await remote_services.NativeServiceObserver.bind(deps, control_spec=db.spec(), api_service=ApiService(deps))
            identities = await observer.observe()
            assert identities["RERANK"]["capabilities"] == ["rerank_visual"]
            if change == "route": monkeypatch.setenv("VKM_RERANK_TEXT_BACKEND", "gateway")
            elif change == "retrieval": bodies["retrieval"]["models"]["late"]["process"]["start_ticks"] += 1
            else: bodies["rerank"] = payloads()["rerank"]
            with pytest.raises(ValueError): await observer.observe()
        finally:
            await deps.rerank._client.aclose(); deps.hybrid._embed._http.close()
    asyncio.run(run())


def test_native_observer_rejects_visual_only_for_actual_gateway_text_route(monkeypatch):
    deps, db, bodies, _ = environment(monkeypatch)
    bodies["rerank"] = visual_body()
    monkeypatch.setenv("VKM_RERANK_TEXT_BACKEND", "gateway")
    async def run():
        try:
            with pytest.raises(ValueError):
                await remote_services.NativeServiceObserver.bind(deps, control_spec=db.spec(), api_service=ApiService(deps))
        finally:
            await deps.rerank._client.aclose(); deps.hybrid._embed._http.close()
    asyncio.run(run())


@pytest.mark.parametrize("change", ["missing", "other_deps", "callback", "subclass", "changed_method", "changed_deps"])
def test_visual_only_requires_exact_actual_api_service_and_bound_selector(monkeypatch, change):
    deps, db, bodies, _ = environment(monkeypatch)
    bodies["rerank"] = visual_body()
    monkeypatch.setenv("VKM_RERANK_TEXT_BACKEND", "late")
    service = ApiService(deps)
    if change == "missing": service = None
    elif change == "other_deps": service = ApiService(copy.copy(deps))
    elif change == "callback": service.text_rerank_backend = lambda: "late"
    elif change == "subclass":
        class SuppliedService(ApiService): pass
        service = SuppliedService(deps)
    async def run():
        try:
            if change in {"changed_method", "changed_deps"}:
                observer = await remote_services.NativeServiceObserver.bind(deps, control_spec=db.spec(), api_service=service)
                if change == "changed_method": service.text_rerank_backend = lambda: "late"
                else: service.deps = copy.copy(deps)
                with pytest.raises(ValueError): await observer.observe()
            else:
                with pytest.raises(ValueError):
                    await remote_services.NativeServiceObserver.bind(deps, control_spec=db.spec(), api_service=service)
        finally:
            await deps.rerank._client.aclose(); deps.hybrid._embed._http.close()
    asyncio.run(run())


def test_configuration_summary_never_prints_native_credentials():
    cfg = gateway.GatewayConfig(native_profile=PROFILE, token="synthetic-main-secret",
        text_native_token="synthetic-text-secret", visual_native_token="synthetic-visual-secret")
    assert all(gateway.config_summary(cfg)[key] == "<set>"
               for key in ("token", "text_native_token", "visual_native_token"))


def test_visual_only_resource_loader_does_not_create_text_client_or_load_tokenizer(tmp_path, monkeypatch):
    from vkm_corpus.retrieval.tokens import TokenCounter
    from vkm_corpus.retrieval.m0_head import M0Head
    paths = [tmp_path/name for name in ("visual-tokenizer", "visual-head")]
    for path in paths: path.write_bytes(b"SYNTHETIC resource")
    calls = []
    def forbidden(*args, **kwargs): raise AssertionError("disabled text client constructed")
    def token(path, expected):
        calls.append(path)
        return SimpleNamespace(sha256=expected)
    monkeypatch.setattr(gateway, "TextBackend", forbidden)
    monkeypatch.setattr(TokenCounter, "from_file", token)
    monkeypatch.setattr(M0Head, "from_npz", lambda path, expected: SimpleNamespace(sha256=expected))
    cfg = gateway.GatewayConfig(native_profile=PROFILE, m0_tokenizer=str(paths[0]), m0_head=str(paths[1]))
    resources = gateway.load_resources(cfg)
    try:
        assert resources.text is resources.v35_tokens is None
        assert calls == [str(paths[0])]
        assert set(resources.load_files) == {str(path) for path in paths}
    finally:
        if resources.load_watch is not None: resources.load_watch.close()


def test_qualified_gateway_buffers_response_until_after_native_recheck(monkeypatch):
    from test_rerank_gateway import make_client, _hdr, _text_body
    events = []
    async def observe():
        events.append("observe")
        if len(events) == 2: raise ValueError("synthetic native profile drift during inference")
        return {"status": "READY"}
    async def bind(*args): return SimpleNamespace(observe=observe)
    monkeypatch.setattr(remote_rerank.RerankServiceLease, "bind", bind)
    client, resources = make_client(qualified_identity=True)
    with client:
        response = client.post("/v1/rerank/text", json=_text_body(), headers=_hdr())
        assert len(resources.text.calls) == 1 and events == ["observe", "observe"]
        assert response.status_code == 503 and "scores" not in response.json()


def lease_fixture(tmp_path, monkeypatch):
    from vkm_corpus.retrieval.backends import VisualBackend
    from vkm_corpus.update import service_identity
    paths = [tmp_path/name for name in ("visual.json", "head.npz")]
    for path in paths: path.write_bytes(b"SYNTHETIC " + path.name.encode())
    hashes = [sha256_of(path) for path in paths]
    cfg = gateway.GatewayConfig(native_profile=PROFILE, qualified_identity=True, token="gateway",
        visual_native_token="visual-token", visual_url="http://visual.test", warmup=False,
        m0_tokenizer=str(paths[0]), m0_head=str(paths[1]))
    proof = payloads()["rerank"]["models"]["visual"]
    proof["resources"] = {"weights": cfg.m0_weights_sha256, "tokenizer": cfg.m0_weights_sha256,
                          "mmproj": cfg.m0_mmproj_sha256}
    calls = []
    def transport(req):
        assert req.url.host == "visual.test" and req.url.path == "/identity"
        assert req.headers["Authorization"] == "Bearer visual-token"
        calls.append(req.url.path)
        return httpx.Response(200, json=proof)
    visual = VisualBackend(cfg.visual_url)
    visual._client = httpx.AsyncClient(base_url=cfg.visual_url, transport=httpx.MockTransport(transport))
    res = gateway.Resources(text=None, visual=visual, v35_tokens=None,
        m0_tokens=SimpleNamespace(sha256=hashes[0]), head=SimpleNamespace(sha256=hashes[1]),
        load_files=capture_load_files(paths), load_watch=SimpleNamespace(check=lambda: None))
    monkeypatch.setattr(remote_rerank, "own_process", lambda: {"pid": 1, "start_ticks": 2})
    monkeypatch.setattr(service_identity, "service_code_identity", lambda profile: H)
    monkeypatch.setattr(service_identity, "service_dependencies_identity", lambda profile: H)
    return cfg, res, calls


@pytest.mark.parametrize("change", [None, "text_object", "text_tokenizer", "profile", "visual_client"])
def test_visual_lease_never_contacts_disabled_text_and_pins_profile(tmp_path, monkeypatch, change):
    cfg, res, calls = lease_fixture(tmp_path, monkeypatch)
    async def run():
        try:
            lease = await remote_rerank.RerankServiceLease.bind(cfg, res)
            value = await lease.observe()
            assert value["schema"] == "vkm-rerank-native-identity/2" and value["profile"] == PROFILE
            assert value["text_route"] == "DISABLED_UNAVAILABLE" and set(value["models"]) == {"visual"}
            assert set(value["resources"]) == {"visual_tokenizer", "visual_head"}
            if change == "text_object": res.text = object()
            elif change == "text_tokenizer": res.v35_tokens = object()
            elif change == "profile": object.__setattr__(cfg, "native_profile", "BOTH_NATIVE_V1")
            elif change == "visual_client": res.visual._client.base_url = "http://other.test"
            if change:
                with pytest.raises(ValueError): await lease.observe()
            assert len(calls) == 2
        finally: await res.visual.aclose()
    asyncio.run(run())


def test_visual_profile_refuses_live_text_object_even_before_probe(tmp_path, monkeypatch):
    cfg, res, calls = lease_fixture(tmp_path, monkeypatch)
    res.text = object()
    async def run():
        try:
            with pytest.raises(ValueError): await remote_rerank.RerankServiceLease.bind(cfg, res)
            assert calls == []
        finally: await res.visual.aclose()
    asyncio.run(run())


def optional_status_fixture():
    from vkm_corpus.update.acceptance import CandidatePin
    from vkm_corpus.update.contracts import ComponentIdentity
    from vkm_evidence.contracts import record_hash
    native = retrieval()
    rerank = remote_services.rerank_identity(visual_body(), H, text_backend="late", retrieval=native)
    pin = CandidatePin(code_commit="a" * 40, code_tree_sha256=H, dependencies_sha256=H,
        access_config_sha256=H, policy_sha256=H, duckdb_file_sha256=H,
        components=tuple(ComponentIdentity(component=kind, revision="synthetic", manifest_sha256=H,
            policy_sha256=H, built_from={}) for kind in ("DOCUMENT", "DUCKDB")), services=(native, rerank))
    body = {"ok": True, "meta": {"api_version": "0.1.0", "request_id": "synthetic-status"}, "status": {
        "api_version": "0.1.0", "canonical": {"snapshot_id": pin.snapshot},
        "production_controls": {"profile": "shadow", "source_policy": "ENFORCED",
            "generation": {"status": "READY", "generation": record_hash(pin)}},
        "dependencies": {"rerank_text_backend": "late", "rerank": {"available": True, "backends": {
            "text": {"kind": "text", "status": "unavailable"}, "visual": {"kind": "visual", "status": "ready"}}}}}}
    return pin, body


def test_status_accepts_only_bound_intentionally_disabled_text():
    from vkm_corpus.update.acceptance import _corpus_status
    pin, body = optional_status_fixture()
    _corpus_status(body, pin)


@pytest.mark.parametrize("change", ["no_fallback", "fallback_drift", "retrieval_drift", "no_retrieval",
    "no_late_scores", "no_late_pack", "text_model", "text_tokenizer", "v1", "other_capability",
    "wrong_route", "text_error", "text_available_false", "text_not_run", "text_state_unavailable",
    "text_nested_error", "text_mixed_snapshot", "visual_unavailable", "other_unavailable", "canonical_mixed"])
def test_status_disabled_text_exception_cannot_hide_other_failure(change):
    from vkm_corpus.update.acceptance import AcceptanceError, _corpus_status
    from vkm_evidence.contracts import record_hash
    pin, body = optional_status_fixture()
    native, rerank = pin.services
    resources = dict(rerank.resources)
    if change == "no_fallback": resources.pop("text_fallback_late")
    elif change == "fallback_drift": resources["text_fallback_late"] = "f" * 64
    elif change == "retrieval_drift": native = native.model_copy(update={"instance_sha256": "f" * 64})
    elif change == "no_late_scores": native = native.model_copy(update={"capabilities": ("dense_query",)})
    elif change == "no_late_pack":
        retained = dict(native.resources); retained.pop("LATE_PACK")
        native = native.model_copy(update={"resources": retained})
    elif change == "text_model": resources["text_model_weights"] = H
    elif change == "text_tokenizer": resources["text_tokenizer"] = H
    elif change == "v1": rerank = rerank.model_copy(update={"capabilities": ("rerank_visual", "rerank_text")})
    elif change == "other_capability": rerank = rerank.model_copy(update={"capabilities": ("rerank_visual", "other")})
    rerank = rerank.model_copy(update={"resources": resources})
    pin = pin.model_copy(update={"services": (rerank,) if change == "no_retrieval" else (native, rerank)})
    body["status"]["production_controls"]["generation"]["generation"] = record_hash(pin)
    deps = body["status"]["dependencies"]
    text = deps["rerank"]["backends"]["text"]
    if change == "wrong_route": deps["rerank_text_backend"] = "gateway"
    elif change == "text_error": text["error"] = "failed"
    elif change == "text_available_false": text["available"] = False
    elif change == "text_not_run": text["status"] = "NOT_RUN"
    elif change == "text_state_unavailable": text["state"] = "UNAVAILABLE"
    elif change == "text_nested_error": text["nested"] = {"status": "UNAVAILABLE"}
    elif change == "text_mixed_snapshot": text["matches_canonical_snapshot"] = False
    elif change == "visual_unavailable": deps["rerank"]["backends"]["visual"]["status"] = "unavailable"
    elif change == "other_unavailable": deps["other"] = {"status": "UNAVAILABLE"}
    elif change == "canonical_mixed": body["status"]["canonical"]["matches_canonical_snapshot"] = False
    with pytest.raises(AcceptanceError): _corpus_status(body, pin)
