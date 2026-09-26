"""Lightweight schema / provenance / chronology / leakage tests of the vkm_world foundation (no numerics)."""
from datetime import date
from pathlib import Path

import pytest

from vkm_world.chronology.events import Event, EventType, chronology_errors, known_at
from vkm_world.core.provenance import (EpistemicStatus as S, Provenance, Quantity, Scale, Scope, SourceRef,
                                       SpatialLevel as L, SpatialSupport, TemporalSupport)
from vkm_world.core.units import check_unit, is_hard_error, lookup
from vkm_world.evidence.sources import CoverageLevel, SourceRecord, coverage_errors
from vkm_world.geology.boreholes import (BoreholeRecord, DepthReference, PickInterpretation, PickObservation,
                                         Usability3D, borehole_errors, ordering_errors)
from vkm_world.geology.horizons import HorizonRepresentation, HorizonRepresentationKind
from vkm_world.governance.leakage import scan
from vkm_world.materials.parameters import MaterialParameter
from vkm_world.mathmeta.models import MathModelRecord
from vkm_world.mining.objects import DesignOrActual, MiningObject, MiningObjectKind
from vkm_world.spatial.hierarchy import SpatialNode, hierarchy_errors
from vkm_world.worldspec import io
from vkm_world.worldspec.model import WorldMeta, WorldSpec

ROOT = Path(__file__).resolve().parents[2]
SRC = (SourceRef(source_id="VKM-SRC-012", pdf_page=40),)
FACT = Provenance(status=S.FACT, sources=SRC, scope=Scope.SKRU1_SKRU2)
CHOICE = Provenance(status=S.MODEL_CHOICE, rationale="test fixture")


def q(v, unit="m", prov=FACT):
    return Quantity(name="x", unit=unit, value=v, provenance=prov)


# ---------------------------------------------------------------- units metadata
def test_units_metadata():
    assert lookup("МПа").canonical == "Pa"
    assert check_unit("youngs_modulus", "ГПа") == []
    assert is_hard_error(check_unit("youngs_modulus", "мм"))         # incompatible dimension
    amb = check_unit("unit_weight", "т/м3")
    assert amb and not is_hard_error(amb)                             # ambiguous, flagged not rejected
    assert is_hard_error(check_unit("density", "попугаи"))            # unknown unit string
    assert is_hard_error(check_unit("density", None))


def test_material_parameter_rejects_bad_units_and_flags_transfer():
    with pytest.raises(ValueError):
        MaterialParameter(id="MP-1", provenance=FACT, variable="ucs", material="галит",
                          quantity=Quantity(name="ucs", unit="м", value=20, provenance=FACT))
    lab = MaterialParameter(id="MP-2", provenance=FACT, variable="ucs", material="галит",
                            quantity=Quantity(name="ucs", unit="МПа", low=18, high=30, provenance=Provenance(
                                status=S.FACT, sources=SRC, scale=Scale.LAB, scope=Scope.VKM_REGIONAL)))
    errs = lab.use_errors(Scale.MASSIF, Scope.SKRU1)
    assert any("LAB value used as MASSIF" in e for e in errs)
    assert any("VKM_REGIONAL used for SKRU1" in e for e in errs)


# ---------------------------------------------------------------- spatial hierarchy / scope
def test_spatial_hierarchy_rules():
    nodes = [SpatialNode(id="VKM", level=L.DEPOSIT, provenance=FACT),
             SpatialNode(id="SOL", level=L.DISTRICT, parent_id="VKM", provenance=FACT),
             SpatialNode(id="SKRU1", level=L.MINE, parent_id="SOL", provenance=FACT),
             SpatialNode(id="P1", level=L.PANEL, parent_id="SKRU1", provenance=CHOICE),
             SpatialNode(id="BAD", level=L.CHAMBER, parent_id="SKRU1", provenance=CHOICE),
             SpatialNode(id="ORPHAN", level=L.BLOCK, parent_id="NOPE", provenance=CHOICE)]
    errs = hierarchy_errors(nodes)
    assert any("BAD: parent level mine not allowed for chamber" in e for e in errs)
    assert any("ORPHAN: parent 'NOPE' does not exist" in e for e in errs)
    assert not any(e.startswith("P1") for e in errs)


def test_invalid_spatial_scope_in_world():
    w = WorldSpec(meta=WorldMeta(world_id="T", title="t"),
                  spatial_nodes=[SpatialNode(id="VKM", level=L.DEPOSIT, provenance=FACT)],
                  unknowns=[])
    w.boreholes.append(BoreholeRecord(id="BH-1", provenance=Provenance(
        status=S.FACT, sources=SRC, spatial=SpatialSupport(level=L.BOREHOLE, entity_id="NOT-IN-TREE"))))
    assert any("spatial support entity 'NOT-IN-TREE'" in e for e in w.validate_world())


# ---------------------------------------------------------------- boreholes
def test_duplicate_and_contradictory_borehole_ids():
    holes = [BoreholeRecord(id="BH-75", aliases=("75",), provenance=FACT),
             BoreholeRecord(id="BH-75", provenance=FACT),
             BoreholeRecord(id="BH-X", aliases=("75",), provenance=FACT)]
    errs = borehole_errors(holes, [])
    assert "duplicate borehole id BH-75" in errs
    assert any("alias '75' claimed by both" in e for e in errs)


def test_usable_3d_requires_location():
    errs = borehole_errors([BoreholeRecord(id="BH-1", usable_for_3d=Usability3D.YES, provenance=FACT)], [])
    assert any("usable_for_3d=YES but no located collar" in e for e in errs)


def test_pick_geometry_and_ordering_is_flagged_not_deleted():
    with pytest.raises(ValueError, match="below bottom"):
        PickObservation(id="PK-0", borehole_id="BH-1", unit_id="KRII", unit_as_printed="КрII",
                        reference=DepthReference.DEPTH_BELOW_COLLAR, top=q(300.0), bottom=q(290.0), provenance=FACT)
    picks = [PickObservation(id="PK-1", borehole_id="BH-1", unit_id="SMT", unit_as_printed="СМТ",
                             reference=DepthReference.DEPTH_BELOW_COLLAR, top=q(250.0), provenance=FACT),
             PickObservation(id="PK-2", borehole_id="BH-1", unit_id="PKS", unit_as_printed="ПКС",
                             reference=DepthReference.DEPTH_BELOW_COLLAR, top=q(200.0), provenance=FACT)]
    errs = ordering_errors(picks, {"SMT": 0, "PKS": 1})
    assert errs and "inverted order" in errs[0]


def test_interpretation_must_cite_existing_observations():
    with pytest.raises(ValueError):
        PickInterpretation(id="PI-1", borehole_id="BH-1", unit_id="KRII", from_observations=(),
                           rationale="x", provenance=CHOICE)
    pi = PickInterpretation(id="PI-2", borehole_id="BH-1", unit_id="KRII", from_observations=("PK-404",),
                            rationale="x", provenance=CHOICE)
    errs = borehole_errors([BoreholeRecord(id="BH-1", provenance=FACT)], [], [pi])
    assert any("unknown observation 'PK-404'" in e for e in errs)


def test_interpolated_surface_must_be_interpolation():
    with pytest.raises(ValueError):
        HorizonRepresentation(id="HR-1", kind=HorizonRepresentationKind.INTERPOLATED_SURFACE, provenance=FACT,
                              method="kriging", inputs=("PK-1",))


# ---------------------------------------------------------------- chronology
def ev(i, t, objs, d0, d1=None):
    return Event(id=i, event_type=t, objects=objs, provenance=FACT,
                 time=TemporalSupport(event_date=d0, event_date_end=d1))


def test_backfill_before_excavation_and_before_commissioning():
    events = [ev("E1", EventType.MINE_COMMISSIONING, ("SKRU1",), date(1934, 1, 1)),
              ev("E2", EventType.EXTRACTION_START, ("BLK-1",), date(1960, 5, 1)),
              ev("E3", EventType.BACKFILL_START, ("CH-1",), date(1959, 1, 1)),
              ev("E4", EventType.EXTRACTION_START, ("BLK-2",), date(1930, 1, 1))]
    errs = chronology_errors(events, parent_of={"CH-1": "BLK-1"}, mine_of={"BLK-2": "SKRU1", "BLK-1": "SKRU1"})
    assert any("backfill of CH-1 starts 1959-01-01 before its extraction 1960-05-01" in e for e in errs)
    assert any("before commissioning of SKRU1" in e for e in errs)


def test_event_end_before_start_rejected_and_unknown_object():
    with pytest.raises(ValueError):
        ev("E9", EventType.EXTRACTION_PERIOD, ("B",), date(1970, 1, 1), date(1960, 1, 1))
    errs = chronology_errors([ev("E1", EventType.FLOODING, ("GHOST",), date(1986, 1, 1))], known_ids={"SKRU1"})
    assert any("unknown object 'GHOST'" in e for e in errs)


def test_information_availability_filter():
    e = Event(id="I1", event_type=EventType.MONITORING_CAMPAIGN, objects=(), provenance=FACT,
              time=TemporalSupport(measurement_date=date(2011, 6, 1), available_from=date(2012, 12, 1)))
    assert known_at([e], date(2012, 1, 1)) == []
    assert known_at([e], date(2013, 1, 1)) == [e]


# ---------------------------------------------------------------- mining / teaching examples
def test_teaching_example_cannot_be_site_fact():
    from vkm_world.mining.objects import mining_object_errors
    o = MiningObject(id="CH-T", kind=MiningObjectKind.CHAMBER, design_or_actual=DesignOrActual.TEACHING,
                     provenance=Provenance(status=S.FACT, sources=SRC))
    assert any("teaching example stored as FACT" in e for e in mining_object_errors([o]))


# ---------------------------------------------------------------- sources / formulas
def test_coverage_master_rules():
    recs = [SourceRecord(source_id="VKM-SRC-001", coverage_level=CoverageLevel.UNSEEN),
            SourceRecord(source_id="VKM-SRC-002", coverage_level=CoverageLevel.FULLY_REVIEWED)]
    errs = coverage_errors(recs)
    assert any("VKM-SRC-001" in e and "not allowed" in e for e in errs)
    assert any("VKM-SRC-002" in e and "basis" in e for e in errs)
    with pytest.raises(ValueError):
        SourceRecord(source_id="SRC-1")


def test_formula_requires_source_and_locator():
    with pytest.raises(ValueError):
        MathModelRecord(model_id="MM-1", name_ru="x", math_class="ALGEBRAIC", equation_plain="a=b",
                        variables="a:x[m]", source_ids="", locator="", origin="ORIGINAL_SOURCE")


# ---------------------------------------------------------------- serialisation / schema / leakage
def test_serialisation_roundtrip_is_deterministic(tmp_path):
    w = WorldSpec(meta=WorldMeta(world_id="T", title="t"),
                  spatial_nodes=[SpatialNode(id="VKM", level=L.DEPOSIT, provenance=FACT)])
    h1 = io.save(w, tmp_path / "w.json")
    w2 = io.load(tmp_path / "w.json")
    assert io.content_hash(w2) == h1 == io.content_hash(w)
    assert w2.validate_world() == []


def test_committed_schema_matches_code():
    committed = ROOT / "schemas" / "worldspec_vnext.schema.json"
    assert committed.exists(), "run scripts/export_worldspec_schema.py"
    assert committed.read_text(encoding="utf-8") == io.json_schema()


def test_public_tree_has_no_private_leakage():
    problems = scan(ROOT)
    assert problems == [], "\n".join(problems[:20])


def test_leakage_scanner_detects_violations(tmp_path):
    (tmp_path / "a.pdf").write_bytes(b"%PDF")
    (tmp_path / "t.csv").write_text("id,quote\n1,текст\n", encoding="utf-8")
    (tmp_path / "c.py").write_text("P='/home/user/" + "vkm-subsidence-forecasting_resourses/x'\n", encoding="utf-8")
    probs = scan(tmp_path, files=list(tmp_path.iterdir()))
    assert len(probs) == 3
