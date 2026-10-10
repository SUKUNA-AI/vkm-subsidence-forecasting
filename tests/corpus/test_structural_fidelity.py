"""Synthetic native structures and raw-grid accountability; no private corpus or OCR calls."""
from __future__ import annotations

import base64
import zipfile

import pytest

from vkm_corpus.ocr import normalize as n


def test_nested_environments_check_nesting_not_only_names():
    assert n.latex_structure_ok(r"\begin{array} \begin{matrix} x \end{matrix} \end{array}")
    assert not n.latex_structure_ok(r"\begin{array} \begin{matrix} x \end{array} \end{matrix}")


def test_empty_ocr_rows_kept_in_raw_audit_native_empty_rows_retained():
    raw = "<table><tr><td>x</td><td rowspan='3'></td></tr><tr><td></td></tr><tr></tr></table>"
    ocr = n.normalize_table(raw)
    assert ocr["n_rows"] == 1
    assert ocr["raw_grid"]["n_rows"] == 3
    assert ocr["raw_grid"]["cells"][1]["row_span"] == 3
    assert {d["code"] for d in ocr["dispositions"]} == {
        "SUPPRESSED_TRAILING_EMPTY_ROW", "SUPPRESSED_TRAILING_EMPTY", "CLIPPED_EMPTY_SPAN"}
    native = n.normalize_table(raw, preserve_empty=True)
    assert native["n_rows"] == 3 and not native["dispositions"]
    bands = n.normalize_table_bands([raw, "<table><tr><td>y</td></tr></table>"])
    assert bands["n_rows"] == 2
    assert bands["band_audits"][1]["normalized_row_offset"] == 1
    assert bands["band_audits"][0]["raw_grid"]["n_rows"] == 3


def test_nested_html_table_not_flattened_or_truncated():
    raw = "<table><tr><td>outer<table><tr><td>inner</td></tr></table>end</td><td>b</td></tr>" \
          "<tr><td>second</td><td>c</td></tr></table>"
    outer = n.normalize_table(raw, preserve_empty=True)
    assert (outer["n_rows"], outer["n_cols"]) == (2, 2)
    assert [c["text"] for c in outer["cells"]] == ["outerend", "b", "second", "c"]
    assert outer["dispositions"][0]["code"] == "NESTED_TABLE_SEPARATE"


def test_docx_nested_table_textbox_notes_and_math_all_have_locators(tmp_path):
    pytest.importorskip("lxml")
    from vkm_corpus.extract.docx import NS, read_docx

    ns = ' '.join(f'xmlns:{k}="{v}"' for k, v in NS.items())
    body = f'''<w:document {ns}><w:body>
      <w:p><w:r><w:t>body</w:t></w:r><w:txbxContent><w:p><w:r><w:t>box</w:t></w:r></w:p></w:txbxContent></w:p>
      <w:tbl><w:tr><w:tc><w:p><w:r><w:t>outer</w:t></w:r></w:p>
        <w:tbl><w:tr><w:tc><w:p><w:r><w:t>inner</w:t></w:r>
          <m:oMath><m:r><m:t>x+y</m:t></m:r></m:oMath></w:p></w:tc></w:tr></w:tbl>
      </w:tc></w:tr></w:tbl>
      <w:p><w:del><w:r><w:delText>deleted</w:delText></w:r></w:del><w:fldSimple w:instr="DATE"/></w:p>
    </w:body></w:document>'''
    footnotes = f'''<?xml version="1.0" encoding="UTF-16"?><w:footnotes {ns}><w:footnote w:id="1"><w:p>
      <w:r><w:t>note</w:t></w:r><m:oMath><m:r><m:t>a=b</m:t></m:r></m:oMath>
      </w:p></w:footnote></w:footnotes>'''.encode("utf-16")
    path = tmp_path / "synthetic.docx"
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("word/document.xml", body)
        z.writestr("word/footnotes.xml", footnotes)
    doc = read_docx(path)
    assert [p.text for p in doc.paragraphs] == ["body", "box", "", "note"]
    assert doc.paragraphs[1].container == "TEXTBOX"
    assert doc.paragraphs[-1].block_type == "FOOTNOTE"
    assert doc.paragraphs[-1].path.startswith("word/footnotes.xml#")
    assert [t.cells[0]["text"] for t in doc.tables] == ["outer", "inner"]
    assert len(doc.maths) == 2 and len({m.paragraph_path for m in doc.maths}) == 2
    assert base64.b64decode(doc.raw_parts_base64["word/footnotes.xml"]) == footnotes
    assert {d["code"] for d in doc.diagnostics} >= {"RAW_ONLY_DEL", "RAW_ONLY_FLDSIMPLE"}


def test_epub_native_mathml_and_raw_bytes(tmp_path):
    pytest.importorskip("lxml")
    from vkm_corpus.extract.epub import SpineUnit, parse_unit

    raw = b'<html xmlns="http://www.w3.org/1999/xhtml"><body><p>formula <math xmlns="http://www.w3.org/1998/Math/MathML" display="block"><mi>x</mi><mo>=</mo><mn>2</mn></math></p><object data="embedded.bin"/></body></html>'
    path = tmp_path / "synthetic.epub"
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("unit.xhtml", raw)
    unit = SpineUnit(1, "unit1", "unit.xhtml", True, "application/xhtml+xml")
    with zipfile.ZipFile(path) as z:
        parse_unit(z, unit, {})
    assert base64.b64decode(unit.raw_markup_base64) == raw
    assert len(unit.maths) == 1 and unit.maths[0].linear_text == "x=2"
    assert unit.maths[0].display and unit.maths[0].xpath
    assert "MathML" in unit.maths[0].markup
    assert unit.diagnostics == [{"code": "RAW_ONLY_OBJECT", "xpath": "/*/*/*[2]"}]


def test_docx_grid_omitted_columns_header_and_merge_continuation_preserved():
    etree = pytest.importorskip("lxml.etree")
    from vkm_corpus.extract.docx import W, _grid

    table = etree.fromstring(f'''<w:tbl xmlns:w="{W[1:-1]}">
      <w:tr><w:trPr><w:gridBefore w:val="1"/><w:gridAfter w:val="2"/><w:tblHeader/></w:trPr>
      <w:tc><w:tcPr><w:vMerge w:val="restart"/></w:tcPr><w:p><w:r><w:t>top</w:t></w:r>
        <w:txbxContent><w:p><w:r><w:t>separate box</w:t></w:r></w:p></w:txbxContent></w:p></w:tc></w:tr>
      <w:tr><w:trPr><w:gridBefore w:val="1"/></w:trPr><w:tc><w:tcPr><w:vMerge/></w:tcPr>
        <w:p><w:r><w:t>continued text</w:t></w:r></w:p></w:tc></w:tr></w:tbl>''')
    cells, rows, cols = _grid(table)
    assert (rows, cols) == (2, 4) and len(cells) == 1
    cell = cells[0]
    assert cell["col"] == 1 and cell["row_span"] == 2 and cell["is_header"]
    assert cell["text"] == "top\ncontinued text"
    assert cell["merge_fragments"][0]["text"] == "continued text"


def test_pdf_native_glyphs_include_hidden_layer_and_position():
    pymupdf = pytest.importorskip("pymupdf")
    from vkm_corpus.extract.pdf_native import extract_page

    doc = pymupdf.open()
    page = doc.new_page()
    page.insert_text((30, 50), "Visible")
    page.insert_text((30, 80), "Hidden", render_mode=3)
    native = extract_page(doc, 0)
    traces = native.raw["texttrace"]
    assert any(t["hidden"] for t in traces) and any(not t["hidden"] for t in traces)
    assert all(t["locator"].startswith("page:1/texttrace:") for t in traces)
    assert all(set(c) == {"ordinal", "unicode", "glyph_id", "origin", "bbox"} for t in traces for c in t["chars"])
    assert native.features["chars_invisible"] == len("Hidden")
    doc.close()


def test_mathml_canonical_locator_and_id_validation_do_not_fake_image_ocr(tmp_path):
    from types import SimpleNamespace
    from vkm_corpus.extract.model import DocumentX, SourceInput
    from vkm_corpus.pipeline.config import PipelineConfig
    from vkm_corpus.pipeline.assemble import Assembler
    from vkm_corpus.artifacts.store import ArtifactStore
    from vkm_corpus.extract.to_canon import CanonMapper
    from vkm_corpus.contracts import arrow as ca
    from vkm_corpus.duckdb.build import attach_manifest
    from vkm_corpus.parquet.layout import init_root
    from vkm_corpus.parquet.validator import Validator
    from vkm_corpus.testing import rows as R
    duckdb = pytest.importorskip("duckdb")

    raw = {"schema": "vkm.native_raw.epub_unit/1", "index": 1, "blocks": [], "images": [{"href": "unavailable.gif", "xpath": "/html/body/p/img", "order": 2,
              "is_formula_candidate": True, "inline": False}],
           "maths": [{"xpath": "/html/body/p/math", "order": 1, "display": True, "markup": "<math>x=2</math>",
                      "linear_text": "x=2"}]}
    cfg = PipelineConfig(tmp_path / "data", tmp_path)
    src = SourceInput(R.SID, "synthetic.epub", "a" * 64, 0, evidence_scope="GENERAL_METHOD")
    store = ArtifactStore(tmp_path / "artifacts")
    artifact = store.put_json(raw, "NATIVE_RAW", compress=True, source_id=src.source_id)
    prep = {"source_id": src.source_id, "source_sha256": src.sha256,
            "pagination": {"unit": "s", "count": 1, "basis": "EPUB_SPINE"},
            "inspect": {"file_format": "EPUB"}, "pages": [{"page_index": 1, "route": "NATIVE",
                                                         "native_raw_artifact_id": artifact.artifact_id}]}
    asm = Assembler(cfg, store, SimpleNamespace(run_id=R.RUN_ID), src, prep, None)
    asm._crops = lambda _: []
    asm._epub()
    assert asm.accounting.expected["denominator_state"] == "KNOWN"
    assert len(asm.accounting.candidates) == 2  # native MathML and the still-unread formula image
    assert asm.result.pages[0].ocr_status == "REQUIRED"  # MathML success cannot satisfy an image OCR task
    asm.result.document = DocumentX("EPUB", None, None, None)
    rows = CanonMapper(asm.result, run_id=R.RUN_ID, config_hashes={}).formulas()
    math = rows[0]
    assert math.raw_locator == raw["maths"][0]["xpath"]
    assert math.raw_output == "<math>x=2</math>" and math.normalized_latex is None
    assert math.recognition_method == "NATIVE_MATHML" and math.schema_version == "0.1.2"
    layout = init_root(tmp_path / "canon", "CANONICAL")
    con = duckdb.connect()
    attach_manifest(con, layout, {})
    con.register("formula_rows", ca.rows_to_table("formulas", rows))
    con.execute('INSERT INTO canonical.formulas BY NAME SELECT * FROM formula_rows')
    validator = Validator(layout, {})
    validator.con = con
    validator.check_object_ids()
    assert validator.checks[-1].violations == 0
    con.execute("UPDATE canonical.formulas SET raw_locator='/changed' WHERE raw_format='MATHML'")
    validator.check_object_ids()
    assert validator.checks[-1].violations == 1
    con.close()
