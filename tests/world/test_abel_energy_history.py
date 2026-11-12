import sys
from pathlib import Path
import numpy as np
import pytest
from numpy.testing import assert_allclose
from vkm_world.representations.world0.abel_energy_history import weighted_history_energy
from vkm_world.representations.world0.abel_energy import SpectralEnergyWitness,bulk,dev,direct_history_energy

P=dict(K=10e9,G=6e9,alpha_bulk=.35,delta_bulk=.02,alpha_dev=.65,delta_dev=.03)

def test_all_phase_weighted_gram_against_positive_spectrum_with_cut_event():
    times=np.array([0,1,3,3,6.]);stress=np.random.default_rng(1924).normal(0,1e6,(5,3,6))
    support=np.ones((5,3));support[3:]=[.8,.5,.9];volume=np.array([1.,2.,3.])
    witness=SpectralEnergyWitness(**P,lower=1e-28,upper=1e28,panels=32,order=20)
    state=witness.initial((3,));eps=[];energies=[];Ds=[];tails=[]
    for i in range(5):
        state,d=witness.step(state,stress[i],times[i]-(times[i-1] if i else 0))
        eps.append(bulk(stress[i])/(3*P['K'])+dev(stress[i])/(2*P['G'])+witness.memory_strain(state))
        energies.append(sum(witness.energies(state)));Ds.append(d['physical_dissipation'])
        tails.append(witness.tail_bounds(state))
    r=weighted_history_energy(times,stress,eps,support,volume,**P)
    for i in range(5):
        assert_allclose(r['stored_total_j'][i],np.sum(volume*support[i]*energies[i]),atol=2e-6,rtol=1e-10)
        assert_allclose(r['physical_D_j'][i],np.sum(volume*support[i]*Ds[i]),atol=2e-6,rtol=1e-10)
    assert_allclose(r['removed_stored_total_j'][3],np.sum(volume*(support[2]-support[3])*energies[2]),atol=2e-6)
    assert r['physical_D_j'][3]==0
    assert np.max(abs(r['full_spectrum_balance_error_j']))<1e-9
    assert np.min(r['physical_D_j'])>=0
    assert np.max(abs(r['hybrid_minus_full_work_j']))<2e-6

def test_constant_support_point_sum_equals_independent_closedform_oracle():
    times=np.array([0,1,4,9.]);sigma=np.random.default_rng(22).normal(0,1e6,(4,2,6))
    weights=np.array([2.,7.]);strain=np.zeros_like(sigma)
    oracle=[]
    for j in range(2):
        ref=direct_history_energy(times,sigma[1:,j],**P);oracle.append(ref)
        strain[:,j]=bulk(sigma[:,j])/(3*P['K'])+dev(sigma[:,j])/(2*P['G'])
        strain[1:,j]+=ref['memory_strain']
    r=weighted_history_energy(times,sigma,strain,np.ones((4,2)),weights,**P)
    for i in range(1,4):
        assert_allclose(r['stored_memory_j'][i],sum(weights[j]*oracle[j]['memory_energy'][i-1] for j in range(2)),rtol=2e-13,atol=1e-9)
        assert_allclose(r['physical_D_j'][i],sum(weights[j]*oracle[j]['physical_dissipation'][i-1] for j in range(2)),rtol=2e-13,atol=1e-9)
    assert np.max(abs(r['hybrid_minus_full_work_j']))<1e-9

def test_positive_time_support_change_rejected():
    with pytest.raises(ValueError,match='dt=0'):
        weighted_history_energy([0,1],np.zeros((2,1,6)),np.zeros((2,1,6)),[[1],[.5]],[1],**P)

def test_duplicated_zero_duration_checkpoint_does_not_change_energy():
    s=np.random.default_rng(33).normal(0,1e6,(3,1,6));eps=s/1e10
    a=weighted_history_energy([0,1,2],s,eps,np.ones((3,1)),[1.],**P)
    b=weighted_history_energy([0,1,1,2],s[[0,1,1,2]],eps[[0,1,1,2]],np.ones((4,1)),[1.],**P)
    assert_allclose(a['stored_total_j'],b['stored_total_j'][[0,1,3]],atol=1e-10)
    assert_allclose(a['physical_D_j'],b['physical_D_j'][[0,1,3]],atol=1e-10)

@pytest.mark.parametrize('parameter,value',[('K',float('nan')),('G',float('inf')),('delta_dev',float('nan'))])
def test_nonfinite_material_parameters_rejected(parameter,value):
    params=dict(P);params[parameter]=value
    with pytest.raises(ValueError,match='parameters'):
        weighted_history_energy([0],np.zeros((1,1,6)),np.zeros((1,1,6)),[[1]],[1],**params)
