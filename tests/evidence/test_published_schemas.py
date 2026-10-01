import importlib.util
import copy
import json
from pathlib import Path

import jsonschema
import pytest


def exporter():
    root = Path(__file__).resolve().parents[2]
    spec = importlib.util.spec_from_file_location("export_evidence_schema", root / "scripts/export_evidence_schema.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return root, module


def test_published_evidence_and_campaign_schemas_match_models():
    root, module = exporter()
    for name, data in module.schemas().items():
        assert (root / "schemas/evidence" / name).read_bytes() == data


def test_additive_dataset_and_deployment_contracts_have_valid_json_schemas():
    _, module = exporter()
    output = module.schemas()
    assert {name + ".schema.json" for name in (
        "dataset_version", "dataset_catalogue", "source_version_links", "service_identity",
        "deployment_profile", "shadow_acceptance_plan", "scientific_use_context", "historical_read_context",
        "native_serving_profile", "phase1_migration_inputs", "phase1_migration_plan",
        "phase1_migration_approval", "semantic_extraction_plan", "semantic_extraction_job",
        "semantic_candidate_response", "semantic_model_identity", "late_pack_policy_request",
        "late_pack_policy_qualification")} <= output.keys()
    for raw in output.values():
        jsonschema.Draft202012Validator.check_schema(json.loads(raw))
    generation = json.loads(output["generation.schema.json"])
    assert "ServiceIdentity" in generation["$defs"] and "services" in generation["properties"]
    assert "dataset_inputs" in json.loads(output["campaign.schema.json"])["properties"]
    migration = json.loads(output["phase1_migration_plan.schema.json"])
    assert migration["properties"]["scientific_admission"]["const"] == "NOT_ESTABLISHED"
    assert {"inputs", "base_revision", "source_policies", "canonical_snapshot_sha256"} <= set(migration["required"])
    approval = json.loads(output["phase1_migration_approval.schema.json"])
    assert {"plan_sha256", "base_revision", "publisher", "authority"} <= set(approval["required"])


def dataset_payload():
    from vkm_datasets.manifest import DatasetVersion, FileMember
    from vkm_datasets.policy import Policy

    return DatasetVersion("SYNTHETIC-GIS", (
        FileMember("Карта.tab", 17, "a" * 64), FileMember("Карта.dat", 23, "b" * 64, "COMPANION")),
        ("Карта.tab",), Policy("PRIVATE_LOCAL_ONLY", "INPUT", "owner-synthetic-decision"),
        owner="synthetic", licence="UNKNOWN", received_at="2026-10-01T00:00:00Z",
        source_ids=("VKM-SRC-001",)).as_dict()


def test_dataset_schema_describes_canonical_serialization_without_renaming_legacy_schema():
    from vkm_datasets.manifest import DatasetVersion, SCHEMA

    _, module = exporter()
    schema = module.dataset_version_schema()
    value = dataset_payload()
    assert value["schema"] == SCHEMA == "vkm-dataset-version-v1"
    assert set(schema["required"]) == set(value)
    jsonschema.Draft202012Validator(schema).validate(value)
    assert DatasetVersion.from_dict(value).as_dict() == value


@pytest.mark.parametrize("mutation", ["schema", "role", "sha", "size", "extra", "missing", "policy"])
def test_dataset_schema_rejects_invalid_serialized_shape(mutation):
    _, module = exporter()
    value = copy.deepcopy(dataset_payload())
    if mutation == "schema":
        value["schema"] = "vkm-dataset-version/1"
    elif mutation == "role":
        value["files"][0]["role"] = "UNKNOWN"
    elif mutation == "sha":
        value["files"][0]["sha256"] = "not-a-sha"
    elif mutation == "size":
        value["files"][0]["size_bytes"] = True
    elif mutation == "extra":
        value["unrecognized"] = "value"
    elif mutation == "missing":
        del value["available_from"]
    elif mutation == "policy":
        value["policy"]["experimental_role"] = "FACT"
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.Draft202012Validator(module.dataset_version_schema()).validate(value)


def test_version_links_and_catalogue_preserve_source_identity_and_nonadmission():
    from vkm_datasets.contracts import DatasetCatalogSnapshot, SourceVersionLink

    _, module = exporter()
    schemas = module.schemas()
    links = [SourceVersionLink(source_id="VKM-SRC-001", source_sha256="a" * 64,
                               relation="DESCRIBED_BY").model_dump(mode="json")]
    jsonschema.Draft202012Validator(json.loads(schemas["source_version_links.schema.json"])).validate(links)
    snapshot = DatasetCatalogSnapshot(policy_sha256="a" * 64, producer_identity_sha256="b" * 64, entries=())
    value = snapshot.model_dump(mode="json")
    assert value["scientific_admission"] == "NOT_ESTABLISHED"
    validator = jsonschema.Draft202012Validator(json.loads(schemas["dataset_catalogue.schema.json"]))
    validator.validate(value)
    value["scientific_admission"] = "PASS"
    with pytest.raises(jsonschema.ValidationError):
        validator.validate(value)
