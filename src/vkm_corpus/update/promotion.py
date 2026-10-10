"""Immutable shadow-to-live mapping preflight; this does not qualify live runtime.

Only operator-owned, hash-bound inputs are accepted. Source receipts retain their
scope/profile; no receipt is renamed and no live selector is touched here. Fresh
native rebind, backup/restore and admission remain separate mandatory gates.
"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
import stat
from typing import Literal

from pydantic import Field, model_validator

from vkm_corpus.update.acceptance import (AcceptancePlan, AcceptanceRegistrar,
    SERVING_CHECKS, _verify_probe_receipts, _execute_private_probes, read_tool_names)
from vkm_corpus.update.contracts import ComponentIdentity, GenerationManifest, ServiceIdentity
from vkm_corpus.update.deployment import components_sha256, services_sha256
from vkm_corpus.update.operator_units import CoreUnitControl, bound_json
from vkm_corpus.update.runtime import BoundFile, RuntimeConfig, read_bound
from vkm_evidence.contracts import Identifier, Sha256, StrictModel, record_hash


class PromotionError(ValueError):
    """The proposed mapping is outside the immutable operator approval."""


class RecipeDocument(StrictModel):
    name: Identifier
    file: BoundFile


class PromotionRecipe(StrictModel):
    schema_version: Literal["vkm-promotion-recipe/1"] = "vkm-promotion-recipe/1"
    operator_config: BoundFile
    selected_previous_release_sha256: Sha256
    # Full hash input used by container_fingerprint, not just a claimed digest.
    effective_configurations: dict[Literal["candidate.api", "candidate.mcp", "previous.api", "previous.mcp"], BoundFile]
    documents: tuple[RecipeDocument, ...] = Field(min_length=1, max_length=128)

    @model_validator(mode="after")
    def _complete(self):
        if set(self.effective_configurations) != {"candidate.api", "candidate.mcp", "previous.api", "previous.mcp"}:
            raise ValueError("promotion requires full candidate and previous configuration")
        if len({d.name for d in self.documents}) != len(self.documents):
            raise ValueError("duplicate recipe document")
        if "operator" not in {d.name for d in self.documents}:
            raise ValueError("operator document is required")
        return self


class Mapping(StrictModel):
    document: Identifier
    pointer: str = Field(pattern=r"^/(?:[^~]|~[01])+$", max_length=2048)
    kind: Literal["PROJECT", "AUTHORITY_ROOT", "CONTROL_ROOT", "QUALIFICATION_ROOT",
                  "ORIGINALS_ROOT", "CANONICAL_ROOT", "EVIDENCE_ROOT", "API_ENDPOINT", "MCP_ENDPOINT"]
    before: str = Field(min_length=1, max_length=8192)
    after: str = Field(min_length=1, max_length=8192)

    @model_validator(mode="after")
    def _changed(self):
        if self.before == self.after or "\x00" in self.before + self.after:
            raise ValueError("mapping must describe an actual bounded change")
        return self


class IsolationAttestation(StrictModel):
    schema_version: Literal["vkm-promotion-isolation/1"] = "vkm-promotion-isolation/1"
    shadow_profile_sha256: Sha256
    live_profile_sha256: Sha256
    shadow_project: Literal["vkm-core-shadow"]
    live_project: Literal["vkm-core"]
    shadow_control_root: str
    live_control_root: str
    shadow_api_endpoint: str
    live_api_endpoint: str
    shadow_mcp_endpoint: str
    live_mcp_endpoint: str


class RetainedPrevious(StrictModel):
    schema_version: Literal["vkm-promotion-retained-previous/1"] = "vkm-promotion-retained-previous/1"
    generation: BoundFile
    native_before: BoundFile
    native_after_restore: BoundFile
    selector_name: Literal["core-receiver-release"] = "core-receiver-release"
    receiver_release_sha256: Sha256
    # Independent backup and actual restore receipts are retained, not replaced
    # by a statement inside the recipe. Their runtime verifier is mandatory.
    independent_backup: BoundFile
    restore_receipt: BoundFile


class RetainedTopology(StrictModel):
    """Retained observation content; never a substitute for fresh native proof."""
    schema_version: Literal["vkm-promotion-retained-topology/1"] = "vkm-promotion-retained-topology/1"
    generation_sha256: Sha256
    receiver_release_sha256: Sha256
    components: tuple[ComponentIdentity, ...] = Field(min_length=1)
    services: tuple[ServiceIdentity, ...]


class RecoveryEvidence(StrictModel):
    schema_version: Literal["vkm-promotion-recovery-evidence/1"] = "vkm-promotion-recovery-evidence/1"
    kind: Literal["INDEPENDENT_BACKUP", "RESTORE"]
    scope: Literal["SYNTHETIC", "FULL_COHERENT_PREVIOUS"]
    status: Literal["PASS"]
    generation_sha256: Sha256
    receiver_release_sha256: Sha256
    native_sha256: Sha256
    # Source-bound actual backup/restore verifier output, checked again by the
    # runtime adapter. This wrapper does not invent a universal backup protocol.
    verifier_receipt: BoundFile


class PromotionBinding(StrictModel):
    schema_version: Literal["vkm-shadow-live-promotion/1"] = "vkm-shadow-live-promotion/1"
    mode: Literal["SYNTHETIC", "SHADOW_TO_LIVE"]
    shadow_recipe: BoundFile
    live_recipe: BoundFile
    shadow_plan: BoundFile
    shadow_drill: BoundFile
    shadow_acceptance: BoundFile
    receipt_closure: tuple[BoundFile, ...] = Field(min_length=1, max_length=256)
    isolation_attestation: BoundFile
    retained_previous: BoundFile
    mappings: tuple[Mapping, ...] = Field(min_length=1, max_length=512)

    @model_validator(mode="after")
    def _unique(self):
        if len({(m.document, m.pointer) for m in self.mappings}) != len(self.mappings):
            raise ValueError("duplicate promotion mapping")
        if len({r.sha256 for r in self.receipt_closure}) != len(self.receipt_closure):
            raise ValueError("duplicate closure digest")
        return self

    @property
    def sha256(self):
        return record_hash(self)


@dataclass(frozen=True)
class PromotionPreflight:
    binding_sha256: str
    live_profile_sha256: str
    shadow_profile_sha256: str
    shadow_plan_sha256: str
    shadow_acceptance_sha256: str
    candidate_generation_sha256: str
    previous_generation_sha256: str
    status: str = "BOUND_NOT_RUNTIME_QUALIFIED"


@dataclass(frozen=True)
class PromotionPreviousPreflight:
    binding_sha256: str
    live_profile_sha256: str
    previous_generation_sha256: str
    live_operator_config: BoundFile
    status: str = "BOUND_PREVIOUS_ONLY_NOT_RUNTIME_QUALIFIED"


class LivePromotionProbeReceipt(StrictModel):
    schema_version: Literal["vkm-live-promotion-private-probe/1"] = "vkm-live-promotion-private-probe/1"
    scope: Literal["SYNTHETIC", "LIVE_PRIVATE_REBIND"]
    status: Literal["PASS"] = "PASS"
    scientific_admission: Literal[False] = False
    promotion_binding_sha256: Sha256
    live_profile_sha256: Sha256
    source_shadow_plan_sha256: Sha256
    source_shadow_acceptance_sha256: Sha256
    candidate_generation_sha256: Sha256
    live_probe_context_sha256: Sha256
    receiver_before_sha256: Sha256
    receiver_after_sha256: Sha256
    tools: dict[str, Literal["PASS"]]
    checks: dict[Literal["native_identity", "policy_enforcement", "generation_consistency", "full_mcp"], Literal["PASS"]]
    raw_probe_receipts: tuple[Sha256, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def _full(self):
        if (set(self.tools) != read_tool_names() or set(self.checks) != {
                "native_identity", "policy_enforcement", "generation_consistency", "full_mcp"}
                or len(set(self.raw_probe_receipts)) != len(self.raw_probe_receipts)):
            raise ValueError("live private probe must contain the complete contract")
        return self


async def qualify_live_promotion(plan, *, promotion: PromotionPreflight, transport, fence,
                                 registrar, boundary_check, receiver_proof):
    """Execute private live probes without minting a renamed SHADOW receipt.

    All callbacks are owned by the fixed factory, never supplied by a manifest.
    Fresh authenticated joined receiver proof is taken before and after probes.
    """
    if (type(promotion) is not PromotionPreflight or promotion.status != "BOUND_NOT_RUNTIME_QUALIFIED"
            or plan.sha256 != promotion.shadow_plan_sha256 or plan.deployment_profile_sha256 != promotion.shadow_profile_sha256):
        raise PromotionError("live probes require the exact original promotion source")
    context = record_hash({"schema": "vkm-live-promotion-probe-context/1",
        "promotion_binding_sha256": promotion.binding_sha256, "live_profile_sha256": promotion.live_profile_sha256,
        "source_shadow_plan_sha256": promotion.shadow_plan_sha256,
        "candidate_generation_sha256": promotion.candidate_generation_sha256})
    tools, checks, receipts = {}, {}, []
    from vkm_corpus.update.receiver import ReceiverIdentity
    def observe_receiver():
        proof = ReceiverIdentity.model_validate(receiver_proof())
        if (proof.generation_sha256 != promotion.candidate_generation_sha256 or proof.admission_open
                or proof.code_sha256 != plan.candidate.code_tree_sha256
                or proof.dependencies_sha256 != plan.candidate.dependencies_sha256
                or proof.access_sha256 != plan.candidate.access_config_sha256
                or proof.components_sha256 != plan.candidate.components_sha256
                or proof.services_sha256 != plan.candidate.services_sha256
                or proof.read_contract_sha256 != record_hash(sorted(read_tool_names()))):
            raise PromotionError("live native receiver proof differs from exact bound candidate")
        return proof
    boundary_check()
    observed_before = observe_receiver()
    before = registrar.store(observed_before.model_dump(mode="json"))
    await _execute_private_probes(plan, transport=transport, fence=fence, store=registrar.store,
        tools=tools, checks=checks, receipts=receipts, receipt_schema="vkm-live-promotion-raw-probe/1",
        receipt_plan_sha256=context, boundary_check=boundary_check)
    boundary_check()
    observed_after = observe_receiver()
    if observed_before.model_dump(exclude={"nonce"}) != observed_after.model_dump(exclude={"nonce"}):
        raise PromotionError("actual live receiver changed during private probes")
    after = registrar.store(observed_after.model_dump(mode="json"))
    result = LivePromotionProbeReceipt(scope="SYNTHETIC" if plan.scope == "SYNTHETIC" else "LIVE_PRIVATE_REBIND",
        promotion_binding_sha256=promotion.binding_sha256, live_profile_sha256=promotion.live_profile_sha256,
        source_shadow_plan_sha256=promotion.shadow_plan_sha256,
        source_shadow_acceptance_sha256=promotion.shadow_acceptance_sha256,
        candidate_generation_sha256=promotion.candidate_generation_sha256, live_probe_context_sha256=context,
        receiver_before_sha256=before, receiver_after_sha256=after, tools=tools, checks=checks, raw_probe_receipts=tuple(receipts))
    _verify_probe_receipts(result.model_dump(mode="json"), plan, registrar.read,
        schema="vkm-live-promotion-raw-probe/1", plan_sha256=context)
    boundary_check()
    return {**result.model_dump(mode="json"), "receipt_sha256": registrar.store(result.model_dump(mode="json"))}


def preflight_promotion_previous(approval: BoundFile, *, approved_binding_sha256: str,
                                 current_policy_sha256: str, current_access_sha256: str) -> PromotionPreviousPreflight:
    """Fence only the retained recovery closure; never read candidate artifacts.

    Use only in an explicit RESTORE_PREVIOUS capability. It cannot authorize
    candidate loading, ordinary acceptance or apply, even if its result is valid.
    """
    from vkm_corpus.update.operator import CoreOperatorConfig, operator_environment
    binding = PromotionBinding.model_validate(_read(approval))
    if binding.sha256 != approval.sha256 or approval.sha256 != approved_binding_sha256:
        raise PromotionError("exact canonical promotion approval differs")
    recipe = PromotionRecipe.model_validate(_read(binding.live_recipe))
    config = CoreOperatorConfig.model_validate(_read(recipe.operator_config))
    if (config.units.scope != "PRODUCTION_SWITCH" or config.units.releases[0].project != "vkm-core"
            or config.policy.sha256 != current_policy_sha256 or config.access_config_sha256 != current_access_sha256):
        raise PromotionError("retained live configuration/access/policy differs")
    previous = RetainedPrevious.model_validate(_read(binding.retained_previous))
    key = recipe.selected_previous_release_sha256
    if previous.receiver_release_sha256 != key or key == config.candidate_release_sha256:
        raise PromotionError("retained receiver release is not the exact previous")
    units = {u.sha256: u for u in config.units.releases}
    releases = {r.receiver_release_sha256: r for r in config.releases}
    if key not in units or key not in releases:
        raise PromotionError("whole retained release is missing")
    release, unit = releases[key], units[key]
    generation = GenerationManifest.model_validate(_read(release.generation))
    if _read(previous.generation) != _read(release.generation):
        raise PromotionError("retained previous generation differs")
    runtime = RuntimeConfig.model_validate(_read(release.runtime))
    plan = AcceptancePlan.model_validate(_read(release.acceptance_plan))
    if (runtime.native_serving is None or runtime.policy != config.policy
            or record_hash(runtime) != unit.runtime_config_sha256
            or generation.code_commit != plan.candidate.code_commit or generation.policy_sha256 != plan.candidate.policy_sha256
            or generation.components != plan.candidate.components or generation.services != plan.candidate.services
            or unit.code_sha256 != plan.candidate.code_tree_sha256
            or unit.dependencies_sha256 != plan.candidate.dependencies_sha256
            or unit.access_sha256 != plan.candidate.access_config_sha256):
        raise PromotionError("retained runtime and policy differ")
    control = object.__new__(CoreUnitControl)
    control.config, control.root = config.units, Path(config.units.control_root)
    compose = control._compose(unit)
    env = operator_environment(release.environment)
    if (compose["services"]["api"].get("environment") != env
            or env.get("VKM_UPDATE_RUNTIME_FILE") != release.runtime.path
            or env.get("VKM_SOURCE_POLICY_FILE") != config.policy.path):
        raise PromotionError("retained effective environment differs")
    for receiver in ("api", "mcp"):
        ref = recipe.effective_configurations["previous." + receiver]
        effective = _read(ref)
        if (set(effective) != {"config", "host_config", "mounts", "networks"}
                or record_hash(effective) != unit.units[receiver].config_sha256
                or effective["config"].get("Labels", {}).get("com.docker.compose.project") != unit.project
                or effective["config"].get("Labels", {}).get("com.docker.compose.service") != receiver):
            raise PromotionError("retained effective configuration differs")
    # Traverse only bound files reachable from the previous release, excluding
    # the operator document's candidate references and unqualified data roots.
    pending = [config.policy, release.environment, release.runtime, release.acceptance_plan, unit.compose,
               previous.native_before, previous.native_after_restore, previous.independent_backup, previous.restore_receipt,
               recipe.effective_configurations["previous.api"], recipe.effective_configurations["previous.mcp"]]
    if getattr(release, "baseline_registration", None) is not None:
        pending.append(release.baseline_registration)
    checked = {}
    def refs(value):
        if isinstance(value, dict):
            if set(value) == {"path", "sha256"}:
                yield BoundFile.model_validate(value)
            else:
                for child in value.values():
                    yield from refs(child)
        elif isinstance(value, list):
            for child in value:
                yield from refs(child)
    while pending:
        ref = pending.pop()
        identity = (ref.path, ref.sha256)
        if identity in checked:
            continue
        if len(checked) >= 128:
            raise PromotionError("retained previous control closure exceeds bound")
        body = _read(ref)
        checked[identity] = ref
        pending.extend(refs(body))
    topology = RetainedTopology.model_validate(_read(previous.native_before))
    if (topology != RetainedTopology.model_validate(_read(previous.native_after_restore))
            or topology.generation_sha256 != generation.sha256 or topology.receiver_release_sha256 != key
            or topology.components != generation.components or topology.services != generation.services):
        raise PromotionError("retained native topology differs")
    for kind, ref in (("INDEPENDENT_BACKUP", previous.independent_backup), ("RESTORE", previous.restore_receipt)):
        proof = RecoveryEvidence.model_validate(_read(ref))
        source = _read(proof.verifier_receipt)
        if (proof.kind != kind or proof.scope != ("SYNTHETIC" if binding.mode == "SYNTHETIC" else "FULL_COHERENT_PREVIOUS")
                or proof.generation_sha256 != generation.sha256 or proof.receiver_release_sha256 != key
                or proof.native_sha256 != previous.native_before.sha256 or source.get("status") != "PASS"
                or source.get("generation_sha256") != generation.sha256):
            raise PromotionError("retained backup/restore verification is incomplete")
    for ref in (approval, binding.live_recipe, recipe.operator_config, binding.retained_previous,
                previous.generation, release.generation, *checked.values()):
        _read(ref)
    for ref in (config.units.docker, config.units.compose_binary):
        read_bound(ref)
    return PromotionPreviousPreflight(binding.sha256, config.deployment_profile_sha256, generation.sha256, recipe.operator_config)


def _pointer(parts):
    return "/" + "/".join(str(p).replace("~", "~0").replace("/", "~1") for p in parts)


def _read(ref):
    # bound_json rejects duplicate keys, symlinks/hardlinks, shared writes,
    # oversized documents and mismatching raw bytes. No source content is read.
    path = Path(ref.path)
    if not path.is_absolute() or ".." in path.parts:
        raise PromotionError("promotion inputs must use normalized absolute host paths")
    value = bound_json(ref)
    if not isinstance(value, dict):
        raise PromotionError("promotion input must be a bounded JSON object")
    return value


def _recipe(ref):
    from vkm_corpus.update.operator import CoreOperatorConfig, operator_environment
    from vkm_corpus.update.serving import NativeServingProfile

    recipe = PromotionRecipe.model_validate(_read(ref))
    docs = {d.name: (d.file, _read(d.file)) for d in recipe.documents}
    if docs["operator"][0] != recipe.operator_config:
        raise PromotionError("recipe selects another operator input")
    config = CoreOperatorConfig.model_validate(docs["operator"][1])
    if not Path(recipe.operator_config.path).is_relative_to(Path(config.authority_root)):
        raise PromotionError("operator configuration is outside its independent authority")
    previous = recipe.selected_previous_release_sha256
    if (len(config.releases) != 2 or previous == config.candidate_release_sha256
            or previous not in {r.receiver_release_sha256 for r in config.releases}):
        raise PromotionError("exact candidate and retained previous releases required")
    releases = {r.receiver_release_sha256: r for r in config.releases}
    units = {r.sha256: r for r in config.units.releases}
    compose_validator = object.__new__(CoreUnitControl)
    compose_validator.config = config.units
    compose_validator.root = Path(config.units.control_root)
    required = {"operator": recipe.operator_config, "policy": config.policy}
    roots = {}
    generations, plans = {}, {}
    for name, key in (("candidate", config.candidate_release_sha256), ("previous", previous)):
        release, unit = releases[key], units[key]
        compose_validator._compose(unit)  # Pure fixed-contract validation only.
        for suffix, file in (("compose", unit.compose), ("environment", release.environment),
                             ("runtime", release.runtime), ("generation", release.generation),
                             ("plan", release.acceptance_plan)):
            required[name + "." + suffix] = file
            if name + "." + suffix not in docs or docs[name + "." + suffix][0] != file:
                raise PromotionError("recipe omits a bound release document")
        if getattr(release, "baseline_registration", None) is not None:
            required[name + ".baseline_registration"] = release.baseline_registration
            if (name + ".baseline_registration" not in docs
                    or docs[name + ".baseline_registration"][0] != release.baseline_registration):
                raise PromotionError("closed previous registration is missing from the source closure")
        runtime = RuntimeConfig.model_validate(docs[name + ".runtime"][1])
        env = operator_environment(release.environment)
        if (runtime.native_serving is None or runtime.policy != config.policy
                or record_hash(runtime) != unit.runtime_config_sha256):
            raise PromotionError("runtime/config/policy identity differs")
        required[name + ".native"] = runtime.native_serving
        if name + ".native" not in docs or docs[name + ".native"][0] != runtime.native_serving:
            raise PromotionError("native profile document is missing")
        NativeServingProfile.model_validate(docs[name + ".native"][1])
        if (env.get("VKM_UPDATE_RUNTIME_FILE") != release.runtime.path
                or env.get("VKM_SOURCE_POLICY_FILE") != runtime.policy.path
                or docs[name + ".compose"][1].get("services", {}).get("api", {}).get("environment") != env):
            raise PromotionError("rendered environment selects another runtime/policy")
        plan = AcceptancePlan.model_validate(docs[name + ".plan"][1])
        generation = GenerationManifest.model_validate(docs[name + ".generation"][1])
        if (generation.code_commit != plan.candidate.code_commit or generation.policy_sha256 != plan.candidate.policy_sha256
                or tuple(generation.components) != tuple(plan.candidate.components)
                or tuple(generation.services) != tuple(plan.candidate.services)
                or (unit.code_sha256, unit.dependencies_sha256, unit.access_sha256) !=
                   (plan.candidate.code_tree_sha256, plan.candidate.dependencies_sha256, plan.candidate.access_config_sha256)):
            raise PromotionError("generation and exact acceptance inputs differ")
        for receiver in ("api", "mcp"):
            label = name + "." + receiver
            required[label + ".effective"] = recipe.effective_configurations[label]
            if label + ".effective" not in docs or docs[label + ".effective"][0] != required[label + ".effective"]:
                raise PromotionError("full effective receiver configuration is missing")
            effective = docs[label + ".effective"][1]
            if (set(effective) != {"config", "host_config", "mounts", "networks"}
                    or record_hash(effective) != unit.units[receiver].config_sha256):
                raise PromotionError("effective native configuration hash differs")
            labels = effective.get("config", {}).get("Labels", {})
            if (labels.get("com.docker.compose.project") != unit.project
                    or labels.get("com.docker.compose.service") != receiver):
                raise PromotionError("effective receiver ownership differs")
        roots[name] = runtime
        generations[name], plans[name] = generation, plan
    # All BoundFile links in these control documents must be represented. This
    # retains the exact closure, including pack/graph/policy qualification files.
    def nested(value):
        if isinstance(value, dict):
            if set(value) == {"path", "sha256"}:
                yield BoundFile.model_validate(value)
            else:
                for item in value.values():
                    yield from nested(item)
        elif isinstance(value, list):
            for item in value:
                yield from nested(item)
    binaries = (config.units.docker, config.units.compose_binary)
    for binary in binaries:
        path = read_bound(binary)
        info = path.stat()
        if (any(p.is_symlink() for p in (path, *path.parents))
                or not stat.S_ISREG(info.st_mode) or info.st_nlink != 1):
            raise PromotionError("pinned native executable is indirect")
    known_refs = {(file.path, file.sha256) for file, _ in docs.values()} | {(f.path, f.sha256) for f in binaries}
    for _, (_, value) in docs.items():
        if any((file.path, file.sha256) not in known_refs for file in nested(value)):
            raise PromotionError("recipe has an unbound configuration/qualification link")
    if any(name not in docs or docs[name][0] != file for name, file in required.items()):
        raise PromotionError("recipe control closure differs")
    # Externally pinned executable bytes are not JSON; config links to docker
    # binaries are checked by native operator preflight, never interpreted here.
    return recipe, docs, config, units, roots, generations, plans


def _allow(mapping, document, parts, before, after, shadow, live, shadow_roots, live_roots):
    if not isinstance(before, str) or not isinstance(after, str) or (before, after) != (mapping.before, mapping.after):
        raise PromotionError("mapping does not describe the exact original scalar")
    final = str(parts[-1]) if parts else ""
    if mapping.kind == "PROJECT":
        registered = ((document == "operator" and len(parts) == 4 and
                       tuple(parts[:2]) == ("units", "releases") and final == "project")
                      or (document.endswith(".compose") and tuple(parts) == ("name",))
                      or (document.endswith(".effective") and tuple(parts) ==
                          ("config", "Labels", "com.docker.compose.project")))
        if (before, after) != ("vkm-core-shadow", "vkm-core") or not registered:
            raise PromotionError("project mapping is outside receiver ownership")
        return
    if mapping.kind in {"API_ENDPOINT", "MCP_ENDPOINT"}:
        field = "receiver_url" if mapping.kind == "API_ENDPOINT" else "mcp_receiver_url"
        if document != "operator" or tuple(parts) != ("units", field):
            raise PromotionError("only the registered receiver origins may change")
        if (before, after) != (getattr(shadow.units, field), getattr(live.units, field)):
            raise PromotionError("receiver endpoint mapping differs")
        return
    role = mapping.kind
    if role == "AUTHORITY_ROOT":
        pair = (shadow.authority_root, live.authority_root)
    elif role == "CONTROL_ROOT":
        key = "candidate" if document.startswith("candidate.") else "previous"
        pair = ((shadow_roots[key].runtime_root, live_roots[key].runtime_root)
                if final == "runtime_root" else (shadow.units.control_root, live.units.control_root))
    elif role == "QUALIFICATION_ROOT":
        pair = (shadow.qualification_root, live.qualification_root)
    else:
        field = {"ORIGINALS_ROOT": "originals_root", "CANONICAL_ROOT": "canonical_root", "EVIDENCE_ROOT": "evidence_root"}[role]
        key = "candidate" if document.startswith("candidate.") else "previous"
        pair = (getattr(shadow_roots[key], field), getattr(live_roots[key], field))
    a, b = pair
    if not a or not b or a == b or not (before == a or before.startswith(a.rstrip("/\\") + "/")
                                      or before.startswith(a.rstrip("/\\") + "\\")) or after != b + before[len(a):]:
        raise PromotionError("root mapping crosses its explicitly approved namespace")
    # A root-looking string in a model/resource/value is not a path capability.
    allowed = {"path", "authority_root", "control_root", "qualification_root", "runtime_root", "originals_root",
        "canonical_root", "evidence_root", "native_manifest", "policy_binding", "runtime_database", "dataset_root",
        "dataset_policy_file", "operator_token_file", "source", "target", "Source", "Destination",
        "VKM_DATA_ROOT", "VKM_WORK", "VKM_SOURCE_POLICY_FILE", "VKM_ACCESS_CONTEXT_FILE", "VKM_UPDATE_RUNTIME_FILE",
        "VKM_DEPLOYMENT_TOKEN_FILE", "VKM_API_TOKEN_FILE", "VKM_MCP_TOKEN_FILE", "VKM_DEPLOYMENT_GATE_FILE",
        "VKM_API_WRITE_TOKEN_FILE", "VKM_API_READ_CREDENTIALS_FILE", "VKM_NEO4J_PASSWORD_FILE", "VKM_RERANK_TOKEN_FILE", "VKM_EMBED_TOKEN_FILE",
        "VKM_PG_DSN_FILE", "VKM_EVIDENCE_REVIEWERS_FILE"}
    if final not in allowed and not (parts and "protected_roots" in parts):
        raise PromotionError("root mapping targets an unregistered semantic field")


def _normalize_documents(docs, config, units, role):
    """Replace only verified derived links, never discard a source digest."""
    value = json.loads(json.dumps({name: body for name, (_, body) in docs.items()}))
    references = {(file.path, file.sha256): name for name, (file, _) in docs.items()}
    def visit(item):
        if isinstance(item, dict):
            if set(item) == {"path", "sha256"} and (item["path"], item["sha256"]) in references:
                # Physical path still participates in explicit root mappings.
                return {"path": item["path"], "sha256": "DOCUMENT:" + references[(item["path"], item["sha256"]) ]}
            return {k: visit(v) for k, v in item.items()}
        if isinstance(item, list):
            return [visit(v) for v in item]
        return item
    value = visit(value)
    operator = value["operator"]
    release_roles = {config.candidate_release_sha256: "candidate", role.selected_previous_release_sha256: "previous"}
    operator["candidate_release_sha256"] = "candidate"
    operator["units"]["scope"] = "EXPLICIT_SHADOW_TO_LIVE"
    # These two original values are independently checked and retained in the
    # immutable binding/attestation; live scope is not shadow qualification.
    operator["isolation_attestation_sha256"] = "BOUND_ISOLATION_ATTESTATION"
    operator["releases"] = sorted(operator["releases"], key=lambda r: release_roles[r["receiver_release_sha256"]])
    for release in operator["releases"]:
        release["receiver_release_sha256"] = release_roles[release["receiver_release_sha256"]]
    raw_units = {r.sha256: body for r, body in zip(config.units.releases, operator["units"]["releases"])}
    operator["units"]["releases"] = [raw_units[key] for key in sorted(release_roles, key=release_roles.get)]
    for key in sorted(release_roles, key=release_roles.get):
        label, row = release_roles[key], raw_units[key]
        row["runtime_config_sha256"] = "DOCUMENT:" + label + ".runtime"
        for receiver in ("api", "mcp"):
            row["units"][receiver]["config_sha256"] = "DOCUMENT:" + label + "." + receiver + ".effective"
    return value


def preflight_promotion(approval: BoundFile, *, approved_binding_sha256: str,
                        current_policy_sha256: str, current_access_sha256: str,
                        selected_previous_generation_sha256: str) -> PromotionPreflight:
    """Read/fence the complete approval before any startup or selector side effect.

    The caller supplies independently observed policy/access/previous pins. This
    function intentionally never reports READY, loaded-model proof or admission.
    It must be repeated under the writer gate immediately before applying live.
    """
    original = _read(approval)
    binding = PromotionBinding.model_validate(original)
    if approval.sha256 != approved_binding_sha256 or binding.sha256 != approval.sha256:
        raise PromotionError("exact canonical promotion approval differs")
    shadow_recipe, sd, shadow, su, sr, sg, sp = _recipe(binding.shadow_recipe)
    live_recipe, ld, live, lu, lr, lg, lp = _recipe(binding.live_recipe)
    if (shadow.units.scope != "SHADOW_PRODUCTION" or live.units.scope != "PRODUCTION_SWITCH"
            or shadow.units.releases[0].project != "vkm-core-shadow" or live.units.releases[0].project != "vkm-core"):
        raise PromotionError("promotion requires explicit isolated shadow and live profiles")
    def overlap(x, y):
        a, b = Path(x).resolve(), Path(y).resolve()
        return a.is_relative_to(b) or b.is_relative_to(a)
    shadow_writes = (shadow.authority_root, shadow.units.control_root, shadow.qualification_root)
    live_writes = (live.authority_root, live.units.control_root, live.qualification_root)
    if (any(overlap(a, b) for a in shadow_writes for b in (*live_writes, *live.protected_roots))
            or any(overlap(a, b) for a in live_writes for b in shadow.protected_roots)
            or len({shadow.units.receiver_url, shadow.units.mcp_receiver_url,
                    live.units.receiver_url, live.units.mcp_receiver_url}) != 4):
        raise PromotionError("shadow/live roots or receiver endpoints cross isolation boundaries")
    if (shadow.expected_commit, shadow.expected_code_sha256, shadow.expected_dependencies_sha256,
        shadow.access_config_sha256, shadow.policy.sha256) != (
        live.expected_commit, live.expected_code_sha256, live.expected_dependencies_sha256,
        current_access_sha256, current_policy_sha256):
        raise PromotionError("code/dependencies/access/policy changed during promotion")
    if (live.access_config_sha256, live.policy.sha256) != (current_access_sha256, current_policy_sha256):
        raise PromotionError("current live access or policy is stale/revoked")
    plan = AcceptancePlan.model_validate(_read(binding.shadow_plan))
    if (plan != sp["candidate"] or plan != lp["candidate"] or plan.sha256 != binding.shadow_plan.sha256
            or plan.deployment_profile_sha256 != shadow.deployment_profile_sha256
            or plan.scope != ("SYNTHETIC" if binding.mode == "SYNTHETIC" else "SHADOW_PRODUCTION")
            or plan.drill_receipt_sha256 != binding.shadow_drill.sha256
            or sg["candidate"] != lg["candidate"] or sg["previous"] != lg["previous"]
            or lg["candidate"].acceptance_sha256 != binding.shadow_acceptance.sha256):
        raise PromotionError("original shadow plan/receipt/generation binding differs")
    if lg["previous"].sha256 != selected_previous_generation_sha256:
        raise PromotionError("whole coherent previous generation differs")
    attestation = IsolationAttestation.model_validate(_read(binding.isolation_attestation))
    expected = IsolationAttestation(shadow_profile_sha256=shadow.deployment_profile_sha256,
        live_profile_sha256=live.deployment_profile_sha256, shadow_project="vkm-core-shadow", live_project="vkm-core",
        shadow_control_root=shadow.units.control_root, live_control_root=live.units.control_root,
        shadow_api_endpoint=shadow.units.receiver_url, live_api_endpoint=live.units.receiver_url,
        shadow_mcp_endpoint=shadow.units.mcp_receiver_url, live_mcp_endpoint=live.units.mcp_receiver_url)
    if attestation != expected:
        raise PromotionError("isolation attestation targets another deployment")
    # Keep the original isolation attestation SHA exact; the promotion-specific
    # attestation is additional and cannot replace the shadow qualification.
    if plan.isolation_attestation_sha256 != shadow.isolation_attestation_sha256:
        raise PromotionError("original shadow isolation pin differs")
    previous = RetainedPrevious.model_validate(_read(binding.retained_previous))
    previous_gen = GenerationManifest.model_validate(_read(previous.generation))
    topology_before = RetainedTopology.model_validate(_read(previous.native_before))
    topology_after = RetainedTopology.model_validate(_read(previous.native_after_restore))
    if (previous_gen != lg["previous"] or previous.receiver_release_sha256 != live_recipe.selected_previous_release_sha256
            or topology_before != topology_after
            or topology_before.generation_sha256 != previous_gen.sha256
            or topology_before.receiver_release_sha256 != previous.receiver_release_sha256
            or topology_before.components != previous_gen.components or topology_before.services != previous_gen.services):
        raise PromotionError("retained previous/restore topology differs")
    recovery_sources = []
    for kind, ref in (("INDEPENDENT_BACKUP", previous.independent_backup), ("RESTORE", previous.restore_receipt)):
        report = RecoveryEvidence.model_validate(_read(ref))
        if (report.kind != kind or report.scope != ("SYNTHETIC" if binding.mode == "SYNTHETIC" else "FULL_COHERENT_PREVIOUS")
                or report.generation_sha256 != previous_gen.sha256
                or report.receiver_release_sha256 != previous.receiver_release_sha256
                or report.native_sha256 != previous.native_before.sha256):
            raise PromotionError("independent backup/restore recipe is partial or mismatched")
        source = _read(report.verifier_receipt)
        # The exact verifier receipt belongs to the operator approval. Actual
        # protocol authenticity is checked by its registered runtime verifier.
        if (source.get("status") != "PASS" or source.get("generation_sha256") != previous_gen.sha256):
            raise PromotionError("backup/restore verifier source is not complete")
        recovery_sources.append(report.verifier_receipt)
    closure = {r.sha256: r for r in binding.receipt_closure}
    closure[binding.shadow_drill.sha256] = binding.shadow_drill
    used = set()
    def read_receipt(digest):
        if digest not in closure:
            raise PromotionError("shadow receipt closure is incomplete")
        used.add(digest)
        ref = closure[digest]
        _read(ref)
        raw = Path(ref.path).read_bytes()
        if hashlib.sha256(raw).hexdigest() != ref.sha256:
            raise PromotionError("receipt changed during verification")
        return raw
    verifier = object.__new__(AcceptanceRegistrar)
    verifier.read = read_receipt
    verifier.closed_baseline_verifier = None
    def verify_closed(proof):
        from vkm_corpus.update.bootstrap import require_closed_baseline
        from vkm_corpus.update.deployment import PreviousAdmission
        previous_release = next(r for r in shadow.releases
            if r.receiver_release_sha256 == shadow_recipe.selected_previous_release_sha256)
        if previous_release.baseline_registration is None:
            raise PromotionError("closed shadow drill lacks its source baseline registration")
        qualified = require_closed_baseline(previous_release.baseline_registration, sg["previous"])
        if proof.get("startup_authority_sha256") != qualified.startup_authority_sha256:
            raise PromotionError("closed shadow drill startup authority differs")
        return PreviousAdmission(mode="CLOSED_BASELINE", qualification_sha256=record_hash(qualified),
            closed_owner_key=qualified.closed_owner_key)
    if binding.mode == "SHADOW_TO_LIVE":
        verifier.closed_baseline_verifier = verify_closed
    drill = verifier.require_drill(plan)
    if (drill["previous_components_sha256"] != components_sha256(sg["previous"])
            or drill["previous_services_sha256"] != services_sha256(sg["previous"])
            or drill["adapter_ids"] != ["core-receiver-release"]):
        raise PromotionError("shadow drill restored another coherent previous recipe")
    report = _read(binding.shadow_acceptance)
    expected_report = {"schema": "vkm-serving-acceptance/1", "scope": plan.scope, "plan_sha256": plan.sha256,
        "status": "PASS", **plan.candidate.model_dump(mode="json", exclude={"components", "services"}),
        "components_sha256": plan.candidate.components_sha256, "services_sha256": plan.candidate.services_sha256,
        "deployment_drill_sha256": plan.drill_receipt_sha256,
        "checks": dict.fromkeys(SERVING_CHECKS, "PASS"), "tools": dict.fromkeys(read_tool_names(), "PASS")}
    if set(report) != set(expected_report) | {"raw_probe_receipts"} or any(report.get(k) != v for k, v in expected_report.items()):
        raise PromotionError("full original shadow acceptance differs")
    _verify_probe_receipts(report, plan, read_receipt, schema="vkm-shadow-probe/1", plan_sha256=plan.sha256)
    if used != set(closure):
        raise PromotionError("closure contains unrelated or unverified receipts")
    if set(sd) != set(ld):
        raise PromotionError("shadow/live document inventories differ")
    a = _normalize_documents(sd, shadow, su, shadow_recipe)
    b = _normalize_documents(ld, live, lu, live_recipe)
    changes = {(m.document, m.pointer): m for m in binding.mappings}
    observed = set()
    def compare(before, after, document, parts=()):
        if before == after:
            return
        if isinstance(before, dict) and isinstance(after, dict) and set(before) == set(after):
            for key in before:
                compare(before[key], after[key], document, (*parts, key))
        elif isinstance(before, list) and isinstance(after, list) and len(before) == len(after):
            for i, (x, y) in enumerate(zip(before, after)):
                compare(x, y, document, (*parts, i))
        else:
            key = (document, _pointer(parts))
            if key not in changes:
                raise PromotionError("unapproved shadow/live difference: " + document + key[1])
            _allow(changes[key], document, parts, before, after, shadow, live, sr, lr)
            observed.add(key)
    for name in sorted(a):
        compare(a[name], b[name], name)
    if observed != set(changes):
        raise PromotionError("mapping includes an unused or unknown transformation")
    # Re-read exact byte pins after verification to fence concurrent replacement.
    for ref in (approval, binding.shadow_recipe, binding.live_recipe, binding.shadow_plan, binding.shadow_drill,
                binding.shadow_acceptance, binding.isolation_attestation, binding.retained_previous,
                previous.generation, previous.native_before, previous.native_after_restore,
                previous.independent_backup, previous.restore_receipt, *recovery_sources, *binding.receipt_closure,
                *(ref for ref, _ in sd.values()), *(ref for ref, _ in ld.values())):
        _read(ref)
    for ref in (shadow.units.docker, shadow.units.compose_binary, live.units.docker, live.units.compose_binary):
        read_bound(ref)
    return PromotionPreflight(binding.sha256, live.deployment_profile_sha256, shadow.deployment_profile_sha256,
        plan.sha256, binding.shadow_acceptance.sha256, lg["candidate"].sha256, previous_gen.sha256)
