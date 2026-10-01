"""Qualified local update adapters. Manifests select typed operations, never commands.

All writes are confined to runtime_root; production selectors and remote stores
need separately qualified deployment adapters. Native artifacts remain read only.
"""
from __future__ import annotations

import json
import hashlib
import os
import shutil
import signal
import subprocess
import sys
from pathlib import Path
from typing import Literal

from pydantic import Field, model_validator

from vkm_corpus.parquet.atomic import sha256_of, write_bytes
from vkm_corpus.update.contracts import CampaignManifest, ComponentIdentity, Stage
from vkm_corpus.update.cycle import UpdateCycle
from vkm_corpus.update.generation import GenerationCoordinator, GenerationUnavailable
from vkm_evidence.contracts import Identifier, Sha256, StrictModel, canonical_bytes, record_hash


class BoundFile(StrictModel):
    path: str = Field(min_length=1)
    sha256: Sha256


class Observation(StrictModel):
    component: Literal["DOCUMENT", "DATASET", "DUCKDB", "NAV", "DENSE", "LATE", "VISUAL", "EVIDENCE"]
    native_manifest: str = Field(min_length=1)
    policy_binding: str = Field(min_length=1)
    runtime_database: str | None = None
    dataset_root: str | None = None
    dataset_policy_file: str | None = None
    required: bool = True


class QualityGateBinding(StrictModel):
    plan_artifact: Identifier
    prediction_artifact: Identifier
    required_scope: dict[Identifier, tuple[Identifier, ...]] = Field(min_length=1)

    @model_validator(mode="after")
    def _scope(self):
        if any(not metrics or len(set(metrics)) != len(metrics) for metrics in self.required_scope.values()):
            raise ValueError("quality gate requires explicit unique metric scope")
        return self


class RuntimeConfig(StrictModel):
    schema_version: Literal["vkm-update-runtime/1"] = "vkm-update-runtime/1"
    runtime_root: str
    originals_root: str
    canonical_root: str | None = None
    evidence_root: str | None = None
    policy: BoundFile
    qualification_root: str
    expected_commit: str = Field(pattern=r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")
    dependency_locks: tuple[str, ...] = ()
    memory_budget_gib: float = Field(gt=0)
    memory_reserve_gib: float = Field(gt=0)
    worker_memory_gib: float = Field(gt=0)
    min_free_disk_gib: float = Field(gt=0)
    artifacts: dict[Identifier, BoundFile] = Field(default_factory=dict)
    observations: tuple[Observation, ...] = ()
    quality_gates: dict[Sha256, QualityGateBinding] = Field(default_factory=dict)
    native_serving: BoundFile | None = None
    # Authority is supplied by the operator, separately from incoming artifacts.
    publication_approval: BoundFile | None = None

    @model_validator(mode="after")
    def _separation(self):
        write = Path(self.runtime_root).resolve()
        for read in (self.originals_root, self.canonical_root, self.evidence_root):
            if read:
                protected = Path(read).resolve()
                if write.is_relative_to(protected) or protected.is_relative_to(write):
                    raise ValueError("runtime writes must be disjoint from protected roots")
        if self.memory_reserve_gib + self.worker_memory_gib > self.memory_budget_gib:
            raise ValueError("budget cannot reserve worker and coordinator")
        if len({s.component for s in self.observations}) != len(self.observations):
            raise ValueError("duplicate native component observer")
        if self.publication_approval is not None:
            authority = Path(self.publication_approval.path).resolve()
            if authority.is_relative_to(write) or (self.canonical_root and
                    authority.is_relative_to(Path(self.canonical_root).resolve())):
                raise ValueError("publication approval must be operator-owned outside delivery/write roots")
        return self

    def pipeline_config(self):
        from vkm_corpus.pipeline.config import PipelineConfig
        return PipelineConfig(data_root=Path(self.runtime_root) / "staging", resources_root=Path(self.originals_root),
            profile="production", expected_commit=self.expected_commit, dependency_locks=self.dependency_locks,
            memory_budget_gb=self.memory_budget_gib, memory_reserve_gb=self.memory_reserve_gib,
            source_memory_gb=self.worker_memory_gib, min_free_disk_gb=self.min_free_disk_gib,
            workers=1, ocr_concurrency=1, use_gpu_layout=False)


def contained(root: Path, relative: str) -> Path:
    if not relative or Path(relative).is_absolute() or ":" in relative or "\\" in relative or ".." in Path(relative).parts:
        raise ValueError("unsafe runtime relative path")
    path = root / relative
    if not path.resolve().is_relative_to(root.resolve()):
        raise ValueError("runtime path escapes root")
    for parent in (path, *path.parents):
        if parent == root:
            break
        if parent.is_symlink():
            raise ValueError("indirect runtime path")
    return path


def read_bound(ref: BoundFile) -> Path:
    path = Path(ref.path)
    if path.is_symlink() or not path.is_file() or sha256_of(path) != ref.sha256:
        raise ValueError("bound input changed or unavailable")
    return path


def bounded_subprocess(argv: list[str], *, timeout: int, memory_gib: float, cwd: Path) -> int:
    """Linux production enforcement: kill the entire owned process group on deadline.

    The only runtime caller supplies this module's fixed worker argv. Output is
    not copied into receipts (it could contain source text or credentials).
    """
    if sys.platform != "linux":
        raise RuntimeError("qualified process-tree enforcement is unavailable")
    # No preexec_fn: it can deadlock after fork in threaded service callers.
    wrapper = ("import os,resource,sys;limit=int(sys.argv[1]);"
               "resource.setrlimit(resource.RLIMIT_AS,(limit,limit));"
               "assert resource.getrlimit(resource.RLIMIT_AS)==(limit,limit);"
               "os.execv(sys.argv[2],sys.argv[2:])")
    env = dict(os.environ)
    env.pop("PYTHONSTARTUP", None)
    env.pop("PYTHONHOME", None)
    env["PYTHONPATH"] = str(Path(__file__).resolve().parents[2])
    env["PYTHONNOUSERSITE"] = "1"
    env.update(OPENBLAS_NUM_THREADS="1", OMP_NUM_THREADS="1", MKL_NUM_THREADS="1")
    command = [sys.executable, "-c", wrapper, str(int(memory_gib * 2**30)), *argv]
    process = subprocess.Popen(command, cwd=cwd, env=env, stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
    try:
        return process.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        process.wait()
        raise TimeoutError("stage process group exceeded deadline") from None


_OPTION_KEYS = {
    "VERIFY_ORIGINALS": set(), "EXTRACT": {"mode", "context_artifact"}, "COVERAGE": {"artifact"},
    "CANON_VALIDATE": {"artifact"}, "EVIDENCE_VALIDATE": {"revision"},
    "EVIDENCE_PROJECT": {"revision", "context_artifact"},
    "BUILD_SHADOW": {"kind", "artifact", "context_artifact", "metadata_artifact"},
}
from vkm_datasets.contracts import DATASET_OPTIONS
_OPTION_KEYS.update({name: set(model.model_fields) for name, model in DATASET_OPTIONS.items()})


class UpdateRuntime:
    def __init__(self, config: RuntimeConfig):
        self.config = config
        self.root = Path(config.runtime_root).resolve()

    def identity(self):
        from vkm_corpus.pipeline.context import producer_identity, resource_guard
        cfg = self.config.pipeline_config()
        identity = producer_identity(cfg)
        resource_guard(cfg)
        return identity

    def publication_validation_options(self):
        """Fresh operator authority and policy for the actual canonical adapter.

        The descriptor identifies the producer campaign; it need not equal a
        later code-only validation campaign. The operator freezes its precise
        descriptor hash in this independent runtime binding. An artifact from
        the delivery cannot grant itself approval.
        """
        from vkm_corpus.coverage.publication import PublicationApproval, _json
        from vkm_corpus.parquet.validator import ValidationOptions
        policy_path = read_bound(self.config.policy)
        approval = None
        ref = self.config.publication_approval
        if ref is not None:
            path = read_bound(ref)
            if any(p.is_symlink() for p in (path, *path.parents)):
                raise ValueError("indirect operator approval")
            approval = PublicationApproval.model_validate(_json(path, limit=1024 * 1024))
            read_bound(ref)
            if approval.policy_sha256 != self.config.policy.sha256:
                raise ValueError("publication approval policy differs from operator policy")
        return ValidationOptions(publication_approval=approval, policy_path=policy_path)

    def stage_bindings(self, campaign: CampaignManifest, stage: Stage) -> dict:
        inputs = {"inputs": [i.model_dump(mode="json") for i in campaign.inputs],
                  "artifacts": {k: v.sha256 for k, v in sorted(self.config.artifacts.items())}}
        if campaign.dataset_inputs:
            inputs["dataset_inputs"] = [i.model_dump(mode="json") for i in campaign.dataset_inputs]
        return {"input_sha256": record_hash(inputs),
                "config_sha256": record_hash({"runtime": self.config.model_dump(mode="json"),
                                               "options": stage.options, "operation": stage.operation})}

    def blockers(self, campaign):
        reasons = []
        if any(i.dataset_version_sha256 is not None for i in campaign.inputs):
            reasons.append("LEGACY_DATASET_INPUT_CONTRACT_UNSUPPORTED")
        try:
            from vkm_datasets.update import check_stages
            check_stages(self, campaign)
        except (OSError, ValueError, KeyError, TypeError) as exc:
            from vkm_datasets.catalogue import DatasetBlocked
            reasons.append(exc.reason if isinstance(exc, DatasetBlocked) else "DATASET_ADMISSION_BLOCKED")
        if campaign.code_commit != self.config.expected_commit:
            reasons.append("CONFIG_COMMIT_MISMATCH")
        try:
            read_bound(self.config.policy)
            if self.config.policy.sha256 != campaign.policy_sha256:
                reasons.append("POLICY_CHANGED")
            for artifact in self.config.artifacts.values():
                read_bound(artifact)
            if self.config.publication_approval is not None:
                self.publication_validation_options()
        except (OSError, ValueError):
            reasons.append("BOUND_INPUT_UNAVAILABLE")
        for stage in campaign.stages:
            opts = stage.options
            if stage.operation not in _OPTION_KEYS or set(opts) - _OPTION_KEYS.get(stage.operation, set()):
                reasons.append("ADAPTER_UNAVAILABLE:" + stage.stage_id)
                continue
            if self.stage_bindings(campaign, stage) != {"input_sha256": stage.input_sha256, "config_sha256": stage.config_sha256}:
                reasons.append("STAGE_BINDING_CHANGED:" + stage.stage_id)
            if stage.compute != "CPU":
                reasons.append("COMPUTE_ADAPTER_UNQUALIFIED:" + stage.stage_id)
            if stage.memory_gib > self.config.worker_memory_gib:
                reasons.append("STAGE_MEMORY_EXCEEDS_WORKER:" + stage.stage_id)
            if stage.operation == "EXTRACT" and opts.get("mode") != "native_prepare":
                reasons.append("EXTRACTION_ADAPTER_UNAVAILABLE:" + stage.stage_id)
            if stage.operation == "EXTRACT" or (stage.operation == "BUILD_SHADOW" and opts.get("kind") == "REGISTRY"):
                try:
                    self.require_source_access(campaign, stage)
                    if opts.get("kind") == "REGISTRY":
                        self.registry_inputs(campaign, stage)
                except (OSError, ValueError, KeyError):
                    reasons.append("SOURCE_ADMISSION_BLOCKED:" + stage.stage_id)
            if stage.operation in {"COVERAGE", "CANON_VALIDATE"} and opts.get("artifact") not in self.config.artifacts:
                reasons.append("ARTIFACT_REQUIRED:" + stage.stage_id)
            if stage.operation == "CANON_VALIDATE" and not self.config.canonical_root:
                reasons.append("CANONICAL_ROOT_REQUIRED")
            if stage.operation.startswith("EVIDENCE_"):
                if not self.config.evidence_root or not isinstance(opts.get("revision"), str) or len(opts["revision"]) != 64:
                    reasons.append("PINNED_EVIDENCE_REQUIRED")
                if stage.operation == "EVIDENCE_PROJECT" and opts.get("context_artifact") not in self.config.artifacts:
                    reasons.append("CONTEXT_ARTIFACT_REQUIRED")
            if stage.operation == "BUILD_SHADOW":
                if opts.get("kind") not in {"DUCKDB", "NAV_PACK", "REGISTRY"}:
                    reasons.append("SHADOW_ADAPTER_UNAVAILABLE:" + stage.stage_id)
                elif opts["kind"] != "REGISTRY" and opts.get("artifact") not in self.config.artifacts:
                    reasons.append("ARTIFACT_REQUIRED:" + stage.stage_id)
                if opts.get("kind") == "DUCKDB" and not self.config.canonical_root:
                    reasons.append("CANONICAL_ROOT_REQUIRED")
        return sorted(set(reasons))

    def require_source_access(self, campaign, stage):
        from vkm_corpus.contracts.access import AccessContext
        from vkm_corpus.contracts.policy_store import SourcePolicyStore
        ref = self.config.artifacts[stage.options["context_artifact"]]
        context = AccessContext.model_validate_json(read_bound(ref).read_bytes())
        # Preserve the operator's context: a local CPU worker does not upgrade
        # a CLOUD caller to LOCAL. Target visibility and access are independent.
        store = SourcePolicyStore(read_bound(self.config.policy), lambda: ())
        for item in campaign.inputs:
            if item.lifecycle == "ACTIVE" or stage.options.get("kind") == "REGISTRY":
                store.for_source(item.source_id).require(context)

    def registry_inputs(self, campaign, stage):
        """Pin the complete metadata closure consumed by the legacy importer."""
        from vkm_corpus.registry.sources import REGISTER_REL, load_register
        from vkm_corpus.registry.rules import lifecycle_for
        root = Path(self.config.originals_root).resolve()
        inventory = json.loads(read_bound(self.config.artifacts[stage.options["metadata_artifact"]]).read_bytes())
        expected = inventory["files"]
        actual = {p.relative_to(root).as_posix() for p in (root / "00_registry").rglob("*") if p.is_file()}
        if set(expected) != actual:
            raise ValueError("registry metadata closure changed")
        for rel, sha in expected.items():
            if sha256_of(contained(root, rel)) != sha:
                raise ValueError("registry metadata changed")
        rows, _ = load_register(root / REGISTER_REL)
        approved = {i.source_id: i for i in campaign.inputs}
        if {r["resource_id"] for r in rows} != set(approved):
            raise ValueError("registry campaign must inventory every source")
        for row in rows:
            item = approved[row["resource_id"]]
            lifecycle, _ = lifecycle_for(row["migration_status"], row["evidence_scope"])
            expected_lifecycle = {"ABSENT_BY_REGISTER": "MISSING"}.get(lifecycle.value, lifecycle.value)
            if (item.lifecycle != expected_lifecycle or row.get("sha256") != item.source_sha256 or
                    row.get("canonical_path") != item.logical_path or int(row["size_bytes"]) != item.size_bytes):
                raise ValueError("registry original differs from campaign")
        return root

    def _gate(self, digest):
        if not digest:
            return False
        path = contained(Path(self.config.qualification_root).resolve(), digest + ".json")
        try:
            raw = path.read_bytes()
            if hashlib.sha256(raw).hexdigest() != digest:
                return False
            report = json.loads(raw)
            binding = self.config.quality_gates.get(digest)
            if report.get("schema") == "vkm-qualification-report/1" or binding is not None:
                if binding is None or report.get("schema") != "vkm-qualification-report/1":
                    return False
                from vkm_evidence.qualification import FrozenPlan, PredictionSet, qualification_gate
                plan = FrozenPlan.model_validate_json(read_bound(self.config.artifacts[binding.plan_artifact]).read_bytes())
                predictions = PredictionSet.model_validate_json(read_bound(self.config.artifacts[binding.prediction_artifact]).read_bytes())
                if predictions.plan_sha256 != plan.sha256:
                    return False
                return qualification_gate(report, plan, record_hash(predictions), require_corpus=True,
                                          required_scope=binding.required_scope)
            return report.get("status") == "PASS"
        except (OSError, ValueError, KeyError, TypeError, AttributeError):
            return False

    def cycle(self, campaign):
        from vkm_corpus.pipeline.context import available_memory_bytes
        return UpdateCycle(self.root, Path(self.config.originals_root), producer_guard=self.identity,
            adapters={name: lambda stage, request, root: self.execute_stage(campaign, stage, request)
                      for name in _OPTION_KEYS}, memory_available=lambda: available_memory_bytes() / 2**30,
            gate_verifier=self._gate)

    def plan(self, campaign):
        reasons = self.blockers(campaign)
        try:
            base = self.cycle(campaign).plan(campaign)
            reasons.extend(base["reasons"])
        except (OSError, ValueError, RuntimeError):
            base = {"campaign_sha256": campaign.sha256, "producer_identity_sha256": None,
                    "stages": [s.model_dump(mode="json") for s in campaign.stages]}
            reasons.append("PRODUCER_GUARD_REJECTED")
        base.pop("plan_sha256", None)
        base.update(status="BLOCKED" if reasons else "READY", reasons=sorted(set(reasons)))
        # Runtime/config and operation options are already bound by stage hashes.
        return {**base, "plan_sha256": record_hash(base),
                "stage_bindings": {s.stage_id: self.stage_bindings(campaign, s) for s in campaign.stages}}

    def execute(self, campaign, confirmation, *, allow_gpu=False):
        plan = self.plan(campaign)
        if plan["status"] != "READY" or confirmation != plan["plan_sha256"]:
            raise ValueError("fresh qualified runtime plan confirmation required")
        # On READY the core cycle and wrapper hash identical plan bodies.
        return self.cycle(campaign).execute(campaign, confirmation, allow_gpu=allow_gpu)

    def status(self, campaign):
        state = self.cycle(campaign).status(campaign)
        return {"campaign": state, "generation": self.generation_status()}

    def generation_status(self, observer=None):
        observe = observer if observer is not None else lambda: observe_components(self.config.observations)
        return GenerationCoordinator(self.root / "served").status(observe)

    def require_startup(self, observer=None):
        if self.generation_status(observer)["status"] != "READY":
            raise GenerationUnavailable("required served component unavailable at startup")

    def rollback(self):
        # This API deliberately does not claim a multi-store rollback from a
        # metadata pointer alone. Drain/remote-selector adapters are not registered.
        return {"status": "BLOCKED", "reason": "QUALIFIED_DEPLOYMENT_RESTORE_ADAPTER_REQUIRED",
                "generation": self.generation_status()}

    def execute_stage(self, campaign, stage, request_id):
        if request_id != stage_request_id(campaign, stage):
            raise ValueError("stage request identity mismatch")
        reasons = self.blockers(campaign)
        if reasons:
            return {"status": "BLOCKED", "reason": reasons[0], "outputs": []}
        self.identity()  # fresh identity immediately before any write
        folder = contained(self.root, "attempts/" + request_id)
        result_path = folder / "result.json"
        if result_path.is_file():
            previous = json.loads(result_path.read_bytes())
            if previous.get("status") == "PASS":
                valid = all(contained(self.root, o["path"]).is_file() and
                    sha256_of(contained(self.root, o["path"])) == o["sha256"] for o in previous.get("outputs", []))
                return previous if valid else {"status": "BLOCKED", "reason": "PREVIOUS_OUTPUT_CHANGED", "outputs": []}
        folder.mkdir(parents=True, exist_ok=True)
        job = {"config": self.config.model_dump(mode="json"), "campaign": campaign.model_dump(mode="json"),
               "stage": stage.model_dump(mode="json"), "request_id": request_id}
        write_bytes(folder / "tmp", folder / "job.json", canonical_bytes(job))
        argv = [sys.executable, "-m", "vkm_corpus.update.runtime", "--worker", str(folder / "job.json")]
        code = bounded_subprocess(argv, timeout=stage.timeout_seconds, memory_gib=stage.memory_gib,
                                  cwd=Path(__file__).resolve().parents[3])
        if code != 0 or not result_path.is_file():
            return {"status": "FAIL", "reason": "WORKER_FAILED", "outputs": []}
        return json.loads(result_path.read_bytes())


def stage_request_id(campaign, stage):
    return record_hash({"campaign": campaign.sha256, "stage": stage.stage_id,
                        "input": stage.input_sha256, "config": stage.config_sha256})


def _artifact_ids(value):
    if isinstance(value, dict):
        for k, v in value.items():
            if k.endswith("artifact_id") and isinstance(v, str) and v.startswith("sha256:"):
                yield v
            else:
                yield from _artifact_ids(v)
    elif isinstance(value, list):
        for v in value:
            yield from _artifact_ids(v)


def _run_operation(runtime: UpdateRuntime, campaign, stage, folder: Path):
    cfg, opts = runtime.config, stage.options
    artifacts = lambda key: read_bound(cfg.artifacts[opts[key]])
    operation = stage.operation
    if operation in DATASET_OPTIONS:
        from vkm_datasets.update import run_operation
        from vkm_datasets.catalogue import DatasetBlocked
        try:
            return run_operation(runtime, campaign, stage, folder)
        except DatasetBlocked as exc:
            return {"status": "BLOCKED", "reason": exc.reason}
    if operation == "VERIFY_ORIGINALS":
        for item in campaign.inputs:
            if item.lifecycle == "ACTIVE":
                path = contained(Path(cfg.originals_root).resolve(), item.logical_path)
                if not path.is_file() or path.stat().st_size != item.size_bytes or sha256_of(path) != item.source_sha256:
                    raise ValueError("original identity changed")
        return {"status": "PASS", "verified_sources": sum(i.lifecycle == "ACTIVE" for i in campaign.inputs)}
    if operation == "COVERAGE":
        from vkm_evidence.coverage import CoverageLedger
        ledger = CoverageLedger.model_validate_json(artifacts("artifact").read_bytes())
        report = ledger.report()
        return {**report, "status": "PASS" if report["status"] == "ACCOUNTED" else "BLOCKED",
                "gate": "ACCOUNTABILITY_ONLY", "scientific_admission": "NOT_ESTABLISHED"}
    if operation.startswith("EVIDENCE_"):
        from vkm_evidence.journal import EvidenceJournal
        from vkm_evidence.query import EvidenceReader
        from vkm_evidence.validation import validate_reference_graph
        journal = EvidenceJournal(Path(cfg.evidence_root))
        if journal.revision != opts["revision"]:
            raise ValueError("evidence revision changed")
        from vkm_corpus.contracts.policy_store import SourcePolicyStore
        policy_store = SourcePolicyStore(read_bound(cfg.policy), lambda: ())
        reader = EvidenceReader(journal, source_policy=policy_store.for_source)
        revision, records = reader.view()
        validate_reference_graph(records)
        if operation == "EVIDENCE_VALIDATE":
            return {"status": "PASS", "revision": revision, "record_count": len(records),
                    "gate": "INTEGRITY_ONLY", "scientific_admission": "NOT_ESTABLISHED"}
        from vkm_corpus.contracts.access import AccessContext
        from vkm_evidence.projections import build_projection
        context = AccessContext.model_validate_json(artifacts("context_artifact").read_bytes())
        report = build_projection(reader, folder / "evidence", context, producer_identity=runtime.identity()["identity_sha256"])
        if journal.revision != revision:
            raise ValueError("evidence changed during projection")
        return {"status": "PASS", **report}
    if operation == "EXTRACT":
        from vkm_corpus.extract.model import SourceInput
        from vkm_corpus.pipeline.prepare import prepare_source, prep_path
        from vkm_corpus.pipeline.context import open_cache, open_store
        from vkm_corpus import ids
        run_id = ids.new_run_id()
        config = cfg.pipeline_config()
        store, cache = open_store(config, run_id, "update"), open_cache(config, run_id, "update")
        summaries = []
        outputs = set()
        try:
            for item in campaign.inputs:
                if item.lifecycle != "ACTIVE":
                    continue
                prep = prepare_source(config, store, cache, SourceInput(item.source_id, item.logical_path,
                    item.source_sha256, item.size_bytes))
                if prep.get("prepare_signature"):
                    path = prep_path(config.data_root, item.source_id, prep["prepare_signature"])
                    if path.is_file():
                        outputs.add(path.relative_to(runtime.root).as_posix())
                for aid in _artifact_ids(prep):
                    path = store.find(aid)
                    if path is None or sha256_of(path) != aid[7:]:
                        raise ValueError("native prepare artifact missing or changed")
                    outputs.add(path.relative_to(runtime.root).as_posix())
                summaries.append({"source_id": item.source_id, "status": prep.get("status"),
                                  "pages": len(prep.get("pages", [])), "errors": len(prep.get("errors", []))})
            ok = all(s["status"] == "PREPARED" and not s["errors"] for s in summaries)
            return {"status": "PASS" if ok else "BLOCKED", "gate": "NATIVE_PREPARE_ONLY", "sources": summaries,
                    "_external_outputs": sorted(outputs)}
        finally:
            cache.close()
            store.close()
    if operation == "CANON_VALIDATE" or (operation == "BUILD_SHADOW" and opts["kind"] == "DUCKDB"):
        from vkm_corpus.parquet.layout import CanonLayout
        from vkm_corpus.parquet.validator import validate
        manifest = json.loads(artifacts("artifact").read_bytes())
        layout = CanonLayout(Path(cfg.canonical_root)).require("CANONICAL")
        report = validate(layout, manifest, runtime.publication_validation_options())
        runtime.publication_validation_options()  # authority revoked while validating
        if report["status"] != "PASS" or operation == "CANON_VALIDATE":
            return {"status": report["status"], "blocking_failures": report["blocking_failures"],
                    "snapshot_id": manifest.get("snapshot_id")}
        from vkm_corpus.duckdb.build import attach_manifest, apply_sql, verify_fingerprints
        import duckdb
        db = folder / "shadow.duckdb"
        con = duckdb.connect(str(db))
        try:
            attach_manifest(con, layout, manifest, manifest_sha256=cfg.artifacts[opts["artifact"]].sha256)
            apply_sql(con)
            if verify_fingerprints(con, manifest):
                raise ValueError("shadow canonical fingerprints differ")
            con.execute("CHECKPOINT")
        finally:
            con.close()
        runtime.publication_validation_options()
        return {"status": "PASS", "snapshot_id": manifest["snapshot_id"], "gate": "SHADOW_DUCKDB_ONLY"}
    if operation == "BUILD_SHADOW" and opts["kind"] == "REGISTRY":
        from vkm_corpus.parquet.layout import init_root, open_root
        from vkm_corpus.registry.importer import import_registry
        destination = folder / "registry"
        layout = open_root(destination) if destination.exists() else init_root(destination, "STAGING")
        root = runtime.registry_inputs(campaign, stage)
        result = import_registry(layout, root, work_registry=root / "00_registry" / "work_registry",
            workers=1, code_revision=cfg.expected_commit, cli_command="vkm-corpus update registry")
        runtime.registry_inputs(campaign, stage)
        return {"status": "BLOCKED" if result.get("errors") else "PASS", "gate": "STAGING_REGISTRY_ONLY",
                "commit_id": result.get("commit_id"), "errors": result.get("errors")}
    if operation == "BUILD_SHADOW" and opts["kind"] == "NAV_PACK":
        from vkm_corpus.navigation.manifest import load_datasets
        from vkm_corpus.navigation.store import pack
        manifest_path = artifacts("artifact")
        _tables, identity, _manifest = load_datasets(manifest_path.parent, require_verified=True)
        destination = folder / "nav"
        destination.mkdir(exist_ok=True)
        for entry in identity["datasets"].values():
            target = destination / entry["path"]
            if target.exists() and sha256_of(target) != entry["sha256"]:
                raise ValueError("interrupted shadow NAV has changed bytes")
            if not target.exists():
                shutil.copyfile(manifest_path.parent / entry["path"], target)
        target = destination / "manifest.json"
        if target.exists() and sha256_of(target) != sha256_of(manifest_path):
            raise ValueError("interrupted shadow NAV manifest changed")
        if not target.exists():
            shutil.copyfile(manifest_path, target)
        identity.verify_unchanged()
        # The verified input tables otherwise retain a second full NAV in RAM
        # while pack() reads the shadow files. Packing is still CPU/memory bounded.
        _tables.clear()
        identity.tables.clear()
        result = pack(destination)
        return {"status": "PASS", "gate": "SHADOW_REPACK_ONLY", "snapshot_id": result["snapshot_id"]}
    return {"status": "BLOCKED", "reason": "ADAPTER_UNAVAILABLE"}


def worker(job_path: Path) -> int:
    job = json.loads(job_path.read_bytes())
    runtime = UpdateRuntime(RuntimeConfig.model_validate(job["config"]))
    campaign, stage = CampaignManifest.model_validate(job["campaign"]), Stage.model_validate(job["stage"])
    folder = contained(runtime.root, "attempts/" + job["request_id"])
    if (job_path.resolve() != (folder / "job.json").resolve() or stage not in campaign.stages or
            job["request_id"] != stage_request_id(campaign, stage)):
        raise ValueError("worker job identity mismatch")
    runtime.identity()
    if runtime.blockers(campaign):
        raise ValueError("worker qualifications changed")
    report = _run_operation(runtime, campaign, stage, folder)
    result = {"status": report["status"], "outputs": []}
    if stage.operation in DATASET_OPTIONS:
        for key in ("reason", "gate", "scientific_admission"):
            if key in report:
                result[key] = report[key]
    external = report.pop("_external_outputs", [])
    report_path = folder / "operation.json"
    write_bytes(folder / "tmp", report_path, canonical_bytes(report), overwrite=True)
    for output in sorted(folder.rglob("*")):
        if output.is_file() and output.name not in {"job.json", "result.json"} and "tmp" not in output.relative_to(folder).parts:
            if output.is_symlink():
                raise ValueError("indirect stage output")
            result["outputs"].append({"path": output.relative_to(runtime.root).as_posix(), "sha256": sha256_of(output)})
    for relative in external:
        output = contained(runtime.root, relative)
        result["outputs"].append({"path": relative, "sha256": sha256_of(output)})
    write_bytes(folder / "tmp", folder / "result.json", canonical_bytes(result), overwrite=True)
    return 0


def observe_components(specs: tuple[Observation, ...]) -> dict:
    """Read native identities plus an explicit policy binding; never echo expected generation values."""
    observed = {}
    for spec in specs:
        try:
            path = Path(spec.native_manifest)
            if path.is_symlink():
                raise ValueError("indirect component manifest")
            data = path.read_bytes()
            native = json.loads(data)
            digest = hashlib.sha256(data).hexdigest()
            policy_path = Path(spec.policy_binding)
            if policy_path.is_symlink():
                raise ValueError("indirect policy binding")
            binding = json.loads(policy_path.read_bytes())
            if binding.get("component_manifest_sha256") != digest or binding.get("status") != "PASS":
                raise ValueError("component policy binding missing or stale")
            component = spec.component
            parents = {}
            if component == "DOCUMENT":
                revision = native.get("snapshot_id")
            elif component == "DATASET":
                if not spec.dataset_root or not spec.dataset_policy_file:
                    raise ValueError("dataset native root and current policy required")
                from vkm_datasets.catalogue import verify_catalogue
                from vkm_corpus.contracts.policy_store import SourcePolicyStore
                current_policy_file = read_bound(BoundFile(path=spec.dataset_policy_file, sha256=binding["policy_sha256"]))
                policies = SourcePolicyStore(current_policy_file, lambda: ()).read()
                catalogue = verify_catalogue(Path(spec.dataset_root).absolute(), path.absolute(), policies=policies,
                                             policy_sha256=binding["policy_sha256"])
                read_bound(BoundFile(path=spec.dataset_policy_file, sha256=binding["policy_sha256"]))
                revision = catalogue.sha256
            elif component == "DUCKDB":
                if not spec.runtime_database or Path(spec.runtime_database).is_symlink():
                    raise ValueError("actual DuckDB identity unavailable")
                import duckdb
                with duckdb.connect(spec.runtime_database, read_only=True) as con:
                    rows = con.execute("SELECT snapshot_id, manifest_sha256 FROM meta.snapshot").fetchall()
                if rows != [(native.get("snapshot_id"), digest)]:
                    raise ValueError("DuckDB serves another canonical manifest")
                revision = rows[0][0]
                parents = {"DOCUMENT": revision}
            elif component == "NAV":
                if native.get("format") != "vkm-nav-manifest-v1" or native.get("identity_status", "SNAPSHOT_VERIFIED") != "SNAPSHOT_VERIFIED":
                    raise ValueError("NAV identity unverified")
                revision = native.get("snapshot", {}).get("snapshot_id")
                parents = {"DOCUMENT": revision}
                if not spec.runtime_database or Path(spec.runtime_database).is_symlink():
                    raise ValueError("actual NAV database identity unavailable")
                import duckdb
                with duckdb.connect(spec.runtime_database, read_only=True) as con:
                    rows = con.execute("SELECT meta_json FROM nav_meta").fetchall()
                if len(rows) != 1:
                    raise ValueError("ambiguous packed NAV identity")
                packed = json.loads(rows[0][0])
                if (packed.get("identity_status") != "SNAPSHOT_VERIFIED" or
                        packed.get("manifest_sha256") != digest or packed.get("snapshot") != native.get("snapshot")):
                    raise ValueError("packed NAV origin differs from declared manifest")
            elif component in {"DENSE", "LATE", "VISUAL"}:
                revision = native.get("pack_id")
                parents = {"DOCUMENT": native.get("snapshot_id")}
            elif component == "EVIDENCE":
                revision = native.get("commit_id") or native.get("evidence_revision")
                parents = binding.get("built_from", {})
            else:
                # A JSON sidecar cannot demonstrate what the running DuckDB serves.
                raise ValueError("native observer adapter unavailable")
            if not revision or any(not v for v in parents.values()):
                raise ValueError("component native revision missing")
            identity = ComponentIdentity(component=component, revision=revision, manifest_sha256=digest,
                policy_sha256=binding["policy_sha256"], built_from=parents, required=spec.required)
            observed[component] = identity.model_dump(mode="json")
        except Exception:
            if spec.required:
                raise GenerationUnavailable("required native component identity unavailable") from None
    return observed


def nav_compatibility(manifest_path: Path, *, canonical_snapshot: str, canonical_manifest_sha256: str,
                      packed_path: Path | None = None, required_datasets: tuple[str, ...] = ()) -> dict:
    """Read-only compatibility report; legacy identity is never guessed from a directory name.

    Checks bytes and Parquet footers, without loading the corpus into memory or
    requiring all 33 historical datasets. Missing capabilities remain explicit.
    """
    import pyarrow.parquet as pq
    from vkm_corpus.navigation.manifest import snapshot_identified
    path = Path(manifest_path)
    manifest = json.loads(path.read_bytes())
    entries = manifest.get("datasets")
    if not isinstance(entries, dict):
        return {"status": "BLOCKED", "reason": "DATASET_INVENTORY_REQUIRED", "rebuild_required": False}
    if not set(required_datasets).issubset(entries):
        return {"status": "BLOCKED", "reason": "MISSING_REQUIRED_CAPABILITIES",
                "missing": sorted(set(required_datasets) - entries.keys()), "rebuild_required": "DEPENDENCY_DIFF_REQUIRED"}
    for name, entry in entries.items():
        if not isinstance(entry, dict):
            raise ValueError("invalid NAV inventory")
        file = contained(path.parent, entry.get("path") or name + ".parquet")
        if not file.is_file() or not entry.get("sha256") or sha256_of(file) != entry["sha256"]:
            raise ValueError("NAV dataset hash unavailable or changed")
        footer = pq.ParquetFile(file)
        if entry.get("rows") is not None and entry["rows"] != footer.metadata.num_rows:
            raise ValueError("NAV dataset row count differs")
        if entry.get("columns") is not None and entry["columns"] != footer.schema_arrow.names:
            raise ValueError("NAV dataset columns differ")
    snapshot = manifest.get("snapshot") or {}
    if (not snapshot_identified(snapshot) or manifest.get("format") != "vkm-nav-manifest-v1" or
            manifest.get("identity_status", "SNAPSHOT_VERIFIED") != "SNAPSHOT_VERIFIED" or
            (manifest.get("inputs") and manifest["inputs"].get("identity_status") != "SNAPSHOT_VERIFIED") or
            any(part.get("imported") and part["imported"].get("bundle_identity_status") != "SNAPSHOT_VERIFIED"
                for part in (manifest.get("parts") or {}).values())):
        return {"status": "BLOCKED", "reason": "LEGACY_ORIGIN_ATTESTATION_REQUIRED", "datasets": len(entries),
                "rebuild_required": False, "repack_required": "AFTER_VERIFIED_METADATA_MIGRATION"}
    if snapshot.get("snapshot_id") != canonical_snapshot or snapshot.get("manifest_sha256") != canonical_manifest_sha256:
        return {"status": "BLOCKED", "reason": "CANONICAL_IDENTITY_MISMATCH", "datasets": len(entries),
                "rebuild_required": "DEPENDENCY_DIFF_REQUIRED"}
    repack = True
    if packed_path and Path(packed_path).is_file():
        import duckdb
        try:
            with duckdb.connect(str(packed_path), read_only=True) as con:
                rows = con.execute("SELECT meta_json FROM nav_meta").fetchall()
            packed = json.loads(rows[0][0]) if len(rows) == 1 else {}
            repack = not (packed.get("identity_status") == "SNAPSHOT_VERIFIED" and
                          packed.get("manifest_sha256") == sha256_of(path) and packed.get("snapshot") == snapshot)
        except (duckdb.Error, ValueError):
            repack = True
    return {"status": "PASS", "datasets": len(entries), "rebuild_required": False,
            "repack_required": repack, "embeddings_recompute_required": False,
            "scope": "CODE_ONLY_COMPATIBILITY_NOT_SCIENTIFIC_ADMISSION"}


def late_pack_compatibility(manifest_path: Path, *, canonical_snapshot: str, encoder: dict) -> dict:
    """Check the existing late-pack contract and bytes. Code-only changes do not encode data."""
    from vkm_corpus.embeddings.pack import compatibility, COMPATIBLE_FIELDS, PACK_SCHEMA
    path = Path(manifest_path)
    manifest = json.loads(path.read_bytes())
    if manifest.get("schema") != PACK_SCHEMA or not isinstance(manifest.get("pack_id"), str) or not manifest["pack_id"]:
        return {"status": "BLOCKED", "reason": "PACK_MANIFEST_CONTRACT_UNVERIFIED"}
    if any(k not in encoder for k in COMPATIBLE_FIELDS):
        return {"status": "BLOCKED", "reason": "PINNED_QUERY_ENCODER_REQUIRED"}
    if manifest.get("snapshot_id") != canonical_snapshot:
        return {"status": "BLOCKED", "reason": "PACK_SNAPSHOT_MISMATCH"}
    if compatibility(manifest, encoder) or any((manifest.get("config") or {}).get(k) != encoder[k] for k in COMPATIBLE_FIELDS):
        return {"status": "BLOCKED", "reason": "ENCODER_INCOMPATIBLE", "action": "DEPENDENCY_DIFF_REQUIRED"}
    if not manifest.get("files"):
        return {"status": "BLOCKED", "reason": "PACK_FILE_INVENTORY_REQUIRED"}
    for name, meta in manifest["files"].items():
        file = contained(path.parent, name)
        if not file.is_file() or file.stat().st_size != meta.get("bytes") or sha256_of(file) != meta.get("sha256"):
            raise ValueError("pack bytes changed")
    return {"status": "PASS", "repack_required": False, "embeddings_recompute_required": False,
            "scope": "CODE_ONLY_COMPATIBILITY_NOT_SCIENTIFIC_ADMISSION"}


if __name__ == "__main__":
    if len(sys.argv) != 3 or sys.argv[1] != "--worker":
        raise SystemExit(2)
    raise SystemExit(worker(Path(sys.argv[2])))
