"""Concept graph of the navigation layer (NAV §4): builder and query functions on synthetic rows (no source text).

Russian noun-phrase patterns need pymorphy3 (extra ``navigation``); without it those tests skip and the fallback
morphology (stems) is still exercised. The GPU backend is compared with DuckDB only where cuDF is importable.
"""
from __future__ import annotations

import importlib.util

import pytest

pa = pytest.importorskip("pyarrow")
duckdb = pytest.importorskip("duckdb")

from vkm_corpus.navigation import concepts as C  # noqa: E402
from vkm_corpus.navigation import concepts_query as Q  # noqa: E402
from vkm_corpus.navigation import ids as nav_ids  # noqa: E402

HAS_PYMORPHY = importlib.util.find_spec("pymorphy3") is not None
needs_pymorphy = pytest.mark.skipif(not HAS_PYMORPHY, reason="pymorphy3 not installed (extra `navigation`)")

# synthetic pages: (source, page_index, heading, text) — invented sentences
PAGES = [
    ("SYN-A", 1, "Глава 1. Ползучесть соли", "Ползучестью называется процесс медленного деформирования каменной соли "
     "при постоянной нагрузке. Длительная прочность каменной соли зависит от температуры."),
    ("SYN-A", 2, "Раздел 2", "Скорость ползучести растет с температурой. Длительная прочность снижается при высоком "
     "девиаторе напряжений."),
    ("SYN-A", 3, "Раздел 3", "Закладка выработанного пространства уменьшает оседание земной поверхности. Водозащитная "
     "толща (ВЗТ) защищает рудник."),
    ("SYN-A", 4, "Раздел 4", "Мульда сдвижения формируется над выработанным пространством. Оседание земной поверхности "
     "измеряют нивелированием."),
    ("SYN-B", 1, "Введение", "Под длительной прочностью понимается напряжение, при котором ползучесть переходит в "
     "разрушение. Температура влияет на ползучесть."),
    ("SYN-B", 2, "Опыты", "Ползучесть каменной соли изучали при разной температуре; длительная прочность образцов "
     "определена."),
    ("SYN-B", 3, "Закладка", "Закладка камер снижает оседание земной поверхности. Мульда сдвижения становится меньше."),
    ("SYN-B", 4, "Толща", "Водозащитная толща (ВЗТ) над рудником должна сохраняться. Затопление рудника опасно."),
    ("SYN-C", 1, "Creep", "Salt creep (ползучесть) depends on temperature and deviatoric stress. Rock salt creep is "
     "called dislocation creep."),
    ("SYN-C", 2, "Tests", "Creep tests of rock salt were run at high temperature."),
    ("SYN-C", 3, "Subsidence", "Subsidence above backfilled rooms is small."),
]


def synthetic_con(pages=PAGES) -> "duckdb.DuckDBPyConnection":
    con = duckdb.connect()
    con.execute("CREATE SCHEMA canonical")
    con.execute("CREATE TABLE canonical.pages (page_id VARCHAR, source_id VARCHAR, page_index INTEGER)")
    con.execute("""CREATE TABLE canonical.blocks (object_id VARCHAR, page_id VARCHAR, source_id VARCHAR,
                   is_primary_layer BOOLEAN, block_type VARCHAR, reading_order INTEGER, normalized_text VARCHAR)""")
    for sid, pi, heading, text in pages:
        pid = f"{sid}:p{pi:04d}"
        con.execute("INSERT INTO canonical.pages VALUES (?, ?, ?)", [pid, sid, pi])
        rows = [(f"{pid}:b1", "HEADING", 1, heading), (f"{pid}:b2", "TEXT", 2, text),
                # ignored: a reference list, a running header and the secondary text layer
                (f"{pid}:b3", "REFERENCE_LIST", 3, "Реферативный журнал. Реферативный журнал."),
                (f"{pid}:b4", "PAGE_HEADER", 0, "Колонтитул издания")]
        for oid, btype, ro, t in rows:
            con.execute("INSERT INTO canonical.blocks VALUES (?, ?, ?, true, ?, ?, ?)", [oid, pid, sid, btype, ro, t])
        con.execute("INSERT INTO canonical.blocks VALUES (?, ?, ?, false, 'TEXT', 9, ?)",
                    [f"{pid}:b9", pid, sid, "Вторичный слой распознавания текста"])
    return con


def _build(con=None, **kw):
    kw = {"seeds": (), "workers": 1, "backend": "duckdb", "communities": False, **kw}
    return C.build(con or synthetic_con(), **kw)


@pytest.fixture(scope="module")
def built():
    if not HAS_PYMORPHY:
        pytest.skip("pymorphy3 not installed")
    return _build()


def _terms(res) -> dict[str, dict]:
    return {r["lemma_key"]: r for r in res["terms"].to_pylist()}


# ------------------------------------------------------------------------------------------------ pure text functions


def test_script_repair_and_abbreviations():
    assert C._fix_script("kерна") == ("керна", "cyr")          # Latin k inside a Russian word
    assert C._fix_script("сiльвинит")[0] == "сильвинит"
    assert C._fix_script("creep") == ("creep", "lat")
    assert C._is_abbr("ВЗТ") and C._is_abbr("InSAR") and C._is_abbr("СКРУ-1")
    assert not C._is_abbr("МПа") and not C._is_abbr("II") and not C._is_abbr("Соль")
    assert C._abbr_matches("ВЗТ", "водозащитная толща")
    assert C._abbr_matches("НДС", "напряженно-деформированное состояние")
    assert not C._abbr_matches("НДС", "состояние")
    assert C._singular_en("stresses") == "stress" and C._singular_en("properties") == "property"
    assert C._singular_en("analyses") == "analysis" and C._singular_en("mechanics") == "mechanics"


def test_math_is_removed_and_english_phrases_found():
    res = C.analyze_text("The creep rate $\\dot\\varepsilon = A \\sigma^n$ of rock salts.", C.Morphology("crude"))
    keys = {c.key for c in res.cands}
    assert {"creep rate", "rock salt", "creep"} <= keys
    assert not any("varepsilon" in k or "sigma" in k for k in keys)


@needs_pymorphy
def test_russian_noun_phrases_lemmas_and_patterns():
    m = C.Morphology("pymorphy3")
    res = C.analyze_text("С помощью радарной интерферометрии (InSAR) измерено оседание земной поверхности.", m)
    keys = {c.key for c in res.cands}
    assert {"радарный интерферометрия", "оседание земной поверхность", "insar"} <= keys
    assert not any(k.startswith("помощь") for k in keys)                # complex preposition «с помощью»
    assert ("радарный интерферометрия", "ru", "insar", "en", "InSAR", "paren_abbreviation") in res.same_as
    d = C.analyze_text("Процесс медленного деформирования называется ползучестью.", m)
    assert ("ползучесть", "def_nazyvaetsya") in d.definitions
    d = C.analyze_text("Реология (от греч. rheos — течение) — наука о течении.", m)
    assert ("реология", "def_etym") in d.definitions
    assert C.phrase_keys("ползучести соли", m)[0] == "ползучесть соль"
    assert m.nominative("оседания земной поверхности") == "оседание земной поверхности"
    assert m.nominative("горных работ") == "горные работы"


# ------------------------------------------------------------------------------------------------------------ builder


@needs_pymorphy
def test_build_schemas_ids_and_referential_integrity(built):
    terms, mentions, edges = built["terms"], built["term_mentions"], built["term_edges"]
    assert terms.schema.remove_metadata() == C.TERMS_SCHEMA
    assert mentions.schema == C.MENTIONS_SCHEMA and edges.schema == C.EDGES_SCHEMA
    rows = terms.to_pylist()
    ids = [r["term_id"] for r in rows]
    assert len(ids) == len(set(ids))
    assert all(r["term_id"] == nav_ids.term_id(r["lemma_key"]) for r in rows)
    assert {r["rule_version"] for r in rows} == {nav_ids.RULE_VERSIONS["concepts"]}
    assert {r["morphology"] for r in rows} == {"pymorphy3"}
    known = set(ids)
    assert set(mentions.column("term_id").to_pylist()) <= known
    for e in edges.to_pylist():
        assert e["src_term_id"] in known
        assert (e["dst_term_id"] in known) if e["dst_term_id"] is not None else bool(e["dst_ref"])
        assert e["kind"] in C.EDGE_KINDS
    info = C.build_info(terms)
    assert info["counts"]["terms"] == terms.num_rows and info["backend"] == "duckdb"


@needs_pymorphy
def test_frequency_filter_stop_lists_and_ignored_blocks(built):
    t = _terms(built)
    for key in ("ползучесть", "длительный прочность", "температура", "каменный соль", "водозащитный толща", "взт",
                "оседание земной поверхность", "мульда сдвижение", "закладка"):
        assert key in t, key
        assert t[key]["df_units"] >= 2 and t[key]["df_sources"] >= 2
    assert t["длительный прочность"]["lemma"] == "длительная прочность"
    assert "скорость ползучесть" not in t                      # one unit of one source
    assert not any("реферативный" in k or "колонтитул" in k or "вторичный" in k for k in t)   # skipped blocks
    assert "dislocation creep" not in t


@needs_pymorphy
def test_seeds_bypass_frequency_filters():
    res = _build(seeds=("скорость ползучести", "термин которого нет"))
    t = _terms(res)
    assert t["скорость ползучесть"]["seed"] is True and t["скорость ползучесть"]["df_units"] == 1
    assert not any(k.startswith("термин") for k in t)          # a seed never creates an unmentioned term


@needs_pymorphy
def test_cooccurs_edges_respect_thresholds(built):
    t = _terms(built)
    by_id = {r["term_id"]: r["lemma_key"] for r in t.values()}
    co = [e for e in built["term_edges"].to_pylist() if e["kind"] == "CO_OCCURS"]
    assert co
    for e in co:
        assert e["n_units"] >= 3 and e["n_sources"] >= 2 and e["weight"] >= 0.2 and 1 <= len(e["examples"]) <= 3
        assert e["src_term_id"] < e["dst_term_id"]
        a, b = by_id[e["src_term_id"]], by_id[e["dst_term_id"]]
        assert f" {a} " not in f" {b} " and f" {b} " not in f" {a} "   # nested pairs are CONTAINS, not CO_OCCURS
    pairs = {frozenset((by_id[e["src_term_id"]], by_id[e["dst_term_id"]])) for e in co}
    assert frozenset(("ползучесть", "длительный прочность")) in pairs
    cont = {(by_id[e["src_term_id"]], by_id[e["dst_term_id"]]) for e in built["term_edges"].to_pylist()
            if e["kind"] == "CONTAINS"}
    assert ("водозащитный толща", "толща") in cont or ("каменный соль", "соль") in cont


@needs_pymorphy
def test_definitions_and_same_as_edges(built):
    t = _terms(built)
    edges = built["term_edges"].to_pylist()
    defs = {(e["src_term_id"], e["dst_ref"]) for e in edges if e["kind"] == "DEFINED_AS"}
    assert (t["ползучесть"]["term_id"], "SYN-A:p0001:b2") in defs
    assert (t["длительный прочность"]["term_id"], "SYN-B:p0001:b2") in defs
    same = [e for e in edges if e["kind"] == "SAME_AS"]
    vzt = [e for e in same if {e["src_term_id"], e["dst_term_id"]} == {t["водозащитный толща"]["term_id"],
                                                                        t["взт"]["term_id"]}]
    assert vzt and vzt[0]["weight"] == 2 and vzt[0]["n_sources"] == 2
    # «Salt creep (ползучесть)»: the equivalent with the same number of words is taken; «creep» is below the filters
    assert any(e["src_term_id"] == t["ползучесть"]["term_id"] and e["dst_ref"] == "en:creep" for e in same)


@needs_pymorphy
def test_mentions_units_pages_and_best_blocks(built):
    t = _terms(built)
    ms = [m for m in built["term_mentions"].to_pylist() if m["term_id"] == t["ползучесть"]["term_id"]]
    assert {m["unit_kind"] for m in ms} == {"HEADING_GROUP"}
    assert len(ms) == t["ползучесть"]["df_units"]
    assert all(m["unit_id"].startswith("NCU-") and m["page_ids"] and 1 <= len(m["best_block_ids"]) <= 3 for m in ms)
    assert sum(m["tf"] for m in ms) == t["ползучесть"]["tf"]


@needs_pymorphy
def test_section_units_with_fallback_for_uncovered_pages():
    sp = pa.table({"section_id": ["SEC-a1", "SEC-a1", "SEC-a2", "SEC-a2"] + ["SEC-b"] * 4,
                   "page_id": ["SYN-A:p0001", "SYN-A:p0002", "SYN-A:p0003", "SYN-A:p0004",
                               "SYN-B:p0001", "SYN-B:p0002", "SYN-B:p0003", "SYN-B:p0004"]})
    res = _build(section_pages=sp)
    ms = res["term_mentions"].to_pylist()
    kinds = {m["unit_kind"] for m in ms}
    assert kinds == {"SECTION", "HEADING_GROUP"}                # SYN-C has no sections: heading groups
    assert all((m["section_id"] is not None) == (m["unit_kind"] == "SECTION") for m in ms)
    info = C.build_info(res["terms"])
    assert info["counts"]["unit_kinds"]["SECTION"] == 3


@needs_pymorphy
def test_build_is_deterministic():
    a, b = _build(), _build()
    for name in ("terms", "term_mentions", "term_edges"):
        assert a[name].replace_schema_metadata(None).equals(b[name].replace_schema_metadata(None)), name


def test_long_units_are_cut_into_windows():
    sp = pa.table({"section_id": ["SEC-b"] * 4, "page_id": [f"SYN-B:p{i:04d}" for i in range(1, 5)]})
    res = _build(section_pages=sp, max_unit_pages=2, morphology="crude")
    kinds = C.build_info(res["terms"])["counts"]["unit_kinds"]
    assert kinds["SECTION"] == 2 and kinds["HEADING_GROUP"] == 7     # SEC-b: two windows; A and C: page groups


def test_fallback_morphology_without_pymorphy():
    res = _build(morphology="crude")
    rows = res["terms"].to_pylist()
    assert rows and {r["morphology"] for r in rows} == {"crude"}
    keys = {r["lemma_key"] for r in rows}
    assert "ползучест" in keys and "взт" in keys
    assert any(e["kind"] == "CO_OCCURS" for e in res["term_edges"].to_pylist())


@pytest.mark.gpu
@pytest.mark.skipif(importlib.util.find_spec("cudf") is None, reason="cuDF not installed (GPU backend)")
def test_gpu_backend_matches_duckdb():
    cpu = _build(morphology="crude")
    gpu = _build(morphology="crude", backend="cudf")
    assert C.build_info(gpu["terms"])["backend"] == "cudf"
    for name in ("term_mentions", "term_edges"):
        assert cpu[name].equals(gpu[name]), name


# ------------------------------------------------------------------------------------------------------------- query


@needs_pymorphy
def test_explore_concept_answers_with_ids_and_counts(built):
    con = duckdb.connect()
    Q.attach(con, built)
    r = Q.explore_concept(con, "ползучести", limit=5)
    assert r["match"]["lemma"] == "ползучесть" and r["match"]["match"] == "lemma_key"
    assert any(n["lemma"] == "длительная прочность" for n in r["neighbours"])
    assert all({"npmi", "score", "n_units", "n_sources", "example_page_ids"} <= set(n) for n in r["neighbours"])
    assert r["definitions"][0]["block_id"] == "SYN-A:p0001:b2" and r["definitions"][0]["source_id"] == "SYN-A"
    assert any(s["lemma"] == "en:creep" for s in r["same_as"])
    assert r["top_units"] and r["top_sources"][0]["source_id"] in ("SYN-A", "SYN-B")
    assert "not a physical" in r["note"]
    again = Q.explore_concept(con, r["match"]["term_id"])
    assert again["match"]["term_id"] == r["match"]["term_id"]
    vzt = Q.explore_concept(con, "ВЗТ")
    assert any(s["lemma"] == "водозащитная толща" for s in vzt["same_as"])
    assert Q.explore_concept(con, "совершенно неизвестное понятие")["match"] is None
    assert Q.find_terms(con, "") == []


@needs_pymorphy
def test_find_terms_falls_back_to_containment(built):
    con = duckdb.connect()
    Q.attach(con, built)
    rows = Q.find_terms(con, "толща")
    assert rows and rows[0]["lemma_key"] == "толща"
    rows = Q.find_terms(con, "водозащитной")                    # an adjective alone: terms containing it
    assert rows and all("водозащитный" in r["lemma_key"] for r in rows)
