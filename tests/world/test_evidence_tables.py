"""Current source readings, identifiers and derived modality comparison; no historical Git runtime."""
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
BUILDER = ROOT / "scripts" / "build_monitoring_evidence.py"
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


def test_monitoring_tables_rebuild_identically_from_current_inputs():
    done = subprocess.run([sys.executable, str(BUILDER), "monitoring", "--check"], capture_output=True, text=True)
    assert done.returncode == 0, done.stdout + done.stderr


def test_current_readings_rebuild_without_git_or_legacy_checkout(tmp_path):
    import shutil
    script = tmp_path / "scripts" / BUILDER.name
    script.parent.mkdir()
    shutil.copyfile(BUILDER, script)
    for relative in ("source_readings.csv", "source_comparison.csv", "musikhin_vkm_src002_p15_profiles.csv",
                     "musikhin_modality_comparison.csv", "musikhin_vkm_src002_p15_build_manifest.json"):
        target = tmp_path / "evidence/monitoring" / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(MON / relative, target)
    assert not (tmp_path / ".git").exists()
    done = subprocess.run([sys.executable, "-I", str(script), "monitoring", "--check"],
                          capture_output=True, text=True)
    assert done.returncode == 0, done.stdout + done.stderr
    # A changed same-size source reading must fail before any derived file is replaced.
    source = tmp_path / "evidence/monitoring/source_readings.csv"
    data = source.read_bytes()
    source.write_bytes(data.replace(b"VKM", b"XKM", 1) if b"VKM" in data else data[:-1] + b"x")
    before = (tmp_path / "evidence/monitoring/musikhin_vkm_src002_p15_profiles.csv").read_bytes()
    bad = subprocess.run([sys.executable, "-I", str(script), "monitoring"], capture_output=True, text=True)
    assert bad.returncode != 0
    assert (tmp_path / "evidence/monitoring/musikhin_vkm_src002_p15_profiles.csv").read_bytes() == before
