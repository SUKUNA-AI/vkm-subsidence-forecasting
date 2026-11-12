import itertools
import numpy as np
import pytest
from vkm_world.representations.world0.coupled_forms import (kelvin_vector,kelvin_tensor,reorder_kelvin,
                           DEFAULT_SHEAR_ORDER,PROJECT_SHEAR_ORDER)

def test_explicit_project_order_matches_tensor_stage_shear_slots():
    a=np.array([[1.,.3,.7],[.3,2.,-.2],[.7,-.2,3.]])
    actual=kelvin_vector(a,shear_order=PROJECT_SHEAR_ORDER)
    expected=np.array([1,2,3,np.sqrt(2)*.3,np.sqrt(2)*(-.2),np.sqrt(2)*.7])
    np.testing.assert_allclose(actual,expected,atol=1e-15)
    default=kelvin_vector(a)
    np.testing.assert_array_equal(default,kelvin_vector(a,shear_order=DEFAULT_SHEAR_ORDER))
    assert np.max(abs(default-actual))>.1

@pytest.mark.parametrize('order',list(itertools.permutations(('xy','yz','xz'))))
def test_mandel_bridge_roundtrip_and_work(order):
    stress=np.array([[2.,1.1,-.3],[1.1,-1.,.7],[-.3,.7,3.]])
    strain=np.array([[.2,.4,-.8],[.4,-.3,.6],[-.8,.6,.1]])
    a=kelvin_vector(stress,shear_order=order)
    b=kelvin_vector(strain,shear_order=order)
    np.testing.assert_allclose(kelvin_tensor(a,shear_order=order),stress,atol=2e-15)
    assert abs(a@b-np.sum(stress*strain))<2e-14
    project_a=reorder_kelvin(a,source_order=order,target_order=PROJECT_SHEAR_ORDER)
    project_b=reorder_kelvin(b,source_order=order,target_order=PROJECT_SHEAR_ORDER)
    np.testing.assert_allclose(project_a,kelvin_vector(stress,shear_order=PROJECT_SHEAR_ORDER),atol=1e-15)
    np.testing.assert_array_equal(reorder_kelvin(project_a,source_order=PROJECT_SHEAR_ORDER,target_order=order),a)
    assert abs(project_a@project_b-a@b)<2e-14

@pytest.mark.parametrize('order',[('xy','xy','yz'),('xy','yz'),('12','23','13'),'xy,yz,xz'])
def test_ambiguous_or_wrong_order_is_rejected(order):
    with pytest.raises(ValueError):kelvin_vector(np.eye(3),shear_order=order)
    with pytest.raises(ValueError):reorder_kelvin(np.ones(6),source_order=order,target_order=PROJECT_SHEAR_ORDER)

def test_mismatched_pair_order_changes_work_and_requires_adapter():
    stress=np.array([[0.,1.,4.],[1.,0.,2.],[4.,2.,0.]])
    rate=np.array([[0.,5.,7.],[5.,0.,3.],[7.,3.,0.]])
    s=kelvin_vector(stress);e=kelvin_vector(rate,shear_order=PROJECT_SHEAR_ORDER)
    assert abs(s@e-np.sum(stress*rate))>1
    aligned=reorder_kelvin(s,source_order=DEFAULT_SHEAR_ORDER,target_order=PROJECT_SHEAR_ORDER)
    assert abs(aligned@e-np.sum(stress*rate))<1e-13

def test_actual_tensorstages_B_engineering_strain_bridge_preserves_work():
    from vkm_world.representations.world0.multiseam import strain_matrix
    from vkm_world.representations.world0.coupled_forms import (engineering_strain_to_kelvin,kelvin_strain_to_engineering,
                               voigt_stress_to_kelvin,kelvin_stress_to_voigt)
    # Affine tetra carrier probes the actual generic B builder's order/factors.
    xyz=np.array([[[0.,0,0],[1,0,0],[0,1,0],[0,0,1]]])
    gradients=np.array([[-1.,-1,-1],[1,0,0],[0,1,0],[0,0,1]])
    gradient_u=np.array([[.1,.4,-.2],[.7,-.3,.6],[.9,-.8,.2]])
    displacement=(xyz[0]@gradient_u.T).ravel()
    b,_=strain_matrix(xyz,gradients)
    engineering=b[0]@displacement
    tensor=.5*(gradient_u+gradient_u.T)
    mandel=engineering_strain_to_kelvin(engineering,source_order=PROJECT_SHEAR_ORDER,target_order=DEFAULT_SHEAR_ORDER)
    np.testing.assert_allclose(mandel,kelvin_vector(tensor),atol=1e-15)
    np.testing.assert_allclose(kelvin_strain_to_engineering(mandel,source_order=DEFAULT_SHEAR_ORDER,target_order=PROJECT_SHEAR_ORDER),engineering,atol=1e-15)
    stress_tensor=np.array([[2.,.4,.7],[.4,3.,-.8],[.7,-.8,1.]])
    stress_mandel=kelvin_vector(stress_tensor)
    physical_stress=kelvin_stress_to_voigt(stress_mandel,source_order=DEFAULT_SHEAR_ORDER,target_order=PROJECT_SHEAR_ORDER)
    np.testing.assert_allclose(voigt_stress_to_kelvin(physical_stress,source_order=PROJECT_SHEAR_ORDER,target_order=DEFAULT_SHEAR_ORDER),stress_mandel,atol=1e-15)
    assert abs(physical_stress@engineering-stress_mandel@mandel)<2e-14
    assert abs(physical_stress@engineering-np.sum(stress_tensor*tensor))<2e-14
