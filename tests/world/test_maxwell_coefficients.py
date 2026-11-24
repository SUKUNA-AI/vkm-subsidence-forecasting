import numpy as np
import pytest
from scipy.sparse import csr_matrix

from vkm_world.representations.world0.constitutive import DeviatoricMaxwell, H, P
from vkm_world.representations.world0.maxwell_coefficients import (
    MaxwellGroup, memory_load, ratios, tangent, update, work_terms,
)


def test_arbitrary_history_matches_point_law_and_endpoint_energy():
    # Identity sampling in Mandel space gives a direct point-law comparison.
    bulk, shear, eta = 4.e9, 2.e9, 8.e13
    point = DeviatoricMaxwell(bulk, shear, eta)
    elastic = csr_matrix(3*bulk*H+2*shear*P)
    group = MaxwellGroup(shear/eta, csr_matrix(2*shear*P))
    s = point.initial_state()
    q = np.zeros((1,6))
    path = ([.002,.001,-.004,0.,.003,-.001],
            [.004,.002,-.003,.001,0.,.003],
            [.006,-.002,-.004,.001,-.001,.002])
    for dt, values in zip((3600.,180.,0.), path, strict=True):
        u = np.asarray(values)
        r = ratios([group], dt)
        new = update(q,u,r)
        expected = point.trial(s,u,dt) if dt else point.instantaneous(s,u)
        force = tangent(elastic,[group],r)@u - memory_load([group],q,r)
        np.testing.assert_allclose(force,expected.stress,atol=1e-7,rtol=1e-12)
        np.testing.assert_allclose(P@new[0],expected.state.memory["creep"],atol=1e-15)
        dE,diss,excess=work_terms(elastic,[group],r,s.strain,u,q,new)
        np.testing.assert_allclose(dE+diss+excess,(u-s.strain)@force,atol=1e-8)
        assert diss>=0. and excess>=0.
        if not dt:
            assert new.tobytes()==q.tobytes()
        s,q=expected.state,new


def test_state_fails_closed_for_bad_dimensions_and_negative_time():
    with pytest.raises(ValueError,match="nonnegative"):
        ratios([], -1.)
    with pytest.raises(ValueError,match="exact groups"):
        update(np.zeros((1,4)),np.zeros(3),np.ones(1))
    with pytest.raises(ValueError,match="finite Maxwell state"):
        update(np.zeros((1,3)),np.array([np.nan,0.,0.]),np.ones(1))



@pytest.mark.parametrize("dt", [True, np.bool_(False), [1.], np.array(1.), 1j, np.inf])
def test_time_increment_requires_a_finite_real_scalar(dt):
    with pytest.raises(ValueError, match="Maxwell time increment"):
        ratios([MaxwellGroup(1., csr_matrix([[2.]]))], dt)


@pytest.mark.parametrize("rate", [True, np.bool_(True), [1.], np.array(1.), 1j, np.nan])
def test_rate_requires_a_positive_finite_real_scalar(rate):
    with pytest.raises(ValueError, match="Maxwell rate"):
        MaxwellGroup(rate, csr_matrix([[2.]]))


@pytest.mark.parametrize("failure", ["duplicate", "unsorted", "out_of_range", "truncated_pointer"])
def test_csr_validation_reads_actual_arrays_even_if_canonical_flag_is_cached(failure):
    matrix = csr_matrix([[2., 1.], [0., 2.]])
    assert matrix.has_canonical_format
    if failure == "duplicate":
        matrix.indices[1] = matrix.indices[0]
    elif failure == "unsorted":
        matrix.indices[:2] = matrix.indices[:2][::-1]
    elif failure == "out_of_range":
        matrix.indices[-1] = 4
    else:
        matrix.indptr[-1] -= 1
    assert matrix.has_canonical_format
    with pytest.raises(ValueError, match="CSR"):
        MaxwellGroup(1., matrix)
    with pytest.raises(ValueError, match="CSR"):
        tangent(matrix, [], np.empty(0))


def test_group_owns_a_readonly_snapshot_and_rejects_buffer_replacement():
    source = csr_matrix([[3.]])
    group = MaxwellGroup(1., source)
    source.data[0] = 19.
    r = ratios([group], 1.)
    assert memory_load([group], np.ones((1, 1)), r).item() == 1.5
    with pytest.raises(ValueError):
        group.stiffness.data.setflags(write=True)
    group.stiffness.data.dtype = np.int64
    with pytest.raises(ValueError, match="CSR.*changed"):
        memory_load([group], np.ones((1, 1)), r)
    group.stiffness.data.dtype = np.float64
    group.stiffness.data = np.array([7.])
    with pytest.raises(ValueError, match="CSR.*changed"):
        memory_load([group], np.ones((1, 1)), r)


@pytest.mark.parametrize("bad", [[-0.25], [np.nan], [np.inf], [1j], [True], [[0.25]]])
def test_all_consumers_reject_invalid_direct_ratios(bad):
    elastic = csr_matrix([[4.]])
    groups = [MaxwellGroup(1., csr_matrix([[2.]]))]
    u = np.zeros(1)
    q = np.zeros((1, 1))
    r = np.asarray(bad)
    for compute in (
        lambda: tangent(elastic, groups, r),
        lambda: memory_load(groups, q, r),
        lambda: update(q, u, r),
        lambda: work_terms(elastic, groups, r, u, u, q, q),
    ):
        with pytest.raises(ValueError, match="ratios"):
            compute()


def test_operator_ratios_keep_same_length_group_order_and_values_bound():
    elastic = csr_matrix([[5.]])
    groups = [MaxwellGroup(1., csr_matrix([[2.]])), MaxwellGroup(2., csr_matrix([[1.]]))]
    r = ratios(groups, 1.)
    q = np.ones((2, 1))
    u = np.zeros(1)
    for compute in (
        lambda: tangent(elastic, groups[::-1], r),
        lambda: memory_load(groups[::-1], q, r),
        lambda: work_terms(elastic, groups[::-1], r, u, u, q, q),
    ):
        with pytest.raises(ValueError, match="group identity or ordering"):
            compute()
    with pytest.raises(ValueError, match="ratios"):
        update(q, u, r[::-1])
    with pytest.raises(ValueError, match="ratios"):
        update(q, u, r.view(np.int64))


def test_state_and_operator_shapes_are_not_broadcast():
    elastic = csr_matrix(np.eye(2))
    group = MaxwellGroup(1., csr_matrix([[1.]]))
    r = ratios([group], 1.)
    with pytest.raises(ValueError, match="basis"):
        tangent(elastic, [group], r)
    with pytest.raises(ValueError, match="basis"):
        memory_load([group], np.zeros((1, 2)), r)
    with pytest.raises(ValueError, match="displacement"):
        update(np.zeros((1, 2)), np.zeros((1, 2)), r)
    with pytest.raises(ValueError, match="displacement"):
        work_terms(elastic, [], np.empty(0), np.zeros((1, 2)), np.zeros(2), np.zeros((0, 2)), np.zeros((0, 2)))


def test_instantaneous_event_preserves_signed_zero_and_extreme_finite_memory():
    q = np.array([[-0., np.finfo(float).max]])
    with np.errstate(over="raise", invalid="raise"):
        trial = update(q, np.array([+0., -np.finfo(float).max]), np.zeros(1))
    assert trial.tobytes() == q.tobytes()


def test_two_partial_groups_match_direct_constitutive_and_work_equations():
    metrics = tuple(np.asarray(weights) for weights in (
        [[1., 0.], [.5, 1.]],
        [[1., -.25], [0., .7]],
    ))
    S = [metric.T @ metric for metric in metrics]
    K = np.diag([4., 5.]) + sum(S)
    elastic = csr_matrix(K)
    groups = [MaxwellGroup(rate, csr_matrix(stiffness))
              for rate, stiffness in zip((0.13, 2.), S, strict=True)]
    lag = np.array([[.4, -.1], [0., .8]])
    u0 = np.array([.15, -.7])
    point_memory = [metric @ old for metric, old in zip(metrics, lag, strict=True)]
    for dt, u1 in ((0.4, np.array([.7, .1])), (0.2, np.array([-.15, .2])),
                   (0., np.array([.6, -.4]))):
        r = ratios(groups, dt)
        trial = update(lag, u1, r)
        traction = K @ u1
        change = .5 * (u1 @ K @ u1 - u0 @ K @ u0)
        dissipation = 0.
        for i, (metric, old, ratio) in enumerate(zip(metrics, point_memory, r, strict=True)):
            qualified = (old + ratio * (metric @ u1)) / (1. + ratio)
            np.testing.assert_allclose(metric @ trial[i], qualified, rtol=1e-14, atol=1e-14)
            traction -= metric.T @ qualified
            change += .5 * (qualified @ qualified - old @ old)
            change -= (metric @ u1) @ qualified - (metric @ u0) @ old
            if ratio:
                increment = qualified - old
                dissipation += (increment @ increment) / ratio
            point_memory[i] = qualified
        np.testing.assert_allclose(tangent(elastic, groups, r) @ u1 - memory_load(groups, lag, r), traction)
        stored, loss, quadrature = work_terms(elastic, groups, r, u0, u1, lag, trial)
        np.testing.assert_allclose([stored, loss], [change, dissipation], atol=1e-14)
        np.testing.assert_allclose((u1-u0) @ traction, stored + loss + quadrature, atol=1e-14)
        u0, lag = u1, trial



def test_large_ratio_retains_finite_old_memory_and_tangent_stiffness():
    group = MaxwellGroup(1.e17, csr_matrix([[3.]]))
    r = ratios([group], 1.)
    expected = np.array([[2.]])
    np.testing.assert_allclose(update(np.array([[1.e17]]), np.array([1.]), r), expected)
    np.testing.assert_allclose(tangent(group.stiffness, [group], r).data,
                               [3.e-17], rtol=1.e-14, atol=0.)
