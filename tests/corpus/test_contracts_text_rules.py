"""Text rules page_text_v1 / rerank_text_v1 / normalize_text_v1 (H-04). Synthetic text only."""
from __future__ import annotations

import hashlib

from vkm_corpus.contracts.text_rules import normalize_text_v1, page_text_v1, rerank_text_v1


def _block(oid, order, text, btype="TEXT", primary=True):
    return {"object_id": oid, "reading_order": order, "normalized_text": text, "block_type": btype,
            "is_primary_layer": primary}


def test_normalize_whitespace_hyphenation_and_controls():
    raw = "Первая стро-\nка и  вто­\nрая\r\nлиния​ ﬁne\x07"
    assert normalize_text_v1(raw) == "Первая строка и вторая линия fine"
    assert normalize_text_v1("северо-\nЗапад") == "северо- Запад"      # only lower-case continuation joins
    assert normalize_text_v1(None) == "" and normalize_text_v1("  \n ") == ""
    assert normalize_text_v1("ёлка �") == "ёлка �"            # ё and U+FFFD are kept


def test_normalize_is_idempotent():
    for s in ["a-\nb", " x\t\ty ", "ab́c", "слово-\nслово", "A­\nB"]:
        once = normalize_text_v1(s)
        assert normalize_text_v1(once) == once


def test_page_text_uses_primary_layer_in_reading_order_without_running_heads():
    blocks = [
        _block("S:p0001:b2", 2, "второй абзац"),
        _block("S:p0001:b1", 1, "первый  абзац"),
        _block("S:p0001:b0", 0, "Колонтитул", btype="PAGE_HEADER"),
        _block("S:p0001:b9", 9, "17", btype="PAGE_NUMBER"),
        _block("S:p0001:bx", 1, "другой слой", primary=False),
        _block("S:p0001:be", 5, "   "),
    ]
    pt = page_text_v1(blocks)
    assert pt.normalized_text == "первый абзац\n\nвторой абзац"
    assert pt.text_sha256 == hashlib.sha256(pt.normalized_text.encode("utf-8")).hexdigest()
    assert pt.char_count == len(pt.normalized_text) and pt.rule == "page_text_v1"
    assert pt.block_ids == ("S:p0001:b1", "S:p0001:b2")
    assert page_text_v1(reversed(blocks)) == pt                          # order of input rows does not matter


def test_page_without_text():
    pt = page_text_v1([_block("S:p0001:b0", 0, "Колонтитул", btype="PAGE_FOOTER")])
    assert (pt.normalized_text, pt.text_sha256, pt.char_count) == (None, None, 0)


def test_rerank_text_by_kind():
    assert rerank_text_v1("BLOCK", {"normalized_text": "абзац"}) == "абзац"
    assert rerank_text_v1("FIGURE", {"figure_label": "Рис. 1", "caption_normalized": "схема"}) == "Рис. 1 схема"
    assert rerank_text_v1("FIGURE", {"figure_label": None, "caption_normalized": None}) is None
    assert rerank_text_v1("TABLE", {"table_label": "Таблица 2", "caption_normalized": None,
                                    "normalized_text": "a | b"}) == "Таблица 2\na | b"
    assert rerank_text_v1("FORMULA", {"equation_label": "(1)", "normalized_latex": None, "raw_format": "LATEX",
                                      "raw_output": "x=1"}) == "(1) x=1"
    assert rerank_text_v1("FORMULA", {"equation_label": None, "normalized_latex": None, "raw_format": "IMAGE_ONLY",
                                      "raw_output": "ignored"}) is None
