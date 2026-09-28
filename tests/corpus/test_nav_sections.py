"""Sections layer of NAV (rule sections_v1) on synthetic canonical rows: method priority, nesting and non-overlap,
printed-TOC page mapping, numbering levels, junk-outline filter, stable ids, queries, CLI, native outline readers."""
from __future__ import annotations

import json
import zipfile
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

duckdb = pytest.importorskip("duckdb")

from vkm_corpus.navigation import cli as nav_cli  # noqa: E402
from vkm_corpus.navigation import outline as O  # noqa: E402
from vkm_corpus.navigation import sections as S  # noqa: E402
from vkm_corpus.navigation import sections_query as Q  # noqa: E402
from vkm_corpus.navigation.ids import section_id  # noqa: E402

DDL = """
CREATE SCHEMA canonical;
CREATE SCHEMA meta;
CREATE TABLE meta.snapshot (snapshot_id VARCHAR, manifest_sha256 VARCHAR, pipeline_version VARCHAR);
CREATE TABLE canonical.pages (source_id VARCHAR, page_id VARCHAR, page_index INTEGER, printed_page_labels VARCHAR[],
  printed_label_status VARCHAR, normalized_text VARCHAR);
CREATE TABLE canonical.documents (source_id VARCHAR, format_detected VARCHAR);
CREATE TABLE canonical.sources (source_id VARCHAR, source_class_raw VARCHAR);
CREATE TABLE canonical.source_work_links (source_id VARCHAR, work_id VARCHAR, link_type VARCHAR, is_primary BOOLEAN,
  page_start INTEGER, page_end INTEGER, curation_status VARCHAR);
CREATE TABLE canonical.works (work_id VARCHAR, title VARCHAR);
CREATE TABLE canonical.blocks (source_id VARCHAR, page_id VARCHAR, object_id VARCHAR, block_type VARCHAR,
  reading_order INTEGER, text VARCHAR, is_primary_layer BOOLEAN, char_count INTEGER);
CREATE TABLE canonical.figures (object_id VARCHAR, source_id VARCHAR, page_id VARCHAR);
CREATE TABLE canonical.tables (object_id VARCHAR, source_id VARCHAR, page_id VARCHAR);
CREATE TABLE canonical.formulas (object_id VARCHAR, source_id VARCHAR, page_id VARCHAR);
CREATE TABLE canonical.bibliography_entries (object_id VARCHAR, source_id VARCHAR, page_id VARCHAR);
INSERT INTO meta.snapshot VALUES ('snap-test', 'ab', '0.1.0');
"""


class Canon:
    def __init__(self, path: str | None = None):
        self.con = duckdb.connect(path or ":memory:")
        self.con.execute(DDL)
        self.n_blocks = 0

    def source(self, sid: str, n_pages: int, *, fmt: str = "PDF", klass: str = "monograph", offset: int | None = None,
               roman: int = 0, work: str | None = None, title: str | None = None) -> None:
        for i in range(1, n_pages + 1):
            labels: list[str] = []
            if i <= roman:
                labels = [["i", "ii", "iii", "iv", "v", "vi"][i - 1]]
            elif offset is not None and i - offset >= 1:
                labels = [str(i - offset)]
            self.con.execute("INSERT INTO canonical.pages VALUES (?, ?, ?, ?, ?, ?)",
                             [sid, f"{sid}:p{i:04d}", i, labels, "CONSISTENT_SEQUENCE" if labels else "NONE", ""])
        self.con.execute("INSERT INTO canonical.documents VALUES (?, ?)", [sid, fmt])
        self.con.execute("INSERT INTO canonical.sources VALUES (?, ?)", [sid, klass])
        wid = work or f"W-{sid}"
        self.con.execute("INSERT INTO canonical.source_work_links VALUES (?, ?, 'FULL_COPY', true, NULL, NULL, "
                         "'CURATED')", [sid, wid])
        self.con.execute("INSERT INTO canonical.works VALUES (?, ?)", [wid, title or f"Work of {sid}"])

    def block(self, sid: str, page: int, btype: str, text: str, order: int | None = None) -> str:
        self.n_blocks += 1
        oid = f"{sid}:p{page:04d}:b{self.n_blocks:05d}"
        self.con.execute("INSERT INTO canonical.blocks VALUES (?, ?, ?, ?, ?, ?, true, ?)",
                         [sid, f"{sid}:p{page:04d}", oid, btype, order if order is not None else self.n_blocks, text,
                          len(text)])
        self.con.execute("UPDATE canonical.pages SET normalized_text = normalized_text || ' ' || ? WHERE page_id = ?",
                         [text, f"{sid}:p{page:04d}"])
        return oid


BOOK_TOC = "\n".join([
    "Оглавление",
    "Введение ........................ 1",
    "Глава 1. Основы ....... 3",
    "1.1. Первый раздел",
    "с длинным заглавием ........ 3",
    "1.2. Второй раздел ...... 9",
    "Глава 2. Методы ....... 15",
    "2.1. Модели ....... 16",
    "Заключение ....... 30",
    "Список литературы ....... 33",
])
BOOK_HEADINGS = [(5, "ВВЕДЕНИЕ"), (7, "ГЛАВА 1. ОСНОВЫ"), (7, "1.1. Первый раздел с длинным заглавием"),
                 (13, "1.2. Второй раздел"), (19, "ГЛАВА 2. МЕТОДЫ"), (20, "2.1. Модели"), (34, "ЗАКЛЮЧЕНИЕ"),
                 (37, "СПИСОК ЛИТЕРАТУРЫ")]
BOOK_OUTLINE = [{"level": 1, "title": "Введение", "page_index": 5},
                {"level": 1, "title": "Глава 1. Основы", "page_index": 7},
                {"level": 2, "title": "1.1. Первый раздел с длинным заглавием", "page_index": 7},
                {"level": 2, "title": "1.2. Второй раздел", "page_index": 13},
                {"level": 1, "title": "Глава 2. Методы", "page_index": 19},
                {"level": 2, "title": "2.1. Модели", "page_index": 20},
                {"level": 1, "title": "Заключение", "page_index": 34},
                {"level": 1, "title": "Список литературы", "page_index": 37}]
EXPECTED_BOOK = [("Введение", 1, 5, 6), ("Основы", 1, 7, 18), ("Первый раздел с длинным заглавием", 2, 7, 12),
                 ("Второй раздел", 2, 13, 18), ("Методы", 1, 19, 33), ("Модели", 2, 20, 33),
                 ("Заключение", 1, 34, 36), ("Список литературы", 1, 37, 40)]


def make_corpus(path: str | None = None) -> Canon:
    c = Canon(path)
    # A: a book of 40 pages, roman front matter i–iv, printed page = physical − 4, printed TOC on page 3
    c.source("SRC-A", 40, offset=4, roman=4)
    c.block("SRC-A", 1, "TITLE", "Синтетическая книга")
    c.block("SRC-A", 3, "TABLE_OF_CONTENTS", BOOK_TOC)
    for page, text in BOOK_HEADINGS:
        c.block("SRC-A", page, "HEADING", text)
    # B: a report with numbered headings only (no labels, no TOC)
    c.source("SRC-B", 12, klass="technical_report")
    for page, text in [(2, "1. Постановка задачи"), (4, "1.1 Исходные данные"), (6, "1.2 Допущения"),
                       (8, "2. Решение"), (10, "Выводы")]:
        c.block("SRC-B", page, "HEADING", text)
    # C: an article: a title and unnumbered headings
    c.source("SRC-C", 6, klass="journal_article")
    c.block("SRC-C", 1, "TITLE", "Оседание земной поверхности")
    c.block("SRC-C", 1, "TITLE", "над выработками")
    c.block("SRC-C", 1, "HEADING", "УДК 622.83")
    c.block("SRC-C", 2, "HEADING", "Методика наблюдений")
    c.block("SRC-C", 4, "HEADING", "Результаты")
    # D: nothing structural
    c.source("SRC-D", 5, klass="journal_article", title="Короткая заметка")
    # E: a journal issue with three articles, each restarting its numbering
    c.source("SRC-E", 10, klass="journal_issue")
    for page, title in [(1, "Первая статья"), (4, "Вторая статья"), (8, "Третья статья")]:
        c.block("SRC-E", page, "TITLE", title)
        c.block("SRC-E", page, "HEADING", "1. Введение")
    return c


def rows_of(tables, sid):
    return [r for r in tables["sections"].to_pylist() if r["source_id"] == sid]


# ------------------------------------------------------------------------------------------ priority & tree
def test_method_priority_outline_toc_numbering_layout_whole():
    c = make_corpus()
    with_outline = S.build(c.con, outlines={"SRC-A": BOOK_OUTLINE})
    without = S.build(c.con)
    assert {r["method"] for r in rows_of(with_outline, "SRC-A")} == {"PDF_OUTLINE"}
    assert {r["method"] for r in rows_of(without, "SRC-A")} == {"PRINTED_TOC"}
    assert {r["method"] for r in rows_of(without, "SRC-B")} == {"HEADING_NUMBERING"}
    assert {r["method"] for r in rows_of(without, "SRC-C")} == {"HEADING_LAYOUT"}
    assert {r["method"] for r in rows_of(without, "SRC-D")} == {"WHOLE_SOURCE"}
    d = rows_of(without, "SRC-D")[0]
    assert (d["title"], d["page_start_index"], d["page_end_index"], d["work_id"]) == ("Короткая заметка", 1, 5,
                                                                                     "W-SRC-D")


@pytest.mark.parametrize("outlines", [None, {"SRC-A": BOOK_OUTLINE}])
def test_book_tree_levels_and_ranges(outlines):
    tables = S.build(duckdb_con := make_corpus().con, outlines=outlines)
    got = [(r["title"], r["level"], r["page_start_index"], r["page_end_index"]) for r in rows_of(tables, "SRC-A")]
    assert got == EXPECTED_BOOK
    rows = rows_of(tables, "SRC-A")
    by_id = {r["section_id"]: r for r in rows}
    assert by_id[rows[2]["parent_section_id"]]["title"] == "Основы"
    assert rows[2]["title_path"] == "Глава 1 Основы › 1.1 Первый раздел с длинным заглавием"
    assert rows[1]["numbering"] == "Глава 1" and rows[2]["numbering"] == "1.1"
    assert all(r["heading_block_id"] for r in rows)          # every heading found on its start page
    assert all(r["page_start_id"] == f"SRC-A:p{r['page_start_index']:04d}" for r in rows)
    assert min(r["confidence"] for r in rows) >= 0.8          # confirmed by the other methods
    duckdb_con.close()


def test_invariants_nesting_non_overlap_and_section_pages():
    tables = S.build(make_corpus().con)
    checks = S.check(tables["sections"], tables["section_pages"])
    for bad in ("duplicate_ids", "empty_range", "missing_parent", "child_outside_parent", "sibling_overlap",
                "level_not_parent_plus_one", "root_not_level_1", "section_pages_orphans"):
        assert checks.get(bad, 0) == 0, (bad, checks)
    sp = tables["section_pages"].to_pylist()
    a = {r["page_index"]: r["section_id"] for r in sp if r["source_id"] == "SRC-A"}
    title = {r["section_id"]: r["title"] for r in tables["sections"].to_pylist()}
    assert set(a) == set(range(5, 41))                        # front matter before the first section is uncovered
    assert title[a[8]] == "Первый раздел с длинным заглавием" and title[a[19]] == "Методы"


def test_check_detects_overlap_and_child_outside_parent():
    rows = [dict(section_id="S1", source_id="X", parent_section_id=None, level=1, ordinal=1, page_start_index=1,
                 page_end_index=10),
            dict(section_id="S2", source_id="X", parent_section_id=None, level=1, ordinal=2, page_start_index=5,
                 page_end_index=12),
            dict(section_id="S3", source_id="X", parent_section_id="S1", level=2, ordinal=3, page_start_index=9,
                 page_end_index=11)]
    out = S.check(pa.Table.from_pylist(rows))
    assert out["sibling_overlap"] == 1 and out["child_outside_parent"] == 1


def test_container_articles_are_level_one_with_their_headings():
    rows = rows_of(S.build(make_corpus().con), "SRC-E")
    tops = [(r["title"], r["page_start_index"], r["page_end_index"]) for r in rows if r["level"] == 1]
    assert tops == [("Первая статья", 1, 3), ("Вторая статья", 4, 7), ("Третья статья", 8, 10)]
    assert sum(1 for r in rows if r["level"] == 2 and r["title"] == "Введение") == 3


def test_article_title_is_merged_and_noise_heading_dropped():
    rows = rows_of(S.build(make_corpus().con), "SRC-C")
    assert [(r["level"], r["title"]) for r in rows] == [(1, "Оседание земной поверхности над выработками"),
                                                        (2, "Методика наблюдений"), (2, "Результаты")]
    assert rows[0]["page_end_index"] == 6


def test_ids_are_stable_and_follow_the_shared_rule():
    t1 = S.build(make_corpus().con)["sections"].to_pylist()
    t2 = S.build(make_corpus().con)["sections"].to_pylist()
    assert [r["section_id"] for r in t1] == [r["section_id"] for r in t2]
    for r in t1:
        assert r["section_id"] == section_id(r["source_id"], r["method"], r["level"], r["ordinal"], r["title"])
        assert r["rule_version"] == "sections_v1"


# ------------------------------------------------------------------------------------------ printed TOC
def test_printed_toc_maps_labels_offset_and_heading_offset():
    lm = S.label_map([(1, ["i"], "CONSISTENT_SEQUENCE"), (2, ["ii"], "CONSISTENT_SEQUENCE")]
                     + [(p, [str(p - 2)], "CONSISTENT_SEQUENCE") for p in range(3, 20)])
    assert lm.offset == 2 and lm.direct["ii"] == 2 and lm.direct["5"] == 7
    st: dict = {}
    from collections import Counter
    cnt: Counter = Counter()
    ents = S.toc_candidates(["Preface ..... ii", "1 Intro ..... 1", "2 Theory ..... 5"], lm, 19, [], cnt)
    assert [(e.title, e.page, e.page_origin) for e in ents] == [("Preface", 2, "LABEL"), ("Intro", 3, "LABEL"),
                                                                ("Theory", 7, "LABEL")]
    # no labels: the dominant offset of TOC titles found as headings
    none = S.label_map([(p, [], "NONE") for p in range(1, 20)])
    heads = [(4, S.match_key(None, "Intro"), "b1"), (9, S.match_key(None, "Theory"), "b2")]
    ents = S.toc_candidates(["1 Intro ..... 1", "2 Theory ..... 6", "3 Results ..... 10"], none, 19, heads, cnt)
    assert [(e.page, e.page_origin) for e in ents] == [(4, "HEADING_OFFSET"), (9, "HEADING_OFFSET"),
                                                       (13, "HEADING_OFFSET")]
    assert st == {}


def test_unmappable_printed_page_is_dropped_not_guessed():
    from collections import Counter
    lm = S.label_map([(p, [str(p - 2)], "CONSISTENT_SEQUENCE") for p in range(3, 20)])
    cnt: Counter = Counter()
    ents = S.toc_candidates(["Preface ..... xvii", "Глава 1. Начало", "1.1 Intro ..... 1", "1.2 Theory ..... 5"],
                            lm, 19, [], cnt)
    assert [(e.title, e.page, e.page_origin) for e in ents] == [("Начало", 3, "NEXT_ENTRY"), ("Intro", 3, "LABEL"),
                                                                ("Theory", 7, "LABEL")]
    assert cnt["toc_entries_unmapped_page"] == 1


def test_numbered_title_block_is_not_merged_into_the_title():
    ents = S.layout_candidates([S.Block("t1", 1, 1, "TITLE", "Оседания над рудником"),
                                S.Block("t2", 1, 2, "TITLE", "II. Метод устойчивых отражателей")], [])
    assert [(e.title, e.numbering) for e in ents] == [("Оседания над рудником", None),
                                                      ("Метод устойчивых отражателей", "II")]


def test_toc_entry_moves_to_its_heading_within_two_pages():
    c = Canon()
    c.source("SRC-T", 30, offset=0)
    c.block("SRC-T", 2, "TABLE_OF_CONTENTS", "1. Alpha .... 5\n2. Beta .... 12\n3. Gamma .... 20")
    c.block("SRC-T", 5, "HEADING", "1. Alpha")
    c.block("SRC-T", 13, "HEADING", "2. Beta")       # the printed TOC is off by one here
    c.block("SRC-T", 20, "HEADING", "3. Gamma")
    stats: dict = {}
    rows = rows_of(S.build(c.con, stats=stats), "SRC-T")
    assert [(r["title"], r["page_start_index"]) for r in rows] == [("Alpha", 5), ("Beta", 13), ("Gamma", 20)]
    assert stats["counters"]["toc_page_shifted_to_heading"] == 1


def test_parse_toc_lines_wrapping_numbering_bare_pages_bullets():
    got = S.parse_toc(["CONTENTS", "1.1\t", "Water  1", "2.2. Складчатые дислокации: их",
                       "взаимоотношения", "19", "CHAPTER 3.", "METHODS .......... 29",
                       "Notation  24  •  Problems  24", "Глава 1", "Основы ... 3"])
    assert got == [("1.1 Water", "1"), ("2.2. Складчатые дислокации: их взаимоотношения", "19"),
                   ("CHAPTER 3 METHODS", "29"), ("Notation", "24"), ("Problems", "24"), ("Глава 1 Основы", "3")]


def test_parse_toc_leader_then_page_on_the_next_line():
    got = S.parse_toc(["1.2. Варианты схемы", "работ ..........", "12", "1.3. Анализ ....... 14"])
    assert got == [("1.2. Варианты схемы работ", "12"), ("1.3. Анализ", "14")]


def test_parse_toc_column_layout_numbers_in_their_own_block():
    titles = "Введение …………\n1 Первая глава …………\n1.1 Раздел …………\n2 Вторая глава ……\nЛитература ……"
    got = S.parse_toc([titles, "4", "6\n6", "14\n19"])
    assert got == [("Введение", "4"), ("1 Первая глава", "6"), ("1.1 Раздел", "6"), ("2 Вторая глава", "14"),
                   ("Литература", "19")]


# ------------------------------------------------------------------------------------------ numbering
@pytest.mark.parametrize("text,numbering,rank,rest", [
    ("Глава 3. Методы", "Глава 3", 1, "Методы"),
    ("ГЛАВА III Методы", "ГЛАВА III", 1, "Методы"),
    ("3.2.1 Определение модуля", "3.2.1", 3, "Определение модуля"),
    ("1.1. Краткая характеристика", "1.1", 2, "Краткая характеристика"),
    ("§ 5. Уравнения", "§ 5", 2, "Уравнения"),
    ("Chapter 3 Theory", "Chapter 3", 1, "Theory"),
    ("ЧАСТЬ II. Практика", "ЧАСТЬ II", 0, "Практика"),
    ("I. Introduction", "I", 1, "Introduction"),
    ("2.Содержание", "2", 1, "Содержание"),
])
def test_parse_numbering(text, numbering, rank, rest):
    num, got_rest = S.parse_numbering(text)
    assert (num.text, num.rank, got_rest) == (numbering, rank, rest)


@pytest.mark.parametrize("text", ["1998 год", "Рис. 3.1 Схема", "Таблица 2", "12,5 МПа"])
def test_parse_numbering_rejects_non_headings(text):
    assert S.parse_numbering(text)[0] is None


def test_levels_from_numbering_parts_chapters_sections_and_keywords():
    ents = [S.make_entry(t, p, i) for i, (t, p) in enumerate([
        ("Введение", 1), ("Часть I. Теория", 2), ("Глава 1. Основы", 2), ("1.1 Термины", 3), ("1.1.1 Детали", 3),
        ("Глава 2. Модели", 5), ("Часть II. Практика", 8), ("Глава 3. Опыт", 8), ("Заключение", 10)])]
    S.assign_ranks(ents)
    S.build_tree(ents, 12)
    assert [e.level for e in ents] == [1, 1, 2, 3, 4, 2, 1, 2, 1]
    assert [(e.page, e.end) for e in ents] == [(1, 1), (2, 7), (2, 4), (3, 4), (3, 4), (5, 7), (8, 9), (8, 9),
                                              (10, 12)]


def test_heading_numbering_keeps_each_family_in_order():
    heads = [S.Block(f"b{i}", p, i, "HEADING", t) for i, (p, t) in enumerate([
        (1, "Глава 1. Основы"), (2, "§ 1. Первый"), (3, "§ 2. Второй"), (4, "Глава 2. Модели"), (5, "§ 3. Третий"),
        (6, "§ 1. Ссылка на параграф из списка"), (7, "§ 4. Четвёртый")])]
    from collections import Counter
    ents = S.numbering_candidates(heads, Counter())
    assert [e.numbering for e in ents] == ["Глава 1", "§ 1", "§ 2", "Глава 2", "§ 3", "§ 4"]
    S.build_tree(ents, 8)
    assert [e.level for e in ents] == [1, 2, 2, 1, 2, 2]


def test_lnds():
    assert S.lnds([1, 5, 2, 3, 3, 9, 4]) == [0, 2, 3, 4, 6]
    assert S.lnds([]) == []


# ------------------------------------------------------------------------------------------ junk outlines
@pytest.mark.parametrize("items,pages,reason", [
    ([{"level": 1, "title": f"Scan {i}", "page_index": i} for i in range(1, 31)], 30, "ONE_PER_PAGE"),
    ([{"level": 1, "title": f"Line {i}", "page_index": 1} for i in range(20)], 8, "COLLAPSED_DESTINATIONS"),
    ([{"level": 1, "title": f"Row {i}", "page_index": 1 + i % 5} for i in range(40)], 5, "MORE_ENTRIES_THAN_3X_PAGES"),
    ([{"level": 1, "title": f"{i}.pdf", "page_index": 1 + 10 * i} for i in range(12)], 130, "JUNK_TITLES"),
    ([{"level": 1, "title": f"стр{i:03d}", "page_index": i + 1} for i in range(30)], 30, "JUNK_TITLES"),
    ([{"level": 1, "title": "Only one", "page_index": 1}], 9, "TOO_FEW_ENTRIES"),
    ([], 9, "EMPTY"),
])
def test_junk_outline_filter(items, pages, reason):
    assert S.filter_outline(items, pages) == ([], reason)


def test_valid_outline_is_cleaned_and_out_of_range_entries_dropped():
    ents, reason = S.filter_outline([{"level": 1, "title": "﻿ВВЕДЕНИЕ\x00\x00", "page_index": 3},
                                     {"level": 2, "title": "1.1 Раздел", "page_index": 4},
                                     {"level": 1, "title": "Лишнее", "page_index": 99},
                                     {"level": 1, "title": "Без цели", "page_index": None}], 10)
    assert reason is None
    assert [(e.rank, e.title, e.numbering, e.page) for e in ents] == [(1, "ВВЕДЕНИЕ", None, 3),
                                                                      (2, "Раздел", "1.1", 4)]


def test_junk_outline_falls_back_to_the_printed_toc():
    c = make_corpus()
    junk = [{"level": 1, "title": f"стр{i:03d}", "page_index": i} for i in range(1, 41)]
    stats: dict = {}
    rows = rows_of(S.build(c.con, outlines={"SRC-A": junk}, stats=stats), "SRC-A")
    assert {r["method"] for r in rows} == {"PRINTED_TOC"}
    assert stats["counters"]["outline_rejected_junk_titles"] == 1


def test_epub_and_djvu_outline_methods_by_document_format():
    c = Canon()
    c.source("SRC-EP", 6, fmt="EPUB")
    c.source("SRC-DJ", 6, fmt="DJVU")
    ol = [{"level": 1, "title": "One", "page_index": 1}, {"level": 1, "title": "Two", "page_index": 4}]
    t = S.build(c.con, outlines={"SRC-EP": ol, "SRC-DJ": ol})
    assert {r["method"] for r in rows_of(t, "SRC-EP")} == {"EPUB_NAV"}
    assert {r["method"] for r in rows_of(t, "SRC-DJ")} == {"DJVU_OUTLINE"}


# ------------------------------------------------------------------------------------------ queries
def test_queries_outline_section_and_page():
    c = make_corpus()
    c.con.execute("INSERT INTO canonical.figures VALUES ('SRC-A:p0008:f1', 'SRC-A', 'SRC-A:p0008')")
    c.con.execute("INSERT INTO canonical.formulas VALUES ('SRC-A:p0014:q1', 'SRC-A', 'SRC-A:p0014')")
    c.con.execute("INSERT INTO canonical.formulas VALUES ('SRC-A:p0030:q1', 'SRC-A', 'SRC-A:p0030')")
    tables = S.build(c.con)
    c.con.register("nav_sections", tables["sections"])
    c.con.register("nav_section_pages", tables["section_pages"])
    outline = Q.get_outline(c.con, "SRC-A")
    assert outline["n_sections"] == 8 and outline["methods"] == ["PRINTED_TOC"]
    assert [s["title"] for s in outline["sections"]] == ["Введение", "Основы", "Методы", "Заключение",
                                                         "Список литературы"]
    assert [s["title"] for s in outline["sections"][1]["children"]] == ["Первый раздел с длинным заглавием",
                                                                       "Второй раздел"]
    assert len(Q.get_outline(c.con, "SRC-A", max_level=1)["sections"][1]["children"]) == 0
    ch1 = outline["sections"][1]["section_id"]
    sec = Q.get_section(c.con, ch1)
    assert sec["counts"] == {"figures": 1, "tables": 0, "formulas": 1, "bibliography_entries": 0}
    assert [p["title"] for p in sec["path"]] == ["Основы"] and len(sec["children"]) == 2
    assert sec["pages"]["n_pages"] == 12 and sec["pages"]["page_ids"][0] == "SRC-A:p0007"
    sub = Q.get_section(c.con, sec["children"][1]["section_id"])
    assert [p["title"] for p in sub["path"]] == ["Основы", "Второй раздел"] and sub["parent"]["section_id"] == ch1
    page = Q.section_of_page(c.con, "SRC-A:p0014")
    assert page["sections"][0]["title"] == "Второй раздел"
    assert [p["numbering"] for p in page["path"]] == ["Глава 1", "1.2"]
    assert Q.section_of_page(c.con, "SRC-A:p0003") is None
    assert Q.get_section(c.con, "SEC-missing") is None


# ------------------------------------------------------------------------------------------ CLI
def test_nav_group_is_registered():
    from vkm_corpus import cli

    assert cli.GROUPS["nav"] == "vkm_corpus.navigation.cli"
    args = cli.build_parser(["nav", "build", "--duckdb", "x", "--out", "y"]).parse_args(
        ["nav", "build", "--duckdb", "x", "--out", "y"])
    assert args.part == "all"


def test_cli_build_writes_parquet_manifest_and_attach_reads_it(tmp_path, monkeypatch):
    db = tmp_path / "canon.duckdb"
    c = make_corpus(str(db))
    c.con.close()
    ol = tmp_path / "outlines.json"
    ol.write_text(json.dumps({"format": O.FORMAT, "outlines": {"SRC-A": BOOK_OUTLINE}}), encoding="utf-8")
    out = tmp_path / "nav"
    from vkm_corpus import cli

    assert cli.main(["nav", "build", "--duckdb", str(db), "--out", str(out), "--outlines", str(ol),
                     "--part", "sections"]) == 0
    manifest = json.loads((out / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["snapshot"]["snapshot_id"] == "snap-test"
    assert manifest["parts"]["sections"]["status"] == "BUILT"
    assert manifest["parts"]["sections"]["rule_version"] == "sections_v1"
    assert manifest["outlines"]["n_sources"] == 1
    stats = manifest["parts"]["sections"]["stats"]
    assert stats["sources_by_method"]["PDF_OUTLINE"] == 1 and stats["checks"].get("sibling_overlap", 0) == 0
    for name in ("sections", "section_pages"):
        meta = manifest["datasets"][name]
        assert pq.read_table(out / meta["path"]).num_rows == meta["rows"] > 0
        assert len(meta["sha256"]) == 64
    # a part whose module is absent is skipped and recorded
    monkeypatch.setitem(nav_cli.PARTS, "ghost", "vkm_corpus.navigation.no_such_module:build")
    con = duckdb.connect(str(db), read_only=True)
    try:
        m2 = nav_cli.build_parts(con, out, ["ghost"])
        assert m2["parts"]["ghost"]["status"] == "SKIPPED_MODULE_MISSING"
        assert m2["parts"]["sections"]["status"] == "BUILT"           # merged with the earlier manifest
        Q.attach(con, out)
        assert Q.get_outline(con, "SRC-A")["methods"] == ["PDF_OUTLINE"]
    finally:
        con.close()


# ------------------------------------------------------------------------------------------ native outlines
def _epub(path: Path) -> None:
    container = ('<?xml version="1.0"?><container xmlns="urn:oasis:names:tc:opendocument:xmlns:container">'
                 '<rootfiles><rootfile full-path="OEBPS/content.opf"/></rootfiles></container>')
    opf = ('<?xml version="1.0"?><package xmlns="http://www.idpf.org/2007/opf" version="3.0"><manifest>'
           '<item id="nav" href="nav.xhtml" properties="nav" media-type="application/xhtml+xml"/>'
           '<item id="c1" href="text/ch1.xhtml" media-type="application/xhtml+xml"/>'
           '<item id="c2" href="text/ch2.xhtml" media-type="application/xhtml+xml"/>'
           '</manifest><spine><itemref idref="c1"/><itemref idref="c2"/></spine></package>')
    nav = ('<?xml version="1.0"?><html xmlns="http://www.w3.org/1999/xhtml" xmlns:epub="http://www.idpf.org/2007/ops">'
           '<body><nav epub:type="toc"><ol><li><a href="text/ch1.xhtml">Chapter 1</a><ol>'
           '<li><a href="text/ch2.xhtml#s1">Section 1.1</a></li></ol></li>'
           '<li><span>Unlinked part</span></li></ol></nav></body></html>')
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("mimetype", "application/epub+zip")
        z.writestr("META-INF/container.xml", container)
        z.writestr("OEBPS/content.opf", opf)
        z.writestr("OEBPS/nav.xhtml", nav)
        z.writestr("OEBPS/text/ch1.xhtml", "<html/>")
        z.writestr("OEBPS/text/ch2.xhtml", "<html/>")


def test_epub_outline_resolves_spine_positions(tmp_path):
    pytest.importorskip("lxml")
    p = tmp_path / "book.epub"
    _epub(p)
    entries, n = O.epub_outline(p)
    assert n == 2
    assert entries == [{"level": 1, "title": "Chapter 1", "page_index": 1},
                       {"level": 2, "title": "Section 1.1", "page_index": 2},
                       {"level": 1, "title": "Unlinked part", "page_index": None}]


def test_djvu_outline_pages_and_component_names(monkeypatch, tmp_path):
    listing = b"     I    11768  shared.djbz\n   1 P     9304  a_0001.djvu\n   2 P    10201  b 0002.djvu\n" \
              b"   3 P    10201  c_0003.djvu\n"
    outline = (b'(bookmarks\n ("\\320\\223\\320\\273\\320\\260\\320\\262\\320\\260 1" "#1"\n'
               b'  ("Sub" "#b 0002.djvu" ))\n ("Two" "#3" ))\n')
    monkeypatch.setattr(O, "_djvused", lambda path, command, timeout: listing if command == "ls" else outline)
    entries, n = O.djvu_outline(tmp_path / "x.djvu")
    assert n == 3
    assert entries == [{"level": 1, "title": "Глава 1", "page_index": 1},
                       {"level": 2, "title": "Sub", "page_index": 2},
                       {"level": 1, "title": "Two", "page_index": 3}]


def test_load_outlines_accepts_wrapped_and_bare(tmp_path):
    bare = tmp_path / "bare.json"
    bare.write_text(json.dumps({"S": [{"level": 1, "title": "T", "page_index": 1}]}), encoding="utf-8")
    wrapped = tmp_path / "wrapped.json"
    wrapped.write_text(json.dumps({"format": O.FORMAT, "outlines": {"S": []}, "sources": {}}), encoding="utf-8")
    assert O.load_outlines(bare) == {"S": [{"level": 1, "title": "T", "page_index": 1}]}
    assert O.load_outlines(wrapped) == {"S": []}
