"""Offline non-editable wheel packaging and synthetic service startup.

Uses the existing build backend and installer, installs only the local wheel to
a temporary target, and forbids API/GPU imports. No network or model workloads.
"""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys


PROBE = r'''
import importlib.abc, json, os, sys
from pathlib import Path
assert 'PYTHONPATH' not in os.environ
target = Path(sys.argv[1]).resolve()
sys.path.insert(0, str(target))
class Forbidden(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if any(fullname == x or fullname.startswith(x + '.') for x in
               ('vkm_corpus.api', 'duckdb', 'mcp', 'torch', 'transformers', 'safetensors')):
            raise ImportError('forbidden unrelated API/model runtime import: ' + fullname)
sys.meta_path.insert(0, Forbidden())
import vkm_corpus, vkm_evidence
assert Path(vkm_corpus.__file__).resolve().is_relative_to(target)
assert Path(vkm_evidence.__file__).resolve().is_relative_to(target)
from vkm_corpus.update.service_identity import *
from vkm_corpus.update.service_identity import _source_path
inventories = {p: {'code': code_inventory(p), 'dependencies': dependency_inventory(p)}
               for p in (RERANK, RETRIEVAL)}
for value in inventories.values():
    assert all(_source_path(module).resolve().is_relative_to(target) for module in value['code']['modules'])
from vkm_corpus.retrieval.gateway import GatewayConfig, load_resources, create_app
from vkm_corpus.retrieval_service.app import create_app as retrieval_app
from vkm_corpus.retrieval import pins
from vkm_corpus.update.remote_models import NativeModelProof
from fastapi.testclient import TestClient
from tokenizers import Tokenizer, models
import hashlib, httpx, numpy as np
from types import SimpleNamespace
# This test qualifies packaging/startup integration only on every host. Actual
# Linux mutation/process proofs have their own environment-accounted tests.
from vkm_corpus.update import native_files, remote_models, remote_rerank
native_files.optional_watch = lambda paths: SimpleNamespace(check=lambda: None, close=lambda: None)
remote_rerank.own_process = lambda: {'pid': 1, 'start_ticks': 2}
root = Path.cwd()
tok = root / 'synthetic-tokenizer.json'
Tokenizer(models.WordLevel({'[UNK]': 0, 'synthetic': 1}, unk_token='[UNK]')).save(str(tok))
head = root / 'synthetic-head.npz'
np.savez_compressed(head, W1=np.zeros((1536,1536), dtype=np.float32), b1=np.zeros(1536),
                    W2=np.zeros(1536), b2=np.zeros(1), logit_bias=np.array([2.65]))
sha = lambda p: hashlib.sha256(p.read_bytes()).hexdigest()
cfg = GatewayConfig(token='synthetic', warmup=False, v35_tokenizer=str(tok),
    v35_tokenizer_sha256=sha(tok), m0_tokenizer=str(tok), m0_tokenizer_sha256=sha(tok),
    m0_head=str(head), m0_head_sha256=sha(head), text_url='http://text.test', visual_url='http://visual.test')
# Compatibility startup uses the real resource loaders but no inference/health calls.
resources = load_resources(cfg)
with TestClient(create_app(cfg, resources=resources)) as client:
    response = client.get('/identity', headers={'X-VKM-Rerank-Token':'synthetic'})
    assert response.status_code == 503, response.text
# Retrieval compatibility startup does not demand API libraries or optional slots.
with TestClient(retrieval_app(encoders={}, start_residency=False)) as client:
    response = client.get('/health')
    assert response.status_code == 503 and response.json()['status'] == 'down'
    assert response.json()['ready'] is True  # startup succeeded; no models were loaded
from dataclasses import replace
cfg = replace(cfg, qualified_identity=True, text_native_token='nt', visual_native_token='nv')
resources = load_resources(cfg)
calls = []
def respond(request):
    assert request.method == 'GET' and request.url.path == '/identity'
    kind = 'text' if request.url.host == 'text.test' else 'visual'
    assert request.headers['Authorization'] == 'Bearer ' + ('nt' if kind == 'text' else 'nv')
    calls.append(kind)
    values = {'weights': pins.TEXT['weights_sha256'] if kind=='text' else cfg.m0_weights_sha256,
              'tokenizer': sha(tok)}
    if kind == 'visual': values['mmproj'] = cfg.m0_mmproj_sha256
    proof = NativeModelProof(kind=kind, instance_sha256='a'*64, code_sha256='b'*64,
        dependencies_sha256='c'*64, config_sha256='d'*64, resources=values)
    return httpx.Response(200, json=proof.model_dump(mode='json'))
resources.text._client = httpx.AsyncClient(base_url=cfg.text_url, transport=httpx.MockTransport(respond))
resources.visual._client = httpx.AsyncClient(base_url=cfg.visual_url, transport=httpx.MockTransport(respond))
with TestClient(create_app(cfg, resources=resources)) as client:
    response = client.get('/identity', headers={'X-VKM-Rerank-Token':'synthetic'})
    assert response.status_code == 200, response.text
    assert response.json()['functional_qualification'] == 'NOT_RUN'
assert calls == ['text','visual','text','visual']
print(json.dumps({'scope':'SYNTHETIC_ONLY', 'installed_from':str(target),
    'qualified_gateway':'SYNTHETIC_STARTUP_PASS', 'native_filesystem_qualification':'NOT_RUN',
    'module_counts':{p:len(v['code']['modules']) for p,v in inventories.items()},
    'dependency_counts':{p:len(v['dependencies']['packages']) for p,v in inventories.items()}}))
'''


def test_installed_wheel_narrow_services_start_without_pythonpath(tmp_path):
    repo = Path(__file__).resolve().parents[2]
    source, wheels, target = (tmp_path / p for p in ("build-source", "wheels", "installed"))
    source.mkdir(); wheels.mkdir()
    shutil.copy2(repo / "pyproject.toml", source / "pyproject.toml")
    shutil.copytree(repo / "src", source / "src", ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "*.egg-info"))
    env = dict(os.environ)
    env.pop("PYTHONPATH", None)
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    def run(args, cwd):
        result = subprocess.run(args, cwd=cwd, env=env, capture_output=True, text=True,
                                encoding="utf-8", errors="replace", timeout=120)
        assert result.returncode == 0, result.stdout[-8000:] + result.stderr[-8000:]
        return result.stdout
    run([sys.executable, "-I", "-c", "import setuptools.build_meta as b,sys; b.build_wheel(sys.argv[1])", str(wheels)], source)
    wheel, = wheels.glob("*.whl")
    uv = shutil.which("uv")
    if uv:
        run([uv, "pip", "install", "--offline", "--no-deps", "--no-index", "--target", str(target), str(wheel)], tmp_path)
    else:
        # No network/bootstrap fallback: an existing pip is required if uv is absent.
        run([sys.executable, "-I", "-m", "pip", "install", "--no-deps", "--no-index",
             "--target", str(target), str(wheel)], tmp_path)
    output = run([sys.executable, "-I", "-c", PROBE, str(target)], tmp_path)
    result = json.loads(output.strip().splitlines()[-1])
    assert result["scope"] == "SYNTHETIC_ONLY"
    assert all(n > 20 for n in result["module_counts"].values())
    assert all(n > 20 for n in result["dependency_counts"].values())
    import hashlib
    result["wheel_sha256"] = hashlib.sha256(wheel.read_bytes()).hexdigest()
    result["wheel_bytes"] = wheel.stat().st_size
    result["wheel_file"] = wheel.name
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
