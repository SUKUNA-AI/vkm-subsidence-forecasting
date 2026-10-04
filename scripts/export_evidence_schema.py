"""Deterministic published contracts; --check verifies without modifying files."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from vkm_evidence.contracts import EvidenceBatch, ScientificUseContext  # noqa: E402
from vkm_evidence.temporal import HistoricalReadContext  # noqa: E402
from vkm_evidence.coverage import CoverageLedger  # noqa: E402
from pydantic import TypeAdapter  # noqa: E402
from vkm_corpus.update.contracts import CampaignManifest, GenerationManifest, ServiceIdentity  # noqa: E402
from vkm_corpus.update.deployment import DeploymentProfile  # noqa: E402
from vkm_corpus.update.acceptance import AcceptancePlan  # noqa: E402
from vkm_corpus.update.runtime import RuntimeConfig  # noqa: E402
from vkm_corpus.update.serving import NativeServingProfile  # noqa: E402
from vkm_corpus.update.operator import CoreOperatorConfig  # noqa: E402
from vkm_corpus.update.operator_units import UnitControlConfig  # noqa: E402
from vkm_corpus.update.receiver import SignedReceiverIdentity, SignedMcpReceiverIdentity  # noqa: E402
from vkm_datasets.manifest import DatasetVersion, SCHEMA as DATASET_SCHEMA  # noqa: E402
from vkm_datasets.contracts import DatasetCatalogSnapshot, SourceVersionLink  # noqa: E402
from vkm_evidence.qualification import FrozenPlan, FrozenRegistration, GoldSet, PredictionSet, MatchAdjudication  # noqa: E402
from vkm_evidence.cli import TrustedQualificationRegistration  # noqa: E402
from vkm_evidence.migration import FrozenMigrationInputs, MigrationPlan, MigrationApproval  # noqa: E402
from vkm_evidence.extraction import ExtractionPlan, ExtractionJob, CandidateResponse, ExtractionModelIdentity  # noqa: E402
from vkm_corpus.update.pack_policy import PackPolicyRequest, PackPolicyQualification  # noqa: E402
from vkm_corpus.coverage.publication import PublicationRequest, PublicationDescriptor, PublicationApproval  # noqa: E402

MODELS = {"evidence_batch": EvidenceBatch, "object_coverage": CoverageLedger,
          "campaign": CampaignManifest, "generation": GenerationManifest, "update_runtime": RuntimeConfig,
          "qualification_plan": FrozenPlan, "qualification_registration": FrozenRegistration,
          "qualification_gold": GoldSet, "qualification_predictions": PredictionSet,
          "qualification_adjudication": MatchAdjudication,
          "qualification_preregistration_receipt": TrustedQualificationRegistration,
          "dataset_version": DatasetVersion, "dataset_catalogue": DatasetCatalogSnapshot,
          "source_version_links": TypeAdapter(tuple[SourceVersionLink, ...]),
          "service_identity": ServiceIdentity, "deployment_profile": DeploymentProfile,
          "shadow_acceptance_plan": AcceptancePlan,
          "scientific_use_context": ScientificUseContext, "historical_read_context": HistoricalReadContext,
          "native_serving_profile": NativeServingProfile,
          "core_operator": CoreOperatorConfig, "core_unit_control": UnitControlConfig,
          "receiver_identity": SignedReceiverIdentity, "mcp_receiver_identity": SignedMcpReceiverIdentity,
          "phase1_migration_inputs": FrozenMigrationInputs,
          "phase1_migration_plan": MigrationPlan, "phase1_migration_approval": MigrationApproval,
          "semantic_extraction_plan": ExtractionPlan, "semantic_extraction_job": ExtractionJob,
          "semantic_candidate_response": CandidateResponse, "semantic_model_identity": ExtractionModelIdentity,
          "late_pack_policy_request": PackPolicyRequest, "late_pack_policy_qualification": PackPolicyQualification,
          "accounting_publication_request": PublicationRequest,
          "accounting_publication": PublicationDescriptor, "accounting_publication_approval": PublicationApproval}


def dataset_version_schema():
    """Describe canonical as_dict() bytes, not the dataclass constructor.

    DatasetVersion deliberately has no Pydantic runtime dependency in vendor GIS
    environments. Python from_dict/relative_name still enforce cross-field,
    filesystem and temporal invariants that JSON Schema alone cannot establish.
    """
    result = TypeAdapter(DatasetVersion).json_schema()
    result["$comment"] = ("Serialized DatasetVersion.as_dict() contract. Admission additionally requires "
        "DatasetVersion.from_dict(), path containment, fresh member hashes and explicit policy checks; "
        "JSON Schema validation alone never admits a dataset.")
    props = result["properties"]
    props["schema"] = {"const": DATASET_SCHEMA, "type": "string"}
    result["required"] = sorted(props)
    result["additionalProperties"] = False
    props["dataset_id"]["pattern"] = r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$"
    props["files"]["minItems"] = 1
    props["entrypoints"].update(minItems=1, uniqueItems=True)
    props["source_ids"]["items"]["pattern"] = r"^VKM-SRC-\d+$"
    props["parents"]["items"]["pattern"] = r"^[a-f0-9]{64}$"
    for name in ("owner", "licence"):
        props[name]["pattern"] = r"\S"
    member = result["$defs"]["FileMember"]
    member["additionalProperties"] = False
    member["required"] = sorted(member["properties"])
    member["properties"]["path"]["minLength"] = 1
    member["properties"]["size_bytes"]["minimum"] = 0
    member["properties"]["sha256"]["pattern"] = r"^[a-f0-9]{64}$"
    member["properties"]["role"]["enum"] = ["ORIGINAL", "COMPANION", "METADATA"]
    policy = result["$defs"]["Policy"]
    policy["additionalProperties"] = False
    policy["properties"]["decision_ref"]["pattern"] = r"\S"
    return result


def model_schema(model):
    if model is DatasetVersion:
        return dataset_version_schema()
    if isinstance(model, TypeAdapter):
        return {"title": "SourceVersionLinks", **model.json_schema()}
    return model.model_json_schema()


def schemas():
    return {name + ".schema.json": (json.dumps(model_schema(model), ensure_ascii=False,
        sort_keys=True, indent=2) + "\n").encode("utf-8") for name, model in MODELS.items()}


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args(argv)
    target = ROOT / "schemas/evidence"
    for name, data in schemas().items():
        path = target / name
        if args.check:
            if not path.is_file() or path.read_bytes() != data:
                print("STALE " + path.relative_to(ROOT).as_posix())
                return 1
        else:
            target.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
    print("PASS" if args.check else "EXPORTED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
