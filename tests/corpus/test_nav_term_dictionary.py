"""Term dictionary of the navigation layer (NAV ``term_translations``): builder, query functions and registration on
synthetic rows (invented sentences, no corpus text). Vectors are synthetic too: phrases of one intended concept share
a random unit vector, every other phrase gets its own (nearly orthogonal) one.

The Russian noun-phrase keys need pymorphy3 (extra ``navigation``); without it the builder tests skip.
"""
from __future__ import annotations

import importlib.util
import json

import pytest

pa = pytest.importorskip("pyarrow")
duckdb = pytest.importorskip("duckdb")
np = pytest.importorskip("numpy")

from vkm_corpus.navigation import cli as nav_cli  # noqa: E402
from vkm_corpus.navigation import concepts as C  # noqa: E402
from vkm_corpus.navigation import ids as nav_ids  # noqa: E402
from vkm_corpus.navigation import store as nav_store  # noqa: E402
from vkm_corpus.navigation import term_dictionary as TD  # noqa: E402
from vkm_corpus.navigation import term_dictionary_query as Q  # noqa: E402
from vkm_corpus.navigation import term_dictionary_seeds as S  # noqa: E402
from vkm_corpus.navigation import term_vectors as TV  # noqa: E402

HAS_PYMORPHY = importlib.util.find_spec("pymorphy3") is not None
needs_pymorphy = pytest.mark.skipif(not HAS_PYMORPHY, reason="pymorphy3 not installed (extra `navigation`)")

# (source, page, block type, text) — invented sentences
BLOCKS = [
    ("SYN-A", 1, "TEXT", "Ключевые слова: ползучесть соли, мульда сдвижения, закладка, InSAR."),
    ("SYN-A", 1, "TEXT", "Ползучесть соли определяет мульда сдвижения над камерами; закладка снижает оседание."),
    ("SYN-A", 2, "TEXT", "Keywords: salt creep, subsidence trough, backfill, InSAR."),
    ("SYN-A", 2, "TEXT", "Salt creep controls the subsidence trough above rooms; backfill reduces subsidence."),
    ("SYN-A", 3, "TEXT", "Эффект ракурса (англ. foreshortening) искажает радарные снимки склонов."),
    ("SYN-A", 3, "CAPTION", "Рис. 2. Мульда сдвижения и закладка камер Fig. 2. Subsidence trough and room backfill"),
    ("SYN-A", 4, "TEXT", "Мульда сдвижения (или мульда оседания) растет медленно; ползучесть соли продолжается."),
    ("SYN-B", 1, "TEXT", "Ползучесть соли и мульда сдвижения изучены; закладка камер снижает оседание."),
    ("SYN-B", 2, "TEXT", "Мульда оседания и мульда сдвижения описаны; эффект ракурса учтен при обработке."),
    ("SYN-B", 3, "TEXT", "Salt creep and the subsidence trough were measured; room backfill was tested."),
    ("SYN-B", 4, "TEXT", "The inter-chamber pillar and the interchamber pillar carry the roof; foreshortening "
                         "affects radar images."),
    ("SYN-C", 1, "TEXT", "The interchamber pillar and the inter-chamber pillar were loaded; salt creep was slow."),
    ("SYN-C", 2, "TEXT", "Subsidence trough and salt creep near backfill; foreshortening of radar images."),
    ("SYN-C", 3, "TEXT", "Эффект ракурса и мульда оседания; ползучесть соли и закладка камер."),
    ("SYN-C", 4, "TEXT", "Модуль упругости соли измерен; модуль упругости пород меньше."),
    ("SYN-C", 5, "TEXT", "The elastic modulus of salt was measured; the elastic modulus of rock is lower."),
]
SEEDS = ("ползучесть соли", "мульда сдвижения", "мульда оседания", "закладка", "закладка камер", "эффект ракурса",
         "модуль упругости", "salt creep", "subsidence trough", "backfill", "backfill of rooms", "foreshortening",
         "elastic modulus", "interchamber pillar", "inter-chamber pillar", "room backfill")
# intended concepts: phrases of one concept share a vector
CONCEPTS = {
    "creep": ["ползучесть соли", "salt creep"],
    "trough": ["мульда сдвижения", "subsidence trough"],
    "backfill": ["закладка", "backfill"],
    "backfill_rooms": ["закладка камер", "room backfill"],
    "fore": ["эффект ракурса", "foreshortening"],
    "modulus": ["модуль упругости", "elastic modulus"],
    "pillar": ["interchamber pillar", "inter-chamber pillar"],
}
NO_SEEDS = {"translations": (), "abbreviations": (), "synonyms": ()}


def synthetic_con(blocks=BLOCKS) -> "duckdb.DuckDBPyConnection":
    con = duckdb.connect()
    con.execute("CREATE SCHEMA canonical")
    con.execute("CREATE TABLE canonical.pages (page_id VARCHAR, source_id VARCHAR, page_index INTEGER)")
    con.execute("""CREATE TABLE canonical.blocks (object_id VARCHAR, page_id VARCHAR, source_id VARCHAR,
                   is_primary_layer BOOLEAN, block_type VARCHAR, reading_order INTEGER, normalized_text VARCHAR)""")
    seen_pages = set()
    for n, (sid, pi, btype, text) in enumerate(blocks):
        pid = f"{sid}:p{pi:04d}"
        if pid not in seen_pages:
            seen_pages.add(pid)
            con.execute("INSERT INTO canonical.pages VALUES (?, ?, ?)", [pid, sid, pi])
            con.execute("INSERT INTO canonical.blocks VALUES (?, ?, ?, true, 'HEADING', 0, ?)",
                        [f"{pid}:b0", pid, sid, f"Раздел {pi}"])
        con.execute("INSERT INTO canonical.blocks VALUES (?, ?, ?, true, ?, ?, ?)",
                    [f"{pid}:b{n + 1}", pid, sid, btype, n + 1, text])
    return con


def formula_symbols_table() -> "pa.Table":
    return pa.table({"formula_id": ["SYN-C:p0004:f1", "SYN-C:p0005:f2"], "source_id": ["SYN-C", "SYN-C"],
                     "symbol": ["E", "E"], "symbol_key": ["E", "E"],
                     "definition": ["модуль упругости соли, МПа", "elastic modulus of salt, MPa"],
                     "definition_block_id": ["SYN-C:p0004:b15", "SYN-C:p0005:b16"]})


def synthetic_vectors(ctx: TD.Context, dim: int = 64) -> "pa.Table":
    rng = np.random.default_rng(7)
    concept_vec = {c: rng.normal(size=dim) for c in CONCEPTS}
    key_concept = {}
    for c, phrases in CONCEPTS.items():
        for ph in phrases:
            key_concept[C.phrase_keys(ph, ctx.morph)[0]] = c
    # «мульда оседания»: close to the trough concept (cos ≈ 0.85) but never the neighbour of «subsidence trough»
    mixed = {C.phrase_keys("мульда оседания", ctx.morph)[0]: "trough"}
    table = TD.phrases_table(ctx)
    vecs = []
    for key in table.column("key").to_pylist():
        if key in mixed:
            c = concept_vec[mixed[key]] / np.linalg.norm(concept_vec[mixed[key]])
            r = rng.normal(size=dim)
            r -= c * float(np.dot(r, c))
            v = 0.85 * c + 0.53 * r / np.linalg.norm(r)
        elif key in key_concept:
            v = concept_vec[key_concept[key]] + rng.normal(size=dim) * 0.05
        else:
            v = rng.normal(size=dim)
        vecs.append((v / np.linalg.norm(v)).astype(np.float32))
    return table.append_column("vector", pa.array([list(map(float, v)) for v in vecs], pa.list_(pa.float32())))


@pytest.fixture(scope="module")
def built():
    if not HAS_PYMORPHY:
        pytest.skip("pymorphy3 not installed")
    con = synthetic_con()
    concepts = C.build(con, seeds=SEEDS, workers=1, backend="duckdb", communities=False)
    terms, edges = concepts["terms"], concepts["term_edges"]
    fs = formula_symbols_table()
    ctx = TD.make_context(con, terms=terms)
    TD.collect(con, ctx, terms=terms, term_edges=edges, formula_symbols=fs, seeds=NO_SEEDS)
    vectors = synthetic_vectors(ctx)
    stats: dict = {}
    res = TD.build(con, terms=terms, term_edges=edges, formula_symbols=fs, seeds=NO_SEEDS, stats=stats,
                   term_vectors=vectors, nn_min_df=1, nn_min_sources=1)
    return {"con": con, "terms": terms, "edges": edges, "table": res["term_translations"], "stats": stats,
            "vectors": vectors, "fs": fs}


def _find(table, relation, a, b):
    for r in table.to_pylist():
        if r["relation"] == relation and {r["key_a"], r["key_b"]} == {a, b}:
            return r
    return None


# ------------------------------------------------------------------------------------------------ pure helpers
def test_script_language_and_helpers():
    assert TD.script_lang("ползучесть соли") == "ru"
    assert TD.script_lang("salt creep") == "en"
    assert TD.script_lang("Steinsalz über Tage") == "de" and TD.script_lang("Kriechen", "de") == "de"
    assert TD._fix_hyphen_breaks("РАДАР- НАЯ ИНТЕРФЕРОМЕТРИЯ") == "РАДАРНАЯ ИНТЕРФЕРОМЕТРИЯ"
    assert TD._fix_hyphen_breaks("Coulomb- Mohr criterion") == "Coulomb-Mohr criterion"
    assert TD._compress("inter chamber pillar", "en") == TD._compress("interchamber pillar", "en")
    assert TD._compress("levelling", "en") == TD._compress("leveling", "en")
    assert TD._split_keywords("салт, мульда; закладка. Введение Текст статьи") == ["салт", "мульда", "закладка"]
    assert TD.page_of_block("VKM-SRC-001:p0003:b1a2b3") == "VKM-SRC-001:p0003"
    a, b = TD.Phrase("ползучесть", "ru", "ползучесть"), TD.Phrase("creep", "en", "creep")
    assert TD.orient("TRANSLATION", b, a) == (a, b)                         # ru first
    assert TD.pair_id("TRANSLATION", a, b) == TD.pair_id("TRANSLATION", a, b)
    assert TD.pair_id("TRANSLATION", a, b).startswith("TTR-") and len(TD.pair_id("TRANSLATION", a, b)) == 20
    assert TD.method_confidence("CURATED_SEED", 0, 0, 0) == 1.0
    assert TD.method_confidence("XLING_NEIGHBOURS", 0.95, 0, 1) > TD.method_confidence("XLING_NEIGHBOURS", 0.85, 0, 1)


def test_seed_list_is_large_and_well_formed():
    assert len(S.TRANSLATIONS) >= 200 and len({r[0].lower() for r in S.TRANSLATIONS}) >= 200
    for row in S.TRANSLATIONS:
        assert 2 <= len(row) <= 3 and all(x.strip() == x and x for x in row)
        assert TD.script_lang(row[0]) == "ru" and TD.script_lang(row[1]) in ("en", "de")
    for full, abbr, lang in S.ABBREVIATIONS:
        assert lang in ("ru", "en") and full and abbr and len(abbr) < len(full)
    assert all(g[0] in ("ru", "en") and len(g) >= 3 for g in S.SYNONYMS)


def test_registration():
    assert nav_ids.RULE_VERSIONS["translations"] == TD.RULE_VERSION == "term_translations_v1"
    assert "term_translations" in nav_ids.DATASETS
    assert nav_cli.PARTS["translations"] == "vkm_corpus.navigation.term_dictionary:build"
    assert nav_cli.datasets_of("translations") == ("term_translations",)
    for name in ("translate_term", "synonyms", "translate_query"):
        assert nav_store.resolve(name) is getattr(Q, name)


def test_encode_with_injected_encoder():
    class Fake:
        def encode_queries(self, texts):
            return np.stack([np.eye(4, dtype=np.float32)[len(t) % 4] * 2 for t in texts])

    t = pa.table({"lang": ["ru", "en"], "key": ["соль", "salt"], "text": ["соль", "salt"]})
    out = TV.encode(t, models_dir=None, spec_path="unused", encoder=Fake())
    assert out.column_names == ["lang", "key", "text", "vector"] and out.num_rows == 2
    vec = out.column("vector").to_pylist()[0]
    assert len(vec) == 4 and abs(sum(x * x for x in vec) - 1.0) < 1e-6          # L2-normalised
    assert TV.info(out)["dimension"] == 4 and TV.info(out)["prompt"] == TV.PROMPT
    vmap = TD.load_term_vectors(out)
    assert set(vmap) == {("ru", "соль"), ("en", "salt")}


# ------------------------------------------------------------------------------------------------ builder
@needs_pymorphy
def test_build_schema_ids_and_status(built):
    table = built["table"]
    assert table.schema.remove_metadata() == TD.SCHEMA
    rows = table.to_pylist()
    assert rows and len({r["pair_id"] for r in rows}) == len(rows)
    for r in rows:
        assert r["relation"] in TD.RELATIONS and r["status"] in (TD.STATUS_AUTO, TD.STATUS_SEED)
        assert r["term_id_a"] == nav_ids.term_id(r["key_a"]) and r["term_id_b"] == nav_ids.term_id(r["key_b"])
        assert r["rule_version"] == TD.RULE_VERSION and 0.0 <= r["score"] <= 1.0
        assert r["methods"] and set(r["methods"]) <= set(TD.METHODS)
        assert {e["method"] for e in r["evidence"]} == set(r["methods"])        # every method has evidence rows
        if r["relation"] == "TRANSLATION":
            assert r["lang_a"] != r["lang_b"] and TD.LANG_ORDER[r["lang_a"]] < TD.LANG_ORDER[r["lang_b"]]
        if r["relation"] == "SYNONYM":
            assert r["lang_a"] == r["lang_b"]
    assert {r["status"] for r in rows} == {TD.STATUS_AUTO}                    # no seeds in this build
    info = TD.build_info(table)
    assert info["pairs"] == len(rows) and info["rule_version"] == TD.RULE_VERSION and info["vectors"] > 0


@needs_pymorphy
def test_keyword_lists_captions_and_glosses(built):
    t = built["table"]
    kw = _find(t, "TRANSLATION", "ползучесть соль", "salt creep")
    assert kw is not None and "KEYWORD_LISTS" in kw["methods"]
    ev = next(e for e in kw["evidence"] if e["method"] == "KEYWORD_LISTS")
    assert ev["source_id"] == "SYN-A" and ev["page_id"] == "SYN-A:p0001" and ev["other_page_id"] == "SYN-A:p0002"
    assert ev["block_id"].startswith("SYN-A:p0001:") and ev["other_block_id"].startswith("SYN-A:p0002:")
    assert _find(t, "TRANSLATION", "закладка", "backfill") is not None
    assert _find(t, "TRANSLATION", "insar", "insar") is None                 # identical items are anchors, not pairs
    cap = _find(t, "TRANSLATION", "мульда сдвижение", "subsidence trough")
    assert cap is not None and {"KEYWORD_LISTS", "BILINGUAL_CAPTIONS"} <= set(cap["methods"])
    gloss = _find(t, "TRANSLATION", "эффект ракурс", "foreshortening")
    assert gloss is not None and "PAREN_GLOSS" in gloss["methods"]
    assert next(e for e in gloss["evidence"] if e["method"] == "PAREN_GLOSS")["page_id"] == "SYN-A:p0003"


@needs_pymorphy
def test_symbols_neighbours_and_monolingual(built):
    t = built["table"]
    sym = _find(t, "TRANSLATION", "модуль упругость", "elastic modulus")
    assert sym is not None and "SYMBOL_DEFINITIONS" in sym["methods"]
    assert any(e["detail"] == "symbol E" for e in sym["evidence"])
    assert "XLING_NEIGHBOURS" in sym["methods"]                                # mutual neighbours, same concept
    ortho = _find(t, "SYNONYM", "inter chamber pillar", "interchamber pillar")
    assert ortho is not None and ortho["methods"] == ["ORTHOGRAPHIC"]
    pat = _find(t, "SYNONYM", "мульда сдвижение", "мульда оседание")
    assert pat is not None and "SYNONYM_PATTERN" in pat["methods"]
    assert all(not (r["lang_a"] == r["lang_b"] and r["key_a"] == r["key_b"]) for r in t.to_pylist())


@needs_pymorphy
def test_without_vectors_rules_only(built):
    con, terms, edges = built["con"], built["terms"], built["edges"]
    res = TD.build(con, terms=terms, term_edges=edges, seeds=NO_SEEDS)
    t = res["term_translations"]
    methods = {m for ms in t.column("methods").to_pylist() for m in ms}
    assert "XLING_NEIGHBOURS" not in methods and "SYMBOL_DEFINITIONS" not in methods
    kw = _find(t, "TRANSLATION", "ползучесть соль", "salt creep")                 # positional: equal lists
    assert kw is not None and kw["cosine"] is None
    assert TD.build(con, terms=None) is None                                   # SKIPPED_NO_INPUT


@needs_pymorphy
def test_curated_seeds_are_reviewed_by_agent(built):
    con, terms, edges = built["con"], built["terms"], built["edges"]
    seeds = {"translations": (("ползучесть соли", "salt creep"), ("реология", "rheology", "Rheologie")),
             "abbreviations": (("водозащитная толща", "ВЗТ", "ru"),),
             "synonyms": (("ru", "мульда сдвижения", "мульда оседания"),)}
    t = TD.build(con, terms=terms, term_edges=edges, seeds=seeds, term_vectors=built["vectors"])["term_translations"]
    both = _find(t, "TRANSLATION", "ползучесть соль", "salt creep")
    assert both["status"] == TD.STATUS_SEED and both["score"] == 1.0
    assert {"CURATED_SEED", "KEYWORD_LISTS"} <= set(both["methods"])              # corpus evidence kept
    rheo = _find(t, "TRANSLATION", "реология", "rheology")
    assert rheo["status"] == TD.STATUS_SEED and rheo["methods"] == ["CURATED_SEED"] and not rheo["in_terms_a"]
    assert _find(t, "TRANSLATION", "rheology", "rheologie")["lang_b"] == "de"
    abbr = _find(t, "ABBREVIATION", "водозащитный толща", "взт")
    assert abbr is not None and abbr["key_a"] == "водозащитный толща"            # full form first


# ------------------------------------------------------------------------------------------------ queries
@pytest.fixture(scope="module")
def qcon(built):
    con = duckdb.connect()
    con.register("t_in", built["table"])
    con.execute("CREATE TABLE term_translations AS SELECT * FROM t_in")
    con.register("terms_in", built["terms"])
    con.execute("CREATE TABLE terms AS SELECT * FROM terms_in")
    return con


@needs_pymorphy
def test_translate_term_and_synonyms(qcon):
    r = Q.translate_term(qcon, "ползучести соли")                             # inflected form
    assert r["match"]["language"] == "ru" and r["match"]["key"] == "ползучесть соль"
    top = r["translations"][0]
    assert top["lemma"] == "salt creep" and top["language"] == "en" and top["example_page_ids"]
    assert top["status"] == TD.STATUS_AUTO and "KEYWORD_LISTS" in top["methods"]
    back = Q.translate_term(qcon, "Salt creep", target="ru")
    assert back["translations"][0]["key"] == "ползучесть соль"
    by_id = Q.translate_term(qcon, top["term_id"])
    assert by_id["match"]["key"] == "salt creep"
    syn = Q.synonyms(qcon, "мульда оседания")
    assert "мульда сдвижение" in {s["key"] for s in syn["synonyms"]}
    via = Q.translate_term(qcon, "мульда оседания", target="en")               # through its synonym
    assert any(e["lemma"] == "subsidence trough" and e.get("via") for e in via["translations"])
    none = Q.translate_term(qcon, "совершенно неизвестный термин")
    assert none["match"] is None and none["translations"] == [] and "not a reviewed fact" in none["note"]


@needs_pymorphy
def test_translate_query(qcon):
    r = Q.translate_query(qcon, "ползучесть соли и закладка камер", min_score=0.0)
    assert r["source_language"] == "ru" and r["target_language"] == "en"
    assert r["translation"] == "salt creep room backfill" and r["coverage"] == 1.0
    assert [x["span"] for x in r["terms"]] == ["ползучесть соли", "закладка камер"]    # longest spans win
    en = Q.translate_query(qcon, "salt creep near the subsidence trough", min_score=0.0)
    assert en["target_language"] == "ru" and en["translation"].startswith("ползучесть соли")
    low = Q.translate_query(qcon, "ползучесть соли", min_score=1.01)              # nothing trusted enough
    assert low["translation"] is None and low["terms"] == []
    part = Q.translate_query(qcon, "ползучесть соли в неизвестных новых горизонтах", min_score=0.0)
    assert part["translation"] is None and part["coverage"] < Q.EXPANSION_MIN_COVERAGE
    kept = Q.translate_query(qcon, "ползучесть соли по данным InSAR", min_score=0.0, min_coverage=0.0)
    assert "InSAR" in kept["translation"]                                        # language-neutral token kept
    assert json.dumps(r, ensure_ascii=False)                                     # JSON-serialisable
