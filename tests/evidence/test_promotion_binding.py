"""Synthetic immutable mapping tests; no native runtime or live claim is minted."""
from __future__ import annotations

import asyncio
import hashlib
from pathlib import Path
from types import SimpleNamespace

import pytest

from vkm_corpus.update import acceptance as A
from vkm_corpus.update import promotion as P
from vkm_corpus.update.contracts import ComponentIdentity, GenerationManifest, ServiceIdentity
from vkm_corpus.update.generation import GenerationUnavailable
from vkm_corpus.update.operator import CoreOperator, CoreOperatorConfig, OperatorRelease
from vkm_corpus.update.operator_units import ComposeRelease, UnitControlConfig, UnitPin
from vkm_corpus.update.runtime import BoundFile, RuntimeConfig
from vkm_corpus.search.mappings import INDEX_TYPES
from vkm_evidence.contracts import canonical_bytes, record_hash


def write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    raw = canonical_bytes(value)
    path.write_bytes(raw)
    path.chmod(0o600)
    return BoundFile(path=str(path), sha256=hashlib.sha256(raw).hexdigest())


def fixture(tmp_path, *, mutate_live=None):
    base = tmp_path
    shared = base / "shared"
    shared.mkdir(exist_ok=True)
    binaries = {}
    for name in ("docker", "docker-compose"):
        path = shared / name
        path.write_bytes(b"synthetic executable bytes; never executed")
        binaries[name] = BoundFile(path=str(path), sha256=hashlib.sha256(path.read_bytes()).hexdigest())
    saved = {}
    for label in ("shadow", "live"):
        authority = base / label / "authority"
        runtime_root = base / label / "runtime"
        control = runtime_root / "served"
        control.mkdir(parents=True)
        (control / "admission.lock").touch()
        qualification = base / label / "qualification"
        qualification.mkdir()
        data = base / label / "data"
        roots = {kind: data / kind for kind in ("originals", "canonical", "evidence")}
        for path in roots.values():
            path.mkdir(parents=True)
        docs = {}
        def document(name, value, docs=docs, authority=authority):
            ref = write(authority / (name + ".json"), value)
            docs[name] = ref
            return ref
        policy = document("policy", {"synthetic": True})
        placeholder = document("placeholder", {"synthetic": True, "kind": "temporary-preparation"})
        # Source-native links are retained and read through the closure.
        links = {name: document(name, {"synthetic": True, "name": name})
                 for name in ("graph", "late-policy", "late-qualification")}
        project = "vkm-core-shadow" if label == "shadow" else "vkm-core"
        unit_releases, releases, runtimes, pins = [], [], {}, {}
        for name in ("candidate", "previous"):
            native = document(name + ".native", {
                "schema_version": "vkm-native-serving-profile/1",
                "search": {"prefix": "fixture", "indices": {kind: "fixture-" + kind + "-m1-locked" for kind in INDEX_TYPES}},
                "graph_manifest": links["graph"].model_dump(mode="json"),
                "late_policy_binding": links["late-policy"].model_dump(mode="json"),
                "late_qualification": links["late-qualification"].model_dump(mode="json"),
                "control": {"schema_sha256": "c" * 64, "workers": [{"host_role": "CORE", "kind": "PUBLISHER"}]},
                "max_search_documents": 10, "search_timeout_seconds": 1})
            runtime = RuntimeConfig(runtime_root=str(runtime_root), originals_root=str(roots["originals"]),
                canonical_root=str(roots["canonical"]), evidence_root=str(roots["evidence"]), policy=policy,
                qualification_root=str(qualification), expected_commit="a" * 40, native_serving=native,
                memory_budget_gib=4, memory_reserve_gib=1, worker_memory_gib=1, min_free_disk_gib=1)
            runtimes[name] = runtime
            runtime_ref = document(name + ".runtime", runtime)
            env = {"VKM_API_PROFILE": "production", "VKM_DATA_ROLE": "canonical",
                   "VKM_UPDATE_RUNTIME_FILE": runtime_ref.path, "VKM_SOURCE_POLICY_FILE": policy.path}
            compose = {"name": project, "services": {
                "api": {"image": "fixture-api@sha256:" + "1" * 64,
                        "command": ["api", "serve", "--host", "0.0.0.0", "--port", "8000"], "environment": env},
                "mcp": {"image": "fixture-mcp@sha256:" + "2" * 64,
                        "command": ["mcp", "serve", "--kind", "read", "--host", "0.0.0.0", "--port", "8765"],
                        "environment": {"VKM_API_URL": "http://api:8000", "VKM_API_TOKEN_FILE": str(authority / "read-token"),
                            "VKM_MCP_TOKEN_FILE": str(authority / "mcp-token"),
                            "VKM_DEPLOYMENT_TOKEN_FILE": str(authority / "operator-token"),
                            "VKM_DEPLOYMENT_GATE_FILE": str(control / "admission.lock")}}}}
            for spec in compose["services"].values():
                spec.update(read_only=True, cap_drop=["ALL"], mem_limit=1024**3, cpus=1,
                    volumes=[{"type": "bind", "source": str(control), "target": str(control), "read_only": True,
                              "bind": {"create_host_path": False}},
                             {"type": "bind", "source": str(control / "admission.lock"),
                              "target": str(control / "admission.lock"), "read_only": False,
                              "bind": {"create_host_path": False}}])
            if label == "live" and mutate_live:
                mutate_live(name, compose)
            env_ref = document(name + ".environment", env)
            unit_pins = {}
            for receiver, image in (("api", "1"), ("mcp", "2")):
                effective = {"config": {"Labels": {"com.docker.compose.project": project,
                            "com.docker.compose.service": receiver, "com.docker.compose.container-number": "1"},
                            "Env": ["SYNTHETIC=true"], "User": "fixture"},
                            "host_config": {"Memory": 1024**3}, "mounts": [], "networks": ["fixture-shared"]}
                document(name + "." + receiver + ".effective", effective)
                unit_pins[receiver] = UnitPin(image_id="sha256:" + image * 64, config_sha256=record_hash(effective))
            release = ComposeRelease(compose=document(name + ".compose", compose), project=project, units=unit_pins,
                runtime_config_sha256=record_hash(runtime), code_sha256="c" * 64,
                dependencies_sha256="d" * 64, access_sha256="e" * 64, mcp_read_principal_sha256="8" * 64)
            unit_releases.append(release)
            releases.append(OperatorRelease(receiver_release_sha256=release.sha256, environment=env_ref,
                            runtime=runtime_ref, generation=placeholder, acceptance_plan=placeholder))
            components = tuple(ComponentIdentity(component=kind, revision=name, manifest_sha256=record_hash(name),
                policy_sha256=policy.sha256, built_from={}) for kind in ("DOCUMENT", "DUCKDB"))
            service = ServiceIdentity(service="RERANK", instance_sha256="1" * 64, code_sha256="2" * 64,
                dependencies_sha256="3" * 64, config_sha256="4" * 64, endpoint_sha256="5" * 64,
                runtime_sha256="6" * 64, resources={"text": "7" * 64, "visual": "8" * 64}, capabilities=("READ",))
            pins[name] = A.CandidatePin(code_commit="a" * 40, code_tree_sha256="c" * 64,
                dependencies_sha256="d" * 64, access_config_sha256="e" * 64, policy_sha256=policy.sha256,
                duckdb_file_sha256="b" * 64, components=components, services=(service,))
        units = UnitControlConfig(scope="SHADOW_PRODUCTION" if label == "shadow" else "PRODUCTION_SWITCH",
            docker=binaries["docker"], compose_binary=binaries["docker-compose"], docker_socket=str(shared / "docker.sock"),
            control_root=str(control), receiver_url="http://127.0.0.1:" + ("18000" if label == "shadow" else "8000"),
            mcp_receiver_url="http://127.0.0.1:" + ("18765" if label == "shadow" else "8765"),
            operator_token_file=str(authority / "operator-token"), releases=tuple(unit_releases))
        config = CoreOperatorConfig(units=units, releases=tuple(releases), candidate_release_sha256=unit_releases[0].sha256,
            expected_commit="a" * 40, expected_code_sha256="c" * 64, expected_dependencies_sha256="d" * 64,
            access_config_sha256="e" * 64, policy=policy, authority_root=str(authority),
            protected_roots=tuple(str(p) for p in roots.values()), qualification_root=str(qualification),
            isolation_attestation_sha256="9" * 64 if label == "shadow" else None)
        saved[label] = SimpleNamespace(config=config, docs=docs, authority=authority, document=document,
                                      pins=pins, units=unit_releases, runtimes=runtimes)
    shadow, live = saved["shadow"], saved["live"]
    receipt_dir = shared / "receipts"
    receipt_dir.mkdir()
    closure = []
    def receipt(value):
        raw = canonical_bytes(value)
        digest = hashlib.sha256(raw).hexdigest()
        ref = write(receipt_dir / (digest + ".json"), value)
        closure.append(ref)
        return ref
    events = []
    for phase in A.DRILL_PHASES:
        events.append(receipt({"schema": "vkm-deployment-event/1", "phase": phase, "request_key": "3" * 64,
            "parent_sha256": events[-1].sha256 if events else None,
            "detail": {"native_partial_sha256": "a" * 64, "bindings_partial_sha256": "a" * 64}
                      if phase == "FAULT_INJECTED" else {}}))
    pin = shadow.pins["candidate"]
    drill = receipt({"schema": "vkm-deployment-drill/1", "scope": "SYNTHETIC", "status": "PASS",
        "candidate_components_sha256": pin.components_sha256, "candidate_services_sha256": pin.services_sha256,
        "previous_components_sha256": shadow.pins["previous"].components_sha256,
        "previous_services_sha256": shadow.pins["previous"].services_sha256,
        "code_commit": pin.code_commit, "policy_sha256": pin.policy_sha256,
        "code_tree_sha256": pin.code_tree_sha256, "dependencies_sha256": pin.dependencies_sha256,
        "access_config_sha256": pin.access_config_sha256,
        "deployment_profile_sha256": shadow.config.deployment_profile_sha256,
        "native_before_sha256": "f" * 64, "native_after_restore_sha256": "f" * 64,
        "bindings_before_sha256": "b" * 64, "bindings_after_restore_sha256": "b" * 64,
        "transitions": [{"phase": phase, "journal_sha256": ref.sha256} for phase, ref in zip(A.DRILL_PHASES, events)],
        "adapter_ids": ["core-receiver-release"], "journal_receipts": [r.sha256 for r in events],
        "checks": dict.fromkeys(A.DRILL_CHECKS, "PASS")})
    tools = {name: A.ToolProbe(arguments={}, expected_object_ids=() if name == "get_corpus_status" else ("VKM-SRC-001",))
             for name in A.read_tool_names()}
    plan = A.AcceptancePlan(scope="SYNTHETIC", candidate=pin, tools=tools, source_id="VKM-SRC-001",
        reader_principal="reader", denied_principal="denied", drill_receipt_sha256=drill.sha256,
        deployment_profile_sha256=shadow.config.deployment_profile_sha256, isolation_attestation_sha256="9" * 64)
    probes = []
    operations = [("MCP", "tools/list", record_hash({}))]
    operations += [("MCP", name, record_hash(p.arguments)) for name, p in tools.items()]
    operations += [("API", method + " " + route, record_hash({"principal": principal, "body": body}))
                  for principal, method, route, body, _, _ in A._api_probes(plan)]
    for kind, operation, args in operations:
        probes.append(receipt({"schema": "vkm-shadow-probe/1", "plan_sha256": plan.sha256, "status": "PASS",
            "kind": kind, "operation": operation, "arguments_sha256": args, "response_sha256": record_hash(operation),
            "response_bytes": 1, "native_components_sha256": record_hash(pin.component_map),
            "native_services_sha256": record_hash(pin.service_map)}))
    accepted = receipt({"schema": "vkm-serving-acceptance/1", "scope": "SYNTHETIC", "plan_sha256": plan.sha256,
        "status": "PASS", **pin.model_dump(mode="json", exclude={"components", "services"}),
        "components_sha256": pin.components_sha256, "services_sha256": pin.services_sha256,
        "deployment_drill_sha256": drill.sha256, "checks": dict.fromkeys(A.SERVING_CHECKS, "PASS"),
        "tools": dict.fromkeys(A.read_tool_names(), "PASS"), "raw_probe_receipts": [r.sha256 for r in probes]})
    closure.remove(accepted)
    recipes = {}
    configs = {}
    generations = {}
    for label, obj in saved.items():
        bindings = []
        for name, unit in zip(("candidate", "previous"), obj.units):
            active_plan = plan if name == "candidate" else plan.model_copy(update={"candidate": obj.pins["previous"]})
            # No previous acceptance is asserted by these synthetic mapping tests.
            generation = GenerationManifest(code_commit=active_plan.candidate.code_commit,
                policy_sha256=active_plan.candidate.policy_sha256, components=active_plan.candidate.components,
                services=active_plan.candidate.services, acceptance_sha256=accepted.sha256 if name == "candidate" else "0" * 64)
            gen_ref = obj.document(name + ".generation", generation)
            plan_ref = obj.document(name + ".plan", active_plan)
            generations[label, name] = generation
            bindings.append(OperatorRelease(receiver_release_sha256=unit.sha256,
                environment=obj.docs[name + ".environment"], runtime=obj.docs[name + ".runtime"],
                generation=gen_ref, acceptance_plan=plan_ref))
        config = obj.config.model_copy(update={"releases": tuple(bindings)})
        assert config.deployment_profile_sha256 == obj.config.deployment_profile_sha256
        obj.document("operator", config)
        obj.docs.pop("placeholder")
        recipe = P.PromotionRecipe(operator_config=obj.docs["operator"], selected_previous_release_sha256=obj.units[1].sha256,
            effective_configurations={name + "." + receiver: obj.docs[name + "." + receiver + ".effective"]
                                      for name in ("candidate", "previous") for receiver in ("api", "mcp")},
            documents=tuple(P.RecipeDocument(name=name, file=ref) for name, ref in sorted(obj.docs.items())))
        recipes[label] = write(shared / (label + ".recipe.json"), recipe)
        configs[label] = config
    shadow, live = configs["shadow"], configs["live"]
    attestation = write(shared / "isolation.json", P.IsolationAttestation(
        shadow_profile_sha256=shadow.deployment_profile_sha256, live_profile_sha256=live.deployment_profile_sha256,
        shadow_project="vkm-core-shadow", live_project="vkm-core", shadow_control_root=shadow.units.control_root,
        live_control_root=live.units.control_root, shadow_api_endpoint=shadow.units.receiver_url,
        live_api_endpoint=live.units.receiver_url, shadow_mcp_endpoint=shadow.units.mcp_receiver_url,
        live_mcp_endpoint=live.units.mcp_receiver_url))
    previous = generations["live", "previous"]
    native = write(shared / "native-previous.json", P.RetainedTopology(generation_sha256=previous.sha256,
        receiver_release_sha256=live.units.releases[1].sha256, components=previous.components, services=previous.services))
    proof = {"status": "PASS", "scope": "SYNTHETIC", "generation_sha256": previous.sha256,
             "receiver_release_sha256": live.units.releases[1].sha256, "native_sha256": native.sha256}
    backup_source = write(shared / "backup-verifier.json", {"synthetic": True, "status": "PASS", "generation_sha256": previous.sha256})
    restore_source = write(shared / "restore-verifier.json", {"synthetic": True, "status": "PASS", "generation_sha256": previous.sha256, "kind": "restore"})
    previous_ref = write(shared / "retained-previous.json", P.RetainedPrevious(
        generation=saved["live"].docs["previous.generation"], native_before=native, native_after_restore=native,
        receiver_release_sha256=live.units.releases[1].sha256,
        independent_backup=write(shared / "backup.json", P.RecoveryEvidence(**proof, kind="INDEPENDENT_BACKUP", verifier_receipt=backup_source)),
        restore_receipt=write(shared / "restore.json", P.RecoveryEvidence(**proof, kind="RESTORE", verifier_receipt=restore_source))))
    a, b = P._recipe(recipes["shadow"]), P._recipe(recipes["live"])
    sd = P._normalize_documents(a[1], a[2], a[3], a[0])
    ld = P._normalize_documents(b[1], b[2], b[3], b[0])
    mappings = []
    def differences(x, y, name, parts=()):
        if x == y:
            return
        if isinstance(x, dict) and isinstance(y, dict):
            for key in x:
                differences(x[key], y[key], name, (*parts, key))
        elif isinstance(x, list) and isinstance(y, list):
            for i, (v, w) in enumerate(zip(x, y)):
                differences(v, w, name, (*parts, i))
        else:
            if x == "vkm-core-shadow" and y == "vkm-core":
                kind = "PROJECT"
            elif parts == ("units", "receiver_url"):
                kind = "API_ENDPOINT"
            elif parts == ("units", "mcp_receiver_url"):
                kind = "MCP_ENDPOINT"
            elif str(x).startswith(shadow.authority_root):
                kind = "AUTHORITY_ROOT"
            elif str(x).startswith(shadow.units.control_root) or str(parts[-1]) == "runtime_root":
                kind = "CONTROL_ROOT"
            elif str(x).startswith(shadow.qualification_root):
                kind = "QUALIFICATION_ROOT"
            else:
                kind = next(k.upper() + "_ROOT" for k in ("originals", "canonical", "evidence") if
                            str(x).startswith(str(getattr(a[4]["candidate"], k + "_root"))))
            mappings.append(P.Mapping(document=name, pointer=P._pointer(parts), kind=kind, before=x, after=y))
    for name in sd:
        differences(sd[name], ld[name], name)
    binding = P.PromotionBinding(mode="SYNTHETIC", shadow_recipe=recipes["shadow"], live_recipe=recipes["live"],
        shadow_plan=saved["shadow"].docs["candidate.plan"], shadow_drill=drill, shadow_acceptance=accepted,
        receipt_closure=tuple(closure), isolation_attestation=attestation, retained_previous=previous_ref,
        mappings=tuple(mappings))
    approval = write(shared / "promotion.json", binding)
    return SimpleNamespace(approval=approval, binding=binding, previous=previous, policy=pin.policy_sha256,
                           access=pin.access_config_sha256, saved=saved, configs=configs, recipes=recipes, shared=shared)


def run(s, **kwargs):
    return P.preflight_promotion(kwargs.get("approval", s.approval),
        approved_binding_sha256=kwargs.get("approved_binding_sha256", s.approval.sha256),
        current_policy_sha256=kwargs.get("current_policy_sha256", s.policy),
        current_access_sha256=kwargs.get("current_access_sha256", s.access),
        selected_previous_generation_sha256=kwargs.get("selected_previous_generation_sha256", s.previous.sha256))


def reapprove(s, binding):
    ref = write(s.shared / "altered.json", binding)
    return run(s, approval=ref, approved_binding_sha256=ref.sha256)


def test_valid_synthetic_mapping_preserves_original_scope_and_profile(tmp_path):
    s = fixture(tmp_path)
    original = {path: path.read_bytes() for path in tmp_path.rglob("*") if path.is_file()}
    result = run(s)
    assert result.status == "BOUND_NOT_RUNTIME_QUALIFIED"
    assert result.shadow_profile_sha256 != result.live_profile_sha256
    assert result.shadow_acceptance_sha256 == s.binding.shadow_acceptance.sha256
    assert original == {path: path.read_bytes() for path in tmp_path.rglob("*") if path.is_file()}
    assert P._read(s.binding.shadow_plan)["scope"] == "SYNTHETIC"
    assert run(s) == result


def test_project_alias_cannot_rewrite_a_data_or_model_name(tmp_path):
    s = fixture(tmp_path)
    a = P._recipe(s.binding.shadow_recipe)
    b = P._recipe(s.binding.live_recipe)
    mapping = P.Mapping(document="graph", pointer="/name", kind="PROJECT", before="vkm-core-shadow", after="vkm-core")
    with pytest.raises(P.PromotionError, match="ownership"):
        P._allow(mapping, "graph", ("name",), mapping.before, mapping.after, a[2], b[2], a[4], b[4])


@pytest.mark.parametrize("pin", ["policy", "access", "previous", "approval"])
def test_current_independent_identity_mismatch_blocks(tmp_path, pin):
    s = fixture(tmp_path)
    key = {"policy": "current_policy_sha256", "access": "current_access_sha256",
           "previous": "selected_previous_generation_sha256", "approval": "approved_binding_sha256"}[pin]
    with pytest.raises(P.PromotionError):
        run(s, **{key: "0" * 64})


@pytest.mark.parametrize("case", ["remove", "unknown", "wrong_kind", "wrong_target", "project_root", "endpoint_crossover"])
def test_mapping_cannot_expand_its_capabilities(tmp_path, case):
    s = fixture(tmp_path)
    mappings = list(s.binding.mappings)
    if case == "remove":
        mappings.pop()
    elif case == "unknown":
        mappings.append(P.Mapping(document="unregistered", pointer="/model", kind="AUTHORITY_ROOT", before="a", after="b"))
    else:
        index = next(i for i, m in enumerate(mappings) if m.kind == "API_ENDPOINT")
        m = mappings[index]
        change = {"kind": "CONTROL_ROOT"} if case == "wrong_kind" else {"after": "http://127.0.0.1:9"}
        if case == "project_root":
            index = next(i for i, m in enumerate(mappings) if m.kind == "AUTHORITY_ROOT")
            m = mappings[index]
            change = {"kind": "PROJECT"}
        elif case == "endpoint_crossover":
            change = {"kind": "MCP_ENDPOINT"}
        mappings[index] = m.model_copy(update=change)
    with pytest.raises(P.PromotionError):
        reapprove(s, s.binding.model_copy(update={"mappings": tuple(mappings)}))


@pytest.mark.parametrize("ref", ["shadow_recipe", "live_recipe", "shadow_plan", "shadow_drill", "shadow_acceptance",
                                  "isolation_attestation", "retained_previous"])
def test_bound_document_mutation_is_detected(tmp_path, ref):
    s = fixture(tmp_path)
    path = Path(getattr(s.binding, ref).path)
    path.write_bytes(path.read_bytes() + b" ")
    with pytest.raises(ValueError):
        run(s)


def test_missing_probe_and_transition_receipts_cannot_be_summary_pass(tmp_path):
    s = fixture(tmp_path)
    for ref in (s.binding.receipt_closure[0], s.binding.receipt_closure[-1]):
        altered = tuple(f for f in s.binding.receipt_closure if f != ref)
        with pytest.raises(ValueError, match="incomplete"):
            reapprove(s, s.binding.model_copy(update={"receipt_closure": altered}))


def test_unrelated_digest_does_not_replace_full_receipt_closure(tmp_path):
    s = fixture(tmp_path)
    unrelated = write(s.shared / "unrelated.json", {"status": "PASS"})
    with pytest.raises(P.PromotionError, match="unrelated"):
        reapprove(s, s.binding.model_copy(update={"receipt_closure": (*s.binding.receipt_closure, unrelated)}))


@pytest.mark.parametrize("field", ["generation_sha256", "receiver_release_sha256", "native_sha256", "status"])
def test_partial_previous_backup_restore_recipe_is_rejected(tmp_path, field):
    s = fixture(tmp_path)
    previous = P.RetainedPrevious.model_validate(P._read(s.binding.retained_previous))
    report = P._read(previous.restore_receipt)
    report[field] = "NOT_RUN" if field == "status" else "0" * 64
    changed = write(s.shared / "changed-restore.json", report)
    previous = previous.model_copy(update={"restore_receipt": changed})
    previous_ref = write(s.shared / "changed-previous.json", previous)
    with pytest.raises(ValueError):
        reapprove(s, s.binding.model_copy(update={"retained_previous": previous_ref}))


def test_saved_digest_is_not_a_native_proof_or_live_acceptance(tmp_path):
    s = fixture(tmp_path)
    before = P._read(s.binding.shadow_plan)
    with pytest.raises(P.PromotionError, match="original shadow"):
        reapprove(s, s.binding.model_copy(update={"mode": "SHADOW_TO_LIVE"}))
    assert before == P._read(s.binding.shadow_plan)
    assert run(s).status != "READY"


@pytest.mark.parametrize("capability", ["write", "admin", "upstream", "memory", "image"])
def test_receiver_capabilities_cannot_be_promoted_by_mapping(tmp_path, capability):
    def mutate(name, compose):
        if name != "candidate":
            return
        mcp = compose["services"]["mcp"]
        if capability == "write":
            mcp["environment"]["VKM_API_WRITE_TOKEN_FILE"] = "unregistered"
        elif capability == "admin":
            mcp["command"][3] = "admin"
        elif capability == "upstream":
            mcp["environment"]["VKM_API_URL"] = "http://other-api:8000"
        elif capability == "memory":
            mcp["mem_limit"] = 0
        else:
            mcp["image"] = "unpinned:latest"
    with pytest.raises((ValueError, GenerationUnavailable)):
        fixture(tmp_path, mutate_live=mutate)


def test_previous_only_recovery_does_not_read_missing_or_corrupt_candidate(tmp_path):
    s = fixture(tmp_path)
    previous = P.preflight_promotion_previous(s.approval, approved_binding_sha256=s.approval.sha256,
        current_policy_sha256=s.policy, current_access_sha256=s.access)
    for name, ref in s.saved["live"].docs.items():
        if name.startswith("candidate."):
            Path(ref.path).unlink()
    Path(s.binding.shadow_acceptance.path).write_bytes(b"corrupt candidate acceptance")
    with pytest.raises(ValueError):
        run(s)
    assert P.preflight_promotion_previous(s.approval, approved_binding_sha256=s.approval.sha256,
        current_policy_sha256=s.policy, current_access_sha256=s.access) == previous
    assert previous.status == "BOUND_PREVIOUS_ONLY_NOT_RUNTIME_QUALIFIED"


def test_previous_only_recovery_still_requires_full_retained_configuration(tmp_path):
    s = fixture(tmp_path)
    ref = s.saved["live"].docs["previous.api.effective"]
    Path(ref.path).write_bytes(b'{}')
    with pytest.raises(ValueError):
        P.preflight_promotion_previous(s.approval, approved_binding_sha256=s.approval.sha256,
            current_policy_sha256=s.policy, current_access_sha256=s.access)


def test_native_factory_rejects_synthetic_promotion_before_any_portal_or_unit(tmp_path, monkeypatch):
    import vkm_corpus.update.operator as O
    s = fixture(tmp_path)
    approval = write(s.saved["live"].authority / "promotion-approval.json", s.binding)
    config = s.configs["live"].model_copy(update={"promotion_approval": approval})
    ref = write(s.saved["live"].authority / "native-operator.json", config)
    monkeypatch.setattr(O.platform, "system", lambda: "Linux")
    monkeypatch.setattr(O, "NativePortal", lambda: pytest.fail("portal created before promotion qualification"))
    monkeypatch.setattr(O, "CoreUnitControl", lambda _: pytest.fail("unit control created before qualification"))
    with pytest.raises(GenerationUnavailable, match="synthetic promotion"):
        CoreOperator(config, config_ref=ref)


def test_recovery_only_operator_cannot_load_or_apply_candidate():
    operator = object.__new__(CoreOperator)
    operator.recovery_only = True
    operator._release = lambda _: pytest.fail("recovery loaded candidate")
    operator.units = SimpleNamespace(select=lambda *_a, **_k: pytest.fail("recovery applied candidate"))
    with pytest.raises(GenerationUnavailable, match="recovery-only"):
        operator._candidate()
    with pytest.raises(GenerationUnavailable, match="recovery-only"):
        operator._apply(None, "request")


@pytest.mark.parametrize("closed_previous", [False, True])
@pytest.mark.parametrize("live_failure", [False, True])
def test_live_probe_is_after_actual_rebind_before_open_and_never_on_previous_restore(
        tmp_path, monkeypatch, closed_previous, live_failure):
    """Real durable sequence; explicit SYNTHETIC units and private probe fake.

    No production promotion, loaded model or full 49-tool receipt is minted.
    """
    from test_deployment_lifecycle import rig
    from test_closed_baseline_deployment import closed_rig
    from vkm_corpus.update import operator as O
    from vkm_corpus.update.admission import read_state
    from vkm_corpus.update.deployment import ReceiverControl, SelectorAdapter

    built = closed_rig(tmp_path) if closed_previous else rig(tmp_path)
    controller, old, new, state, _, barrier = built[:6]
    assert controller._profile().scope == "SYNTHETIC"
    events, native = [], {"selected": old}
    operator = object.__new__(CoreOperator)
    operator.config = SimpleNamespace(promotion_approval="SYNTHETIC-order-only", candidate_release_sha256="candidate",
        max_duckdb_bytes=1)
    operator.root, operator.barrier, operator.controller = controller.root, barrier, controller
    operator.recovery_only, operator._drilling = False, False
    operator._fence_config = lambda: None
    operator._fence_promotion = lambda **_: "SYNTHETIC-order-only"

    def selected():
        manifest = new if state["DOCUMENT"]["revision"] == "new" else old
        return SimpleNamespace(manifest=manifest)
    operator._selected = selected
    value = SimpleNamespace(manifest=new, plan=SimpleNamespace(candidate="SYNTHETIC", total_timeout_seconds=1),
        components=lambda: state, app=SimpleNamespace(state=SimpleNamespace(config="SYNTHETIC", service=SimpleNamespace(
            deps=SimpleNamespace(canon=SimpleNamespace(path=tmp_path / "synthetic.duckdb"),
                access_policy=SimpleNamespace(path=tmp_path / "synthetic.policy"), nav=None)))))
    operator._candidate = lambda: value
    operator._qualify = lambda manifest: None

    def rebind(manifest, **_):
        assert controller._current() == manifest and not (controller.root / "MAINTENANCE").exists()
        assert read_state(controller.root).status == "CLOSED"
        native["selected"] = manifest
        events.append("rebind-candidate" if manifest == new else "rebind-previous")
        return SimpleNamespace(model_dump=lambda **_: {"scope": "SYNTHETIC", "generation": manifest.sha256})
    operator.units = SimpleNamespace(rebind=rebind, selected=lambda: SimpleNamespace(
        sha256="candidate" if selected().manifest == new else "previous"))
    operator.portal = SimpleNamespace(call=lambda coro, **_: asyncio.run(coro))

    class PreliminaryFence:
        def __init__(self, *args, **kwargs):
            pass
        def check(self, *, full=False):
            assert native["selected"] == old and controller._current() == old
            assert (controller.root / "MAINTENANCE").exists() and barrier.status()["paused"]
            if full:
                events.append("preliminary-probe")
        def close(self):
            pass
    monkeypatch.setattr(O, "CandidateFence", PreliminaryFence)

    async def live_accept(candidate):
        assert candidate == value and native["selected"] == new and controller._current() == new
        status = read_state(controller.root)
        assert status.status == "CLOSED"
        controller._publication_fence(status.request_key, new)
        events.append("live-accept")
        if live_failure:
            raise GenerationUnavailable("synthetic live probe failed")
        return {"status": "PASS", "scope": "SYNTHETIC"}
    operator._live_accept = live_accept

    def capture():
        return {"generation": selected().manifest.sha256}
    def apply(manifest, request_id):
        assert native["selected"] == old
        events.append("select")
        state.clear()
        state.update({c.component: c.model_dump(mode="json") for c in manifest.components})
    def restore(binding, request_id):
        assert binding == {"generation": old.sha256}
        events.append("restore")
        state.clear()
        state.update({c.component: c.model_dump(mode="json") for c in old.components})
    controller.adapters = {"document": SelectorAdapter("document", capture, apply, restore)}
    profile = controller.identity_provider()
    controller.identity_provider = lambda: profile.model_copy(update={"adapter_ids": ("document",)})
    controller.receivers = {barrier.receiver_id: ReceiverControl(barrier, operator._rebind)}
    controller.candidate_probe = operator._probe
    controller.fault = lambda phase: events.append("current") if phase == "CURRENT_WRITTEN" else (
        events.append("open") if phase == "ADMISSION_OPEN" else None)
    plan = controller.plan(new, "synthetic-promotion-order")
    if live_failure:
        with pytest.raises(GenerationUnavailable, match="synthetic live probe failed"):
            controller.switch(new, "synthetic-promotion-order", plan["plan_sha256"])
        assert controller._current() == old and native["selected"] == old
        assert events.count("live-accept") == 1 and "open" not in events[:events.index("restore")]
        if closed_previous:
            assert read_state(controller.root).status == "CLOSED"
    else:
        assert controller.switch(new, "synthetic-promotion-order", plan["plan_sha256"])["status"] == "PASS"
        assert events == ["select", "preliminary-probe", "current", "rebind-candidate", "live-accept", "open"]


def test_live_probe_has_separate_scope_and_cannot_be_ordinary_acceptance(tmp_path, monkeypatch):
    # Reuse the existing explicitly SYNTHETIC private-probe fixture. No production
    # transport, native process or serving acceptance is claimed here.
    from test_shadow_acceptance import setup as acceptance_setup, FakeTransport
    s = acceptance_setup.__wrapped__(tmp_path, monkeypatch)
    promotion = P.PromotionPreflight("1" * 64, "2" * 64, s.plan.deployment_profile_sha256,
        s.plan.sha256, "3" * 64, "4" * 64, "5" * 64)
    from vkm_corpus.update.receiver import ReceiverIdentity
    proof = ReceiverIdentity(nonce="a" * 64, instance="b" * 64, process_pid=1, process_start_ticks=1,
        pid_namespace_inode=1, generation_sha256=promotion.candidate_generation_sha256,
        runtime_config_sha256="c" * 64, code_sha256=s.plan.candidate.code_tree_sha256,
        dependencies_sha256=s.plan.candidate.dependencies_sha256, access_sha256=s.plan.candidate.access_config_sha256,
        components_sha256=s.plan.candidate.components_sha256, services_sha256=s.plan.candidate.services_sha256,
        gate_device=1, gate_inode=1, admission_open=False, read_contract_sha256=record_hash(sorted(A.read_tool_names())))
    calls = []
    async def execute(proof_provider):
        return await P.qualify_live_promotion(s.plan, promotion=promotion, transport=FakeTransport(s.plan.candidate),
            fence=s.fence, registrar=s.reg, boundary_check=lambda: calls.append("fence"), receiver_proof=proof_provider)
    report = asyncio.run(execute(lambda: proof.model_dump(mode="json")))
    assert report["scope"] == "SYNTHETIC"
    assert report["schema_version"] == "vkm-live-promotion-private-probe/1"
    assert report["source_shadow_plan_sha256"] == s.plan.sha256
    assert report["scientific_admission"] is False and len(calls) > 2
    with pytest.raises(A.AcceptanceError):
        s.reg.register(P._read(BoundFile(path=str(s.reg.root / (report["receipt_sha256"] + ".json")),
                                       sha256=report["receipt_sha256"])), s.plan)
    with pytest.raises(ValueError):
        asyncio.run(execute(lambda: {"digest": "0" * 64, "status": "READY"}))
    counter = 0
    def replaced():
        nonlocal counter
        counter += 1
        return proof.model_copy(update={"instance": ("b" if counter == 1 else "f") * 64}).model_dump(mode="json")
    with pytest.raises(P.PromotionError, match="receiver changed"):
        asyncio.run(execute(replaced))
