"""Integrity of the public-safe Phase-1 catalogues (evidence/, catalogues/) built by scripts/build_public_catalogues.py.

Data-free apart from the committed CSVs: checks the build manifest, schema compatibility with ``vkm_world`` and
cross-references between catalogues. It does not need the PRIVATE repository.
"""
from __future__ import annotations

import csv
import hashlib
import json
import re
import sys
from pathlib import Path

import pytest

from vkm_world.core.provenance import EpistemicStatus as S
from vkm_world.core.provenance import Provenance
from vkm_world.core.provenance import SpatialLevel as L
from vkm_world.governance.leakage import FORBIDDEN_COLUMNS
from vkm_world.mathmeta.models import MathModelRecord
from vkm_world.physics.processes import ExecutionStatus
from vkm_world.spatial.hierarchy import SpatialNode, hierarchy_errors

ROOT = Path(__file__).resolve().parents[2]
MANIFEST = ROOT / "evidence" / "PUBLIC_CATALOGUE_MANIFEST.json"
REGISTRY = ROOT / "catalogues" / "mathematics" / "MATHEMATICAL_MODEL_REGISTRY.csv"
MODEL_ID = re.compile(r"MM-[A-Z0-9]+(?:-[A-Z0-9]+)*-\d{3}")
csv.field_size_limit(sys.maxsize)


def _rows(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8", newline="") as stream:
        return list(csv.DictReader(stream))


def _manifest() -> dict:
    return json.loads(MANIFEST.read_text(encoding="utf-8"))


def test_manifest_matches_committed_catalogues():
    m = _manifest()
    assert m["leakage_problems"] == []
    assert set(m["dropped_columns"]) == FORBIDDEN_COLUMNS
    mapping = json.loads((ROOT / "scripts" / "public_catalogue_map.json").read_text(encoding="utf-8"))
    assert {f["target"] for f in m["files"]} == set(mapping.values())
    for f in m["files"]:
        assert f["status"] == "OK", f
        target = ROOT / f["target"]
        assert hashlib.sha256(target.read_bytes()).hexdigest() == f["target_sha256"], f["target"]


# link / long-form tables whose first column legitimately repeats (one row per membership, lineage version, site group)
LINK_TABLES = {"evidence/sources/source_family_membership.csv", "evidence/sources/repeated_secondary_citations.csv",
               "evidence/monitoring/monitoring_systems_by_site.csv"}
CSV_TARGETS = sorted(t for t in json.loads((ROOT / "scripts" / "public_catalogue_map.json").read_text(encoding="utf-8"))
                     .values() if t.endswith(".csv"))


@pytest.mark.parametrize("target", CSV_TARGETS)
def test_public_catalogue_has_no_quote_columns_and_unique_ids(target):
    path = ROOT / target
    with path.open(encoding="utf-8", newline="") as stream:
        header = next(csv.reader(stream))
    assert not FORBIDDEN_COLUMNS & {h.strip().lower() for h in header}
    rows = _rows(path)
    assert rows, f"{target} is empty"
    if target in LINK_TABLES:
        return
    ids = [r[header[0]] for r in rows]          # every other catalogue is keyed by its first column
    dups = sorted({i for i in ids if ids.count(i) > 1})
    assert not dups and all(ids), f"{target}: empty or duplicate ids {dups[:5]}"


def test_math_registry_rows_match_schema():
    rows = _rows(REGISTRY)
    assert len(rows) >= 250
    ids = [r["model_id"] for r in rows]
    assert len(set(ids)) == len(ids)
    valid_exec = {e.value for e in ExecutionStatus}
    for r in rows:
        data = {k: (v if v != "" else None) for k, v in r.items()}
        for k in ("variables", "equation_plain", "source_ids", "locator", "name_ru"):
            data[k] = r[k]
        MathModelRecord(**data)   # raises on a formula without source/locator or an unknown class/origin
        assert set(r["future_execution"].split(";")) <= valid_exec, r["model_id"]
        assert r["status"] in {s.value for s in S}, r["model_id"]


@pytest.mark.parametrize("path", ["catalogues/physics/physics_coverage_and_execution_matrix.csv",
                                  "catalogues/observations/observation_operator_design.csv",
                                  "catalogues/mathematics/formula_conflicts.csv",
                                  "catalogues/mathematics/record_to_model_map.csv"])
def test_model_references_resolve(path):
    registry = {r["model_id"] for r in _rows(REGISTRY)}
    refs = set(MODEL_ID.findall((ROOT / path).read_text(encoding="utf-8")))
    assert refs, f"{path} references no models"
    assert refs <= registry, sorted(refs - registry)[:10]


def test_spatial_hierarchy_catalogue_satisfies_schema():
    rows = _rows(ROOT / "evidence" / "mining" / "spatial_hierarchy.csv")
    prov = Provenance(status=S.UNKNOWN)   # structure-only check; provenance is carried by the catalogue columns
    nodes = [SpatialNode(id=r["entity_id"], name=r["name"], level=L(r["level"]), parent_id=r["parent_id"] or None,
                         also_within=tuple(x.strip() for x in r["also_within"].split(";") if x.strip()),
                         provenance=prov) for r in rows]
    assert len(nodes) >= 600
    assert hierarchy_errors(nodes) == []
