"""Explicit scientific context and historical reads never weaken current policy."""
from datetime import datetime, timezone
import json

import pytest
from fastapi.testclient import TestClient

from vkm_corpus.api.app import ApiConfig, create_app
from vkm_corpus.api.fixtures import synthetic_service
from vkm_corpus.contracts.access import AccessContext
from vkm_evidence.contracts import (Entity, EvidenceBatch, EvidenceTransfer, ReviewDecision,
    ScientificUseAdmission, ScientificUseContext, ScientificTransferBinding, Mention, ObservationSet,
    canonical_bytes, record_hash)
from vkm_evidence.journal import EvidenceJournal
from vkm_evidence.objects import ObjectCatalogue, OriginalObject
from vkm_evidence.query import EvidenceReader
from vkm_evidence.temporal import HistoricalReadContext
from vkm_evidence.validation import admission_state, policy_hash, version_references
from vkm_world.core.provenance import Scope, Scale
from test_scientific_quantity_admission import BASE, CONTEXT, POLICY, REF, TIME, assess, quantity


def scientific_fixture(transfer_status="ENGINEERING_ASSUMPTION"):
    provenance = quantity().provenance.model_dump(mode="json")
    provenance.update(scope="NON_VKM", scale="LAB", transfer={
        "from_scale": "LAB", "to_scale": "MASSIF", "from_scope": "NON_VKM", "to_scope": "SKRU1",
        "method": "synthetic explicit scenario transfer", "rationale": "synthetic reviewed applicability",
        "status": transfer_status})
    _, observation = assess(quantity(name="youngs_modulus", unit="MPa", provenance=provenance))
    transfer = EvidenceTransfer(**BASE, record_id="transfer", target=observation.version_ref,
        transfer=observation.quantity.provenance.transfer, supports=(REF,), time=TIME)
    closure = {r.record_id: r for r in (observation, transfer)}
    reviews = tuple(ReviewDecision(**BASE, record_id="review-" + r.record_id, target=r.version_ref,
        decision="SEMANTIC_REVIEWED", source_verified=True, supports=(REF,), reviewer_authority="reviewer",
        rationale="synthetic original inspection", time=TIME) for r in closure.values())
    binding = ScientificTransferBinding(target=observation.version_ref, transfer_record=transfer.version_ref,
                                        transfer_sha256=record_hash(transfer.transfer))
    use = ScientificUseContext(use="MATERIAL_PARAMETER", site="SKRU1", scale="MASSIF", transfers=(binding,))
    admission = ScientificUseAdmission(**BASE, record_id="admission", purpose="explicit scientific context",
        origin="2026-10-01", targets=(observation.version_ref,), dependency_versions=tuple(r.version_ref for r in closure.values()),
        review_versions=tuple(r.version_ref for r in reviews), policy_sha256=policy_hash(closure), status="READY",
        use_context=use, time=TIME)
    return admission, {**closure, **{r.record_id: r for r in reviews}}


def test_legacy_admission_bytes_parse_without_context_but_do_not_authorize_use():
    admission, records = scientific_fixture()
    archival = admission.model_dump(mode="json")
    archival.pop("use_context")
    parsed = ScientificUseAdmission.model_validate(archival)
    assert record_hash(parsed) == record_hash(archival)
    assert "use_context" not in json.loads(canonical_bytes(parsed))
    state = admission_state(parsed, records, CONTEXT)
    assert state["status"] == "NOT_READY" and "SCIENTIFIC_USE_CONTEXT_MISSING" in state["reasons"]


@pytest.mark.parametrize("field,value", [("site", "UNSTATED"), ("scale", "UNSTATED"), ("use", "free purpose")])
def test_typed_context_never_infers_unknown_axes(field, value):
    context = {"use": "MATERIAL_PARAMETER", "site": "SKRU1", "scale": "MASSIF", field: value}
    with pytest.raises(ValueError):
        ScientificUseContext.model_validate(context)


def test_purpose_alone_cannot_promote_lab_value_to_massif():
    provenance = quantity().provenance.model_dump(mode="json")
    provenance["scale"] = "LAB"
    state, observation = assess(quantity(name="youngs_modulus", unit="MPa", provenance=provenance),
        use_context={"use": "MATERIAL_PARAMETER", "site": "SKRU1", "scale": "MASSIF"})
    assert state["status"] == "NOT_READY" and "TRANSFER_MISSING:observation" in state["reasons"]
    assert observation.quantity.provenance.scale.value == "LAB"


def test_exact_reviewed_transfer_is_ready_without_promoting_source_status(tmp_path):
    admission, records = scientific_fixture()
    state = admission_state(admission, records, CONTEXT)
    assert state["status"] == "READY", state
    assert state["use_context_sha256"] == record_hash(admission.use_context)
    assert state["field_validation"] == "NOT_ESTABLISHED"
    assert records["observation"].quantity.provenance.scope.value == "NON_VKM"
    assert records["transfer"].transfer.status.value == "ENGINEERING_ASSUMPTION"
    assert records["transfer"].version_ref in version_references(admission)
    catalogue = ObjectCatalogue((OriginalObject(REF, POLICY, "synthetic original"),))
    journal = EvidenceJournal(tmp_path / "journal", object_validator=catalogue.validate, reviewers=frozenset({"reviewer"}))
    for record in (*records.values(), admission):
        journal.publish(record.record_id, journal.revision, EvidenceBatch(records=(record,)), CONTEXT)
    assert EvidenceReader(journal).get("admission", CONTEXT)["current_admission"]["status"] == "READY"


def test_transfer_cannot_promote_derived_application_to_fact():
    admission, records = scientific_fixture(transfer_status="FACT")
    state = admission_state(admission, records, CONTEXT)
    assert state["status"] == "NOT_READY" and "TRANSFER_STATUS:observation" in state["reasons"]


def test_transfer_is_not_an_independent_field_validation_observation():
    admission, records = scientific_fixture()
    admission = admission.model_copy(update={"use_context": admission.use_context.model_copy(update={"use": "VALIDATION_OBSERVATION"})})
    state = admission_state(admission, records, CONTEXT)
    assert state["status"] == "NOT_READY"
    assert "SCIENTIFIC_VALIDATION_OBSERVATION_NOT_ESTABLISHED:observation" in state["reasons"]


def admit_targets(targets, closure, use):
    reviews = tuple(ReviewDecision(**BASE, record_id="review-" + r.record_id, target=r.version_ref,
        decision="SEMANTIC_REVIEWED", source_verified=True, supports=r.supports, reviewer_authority="reviewer",
        rationale="synthetic original inspection", time=TIME) for r in closure.values())
    admission = ScientificUseAdmission(**BASE, record_id="admission", purpose="text cannot authorize numeric use",
        origin="2026-10-01", targets=tuple(r.version_ref for r in targets),
        dependency_versions=tuple(r.version_ref for r in closure.values()),
        review_versions=tuple(r.version_ref for r in reviews), policy_sha256=policy_hash(closure), status="READY",
        use_context={"use": use, "site": "SKRU1", "scale": "FIELD"})
    return admission_state(admission, {**closure, **{r.record_id: r for r in reviews}}, CONTEXT)


@pytest.mark.parametrize("use", ["IDENTITY", "GEOMETRY_INPUT", "MATERIAL_PARAMETER", "CALIBRATION_INPUT",
                                "PREDICTION_INPUT", "BENCHMARK_INPUT", "SCENARIO_INPUT"])
def test_reviewed_text_mention_cannot_become_interpreted_scientific_input(use):
    mention = Mention(**BASE, record_id="mention", literal="synthetic numeric-looking 123 m",
                      supports=(REF,), time=TIME)
    state = admit_targets((mention,), {mention.record_id: mention}, use)
    assert state["status"] == "NOT_READY", state
    assert any(reason.startswith("SCIENTIFIC_TARGET_") for reason in state["reasons"])


def test_reviewed_mention_can_only_be_admitted_as_source_interpretation():
    mention = Mention(**BASE, record_id="mention", literal="synthetic original", supports=(REF,), time=TIME)
    assert admit_targets((mention,), {mention.record_id: mention}, "SOURCE_INTERPRETATION")["status"] == "READY"


@pytest.mark.parametrize("fault", [None, "unverified_lineage", "empty", "text_child", "unpinned"])
def test_numeric_observation_set_requires_exact_qualified_children(fault):
    _, observation = assess(quantity())
    if fault == "text_child":
        observation = Mention(**BASE, record_id="observation", literal="123", supports=(REF,), time=TIME)
    refs = () if fault == "unpinned" else (observation.version_ref,)
    group = ObservationSet(**BASE, record_id="set", supports=(REF,), time=TIME, references=refs,
        observation_ids=() if fault == "empty" else (observation.record_id,),
        origins=({"origin_id": "synthetic-primary", "verified": True, "independence_basis": "source inspected"},),
        lineage_state="UNKNOWN" if fault == "unverified_lineage" else "VERIFIED")
    state = admit_targets((group,), {group.record_id: group, observation.record_id: observation}, "CALIBRATION_INPUT")
    assert state["status"] == ("READY" if fault is None else "NOT_READY"), state


@pytest.mark.parametrize("fault", ["hash", "record_version", "target", "closure", "review", "site", "scale"])
def test_transfer_hash_target_review_and_context_are_all_bound(fault):
    admission, records = scientific_fixture()
    use = admission.use_context
    binding = use.transfers[0]
    if fault == "hash":
        binding = binding.model_copy(update={"transfer_sha256": "0" * 64})
    elif fault == "record_version":
        binding = binding.model_copy(update={"transfer_record": binding.transfer_record.model_copy(update={"record_sha256": "0" * 64})})
    elif fault == "target":
        binding = binding.model_copy(update={"target": records["transfer"].version_ref})
    elif fault == "closure":
        admission = admission.model_copy(update={"dependency_versions": (records["observation"].version_ref,)})
    elif fault == "review":
        admission = admission.model_copy(update={"review_versions": (records["review-observation"].version_ref,)})
    elif fault == "site":
        use = use.model_copy(update={"site": Scope.SKRU2})
    elif fault == "scale":
        use = use.model_copy(update={"scale": Scale.FIELD})
    admission = ScientificUseAdmission.model_validate({**admission.model_dump(mode="json"),
        "use_context": {**use.model_dump(mode="json"), "transfers": [binding.model_dump(mode="json")]}})
    assert admission_state(admission, records, CONTEXT)["status"] == "NOT_READY"


def history(tmp_path):
    writer = AccessContext(principal="reviewer", execution="LOCAL", granted_classes={"PUBLIC", "PRIVATE_LOCAL_ONLY"})
    catalogue = ObjectCatalogue((OriginalObject(REF, POLICY, "synthetic original"),))
    journal = EvidenceJournal(tmp_path / "journal", object_validator=catalogue.validate)
    def entity(rid, year=2020, **extra):
        return Entity(record_id=rid, recorded_at=datetime(year, 1, 1, tzinfo=timezone.utc), actor="reviewer",
            policy=POLICY, supports=(REF,), entity_type="MINE", label="synthetic " + str(year),
            site_scope="SKRU1", time={"available_from": f"{year}-01-01", "precision": "day"}, **extra)
    old = entity("entity")
    other = entity("other")
    journal.publish("original", journal.revision, EvidenceBatch(records=(old, other)), writer)
    original_revision = journal.revision
    correction = entity("entity", 2022, revision=2, supersedes=old.version_ref)
    journal.publish("correction", journal.revision, EvidenceBatch(records=(correction,)), writer)
    source_policy = [POLICY]
    reader = EvidenceReader(journal, source_policy=lambda _: source_policy[0])
    return journal, reader, writer, old, correction, original_revision, source_policy


def historical(reader, revision, *, year=2025, recorded="2025-01-01T00:00:00Z"):
    return reader.at_revision(journal_revision=revision, as_of=f"{year}-01-01", recorded_at=recorded)


@pytest.mark.parametrize("selector", ["revision", "recorded_at", "available_at"])
def test_historical_reads_bind_both_times_and_exact_retained_revision(tmp_path, selector):
    journal, reader, writer, old, correction, original, _ = history(tmp_path)
    if selector == "revision":
        selected = historical(reader, original)
    elif selector == "recorded_at":
        selected = historical(reader, journal.revision, recorded="2021-01-01T00:00:00Z")
    else:
        selected = historical(reader, journal.revision, year=2021)
    answer = selected.get("entity", writer)
    assert answer["record_sha256"] == record_hash(old)
    assert answer["view_context_sha256"] == record_hash(answer["view_context"])
    assert reader.get("entity", writer)["record_sha256"] == record_hash(correction)


def test_historical_revision_must_be_current_ancestor_and_clock_must_be_aware(tmp_path):
    journal, reader, writer, *_ = history(tmp_path)
    with pytest.raises(ValueError, match="retained"):
        historical(reader, "0" * 64).page(writer)
    with pytest.raises(ValueError, match="timezone-aware"):
        HistoricalReadContext(journal_revision=journal.revision, as_of="2020-01-01", recorded_at="2020-01-01T00:00:00")


@pytest.mark.parametrize("revocation", ["source", "record"])
def test_current_policy_revocation_cannot_be_bypassed_by_old_revision(tmp_path, revocation):
    journal, reader, writer, old, correction, original, policies = history(tmp_path)
    public = AccessContext(principal="reader", execution="CLOUD", granted_classes={"PUBLIC"})
    selected = historical(reader, original)
    page = selected.page(public, limit=1)
    assert page["has_more"] and selected.get("entity", public)["record_sha256"] == record_hash(old)
    private = type(POLICY).model_validate({**POLICY.model_dump(mode="json"),
        "access_class": "PRIVATE_LOCAL_ONLY", "policy_version": "revoked"})
    if revocation == "source":
        policies[0] = private
    else:
        revoked = correction.model_copy(update={"revision": 3, "supersedes": correction.version_ref, "policy": private})
        journal.publish("restrict", journal.revision, EvidenceBatch(records=(revoked,)), writer)
    with pytest.raises(KeyError):
        selected.get("entity", public)
    with pytest.raises(ValueError, match="stale"):
        selected.page(public, cursor=page["next_cursor"])


def test_historical_cursor_cannot_cross_record_time_or_origin(tmp_path):
    journal, reader, writer, _, _, original, _ = history(tmp_path)
    selected = historical(reader, original)
    cursor = selected.page(writer, limit=1)["next_cursor"]
    for other in (historical(reader, original, year=2024),
                  historical(reader, original, recorded="2024-01-01T00:00:00Z"), historical(reader, journal.revision)):
        with pytest.raises(ValueError, match="stale"):
            other.page(writer, cursor=cursor)


def test_api_historical_context_is_explicit_and_missing_selectors_do_not_default(tmp_path):
    journal, reader, writer, old, correction, original, policies = history(tmp_path)
    service, *_ = synthetic_service(tmp_path / "canonical")
    service.deps.evidence = reader
    client = TestClient(create_app(service, ApiConfig(read_tokens={"read": "reader"}, access_contexts={"reader": writer})))
    headers = {"Authorization": "Bearer read"}
    params = {"journal_revision": original, "as_of": "2025-01-01", "recorded_at": "2025-01-01T00:00:00Z"}
    response = client.get("/v1/evidence/records/entity", params=params, headers=headers)
    assert response.status_code == 200
    assert response.json()["item"]["record"]["record_sha256"] == record_hash(old)
    for removed in params:
        partial = {key: value for key, value in params.items() if key != removed}
        assert client.get("/v1/evidence/records/entity", params=partial, headers=headers).status_code == 400


def test_historical_dependency_closure_uses_exact_selected_versions_and_availability(tmp_path):
    journal, reader, writer, old, correction, original, policies = history(tmp_path)
    dependent = Entity(**BASE, record_id="dependent", entity_type="MINE", label="synthetic dependent",
        site_scope="SKRU1", supports=(REF,), references=(correction.version_ref,),
        time={"available_from": "2020-01-01", "precision": "day"})
    journal.publish("dependent", journal.revision, EvidenceBatch(records=(dependent,)), writer)
    # It cannot see an older value through a reference pinned to a newer value,
    # even when the dependent's own availability assertion is earlier.
    selected = historical(reader, journal.revision, year=2021, recorded="2027-01-01T00:00:00Z")
    assert selected.get("entity", writer)["record_sha256"] == record_hash(old)
    with pytest.raises(KeyError):
        selected.get("dependent", writer)
    ids = {r["record_id"] for r in selected.page(writer)["items"]}
    assert ids == {"entity", "other"}


def test_current_reader_never_interprets_latest_interpretation_as_historical_data(tmp_path):
    journal, reader, writer, old, correction, original, _ = history(tmp_path)
    page = reader.page(writer, as_of=datetime(2021, 1, 1).date())
    assert "entity" not in {r["record_id"] for r in page["items"]}
    assert historical(reader, journal.revision, year=2021).get("entity", writer)["record_sha256"] == record_hash(old)


def test_real_mcp_preserves_exact_historical_selector_contract(tmp_path):
    import asyncio
    import httpx
    from mcp import Client
    from vkm_corpus.mcp.api_client import ApiClient
    from vkm_corpus.mcp.servers import build_read_server
    from vkm_corpus.api.production import read_tool_names

    journal, reader, writer, old, correction, original, policies = history(tmp_path)
    service, *_ = synthetic_service(tmp_path / "canon")
    service.deps.evidence = reader
    api = ApiClient("http://synthetic.invalid", "read", transport=httpx.ASGITransport(app=create_app(service,
        ApiConfig(read_tokens={"read": "reader"}, access_contexts={"reader": writer}))))
    async def run():
        try:
            async with Client(build_read_server(api)) as mcp:
                tools = {t.name: t for t in (await mcp.list_tools()).tools}
                assert set(tools) == read_tool_names()
                for name in ("list_evidence", "get_evidence_record", "get_evidence_dependencies", "get_evidence_review_packet"):
                    assert {"journal_revision", "as_of", "recorded_at"} <= set(tools[name].input_schema["properties"])
                response = await mcp.call_tool("get_evidence_record", {"record_id": "entity",
                    "journal_revision": original, "as_of": "2025-01-01", "recorded_at": "2025-01-01T00:00:00Z"})
                value = response.structured_content
                assert value["ok"] is True
                assert value["item"]["record"]["record_sha256"] == record_hash(old)
                assert value["item"]["record"]["view_context"]["historical"]["journal_revision"] == original
        finally:
            await api.aclose()
    asyncio.run(run())
