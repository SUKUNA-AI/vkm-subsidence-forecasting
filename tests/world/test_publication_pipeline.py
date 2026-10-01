"""Publication transactions and catalogue verification over synthetic files only."""
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

ROOT = Path(__file__).resolve().parents[2]


def module(name, rel):
    spec = importlib.util.spec_from_file_location(name, ROOT/rel)
    result = importlib.util.module_from_spec(spec)
    sys.modules[name] = result
    spec.loader.exec_module(result)
    return result


B = module('publication_builder', 'scripts/build_public_catalogues.py')
V = module('publication_verifier', 'scripts/verify_canonical_repository.py')


def put(root, rel, content):
    path = root/rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding='utf8')
    return path


def tree(tmp_path):
    pub, private = tmp_path/'pub', tmp_path/'private'
    canon = private/'11_evidence_vnext/canonical'
    mapping = {'a.csv': 'evidence/a.csv', 'z.csv': 'evidence/z.csv'}
    put(pub, 'scripts/public_catalogue_map.json', json.dumps(mapping))
    for key, target in mapping.items():
        put(canon, key, 'id,summary\n1,synthetic summary\n')
        put(pub, target, 'previous public bytes\n')
    put(pub, 'evidence/PUBLIC_CATALOGUE_MANIFEST.json', '{"previous": true}\n')
    return pub, private, canon, mapping


@pytest.mark.parametrize('failure', ['missing', 'malformed', 'leakage'])
def test_catalogue_validation_failure_has_no_partial_publication(tmp_path, monkeypatch, failure):
    pub, private, canon, mapping = tree(tmp_path)
    if failure == 'missing':
        (canon/'z.csv').unlink()
    elif failure == 'malformed':
        (canon/'z.csv').write_text('')
    else:
        machine = '/home/' + 'user/synthetic-secret/'
        (canon/'z.csv').write_text('id,summary\n1,' + machine + '\n')
    tracked = [pub/path for path in (*mapping.values(), 'evidence/PUBLIC_CATALOGUE_MANIFEST.json')]
    before = {p: p.read_bytes() for p in tracked}
    monkeypatch.setattr(B, 'ROOT', pub)
    monkeypatch.setenv('VKM_RESOURCES_ROOT', str(private))
    assert B.main() != 0
    assert {p: p.read_bytes() for p in tracked} == before


def test_valid_catalogue_build_is_complete_and_deterministic(tmp_path, monkeypatch):
    pub, private, _, mapping = tree(tmp_path)
    monkeypatch.setattr(B, 'ROOT', pub)
    monkeypatch.setenv('VKM_RESOURCES_ROOT', str(private))
    assert B.main() == 0
    manifest = pub/'evidence/PUBLIC_CATALOGUE_MANIFEST.json'
    first = manifest.read_bytes()
    assert B.main() == 0
    assert manifest.read_bytes() == first
    files = json.loads(first)['files']
    assert {(f['source'], f['target']) for f in files} == set(mapping.items())


def test_catalogue_map_cannot_overwrite_its_reserved_manifest(tmp_path, monkeypatch):
    pub, private, canon, mapping = tree(tmp_path)
    mapping['summary.json'] = 'evidence/PUBLIC_CATALOGUE_MANIFEST.json'
    put(pub, 'scripts/public_catalogue_map.json', json.dumps(mapping))
    put(canon, 'summary.json', '{"synthetic": true}')
    tracked = [pub/path for path in (*mapping.values(), 'evidence/PUBLIC_CATALOGUE_MANIFEST.json')]
    before = {p: p.read_bytes() for p in tracked}
    monkeypatch.setattr(B, 'ROOT', pub)
    monkeypatch.setenv('VKM_RESOURCES_ROOT', str(private))
    assert B.main() != 0
    assert {p: p.read_bytes() for p in tracked} == before


def test_catalogue_publish_rolls_back_replace_failure(tmp_path, monkeypatch):
    from vkm_world.governance import publication
    pub, private, _, mapping = tree(tmp_path)
    tracked = [pub/path for path in (*mapping.values(), 'evidence/PUBLIC_CATALOGUE_MANIFEST.json')]
    before = {p: p.read_bytes() for p in tracked}
    monkeypatch.setattr(B, 'ROOT', pub)
    monkeypatch.setenv('VKM_RESOURCES_ROOT', str(private))
    replace = publication.os.replace
    calls = []
    def fail_second(source, target):
        calls.append(target)
        if len(calls) == 2:
            raise OSError('synthetic replacement failure')
        return replace(source, target)
    monkeypatch.setattr(publication.os, 'replace', fail_second)
    assert B.main() != 0
    assert {p: p.read_bytes() for p in tracked} == before


def test_failed_rollback_preserves_original_backup_and_blocks_republication(tmp_path, monkeypatch):
    from vkm_world.governance import publication
    pub, private, _, mapping = tree(tmp_path)
    first = pub/mapping['a.csv']
    original_bytes = first.read_bytes()
    monkeypatch.setattr(B, 'ROOT', pub)
    monkeypatch.setenv('VKM_RESOURCES_ROOT', str(private))
    replace = publication.os.replace
    calls = []
    def fail_publish_and_restore(source, target):
        calls.append(target)
        if len(calls) in (2, 3):
            raise OSError('synthetic replacement/restoration failure')
        return replace(source, target)
    monkeypatch.setattr(publication.os, 'replace', fail_publish_and_restore)
    assert B.main() != 0
    backups = list((pub/'evidence').glob('.vkm-backup-*'))
    assert any(path.read_bytes() == original_bytes for path in backups)
    assert (pub/'work/publication_recovery.json').is_file()
    current_bytes = first.read_bytes()
    monkeypatch.setattr(publication.os, 'replace', replace)
    assert B.main() != 0
    assert first.read_bytes() == current_bytes
    assert any(path.read_bytes() == original_bytes for path in backups)


@pytest.mark.parametrize('bad_json', ['{"value": NaN}', '{"value": Infinity}', '{"value": 1e400}',
                                    '{"value": 1, "value": 2}'])
def test_json_preflight_rejects_nonfinite_and_duplicate_keys(tmp_path, monkeypatch, bad_json):
    pub, private, canon, mapping = tree(tmp_path)
    mapping['z.json'] = 'evidence/z.json'
    put(pub, 'scripts/public_catalogue_map.json', json.dumps(mapping))
    put(canon, 'z.json', bad_json)
    put(pub, 'evidence/z.json', '{"previous": true}')
    tracked = [pub/path for path in (*mapping.values(), 'evidence/PUBLIC_CATALOGUE_MANIFEST.json')]
    before = {p: p.read_bytes() for p in tracked}
    monkeypatch.setattr(B, 'ROOT', pub)
    monkeypatch.setenv('VKM_RESOURCES_ROOT', str(private))
    assert B.main() != 0
    assert {p: p.read_bytes() for p in tracked} == before


def test_concurrent_publication_fails_without_replacing_outputs(tmp_path):
    from vkm_world.governance.publication import publication_lock
    pub, _, _, _ = tree(tmp_path)
    target = pub/'evidence/a.csv'
    before = target.read_bytes()
    code = ('from pathlib import Path; from vkm_world.governance.publication import publish_batch; '
            'import sys; publish_batch(Path(sys.argv[1]), {"evidence/a.csv": b"changed\\n"})')
    env = dict(os.environ, PYTHONPATH=str(ROOT/'src'))
    with publication_lock(pub):
        done = subprocess.run([sys.executable, '-c', code, str(pub)], env=env,
                              capture_output=True, text=True, timeout=10)
    assert done.returncode != 0
    assert target.read_bytes() == before


def test_manifest_public_check_runs_without_private(tmp_path, monkeypatch):
    pub, private, _, _ = tree(tmp_path)
    monkeypatch.setattr(B, 'ROOT', pub)
    monkeypatch.setenv('VKM_RESOURCES_ROOT', str(private))
    assert B.main() == 0
    report = V.verify(pub, groups=['catalogue_sync'], use_env=False)
    checks = {c['id']: c for c in report['checks']}
    assert checks['catalogue_sync:manifest']['status'] == 'PASS'
    assert checks['catalogue_sync:public_vs_private']['status'] == 'SKIPPED'
    assert checks['catalogue_sync:verbatim']['status'] == 'SKIPPED'
    (pub/'evidence/a.csv').write_text('changed public bytes\n')
    report = V.verify(pub, groups=['catalogue_sync'], use_env=False)
    assert report['exit_code'] == 1


@pytest.mark.parametrize('mutation', ['empty', 'omitted', 'duplicate', 'wrong_pair', 'missing_status', 'bad_status'])
def test_manifest_requires_complete_bijection_and_ok_status(tmp_path, mutation):
    pub, private, canon, mapping = tree(tmp_path)
    files = [{'source': source, 'target': target, 'status': 'OK',
              'source_sha256': hashlib.sha256((canon/source).read_bytes()).hexdigest(),
              'target_sha256': hashlib.sha256((pub/target).read_bytes()).hexdigest()}
             for source, target in mapping.items()]
    if mutation == 'empty':
        files = []
    elif mutation == 'omitted':
        files.pop()
    elif mutation == 'duplicate':
        files.append(dict(files[0]))
    elif mutation == 'wrong_pair':
        files[0]['target'], files[1]['target'] = files[1]['target'], files[0]['target']
    elif mutation == 'missing_status':
        files[0].pop('status')
    else:
        files[0]['status'] = 'MISSING'
    put(pub, 'evidence/PUBLIC_CATALOGUE_MANIFEST.json', json.dumps({'files': files}))
    report = V.verify(pub, resources_root=private, groups=['catalogue_sync'], use_env=False)
    checks = {c['id']: c for c in report['checks']}
    assert checks['catalogue_sync:public_vs_private']['status'] == 'FAIL'
    assert report['exit_code'] == 1


def synthetic_quote(n):
    return ' '.join('syntheticword' + chr(97+i) for i in range(n))


def test_verbatim_check_uses_mapping_even_when_manifest_omits_source(tmp_path):
    pub, private, canon, _ = tree(tmp_path)
    quote = synthetic_quote(25)
    put(canon, 'z.csv', 'id,quote\n1,' + quote + '\n')
    put(pub, 'docs/report.md', quote)
    put(pub, 'evidence/PUBLIC_CATALOGUE_MANIFEST.json', '{"files": []}')
    report = V.verify(pub, resources_root=private, groups=['catalogue_sync'], use_env=False)
    checks = {c['id']: c for c in report['checks']}
    assert checks['catalogue_sync:verbatim']['status'] == 'FAIL'


@pytest.mark.parametrize('separator', [' ', '-', '.'])
@pytest.mark.parametrize('count', [24, 25])
def test_generated_csv_rechecks_exact_word_boundary_after_shortening(tmp_path, monkeypatch, separator, count):
    from vkm_world.governance.leakage import longest_shared_run, quote_shingles, words
    pub, private, canon, mapping = tree(tmp_path)
    quote = separator.join(chr(97+i) for i in range(count)) if separator == ' ' else separator.join(
        'syntheticword'+chr(97+i) for i in range(count))
    put(canon, 'z.csv', 'id,summary,quote\n1,' + quote + ',' + quote + '\n')
    monkeypatch.setattr(B, 'ROOT', pub)
    monkeypatch.setenv('VKM_RESOURCES_ROOT', str(private))
    assert B.main() == 0
    text = (pub/mapping['z.csv']).read_text()
    assert longest_shared_run(words(text), quote_shingles([quote]))[0] < 25
    if count == 24:
        assert quote in text


def test_quote_shortening_preserves_exact_decimal_case_and_punctuation_prefix(tmp_path, monkeypatch):
    import csv
    from vkm_world.governance.leakage import longest_shared_run, quote_shingles, words
    pub, private, canon, mapping = tree(tmp_path)
    prefix = '0.03; SYNTHETIC-WORD: '
    quote = prefix + ' '.join(chr(97+i) for i in range(25))
    put(canon, 'z.csv', 'id,summary,quote\n1,' + quote + ',' + quote + '\n')
    monkeypatch.setattr(B, 'ROOT', pub)
    monkeypatch.setenv('VKM_RESOURCES_ROOT', str(private))
    assert B.main() == 0
    with (pub/mapping['z.csv']).open(newline='') as stream:
        row = next(csv.DictReader(stream))
    assert row['summary'].startswith(prefix)
    assert longest_shared_run(words(row['summary']), quote_shingles([quote]))[0] < 25


@pytest.mark.parametrize('separator', [' ', '-', '.'])
def test_quote_shortening_never_exposes_content_past_legacy_word_limit(tmp_path, monkeypatch, separator):
    import csv
    from vkm_world.governance.leakage import longest_shared_run, quote_shingles, words
    pub, private, canon, mapping = tree(tmp_path)
    numeric_prefix = '0.03;  ' + '  '.join(str(i) for i in range(1, 19)) + '  SYNTHETIC-WORD: '
    quote = numeric_prefix + separator.join('syntheticword' + chr(97+i) for i in range(25))
    put(canon, 'z.csv', 'id,summary,quote\n1,' + quote + ',' + quote + '\n')
    monkeypatch.setattr(B, 'ROOT', pub)
    monkeypatch.setenv('VKM_RESOURCES_ROOT', str(private))
    assert B.main() == 0
    with (pub/mapping['z.csv']).open(newline='') as stream:
        row = next(csv.DictReader(stream))
    marker = ' … [сокращено: дословный текст источника — только в PRIVATE]'
    excerpt = row['summary'].removesuffix(marker)
    legacy_boundary = quote.index('SYNTHETIC-WORD:') + len('SYNTHETIC-WORD:')
    assert quote.startswith(excerpt)
    assert len(excerpt) <= legacy_boundary
    assert excerpt.split() == quote.split()[:20]
    assert sum(not token.isdigit() for token in words(excerpt)) <= 20
    assert longest_shared_run(words(row['summary']), quote_shingles([quote]))[0] < 25


def test_verifier_detects_exactly_25_short_alphabetic_words(tmp_path, monkeypatch):
    pub, private, canon, mapping = tree(tmp_path)
    quote = ' '.join(chr(97+i) for i in range(25))
    assert len(quote) == 49
    put(canon, 'z.csv', 'id,quote\n1,' + quote + '\n')
    put(pub, mapping['z.csv'], 'id,summary\n1,' + quote + '\n')
    files = [{'source': source, 'target': target, 'status': 'OK',
              'source_sha256': hashlib.sha256((canon/source).read_bytes()).hexdigest(),
              'target_sha256': hashlib.sha256((pub/target).read_bytes()).hexdigest()}
             for source, target in mapping.items()]
    put(pub, 'evidence/PUBLIC_CATALOGUE_MANIFEST.json', json.dumps({'files': files}))
    report = V.verify(pub, resources_root=private, groups=['catalogue_sync'], use_env=False)
    checks = {c['id']: c for c in report['checks']}
    assert checks['catalogue_sync:manifest']['status'] == 'PASS'
    assert checks['catalogue_sync:public_vs_private']['status'] == 'PASS'
    assert checks['catalogue_sync:verbatim']['status'] == 'FAIL'


@pytest.mark.parametrize('split_words', [False, True])
@pytest.mark.parametrize('target', ['evidence/synthetic.json', 'docs/corpus_platform/receipts/synthetic.json'])
def test_self_consistent_manifest_cannot_hide_quote_in_nested_public_json(tmp_path, monkeypatch, target, split_words):
    pub, private, canon, mapping = tree(tmp_path)
    mapping['summary.json'] = 'evidence/synthetic.json'
    put(pub, 'scripts/public_catalogue_map.json', json.dumps(mapping))
    quote = ' '.join(chr(97+i) for i in range(25))
    put(canon, 'z.csv', 'id,summary,quote\n1,synthetic paraphrase,' + quote + '\n')
    put(canon, 'summary.json', '{"summary": "synthetic paraphrase"}')
    monkeypatch.setattr(B, 'ROOT', pub)
    monkeypatch.setenv('VKM_RESOURCES_ROOT', str(private))
    assert B.main() == 0
    payload = quote.split() if split_words else quote
    public_json = put(pub, target, json.dumps({'unknown_nested': [{'unknown_field': payload}]}))
    if target == mapping['summary.json']:
        manifest = json.loads((pub/'evidence/PUBLIC_CATALOGUE_MANIFEST.json').read_text())
        for entry in manifest['files']:
            if entry['target'] == target:
                entry['target_sha256'] = hashlib.sha256(public_json.read_bytes()).hexdigest()
        put(pub, 'evidence/PUBLIC_CATALOGUE_MANIFEST.json', json.dumps(manifest))
    report = V.verify(pub, resources_root=private, groups=['catalogue_sync'], use_env=False)
    checks = {c['id']: c for c in report['checks']}
    assert checks['catalogue_sync:manifest']['status'] == 'PASS'
    assert checks['catalogue_sync:public_vs_private']['status'] == 'PASS'
    assert checks['catalogue_sync:verbatim']['status'] == 'FAIL'


def test_builder_rejects_quote_split_into_json_list_before_publication(tmp_path, monkeypatch):
    pub, private, canon, mapping = tree(tmp_path)
    mapping['summary.json'] = 'evidence/synthetic.json'
    put(pub, 'scripts/public_catalogue_map.json', json.dumps(mapping))
    quote = ' '.join(chr(97+i) for i in range(25))
    put(canon, 'z.csv', 'id,quote\n1,' + quote + '\n')
    put(canon, 'summary.json', json.dumps({'summary': quote.split()}))
    put(pub, 'evidence/synthetic.json', '{"previous": true}')
    tracked = [pub/path for path in (*mapping.values(), 'evidence/PUBLIC_CATALOGUE_MANIFEST.json')]
    before = {p: p.read_bytes() for p in tracked}
    monkeypatch.setattr(B, 'ROOT', pub)
    monkeypatch.setenv('VKM_RESOURCES_ROOT', str(private))
    assert B.main() != 0
    assert {p: p.read_bytes() for p in tracked} == before


def test_json_bibliography_exclusion_does_not_join_unrelated_quote_fragments(tmp_path, monkeypatch):
    pub, private, canon, mapping = tree(tmp_path)
    mapping['summary.json'] = 'evidence/synthetic.json'
    put(pub, 'scripts/public_catalogue_map.json', json.dumps(mapping))
    before = ['syntheticword' + chr(97+i) for i in range(12)]
    after = ['syntheticword' + chr(109+i) for i in range(12)]
    quote = ' '.join([*before, 'title', *after])
    put(canon, 'z.csv', 'id,quote\n1,' + quote + '\n')
    put(canon, 'summary.json', json.dumps({'summary': [*before, {'title': 'synthetic bibliography'}, *after]}))
    monkeypatch.setattr(B, 'ROOT', pub)
    monkeypatch.setenv('VKM_RESOURCES_ROOT', str(private))
    assert B.main() == 0
    report = V.verify(pub, resources_root=private, groups=['catalogue_sync'], use_env=False)
    assert report['exit_code'] == 0


@pytest.mark.parametrize('failure', ['long_quote', 'exactly_25', 'leakage', 'canonical_quote'])
def test_report_batch_preflight_blocks_leakage_before_any_write(tmp_path, failure):
    pub, private, canon, _ = tree(tmp_path)
    (pub/'src').symlink_to(ROOT/'src', target_is_directory=True)
    syn = tmp_path/'synthesis'
    put(syn, 'SYNTHETIC/first.md', 'Synthetic public paraphrase.\n')
    if failure in ('long_quote', 'exactly_25'):
        text = '«' + synthetic_quote(26 if failure == 'long_quote' else 25) + '»\n'
    elif failure == 'leakage':
        text = '/home/' + 'user/synthetic-secret/\n'
    else:
        text = synthetic_quote(25)
        put(canon, 'a.csv', 'id,quote\n1,' + text + '\n')
    put(syn, 'SYNTHETIC/second.md', text)
    first = put(pub, 'docs/science/first.md', 'previous first\n')
    second = put(pub, 'docs/science/second.md', 'previous second\n')
    before = (first.read_bytes(), second.read_bytes())
    env = dict(os.environ, VKM_PUB=str(pub), VKM_SYNTH_DIR=str(syn), VKM_RESOURCES_ROOT=str(private))
    done = subprocess.run([sys.executable, str(ROOT/'docs/reset_2026_09/run_kit/tools/publish_reports.py'),
                           'SYNTHETIC:first.md', 'SYNTHETIC:second.md'], env=env, capture_output=True, text=True)
    assert done.returncode != 0
    assert (first.read_bytes(), second.read_bytes()) == before
