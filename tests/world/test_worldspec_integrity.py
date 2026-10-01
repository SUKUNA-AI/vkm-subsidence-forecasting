"""Structural integrity of synthetic WorldSpec records; no scientific execution."""
from datetime import date

import pytest
from pydantic import ValidationError

from vkm_world.chronology.events import Event
from vkm_world.core.provenance import (EpistemicStatus as S, Provenance, Quantity, Scale, Scope,
                                       SourceRef, SpatialSupport, TemporalSupport, Uncertainty)
from vkm_world.geology.boreholes import BoreholeRecord, Location, PickObservation
from vkm_world.geology.horizons import Horizon, HorizonRepresentation, HorizonRepresentationKind
from vkm_world.materials.parameters import MaterialParameter
from vkm_world.mathmeta.models import MathModelRecord
from vkm_world.mining.backfill import BackfillRecord
from vkm_world.mining.objects import MiningObject
from vkm_world.observations.catalog import ObservationOperatorSpec
from vkm_world.physics.processes import ProcessDefinition
from vkm_world.spatial.crs import CoordinateTransform
from vkm_world.worldspec import io
from vkm_world.worldspec.model import WorldMeta, WorldSpec, empty_world


def choice(**kwargs):
    return Provenance(status=S.MODEL_CHOICE, rationale="synthetic integrity fixture",
                      scale=Scale.DESIGN, scope=Scope.PROJECT, **kwargs)


def world(**kwargs):
    return WorldSpec(meta=WorldMeta(world_id="SYNTHETIC", title="integrity fixture"), **kwargs)


def material(**kwargs):
    return MaterialParameter(id="MP-1", provenance=choice(), variable="creep_param_A",
                             material="synthetic salt", quantity=Quantity(
                                 name="A", unit="unresolved archival unit", value=1, provenance=choice()), **kwargs)


def formula(**kwargs):
    return MathModelRecord(model_id="MM-SYNTHETIC", name_ru="synthetic formula", math_class="ALGEBRAIC",
                           equation_plain="a=b", variables="a:synthetic[m]", source_ids="EXT-SRC-001",
                           locator="synthetic fixture", origin="DERIVED_THIS_PROJECT", **kwargs)


def test_empty_design_and_unresolved_archival_units_are_preserved():
    assert empty_world("DESIGN", "empty design").validate_world() == []
    assert world(materials=[material()]).validate_world() == []


@pytest.mark.parametrize("collection,item", [
    ("processes", ProcessDefinition(id="P", provenance=choice(), domain="rheology", causal_role="fixture",
                                    math_models=("MM-MISSING",))),
    ("observation_operators", ObservationOperatorSpec(id="OP", provenance=choice(), modality="other",
                                                     state_inputs=(), output="fixture",
                                                     math_models=("MM-MISSING",))),
    ("mining_objects", MiningObject(id="M", provenance=choice(), kind="other", seams=("U-MISSING",))),
    ("mining_objects", MiningObject(id="M", provenance=choice(), kind="other", spatial_node="S-MISSING")),
])
def test_explicit_references_fail_even_with_empty_registry(collection, item):
    assert world(**{collection: [item]}).validate_world()


def test_law_references_resolve_exact_identifiers_without_namespace_conversion():
    m = material(law_id="MR-RHEO-SYNTHETIC")
    w = world(materials=[m], math_models=[formula()])
    assert any("MR-RHEO-SYNTHETIC" in e for e in w.validate_world())
    assert w.validate_world(registered_laws={"MR-RHEO-SYNTHETIC"}) == []
    m.law_id = "MM-SYNTHETIC"
    assert w.validate_world() == []
    m.law_id = "MR-RHEO-MISSING"
    assert w.validate_world(registered_laws={"MR-RHEO-SYNTHETIC"})


def test_nested_world_object_ids_and_provenance_inputs_are_checked():
    rep = HorizonRepresentation(id="HR", provenance=choice(), kind="SOURCE_MAP")
    w = world(horizons=[Horizon(id="H1", provenance=choice(), representations=(rep,)),
                        Horizon(id="H2", provenance=choice(), representations=(rep,))])
    assert any("duplicate" in e and "HR" in e for e in w.validate_world())
    w = world(materials=[material()])
    w.materials[0].quantity.provenance.inputs = ("MISSING-INPUT",)
    assert any("MISSING-INPUT" in e for e in w.validate_world())


def test_picks_only_inputs_are_pick_references_but_source_map_inputs_are_locators():
    rep = HorizonRepresentation(id="HR", provenance=choice(), kind="PICKS_ONLY", inputs=("MISSING-PICK",))
    w = world(horizons=[Horizon(id="H", provenance=choice(), representations=(rep,))])
    assert any("MISSING-PICK" in e for e in w.validate_world())
    rep.inputs = ("PK",)
    w.boreholes = [BoreholeRecord(id="BH", provenance=choice())]
    w.picks = [PickObservation(id="PK", provenance=choice(), borehole_id="BH", unit_id="UNRESOLVED:fixture",
                              unit_as_printed="fixture", reference="depth_below_collar",
                              top=Quantity(name="top", unit="m", value=1, provenance=choice()))]
    assert w.validate_world() == []
    rep.kind = HorizonRepresentationKind.SOURCE_MAP
    rep.inputs = ("synthetic source figure 1, page 2",)
    assert w.validate_world() == []


@pytest.mark.parametrize("revealer", [None, "EXTRACTION_START"])
def test_revealed_by_requires_an_existing_information_event(revealer):
    physical = Event(id="PHYSICAL", event_type="GEOLOGICAL", provenance=choice(), time=TemporalSupport(),
                     revealed_by=("REVEALER",))
    events = [physical]
    if revealer:
        events.append(Event(id="REVEALER", event_type=revealer, provenance=choice(), time=TemporalSupport()))
    w = world(events=events)
    assert any("REVEALER" in e for e in w.validate_world())
    w.events = [physical, Event(id="REVEALER", event_type="PUBLICATION", provenance=choice(), time=TemporalSupport())]
    assert w.validate_world() == []


@pytest.mark.parametrize("event_type", ["BACKFILL_START", "BACKFILL_END", "BACKFILL_PERIOD"])
def test_backfill_event_references_require_a_backfill_event_kind(event_type):
    event = Event(id="EVENT", event_type="GEOLOGICAL", provenance=choice(), time=TemporalSupport())
    w = world(events=[event], mining_objects=[MiningObject(id="M", provenance=choice(), kind="other")],
              backfill=[BackfillRecord(id="BF", provenance=choice(), target_objects=("M",), events=("EVENT",))])
    assert any("EVENT" in e and "backfill" in e for e in w.validate_world())
    w.events = [Event(id="EVENT", event_type=event_type, provenance=choice(), time=TemporalSupport())]
    assert w.validate_world() == []


def test_all_nested_quantity_sources_evidence_and_spatial_support_are_checked():
    w = world(materials=[material()])
    w.materials[0].quantity.provenance = Provenance(status=S.FACT, scale=Scale.LAB, scope=Scope.NON_VKM,
        sources=(SourceRef(source_id="EXT-SRC-002", locator="synthetic", evidence_ids=("EV-SYNTHETIC",)),),
        spatial=SpatialSupport(entity_id="MISSING-SPATIAL"))
    errs = w.validate_world(registered_sources={"EXT-SRC-001"}, registered_evidence=set())
    assert any("EXT-SRC-002" in e for e in errs)
    assert any("EV-SYNTHETIC" in e for e in errs)
    assert any("MISSING-SPATIAL" in e for e in errs)


def test_nested_location_provenance_and_math_metadata_sources_are_checked():
    w = world(boreholes=[BoreholeRecord(id="BH", provenance=choice(),
        location=Location(id="LOC", provenance=Provenance(status=S.FACT,
            sources=(SourceRef(source_id="VKM-SRC-999", locator="synthetic"),))))],
        math_models=[formula(vn_ids="EV-MISSING")])
    errs = w.validate_world(registered_sources=set(), registered_evidence=set())
    assert any("VKM-SRC-999" in e for e in errs)
    assert any("EXT-SRC-001" in e for e in errs)
    assert any("EV-MISSING" in e for e in errs)


@pytest.mark.parametrize("field", ["value", "low", "high"])
@pytest.mark.parametrize("number", [float("nan"), float("inf"), -float("inf")])
def test_nonfinite_quantities_are_rejected(field, number):
    with pytest.raises(ValidationError):
        Quantity(name="synthetic", unit="m", provenance=choice(), **{field: number})


@pytest.mark.parametrize("field,value", [("params", {"std": float("nan")}), ("values", (float("inf"),))])
def test_nonfinite_uncertainty_is_rejected(field, value):
    with pytest.raises(ValidationError):
        Uncertainty(**{field: value})


def test_nonfinite_transform_parameters_are_rejected():
    with pytest.raises(ValidationError):
        CoordinateTransform(id="T", provenance=choice(), from_crs="A", to_crs="B", parameters={"x": float("nan")})


@pytest.mark.parametrize("mutation", ["nonfinite", "unknown_value", "time_order", "bad_unit"])
def test_validation_and_serialization_revalidate_mutated_models(mutation):
    m = MaterialParameter(id="MP", provenance=choice(), variable="youngs_modulus", material="synthetic salt",
                          quantity=Quantity(name="E", unit="MPa", value=1, provenance=choice()))
    w = world(materials=[m])
    assert w.validate_world() == []
    if mutation == "nonfinite":
        m.quantity.value = float("nan")
    elif mutation == "unknown_value":
        m.quantity.provenance.status = S.UNKNOWN
    elif mutation == "time_order":
        m.quantity.provenance.temporal = TemporalSupport(measurement_date=date(2020, 1, 1))
        m.quantity.provenance.temporal.available_from = date(2019, 1, 1)
    else:
        m.quantity.unit = "m"
    assert w.validate_world(), "a cached construction-time PASS must not survive invalid nested mutation"
    with pytest.raises(ValidationError):
        io.to_json(w)
