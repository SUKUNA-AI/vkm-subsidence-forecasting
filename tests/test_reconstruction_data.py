import pandas as pd
import pytest

from skru1.reconstruction_data import (
    METADATA, field_catalog, known_source_available, reconcile_membership,
    preserve_unchanged_csv_cells, repair_samples, select_features,
)


def measurement_fixture(rejected=True):
    membership = pd.DataFrame([
        dict(campaign_id='C1', point_id='P1', profile_id='L1', date='2020-01-01',
             targeted=True, observed=True, membership_status='observed', missing_reason=''),
        dict(campaign_id='C2', point_id='P1', profile_id='L1', date='2020-02-01',
             targeted=True, observed=True, membership_status='observed', missing_reason=''),
    ])
    adjusted = pd.DataFrame([dict(campaign_id='C1', point_id='P1', profile_id='L1',
        date='2020-01-01', qc_status='accepted', observed_settlement_mm=10., standard_uncertainty_mm=1.)])
    runs = pd.DataFrame([
        dict(run_id='R1', campaign_id='C1', profile_id='L1', qc_status='accepted'),
        dict(run_id='R2', campaign_id='C2', profile_id='L1', qc_status='rejected_repeat_required' if rejected else 'accepted'),
    ])
    return membership, adjusted, runs


def test_failed_qc_is_not_observed_and_failure_is_evidenced():
    membership, adjusted, runs = measurement_fixture()
    fixed, log = reconcile_membership(membership, adjusted, runs)
    assert fixed.observed.tolist() == [True, False]
    assert fixed.iloc[1].missing_reason == 'rejected_after_qc'
    assert log.iloc[0].evidence_run_ids == 'R2'
    assert membership.observed.tolist() == [True, True]


def test_absent_adjustment_does_not_invent_qc_failure():
    fixed, log = reconcile_membership(*measurement_fixture(rejected=False))
    assert fixed.iloc[1].missing_reason == 'missing_accepted_adjustment'
    assert log.iloc[0].evidence_run_ids == ''


def test_duplicate_and_mismatched_measurement_keys_fail_closed():
    membership, adjusted, runs = measurement_fixture()
    with pytest.raises(ValueError, match='duplicate'):
        reconcile_membership(membership, pd.concat([adjusted, adjusted]), runs)
    adjusted.loc[0, 'date'] = '2020-01-02'
    with pytest.raises(ValueError, match='dates differ'):
        reconcile_membership(membership, adjusted, runs)


@pytest.mark.parametrize('available', [None, '', pd.NA, '2023-02-01'])
def test_unknown_or_future_source_is_unavailable(available):
    assert not known_source_available(available, '2020-01-01')


def test_known_source_at_origin_is_available():
    assert known_source_available('2020-01-01', '2020-01-01')


def feature_fixture():
    f = pd.DataFrame([{**{c:'key' for c in METADATA}, 'current_date':'2020-01-01',
        'last_rate_mm_y':20., 'kzt':.5}])
    contract = pd.DataFrame([{'field':c,'role':'METADATA' if c in METADATA else 'MODEL_FEATURE',
        'allowed':c not in METADATA,'reason':''} for c in f])
    return f, field_catalog(contract, list(f))


def test_static_snapshot_is_excluded_by_default_and_requires_explicit_assumption():
    f, catalog = feature_fixture()
    assert 'kzt' not in select_features(f, catalog)
    with pytest.raises(ValueError, match='explicit'):
        select_features(f, catalog, view='reconstruction_assumptions')
    augmented = select_features(f, catalog, view='reconstruction_assumptions', acknowledge_assumptions=True)
    assert augmented.kzt.iloc[0] == .5
    assert pd.isna(catalog.set_index('field').loc['kzt', 'source_available_at'])


def test_a_known_future_timestamp_is_checked_not_just_catalog_flag():
    f, catalog = feature_fixture()
    catalog.loc[catalog.field.eq('last_rate_mm_y'), 'source_available_at'] = '2021-01-01'
    with pytest.raises(ValueError, match='unavailable'):
        select_features(f, catalog)


def test_next_successful_observation_cannot_replace_failed_planned_target():
    metadata = dict(sample_id='id', point_id='P1', profile_id='L1',current_campaign_id='C2',
                    current_date='2020-02-01',target_campaign_id='C4',target_date='2020-04-01',
                    split='train',forecast_horizon_days=60)
    f = pd.DataFrame([{**metadata, 'missing_campaigns_since_previous':0}])
    t = pd.DataFrame([{**metadata, 'target_available':True, 'observed_rate_mm_y':1.}])
    membership = pd.DataFrame([dict(campaign_id=c,point_id='P1',profile_id='L1',date=d,
        targeted=True,observed=c!='C3',membership_status='observed' if c!='C3' else 'missing',
        missing_reason='' if c!='C3' else 'rejected_after_qc')
        for c,d in [('C1','2020-01-01'),('C2','2020-02-01'),('C3','2020-03-01'),('C4','2020-04-01')]])
    adjusted = membership.loc[membership.observed].assign(qc_status='accepted')
    with pytest.raises(ValueError, match='next planned'):
        repair_samples(f, t, membership, adjusted)


def test_metadata_repair_preserves_original_numeric_tokens(tmp_path):
    source = tmp_path / 'parent.csv'
    source.write_text('sample_id,value,status\na,0.123456789012345678901,old\nb,,\n')
    before = pd.read_csv(source)
    after = before.iloc[::-1].reset_index(drop=True).copy()
    after['status'] = 'corrected'
    saved = preserve_unchanged_csv_cells(source, before, after)
    assert saved.sample_id.tolist() == ['b', 'a']
    assert saved.value.tolist() == ['', '0.123456789012345678901']
    assert saved.status.tolist() == ['corrected', 'corrected']
