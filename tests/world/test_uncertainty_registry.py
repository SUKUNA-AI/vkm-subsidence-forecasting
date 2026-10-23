"""Synthetic tests of the uncertainty taxonomy, registry, per-class budget and NROY design checks.

All numbers here are synthetic; no real observation, world or solver result is used.
"""
from __future__ import annotations

import copy
import math
from datetime import date
from pathlib import Path

import pytest

from vkm_world.uncertainty import (MixedClassesError, RegistryError, Swing, UncertaintyClass, build_class_budgets,
                                   interaction_index, json_schema, swings_from_registry, to_csv, total_over_classes,
                                   validate_registry)
from vkm_world.uncertainty import nroy
from vkm_world.uncertainty.budget import assert_single_class

ROOT = Path(__file__).resolve().parents[2]


def _element(eid="UE-W-PAR-01", cls="PHYSICAL_HYPOTHESIS", kind="MATERIAL_PARAMETER", **over):
    e = {
        "element_id": eid, "title_ru": "синтетический параметр", "uncertainty_class": cls, "kind": kind,
        "arises_at": "WORLDSPEC", "affects": ["PHYSICAL_WORLD"], "description_ru": "синтетика",
        "quantity": "E", "unit": "Pa", "site_scope": "PROJECT", "scale": "MASSIF", "evidence_status": ["MODEL_CHOICE"],
        "sources": [{"source_id": "SYN-1", "locator": "p1"}],
        "representation": {"kind": "RANGE", "subjectivity": "EVIDENCE", "range": {"low": 1.0, "high": 3.0, "central": 2.0, "unit": "Pa"}},
        "measured": "MEASURED",
        "measurements": [{"experiment_id": "X1", "by_ru": "OAT", "where": "synthetic", "result_status": "MODEL_RESULT",
                          "representation_context": "toy", "effects": [{"qoi": "s_max", "unit": "mm", "central": 10.0,
                                                                         "low": 8.0, "high": 15.0}]}],
        "unknown_ru": "всё остальное",
    }
    e.update(over)
    return e


def _registry(elements):
    return {"registry_id": "SYN", "created": "2026-10-23", "target_ru": "синтетика",
            "qois": [{"qoi_id": "s_max", "name_ru": "максимум", "unit": "mm", "definition_ru": "max s"}],
            "elements": elements}


def test_valid_registry_roundtrip_and_csv():
    reg = validate_registry(_registry([_element(), _element("UE-R-MESH-01", "REPRESENTATION_CHOICE", "SPATIAL_DISCRETIZATION")]))
    assert reg.summary()["n_elements"] == 2
    again = validate_registry(reg.model_dump_json())
    assert again == reg
    csv = to_csv(reg)
    assert csv.splitlines()[0].startswith("element_id,uncertainty_class")
    assert "s_max: 8..15 mm" in csv


@pytest.mark.parametrize("over, msg", [
    ({"element_id": "UE-R-PAR-01"}, "prefix"),                                   # world element with R prefix
    ({"kind": "SOLVER_IMPLEMENTATION"}, "kind"),                                 # representation kind in world class
    ({"representation": {"kind": "UNKNOWN", "subjectivity": "NOT_REPRESENTED", "range": {"central": 1, "unit": "Pa"}}}, "UNKNOWN"),
    ({"representation": {"kind": "DISCRETE_SET", "subjectivity": "EVIDENCE",
                         "options": [{"option_id": "a", "label_ru": "a", "status": "FACT"}]}}, "two options"),
    ({"representation": {"kind": "RANGE", "subjectivity": "EVIDENCE", "range": {"low": 1, "high": 2, "unit": "Pa"},
                         "weights": {"a": 0.5, "b": 0.5}}}, "MODEL_CHOICE"),
    ({"representation": {"kind": "RANGE", "subjectivity": "MODEL_CHOICE", "range": {"low": 1, "high": 2, "unit": "Pa"}}}, "rationale"),
    ({"representation": {"kind": "DISTRIBUTION", "subjectivity": "MIXED", "rationale_ru": "r",
                         "distribution": {"family": "lognormal", "parameters": {"mu": 0, "sigma": 1}, "subjectivity": "EVIDENCE"}}}, "basis"),
    ({"sources": []}, "no source"),
    ({"evidence_status": ["UNKNOWN"]}, "UNKNOWN completed"),
    ({"measured": "NOT_MEASURED"}, "cannot carry effects"),
    ({"measured": "MEASURED", "measurements": []}, "needs a measurement"),
    ({"measured": "PENDING_PARALLEL_WORK", "measurements": []}, "session"),
])
def test_rules_reject(over, msg):
    with pytest.raises(RegistryError, match=msg):
        validate_registry(_registry([_element(**over)]))


def test_unknown_element_without_source_is_allowed():
    e = _element(evidence_status=["UNKNOWN"], sources=[], measured="NOT_MEASURED", measurements=[],
                 representation={"kind": "UNKNOWN", "subjectivity": "NOT_REPRESENTED"})
    validate_registry(_registry([e]))


def test_registry_level_problems():
    e1 = _element()
    e2 = copy.deepcopy(e1)
    with pytest.raises(RegistryError, match="duplicate"):
        validate_registry(_registry([e1, e2]))
    bad = _element()
    bad["measurements"][0]["effects"][0]["unit"] = "m"
    with pytest.raises(RegistryError, match="unit"):
        validate_registry(_registry([bad]))
    bad = _element(links={"related_elements": ["UE-W-XX-99"]})
    with pytest.raises(RegistryError, match="related"):
        validate_registry(_registry([bad]))


def test_budget_never_mixes_classes():
    w = Swing("UE-W-A-01", UncertaintyClass.PHYSICAL_HYPOTHESIS, "s", "mm", 10.0, 5.0, 30.0)
    w2 = Swing("UE-W-B-01", UncertaintyClass.PHYSICAL_HYPOTHESIS, "s", "mm", 10.0, 9.0, 12.0)
    r = Swing("UE-R-A-01", UncertaintyClass.REPRESENTATION_CHOICE, "s", "mm", 10.0, 9.9, 10.1)
    with pytest.raises(MixedClassesError):
        assert_single_class([w, r])
    with pytest.raises(MixedClassesError):
        total_over_classes([w, r])
    b = build_class_budgets([w, w2, r])
    world = b[(UncertaintyClass.PHYSICAL_HYPOTHESIS, "s", None, "")]
    rep = b[(UncertaintyClass.REPRESENTATION_CHOICE, "s", None, "")]
    assert [s.element_id for s in world.ranked()] == ["UE-W-A-01", "UE-W-B-01"]
    assert world.oat_envelope() == (5.0, 30.0)
    lo, hi = world.log_additive_envelope()
    assert math.isclose(lo, 10 * 0.5 * 0.9) and math.isclose(hi, 10 * 3.0 * 1.2)
    assert rep.oat_envelope() == (9.9, 10.1)
    assert w.magnitude() == pytest.approx(math.log(3.0))


def test_budget_separates_representation_contexts():
    a = Swing("UE-W-A-01", UncertaintyClass.PHYSICAL_HYPOTHESIS, "s", "mm", 10.0, 5.0, 30.0, representation_context="MAPDL")
    b = Swing("UE-W-B-01", UncertaintyClass.PHYSICAL_HYPOTHESIS, "s", "mm", 10.0, 9.0, 12.0, representation_context="OGS")
    budgets = build_class_budgets([a, b])
    assert len(budgets) == 2
    assert {k[3] for k in budgets} == {"MAPDL", "OGS"}


def test_source_free_evidence_status_rejected():
    rep = {"kind": "RANGE", "subjectivity": "MODEL_CHOICE", "rationale_ru": "сценарий",
           "range": {"low": 1, "high": 2, "unit": "Pa"}}
    with pytest.raises(RegistryError, match="evidence-backed"):
        validate_registry(_registry([_element(sources=[], representation=rep, evidence_status=["FACT", "MODEL_CHOICE"])]))
    validate_registry(_registry([_element(sources=[], representation=rep, evidence_status=["MODEL_CHOICE"])]))


def test_interaction_index():
    assert interaction_index(1, 2, 3, 6) == pytest.approx(0.0)
    assert interaction_index(1, 2, 3, 12) == pytest.approx(math.log(2))


def test_swings_from_registry():
    reg = validate_registry(_registry([_element()]))
    sw = swings_from_registry(reg, "s_max")
    assert len(sw) == 1 and sw[0].ratios() == (0.8, 1.5)


def test_committed_uncertainty_schema_matches_code():
    committed = ROOT / "schemas" / "uncertainty_registry.schema.json"
    assert committed.exists(), "run scripts/export_uncertainty_schema.py"
    assert committed.read_text(encoding="utf-8") == json_schema()
    committed = ROOT / "schemas" / "nroy_design.schema.json"
    assert committed.exists(), "run scripts/export_uncertainty_schema.py"
    assert committed.read_text(encoding="utf-8") == nroy.json_schema()


# ------------------------------------------------------------------ NROY design (synthetic)
def _design(**over):
    d = {
        "design_id": "SYN-NROY", "t0": "2004-12-31", "availability_mode": "SIMULATED_ENTERPRISE_VIEW",
        "availability_mode_owner_approved": True,
        "data": [
            {"dataset_id": "L1", "dataset_type": "REAL", "role": "SCREENING", "observation_world_id": "OW-LVL",
             "epochs": ["1992-12-31", "1995-12-31", "2004-12-31"], "publication_date": "2013-01-01"},
            {"dataset_id": "L1T", "dataset_type": "REAL", "role": "HELD_OUT_TARGET", "observation_world_id": "OW-LVL",
             "epochs": ["2007-12-31", "2010-12-31"], "publication_date": "2013-01-01"},
        ],
        "metrics": [{"metric_id": "M1", "definition_ru": "прирост", "unit": "mm"}],
        "tolerance": [
            {"kind": "DIGITIZATION", "uncertainty_class": "OBSERVATION", "sigma": 5.0, "unit": "mm", "status": "DERIVATION"},
            {"kind": "REPRESENTATION_ERROR", "uncertainty_class": "REPRESENTATION_CHOICE", "sigma": 2.0, "unit": "mm", "status": "DERIVATION"},
            {"kind": "MODEL_DISCREPANCY", "uncertainty_class": "EPISTEMIC_RULE", "sigma": 20.0, "unit": "mm", "status": "MODEL_CHOICE"},
        ],
        "rule": {"combination": "VARIANCE_SUM_IMPLAUSIBILITY", "threshold": 3.0, "owner_approved": True},
        "guard": {"generator_families": ["norton"], "generator_solver": "MAPDL", "generator_resolution": "dx1.3",
                  "held_out_families": ["damage"], "test_solver": "OGS", "test_resolution": "dx0.65"},
    }
    d.update(over)
    return nroy.NroyDesign.model_validate(d)


def test_nroy_design_consistent():
    assert nroy.check_design(_design()) == []


def test_nroy_design_catches_traps():
    d = _design(availability_mode="PUBLICATION_DATE")
    assert any("future leakage" in p for p in nroy.check_design(d))
    d = _design()
    d.data[0].dataset_type = "SYNTHETIC"
    assert any("never an observation" in p for p in nroy.check_design(d))
    d = _design()
    d.data[1].epochs = [date(2004, 12, 31)]
    probs = nroy.check_design(d)
    assert any("<= t0" in p for p in probs) and any("share epochs" in p for p in probs)
    d = _design()
    d.tolerance[1].uncertainty_class = UncertaintyClass.OBSERVATION
    assert any("REPRESENTATION_ERROR: class" in p for p in nroy.check_design(d))
    d = _design()
    d.guard.calibration_datasets = ["L1"]
    d.guard.test_solver = "MAPDL"
    probs = nroy.check_design(d)
    assert any("does not calibrate" in p for p in probs) and any("different from the generator" in p for p in probs)
    d = _design()
    d.rule.owner_approved = False
    assert any(p.startswith("OWNER:") for p in nroy.check_design(d))


def test_nroy_design_requires_target_sigma_and_units():
    d = _design()
    d.data = [x for x in d.data if x.role == "SCREENING"]
    assert any("no held-out target" in p for p in nroy.check_design(d))
    d = _design()
    d.tolerance[0].sigma = None
    d.tolerance[0].bound = 9.0
    assert any("VARIANCE_SUM needs sigma" in p for p in nroy.check_design(d))
    with pytest.raises(ValueError, match="bound-only"):
        nroy.implausibility(10.0, d.tolerance)
    d = _design()
    d.tolerance[0].unit = "m"
    assert any("units differ" in p for p in nroy.check_design(d))
    with pytest.raises(ValueError, match="units differ"):
        nroy.implausibility(10.0, d.tolerance, unit="mm")
    d = _design()
    d.metrics.append(nroy.Metric(metric_id="SHAPE", definition_ru="форма", unit="1"))
    for c in d.tolerance:
        c.metric_ids = ["M1"]
    assert any("SHAPE: no tolerance component" in p for p in nroy.check_design(d))
    d.tolerance[0].metric_ids = ["NOPE"]
    assert any("unknown metric ids" in p for p in nroy.check_design(d))


def test_implausibility_math_synthetic():
    comps = _design().tolerance
    i = nroy.implausibility(41.0, comps)
    assert i == pytest.approx(41.0 / math.sqrt(25 + 4 + 400))
    assert nroy.implausibility(54.0, comps, "BOUND_SUM") == pytest.approx(54.0 / 54.0)
    assert nroy.implausibility(10.0, comps, "PER_COMPONENT_ALL") == pytest.approx(0.5)
    rel = [nroy.ToleranceComponent(kind="REPRESENTATION_ERROR", uncertainty_class="REPRESENTATION_CHOICE", sigma=10.0,
                                   unit="%", relative=True, status="DERIVATION")]
    assert nroy.implausibility(6.0, rel, prediction=30.0) == pytest.approx(2.0)
    with pytest.raises(ValueError, match="relative"):
        nroy.implausibility(6.0, rel)
    rule = nroy.Rule(combination="VARIANCE_SUM_IMPLAUSIBILITY", threshold=3.0, aggregate_over_metrics="SECOND_MAX")
    assert nroy.verdict([5.0, 1.0], rule) == "NOT_RULED_OUT_YET"
    assert nroy.verdict([5.0, 4.0], rule) == "RULED_OUT"
