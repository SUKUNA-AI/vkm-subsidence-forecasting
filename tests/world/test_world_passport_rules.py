"""Rules v2 of the world passport (12.10.2026): equal numbers are not identity, UNKNOWN origin is not ORIGINAL,
FACT needs a reviewed original natural value of the site, ids stay when rows are added, the prior proposal keeps
branches / evidence kinds / units / law definitions / unstated sites apart, receipts list the inputs actually read."""
from __future__ import annotations

import csv
import importlib.util
from pathlib import Path

import pytest

from vkm_world.extraction import passport_rules as PR

ROOT = Path(__file__).resolve().parents[2]


def _load(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def wpp():
    return _load("build_world_passport")


@pytest.fixture(scope="module")
def wpr():
    return _load("build_world_priors")


BASE = dict(ev_id="", producer="SOL_HIGH_FULL", producer_record_id="", module="C1", parameter="THICKNESS", kind="",
            target="PKS", material_class="", material_as_printed="", borehole_id="", scale="FIELD", branch="-",
            site_norm="SKRU1", site_group="SKRU1", site_basis="PAGE", content_kind="", value_as_printed="12",
            unit_as_printed="м", si_min="12", si_max="12", si_unit="m", conversion="x1", symbol="",
            equation_as_printed="", time_as_printed="", conditions="", method="", n_samples="", origin="ORIGINAL",
            cited_ref="", source_id="VKM-SRC-011", page_id="VKM-SRC-011:p0131", locator="", quote="", status="FACT",
            verification="PHASE1_REVIEWED", conflict_ids="", notes="")


def rows(*specs):
    out = []
    for i, kw in enumerate(specs, 1):
        out.append(dict(BASE, ev_id=f"WPV-{i:06d}", producer_record_id=kw.pop("rec", f"R{i}"), **kw))
    return out


def counted(wpp, ev):
    reg, _links = wpp.registry(ev, {})
    return {(r["module"], r["parameter"], r["target"], r["branch"], r["evidence_kind"]): r for r in reg
            if r["n_evidence"]}


# ------------------------------------------------------------------------------------- copies are not identity
def test_equal_values_of_different_boreholes_both_count(wpp):
    ev = rows(dict(producer="CURATED_BOREHOLE_PICKS_20261010", borehole_id="BH-1", conditions="скв. 12"),
              dict(producer="CURATED_BOREHOLE_PICKS_20261010", borehole_id="BH-2", conditions="скв. 40"))
    wpp.clusters(ev)
    assert [x["copy_role"] for x in ev] == ["PRIMARY", "SAME_PAGE_VALUE_CANDIDATE"]
    g = counted(wpp, ev)[("C1", "THICKNESS", "PKS", "-", "MEASUREMENT")]
    assert g["n_evidence"] == 2 and g["n_match_candidates"] == 1


def test_equal_number_in_lab_and_massif_stays_in_both_branches(wpp):
    ev = rows(dict(producer="PHASE1_CATALOGUE", module="C2", parameter="E", target="SYLVINITE", scale="LAB",
                   branch="A", si_min="1e+10", si_max="1e+10", si_unit="Pa"),
              dict(module="C2", parameter="E", target="SYLVINITE", scale="MASSIF", branch="B", si_min="1e+10",
                   si_max="1e+10", si_unit="Pa", status="AUTO_EXTRACTED_UNREVIEWED"))
    wpp.clusters(ev)
    assert ev[1]["copy_role"] == "SAME_PAGE_VALUE_CANDIDATE"          # rule v1 dropped it as SAME_NUMBER_OTHER_PRODUCER
    g = counted(wpp, ev)
    assert g[("C2", "E", "SYLVINITE", "A", "MEASUREMENT")]["n_evidence"] == 1
    assert g[("C2", "E", "SYLVINITE", "B", "MEASUREMENT")]["n_evidence"] == 1


def test_different_cells_of_one_page_both_count(wpp):
    ev = rows(dict(locator="табл. 2, строка 1", material_as_printed="КрII"),
              dict(locator="табл. 2, строка 3", material_as_printed="КрII"))
    wpp.clusters(ev)
    assert counted(wpp, ev)[("C1", "THICKNESS", "PKS", "-", "MEASUREMENT")]["n_evidence"] == 2


def test_confirmed_reextraction_counts_once_and_an_unpaired_equal_number_stays(wpp):
    ev = rows(dict(rec="high_full:P1:001"), dict(producer="CLAUDE_DOUBLE_ENTRY", rec="double:P1:001"))
    wpp.clusters(ev, {"double:P1:001": "high_full:P1:001"})
    assert [x["copy_role"] for x in ev] == ["PRIMARY", "CONFIRMED_REEXTRACTION"]
    assert counted(wpp, ev)[("C1", "THICKNESS", "PKS", "-", "MEASUREMENT")]["n_evidence"] == 1
    ev = rows(dict(rec="high_full:P1:001"), dict(producer="CLAUDE_DOUBLE_ENTRY", rec="double:P1:009"))
    wpp.clusters(ev, {"double:P1:001": "high_full:P1:001"})              # not the MATCH partner: a candidate
    assert counted(wpp, ev)[("C1", "THICKNESS", "PKS", "-", "MEASUREMENT")]["n_evidence"] == 2


def test_cited_copy_is_excluded_only_with_a_resolved_link_to_the_primary(wpp):
    spec = (dict(source_id="VKM-SRC-011"),
            dict(source_id="VKM-SRC-045", page_id="VKM-SRC-045:p0010", origin="CITED", cited_ref="[12]"))
    ev = rows(*[dict(s) for s in spec])
    wpp.clusters(ev, {}, {"WPV-000002": {"VKM-SRC-011"}})
    assert ev[1]["copy_role"] == "COPY_CITED_CONFIRMED"
    assert counted(wpp, ev)[("C1", "THICKNESS", "PKS", "-", "MEASUREMENT")]["n_evidence"] == 1
    ev = rows(*[dict(s) for s in spec])
    wpp.clusters(ev, {}, {"WPV-000002": {"VKM-SRC-099"}})               # cites another work: equal number only
    assert ev[1]["copy_role"] == "COPY_CITED_CANDIDATE"
    ev = rows(dict(spec[0]), dict(spec[1], scale="LAB"))                # another scale: not the same measurement
    wpp.clusters(ev, {}, {"WPV-000002": {"VKM-SRC-011"}})
    assert ev[1]["copy_role"] == "COPY_CITED_CANDIDATE"


# --------------------------------------------------------------------------------------------- origin and FACT
def test_origin_classes():
    assert PR.origin_class("ORIGINAL") == PR.origin_class("ORIGINAL (форма модели — по [75])") == PR.ORIGINAL
    for o in ("UNKNOWN", "", None, "ORIGINAL_OR_UNSTATED", "UNSTATED (model input)", "EXTERNAL (v1 EXT stream)"):
        assert PR.origin_class(o) == PR.UNKNOWN
    assert PR.origin_class("CITED: ссылка [69] на норматив") == PR.origin_class("cited") == PR.CITED


@pytest.mark.parametrize("change, reason", [
    (dict(origin="UNKNOWN"), "происхождение не установлено"),
    (dict(origin="CITED: [12]"), "пересказ"),
    (dict(status="AUTO_EXTRACTED_UNREVIEWED"), "статус строки не FACT"),
    (dict(verification="VERIFIED_PROGRAMMATIC"), "значение не проверено"),
    (dict(scale="DERIVATION"), "не только натурный масштаб"),
    (dict(site_basis="DOCUMENT_CONTEXT (WHOLE_DOCUMENT: статья о СКРУ-1)"), "по контексту документа"),
    (dict(status="INTERPOLATION"), "статус строки не FACT")])
def test_fact_needs_every_check(wpp, change, reason):
    good = rows({})
    assert wpp.world_status(good, "-", "SKRU1")[0] == "FACT"
    st, basis = wpp.world_status(rows(dict(change)), "-", "SKRU1")
    assert st == "SCENARIO_RANGE" and reason in basis
    st, basis = wpp.world_status(rows({}, dict(change)), "-", "SKRU1")       # one weak row spoils the group
    assert st == "SCENARIO_RANGE"


def test_checks_are_reported_apart():
    c = PR.checks(rows({}, dict(origin="UNKNOWN", verification="VERIFIED_PROGRAMMATIC",
                                site_basis="DOCUMENT_CONTEXT (x)")))
    assert c == {"origin_check": "HAS_UNKNOWN", "value_check": "PARTLY_REVIEWED",
                 "site_check": "SITE_FROM_DOCUMENT_CONTEXT", "scientific_use": "NOT_ESTABLISHED"}


# ------------------------------------------------------------------------------------------------- stable ids
def test_ledger_inherits_one_to_one_retires_splits_and_never_reuses_numbers():
    led = PR.IdLedger([{"id": "WPP-00001", "kind": "WPP", "natural_key": "a", "state": "ACTIVE"},
                       {"id": "WPP-00002", "kind": "WPP", "natural_key": "b", "state": "ACTIVE"},
                       {"id": "WPP-00003", "kind": "WPP", "natural_key": "c", "state": "ACTIVE"}])
    ids = led.assign("WPP", [("a|M", "a"), ("b|M", "b"), ("b|N", "b"), ("d", "d")], "v2", 5)
    assert ids["a|M"] == "WPP-00001"                                       # 1:1 → inherited
    assert {ids["b|M"], ids["b|N"], ids["d"]} == {"WPP-00004", "WPP-00005", "WPP-00006"}
    t = {r["id"]: r for r in led.table()}
    assert t["WPP-00001"]["previous_keys"] == "a" and t["WPP-00001"]["state"] == "ACTIVE"
    assert t["WPP-00002"]["state"] == "RETIRED" and t["WPP-00002"]["superseded_by"] == ";".join(
        sorted((ids["b|M"], ids["b|N"])))
    assert t["WPP-00003"]["state"] == "RETIRED" and t["WPP-00003"]["superseded_by"] == ""
    again = led.assign("WPP", [("a|M", "a"), ("b|M", "b"), ("b|N", "b"), ("d", "d"), ("c", "c")], "v3", 5)
    assert again["c"] == "WPP-00003" and t["WPP-00003"]["state"] == "ACTIVE"   # a returning key gets its id back
    with pytest.raises(ValueError):
        led.assign("WPP", [("x", "x"), ("x", "x")], "v3", 5)


WPE = dict(wpe_id="", param_id="", parameter="E", module="C2", quantity="", material_class="SYLVINITE", seam="KR2",
           scale="LAB", site_scope="VKM_REGIONAL", site_group="VKM_REGIONAL", status="FACT", evidence_type="", si_min="",
           si_max="", si_unit="Pa", conversion="x1e+09", value_as_printed="", unit_as_printed="ГПа", test_method="",
           n_samples="", conditions="", source_id="VKM-SRC-025", pdf_page="57", printed_page="", locator="",
           catalogue="", catalogue_row_id="", transfer_status_SKRU1="", original_or_cited="ORIGINAL", confidence="",
           phase1_notes="")
LAW = dict(row_id="", law_id="L01", rheo_ids="", family="NORTON", equation_as_printed="", equation_normalized="",
           parameter="A", value_as_printed="", unit_as_printed="", si_min="", si_max="", si_unit="", conversion="x1",
           lithology_as_printed="сильвинит", material_class="SYLVINITE", scale="CALIBRATED_EFFECTIVE_MODEL",
           branch="B", calibration_data="", stress_range="", time_range="", temperature="", n_samples="",
           site_as_printed="", site_norm="SKRU1", skru1_flag="", status="MODEL_CHOICE", original_or_cited="ORIGINAL",
           cited_work="", source_id="VKM-SRC-036", page_id="VKM-SRC-036:p0010", locator="", quote="",
           verified_on_page="YES", conflict_ids="", notes="")


def _write(path: Path, rs: list[dict]):
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(rs[0]), lineterminator="\n")
        w.writeheader()
        w.writerows(rs)


@pytest.fixture()
def canon(tmp_path):
    c = tmp_path / "canonical"
    _write(c / "WORLD_PARAMETERS" / "world_parameter_evidence.csv", [
        dict(WPE, wpe_id="WPE-00001", si_min="1.08e+10", si_max="1.08e+10", value_as_printed="10,8"),
        dict(WPE, wpe_id="WPE-00002", scale="MASSIF", si_min="4e+09", si_max="4e+09", value_as_printed="4"),
        dict(WPE, wpe_id="WPE-00003", scale="CALIBRATED_EFFECTIVE_MODEL", si_min="4e+08", si_max="4e+08",
             value_as_printed="0,4", source_id="VKM-SRC-012"),
        dict(WPE, wpe_id="WPE-00004", scale="MASSIF", site_scope="UNKNOWN", site_group="GENERAL_OR_UNSTATED",
             si_min="9e+09", si_max="9e+09", value_as_printed="9", source_id="VKM-SRC-099")])
    _write(c / "WORLD_PARAMETERS" / "curated" / "c2_creep_laws.csv", [
        dict(LAW, row_id="CL-1", family="OVERSTRESS", parameter="η", equation_normalized="ε̇ = ⟨F⟩/η", si_min="1e+15",
             si_max="1e+15", si_unit="Pa·s"),
        dict(LAW, row_id="CL-2", family="OVERSTRESS", parameter="η", equation_normalized="λ̇ = [(σe/σy)^(1/m) − 1]/η",
             si_min="1e+06", si_max="1e+06", si_unit="s"),
        dict(LAW, row_id="CL-3", family="OVERSTRESS", parameter="η", equation_normalized="λ̇ = [(σe/σy)^(1/m) − 1]/η",
             si_min="2e+06", si_max="2e+06", si_unit="s", site_norm="")])
    return c


def _passport(wpp, canon):
    out = canon / "WORLD_PASSPORT"
    files = wpp.build(canon, ["high_full"], None)
    out.mkdir(parents=True, exist_ok=True)
    for k, v in files.items():
        (out / k).write_bytes(v)
    return files


def _table(files, name):
    return list(csv.DictReader(files[name].decode("utf-8").splitlines()))


def test_priors_keep_kinds_units_definitions_and_unstated_sites_apart(wpp, wpr, canon):
    _passport(wpp, canon)
    pri = _table(wpr.build(canon), "world_priors_proposal.csv")
    e_b = {(r["evidence_kind"], r["site_group"]): r for r in pri
           if r["parameter"] == "E" and r["target"] == "SYLVINITE" and r["branch_or_hypothesis"] == "B"}
    assert set(e_b) == {("MEASUREMENT", "VKM"), ("PUBLISHED_MODEL", "VKM"), ("MEASUREMENT", "SITE_UNSTATED")}
    assert (e_b[("MEASUREMENT", "VKM")]["proposed_min"], e_b[("PUBLISHED_MODEL", "VKM")]["proposed_min"]) == \
        ("4e+09", "4e+08")                                                   # massif ≠ model parameter
    assert e_b[("MEASUREMENT", "SITE_UNSTATED")]["proposed_min"] == "9e+09"   # unstated site ≠ VKM context
    eta = [r for r in pri if r["parameter"].startswith("CREEP.OVERSTRESS")]
    assert len(eta) == 3 and len({r["si_unit"] for r in eta}) == 2
    assert {r["site_group"] for r in eta} == {"SKRU1", "GENERAL_OR_UNSTATED"}
    assert all(r["approval"] == "PROPOSAL_NOT_APPROVED" for r in pri)
    reg = _table(wpp.build(canon, ["high_full"], None), "world_passport.csv")
    eta_reg = [r for r in reg if r["parameter"].startswith("CREEP.OVERSTRESS") and r["site_group"] == "SKRU1"]
    assert len(eta_reg) == 2 and len({r["law_definition"] for r in eta_reg}) == 2


def test_ids_stay_when_a_row_is_added(wpp, wpr, canon):
    first = _passport(wpp, canon)
    pri1 = wpr.build(canon)
    for k, v in pri1.items():
        (canon / "WORLD_PASSPORT" / k).write_bytes(v)
    ev1 = {(r["producer"], r["producer_record_id"]): r["ev_id"] for r in _table(first, "passport_evidence.csv")}
    reg1 = {r["natural_key"]: r["passport_id"] for r in _table(first, "world_passport.csv")}
    pr1 = {r["natural_key"]: r["prior_id"] for r in _table(pri1, "world_priors_proposal.csv")}
    p = canon / "WORLD_PARAMETERS" / "world_parameter_evidence.csv"       # a row that sorts before all others
    old = p.read_text(encoding="utf-8")
    p.write_text(old + ",".join(f'"{v}"' for v in dict(WPE, wpe_id="WPE-00000", module="C1", parameter="DEPTH",
                                                        si_min="300", si_max="300", si_unit="m").values()) + "\n",
                 encoding="utf-8")
    second = _passport(wpp, canon)
    ev2 = {(r["producer"], r["producer_record_id"]): r["ev_id"] for r in _table(second, "passport_evidence.csv")}
    assert all(ev2[k] == v for k, v in ev1.items())                                  # nothing renumbered
    assert ev2[("PHASE1_CATALOGUE", "WPE-00000")] == f"WPV-{len(ev1) + 1:06d}"     # the new row: a new number
    reg2 = {r["natural_key"]: r["passport_id"] for r in _table(second, "world_passport.csv")}
    assert all(reg2[k] == v for k, v in reg1.items() if k in reg2)
    pr2 = {r["natural_key"]: r["prior_id"] for r in _table(wpr.build(canon), "world_priors_proposal.csv")}
    assert all(pr2[k] == v for k, v in pr1.items() if k in pr2)
    assert second["passport_id_ledger.csv"] == wpp.build(canon, ["high_full"], None)["passport_id_ledger.csv"]


def test_receipts_list_the_inputs_actually_read_and_the_code(wpp, wpr, canon):
    import json
    files = _passport(wpp, canon)
    rec = json.loads(files["build_receipt.json"])
    assert rec["inputs"]["WORLD_PARAMETERS/curated/c2_creep_laws.csv"] != "ABSENT"
    for k in ("GEOLOGY_COORDS/stratigraphic_units.csv", "MINING/spatial_hierarchy.csv", "BOREHOLES/borehole_picks.csv",
              "BOREHOLES/borehole_catalog.csv", "WORLD_EXTRACTION/high_full/accepted.jsonl",
              "WORLD_PASSPORT/passport_id_ledger.csv", "WORLD_PASSPORT/evidence_id_ledger.csv"):
        assert k in rec["inputs"]                                    # read (or recorded ABSENT), never silently skipped
    assert set(rec["code_sha256"]) >= {"scripts/build_world_passport.py", "src/vkm_world/extraction/passport_rules.py"}
    assert all("c4_map" not in k for k in rec["inputs"])            # only files actually read
    prec = json.loads(wpr.build(canon)["world_priors_proposal_receipt.json"])
    assert {"WORLD_PASSPORT/passport_evidence.csv", "WORLD_PARAMETERS/curated/c3_hypotheses.csv",
            "WORLD_PASSPORT/priors_id_ledger.csv"} <= set(prec["inputs"])
    assert "WORLD_PASSPORT/world_passport.csv" not in prec["inputs"]   # v1 listed it but never read it


def test_pick_without_status_or_origin_is_unknown(wpp, canon):
    pick = dict(pick_id="PK-1", borehole_id="", borehole_id_as_printed="скв. 12", unit_as_printed="ПКС",
                unit_canonical_guess="ПКС", pick_kind="STRAT_BOUNDARY", reference="", top_depth_m="251.5",
                bottom_depth_m="", top_abs_m="", bottom_abs_m="", thickness_m="", value_as_printed="",
                source_id="VKM-SRC-011", locator="", extraction_method="TEXT_LAYER", status="", scope="SKRU1",
                scale="FIELD", notes="", quote="")
    _write(canon / "BOREHOLES" / "borehole_picks.csv", [pick])
    ev = {r["producer_record_id"]: r for r in _table(wpp.build(canon, ["high_full"], None), "passport_evidence.csv")}
    assert (ev["PK-1-DEPTH_TOP"]["status"], ev["PK-1-DEPTH_TOP"]["origin"]) == ("UNKNOWN", "UNKNOWN")
