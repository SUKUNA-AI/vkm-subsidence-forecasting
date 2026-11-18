"""World parameter passport, stage 1 (scripts/build_world_parameters.py): SI conversion, material classes of the world,
long-term strength printed as a fraction, backfill densities recorded under a rock lithology, aggregation."""
from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "build_world_parameters.py"


@pytest.fixture(scope="module")
def wp():
    spec = importlib.util.spec_from_file_location("build_world_parameters", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.mark.parametrize("value,unit,kind,expected", [
    (1.5, "ГПа", "stress", 1.5e9), (24.3, "МПа", "stress", 24.3e6), (10, "тс/м2", "stress", 98066.5),
    (100, "кгс/см2", "stress", 9806650), (2.1, "г/см3", "density", 2100.0), (2.24, "т/м³", "density", 2240.0),
    (0.022, "МН/м3", "density", 0.022e6 / 9.80665), (0.022, "МПа/м", "density", 0.022e6 / 9.80665),
    (2.8, "10^4*N/m3", "density", 2.8e4 / 9.80665), (0.3, "-", "ratio", 0.3), (3.9, "%", "strain", 0.039),
    (30, "градус", "angle", 30.0)])
def test_si_conversion(wp, value, unit, kind, expected):
    got, rule = wp.convert(value, unit, kind)
    assert got == pytest.approx(expected) and rule != "UNIT_NOT_CONVERTED"


def test_unknown_unit_is_kept_unconverted(wp):
    assert wp.convert(5.0, "бар (МПа)", "stress") == (None, "UNIT_NOT_CONVERTED")


@pytest.mark.parametrize("lith,layer,seam,cls", [
    ("каменная соль", "Покровная каменная соль", "PKS", "ROCKSALT"),       # «покровная» is salt, not cover deposits
    ("сильвинит", "", "KR2", "SYLVINITE"), ("карналлит", "", "V", "CARNALLITE"),
    ("mixed", "Ангидрит-галит", "", "TRANSITION_OVERBURDEN"), ("мергель", "", "GAT", "TRANSITION_OVERBURDEN"),
    ("", "глинистый прослой", "", "CLAY_CONTACT"), ("mean of overburden column", "", "", "COLUMN"),
    ("соляные породы", "", "", "SALT_GENERAL"), ("salt rocks", "", "", "SALT_GENERAL"),
    ("", "", "MPS", "ROCKSALT"), ("", "", "", "UNCLASSIFIED")])
def test_material_class(wp, lith, layer, seam, cls):
    assert wp.material_class(lith, layer, seam) == cls


def test_backfill_density_under_a_rock_lithology(wp):
    assert wp.material_class("каменная соль", "", "", "механическая закладка самоходными вагонами") == "BACKFILL"


def row(**kw):
    base = {"row_id": "MR-1", "variable": "ucs", "value_point": "24.3", "value_min": "", "value_max": "",
            "unit_as_printed": "МПа", "material_lithology": "сильвинит", "layer_or_unit_as_printed": "", "unit_seam": "KR2",
            "scale": "LAB", "site_scope": "SKRU1", "status": "FACT", "evidence_type": "LAB_TEST",
            "value_as_printed": "24,3", "test_method": "", "n_samples": "", "conditions": "", "source_id": "VKM-SRC-001",
            "pdf_page": "10", "printed_page": "9", "locator": "p. 10", "transfer_status_SKRU1": "", "original_or_cited": "",
            "confidence": "", "duplicate_of": "", "notes": ""}
    base.update(kw)
    return base


def test_evidence_rows_and_summary(wp):
    mech = [row(), row(row_id="MR-2", value_point="", value_min="20", value_max="30", source_id="VKM-SRC-002"),
            row(row_id="MR-3", variable="long_term_strength", value_point="0.37", unit_as_printed="-"),
            row(row_id="MR-4", variable="damage_softening"), row(row_id="MR-5", duplicate_of="MR-1")]
    ev, skipped = wp.evidence_rows(mech)
    assert skipped == {"VARIABLE_NOT_A_WORLD_PARAMETER": 1, "DUPLICATE_ROW": 1}
    by = {x["catalogue_row_id"]: x for x in ev}
    assert by["MR-1"]["param_id"] == "UCS.SYLVINITE" and float(by["MR-1"]["si_min"]) == pytest.approx(24.3e6)
    assert (float(by["MR-2"]["si_min"]), float(by["MR-2"]["si_max"])) == (20e6, 30e6)
    assert by["MR-3"]["parameter"] == "LTS_RATIO" and float(by["MR-3"]["si_min"]) == pytest.approx(0.37)
    summ = {(s["param_id"], s["site_group"]): s for s in wp.summary_rows(ev)}
    s = summ[("UCS.SYLVINITE", "SKRU1")]
    assert (s["n_rows"], s["n_sources"], float(s["si_min"]), float(s["si_max"])) == (2, 2, 20e6, 30e6)
    assert all(x["wpe_id"].startswith("WPE-") for x in ev)


def test_kaiser_study_counts_once_and_retains_citation_occurrences(wp):
    evidence = dict(parameter="LAMBDA", material_class="SALT_GENERAL", scale="MASSIF", site_scope="VKM_REGIONAL",
                    site_group="VKM_REGIONAL", status="FACT", evidence_type="LITERATURE_CITED", si_unit="1",
                    si_min="0.71", si_max="0.71", original_or_cited="CITED: Асанов В.А., Токсаров В.Н. 1999")
    lebedeva = dict(evidence, wpe_id="WPE-00851", source_id="VKM-SRC-012",
                    catalogue_row_id="MR-MECH-1560", pdf_page="27")
    beltyukov = dict(evidence, wpe_id="WPE-00852", source_id="VKM-SRC-029",
                    catalogue_row_id="MR-MECH-1564", pdf_page="45")
    unknown = dict(evidence, wpe_id="WPE-other", source_id="VKM-SRC-999", catalogue_row_id="MR-unknown",
                   pdf_page="1", si_min="0.60", si_max="0.60")
    rows = [lebedeva, beltyukov, unknown]
    result, = wp.summary_rows(rows)
    assert result["wpe_ids"] == "WPE-00851;WPE-00852;WPE-other"
    assert (result["n_rows"], result["n_sources"], result["n_converted"]) == (3, 3, 2)
    assert float(result["si_median"]) == pytest.approx(0.655)
    assert wp.summary_rows([lebedeva, dict(beltyukov, pdf_page="46")])[0]["n_converted"] == 2
    assert wp.summary_rows([lebedeva, dict(beltyukov, si_max="0.72")])[0]["n_converted"] == 2
    assert wp.summary_rows([lebedeva])[0]["n_converted"] == 1


def test_non_salt_stress_rows_keep_ids_and_leave_salt_summary(wp):
    stress = dict(material_lithology="mixed", unit_seam="", scale="MASSIF", site_scope="VKM_REGIONAL",
                  source_id="VKM-SRC-030", transfer_status_SKRU1="NON_TRANSFERABLE | VKM_REGIONAL_PRIOR",
                  test_method="in_situ", status="DERIVATION", evidence_type="CALCULATED_BY_AUTHOR")
    cited = dict(stress, source_id="VKM-SRC-023", site_scope="NON_VKM", unit_as_printed="-",
                 value_point="1.4", original_or_cited="CITED: [23]", locator="PARA 214",
                 transfer_status_SKRU1="NON_TRANSFERABLE | NON_VKM_ANALOGUE")
    mech = [
        row(**stress, row_id="MR-MECH-1612", variable="max_horizontal_stress"),
        row(**stress, row_id="MR-MECH-1613", variable="min_horizontal_stress"),
        row(**cited, row_id="MR-MECH-1561", variable="lateral_stress_ratio"),
        row(**dict(stress, source_id="VKM-SRC-041", material_lithology="salt rocks"),
            row_id="MR-other", variable="horizontal_stress_magnitude"),
    ]
    ev, skipped = wp.evidence_rows(mech)
    by_source = {r["catalogue_row_id"]: r for r in ev}
    assert not skipped
    assert [(r["wpe_id"], r["material_class"]) for r in by_source.values()] == [
        ("WPE-00001", "UNCLASSIFIED"), ("WPE-00002", "UNCLASSIFIED"),
        ("WPE-00003", "SALT_GENERAL"), ("WPE-00004", "UNCLASSIFIED"),
    ]
    assert all("NON_TRANSFERABLE" in r["transfer_status_SKRU1"] for r in ev)
    assert by_source["MR-MECH-1612"]["status"] == "DERIVATION"
    assert by_source["MR-MECH-1561"]["original_or_cited"] == "CITED: [23]"
    salt = [x for x in wp.summary_rows(ev) if x["material_class"] == "SALT_GENERAL"]
    assert [(x["param_id"], x["wpe_ids"]) for x in salt] == [("SIGMA_H.SALT_GENERAL", "WPE-00003")]

@pytest.mark.parametrize("value,unit,kind,expected", [
    (450, "мм", "length", 0.45), (12, "м", "length", 12.0), (1.5, "км", "length", 1500.0), (35, "см", "length", 0.35),
    (70, "%", "fraction", 0.7), (0.65, "доли", "fraction", 0.65), (24, "°C", "temperature", 24.0)])
def test_si_conversion_passport_kinds(wp, value, unit, kind, expected):
    """Kinds added for the passport (C1 thickness / depth, C4 geometry and ratios, C3 temperature in °C)."""
    got, rule = wp.convert(value, unit, kind)
    assert got == pytest.approx(expected) and rule != "UNIT_NOT_CONVERTED"
    assert wp.convert(value, "фут", kind)[0] is None
