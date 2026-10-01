"""Synthetic publication inputs: source text and private IDs never leave work."""
from types import SimpleNamespace

import pytest

from benchmarks.abc_completion_v1 import public_receipt as P
from benchmarks.abc_completion_v1.frozen_integrity import verify as frozen_verify
from benchmarks.abc_completion_v1.package import load, sha, write


def completion():
    return {'datasets': {k: {'record_count': 1, 'counts': {'ACCEPTED': 1, 'CONDITIONAL': 0, 'UNRESOLVED': 0},
                            'partitions': {'ACCEPTED': ['synthetic-private-id']}} for k in 'ABC'},
            'original_queue': {'cases': 3, 'processed': 3, 'by_dataset': {k: 1 for k in 'ABC'}},
            'glm_retry_closure': {'truncation_cases_closed': 1, 'reader_attempt_count': 1,
                                  'resolution_counts': {'OCR_RETRY_COMPLETE_UNREVIEWED': 1}},
            'coverage': {'sources': 1, 'pages_screened': 2, 'source_sha_counts': {'MATCH': 1},
                         'status_counts': {'SCREENED_AUTOMATICALLY': 1}, 'exhaustive_figure_recall_proven': False,
                         'candidate_dispositions': 1, 'visually_role_checked_candidates': 0,
                         'new_explicit_candidates_after_dedup': 1,
                         'disposition_counts': {'GENERAL_METHOD_DISCOVERY_ONLY': 1}},
            'astra_review': {'case_count': 1, 'priorities': {'P1': 1}},
            'topology': {'original_invalid': 1, 'status_counts': {'REPAIRED_GRAPHIC_CANDIDATE': 1,
                         'UNRESOLVED_GRAPHIC_TOPOLOGY': 0}, 'repaired_layers': 1, 'strict_qgis_passed': 1,
                         'semantic_geometry_accepted_by_topology': False, 'new_coordinates_or_nonzero_edges': 0},
            'frozen_integrity': {'status': 'PASS', 'checked_file_count': 1},
            'raw_frozen_unchanged': True, 'admitted_to_evidence': False}


@pytest.mark.parametrize('section', ['original_queue', 'glm_retry_closure', 'coverage', 'astra_review',
                                    'topology', 'frozen_integrity'])
def test_unknown_nested_fields_cannot_be_published(section):
    value = completion()
    value[section]['unrecognized'] = {'scientific_payload': 'synthetic-sentinel'}
    with pytest.raises(ValueError):
        P.aggregates(value)


@pytest.mark.parametrize('bad', [True, -1, ['synthetic-sentinel'], float('nan'), float('inf'), 1.0])
def test_count_requires_nonnegative_integer(bad):
    value = completion()
    value['datasets']['A']['record_count'] = bad
    with pytest.raises(ValueError):
        P.aggregates(value)


def test_unknown_count_keys_and_inconsistent_totals_are_rejected():
    value = completion()
    value['coverage']['status_counts']['synthetic-sentinel'] = 1
    with pytest.raises(ValueError):
        P.aggregates(value)
    value = completion()
    value['datasets']['A']['counts']['ACCEPTED'] = 2
    with pytest.raises(ValueError):
        P.aggregates(value)


def test_public_projection_keeps_counts_and_drops_private_partitions():
    result = P.aggregates(completion())
    assert result['datasets']['A']['counts']['ACCEPTED'] == 1
    assert 'synthetic-private-id' not in str(result)


def publication_fixture(repo):
    """Real frozen-byte verification over tiny synthetic manifests and inputs."""
    old = repo/'work/figure_readings_2026-09-29/v2'
    write(old/'input.json', {'synthetic': 1})
    write(old/'raw_responses_manifest.json', {})
    write(old/'assembled_outputs_manifest.json', {'files': {}})
    write(repo/'docs/corpus_platform/receipts/figure_readings_v2.json', {
        'input_hashes': {'input.json': sha(old/'input.json')}, 'manual_review_hashes': {},
        'raw_response_manifest_sha256': sha(old/'raw_responses_manifest.json'),
        'output_manifest_hash': sha(old/'assembled_outputs_manifest.json')})
    gpkg = repo/'work/geometry_2026-09-29/geometry_sources.gpkg'
    write(gpkg, {'synthetic': 2})
    manifest = repo/'work/geometry_2026-09-29/qgis_import_manifest.json'
    write(manifest, {})
    write(repo/'work/geometry_2026-09-29/layers_manifest.json', {'layers': []})
    acceptance = repo/'work/qgis_2026-09-30/source_import/acceptance.json'
    write(acceptance, {'output_sha256': sha(gpkg), 'manifest_sha256': sha(manifest)})
    write(repo/'docs/corpus_platform/receipts/qgis_mcp_2026-09-30.json', {
        'acceptance_artifacts': {acceptance.relative_to(repo).as_posix(): sha(acceptance)}})
    root = repo/'work/run'
    frozen = frozen_verify(repo)
    files = {'completion': 'completion_receipt.json', 'A_ACCEPTED': 'accepted/A_ACCEPTED.json',
             'B_ACCEPTED': 'accepted/B_ACCEPTED.json', 'C_ACCEPTED': 'accepted/C_ACCEPTED.json',
             'queue_closure': 'queue_closure.json', 'astra_manifest': 'ASTRA_REVIEW_PACKAGE/manifest.json',
             'astra_source_first_index': 'ASTRA_REVIEW_PACKAGE/source_first_index.json',
             'frozen_integrity': 'frozen_integrity.json', 'GPU_release': 'retries/gpu_release_receipt.json',
             'coverage_finalization': 'coverage/screening_finalization_receipt.json',
             'topology_classification': 'geometry/topology/classification.json',
             'topology_receipt': 'geometry/topology/receipt.json',
             'topology_qgis_acceptance': 'geometry/topology/qgis_acceptance.json'}
    for name, rel in files.items():
        write(root/rel, frozen if name == 'frozen_integrity' else {'synthetic': True})
    value = completion()
    value['frozen_integrity']['checked_file_count'] = frozen['checked_file_count']
    write(root/'completion_receipt.json', value)
    code = repo/'benchmarks/abc_completion_v1/public_receipt.py'
    code.parent.mkdir(parents=True)
    code.write_text('# synthetic code\n')
    helpers = [repo/path for path in ('src/vkm_world/governance/publication.py',
                                     'src/vkm_world/governance/leakage.py')]
    for path in helpers:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('# synthetic helper\n')
    verification = {'schema': 'vkm.sol_abc_verification/1', 'status': 'PASS',
                    'private_output_hashes': {name: sha(root/rel) for name, rel in files.items()},
                    'code_hashes': {path.relative_to(repo).as_posix(): sha(path) for path in [code, *helpers]},
                    'checks': {name: 'PASS' for name in ('accepted_datasets', 'queue_closure', 'glm_retry_closure',
                              'coverage', 'topology', 'frozen_integrity', 'public_leakage', 'tests',
                              'canonical_repository')}, 'limitations': {}}
    write(root/'verification_summary.json', verification)
    return root, verification, old/'input.json'


@pytest.mark.parametrize('mutation', ['not_run', 'stale_output', 'stale_code', 'extra', 'frozen_changed', 'false_claim'])
def test_publication_gate_preserves_previous_output_on_failure(tmp_path, monkeypatch, mutation):
    root, verification, frozen_file = publication_fixture(tmp_path)
    target = tmp_path/'docs/corpus_platform/receipts/result.json'
    write(target, {'previous': True})
    before = target.read_bytes()
    if mutation == 'not_run':
        verification['checks']['tests'] = 'NOT_RUN'
    elif mutation == 'stale_output':
        write(root/'accepted/A_ACCEPTED.json', {'changed': True})
    elif mutation == 'stale_code':
        (tmp_path/'benchmarks/abc_completion_v1/public_receipt.py').write_text('# changed\n')
    elif mutation == 'extra':
        verification['unrecognized'] = {'scientific_payload': 'synthetic-sentinel'}
    elif mutation == 'frozen_changed':
        write(frozen_file, {'changed': True})
    else:
        value = load(root/'completion_receipt.json')
        value['raw_frozen_unchanged'] = False
        write(root/'completion_receipt.json', value)
        verification['private_output_hashes']['completion'] = sha(root/'completion_receipt.json')
    write(root/'verification_summary.json', verification)
    monkeypatch.chdir(tmp_path)
    with pytest.raises(ValueError):
        P.main(SimpleNamespace(work='work/run', out='docs/corpus_platform/receipts/result.json'))
    assert target.read_bytes() == before


def test_publication_accepts_current_hash_bound_qa_and_is_deterministic(tmp_path, monkeypatch):
    publication_fixture(tmp_path)
    monkeypatch.chdir(tmp_path)
    args = SimpleNamespace(work='work/run', out='docs/corpus_platform/receipts/result.json')
    P.main(args)
    target = tmp_path/args.out
    first = target.read_bytes()
    P.main(args)
    assert target.read_bytes() == first
    assert load(target)['raw_frozen_unchanged'] is True


def test_completion_parse_and_hash_cannot_bind_different_versions(tmp_path, monkeypatch):
    root, verification, _ = publication_fixture(tmp_path)
    target = tmp_path/'docs/corpus_platform/receipts/result.json'
    write(target, {'previous': True})
    before = target.read_bytes()
    original = P.strict_json
    first = True
    def mutate_after_parse(text):
        nonlocal first
        value = original(text)
        if first:
            first = False
            changed = load(root/'completion_receipt.json')
            changed['datasets']['A']['record_count'] = 2
            changed['datasets']['A']['counts']['ACCEPTED'] = 2
            write(root/'completion_receipt.json', changed)
            verification['private_output_hashes']['completion'] = sha(root/'completion_receipt.json')
            write(root/'verification_summary.json', verification)
        return value
    monkeypatch.setattr(P, 'strict_json', mutate_after_parse)
    monkeypatch.chdir(tmp_path)
    with pytest.raises(ValueError):
        P.main(SimpleNamespace(work='work/run', out='docs/corpus_platform/receipts/result.json'))
    assert target.read_bytes() == before


@pytest.mark.parametrize('mutation', ['artifact', 'code', 'qa', 'frozen', 'frozen_reference'])
def test_changed_identity_at_publication_boundary_preserves_previous_output(tmp_path, monkeypatch, mutation):
    from vkm_world.governance import publication
    root, _, frozen_file = publication_fixture(tmp_path)
    target = tmp_path/'docs/corpus_platform/receipts/result.json'
    write(target, {'previous': True})
    before = target.read_bytes()
    original = publication.scan
    def mutate_after_output_scan(*args, **kwargs):
        problems = original(*args, **kwargs)
        path = {'artifact': root/'accepted/A_ACCEPTED.json',
                'code': tmp_path/'benchmarks/abc_completion_v1/public_receipt.py',
                'qa': root/'verification_summary.json', 'frozen': frozen_file,
                'frozen_reference': tmp_path/'docs/corpus_platform/receipts/figure_readings_v2.json'}[mutation]
        path.write_bytes(b'{"changed": true}\n')
        return problems
    monkeypatch.setattr(publication, 'scan', mutate_after_output_scan)
    monkeypatch.chdir(tmp_path)
    with pytest.raises(ValueError):
        P.main(SimpleNamespace(work='work/run', out='docs/corpus_platform/receipts/result.json'))
    assert target.read_bytes() == before
