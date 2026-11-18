"""End-to-end native EPUB table and formula ownership."""
from __future__ import annotations

import base64
import hashlib
from zipfile import ZipFile

import pytest


@pytest.mark.parametrize("prefix", ["", "x:"])
def test_native_epub_table_uses_xhtml_grid_and_dom_text_order(tmp_path, prefix, monkeypatch):
    pytest.importorskip("lxml")
    from vkm_corpus.extract.model import SourceInput
    from vkm_corpus.extract.to_canon import CanonMapper
    from vkm_corpus.ocr import normalize
    from vkm_corpus.pipeline.assemble import Assembler
    from vkm_corpus.pipeline.commit import commit_signature
    from vkm_corpus.pipeline.config import PipelineConfig
    from vkm_corpus.pipeline.context import open_cache, open_store
    from vkm_corpus.pipeline.prepare import prepare_source

    x = prefix
    content = f'''<html xmlns="http://www.w3.org/1999/xhtml"
                       xmlns:x="http://www.w3.org/1999/xhtml"
                       xmlns:m="http://www.w3.org/1998/Math/MathML">
      <body><p>table introduction</p>
        <{x}table id="outer"><{x}thead><{x}tr>
          <{x}td colspan="2">Pressure <{x}em><{x}span>MP</{x}span>a</{x}em> (unit)</{x}td>
        </{x}tr></{x}thead><{x}tbody>
          <{x}tr><{x}td rowspan="2">Station</{x}td><{x}td>before <{x}em><{x}span>≥</{x}span>12</{x}em> after
            <{x}table id="child"><{x}tbody><{x}tr><{x}td>Child <m:math><m:mi>p</m:mi><m:mo>≥</m:mo><m:mn>2</m:mn></m:math></{x}td></{x}tr></{x}tbody></{x}table> tail
          </{x}td></{x}tr>
          <{x}tr><{x}td></{x}td></{x}tr>
        </{x}tbody></{x}table>
      </body></html>'''.encode("utf-8")
    path = tmp_path / "structure.epub"
    with ZipFile(path, "w") as z:
        z.writestr("mimetype", "application/epub+zip")
        z.writestr("META-INF/container.xml", '<container><rootfiles><rootfile full-path="OPS/book.opf"/></rootfiles></container>')
        z.writestr("OPS/book.opf", '<package version="3.0"><manifest><item id="page" href="chapter.xhtml" '
                   'media-type="application/xhtml+xml"/></manifest><spine><itemref idref="page"/></spine></package>')
        z.writestr("OPS/chapter.xhtml", content)
    data = path.read_bytes()
    source = SourceInput("VKM-SRC-901", path.name, hashlib.sha256(data).hexdigest(), len(data),
                         evidence_scope="GENERAL_METHOD")
    cfg = PipelineConfig(tmp_path / "data", tmp_path)
    run = "RUN-20260928T000000Z-0000c0de"
    store, cache = open_store(cfg, run, "wp03"), open_cache(cfg, run, "wp03")
    prep = prepare_source(cfg, store, cache, source)
    result = Assembler(cfg, store, cache, source, prep, None).build()
    mapper = CanonMapper(result, run_id=run, config_hashes={"objects": "c" * 64})
    outer, child = mapper.tables()
    formula, = mapper.formulas()

    assert (outer.n_rows, outer.n_cols, outer.header_rows) == (3, 2, 1)
    assert [(c.row, c.col, c.row_span, c.col_span, c.is_header, c.text) for c in outer.cells] == [
        (0, 0, 1, 2, True, "Pressure MPa (unit)"),
        (1, 0, 2, 1, False, "Station"),
        (1, 1, 1, 1, False, "before ≥12 after tail"),
        (2, 1, 1, 1, False, ""),
    ]
    assert (child.n_rows, child.n_cols, child.cells[0].text) == (1, 1, "Child p≥2")
    assert {outer.page_id, child.page_id, formula.page_id} == {"VKM-SRC-901:s0001"}
    assert len({outer.object_id, child.object_id, formula.object_id}) == 3
    assert child.raw_locator.startswith(outer.raw_locator + "/")
    assert formula.raw_locator.startswith(child.raw_locator + "/")
    assert formula.raw_format == "MATHML" and formula.native_glyph_text == "p≥2"
    assert "<m:mo>≥</m:mo>" in formula.raw_output and formula.normalized_latex is None
    raw = store.read_json(prep["pages"][0]["native_raw_artifact_id"])
    assert base64.b64decode(raw["raw_markup_base64"]) == content
    assert outer.raw_output == raw["tables"][0]["html"]
    assert child.raw_output == raw["tables"][1]["html"]

    signature = commit_signature(cfg, cache, source, prep, None, accounting_events=[])
    monkeypatch.setattr(normalize, "XHTML_TABLE_NORMALIZE_RULE", "synthetic-next-rule")
    assert commit_signature(cfg, cache, source, prep, None, accounting_events=[]) != signature
    rebuilt = Assembler(cfg, store, cache, source, prep, None).build()
    next_mapper = CanonMapper(rebuilt, run_id=run, config_hashes={"objects": "c" * 64})
    assert [row.object_id for row in next_mapper.tables()] != [outer.object_id, child.object_id]
    assert next_mapper.formulas()[0].object_id == formula.object_id
