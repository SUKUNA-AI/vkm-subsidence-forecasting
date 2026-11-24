"""Payload integrity for both supported manifest contracts."""
from hashlib import sha256
import importlib.util
import json
from pathlib import Path

import pytest

SOURCE = Path(__file__).resolve().parents[2] / 'scripts/verify_release_package.py'
SPEC = importlib.util.spec_from_file_location('verify_release_package', SOURCE)
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


@pytest.mark.parametrize('mapping', [False, True])
def test_payload_contracts_and_corruption(tmp_path, mapping):
    payload = tmp_path / 'sheet.pdf'
    payload.write_bytes(b'%PDF-actual-payload')
    item = {'sha256': sha256(payload.read_bytes()).hexdigest()}
    files = {'sheet.pdf': dict(item, bytes=payload.stat().st_size)} if mapping else [
        dict(item, size=payload.stat().st_size, path='sheet.pdf')]
    manifest = tmp_path / 'MANIFEST.json'
    manifest.write_text(json.dumps({'schema': 'test', 'files': files}), encoding='utf-8')
    assert MODULE.verify(manifest)['files'] == 1
    payload.write_bytes(b'%PDF-actual-payloae')
    with pytest.raises(ValueError, match='hash mismatch'):
        MODULE.verify(manifest)


@pytest.mark.parametrize('name', ['../outside', '/outside', 'C:/outside', 'bad\\file'])
def test_mapping_manifest_rejects_escaping_paths(tmp_path, name):
    manifest = tmp_path / 'MANIFEST.json'
    manifest.write_text(json.dumps({'files': {name: {'bytes': 1, 'sha256': '0'*64}}}))
    with pytest.raises(ValueError, match='non-relative'):
        MODULE.verify(manifest)


def test_conflicting_size_is_rejected(tmp_path):
    (tmp_path / 'file').write_bytes(b'a')
    manifest = tmp_path / 'MANIFEST.json'
    manifest.write_text(json.dumps({'files': {'file': {'size': 1, 'bytes': 2, 'sha256': sha256(b'a').hexdigest()}}}))
    with pytest.raises(ValueError, match='conflicting'):
        MODULE.verify(manifest)
