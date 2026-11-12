"""A checkpoint must bind actual stiffness ordering and all physical dependencies."""
from copy import deepcopy
import json
import numpy as np
import pytest
from scipy.sparse import csr_matrix
from vkm_world.representations.world0.binding import (
    representation_snapshot,assert_applicable,write_checkpoint_binding,restore_bound,
)


def setup():
    matrix=csr_matrix([[3.,-1.,0.],[-1.,4.,-1.],[0.,-1.,2.]])
    fields={k:{"digest":"same"} for k in ("geometry","materials","topology","boundary","partition","laws")}
    return matrix,np.array([0,1,2]),fields


@pytest.mark.parametrize("dependency",["geometry","materials","topology","boundary","partition","laws"])
def test_changed_physical_dependency_rejected_before_restore(tmp_path,dependency):
    matrix,free,fields=setup()
    state=tmp_path/"state.bin";state.write_bytes(b"accepted-memory")
    original=representation_snapshot(matrix,free,fields)
    sidecar=tmp_path/"binding.json"
    write_checkpoint_binding(sidecar,state,original,{"kind":"executed"})
    changed=deepcopy(fields);changed[dependency]["digest"]="changed"
    with pytest.raises(ValueError,match="stale representation"):
        restore_bound(state,sidecar,representation_snapshot(matrix,free,changed),
                      lambda _:pytest.fail("stale context reached restore"))


def test_actual_restore_and_byte_and_matrix_guards(tmp_path):
    matrix,free,fields=setup()
    state=tmp_path/"state.bin";state.write_bytes(b"accepted-memory")
    original=representation_snapshot(matrix,free,fields)
    sidecar=tmp_path/"binding.json"
    write_checkpoint_binding(sidecar,state,original,{"kind":"executed"})
    assert restore_bound(state,sidecar,original,lambda p:p.read_bytes())==b"accepted-memory"
    with pytest.raises(ValueError,match="stale representation"):
        restore_bound(state,sidecar,representation_snapshot(matrix*1.01,free,fields),lambda _:None)
    with pytest.raises(ValueError,match="stale representation"):
        restore_bound(state,sidecar,representation_snapshot(matrix,free[::-1],fields),lambda _:None)
    state.write_bytes(b"different-memory")
    with pytest.raises(ValueError,match="checkpoint bytes"):
        restore_bound(state,sidecar,original,lambda _:None)


def test_required_dependencies_and_signature_integrity():
    matrix,free,fields=setup()
    with pytest.raises(ValueError,match="required physical"):
        representation_snapshot(matrix,free,{"geometry":"only-one"})
    with pytest.raises(ValueError,match="ordering"):
        representation_snapshot(matrix,[0.,1.5,2.],fields)
    original=representation_snapshot(matrix,free,fields)
    damaged=deepcopy(original);damaged["components"]["laws"]["digest"]="tampered"
    with pytest.raises(ValueError,match="corrupt representation"):
        assert_applicable(damaged,original)
