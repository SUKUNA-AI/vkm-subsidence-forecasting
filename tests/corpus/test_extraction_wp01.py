"""Required-behavior regressions for ENC-01 and EPUB-01–03."""
from __future__ import annotations

import base64
import hashlib
from zipfile import ZipFile

import pytest

from vkm_corpus.extract.text_repair import remap_cp1251


def test_mixed_cp1251_preserves_valid_math_and_latin():
    text = "×èñëî; E = 2 × 10^3; a ÷ b; Müller; déjà vu"
    assert remap_cp1251(text) == "Число; E = 2 × 10^3; a ÷ b; Müller; déjà vu"


def test_contextual_repair_preserves_ambiguous_and_declared_unicode_runs():
    from vkm_corpus.extract.text_repair import contextual_cp1251

    value = contextual_cp1251("ä÷ó, été")
    assert value.text == "ä÷ó, été"
    span, = value.spans
    assert (span.start, span.end, span.raw, span.replacement, span.reason) == (
        0, 3, "ä÷ó", None, "AMBIGUOUS_CP1251_WORD")
    mapped = contextual_cp1251("×èñëî", mapped_font=True)
    assert mapped.text == "×èñëî"
    assert mapped.spans[0].reason == "DECLARED_UNICODE_FONT"


def _epub(tmp_path, *, body, nav=None, ncx=None, other=None, nav_member="OPS/nav/toc.xhtml", encoded=False,
          payloads=()):
    pytest.importorskip("lxml")
    from vkm_corpus.extract.epub import read_epub

    path = tmp_path / "test.epub"
    ns = 'xmlns="http://www.w3.org/1999/xhtml" xmlns:epub="http://www.idpf.org/2007/ops"'
    member_href = "text/ch%20one.xhtml" if encoded else "text/ch one.xhtml"
    manifest = f'<item id="c" href="{member_href}" media-type="application/xhtml+xml"/>'
    spine = '<itemref idref="c"/>'
    if nav is not None:
        manifest += f'<item id="n" href="{nav_member.removeprefix("OPS/")}" properties="nav" media-type="application/xhtml+xml"/>'
    if ncx is not None:
        manifest += '<item id="x" href="nav/pages.ncx" media-type="application/x-dtbncx+xml"/>'
    if other is not None:
        manifest += '<item id="d" href="text/other.xhtml" media-type="application/xhtml+xml"/>'
        spine += '<itemref idref="d"/>'
    raw = f"<html {ns}><body>{body}</body></html>".encode()
    with ZipFile(path, "w") as z:
        z.writestr("mimetype", "application/epub+zip")
        z.writestr("META-INF/container.xml", '<container><rootfiles><rootfile full-path="OPS/book.opf"/></rootfiles></container>')
        z.writestr("OPS/book.opf", f'<package version="3.0"><manifest>{manifest}</manifest><spine>{spine}</spine></package>')
        z.writestr("OPS/text/ch one.xhtml", raw)
        if other is not None:
            z.writestr("OPS/text/other.xhtml", f"<html {ns}><body>{other}</body></html>")
        if nav is not None:
            z.writestr(nav_member, f"<html {ns}>{nav}</html>")
        if ncx is not None:
            z.writestr("OPS/nav/pages.ncx", ncx)
        for member, payload in payloads:
            z.writestr(member, payload)
    return read_epub(path), raw


def test_epub_mixed_dom_has_one_order_with_raw_locators(tmp_path):
    doc, raw = _epub(tmp_path, body='<div id="mix">BEFORE <p>M<em>ID</em>DLE</p> AFTER</div>')
    unit = doc.spine[0]
    assert [b.text for b in unit.blocks] == ["BEFORE", "MIDDLE", "AFTER"]
    assert len({b.locator for b in unit.blocks}) == 3
    assert base64.b64decode(unit.raw_markup_base64) == raw


def test_epub_nested_list_owns_text_and_keeps_real_duplicates(tmp_path):
    doc, _ = _epub(tmp_path, body='<blockquote><ol><li>First<ul><li>Same</li><li>Same</li></ul>'
                            ' tail</li></ol></blockquote>')
    assert [b.text for b in doc.spine[0].blocks] == ["First", "Same", "Same", "tail"]
    assert all(b.block_type == "LIST_ITEM" for b in doc.spine[0].blocks)


def test_epub_page_list_is_resolved_from_nav_member(tmp_path):
    doc, _ = _epub(tmp_path, body='<p id="a">text</p>', nav='<body><nav epub:type="page-list"><a '
                    'href="../text/ch%20one.xhtml#a">12</a></nav></body>')
    assert (doc.page_list_source, doc.n_page_anchors) == ("NAV_PAGE_LIST", 1)
    assert doc.spine[0].page_anchors == [("12", "NAV_PAGE_LIST")]


def test_epub_page_list_resolves_ncx_bases_and_encoded_targets(tmp_path):
    doc, _ = _epub(tmp_path, body='<p id="значение">print location</p>', encoded=True, ncx='''<ncx>
      <pageList xml:base="../"><pageTarget><navLabel><text>iv</text></navLabel>
      <content xml:base="text/" src="ch%20one.xhtml#%D0%B7%D0%BD%D0%B0%D1%87%D0%B5%D0%BD%D0%B8%D0%B5"/></pageTarget>
      </pageList></ncx>''')
    assert doc.page_list_source == "NCX_PAGE_LIST"
    anchor, = doc.spine[0].page_anchor_details
    assert (anchor["label"], anchor["member"], anchor["fragment"], anchor["source_member"]) == (
        "iv", "OPS/text/ch one.xhtml", "значение", "OPS/nav/pages.ncx")
    assert doc.spine[0].index == 1


def test_epub_nav_fragment_only_and_html_base(tmp_path):
    doc, _ = _epub(tmp_path, body='<p id="target">page</p>',
                    nav='<head><base href="../text/ch%20one.xhtml"/></head><body>'
                        '<nav epub:type="page-list"><a href="#target">42</a></nav></body>')
    assert doc.spine[0].page_anchors == [("42", "NAV_PAGE_LIST")]


def test_epub_invalid_nav_keeps_fallback_and_reports_conflicts(tmp_path):
    doc, _ = _epub(tmp_path, body='<span epub:type="pagebreak" id="conflict" title="34"/>'
                                 '<span epub:type="pagebreak" id="valid" title="35"/>',
                    nav='''<body><nav epub:type="page-list">
                      <a href="../text/ch%20one.xhtml#missing">3</a>
                      <a href="../text/absent.xhtml">4</a>
                      <a href="https://invalid.test/">5</a>
                      <a href="%2e%2e/../../text/ch%20one.xhtml">6</a>
                      <a href="../text/ch%20one.xhtml#conflict">36</a>
                    </nav></body>''')
    assert doc.page_list_source == "EPUB_PAGEBREAK"
    assert doc.spine[0].page_anchors == [("35", "EPUB_PAGEBREAK")]
    assert [d["reason"] for d in doc.pagination_diagnostics] == [
        "PAGE_FRAGMENT_NOT_FOUND", "PAGE_LINK_NOT_ADMITTED_SPINE_MEMBER", "NON_PACKAGE_URI",
        "MEMBER_ESCAPES_PACKAGE"]
    conflict, = [d for d in doc.spine[0].diagnostics if d["code"] == "PAGE_ANCHOR_CONFLICT"]
    assert {item["label"] for item in conflict["candidates"]} == {"34", "36"}


def test_epub_typed_children_have_separate_ownership(tmp_path):
    doc, _ = _epub(tmp_path, body='<blockquote>one <em>word</em><br/> next'
                    '<table><tr><td>cell</td></tr></table> after-table'
                    '<math xmlns="http://www.w3.org/1998/Math/MathML"><mi>x</mi></math> after-math'
                    '<pre> line\n second  line\n</pre></blockquote>')
    unit, = doc.spine
    assert [b.text for b in unit.blocks] == ["one word next", "after-table", "after-math", " line\n second  line\n"]
    assert unit.blocks[0].order < unit.tables[0].order < unit.blocks[1].order < unit.maths[0].order < unit.blocks[2].order
    assert "after-table" not in unit.tables[0].html and "after-math" not in unit.maths[0].markup
    text_slots = [slot for b in unit.blocks for slot in b.text_slots]
    assert len(text_slots) == len(set(text_slots))


def test_epub_svg_cover_keeps_referenced_image(tmp_path):
    doc, _ = _epub(tmp_path, body='<svg xmlns="http://www.w3.org/2000/svg" xmlns:xlink="http://www.w3.org/1999/xlink">'
                    '<title>Embedded figure</title><image xlink:href="../assets/cover.jpg"/></svg>',
                    payloads=(("OPS/assets/cover.jpg", b"fixture"),))
    unit, = doc.spine
    assert unit.blocks == []
    image, = unit.images
    assert image.href == "OPS/assets/cover.jpg" and not image.is_formula_candidate
    assert unit.diagnostics[0]["code"] == "RAW_ONLY_SVG"


def test_pdf_native_repair_keeps_raw_runs_and_flags_only_changes(tmp_path):
    pymupdf = pytest.importorskip("pymupdf")
    from vkm_corpus.extract.classify import classify_pdf_page, route_for
    from vkm_corpus.extract.model import SourceInput
    from vkm_corpus.extract.pdf_native import block_text, extract_page
    from vkm_corpus.pipeline.assemble import Assembler
    from vkm_corpus.pipeline.config import PipelineConfig
    from vkm_corpus.pipeline.context import open_cache, open_store
    from vkm_corpus.pipeline.prepare import prepare_source

    doc = pymupdf.open()
    page = doc.new_page()
    page.insert_text((40, 30), "×èñëî; E = 2 × 10^3; a ÷ b; Müller")
    page.insert_text((40, 75), "Valide déjà vu")
    path = tmp_path / "mixed.pdf"
    doc.save(path)
    result = extract_page(doc, 0)
    assert route_for(*classify_pdf_page(result.features))[0] == "NATIVE_REPAIR"
    repaired = result.raw["cp1251_repair"]["blocks"]
    first, second = result.raw["blocks"]
    evidence = repaired[str(first["n"])]["runs"][0]
    assert evidence["spans"] == [dict(start=0, end=5, raw="×èñëî", replacement="Число", reason="CP1251_WORD_SHAPE")]
    assert evidence["font_resources"] and result.raw["texttrace"]
    assert repaired[str(first["n"])]["text"] == "Число; E = 2 × 10^3; a ÷ b; Müller"
    assert result.raw["cp1251_repair"]["raw_plain_text"] == result.plain_text
    assert block_text(first) == "×èñëî; E = 2 × 10^3; a ÷ b; Müller"
    assert str(second["n"]) not in repaired
    doc.close()
    raw_bytes = path.read_bytes()
    src = SourceInput("VKM-SRC-901", path.name, hashlib.sha256(raw_bytes).hexdigest(), len(raw_bytes))
    cfg = PipelineConfig(tmp_path / "data", tmp_path)
    run = "RUN-20260928T000000Z-0000c0de"
    store, cache = open_store(cfg, run, "wp01"), open_cache(cfg, run, "wp01")
    prep = prepare_source(cfg, store, cache, src)
    asm = Assembler(cfg, store, cache, src, prep, None)
    built = asm.build()
    assert [b.text for b in built.blocks] == ["Число; E = 2 × 10^3; a ÷ b; Müller", "Valide déjà vu"]
    assert ["ENCODING_REPAIRED" in b.quality_flags for b in built.blocks] == [True, False]
    assert {b.generation for b in built.blocks} == {"3"}


def test_epub_current_prepare_does_not_reuse_legacy_cache_or_merge_duplicate_runs(tmp_path):
    from vkm_corpus.extract.model import SourceInput
    from vkm_corpus.extract.to_canon import CanonMapper
    from vkm_corpus.pipeline.assemble import Assembler
    from vkm_corpus.pipeline.commit import load_state
    from vkm_corpus.pipeline.config import PipelineConfig
    from vkm_corpus.pipeline.context import open_cache, open_store
    from vkm_corpus.pipeline.prepare import PREP_SCHEMA, prep_path, prepare_signature, prepare_source
    _epub(tmp_path, body='<div>SAME<p id="n" epub:type="pagebreak" title="8">MIDDLE</p>SAME</div>',
          nav='<body><nav epub:type="page-list"><a href="../text/ch%20one.xhtml#n">9</a></nav></body>')
    path = (tmp_path / "test.epub").rename(tmp_path / "renamed.bin")
    raw_bytes = path.read_bytes()
    src = SourceInput("VKM-SRC-901", path.name, hashlib.sha256(raw_bytes).hexdigest(), len(raw_bytes),
                      evidence_scope="GENERAL_METHOD")
    cfg = PipelineConfig(tmp_path / "data", tmp_path)
    run = "RUN-20260928T000000Z-0000c0de"
    legacy_sig = prepare_signature(cfg, src, file_format="UNKNOWN")
    legacy = prep_path(cfg.data_root, src.source_id, legacy_sig)
    legacy.parent.mkdir(parents=True)
    old = '{"schema":"%s","status":"PREPARED","prepare_signature":"%s","inspect":{"file_format":"EPUB"}}' % (
        PREP_SCHEMA, legacy_sig)
    legacy.write_text(old, encoding="utf-8")
    assert load_state(cfg, src) == (None, None)
    store, cache = open_store(cfg, run, "wp01"), open_cache(cfg, run, "wp01")
    prep = prepare_source(cfg, store, cache, src)
    assert prep["status"] == "PREPARED" and prep["prepare_signature"] != legacy_sig
    loaded, visual = load_state(cfg, src)
    assert loaded["prepare_signature"] == prep["prepare_signature"] and visual is None
    assert loaded["pages"] == prep["pages"]
    assert legacy.read_text(encoding="utf-8") == old
    assembler = Assembler(cfg, store, cache, src, prep, None)
    result = assembler.build()
    assert [b.text for b in result.blocks] == ["SAME", "MIDDLE", "SAME"]
    assert {b.generation for b in result.blocks} == {"3"} and len({b.raw_locator for b in result.blocks}) == 3
    assert len({item["locator"] for item in assembler.accounting.candidates.values()}) == 3
    mapper = CanonMapper(result, run_id=run, config_hashes={"objects": "c" * 64})
    blocks = mapper.blocks()
    assert len({b.object_id for b in blocks}) == 3
    page, = mapper.pages(blocks)
    assert page.page_id == "VKM-SRC-901:s0001"
    assert page.printed_label_status == "CONFLICT" and page.printed_page_labels == []
