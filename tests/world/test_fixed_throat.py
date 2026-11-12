import math
import pytest
from vkm_world.representations.world0.fixed_throat import FixedThroat


def throat(**kw):
    args = dict(length_m=186.339, diameter_m=.5, density_kg_m3=1000., dynamic_viscosity_Pa_s=.001)
    args.update(kw)
    return FixedThroat(**args)


def test_inventory_and_energy():
    t = throat()
    assert t.volume_m3 == pytest.approx(math.pi*.5**2/4*186.339)
    assert t.fluid_mass_kg == pytest.approx(t.volume_m3*1000)
    for q in [-20., -1., 0., 1., 20.]:
        assert t.energy(q) == pytest.approx(.5*t.fluid_mass_kg*(q/t.area_m2)**2)
        assert t.dissipation_power(q) >= 0
        assert t.pressure_loss(-q) == -t.pressure_loss(q)
        assert t.energy(-q) == t.energy(q)


def test_tangent():
    t = throat()
    for q in [-5., -.1, 0., .1, 5.]:
        h = 1e-7
        fd = (t.pressure_loss(q+h)-t.pressure_loss(q-h))/(2*h)
        assert fd == pytest.approx(t.pressure_loss_derivative(q), rel=1e-4)


def test_backward_euler_first_law_and_reversal():
    t = throat()
    for q0 in [-5., 0., 5.]:
        for dp in [-1e7, 0., 1e7]:
            for dt in [.01, 1., 1000.]:
                q = t.backward_euler_step(q0, dp, dt)
                work = dt*dp*q
                de = t.energy(q)-t.energy(q0)
                heat = dt*t.dissipation_power(q)
                numerical = .5*t.inertance*(q-q0)**2
                assert abs(work-de-heat-numerical) <= 5e-13*max(1.,abs(work),abs(de),heat,numerical)
                assert q == -t.backward_euler_step(-q0,-dp,dt)


def test_linear_transient_convergence():
    t = throat(darcy_friction_factor=0., minor_loss_coefficient=0.)
    tau = t.inertance/t.linear_resistance
    dp = 100.
    exact = dp/t.linear_resistance*(1-math.exp(-1))
    errors = []
    for n in [50,100,200]:
        q = 0.
        for _ in range(n):
            q = t.backward_euler_step(q,dp,tau/n)
        errors.append(abs(q-exact))
    assert errors[0]/errors[1] > 1.98
    assert errors[1]/errors[2] > 1.99


@pytest.mark.parametrize('key,value', [('length_m',0.),('diameter_m',-1.),('density_kg_m3',float('nan')),('dynamic_viscosity_Pa_s',0.),('darcy_friction_factor',-1.),('minor_loss_coefficient',float('inf')),('kinetic_shape_factor',0.),('kinetic_shape_factor',.5)])
def test_invalid_parameters(key,value):
    with pytest.raises(ValueError):
        throat(**{key:value})


def test_restart_input_signature():
    assert throat().signature == throat().signature
    assert throat().signature != throat(diameter_m=.51).signature
    assert throat().signature != throat(kinetic_shape_factor=2.).signature
    with pytest.raises(ValueError):
        throat().backward_euler_step(0.,1.,0.)
    with pytest.raises(ValueError):
        throat().pressure_loss(float('nan'))
