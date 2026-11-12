import numpy as np
from scipy.integrate import quad
from vkm_world.representations.world0.acoustic_pipe import Pipe,CV
from vkm_world.representations.world0.acoustic_godunov import initial,advance,energy,mass,friction

def test_cfl_one_exact_translating_characteristics():
    m=Pipe(100,.5,cells=64,viscosity_pa_s=0,friction_factor=0,entrance_loss=0)
    p=np.zeros(64);p[15:25]=1000;Z=m.density_kg_m3*m.sound_speed/m.area
    s=initial(m,p,p/Z);E=energy(m,s)
    for j in range(12):
        (s,),_,r=advance([m],[s],m.dx/m.sound_speed,[0],[0])
        assert abs(r['balance_error_j'])<1e-10
    assert np.allclose(s.pressure,np.roll(p,12),atol=1e-11)
    assert abs(energy(m,s)-E)<1e-10

def test_exact_friction_split_against_adaptive_integrals():
    m=Pipe(100,.5,cells=16);q=np.array([-.3,.00001,0.,.5]);h=.11
    a=8*np.pi*m.viscosity_pa_s/(m.density_kg_m3*m.area);b=m.friction_factor/(2*m.diameter_m*m.area)
    I=m.density_kg_m3*m.dx/m.area
    f=lambda t:q*np.exp(-a*t)/(1+b*abs(q)*(-np.expm1(-a*t))/a)
    qn,dl,dq=friction(m,q,h)
    assert np.allclose(qn,f(h),rtol=1e-14)
    expectedL=quad(lambda t:I*a*np.dot(f(t),f(t)),0,h,epsabs=1e-13)[0]
    expectedQ=quad(lambda t:I*b*np.sum(abs(f(t))**3),0,h,epsabs=1e-13)[0]
    assert np.isclose(dl,expectedL,rtol=1e-9)
    assert np.isclose(dq,expectedQ,rtol=1e-11)

def test_moving_cv_power_mass_restart_and_passivity():
    m=Pipe(10,.2,cells=16);s=initial(m,np.linspace(2e5,1e5,16));states=(s,s);cv=CV(.2,.21,1e5)
    E=sum(energy(m,x) for x in states)+cv.energy;M=sum(mass(m,x) for x in states)+cv.mass
    h=.8*m.dx/m.sound_speed
    new,c,r=advance([m,m],states,h,[2e5,2e5],cv=cv,new_volume=.200001)
    assert abs(r['balance_error_j'])<1e-8 and abs(r['mass_balance_error_ref_m3'])<1e-13
    assert r['linear_D_j']>=0 and r['quadratic_D_j']>=0 and r['entrance_D_j']>=0
    restored=tuple(initial(m,x.pressure.copy(),x.flow.copy(),x.time_s) for x in new)
    aa,ac,ar=advance([m,m],new,h,[2e5,2e5],cv=c,new_volume=.200002)
    bb,bc,br=advance([m,m],restored,h,[2e5,2e5],cv=c,new_volume=.200002)
    assert ac==bc and ar==br and all(np.array_equal(x.pressure,y.pressure) for x,y in zip(aa,bb))
    try:new[0].pressure.setflags(write=True)
    except ValueError:pass
    else:assert False

def test_reverse_entrance_energy_and_mass():
    m=Pipe(10,.2,cells=16);s=initial(m,0)
    for _ in range(12):
        (s,),_,r=advance([m],[s],m.dx/m.sound_speed,[0],[1e5])
        assert abs(r['balance_error_j'])<1e-8 and abs(r['mass_balance_error_ref_m3'])<1e-13
        assert r['entrance_D_j']>=0
    assert s.flow.mean()<0


import numpy as np
import pytest
from vkm_world.representations.world0.acoustic_pipe import Pipe,CV,DomainError
from vkm_world.representations.world0.acoustic_godunov import initial,advance,check,CellState

def test_domain_fail_does_not_mutate_accepted_state():
    m=Pipe(10,.5,cells=16);s=initial(m,1e5);old=s.pressure.tobytes()+s.flow.tobytes()
    with pytest.raises(DomainError):advance([m],[s],m.dx/m.sound_speed,[1e9],[0])
    assert old==s.pressure.tobytes()+s.flow.tobytes()
    with pytest.raises(DomainError):initial(m,3e7)
    with pytest.raises(DomainError):initial(m,0,q=m.area*m.sound_speed*.02)

def test_identity_and_cfl_guards():
    m=Pipe(10,.5,cells=16);s=initial(m,1e5)
    with pytest.raises(ValueError):check(Pipe(11,.5,cells=16),s)
    with pytest.raises(ValueError):advance([m],[s],1.1*m.dx/m.sound_speed,[0],[0])
    with pytest.raises(ValueError):check(m,CellState(s.pressure,s.flow[:-1],0,s.signature))

def test_passive_zero_pressure_ports_have_no_heat_alias():
    m=Pipe(10,.5,cells=16,viscosity_pa_s=0,friction_factor=0,entrance_loss=0)
    s=initial(m,1000*np.sin(np.pi*(np.arange(16)+.5)/16))
    for _ in range(100):
        (s,),_,r=advance([m],[s],.7*m.dx/m.sound_speed,[0],[0])
        assert r['linear_D_j']==r['quadratic_D_j']==r['entrance_D_j']==0
        assert r['boundary_work_j']==0
        assert r['algorithmic_energy_j']>=-1e-12
        assert abs(r['balance_error_j'])<1e-10


"""Independent damped-wave oracle; synthetic viscosity, NOT actual retuning."""
import numpy as np
from vkm_world.representations.world0.acoustic_pipe import Pipe
from vkm_world.representations.world0.acoustic_godunov import initial,advance,energy

def test_linear_wave_attenuation_converges_with_physical_heat_separate():
    errors=[]
    for n in [16,32,64]:
        # Strong enough damping to test the coefficient independently of roundoff.
        m=Pipe(100,.5,cells=n,viscosity_pa_s=7.8125,friction_factor=0,entrance_loss=0)
        a=8*np.pi*m.viscosity_pa_s/(m.density_kg_m3*m.area)
        x=(np.arange(n)+.5)*m.dx;k=np.pi/m.length_m;omega=m.sound_speed*k;wd=np.sqrt(omega**2-a**2/4)
        s=initial(m,1000*np.sin(k*x));E0=energy(m,s);D=Q=0.;duration=1.5*m.length_m/m.sound_speed
        steps=3*n//2;dt=duration/steps
        for _ in range(steps):
            (s,),_,r=advance([m],[s],dt,[0],[0]);D+=r['linear_D_j'];Q+=r['algorithmic_energy_j']
            assert r['quadratic_D_j']==r['entrance_D_j']==0
        exactp=1000*np.exp(-a*duration/2)*(np.cos(wd*duration)+a/(2*wd)*np.sin(wd*duration))*np.sin(k*x)
        exactq=-1000*m.area/m.density_kg_m3*k*np.exp(-a*duration/2)*np.sin(wd*duration)/wd*np.cos(k*x)
        # Fixed normalization since this endpoint pressure is near zero.
        errors.append(float(np.sqrt(np.mean((s.pressure-exactp)**2)+np.mean((m.density_kg_m3*m.sound_speed/m.area*(s.flow-exactq))**2))/1000))
        assert D>0 and abs(energy(m,s)+D+Q-E0)<1e-10
    assert errors[2]<errors[1]<errors[0] and errors[2]<.005
    assert 3<errors[1]/errors[2]<5
