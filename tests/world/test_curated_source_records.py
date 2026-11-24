"""Manual records survive retirement of a site-specific container without gaining factual status."""
import csv
import importlib.util
import io
import json
from pathlib import Path

import pytest

from vkm_world.extraction import curated_records as CR
from vkm_world.governance.leakage import FORBIDDEN_COLUMNS


def entry(kind="EVIDENCE", **fields):
    return {"schema": "vkm.curated_source_record/1", "record_type": kind,
            "source_table": "old/table.csv", "source_sha256": "a" * 64, "quote": "Private quote",
            "source_record": {"record_id": "LOCAL-1", "status": "UNKNOWN", **fields}}


def store(canon, rows):
    p = canon / CR.INPUT
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")


def test_unknowns_and_original_fields_are_lossless_and_private(tmp_path):
    original = entry(zero_epoch="UNKNOWN (possibly 1989)", value="", available_from="2013-12-31")
    store(tmp_path, [original])
    projected, = CR.passport_rows(CR.load(tmp_path))
    assert projected["producer_record_id"] == "LOCAL-1"
    assert projected["status"] == "UNKNOWN" and projected["si_min"] == ""
    assert json.loads(projected["source_record_json"]) == original["source_record"]
    assert projected["quote"] == original["quote"]
    assert {"quote", "source_record_json", "geometry_wkt"} <= FORBIDDEN_COLUMNS


@pytest.mark.parametrize("change", ["duplicate", "quote_in_payload", "number_in_payload", "unknown_type"])
def test_bad_migration_contract_is_rejected_before_consumption(tmp_path, change):
    row = entry()
    if change == "quote_in_payload":
        row["source_record"]["quote"] = "cannot hide a quote in the preserved row"
    elif change == "number_in_payload":
        row["source_record"]["value"] = 0  # source CSV strings must not coerce UNKNOWN / blanks to numbers
    elif change == "unknown_type":
        row["record_type"] = "FACT"
    store(tmp_path, [row, row] if change == "duplicate" else [row])
    with pytest.raises(ValueError):
        CR.load(tmp_path)


def test_equal_labels_do_not_merge_distinct_geometries():
    a = entry("OBJECT_GEOMETRY", feature_kind="TRACE", label="profile", page_id="P:1", source_id="S",
              wkt_sh1="LINESTRING (0 0, 1 1)", frame="UNKNOWN")
    b = entry("OBJECT_GEOMETRY", **dict(a["source_record"], record_id="LOCAL-2", wkt_sh1="LINESTRING (0 0, 2 2)"))
    rows = CR.object_rows([a, b])
    assert len(rows) == 2
    assert rows[0]["object_id"] != rows[1]["object_id"]
    assert rows[0]["geometry_wkt"] != rows[1]["geometry_wkt"]


def test_passport_rebuild_reads_common_input_and_keeps_quote_separate(tmp_path):
    root = Path(__file__).resolve().parents[2]
    spec = importlib.util.spec_from_file_location("curated_test_passport", root / "scripts/build_world_passport.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    store(tmp_path, [entry()])
    files = module.build(tmp_path, [], None)
    rows = list(csv.DictReader(io.StringIO(files["passport_evidence_other.csv"].decode("utf-8"))))
    assert rows[0]["producer_record_id"] == "LOCAL-1" and rows[0]["status"] == "UNKNOWN"
    receipt = json.loads(files["build_receipt.json"])
    assert receipt["inputs"][CR.INPUT] != "ABSENT"
    assert not (tmp_path / "SITE_BLOCK201_LINE12").exists()


def test_planned_fact_about_a_plan_is_not_actual_mining(tmp_path):
    root = Path(__file__).resolve().parents[2]
    spec = importlib.util.spec_from_file_location("curated_test_history", root / "scripts/build_skru1_mine_history.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    row = entry("MINING_HISTORY", object="SKRU1:BLK:201", part_of_block="north", seam="Вк",
                event="SECONDARY_MINING", start_min="UNKNOWN", start_max="UNKNOWN", end_min="UNKNOWN",
                end_max="≤2011", source_id="S", page_id="S:1", locator="p1", method_as_printed="",
                time_basis="", notes="", status="PLANNED (FACT about a plan) / UNKNOWN (execution)")
    row["consumer_origin"] = "B201"
    store(tmp_path, [row])
    history, _ = module.load_curated_context(tmp_path)
    assert history[0]["planned_or_actual"] == "PLANNED"
    assert history[0]["start_min"] is None
    assert history[0]["end_max"] == 2011
    assert history[0]["origin_row"] == "LOCAL-1" and history[0]["object_keys"] == "BLK:201"
