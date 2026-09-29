"""Synthetic NAV datasets and an in-memory Neo4j stand-in for offline tests of the NAV graph (no corpus text).

* :func:`write_synthetic_nav` writes ``<dir>/<dataset>.parquet`` with the real schemas of the navigation modules and a
  ``manifest.json`` like ``vkm-corpus nav build``: two sources (``VKM-SRC-101`` with a chapter tree, ``102`` as one
  section), formulas with where-clauses, a formula reference chain, a parameter candidate, ten terms with
  co-occurrence / containment / translation / definition edges, (optionally) three topics in the column layout
  the projection expects from agent T, and (optionally, ``extras``) two structured tables with columns and cells,
  five parameter candidates of ``parameters_v2`` (table, text, near a formula, one without a section), five pairs of
  the term dictionary (four dictionary-only terms, one pair of one term id with itself) and three object duplicate
  groups (figures, tables, formulas; one without a primary).
* :class:`FakeNavNeo4j` answers the statements of :mod:`vkm_corpus.graph.nav` and :mod:`vkm_corpus.graph.nav_query`
  by their ``// vkm-nav:<op>`` tag over dicts (nodes with label sets, relationships per type) — enough to run the
  loader, the checks N1–N11 and the query shaping end to end without a server. DOCUMENT nodes are added with
  :func:`add_document_graph` (only the nodes the NAV rows reference, plus page/object edges).
"""
from __future__ import annotations

import hashlib
import json
from collections import defaultdict, deque
from pathlib import Path
from typing import Any

from vkm_corpus.graph import nav_schema as N
from vkm_corpus.graph import schema as S
from vkm_corpus.graph.nav_query import NODE_KEYS, REL_KEYS
from vkm_corpus.navigation import ids as nav_ids

SNAPSHOT = "snap-20260928T000000Z-0000abcd"
S1, S2 = "VKM-SRC-101", "VKM-SRC-102"


def pid(source: str, n: int) -> str:
    return f"{source}:p{n:04d}"


def oid(page: str, kind: str, n: int) -> str:
    return f"{page}:{kind}{n:012x}"


F1, F2, F3 = oid(pid(S1, 2), "m", 1), oid(pid(S1, 3), "m", 2), oid(pid(S1, 5), "m", 3)
F4, F5 = oid(pid(S2, 2), "m", 4), oid(pid(S2, 3), "m", 5)
B_DEF1, B_DEF2, B_DEF3 = oid(pid(S1, 2), "b", 0x21), oid(pid(S1, 3), "b", 0x31), oid(pid(S1, 5), "b", 0x51)
B_DEF4, B_REF, B_TERMDEF = oid(pid(S2, 2), "b", 0x61), oid(pid(S1, 4), "b", 0x41), oid(pid(S1, 3), "b", 0x32)
T1, T2 = oid(pid(S1, 3), "t", 0x71), oid(pid(S2, 2), "t", 0x72)            # canonical tables (structured grids)
FIG1, FIG2 = oid(pid(S1, 5), "f", 0x81), oid(pid(S2, 1), "f", 0x82)        # one figure printed in both sources

TERMS: dict[str, dict[str, Any]] = {   # lemma_key → row fields
    "ползучесть": {"lemma": "ползучесть", "surface_forms": ["ползучесть", "ползучести"], "seed": True, "df": 6},
    "скорость ползучесть": {"lemma": "скорость ползучести", "surface_forms": ["скорость ползучести"], "df": 4},
    "ползучесть соль": {"lemma": "ползучесть соли", "surface_forms": ["ползучесть соли", "ползучести соли"], "df": 3},
    "конвергенция": {"lemma": "конвергенция", "surface_forms": ["конвергенция", "конвергенции"], "seed": True,
                     "df": 5},
    "оседание": {"lemma": "оседание", "surface_forms": ["оседание", "оседания"], "seed": True, "df": 7},
    "напряжение": {"lemma": "напряжение", "surface_forms": ["напряжение", "напряжения"], "df": 8},
    "creep": {"lemma": "creep", "surface_forms": ["creep"], "language": "en", "df": 3},
    "выработка": {"lemma": "выработка", "surface_forms": ["выработка", "выработки"], "df": 5},
    # the terms of two properties of the parameters vocabulary (label_ru), for TABULATES / VALUE_OF
    "плотность": {"lemma": "плотность", "surface_forms": ["плотность", "плотности"], "df": 4},
    "модуль деформация": {"lemma": "модуль деформации", "surface_forms": ["модуль деформации"], "df": 3},
}
# pairs of the term dictionary: (relation, (lang, key, lemma) of a, (lang, key, lemma) of b, score, status, methods)
DICTIONARY: tuple[tuple[str, tuple[str, str, str], tuple[str, str, str], float, str, tuple[str, ...]], ...] = (
    ("TRANSLATION", ("ru", "ползучесть", "ползучесть"), ("en", "creep", "creep"), 1.0, "REVIEWED_BY_AGENT",
     ("CURATED_SEED", "KEYWORD_LISTS")),
    ("TRANSLATION", ("ru", "оседание", "оседание"), ("en", "subsidence", "subsidence"), 0.92,
     "AUTO_EXTRACTED_UNREVIEWED", ("KEYWORD_LISTS",)),
    ("ABBREVIATION", ("ru", "водозащитный толща", "водозащитная толща"), ("ru", "взт", "ВЗТ"), 0.9,
     "AUTO_EXTRACTED_UNREVIEWED", ("SAME_AS_ABBREVIATION",)),
    ("SYNONYM", ("ru", "выработка", "выработка"), ("ru", "горный выработка", "горная выработка"), 0.7,
     "AUTO_EXTRACTED_UNREVIEWED", ("SUBSTITUTION",)),
    ("TRANSLATION", ("ru", "gps", "GPS"), ("en", "gps", "GPS"), 0.95, "AUTO_EXTRACTED_UNREVIEWED",
     ("ORTHOGRAPHIC",)),                                   # one lemma key in two languages: one term id, skipped
)
DICTIONARY_ONLY = ("subsidence", "водозащитный толща", "взт", "горный выработка")


def tid(key: str) -> str:
    return nav_ids.term_id(key)


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _edge_id(kind: str, src: str, dst: str) -> str:
    return "TED-" + hashlib.sha256(f"vkm-nav-term-edge-v1|{kind}|{src}|{dst}".encode()).hexdigest()[:16]


def section_rows() -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, str]]:
    spec = [  # (key, source, parent key, level, ordinal, numbering, title, start, end, method)
        ("ch1", S1, None, 1, 1, "Глава 1", "Ползучесть", 1, 4, "PDF_OUTLINE"),
        ("s11", S1, "ch1", 2, 2, "1.1", "Скорость ползучести", 1, 2, "PDF_OUTLINE"),
        ("s12", S1, "ch1", 2, 3, "1.2", "Конвергенция выработок", 3, 4, "PDF_OUTLINE"),
        ("ch2", S1, None, 1, 4, "Глава 2", "Оседание", 5, 6, "PDF_OUTLINE"),
        ("w2", S2, None, 1, 1, None, "Весь источник", 1, 3, "WHOLE_SOURCE"),
    ]
    ids = {k: nav_ids.section_id(src, method, level, ordinal, title) for k, src, _p, level, ordinal, _n, title, _s, _e,
           method in spec}
    titles = {s[0]: s[6] for s in spec}
    rows = []
    for k, src, parent, level, ordinal, numbering, title, start, end, method in spec:
        path = title if parent is None else f"{titles[parent]} › {title}"
        rows.append({"section_id": ids[k], "source_id": src, "work_id": None, "parent_section_id": ids.get(parent),
                     "level": level, "ordinal": ordinal, "numbering": numbering, "title": title, "title_path": path,
                     "page_start_id": pid(src, start), "page_end_id": pid(src, end), "page_start_index": start,
                     "page_end_index": end, "method": method, "confidence": 0.85, "heading_block_id": None,
                     "rule_version": "sections_v1"})
    deepest = {pid(S1, 1): "s11", pid(S1, 2): "s11", pid(S1, 3): "s12", pid(S1, 4): "s12", pid(S1, 5): "ch2",
               pid(S1, 6): "ch2", pid(S2, 1): "w2", pid(S2, 2): "w2", pid(S2, 3): "w2"}
    pages = [{"section_id": ids[k], "page_id": p, "source_id": p.split(":")[0], "page_index": int(p[-4:]),
              "rule_version": "sections_v1"} for p, k in deepest.items()]
    return rows, pages, ids


def formula_rows(sec: dict[str, str]) -> dict[str, list[dict[str, Any]]]:
    rv = {"rule_version": "formula_context_v1", "review_status": "AUTO_EXTRACTED_UNREVIEWED"}
    ctx = []
    for f, src, page, section, number in ((F1, S1, 2, "s11", "1.1"), (F2, S1, 3, "s12", "1.2"),
                                          (F3, S1, 5, "ch2", "2.1"), (F4, S2, 2, "w2", None), (F5, S2, 3, None, None)):
        ctx.append({"formula_id": f, "source_id": src, "page_id": pid(src, page), "page_index": page,
                    "kind": "DISPLAY", "latex_len": 20, "equation_number": number, "number_method": "TAG" if number
                    else None, "extra_numbers": [], "section_id": sec.get(section) if section else None,
                    "where_block_ids": [], "n_symbols": 2, "n_defined_symbols": 1, "n_refs_in": 0, "n_parameters": 0,
                    **rv})

    def sym(f: str, src: str, symbol: str, definition: str | None, unit: str | None = None,
            block: str | None = None) -> dict[str, Any]:
        return {"formula_id": f, "source_id": src, "symbol": symbol, "symbol_key": symbol,
                "symbol_id": nav_ids.symbol_id(src, symbol), "role": "RHS", "n_occurrences": 1, "in_formula": True,
                "definition": definition, "unit": unit, "definition_block_id": block if definition else None,
                "definition_symbol_raw": symbol if definition else None,
                "definition_key": nav_ids.norm_text(definition) if definition else None,
                "match_method": "EXACT" if definition else None, **rv}

    symbols = [sym(F1, S1, "σ", "напряжение", "МПа", B_DEF1), sym(F1, S1, "t", None),
               sym(F2, S1, "ε̇", "скорость ползучести", "1/сут", B_DEF2), sym(F2, S1, "σ", "напряжение", "МПа", B_DEF2),
               sym(F3, S1, "ε̇", "скорость ползучести", "1/сут", B_DEF3), sym(F3, S1, "η", None),
               sym(F4, S2, "w", "оседание поверхности", "мм", B_DEF4)]
    refs = [{"ref_id": nav_ids.formula_ref_id(B_DEF3, F2), "block_id": B_DEF3, "source_id": S1, "page_id": pid(S1, 5),
             "formula_id": F2, "number_text": "(1.2)", "number_key": "1.2", "ref_type": "SUBSTITUTION", "cue": "подставляя",
             "resolution": "UNIQUE", "n_candidates": 1, "n_mentions": 1, "char_start": 10, "citing_formula_id": F3,
             **rv},
            {"ref_id": nav_ids.formula_ref_id(B_REF, F2), "block_id": B_REF, "source_id": S1, "page_id": pid(S1, 4),
             "formula_id": F2, "number_text": "(1.2)", "number_key": "1.2", "ref_type": "MENTION", "cue": "по формуле",
             "resolution": "UNIQUE", "n_candidates": 1, "n_mentions": 1, "char_start": 5, "citing_formula_id": None,
             **rv},
            {"ref_id": nav_ids.formula_ref_id(B_REF, "none"), "block_id": B_REF, "source_id": S1,
             "page_id": pid(S1, 4), "formula_id": None, "number_text": "(9.9)", "number_key": "9.9",
             "ref_type": "MENTION", "cue": "по формуле", "resolution": "NOT_FOUND", "n_candidates": 0,
             "n_mentions": 1, "char_start": 40, "citing_formula_id": None, **rv}]
    params = [{"parameter_id": nav_ids.parameter_id(F2, "n", "4,5"), "formula_id": F2, "source_id": S1, "symbol": "n",
               "symbol_raw": "n", "value_text": "4,5", "value": 4.5, "value_min": None, "value_max": None,
               "unit": None, "block_id": B_DEF2, "in_formula": False, "context_kind": "WHERE", **rv}]
    return {"formula_context": ctx, "formula_symbols": symbols, "formula_refs": refs, "formula_parameters": params}


def concept_rows(sec: dict[str, str]) -> dict[str, list[dict[str, Any]]]:
    terms = []
    for key, t in TERMS.items():
        terms.append({"term_id": tid(key), "lemma": t["lemma"], "lemma_key": key, "surface_forms": t["surface_forms"],
                      "language": t.get("language", "ru"), "n_words": len(key.split()), "kind": "NP",
                      "df_units": t["df"], "df_sources": 2, "tf": t["df"] * 3, "cvalue": 1.0, "idf": 1.0,
                      "seed": bool(t.get("seed")), "community": 1, "morphology": "pymorphy3",
                      "rule_version": "concepts_v1"})
    mentions = []

    def mention(key: str, section: str | None, tf: int, pages: list[str], unit: str, kind: str = "SECTION") -> None:
        mentions.append({"term_id": tid(key), "unit_id": unit, "unit_kind": kind,
                         "section_id": sec[section] if section else None, "source_id": pages[0].split(":")[0],
                         "tf": tf, "tfidf": float(tf), "page_ids": pages, "best_block_ids": [],
                         "rule_version": "concepts_v1"})

    mention("ползучесть", "s11", 3, [pid(S1, 1)], "NCU-0000000000000001")
    mention("ползучесть", "s12", 2, [pid(S1, 3)], "NCU-0000000000000002")
    mention("скорость ползучесть", "s11", 4, [pid(S1, 2)], "NCU-0000000000000001")
    mention("скорость ползучесть", "s12", 1, [pid(S1, 3)], "NCU-0000000000000002")
    mention("скорость ползучесть", "s12", 2, [pid(S1, 4)], "NCU-0000000000000003")       # a second window of 1.2
    mention("ползучесть соль", "s11", 2, [pid(S1, 1)], "NCU-0000000000000001")
    mention("конвергенция", "s12", 5, [pid(S1, 3), pid(S1, 4)], "NCU-0000000000000002")
    mention("оседание", "ch2", 6, [pid(S1, 5)], "NCU-0000000000000004")
    mention("оседание", "w2", 2, [pid(S2, 1)], "NCU-0000000000000005")
    mention("напряжение", "s11", 1, [pid(S1, 2)], "NCU-0000000000000001")
    mention("выработка", "s12", 2, [pid(S1, 4)], "NCU-0000000000000003")
    mention("creep", None, 1, [pid(S2, 3)], "NCU-0000000000000006", kind="HEADING_GROUP")
    edges = []

    def edge(kind: str, a: str, b: str | None, *, ref: str | None = None, weight: float = 1.0, n_units: int = 3,
             n_sources: int = 2, examples: list[str] | None = None, rule: str = "npmi_units") -> None:
        src, dst = tid(a), (tid(b) if b else None)
        if kind in ("CO_OCCURS", "SAME_AS") and dst and dst < src:
            src, dst = dst, src
        edges.append({"edge_id": _edge_id(kind, src, dst if dst is not None else ref), "kind": kind,
                      "src_term_id": src, "dst_term_id": dst, "dst_ref": ref, "weight": weight, "n_units": n_units,
                      "n_sources": n_sources, "examples": examples or [], "example_block_ids": [], "rule": rule,
                      "rule_version": "concepts_v1"})

    edge("CO_OCCURS", "ползучесть соль", "скорость ползучесть", weight=0.6, n_units=4, examples=[pid(S1, 1)])
    edge("CO_OCCURS", "скорость ползучесть", "конвергенция", weight=0.5, n_units=3, examples=[pid(S1, 3)])
    edge("CO_OCCURS", "конвергенция", "оседание", weight=0.45, n_units=5, examples=[pid(S1, 4), pid(S1, 5)])
    edge("CO_OCCURS", "ползучесть", "напряжение", weight=0.3, n_units=3, examples=[pid(S1, 2)])
    edge("CONTAINS", "ползучесть соль", "ползучесть", weight=0.5, rule="lexical_containment")
    edge("CONTAINS", "скорость ползучесть", "ползучесть", weight=0.6, rule="lexical_containment")
    edge("SAME_AS", "ползучесть", "creep", weight=2.0, examples=[pid(S2, 3)], rule="paren_translation")
    edge("SAME_AS", "ползучесть", None, ref="en:salt creep", weight=1.0, examples=[pid(S2, 3)],
         rule="paren_translation")
    edge("DEFINED_AS", "конвергенция", None, ref=B_TERMDEF, n_units=1, n_sources=1, examples=[pid(S1, 3)],
         rule="def_nazyvaetsya")
    return {"terms": terms, "term_mentions": mentions, "term_edges": edges}


TOPIC_IDS = ("TOP-0000000000000001", "TOP-0000000000000002", "TOP-0000000000000003")


def topic_rows(sec: dict[str, str]) -> dict[str, list[dict[str, Any]]]:
    """Agent T's layout (``vkm_corpus.navigation.topics``): level 1 = fine … coarse above; label terms, not labels."""
    t1, t2, t3 = TOPIC_IDS
    rv = {"rule_version": "topics_v1"}
    topics = [{"topic_id": t1, "level": 2, "parent_topic_id": None, "n_children": 1, "n_sections": 2, "n_sources": 1,
               "source_ids": [S1], "label_terms": ["реология", "соль"], "label_term_ids": [],
               "central_section_ids": [sec["s11"]], "coherence": 0.7, **rv},
              {"topic_id": t2, "level": 1, "parent_topic_id": t1, "n_children": 0, "n_sections": 2, "n_sources": 1,
               "source_ids": [S1], "label_terms": ["скорость ползучести"],
               "label_term_ids": [tid("скорость ползучесть")],
               "central_section_ids": [sec["s11"]], "coherence": 0.8, **rv},
              {"topic_id": t3, "level": 1, "parent_topic_id": None, "n_children": 0, "n_sections": 2, "n_sources": 2,
               "source_ids": [S1, S2], "label_terms": ["оседание"], "label_term_ids": [],
               "central_section_ids": [sec["ch2"]], "coherence": 0.6, **rv}]
    members = [{"topic_id": t2, "section_id": sec["s11"], "level": 1, "source_id": S1, "similarity": 0.81, "rank": 1,
                **rv},
               {"topic_id": t2, "section_id": sec["s12"], "level": 1, "source_id": S1, "similarity": 0.64, "rank": 2,
                **rv},
               {"topic_id": t3, "section_id": sec["ch2"], "level": 1, "source_id": S1, "similarity": 0.77, "rank": 1,
                **rv},
               {"topic_id": t3, "section_id": sec["w2"], "level": 1, "source_id": S2, "similarity": 0.52, "rank": 2,
                **rv}]
    tedges = [{"topic_id_a": t2, "topic_id_b": t3, "level": 1, "cosine": 0.42, "n_links": 3, **rv}]
    aggregates = [{"section_id": sec["s12"], "source_id": S1, "n_pages": 2, "n_units": 5, "n_text_units": 4,
                   "central_unit_ids": ["u1"], "central_page_ids": [pid(S1, 3)], "central_object_ids": [[B_DEF2]],
                   "key_terms": ["конвергенция", "скорость ползучести"],
                   "key_term_ids": [tid("конвергенция"), tid("скорость ползучесть")], "n_formulas": 1,
                   "n_figures": 0, "n_tables": 0, "n_bib_entries": 0, **rv}]
    return {"topics": topics, "topic_members": members, "topic_edges": tedges, "section_aggregates": aggregates}


def table_rows(sec: dict[str, str]) -> dict[str, list[dict[str, Any]]]:
    """Two structured tables (agent TB's schemas): T1 names two properties in value columns (and one in a value row),
    T2 names a property only in its caption, which is longer than the NavTable caption cut."""
    rv = {"rule_version": "tables_v1", "review_status": "AUTO_EXTRACTED_UNREVIEWED"}
    n1, n2 = nav_ids.table_id(T1), nav_ids.table_id(T2)
    structure = [
        {"table_id": T1, "nav_table_id": n1, "source_id": S1, "page_id": pid(S1, 3), "page_index": 3,
         "section_id": sec["s12"], "table_label": "Таблица 1.1", "table_number": "1.1", "caption": "Свойства пород",
         "n_rows": 4, "n_cols": 3, "n_cells": 12, "n_filled_cells": 12, "n_numeric_cells": 5, "n_text_cells": 7,
         "n_flagged_cells": 0, "n_spanning_cells": 0, "n_header_rows": 1, "header_method": "FIRST_NUMERIC_ROW",
         "n_bands": 1, "n_blocks": 1, "orientation": "NORMAL", "recognition_method": "OCR_GLM", "raw_format": "HTML",
         "origin": "OCR", "parse_method": "BANDS", "confidence": 0.8, "structure_ok": True, "covers_region": True,
         "quality_flags": [], "property_keys": ["deformation_modulus", "density"], "materials": ["каменная соль"],
         "caption_property_key": None, **rv},
        {"table_id": T2, "nav_table_id": n2, "source_id": S2, "page_id": pid(S2, 2), "page_index": 2,
         "section_id": sec["w2"], "table_label": None, "table_number": None,
         "caption": "Предел прочности на одноосное сжатие " + "образцов " * 40, "n_rows": 2, "n_cols": 2,
         "n_cells": 4, "n_filled_cells": 4, "n_numeric_cells": 2, "n_text_cells": 2, "n_flagged_cells": 0,
         "n_spanning_cells": 0, "n_header_rows": 1, "header_method": "FIRST_NUMERIC_ROW", "n_bands": 1, "n_blocks": 1,
         "orientation": "NORMAL", "recognition_method": "NATIVE_FIND_TABLES", "raw_format": "GRID", "origin": "NATIVE",
         "parse_method": "GRID", "confidence": 0.85, "structure_ok": True, "covers_region": False,
         "quality_flags": ["NO_HEADER"], "property_keys": ["ucs"], "materials": [], "caption_property_key": "ucs",
         **rv}]
    columns = [{"table_id": T1, "nav_table_id": n1, "block": 0, "col": 0, "role": "LABEL", "property_key": None, **rv},
               {"table_id": T1, "nav_table_id": n1, "block": 0, "col": 1, "role": "VALUE", "unit_raw": "ГПа",
                "property_key": "deformation_modulus", "property_label": "модуль деформации", **rv},
               {"table_id": T1, "nav_table_id": n1, "block": 0, "col": 2, "role": "VALUE", "unit_raw": "г/см3",
                "property_key": "density", "property_label": "плотность", **rv},
               {"table_id": T2, "nav_table_id": n2, "block": 0, "col": 0, "role": "LABEL", **rv},
               {"table_id": T2, "nav_table_id": n2, "block": 0, "col": 1, "role": "VALUE", **rv}]

    def cell(table: str, nav: str, row: int, col: int, role: str, text: str, **kw: Any) -> dict[str, Any]:
        return {"cell_id": nav_ids.table_cell_id(nav, row, col), "table_id": table, "nav_table_id": nav,
                "source_id": table.split(":")[0], "page_id": table.rsplit(":", 1)[0], "row": row, "col": col,
                "row_span": 1, "col_span": 1, "band": 0, "block": 0, "text": text, "text_clean": text,
                "is_header": role == "HEADER", "row_role": role, "flags": [], **kw, **rv}

    cells = [cell(T1, n1, 0, 0, "HEADER", "Порода"), cell(T1, n1, 0, 1, "HEADER", "E, ГПа"),
             cell(T1, n1, 0, 2, "HEADER", "ρ, г/см3"), cell(T1, n1, 1, 0, "DATA", "соль"),
             cell(T1, n1, 1, 1, "DATA", "12,5", value_type="NUMBER", value_min=12.5, value_max=12.5),
             cell(T1, n1, 2, 2, "DATA", "2,16", value_type="NUMBER", value_min=2.16, value_max=2.16),
             cell(T1, n1, 3, 0, "DATA", "плотность", row_property_key="density"),
             cell(T2, n2, 0, 0, "HEADER", "Образец"), cell(T2, n2, 1, 1, "DATA", "20", value_type="NUMBER")]
    return {"table_structure": structure, "table_columns": columns, "table_cells": cells}


PARAMETER_IDS: dict[str, str] = {}


def parameter_rows(sec: dict[str, str]) -> list[dict[str, Any]]:
    """Five candidates of parameters_v2: two read from T1, one from text, one next to formula F2 (a property without a
    term), one without a section."""
    rv = {"rule_version": "parameters_v2", "review_status": "AUTO_EXTRACTED_UNREVIEWED"}
    spec = [  # (name, method, anchor, locator, property, label, value, unit, page, section, extra)
        ("table_e", "TABLE", T1, "1:1", "deformation_modulus", "модуль деформации", "12,5", "ГПа", pid(S1, 3), "s12",
         {"table_id": T1, "table_row": 1, "table_col": 1, "material": "каменная соль"}),
        ("table_rho", "TABLE", T1, "2:2", "density", "плотность", "2,16", "г/см3", pid(S1, 3), "s12",
         {"table_id": T1, "table_row": 2, "table_col": 2, "material": "UNKNOWN"}),
        ("text_rho", "TEXT", B_DEF2, "5", "density", "плотность", "2,2", "г/см3", pid(S1, 3), "s12",
         {"block_id": B_DEF2, "char_start": 5, "char_end": 8, "material": "UNKNOWN"}),
        ("formula_ucs", "NEAR_FORMULA", B_DEF2, "40", "ucs", "предел прочности на одноосное сжатие", "4,5", "МПа",
         pid(S1, 3), "s12", {"block_id": B_DEF2, "formula_id": F2, "char_start": 40, "char_end": 43,
                             "material": "UNKNOWN"}),
        ("text_nosec", "TEXT", B_DEF4, "7", "density", "плотность", "2,1", "г/см3", pid(S2, 2), None,
         {"block_id": B_DEF4, "char_start": 7, "char_end": 10, "material": "UNKNOWN"}),
    ]
    rows = []
    for name, method, anchor, loc, prop, label, value, unit, page, section, extra in spec:
        cid = nav_ids.parameter_candidate_id(anchor, loc, prop, value)
        PARAMETER_IDS[name] = cid
        rows.append({"candidate_id": cid, "property_key": prop, "property_label": label, "property_group": "mechanics",
                     "symbol": None, "value_text": value, "unit_raw": unit, "scale_hint": "UNKNOWN",
                     "site_hint": "UNKNOWN", "method": method, "source_id": page.split(":")[0], "page_id": page,
                     "page_index": int(page[-4:]), "section_id": sec[section] if section else None,
                     "confidence": 0.7, "flags": [], "scale_cues": [], "source_site_scope": [], **extra, **rv})
    return rows


def _pair_id(relation: str, a: tuple[str, str, str], b: tuple[str, str, str]) -> str:
    """``term_dictionary.pair_id`` of (relation, (lang, key, …) of a, of b)."""
    return "TTR-" + hashlib.sha256(f"vkm-nav-term-pair-v1|{relation}|{a[0]}|{a[1]}|{b[0]}|{b[1]}".encode("utf-8")) \
        .hexdigest()[:16]


def dictionary_rows() -> list[dict[str, Any]]:
    """Pairs of agent TR's ``term_translations`` (schema ``term_dictionary.SCHEMA``)."""
    rows = []
    for relation, a, b, score, status, methods in DICTIONARY:
        rows.append({"pair_id": _pair_id(relation, a, b), "relation": relation, "term_id_a": tid(a[1]),
                     "term_id_b": tid(b[1]), "lemma_a": a[2], "lemma_b": b[2], "key_a": a[1], "key_b": b[1],
                     "lang_a": a[0], "lang_b": b[0], "in_terms_a": a[1] in TERMS, "in_terms_b": b[1] in TERMS,
                     "methods": list(methods),
                     "evidence": [{"method": methods[-1], "source_id": S2, "page_id": pid(S2, 3), "block_id": None,
                                   "other_page_id": None, "other_block_id": None, "detail": None, "score": score},
                                  {"method": methods[0], "source_id": S1, "page_id": pid(S1, 1), "block_id": None,
                                   "other_page_id": None, "other_block_id": None, "detail": None, "score": score}],
                     "n_sources": 2, "n_occurrences": 2, "cosine": None, "score": score, "status": status,
                     "rule_version": "term_translations_v1"})
    return rows


GROUP_IDS: dict[str, str] = {}


def object_dup_rows(sec: dict[str, str]) -> dict[str, list[dict[str, Any]]]:
    """Three groups of agent U2's layout: a figure reprinted (primary FIG1), a table reused (primary T1), a formula
    shared without a known year (no primary)."""
    rv = {"rule_version": "object_duplicates_v1"}
    spec = [  # (name, object type, kind, primary rule, members: (object id, page, section, is_primary))
        ("figure", "FIGURE", "REPRINT", "EARLIEST_YEAR", ((FIG1, pid(S1, 5), "ch2", True), (FIG2, pid(S2, 1), "w2",
                                                                                            False))),
        ("table", "TABLE", "REUSED_TABLE", "EARLIEST_YEAR", ((T1, pid(S1, 3), "s12", True), (T2, pid(S2, 2), "w2",
                                                                                            False))),
        ("formula", "FORMULA", "SHARED_FORMULA", "UNKNOWN_YEAR", ((F1, pid(S1, 2), "s11", False),
                                                                  (F4, pid(S2, 2), "w2", False))),
    ]
    clusters, members = [], []
    for name, object_type, kind, rule, ms in spec:
        cid = nav_ids.object_dup_cluster_id(object_type, [m[0] for m in ms])
        GROUP_IDS[name] = cid
        primary = next((m[0] for m in ms if m[3]), None)
        clusters.append({"cluster_id": cid, "object_type": object_type, "kind": kind, "match_basis": "IMAGE",
                         "n_members": len(ms), "n_sources": 2, "n_works": 2, "n_groups": 2, "source_ids": [S1, S2],
                         "work_ids": [], "primary_source_id": S1 if primary else None, "primary_object_id": primary,
                         "primary_work_id": None, "primary_year": 1990 if primary else None, "primary_rule": rule,
                         "reference_source_id": S1, "reference_object_id": ms[0][0],
                         "label": "1" if object_type == "FIGURE" else None, **rv})
        for i, (obj, page, section, is_primary) in enumerate(ms):
            members.append({"cluster_id": cid, "object_id": obj, "object_type": object_type, "kind": kind,
                            "source_id": obj.split(":")[0], "work_id": None, "year": 1990 if is_primary else 2001,
                            "page_id": page, "page_index": int(page[-4:]), "label": None,
                            "match": "REFERENCE" if i == 0 else "IMAGE", "similarity": 1.0 if i == 0 else 0.93,
                            "section_id": sec[section], "is_primary": is_primary, "is_reference": i == 0, **rv})
    return {"object_dup_clusters": clusters, "object_dup_members": members}


def write_synthetic_nav(nav_dir: str | Path, *, topics: bool = True, extras: bool = True,
                        snapshot_id: str = SNAPSHOT) -> dict[str, Any]:
    """Write the synthetic NAV build; returns ids of interest (sections, formulas, blocks, terms, topics, tables,
    parameter values, dictionary pairs, object duplicate groups)."""
    import pyarrow as pa
    import pyarrow.parquet as pq

    from vkm_corpus.navigation import concepts, formulas, object_duplicates, parameters, sections
    from vkm_corpus.navigation import tables as nav_tables
    from vkm_corpus.navigation import term_dictionary

    nav_dir = Path(nav_dir)
    nav_dir.mkdir(parents=True, exist_ok=True)
    secs, pages, sec_ids = section_rows()
    tables: dict[str, tuple[list[dict[str, Any]], Any, str]] = {
        "sections": (secs, sections.SECTIONS_SCHEMA, "sections"),
        "section_pages": (pages, sections.SECTION_PAGES_SCHEMA, "sections")}
    fr = formula_rows(sec_ids)
    for name, schema in (("formula_context", formulas.FORMULA_CONTEXT_SCHEMA()),
                         ("formula_symbols", formulas.FORMULA_SYMBOLS_SCHEMA()),
                         ("formula_refs", formulas.FORMULA_REFS_SCHEMA()),
                         ("formula_parameters", formulas.FORMULA_PARAMETERS_SCHEMA())):
        tables[name] = (fr[name], schema, "formulas")
    cr = concept_rows(sec_ids)
    for name, schema in (("terms", concepts.TERMS_SCHEMA), ("term_mentions", concepts.MENTIONS_SCHEMA),
                         ("term_edges", concepts.EDGES_SCHEMA)):
        tables[name] = (cr[name], schema, "concepts")
    if topics:
        for name, rows in topic_rows(sec_ids).items():
            tables[name] = (rows, None, "topics")
    if extras:
        tr = table_rows(sec_ids)
        for name, schema in (("table_structure", nav_tables.TABLE_STRUCTURE_SCHEMA()),
                             ("table_cells", nav_tables.TABLE_CELLS_SCHEMA()),
                             ("table_columns", nav_tables.TABLE_COLUMNS_SCHEMA())):
            tables[name] = (tr[name], schema, "tables")
        tables["parameter_candidates"] = (parameter_rows(sec_ids), parameters.PARAMETER_CANDIDATES_SCHEMA(),
                                          "parameters")
        tables["term_translations"] = (dictionary_rows(), term_dictionary.SCHEMA, "translations")
        od = object_dup_rows(sec_ids)
        tables["object_dup_clusters"] = (od["object_dup_clusters"], object_duplicates.CLUSTERS_SCHEMA,
                                         "object_duplicates")
        tables["object_dup_members"] = (od["object_dup_members"], object_duplicates.MEMBERS_SCHEMA,
                                        "object_duplicates")
    manifest: dict[str, Any] = {"format": "vkm-nav-manifest-v1", "layer_status": "DERIVED",
                                "review_status": "AUTO_EXTRACTED_UNREVIEWED", "built_at": "2026-09-28T00:00:00Z",
                                "snapshot": {"snapshot_id": snapshot_id, "manifest_sha256": "0" * 64,
                                             "pipeline_version": "0.1.0"},
                                "parts": {}, "datasets": {}}
    for name, (rows, schema, part) in tables.items():
        table = pa.Table.from_pylist(rows, schema=schema) if schema is not None else pa.Table.from_pylist(rows)
        path = nav_dir / f"{name}.parquet"
        pq.write_table(table, path)
        manifest["datasets"][name] = {"path": path.name, "part": part, "rows": table.num_rows,
                                      "columns": table.schema.names, "sha256": _sha(path)}
        entry = manifest["parts"].setdefault(part, {"status": "BUILT", "rule_version": nav_ids.RULE_VERSIONS.get(
            part, {"formulas": "formula_context_v1"}.get(part)), "datasets": []})
        entry["datasets"].append(name)
    (nav_dir / "manifest.json").write_bytes(json.dumps(manifest, ensure_ascii=False, indent=1, sort_keys=True)
                                            .encode("utf-8"))
    return {"sections": sec_ids, "formulas": (F1, F2, F3, F4, F5), "terms": {k: tid(k) for k in TERMS},
            "topics": TOPIC_IDS if topics else (), "snapshot_id": snapshot_id,
            "blocks": (B_DEF1, B_DEF2, B_DEF3, B_DEF4, B_REF, B_TERMDEF),
            "tables": {T1: nav_ids.table_id(T1), T2: nav_ids.table_id(T2)} if extras else {},
            "values": dict(PARAMETER_IDS) if extras else {},
            "dictionary_only": {k: tid(k) for k in DICTIONARY_ONLY} if extras else {},
            "groups": dict(GROUP_IDS) if extras else {}}


# ================================================================================================ fake driver
class FakeNavNeo4j:
    """In-memory stand-in for the NAV loader and queries (dispatch on the ``// vkm-nav:<op>`` tag)."""

    def __init__(self) -> None:
        self.nodes: dict[str, dict[str, Any]] = {}
        self.rels: dict[str, dict[tuple[str, str, Any], dict[str, Any]]] = defaultdict(dict)
        self.schema_names: set[str] = set()
        self.doc_run: dict[str, Any] | None = None
        self.ops: list[str] = []
        self.closed = False

    # -------------------------------------------------------------- driver API
    def execute_query(self, query: Any, parameters_: dict[str, Any] | None = None, database_: str | None = None,
                      routing_: str | None = None, **_kw: Any) -> tuple[list[dict[str, Any]], None, None]:
        text = str(getattr(query, "text", query))
        params = dict(parameters_ or {})
        head = text.lstrip()
        if head.startswith(("CREATE CONSTRAINT", "CREATE INDEX")):
            self.schema_names.add(head.split()[2])
            return [], None, None
        if head.startswith(("SHOW CONSTRAINTS", "SHOW INDEXES")):
            return [{"name": n} for n in sorted(self.schema_names)], None, None
        if not head.startswith("// vkm-nav:"):
            raise AssertionError(f"unexpected statement: {head[:80]!r}")
        tag = head.split("\n", 1)[0][len("// vkm-nav:"):].split()
        op, arg = tag[0], (tag[1] if len(tag) > 1 else None)
        self.ops.append(op)
        return getattr(self, "_op_" + op.replace("-", "_"))(arg, params, text), None, None

    def close(self) -> None:
        self.closed = True

    # -------------------------------------------------------------- helpers
    def add_node(self, label: str, node_id: str, layer_label: str = S.LAYER_LABELS[S.LAYER], **props: Any) -> None:
        self.nodes[node_id] = {"labels": {label, layer_label}, "props": {"id": node_id, **props}}

    def add_rel(self, rel_type: str, a: str, b: str, key: Any = None, **props: Any) -> None:
        self.rels[rel_type][(a, b, key)] = dict(props)

    def _has(self, node_id: str, label: str) -> bool:
        node = self.nodes.get(node_id)
        return bool(node) and label in node["labels"]

    def _nav(self, node: dict[str, Any]) -> bool:
        return N.LAYER_LABEL in node["labels"]

    def _delete_node(self, node_id: str) -> None:
        self.nodes.pop(node_id, None)
        for t in list(self.rels):
            for k in [k for k in self.rels[t] if node_id in (k[0], k[1])]:
                del self.rels[t][k]

    def node_map(self, node_id: str) -> dict[str, Any]:
        node = self.nodes[node_id]
        props = node["props"]
        out = {k: props.get(k) for k in NODE_KEYS}
        out["labels"] = sorted(node["labels"])
        out["equation_number"] = None
        out["definition_block_id"] = None
        if "Formula" in node["labels"]:
            for (a, _b, _k), p in sorted(self.rels.get("IN_SECTION", {}).items()):
                if a == node_id:
                    out["equation_number"] = p.get("equation_number")
                    break
        if "FormulaSymbol" in node["labels"]:
            for (a, _b, _k), p in sorted(self.rels.get("DEFINED_FOR", {}).items()):
                if a == node_id:
                    out["definition_block_id"] = p.get("definition_block_id")
                    break
        return out

    @staticmethod
    def rel_map(rel_type: str, a: str, b: str, props: dict[str, Any]) -> dict[str, Any]:
        return {"type": rel_type, "from": a, "to": b, **{k: props.get(k) for k in REL_KEYS}}

    # -------------------------------------------------------------- loader ops
    def _op_server(self, _a: Any, _p: dict[str, Any], _t: str) -> list[dict[str, Any]]:
        return [{"name": "Neo4j Kernel", "versions": ["5.26.31"], "edition": "community"}]

    def _op_doc_state(self, _a: Any, _p: dict[str, Any], _t: str) -> list[dict[str, Any]]:
        return [dict(self.doc_run)] if self.doc_run else []

    def _op_doc_count_label(self, label: str, _p: dict[str, Any], _t: str) -> list[dict[str, Any]]:
        return [{"n": sum(1 for n in self.nodes.values() if label in n["labels"])}]

    def _op_doc_count_rel(self, rel_type: str, _p: dict[str, Any], _t: str) -> list[dict[str, Any]]:
        return [{"n": len(self.rels.get(rel_type, {}))}]

    def _op_meta_get(self, _a: Any, p: dict[str, Any], _t: str) -> list[dict[str, Any]]:
        node = self.nodes.get(p["id"])
        return [{"props": dict(node["props"]), "age": 0}] if node else []

    def _op_meta_set(self, _a: Any, p: dict[str, Any], _t: str) -> list[dict[str, Any]]:
        node = self.nodes.setdefault(p["id"], {"labels": {N.META_LABEL}, "props": {"id": p["id"]}})
        node["labels"].add(N.LAYER_LABEL)
        node["props"].update(p["props"])
        node["props"]["heartbeat_at"] = "now"
        return [{"id": p["id"]}]

    def _op_merge_nodes(self, label: str, p: dict[str, Any], _t: str) -> list[dict[str, Any]]:
        for row in p["rows"]:
            node = self.nodes.get(row["id"])
            if node is None or label not in node["labels"]:
                self.nodes[row["id"]] = {"labels": {label, N.LAYER_LABEL}, "props": dict(row["props"])}
            else:
                node["props"] = dict(row["props"])
                node["labels"].add(N.LAYER_LABEL)
        return [{"written": len(p["rows"])}]

    def _op_merge_rels(self, name: str, p: dict[str, Any], _t: str) -> list[dict[str, Any]]:
        rel = N.REL_BY_NAME[name]
        written = 0
        for row in p["rows"]:
            if self._has(row["from_id"], rel.start) and self._has(row["to_id"], rel.end):
                self.rels[rel.type][(row["from_id"], row["to_id"], row.get("key") if rel.key else None)] = \
                    dict(row["props"])
                written += 1
        return [{"written": written}]

    def _op_missing_endpoints(self, name: str, p: dict[str, Any], _t: str) -> list[dict[str, Any]]:
        rel = N.REL_BY_NAME[name]
        out = []
        for row in p["rows"]:
            a, b = self._has(row["from_id"], rel.start), self._has(row["to_id"], rel.end)
            if not (a and b):
                out.append({"from_id": row["from_id"], "to_id": row["to_id"], "missing_from": not a,
                            "missing_to": not b})
        return out[:20]

    def _op_sweep_rels(self, rel_type: str, p: dict[str, Any], _t: str) -> list[dict[str, Any]]:
        keys = [k for k, v in self.rels.get(rel_type, {}).items()
                if (v.get("projection_run_id") or "") != p["run_id"]]
        for k in keys[: p["batch"]]:
            del self.rels[rel_type][k]
        return [{"n": min(len(keys), p["batch"])}]

    def _op_sweep_nodes(self, _a: Any, p: dict[str, Any], _t: str) -> list[dict[str, Any]]:
        ids = [i for i, n in self.nodes.items() if self._nav(n) and N.META_LABEL not in n["labels"]
               and (n["props"].get("projection_run_id") or "") != p["run_id"]][: p["batch"]]
        for i in ids:
            self._delete_node(i)
        return [{"n": len(ids)}]

    def _op_drop_rels(self, rel_type: str, p: dict[str, Any], _t: str) -> list[dict[str, Any]]:
        keys = list(self.rels.get(rel_type, {}))[: p["batch"]]
        for k in keys:
            del self.rels[rel_type][k]
        return [{"n": len(keys)}]

    def _op_drop_nodes(self, _a: Any, p: dict[str, Any], _t: str) -> list[dict[str, Any]]:
        ids = [i for i, n in self.nodes.items() if self._nav(n)][: p["batch"]]
        for i in ids:
            self._delete_node(i)
        return [{"n": len(ids)}]

    def _op_count_label(self, label: str, _p: dict[str, Any], _t: str) -> list[dict[str, Any]]:
        return [{"n": sum(1 for n in self.nodes.values() if label in n["labels"])}]

    def _op_count_rel(self, name: str, _p: dict[str, Any], _t: str) -> list[dict[str, Any]]:
        rel = N.REL_BY_NAME[name]
        return [{"n": sum(1 for (a, b, _k) in self.rels.get(rel.type, {})
                          if self._has(a, rel.start) and self._has(b, rel.end))}]

    def _op_tree_edges(self, label: str, _p: dict[str, Any], _t: str) -> list[dict[str, Any]]:
        return [{"child": a, "parent": b} for (a, b, _k) in self.rels.get("NAV_CHILD_OF", {})
                if self._has(a, label) and self._has(b, label)]

    def _op_sections_without_pages(self, _a: Any, _p: dict[str, Any], _t: str) -> list[dict[str, Any]]:
        covered = {a for (a, _b, _k) in self.rels.get("COVERS_PAGE", {})}
        missing = sorted(i for i, n in self.nodes.items() if "NavSection" in n["labels"] and i not in covered)
        return [{"n": len(missing), "examples": missing[:20]}]

    def _op_collisions(self, _a: Any, p: dict[str, Any], _t: str) -> list[dict[str, Any]]:
        doc_layer = S.LAYER_LABELS[S.LAYER]
        n = 0
        for node in self.nodes.values():
            if not self._nav(node):
                continue
            labels = node["labels"]
            if doc_layer in labels or labels & set(p["doc_labels"]) or len(labels & set(p["nav_types"])) != 1:
                n += 1
        return [{"n": n}]

    def _op_doc_with_nav_labels(self, _a: Any, p: dict[str, Any], _t: str) -> list[dict[str, Any]]:
        doc_layer = S.LAYER_LABELS[S.LAYER]
        return [{"n": sum(1 for x in self.nodes.values() if doc_layer in x["labels"] and x["labels"] & set(
            p["nav_labels"]))}]

    def _op_bad_node_props(self, _a: Any, p: dict[str, Any], _t: str) -> list[dict[str, Any]]:
        n = 0
        for node in self.nodes.values():
            if not self._nav(node) or N.META_LABEL in node["labels"]:
                continue
            pr = node["props"]
            if pr.get("layer") != p["layer"] or pr.get("snapshot_id") != p["snapshot"] or not pr.get("rule_version") \
                    or not pr.get("id") or (p.get("run_id") and pr.get("projection_run_id") != p["run_id"]):
                n += 1
        return [{"n": n}]

    def _op_bad_rel_props(self, rel_type: str, p: dict[str, Any], _t: str) -> list[dict[str, Any]]:
        return [{"n": sum(1 for v in self.rels.get(rel_type, {}).values()
                          if v.get("layer") != p["layer"] or v.get("snapshot_id") != p["snapshot"]
                          or not v.get("rule_version")
                          or (p.get("run_id") and v.get("projection_run_id") != p["run_id"]))}]

    # -------------------------------------------------------------- checks N8–N11 of the new parts
    def _edges_of(self, node_id: str, types: tuple[str, ...], direction: str = "both") -> list[tuple[str, str, str]]:
        out = []
        for t in types:
            for (a, b, _k) in self.rels.get(t, {}):
                if (direction in ("out", "both") and a == node_id) or (direction in ("in", "both") and b == node_id):
                    out.append((t, a, b))
        return out

    def _op_dictionary_check(self, _a: Any, _p: dict[str, Any], _t: str) -> list[dict[str, Any]]:
        concept = ("CO_OCCURS", "CONTAINS_TERM", "SAME_TERM_AS", "DEFINED_AS", "MENTIONED_IN", "SYMBOL_OF")
        only = [i for i, n in self.nodes.items() if "Term" in n["labels"] and n["props"].get("dictionary_only")]
        return [{"dictionary_only": len(only),
                 "without_pairs": sum(1 for i in only if not self._edges_of(i, N.DICTIONARY_TYPES)),
                 "in_concept_graph": sum(1 for i in only if self._edges_of(i, concept)),
                 "self_pairs": sum(1 for t in N.DICTIONARY_TYPES for (a, b, _k) in self.rels.get(t, {}) if a == b)}]

    def _op_tables_check(self, _a: Any, _p: dict[str, Any], _t: str) -> list[dict[str, Any]]:
        tables = [(i, n["props"]) for i, n in self.nodes.items() if "NavTable" in n["labels"]]
        out = {"tables": len(tables), "without_grid": 0, "several_grids": 0, "several_sections": 0}
        for i, props in tables:
            grids = self._edges_of(i, ("GRID_OF",), "out")
            out["without_grid"] += not any(b == props.get("table_id") and self._has(b, "Table")
                                           for _t2, _a2, b in grids)
            out["several_grids"] += len(grids) > 1
            out["several_sections"] += len(self._edges_of(i, ("TABLE_IN_SECTION",), "out")) > 1
        return [out]

    def _op_dup_groups_check(self, _a: Any, _p: dict[str, Any], _t: str) -> list[dict[str, Any]]:
        out = {"groups": 0, "member_count_mismatch": 0, "under_two_members": 0, "primary_not_member": 0,
               "primary_without_rule": 0, "wrong_type": 0}
        for i, n in self.nodes.items():
            if "ObjectDupGroup" not in n["labels"]:
                continue
            g = n["props"]
            out["groups"] += 1
            members = [(a, props) for (a, b, _k), props in self.rels.get("DUP_MEMBER_OF", {}).items() if b == i]
            primaries = [a for a, props in members if props.get("is_primary")]
            out["member_count_mismatch"] += g.get("n_members") is not None and len(members) != g["n_members"]
            out["under_two_members"] += len(members) < 2
            primary = g.get("primary_object_id")
            out["primary_not_member"] += bool(primary) and primary not in primaries
            out["primary_without_rule"] += not g.get("primary_object_id") and bool(primaries)
            want = N.DUP_MEMBER_LABELS.get(g.get("object_type"))
            out["wrong_type"] += sum(1 for a, _props in members if not self._has(a, want or "?"))
        return [out]

    def _op_values_check(self, _a: Any, _p: dict[str, Any], _t: str) -> list[dict[str, Any]]:
        out = {"n_values": 0, "in_table_not_table": 0, "in_table_other_table": 0, "near_formula_other_method": 0,
               "several_edges": 0}
        for i, n in self.nodes.items():
            if "ParameterValue" not in n["labels"]:
                continue
            v = n["props"]
            out["n_values"] += 1
            in_table = self._edges_of(i, ("IN_TABLE",), "out")
            near = self._edges_of(i, ("NEAR_FORMULA",), "out")
            out["in_table_not_table"] += v.get("method") != "TABLE" and bool(in_table)
            out["in_table_other_table"] += any(       # Cypher `<>` with a null side is null (not counted)
                None not in (self.nodes[b]["props"].get("table_id"), v.get("table_id"))
                and self.nodes[b]["props"].get("table_id") != v.get("table_id") for _t2, _a2, b in in_table)
            out["near_formula_other_method"] += v.get("method") != "NEAR_FORMULA" and bool(near)
            out["several_edges"] += any(len(self._edges_of(i, (t,), "out")) > 1 for t in (
                "IN_TABLE", "NEAR_FORMULA", "VALUE_IN_SECTION", "VALUE_OF"))
        return [out]

    # -------------------------------------------------------------- query ops
    def _terms(self) -> list[dict[str, Any]]:
        return [n["props"] for n in self.nodes.values() if "Term" in n["labels"]]

    @staticmethod
    def _term_row(t: dict[str, Any]) -> dict[str, Any]:
        return {"id": t["id"], "lemma": t.get("lemma"), "lemma_key": t.get("lemma_key"), "df_units": t.get("df_units"),
                "df_sources": t.get("df_sources"), "language": t.get("language"), "kind": t.get("kind"),
                "dictionary_only": t.get("dictionary_only")}

    def _op_find_terms(self, _a: Any, p: dict[str, Any], _t: str) -> list[dict[str, Any]]:
        rows = [self._term_row(t) for t in self._terms() if t["id"] == p["text"] or t.get("lemma_key") in p["keys"]]
        return sorted(rows, key=lambda r: (-(r["df_units"] or 0), r["id"]))[: p["limit"]]

    def _op_find_terms_names(self, _a: Any, p: dict[str, Any], _t: str) -> list[dict[str, Any]]:
        rows = [self._term_row(t) for t in self._terms() if p["norm"] in (t.get("name_keys") or [])]
        return sorted(rows, key=lambda r: (-(r["df_units"] or 0), r["id"]))[: p["limit"]]

    def _adjacent(self, node_id: str, types: set[str] | None) -> list[tuple[str, str, str, Any, dict[str, Any]]]:
        out = []
        for t, rels in self.rels.items():
            if types is not None and t not in types:
                continue
            for (a, b, k), props in rels.items():
                if a == node_id:
                    out.append((t, a, b, k, props))
                elif b == node_id:
                    out.append((t, a, b, k, props))
        return out

    def _op_shortest_length(self, arg: str, p: dict[str, Any], t: str) -> list[dict[str, Any]]:
        paths = self._op_paths(arg, {**p, "cap": 1}, t)
        return [{"n": len(paths[0]["rels"])}] if paths else []

    def _op_paths(self, arg: str, p: dict[str, Any], _t: str) -> list[dict[str, Any]]:
        max_len, types, labels = int(arg), set(p["rel_types"]), set(p["labels"])
        a, b = p["a"], p["b"]
        if a not in self.nodes or b not in self.nodes or a == b:
            return []

        def allowed(i: str) -> bool:
            return bool(self.nodes[i]["labels"] & labels)

        dist = {a: 0}
        preds: dict[str, list[tuple[str, tuple[str, str, str, Any, dict[str, Any]]]]] = defaultdict(list)
        queue = deque([a])
        while queue:
            x = queue.popleft()
            if dist[x] >= max_len or (b in dist and dist[x] >= dist[b]):
                continue
            for rel in sorted(self._adjacent(x, types), key=lambda r: (r[0], r[1], r[2], str(r[3]))):
                y = rel[2] if rel[1] == x else rel[1]
                if not allowed(y):
                    continue
                if y not in dist:
                    dist[y] = dist[x] + 1
                    queue.append(y)
                if dist[y] == dist[x] + 1:
                    preds[y].append((x, rel))
        if b not in dist:
            return []
        paths: list[list[tuple[str, Any]]] = []

        def walk(node: str, acc: list[tuple[str, Any]]) -> None:
            if len(paths) >= p["cap"]:
                return
            if node == a:
                paths.append(list(reversed(acc)))
                return
            for prev, rel in preds[node]:
                walk(prev, acc + [(node, rel)])

        walk(b, [])
        out = []
        for steps in paths:
            ids = [a] + [n for n, _r in steps]
            out.append({"nodes": [self.node_map(i) for i in ids],
                        "rels": [self.rel_map(r[0], r[1], r[2], r[4]) for _n, r in steps]})
        return out

    def _groups(self, node_id: str, exclude: str | None, per_type: int) -> list[dict[str, Any]]:
        grouped: dict[tuple[str, bool], list[tuple[str, str, str, Any, dict[str, Any]]]] = defaultdict(list)
        for rel in self._adjacent(node_id, None):
            other = rel[2] if rel[1] == node_id else rel[1]
            if other == exclude:
                continue
            grouped[(rel[0], rel[1] == node_id)].append(rel)
        out = []
        for (t, is_out), rels in sorted(grouped.items()):
            def strength(r: tuple[str, str, str, Any, dict[str, Any]]) -> float:
                pr = r[4]                              # as nav_query._ORDER_R
                if pr.get("npmi") is not None and pr.get("n_units") is not None:
                    return float(pr["npmi"]) * pr["n_units"] / (pr["n_units"] + 2.0)
                for k in ("similarity", "score", "cosine", "tfidf", "weight"):
                    if pr.get(k) is not None:
                        return float(pr[k])
                return 0.0
            rels.sort(key=lambda r: (-strength(r), r[2] if r[1] == node_id else r[1]))
            items = [{"r": self.rel_map(r[0], r[1], r[2], r[4]),
                      "m": self.node_map(r[2] if r[1] == node_id else r[1])} for r in rels[:per_type]]
            out.append({"type": t, "out": is_out, "total": len(rels), "items": items})
        return out

    def _op_neighbourhood(self, layer: str, p: dict[str, Any], _t: str) -> list[dict[str, Any]]:
        node = self.nodes.get(p["id"])
        if node is None or S.LAYER_LABELS[layer] not in node["labels"]:
            return []
        return [{"node": self.node_map(p["id"]), "groups": self._groups(p["id"], None, p["per_type"])}]

    def _op_neighbourhood_2(self, layer: str, p: dict[str, Any], _t: str) -> list[dict[str, Any]]:
        out = []
        for mid in p["ids"]:
            node = self.nodes.get(mid)
            if node is None or S.LAYER_LABELS[layer] not in node["labels"]:
                continue
            out.append({"mid": mid, "groups": self._groups(mid, p["root"], p["per_type"])})
        return out


def add_document_graph(fake: FakeNavNeo4j, nav_dir: str | Path, *, snapshot_id: str = SNAPSHOT,
                       drop: tuple[str, ...] = ()) -> dict[str, Any]:
    """DOCUMENT nodes referenced by the NAV datasets of ``nav_dir`` (+ HAS_PAGE/HAS_FORMULA/HAS_BLOCK) and a COMPLETE
    DOCUMENT ProjectionRun whose counts are the fake's; ids in ``drop`` are left out (dangling-reference tests)."""
    import duckdb

    con = duckdb.connect()
    d = Path(nav_dir)

    def ids(sql: str, *datasets: str) -> set[str]:
        if not all((d / f"{x}.parquet").is_file() for x in datasets):
            return set()
        for x in datasets:
            sql = sql.replace("{" + x + "}", f"read_parquet('{(d / (x + '.parquet')).as_posix()}')")
        return {r[0] for r in con.execute(sql).fetchall() if r[0]}

    pages = ids("SELECT DISTINCT page_id FROM {section_pages}", "section_pages")
    formulas = ids("SELECT formula_id FROM {formula_context}", "formula_context")
    blocks = ids("SELECT block_id FROM {formula_refs} WHERE block_id IS NOT NULL "
                 "UNION SELECT dst_ref FROM {term_edges} WHERE kind = 'DEFINED_AS' "
                 "UNION SELECT definition_block_id FROM {formula_symbols} WHERE definition_block_id IS NOT NULL",
                 "formula_refs", "term_edges", "formula_symbols")
    blocks |= ids("SELECT block_id FROM {parameter_candidates}", "parameter_candidates")
    formulas |= ids("SELECT formula_id FROM {parameter_candidates}", "parameter_candidates")
    tables = ids("SELECT table_id FROM {table_structure}", "table_structure")
    tables |= ids("SELECT object_id FROM {object_dup_members} WHERE object_type = 'TABLE'", "object_dup_members")
    figures = ids("SELECT object_id FROM {object_dup_members} WHERE object_type = 'FIGURE'", "object_dup_members")
    formulas |= ids("SELECT object_id FROM {object_dup_members} WHERE object_type = 'FORMULA'", "object_dup_members")
    con.close()
    sources = {p.split(":")[0] for p in pages | formulas | blocks | tables | figures}
    for s in sorted(sources - set(drop)):
        fake.add_node("Source", s, source_id=s)
    for p in sorted(pages - set(drop)):
        fake.add_node("Page", p, source_id=p.split(":")[0], page_index=int(p[-4:]))
        fake.add_rel("HAS_PAGE", p.split(":")[0], p)
    for label, rel_type, objects, extra in (("Formula", "HAS_FORMULA", formulas, {"formula_kind": "DISPLAY"}),
                                            ("Block", "HAS_BLOCK", blocks, {"block_type": "TEXT"}),
                                            ("Table", "HAS_TABLE", tables, {}), ("Figure", "HAS_FIGURE", figures, {})):
        for o in sorted(objects - set(drop)):
            page = o.rsplit(":", 1)[0]
            fake.add_node(label, o, source_id=o.split(":")[0], page_id=page, **extra)
            fake.add_rel(rel_type, page, o)
    counts = {"nodes": {n.label: sum(1 for x in fake.nodes.values() if n.label in x["labels"]) for n in S.NODE_TYPES},
              "rels": {r.type: len(fake.rels.get(r.type, {})) for r in S.REL_TYPES}}
    fake.doc_run = {"id": "VKM-PRJ-DOC-20260928T000000Z-00000000", "status": "COMPLETE", "snapshot_id": snapshot_id,
                    "counts_json": json.dumps(counts), "content_digest": "0" * 64}
    return counts
