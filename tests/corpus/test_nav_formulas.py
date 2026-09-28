"""Formula layer of NAV (agent N2): LaTeX symbols, «где» definitions, numbers, references, parameter candidates,
the build over a synthetic canonical snapshot and the query functions. Synthetic rows only."""
from __future__ import annotations

import pytest

pytest.importorskip("pyarrow")
duckdb = pytest.importorskip("duckdb")

from vkm_corpus.navigation import formulas as F  # noqa: E402
from vkm_corpus.navigation import formulas_query as Q  # noqa: E402
from vkm_corpus.navigation import ids as nav_ids  # noqa: E402

SRC = "VKM-SRC-T01"
P1, P2 = f"{SRC}:p0001", f"{SRC}:p0002"


# ------------------------------------------------------------------------------------------------ pure functions
def test_latex_symbols_roles_and_normalisation():
    s = F.latex_symbols(r"\dot{\varepsilon} = A \sigma^{n} \exp(-Q/RT)")
    assert s["ε̇"]["role"] == "LHS"
    assert {k for k, v in s.items() if v["role"] == "RHS"} == {"A", "σ", "n", "Q", "R", "T"}
    assert "σ^n" in s["σ"]["alts"]                                   # a power, n is its own symbol
    assert set(F.latex_symbols(r"\sigma_ {1} + c _ {1 1} ^ {(1)}")) == {"σ_1", "c_11^(1)"}
    assert set(F.latex_symbols(r"\frac{d z}{d t} = f _ {1}")) == {"z", "t", "f_1"}    # differentials dropped
    assert set(F.latex_symbols(r"\dot {\varepsilon} ^ {c} + \bar {\varepsilon} _ {p l} ^ {f}")) == {"ε̇^c", "ε̄_pl^f"}
    assert set(F.latex_symbols(r"Q_{\mathrm{ВМП}} = V_{\mathrm{ЗОГ}} / t")) == {"Q_ВМП", "V_ЗОГ", "t"}
    assert set(F.latex_symbols(r"V = 4 3 2 9. 5 \mathrm {m / s} + e ^ {- t / T _ {1}}")) == {"V", "t", "T_1"}
    two = F.latex_symbols(r"\left\{ \begin{array}{l l} x = 1 \\ y = 2 z \end{array} \right.")
    assert (two["x"]["role"], two["y"]["role"], two["z"]["role"]) == ("LHS", "LHS", "RHS")
    assert F.latex_symbols(r"P _ {1} < P < P ^ {*}")["P"]["role"] == "UNKNOWN"


def test_symbols_match_across_latex_and_unicode():
    assert F.plain_symbol("σ1") == "σ_1" == F.latex_symbol_list(r"\sigma_{1}")[0]
    assert F.plain_symbol("Qвмп") == "Q_вмп"
    assert F.plain_symbol("\U0001d444\U0001d457") == "Q_j"                  # mathematical italic Q j
    assert F.symbol_key("Н") == F.symbol_key("H")                         # Cyrillic look-alike base
    assert F.symbol_key("ε_п", loose=True) == F.symbol_key("ε_π", loose=True)
    assert F.symbol_key("ε", loose=True) != F.symbol_key("e", loose=True)  # Greek bases are never folded


def test_where_definitions_ocr_native_english():
    d = F.parse_definitions(r"где $\sigma$ — напряжение, МПа; $\varepsilon$ — деформация; $P, p$ — силы")
    assert [(x.symbols, x.description, x.unit) for x in d] == [
        (["σ"], "напряжение", "МПа"), (["ε"], "деформация", None), (["P", "p"], "силы", None)]
    native = F.parse_definitions(" \nгде \nВМП\nQ\n — производительность вентилятора, м3/с; а - ширина камеры, м")
    assert [(x.symbols, x.unit) for x in native] == [(["Q_ВМП"], "м3/с"), (["а"], "м")]
    clause = F.parse_definitions("где r — радиус точки О, a v — их скорости. Рассмотрим далее.")
    assert [x.symbols for x in clause] == [["r"], ["v"]] and clause[1].description == "их скорости"
    en = F.parse_definitions("where σ is the stress (MPa), ε is the strain, and E is Young's modulus")
    assert [(x.symbols, x.description, x.unit) for x in en] == [
        (["σ"], "stress", "MPa"), (["ε"], "strain", None), (["E"], "Young's modulus", None)]
    assert F.parse_definitions("где функция опорного напряжения принимает вид") == []
    assert F.parse_definitions("where σ is given by Eq. (1.20)") == []                # a pointer, not a meaning


def test_numbers_printed_and_latex():
    assert F.parse_number_text("(3.2)") == ("3.2", "(3.2)")
    assert F.parse_number_text("(5-99)")[0] == "5.99"
    assert F.parse_number_text("ð1:9Þ")[0] == "1.9"                               # Springer text-layer font
    assert F.parse_number_text("(2.1а)")[0] == "2.1a"
    assert F.parse_number_text("2", parens=True) is None and F.parse_number_text("0),", parens=True) is None
    assert F.parse_number_text("σ = Eε, (3.5)", trailing=True, parens=True)[0] == "3.5"
    assert F.latex_number(r"\sigma = E \varepsilon \tag {3.2}")[0] == "3.2"
    assert F.latex_number(r"\sigma = E \varepsilon \qquad (3 . 2)")[0] == "3.2"


def test_reference_mentions_and_types():
    def refs(t):
        return [(r.key, r.ref_type) for r in F.find_ref_mentions(t)]

    assert refs("Подставляя (2.1) в (2.4), получим") == [("2.1", "SUBSTITUTION"), ("2.4", "SUBSTITUTION")]
    assert refs("по формуле (3.2) определяем") == [("3.2", "MENTION")]
    assert refs("из (3.5) следует, что") == [("3.5", "DERIVATION_HINT")]
    assert refs("(см. формулу 4.1)") == [("4.1", "MENTION")]
    assert refs("Substitution of Eq. (6.2) into Eq. (6.3) yields") == [("6.2", "SUBSTITUTION"),
                                                                        ("6.3", "SUBSTITUTION")]
    assert refs("в формулах (19.1) и (19.7) явно") == [("19.1", "MENTION"), ("19.7", "MENTION")]
    for noise in ("Block (2017) and Bock (2020)", "(13) Climate Action, (14) Life", "J = E σ , (7.30)",
                  "(3.64)", "мощность (10–15) м в среднем"):
        assert refs(noise) == [], noise


def test_parameter_candidates():
    def pars(t):
        return [(p.symbol, p.value, p.value_min, p.value_max, p.unit) for p in F.parse_parameters(t)]

    assert pars("n = 4,5") == [("n", 4.5, None, None, None)]
    assert pars("A = 2·10⁻⁵ 1/сут") == [("A", 2e-05, None, None, "1/сут")]
    assert pars(r"при $A = 2 \cdot 10^{-5}$ 1/сут и $E=18$ ГПа") == [("A", 2e-05, None, None, "1/сут"),
                                                                    ("E", 18.0, None, None, "ГПа")]
    assert pars("σсж = 25 МПа; ν = 0,25–0,3") == [("σ_сж", 25.0, None, None, "МПа"), ("ν", None, 0.25, 0.3, None)]
    for noise in ("x = 2y", "t = 0", "α = 1", "n = 1, 2, …, N", "j = 1 or 2", "(i = 1,4)", "i = 3",
                  "где $ W_{i} = 2 V_{i} / t $", r"$\xi = \frac {1}{2}$"):
        assert pars(noise) == [], noise


# ------------------------------------------------------------------------------------------------ build
def _canon() -> duckdb.DuckDBPyConnection:
    con = duckdb.connect()
    con.execute("CREATE SCHEMA canonical")
    con.execute("CREATE TABLE canonical.pages (page_id VARCHAR, page_index INTEGER)")
    con.execute("""CREATE TABLE canonical.formulas (object_id VARCHAR, source_id VARCHAR, page_id VARCHAR,
        formula_kind VARCHAR, equation_label VARCHAR, normalized_latex VARCHAR, raw_output VARCHAR,
        bbox_x0 DOUBLE, bbox_y0 DOUBLE, bbox_x1 DOUBLE, bbox_y1 DOUBLE, bbox_space VARCHAR)""")
    con.execute("""CREATE TABLE canonical.blocks (object_id VARCHAR, source_id VARCHAR, page_id VARCHAR,
        is_primary_layer BOOLEAN, block_type VARCHAR, reading_order INTEGER, bbox_x0 DOUBLE, bbox_y0 DOUBLE,
        bbox_x1 DOUBLE, bbox_y1 DOUBLE, bbox_space VARCHAR, text VARCHAR)""")
    con.executemany("INSERT INTO canonical.pages VALUES (?, ?)", [(P1, 1), (P2, 2)])
    fml = [
        (f"{P1}:mf1", SRC, P1, "DISPLAY", None, r"\dot{\varepsilon} = A \sigma^{n}", None, 100, 90, 300, 110),
        (f"{P1}:mf2", SRC, P1, "INLINE", None, r"\dot{\varepsilon}", None, 62, 124, 80, 134),
        (f"{P2}:mf3", SRC, P2, "DISPLAY", None, r"\varepsilon = \varepsilon_{0} + A \sigma^{n} t \tag {3.6}", None,
         100, 90, 300, 110),
        (f"{P2}:mf4", SRC, P2, "DISPLAY", None, r"\sigma = E \varepsilon", None, 100, 150, 300, 170),
    ]
    con.executemany("INSERT INTO canonical.formulas VALUES (?,?,?,?,?,?,?,?,?,?,?, 'PAGE_PT_TL')", fml)
    blk = [
        (f"{P1}:b1", P1, "TEXT", 1, 50, 50, 460, 80, "Скорость ползучести описывается степенным законом"),
        (f"{P1}:b2", P1, "FORMULA_NUMBER", 2, 420, 92, 450, 106, "(3.2)"),
        (f"{P1}:b3", P1, "TEXT", 3, 50, 120, 460, 160,
         "где $\\dot{\\varepsilon}$ — скорость установившейся ползучести, 1/сут; $\\sigma$ — напряжение, МПа; "
         "$A$, $n$ — параметры материала; $n = 4,5$; $A = 2 \\cdot 10^{-5}$ 1/сут."),
        (f"{P1}:b9", P1, "PAGE_NUMBER", 4, 250, 700, 260, 710, "1"),
        (f"{P2}:b4", P2, "TEXT", 1, 50, 50, 460, 80, "Подставляя (3.2) в (3.5), получим"),
        (f"{P2}:b5", P2, "TEXT", 2, 50, 120, 460, 140, "Здесь время отсчитывается от начала нагружения."),
        (f"{P2}:b6", P2, "TEXT", 3, 100, 150, 460, 170, "σ = Eε, (3.5)"),
        (f"{P2}:b7", P2, "TEXT", 4, 50, 180, 460, 200, "По формуле (3.6) находим деформацию; E = 18 ГПа."),
    ]
    con.executemany("INSERT INTO canonical.blocks VALUES (?, 'VKM-SRC-T01', ?, true, ?, ?, ?, ?, ?, ?, "
                    "'PAGE_PT_TL', ?)", blk)
    return con


@pytest.fixture(scope="module")
def built():
    con = _canon()
    sp = [{"section_id": "SEC-a", "page_id": P1}, {"section_id": "SEC-b", "page_id": P2}]
    tables = F.build(con, section_pages=sp)
    return con, tables


def _by(table, key):
    return {r[key]: r for r in table.to_pylist()}


def test_build_context_numbers_where_sections(built):
    _, t = built
    ctx = _by(t["formula_context"], "formula_id")
    f1, f2, f3, f4 = (ctx[f"{P1}:mf1"], ctx[f"{P1}:mf2"], ctx[f"{P2}:mf3"], ctx[f"{P2}:mf4"])
    assert (f1["equation_number"], f1["number_method"], f1["number_block_id"]) == ("3.2", "FORMULA_NUMBER_BLOCK",
                                                                                  f"{P1}:b2")
    assert (f3["equation_number"], f3["number_method"]) == ("3.6", "LATEX_TAG")
    assert (f4["equation_number"], f4["number_method"]) == ("3.5", "TEXT_LAYER_NUMBER")
    assert f1["intro_block_id"] == f"{P1}:b1" and f1["where_block_ids"] == [f"{P1}:b3"]
    assert f3["intro_block_id"] == f"{P2}:b4" and f3["where_block_ids"] == []      # «Здесь» without definitions
    assert f4["next_block_id"] == f"{P2}:b7"                                     # the text-layer line b6 is skipped
    assert f2["kind"] == "INLINE" and f2["host_block_id"] == f"{P1}:b3"
    assert (f1["section_id"], f4["section_id"]) == ("SEC-a", "SEC-b")
    assert {r["rule_version"] for r in t["formula_context"].to_pylist()} == {nav_ids.RULE_VERSIONS["formulas"]}
    assert {r["review_status"] for r in t["formula_context"].to_pylist()} == {"AUTO_EXTRACTED_UNREVIEWED"}


def test_build_symbols_definitions(built):
    _, t = built
    rows = [r for r in t["formula_symbols"].to_pylist() if r["formula_id"] == f"{P1}:mf1"]
    by = {r["symbol"]: r for r in rows}
    assert by["ε̇"]["role"] == "LHS" and by["ε̇"]["definition"] == "скорость установившейся ползучести"
    assert by["ε̇"]["unit"] == "1/сут" and by["σ"]["unit"] == "МПа" and by["σ"]["role"] == "RHS"
    assert by["A"]["definition"] == by["n"]["definition"] == "параметры материала"
    assert by["σ"]["symbol_id"] == nav_ids.symbol_id(SRC, "σ") and by["σ"]["definition_block_id"] == f"{P1}:b3"
    assert all(r["in_formula"] for r in rows)


def test_build_refs_and_parameters(built):
    _, t = built
    refs = {(r["block_id"], r["formula_id"]): r for r in t["formula_refs"].to_pylist()}
    sub1 = refs[(f"{P2}:b4", f"{P1}:mf1")]
    sub2 = refs[(f"{P2}:b4", f"{P2}:mf4")]
    assert sub1["ref_type"] == sub2["ref_type"] == "SUBSTITUTION"
    assert sub1["citing_formula_id"] == f"{P2}:mf3"                         # «подставляя … получим» + formula
    assert sub1["ref_id"] == nav_ids.formula_ref_id(f"{P2}:b4", f"{P1}:mf1")
    assert refs[(f"{P2}:b7", f"{P2}:mf3")]["ref_type"] == "DERIVATION_HINT"
    assert len(refs) == 3
    par = {(r["formula_id"], r["symbol"]): r for r in t["formula_parameters"].to_pylist()}
    assert par[(f"{P1}:mf1", "n")]["value"] == 4.5 and par[(f"{P1}:mf1", "n")]["context_kind"] == "WHERE_BLOCK"
    assert par[(f"{P1}:mf1", "A")]["unit"] == "1/сут" and par[(f"{P1}:mf1", "A")]["in_formula"]
    assert par[(f"{P2}:mf4", "E")]["value"] == 18.0 and par[(f"{P2}:mf4", "E")]["unit"] == "ГПа"


def test_build_is_deterministic(built):
    con, t = built
    again = F.build(con, section_pages=[{"section_id": "SEC-a", "page_id": P1},
                                        {"section_id": "SEC-b", "page_id": P2}])
    for name in t:
        assert again[name].equals(t[name]), name
    s = F.summarize(t)
    assert s["display_with_number"] == 3 and s["refs"] == 3 and s["symbols_with_definition"] == 4


def test_query_functions(built):
    con, t = built
    Q.register_tables(con, t)
    ctx = Q.get_formula_context(con, f"{P1}:mf1")
    assert ctx["equation_number"] == "3.2" and ctx["latex"].startswith(r"\dot")
    assert ctx["intro"]["text"].startswith("Скорость") and ctx["where"][0]["block_id"] == f"{P1}:b3"
    assert {r["symbol"] for r in ctx["symbols"] if r["definition"]} == {"ε̇", "σ", "A", "n"}
    assert {r["block_id"] for r in ctx["referenced_by"]} == {f"{P2}:b4"}
    assert {r["symbol"] for r in ctx["parameters"]} == {"n", "A"}
    f3 = Q.get_formula_context(con, f"{P2}:mf3")
    assert {r["equation_number"] for r in f3["refers_to"]} == {"3.2", "3.5"}      # «подставляя (3.2) в (3.5)»
    assert Q.get_formula_context(con, "nope") is None
    hits = Q.find_formulas(con, concept="скорость ползучести")
    assert [(h["formula_id"], h["symbol"]) for h in hits] == [(f"{P1}:mf1", "ε̇")]
    by_sym = Q.find_formulas(con, symbol=r"\sigma", source_id=SRC)
    assert {h["formula_id"] for h in by_sym} == {f"{P1}:mf1", f"{P2}:mf3", f"{P2}:mf4"}
    with pytest.raises(ValueError):
        Q.find_formulas(con, symbol="σ")                                         # never a bare symbol across books
    with pytest.raises(ValueError):
        Q.find_formulas(con)
