"""Publish only allowlisted aggregates; never source readings or coordinates."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

from benchmarks.abc_completion_v1.package import sha
from benchmarks.abc_completion_v1.frozen_integrity import verify as verify_frozen
from vkm_world.governance.publication import publish_batch, strict_json


OUTPUT_PATHS = {
    'completion': 'completion_receipt.json', 'A_ACCEPTED': 'accepted/A_ACCEPTED.json',
    'B_ACCEPTED': 'accepted/B_ACCEPTED.json', 'C_ACCEPTED': 'accepted/C_ACCEPTED.json',
    'queue_closure': 'queue_closure.json', 'astra_manifest': 'ASTRA_REVIEW_PACKAGE/manifest.json',
    'astra_source_first_index': 'ASTRA_REVIEW_PACKAGE/source_first_index.json',
    'frozen_integrity': 'frozen_integrity.json', 'GPU_release': 'retries/gpu_release_receipt.json',
    'coverage_finalization': 'coverage/screening_finalization_receipt.json',
    'topology_classification': 'geometry/topology/classification.json',
    'topology_receipt': 'geometry/topology/receipt.json',
    'topology_qgis_acceptance': 'geometry/topology/qgis_acceptance.json',
}
QA_CHECKS = ('accepted_datasets', 'queue_closure', 'glm_retry_closure', 'coverage', 'topology',
             'frozen_integrity', 'public_leakage', 'tests', 'canonical_repository')
QA_LIMITATIONS = ('optional_runtime_skips', 'unavailable_frozen_git_refs')


def integer(value, name):
    if type(value) is not int or value < 0:
        raise ValueError(f'{name} must be a nonnegative integer')
    return value


def object_fields(value, fields, name):
    if not isinstance(value, dict) or set(value) != set(fields):
        raise ValueError(f'{name} must contain exactly the public schema fields')
    return {key: validate(value[key], spec, f'{name}.{key}') for key, spec in fields.items()}


def validate(value, spec, name):
    if spec is int:
        return integer(value, name)
    if isinstance(spec, dict):
        return object_fields(value, spec, name)
    if isinstance(spec, frozenset):
        if not isinstance(value, dict) or not set(value).issubset(spec):
            raise ValueError(f'{name} contains unknown count categories')
        return {key: integer(count, f'{name}.{key}') for key, count in value.items()}
    if type(value) is not type(spec) or value != spec:
        raise ValueError(f'{name} has an unsupported public value')
    return value


AGGREGATE_SCHEMA = {
    'original_queue': {'cases': int, 'processed': int, 'by_dataset': frozenset('ABC')},
    'glm_retry_closure': {'truncation_cases_closed': int, 'reader_attempt_count': int,
        'resolution_counts': frozenset(('OCR_RETRY_COMPLETE_UNREVIEWED', 'VERIFIED_NO_TEXT_IN_TILE',
                                       'SOURCE_LABELS_PARTLY_UNREADABLE'))},
    'coverage': {'sources': int, 'pages_screened': int,
        'source_sha_counts': frozenset(('MATCH', 'MISSING', 'MISMATCH')),
        'status_counts': frozenset(('EXCLUDED_BY_REGISTER', 'UNRESOLVED_SOURCE_INTEGRITY',
            'UNRESOLVED_CANONICAL_PAGES', 'SCREENED_WITH_GAPS', 'SCREENED_AUTOMATICALLY')),
        'exhaustive_figure_recall_proven': False, 'candidate_dispositions': int,
        'visually_role_checked_candidates': int, 'new_explicit_candidates_after_dedup': int,
        'disposition_counts': frozenset(('EXISTING_INVENTORY_REGION', 'PAGE_CONTEXT_ONLY_NO_OBJECT_ASSOCIATION',
            'GENERAL_METHOD_DISCOVERY_ONLY', 'OTHER_SITE_OR_ANALOG_DISCOVERY_ONLY',
            'REGIONAL_OR_MIXED_SCOPE_DISCOVERY_ONLY'))},
    'astra_review': {'case_count': int, 'priorities': frozenset(('P0', 'P1', 'P2', 'P3'))},
    'topology': {'original_invalid': int,
        'status_counts': {'REPAIRED_GRAPHIC_CANDIDATE': int, 'UNRESOLVED_GRAPHIC_TOPOLOGY': int},
        'repaired_layers': int, 'strict_qgis_passed': int,
        'semantic_geometry_accepted_by_topology': False, 'new_coordinates_or_nonzero_edges': 0},
    'frozen_integrity': {'status': 'PASS', 'checked_file_count': int},
}


def aggregates(completion):
    if (not isinstance(completion, dict) or not isinstance(completion.get('datasets'), dict)
            or set(completion['datasets']) != set('ABC')):
        raise ValueError('public receipt requires exactly datasets A, B and C')
    validate(completion.get('raw_frozen_unchanged'), True, 'raw_frozen_unchanged')
    validate(completion.get('admitted_to_evidence'), False, 'admitted_to_evidence')
    datasets = {}
    for name, summary in completion['datasets'].items():
        if not isinstance(summary, dict):
            raise ValueError('dataset summary must be an object')
        datasets[name] = object_fields({key: summary.get(key) for key in ('record_count', 'counts')},
            {'record_count': int, 'counts': {key: int for key in ('ACCEPTED', 'CONDITIONAL', 'UNRESOLVED')}},
            f'datasets.{name}')
        if sum(datasets[name]['counts'].values()) != datasets[name]['record_count']:
            raise ValueError('dataset counts do not cover records')
    public = {key: validate(completion.get(key), spec, key) for key, spec in AGGREGATE_SCHEMA.items()}
    queue, retry, coverage, astra, topology, frozen = (public[key] for key in AGGREGATE_SCHEMA)
    if (queue['cases'] != queue['processed'] or sum(queue['by_dataset'].values()) != queue['processed']
            or sum(retry['resolution_counts'].values()) != retry['truncation_cases_closed']
            or retry['reader_attempt_count'] < retry['truncation_cases_closed']
            or sum(coverage['source_sha_counts'].values()) != coverage['sources']
            or sum(coverage['status_counts'].values()) != coverage['sources']
            or sum(coverage['disposition_counts'].values()) != coverage['candidate_dispositions']
            or coverage['visually_role_checked_candidates'] > coverage['candidate_dispositions']
            or coverage['new_explicit_candidates_after_dedup'] > coverage['candidate_dispositions']
            or sum(astra['priorities'].values()) != astra['case_count']
            or sum(topology['status_counts'].values()) != topology['original_invalid']
            or topology['strict_qgis_passed'] != topology['status_counts']['REPAIRED_GRAPHIC_CANDIDATE']
            or topology['repaired_layers'] > topology['strict_qgis_passed']
            or frozen['checked_file_count'] == 0):
        raise ValueError('public aggregate totals or completed checks disagree')
    return {'schema':'vkm.sol_abc_public_receipt/1','date':'2026-09-30',
        'snapshot':'snap-20260929T175107Z-574daaac',
        'datasets':datasets, **public,
        'admitted_to_evidence':False,'raw_frozen_unchanged':True,
        'private_scientific_data_published':False,'new_qwen_calls':0,
        'core_edge_changed':False,'solver_ml_cad_runs':0}


def checked_verification(verification, output_hashes, code_hashes):
    """QA binds the exact current inputs; arbitrary reviewer prose stays PRIVATE."""
    fields = {'schema', 'status', 'private_output_hashes', 'code_hashes', 'checks', 'limitations'}
    if not isinstance(verification, dict) or set(verification) != fields:
        raise ValueError('verification must use the closed vkm.sol_abc_verification/1 schema')
    if (verification['schema'] != 'vkm.sol_abc_verification/1'
            or verification['status'] not in ('PASS', 'PASS_WITH_DISCLOSED_LIMITATIONS')):
        raise ValueError('complete QA before publishing the final public receipt')
    if verification['private_output_hashes'] != output_hashes or verification['code_hashes'] != code_hashes:
        raise ValueError('verification is stale: current artifact or code hashes changed')
    checks = verification['checks']
    if not isinstance(checks, dict) or set(checks) != set(QA_CHECKS):
        raise ValueError('verification must cover every required QA check')
    for name, status in checks.items():
        allowed = ('PASS', 'PASS_WITH_DISCLOSED_LIMITATIONS') if name in ('tests', 'canonical_repository') else ('PASS',)
        if status not in allowed:
            raise ValueError(f'QA check is not completed: {name}')
    limitations = validate(verification['limitations'], frozenset(QA_LIMITATIONS), 'verification.limitations')
    limited = any(limitations.values())
    if ((verification['status'] == 'PASS_WITH_DISCLOSED_LIMITATIONS') != limited
            or ('PASS_WITH_DISCLOSED_LIMITATIONS' in checks.values()) != limited
            or (checks['tests'] == 'PASS_WITH_DISCLOSED_LIMITATIONS') != bool(limitations.get('optional_runtime_skips'))
            or (checks['canonical_repository'] == 'PASS_WITH_DISCLOSED_LIMITATIONS') != bool(limitations.get('unavailable_frozen_git_refs'))):
        raise ValueError('QA limitations must be explicit and agree with check statuses')
    return {'schema': verification['schema'], 'status': verification['status'],
            'private_output_hashes': dict(output_hashes), 'code_hashes': dict(code_hashes),
            'checks': dict(checks), 'limitations': limitations}


def main(args):
    repo=Path.cwd().resolve();root=(repo/args.work).resolve();out=(repo/args.out).resolve()
    if not root.is_relative_to(repo/'work'):raise ValueError('full provenance must be read from PRIVATE work')
    if out.parent!=repo/'docs/corpus_platform/receipts':raise ValueError('publish only into public receipts')
    output_files={name:(root/path).resolve() for name,path in OUTPUT_PATHS.items()}
    if any(not path.is_relative_to(repo/'work') for path in output_files.values()):
        raise ValueError('private output references must remain in work')
    # Parsing and SHA-256 always use the same immutable bytes, including private JSON.
    snapshots = {name: path.read_bytes() for name,path in output_files.items()}
    result=aggregates(strict_json(snapshots['completion'].decode('utf8')))
    result['private_output_hashes']={name:hashlib.sha256(data).hexdigest() for name,data in snapshots.items()}
    code_files = set((repo/'benchmarks/abc_completion_v1').glob('*.py'))
    code_files.update(repo/path for path in ('src/vkm_world/governance/publication.py',
                                           'src/vkm_world/governance/leakage.py'))
    code_snapshots = {path: path.read_bytes() for path in code_files}
    result['code_hashes']={path.relative_to(repo).as_posix():hashlib.sha256(data).hexdigest()
                           for path,data in sorted(code_snapshots.items())}
    verification_path = (root/'verification_summary.json').resolve()
    if not verification_path.is_relative_to(repo/'work'):
        raise ValueError('private verification must remain in work')
    verification_bytes = verification_path.read_bytes()
    verification=strict_json(verification_bytes.decode('utf8'))
    result['verification']=checked_verification(verification,result['private_output_hashes'],result['code_hashes'])
    reference_files = [repo/path for path in ('docs/corpus_platform/receipts/figure_readings_v2.json',
                                             'docs/corpus_platform/receipts/qgis_mcp_2026-09-30.json',
                                             'work/geometry_2026-09-29/layers_manifest.json')]
    reference_hashes = {path: sha(path) for path in reference_files}
    actual_frozen = verify_frozen(repo)
    recorded_frozen = strict_json(snapshots['frozen_integrity'].decode('utf8'))
    if (recorded_frozen.get('status') != 'PASS' or recorded_frozen.get('files') != actual_frozen['files']
            or recorded_frozen.get('checked_file_count') != actual_frozen['checked_file_count']
            or result['frozen_integrity']['checked_file_count'] != actual_frozen['checked_file_count']):
        raise ValueError('frozen assertion does not match the verified frozen file table')

    def recheck_identities():
        # publish_batch invokes this under its native process lock after all staging,
        # scanning and backups, immediately before the first destination replacement.
        current_code = set((repo/'benchmarks/abc_completion_v1').glob('*.py'))
        current_code.update(repo/path for path in ('src/vkm_world/governance/publication.py',
                                                 'src/vkm_world/governance/leakage.py'))
        if current_code != code_files:
            raise ValueError('publication code inventory changed after QA')
        expected = {output_files[name]: digest for name,digest in result['private_output_hashes'].items()}
        expected.update({path:hashlib.sha256(data).hexdigest() for path,data in code_snapshots.items()})
        expected.update(reference_hashes)
        expected[verification_path] = hashlib.sha256(verification_bytes).hexdigest()
        expected.update({repo/path:digest for path,digest in actual_frozen['files'].items()})
        if any(not path.is_file() or sha(path) != digest for path,digest in expected.items()):
            raise ValueError('publication input, code, QA or frozen identity changed before replacement')
    publish_batch(repo, {out.relative_to(repo).as_posix(): (
        json.dumps(result,ensure_ascii=False,indent=2,sort_keys=True,allow_nan=False)+'\n').encode('utf8')},
        before_replace=recheck_identities)
    print('Published source-free A/B/C aggregate receipt')


if __name__=='__main__':
    parser=argparse.ArgumentParser()
    parser.add_argument('--work',default='work/abc_completion_2026-09-30')
    parser.add_argument('--out',default='docs/corpus_platform/receipts/abc_sol_completion_2026-09-30.json')
    main(parser.parse_args())
