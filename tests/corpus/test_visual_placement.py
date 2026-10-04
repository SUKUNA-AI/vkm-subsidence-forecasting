"""Allocation metadata, provenance and strict CUDA profile; no model values."""
from copy import deepcopy
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest

from vkm_corpus.update.visual_placement import (
    OwnedTargetWeightPlacement, TargetWeightPlacement, TargetWeightTensor, require_cuda_weight_profile)
from vkm_corpus.update import visual_owner_bridge as vb
from vkm_corpus.update.visual_owner_bridge import LoadedWitness, UPSTREAM
from vkm_evidence.contracts import canonical_bytes


def tensor(name="blk.0.weight", *, index=0, gpu=True, host=False):
    base=10000+index*1000
    group=name.rsplit('.',1)[0] if name.startswith("blk.") else "output" if name.startswith("output") else "other"
    return dict(name=name,group=group,aliases=[],tensor=100+index,storage_tensor=100+index,data=base,storage_data=base,
        buffer=200+index,base=base,buft=300+index,device=400+int(gpu),tensor_bytes=64,
        storage_bytes=64,buffer_bytes=128,host=host,device_kind="GPU" if gpu else "CPU",
        device_name="CUDA0" if gpu else "CPU",buffer_name="CUDA0" if gpu else "CPU",
        view_chain=[100+index],view_offsets=[],tensor_type=0,shape=[16,1,1,1])


def placement(values=None):
    if values is None: values=[tensor()]
    return TargetWeightPlacement.model_validate(dict(schema_version="vkm-native-target-weight-placement/1",
        scope="LLAMA_TARGET_WEIGHT_PLACEMENT",model_handle=1,context_handle=2,no_alloc=False,
        layer_count=28,total_layer_count=28,tensors=sorted(values,key=lambda value:(value["name"],value["tensor"]))))


def test_actual_buffer_identity_distinguishes_cuda_host_and_cpu():
    cuda=placement(); assert cuda.summary()["groups"]=={"blk.0":"GPU"}
    host=tensor(host=True);host["buffer_name"]="CUDA_Host"
    result=placement([host]).summary()
    assert result["groups"]=={"blk.0":"HOST"} and result["gpu_buffer_bytes"]==0
    cpu=placement([tensor(gpu=False,host=True)])
    assert cpu.summary()["host_buffer_bytes"]==128
    for key in ("mmproj","context_buffers","kernel_execution","full_gpu_residency","performance"):
        assert cuda.summary()[key]=="NOT_PROVEN"


@pytest.mark.parametrize("field,value",[("tensor",0),("data",0),("buffer",0),("device",0),("buffer_bytes",0),
    ("tensor_bytes",129),("data",10100),("base",2**64-16),("device_kind","META"),
    ("buffer_name","CUDA_Split"),("buffer_name","meta"),("tensor",True),("host",1),
    ("tensor_bytes",64.0),("shape",[16,0,1,1]),("view_chain",[100,100]),
    ("storage_tensor",101),("view_offsets",[0])])
def test_invalid_allocations_are_rejected(field,value):
    data=tensor();data[field]=value
    with pytest.raises(ValueError):TargetWeightTensor.model_validate(data)


def test_bounded_views_require_real_storage_span_and_offsets():
    data=tensor();data.update(storage_tensor=101,view_chain=[100,101],view_offsets=[16],
        storage_bytes=128,data=10016)
    valid=TargetWeightTensor.model_validate(data);assert valid.data==valid.storage_data+16
    for update in ({"view_offsets":[32]},{"view_chain":[100,101,100],"view_offsets":[0,0]},
            {"storage_bytes":64},{"view_offsets":[2**64-1]}):
        with pytest.raises(ValueError):TargetWeightTensor.model_validate(data|update)


def test_dedup_buffer_counts_and_group_mixed_domain():
    gpu=tensor("blk.0.a");shared=deepcopy(gpu)
    shared.update(name="blk.0.b",tensor=101,storage_tensor=101,view_chain=[101],data=10064,storage_data=10064)
    assert placement([gpu,shared]).summary()["gpu_buffer_bytes"]==128
    cpu=tensor("blk.0.c",index=2,gpu=False,host=True)
    assert placement([gpu,cpu]).summary()["groups"]=={"blk.0":"MIXED"}
    with pytest.raises(ValueError):placement([gpu,shared|{"device":999}])
    with pytest.raises(ValueError):placement([gpu,gpu])


def test_fingerprint_binds_count_preserving_buffer_tensor_and_view_changes():
    before=placement([tensor("blk.0.a"),tensor("blk.1.a",index=1)])
    assert before.sha256==placement([tensor("blk.1.a",index=1),tensor("blk.0.a")]).sha256
    swapped=[tensor("blk.1.a"),tensor("blk.0.a",index=1)]
    after=placement(swapped)
    assert after.summary()==before.summary() and after.sha256!=before.sha256
    same=before.model_dump();same["tensors"][0]["device"]=999
    assert TargetWeightPlacement.model_validate(same).sha256!=before.sha256
    data=tensor();data.update(storage_tensor=999,view_chain=[100,999],view_offsets=[0])
    view=placement([data])
    assert view.summary()==placement().summary() and view.sha256!=placement().sha256


@pytest.mark.parametrize("field,value",[("no_alloc",True),("no_alloc",0),("layer_count",True),
    ("layer_count",0),("model_handle",0),("scope","GPU_RESIDENCY")])
def test_unallocated_or_wrong_scope_never_qualifies(field,value):
    data=placement().model_dump();data[field]=value
    with pytest.raises(ValueError):TargetWeightPlacement.model_validate(data)


def test_approved_workload_requires_actual_twenty_cuda_groups():
    values=[tensor(f"blk.{i}.weight",index=i,gpu=i>=9,host=i<9) for i in range(28)]
    values.append(tensor("output_norm.weight",index=29))
    values.append(tensor("token_embd.weight",index=30,gpu=False,host=True))
    baseline=placement(values)
    assert require_cuda_weight_profile(baseline)["gpu_buffer_bytes"]==20*128
    # CUDA-associated pinned host allocation must not satisfy the GPU gate.
    host=deepcopy(values);host[9]["host"]=True;host[9]["buffer_name"]="CUDA_Host"
    wrong=deepcopy(values);wrong[8]=tensor("blk.8.weight",index=8);wrong[27]=tensor("blk.27.weight",index=27,gpu=False,host=True)
    first_twenty=[tensor(f"blk.{i}.weight",index=i,gpu=i<20,host=i>=20) for i in range(28)]
    first_twenty.extend([tensor("output_norm.weight",index=29,gpu=False,host=True),values[-1]])
    missing_host=[value for value in values if value["group"]!="blk.0"]
    for changed in (host,values[:-2],values[:-1],missing_host,wrong,first_twenty,
            values+[tensor("blk.9.other",index=31,gpu=False,host=True)]):
        with pytest.raises(ValueError):require_cuda_weight_profile(placement(changed))
    data=baseline.model_dump();data["layer_count"]=29
    with pytest.raises(ValueError):require_cuda_weight_profile(TargetWeightPlacement.model_validate(data))
    data=baseline.model_dump();data["total_layer_count"]=29
    with pytest.raises(ValueError):require_cuda_weight_profile(TargetWeightPlacement.model_validate(data))
    cpu=placement([tensor("blk.0.weight",gpu=False,host=True)])
    with pytest.raises(ValueError):require_cuda_weight_profile(cpu)


def test_tied_output_name_and_same_pointer_alias_preserve_all_occurrences():
    input_tensor=tensor("token_embd.weight",gpu=False,host=True)
    output=tensor("token_embd.weight",index=1);output["group"]="output"
    recorded=placement([input_tensor,output])
    assert recorded.summary()["groups"]=={"other":"HOST","output":"GPU"}
    assert recorded.summary()["tensor_count"]==2
    shared=tensor("token_embd.weight");shared.update(group="output",aliases=["INPUT","OUTPUT"])
    alias=placement([shared]);assert alias.summary()["groups"]=={"other":"GPU","output":"GPU"}
    with pytest.raises(ValueError):require_cuda_weight_profile(alias)
    with pytest.raises(ValueError):placement([input_tensor,input_tensor|{"group":"output"}])
    with pytest.raises(ValueError):placement([shared|{"aliases":["OUTPUT","INPUT"]}])
    changed=output|{"aliases":["INPUT","OUTPUT"]}
    assert placement([changed]).sha256!=placement([output]).sha256


def test_native_lifetime_binding_and_outward_summary_have_no_raw_handles():
    witness=dict(schema_version="vkm-native-loaded-witness/1",upstream_commit=UPSTREAM,nonce="a"*64,
        epoch=1,capture_before_load=True,handles=dict(model=1,context=2,mmproj=3,vocab=4),
        paths=dict(weights=str(Path.cwd()/"weights"),tokenizer=str(Path.cwd()/"weights"),mmproj=str(Path.cwd()/"mmproj")),
        target_weight_placement=placement().model_dump())
    loaded=LoadedWitness.model_validate(witness)
    assert loaded.target_weight_placement.sha256==placement().sha256
    wrong=deepcopy(witness);wrong["target_weight_placement"]["context_handle"]=99
    with pytest.raises(ValueError):LoadedWitness.model_validate(wrong)
    proof=OwnedTargetWeightPlacement(instance_sha256="a"*64,process_sha256="b"*64,
        witness_sha256="c"*64,placement_sha256=placement().sha256,observation=placement().summary())
    raw=canonical_bytes(proof)
    assert b'"tensor"' not in raw and b'"data"' not in raw and b"blk.0.weight" not in raw
    assert b"NOT_PROVEN" in raw


def test_production_missing_auxiliary_observation_is_rejected_before_baseline(monkeypatch):
    """Protocol branch unit test, not an injected production owner qualification."""
    resources=dict(weights=str(Path.cwd()/"weights"),tokenizer=str(Path.cwd()/"weights"),mmproj=str(Path.cwd()/"mmproj"))
    @contextmanager
    def stream(method,path,*,params,timeout):
        response=dict(schema_version="vkm-native-loaded-witness/1",upstream_commit=UPSTREAM,
            nonce=params["nonce"],epoch=1,capture_before_load=True,
            handles=dict(model=1,context=2,mmproj=3,vocab=4),paths=resources)
        yield SimpleNamespace(status_code=200,headers={},iter_raw=lambda:iter([canonical_bytes(response)]))
    owner=vb.OwnedHookChildOwner.__new__(vb.OwnedHookChildOwner)
    owner.recipe=SimpleNamespace(scope="VISUAL_OWNER_PRODUCTION",executable="/not-executed",
        implementation_files=(),resources=resources)
    owner.pid=123;owner.client=SimpleNamespace(stream=stream);owner._witness=None
    monkeypatch.setattr(vb,"_mapped_implementation",lambda pid:{"/not-executed"})
    with pytest.raises(ValueError,match="actual target-weight placement unavailable"):
        owner._loaded_resource_fence()
    assert owner._witness is None
