"""G3 other wordings (``navigation.expansion_query.expand_query``) on synthetic NAV tables: synonyms and abbreviations
of the term dictionary replace the query's phrases, OCR spellings are skipped, a narrower term of the query's main
concept is appended, and the function is registered for ``NavStore.run``. Invented terms and pairs; the Russian
phrase keys need pymorphy3 (extra ``navigation``)."""
from __future__ import annotations

import importlib.util

import pytest

duckdb = pytest.importorskip("duckdb")

from vkm_corpus.navigation import concepts as C  # noqa: E402
from vkm_corpus.navigation import expansion_query as X  # noqa: E402
from vkm_corpus.navigation import store as nav_store  # noqa: E402

needs_pymorphy = pytest.mark.skipif(importlib.util.find_spec("pymorphy3") is None,
                                    reason="pymorphy3 not installed (extra `navigation`)")
MORPH = C.Morphology() if importlib.util.find_spec("pymorphy3") is not None else None


def key(phrase: str) -> str:
    return C.phrase_keys(phrase, MORPH)[0]


def _con(pairs, terms, contains):
    con = duckdb.connect()
    con.execute("""CREATE TABLE term_translations (pair_id VARCHAR, relation VARCHAR, lang_a VARCHAR, key_a VARCHAR,
                   lemma_a VARCHAR, lang_b VARCHAR, key_b VARCHAR, lemma_b VARCHAR, in_terms_a BOOLEAN,
                   in_terms_b BOOLEAN, score DOUBLE)""")
    for i, (rel, a, b, score) in enumerate(pairs):
        con.execute("INSERT INTO term_translations VALUES (?, ?, 'ru', ?, ?, 'ru', ?, ?, true, true, ?)",
                    [f"TTR-{i:016x}", rel, key(a), a, key(b), b, score])
    con.execute("CREATE TABLE terms (term_id VARCHAR, lemma VARCHAR, lemma_key VARCHAR, language VARCHAR, "
                "df_units INTEGER)")
    for i, (lemma, df) in enumerate(terms):
        con.execute("INSERT INTO terms VALUES (?, ?, ?, 'ru', ?)", [f"TRM-{i:016x}", lemma, key(lemma), df])
    ids = {lemma: f"TRM-{i:016x}" for i, (lemma, _df) in enumerate(terms)}
    con.execute("CREATE TABLE term_edges (kind VARCHAR, src_term_id VARCHAR, dst_term_id VARCHAR)")
    for narrow, broad in contains:
        con.execute("INSERT INTO term_edges VALUES ('CONTAINS', ?, ?)", [ids[narrow], ids[broad]])
    return con


PAIRS = [("ABBREVIATION", "водозащитная толща", "ВЗТ", 1.0),
         ("SYNONYM", "камерная система разработки", "камерная система отработки", 0.95),
         ("SYNONYM", "месторождение", "место рождения", 0.95),          # an OCR split: never an expansion
         ("SYNONYM", "закладка", "закладочный массив", 0.5)]            # below the trusted score
TERMS = [("водозащитная толща", 40), ("мощность водозащитной толщи", 25), ("нарушение водозащитной толщи", 12),
         ("подработка водозащитной толщи", 3), ("толща", 90), ("вмещающая толща", 30), ("мощность", 70)]
CONTAINS = [("мощность водозащитной толщи", "водозащитная толща"),
            ("нарушение водозащитной толщи", "водозащитная толща"),
            ("подработка водозащитной толщи", "водозащитная толща"), ("вмещающая толща", "толща")]


@needs_pymorphy
def test_equivalents_replace_phrases_by_trusted_pairs():
    con = _con(PAIRS, TERMS, CONTAINS)
    r = X.expand_query(con, "мощность водозащитной толщи", morph=MORPH)
    # the whole query is a term without narrower terms: only the abbreviation wording
    assert [x["kind"] for x in r["expansions"]] == ["equivalents"]
    eq = r["expansions"][0]
    assert eq["text"] == "мощность ВЗТ" and eq["terms"][0]["relation"] == "ABBREVIATION"
    assert eq["terms"][0]["span"] == "водозащитной толщи" and r["phrases"] == 4
    # a trusted synonym replaces its phrase; an OCR split and an untrusted pair do not
    s = X.expand_query(con, "камерная система разработки месторождения", morph=MORPH, narrower=False)
    assert [x["text"] for x in s["expansions"]] == ["камерная система отработки месторождения"]
    assert X.expand_query(con, "закладка", morph=MORPH, narrower=False)["expansions"] == []
    low = X.expand_query(con, "закладка", morph=MORPH, narrower=False, min_score=0.4)
    assert low["expansions"][0]["text"] == "закладочный массив"
    assert X.expand_query(con, "   ", morph=MORPH)["expansions"] == []


@needs_pymorphy
def test_narrower_term_of_the_main_concept_only():
    con = _con(PAIRS, TERMS, CONTAINS)
    r = X.expand_query(con, "состояние водозащитной толщи", morph=MORPH)
    nar = [x for x in r["expansions"] if x["kind"] == "narrower"]
    # the main concept «водозащитная толща» (two words); the most frequent narrower term with df ≥ 5 comes first,
    # the rare one (df 3) never
    assert nar[0]["text"] == "состояние водозащитной толщи мощность водозащитной толщи"
    assert nar[0]["terms"][0]["df_units"] == 25 and nar[0]["terms"][0]["span"] == "водозащитной толщи"
    # a narrower term already in the query is skipped: the next one
    r2 = X.expand_query(con, "мощность водозащитной толщи", morph=MORPH, max_expansions=2)
    assert [x["text"] for x in r2["expansions"] if x["kind"] == "narrower"] == []  # main concept = the whole query
    # a one-word main concept has no narrower expansion (its narrower terms are generic)
    assert X.expand_query(con, "толща", morph=MORPH)["expansions"] == []
    one = X.expand_query(con, "состояние водозащитной толщи", morph=MORPH, max_expansions=1)
    assert [x["kind"] for x in one["expansions"]] == ["equivalents"]


@needs_pymorphy
def test_relations_limit_the_equivalents_to_one_kind():
    """The hybrid search's ``expand=terms`` asks for synonyms and abbreviations separately; the default is both."""
    con = _con(PAIRS, TERMS, CONTAINS)

    def texts(q, **kw):
        return [x["text"] for x in X.expand_query(con, q, morph=MORPH, narrower=False, **kw)["expansions"]]

    assert texts("мощность водозащитной толщи", relations=("ABBREVIATION",)) == ["мощность ВЗТ"]
    assert texts("мощность водозащитной толщи", relations=("SYNONYM",)) == []
    q = "камерная система разработки месторождения"
    assert texts(q, relations=("SYNONYM",)) == ["камерная система отработки месторождения"] == texts(q)
    assert texts(q, relations=("ABBREVIATION",)) == [] and texts(q, relations=()) == []
    assert texts(q, relations=("TRANSLATION",)) == []                      # not an equivalent relation


@needs_pymorphy
def test_missing_tables_skip_their_part():
    con = duckdb.connect()
    con.execute("CREATE TABLE terms (term_id VARCHAR, lemma VARCHAR, lemma_key VARCHAR, language VARCHAR, "
                "df_units INTEGER)")
    assert X.expand_query(con, "расчёт междукамерных целиков", morph=MORPH)["expansions"] == []


def test_registered_for_the_nav_store():
    assert nav_store.QUERY_FUNCTIONS["expand_query"] == "vkm_corpus.navigation.expansion_query:expand_query"
    assert nav_store.resolve("expand_query") is X.expand_query
