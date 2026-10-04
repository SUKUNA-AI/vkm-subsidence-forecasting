"""Native Word grid fidelity through the real assembler/mapper, using synthetic packages only.

Pagination metadata below is a synthetic boundary fixture, not renderer/runtime qualification.
"""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
import hashlib
from types import SimpleNamespace
import zipfile

import pytest

from vkm_corpus.extract.docx import DOCX_GRID_RULE, NS, W, native_table_grid, read_docx

etree = pytest.importorskip("lxml.etree")
RUN = "RUN-20261004T100000Z-01020304"


def cell(text="", *, properties=""):
    return f'<w:tc><w:tcPr>{properties}</w:tcPr><w:p><w:r><w:t>{text}</w:t></w:r></w:p></w:tc>'


def row(cells, *, properties=""):
    return f'<w:tr><w:trPr>{properties}</w:trPr>{cells}</w:tr>'


def table(rows, *, cols=None):
    grid = "" if cols is None else "<w:tblGrid>" + "<w:gridCol/>" * cols + "</w:tblGrid>"
    return f'<w:tbl xmlns:w="{NS["w"]}">{grid}{rows}</w:tbl>'


def gap_table(gap):
    return table(row(cell("top", properties='<w:vMerge w:val="restart"/>')) + gap
                 + row(cell("later", properties="<w:vMerge/>")))


@pytest.mark.parametrize("gap", [
    row(cell("other"), properties='<w:gridBefore w:val="1"/>'),
    row("", properties='<w:gridAfter w:val="1"/>'),
    row(""),
    row(cell("replacement")),
])
def test_vertical_merge_never_crosses_a_missing_or_replaced_row(gap):
    # Previously the gridBefore/empty-row cases appended row2 to row0 and
    # reported row_span=2 (covering row1), with no independent row2 cell.
    cells, rows, _, audit = native_table_grid(etree.fromstring(gap_table(gap)))
    assert rows == 3
    assert cells[0]["row_span"] == 1 and cells[0]["text"] == "top"
    later = next(c for c in cells if c["row"] == 2)
    assert later["text"] == "later" and later["row_span"] == 1
    assert later["structural_diagnostics"] == ["ORPHAN_VERTICAL_MERGE"]
    assert audit["status"] == "NEEDS_REVIEW"
    assert audit["physical_cell_count"] == len(audit["physical_cells"])


def test_adjacent_merge_keeps_all_fragment_locators_and_physical_header_rows():
    xml = table(row(cell("Высота, м", properties='<w:gridSpan w:val="2"/><w:vMerge w:val="restart"/>'),
                    properties="<w:tblHeader/>")
                + row(cell("continued", properties='<w:gridSpan w:val="2"/><w:vMerge/>'),
                      properties="<w:tblHeader/>")
                + row(cell("last", properties='<w:gridSpan w:val="2"/><w:vMerge/>')), cols=2)
    cells, rows, cols, audit = native_table_grid(etree.fromstring(xml))
    assert (rows, cols, len(cells)) == (3, 2, 1)
    assert cells[0]["row_span"] == 3 and cells[0]["text"] == "Высота, м\ncontinued\nlast"
    assert [f["row"] for f in cells[0]["merge_fragments"]] == [1, 2]
    assert audit["physical_cell_count"] == 3
    assert len({c["raw_locator"] for c in audit["physical_cells"]}) == 3
    assert [r["is_header"] for r in audit["rows"]] == [True, True, False]
    assert audit["status"] == "PARSED_UNREVIEWED"
    assert audit["scientific_admission"] == "NOT_ESTABLISHED"


def test_mismatched_vertical_span_does_not_join_and_remains_review_debt():
    xml = table(row(cell("top", properties='<w:gridSpan w:val="2"/><w:vMerge w:val="restart"/>'))
                + row(cell("next", properties="<w:vMerge/>")), cols=2)
    cells, _, _, audit = native_table_grid(etree.fromstring(xml))
    assert [c["text"] for c in cells] == ["top", "next"]
    assert [c["row_span"] for c in cells] == [1, 1]
    assert {d["code"] for d in audit["diagnostics"]} == {
        "VERTICAL_MERGE_SPAN_MISMATCH", "ROW_GRID_WIDTH_MISMATCH"}


def test_unknown_header_boolean_is_not_silently_promoted_to_header():
    xml = table(row(cell("unknown"), properties='<w:tblHeader w:val="unknown"/>'), cols=1)
    cells, _, _, audit = native_table_grid(etree.fromstring(xml))
    assert cells[0]["is_header"] is False
    assert audit["status"] == "NEEDS_REVIEW"
    assert audit["diagnostics"][0]["code"] == "INVALID_TABLE_HEADER"
    assert audit["rows"][0]["header_declaration"]["raw_value"] == "unknown"


@pytest.mark.parametrize("properties,row_properties,code", [
    ('<w:gridSpan w:val="0"/>', "", "INVALID_GRID_SPAN"),
    ('<w:gridSpan w:val="-1"/>', "", "INVALID_GRID_SPAN"),
    ('<w:gridSpan w:val="bad"/>', "", "INVALID_GRID_SPAN"),
    ('<w:gridSpan/>', "", "INVALID_GRID_SPAN"),
    ('<w:gridSpan w:val="2147483648"/>', "", "INVALID_GRID_SPAN"),
    ("", '<w:gridBefore w:val="-1"/>', "INVALID_GRID_BEFORE"),
    ("", '<w:gridAfter w:val="bad"/>', "INVALID_GRID_AFTER"),
    ("", '<w:gridBefore w:val="2147483647"/>', "GRID_BEFORE_LIMIT"),
    ("", '<w:gridAfter w:val="2147483647"/>', "GRID_AFTER_LIMIT"),
    ("<w:hMerge/>", "", "UNSUPPORTED_HORIZONTAL_MERGE"),
])
def test_invalid_geometry_is_raw_accounted_without_guessed_spans(properties, row_properties, code):
    xml = table(row(cell("1,25", properties=properties) + cell(""), properties=row_properties))
    cells, rows, cols, audit = native_table_grid(etree.fromstring(xml))
    assert cells == [] and rows == 1 and cols is None
    assert audit["status"] == "STRUCTURE_UNRESOLVED"
    assert code in {d["code"] for d in audit["diagnostics"]}
    assert [c["text"] for c in audit["physical_cells"]] == ["1,25", ""]
    assert all(c["raw_locator"] for c in audit["physical_cells"])


def test_small_xml_huge_gridspan_and_vmerge_without_tblgrid_never_reaches_geometry_consumers():
    from vkm_corpus.extract.docx import MAX_DOCX_GRID_COLUMNS
    xml = table(row(cell("top", properties=f'<w:gridSpan w:val="{MAX_DOCX_GRID_COLUMNS + 1}"/><w:vMerge w:val="restart"/>'))
                + row(cell("next", properties=f'<w:gridSpan w:val="{MAX_DOCX_GRID_COLUMNS + 1}"/><w:vMerge/>')))
    assert len(xml) < 600
    cells, rows, cols, audit = native_table_grid(etree.fromstring(xml))
    assert cells == [] and rows == 2 and cols is None
    assert audit["status"] == "STRUCTURE_UNRESOLVED"
    assert audit["physical_cell_count"] == 2 and [c["text"] for c in audit["physical_cells"]] == ["top", "next"]
    assert {d["code"] for d in audit["diagnostics"]} == {"GRID_COLUMN_LIMIT"}
    assert all(c["declared_grid_span"] == str(MAX_DOCX_GRID_COLUMNS + 1) for c in audit["physical_cells"])


def test_small_sparse_xml_cannot_expand_into_unbounded_grid_area():
    from vkm_corpus.extract.docx import MAX_DOCX_GRID_POSITIONS
    width = 1024
    height = MAX_DOCX_GRID_POSITIONS // width + 1
    xml = table(row(cell("wide", properties=f'<w:gridSpan w:val="{width}"/>')) + row("") * (height - 1))
    assert len(xml) < 100_000
    cells, rows, cols, audit = native_table_grid(etree.fromstring(xml))
    assert cells == [] and rows == height and cols is None
    assert audit["physical_cell_count"] == 1 and audit["physical_cells"][0]["text"] == "wide"
    assert audit["diagnostics"][-1]["code"] == "GRID_POSITION_LIMIT"


def test_declared_grid_and_257_columns_are_not_truncated_or_autofilled():
    wide = table(row("".join(cell("" if i == 100 else f"Колонка{i}") for i in range(257)),
                     properties="<w:tblHeader/>") + row("".join(cell("1,25") for _ in range(257))), cols=257)
    cells, rows, cols, audit = native_table_grid(etree.fromstring(wide))
    assert (len(cells), rows, cols) == (514, 2, 257)
    assert cells[100]["text"] == "" and cells[-1]["col"] == 256
    assert audit["status"] == "PARSED_UNREVIEWED" and audit["physical_cell_count"] == 514
    short = table(row(cell("left")), cols=3)
    cells, _, cols, audit = native_table_grid(etree.fromstring(short))
    assert cols == 3 and len(cells) == 1  # declared empty columns are not invented cells
    assert audit["status"] == "NEEDS_REVIEW"
    assert audit["diagnostics"][0]["code"] == "ROW_GRID_WIDTH_MISMATCH"


def assemble_package(tmp_path, markup, *, part="word/document.xml", mutation=None):
    from vkm_corpus.artifacts.store import ArtifactStore
    from vkm_corpus.extract.model import SourceInput
    from vkm_corpus.extract.to_canon import CanonMapper
    from vkm_corpus.pipeline.assemble import Assembler
    from vkm_corpus.pipeline.config import PipelineConfig
    from vkm_corpus.pipeline import prepare

    path = tmp_path / "synthetic.docx"
    ns = " ".join(f'xmlns:{key}="{value}"' for key, value in NS.items())
    main = f'<w:document {ns}><w:body>{markup if part == "word/document.xml" else ""}</w:body></w:document>'
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("[Content_Types].xml", '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"/>')
        z.writestr("word/document.xml", main)
        if part != "word/document.xml":
            z.writestr(part, f'<w:footnotes {ns}><w:footnote w:id="1">{markup}</w:footnote></w:footnotes>')
    original = path.read_bytes()
    source = SourceInput("VKM-SRC-901", path.name, hashlib.sha256(original).hexdigest(), len(original),
                         evidence_scope="GENERAL_METHOD")
    doc = read_docx(path)
    cfg = PipelineConfig(tmp_path / "data", tmp_path)
    store = ArtifactStore(cfg.data_root / "artifacts")
    cache = SimpleNamespace(run_id=RUN, get_stage=lambda _: None)
    native = {"source_id": source.source_id, "source_sha256": source.sha256, "pages": [], "errors": []}
    prepare._prepare_docx(cfg, store, cache, source, path, native)
    assert native["pagination"] is None and native["errors"][0]["code"] == "RENDER_FAILED"
    raw = store.read_json(native["document_raw_artifact_id"])
    assert prepare.docx_grid_current(raw, source)
    if mutation:
        mutation(raw)
    artifact = store.put_json(raw, "NATIVE_RAW", compress=True, source_id=source.source_id,
                              producer_signature=native["docx_grid_signature"])
    prep = {"source_id": source.source_id, "source_sha256": source.sha256, "inspect": {"file_format": "DOCX"},
            "status": "PREPARED", "run_id": RUN, "prepare_signature": "b" * 64,
            **prepare.docx_grid_identity(source),
            "document_raw_artifact_id": artifact.artifact_id,
            # No rendering is run; the single page is only synthetic metadata for this consumer fixture.
            "pagination": {"unit": "r", "page_kind": "DOCX_RENDERED_PAGE", "basis": "DOCX_PINNED_RENDER",
                           "count": 1, "method": "synthetic-test-boundary"},
            "pages": [{"page_index": 1, "page_kind": "DOCX_RENDERED_PAGE", "route": "NATIVE",
                       "page_class": "RENDERED_FROM_SOURCE"}]}
    assembler = Assembler(cfg, store, cache, source, prep, None)
    result = assembler.build()
    mapper = CanonMapper(result, run_id=RUN, config_hashes={"objects": "c" * 64},
                         created_at=datetime(2026, 10, 4, 10, tzinfo=timezone.utc))
    mapped = mapper.build()
    report = assembler.accounting.finish(result, mapped, cache, code_revision="synthetic-native-grid-test")
    assert path.read_bytes() == original  # no original or old extraction overwrite
    return doc, result, mapped, report, store


@pytest.mark.parametrize("part", ["word/document.xml", "word/footnotes.xml"])
def test_native_adapter_to_canonical_and_coverage_preserves_exact_physical_provenance(tmp_path, part):
    from vkm_corpus.contracts import arrow as ca
    from vkm_corpus.duckdb.build import attach_manifest
    from vkm_corpus.parquet.layout import init_root

    duckdb = pytest.importorskip("duckdb")
    markup = gap_table(row(cell("other"), properties='<w:gridBefore w:val="1"/>'))
    doc, result, mapped, report, store = assemble_package(tmp_path, markup, part=part)
    canon = mapped.tables["tables"][0]
    assert canon.n_rows == 3 and len(canon.cells) == 3
    assert [(c.row, c.text) for c in canon.cells] == [(0, "top"), (1, "other"), (2, "later")]
    assert "TABLE_STRUCTURE_UNCERTAIN" in canon.quality_flags
    assert canon.raw_output == doc.tables[0].xml
    assert canon.raw_content_sha256 == hashlib.sha256(doc.tables[0].xml.encode("utf-8")).hexdigest()
    assert canon.raw_locator == canon.docx_paragraph_path == doc.tables[0].path
    assert canon.review_status == "AUTO_EXTRACTED_UNREVIEWED" and canon.extraction_signature is None
    assert canon.continues_object_id is None and canon.continuation_provenance is None
    refs = [r for r in canon.raw_artifacts if r.role == "STRUCTURAL_DIAGNOSTICS"]
    assert len(refs) == 1 and refs[0].artifact_id in mapped.artifact_ids
    audit = store.read_json(refs[0].artifact_id)
    assert audit["source_sha256"] == canon.source_sha256
    assert audit["native_raw_artifact_id"] == canon.raw_artifact_id
    assert audit["table_raw_content_sha256"] == canon.raw_content_sha256
    assert audit["grid"]["rule"] == DOCX_GRID_RULE
    assert audit["grid"]["physical_cell_count"] == 3
    root = etree.fromstring(canon.raw_output.encode())
    for physical in audit["grid"]["physical_cells"]:
        prefix = canon.raw_locator
        assert physical["raw_locator"].startswith(prefix + "/")
        detached = root.getroottree().getpath(root) + physical["raw_locator"][len(prefix):]
        assert len(root.getroottree().xpath(detached, namespaces=NS)) == 1
    if part != "word/document.xml":
        assert all(c["raw_locator"].startswith(part + "#") for c in audit["grid"]["physical_cells"])
    disposition = next(c for c in report["ledger"]["candidates"] if c["kind"] == "TABLE")
    assert disposition["state"] == "NEEDS_REVIEW" and disposition["reason"] == "NATIVE_TABLE_STRUCTURE_UNCERTAIN"
    assert disposition["output_objects"] == [canon.object_id]
    assert report["report"]["candidate_review_debt"] == 1
    assert report["scientific_admission"] == "NOT_ESTABLISHED"
    assert report["report"]["extraction_completeness"] == "NOT_ESTABLISHED"
    # Actual canonical schema/DuckDB consumers retain every projected row and the audit reference.
    con = duckdb.connect()
    attach_manifest(con, init_root(tmp_path / "canonical", "CANONICAL"), {})
    con.register("native_tables", ca.rows_to_table("tables", [canon]))
    con.execute("INSERT INTO canonical.tables BY NAME SELECT * FROM native_tables")
    assert con.execute("SELECT array_length(cells) FROM canonical.tables").fetchone()[0] == 3
    con.close()
    # Adding the diagnostic artifact/flags does not change the existing physical ID or raw XML hash.
    previous = deepcopy(result)
    previous.tables[0].raw_artifacts = [r for r in previous.tables[0].raw_artifacts if r[0] != "STRUCTURAL_DIAGNOSTICS"]
    previous.tables[0].quality_flags = []
    from vkm_corpus.extract.to_canon import CanonMapper
    old = CanonMapper(previous, run_id=RUN, config_hashes={"objects": "c" * 64}).tables()[0]
    assert old.object_id == canon.object_id and old.raw_content_sha256 == canon.raw_content_sha256


@pytest.mark.parametrize("mutation", ["missing", "rule", "status", "locator", "projected-cell", "physical-cell", "raw-xml",
                                     "native-signature", "native-rule", "native-libraries", "native-source"])
def test_legacy_or_forged_grid_audit_cannot_be_treated_as_current_parsed_grid(tmp_path, mutation):
    def mutate(raw):
        t = raw["tables"][0]
        if mutation == "missing":
            t.pop("grid_audit")
        elif mutation == "rule":
            t["grid_audit"]["rule"] = "legacy/1"
        elif mutation == "status":
            t["grid_audit"]["status"] = "SCIENTIFICALLY_ACCEPTED"
        elif mutation == "locator":
            t["grid_audit"]["table_locator"] = "/another-table"
        elif mutation == "projected-cell":
            t["grid_audit"]["projected_cells"][0]["text"] = "changed"
        elif mutation == "physical-cell":
            t["grid_audit"]["physical_cells"][0]["text"] = "changed"
        elif mutation == "native-signature":
            raw["docx_grid_signature"] = "0" * 64
        elif mutation == "native-rule":
            raw["docx_grid_rule"] = "legacy/1"
        elif mutation == "native-libraries":
            raw["docx_grid_libraries"] = {"lxml": "unknown", "libxml2": "unknown"}
        elif mutation == "native-source":
            raw["source_sha256"] = "0" * 64
        else:
            t["xml"] = t["xml"].replace("original", "changed")
    _, _, mapped, report, store = assemble_package(tmp_path, table(row(cell("original")), cols=1), mutation=mutate)
    canon = mapped.tables["tables"][0]
    assert "TABLE_STRUCTURE_UNCERTAIN" in canon.quality_flags
    audit_id = next(r.artifact_id for r in canon.raw_artifacts if r.role == "STRUCTURAL_DIAGNOSTICS")
    audit = store.read_json(audit_id)
    assert audit["grid"]["status"] == "NOT_AVAILABLE"
    assert audit["grid"]["diagnostics"][0]["code"] == "NATIVE_GRID_AUDIT_MISSING_OR_INCONSISTENT"
    assert report["ledger"]["candidates"][0]["state"] == "NEEDS_REVIEW"


def test_unresolved_native_grid_still_publishes_raw_form_and_explicit_debt(tmp_path):
    doc, _, mapped, report, store = assemble_package(tmp_path,
        table(row(cell("retained", properties='<w:gridSpan w:val="0"/>'))))
    canon = mapped.tables["tables"][0]
    assert canon.cells == [] and canon.n_cols is None and canon.n_rows == 1
    assert canon.normalized_text == "retained" and canon.raw_output == doc.tables[0].xml
    assert report["report"]["candidate_review_debt"] == 1
    audit_id = next(r.artifact_id for r in canon.raw_artifacts if r.role == "STRUCTURAL_DIAGNOSTICS")
    assert store.read_json(audit_id)["grid"]["physical_cells"][0]["text"] == "retained"


def test_similar_tables_and_changed_header_units_never_invent_continuation(tmp_path):
    markup = table(row(cell("Высота, м"), properties="<w:tblHeader/>") + row(cell("1,25")), cols=1)
    markup += table(row(cell("Высота, см"), properties="<w:tblHeader/>") + row(cell("125")), cols=1)
    _, _, mapped, report, _ = assemble_package(tmp_path, markup)
    assert len(mapped.tables["tables"]) == 2
    assert [r.cells[0].text for r in mapped.tables["tables"]] == ["Высота, м", "Высота, см"]
    assert all(r.continues_object_id is None for r in mapped.tables["tables"])
    assert all(r.review_status == "AUTO_EXTRACTED_UNREVIEWED" for r in mapped.tables["tables"])
    assert report["report"]["candidate_review_debt"] == 0
    assert report["scientific_admission"] == "NOT_ESTABLISHED"


def test_docx_child_rule_changes_without_invalidating_prepare_or_ocr_call_keys(tmp_path, monkeypatch):
    from vkm_corpus.extract import docx
    from vkm_corpus.extract.model import SourceInput
    from vkm_corpus.pipeline import prepare
    from vkm_corpus.pipeline.config import PipelineConfig

    monkeypatch.setattr(prepare, "library_versions", lambda: {})
    cfg = PipelineConfig(tmp_path / "data", tmp_path)
    docx_source = SourceInput("VKM-SRC-901", "synthetic.DOCX", "a" * 64, 1)
    pdf_source = SourceInput("VKM-SRC-902", "synthetic.pdf", "b" * 64, 1)
    docx_sig = prepare.prepare_signature(cfg, docx_source)
    pdf_sig = prepare.prepare_signature(cfg, pdf_source)
    child = prepare.docx_grid_identity(docx_source)
    from vkm_corpus.pipeline.ocr_stage import call_signature_for
    ocr_sig = call_signature_for(cfg, "table", "c" * 64)
    monkeypatch.setattr(docx, "DOCX_GRID_RULE", "docx-native-grid/1")
    assert prepare.docx_grid_identity(docx_source) != child
    assert prepare.prepare_signature(cfg, docx_source) == docx_sig
    assert prepare.prepare_signature(cfg, pdf_source) == pdf_sig
    assert call_signature_for(cfg, "table", "c" * 64) == ocr_sig


@pytest.mark.parametrize("extension", ["docx", "bin"])
@pytest.mark.parametrize("failure", ["missing", "signature", "libraries", "raw-signature"])
def test_actual_prepare_cache_and_load_state_reject_old_or_forged_docx_even_if_renamed(
        tmp_path, monkeypatch, extension, failure):
    import json
    from vkm_corpus.artifacts.store import ArtifactStore
    from vkm_corpus.extract.model import SourceInput
    from vkm_corpus.pipeline import commit, prepare
    from vkm_corpus.pipeline.config import PipelineConfig

    monkeypatch.setattr(prepare, "library_versions", lambda: {})
    path = tmp_path / ("source." + extension)
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("[Content_Types].xml", "<Types/>")
        z.writestr("word/document.xml", f'<w:document xmlns:w="{NS["w"]}"><w:body/></w:document>')
    source = SourceInput("VKM-SRC-901", path.name, hashlib.sha256(path.read_bytes()).hexdigest(), path.stat().st_size)
    cfg = PipelineConfig(tmp_path / "data", tmp_path)
    store = ArtifactStore(cfg.data_root / "artifacts")
    identity = prepare.docx_grid_identity(source)
    raw = {"schema": "vkm.native_raw.docx_document/1", "source_id": source.source_id,
           "source_sha256": source.sha256, **identity}
    if failure == "raw-signature":
        raw["docx_grid_signature"] = "0" * 64
    aid = store.put_json(raw, "NATIVE_RAW", source_id=source.source_id).artifact_id
    sig = commit.prep_signature_for(cfg, source)
    cached = {"schema": prepare.PREP_SCHEMA, "source_id": source.source_id, "source_sha256": source.sha256,
              "prepare_signature": sig, "status": "PREPARED", "transient_errors": False,
              "inspect": {"file_format": "DOCX"}, "document_raw_artifact_id": aid, "sentinel": "old-cache", **identity}
    if failure == "missing":
        cached.pop("docx_grid_signature")
    elif failure == "signature":
        cached["docx_grid_signature"] = "0" * 64
    elif failure == "libraries":
        cached["docx_grid_libraries"] = {"lxml": "old", "libxml2": "old"}
    cache_file = prepare.prep_path(cfg.data_root, source.source_id, sig)
    cache_file.parent.mkdir(parents=True)
    cache_file.write_text(json.dumps(cached), encoding="utf-8")
    assert commit.load_state(cfg, source) == (None, None)
    calls = []
    original_native = prepare._prepare_docx
    def actual_native(*args):
        calls.append(True)
        return original_native(*args)
    monkeypatch.setattr(prepare, "_prepare_docx", actual_native)
    cache = SimpleNamespace(run_id=RUN, get_stage=lambda _: None, add_stage=lambda **_: None)
    fresh = prepare.prepare_source(cfg, store, cache, source)
    assert calls == [True] and "sentinel" not in fresh
    assert prepare.docx_grid_current(fresh, source)
    assert prepare.docx_grid_current(store.read_json(fresh["document_raw_artifact_id"]), source)
    # There is deliberately no renderer: native freshness cannot become runtime/scientific readiness.
    assert fresh["errors"][0]["code"] == "RENDER_FAILED" and fresh["pagination"] is None


@pytest.mark.parametrize("extension", ["docx", "bin"])
@pytest.mark.parametrize("failure", ["summary-signature", "raw-signature", "artifact-bytes", "missing-artifact", "rule"])
def test_actual_planner_cannot_keep_docx_up_to_date_after_child_or_native_artifact_change(
        tmp_path, monkeypatch, extension, failure):
    import json
    from vkm_corpus.pipeline import commit, prepare
    from vkm_corpus.pipeline.config import PipelineConfig
    from vkm_corpus.pipeline.plan import build_plan

    _, result, _, _, store = assemble_package(tmp_path, table(row(cell("native")), cols=1))
    source = result.source
    if extension != "docx":
        (tmp_path / source.canonical_path).rename(tmp_path / ("synthetic." + extension))
        source.canonical_path = "synthetic." + extension
    cfg = PipelineConfig(tmp_path / "data", tmp_path)
    raw_record = next(r for r in store.records if r.artifact_kind == "NATIVE_RAW"
                      and prepare.docx_grid_current(store.read_json(r.artifact_id), source))
    prep = {"schema": prepare.PREP_SCHEMA, "source_id": source.source_id, "source_sha256": source.sha256,
            "prepare_signature": commit.prep_signature_for(cfg, source), "status": "PREPARED",
            "inspect": {"file_format": "DOCX"}, "document_raw_artifact_id": raw_record.artifact_id,
            "pages": [], "transient_errors": False, **prepare.docx_grid_identity(source)}
    cache_file = prepare.prep_path(cfg.data_root, source.source_id, prep["prepare_signature"])
    cache_file.parent.mkdir(parents=True, exist_ok=True)
    cache_file.write_text(json.dumps(prep), encoding="utf-8")
    cache = SimpleNamespace(calls={}, get_stage=lambda _: None)
    baseline_signature = commit.commit_signature(cfg, cache, source, prep, None, accounting_events=[])
    assert baseline_signature
    # This fixture represents only a staging ledger signature, not qualified production/scientific admission.
    monkeypatch.setattr(commit, "load_ledger", lambda _: {
        source.source_id: {"commit_signature": baseline_signature, "document_processing_status": "COMPLETE"}})
    baseline = build_plan(cfg, [source], cache, store)
    assert baseline["sources"][0]["action"] == "UP_TO_DATE"
    if failure == "summary-signature":
        prep["docx_grid_signature"] = "0" * 64
        cache_file.write_text(json.dumps(prep), encoding="utf-8")
    elif failure == "rule":
        from vkm_corpus.extract import docx
        monkeypatch.setattr(docx, "DOCX_GRID_RULE", "future-child-rule")
    elif failure == "missing-artifact":
        store.path_for(raw_record).unlink()
    elif failure == "artifact-bytes":
        store.path_for(raw_record).write_bytes(b"synthetic artifact corruption")
    else:
        raw = store.read_json(raw_record.artifact_id)
        raw["docx_grid_signature"] = "0" * 64
        prep["document_raw_artifact_id"] = store.put_json(raw, "NATIVE_RAW", compress=True,
            source_id=source.source_id).artifact_id
        cache_file.write_text(json.dumps(prep), encoding="utf-8")
    assert commit.load_state(cfg, source) == (None, None)
    assert commit.commit_signature(cfg, cache, source, prep, None, accounting_events=[]) is None
    after = build_plan(cfg, [source], cache, store)
    assert after["sources"][0]["action"] == "PROCESS"
    assert after["sources"][0]["commit"] == "SIGNATURE_CHANGED"
    assert after["sources"][0]["prepare"] == "SIGNATURE_CHANGED"
    assert "DOCX_NATIVE_GRID_STALE" in after["sources"][0]["reasons"]
    assert after["totals"]["up_to_date"] == 0


def test_non_docx_commit_signature_matches_exact_previous_payload_and_reuses_ocr(tmp_path, monkeypatch):
    from vkm_corpus.extract.bibliography import CONFIG_HASH as BIBLIOGRAPHY_RULES
    from vkm_corpus.extract.model import SourceInput
    from vkm_corpus.pipeline import commit
    from vkm_corpus.pipeline.config import PipelineConfig
    from vkm_corpus.versions import PIPELINE_VERSION

    cfg = PipelineConfig(tmp_path / "data", tmp_path)
    cache = SimpleNamespace(calls={"synthetic": [{"source_id": "VKM-SRC-902", "status": "OK", "kind": "OCR",
                                                "raw_artifact_id": "sha256:" + "c" * 64}]})
    source = SourceInput("VKM-SRC-902", "source.pdf", "b" * 64, 123)
    prep = {"source_id": source.source_id, "source_sha256": source.sha256, "prepare_signature": "a" * 64,
            "inspect": {"file_format": "PDF"}}
    monkeypatch.setattr(commit, "load_decision", lambda *_: None)
    expected = commit._cfg_hash({"pipeline": PIPELINE_VERSION, "prep": prep["prepare_signature"],
        "visual": None, "visual_complete": None,
        "configs": {k: cfg.stage_config(k) for k in ("REGIONS", "OCR", "NORMALIZE", "SCENARIO_B")},
        "ocr_results": commit._cfg_hash({"ids": ["sha256:" + "c" * 64]}), "decision": None,
        "to_canon": "to_canon_v3", "bibliography": BIBLIOGRAPHY_RULES,
        "accounting": "pipeline-object-accounting/1", "accounting_events": []})
    assert commit.commit_signature(cfg, cache, source, prep, None, accounting_events=[]) == expected
    assert cache.calls["synthetic"][0]["raw_artifact_id"] == "sha256:" + "c" * 64


def _production_disguised_docx_cache(tmp_path, monkeypatch, cached_format):
    """Synthetic source and legacy no-child ledger; real source SHA admission remains enabled."""
    import json
    from dataclasses import replace
    from vkm_corpus.extract.model import SourceInput
    from vkm_corpus.pipeline import commit, plan, prepare
    from vkm_corpus.pipeline.config import PipelineConfig

    path = tmp_path / "source.bin"
    with zipfile.ZipFile(path, "w") as package:
        package.writestr("[Content_Types].xml", "<Types/>")
        package.writestr("word/document.xml", f'<w:document xmlns:w="{NS["w"]}"><w:body/></w:document>')
    source = SourceInput("VKM-SRC-901", path.name, hashlib.sha256(path.read_bytes()).hexdigest(), path.stat().st_size)
    cfg = PipelineConfig(tmp_path / "data", tmp_path, profile="production")
    monkeypatch.setattr(prepare, "library_versions", lambda: {})
    monkeypatch.setattr(commit, "load_decision", lambda *_: None)
    # Only host/code qualification is replaced; this is never a production receipt.
    monkeypatch.setattr(plan, "producer_identity", lambda _: {"scope": "SYNTHETIC_ONLY", "verified": True})
    prep = {"schema": prepare.PREP_SCHEMA, "source_id": source.source_id, "source_sha256": source.sha256,
            "prepare_signature": commit.prep_signature_for(cfg, source), "status": "PREPARED",
            "pages": [], "transient_errors": False}
    if cached_format is not None:
        prep["inspect"] = {"file_format": cached_format}
    target = prepare.prep_path(cfg.data_root, source.source_id, prep["prepare_signature"])
    target.parent.mkdir(parents=True)
    target.write_text(json.dumps(prep), encoding="utf-8")
    cache = SimpleNamespace(calls={}, get_stage=lambda _: None)
    legacy_signature = commit.commit_signature(replace(cfg, profile="exploratory"), cache, source, prep, None,
                                               accounting_events=[])
    monkeypatch.setattr(commit, "load_ledger", lambda _: {source.source_id: {
        "commit_signature": legacy_signature, "document_processing_status": "COMPLETE"}})
    return cfg, source, prep, cache, path


@pytest.mark.parametrize("cached_format", ["PDF", "EPUB", "UNKNOWN", None])
def test_production_load_state_detects_actual_docx_despite_forged_cached_format(tmp_path, monkeypatch, cached_format):
    from vkm_corpus.pipeline import commit
    cfg, source, _, _, _ = _production_disguised_docx_cache(tmp_path, monkeypatch, cached_format)
    assert commit.load_state(cfg, source) == (None, None)


@pytest.mark.parametrize("cached_format", ["PDF", "EPUB", "UNKNOWN", None])
def test_production_commit_signature_detects_actual_docx_independently(tmp_path, monkeypatch, cached_format):
    from vkm_corpus.pipeline import commit
    cfg, source, prep, cache, _ = _production_disguised_docx_cache(tmp_path, monkeypatch, cached_format)
    assert commit.commit_signature(cfg, cache, source, prep, None, accounting_events=[]) is None


@pytest.mark.parametrize("cached_format", ["PDF", "EPUB", "UNKNOWN", None])
def test_production_plan_cannot_skip_disguised_docx_child_admission(tmp_path, monkeypatch, cached_format):
    from vkm_corpus.pipeline.plan import build_plan
    cfg, source, _, cache, _ = _production_disguised_docx_cache(tmp_path, monkeypatch, cached_format)
    result = build_plan(cfg, [source], cache, SimpleNamespace())
    assert result["sources"][0]["source_identity"]["verification"] == "FRESH_SHA256"
    assert result["sources"][0]["action"] == "PROCESS"
    assert result["sources"][0]["prepare"] == "SIGNATURE_CHANGED"
    assert "SOURCE_FORMAT_CACHE_MISMATCH" in result["sources"][0]["reasons"]
    assert result["totals"]["up_to_date"] == 0


@pytest.mark.parametrize("consumer", ["load_state", "commit_signature", "plan"])
@pytest.mark.parametrize("failure", ["missing", "same_size_same_mtime_drift"])
def test_production_cached_format_gate_requires_current_original_bytes(tmp_path, monkeypatch, consumer, failure):
    import os
    from vkm_corpus.pipeline import commit, plan
    from vkm_corpus.registry.sources import RegisterError

    cfg, source, prep, cache, path = _production_disguised_docx_cache(tmp_path, monkeypatch, "PDF")
    if failure == "missing":
        path.unlink()
    else:
        before = path.stat()
        raw = bytearray(path.read_bytes())
        raw[0] ^= 1
        path.write_bytes(raw)
        os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns))
    with pytest.raises((OSError, RegisterError)):
        if consumer == "load_state":
            commit.load_state(cfg, source)
        elif consumer == "commit_signature":
            commit.commit_signature(cfg, cache, source, prep, None, accounting_events=[])
        else:
            plan.build_plan(cfg, [source], cache, SimpleNamespace())


@pytest.mark.parametrize("fmt", ["PDF", "DJVU", "EPUB"])
def test_production_non_docx_admission_preserves_signatures_and_plan_hashes_source_once(tmp_path, monkeypatch, fmt):
    import json
    from dataclasses import replace
    from vkm_corpus.extract.model import SourceInput
    from vkm_corpus.pipeline import commit, plan, prepare
    from vkm_corpus.pipeline.config import PipelineConfig
    from vkm_corpus.pipeline.ocr_stage import call_signature_for
    from vkm_corpus.registry import sources

    path = tmp_path / "source.bin"
    if fmt == "EPUB":
        with zipfile.ZipFile(path, "w") as package:
            package.writestr("mimetype", "application/epub+zip")
    else:
        path.write_bytes(b"%PDF-1.4\n%%EOF\n" if fmt == "PDF" else b"AT&TFORM\0\0\0\0DJVU")
    source = SourceInput("VKM-SRC-902", path.name, hashlib.sha256(path.read_bytes()).hexdigest(), path.stat().st_size)
    cfg = PipelineConfig(tmp_path / "data", tmp_path, profile="production")
    diagnostic = replace(cfg, profile="exploratory")
    monkeypatch.setattr(prepare, "library_versions", lambda: {})
    monkeypatch.setattr(commit, "load_decision", lambda *_: None)
    monkeypatch.setattr(plan, "producer_identity", lambda _: {"scope": "SYNTHETIC_ONLY", "verified": True})
    cache = SimpleNamespace(calls={"cached": [{"source_id": source.source_id, "status": "OK", "kind": "OCR",
                                              "raw_artifact_id": "sha256:" + "c" * 64}]}, get_stage=lambda _: None)
    prep = {"schema": prepare.PREP_SCHEMA, "source_id": source.source_id, "source_sha256": source.sha256,
            "prepare_signature": commit.prep_signature_for(cfg, source), "status": "PREPARED",
            "inspect": {"file_format": fmt}, "pages": []}
    before = commit.commit_signature(diagnostic, cache, source, prep, None, accounting_events=[])
    assert before == commit.commit_signature(cfg, cache, source, prep, None, accounting_events=[])
    assert commit.prep_signature_for(cfg, source) == commit.prep_signature_for(diagnostic, source)
    assert call_signature_for(cfg, "table", "d" * 64) == call_signature_for(diagnostic, "table", "d" * 64)
    target = prepare.prep_path(cfg.data_root, source.source_id, prep["prepare_signature"])
    target.parent.mkdir(parents=True)
    target.write_text(json.dumps(prep), encoding="utf-8")
    monkeypatch.setattr(commit, "load_ledger", lambda _: {source.source_id: {
        "commit_signature": before, "document_processing_status": "COMPLETE"}})
    hashes = []
    original_hash = sources.sha256_of

    def count_hash(p):
        hashes.append(p)
        return original_hash(p)

    monkeypatch.setattr(sources, "sha256_of", count_hash)
    result = plan.build_plan(cfg, [source], cache, SimpleNamespace())
    assert result["sources"][0]["source_identity"]["file_format"] == fmt
    assert result["sources"][0]["action"] == "UP_TO_DATE"
    assert hashes == [path]  # load_state/commit_signature reuse only the planner's fresh admission result.
    assert cache.calls["cached"][0]["raw_artifact_id"] == "sha256:" + "c" * 64


def test_production_current_docx_native_proof_still_allows_cached_reuse(tmp_path, monkeypatch):
    import json
    from vkm_corpus.pipeline import commit, plan, prepare
    from vkm_corpus.pipeline.config import PipelineConfig

    _, result, _, _, store = assemble_package(tmp_path, table(row(cell("synthetic")), cols=1))
    source = result.source
    cfg = PipelineConfig(tmp_path / "data", tmp_path, profile="production")
    monkeypatch.setattr(prepare, "library_versions", lambda: {})
    monkeypatch.setattr(commit, "load_decision", lambda *_: None)
    monkeypatch.setattr(plan, "producer_identity", lambda _: {"scope": "SYNTHETIC_ONLY", "verified": True})
    native = next(r for r in store.records if r.artifact_kind == "NATIVE_RAW"
                  and prepare.docx_grid_current(store.read_json(r.artifact_id), source))
    prep = {"schema": prepare.PREP_SCHEMA, "source_id": source.source_id, "source_sha256": source.sha256,
            "prepare_signature": commit.prep_signature_for(cfg, source), "status": "PREPARED",
            "inspect": {"file_format": "DOCX"}, "document_raw_artifact_id": native.artifact_id,
            "pages": [], **prepare.docx_grid_identity(source)}
    target = prepare.prep_path(cfg.data_root, source.source_id, prep["prepare_signature"])
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(prep), encoding="utf-8")
    cache = SimpleNamespace(calls={}, get_stage=lambda _: None)
    signature = commit.commit_signature(cfg, cache, source, prep, None, accounting_events=[])
    assert signature and commit.load_state(cfg, source)[0] is not None
    monkeypatch.setattr(commit, "load_ledger", lambda _: {source.source_id: {
        "commit_signature": signature, "document_processing_status": "COMPLETE"}})
    planned = plan.build_plan(cfg, [source], cache, store)
    assert planned["sources"][0]["action"] == "UP_TO_DATE"
    assert planned["sources"][0]["source_identity"]["file_format"] == "DOCX"


@pytest.mark.parametrize("consumer", ["load_state", "commit_signature", "plan", "prepare"])
@pytest.mark.parametrize("failure", ["malformed_zip", "lfs", "unknown", "plain_zip", "image"])
def test_production_invalid_or_unsupported_original_cannot_reuse_successful_cache(
        tmp_path, monkeypatch, consumer, failure):
    import json
    from dataclasses import replace
    from vkm_corpus.extract.detect import inspect_file
    from vkm_corpus.pipeline import commit, plan, prepare
    from vkm_corpus.registry.sources import RegisterError

    cfg, source, prep, cache, path = _production_disguised_docx_cache(tmp_path, monkeypatch, "UNKNOWN")
    if failure == "plain_zip":
        with zipfile.ZipFile(path, "w") as package:
            package.writestr("synthetic.txt", "not a document package")
    else:
        path.write_bytes({
            "malformed_zip": b"PK\x03\x04truncated synthetic ZIP",
            "lfs": b"version https://git-lfs.github.com/spec/v1\noid sha256:" + b"a" * 64 + b"\nsize 123\n",
            "unknown": b"synthetic unknown file bytes",
            "image": b"\x89PNG\r\n\x1a\nsynthetic image header",
        }[failure])
    source.sha256 = hashlib.sha256(path.read_bytes()).hexdigest()
    source.size_bytes = path.stat().st_size
    prep.update(source_sha256=source.sha256, prepare_signature=commit.prep_signature_for(cfg, source),
                inspect={"file_format": inspect_file(path, hash_file=False).file_format})
    target = prepare.prep_path(cfg.data_root, source.source_id, prep["prepare_signature"])
    target.write_text(json.dumps(prep), encoding="utf-8")
    old_signature = commit.commit_signature(replace(cfg, profile="exploratory"), cache, source, prep, None,
                                            accounting_events=[])
    monkeypatch.setattr(commit, "load_ledger", lambda _: {source.source_id: {
        "commit_signature": old_signature, "document_processing_status": "COMPLETE"}})

    def consume():
        if consumer == "load_state":
            return commit.load_state(cfg, source)
        if consumer == "commit_signature":
            return commit.commit_signature(cfg, cache, source, prep, None, accounting_events=[])
        if consumer == "prepare":
            return prepare.prepare_source(cfg, None, SimpleNamespace(run_id=RUN, add_stage=lambda **_: None), source)
        return plan.build_plan(cfg, [source], cache, SimpleNamespace())

    if consumer == "prepare":
        result = consume()
        assert result["status"] == ("FAILED" if failure in {"malformed_zip", "lfs"} else "UNSUPPORTED")
        assert result["errors"]
        if failure in {"malformed_zip", "lfs"}:
            assert json.loads(target.read_text(encoding="utf-8")) == prep
    elif failure in {"malformed_zip", "lfs"}:
        with pytest.raises(RegisterError):
            consume()
    elif consumer == "load_state":
        assert consume() == (None, None)
    elif consumer == "commit_signature":
        assert consume() is None
    else:
        result = consume()
        assert result["sources"][0]["action"] == "PROCESS"
        assert "SOURCE_FORMAT_CACHE_MISMATCH" in result["sources"][0]["reasons"]
        assert result["totals"]["up_to_date"] == 0


@pytest.mark.parametrize("inspection", ["missing", "lfs", "unreadable"])
def test_fresh_source_identity_rejects_inspection_failure_after_hash(tmp_path, monkeypatch, inspection):
    from vkm_corpus.extract import detect
    from vkm_corpus.registry.sources import RegisterError, fresh_source_identity

    path = tmp_path / "synthetic.bin"
    path.write_bytes(b"%PDF-1.4\n%%EOF\n")
    result = detect.FileInspection(path_exists=inspection != "missing", file_format="PDF",
                                   is_lfs_pointer=inspection == "lfs",
                                   flags=["UNREADABLE"] if inspection == "unreadable" else [])
    monkeypatch.setattr(detect, "inspect_file", lambda *_args, **_kw: result)
    with pytest.raises(RegisterError):
        fresh_source_identity(tmp_path, path.name, hashlib.sha256(path.read_bytes()).hexdigest(), path.stat().st_size)
