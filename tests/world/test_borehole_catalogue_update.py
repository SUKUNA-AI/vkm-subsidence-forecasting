"""Phase 1 borehole catalogue updated with the page-review picks (scripts/update_borehole_catalogue.py) on a synthetic
PRIVATE tree: Phase 1 rows are kept byte for byte, empty cells are filled, a Phase 1 value is never overwritten (conflict
issue), boreholes new to the catalogue get their own rows (a group stays a group), possible identities are flagged and not
merged, the run is idempotent, and the world passport reads each review pick once."""
from __future__ import annotations

import csv
import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

CAT_COLS = ("borehole_id,alternate_ids,aliases_qualified,type,spatial_entity_id,sources,locators,mine_or_site,"
            "site_attribution_basis,site_attribution_status,site_attribution_method,location_description,x,y,"
            "crs_as_printed,xy_locator,xy_status,xy_method,xy_uncertainty,xy_alternative_readings,map_reference,"
            "collar_elevation,collar_elevation_as_printed,collar_elevation_status,collar_elevation_basis,"
            "implied_rkb_elevation_derivation,total_depth,total_depth_note,date,stratigraphic_picks_summary,"
            "seam_picks_summary,other_pick_observations,n_pick_rows,pick_references,lithology,logging,core,acoustic,vp,"
            "gamma,lab_data,other_evidence_mentions,uncertainty,provenance_vn_ids,extraction_methods,quote_checks,"
            "visual_check,identity_basis,identity_confidence,area_context,n_mentions,status,scope,scale,spatial_level,"
            "confidence,usable_for_3d,usable_grade,usable_reason,unresolved_questions,notes,quote").split(",")
PICK_COLS = ("pick_id,borehole_id,borehole_id_as_printed,unit_as_printed,unit_canonical_guess,pick_kind,reference,"
             "reference_detail,top_depth_m,bottom_depth_m,top_abs_m,bottom_abs_m,thickness_m,value_as_printed,"
             "tvd_as_printed,units,source_id,locator,vn_id,extraction_method,quote_check,visual_check,pick_quality,"
             "order_check,status,scope,scale,confidence,notes,quote").split(",")
ISSUE_COLS = ("issue_id,issue_type,borehole_ids,ids_as_printed,sources,locators,vn_ids,description,evidence_for_identity,"
              "evidence_against_identity,decision,identity_confidence,what_would_resolve,issue_status,quote").split(",")
REVIEW_COLS = ["pick_id", "pick_key", "group", "page_id", "source_id", "borehole_as_printed", "borehole_id_catalogue",
               "site_as_printed", "site_norm", "unit_as_printed", "unit_canonical", "pick_kind", "top_depth_m",
               "bottom_depth_m", "top_abs_m", "bottom_abs_m", "thickness_m", "collar_elevation_m", "total_depth_m", "x",
               "y", "crs_as_printed", "value_as_printed", "read_from", "status", "scale", "confidence", "verification",
               "locator", "quote", "notes"]


def _load(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _write(path: Path, cols: list[str], rows: list[dict], crlf: bool = True):
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=cols, lineterminator="\r\n" if crlf else "\n")
        w.writeheader()
        w.writerows([{c: r.get(c, "") for c in cols} for r in rows])


def _rows(path: Path) -> dict[str, dict]:
    with open(path, encoding="utf-8", newline="") as f:
        rows = list(csv.DictReader(f))
    return {r[next(iter(r))]: r for r in rows}


def _tree(tmp_path: Path) -> Path:
    c = tmp_path / "canonical"
    bh = c / "BOREHOLES"
    base = dict(status="FACT", scale="FIELD", spatial_level="borehole", site_attribution_status="FACT",
                usable_for_3d="NO", usable_reason="нет отбивок", n_pick_rows="1", sources="VKM-SRC-012",
                extraction_methods="VISUAL_READ", type="exploration")
    _write(bh / "borehole_catalog.csv", CAT_COLS, [
        dict(base, borehole_id="BH-1", alternate_ids="12", scope="SKRU3", mine_or_site="СКРУ-3"),
        dict(base, borehole_id="BH-2", alternate_ids="40", scope="OTHER_VKM_SITE", total_depth="300",
             unresolved_questions="ISS-1")])
    _write(bh / "borehole_picks.csv", PICK_COLS, [dict(
        pick_id="PK-1", borehole_id="BH-1", unit_as_printed="ПКС", unit_canonical_guess="PKS",
        pick_kind="STRAT_BOUNDARY", reference="depth_below_collar", top_depth_m="250", source_id="VKM-SRC-012",
        units="m", vn_id="EV-VN-S012-0001", status="FACT", scope="SKRU3", scale="FIELD", quote="«250 м»")])
    _write(bh / "borehole_identity_issues.csv", ISSUE_COLS, [dict(issue_id="ISS-1", issue_type="READING_VARIANT")])
    pick = dict(group="1", source_id="VKM-SRC-048", read_from="IMAGE", status="FACT", scale="FIELD",
                confidence="HIGH", verification="ADJUDICATED_JUDGE")
    _write(c / "WORLD_PARAMETERS" / "curated" / "borehole_picks_20261010.csv", REVIEW_COLS, [
        dict(pick, pick_id="BPR-00001", page_id="VKM-SRC-048:p0010", borehole_as_printed="скв. 12",
             borehole_id_catalogue="BH-1", site_norm="SKRU3", unit_as_printed="ПКС", unit_canonical="PKS",
             pick_kind="TOP", top_depth_m="251.5", value_as_printed="251,5"),
        dict(pick, pick_id="BPR-00002", page_id="VKM-SRC-048:p0010", borehole_as_printed="скв. 12",
             borehole_id_catalogue="BH-1", site_norm="SKRU3", pick_kind="COLLAR_ELEVATION", collar_elevation_m="120.5",
             value_as_printed="+120,5"),
        dict(pick, pick_id="BPR-00003", page_id="VKM-SRC-048:p0011", borehole_as_printed="40",
             borehole_id_catalogue="BH-2", site_norm="VKM_UNSPECIFIED", pick_kind="TOTAL_DEPTH", total_depth_m="310",
             value_as_printed="310"),
        dict(pick, pick_id="BPR-00004", page_id="VKM-SRC-048:p0012", borehole_as_printed="скв. № 40",
             site_norm="VKM_UNSPECIFIED", unit_as_printed="СМТ", unit_canonical="SMT", pick_kind="THICKNESS",
             thickness_m="35", value_as_printed="35"),
        dict(pick, pick_id="BPR-00005", page_id="VKM-SRC-048:p0013",
             borehole_as_printed="во всех скважинах (перечень не дан)", site_norm="VKM_UNSPECIFIED",
             unit_as_printed="ПП", unit_canonical="PP", pick_kind="THICKNESS", thickness_m="12", value_as_printed="12")])
    return c


def test_catalogue_update_keeps_phase1_and_adds_review(tmp_path):
    upd = _load("update_borehole_catalogue")
    c = _tree(tmp_path)
    bh = c / "BOREHOLES"
    phase1_picks = (bh / "borehole_picks.csv").read_bytes()
    s = upd.update(c)
    assert not s["problems"] and s["new_rows"] == 2 and s["phase1_rows_updated"] == 2
    assert (bh / "base_phase1" / "borehole_picks.csv").read_bytes() == phase1_picks
    assert (bh / "borehole_picks.csv").read_bytes().startswith(phase1_picks)          # Phase 1 rows byte for byte
    cat = _rows(bh / "borehole_catalog.csv")
    one = cat["BH-1"]
    assert (one["collar_elevation"], one["usable_for_3d"], one["n_pick_rows"]) == ("120.5", "PARTIAL", "3")
    assert one["sources"] == "VKM-SRC-012;VKM-SRC-048" and "PAGE_REVIEW" in one["collar_elevation_basis"]
    assert cat["BH-2"]["total_depth"] == "300"                                         # never overwritten
    new = cat["BH-PR-S048-40"]
    assert (new["scope"], new["usable_for_3d"], new["type"]) == ("VKM_REGIONAL", "PARTIAL", "unknown")
    grp = next(r for k, r in cat.items() if k.startswith("BH-PR-S048-GROUP"))
    assert grp["type"].startswith("GROUP") and grp["usable_for_3d"] == "NO"
    iss = list(_rows(bh / "borehole_identity_issues.csv").values())
    conflict = next(i for i in iss if i["issue_type"] == "VALUE_CONFLICT")
    same = next(i for i in iss if i["issue_type"] == "POSSIBLE_SAME_NOT_MERGED")
    assert conflict["borehole_ids"] == "BH-2" and conflict["decision"] == "KEPT_PHASE1_VALUE"
    assert same["borehole_ids"] == "BH-PR-S048-40;BH-2" and same["decision"] == "KEPT_SEPARATE"
    assert set(cat["BH-2"]["unresolved_questions"].split(";")) == {"ISS-1", conflict["issue_id"]}
    picks = _rows(bh / "borehole_picks.csv")
    td = picks["PKR-20261010-00003"]
    assert (td["bottom_depth_m"], td["reference"], td["vn_id"]) == ("310", "depth_below_collar", "BPR-00003")
    first = {p.name: p.read_bytes() for p in bh.glob("borehole_*.csv")}
    upd.update(c)
    assert {p.name: p.read_bytes() for p in bh.glob("borehole_*.csv")} == first     # idempotent


def test_passport_reads_review_picks_once_with_catalogue_ids(tmp_path):
    upd, wpp = _load("update_borehole_catalogue"), _load("build_world_passport")
    c = _tree(tmp_path)
    upd.update(c)
    ev = list(csv.DictReader(wpp.build(c, ["high_full"], None)["passport_evidence.csv"].decode("utf-8").splitlines()))
    ids = {r["producer_record_id"]: r for r in ev}
    assert not any(k.startswith("PKR-") for k in ids)
    assert ids["BPR-00004-THICKNESS"]["borehole_id"] == "BH-PR-S048-40"
    assert ids["PK-1-DEPTH_TOP"]["si_min"] == "250"
