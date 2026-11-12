import numpy as np
from numpy.testing import assert_allclose
from vkm_world.representations.world0.abel_memory_bounds import prefix_drift

def test_old_tail_plus_new_constant_matches_direct_integral_and_bound():
    t=np.array([0.,1.,3.,3.]);s=np.random.default_rng(319).normal(size=(4,2,6))*1e6
    for branch in ('bulk','dev'):
        bulk=np.zeros_like(s);bulk[...,:3]=s[...,:3].mean(axis=-1)[...,None]
        p=bulk if branch=='bulk' else s-bulk
        a=.6;d=.0002;M=2e9;h=.25;b=1-a
        r=prefix_drift(t,s,h,alpha=a,delta=d,modulus=M,branch=branch)
        previous=d/(M*b)*sum(p[i]*((3-t[i-1])**b-(3-t[i])**b) for i in (1,2))
        future=d/(M*b)*(sum(p[i]*((3+h-t[i-1])**b-(3+h-t[i])**b) for i in (1,2))+p[-1]*h**b)
        assert_allclose(r['held_last_stress_memory_change'],future-previous,rtol=1e-13,atol=1e-20)
        bound=r['old_memory_change_norm']+r['new_stress_norm_coefficient']*np.linalg.norm(p[-1],axis=-1)
        assert np.all(np.linalg.norm(future-previous,axis=-1)<=bound*(1+1e-14))

def test_long_prefix_small_h_stable_and_nonzero():
    s=np.array([[0,0,0,0,0,0],[1e6,-1e6,0,0,0,0]],float)
    r=prefix_drift([0,1e9],s,1.,alpha=.6,delta=2e-4,modulus=1e9,branch='dev')
    b=.4;expected=2e-4/(1e9*b)*s[-1]*(1e9**b*np.expm1(b*np.log1p(1e-9)))
    assert_allclose(r['held_last_stress_memory_change'],expected,rtol=1e-9,atol=1e-24)
