"""Integrity of public evidence tables rebuilt from frozen legacy artifacts (D-07 Musikhin digitization,
legacy source-id map). Data-free apart from the committed CSVs; the rebuild check needs the legacy git objects."""
from __future__ import annotations

import csv
import hashlib
import json
import re
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
MON = ROOT / "evidence" / "monitoring"
SRC_MAP = ROOT / "evidence" / "sources" / "legacy_source_id_map.csv"
BUILDER = ROOT / "scripts" / "build_evidence_from_legacy.py"
LEGACY_COMMIT = "d54025d4c47b79b864076a33a4ecf877174ec922"


def _rows(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8", newline="") as stream:
        return list(csv.DictReader(stream))


def test_musikhin_profiles_contract():
    rows = _rows(MON / "musikhin_vkm_src002_p15_profiles.csv")
    assert len(rows) == 258
    ids = [r["id"] for r in rows]
    assert len(set(ids)) == len(ids)
    assert all(re.fullmatch(r"OBS-MUS-P15-(1|5|17|6)-(LEVELLING|INSAR)-\d{3}", i) for i in ids)
    assert {r["source_id"] for r in rows} == {"VKM-SRC-002"} and {r["locator"] for r in rows} == {"PDF p.15"}
    assert {r["method"] for r in rows} == {"levelling", "insar"}
    assert {(r["interval_start"], r["interval_end"]) for r in rows} == {("2011", "2016"), ("2015", "2016")}
    for r in rows:
        has_value = r["value_mm"] != ""
        assert (r["status"] == "FACT") == has_value and (r["status"] == "UNKNOWN") == (not has_value)
        assert (r["digitization_status"] == "unresolved_occlusion") == (not has_value)   # unknown stays unknown
        assert (r["digitization_uncertainty_mm"] != "") == has_value
        if has_value:
            assert float(r["digitization_uncertainty_mm"]) > 0
        assert r["scope"] == "VKM_Solikamsk (SKRU-1 or SKRU-2 not proven)"
        assert r["evidence_note"] == "discovery-only; not a time series"
    # interval displacements, not a time series: no dates, no time-series eligibility, no synthetic point mapping
    forbidden = {"date", "measurement_date", "eligible_as_t1_time_series", "mapped_synthetic_point_id"}
    assert not forbidden & set(rows[0])
    values = [float(r["value_mm"]) for r in rows if r["value_mm"]]
    assert len(values) == 253 and min(values) == -369.6 and max(values) == 4.3


def test_musikhin_modality_comparison_contract():
    rows = _rows(MON / "musikhin_modality_comparison.csv")
    assert len(rows) == 7 and {r["status"] for r in rows} == {"DERIVATION"}
    assert all(-1.0 <= float(r["pearson_correlation"]) <= 1.0 for r in rows)
    assert all(0.0 <= float(r["fraction_within_reading_bounds"]) <= 1.0 for r in rows)


def test_monitoring_manifest_hashes_match_tables():
    manifest = json.loads((MON / "musikhin_vkm_src002_p15_manifest.json").read_text(encoding="utf-8"))
    assert manifest["source"]["source_id"] == "VKM-SRC-002" and manifest["legacy_commit"] == LEGACY_COMMIT
    for out in manifest["outputs"]:
        assert hashlib.sha256((ROOT / out["path"]).read_bytes()).hexdigest() == out["sha256"], out["path"]


def test_legacy_source_id_map_contract():
    rows = _rows(SRC_MAP)
    assert [r["legacy_source_id"] for r in rows] == [f"SRC{i:02d}" for i in range(1, 12)] + ["SUP01"]
    assert len({r["vkm_src_id"] for r in rows}) == len(rows)
    assert all(re.fullmatch(r"VKM-SRC-\d{3}", r["vkm_src_id"]) and re.fullmatch(r"[0-9a-f]{64}", r["sha256"])
               for r in rows)
    assert {r["legacy_source_id"]: r["vkm_src_id"] for r in rows}["SUP01"] == "VKM-SRC-002"


def _legacy_objects_available() -> bool:
    return subprocess.run(["git", "-C", str(ROOT), "cat-file", "-e", f"{LEGACY_COMMIT}^{{commit}}"],
                          capture_output=True).returncode == 0


@pytest.mark.skipif(not _legacy_objects_available(), reason="legacy commit not in this clone (git fetch origin legacy)")
def test_monitoring_tables_rebuild_identically_from_legacy_objects():
    done = subprocess.run([sys.executable, str(BUILDER), "monitoring", "--check"], capture_output=True, text=True)
    assert done.returncode == 0, done.stdout + done.stderr
