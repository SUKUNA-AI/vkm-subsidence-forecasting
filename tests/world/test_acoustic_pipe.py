import numpy as np
import pytest
from dataclasses import replace
from vkm_world.representations.world0.acoustic_pipe import Pipe,CV,State,DomainError,advance

def test_standing_wave_convergence_and_energy():
    errors=[]
    for N in (16,32,64):
        m=Pipe(186.33899812498257,.5,viscosity_pa_s=0.,friction_factor=0.,entrance_loss=0.,cells=N)
        x=(np.arange(N)+.5)*m.dx;xf=np.arange(N+1)*m.dx;amplitude=1000.;s=m.initial(amplitude*np.sin(np.pi*x/m.length_m));E=m.energy(s)
        end=.7*m.length_m/m.sound_speed;n=round(end/(.5*m.dx/m.sound_speed));dt=end/n
        for _ in range(n):
            (s,),_,r=advance([m],[s],dt,[0],[0]);assert abs(r['numerical_energy_defect_j'])<1e-10
        w=np.pi*m.sound_speed/m.length_m
        exactp=amplitude*np.sin(np.pi*x/m.length_m)*np.cos(w*end)
        exactq=-amplitude*m.area/(m.density_kg_m3*m.sound_speed)*np.cos(np.pi*xf/m.length_m)*np.sin(w*end)
        err=np.linalg.norm(s.pressure-exactp)/np.linalg.norm(exactp)+np.linalg.norm(s.flow-exactq)/np.linalg.norm(exactq);errors.append(err)
        assert abs(m.energy(s)-E)<1e-10
    assert errors[2]<errors[1]<errors[0] and errors[2]<.005
    assert 3<errors[1]/errors[2]<5

def test_nonlinear_steady_loss_exact():
    m=Pipe(186.33899812498257,.5,cells=24);q=np.full(25,.01);loss=m.loss(q)[0]
    left=1e5+loss.sum();right=1e5;p=left-np.cumsum(loss)[:-1]
    s=m.initial(p,q);(new,),_,r=advance([m],[s],.001,[left],[right])
    np.testing.assert_allclose(new.pressure,p,rtol=1e-13,atol=1e-8);np.testing.assert_allclose(new.flow,q,rtol=1e-12)
    assert r['linear_D_j']>0 and r['quadratic_D_j']>0
    assert abs(r['numerical_energy_defect_j'])<1e-9

def test_passive_ports_do_not_create_energy():
    m=Pipe(10.,.2,cells=12);s=m.initial(np.sin(np.arange(12))*100,flow=.001);E=m.energy(s);D=0.
    for _ in range(20):
        (s,),_,r=advance([m],[s],.0001,[0],[0]);D+=r['linear_D_j']+r['quadratic_D_j']
        assert m.energy(s)<=E+1e-12
    assert abs(m.energy(s)+D-E)<1e-11

def test_moving_cv_discrete_gradient_and_port_cancellation():
    m=Pipe(10.,.2,cells=10);s=m.initial(1e5);cv=CV(.2,.21,1e5)
    new,cvn,r=advance([m,m],[s,s],.0001,[1e5,1e5],cv=cv,new_volume=.200001)
    dM=cvn.mass-cv.mass;dV=cvn.volume_m3-cv.volume_m3;pavg=.5*(cvn.pressure_pa+cv.pressure_pa)
    Pi=pavg+cv.pressure_pa*cvn.pressure_pa/(2*cv.bulk_pa)
    assert abs(cvn.energy-cv.energy-pavg*dM+Pi*dV)<1e-9
    assert abs(r['numerical_energy_defect_j'])<1e-8 and abs(r['mass_balance_error_ref_m3'])<1e-14
    assert r['moving_volume_work_j']<0
    # Mechanical Π must differ from mass potential for nonzero compressibility.
    assert cvn.Pi!=cvn.pressure_pa and cvn.absolute==cvn.reference_absolute_pa+cvn.Pi

def test_restart_immutable_and_foreign_state_rejection():
    m=Pipe(10.,.2,cells=8);s=m.initial(np.arange(8.)*10)
    with pytest.raises(ValueError):s.pressure.setflags(write=True)
    with pytest.raises(ValueError):advance([replace(m,length_m=11.)],[s],.001,[0],[0])
    (a,),_,_=advance([m],[s],.0001,[0],[0]);r=m.initial(a.pressure,a.flow,a.time_s)
    b=advance([m],[a],.0001,[0],[0])[0][0];c=advance([m],[r],.0001,[0],[0])[0][0]
    np.testing.assert_array_equal(b.pressure,c.pressure);np.testing.assert_array_equal(b.flow,c.flow)

def test_guards_reject_without_retuning():
    m=Pipe(10.,.2,cells=8)
    with pytest.raises(DomainError):m.initial(0,flow=m.area*m.sound_speed*.02)
    with pytest.raises(DomainError):m.initial(m.bulk_pa*.02)
    with pytest.raises(DomainError):m.initial(-2.1e6)
    with pytest.raises(DomainError):CV(.2,.2,-2.1e6)
