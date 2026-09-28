"""EPUB (zipfile + lxml) and DOCX (python-docx + raw OMML) extraction on synthetic containers (agent C)."""
from __future__ import annotations

import pytest

pytest.importorskip("lxml")
pytest.importorskip("PIL")

from vkm_corpus.extract import epub  # noqa: E402
from vkm_corpus.extract.detect import inspect_file  # noqa: E402
from vkm_corpus.extract.synthetic import make_docx, make_epub  # noqa: E402


def test_epub_spine_blocks_anchors_formulas(tmp_path):
    path = make_epub(tmp_path / "s.epub", n_units=3)
    assert inspect_file(path).file_format == "EPUB"
    ed = epub.read_epub(path)
    assert len(ed.spine) == 3 == epub.count_spine_stdlib(path)
    assert ed.page_list_source == "CALIBRE_PAGE_ID" and ed.n_page_anchors == 6
    u1 = ed.spine[0]
    assert [a[0] for a in u1.page_anchors] == ["1", "2"]
    gifs = [i for i in u1.images if i.is_formula_candidate]
    assert len(gifs) == 2
    display, inline = gifs
    assert display.inline is False and display.equation_label == "(1.2)"
    assert inline.inline is True
    assert any(not i.is_formula_candidate for i in u1.images)  # JPEG with alt → figure
    assert len(u1.tables) == 1
    assert any(b.block_type == "HEADING" for b in u1.blocks)
    last = ed.spine[-1]
    assert last.bib_ids == ["BIBe-1", "BIBe-2"]
    assert sum(1 for b in last.blocks if b.block_type == "REFERENCE_LIST") == 2


def test_epub_pagination(tmp_path):
    from vkm_corpus.extract.pages import paginate_epub

    p = paginate_epub(make_epub(tmp_path / "s.epub", n_units=4))
    assert (p.unit, p.page_kind, p.count, p.check_count) == ("s", "EPUB_SPINE_ITEM", 4, 4)


def test_docx_paragraphs_omml_tables(tmp_path):
    pytest.importorskip("docx")
    from vkm_corpus.extract.docx import align_to_pages, read_docx

    path = make_docx(tmp_path / "s.docx")
    assert inspect_file(path).file_format == "DOCX"
    d = read_docx(path)
    assert d.counts["omath"] == 2 and d.counts["omath_para"] == 1
    kinds = sorted(m.display for m in d.maths)
    assert kinds == [False, True]
    assert all(m.omml.startswith("<m:oMath") for m in d.maths)
    assert d.maths[0].linear_text == "E=mc2"
    assert d.counts["tables_body"] == 1 and d.tables[0].n_rows == 2 and d.tables[0].n_cols == 2
    assert d.paragraphs[0].block_type == "HEADING"
    assert all(p.path.startswith("/w:document/w:body/w:p[") for p in d.paragraphs)
    # render alignment is a DERIVATION: sequential text search over the render's page texts
    pages = ["Synthetic heading " + d.paragraphs[1].text[:30], d.paragraphs[1].text[30:] + " rest"]
    got = align_to_pages(["Synthetic heading", d.paragraphs[1].text], pages)
    assert got[0][0] == 1 and got[1][0] == 1
