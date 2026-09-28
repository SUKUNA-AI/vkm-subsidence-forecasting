"""Embedding units ``vkm-units-v1`` (agent J): grouping rules on hand-made rows and on the synthetic canon of D."""
from __future__ import annotations

import pytest

from vkm_corpus.retrieval_lab.units import SourceMeta, Unit, UnitConfig, build_units, ctx_windows, render, unit_id


def _page(pid: str, idx: int, sid: str = "VKM-SRC-001") -> dict:
    return {"page_id": pid, "source_id": sid, "page_index": idx, "page_kind": "PDF_PAGE",
            "printed_page_labels": [str(idx + 10)], "normalized_text": "x"}


def _block(pid: str, n: int, text: str, btype: str = "TEXT", primary: bool = True, y: float | None = None) -> dict:
    y = 100.0 + 40 * n if y is None else y
    return {"object_id": f"{pid}:b{n:012x}", "source_id": pid.split(":")[0], "page_id": pid, "block_type": btype,
            "reading_order": n, "is_primary_layer": primary, "normalized_text": text, "language": "ru",
            "bbox_x0": 50.0, "bbox_y0": y, "bbox_x1": 500.0, "bbox_y1": y + 30}


P1, P2 = "VKM-SRC-001:p0001", "VKM-SRC-001:p0002"


def test_block_groups_headings_running_heads_and_layers():
    blocks = [_block(P1, 0, "Колонтитул журнала", "PAGE_HEADER"),
              _block(P1, 1, "1. Введение", "HEADING"),
              _block(P1, 2, "Первый абзац о сдвижении горных пород. " * 20),
              _block(P1, 3, "Второй абзац про оседание земной поверхности. " * 20),
              _block(P1, 4, "Скрытый слой", primary=False),
              _block(P1, 5, "Короткий хвост."),
              _block(P1, 6, "12", "PAGE_NUMBER")]
    units = build_units([_page(P1, 1)], blocks, config=UnitConfig())
    groups = [u for u in units if u.kind == "BLOCK_GROUP"]
    text = "\n".join(u.text for u in groups)
    assert "Колонтитул" not in text and "Скрытый слой" not in text and "\n12" not in text
    assert groups[0].text.startswith("1. Введение\nПервый абзац"), "heading sticks to the following text"
    assert groups[-1].text.endswith("Короткий хвост."), "short tail joins the previous group"
    assert all(u.section_title == "1. Введение" for u in groups)
    assert all(len(u.text) <= 1600 for u in groups)
    assert len(groups) == 2 and groups[0].next_text and groups[-1].prev_text
    again = build_units([_page(P1, 1)], blocks)
    assert [u.unit_id for u in again] == [u.unit_id for u in units]


def test_long_block_is_split_into_parts_at_sentences():
    long_text = " ".join(f"Предложение номер {i} о ползучести каменной соли." for i in range(80))
    units = build_units([_page(P1, 1)], [_block(P1, 1, long_text)])
    parts = [u for u in units if u.kind == "BLOCK_GROUP"]
    assert len(parts) >= 2 and all(len(u.text) <= 1600 for u in parts)
    assert [u.part for u in parts] == list(range(len(parts)))
    assert all(u.text.endswith(".") for u in parts)
    assert len({u.unit_id for u in parts}) == len(parts)


def test_figure_table_formula_units_carry_context():
    cap = _block(P1, 3, "Рис. 3.1. Мульда сдвижения над панелью", "CAPTION")
    blocks = [_block(P1, 1, "Общее введение без ссылок."),
              _block(P1, 2, "Как видно на рис. 3.1, максимум оседаний смещён к центру панели."),
              cap, _block(P1, 4, "Табл. 2 содержит параметры ползучести."),
              _block(P1, 5, "где η — вязкость, t — время.", y=700.0)]
    fig = {"object_id": f"{P1}:f000000000001", "page_id": P1, "source_id": "VKM-SRC-001", "figure_label": "Рис. 3.1",
           "caption_normalized": "Мульда сдвижения над панелью", "caption_block_id": cap["object_id"],
           "image_artifact_id": "sha256:" + "a" * 64}
    tab = {"object_id": f"{P1}:t000000000001", "page_id": P1, "source_id": "VKM-SRC-001", "table_label": "Таблица 2",
           "caption_normalized": "Параметры", "normalized_text": "параметр | значение\nA | 1,5"}
    frm = {"object_id": f"{P1}:m000000000001", "page_id": P1, "source_id": "VKM-SRC-001", "equation_label": "(3.2)",
           "normalized_latex": r"\dot\varepsilon = A \sigma^n", "raw_format": "LATEX",
           "bbox_x0": 100.0, "bbox_y0": 650.0, "bbox_x1": 400.0, "bbox_y1": 690.0}
    units = build_units([_page(P1, 1)], blocks, [fig], [tab], [frm])
    kinds = {u.kind: u for u in units}
    assert "максимум оседаний" in kinds["FIGURE"].text and kinds["FIGURE"].text.startswith("Рис. 3.1 Мульда")
    assert "A | 1,5" in kinds["TABLE"].text and "Табл. 2 содержит" in kinds["TABLE"].text
    assert kinds["FORMULA"].text.splitlines()[1].startswith("(3.2)") and "где η" in kinds["FORMULA"].text
    assert all("Мульда сдвижения над панелью" not in u.text for u in units if u.kind == "BLOCK_GROUP"), \
        "a caption used by a figure is not repeated in block groups"


def test_image_only_formula_and_captionless_figure_are_skipped():
    frm = {"object_id": f"{P1}:m000000000002", "page_id": P1, "source_id": "VKM-SRC-001", "raw_format": "IMAGE_ONLY"}
    fig = {"object_id": f"{P1}:f000000000002", "page_id": P1, "source_id": "VKM-SRC-001"}
    units = build_units([_page(P1, 1)], [], [fig], [], [frm])
    assert units == []


def test_context_variants_and_windows():
    meta = SourceMeta("VKM-SRC-001", "VKM-WRK-001", "Синтетическая статья", "Альфаев А.А.", 2020, "Вестник",
                      "journal_article", "VKM_regional")
    u = Unit(unit_id("BLOCK_GROUP", ["x"]), "BLOCK_GROUP", "VKM-SRC-001", P1, ("x",), "Текст единицы.",
             section_title="2. Методы", prev_text="Предыдущее.", next_text="Следующее.")
    assert render(u, "A") == "Текст единицы."
    assert render(u, "B", meta).splitlines() == ["Синтетическая статья. 2. Методы", "Текст единицы."]
    assert render(u, "C", meta).splitlines()[1:] == ["Предыдущее.", "Текст единицы.", "Следующее."]
    assert "область: VKM_regional" in render(u, "D", meta, "15") and "с. 15" in render(u, "D", meta, "15")
    units = build_units([_page(P1, 1), _page(P2, 2)],
                        [_block(P1, i, f"Абзац {i} " + "слово " * 150) for i in range(1, 6)] +
                        [_block(P2, i, f"Абзац {i} " + "слово " * 150) for i in range(1, 6)])
    windows = ctx_windows(units, max_units=4)
    assert [len(w) for w in windows][0] == 4 and sum(len(w) for w in windows) == len(units)


def test_units_of_synthetic_canon(tmp_path):
    pytest.importorskip("duckdb")
    pytest.importorskip("pyarrow")
    from vkm_corpus.retrieval_lab.canon import CanonReader
    from vkm_corpus.testing import synthetic_canon

    canon = synthetic_canon(tmp_path / "data", with_duckdb=True)
    reader = CanonReader.from_duckdb(canon.duckdb_path)
    rows = reader.load_all()
    units = build_units(rows["pages"], rows["blocks"], rows["figures"], rows["tables"], rows["formulas"],
                        rows["bibliography"])
    kinds = {u.kind for u in units}
    assert {"BLOCK_GROUP", "FIGURE", "TABLE", "FORMULA", "BIB_ENTRY"} <= kinds
    assert all(u.page_id for u in units)
    assert not any("Синтетический колонтитул" in u.text for u in units)
    fig = next(u for u in units if u.kind == "FIGURE")
    assert canon.ids["figure"] in fig.object_ids and "Синтетическая схема мульды" in fig.text
    # the non-primary embedded layer of source 002 page 2 is not in the units; the OCR layer is
    assert any(canon.ids["ocr_block"] in u.object_ids for u in units)
    assert not any(canon.ids["embedded_block"] in u.object_ids and u.page_id.endswith("p0002") for u in units)
    meta = reader.source_meta()
    assert meta["VKM-SRC-025"].work_id == "VKM-WRK-013"
    reader.close()
