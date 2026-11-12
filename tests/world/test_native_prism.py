import numpy as np
import pytest
from vkm_world.representations.world0.native_prism import NativePrismElasticMesh,native_prism_element_data,native_prism_quadrature
from vkm_world.representations.world0.multiseam import elastic_matrix,strain_matrix,element_data

def coords():return np.array([[0.,0,0],[2,0,0],[0,3,0],[0,0,4],[2,0,2.5],[0,3,5.]])

def test_affine_field_and_rigid_rotation_on_distorted_prism():
    x=coords();A=np.array([[.002,.001,-.002],[-.003,.004,.001],[.005,-.001,.003]])
    u=(x@A.T+np.array([.1,-.3,.2])).ravel();expected=np.r_[np.diag(A),A[0,1]+A[1,0],A[1,2]+A[2,1],A[0,2]+A[2,0]]
    skew=A-A.T;rigid=(x@skew.T+np.array([.1,-.3,.2])).ravel()
    for grad,shape,wt in native_prism_quadrature():
        B,_=strain_matrix(x[None],grad)
        np.testing.assert_allclose(B[0]@u,expected,atol=2e-16)
        np.testing.assert_allclose(B[0]@rigid,0,atol=2e-16)
        assert abs(shape.sum()-1)<1e-15
        np.testing.assert_allclose(grad.sum(0),0,atol=1e-16)

def test_exact_volume_and_gravity_moment():
    x=coords();mesh=NativePrismElasticMesh(x,np.arange(6)[None],2e9,.25,[0,1,2])
    expected=3*(4+2.5+5)/3;np.testing.assert_allclose(mesh.volume_m3,[expected],rtol=1e-14)
    f=mesh.body_force_rhs(2300,[.2,-.3,-9.80665]).reshape(-1,3)
    np.testing.assert_allclose(f.sum(0),expected*2300*np.array([.2,-.3,-9.80665]),rtol=1e-14)
    # All nodal forces collinear with g; zero projected moment along g is an exact identity.
    g=np.array([.2,-.3,-9.80665]);assert abs(np.cross(x,f).sum(0)@g)<1e-8

def test_six_rigid_modes_no_spurious_zero_modes():
    k= native_prism_element_data(coords()[None],elastic_matrix([2e9],[.25]))[0][0]
    lam=np.linalg.eigvalsh(k);np.testing.assert_allclose(k,k.T,atol=1e-8)
    assert max(abs(lam[:6]))<max(lam)*1e-12
    assert min(lam[6:])>max(lam)*1e-4

def test_affine_prism_full_six_eight_rules_agree_but_distortion_differs():
    x=coords();x[3:,2]=4;D=elastic_matrix([2e9],[.25]);k6=element_data(x[None],D)[0];k8=native_prism_element_data(x[None],D)[0]
    np.testing.assert_allclose(k6,k8,rtol=1e-13,atol=1e-6)
    a=element_data(coords()[None],D)[0];b=native_prism_element_data(coords()[None],D)[0]
    assert np.linalg.norm(a-b)/np.linalg.norm(b)>1e-4

def test_orientation_and_invalid_mass_rejected():
    x=coords();w=np.array([[0,2,1,3,5,4]])
    with pytest.raises(ValueError):NativePrismElasticMesh(x,w,2e9,.25,[0,1,2])
    m=NativePrismElasticMesh(x,np.arange(6)[None],2e9,.25,[0,1,2])
    with pytest.raises(ValueError):m.body_force_rhs(-1,[0,0,-9.8])

def test_force_equilibrium_and_analytic_clapeyron_identity():
    m=NativePrismElasticMesh(coords(),np.arange(6)[None],2e9,.25,[0,1,2]);f=m.body_force_rhs(2300,[0,0,-9.80665]);u=m.solve(f);r=m.matrix@u-f
    np.testing.assert_allclose(r[m.free_dofs],0,atol=1e-9)
    np.testing.assert_allclose(r.reshape(-1,3).sum(0)+f.reshape(-1,3).sum(0),0,atol=1e-9)
    np.testing.assert_allclose(u@(m.matrix@u),u@f,rtol=1e-13)
