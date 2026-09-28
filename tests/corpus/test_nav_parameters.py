"""Parameter candidates of NAV (agent P): numbers and units, text and table rules, source symbol definitions, joined
line-blocks, the build over a synthetic canonical snapshot, the CLI part and the query functions. Synthetic rows only
(our own sentences, no corpus text)."""
from __future__ import annotations

import math

import pytest

pa = pytest.importorskip("pyarrow")
duckdb = pytest.importorskip("duckdb")

from vkm_corpus.navigation import cli as nav_cli  # noqa: E402
from vkm_corpus.navigation import ids as nav_ids  # noqa: E402
from vkm_corpus.navigation import parameters as P  # noqa: E402
from vkm_corpus.navigation import parameters_query as Q  # noqa: E402
from vkm_corpus.navigation import parameters_vocab as V  # noqa: E402
from vkm_corpus.navigation import store  # noqa: E402

SRC, SRC2 = "VKM-SRC-T11", "VKM-SRC-T12"
P1, P2, P3, Q1 = f"{SRC}:p0001", f"{SRC}:p0002", f"{SRC}:p0003", f"{SRC2}:p0001"


def _one(text, lang="ru"):
    cands, _plain, _map = P.text_candidates(text, lang)
    return cands


def _cell(r, c, text, rs=1, cs=1):
    return {"row": r, "col": c, "row_span": rs, "col_span": cs, "is_header": False, "text": text}


# ------------------------------------------------------------------------------------------------ numbers and units
def test_numbers_ranges_powers_and_units():
    def vals(text):
        return [(v.vmin, v.vmax, v.qualifier, v.unit.canon if v.unit else None, v.reject)
                for v in P.scan_values(P.plain_with_map(text)[0])]

    assert vals("равен 2,5 МПа") == [(2.5, 2.5, "=", "МПа", None)]
    assert vals("от 10 до 15 ГПа") == [(10.0, 15.0, "range", "ГПа", None)]
    assert vals("0,5–1,5 ГПа")[0][:3] == (0.5, 1.5, "range")
    assert vals("25,8 ± 3,1 МПа")[0][2] == "±"
    (v,) = P.scan_values("2·10^-5 1/сут")
    assert math.isclose(v.vmin, 2e-5) and v.unit.dim == V.STRAIN_RATE
    assert math.isclose(P.scan_values("0,9 Ч 104 МПа")[0].vmin, 9000.0)       # a lost superscript: ×10⁴
    assert math.isclose(P.scan_values("1–5·10^-5")[0].vmin, 1e-5)               # a shared power
    assert math.isclose(P.scan_values(P.plain_with_map("2·10⁻⁵")[0])[0].vmin, 2e-5)
    assert vals("в 1985 г.")[0][4] == "year"
    assert vals("50×50×50 мм")[0][4] == "tuple"
    assert vals("в 2 раза")[0][4] == "times"
    assert vals("(3)")[0][4] == "enumerator"
    a = P.scan_values("66°13′")[0]
    assert math.isclose(a.vmin, 66 + 13 / 60) and a.unit.dim == V.ANGLE
    lst = P.scan_values("20, 15 и 10 ГПа")
    assert [x.unit.canon for x in lst] == ["ГПа"] * 3 and lst[0].unit_inherited and lst[0].list_id == lst[2].list_id
    si = P.value_si(1.0, 1.0, None, P.match_unit("кгс/см2", 0))
    assert math.isclose(si[0], 98066.5)
    assert P.match_unit("т/м3", 0).dim == V.DENSITY and P.match_unit("кН/м³", 0).dim == V.WEIGHT
    assert P.match_unit("мм/год", 0).dim == V.VELOCITY and P.match_unit("мм в год", 0).dim == V.VELOCITY
    assert P.match_unit("пачка", 0) is None                                  # «па…» inside a word is no unit
    assert P.match_unit("с", 0) is None                                       # a bare «с» is never a time unit


# ------------------------------------------------------------------------------------------------ text rules
def test_text_value_property_material_scale_site():
    c = _one("Модуль упругости каменной соли составляет 20 ГПа, сильвинита — 15 ГПа.")
    assert [(x.prop.key, x.val.vmin, x.unit.canon, x.material) for x in c] == [
        ("youngs_modulus", 20.0, "ГПа", "каменная соль"), ("youngs_modulus", 15.0, "ГПа", "сильвинит")]
    c = _one("Предел прочности на одноосное сжатие образцов карналлита СКРУ-1 изменяется от 18 до 32 МПа.")
    assert (c[0].prop.key, c[0].qualifier, c[0].scale, c[0].site, c[0].material) == (
        "ucs", "range", "LAB", "СКРУ-1", "карналлит")
    c = _one("Угол внутреннего трения и сцепление соответственно φ = 32°; С = 2,2 МПа.")
    assert [(x.prop.key, x.symbol) for x in c] == [("friction_angle", "φ"), ("cohesion", "С")]
    c = _one("Модуль упругости каменной соли и сильвинита соответственно 20 и 15 ГПа.")
    assert [x.material for x in c] == ["каменная соль", "сильвинит"] and "RESPECTIVE_ORDER" in c[0].flags
    c = _one("Нормативные значения модуля деформации соли 0,5–1,5 ГПа.")
    assert (c[0].prop.key, c[0].qualifier, c[0].scale) == ("deformation_modulus", "range", "NORMATIVE")
    c = _one("Коэффициент бокового распора составляет 0,71; средний предел прочности соли равен 25 МПа.")
    assert [(x.prop.key, x.val.vmin, x.qualifier) for x in c] == [("lateral_pressure_coefficient", 0.71, "="),
                                                                  ("strength_unspecified", 25.0, "mean")]
    c = _one("Скорость установившейся ползучести составила 2·10-5 1/сут на образцах.")
    assert c[0].prop.key == "creep_rate" and math.isclose(c[0].val.vmin, 2e-5) and c[0].scale == "LAB"
    c = _one("Сцепление, МПа 10,5 5,7 22,5")
    assert [x.val.vmin for x in c] == [10.5, 5.7, 22.5] and all("SERIES" in x.flags for x in c)
    c = _one("The Young's modulus of rock salt is 30 GPa in laboratory tests.", "en")
    assert (c[0].prop.key, c[0].material, c[0].scale) == ("youngs_modulus", "каменная соль", "LAB")
    c = _one("Скорость оседания в 2013 г. составляла 450–500 мм/год (Старобинское месторождение).")
    assert c[0].prop.key == "subsidence_rate" and c[0].site == "ANALOGUE:Старобинское"


def test_text_rejections():
    assert _one("Модуль упругости снижается на 20 % при увлажнении.") == []            # a relative change
    assert _one("Плотность отражателей увеличилась в 100–200 раз.") == []
    assert not [x for x in _one("Модуль деформации при нагрузке 5 МПа составил 2 ГПа.") if x.val.vmin == 5.0]
    assert _one("Коэффициент Пуассона (рис. 3) показан выше.") == []                     # a reference
    assert _one("Степень нагружения целиков (1) определяется по формуле.") == []           # an enumerator
    assert _one("Porosity, % 45 30 15 0 -15") == []                                       # axis ticks
    assert _one("Мощность двигателя 132 кВт.") == []                                       # power, not thickness
    c = _one("Chain pillars at a depth of 700 m were 8 m wide and 3 m high.", "en")
    assert [(x.prop.key, x.val.vmin) for x in c] == [("depth", 700.0)]
    assert _one("Коэффициент закладки должен быть не менее 0,7.")[0].qualifier == "≥"
    assert P.text_candidates(None)[0] == []


def test_symbol_definitions_and_source_table():
    defs = []
    P.text_candidates("где Dпр — предельный модуль деформации; модуль спада (М, ГПа) и σсж = 25 МПа", "ru",
                      definitions=defs)
    got = {(P._hint_key(s), p) for s, p in defs}
    assert (P._hint_key("Dпр"), "deformation_modulus") in got and (P._hint_key("М"), "softening_modulus") in got
    table = P.source_symbol_table({SRC: {P._hint_key("Dпр"): {"deformation_modulus": 1},
                                         "a": {"room_width": 1},
                                         P._hint_key("E"): {"youngs_modulus": 1, "deformation_modulus": 1}}})
    assert table == {SRC: {P._hint_key("Dпр"): "deformation_modulus"}}   # one-letter needs 2, conflicts dropped


# ------------------------------------------------------------------------------------------------ tables
def test_table_headers_units_stats_symbols():
    cells = [_cell(0, 0, "Порода"), _cell(0, 1, "Модуль деформации, ГПа", cs=2), _cell(0, 3, "ν"),
             _cell(1, 1, "min"), _cell(1, 2, "max"),
             _cell(2, 0, "Каменная соль"), _cell(2, 1, "0,07"), _cell(2, 2, "5,21"), _cell(2, 3, "0,3"),
             _cell(3, 0, "Сильвинит"), _cell(3, 1, "0,33"), _cell(3, 2, "3,71"), _cell(3, 3, "0,25")]
    got = P.table_candidates(cells, 4, 4, "Свойства соляных пород (лабораторные испытания)", "Таблица 1")
    rows = {(r, c): x for x, r, c in got}
    assert (rows[(2, 1)].prop.key, rows[(2, 1)].qualifier, rows[(2, 1)].unit.canon) == ("deformation_modulus",
                                                                                        "min", "ГПа")
    assert rows[(3, 3)].prop.key == "poisson_ratio" and rows[(3, 3)].material == "сильвинит"
    assert all(x.scale == "LAB" for x, _, _ in got)
    mult = P.table_candidates([_cell(0, 0, "Порода"), _cell(0, 1, "E·10⁻³, МПа модуль упругости"),
                               _cell(1, 0, "Сильвинит"), _cell(1, 1, "13,79")], 2, 2)
    rec = P._cand_record(mult[0][0], "TABLE")
    assert rec["value_min"] == 13.79 and rec["value_si_min"] is None and "HEADER_MULTIPLIER" in rec["flags"]


def test_table_axis_rows_groups_conflicts_and_one_quantity():
    # a two-way table: the header over an axis row names the axis, the body is another quantity
    axis = [_cell(0, 0, "q", rs=2), _cell(0, 1, "Вязкость η, Па·с", cs=3), _cell(1, 1, "0,3"), _cell(1, 2, "0,4"),
            _cell(1, 3, "0,5"), _cell(2, 0, "30"), _cell(2, 1, "0,20"), _cell(2, 2, "0,21"), _cell(2, 3, "0,22")]
    assert P.table_candidates(axis, 3, 4) == []
    # a group row gives the material of the rows below it
    grp = [_cell(0, 0, "Размер, мм"), _cell(0, 1, "σсж, МПа"), _cell(1, 0, "Красный сильвинит", cs=2),
           _cell(2, 0, "20"), _cell(2, 1, "26,1")]
    (x, r, c), = P.table_candidates(grp, 3, 2)
    assert (x.prop.key, x.material, r, c) == ("ucs", "сильвинит", 2, 1) and "MATERIAL_FROM_GROUP_ROW" in x.flags
    # an OCR-shifted header («… деформация» over «v») is ambiguous: nothing is read from that column
    shift = [_cell(0, 0, "Порода"), _cell(0, 1, "Относительная предельная деформация"), _cell(1, 1, "v"),
             _cell(2, 0, "Каменная соль"), _cell(2, 1, "0,25")]
    assert P.table_candidates(shift, 3, 2) == []
    # a table of one quantity named in the caption
    one = [_cell(0, 0, "Порода"), _cell(0, 1, "Кол-во образцов"), _cell(0, 2, "Прямой метод"),
           _cell(1, 0, "Карналлит"), _cell(1, 1, "15"), _cell(1, 2, "0,20-0,25")]
    got = P.table_candidates(one, 2, 3, "Коэффициент длительной прочности пород")
    assert [(x.prop.key, x.val.vmin, x.val.vmax, c) for x, _, c in got] == [
        ("long_term_strength_ratio", 0.2, 0.25, 2)]
    # a symbol the source defines
    src_syms = {P._hint_key("Dпр"): "deformation_modulus"}
    got = P.table_candidates([_cell(0, 0, "Порода"), _cell(0, 1, "Dпр, ГПа"), _cell(1, 0, "Сильвинит"),
                              _cell(1, 1, "0,50")], 2, 2, source_symbols=src_syms)
    assert got[0][0].prop.key == "deformation_modulus" and "SYMBOL_DEFINED_IN_SOURCE" in got[0][0].flags


# ------------------------------------------------------------------------------------------------ line-blocks
def test_join_runs_and_locators():
    text, segs = P.join_run(["средняя скорость нарастания оседаний соста-", "вила 29-35 мм/год, а на линии 2"])
    assert "составила 29-35" in text
    blocks = [("b1", SRC, P1, 1, 1, 10.0, "ru", "Мощность пласта", "TEXT", None, None, None, None),
              ("b2", SRC, P1, 1, 2, 20.0, "ru", "составляет 3,5 м.", "TEXT", None, None, None, None),
              ("b3", SRC, P1, 1, 3, 30.0, "ru", "Новый абзац 7 м", "TEXT", None, None, None, None)]
    runs = P.make_runs(blocks)
    assert [[b[0] for b in r] for r in runs] == [["b1", "b2"], ["b3"]]
    out, defs = P._text_batch(([[(b[0], b[7], b[6]) for b in runs[0]]], 150))
    (rec,) = out
    assert rec["block_id"] == "b2" and "составляет 3,5 м."[rec["char_start"]:rec["char_end"]] == "3,5 м"
    assert "CROSS_BLOCK" in rec["flags"] and rec["property_key"] == "seam_thickness"


# ------------------------------------------------------------------------------------------------ build
def _canon() -> duckdb.DuckDBPyConnection:
    con = duckdb.connect()
    con.execute("CREATE SCHEMA canonical")
    con.execute("CREATE TABLE canonical.pages (page_id VARCHAR, source_id VARCHAR, page_index INTEGER)")
    con.execute("""CREATE TABLE canonical.blocks (object_id VARCHAR, source_id VARCHAR, page_id VARCHAR,
        is_primary_layer BOOLEAN, block_type VARCHAR, reading_order INTEGER, bbox_x0 DOUBLE, bbox_y0 DOUBLE,
        bbox_x1 DOUBLE, bbox_y1 DOUBLE, bbox_space VARCHAR, language VARCHAR, normalized_text VARCHAR)""")
    con.execute("""CREATE TABLE canonical.tables (object_id VARCHAR, source_id VARCHAR, page_id VARCHAR,
        table_label VARCHAR, caption VARCHAR, n_rows INTEGER, n_cols INTEGER, header_rows SMALLINT,
        cells STRUCT("row" INTEGER, col INTEGER, row_span SMALLINT, col_span SMALLINT, is_header BOOLEAN,
        "text" VARCHAR)[], bbox_y0 DOUBLE)""")
    con.execute("CREATE TABLE canonical.figures (page_id VARCHAR, bbox_x0 DOUBLE, bbox_y0 DOUBLE, bbox_x1 DOUBLE, "
                "bbox_y1 DOUBLE, bbox_space VARCHAR)")
    con.execute("CREATE TABLE canonical.sources (source_id VARCHAR, site_scope VARCHAR[], source_class_raw VARCHAR)")
    con.executemany("INSERT INTO canonical.pages VALUES (?, ?, ?)",
                    [(P1, SRC, 1), (P2, SRC, 2), (P3, SRC, 3), (Q1, SRC2, 1)])
    con.executemany("INSERT INTO canonical.sources VALUES (?, ?, ?)",
                    [(SRC, ["VKM_REGIONAL"], "monograph"), (SRC2, ["VKM_REGIONAL"], "normative_document")])
    blk = [
        (f"{P1}:b1", SRC, P1, "HEADING", 1, 50, 40, 400, 50, "Глава 2. Свойства пород"),
        (f"{P1}:b2", SRC, P1, "TEXT", 2, 50, 60, 400, 90,
         "По результатам испытаний образцов модуль упругости каменной соли составляет 20 ГПа."),
        (f"{P1}:b3", SRC, P1, "TEXT", 3, 50, 100, 400, 120, "где Dпр — предельный модуль деформации;"),
        (f"{P1}:b4", SRC, P1, "TEXT", 4, 120, 210, 150, 220, "0 15 30 45"),          # inside a figure
        (f"{P2}:b1", SRC, P2, "TEXT", 1, 50, 60, 400, 90, "Предельный модуль деформации Dпр равен 0,50 ГПа."),
        (f"{P3}:b1", SRC, P3, "TEXT", 1, 50, 60, 400, 90, "Ширина камеры на СКРУ-2 составляет 16 м."),
        (f"{Q1}:b1", SRC2, Q1, "TEXT", 1, 50, 60, 400, 90, "Степень нагружения междукамерных целиков 0,4."),
    ]
    con.executemany("INSERT INTO canonical.blocks VALUES (?, ?, ?, true, ?, ?, ?, ?, ?, ?, 'PAGE_PT_TL', 'ru', ?)",
                    blk)
    con.execute("INSERT INTO canonical.figures VALUES (?, 100, 200, 300, 300, 'PAGE_PT_TL')", [P1])
    cells = [_cell(0, 0, "Порода"), _cell(0, 1, "Dпр, ГПа"), _cell(0, 2, "Модуль упругости, ГПа"),
             _cell(1, 0, "Каменная соль"), _cell(1, 1, "0,50"), _cell(1, 2, "20")]
    con.execute("INSERT INTO canonical.tables VALUES (?, ?, ?, 'Таблица 2.1', 'Свойства пород', 2, 3, NULL, ?, 400)",
                [f"{P2}:t1", SRC, P2, cells])
    return con


@pytest.fixture(scope="module")
def built():
    con = _canon()
    sp = [{"section_id": "SEC-a", "page_id": P1}, {"section_id": "SEC-a", "page_id": P2},
          {"section_id": "SEC-b", "page_id": P3}]
    stats: dict = {}
    tables = P.build(con, section_pages=sp, sections=[], stats=stats, workers=1)
    return con, tables, stats


def test_build_candidates_locators_sections_and_ids(built):
    _con, tables, stats = built
    assert tables["parameter_candidates"].schema.names == list(P.CANDIDATE_COLUMNS)
    rows = tables["parameter_candidates"].to_pylist()
    by = {(r["page_id"], r["property_key"], r["method"]): r for r in rows}
    t = by[(P1, "youngs_modulus", "TEXT")]
    assert (t["material"], t["scale_hint"], t["unit_si"], t["value_si_min"]) == ("каменная соль", "LAB", "Pa", 20e9)
    text = "По результатам испытаний образцов модуль упругости каменной соли составляет 20 ГПа."
    assert text[t["char_start"]:t["char_end"]] == "20 ГПа" and t["section_id"] == "SEC-a"
    tab = by[(P2, "deformation_modulus", "TABLE")]
    assert "SYMBOL_DEFINED_IN_SOURCE" in tab["flags"] and (tab["table_row"], tab["table_col"]) == (1, 1)
    assert "ALSO_TEXT" in tab["flags"]                                   # the same value in the text was merged
    assert by[(P3, "room_width", "TEXT")]["site_hint"] == "СКРУ-2"
    norm = by[(Q1, "loading_degree", "TEXT")]
    assert (norm["scale_hint"], norm["scale_basis"]) == ("NORMATIVE", "SOURCE_CLASS")
    assert not [r for r in rows if r["block_id"] == f"{P1}:b4"]          # numbers inside a figure are skipped
    assert all(r["candidate_id"].startswith("PRM-") and r["review_status"] == P.REVIEW_STATUS for r in rows)
    assert stats["counters"]["candidates"] == len(rows) and stats["rule_version"] == "parameters_v1"
    again = P.build(_canon(), section_pages=[], sections=[], workers=1)["parameter_candidates"].to_pylist()
    assert sorted(r["candidate_id"] for r in again) == sorted(r["candidate_id"] for r in rows)
    summ = tables["parameter_summary"].to_pylist()
    s = [x for x in summ if x["property_key"] == "youngs_modulus" and x["material"] == "каменная соль"]
    assert s and s[0]["n_candidates"] >= 1 and s[0]["note"] == P.SUMMARY_NOTE
    info = P.summarize(tables)
    assert info["candidates"] == len(rows) and "TABLE" in info["by_method"]


def test_cli_part_and_inputs(tmp_path, built):
    assert list(nav_cli.PARTS).index("parameters") == list(nav_cli.PARTS).index("formulas") + 1
    assert nav_ids.RULE_VERSIONS["parameters"] == "parameters_v1"
    assert {"parameter_candidates", "parameter_summary"} <= set(nav_ids.DATASETS)
    assert nav_ids.parameter_candidate_id("b", "3", "ucs", "25") == nav_ids.parameter_candidate_id("b", "3", "ucs",
                                                                                                    "25")
    import pyarrow.parquet as pq

    inputs = tmp_path / "earlier"
    inputs.mkdir()
    pq.write_table(pa.table({"section_id": ["SEC-z"], "page_id": [P3]}), inputs / "section_pages.parquet")
    loaded, refs = nav_cli.load_inputs(inputs, set())
    assert set(loaded) == {"section_pages"} and refs["section_pages"]["rows"] == 1
    con = _canon()
    manifest = nav_cli.build_parts(con, tmp_path / "out", ["parameters"], inputs=loaded, inputs_ref=refs)
    assert manifest["parts"]["parameters"]["status"] == "BUILT" and manifest["inputs"] == refs
    rows = pq.read_table(tmp_path / "out" / "parameter_candidates.parquet").to_pylist()
    assert {r["section_id"] for r in rows if r["page_id"] == P3} == {"SEC-z"}


# ------------------------------------------------------------------------------------------------ queries
def test_query_functions(built):
    _con, tables, _stats = built
    assert Q.resolve_property("модуля деформации") == ["deformation_modulus"]
    assert Q.resolve_property("ucs") == ["ucs"] and Q.resolve_property("Young's modulus") == ["youngs_modulus"]
    assert Q.resolve_property("нечто непонятное") == []
    mats = Q.resolve_material("каменной соли")
    assert mats[0] == "каменная соль" and "покровная каменная соль" in mats
    assert "сильвинит" in Q.resolve_material("соляные породы")
    con = duckdb.connect()
    Q.register_tables(con, tables)
    r = Q.find_parameters(con, property="модуль упругости", material="каменная соль")
    assert r["total"] >= 1 and all(c["property_key"] == "youngs_modulus" for c in r["candidates"])
    assert r["candidates"][0]["review_status"] == Q.REVIEW_STATUS and "not evidence" in r["note"]
    assert Q.find_parameters(con, property="ширина камеры", site="СКРУ")["total"] == 1
    assert Q.find_parameters(con, property="ширина камеры", site="ВКМ")["total"] == 1
    assert Q.find_parameters(con, property="ширина камеры", site="ANALOGUE")["total"] == 0
    assert Q.find_parameters(con, property="степень нагружения", scale="normative")["total"] == 1
    bad = Q.find_parameters(con, property="нечто непонятное")
    assert bad["unresolved"] == ["property"] and bad["candidates"] == []
    s = Q.parameter_summary(con, "модуль упругости", "соляные породы")
    assert s["rows"] and s["rows"][0]["material"] == "каменная соль" and "never a recommended value" in s["note"]
    assert {"find_parameters", "parameter_summary"} <= set(store.QUERY_FUNCTIONS)
    assert store.resolve("find_parameters") is Q.find_parameters
