from __future__ import annotations

import hashlib
from copy import deepcopy
from dataclasses import replace
from datetime import datetime, timezone

import pytest

from vkm_corpus.api.canon import CanonStore, SnapshotInfo, kind_of
from vkm_corpus.contracts.access import AccessContext, ResourcePolicy
from vkm_corpus.contracts.access_vocab import ExperimentalRole
from vkm_evidence.contracts import Entity, FormulaInterpretation, ObjectRef, record_hash
from vkm_evidence.objects import canonical_resolver
from vkm_evidence.query import EvidenceReader
from vkm_evidence.review_packet import ReviewPacketLimit, build_review_packet


SHA = "a" * 64
OTHER = "b" * 64
SID = "VKM-SRC-001"
PAGE = SID + ":p0001"
OID = PAGE + ":b123456abcdef"
POLICY = ResourcePolicy(access_class="PUBLIC", experimental_role="INPUT", policy_version="1", authority="owner")
CONTEXT = AccessContext(principal="reviewer", execution="CLOUD")
REF = ObjectRef(source_id=SID, source_sha256=SHA, snapshot_id="snap-1", object_id=OID, object_version=SHA,
                content_sha256=OTHER, locator="block:1", extraction_generation="1")
TEXT = "Соль: σ = 12 МПа; значение из синтетического источника."


class Journal:
    revision = "c" * 64

    def __init__(self, records):
        self.data = {r.record_id: r for r in records}

    def records(self, revision):
        assert revision == self.revision
        return self.data


class Canon:
    def __init__(self, rows):
        self.rows = {r["object_id"]: r for r in rows}
        self.identity = SnapshotInfo("snap-1", "d" * 64, "2026-10-01", "synthetic")
        self.calls = []
        self.after_lookup = None

    def snapshot(self):
        return self.identity

    def snapshot_id(self):
        return self.snapshot().snapshot_id

    def row(self, kind, oid):
        self.calls.append((kind, oid))
        if self.after_lookup:
            self.after_lookup()
        return deepcopy(self.rows.get(oid))


def row(ref=REF, **changes):
    return {"object_id": ref.object_id, "object_kind": kind_of(ref.object_id), "source_id": ref.source_id,
        "source_sha256": ref.source_sha256, "content_sha256": ref.content_sha256,
        "extraction_generation": 1, "extraction_signature": ref.object_version, "raw_locator": ref.locator,
        "page_id": PAGE, "docx_paragraph_path": None, "text": TEXT, "region_origin": "NATIVE_TEXT",
        "origin": "NATIVE", "bbox_space": "NONE", **changes}


def entity(rid="e1", supports=(REF,), **kw):
    return Entity(record_id=rid, actor="extractor", recorded_at=datetime(2026, 10, 1, tzinfo=timezone.utc),
                  policy=POLICY, entity_type="MINE", label="Синтетический объект", supports=supports, **kw)


def reader(*records, policy=lambda _: POLICY):
    return EvidenceReader(Journal(records or (entity(),)), source_policy=policy)


def test_packet_is_exact_deterministic_and_does_not_review():
    evidence = reader()
    canon = Canon([row()])
    first = build_review_packet(evidence, canon, "e1", CONTEXT)
    assert first == build_review_packet(evidence, canon, "e1", CONTEXT)
    claimed_hash = first.pop("packet_sha256")
    assert claimed_hash == record_hash(first)
    assert first["record_sha256"] == record_hash(entity())
    assert first["record"] == entity().model_dump(mode="json")
    assert first["review_state"] == "NOT_REVIEWED" and first["scientific_admission"] == "NOT_RUN"
    assert first["decision_binding"]["target"] == entity().version_ref.model_dump(mode="json")
    assert first["original_supports"][0]["excerpt"]["text"] == TEXT
    first["record"]["label"] = "changed by consumer"
    assert evidence.get("e1", CONTEXT)["record"]["label"] != "changed by consumer"


def test_unicode_fragment_is_checked_against_actual_block_text():
    ref = REF.model_copy(update={"char_start": 6, "char_end": 12,
        "fragment_sha256": hashlib.sha256(TEXT[6:12].encode()).hexdigest()})
    packet = build_review_packet(reader(entity(supports=(ref,))), Canon([row()]), "e1", CONTEXT)
    original = packet["original_supports"][0]
    assert original["fragment_check"]["status"] == "VERIFIED"
    assert original["fragment_check"]["representation"] == "text"
    assert original["excerpt"]["text"] == TEXT[6:12]


@pytest.mark.parametrize("change,error", [
    ({"locator": "fake"}, "locator mismatch"),
    ({"source_sha256": OTHER}, "identity mismatch"),
    ({"char_start": 0, "char_end": 4, "fragment_sha256": SHA}, "fragment hash mismatch"),
    ({"char_start": 0, "char_end": 999, "fragment_sha256": SHA}, "out of bounds"),
    ({"object_version": OTHER}, "identity mismatch"),
])
def test_forged_original_support_is_rejected(change, error):
    ref = REF.model_copy(update=change)
    with pytest.raises(ValueError, match=error):
        build_review_packet(reader(entity(supports=(ref,))), Canon([row()]), "e1", CONTEXT)


def test_missing_original_and_old_snapshot_cannot_be_retargeted():
    with pytest.raises(ValueError, match="unavailable"):
        build_review_packet(reader(), Canon([]), "e1", CONTEXT)
    canon = Canon([row()])
    canon.identity = replace(canon.identity, snapshot_id="snap-2")
    with pytest.raises(ValueError, match="no current-snapshot retargeting"):
        build_review_packet(reader(), canon, "e1", CONTEXT)
    assert canon.calls == []


def test_source_policy_is_required_and_checked_before_original_lookup():
    canon = Canon([row()])
    with pytest.raises(ValueError, match="authoritative source policy"):
        build_review_packet(reader(policy=None), canon, "e1", CONTEXT)
    denied = POLICY.model_copy(update={"experimental_role": "TEST_SEALED"})
    with pytest.raises(KeyError, match="record not found"):
        build_review_packet(reader(policy=lambda _: denied), canon, "e1", CONTEXT)
    assert canon.calls == []


def test_fine_grained_denied_parent_hides_target_and_counts_before_lookup():
    parent = entity("sealed-parent").model_copy(update={"policy": POLICY.model_copy(update={"experimental_role": ExperimentalRole.TARGET})})
    child = entity(references=(parent.version_ref,))
    canon = Canon([row()])
    with pytest.raises(KeyError) as error:
        build_review_packet(reader(child, parent), canon, "e1", CONTEXT)
    assert str(error.value) == "'record not found'" and canon.calls == []


def test_dependency_and_symbol_supports_are_complete():
    second = REF.model_copy(update={"object_id": PAGE + ":b123456abcdee", "locator": "block:2"})
    parent = entity("parent", supports=(second,))
    formula = FormulaInterpretation(record_id="formula", actor="extractor", recorded_at=entity().recorded_at,
        policy=POLICY, supports=(REF,), original_form="σ = p", representation="TEXT", references=(parent.version_ref,),
        symbols=({"symbol": "p", "definition": "pressure", "scope": "formula", "unit": "MPa", "supports": (second,)},))
    packet = build_review_packet(reader(formula, parent), Canon([row(), row(second)]), "formula", CONTEXT)
    assert len(packet["dependency_references"]) == 1 and packet["references_complete"]
    assert len(packet["original_supports"]) == 2 and packet["supports_complete"]
    assert packet["units"]["symbols"][0]["unit"] == "MPa"
    assert {u["path"] for o in packet["original_supports"] for u in o["used_by"]} == {"supports/0", "symbols/0/supports/0"}


def test_stale_dependency_fails_instead_of_resolving_new_version():
    old = entity("parent")
    new = old.model_copy(update={"label": "new interpretation"})
    child = entity(references=(old.version_ref,))
    canon = Canon([row()])
    # The historical pin is absent from this fixture. Visibility now closes
    # before lookup, since its original policy cannot be established.
    with pytest.raises(KeyError, match="record not found"):
        build_review_packet(reader(child, new), canon, "e1", CONTEXT)
    assert canon.calls == []


def test_forged_source_prefix_is_rejected_before_private_lookup():
    ref = REF.model_copy(update={"object_id": "VKM-SRC-002:p0001:b123456abcdef"})
    canon = Canon([row(ref, source_id="VKM-SRC-002")])
    with pytest.raises(ValueError, match="identity mismatch"):
        build_review_packet(reader(entity(supports=(ref,))), canon, "e1", CONTEXT)
    assert canon.calls == []


def test_native_xml_location_never_gets_a_fake_page_or_box():
    ref = REF.model_copy(update={"object_id": SID + ":doc:b123456abcdef", "locator": "body/p[1]"})
    native = row(ref, raw_locator=None, page_id=None, docx_paragraph_path="body/p[1]", region_origin="DOCX_ELEMENT")
    packet = build_review_packet(reader(entity(supports=(ref,))), Canon([native]), "e1", CONTEXT)
    obj = packet["original_supports"][0]
    assert obj["locator"] == {"kind": "NATIVE_XML", "value": "body/p[1]", "page_id": None, "docx_paragraph_path": "body/p[1]"}
    assert obj["geometry"]["bbox"] is None and obj["geometry"]["unit"] == "UNKNOWN"


def test_table_preview_has_honest_limits_and_complete_cursor_route():
    ref = REF.model_copy(update={"object_id": PAGE + ":t123456abcdef", "locator": "table:1"})
    cells = [{"row": 0, "col": col, "text": str(col), "row_span": 1, "col_span": 1} for col in range(257)]
    table = row(ref, raw_output="<native-table/>", cells=cells, n_rows=1, n_cols=257)
    packet = build_review_packet(reader(entity(supports=(ref,))), Canon([table]), "e1", CONTEXT, max_table_cells=2)
    obj = packet["original_supports"][0]
    assert obj["table"]["preview_truncated"] and obj["table"]["canonical_cells_total"] == 257
    assert len(obj["table"]["cells_preview"]) == 2
    assert obj["table"]["native_table_completeness"] == "NOT_ESTABLISHED"
    route = obj["table"]["complete_grid_navigation"]
    assert route["start_url"].startswith("/v1/nav/table/")
    assert route["cursor_parameter"] == "cursor" and route["follow_until_has_more_false"]
    assert route["availability"] == "NOT_CHECKED"


@pytest.mark.parametrize("kind,code,payload", [
    ("FORMULA", "m", {"raw_output": "<m:oMath/>", "normalized_latex": "x=2"}),
    ("FIGURE", "f", {"caption": "Рисунок 1"}),
    ("PAGE", None, {"normalized_text": "Нормализованная страница"}),
])
def test_original_kinds_preserve_their_actual_text_and_image_identity(kind, code, payload):
    oid = PAGE if code is None else PAGE + ":" + code + "123456abcdef"
    ref = REF.model_copy(update={"object_id": oid, "locator": PAGE})
    artifact = "sha256:" + SHA
    original = row(ref, raw_locator=None, image_artifact_id=artifact, **payload)
    packet = build_review_packet(reader(entity(supports=(ref,))), Canon([original]), "e1", CONTEXT)
    obj = packet["original_supports"][0]
    assert obj["excerpt"]["text"] == next(iter(payload.values()))
    assert obj["images"][0]["sha256"] == SHA and not obj["images"][0]["bytes_verified"]
    assert "/v1/artifact/" in obj["images"][0]["content_url"]
    assert obj["links"]["response_snapshot_check_required"]


@pytest.mark.parametrize("limits,error", [({"max_text_chars": 0}, "limits out of range"),
                                         ({"max_packet_bytes": 100}, "byte limit")])
def test_bounds_do_not_silently_drop_evidence(limits, error):
    with pytest.raises(ValueError, match=error):
        build_review_packet(reader(), Canon([row()]), "e1", CONTEXT, **limits)


def test_support_caps_fail_but_preview_truncation_is_explicit():
    other = REF.model_copy(update={"object_id": PAGE + ":b123456abcdee", "locator": "block:2"})
    e = entity(supports=(REF, other))
    with pytest.raises(ReviewPacketLimit, match="original supports exceed"):
        build_review_packet(reader(e), Canon([row(), row(other)]), "e1", CONTEXT, max_supports=1)
    packet = build_review_packet(reader(), Canon([row()]), "e1", CONTEXT, max_text_chars=4)
    excerpt = packet["original_supports"][0]["excerpt"]
    assert excerpt["truncated"] and excerpt["chars_total"] == len(TEXT) and len(excerpt["text"]) == 4


def test_snapshot_policy_and_evidence_races_abort_packets():
    canon, evidence = Canon([row()]), reader()
    canon.after_lookup = lambda: setattr(canon, "identity", replace(canon.identity, manifest_sha256=OTHER))
    with pytest.raises(ValueError, match="snapshot changed"):
        build_review_packet(evidence, canon, "e1", CONTEXT)
    canon = Canon([row()])
    canon.after_lookup = lambda: setattr(evidence.journal, "revision", OTHER)
    with pytest.raises(ValueError, match="generation changed"):
        build_review_packet(evidence, canon, "e1", CONTEXT)
    current = [POLICY]
    evidence, canon = reader(policy=lambda _: current[0]), Canon([row()])
    canon.after_lookup = lambda: current.__setitem__(0, POLICY.model_copy(update={"policy_version": "2"}))
    with pytest.raises(PermissionError, match="policy changed"):
        build_review_packet(evidence, canon, "e1", CONTEXT)


@pytest.mark.parametrize("native", [False, True])
def test_resolver_real_canonstore_schema_and_unicode_spans(native):
    duckdb = pytest.importorskip("duckdb")
    con = duckdb.connect(":memory:")
    try:
        con.execute("CREATE SCHEMA meta")
        con.execute("CREATE TABLE meta.snapshot AS SELECT 'snap-1' snapshot_id, ? manifest_sha256, "
                    "TIMESTAMPTZ '2026-10-01 00:00:00+00' built_at, 'synthetic' duckdb_version", [SHA])
        con.execute("CREATE TABLE meta.commits(commit_key VARCHAR, commit_id VARCHAR)")
        ref = REF.model_copy(update={"char_start": 0, "char_end": 4,
            "fragment_sha256": hashlib.sha256(TEXT[:4].encode()).hexdigest()})
        if native:
            ref = ref.model_copy(update={"object_id": SID + ":doc:b123456abcdef", "locator": "body/p[1]"})
        con.execute("CREATE TABLE blocks AS SELECT ? object_id, ? source_id, ? source_sha256, ? content_sha256, "
            '1 extraction_generation, ? extraction_signature, ? raw_locator, ? docx_paragraph_path, ? page_id, ? AS "text"',
            [ref.object_id, SID, SHA, OTHER, SHA, None if native else ref.locator,
             ref.locator if native else None, None if native else PAGE, TEXT])
        assert canonical_resolver(CanonStore(connection=con), lambda _: POLICY)(ref) == POLICY
    finally:
        con.close()


def _legacy_canon(signature, commit="CMT-legacy0001", content=OTHER):
    """A real CanonStore over one block; ``signature=None`` models the historical unsigned snapshot."""
    duckdb = pytest.importorskip("duckdb")
    con = duckdb.connect(":memory:")
    con.execute("CREATE SCHEMA meta")
    con.execute("CREATE TABLE meta.snapshot AS SELECT 'snap-1' snapshot_id, ? manifest_sha256, "
                "TIMESTAMPTZ '2026-10-01 00:00:00+00' built_at, 'synthetic' duckdb_version", [SHA])
    con.execute("CREATE TABLE meta.commits(commit_key VARCHAR, commit_id VARCHAR)")
    if commit is not None:
        con.execute("INSERT INTO meta.commits VALUES (?, ?)", [SID, commit])
    con.execute("CREATE TABLE blocks AS SELECT ? object_id, ? source_id, ? source_sha256, ? content_sha256, "
                '1 extraction_generation, ? extraction_signature, ? raw_locator, NULL docx_paragraph_path, ? page_id, '
                '? AS "text"', [OID, SID, SHA, content, signature, REF.locator, PAGE, TEXT])
    return con


def test_unsigned_legacy_object_binds_only_by_the_served_content_at_commit_version():
    """Owner decision 05.10.2026: historical objects carry no extraction_signature; they are cited by the
    version the API serves (<content_sha256>@<commit>), the signature stays UNKNOWN."""
    con = _legacy_canon(signature=None)
    try:
        store = CanonStore(connection=con)
        resolve = canonical_resolver(store, lambda _: POLICY)
        served = OTHER + "@CMT-legacy0001"
        assert resolve(REF.model_copy(update={"object_version": served})) == POLICY
        for wrong in (OTHER + "@CMT-other00001", SHA + "@CMT-legacy0001", OTHER, SHA, "UNKNOWN"):
            with pytest.raises(ValueError):
                resolve(REF.model_copy(update={"object_version": wrong}))
    finally:
        con.close()


def test_unsigned_object_without_a_commit_never_binds():
    con = _legacy_canon(signature=None, commit=None)
    try:
        resolve = canonical_resolver(CanonStore(connection=con), lambda _: POLICY)
        for version in (OTHER + "@None", OTHER + "@", OTHER):
            with pytest.raises(ValueError):
                resolve(REF.model_copy(update={"object_version": version}))
    finally:
        con.close()


def test_signed_object_still_requires_its_signature_not_the_content_at_commit_form():
    con = _legacy_canon(signature=SHA)
    try:
        resolve = canonical_resolver(CanonStore(connection=con), lambda _: POLICY)
        assert resolve(REF.model_copy(update={"object_version": SHA})) == POLICY
        with pytest.raises(ValueError):
            resolve(REF.model_copy(update={"object_version": OTHER + "@CMT-legacy0001"}))
    finally:
        con.close()


class LegacyCanon(Canon):
    """A canon that also knows the commit of the pinned snapshot (as CanonStore does)."""

    def commit_of(self, key):
        return "CMT-legacy0001" if key == SID else None


def test_review_packet_shows_an_unsigned_legacy_support():
    served = OTHER + "@CMT-legacy0001"
    ref = REF.model_copy(update={"object_version": served})
    packet = build_review_packet(reader(entity(supports=(ref,))), LegacyCanon([row(ref, extraction_signature=None)]),
                                 "e1", CONTEXT)
    assert packet["original_supports"][0]["excerpt"]["text"] == TEXT
    assert packet["review_state"] == "NOT_REVIEWED"


def test_empty_signature_or_missing_commit_lookup_fails_closed():
    served = OTHER + "@CMT-legacy0001"
    ref = REF.model_copy(update={"object_version": served})
    with pytest.raises(ValueError):           # "" is a corrupt signature, never the unsigned form
        build_review_packet(reader(entity(supports=(ref,))), LegacyCanon([row(ref, extraction_signature="")]),
                            "e1", CONTEXT)
    with pytest.raises(ValueError):           # a canon without commit identity cannot resolve unsigned objects
        build_review_packet(reader(entity(supports=(ref,))), Canon([row(ref, extraction_signature=None)]),
                            "e1", CONTEXT)
