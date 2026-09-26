from datetime import date

import pytest

from vkm_world.core.provenance import (
    EpistemicStatus as S, EvidenceType, Provenance, Quantity, Scale, Scope, SourceRef,
    TemporalSupport, Transfer, check_scale_use, check_site_use,
)

SRC = (SourceRef(source_id="VKM-SRC-025", pdf_page=31, evidence_ids=("EV-AUD-0002",)),)


def test_unknown_stays_unknown():
    with pytest.raises(ValueError, match="UNKNOWN stays UNKNOWN"):
        Quantity(name="k", unit="m2", value=0.0, provenance=Provenance(status=S.UNKNOWN))
    q = Quantity(name="k", unit="m2", provenance=Provenance(status=S.UNKNOWN))
    assert q.value is None


def test_fact_requires_source():
    with pytest.raises(ValueError, match="requires at least one source"):
        Provenance(status=S.FACT)
    Provenance(status=S.FACT, sources=SRC)


def test_model_choice_requires_rationale():
    with pytest.raises(ValueError, match="rationale"):
        Provenance(status=S.MODEL_CHOICE)
    Provenance(status=S.MODEL_CHOICE, rationale="reference domain size")


def test_interpolation_requires_method_and_inputs():
    with pytest.raises(ValueError, match="INTERPOLATION"):
        Provenance(status=S.INTERPOLATION, method="kriging")
    Provenance(status=S.INTERPOLATION, method="ordinary kriging", inputs=("BH-1", "BH-2"))


def test_teaching_example_never_fact():
    with pytest.raises(ValueError, match="TEACHING_EXAMPLE"):
        Provenance(status=S.FACT, sources=SRC, evidence_type=EvidenceType.TEACHING_EXAMPLE)


def test_analogue_cannot_be_skru1():
    with pytest.raises(ValueError, match="ANALOGUE"):
        Provenance(status=S.ANALOGUE, sources=SRC, scope=Scope.SKRU1)


def test_range_kept_and_checked():
    q = Quantity(name="nu", unit="1", low=0.30, high=0.45,
                 provenance=Provenance(status=S.FACT, sources=SRC, scale=Scale.LAB, scope=Scope.VKM_REGIONAL))
    assert q.is_range and q.value is None
    with pytest.raises(ValueError, match="low > high"):
        Quantity(name="nu", unit="1", low=0.5, high=0.3, provenance=Provenance(status=S.FACT, sources=SRC))


def test_lab_to_massif_requires_transfer():
    lab = Quantity(name="E", unit="GPa", low=10, high=20,
                   provenance=Provenance(status=S.FACT, sources=SRC, scale=Scale.LAB, scope=Scope.VKM_REGIONAL))
    assert check_scale_use(lab, Scale.MASSIF)
    moved = Quantity(name="E", unit="GPa", low=10, high=20, provenance=Provenance(
        status=S.FACT, sources=SRC, scale=Scale.LAB, scope=Scope.VKM_REGIONAL,
        transfer=Transfer(from_scale=Scale.LAB, to_scale=Scale.MASSIF, method="ENGINEERING_ASSUMPTION k=1",
                          status=S.ENGINEERING_ASSUMPTION, rationale="no scale-effect data for this unit")))
    assert check_scale_use(moved, Scale.MASSIF) == []


def test_offsite_value_flagged():
    q = Quantity(name="lambda", unit="1", value=0.6, provenance=Provenance(
        status=S.FACT, sources=(SourceRef(source_id="VKM-SRC-029", pdf_page=128),), scope=Scope.BKPRU2))
    assert check_site_use(q)
    a = Quantity(name="lambda", unit="1", value=0.6, provenance=Provenance(
        status=S.ANALOGUE, sources=(SourceRef(source_id="VKM-SRC-029", pdf_page=128),), scope=Scope.BKPRU2))
    assert check_site_use(a) == []


def test_information_availability():
    t = TemporalSupport(measurement_date=date(2015, 6, 1), publication_date=date(2023, 1, 1),
                        available_from=date(2023, 1, 1))
    assert t.usable_at(date(2020, 1, 1)) is False
    assert t.usable_at(date(2024, 1, 1)) is True
    assert TemporalSupport().usable_at(date(2024, 1, 1)) is None
    with pytest.raises(ValueError, match="available_from precedes"):
        TemporalSupport(measurement_date=date(2015, 6, 1), available_from=date(2014, 1, 1))


def _q(scope, status=S.FACT, scale=Scale.LAB, transfer=None, name="ucs"):
    kw = {"sources": SRC} if status in (S.FACT, S.DERIVATION, S.ANALOGUE) else {}
    if status is S.DERIVATION:
        kw["method"] = "test"
    if status in (S.MODEL_CHOICE, S.ENGINEERING_ASSUMPTION):
        kw["rationale"] = "test"
    return Quantity(name=name, unit="MPa", value=20.0,
                    provenance=Provenance(status=status, scope=scope, scale=scale, transfer=transfer, **kw))


def test_pillar_zone_is_not_field_wide_skru1():
    """ATTRIBUTION-007 / TRANSFER-023: the SKRU-1/SKRU-2 pillar zone is SKRU-1 data only locally."""
    q = _q(Scope.SKRU1_SKRU2_PILLAR)
    assert check_site_use(q, Scope.SKRU1)
    assert check_site_use(q, Scope.SKRU1, local_to_pillar=True) == []
    moved = _q(Scope.SKRU1_SKRU2_PILLAR, transfer=Transfer(from_scope=Scope.SKRU1_SKRU2_PILLAR, to_scope=Scope.SKRU1,
                                                          method="pillar zone → field, stated assumption",
                                                          status=S.ENGINEERING_ASSUMPTION, rationale="test"))
    assert check_site_use(moved, Scope.SKRU1) == []
    assert check_site_use(_q(Scope.SKRU1), Scope.SKRU1) == []


@pytest.mark.parametrize("scope", [Scope.SKRU1_OR_SKRU2_UNATTRIBUTED, Scope.SOLIKAMSK_GROUP, Scope.SKRU1_SKRU2,
                                   Scope.GENERAL_METHOD])
def test_unattributed_pooled_legacy_and_general_values_need_explicit_status(scope):
    """TRANSFER-023/024: unattributed, pooled, legacy joint and literature-range FACTs are not SKRU-1 values."""
    assert check_site_use(_q(scope), Scope.SKRU1)
    assert check_site_use(_q(scope, status=S.MODEL_CHOICE), Scope.SKRU1) == []


def test_analogue_allowed_for_pooled_but_not_for_pillar_scope():
    Provenance(status=S.ANALOGUE, sources=SRC, scope=Scope.SOLIKAMSK_GROUP)
    with pytest.raises(ValueError, match="ANALOGUE cannot have an SKRU-1 scope"):
        Provenance(status=S.ANALOGUE, sources=SRC, scope=Scope.SKRU1_SKRU2_PILLAR)


def test_unstated_scale_is_not_a_free_pass():
    """TRANSFER-024: the default scale is UNSTATED and blocks use at any required scale."""
    q = _q(Scope.SKRU1, scale=Scale.UNSTATED)
    assert Provenance(status=S.FACT, sources=SRC).scale is Scale.UNSTATED
    assert check_scale_use(q, Scale.MASSIF)
    assert check_scale_use(_q(Scope.SKRU1, scale=Scale.NOT_APPLICABLE), Scale.MASSIF) == []


def test_material_parameter_requires_a_scale():
    from vkm_world.materials.parameters import MaterialParameter
    with pytest.raises(ValueError, match="must state its scale"):
        MaterialParameter(id="MP-1", variable="ucs", material="сильвинит", provenance=Provenance(status=S.FACT, sources=SRC),
                          quantity=_q(Scope.SKRU1, scale=Scale.NOT_APPLICABLE))
    MaterialParameter(id="MP-2", variable="ucs", material="сильвинит", provenance=Provenance(status=S.FACT, sources=SRC),
                      quantity=_q(Scope.SKRU1, scale=Scale.LAB))
