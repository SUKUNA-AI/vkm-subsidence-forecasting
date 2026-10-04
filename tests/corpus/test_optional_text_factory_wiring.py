"""Actual factory load methods, synthetic HTTP/PG only; no Docker or models."""
import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest

from test_remote_services import H, environment
from test_optional_text_qualification import optional_status_fixture, visual_body
from vkm_corpus.api.service import ApiService
from vkm_corpus.api.production import read_tool_names
from vkm_corpus.contracts.access import AccessContext
from vkm_corpus.update import bootstrap_native, operator
from vkm_corpus.update.acceptance import AcceptancePlan, ToolProbe
from vkm_corpus.update.contracts import GenerationManifest
from vkm_corpus.update.remote_services import NativeServiceObserver
from vkm_corpus.update.runtime import BoundFile, RuntimeConfig
from vkm_corpus.update.serving import NativeServingBindings
from vkm_evidence.contracts import record_hash


class BoundActualService(Exception):
    """Stop immediately after native service binding, before graph/data reads."""


@pytest.mark.parametrize("factory", ["operator", "bootstrap"])
@pytest.mark.parametrize("change", [None, "missing_service", "changed_selector", "different_deps"])
def test_factories_pass_actual_service_into_optional_native_binding(tmp_path, monkeypatch, factory, change):
    from vkm_corpus.api import app as api
    deps, db, bodies, calls = environment(monkeypatch)
    bodies["rerank"] = visual_body()
    service = ApiService(deps)
    monkeypatch.setenv("VKM_RERANK_TEXT_BACKEND", "late")
    ref = lambda name: BoundFile(path=str(tmp_path / "authority" / name), sha256=H)
    runtime_ref, env_ref, plan_ref, gen_ref = [ref(n) for n in ("runtime", "env", "plan", "generation")]
    runtime = RuntimeConfig(runtime_root=str(tmp_path / "runtime"), originals_root=str(tmp_path / "originals"),
        policy=ref("policy"), qualification_root=str(tmp_path / "receipts"), expected_commit="a" * 40,
        memory_budget_gib=2, memory_reserve_gib=.5, worker_memory_gib=1, min_free_disk_gib=1,
        native_serving=ref("native"))
    pin, _ = optional_status_fixture()
    plan = AcceptancePlan(candidate=pin, scope="SHADOW_PRODUCTION", source_id="VKM-SRC-001",
        reader_principal="reader", denied_principal="denied", deployment_profile_sha256=H,
        isolation_attestation_sha256=H,
        tools={name: ToolProbe(arguments={}, expected_object_ids=() if name == "get_corpus_status" else ("VKM-SRC-001",))
               for name in read_tool_names()})
    manifest = GenerationManifest(code_commit=pin.code_commit, policy_sha256=pin.policy_sha256,
        components=pin.components, services=pin.services, acceptance_sha256=H)
    context = AccessContext(principal="reader", execution="LOCAL")
    release = SimpleNamespace(runtime_config_sha256=record_hash(runtime), code_sha256=pin.code_tree_sha256,
        dependencies_sha256=pin.dependencies_sha256, access_sha256=pin.access_config_sha256,
        mcp_read_principal_sha256=record_hash(context))
    env = {"VKM_UPDATE_RUNTIME_FILE": runtime_ref.path, "VKM_SOURCE_POLICY_FILE": runtime.policy.path}
    app = SimpleNamespace(state=SimpleNamespace(service=service,
        config=SimpleNamespace(access_contexts={"reader": context}),
        update_runtime=SimpleNamespace(config=runtime)))
    def build(**kwargs):
        assert kwargs == {"environ": env, "_defer_generation_binding": True}
        return app
    monkeypatch.setattr(api, "build_from_settings", build)
    data = {runtime_ref.path: runtime.model_dump(mode="json"), plan_ref.path: plan.model_dump(mode="json"),
            gen_ref.path: manifest.model_dump(mode="json")}
    module = operator if factory == "operator" else bootstrap_native
    monkeypatch.setattr(module, "operator_environment", lambda ref: env)
    monkeypatch.setattr(module, "bound_json", lambda ref: data[ref.path])
    captured = []
    async def bind(actual_deps, profile, *, api_service=None):
        assert actual_deps is deps and profile == runtime.native_serving
        captured.append(api_service)
        if change == "missing_service": api_service = None
        elif change == "changed_selector": api_service.text_rerank_backend = lambda: "late"
        elif change == "different_deps": api_service.deps = SimpleNamespace(**vars(deps))
        native = await NativeServiceObserver.bind(actual_deps, control_spec=db.spec(), api_service=api_service)
        assert captured == [service] and native.api_service is service
        assert "text_fallback_late" in (await native.observe())["RERANK"]["resources"]
        raise BoundActualService()
    monkeypatch.setattr(NativeServingBindings, "bind", bind)
    if factory == "operator":
        controller = object.__new__(operator.CoreOperator)
        controller.bindings = {"candidate": SimpleNamespace(environment=env_ref, runtime=runtime_ref,
            acceptance_plan=plan_ref, generation=gen_ref, baseline_registration=None)}
        controller.root = Path(runtime.runtime_root) / "served"
        controller.config = SimpleNamespace(policy=runtime.policy, protected_roots=(runtime.originals_root,),
            candidate_release_sha256="candidate", promotion_approval=None, deployment_profile_sha256=H)
        controller.units = SimpleNamespace(releases={"candidate": release},
            _compose=lambda _: {"services": {"api": {"environment": env}}})
        load = lambda: controller._load("candidate")
    else:
        controller = object.__new__(bootstrap_native.IsolatedBootstrap)
        controller.intent = SimpleNamespace(environment=env_ref, runtime=runtime_ref)
        controller.probes, controller.manifest = plan, manifest
        load = lambda: controller._load_private(release)
    async def run():
        try:
            with pytest.raises(BoundActualService if change is None else ValueError):
                await load()
            assert captured == [service]
        finally:
            await deps.rerank._client.aclose()
            deps.hybrid._embed._http.close()
    asyncio.run(run())
