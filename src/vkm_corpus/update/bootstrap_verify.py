"""Fresh immutable first-baseline evidence verification, independent of CURRENT.

This consumer restores an original CLOSED policy during a later transaction.
It never authorizes public/scientific reads or relies on today's CLOSED owner,
which correctly belongs to the later transaction until coherent restoration.
No candidate file from that later transaction is consulted.
"""
from __future__ import annotations

import hashlib
from pathlib import Path

from pydantic import TypeAdapter

from vkm_corpus.update.acceptance import _verify_probe_receipts, read_tool_names
from vkm_corpus.update.admission import AdmissionUnavailable, _ordinary_bytes
from vkm_corpus.update.bootstrap import (BaselineQualification, BaselineRegistration,
    BootstrapBoundaryReceipt, BootstrapIntent, BootstrapPreparation, BootstrapProbePlan,
    BootstrapStartupAuthority, require_independent_bootstrap_backup)
from vkm_corpus.update.bootstrap_probes import BOOTSTRAP_CHECKS, PROBE_SCHEMA, VERIFIED, _bindings
from vkm_corpus.update.contracts import GenerationManifest
from vkm_corpus.update.deployment import components_sha256, services_sha256
from vkm_corpus.update.generation import GenerationUnavailable
from vkm_corpus.update.operator_units import ComposeRelease, strict_json
from vkm_corpus.update.receiver import ReceiverIdentity
from vkm_corpus.update.runtime import BoundFile, RuntimeConfig
from vkm_evidence.contracts import Sha256, record_hash

MAX_BYTES = 2 * 1024 * 1024


def _fresh(ref: BoundFile) -> bytes:
    path = Path(ref.path)
    if not path.is_absolute() or ".." in path.parts:
        raise GenerationUnavailable("baseline evidence requires direct absolute immutable references")
    raw = _ordinary_bytes(path, MAX_BYTES)
    if hashlib.sha256(raw).hexdigest() != ref.sha256:
        raise GenerationUnavailable("baseline evidence bytes changed")
    return raw


def _model(ref, cls):
    value = cls.model_validate(strict_json(_fresh(ref)))
    if record_hash(value) != ref.sha256:
        raise GenerationUnavailable("baseline canonical typed evidence differs")
    return value


def _read_hash(root: Path, digest: str) -> bytes:
    TypeAdapter(Sha256).validate_python(digest)
    return _fresh(BoundFile(path=str(root / (digest + ".json")), sha256=digest))


def _native(ref, generation, release):
    proof = _model(ref, ReceiverIdentity)
    wanted = {"generation_sha256": generation.sha256, "runtime_config_sha256": release.runtime_config_sha256,
        "code_sha256": release.code_sha256, "dependencies_sha256": release.dependencies_sha256,
        "access_sha256": release.access_sha256, "components_sha256": components_sha256(generation),
        "services_sha256": services_sha256(generation), "admission_open": False,
        "read_contract_sha256": record_hash(sorted(read_tool_names()))}
    if any(getattr(proof, key) != value for key, value in wanted.items()):
        raise GenerationUnavailable("baseline closed native receiver context differs")
    return proof


def _preparation(ref, intent, startup, runtime):
    preparation = _model(ref, BootstrapPreparation)
    release = ComposeRelease.model_validate(preparation.receiver_release)
    pin = startup.candidate
    if (preparation.intent_sha256 != intent.sha256 or preparation.startup_authority_sha256 != startup.sha256
            or preparation.recipe_sha256 != record_hash(intent.recipe)
            or preparation.runtime_sha256 != record_hash(runtime)
            or release.compose != intent.recipe.compose or release.project != intent.recipe.project
            or release.runtime_config_sha256 != preparation.runtime_sha256
            or release.code_sha256 != pin.code_tree_sha256 or release.dependencies_sha256 != pin.dependencies_sha256
            or release.access_sha256 != pin.access_config_sha256
            or {unit: unit_pin.image_id for unit, unit_pin in release.units.items()} != intent.recipe.images
            or any(unit_pin.config_sha256 == "0" * 64 for unit_pin in release.units.values())):
        raise GenerationUnavailable("baseline captured receiver release differs from exact startup recipe")
    return preparation, release


def _private_report(ref, plan):
    report = strict_json(_fresh(ref))
    expected = {**_bindings(plan), "status": VERIFIED, "tools": dict.fromkeys(read_tool_names(), "PASS"),
                "checks": dict.fromkeys(BOOTSTRAP_CHECKS, "PASS")}
    if (not isinstance(report, dict) or set(report) != set(expected) | {"closed_authority_sha256", "raw_probe_receipts"}
            or any(report.get(key) != value for key, value in expected.items())):
        raise GenerationUnavailable("baseline private qualification is incomplete or grants different admission")
    TypeAdapter(Sha256).validate_python(report["closed_authority_sha256"])
    _verify_probe_receipts(report, plan.probes, lambda digest: _read_hash(Path(ref.path).parent, digest),
                           schema=PROBE_SCHEMA, plan_sha256=plan.sha256)
    return report


def _journals(boundary, authority, failed_preparation, failed_release, *, root, plan):
    previous, phases, receipts = None, [], []
    fields = {"schema", "request_key", "parent_sha256", "phase", "detail"}
    for digest in boundary.journal_receipts:
        item = strict_json(_read_hash(root, digest))
        if (not isinstance(item, dict) or set(item) != fields or item["schema"] != "vkm-deployment-event/1"
                or item["request_key"] != authority.request_key or item["parent_sha256"] != previous
                or not isinstance(item["phase"], str) or not item["phase"] or not isinstance(item["detail"], dict)):
            raise GenerationUnavailable("baseline boundary journal chain or original owner differs")
        phases.append(item["phase"])
        receipts.append(item)
        previous = digest
    if not phases or phases[-1] != "EMPTY_RESTORED":
        raise GenerationUnavailable("baseline boundary does not end in verified EMPTY restoration")
    attempts = [i for i, item in enumerate(receipts) if item["phase"] == "BOOTSTRAP_ATTEMPT"]
    if attempts:
        marker = receipts[attempts[-1]]["detail"]
        if (set(marker) != {"intent_sha256", "plan_sha256"}
                or marker["intent_sha256"] != failed_preparation.intent_sha256):
            raise GenerationUnavailable("baseline final attempt belongs to a different intent")
        attempted = strict_json(_read_hash(root, marker["plan_sha256"]))
        wanted = {"schema": "vkm-bootstrap-execution-plan/1", "status": "PLANNED_CLOSED", "scope": boundary.scope,
            "intent_sha256": failed_preparation.intent_sha256, "control_sha256": attempted.get("control_sha256"),
            "private_probe_plan_sha256": plan.sha256, "legacy_sha256": boundary.legacy_before_sha256,
            "empty_sha256": boundary.empty_before_sha256, "serving_admission": False, "scientific_admission": False}
        TypeAdapter(Sha256).validate_python(wanted["control_sha256"])
        if attempted != wanted:
            raise GenerationUnavailable("baseline final attempt is not the approved closed execution plan")
        receipts = receipts[attempts[-1] + 1:]
        phases = [item["phase"] for item in receipts]
    elif boundary.scope != "SYNTHETIC":
        raise GenerationUnavailable("production baseline omits its explicit execution attempt")
    main = [item for item in receipts if item["phase"] not in {"ADMISSION_CLOSED", "DRAINED"}]
    if [item["phase"] for item in main] != ["COLD_PREPARED", "CLOSED_NATIVE_STARTED", "FIRST_PRIVATE_PROBES_VERIFIED", "EMPTY_RESTORED",
            "COLD_PREPARED", "PARTIAL_NATIVE_START", "EMPTY_RESTORED"]:
        raise GenerationUnavailable("baseline boundary omits full-start or actual partial-start fallback")
    if (main[4]["detail"] != {"preparation_sha256": record_hash(failed_preparation),
            "receiver_release_sha256": failed_release.sha256}
            or main[5]["detail"] != {"native_sha256": boundary.failed_native_start_sha256}
            or set(main[2]["detail"]) != {"private_probe_sha256"}
            or any(item["detail"] != {"empty_sha256": boundary.empty_after_sha256,
                "legacy_sha256": boundary.legacy_after_sha256} for item in (main[3], main[6]))):
        raise GenerationUnavailable("baseline boundary journal does not bind preparation/start/restore")
    for start_index, end_index in ((phases.index("FIRST_PRIVATE_PROBES_VERIFIED"), phases.index("EMPTY_RESTORED")),
            (phases.index("PARTIAL_NATIVE_START"), len(phases) - 1)):
        if phases[start_index + 1:end_index] != ["ADMISSION_CLOSED", "DRAINED"]:
            raise GenerationUnavailable("baseline fallback was not closed and drained before removal")
    digest = main[2]["detail"]["private_probe_sha256"]
    _private_report(BoundFile(path=str(root / (digest + ".json")), sha256=digest), plan)
    return main


def require_closed_baseline(registration: BoundFile, manifest: GenerationManifest, *, production=True) -> BaselineQualification:
    """Consume a full registered first baseline, never an ordinary READY receipt.

    ``production=False`` is an explicit synthetic-test consumer. Native operator
    callers use the default and cannot borrow a SYNTHETIC receipt chain.
    Verification does not acquire locks, inspect/change selectors or start jobs.
    """
    try:
        reg = _model(registration, BaselineRegistration)
        if production and reg.scope != "BOOTSTRAP_SHADOW_PRODUCTION":
            raise GenerationUnavailable("synthetic baseline cannot qualify a production previous")
        intent = _model(reg.intent, BootstrapIntent)
        plan = _model(reg.probes, BootstrapProbePlan)
        startup = _model(intent.startup_authority, BootstrapStartupAuthority)
        boundary = _model(reg.boundary, BootstrapBoundaryReceipt)
        qualified = _model(reg.qualification, BaselineQualification)
        intent.require_plan(plan, startup)
        if intent.scope == "BOOTSTRAP_SHADOW_PRODUCTION" and intent.retained_startup != startup:
            raise GenerationUnavailable("production baseline omits the retained immutable startup topology")
        backup = require_independent_bootstrap_backup(intent)
        if not reg.scope == intent.scope == plan.scope == boundary.scope == qualified.scope:
            raise GenerationUnavailable("baseline evidence scopes differ")
        expected_manifest = GenerationManifest(components=startup.candidate.components, services=startup.candidate.services,
            code_commit=startup.candidate.code_commit, policy_sha256=startup.candidate.policy_sha256,
            acceptance_sha256=startup.sha256)
        if manifest != expected_manifest:
            raise GenerationUnavailable("baseline cannot restore another generation")
        wanted = {"intent_sha256": intent.sha256, "startup_authority_sha256": startup.sha256,
            "preparation_sha256": reg.preparation.sha256, "baseline_generation_sha256": manifest.sha256,
            "private_probe_receipt_sha256": reg.private_probe.sha256, "boundary_receipt_sha256": reg.boundary.sha256,
            "repeated_native_start_sha256": reg.native_start.sha256, "legacy_topology_sha256": intent.legacy.sha256,
            "closed_owner_key": startup.request_key}
        if any(getattr(qualified, key) != value for key, value in wanted.items()):
            raise GenerationUnavailable("baseline final qualification chain or original owner differs")
        control_root = Path(startup.control_root)
        refs = (registration, reg.intent, reg.probes, reg.preparation, reg.failed_preparation, reg.private_probe, reg.boundary,
            reg.native_start, reg.qualification, intent.startup_authority, intent.runtime, intent.environment,
            intent.recipe.compose, intent.independent_backup, intent.legacy.compose, *intent.legacy.selectors,
            *((backup.copy_verification, backup.restore_verification) if backup is not None else ()))
        if any(Path(ref.path).is_relative_to(control_root) for ref in refs):
            raise GenerationUnavailable("baseline authority and receipts belong to mutable serving state")
        for ref in refs: _fresh(ref)
        runtime = _model(intent.runtime, RuntimeConfig)
        preparation, release = _preparation(reg.preparation, intent, startup, runtime)
        failed_preparation, failed_release = _preparation(reg.failed_preparation, intent, startup, runtime)
        pin = startup.candidate
        if (runtime.bootstrap_startup != intent.startup_authority
                or Path(runtime.runtime_root) / "served" != control_root or runtime.policy.sha256 != pin.policy_sha256
                or runtime.expected_commit != pin.code_commit):
            raise GenerationUnavailable("baseline captured receiver release differs from exact startup recipe")
        _fresh(runtime.policy)
        _private_report(reg.private_probe, plan)
        legacy_hash = record_hash({"topology": intent.legacy.sha256,
            "native": {unit: value.model_dump(mode="json") for unit, value in intent.legacy.units.items()}})
        empty_hash = record_hash({"project": "vkm-core-shadow", "containers": [], "selectors": [],
            "networks": {n.name: {"id": n.network_id, "config_sha256": n.config_sha256} for n in intent.recipe.networks}})
        if (boundary.intent_sha256 != intent.sha256 or boundary.startup_authority_sha256 != startup.sha256
                or boundary.preparation_sha256 != reg.failed_preparation.sha256
                or boundary.legacy_before_sha256 != legacy_hash or boundary.legacy_after_sha256 != legacy_hash
                or boundary.empty_before_sha256 != empty_hash or boundary.empty_after_sha256 != empty_hash):
            raise GenerationUnavailable("baseline EMPTY fallback or retained legacy identity differs")
        receipt_root = Path(reg.qualification.path).parent
        main = _journals(boundary, startup, failed_preparation, failed_release, root=receipt_root, plan=plan)
        first_ref = BoundFile(path=str(receipt_root / (main[0]["detail"]["preparation_sha256"] + ".json")),
                             sha256=main[0]["detail"]["preparation_sha256"])
        first_preparation, first_release = _preparation(first_ref, intent, startup, runtime)
        if (main[0]["detail"] != {"preparation_sha256": first_ref.sha256, "receiver_release_sha256": first_release.sha256}
                or set(main[1]["detail"]) != {"receiver_release_sha256", "native_proof_sha256"}
                or main[1]["detail"]["receiver_release_sha256"] != first_release.sha256):
            raise GenerationUnavailable("baseline first closed start differs from initial preparation")
        first = _native(BoundFile(path=str(receipt_root / (main[1]["detail"]["native_proof_sha256"] + ".json")),
            sha256=main[1]["detail"]["native_proof_sha256"]), manifest, first_release)
        partial = strict_json(_read_hash(receipt_root, boundary.failed_native_start_sha256))
        expected_partial = {"schema": "vkm-bootstrap-partial-start/1", "scope": reg.scope,
            "intent_sha256": intent.sha256, "admission": "CLOSED", "units": {
                unit: {"container_id": failed_preparation.native_container_ids[unit], "running": unit == "api",
                       **failed_release.units[unit].model_dump(mode="json")} for unit in ("api", "mcp")}}
        if (partial != expected_partial or any(type(partial["units"][u]["running"]) is not bool for u in ("api", "mcp"))):
            raise GenerationUnavailable("baseline failed start is not the captured API-only closed native pair")
        repeated = _native(reg.native_start, manifest, release)
        if (first.instance == repeated.instance or first.nonce == repeated.nonce
                or (first.process_pid, first.process_start_ticks, first.pid_namespace_inode)
                == (repeated.process_pid, repeated.process_start_ticks, repeated.pid_namespace_inode)):
            raise GenerationUnavailable("baseline repeated native start reused the failed process/challenge")
        for ref in refs: _fresh(ref)
        return qualified
    except GenerationUnavailable:
        raise
    except (AdmissionUnavailable, OSError, ValueError, TypeError, KeyError, AttributeError) as exc:
        raise GenerationUnavailable("closed baseline evidence is unavailable or invalid") from exc
