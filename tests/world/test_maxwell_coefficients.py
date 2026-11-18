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
