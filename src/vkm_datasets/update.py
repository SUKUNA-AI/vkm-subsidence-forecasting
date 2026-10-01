"""Dataset adapters for the existing bounded update runtime; no arbitrary commands."""
from pathlib import Path
import importlib.util
import importlib.metadata
import sys

from vkm_corpus.contracts.access import AccessContext
from vkm_corpus.contracts.policy_store import SourcePolicyStore
from vkm_evidence.contracts import record_hash
from .catalogue import (DatasetBlocked, artifact, current_policy, policy_key, publish_catalogue,
                        publish_inspection, read_json, require_selection_policy, verify_catalogue, verify_inspection)
from .contracts import DATASET_OPTIONS, DatasetCatalogSnapshot, DatasetSelection
from .manifest import DatasetVersion, Registry, confined, discover, sha256, verify_members
from .policy import Policy


def _bound(runtime, alias):
    from vkm_corpus.update.runtime import read_bound
    return read_bound(runtime.config.artifacts[alias])


def _folder(runtime, campaign, stage):
    from vkm_corpus.update.runtime import stage_request_id
    return runtime.root / "attempts" / stage_request_id(campaign, stage)


def ancestors(campaign, stage):
    stages = {s.stage_id: s for s in campaign.stages}
    found, pending = set(), list(stage.depends_on)
    while pending:
        sid = pending.pop()
        if sid not in found:
            found.add(sid)
            pending.extend(stages[sid].depends_on)
    return [s for s in campaign.stages if s.stage_id in found]


def preceding(campaign, stage, operation, dataset_id, entrypoint=None):
    found = [s for s in ancestors(campaign, stage) if s.operation == operation and
             s.options.get("dataset_id") == dataset_id and
             (entrypoint is None or s.options.get("entrypoint") == entrypoint)]
    if len(found) != 1:
        raise DatasetBlocked("DATASET_DEPENDENCY_MISSING_OR_AMBIGUOUS")
    return found[0]


def _check_successor(version, old):
    if version.parents and old is None:
        raise DatasetBlocked("DATASET_PARENT_SELECTION_REQUIRED")
    if old is not None and old.version_sha256 != version.digest:
        if old.version_sha256 not in version.parents:
            raise DatasetBlocked("DATASET_PARENT_SELECTION_MISSING")
        if set(version.parents) != {old.version_sha256}:
            raise DatasetBlocked("DATASET_MULTIPARENT_UPDATE_UNQUALIFIED")


def _check_initial_scope(runtime, version):
    # Existing append-only history cannot be reset by omitting previous_snapshot.
    path = confined(runtime.root, "datasets/versions/" + version.dataset_id, must_exist=False)
    if path.exists() and any(p.stem != version.digest for p in path.glob("*.json")):
        raise DatasetBlocked("DATASET_PREVIOUS_CATALOGUE_REQUIRED")


def validate_input(runtime, campaign, item, context_alias):
    """Policy precedes manifest/native inspection; hashes are fresh on every call."""
    from vkm_corpus.update.runtime import read_bound
    context = AccessContext.model_validate(read_json(_bound(runtime, context_alias)))
    policy_path = read_bound(runtime.config.policy)
    policies = SourcePolicyStore(policy_path, lambda: ()).read()
    policy = policies.get(policy_key(item.dataset_id))
    if policy is None:
        raise DatasetBlocked("DATASET_POLICY_UNCLASSIFIED")
    policy.require(context)
    destination = "cloud" if context.execution == "CLOUD" else "local"
    # A granted evaluator context does not turn general intake into a sealed
    # evaluator. Enforce this before even opening the dataset manifest.
    Policy(policy.access_class, policy.experimental_role, policy.authority).require(destination)
    for link in item.source_links:
        parent = policies.get(link.source_id)
        if parent is None:
            raise DatasetBlocked("DATASET_SOURCE_POLICY_UNCLASSIFIED")
        parent.require(context)
        if not policy.preserves(parent):
            raise DatasetBlocked("DATASET_SOURCE_POLICY_WIDENING")
    path = _bound(runtime, item.manifest_artifact)
    version = DatasetVersion.from_dict(read_json(path))
    if version.dataset_id != item.dataset_id or version.digest != item.version_sha256:
        raise DatasetBlocked("DATASET_VERSION_BINDING_MISMATCH")
    current_policy(policies, version, context)
    version.policy.require(destination)
    if set(version.source_ids) != {s.source_id for s in item.source_links}:
        raise DatasetBlocked("DATASET_SOURCE_LINK_MISMATCH")
    register_sha = None
    if item.source_links:
        from vkm_corpus.registry.sources import load_register
        register = _bound(runtime, item.source_register_artifact)
        if register.stat().st_size > 32 * 1024 * 1024:
            raise DatasetBlocked("SOURCE_REGISTER_METADATA_LIMIT")
        rows, register_sha = load_register(register)
        if register_sha != runtime.config.artifacts[item.source_register_artifact].sha256:
            raise DatasetBlocked("SOURCE_REGISTER_CHANGED")
        sources = {r["resource_id"]: r for r in rows}
        if any(sources.get(link.source_id, {}).get("sha256") != link.source_sha256 for link in item.source_links):
            raise DatasetBlocked("DATASET_SOURCE_VERSION_MISMATCH")
    # confined also rejects indirect ancestors, unlike mere resolve containment.
    root = confined(Path(runtime.config.originals_root), item.originals_prefix, must_exist=False)
    if item.lifecycle == "ACTIVE":
        verify_members(root, version.files)
        actual = discover(root, version.entrypoints, additional=tuple(f.path for f in version.files if f.role == "METADATA"))
        if actual != version.files:
            raise DatasetBlocked("DATASET_COMPANION_CLOSURE_MISMATCH")
    _bound(runtime, item.manifest_artifact)
    read_bound(runtime.config.policy)
    return version, root, policy, context, destination, policies, register_sha


def _request(runtime, campaign, stage, version, context):
    opts = DATASET_OPTIONS[stage.operation].model_validate(stage.options)
    implementation = {name: sha256(Path(__file__).with_name(name))
                      for name in ("workbooks.py", "manifest.py", "update.py", "catalogue.py")}
    engine = {"python": sys.version, "implementation_sha256": implementation}
    suffix = Path(opts.entrypoint).suffix.lower()
    if suffix in {".tab", ".mif", ".gpkg"}:
        from .gis import _engine, _request_identity, VectorLimits
        gdal, _ = _engine()
        _, engine["vector_request_sha256"] = _request_identity(version, opts.entrypoint,
            "cloud" if context.execution == "CLOUD" else "local", opts.keys, VectorLimits(**opts.vector_limits), gdal)
    elif suffix == ".xls":
        try:
            engine["xlrd"] = importlib.metadata.version("xlrd")
        except importlib.metadata.PackageNotFoundError as exc:
            raise DatasetBlocked("XLS_RUNTIME_UNAVAILABLE") from exc
    return {"schema": "vkm-dataset-inspection-request/1", "version": version.digest,
            "policy_sha256": campaign.policy_sha256, "context": context.model_dump(mode="json"),
            "options": opts.model_dump(mode="json"), "engine": engine,
            "producer_identity_sha256": campaign.producer_identity_sha256}


def check_stages(runtime, campaign):
    items = {i.dataset_id: i for i in campaign.dataset_inputs}
    for stage in campaign.stages:
        if stage.operation not in DATASET_OPTIONS:
            continue
        opts = DATASET_OPTIONS[stage.operation].model_validate(stage.options)
        selected = opts.dataset_ids if stage.operation == "DATASET_REGISTER" else (opts.dataset_id,)
        for did in selected:
            item = items[did]
            version, root, policy, context, destination, policies, _ = validate_input(runtime, campaign, item, opts.context_artifact)
            if stage.operation != "DATASET_VERIFY":
                preceding(campaign, stage, "DATASET_VERIFY", did)
            if stage.operation in {"DATASET_INSPECT", "DATASET_CONVERT"}:
                if item.lifecycle != "ACTIVE" or opts.entrypoint not in version.entrypoints:
                    raise DatasetBlocked("DATASET_ENTRYPOINT_NOT_ACTIVE")
                suffix = Path(opts.entrypoint).suffix.lower()
                if suffix not in {".xlsx", ".xls", ".tab", ".mif", ".gpkg"}:
                    raise DatasetBlocked("DATASET_INSPECTOR_UNQUALIFIED")
                if suffix in {".tab", ".mif", ".gpkg"}:
                    if importlib.util.find_spec("osgeo") is None:
                        raise DatasetBlocked("GDAL_RUNTIME_UNAVAILABLE")
                elif suffix == ".xls" and importlib.util.find_spec("xlrd") is None:
                    raise DatasetBlocked("XLS_RUNTIME_UNAVAILABLE")
            if stage.operation == "DATASET_CONVERT":
                if suffix not in {".tab", ".mif", ".gpkg"}:
                    raise DatasetBlocked("DATASET_CONVERSION_KIND_MISMATCH")
                inspection = preceding(campaign, stage, "DATASET_INSPECT", did, opts.entrypoint)
                earlier = DATASET_OPTIONS[inspection.operation].model_validate(inspection.options)
                if earlier.keys != opts.keys or earlier.vector_limits != opts.vector_limits:
                    raise DatasetBlocked("DATASET_INSPECTION_CONVERSION_CONFIG_MISMATCH")
                bundle = _folder(runtime, campaign, stage) / "conversion"
                if bundle.exists():
                    if not (bundle / "receipt.json").is_file():
                        raise DatasetBlocked("INCOMPLETE_CONVERSION_REQUIRES_NEW_CAMPAIGN")
                    from .gis import _engine, _request_identity, VectorLimits, verify_conversion_bundle
                    gdal, _ = _engine()
                    _, request = _request_identity(version, opts.entrypoint, destination, opts.keys,
                                                   VectorLimits(**opts.vector_limits), gdal)
                    verify_conversion_bundle(bundle, version.digest, expected_request=request)
            if stage.operation == "DATASET_INSPECT":
                folder = _folder(runtime, campaign, stage)
                marker = folder / "inspection-receipt.json"
                if marker.exists():
                    verify_inspection(runtime.root, artifact(runtime.root, marker), version_sha256=version.digest,
                                      request_sha256=record_hash(_request(runtime, campaign, stage, version, context)))
                elif (folder / "inspection.json").exists():
                    raise DatasetBlocked("INCOMPLETE_INSPECTION_REQUIRES_NEW_CAMPAIGN")
            if stage.operation == "DATASET_REGISTER" and item.lifecycle == "ACTIVE":
                for name in version.entrypoints:
                    preceding(campaign, stage, "DATASET_INSPECT", did, name)
                    if Path(name).suffix.lower() in {".tab", ".mif", ".gpkg"}:
                        preceding(campaign, stage, "DATASET_CONVERT", did, name)
            if stage.operation == "DATASET_REGISTER" and version.parents and not opts.previous_snapshot_artifact:
                raise DatasetBlocked("DATASET_PARENT_SELECTION_REQUIRED")
            if stage.operation == "DATASET_REGISTER" and not opts.previous_snapshot_artifact:
                _check_initial_scope(runtime, version)
        if stage.operation == "DATASET_REGISTER" and opts.previous_snapshot_artifact:
            replacements = {did: policies[policy_key(did)] for did in selected}
            previous = verify_catalogue(runtime.root, _bound(runtime, opts.previous_snapshot_artifact),
                policies=policies, context=context, replacement_policies=replacements)
            previous_by_id = {entry.dataset_id: entry for entry in previous.entries}
            for did in selected:
                replacement, *_ = validate_input(runtime, campaign, items[did], opts.context_artifact)
                _check_successor(replacement, previous_by_id.get(did))
                if did not in previous_by_id:
                    _check_initial_scope(runtime, replacement)
            for entry in previous.entries:
                if entry.dataset_id not in selected:
                    require_selection_policy(policies, entry, context)
                else:
                    replacement, *_ = validate_input(runtime, campaign, items[entry.dataset_id], opts.context_artifact)
                    newer_policy = policies[policy_key(entry.dataset_id)]
                    if not newer_policy.preserves(entry.policy):
                        raise DatasetBlocked("DATASET_POLICY_DOWNGRADE")
                    _check_successor(replacement, entry)


def _proof(runtime, campaign, parent):
    # The authoritative cycle receipt, not a guessed operation.json, pins outputs.
    state = runtime.cycle(campaign).status(campaign)
    entry = state["stages"].get(parent.stage_id, {})
    if entry.get("status") != "PASS":
        raise DatasetBlocked("DATASET_DEPENDENCY_NOT_COMPLETE")
    receipt = read_json(runtime.root / (entry["receipt_sha256"] + ".receipt.json"))
    path = _folder(runtime, campaign, parent) / "operation.json"
    relative = path.relative_to(runtime.root).as_posix()
    output = next((o for o in receipt["outputs"] if o["path"] == relative), None)
    if output is None or sha256(path) != output["sha256"]:
        raise DatasetBlocked("DATASET_DEPENDENCY_PROOF_CHANGED")
    return read_json(path)


def run_operation(runtime, campaign, stage, folder):
    from .contracts import DatasetArtifact
    opts = DATASET_OPTIONS[stage.operation].model_validate(stage.options)
    items = {i.dataset_id: i for i in campaign.dataset_inputs}
    if stage.operation == "DATASET_REGISTER":
        _, _, _, context, _, policies, _ = validate_input(runtime, campaign, items[opts.dataset_ids[0]], opts.context_artifact)
        previous = verify_catalogue(runtime.root, _bound(runtime, opts.previous_snapshot_artifact), policies=policies,
            context=context, replacement_policies={did: policies.get(policy_key(did)) for did in opts.dataset_ids}) if opts.previous_snapshot_artifact else None
        entries = {e.dataset_id: e for e in previous.entries} if previous else {}
        outputs = []
        for did in opts.dataset_ids:
            item = items[did]
            version, root, policy, context, _, policies, register_sha = validate_input(runtime, campaign, item, opts.context_artifact)
            _proof(runtime, campaign, preceding(campaign, stage, "DATASET_VERIFY", did))
            inspections, conversions = {}, {}
            if item.lifecycle == "ACTIVE":
                for name in version.entrypoints:
                    inspected = _proof(runtime, campaign, preceding(campaign, stage, "DATASET_INSPECT", did, name))
                    if inspected.get("dataset_version") != version.digest or inspected.get("entrypoint") != name:
                        raise DatasetBlocked("DATASET_DEPENDENCY_VERSION_MISMATCH")
                    inspections[name] = DatasetArtifact.model_validate(inspected["inspection_receipt"])
                    if Path(name).suffix.lower() in {".tab", ".mif", ".gpkg"}:
                        converted = _proof(runtime, campaign, preceding(campaign, stage, "DATASET_CONVERT", did, name))
                        if converted.get("dataset_version") != version.digest or converted.get("entrypoint") != name:
                            raise DatasetBlocked("DATASET_DEPENDENCY_VERSION_MISMATCH")
                        conversions[name] = DatasetArtifact.model_validate(converted["conversion_receipt"])
            old = entries.get(did)
            _check_successor(version, old)
            if old is None:
                _check_initial_scope(runtime, version)
            if old and not policy.preserves(old.policy):
                raise DatasetBlocked("DATASET_POLICY_DOWNGRADE")
            path = Registry(runtime.root / "datasets" / "versions").register(version)
            outputs.append(path.relative_to(runtime.root).as_posix())
            inherited = {(x.source_id, x.source_sha256, x.relation): x
                         for x in (*old.source_links, *old.inherited_source_links)} if old else {}
            entries[did] = DatasetSelection(dataset_id=did, version_sha256=version.digest,
                manifest=artifact(runtime.root, path), lifecycle=item.lifecycle, reason=item.reason, policy=policy,
                source_links=item.source_links, source_register_sha256=register_sha,
                inherited_source_links=tuple(inherited[k] for k in sorted(inherited)),
                inspections=inspections, conversions=conversions)
        # Carried-forward entries may not bypass a current policy change.
        for entry in entries.values():
            require_selection_policy(policies, entry, context)
        snapshot = DatasetCatalogSnapshot(policy_sha256=campaign.policy_sha256,
            producer_identity_sha256=campaign.producer_identity_sha256,
            previous_snapshot_sha256=previous.sha256 if previous else None,
            entries=tuple(entries[k] for k in sorted(entries)))
        # Validate all outputs before committing the catalogue, by the same checks
        # used for readers. Publication marker itself is immutable and last.
        from .catalogue import write_once
        from vkm_evidence.contracts import canonical_bytes
        pending = folder / (snapshot.sha256 + ".json")
        write_once(pending, canonical_bytes(snapshot))
        verify_catalogue(runtime.root, pending, policies=policies, policy_sha256=campaign.policy_sha256)
        for did in opts.dataset_ids:
            validate_input(runtime, campaign, items[did], opts.context_artifact)
        path = publish_catalogue(runtime.root, snapshot)
        outputs.append(path.relative_to(runtime.root).as_posix())
        return {"status": "PASS", "gate": "REGISTERED_NATIVE_DATASETS_NOT_SCIENTIFIC_ADMISSION",
            "catalogue_sha256": snapshot.sha256, "catalogue": artifact(runtime.root, path).model_dump(mode="json"),
            "_external_outputs": outputs, "scientific_admission": "NOT_ESTABLISHED"}
    item = items[opts.dataset_id]
    version, root, policy, context, destination, _, _ = validate_input(runtime, campaign, item, opts.context_artifact)
    result = {"status": "PASS", "dataset_id": item.dataset_id, "dataset_version": version.digest,
              "scientific_admission": "NOT_ESTABLISHED"}
    if stage.operation == "DATASET_VERIFY":
        return {**result, "gate": "ORIGINAL_BYTES_AND_COMPANIONS" if item.lifecycle == "ACTIVE" else
                "DATASET_LIFECYCLE_METADATA_ONLY", "lifecycle": item.lifecycle}
    _proof(runtime, campaign, preceding(campaign, stage, "DATASET_VERIFY", item.dataset_id))
    if stage.operation == "DATASET_INSPECT":
        from .workbooks import inspect_workbook, Limits
        from .gis import inspect_vector, VectorLimits
        def produce():
            if Path(opts.entrypoint).suffix.lower() in {".xlsx", ".xls"}:
                return inspect_workbook(root, version, opts.entrypoint, include_values=opts.include_values,
                                        destination=destination, limits=Limits(**opts.workbook_limits))
            return {**inspect_vector(root, version, opts.entrypoint, destination=destination,
                                  keys=opts.keys, limits=VectorLimits(**opts.vector_limits)),
                    "dataset_id": version.dataset_id, "entrypoint": opts.entrypoint}
        ref = publish_inspection(runtime.root, folder, version, opts.entrypoint,
                                _request(runtime, campaign, stage, version, context), produce)
        validate_input(runtime, campaign, item, opts.context_artifact)
        return {**result, "gate": "NATIVE_INSPECTION_ONLY", "entrypoint": opts.entrypoint,
                "inspection_receipt": ref.model_dump(mode="json")}
    from .gis import convert_to_gpkg, ConversionBlocked, VectorLimits
    _proof(runtime, campaign, preceding(campaign, stage, "DATASET_INSPECT", item.dataset_id, opts.entrypoint))
    try:
        converted = convert_to_gpkg(root, version, opts.entrypoint, folder / "conversion", destination=destination,
                                    keys=opts.keys, limits=VectorLimits(**opts.vector_limits))
    except ConversionBlocked as exc:
        return {"status": "BLOCKED", "reason": exc.reason}
    if converted["status"] != "PASS_CONVERSION":
        return {"status": "BLOCKED", "reason": "DATASET_CONVERSION_REJECTED"}
    validate_input(runtime, campaign, item, opts.context_artifact)
    return {**result, "gate": "LOSSLESS_NATIVE_CONVERSION_NOT_SCIENTIFIC_ADMISSION", "entrypoint": opts.entrypoint,
            "conversion_receipt": artifact(runtime.root, folder / "conversion" / "receipt.json").model_dump(mode="json")}
