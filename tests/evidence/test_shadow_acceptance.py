"""CPU contract tests: fake transcripts are always SYNTHETIC, never production proof."""
from __future__ import annotations

import asyncio
import copy
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from vkm_corpus.update import acceptance as A
from vkm_corpus.update.contracts import ComponentIdentity, ServiceIdentity
from vkm_corpus.api.app import ApiConfig
from vkm_corpus.contracts.access import AccessContext
from vkm_evidence.contracts import canonical_bytes, record_hash


def body(snapshot="candidate", object_id="VKM-SRC-001"):
    return {"ok": True, "meta": {"request_id": "probe", "api_version": "0.1.0",
            "canonical_snapshot_id": snapshot, "warnings": []},
        "item": {"envelope": {"object_id": object_id, "object_kind": "STATUS",
            "review_status": "AUTO_EXTRACTED_UNREVIEWED", "layer": "SERVICE", "payload_form": "NORMALIZED"},
            "record": {"synthetic": True}}}


def denial(code):
    return {"ok": False, "meta": {"request_id": "denied"}, "error": {"code": code, "message": "denied"}}


class FakeTransport:
    def __init__(self, pin):
        self.pin = pin
        self.names = sorted(A.read_tool_names())
        self.edit = lambda _name, value: value
        self.slow = False
        self.on_call = lambda: None

    async def list_tools(self, cursor=None):
        start = int(cursor or 0)
        names = self.names[start:start + 10]
        return {"tools": [{"name": n, "input_schema": {"type": "object"}, "read_only": True} for n in names],
                "next_cursor": str(start + 10) if start + 10 < len(self.names) else None}

    async def call_tool(self, name, arguments):
        if self.slow:
            await asyncio.sleep(1)
        self.on_call()
        value = body()
        if name == "get_corpus_status":
            value = {"ok": True, "meta": {"request_id": "probe", "api_version": "0.1.0"}, "status": {
                "api_version": "0.1.0", "canonical": {"snapshot_id": "candidate"}, "dependencies": {},
                "production_controls": {"profile": "shadow", "source_policy": "ENFORCED",
                    "generation": {"status": "READY", "generation": record_hash(self.pin)}}}}
        content = []
        if name == "get_page_image":
            import base64
            import io
            from PIL import Image
            image = io.BytesIO()
            Image.new("RGB", (2, 2)).save(image, format="PNG")
            content = [{"type": "image", "data": base64.b64encode(image.getvalue()).decode("ascii"), "mimeType": "image/png"}]
            value["image"] = {"width": 2, "height": 2}
        return self.edit(name, {"isError": False, "structuredContent": value, "content": content})

    async def api(self, principal, method, path, body=None):
        code = "UNAUTHORIZED" if principal is None else "FORBIDDEN" if principal == "denied" or method == "POST" else None
        return {"http_status": 401 if code == "UNAUTHORIZED" else 403 if code else 200,
                "body": denial(code) if code else globals()["body"]()}

    async def observe_services(self):
        return self.pin.service_map


@pytest.fixture()
def setup(tmp_path, monkeypatch):
    # These tests exercise transcript/identity validation, not repeated inventory
    # of the concurrently edited checkout. The real transport test below hashes
    # actual code; the drift test explicitly changes this observed code identity.
    monkeypatch.setattr(A, "serving_code_identity", lambda: "c" * 64)
    duckdb = tmp_path / "candidate.duckdb"
    duckdb.write_bytes(b"synthetic bytes, fake transport only")
    policy = tmp_path / "policy.json"
    policy.write_bytes(b"{}")
    config = ApiConfig(read_tokens={"secret-reader": "reader", "secret-denied": "denied"}, access_contexts={
        "reader": AccessContext(principal="reader", execution="LOCAL"),
        "denied": AccessContext(principal="denied", execution="CLOUD", granted_classes=frozenset())})
    digest = lambda p: A.hashlib.sha256(p.read_bytes()).hexdigest()
    components = tuple(ComponentIdentity(component=kind, revision="candidate", manifest_sha256="a" * 64,
        policy_sha256=digest(policy), built_from={}) for kind in ("DOCUMENT", "DUCKDB"))
    pin = A.CandidatePin(code_commit="b" * 40, code_tree_sha256=A.serving_code_identity(),
        dependencies_sha256=A.serving_dependencies_identity(), access_config_sha256=A.serving_access_identity(config),
        policy_sha256=digest(policy), duckdb_file_sha256=digest(duckdb), components=components)
    observed = copy.deepcopy(pin.component_map)
    fence = A.CandidateFence(pin, duckdb_path=duckdb, policy_path=policy, api_config=config,
                            observer=lambda: observed, production=False)
    root = tmp_path / "receipts"
    root.mkdir()
    reg = A.AcceptanceRegistrar(root, approved_plan_sha256="0" * 64)
    phases = []
    for name in A.DRILL_PHASES:
        phases.append(reg.store({"schema": "vkm-deployment-event/1", "phase": name, "request_key": "3" * 64,
            "parent_sha256": phases[-1] if phases else None, "detail": {
                "native_partial_sha256": "a" * 64, "bindings_partial_sha256": "a" * 64} if name == "FAULT_INJECTED" else {}}))
    drill = {"schema": "vkm-deployment-drill/1", "scope": "SYNTHETIC", "status": "PASS",
        "candidate_components_sha256": pin.components_sha256, "code_commit": pin.code_commit,
        "candidate_services_sha256": pin.services_sha256, "previous_services_sha256": record_hash([]),
        "policy_sha256": pin.policy_sha256, "code_tree_sha256": pin.code_tree_sha256,
        "dependencies_sha256": pin.dependencies_sha256, "access_config_sha256": pin.access_config_sha256,
        "deployment_profile_sha256": "d" * 64,
        "previous_components_sha256": "e" * 64, "native_before_sha256": "f" * 64,
        "native_after_restore_sha256": "f" * 64,
        "bindings_before_sha256": "b" * 64, "bindings_after_restore_sha256": "b" * 64,
        "transitions": [{"phase": name, "journal_sha256": digest} for name, digest in zip(A.DRILL_PHASES, phases)],
        "adapter_ids": ["synthetic-only"], "journal_receipts": phases, "checks": dict.fromkeys(A.DRILL_CHECKS, "PASS")}
    plan = A.AcceptancePlan(candidate=pin, scope="SYNTHETIC", source_id="VKM-SRC-001",
        tools={name: A.ToolProbe(arguments={}, expected_object_ids=() if name == "get_corpus_status" else ("VKM-SRC-001",))
               for name in A.read_tool_names()},
        reader_principal="reader", denied_principal="denied", deployment_profile_sha256="d" * 64,
        drill_receipt_sha256=reg.store(drill))
    reg.approved_plan_sha256 = plan.sha256
    return SimpleNamespace(plan=plan, fence=fence, reg=reg, transport=FakeTransport(pin), observed=observed, drill=drill)


def run(s, **kwargs):
    return asyncio.run(A.qualify_shadow(kwargs.get("plan", s.plan), transport=kwargs.get("transport", s.transport),
                                       fence=s.fence, registrar=s.reg))


def test_complete_fixed_contract_publishes_only_synthetic_receipt(setup):
    out = run(setup)
    assert out["status"] == "PASS", out
    assert out["scope"] == "SYNTHETIC"
    assert set(out["tools"]) == A.read_tool_names()
    assert out["checks"] == dict.fromkeys(A.SERVING_CHECKS, "PASS")
    stored = setup.reg.read(out["receipt_sha256"])
    assert b"secret-reader" not in stored and b"synthetic bytes" not in stored
    assert json.loads(stored)["raw_probe_receipts"]


@pytest.mark.parametrize("mode", ["generic", "error", "notrun", "empty", "wrong_snapshot", "warning", "is_error", "string_ok"])
def test_nonqualified_actual_tool_response_blocks(setup, mode):
    def change(name, value):
        if mode == "generic":
            return {"status": "PASS"}
        if mode == "error":
            value["structuredContent"] = denial("DEPENDENCY_UNAVAILABLE")
        if mode == "notrun":
            value["structuredContent"] = {"status": "NOT_RUN"}
        if mode == "empty":
            value["structuredContent"].pop("item")
        if mode == "wrong_snapshot":
            value["structuredContent"]["meta"]["canonical_snapshot_id"] = "old"
        if mode == "warning":
            value["structuredContent"]["meta"]["warnings"] = [{"code": "PARTIAL", "message": "partial"}]
        if mode == "is_error":
            value["isError"] = True
        if mode == "string_ok":
            value["structuredContent"]["ok"] = "true"
        return value
    setup.transport.edit = change
    out = run(setup)
    assert out["status"] == "FAIL" and "receipt_sha256" not in out
    assert json.loads(setup.reg.read(out["attempt_receipt_sha256"]))["status"] == "FAIL"


def test_missing_tool_and_unknown_tool_block(setup):
    setup.transport.names.pop()
    assert run(setup)["status"] == "FAIL"
    setup.transport.names.append("reprocess_source")
    assert run(setup)["status"] == "FAIL"


@pytest.mark.parametrize("fault", ["status_not_run", "image_missing", "image_invalid"])
def test_actual_status_and_binary_contracts_are_required(setup, fault):
    def change(name, value):
        if name == "get_corpus_status" and fault == "status_not_run":
            value["structuredContent"]["status"]["dependencies"]["control"] = {"status": "NOT_RUN"}
        if name == "get_page_image" and fault == "image_missing":
            value["content"] = []
        if name == "get_page_image" and fault == "image_invalid":
            value["content"][0]["data"] = "bm90IGFuIGltYWdl"
        return value
    setup.transport.edit = change
    assert run(setup)["status"] == "FAIL"


def test_timeout_is_failure_without_ack(setup):
    setup.transport.slow = True
    plan = setup.plan.model_copy(update={"call_timeout_seconds": 0.001})
    setup.reg.approved_plan_sha256 = plan.sha256
    assert run(setup, plan=plan)["status"] == "FAIL"


@pytest.mark.parametrize("what", ["policy", "native", "duckdb", "access"])
def test_mid_probe_drift_is_rejected(setup, what):
    def drift():
        if what == "policy":
            setup.fence.policy_path.write_bytes(b'{"changed":true}')
        elif what == "native":
            setup.observed["DUCKDB"]["revision"] = "different"
        elif what == "duckdb":
            setup.fence.duckdb_path.write_bytes(b"different")
        else:
            setup.fence.api_config.write_tokens["secret-reader"] = "reader"
    setup.transport.on_call = drift
    assert run(setup)["status"] == "FAIL"


def test_absent_transport_or_drill_is_not_run(setup):
    out = run(setup, transport=None)
    assert out["status"] == "NOT_RUN" and "receipt_sha256" not in out
    with pytest.raises(A.AcceptanceError, match="non-PASS"):
        setup.reg.register(json.loads(setup.reg.read(out["attempt_receipt_sha256"])), setup.plan)
    plan = setup.plan.model_copy(update={"drill_receipt_sha256": None})
    setup.reg.approved_plan_sha256 = plan.sha256
    assert run(setup, plan=plan)["status"] == "NOT_RUN"


def test_fake_transport_cannot_mint_production_scope(setup):
    plan = setup.plan.model_copy(update={"scope": "SHADOW_PRODUCTION", "isolation_attestation_sha256": "1" * 64})
    setup.reg.approved_plan_sha256 = plan.sha256
    with pytest.raises(A.AcceptanceError, match="real private"):
        run(setup, plan=plan)


@pytest.mark.parametrize("field,value", [("scope", "SHADOW_PRODUCTION"), ("status", "NOT_RUN"),
    ("native_after_restore_sha256", "2" * 64), ("deployment_profile_sha256", "2" * 64),
    ("candidate_components_sha256", "2" * 64), ("journal_receipts", [])])
def test_drill_is_independent_and_context_bound(setup, field, value):
    proof = {**setup.drill, field: value}
    plan = setup.plan.model_copy(update={"drill_receipt_sha256": setup.reg.store(proof)})
    setup.reg.approved_plan_sha256 = plan.sha256
    assert run(setup, plan=plan)["status"] == "FAIL"


def test_ack_loss_retry_reestablishes_durability_for_exact_receipt(setup, monkeypatch):
    from vkm_evidence import cli
    out = run(setup)
    proof = json.loads(setup.reg.read(out["receipt_sha256"]))
    original = cli._qualification_sync_directory
    monkeypatch.setattr(cli, "_qualification_sync_directory", lambda _: (_ for _ in ()).throw(OSError("lost ACK")))
    with pytest.raises(OSError):
        setup.reg.register(proof, setup.plan)
    monkeypatch.setattr(cli, "_qualification_sync_directory", original)
    assert setup.reg.register(proof, setup.plan) == out["receipt_sha256"]
    assert json.loads(setup.reg.read(out["receipt_sha256"])) == proof


def test_first_publication_ack_loss_never_claims_success_and_identical_retry_recovers(setup, monkeypatch):
    from vkm_evidence import cli
    value = {"schema": "synthetic-private-probe/1", "response_sha256": "8" * 64}
    wanted = record_hash(value)
    original = cli._qualification_sync_directory
    monkeypatch.setattr(cli, "_qualification_sync_directory", lambda _: (_ for _ in ()).throw(OSError("lost ACK")))
    with pytest.raises(OSError):
        setup.reg.store(value)
    # Visibility alone was not reported as durable publication.
    assert (setup.reg.root / (wanted + ".json")).exists()
    monkeypatch.setattr(cli, "_qualification_sync_directory", original)
    assert setup.reg.store(value) == wanted
    assert json.loads(setup.reg.read(wanted)) == value


def test_probe_inventory_must_match_installed_contract(setup):
    data = setup.plan.model_dump(mode="json")
    data["tools"].pop(next(iter(data["tools"])))
    with pytest.raises(ValueError, match="complete installed READ"):
        A.AcceptancePlan.model_validate(data)


@pytest.mark.parametrize("change", ["code", "repeat_probe", "missing_probe", "missing_drill_journal", "modified_probe"])
def test_registration_revalidates_bound_receipts_on_retry(setup, change):
    out = run(setup)
    assert out["status"] == "PASS", out
    proof = json.loads(setup.reg.read(out["receipt_sha256"]))
    if change == "code":
        proof["code_tree_sha256"] = "0" * 64
    elif change == "repeat_probe":
        proof["raw_probe_receipts"].append(proof["raw_probe_receipts"][0])
    elif change == "missing_probe":
        proof["raw_probe_receipts"].pop()
    elif change == "missing_drill_journal":
        (setup.reg.root / (setup.drill["journal_receipts"][0] + ".json")).unlink()
    else:
        (setup.reg.root / (proof["raw_probe_receipts"][0] + ".json")).write_bytes(b'{"status":"PASS"}')
    with pytest.raises((A.AcceptanceError, ValueError, FileNotFoundError)):
        setup.reg.register(proof, setup.plan)


def test_response_and_native_hash_budgets_block_before_admission(setup):
    plan = setup.plan.model_copy(update={"max_response_bytes": 3})
    setup.reg.approved_plan_sha256 = plan.sha256
    assert run(setup, plan=plan)["status"] == "FAIL"
    setup.reg.approved_plan_sha256 = setup.plan.sha256
    setup.fence.max_duckdb_bytes = 1
    assert run(setup)["status"] == "FAIL"


def test_actual_dependency_drift_blocks(setup, monkeypatch):
    monkeypatch.setattr(A, "serving_dependencies_identity", lambda: "0" * 64)
    assert run(setup)["status"] == "FAIL"


def test_observed_code_drift_blocks(setup, monkeypatch):
    monkeypatch.setattr(A, "serving_code_identity", lambda: "0" * 64)
    assert run(setup)["status"] == "FAIL"


def test_production_fence_installs_native_watch_before_hash_and_rejects_repeated_stat_mutation(setup, monkeypatch):
    from vkm_corpus.update import native_files
    from vkm_corpus.pipeline import context

    events = {"changed": False, "closed": False, "paths": None}
    class Watch:
        def __init__(self, paths):
            events["paths"] = paths
        def check(self):
            if events["changed"] or events["closed"]:
                raise ValueError("native file event invalidated lease")
        def close(self):
            events["closed"] = True
    monkeypatch.setattr(native_files, "NativeFileWatch", Watch)
    monkeypatch.setattr(A.platform, "system", lambda: "Linux")
    # Changing the emulated OS is part of this wiring test, not a real installed
    # dependency identity change. Actual dependency drift is covered separately.
    monkeypatch.setattr(A, "serving_dependencies_identity", lambda: setup.plan.candidate.dependencies_sha256)
    monkeypatch.setattr(context, "code_revision", lambda strict: (setup.plan.candidate.code_commit, False))
    setup.fence.production = True
    original = A._qualify_duckdb_file
    def qualify(*args):
        assert events["paths"] == [setup.fence.duckdb_path, setup.fence.policy_path]
        return original(*args)
    monkeypatch.setattr(A, "_qualify_duckdb_file", qualify)
    with pytest.raises(A.AcceptanceError, match="not installed"):
        setup.fence.check()
    setup.fence.check(full=True)
    signature = setup.fence.signature
    old = setup.fence.duckdb_path.read_bytes()
    setup.fence.duckdb_path.write_bytes(bytes([old[0] ^ 1]) + old[1:])
    assert A.hashlib.sha256(setup.fence.duckdb_path.read_bytes()).hexdigest() != setup.plan.candidate.duckdb_file_sha256
    monkeypatch.setattr(A, "_duckdb_file_signature", lambda _: signature)
    events["changed"] = True
    with pytest.raises(ValueError, match="native file event"):
        setup.fence.check()
    setup.fence.close()
    assert events["closed"]
    with pytest.raises(A.AcceptanceError, match="closed"):
        setup.fence.check(full=True)


def test_production_fence_never_falls_back_when_native_watch_is_unavailable(setup, monkeypatch):
    from vkm_corpus.update import native_files

    def unavailable(paths):
        raise ValueError("native file filesystem is not qualified")
    monkeypatch.setattr(native_files, "NativeFileWatch", unavailable)
    monkeypatch.setattr(A.platform, "system", lambda: "Linux")
    setup.fence.production = True
    with pytest.raises(ValueError, match="not qualified"):
        setup.fence.check(full=True)
    assert setup.fence.signature is None


def test_runtime_service_drift_blocks_receipt_even_for_valid_payload(setup):
    async def changed():
        return {"CONTROL": {"status": "PASS"}}
    setup.transport.observe_services = changed
    out = run(setup)
    assert out["status"] == "FAIL" and "receipt_sha256" not in out


@pytest.mark.parametrize("fault", ["services", "policy", "budget"])
def test_private_asgi_never_releases_payload_before_post_request_identity_check(setup, fault):
    setup.fence.check(full=True)
    transport = object.__new__(A.PrivateASGIProbeTransport)
    transport.fence, transport.plan = setup.fence, setup.plan
    state = {"changed": False}
    class Observer:
        async def observe(self):
            return {"CONTROL": {"status": "PASS"}} if state["changed"] and fault == "services" else {}
    transport._service_observer = Observer()
    secret = b"PRIVATE PAYLOAD MUST NOT ESCAPE"
    async def app(scope, receive, send):
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": secret})
        state["changed"] = True
        if fault == "policy":
            setup.fence.policy_path.write_bytes(b"changed")
    transport._api_app = app
    if fault == "budget":
        transport.plan = setup.plan.model_copy(update={"max_response_bytes": 1})
    sent = []
    async def receive():
        return {"type": "http.request", "body": b""}
    async def send(value):
        sent.append(value)
    asyncio.run(transport._buffered_app({"type": "http"}, receive, send))
    assert sent[0]["status"] == 503
    assert all(secret not in item.get("body", b"") for item in sent)


def test_registrar_accepts_actual_durable_deployment_drill_and_hash_linked_events(setup, tmp_path):
    from test_deployment_lifecycle import rig

    controller, previous, candidate, state, calls, barrier = rig(tmp_path)
    drill = controller.rehearse_failure(candidate, "acceptance-drill", fail_after_adapter="document")
    for digest in drill["journal_receipts"]:
        event = json.loads((controller.root / "journals" / (digest + ".json")).read_bytes())
        assert setup.reg.store(event) == digest
    pin = setup.plan.candidate.model_copy(update={key: drill[key] for key in (
        "code_commit", "code_tree_sha256", "dependencies_sha256", "access_config_sha256", "policy_sha256")})
    pin = A.CandidatePin.model_validate({**pin.model_dump(mode="json"),
        "components": [c.model_dump(mode="json") for c in candidate.components]})
    plan = setup.plan.model_copy(update={"candidate": pin, "deployment_profile_sha256": drill["deployment_profile_sha256"],
                                        "drill_receipt_sha256": setup.reg.store(drill)})
    setup.reg.approved_plan_sha256 = plan.sha256
    assert setup.reg.require_drill(plan) == drill
    assert state == {c.component: c.model_dump(mode="json") for c in previous.components}
    assert not barrier.status()["paused"] and any(call[0] == "apply" for call in calls)


def test_real_private_transport_runs_actual_api_and_mcp_without_socket(tmp_path):
    from vkm_corpus.api.fixtures import synthetic_service
    from vkm_corpus.contracts.policy_store import SourcePolicyStore

    service, canon, _ = synthetic_service(tmp_path / "data")
    policy_path = tmp_path / "policy.json"
    policy_path.write_text(json.dumps({"schema": "vkm-source-policy/1", "policies": {"VKM-SRC-001": {
        "access_class": "PUBLIC", "policy_version": "test", "authority": "synthetic"}}}))
    # Actual policy input format is validated by SourcePolicyStore on each read.
    service.deps.access_policy = SourcePolicyStore(policy_path, lambda: ["VKM-SRC-001"])
    config = ApiConfig(read_tokens={"reader-token": "reader", "denied-token": "denied"}, access_contexts={
        "reader": AccessContext(principal="reader", execution="LOCAL"),
        "denied": AccessContext(principal="denied", execution="CLOUD", granted_classes=frozenset())})
    digest = lambda p: A.hashlib.sha256(Path(p).read_bytes()).hexdigest()
    components = tuple(ComponentIdentity(component=k, revision=canon.snapshot_id, manifest_sha256="a" * 64,
        policy_sha256=digest(policy_path), built_from={}) for k in ("DOCUMENT", "DUCKDB", "SEARCH", "GRAPH"))
    services = tuple(ServiceIdentity(service=name, instance_sha256="a" * 64, code_sha256="b" * 64,
        dependencies_sha256="c" * 64, config_sha256="d" * 64, endpoint_sha256="e" * 64, runtime_sha256="f" * 64)
        for name in ("RERANK", "CONTROL"))
    pin = A.CandidatePin(code_commit="a" * 40, code_tree_sha256=A.serving_code_identity(),
        dependencies_sha256=A.serving_dependencies_identity(), access_config_sha256=A.serving_access_identity(config),
        policy_sha256=digest(policy_path), duckdb_file_sha256=digest(service.canon.path), components=components, services=services)
    fence = A.CandidateFence(pin, duckdb_path=service.canon.path, policy_path=policy_path, api_config=config,
                            observer=lambda: pin.component_map, production=False)
    plan = A.AcceptancePlan(candidate=pin, scope="SYNTHETIC", source_id="VKM-SRC-001",
        tools={name: A.ToolProbe(arguments={}, expected_object_ids=() if name == "get_corpus_status" else ("VKM-SRC-001",))
               for name in A.read_tool_names()},
        reader_principal="reader", denied_principal="denied", deployment_profile_sha256="a" * 64,
        control_spec={"schema_sha256": "a" * 64, "workers": [{"host_role": "CORE", "kind": "PUBLISHER"}]})
    class SyntheticServices:
        async def observe(self):
            return pin.service_map
    from vkm_corpus.update.barrier import ReceiverBarrier
    barrier = ReceiverBarrier("existing-live")
    barrier.pause("existing-maintenance")
    service.deps.admission_barrier = barrier
    async def exercise():
        async with A.PrivateASGIProbeTransport(service, config, fence, plan,
                                              synthetic_service_observer=SyntheticServices()) as transport:
            assert transport._service.deps.admission_barrier is None
            assert service.deps.admission_barrier is barrier and barrier.status()["paused"]
            tools = await transport.list_tools()
            assert {t["name"] for t in tools["tools"]} == A.read_tool_names()
            denied = await transport.api(None, "GET", "/v1/source/VKM-SRC-001")
            assert denied["http_status"] == 401
            answer = await transport.call_tool("get_source", {"source_id": "VKM-SRC-001"})
            assert "structuredContent" in answer
            assert answer["structuredContent"]["ok"] is True
            denied = await transport.api("denied", "GET", "/v1/source/VKM-SRC-001")
            assert denied["http_status"] == 403 and denied["body"]["error"]["code"] == "FORBIDDEN"
            denied = await transport.api("reader", "POST", "/v1/reprocess/source", {
                "target_id": "VKM-SRC-001", "reason": "synthetic read token denial"})
            assert denied["http_status"] == 403
            status = await transport.call_tool("get_corpus_status", {})
            A._tool_response("get_corpus_status", plan.tools["get_corpus_status"], status, pin)
    asyncio.run(exercise())
