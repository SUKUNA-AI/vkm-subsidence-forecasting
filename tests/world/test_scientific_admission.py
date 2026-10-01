"""Synthetic admission and DRAFT protocol checks; no formula evaluation or source-value publication."""
from datetime import date
import importlib
import json
from pathlib import Path
import subprocess
import sys
import warnings

import pytest

from vkm_world.core.provenance import (EpistemicStatus as S, Provenance, Quantity, Scale, Scope, SourceRef,
                                       TemporalSupport, Transfer)
from vkm_world.materials.parameters import MaterialParameter, TestMethod as Method
from vkm_world.core.units import Dimension, UnitInfo
from vkm_world.mathmeta.models import MathModelRecord
from vkm_world.observations.catalog import ObservationDataset, ObservationSystem
from vkm_world.worldspec.io import content_hash
from vkm_world.worldspec.model import UnknownItem, WorldMeta, WorldSpec

ROOT = Path(__file__).resolve().parents[2]


def synthetic_case(normalized=False):
    api = importlib.import_module('vkm_world.validation.scientific')
    time = TemporalSupport(available_from=date(2020, 1, 1), precision='day')
    def prov(evidence='EV-CAL', scale=Scale.LAB, scope=Scope.NON_VKM):
        return Provenance(status=S.FACT, scale=scale, scope=scope, temporal=time.model_copy(deep=True),
                          sources=(SourceRef(source_id='EXT-SRC-001', locator='synthetic fixture', evidence_ids=(evidence,)),))
    def quantity(name, value, unit):
        return Quantity(name=name, value=value, unit=unit, provenance=prov())
    adapter = 'creep-normalized-pa-s/1' if normalized else 'creep-power-pa-s/1'
    form = api.EXACT_FORMS[adapter]
    model = MathModelRecord(model_id='MM-SYNTHETIC', name_ru='synthetic formula', math_class='ALGEBRAIC',
                            equation_plain=form['equation'], variables=form['variables'], source_ids='EXT-SRC-001',
                            locator='synthetic fixture', origin='DERIVED_THIS_PROJECT', vn_ids='EV-FORM')
    values = {'B': (0.01, '1/s'), 'n': (2.0, '1'), 'stress0': (10.0, 'Pa')} if normalized else {'A': (0.01, 'Pa^-2/s'), 'n': (2.0, '1')}
    materials = [MaterialParameter(id=f'MP-{role}', provenance=prov(), variable=f'creep_param_{role}',
                                  material='synthetic salt', law_id='MR-RHEO-SYNTHETIC', conditions='synthetic calibration',
                                  test_method='creep_constant_load', quantity=quantity(role, value, unit))
                 for role, (value, unit) in values.items()]
    world = WorldSpec(meta=WorldMeta(world_id='SYNTHETIC', title='synthetic fixture'), math_models=[model], materials=materials,
                      observation_systems=[ObservationSystem(id='SYS', provenance=prov(), modality='lab_test', observable='synthetic strain')],
                      observation_datasets=[ObservationDataset(id='DATA', provenance=prov(), system_id='SYS', time=time)])
    limits = {'stress': (1.0, 100.0, 'Pa'), 'temperature': (250.0, 350.0, 'K'), 'duration': (1.0, 1000.0, 's')}
    applicability = {key: Quantity(name=key, low=lo, high=hi, unit=unit, provenance=prov()) for key, (lo, hi, unit) in limits.items()}
    review = api.ReviewIndex(registered_sources={'EXT-SRC-001'}, registered_evidence={'EV-CAL', 'EV-FORM'},
                            registered_laws={'MR-RHEO-SYNTHETIC'}, formula_reviews={'MM-SYNTHETIC': {
                                'record_sha256': api.record_hash(model), 'evidence_id': 'EV-FORM', 'time': time}},
                            families={'EV-CAL': {'model_id': model.model_id, 'law_id': 'MR-RHEO-SYNTHETIC',
                                'parameter_hashes': {m.id: api.record_hash(m) for m in materials}, 'material': 'synthetic salt',
                                'test_method': 'creep_constant_load', 'scope': 'NON_VKM', 'scale': 'LAB',
                                'conditions': 'synthetic calibration', 'applicability': applicability, 'time': time}})
    conditions = {'stress': quantity('stress', 10.0, 'Pa'), 'temperature': quantity('temperature', 300.0, 'K'),
                  'duration': quantity('duration', 100.0, 's')}
    binding = api.ScientificUseBinding(world_sha256=content_hash(world), model_id=model.model_id,
                                      model_sha256=api.record_hash(model), review_sha256=api.record_hash(review),
                                      adapter_id=adapter, parameter_ids={role: f'MP-{role}' for role in values},
                                      family_evidence_id='EV-CAL', as_scale='LAB', for_scope='NON_VKM',
                                      origin=date(2021, 1, 1), conditions=conditions)
    return api, world, binding, review


def repin(api, world, binding, review):
    """Explicitly new reviewed fixture after a deliberate synthetic input change."""
    model = world.math_models[0]
    if model.model_id in review.formula_reviews:
        review.formula_reviews[model.model_id].record_sha256 = api.record_hash(model)
    review.families['EV-CAL'].parameter_hashes = {m.id: api.record_hash(m) for m in world.materials}
    binding.world_sha256 = content_hash(world)
    binding.model_sha256 = api.record_hash(model)
    binding.review_sha256 = api.record_hash(review)


@pytest.mark.parametrize('normalized', [False, True])
def test_two_exact_forms_are_ready_without_evaluation_or_mutation(normalized):
    api, world, binding, review = synthetic_case(normalized)
    before = content_hash(world)
    receipt = api.admit_scientific_use(world, binding, review)
    assert receipt['status'] == 'READY'
    assert receipt['readiness_scope'] == 'CONTRACT_CONSISTENCY_ONLY'
    assert receipt['receipt_sha256'] and receipt['input_hashes']['parameters']
    assert content_hash(world) == before


@pytest.mark.parametrize('change,expected', [
    ('wrong_formula', 'UNDETERMINED'), ('wrong_unit', 'BLOCKED'), ('wrong_exponent_unit', 'BLOCKED'),
    ('unknown_unit', 'UNDETERMINED'), ('unknown_value', 'UNDETERMINED'),
    ('mixed_experiment', 'BLOCKED'), ('missing_formula_review', 'UNDETERMINED'),
    ('unknown_formula_availability', 'UNDETERMINED'), ('future_availability', 'BLOCKED'),
    ('unknown_parameter_availability', 'UNDETERMINED'), ('unknown_conditions', 'UNDETERMINED'),
    ('outside_conditions', 'BLOCKED'), ('wrong_material', 'BLOCKED'), ('wrong_method', 'BLOCKED'),
    ('wrong_law', 'BLOCKED'), ('unreviewed_parameter', 'BLOCKED'),
])
def test_scientific_negative_cases(change, expected):
    api, world, binding, review = synthetic_case()
    param = world.materials[0]
    if change == 'wrong_formula': world.math_models[0].equation_plain = 'strain_rate = A * stress'
    elif change == 'wrong_unit': param.quantity.unit = 'm'
    elif change == 'wrong_exponent_unit': param.quantity.unit = 'Pa^-3/s'
    elif change == 'unknown_unit': param.quantity.unit = 'unresolved archival unit'
    elif change == 'unknown_value':
        param.quantity.provenance.status = S.UNKNOWN
        param.quantity.value = None
    elif change == 'mixed_experiment':
        param.quantity.provenance.sources = (SourceRef(source_id='EXT-SRC-001', locator='same synthetic book', evidence_ids=('EV-OTHER',)),)
        review.registered_evidence.add('EV-OTHER')
    elif change == 'missing_formula_review': review.formula_reviews.clear()
    elif change == 'unknown_formula_availability': review.formula_reviews['MM-SYNTHETIC'].time = TemporalSupport()
    elif change == 'future_availability': param.quantity.provenance.temporal.available_from = date(2022, 1, 1)
    elif change == 'unknown_parameter_availability': param.quantity.provenance.temporal = TemporalSupport()
    elif change == 'unknown_conditions': review.families['EV-CAL'].applicability.pop('temperature')
    elif change == 'outside_conditions': binding.conditions['stress'].value = 101.0
    elif change == 'wrong_material': param.material = 'different synthetic material'
    elif change == 'wrong_method': param.test_method = Method.LITERATURE
    elif change == 'wrong_law':
        param.law_id = 'MR-RHEO-OTHER'
        review.registered_laws.add('MR-RHEO-OTHER')
    elif change == 'unreviewed_parameter': review.families['EV-CAL'].parameter_hashes.clear()
    repin(api, world, binding, review)
    if change == 'unreviewed_parameter':
        review.families['EV-CAL'].parameter_hashes.clear()
        binding.review_sha256 = api.record_hash(review)
    assert api.admit_scientific_use(world, binding, review)['status'] == expected


@pytest.mark.parametrize('change', ['missing', 'wrong_source_scale', 'wrong_source_scope', 'wrong_target', 'fact', 'blank_method', 'blank_rationale'])
def test_transfer_requires_actual_four_axes_and_explicit_nonfact_status(change):
    api, world, binding, review = synthetic_case()
    binding.as_scale, binding.for_scope = Scale.MASSIF, Scope.SKRU1
    for param in world.materials:
        param.quantity.provenance.transfer = Transfer(from_scale=Scale.LAB, to_scale=Scale.MASSIF,
            from_scope=Scope.NON_VKM, to_scope=Scope.SKRU1, status=S.ENGINEERING_ASSUMPTION,
            method='synthetic explicit transfer', rationale='synthetic contract test')
    transfer = world.materials[0].quantity.provenance.transfer
    if change == 'missing': world.materials[0].quantity.provenance.transfer = None
    elif change == 'wrong_source_scale': transfer.from_scale = Scale.FIELD
    elif change == 'wrong_source_scope': transfer.from_scope = Scope.SKRU2
    elif change == 'wrong_target': transfer.to_scope = Scope.SKRU2
    elif change == 'fact': transfer.status = S.FACT
    elif change == 'blank_method': transfer.method = ' '
    elif change == 'blank_rationale': transfer.rationale = ' '
    repin(api, world, binding, review)
    assert api.admit_scientific_use(world, binding, review)['status'] == 'BLOCKED'


def test_complete_transfer_preserves_source_status_and_effective_status():
    api, world, binding, review = synthetic_case()
    binding.as_scale, binding.for_scope = Scale.MASSIF, Scope.SKRU1
    for param in world.materials:
        param.quantity.provenance.transfer = Transfer(from_scale=Scale.LAB, to_scale=Scale.MASSIF,
            from_scope=Scope.NON_VKM, to_scope=Scope.SKRU1, status=S.ENGINEERING_ASSUMPTION,
            method='synthetic explicit transfer', rationale='synthetic contract test')
    for quantity in binding.conditions.values():
        quantity.provenance.transfer = Transfer(from_scale=Scale.LAB, to_scale=Scale.MASSIF,
            from_scope=Scope.NON_VKM, to_scope=Scope.SKRU1, status=S.ENGINEERING_ASSUMPTION,
            method='synthetic explicit condition transfer', rationale='same requested use context')
    repin(api, world, binding, review)
    receipt = api.admit_scientific_use(world, binding, review)
    assert receipt['status'] == 'READY'
    assert set(receipt['effective_statuses'].values()) == {'ENGINEERING_ASSUMPTION'}
    assert all(m.quantity.provenance.status is S.FACT for m in world.materials)


@pytest.mark.parametrize('reviewed,axis,unknown', [(r, a, u) for r in (False, True) for a in ('scope', 'scale') for u in (False, True)])
def test_condition_sources_require_matching_context_or_explicit_transfer(reviewed, axis, unknown):
    api, world, binding, review = synthetic_case()
    quantity = review.families['EV-CAL'].applicability['stress'] if reviewed else binding.conditions['stress']
    if axis == 'scope': quantity.provenance.scope = Scope.UNSTATED if unknown else Scope.SKRU2
    else: quantity.provenance.scale = Scale.UNSTATED if unknown else Scale.FIELD
    repin(api, world, binding, review)
    result = api.admit_scientific_use(world, binding, review)
    assert result['status'] != 'READY'
    reference = 'review.family.applicability.stress' if reviewed else 'binding.conditions.stress'
    assert any(item['field_ref'] == reference for item in result['diagnostics'])


@pytest.mark.parametrize('reviewed,transferred', [(r, t) for r in (False, True) for t in (False, True)])
def test_condition_source_context_and_complete_transfer_positive(reviewed, transferred):
    api, world, binding, review = synthetic_case()
    quantity = review.families['EV-CAL'].applicability['stress'] if reviewed else binding.conditions['stress']
    if transferred:
        quantity.provenance.scope, quantity.provenance.scale = Scope.SKRU2, Scale.FIELD
        quantity.provenance.transfer = Transfer(from_scope=Scope.SKRU2, to_scope=Scope.NON_VKM,
            from_scale=Scale.FIELD, to_scale=Scale.LAB, status=S.ENGINEERING_ASSUMPTION,
            method='synthetic explicit condition transfer', rationale='synthetic reviewed application context')
    repin(api, world, binding, review)
    assert api.admit_scientific_use(world, binding, review)['status'] == 'READY'
    assert quantity.provenance.status is S.FACT


@pytest.mark.parametrize('reviewed,spatial', [(r, s) for r in (False, True) for s in (False, True)])
def test_external_condition_declared_world_references_must_resolve(reviewed, spatial):
    api, world, binding, review = synthetic_case()
    quantity = review.families['EV-CAL'].applicability['stress'] if reviewed else binding.conditions['stress']
    if spatial: quantity.provenance.spatial.entity_id = 'MISSING-SYNTHETIC'
    else:
        quantity.provenance.status, quantity.provenance.method = S.DERIVATION, 'synthetic deterministic derivation'
        quantity.provenance.inputs = ('MISSING-SYNTHETIC',)
    repin(api, world, binding, review)
    assert api.admit_scientific_use(world, binding, review)['status'] == 'BLOCKED'


@pytest.mark.parametrize('reviewed,input_id', [(r, i) for r in (False, True) for i in ('DATA', 'MM-SYNTHETIC')])
def test_external_condition_existing_available_world_input_positive(reviewed, input_id):
    api, world, binding, review = synthetic_case()
    quantity = review.families['EV-CAL'].applicability['stress'] if reviewed else binding.conditions['stress']
    quantity.provenance.status, quantity.provenance.method = S.DERIVATION, 'synthetic deterministic derivation'
    quantity.provenance.inputs = (input_id,)
    repin(api, world, binding, review)
    assert api.admit_scientific_use(world, binding, review)['status'] == 'READY'


@pytest.mark.parametrize('change', ['future_record', 'future_dataset_source', 'future_dataset_time', 'unknown_predecessor', 'unknown_blocker'])
def test_selected_records_and_consumed_condition_dependencies_require_availability(change):
    api, world, binding, review = synthetic_case()
    dataset = world.observation_datasets[0]
    if change == 'future_record':
        world.materials[0].provenance.temporal = world.materials[0].provenance.temporal.model_copy(deep=True)
        world.materials[0].provenance.temporal.available_from = date(2022, 1, 1)
    else:
        binding.conditions['stress'].provenance.status = S.DERIVATION
        binding.conditions['stress'].provenance.method = 'synthetic deterministic derivation'
        binding.conditions['stress'].provenance.inputs = ('DATA',)
        if change == 'future_dataset_source':
            dataset.provenance.temporal = dataset.provenance.temporal.model_copy(deep=True)
            dataset.provenance.temporal.available_from = date(2022, 1, 1)
        elif change == 'future_dataset_time':
            dataset.time = dataset.time.model_copy(deep=True)
            dataset.time.available_from = date(2022, 1, 1)
        elif change == 'unknown_predecessor': dataset.provenance.status = S.UNKNOWN
        else:
            world.unknowns.append(UnknownItem(id='U-DEPENDENCY', provenance=Provenance(status=S.UNKNOWN),
                what='synthetic unresolved consumed input', why_it_matters='synthetic derivation', blocks=('DATA',)))
    repin(api, world, binding, review)
    assert api.admit_scientific_use(world, binding, review)['status'] == (
        'UNDETERMINED' if change in ('unknown_predecessor', 'unknown_blocker') else 'BLOCKED')


def test_source_side_use_does_not_consume_a_stored_future_transfer():
    api, world, binding, review = synthetic_case()
    for param in world.materials:
        param.quantity.provenance.transfer = Transfer(from_scale=Scale.LAB, to_scale=Scale.MASSIF,
            from_scope=Scope.NON_VKM, to_scope=Scope.SKRU1, status=S.ENGINEERING_ASSUMPTION,
            method='synthetic future transfer', rationale='only for the explicitly requested target')
    repin(api, world, binding, review)
    assert all(param.use_errors(Scale.LAB, Scope.NON_VKM) == [] for param in world.materials)
    result = api.admit_scientific_use(world, binding, review)
    assert result['status'] == 'READY'
    assert set(result['effective_statuses'].values()) == {'FACT'}


def test_old_metadata_use_path_is_insufficient_for_scientific_admission():
    api, world, binding, review = synthetic_case()
    binding.as_scale, binding.for_scope = Scale.MASSIF, Scope.SKRU1
    for param in world.materials:
        param.quantity.provenance.transfer = Transfer(from_scale=Scale.FIELD, to_scale=Scale.MASSIF,
            from_scope=Scope.SKRU2, to_scope=Scope.SKRU1, status=S.ENGINEERING_ASSUMPTION,
            method='synthetic transfer', rationale='synthetic fixture')
        assert param.use_errors()  # source-side mismatch is now refused by the existing use guard too
    repin(api, world, binding, review)
    assert api.admit_scientific_use(world, binding, review)['status'] == 'BLOCKED'
    for param in world.materials:
        param.quantity.provenance.transfer = None
    world.materials[0].quantity.unit = 'unresolved arbitrary archival unit'
    binding.as_scale, binding.for_scope = Scale.LAB, Scope.NON_VKM
    assert world.materials[0].use_errors(Scale.LAB, Scope.NON_VKM) == []
    repin(api, world, binding, review)
    assert world.validate_world(registered_laws=review.registered_laws) == []
    assert api.admit_scientific_use(world, binding, review)['status'] == 'UNDETERMINED'


def test_exponent_nonunity_unit_factor_cannot_be_discarded(monkeypatch):
    import vkm_world.core.units as units
    api, world, binding, review = synthetic_case()
    monkeypatch.setitem(units._UNITS, 'milliunit', UnitInfo('1', 0.001, Dimension.DIMENSIONLESS))
    world.materials[1].quantity.unit = 'milliunit'
    repin(api, world, binding, review)
    assert api.admit_scientific_use(world, binding, review)['status'] != 'READY'


def test_matching_unstated_scope_cannot_grant_admission():
    api, world, binding, review = synthetic_case()
    for param in world.materials: param.quantity.provenance.scope = Scope.UNSTATED
    review.families['EV-CAL'].scope = Scope.UNSTATED
    binding.for_scope = Scope.UNSTATED
    repin(api, world, binding, review)
    assert api.admit_scientific_use(world, binding, review)['status'] != 'READY'


def test_discrete_reviewed_conditions_do_not_create_a_continuous_applicability_range():
    api, world, binding, review = synthetic_case()
    limit = review.families['EV-CAL'].applicability['stress']
    limit.low, limit.high = None, None
    limit.uncertainty.kind = 'DISCRETE_SET'
    limit.uncertainty.values = (1, 100)
    repin(api, world, binding, review)
    assert api.admit_scientific_use(world, binding, review)['status'] != 'READY'


@pytest.mark.parametrize('selected', [False, True])
def test_explicit_unknown_blockers_apply_only_to_selected_inputs(selected):
    api, world, binding, review = synthetic_case()
    blocker = UnknownItem(id='U-SYNTH', provenance=Provenance(status=S.UNKNOWN),
                          what='synthetic unresolved context', why_it_matters='synthetic contract',
                          blocks=('MP-A',) if selected else ('DATA',))
    world.unknowns.append(blocker)
    repin(api, world, binding, review)
    result = api.admit_scientific_use(world, binding, review)
    assert result['status'] == ('UNDETERMINED' if selected else 'READY')
    if selected:
        diagnostic = next(item for item in result['diagnostics'] if item['code'] == 'SELECTED_INPUT_BLOCKED_BY_UNKNOWN')
        assert diagnostic['severity'] == 'UNDETERMINED'
        assert diagnostic['field_ref'] == 'parameter.A.blockers'
        assert diagnostic['input_sha256'] == api.record_hash(blocker)


@pytest.mark.parametrize('conflicting', [False, True])
def test_formula_conflicts_fail_closed_but_known_variants_are_not_conflicts(conflicting):
    api, world, binding, review = synthetic_case()
    model = world.math_models[0]
    if conflicting: model.conflicting_forms = 'synthetic unresolved conflicting form'
    else: model.known_variants = 'synthetic documented form variant'
    repin(api, world, binding, review)
    result = api.admit_scientific_use(world, binding, review)
    assert result['status'] == ('UNDETERMINED' if conflicting else 'READY')
    if conflicting:
        diagnostic = next(item for item in result['diagnostics'] if item['code'] == 'FORMULA_CONFLICT_UNRESOLVED')
        assert diagnostic['severity'] == 'UNDETERMINED'
        assert diagnostic['field_ref'] == 'formula.conflicting_forms'
        assert diagnostic['input_sha256'] == api.record_hash(model)


def test_admission_diagnostics_identify_logical_field_severity_and_input():
    api, world, binding, review = synthetic_case()
    world.materials[0].quantity.unit = 'm'
    repin(api, world, binding, review)
    result = api.admit_scientific_use(world, binding, review)
    diagnostic = next(item for item in result['diagnostics'] if item['code'] == 'DIMENSION_MISMATCH')
    assert diagnostic['severity'] == 'BLOCKED'
    assert diagnostic['field_ref'] == 'parameter.A.quantity.unit'
    assert diagnostic['input_sha256'] == api.record_hash(world.materials[0])


@pytest.mark.parametrize('kind', ['world', 'binding', 'review'])
def test_stale_input_or_receipt_cannot_grant_admission(kind):
    api, world, binding, review = synthetic_case()
    receipt = api.admit_scientific_use(world, binding, review)
    if kind == 'world': world.materials[0].quantity.value = 0.02
    elif kind == 'binding': binding.conditions['stress'].value = 12.0
    else: review.families['EV-CAL'].conditions = 'changed review'
    assert not api.receipt_matches_current(receipt, world, binding, review)
    if kind != 'binding': assert api.admit_scientific_use(world, binding, review)['status'] == 'BLOCKED'


@pytest.mark.parametrize('value', [float('nan'), float('inf'), float('-inf'), 'synthetic-secret-sentinel'])
def test_mutated_invalid_values_fail_without_serializer_value_warnings(value):
    api, world, binding, review = synthetic_case()
    world.materials[0].quantity.value = value
    with warnings.catch_warnings(record=True) as caught:
        result = api.admit_scientific_use(world, binding, review)
    assert result['status'] == 'BLOCKED' and result['reasons'] == ['INVALID_INPUT']
    assert not caught


def draft_case():
    api, world, binding, review = synthetic_case()
    draft = importlib.import_module('vkm_world.validation.draft')
    # Explicit synthetic source applicability and use context cover the declared 28/31 day horizons.
    review.families['EV-CAL'].applicability['duration'].high = 10_000_000
    binding.conditions['duration'].value = None
    binding.conditions['duration'].low, binding.conditions['duration'].high = 28*86400, 31*86400
    repin(api, world, binding, review)
    samples = [{'sample_id': f'S-{i}', 'origin': f'2021-0{i}-01', 'target': f'2021-0{i+1}-01',
                'target_campaign_id': f'C-{i+1}', 'horizon_days': (date(2021, i+1, 1)-date(2021, i, 1)).days,
                'target_available_from': f'2021-0{i+1}-01', 'group': f'L-{i%2}'} for i in range(1, 4)]
    protocol = draft.DraftProtocol(experiment_id='SYNTHETIC-DRAFT', observation_dataset_id='DATA',
        scientific_question='Are these synthetic contracts internally consistent?', experiment_version='draft/1',
        t0=binding.origin, sample_origin_policy='AT_OR_AFTER_T0', horizon_rule='PLANNED_EPOCH', horizon_steps=1, input_availability_rule='KNOWN_AT_ORIGIN',
        binding_sha256=api.record_hash(binding), code_hashes=draft.implementation_hashes(),
        data_hashes={'DATA': api.record_hash(world.observation_datasets[0])}, independence_rule='DISJOINT_DATA_AND_LINEAGE',
        data_roles={name: {'state': 'USED' if name == 'validation' else 'NOT_APPLICABLE',
            'dataset_ids': ('DATA',) if name == 'validation' else (),
            'sample_ids': tuple(s['sample_id'] for s in samples) if name == 'validation' else (),
            'rationale': 'metadata contract checking only; no fitting, NROY or test opening'}
            for name in ('calibration', 'nroy', 'filtering', 'validation', 'test')},
        dataset_sha256=api.record_hash(world.observation_datasets[0]), data_kind='SYNTHETIC',
        feature_fields=('stress', 'forecast_horizon_days'), metrics=('mae', 'rmse'), acceptance_limits={'mae': 1, 'rmse': 1},
        splitter='rolling_origin', minimum_train_dates=1, samples=samples,
        schedule=[(f'C-{i}', date(2021, i, 1)) for i in range(2, 5)])
    return api, draft, world, binding, review, protocol


def test_draft_consumer_positive_and_stale_negative():
    api, draft, world, binding, review, protocol = draft_case()
    admission = api.admit_scientific_use(world, binding, review)
    result = draft.check_draft_protocol(world, binding, review, protocol, admission)
    assert result['status'] == 'READY_DRAFT' and result['protocol_status'] == 'DRAFT'
    assert result['fold_count'] and result['protocol_sha256']
    world.materials[0].quantity.value = 0.02
    assert draft.check_draft_protocol(world, binding, review, protocol, admission)['status'] == 'BLOCKED'


def test_draft_consumes_owned_inputs_without_rereading_original_after_admission(monkeypatch):
    api, draft, world, binding, review, protocol = draft_case()
    admission = api.admit_scientific_use(world, binding, review)
    original_revalidate = WorldSpec.revalidated
    def mutate_original_before_clone(self):
        if self is world: self.materials[0].quantity.value = 0.02
        return original_revalidate(self)
    monkeypatch.setattr(WorldSpec, 'revalidated', mutate_original_before_clone)
    result = draft.check_draft_protocol(world, binding, review, protocol, admission)
    assert result['status'] == 'READY_DRAFT'
    assert world.materials[0].quantity.value == 0.01
    assert api.admit_scientific_use(world, binding, review)['status'] == 'READY'


@pytest.mark.parametrize('module', ['splits', 'math_models', 'observations'])
def test_draft_pins_reused_guard_implementations(monkeypatch, tmp_path, module):
    _, draft, world, binding, review, protocol = draft_case()
    assert {'scientific', 'draft', 'leakage', 'splits', 'metrics', 'provenance', 'units',
            'materials', 'math_models', 'observations', 'worldspec_model', 'worldspec_io', 'core_base', 'core_io'} <= protocol.code_hashes.keys()
    changed_source = tmp_path / 'changed_public_guard.py'
    changed_source.write_text('# synthetic changed public split guard\n', encoding='utf-8')
    monkeypatch.setattr(getattr(draft, module), '__file__', str(changed_source))
    assert draft.check_draft_protocol(world, binding, review, protocol)['status'] == 'BLOCKED'


@pytest.mark.parametrize('field', ['scientific_question', 'experiment_version', 't0', 'horizon_rule', 'horizon_steps',
                                  'input_availability_rule', 'binding_sha256', 'data_roles', 'independence_rule',
                                  'code_hashes', 'data_hashes', 'sample_origin_policy'])
def test_draft_omitted_mandatory_declaration_is_not_ready(field):
    api, draft, world, binding, review, protocol = draft_case()
    payload = protocol.model_dump(mode='python')
    payload.pop(field, None)
    incomplete = draft.DraftProtocol.model_construct(**payload)
    result = draft.check_draft_protocol(world, binding, review, incomplete)
    assert result['status'] == 'NOT_READY'
    assert result['protocol_status'] == 'DRAFT'
    assert f'DRAFT_MISSING_{field.upper()}' in result['reasons']


def test_incomplete_draft_can_be_stored_without_inventing_declarations():
    api, draft, world, binding, review, _ = draft_case()
    protocol = draft.DraftProtocol(experiment_id='SYNTH-DRAFT')
    result = draft.check_draft_protocol(world, binding, review, protocol)
    assert result['status'] == 'NOT_READY' and result['protocol_status'] == 'DRAFT'
    assert 'DRAFT_MISSING_SCIENTIFIC_QUESTION' in result['reasons']
    assert protocol.model_dump()['t0'] is None


def test_cli_incomplete_draft_is_safe_not_ready(tmp_path):
    _, _, world, _, _, _ = draft_case()
    world_path, protocol_path = tmp_path / 'world.json', tmp_path / 'protocol.json'
    world_path.write_text(world.model_dump_json(), encoding='utf-8')
    protocol_path.write_text(json.dumps({'experiment_id': 'SYNTH-DRAFT'}), encoding='utf-8')
    run = subprocess.run([sys.executable, str(ROOT / 'scripts/check_scientific_use.py'),
                          '--world', str(world_path), '--protocol', str(protocol_path)],
                         capture_output=True, text=True)
    output = json.loads(run.stdout)
    assert run.returncode == 2 and output['status'] == 'NOT_READY'
    assert output['protocol_status'] == 'DRAFT'
    assert 'DRAFT_MISSING_SCIENTIFIC_QUESTION' in output['reasons']
    assert run.stderr == '' and str(tmp_path) not in run.stdout


def test_draft_forecast_horizon_cannot_exceed_admitted_duration():
    api, draft, world, binding, review, protocol = draft_case()
    binding.conditions['duration'].value, binding.conditions['duration'].low, binding.conditions['duration'].high = 100, None, None
    review.families['EV-CAL'].applicability['duration'].low = 1
    review.families['EV-CAL'].applicability['duration'].high = 1000
    repin(api, world, binding, review)
    protocol.binding_sha256 = api.record_hash(binding)
    assert api.admit_scientific_use(world, binding, review)['status'] == 'READY'
    assert draft.check_draft_protocol(world, binding, review, protocol)['status'] == 'BLOCKED'


@pytest.mark.parametrize('source_support', [False, True])
def test_draft_dataset_availability_checked_at_actual_sample_origin(source_support):
    api, draft, world, binding, review, protocol = draft_case()
    dataset = world.observation_datasets[0]
    support = dataset.provenance.temporal if source_support else dataset.time
    support.available_from = date(2021, 2, 1)
    repin(api, world, binding, review)
    protocol.binding_sha256 = api.record_hash(binding)
    protocol.dataset_sha256 = api.record_hash(dataset)
    protocol.data_hashes[dataset.id] = api.record_hash(dataset)
    assert draft.check_draft_protocol(world, binding, review, protocol)['status'] == 'BLOCKED'


@pytest.mark.parametrize('lineage_only', [False, True])
def test_draft_data_role_overlap_and_shared_lineage_are_rejected(lineage_only):
    api, draft, world, binding, review, protocol = draft_case()
    def role(state, ids=(), samples=()):
        values = {'state': state, 'dataset_ids': ids, 'sample_ids': samples, 'rationale': 'synthetic role declaration'}
        return draft.DraftDataRole(**values) if hasattr(draft, 'DraftDataRole') else values
    roles = {name: role('NOT_APPLICABLE') for name in ('calibration', 'nroy', 'filtering', 'validation', 'test')}
    roles['validation'] = role('USED', ('DATA',), tuple(s.sample_id for s in protocol.samples))
    calibration_id = 'DATA'
    if lineage_only:
        calibration = world.observation_datasets[0].model_copy(deep=True)
        calibration.id = 'DATA-CAL'
        calibration.provenance.inputs = ('DATA',)
        calibration.provenance.sources = (SourceRef(source_id='EXT-SRC-001', locator='distinct synthetic extraction', evidence_ids=('EV-OTHER',)),)
        review.registered_evidence.add('EV-OTHER')
        world.observation_datasets.append(calibration)
        calibration_id = calibration.id
        data_hashes = {d.id: api.record_hash(d) for d in world.observation_datasets}
        protocol = protocol.model_copy(update={'data_hashes': data_hashes})
    roles['calibration'] = role('USED', (calibration_id,), ('CAL-ONLY',))
    protocol = protocol.model_copy(update={'data_roles': roles})
    repin(api, world, binding, review)
    if hasattr(protocol, 'binding_sha256'): protocol.binding_sha256 = api.record_hash(binding)
    assert draft.check_draft_protocol(world, binding, review, protocol)['status'] == 'BLOCKED'


@pytest.mark.parametrize('change', ['unassigned_dataset', 'unassigned_samples'])
def test_draft_actual_validation_inputs_must_match_declared_role(change):
    _, draft, world, binding, review, protocol = draft_case()
    role = protocol.data_roles['validation']
    if change == 'unassigned_dataset':
        role.state, role.dataset_ids, role.sample_ids = 'NOT_APPLICABLE', (), ()
    else:
        role.sample_ids = ('UNUSED-SYNTHETIC-SAMPLE',)
    assert draft.check_draft_protocol(world, binding, review, protocol)['status'] == 'BLOCKED'


@pytest.mark.parametrize('selected', [False, True])
def test_draft_unknown_blocker_applies_to_selected_dataset_only(selected):
    api, draft, world, binding, review, protocol = draft_case()
    unrelated = world.observation_datasets[0].model_copy(deep=True)
    unrelated.id = 'DATA-UNUSED'
    world.observation_datasets.append(unrelated)
    world.unknowns.append(UnknownItem(id='U-DATA', provenance=Provenance(status=S.UNKNOWN),
        what='synthetic unresolved dataset', why_it_matters='synthetic validation contract',
        blocks=('DATA',) if selected else ('DATA-UNUSED',)))
    repin(api, world, binding, review)
    protocol.binding_sha256 = api.record_hash(binding)
    assert api.admit_scientific_use(world, binding, review)['status'] == 'READY'
    result = draft.check_draft_protocol(world, binding, review, protocol)
    assert result['status'] == ('NOT_READY' if selected else 'READY_DRAFT')
    if selected: assert result['reasons'] == ['SELECTED_DATASET_BLOCKED_BY_UNKNOWN']


@pytest.mark.parametrize('dependency', ['used_dataset', 'upstream_dataset', 'system'])
def test_draft_unknown_blocker_covers_used_dataset_and_declared_dependencies(dependency):
    api, draft, world, binding, review, protocol = draft_case()
    blocked_id = 'SYS'
    if dependency != 'system':
        extra = world.observation_datasets[0].model_copy(deep=True)
        extra.id = 'DATA-DEPENDENCY'
        extra.provenance.sources = (SourceRef(source_id='EXT-SRC-001', locator='independent synthetic location',
                                             evidence_ids=('EV-OTHER',)),)
        review.registered_evidence.add('EV-OTHER')
        world.observation_datasets.append(extra)
        blocked_id = extra.id
        if dependency == 'used_dataset':
            protocol.data_roles['calibration'] = draft.DraftDataRole(state='USED', dataset_ids=(extra.id,),
                sample_ids=('CAL-ONLY',), rationale='independent synthetic calibration metadata')
            protocol.data_hashes[extra.id] = api.record_hash(extra)
        else:
            world.observation_datasets[0].provenance.inputs = (extra.id,)
    world.unknowns.append(UnknownItem(id='U-DEPENDENCY', provenance=Provenance(status=S.UNKNOWN),
        what='synthetic unresolved dependency', why_it_matters='synthetic dataset contract', blocks=(blocked_id,)))
    repin(api, world, binding, review)
    protocol.binding_sha256 = api.record_hash(binding)
    protocol.dataset_sha256 = api.record_hash(world.observation_datasets[0])
    protocol.data_hashes['DATA'] = protocol.dataset_sha256
    assert api.admit_scientific_use(world, binding, review)['status'] == 'READY'
    result = draft.check_draft_protocol(world, binding, review, protocol)
    assert result['status'] == 'NOT_READY'
    assert result['reasons'] == ['SELECTED_DATASET_BLOCKED_BY_UNKNOWN']


@pytest.mark.parametrize('dependency', ['used_dataset', 'upstream_dataset', 'system', 'unused_dataset'])
def test_draft_consumed_metadata_dependencies_must_be_available(dependency):
    api, draft, world, binding, review, protocol = draft_case()
    if dependency == 'system':
        world.observation_systems[0].provenance.temporal = world.observation_systems[0].provenance.temporal.model_copy(deep=True)
        world.observation_systems[0].provenance.temporal.available_from = date(2022, 1, 1)
    else:
        extra = world.observation_datasets[0].model_copy(deep=True)
        extra.id = 'DATA-FUTURE'
        extra.provenance.sources = (SourceRef(source_id='EXT-SRC-001', locator='independent synthetic future location',
                                             evidence_ids=('EV-OTHER',)),)
        extra.time, extra.provenance.temporal = extra.time.model_copy(deep=True), extra.provenance.temporal.model_copy(deep=True)
        extra.time.available_from = extra.provenance.temporal.available_from = date(2022, 1, 1)
        world.observation_datasets.append(extra)
        review.registered_evidence.add('EV-OTHER')
        if dependency == 'used_dataset':
            protocol.data_roles['calibration'] = draft.DraftDataRole(state='USED', dataset_ids=(extra.id,),
                sample_ids=('CAL-ONLY',), rationale='independent synthetic calibration metadata')
            protocol.data_hashes[extra.id] = api.record_hash(extra)
        elif dependency == 'upstream_dataset': world.observation_datasets[0].provenance.inputs = (extra.id,)
    repin(api, world, binding, review)
    protocol.binding_sha256 = api.record_hash(binding)
    protocol.dataset_sha256 = api.record_hash(world.observation_datasets[0])
    protocol.data_hashes['DATA'] = protocol.dataset_sha256
    assert api.admit_scientific_use(world, binding, review)['status'] == 'READY'
    result = draft.check_draft_protocol(world, binding, review, protocol)
    assert result['status'] == ('READY_DRAFT' if dependency == 'unused_dataset' else 'BLOCKED')
    if dependency != 'unused_dataset': assert result['reasons'] == ['DATA_DEPENDENCY_UNAVAILABLE']


@pytest.mark.parametrize('role,future', [(r, f) for r in ('calibration', 'nroy', 'filtering') for f in (False, True)])
def test_draft_preforecast_role_metadata_is_known_at_t0(role, future):
    api, draft, world, binding, review, protocol = draft_case()
    # Forecasts start after t0; role metadata must already be known at the experiment declaration.
    for index, sample in enumerate(protocol.samples, start=2):
        sample.origin, sample.target = date(2021, index, 1), date(2021, index+1, 1)
        sample.horizon_days = (sample.target-sample.origin).days
        sample.target_available_from, sample.target_campaign_id = sample.target, f'C-{index+1}'
    protocol.schedule = [(f'C-{index}', date(2021, index, 1)) for index in range(3, 6)]
    calibration = world.observation_datasets[0].model_copy(deep=True)
    calibration.id = 'DATA-PREFORECAST'
    calibration.provenance.sources = (SourceRef(source_id='EXT-SRC-001', locator='independent synthetic preforecast metadata',
                                               evidence_ids=('EV-OTHER',)),)
    calibration.time.available_from = calibration.provenance.temporal.available_from = date(2021, 1, 15) if future else date(2020, 1, 1)
    review.registered_evidence.add('EV-OTHER')
    world.observation_datasets.append(calibration)
    protocol.data_roles[role] = draft.DraftDataRole(state='USED', dataset_ids=(calibration.id,), sample_ids=('PRE-ONLY',),
                                                  rationale='independent synthetic preforecast metadata')
    protocol.data_hashes[calibration.id] = api.record_hash(calibration)
    repin(api, world, binding, review)
    protocol.binding_sha256 = api.record_hash(binding)
    result = draft.check_draft_protocol(world, binding, review, protocol)
    assert result['status'] == ('BLOCKED' if future else 'READY_DRAFT')
    if future: assert result['reasons'] == ['DATA_DEPENDENCY_UNAVAILABLE']


def test_draft_validation_metadata_uses_actual_origin_cut_not_t0():
    api, draft, world, binding, review, protocol = draft_case()
    for index, sample in enumerate(protocol.samples, start=2):
        sample.origin, sample.target = date(2021, index, 1), date(2021, index+1, 1)
        sample.horizon_days = (sample.target-sample.origin).days
        sample.target_available_from, sample.target_campaign_id = sample.target, f'C-{index+1}'
    protocol.schedule = [(f'C-{index}', date(2021, index, 1)) for index in range(3, 6)]
    dataset = world.observation_datasets[0]
    dataset.time, dataset.provenance.temporal = dataset.time.model_copy(deep=True), dataset.provenance.temporal.model_copy(deep=True)
    dataset.time.available_from = dataset.provenance.temporal.available_from = date(2021, 1, 15)
    repin(api, world, binding, review)
    protocol.binding_sha256 = api.record_hash(binding)
    protocol.dataset_sha256 = api.record_hash(dataset)
    protocol.data_hashes[dataset.id] = protocol.dataset_sha256
    assert draft.check_draft_protocol(world, binding, review, protocol)['status'] == 'READY_DRAFT'


@pytest.mark.parametrize('same_location', [False, True])
def test_draft_source_location_identity_survives_mixed_evidence_representation(same_location):
    api, draft, world, binding, review, protocol = draft_case()
    calibration = world.observation_datasets[0].model_copy(deep=True)
    calibration.id = 'DATA-CAL'
    source = calibration.provenance.sources[0]
    calibration.provenance.sources = (source.model_copy(update={
        'evidence_ids': (), 'locator': source.locator if same_location else 'independent synthetic calibration location'}),)
    world.observation_datasets.append(calibration)
    protocol.data_roles['calibration'] = draft.DraftDataRole(state='USED', dataset_ids=(calibration.id,),
        sample_ids=('CAL-SYN',), rationale='synthetic calibration role')
    protocol.data_hashes[calibration.id] = api.record_hash(calibration)
    repin(api, world, binding, review)
    protocol.binding_sha256 = api.record_hash(binding)
    result = draft.check_draft_protocol(world, binding, review, protocol)
    assert result['status'] == ('BLOCKED' if same_location else 'READY_DRAFT')
    if same_location: assert result['reasons'] == ['DATA_LINEAGE_OVERLAP']


@pytest.mark.parametrize('field,value', [('scale', 'FIELD'), ('site_applicability', 'SKRU1'),
                                       ('scale', 'synthetic unresolved scale'),
                                       ('site_applicability', 'synthetic unsupported site'),
                                       ('scale', 'GENERAL')])
def test_formula_applicability_requires_exact_reviewed_source_context(field, value):
    api, world, binding, review = synthetic_case()
    setattr(world.math_models[0], field, value)
    repin(api, world, binding, review)
    assert api.admit_scientific_use(world, binding, review)['status'] == 'UNDETERMINED'


@pytest.mark.parametrize('general', [False, True])
def test_formula_exact_source_applicability_or_reviewed_general_specialization(general):
    api, world, binding, review = synthetic_case()
    model = world.math_models[0]
    model.scale, model.site_applicability = ('GENERAL', 'GENERAL') if general else ('LAB', 'NON_VKM')
    if general: review.families['EV-CAL'].formula_specialization = 'synthetic reviewed GENERAL form specialization'
    repin(api, world, binding, review)
    assert api.admit_scientific_use(world, binding, review)['status'] == 'READY'


@pytest.mark.parametrize('conflict', [False, True])
def test_general_specialization_cannot_override_explicit_context_or_formula_conflict(conflict):
    api, world, binding, review = synthetic_case()
    review.families['EV-CAL'].formula_specialization = 'synthetic reviewed GENERAL specialization'
    if conflict: world.math_models[0].conflicting_forms = 'synthetic unresolved conflicting form'
    else: world.math_models[0].scale = 'FIELD'
    repin(api, world, binding, review)
    assert api.admit_scientific_use(world, binding, review)['status'] == 'UNDETERMINED'


def test_draft_spatial_grouping_with_future_training_labels_is_not_forecast_ready():
    api, draft, world, binding, review, protocol = draft_case()
    protocol.splitter = 'leave_one_line_out'
    result = draft.check_draft_protocol(world, binding, review, protocol)
    assert result['status'] == 'BLOCKED'
    assert result['reasons'] == ['TRAINING_LABEL_TEMPORAL_LEAKAGE']


@pytest.mark.parametrize('site_use', [False, True])
def test_unknown_formula_skru1_applicability_is_context_specific(site_use):
    api, world, binding, review = synthetic_case()
    world.math_models[0].status = 'UNKNOWN'
    if site_use:
        binding.as_scale, binding.for_scope = Scale.MASSIF, Scope.SKRU1
        for quantity in [*(p.quantity for p in world.materials), *binding.conditions.values()]:
            quantity.provenance.transfer = Transfer(from_scope=Scope.NON_VKM, to_scope=Scope.SKRU1,
                from_scale=Scale.LAB, to_scale=Scale.MASSIF, status=S.ENGINEERING_ASSUMPTION,
                method='synthetic explicit site transfer', rationale='synthetic requested SKRU1 context')
    repin(api, world, binding, review)
    assert api.admit_scientific_use(world, binding, review)['status'] == ('UNDETERMINED' if site_use else 'READY')


@pytest.mark.parametrize('change', ['unsafe_features', 'random_split', 'missing_metric', 'future_dataset', 'changed_dataset',
                                   'next_successful_target', 'test_role', 'not_draft', 'real_data'])
def test_draft_fail_closed(change):
    api, draft, world, binding, review, protocol = draft_case()
    if change == 'unsafe_features': protocol.feature_fields = ('true_hidden_value',)
    elif change == 'random_split': protocol.splitter = 'random'
    elif change == 'missing_metric': protocol.metrics = ('invented_metric',)
    elif change == 'future_dataset': world.observation_datasets[0].time.available_from = date(2022, 1, 1)
    elif change == 'changed_dataset': world.observation_datasets[0].values_location = 'unread synthetic location'
    elif change == 'next_successful_target': protocol.samples[0].target_campaign_id = 'C-3'
    elif change == 'test_role': protocol.samples[0].role = 'test'
    elif change == 'not_draft': protocol.status = 'PREREGISTERED'
    elif change == 'real_data': protocol.data_kind = 'REAL'
    repin(api, world, binding, review)
    admission = api.admit_scientific_use(world, binding, review)
    assert draft.check_draft_protocol(world, binding, review, protocol, admission)['status'] == 'BLOCKED'


def test_cli_e2e_only_outputs_safe_aggregate(tmp_path):
    api, draft, world, binding, review, protocol = draft_case()
    for name, record in [('world', world), ('binding', binding), ('review', review), ('protocol', protocol)]:
        (tmp_path/f'{name}.json').write_text(record.model_dump_json())
    command = [sys.executable, str(ROOT/'scripts/check_scientific_use.py'), '--world', str(tmp_path/'world.json'),
               '--binding', str(tmp_path/'binding.json'), '--review', str(tmp_path/'review.json'), '--protocol', str(tmp_path/'protocol.json')]
    result = subprocess.run(command, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    output = json.loads(result.stdout)
    assert output['status'] == 'READY_DRAFT'
    assert 'synthetic salt' not in result.stdout and str(tmp_path) not in result.stdout and '0.01' not in result.stdout
    binding.conditions['stress'].value = 101
    (tmp_path/'binding.json').write_text(binding.model_dump_json())
    result = subprocess.run(command, capture_output=True, text=True)
    assert result.returncode != 0 and json.loads(result.stdout)['status'] == 'BLOCKED'


def test_existing_public_catalogue_formula_does_not_gain_admission_without_binding_review():
    import csv
    api = importlib.import_module('vkm_world.validation.scientific')
    with (ROOT/'catalogues/mathematics/MATHEMATICAL_MODEL_REGISTRY.csv').open() as source:
        record = next(row for row in csv.DictReader(source) if row['model_id'].startswith('MM-CREEP-'))
    world = WorldSpec(meta=WorldMeta(world_id='CATALOGUE-CHECK', title='catalogue metadata check'),
                      math_models=[MathModelRecord.model_validate(record)])
    result = api.admit_scientific_use(world, None, None)
    assert result['status'] == 'UNDETERMINED'
    assert result['reasons'] == ['BINDING_MISSING', 'REVIEW_MISSING']
