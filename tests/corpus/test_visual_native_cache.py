"""Bounded synthetic cache filesystem; no image build, GPU or model load."""
from __future__ import annotations

import hashlib
import importlib.util
import json
import os
from pathlib import Path

import pytest

_PATH = Path(__file__).resolve().parents[2] / "infra/edge/visual-owner/verify_native_cache.py"
_SPEC = importlib.util.spec_from_file_location("native_cache_test", _PATH)
cache = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(cache)


def sha(raw): return hashlib.sha256(raw).hexdigest()


def write(root,name,raw):
    path=cache.image_path(root,name);path.parent.mkdir(parents=True,exist_ok=True);path.write_bytes(raw)
    return sha(raw)


@pytest.fixture
def fixture(tmp_path,monkeypatch):
    root=tmp_path/"synthetic-cache-root";root.mkdir()
    before={name:write(root,name,raw) for name,raw in {
        "/src/tools/server/server-context.cpp":b"SYNTHETIC original cpp\n",
        "/src/tools/server/vkm-loaded-lifetime.h":b"SYNTHETIC original header\n",
        "/src/README.md":b"SYNTHETIC unchanged source\n"}.items()}
    candidate={**before,"/src/tools/server/server-context.cpp":sha(b"SYNTHETIC candidate cpp\n"),
        "/src/tools/server/vkm-loaded-lifetime.h":sha(b"SYNTHETIC candidate header\n"),
        "/src/tools/server/vkm-weight-placement.h":sha(b"SYNTHETIC allocation header\n")}
    files={"/src/build/CMakeCache.txt":write(root,"/src/build/CMakeCache.txt",
        ''.join(name+':STRING='+value+'\n' for name,value in cache.FLAGS.items()).encode()),
        "/src/build/compile_commands.json":write(root,"/src/build/compile_commands.json",b'[]'),
        "/out/llama-server":write(root,"/out/llama-server",b'SYNTHETIC NONEXECUTED ELF'),
        "/out/libggml-cuda.so":write(root,"/out/libggml-cuda.so",b'SYNTHETIC NONEXECUTED CUDA ELF'),
        "/usr/lib/dependency.so.1":write(root,"/usr/lib/dependency.so.1",b'SYNTHETIC dependency bytes')}
    plan=dict(schema_version='vkm-native-cache-plan/1',status='PINNED_CACHE_DELTA_NOT_BUILT_NOT_MODEL_QUALIFIED',
        cache_image_id=cache.CACHE_IMAGE,upstream_commit=cache.UPSTREAM,source_archive_sha256=cache.ARCHIVE,
        base_images=dict(cache.BASES),source_files=before,candidate_source_files=candidate,files=files,
        links={},cmake_flags=dict(cache.FLAGS),driver_dependency='libcuda.so.1:RUNTIME_NOT_PROVEN',
        compiler_version='c++ (Ubuntu 13.3.0-6ubuntu2~24.04.1) 13.3.0',
        cuda_compiler_version='Build cuda_13.2.r13.2/compiler.37668154_0',
        cold_inputs_sha256='391d564d3e824d51747694ff13d89f4be18a2edf0e77aaf3a7447e4b09af5106')
    # Explicit tiny metadata-only fixture; production constant remains 3648.
    monkeypatch.setattr(cache,'SOURCE_COUNT',len(before))
    monkeypatch.setattr(cache,'EXPECTED_FILES',set(files))
    monkeypatch.setattr(cache,'EXPECTED_LINKS',{})
    return root,plan,tmp_path/'plan.json'


def verify(fixture,*,phase="before"):
    root,plan,path=fixture;raw=json.dumps(plan,sort_keys=True).encode();path.write_bytes(raw)
    return cache.verify(root,path,sha(raw),phase=phase)


def test_complete_before_cache_and_exact_three_source_delta(fixture):
    root,plan,_=fixture
    baseline=verify(fixture);assert baseline['source_files']==3 and baseline['gpu_model_execution']=='NOT_RUN'
    for name,raw in {"/src/tools/server/server-context.cpp":b"SYNTHETIC candidate cpp\n",
            "/src/tools/server/vkm-loaded-lifetime.h":b"SYNTHETIC candidate header\n",
            "/src/tools/server/vkm-weight-placement.h":b"SYNTHETIC allocation header\n"}.items():write(root,name,raw)
    assert verify(fixture,phase='after-overlay')['source_files']==4
    with pytest.raises(ValueError):verify(fixture,phase='before')


@pytest.mark.parametrize("key,value",[("cache_image_id","sha256:"+"f"*64),('upstream_commit','main'),
    ('source_archive_sha256','f'*64),('status','READY'),('cold_inputs_sha256','f'*64),
    ('compiler_version','GCC 14'),('driver_dependency','GPU_PROVEN')])
def test_immutable_cache_provenance_is_required_before_reading_files(fixture,key,value):
    verify(fixture);fixture[1][key]=value
    with pytest.raises(ValueError,match='provenance'):verify(fixture)


@pytest.mark.parametrize("kind",["source","dependency","cuda","untracked","same_count_delta","missing_host_config","missing_dependency"])
def test_actual_byte_and_complete_delta_guards(fixture,kind):
    root,plan,_=fixture;verify(fixture)
    if kind=='source':write(root,'/src/README.md',b'TAMPERED')
    elif kind=='dependency':write(root,'/usr/lib/dependency.so.1',b'TAMPERED')
    elif kind=='cuda':write(root,'/out/libggml-cuda.so',b'TAMPERED')
    elif kind=='untracked':write(root,'/src/extra.h',b'TAMPERED include')
    elif kind=='same_count_delta':
        plan['candidate_source_files']['/src/README.md']='f'*64
        plan['candidate_source_files']['/src/tools/server/vkm-loaded-lifetime.h']=plan['source_files']['/src/tools/server/vkm-loaded-lifetime.h']
    elif kind=='missing_dependency':del plan['files']['/usr/lib/dependency.so.1']
    else:del plan['files']['/src/build/compile_commands.json']
    with pytest.raises(ValueError):verify(fixture)


def test_actual_cache_flags_cannot_be_declared_over(fixture):
    root,plan,_=fixture;verify(fixture)
    path=cache.image_path(root,'/src/build/CMakeCache.txt');raw=path.read_bytes().replace(b'GGML_CUDA:STRING=ON',b'GGML_CUDA:STRING=OFF')
    path.write_bytes(raw);plan['files']['/src/build/CMakeCache.txt']=sha(raw)
    with pytest.raises(ValueError,match='actual native cached'):verify(fixture)


def test_plan_exact_hash_and_duplicate_json_precede_source_access(fixture):
    root,_,path=fixture;raw=b'{"schema_version":1,"schema_version":2}';path.write_bytes(raw)
    with pytest.raises(ValueError,match='approved'):cache.verify(root,path,'f'*64)
    with pytest.raises(ValueError,match='duplicate'):cache.verify(root,path,sha(raw))


def test_cache_hashes_ignore_restored_mtime_and_reject_hardlink(fixture):
    root,_,_=fixture;verify(fixture);path=cache.image_path(root,'/out/libggml-cuda.so');before=path.stat()
    raw=path.read_bytes();path.write_bytes(b'x'*len(raw));os.utime(path,ns=(before.st_atime_ns,before.st_mtime_ns))
    with pytest.raises(ValueError):verify(fixture)
    path.write_bytes(raw);os.link(path,path.with_name('alias'))
    with pytest.raises(ValueError,match='ordinary'):verify(fixture)


@pytest.mark.parametrize("name",['/src/../secret','/models/value','relative','/src/x\\y','/src/x\0y'])
def test_cache_paths_are_bound_to_image_software_roots(tmp_path,name):
    with pytest.raises(ValueError):cache.image_path(tmp_path,name)
