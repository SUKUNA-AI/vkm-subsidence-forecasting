"""Structured tables of NAV (agent TB): bands of OCR tables, cell text and values, header rows and row roles, units
and their sources, blocks of bands, lost decimal points, glued header words, the build over a synthetic canonical
snapshot, the CLI part, the parameter part fed from the grid and the query functions. Synthetic rows only (our own
tables, no corpus text)."""
from __future__ import annotations

import pytest

pa = pytest.importorskip("pyarrow")
duckdb = pytest.importorskip("duckdb")

from vkm_corpus.navigation import cli as nav_cli  # noqa: E402
from vkm_corpus.navigation import ids as nav_ids  # noqa: E402
from vkm_corpus.navigation import parameters as P  # noqa: E402
from vkm_corpus.navigation import store  # noqa: E402
from vkm_corpus.navigation import tables as T  # noqa: E402
from vkm_corpus.navigation import tables_query as Q  # noqa: E402

SRC = "VKM-SRC-T21"
P1, P2, P3 = f"{SRC}:p0001", f"{SRC}:p0002", f"{SRC}:p0003"


def _html(rows: list[list[str]], th: bool = False) -> str:
    """One band: rows of cells; a cell ``"text|rs|cs"`` carries spans."""
    out = ["<table>"]
    for i, row in enumerate(rows):
        out.append("<tr>")
        for cell in row:
            text, rs, cs = (cell.split("|") + ["1", "1"])[:3]
            tag = "th" if th and i == 0 else "td"
            out.append(f'<{tag} rowspan="{rs}" colspan="{cs}">{text}</{tag}>')
        out.append("</tr>")
    out.append("</table>")
    return "".join(out)


def _stack(*bands: str) -> tuple[str, list[dict], int, int]:
    """raw_output and canonical cells of a table recognised in bands (stacked as the pipeline stacks them)."""
    cells, off, width = [], 0, 0
    for k, html in enumerate(bands):
        b = T.parse_band(html)
        for c in b["cells"]:
            cells.append({"row": c["row"] + off, "col": c["col"], "row_span": c["row_span"],
                          "col_span": c["col_span"], "is_header": bool(c["th"]) and k == 0, "text": c["text"]})
        off += b["n_rows"]
        width = max(width, b["n_cols"])
    return "\n".join(bands), cells, off, width


def _table(tid: str, bands: tuple[str, ...], label: str | None = None, caption: str | None = None, **kw) -> dict:
    raw, cells, nr, nc = _stack(*bands)
    return {"object_id": tid, "source_id": SRC, "page_id": tid.rsplit(":", 1)[0], "table_label": label,
            "caption": caption, "n_rows": nr, "n_cols": nc, "header_rows": None, "cells": cells, "raw_output": raw,
            "raw_format": "HTML", "recognition_method": kw.get("method", "OCR_GLM"),
            "quality_flags": kw.get("flags", []), "origin": "OCR", "bbox_x0": 50.0, "bbox_y0": 100.0,
            "bbox_x1": 500.0, "bbox_y1": 400.0, "bbox_space": "PAGE_PT_TL"}


def _roles(got: dict) -> dict[int, str]:
    return {c["row"]: c["row_role"] for c in got["cells"]}


def _cell(got: dict, r: int, c: int) -> dict:
    return next(x for x in got["cells"] if x["row"] == r and x["col"] == c)


# ------------------------------------------------------------------------------------------------ bands and text
def test_bands_are_reread_from_the_raw_output():
    b0 = _html([["Порода", "E, ГПа"], ["Соль", "20"]])
    b1 = _html([["Сильвинит", "15"], ["Карналлит", "9"]])
    raw, cells, nr, nc = _stack(b0, b1)
    got = T.resolve_bands(raw, cells)
    assert got["band_of"] == [0, 0, 1, 1] and got["widths"] == [2, 2] and got["n_bands"] == 2
    bad = [dict(c) for c in cells]
    bad[-1]["text"] = "8"                                        # the canon does not match the raw output
    assert T.resolve_bands(raw, bad)["band_of"] is None
    one = T.resolve_bands(b0, _stack(b0)[1])
    assert one["band_of"] is None and one["n_bands"] == 1
    cut = T.parse_band("<table><tr><td>a</td><td>1</td></tr><tr><td>b</td>")   # cut at the token cap
    assert cut["n_rows"] == 1 and [c["text"] for c in cut["cells"]] == ["a", "1"]


def test_cell_text_and_values():
    assert T.clean_cell_text("$1.46") == ("1.46", ["UNBALANCED_MATH_CLOSED", "MATH_RENDERED"])
    assert T.clean_cell_text("SiO_{2}$")[0] == "SiO_2"
    assert T.clean_cell_text("$\\sigma_{1}$, МПа")[0].startswith("σ_1")
    assert T.clean_cell_text("ру- доуправление") == ("рудоуправление", ["DEHYPHENATED"])
    assert T.clean_cell_text("+15")[0] == "15"

    def vt(text):
        v = T.cell_value(T.clean_cell_text(text)[0])
        return v["value_type"], (v["val"].vmin, v["val"].vmax) if v.get("val") else None, v.get("flags", [])

    assert vt("2,5") == ("NUMBER", (2.5, 2.5), [])
    assert vt("10–15")[:2] == ("RANGE", (10.0, 15.0))
    assert vt("25,8 ± 3,1")[0] == "PM" and vt("≤ 0,5")[0] == "BOUND"
    t, (lo, _hi), flags = vt("2,8·10-3")
    assert t == "NUMBER" and abs(lo - 2.8e-3) < 1e-12 and "POWER_SUPERSCRIPT_LOST" in flags
    assert vt("0,3 1,5 0,3 1 5") [0] == "MULTI" and vt("0,90/0,70")[0] == "MULTI"
    assert "SPACED_DIGITS_SUSPECT" in vt("2 18")[2]
    assert vt("МПа")[0] == "UNIT" and vt("(МПа)")[0] == "UNIT"
    assert vt("σсж")[0] == "SYMBOL" and vt("Kдл")[0] == "SYMBOL"
    assert vt("Блок")[0] == "TEXT" and vt("Каменная соль")[0] == "TEXT" and vt("—")[0] == "EMPTY"


def test_statistics_numbers_and_labels():
    for text, stat in (("Stандарт, S", "DISPERSION"), ("Коэффициент вариации, %", "DISPERSION"),
                       ("Средневадратичное отклонение", "DISPERSION"), ("СКО", "DISPERSION"),
                       ("Среднее значение", "MEAN"), ("Максимальное", "MAX"), ("Кол-во образцов", "COUNT"),
                       ("n", "COUNT"), ("Медиана", "MEDIAN"), ("Отклонение от вертикали, мм", None),
                       ("Каменная соль", None)):
        assert T.stat_of(text) == stat, text
    assert T.table_number("Таблица 3.2", None) == "3.2"
    assert T.table_number("Table 2.11.", None) == "2.11"
    assert T.table_number(None, "Таблица Пр.8 – Параметры сдвижений") == "Пр.8"
    assert T.table_number(None, "Параметры модели") is None


def test_glued_words_are_split_conservatively():
    words = {"глубина", "отработки", "мощность", "пласта", "внутри", "солевых", "перекристаллизованный"}

    def is_word(w):
        return w in words

    assert T.segment_glued("глубинаотработки", is_word) == ["глубина", "отработки"]
    assert T.segment_glued("внутрисолевых", is_word) is None                      # the head of a compound
    assert T.respace("Мощностьпласта, м", is_word) == "Мощность пласта, м"
    assert T.respace("Перекристаллизованный галит", is_word) is None               # a word the dictionary knows
    assert T.respace("Мощностьпласта", None) is None                               # no dictionary: no change
    words |= {"предел", "длительной", "коэффициент"}
    assert T.respace("Пределdlительной прочности", is_word) == "Предел длительной прочности"   # OCR «dl» = «дл»
    assert T.respace("Козффициентdlительной", is_word) == "Коэффициент длительной"


def test_decimal_point_suspects():
    col = [("a", "2,18", 2.18), ("b", "1,24", 1.24), ("c", "218", 218.0), ("d", "2,51", 2.51), ("e", "1,90", 1.9)]
    assert T.decimal_suspects(col) == {"c"}
    years = [("a", "2,18", 2.18), ("b", "1,24", 1.24), ("c", "1990", 1990.0), ("d", "2,51", 2.51)]
    assert T.decimal_suspects(years) == set()
    mixed = [("a", "12", 12.0), ("b", "15", 15.0), ("c", "2,5", 2.5), ("d", "18", 18.0)]
    assert T.decimal_suspects(mixed) == set()                                      # a column of whole numbers


# ------------------------------------------------------------------------------------------------ one table
def test_headers_roles_units_and_statistics():
    band = _html([
        ["Порода|2|1", "Предел прочности на сжатие, МПа|1|2", "Модуль деформации, ГПа at 293 K|2|1", "ν|2|1"],
        ["min", "max"],
        ["1", "2", "3", "4", "5"],
        ["Каменная соль", "21,5", "1,20", "5,21", "0,30"],
        ["Сильвинит", "18,4", "1,10", "4,07", "0,27"],
        ["Карналлит", "12,6", "0,95", "3,15", "0,32"],
        ["Среднее значение", "17,5", "1,08", "4,14", "0,30"],
        ["Stандарт, S", "3,6", "0,1", "0,84", "0,02"],
        ["Коэффициент вариации, %", "21", "9", "20", "7"],
        ["Количество образцов", "12", "12", "10", "10"],
    ])
    t = _table(f"{P1}:t000000000001", (band,), label="Таблица 2.1", caption="Свойства соляных пород")
    got = T.structure_table(t)
    st = got["structure"]
    roles = _roles(got)
    assert [roles[r] for r in range(10)] == ["HEADER", "HEADER", "NUMBERING_HEAD", "DATA", "DATA", "DATA",
                                             "STAT_MEAN", "STAT_DISPERSION", "STAT_DISPERSION", "STAT_COUNT"]
    assert st["n_header_rows"] == 3 and st["table_number"] == "2.1" and st["orientation"] == "NORMAL"
    assert st["structure_ok"] and st["covers_region"] and st["nav_table_id"] == nav_ids.table_id(t["object_id"])
    cols = {c["col"]: c for c in got["columns"]}
    assert cols[0]["role"] == "LABEL" and cols[1]["header_path"] == ["Предел прочности на сжатие, МПа", "min"]
    assert (cols[1]["property_key"], cols[1]["unit_canonical"], cols[1]["stat"]) == ("ucs", "МПа", "MIN")
    assert cols[3]["unit_canonical"] == "ГПа"                         # «at 293 K» is a condition, not the unit
    assert cols[3]["property_key"] == "deformation_modulus" and cols[4]["property_key"] == "poisson_ratio"
    c = _cell(got, 3, 1)
    assert (c["value_min"], c["unit_canonical"], c["unit_source"]) == (21.5, "МПа", "HEADER")
    assert c["row_label"] == "Каменная соль" and c["header_text"] == "Предел прочности на сжатие, МПа / min"
    assert c["cell_id"] == f"{st['nav_table_id']}:r3c1" and c["is_header"] is False
    assert _cell(got, 0, 1)["is_header"] and _cell(got, 0, 1)["header_path"] == []
    assert "сильвинит" in st["materials"] and {"ucs", "deformation_modulus"} <= set(st["property_keys"])


def test_units_row_units_column_and_conflicts():
    band = _html([["Показатель", "Ед. изм.", "Соль", "Сильвинит"],
                  ["Плотность", "г/см3", "2,16", "2,02"],
                  ["Предел прочности на сжатие", "МПа", "21,5", "18,4"],
                  ["Влажность, %", "МПа", "0,5", "0,4"]])
    got = T.structure_table(_table(f"{P1}:t000000000002", (band,)))
    assert got["structure"]["orientation"] == "TRANSPOSED"
    assert (_cell(got, 1, 2)["unit_canonical"], _cell(got, 1, 2)["unit_source"]) == ("г/см³", "UNITS_COLUMN")
    assert _cell(got, 2, 3)["row_property_key"] == "ucs"
    units = _html([["Порода", "Плотность", "Прочность"], ["", "г/см3", "МПа"], ["Соль", "2,16", "21,5"]])
    got = T.structure_table(_table(f"{P1}:t000000000003", (units,)))
    assert _roles(got)[1] == "UNITS" and _cell(got, 2, 2)["unit_source"] == "UNITS_ROW"
    conflict = _html([["Показатель", "Значение, МПа"], ["Плотность, г/см3", "2,16"], ["Прочность", "21,5"]])
    got = T.structure_table(_table(f"{P1}:t000000000004", (conflict,)))
    c = _cell(got, 1, 1)
    assert c["unit_canonical"] is None and c["unit_source"] == "CONFLICT" and "UNIT_CONFLICT" in c["flags"]
    assert _cell(got, 2, 1)["unit_canonical"] == "МПа"


def test_text_dominated_rows_are_not_swallowed_by_the_header():
    band = _html([["Пачка", "Индекс", "Литология", "Мощность, м"],
                  ["Сильвинитовая|3|1", "А", "+ + +", "2,1"],
                  ["Б", "+ +", "1,6"],
                  ["В", "", "5,1"]])
    got = T.structure_table(_table(f"{P1}:t000000000005", (band,)))
    assert [_roles(got)[r] for r in range(4)] == ["HEADER", "DATA", "DATA", "DATA"]
    assert _cell(got, 1, 3)["header_text"] == "Мощность, м" and _cell(got, 1, 3)["unit_canonical"] == "м"


def test_lost_decimal_point_is_flagged_not_fixed():
    rows = [["Образец", "E, ГПа"]] + [[f"{i}", v] for i, v in
                                       enumerate(["2,18", "1,24", "218", "2,51", "1,90"], 1)]
    got = T.structure_table(_table(f"{P1}:t000000000006", (_html(rows),)))
    c = _cell(got, 3, 1)
    assert "DECIMAL_POINT_SUSPECT" in c["flags"] and c["value_min"] == 218.0
    assert "DECIMAL_POINT_SUSPECT" in got["structure"]["quality_flags"]


def test_bands_blocks_repeated_headers_and_mismatches():
    head = ["Образец", "σсж, МПа", "E, ГПа"]
    b0 = _html([head, ["С-1", "21,5", "20,1"], ["С-2", "18,4", "19,0"]])
    b1 = _html([head, ["С-3", "20,3", "18,7"], ["С-4", "22,0", "21,2"]])         # the header repeated
    b2 = _html([["5,3", "11"], ["4,8", "12"], ["6,0", "10"]])                     # two columns, names lost: shifted
    b3 = _html([["Выработка", "Глубина, м"], ["Штрек", "420"], ["Камера", "415"]])  # a header of its own
    got = T.structure_table(_table(f"{P2}:t000000000007", (b0, b1, b2, b3)))
    st = got["structure"]
    assert st["n_bands"] == 4 and st["n_blocks"] == 3
    assert {"REPEATED_HEADER_ROWS", "BAND_COLUMNS_MISMATCH", "BAND_OWN_HEADER"} <= set(st["quality_flags"])
    roles = _roles(got)
    assert roles[3] == "REPEATED_HEADER" and roles[6] == "DATA" and roles[9] == "HEADER"
    assert _cell(got, 4, 1)["header_text"] == "σсж, МПа" and _cell(got, 4, 1)["block"] == 0
    shifted = _cell(got, 6, 0)
    assert shifted["header_path"] == [] and "BAND_COLUMNS_MISMATCH" in shifted["flags"]
    assert _cell(got, 10, 1)["header_text"] == "Глубина, м" and _cell(got, 10, 1)["block"] == 2
    wide = _html([["С-7", "19,9", "20,0", "С-8", "20,5", "20,2"]])              # a wider band continues the block
    got = T.structure_table(_table(f"{P2}:t000000000008", (b0, wide)))
    assert got["structure"]["n_blocks"] == 1
    assert _cell(got, 3, 1)["header_text"] == "σсж, МПа" and "NO_HEADER" in _cell(got, 3, 4)["flags"]
    header_band = _html([["Пачка", "Индекс", "Мощность, м (от-до/ср)"]])       # a band of the header alone
    body = _html([["Покровная", "ПКС", "13.4-25.8/19.2"], ["Сильвинитовая", "А", "1,5-3,8"]])
    got = T.structure_table(_table(f"{P2}:t000000000013", (header_band, body)))
    st = got["structure"]
    assert st["n_blocks"] == 1 and st["header_method"] == "HEADER_BAND" and "BAND_COLUMNS_MISMATCH" not in \
        st["quality_flags"]
    assert _cell(got, 1, 2)["value_type"] == "MULTI" and _cell(got, 2, 2)["header_text"] == "Мощность, м (от-до/ср)"
    group = _html([["Подстилающая|2|1", "каменная соль", ""], ["Б", "0,8"]])     # a group label, not a header
    got = T.structure_table(_table(f"{P2}:t000000000014", (header_band, body, group)))
    assert got["structure"]["n_blocks"] == 1 and _cell(got, 4, 2)["header_text"] == "Мощность, м (от-до/ср)"


def test_column_units_angle_marks_and_a_top_axis():
    assert T.column_unit("Height H") is None and T.column_unit("Case (a) (km s–1)") is None
    assert T.column_unit("Длина, м").canon == "м" and T.column_unit("Глубина (м)").canon == "м"
    assert T.column_unit("Модуль упругости, ГПа at 293 K").canon == "ГПа" and T.column_unit("h") is None
    v = T.cell_value('15"')
    assert v["value_type"] == "NUMBER" and v["val"].vmin == 15.0 and v["flags"] == ["ANGLE_MARK"]
    assert T.stat_of("Скорость подачи, м/мин") is None and T.stat_of("мин") == "MIN"
    axis = _html([["Боковое давление, МПа", "0,5", "1,0", "2,0"], ["Серия 1", "3", "4", "2"],
                  ["Серия 2", "2", "2", "5"]])
    got = T.structure_table(_table(f"{P3}:t000000000015", (axis,)))
    assert _roles(got)[0] == "AXIS" and _cell(got, 1, 2)["header_path"] == ["Боковое давление, МПа", "1,0"]
    assert got["structure"]["orientation"] == "MATRIX" and "NO_HEADER" not in got["structure"]["quality_flags"]


def test_caption_units_temperature_brackets_and_minutes():
    assert T.column_unit("T(K)").canon == "K" and T.column_unit("Temperature(℃)").canon == "°C"
    assert T.column_unit("Модуль объемного сжатия, K") is None                      # a symbol, not kelvin
    assert T.column_unit("Estimation Bias [mm/yr]").canon == "мм/год"
    assert T.column_unit("Скорость в пласте, см/нс").canon == "см/нс"
    for text, stat in (("Время (мин.)", None), ("Time, min", None), ("мин.", "MIN"), ("Minimalное значение", "MIN"),
                       ("Minimalое значение", "MIN"), ("Maximal value", "MAX"), ("Минимальное", "MIN")):
        assert T.stat_of(text) == stat, text
    cu = T.caption_units
    assert cu("Таблица 2.4 Прочность пород, МПа")["unit"].canon == "МПа"
    assert cu("Таблица 3 Химический состав проб, масс. %")["unit"].canon == "%"
    assert cu("Table 5. Mean RMS (mm) for all baselines")["unit"].canon == "мм"
    assert cu("Статистики содержаний, % (n = 61)")["unit"].canon == "%"
    for text in ("Параметры модели по данным 2000-2021 гг.", "Time correlation over lags t (Days) for series",
                 "Прочность, МПа, и модуль упругости, ГПа", "где h – потеря энергии (Па); v – скорость (м/с)",
                 "Microhardness, H", "Прочность при 20 °C"):
        assert cu(text)["unit"] is None, text
    got = cu("Table 3.1 [Confining Pressure, P, GPa; Differential Stress, (σ_1-σ_3) MPa; Hardness, H, kg mm^-2]")
    assert [(n, s, u.canon) for n, s, u in got["clauses"]] == [("confiningpressure", "p", "ГПа"),
                                                               ("differentialstress", "σ1-σ3", "МПа")]
    density = _html([["T(K)", "ρ(±2)"], ["273", "2168"], ["283", "2165"], ["293", "2163"]])
    got = T.structure_table(_table(f"{P3}:t00000000001a", (density,), label="Table 2.3.",
                                   caption="Halite: Density (kg/m^3)"))
    assert (_cell(got, 1, 0)["unit_canonical"], _cell(got, 1, 0)["unit_source"]) == ("K", "HEADER")
    assert (_cell(got, 1, 1)["unit_canonical"], _cell(got, 1, 1)["unit_source"]) == ("кг/м³", "CAPTION")
    assert {c["col"]: c["unit_source"] for c in got["columns"]} == {0: "HEADER", 1: "CAPTION"}
    sets = _html([["Data Set", "T", "H"], ["1", "298.2", "21.75"], ["2", "374.6", "21.80"], ["3", "441.6", "21.91"]])
    got = T.structure_table(_table(f"{P3}:t00000000001b", (sets,),
                                   caption="[Quench Temperature, T, K; Microhardness, H, kg mm^-2]"))
    assert [_cell(got, 1, c)["unit_canonical"] for c in range(3)] == [None, "K", None]
    ages = _html([["Год", "Скв. 1", "Скв. 2"], ["2001", "12,5", "13,1"], ["2002", "12,9", "13,4"]])
    got = T.structure_table(_table(f"{P3}:t00000000001c", (ages,), caption="Оседание реперов, мм"))
    assert _cell(got, 1, 0)["unit_canonical"] is None and _cell(got, 1, 1)["unit_canonical"] == "мм"
    assert T.column_unit("Author(s), Year [Ref.]") is None                      # a column of calendar years
    layers = _html([["Минерал|2|1", "Слой|1|3"], ["9", "12", "16"], ["Галит", "96,84", "95,10", "94,20"],
                    ["Сильвин", "1,20", "2,05", "3,10"]])
    got = T.structure_table(_table(f"{P3}:t00000000001d", (layers,), caption="Минеральный состав соли, вес. %"))
    assert _roles(got)[1] in T.HEADER_ZONE and _cell(got, 2, 1)["unit_canonical"] == "%"   # the values under an axis
    spec = _html([["Параметр", "Значение"], ["Производительность, т/ч", "26"], ["Основные размеры, мм:|1|2"],
                  ["длина", "10 500"], ["ширина", "2500"], ["Масса, т", "15"]])
    got = T.structure_table(_table(f"{P3}:t00000000001e", (spec,)))
    assert _roles(got)[2] == "GROUP" and _cell(got, 3, 1)["value_min"] == 10500.0
    assert (_cell(got, 3, 1)["unit_canonical"], _cell(got, 3, 1)["unit_source"]) == ("мм", "GROUP_LABEL")
    assert _cell(got, 5, 1)["unit_canonical"] is None                          # «Масса, т» is no item of the group
    assert T.stat_of("Длина комплекта (min) в собранном виде") == "MIN" and T.stat_of("Time (min)") is None


def test_symbol_statistics_sub_level_axes_and_units_after_symbols():
    stats = _html([["Статистика", "Hg", "Cd"], ["x", "0,58", "0,36"], ["σ", "0,31", "0,22"], ["V", "53,4", "61,0"]])
    got = T.structure_table(_table(f"{P3}:t000000000016", (stats,)))
    assert [_roles(got)[r] for r in (1, 2, 3)] == ["STAT_MEAN", "STAT_DISPERSION", "STAT_DISPERSION"]
    assert T.stat_of("Количество шламов, млн т") is None and T.stat_of("Количество испытаний") == "COUNT"
    shear = _html([["№|2|1", "Порода|2|1", "Прочность на сдвиг при σn, МПа|1|3", "Паспорт|1|2"],
                   ["1,0", "3,0", "5,0", "C, МПа", "tgφ"],
                   ["1", "Соль", "1,86", "4,80", "6,66", "0,84", "1,20"],
                   ["2", "Мергель", "1,58", "3,16", "4,73", "0,79", "0,79"]])
    got = T.structure_table(_table(f"{P3}:t000000000017", (shear,)))
    assert [_roles(got)[r] for r in (0, 1, 2)] == ["HEADER", "AXIS", "DATA"]   # a quantity «при» the axis values
    cols = {c["col"]: c for c in got["columns"]}
    assert cols[2]["header_path"] == ["Прочность на сдвиг при σn, МПа", "1,0"] and cols[2]["unit_canonical"] == "МПа"
    assert cols[2]["property_key"] == "shear_strength" and "AXIS_COLUMN" not in cols[2]["flags"]
    (sub, n_rows, n_cols, head, row_map, _f), = T.param_blocks(got["cells"], got["structure"])
    assert row_map == [0, 2, 3] and head == 1                                   # the axis is not read for values
    cands = P.table_candidates(sub, n_rows, n_cols, None, None, None, head)
    assert {(c.prop.key, c.val.vmin) for c, _r, col in cands if col == 2} == {("shear_strength", 1.86),
                                                                             ("shear_strength", 1.58)}
    assert T.column_unit("Высота построек (H), м").canon == "м"
    right = _html([["2°30'", "2°37'30\"", "2°45'", "Долгота"], ["155966,0", "163762,3", "171558,3", "56°"],
                   ["155630,1", "163409,6", "171188,8", "5"]])
    got = T.structure_table(_table(f"{P3}:t000000000018", (right,)))
    assert _roles(got)[0] == "AXIS" and _cell(got, 1, 0)["header_path"] == ["Долгота", "2°30'"]
    formula = _html([["t", "K(t)", "1+∫0 t Kdτ"], ["0,001", "1,4653", "1,0058"], ["0,002", "0,8730", "1,0069"]])
    got = T.structure_table(_table(f"{P3}:t000000000019", (formula,)))
    assert _roles(got)[0] == "HEADER" and got["structure"]["n_header_rows"] == 1


def test_empty_figure_and_axis_tables():
    got = T.structure_table({"object_id": f"{P3}:t000000000009", "source_id": SRC, "page_id": P3, "cells": [],
                             "recognition_method": "OCR_GLM", "raw_format": "HTML"})
    assert got["structure"]["quality_flags"] == ["NO_CELLS"] and not got["structure"]["structure_ok"]
    fig = T.structure_table(_table(f"{P3}:t000000000010", (_html([["a", "b"], ["1", "2"]]),), label="Рис. 4."))
    assert "FIGURE_SUSPECT" in fig["structure"]["quality_flags"] and not fig["structure"]["covers_region"]
    axis = _html([["q|2|1", "Вязкость η, Па·с|1|3"], ["0,3", "0,4", "0,5"], ["30", "0,20", "0,21", "0,22"],
                  ["40", "0,25", "0,26", "0,27"]])
    got = T.structure_table(_table(f"{P3}:t000000000011", (axis,)))
    assert got["structure"]["orientation"] == "MATRIX" and _roles(got)[1] == "AXIS"
    assert all(c["property_key"] is None for c in got["columns"])


# ------------------------------------------------------------------------------------------------ parameters input
def test_param_blocks_leave_out_statistics_and_unaligned_headers():
    head = ["Порода", "σсж, МПа"]
    b0 = _html([head, ["Соль", "21,5"], ["Stандарт, S", "3,6"], ["Кол-во образцов", "12"]])
    b1 = _html([["18,4", "Сильвинит"], ["17,9", "Карналлит"]])                  # columns swapped: not aligned
    got = T.structure_table(_table(f"{P2}:t000000000012", (b0, b1)))
    blocks = T.param_blocks(got["cells"], got["structure"])
    (sub, n_rows, _n_cols, head_rows, row_map, _flags), (sub2, _n2, _c2, head2, row_map2, _f2) = blocks
    assert row_map == [0, 1] and head_rows == 1 and n_rows == 2                  # dispersion and count rows out
    assert head2 == 0 and row_map2 == [4, 5]
    assert {x["text"] for x in sub if x["row"] == 1} == {"Соль", "21,5"}
    cands = [(c.prop.key, c.val.vmin, row_map[r]) for c, r, _col in
             P.table_candidates(sub, n_rows, _n_cols, None, None, None, head_rows)]
    assert cands == [("ucs", 21.5, 1)]


def test_header_unit_conditions_and_generic_value_columns():
    assert P.header_unit("Модуль упругости, GPa at 293 K").canon == "ГПа"
    assert P.header_unit("Предел прочности при 20 °C") is None
    assert P.header_unit("σ1, МПа").canon == "МПа"
    assert P.header_unit("E·10^3 МПа").canon == "МПа"
    assert P.header_unit("Viscosity (1021 Pa s)") is not None                   # 10²¹ with the superscript lost
    cells = [{"row": r, "col": c, "row_span": 1, "col_span": 1, "is_header": False, "text": t}
             for r, row in enumerate([["Параметр", "Default", "Range"], ["Stress exponent n", "3.6", "3-7"]])
             for c, t in enumerate(row)]
    got = P.table_candidates(cells, 2, 3, "Input parameters", None, "en", 1)
    assert sorted((c.prop.key, c.val.text) for c, _r, _c in got) == [("creep_exponent", "3-7"),
                                                                      ("creep_exponent", "3.6")]
    defs = []
    P.text_candidates("Dпр — модуль деформации (секущий) на пределе прочности (Dпр, ГПа).", "ru", definitions=defs)
    assert "strength_unspecified" not in {p for _s, p in defs}


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
        "text" VARCHAR)[], raw_output VARCHAR, raw_format VARCHAR, recognition_method VARCHAR,
        quality_flags VARCHAR[], origin VARCHAR, bbox_x0 DOUBLE, bbox_y0 DOUBLE, bbox_x1 DOUBLE, bbox_y1 DOUBLE,
        bbox_space VARCHAR)""")
    con.execute("CREATE TABLE canonical.figures (page_id VARCHAR, bbox_x0 DOUBLE, bbox_y0 DOUBLE, bbox_x1 DOUBLE, "
                "bbox_y1 DOUBLE, bbox_space VARCHAR)")
    con.execute("CREATE TABLE canonical.sources (source_id VARCHAR, site_scope VARCHAR[], source_class_raw VARCHAR)")
    con.executemany("INSERT INTO canonical.pages VALUES (?, ?, ?)", [(P1, SRC, 1), (P2, SRC, 2)])
    con.execute("INSERT INTO canonical.sources VALUES (?, ['VKM_REGIONAL'], 'monograph')", [SRC])
    stats = _html([["Порода", "Предел прочности на сжатие, МПа", "Примечание"],
                   ["Каменная соль", "21,5", "образцы с глубины 320 м"],
                   ["Stандарт, S", "3,6", ""]])
    t1 = _table(f"{P1}:t00000000000a", (stats,), label="Таблица 1", caption="Прочность каменной соли")
    t2 = _table(f"{P2}:t00000000000b", (_html([["Параметр", "Значение"], ["Плотность, г/см3", "2,16"]]),))
    for t in (t1, t2):
        con.execute("INSERT INTO canonical.tables VALUES (?, ?, ?, ?, ?, ?, ?, NULL, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    [t["object_id"], t["source_id"], t["page_id"], t["table_label"], t["caption"], t["n_rows"],
                     t["n_cols"], t["cells"], t["raw_output"], t["raw_format"], t["recognition_method"],
                     t["quality_flags"], t["origin"], t["bbox_x0"], t["bbox_y0"], t["bbox_x1"], t["bbox_y1"],
                     t["bbox_space"]])
    blk = [  # the text layer of the table region on page 1 (a line per row) and a sentence outside it
        (f"{P1}:b1", SRC, P1, "TEXT", 1, 60, 150, 480, 170, "Каменная соль 21,5 образцы с глубины 320 м"),
        (f"{P1}:b2", SRC, P1, "TEXT", 2, 60, 180, 480, 200, "Stандарт, S 3,6 Предел прочности на сжатие, МПа"),
        (f"{P1}:b3", SRC, P1, "TEXT", 3, 50, 500, 500, 530, "Модуль упругости каменной соли составляет 20 ГПа."),
    ]
    con.executemany("INSERT INTO canonical.blocks VALUES (?, ?, ?, true, ?, ?, ?, ?, ?, ?, 'PAGE_PT_TL', 'ru', ?)",
                    blk)
    return con


@pytest.fixture(scope="module")
def built():
    con = _canon()
    stats: dict = {}
    tables = T.build(con, section_pages=[{"section_id": "SEC-a", "page_id": P1}], sections=[], stats=stats,
                     respace="off")
    return con, tables, stats


def test_build_datasets_ids_and_stats(built):
    _con, tables, stats = built
    assert tables["table_structure"].schema.names == list(T.STRUCTURE_COLUMNS)
    assert tables["table_cells"].schema.names == list(T.CELL_COLUMNS)
    assert tables["table_columns"].schema.names == list(T.COLUMN_COLUMNS)
    st = {r["table_id"]: r for r in tables["table_structure"].to_pylist()}
    s1 = st[f"{P1}:t00000000000a"]
    assert s1["section_id"] == "SEC-a" and s1["page_index"] == 1 and s1["nav_table_id"].startswith("TBL-")
    assert s1["review_status"] == T.REVIEW_STATUS and s1["rule_version"] == "tables_v1"
    cells = tables["table_cells"].to_pylist()
    assert all(c["cell_id"].startswith(c["nav_table_id"] + ":r") for c in cells)
    assert stats["counters"]["tables"] == 2 and stats["rule_version"] == "tables_v1" and stats["respace"] == "off"
    again = T.build(_canon(), respace="off")["table_cells"].to_pylist()
    assert sorted(c["cell_id"] for c in again) == sorted(c["cell_id"] for c in cells)
    info = T.summarize(tables)
    assert info["tables"] == 2 and info["structure_ok"] == 2 and "HEADER" in info["row_roles"]


def test_cli_registers_the_part_before_parameters(tmp_path):
    parts = list(nav_cli.PARTS)
    assert parts.index("tables") + 1 == parts.index("parameters")
    assert nav_cli.datasets_of("tables") == ("table_structure", "table_cells", "table_columns")
    assert nav_cli.parse_parts("parameters,tables") == ["tables", "parameters"]
    assert nav_cli.parse_parts("all") == parts
    with pytest.raises(SystemExit):
        nav_cli.parse_parts("tables,nothing")
    assert nav_ids.RULE_VERSIONS["tables"] == "tables_v1" and nav_ids.RULE_VERSIONS["parameters"] == "parameters_v2"
    assert {"table_structure", "table_cells", "table_columns"} <= set(nav_ids.DATASETS)
    assert nav_ids.table_cell_id(nav_ids.table_id("x:t1"), 3, 2) == nav_ids.table_id("x:t1") + ":r3c2"
    manifest = nav_cli.build_parts(_canon(), tmp_path / "out", ["tables", "parameters"],
                                   part_options={"tables": {"respace": "off"}, "parameters": {"workers": 1}})
    assert manifest["parts"]["tables"]["status"] == "BUILT" and manifest["parts"]["parameters"]["status"] == "BUILT"
    assert manifest["parts"]["parameters"]["stats"]["table_input"] == "table_cells"


def test_parameters_read_the_grid_and_skip_its_text_lines(built):
    con, tables, _stats = built
    stats: dict = {}
    out = P.build(con, section_pages=[], sections=[], table_structure=tables["table_structure"],
                  table_cells=tables["table_cells"], stats=stats, workers=1)
    rows = out["parameter_candidates"].to_pylist()
    got = {(r["method"], r["property_key"], r["value_text"]) for r in rows}
    assert ("TABLE", "ucs", "21,5") in got                                       # the grid
    assert not any(r["value_text"] == "3,6" for r in rows)                      # the dispersion row
    assert ("TEXT", "youngs_modulus", "20") in got                               # a sentence outside the table
    cell_text = [r for r in rows if "CELL_TEXT" in r["flags"]]                   # a value inside a text cell
    assert [(r["property_key"], r["value_min"], r["table_row"], r["table_col"]) for r in cell_text] == [
        ("depth_unspecified", 320.0, 1, 2)]
    assert ("TABLE", "density", "2,16") in got
    assert stats["counters"]["text_blocks_in_tables_skipped"] == 2 and stats["counters"]["tables_structured"] == 2
    assert stats["rule_version"] == "parameters_v2" and stats["table_input"] == "table_cells"
    plain = P.build(con, section_pages=[], sections=[], workers=1)["parameter_candidates"].to_pylist()
    assert any(r["method"] == "TEXT" and r["block_id"] == f"{P1}:b2" for r in plain) or \
        any(r["value_text"] == "3,6" for r in plain)                              # without the grid: v1 behaviour


# ------------------------------------------------------------------------------------------------ queries
def test_query_functions(built):
    _con, tables, _stats = built
    con = duckdb.connect()
    Q.register_tables(con, tables)
    tid = f"{P1}:t00000000000a"
    got = Q.get_table_structured(con, tid)
    assert got["found"] and got["table"]["table_number"] == "1" and got["review_status"] == Q.REVIEW_STATUS
    assert got["rows"][0]["role"] == "HEADER" and "| Каменная соль | 21,5 |" in got["markdown"]
    assert Q.get_table_structured(con, got["table"]["nav_table_id"])["table"]["table_id"] == tid
    assert Q.get_table_structured(con, "no-such-table")["found"] is False
    small = Q.get_table_structured(con, tid, max_rows=1, max_chars=200)
    assert small["truncated"]["rows"] and len(small["rows"]) == 1
    r = Q.find_tables(con, property="предел прочности на сжатие")
    assert r["query"]["property_keys"] == ["ucs"] and [t["table_id"] for t in r["tables"]] == [tid]
    assert r["tables"][0]["matched_columns"][0]["col"] == 1 and "not evidence" in r["note"]
    assert Q.find_tables(con, material="каменная соль")["total"] == 1
    assert Q.find_tables(con, text="плотность")["tables"][0]["table_id"] == f"{P2}:t00000000000b"
    assert Q.find_tables(con, property="ucs", source_id="VKM-SRC-X")["total"] == 0
    bad = Q.find_tables(con, property="нечто непонятное")
    assert bad["unresolved"] == ["property"] and bad["tables"] == []
    assert {"table_structured", "find_tables"} <= set(store.QUERY_FUNCTIONS)
    assert store.resolve("find_tables") is Q.find_tables
