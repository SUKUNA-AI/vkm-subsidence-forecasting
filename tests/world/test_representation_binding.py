"""A checkpoint must bind actual stiffness ordering and all physical dependencies."""
from copy import deepcopy
import json
import numpy as np
import pytest
from scipy.sparse import csr_matrix
from vkm_world.representations.world0.binding import (
    representation_snapshot,assert_applicable,write_checkpoint_binding,restore_bound,
    file_digest, physical_history_materials, verify_native_history_state,
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


def history_inputs(tmp_path):
    field = tmp_path / "selected.npz"
    carrier = tmp_path / "carrier.npz"
    names = ["floor", "contact", "roof"]
    bodies = ["BODY:floor:contact", "BODY:contact:roof"]
    np.savez(field, geometry_id="C1-accepted", bhj_id="BHJ-accepted", horizon_names=names)
    pin = {"geometry_id": "C1-accepted", "bhj_id": "BHJ-accepted",
           "source_field": {"sha256": file_digest(field)}}
    np.savez(carrier, geometry_id=pin["geometry_id"], source_field_sha256=file_digest(field),
             horizon_names=names, body_ids=bodies)
    receipt = {"geometry_id": pin["geometry_id"], "source_field_sha256": file_digest(field),
               "representation_sha256": file_digest(carrier), "geometry_screen_pass": True}
    cards = [{"body_id": name, "E_pa": float(i)} for i, name in enumerate(bodies, start=1)]
    return field, carrier, pin, receipt, cards


def test_current_history_body_binding_is_by_name(tmp_path):
    field, carrier, pin, receipt, cards = history_inputs(tmp_path)
    assert physical_history_materials(field, carrier, pin, receipt, cards[::-1]) == cards
    with pytest.raises(ValueError, match="named material"):
        physical_history_materials(field, carrier, pin, receipt, [1., 2.])
    with pytest.raises(ValueError, match="duplicate"):
        physical_history_materials(field, carrier, pin, receipt, [cards[0], cards[0]])
    with pytest.raises(ValueError, match="body IDs mismatch"):
        physical_history_materials(field, carrier, pin, receipt, [{"body_id": "foreign"}, cards[1]])


def test_current_history_checks_raw_field_and_carrier_identity(tmp_path):
    field, carrier, pin, receipt, cards = history_inputs(tmp_path)
    with pytest.raises(ValueError, match="different C1"):
        physical_history_materials(field, carrier, pin, {**receipt, "geometry_id": "C1-legacy"}, cards)
    field.write_bytes(field.read_bytes() + b"changed")
    with pytest.raises(ValueError, match="field bytes"):
        physical_history_materials(field, carrier, pin, receipt, cards)


def test_current_history_rejects_foreign_checkpoint_even_with_correct_hash(tmp_path):
    path = tmp_path / "pre.npz"
    np.savez(path, signature="same-world", time_year=1939.5)
    digest = file_digest(path)
    verify_native_history_state(path, digest, "same-world", 1939.5)
    with pytest.raises(ValueError, match="foreign"):
        verify_native_history_state(path, digest, "old-world", 1939.5)
    with pytest.raises(ValueError, match="foreign"):
        verify_native_history_state(path, digest, "same-world", 2025.0)
    with pytest.raises(ValueError, match="bytes mismatch"):
        verify_native_history_state(path, "wrong-bytes", "same-world", 1939.5)
