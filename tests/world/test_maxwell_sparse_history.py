import io

import numpy as np
import pytest
from scipy.sparse import csr_matrix

from vkm_world.representations.world0 import maxwell_coefficients as dense
from vkm_world.representations.world0.maxwell_sparse_history import (
    FixedSupportHistory, SupportedGroup,
)


SIGNATURES = ("V-law-1", "B-law-2")
N = 9


def fixture():
    L1 = np.zeros((2,N))
    L1[:, [0,3,7]] = [[1., 3., -1.], [.4, -.7, .5]]
    L2 = np.zeros((2,N))
    L2[:, [2,7]] = [[.3, -.5], [-.2, .1]]
    operators = [dense.MaxwellGroup(a, csr_matrix(L.T @ L))
                 for a, L in ((.35,L1), (4.9,L2))]
    compact = FixedSupportHistory.from_full_groups("exact-basis", N, SIGNATURES, operators)
    return operators, compact


def compare(operators, compact, initial, q, u0, u1, dt):
    r = dense.ratios(operators, dt)
    np.testing.assert_array_equal(compact.memory_load(initial, dt),
                                  dense.memory_load(operators,q,r))
    q1 = dense.update(q,u1,r)
    candidate = compact.update(initial,u1,dt)
    assert candidate.coefficients.tobytes() == compact.pack(q1).coefficients.tobytes()
    K = csr_matrix(np.eye(N)) + sum(op.stiffness for op in operators)
    observed = compact.work_terms(K,u0,u1,initial,candidate,dt)
    expected = dense.work_terms(K,operators,r,u0,u1,q,q1)
    np.testing.assert_allclose(observed, expected, rtol=2e-14, atol=2e-14)
    np.testing.assert_allclose(sum(observed), (u1-u0) @ (K@u1 - dense.memory_load(
        operators,q1,np.zeros(len(operators)))), rtol=2e-14, atol=2e-14)
    return candidate,q1


def test_fixed_support_force_evolution_and_work_with_many_holes():
    operators, compact = fixture()
    assert [g.addresses.tolist() for g in compact.groups] == [[0,3,7], [2,7]]
    assert compact.virgin().coefficients.nbytes == 5*8
    rng = np.random.default_rng(47)
    q = rng.standard_normal((2,N))
    q[0,0] = -0.
    u = rng.standard_normal(N)
    initial = compact.pack(q)
    for dt in (0., .02, .4, 1.e17):
        v = rng.standard_normal(N)
        candidate,q1 = compare(operators,compact,initial,q,u,v,dt)
        if dt == 0.:
            assert candidate.coefficients.tobytes() == initial.coefficients.tobytes()
        q,u,initial = q1,v,candidate


def test_rock_shrinkage_and_restart_keep_same_support_and_state():
    operators, compact = fixture()
    q = np.arange(2*N,dtype=float).reshape(2,N)/10
    initial = compact.pack(q)
    # Remove the first group's whole cohort. Its addresses remain reserved so
    # pack/restart keep a stable layout across every admitted removal.
    op0 = compact.groups[0]
    removed = SupportedGroup(op0.signature,op0.addresses,dense.MaxwellGroup(
        op0.operator.rate_per_s,csr_matrix(op0.operator.stiffness.shape),
    ))
    stage,state = compact.after_removal(initial,(removed,compact.groups[1]))
    assert stage.layout_sha256 == compact.layout_sha256
    assert stage.operators_sha256 != compact.operators_sha256
    assert state.coefficients.tobytes() == initial.coefficients.tobytes()
    with pytest.raises(ValueError,match="stage"):
        stage.memory_load(initial,1.)
    with pytest.raises(ValueError,match="activate"):
        stage.after_removal(state,compact.groups)
    buffer = io.BytesIO()
    np.savez(buffer, coefficients=state.coefficients, layout=state.layout_sha256, stage=state.operators_sha256)
    buffer.seek(0)
    reborn = FixedSupportHistory("exact-basis",N,(removed,compact.groups[1]))
    with np.load(buffer,allow_pickle=False) as checkpoint:
        reopened = reborn.reopen(checkpoint["coefficients"],checkpoint["layout"].item(),checkpoint["stage"].item())
    assert reopened.coefficients.tobytes() == state.coefficients.tobytes()
    after = [dense.MaxwellGroup(operators[0].rate_per_s,csr_matrix((N,N))),operators[1]]
    u0,u1 = np.arange(N)/17,np.arange(N)[::-1]/23
    compare(after,reborn,reopened,q,u0,u1,.75)


def test_wrong_group_order_basis_rate_and_renewed_stiffness_fail_closed():
    operators, compact = fixture()
    a,b = compact.groups
    state = compact.virgin()
    with pytest.raises(ValueError,match="order"):
        compact.after_removal(state,(b,a))
    with pytest.raises(ValueError,match="order"):
        compact.after_removal(state,(SupportedGroup(a.signature,a.addresses,dense.MaxwellGroup(
            a.operator.rate_per_s*2.,a.operator.stiffness,
        )),b))
    with pytest.raises(ValueError,match="diagonal"):
        compact.after_removal(state,(SupportedGroup(a.signature,a.addresses,dense.MaxwellGroup(
            a.operator.rate_per_s,a.operator.stiffness*1.1,
        )),b))
    with pytest.raises(ValueError,match="addresses"):
        FixedSupportHistory("exact-basis", N, (SupportedGroup(a.signature,a.addresses[::-1],a.operator),))
    with pytest.raises(ValueError,match="stage"):
        FixedSupportHistory("wrong-basis", N, compact.groups).reopen(
            state.coefficients,state.layout_sha256,state.operators_sha256,
        )
    with pytest.raises(ValueError,match="float64"):
        compact.reopen(state.coefficients.astype(np.float32),state.layout_sha256,state.operators_sha256)
    with pytest.raises(ValueError,match="finite"):
        compact.reopen(np.repeat(np.nan,5),state.layout_sha256,state.operators_sha256)
    assert not state.coefficients.flags.writeable
    assert not a.addresses.flags.writeable


def test_empty_group_support_remains_exact():
    zero = dense.MaxwellGroup(.25,csr_matrix((N,N)))
    compact = FixedSupportHistory.from_full_groups("exact-basis",N,("zero",),(zero,))
    assert compact.coefficient_count == 0
    np.testing.assert_array_equal(compact.memory_load(compact.virgin(),2.),np.zeros(N))
    assert compact.update(compact.virgin(),np.arange(N),1.).coefficients.size == 0


def test_readonly_storage_also_rejects_reinterpreted_address_dtype():
    _, compact = fixture()
    state = compact.virgin()
    compact.groups[0].addresses.dtype = np.float32
    with pytest.raises(ValueError, match="address buffer"):
        compact.memory_load(state, .25)
