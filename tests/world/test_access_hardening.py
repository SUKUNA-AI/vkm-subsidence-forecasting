"""Data-free schema, identity and multiprocess access regressions."""
from __future__ import annotations

import json
import errno
import multiprocessing as mp
from pathlib import Path

import pytest

from vkm_world.core.io import sha256_file
from vkm_world.validation import access
from vkm_world.validation.splits import SealedTestError

H = 'a' * 64


def _candidate(seed=0, **overrides):
    fields = dict(task='subsidence_rate', split_version='v1', model_id='baseline', dataset_sha256=H,
                  contract_hashes={'features': H, 'targets': H}, manifest_hashes={'train': H, 'validation': H},
                  evaluation_spec_sha256=H, code_commit='cf0d1c8', random_seed=seed, environment_sha256=H)
    fields.update(overrides)
    return access.build_candidate_record(**fields)


def _rehash(record):
    record['candidate_id'] = 'cand-' + access.candidate_digest(record)[:16]
    return record


def _frozen_setup(root):
    artifact = root / 'artifacts' / 'config.json'
    artifact.parent.mkdir()
    artifact.write_text('{}\n', encoding='utf-8')
    record = access.freeze_candidate(root, 'records/candidate.json',
             _candidate(artifact_hashes={'artifacts/config.json': sha256_file(artifact)}))
    current = {key: record[key] for key in ('task', 'split_version', 'contract_hashes', 'manifest_hashes',
               'dataset_sha256', 'evaluation_spec_sha256', 'environment_sha256', 'artifact_hashes')}
    return record, dict(root=root, candidate_target='records/candidate.json', **current)


@pytest.mark.parametrize('field', ['schema_version', 'task', 'split_version', 'model_id', 'dataset_sha256',
    'contract_hashes', 'manifest_hashes', 'evaluation_spec_sha256', 'code_commit', 'random_seed',
    'environment_sha256', 'artifact_hashes', 'test_access_policy'])
def test_self_rehashed_record_cannot_omit_required_field(field):
    record = _candidate()
    del record[field]
    with pytest.raises(access.CandidateFreezeError):
        access.verify_candidate_record(_rehash(record))


@pytest.mark.parametrize(('field', 'value'), [
    ('schema_version', True), ('schema_version', 2), ('task', None), ('task', ''), ('model_id', 3),
    ('dataset_sha256', 'not-a-sha'), ('environment_sha256', H + '\n'), ('random_seed', True),
    ('code_commit', 'not-a-commit'), ('contract_hashes', []), ('contract_hashes', {'features': 'bad'}),
    ('manifest_hashes', {'validation': H}), ('artifact_hashes', {'../escape.json': H}),
    ('test_access_policy', 'retry'), ('notes', 1), ('notes', None), ('notes', ''),
    ('frozen_at_utc', 'not-a-time'), ('unknown_field', H),
])
def test_self_rehashed_record_cannot_bypass_schema(field, value):
    with pytest.raises(access.CandidateFreezeError):
        access.verify_candidate_record(_rehash({**_candidate(), field: value}))


def test_authorization_accepts_exact_persisted_current_setup(tmp_path):
    record, current = _frozen_setup(tmp_path)
    assert access.authorize_test_access(record, **current) == record['candidate_id']


@pytest.mark.parametrize('field', ['task', 'split_version'])
def test_authorization_rejects_current_task_or_split_change(tmp_path, field):
    record, current = _frozen_setup(tmp_path)
    current[field] = 'different'
    with pytest.raises(SealedTestError, match='different task or split version'):
        access.authorize_test_access(record, **current)


def test_authorization_rejects_empty_frozen_artifact_identity(tmp_path):
    record = access.freeze_candidate(tmp_path, 'records/candidate.json', _candidate())
    current = {key: record[key] for key in ('task', 'split_version', 'contract_hashes', 'manifest_hashes',
               'dataset_sha256', 'evaluation_spec_sha256', 'environment_sha256', 'artifact_hashes')}
    with pytest.raises(SealedTestError, match='non-empty frozen artifact_hashes'):
        access.authorize_test_access(record, root=tmp_path, candidate_target='records/candidate.json', **current)


@pytest.mark.parametrize('error_number', [errno.EXDEV, errno.ENOTSUP, errno.EACCES])
def test_unsupported_or_denied_publish_fails_closed(tmp_path, monkeypatch, error_number):
    def unsupported_link(*args, **kwargs):
        raise OSError(error_number, 'synthetic unavailable hardlink')
    monkeypatch.setattr(access.os, 'link', unsupported_link)
    with pytest.raises(OSError) as failed:
        access.freeze_candidate(tmp_path, 'records/candidate.json', _candidate())
    assert failed.value.errno == error_number
    assert not (tmp_path / 'records' / 'candidate.json').exists()
    assert not list((tmp_path / 'work').rglob('*.tmp'))


@pytest.mark.parametrize('field', ['dataset_sha256', 'evaluation_spec_sha256', 'environment_sha256',
                                  'artifact_hashes', 'manifest_hashes'])
def test_authorization_rejects_current_identity_change(tmp_path, field):
    record, current = _frozen_setup(tmp_path)
    current[field] = 'b' * 64 if field.endswith('sha256') else {next(iter(current[field])): 'b' * 64}
    with pytest.raises(SealedTestError):
        access.authorize_test_access(record, **current)


def test_authorization_rejects_partial_manifest_setup(tmp_path):
    record, current = _frozen_setup(tmp_path)
    current['manifest_hashes'] = {'train': H}
    with pytest.raises(SealedTestError):
        access.authorize_test_access(record, **current)


@pytest.mark.parametrize('operation', ['missing', 'changed'])
def test_authorization_rejects_missing_or_changed_artifact(tmp_path, operation):
    record, current = _frozen_setup(tmp_path)
    artifact = tmp_path / 'artifacts' / 'config.json'
    if operation == 'missing':
        artifact.unlink()
    else:
        artifact.write_text('{"changed": true}\n', encoding='utf-8')
    with pytest.raises(SealedTestError):
        access.authorize_test_access(record, **current)


@pytest.mark.parametrize('operation', ['missing', 'different', 'unfrozen'])
def test_authorization_requires_matching_persisted_freeze(tmp_path, operation):
    record, current = _frozen_setup(tmp_path)
    target = tmp_path / 'records' / 'candidate.json'
    if operation == 'missing':
        target.unlink()
    elif operation == 'different':
        target.write_text(json.dumps({**_candidate(1), 'frozen_at_utc': access.utc_now()}), encoding='utf-8')
    else:
        changed = dict(record)
        del changed['frozen_at_utc']
        target.write_text(json.dumps(changed), encoding='utf-8')
    with pytest.raises(SealedTestError):
        access.authorize_test_access(record, **current)


def _freeze_worker(root, seed, start, absence, results):
    original_exists = Path.exists
    def coordinated_exists(path, **kwargs):
        present = original_exists(path, **kwargs)
        if path.name == 'candidate.json' and not present:
            absence.wait(timeout=8)
        return present
    Path.exists = coordinated_exists
    try:
        start.wait(timeout=8)
        record = access.freeze_candidate(root, 'records/candidate.json', _candidate(seed))
        results.put(('success', record['candidate_id']))
    except access.CandidateFreezeError as exc:
        results.put(('rejected', str(exc)))
    except BaseException as exc:
        results.put(('unexpected', repr(exc)))


def _finalize_worker(root, status, entered, release, results):
    if entered is not None:
        original_read = Path.read_text
        def paused_read(path, *args, **kwargs):
            content = original_read(path, *args, **kwargs)
            if path.name == 'test.json':
                entered.set()
                if not release.wait(timeout=8):
                    raise TimeoutError('parent did not release first finalizer')
            return content
        Path.read_text = paused_read
    try:
        ledger = access.finalize_test_access(root, 'ledger/test.json', status=status)
        results.put(('success', ledger['status']))
    except access.RepeatedTestAccessError as exc:
        results.put(('rejected', str(exc)))
    except BaseException as exc:
        results.put(('unexpected', repr(exc)))


def _join(processes):
    try:
        for process in processes:
            process.join(timeout=10)
            assert not process.is_alive(), 'access worker exceeded timeout'
            assert process.exitcode == 0
    finally:
        for process in processes:
            if process.is_alive():
                process.terminate()
                process.join(timeout=3)


@pytest.mark.parametrize('seeds', [(0, 1), (0, 0)])
def test_concurrent_freeze_is_publish_once(tmp_path, seeds):
    context = mp.get_context('spawn')
    start, absence, results = context.Barrier(2), context.Barrier(2), context.Queue()
    workers = [context.Process(target=_freeze_worker, args=(tmp_path, seed, start, absence, results))
               for seed in seeds]
    for worker in workers:
        worker.start()
    _join(workers)
    outcomes = [results.get(timeout=3) for _ in workers]
    expected_successes = 1 if seeds[0] != seeds[1] else 2
    assert sum(outcome[0] == 'success' for outcome in outcomes) == expected_successes, outcomes
    assert all(outcome[0] in {'success', 'rejected'} for outcome in outcomes), outcomes
    saved = json.loads((tmp_path / 'records' / 'candidate.json').read_text(encoding='utf-8'))
    assert all(outcome[1] == saved['candidate_id'] for outcome in outcomes if outcome[0] == 'success')
    assert not list((tmp_path / 'work').rglob('*.tmp'))


def test_concurrent_finalization_cannot_overwrite_terminal_status(tmp_path):
    access.claim_test_access(tmp_path, 'ledger/test.json', _candidate(), test_sample_ids_sha256=H, test_rows=1)
    context = mp.get_context('spawn')
    entered, release, results = context.Event(), context.Event(), context.Queue()
    first = context.Process(target=_finalize_worker, args=(tmp_path, 'consumed', entered, release, results))
    second = context.Process(target=_finalize_worker,
                             args=(tmp_path, 'failed_after_claim', None, None, results))
    first.start()
    try:
        assert entered.wait(timeout=8), 'first finalizer did not read ledger'
        second.start()
        second.join(timeout=10)
        assert not second.is_alive(), 'second finalizer exceeded timeout'
    finally:
        release.set()
        _join([worker for worker in (first, second) if worker.pid is not None])
    outcomes = [results.get(timeout=3) for _ in range(2)]
    assert sum(outcome[0] == 'success' for outcome in outcomes) == 1, outcomes
    assert all(outcome[0] in {'success', 'rejected'} for outcome in outcomes), outcomes
    saved = json.loads((tmp_path / 'ledger' / 'test.json').read_text(encoding='utf-8'))
    assert saved['status'] == next(value for status, value in outcomes if status == 'success')


def test_crashed_finalizer_releases_lock_but_claim_remains_spent(tmp_path):
    access.claim_test_access(tmp_path, 'ledger/test.json', _candidate(), test_sample_ids_sha256=H, test_rows=1)
    context = mp.get_context('spawn')
    entered, release, results = context.Event(), context.Event(), context.Queue()
    worker = context.Process(target=_finalize_worker, args=(tmp_path, 'consumed', entered, release, results))
    worker.start()
    try:
        assert entered.wait(timeout=8), 'finalizer did not read ledger'
    finally:
        worker.terminate()
        worker.join(timeout=3)
    assert not worker.is_alive()
    assert access.finalize_test_access(tmp_path, 'ledger/test.json', status='failed_after_claim')['status'] == \
        'failed_after_claim'
    with pytest.raises(access.RepeatedTestAccessError):
        access.claim_test_access(tmp_path, 'ledger/test.json', _candidate(), test_sample_ids_sha256=H, test_rows=1)
