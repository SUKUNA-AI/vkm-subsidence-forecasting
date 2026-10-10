"""World passport, stage 2 (scripts/build_world_passport.py) and the observation catalogue
(scripts/build_world_observations.py) on a synthetic PRIVATE tree: branches A/B/N, status for the world, UNKNOWN rows,
copy clusters, computed conflicts, objects, determinism; digitized series stay DERIVATION."""
from __future__ import annotations

import csv
import importlib.util
import json
from pathlib import Path

import pytest

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
def wob():
    return _load("build_world_observations")


def _write(path: Path, rows: list[dict]):
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0]), lineterminator="\n")
        w.writeheader()
        w.writerows(rows)


WPE = dict(wpe_id="", param_id="", parameter="E", module="C2", quantity="", material_class="SYLVINITE", seam="KR2",
           scale="LAB", site_scope="VKM_REGIONAL", site_group="VKM_REGIONAL", status="FACT", evidence_type="", si_min="",
           si_max="", si_unit="Pa", conversion="x1e+09", value_as_printed="", unit_as_printed="ГПа", test_method="",
           n_samples="", conditions="", source_id="VKM-SRC-025", pdf_page="57", printed_page="", locator="",
           catalogue="", catalogue_row_id="", transfer_status_SKRU1="", original_or_cited="ORIGINAL", confidence="",
           phase1_notes="")


@pytest.fixture()
def canon(tmp_path):
    c = tmp_path / "11_evidence_vnext" / "canonical"
    wpe = [dict(WPE, wpe_id="WPE-00001", si_min="1.08e+10", si_max="1.08e+10", value_as_printed="10,8"),
           dict(WPE, wpe_id="WPE-00002", si_min="2e+09", si_max="2e+09", value_as_printed="2", source_id="VKM-SRC-037"),
           dict(WPE, wpe_id="WPE-00003", si_min="4e+08", si_max="4e+08", scale="CALIBRATED_EFFECTIVE_MODEL",
                value_as_printed="0,4", source_id="VKM-SRC-012", pdf_page="99"),
           dict(WPE, wpe_id="WPE-00004", si_min="1.08e+10", si_max="1.08e+10", value_as_printed="10,8",
                source_id="VKM-SRC-045", original_or_cited="CITED")]
    _write(c / "WORLD_PARAMETERS" / "world_parameter_evidence.csv", wpe)
    _write(c / "WORLD_PARAMETERS" / "world_c1_thickness_evidence.csv", [dict(
        obs_id="STO-0001", unit_id="PKS", unit="Покровная каменная соль", site_scope="SKRU1", site_group="SKRU1",
        spatial_level="", observation_type="BOREHOLE", thickness_min_m="11", thickness_max_m="22",
        thickness_mean_m="", thickness_as_printed="11–22", status="FACT", scale="FIELD", transferability_to_SKRU1="",
        source_id="VKM-SRC-011", pdf_page="131", printed_page="", locator="", secondary_copy_of="")])
    _write(c / "GEOLOGY_COORDS" / "stratigraphic_units.csv", [dict(
        unit_id="PKS", name_ru="Покровная каменная соль", abbrev="ПКС", aliases="ПКС; покровная каменная соль")])
    _write(c / "MINING" / "spatial_hierarchy.csv", [dict(
        entity_id="SKRU1", name="СКРУ-1", aliases="СКРУ-1; Первый Соликамский рудник", level="mine",
        entity_type_as_source="рудник", parent_id="VKM", mine_attribution="SKRU1", site_scope="SKRU1",
        source_ids="VKM-SRC-014", status="FACT")])
    acc = [{"record_id": "high_full:P1:001", "packet_id": "P1", "kind": "PARAMETER", "page_id": "VKM-SRC-096:p0003",
            "parameter_code": "UCS", "value_min": 24.0, "value_max": 24.0, "unit_as_printed": "МПа",
            "value_as_printed": "24", "material_as_printed": "сильвинит пласта КрII", "scale": "LAB",
            "site_norm": "SKRU1", "origin": "ORIGINAL", "quote": "σсж = 24 МПа"},
           {"record_id": "high_full:P1:002", "packet_id": "P1", "kind": "ENTITY", "page_id": "VKM-SRC-096:p0003",
            "entity_name": "СКРУ-1", "entity_type": "MINE", "site_norm": "SKRU1", "quote": "рудник СКРУ-1"},
           {"record_id": "high_full:P1:003", "packet_id": "P1", "kind": "OBSERVATION", "page_id": "VKM-SRC-096:p0004",
            "parameter_code": "SUBSIDENCE_MAX", "value_min": 82.0, "value_max": 82.0, "unit_as_printed": "мм",
            "value_as_printed": "82", "scale": "FIELD", "site_norm": "SKRU2", "time_as_printed": "2009–2020",
            "quote": "оседания составили 82 мм"}]
    p = c / "WORLD_EXTRACTION" / "high_full" / "accepted.jsonl"
    p.parent.mkdir(parents=True)
    p.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in acc), encoding="utf-8")
    return c


def test_passport_branches_status_unknown_and_copies(wpp, canon):
    files = wpp.build(canon, ["high_full"], None)
    reg = list(csv.DictReader(files["world_passport.csv"].decode("utf-8").splitlines()))
    ev = list(csv.DictReader(files["passport_evidence.csv"].decode("utf-8").splitlines()))
    e_syl = {(r["site_group"], r["branch"]): r for r in reg if r["parameter"] == "E" and r["target"] == "SYLVINITE"}
    assert e_syl[("VKM_REGIONAL", "A")]["world_status"] == "SCENARIO_RANGE"          # LAB → needs a scale factor
    assert e_syl[("VKM_REGIONAL", "B")]["si_min"] == "4e+08"                           # calibrated: branch B, apart
    assert e_syl[("VKM_REGIONAL", "A")]["n_evidence"] == "2"                           # the cited copy is not counted
    roles = {r["producer_record_id"]: r["copy_role"] for r in ev}
    assert roles["WPE-00001"] == "PRIMARY" and roles["WPE-00004"] == "COPY_CITED"
    ucs = [r for r in ev if r["parameter"] == "UCS"][0]
    assert (ucs["si_min"], ucs["material_class"], ucs["branch"]) == ("2.4e+07", "SYLVINITE", "A")
    assert any(r["world_status"] == "UNKNOWN" and r["parameter"] == "PHI" and r["target"] == "COVER" for r in reg)
    c1 = [r for r in reg if r["module"] == "C1" and r["target"] == "PKS"][0]
    assert c1["world_status"] == "FACT" and c1["site_group"] == "SKRU1"
    conf = list(csv.DictReader(files["passport_conflicts.csv"].decode("utf-8").splitlines()))
    assert any(c["kind"] == "SPREAD_GT_3X" and c["parameter"] == "E" for c in conf)   # 10.8 vs 2 GPa: recorded
    objs = list(csv.DictReader(files["world_objects.csv"].decode("utf-8").splitlines()))
    assert [o for o in objs if o["object_id"] == "SKRU1"][0]["n_mentions_extraction"] == "1"
    assert files == wpp.build(canon, ["high_full"], None)                              # deterministic


def test_observation_catalogue_keeps_statuses(wob, canon):
    fig = canon / "WORLD_OBSERVATIONS" / "digitized" / "VKM-SRC-045" / "fig_4_3_4"
    _write(fig / "series.csv", [dict(series_id="045-F434-2012", epoch_or_date="2012", line_id="SKRU2_PL7",
                                     benchmark_id="Rp60", x_value="2012.5", x_unit="year", y_value="-237",
                                     y_unit="mm", sign_convention="down negative", point_kind="VERTEX")])
    (fig / "meta.json").write_text(json.dumps({"source_id": "VKM-SRC-045", "page_id": "VKM-SRC-045:p0110",
                                               "figure_label": "Рисунок 4.3.4", "site_norm": "SKRU2",
                                               "quantity": "оседание репера", "available_from": "2015"},
                                              ensure_ascii=False), encoding="utf-8")
    files = wob.build(canon, ["high_full"], None)
    obs = {r["obs_id"]: r for r in csv.DictReader(files["world_observation_catalog.csv"].decode("utf-8").splitlines())}
    d = obs["WOB-DIG-045-F434-2012"]
    assert (d["status"], d["skru1_flag"], d["modality"], d["value_min"]) == ("DERIVATION", "NO", "LEVELLING", "-237")
    t = obs["WOB-TXT-high_full:P1:003"]
    assert t["status"] == "PUBLISHED_VALUE" and t["time_start"] == "2009" and t["time_end"] == "2020"
    pts = list(csv.DictReader(files["digitized_series_points.csv"].decode("utf-8").splitlines()))
    assert pts[0]["point_id"] == "WOP-000001" and pts[0]["status"] == "DERIVATION"
    assert wob.skru1_flag("SKRU1_OR_SKRU2_UNATTRIBUTED") == "SKRU1_OR_SKRU2"


def test_adjudication_and_document_site(wpp, canon):
    acc = canon / "WORLD_EXTRACTION" / "high_full" / "accepted.jsonl"
    extra = [{"record_id": "high_full:P2:001", "packet_id": "P2", "kind": "PARAMETER", "page_id": "VKM-SRC-201:p0005",
              "parameter_code": "E", "value_min": 2.26, "value_max": 2.26, "unit_as_printed": "МПа",
              "multiplier_as_printed": "E·10⁻³", "value_as_printed": "2,26", "material_as_printed": "C $ \\Pi $ecTp",
              "scale": "LAB", "site_norm": "UNKNOWN", "origin": "ORIGINAL", "quote": "2,26"},
             {"record_id": "high_full:P2:002", "packet_id": "P2", "kind": "PARAMETER", "page_id": "VKM-SRC-201:p0005",
              "parameter_code": "UCS", "value_min": 50.0, "value_max": 50.0, "unit_as_printed": "м",
              "value_as_printed": "50", "scale": "FIELD", "site_norm": "UNKNOWN", "quote": "50 м"}]
    with open(acc, "a", encoding="utf-8") as f:
        for r in extra:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    adj = canon / "WORLD_EXTRACTION" / "adjudication"
    adj.mkdir(parents=True)
    (adj / "adjudication_20261010.jsonl").write_text("\n".join(json.dumps(v, ensure_ascii=False) for v in (
        {"record_id": "high_full:P2:001", "verdict": "CORRECTED", "corrected": {}, "material_class": "SYLVINITE",
         "value_scale_factor": 1000, "branch_hint": "A", "basis": "шапка табл. E·10⁻³"},
        {"record_id": "high_full:P2:002", "verdict": "NOT_WORLD_PARAMETER", "corrected": {}, "basis": "расстояние"},
    )) + "\n", encoding="utf-8")
    _write(canon / "WORLD_PARAMETERS" / "curated" / "source_site_ranges_20261010.csv", [dict(
        source_id="VKM-SRC-201", page_from="1", page_to="20", site_norm="SKRU3", scope_kind="WHOLE_DOCUMENT",
        basis="статья о СКРУ-3", basis_page_id="VKM-SRC-201:p0001", confidence="HIGH")])
    files = wpp.build(canon, ["high_full"], None)
    ev = {r["producer_record_id"]: r for r in csv.DictReader(files["passport_evidence.csv"].decode("utf-8").splitlines())}
    e = ev["high_full:P2:001"]
    assert (e["material_class"], e["si_min"], e["branch"], e["verification"]) == ("SYLVINITE", "2.26e+09", "A",
                                                                                  "ADJUDICATED_CORRECTED")
    assert e["site_norm"] == "SKRU3" and e["site_basis"].startswith("DOCUMENT_CONTEXT")
    assert ev["high_full:P2:002"]["module"] == "EXCLUDED"
    reg = list(csv.DictReader(files["world_passport.csv"].decode("utf-8").splitlines()))
    assert not any("high_full:P2:002" in r["evidence_ids"] for r in reg)


def test_world_priors_proposal_keeps_branches_apart(wpp, canon):
    spec = importlib.util.spec_from_file_location("build_world_priors", ROOT / "scripts" / "build_world_priors.py")
    wpr = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(wpr)
    out = canon / "WORLD_PASSPORT"
    out.mkdir(parents=True, exist_ok=True)
    for k, v in wpp.build(canon, ["high_full"], None).items():
        (out / k).write_bytes(v)
    rows = list(csv.DictReader(wpr.build(canon)["world_priors_proposal.csv"].decode("utf-8").splitlines()))
    e = {(r["branch_or_hypothesis"], r["site_group"]): r for r in rows if r["parameter"] == "E" and r["target"] == "SYLVINITE"}
    assert e[("A", "VKM")]["proposed_min"] == "2e+09" and e[("B", "VKM")]["proposed_max"] == "4e+08"
    assert any(r["parameter"] == "PHI" and r["target"] == "COVER" and r["world_status"] == "UNKNOWN" for r in rows)
