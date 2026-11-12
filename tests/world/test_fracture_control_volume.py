import numpy as np
import pytest
from vkm_world.representations.world0.fracture_control_volume import FractureControlVolume,CavitationDomainError


def model(): return FractureControlVolume(2300.,1e-4,2.2e9,5e-5,2e6,3000.)


@pytest.mark.parametrize('gap',[-3e-4,0.,1e-4,.1])
def test_smooth_positive_volume_and_derivatives(gap):
    f=model(); p=1.1e6; r=f.evaluate(gap,p); h=1e-8
    plus=f.evaluate(gap+h,p); minus=f.evaluate(gap-h,p)
    assert r['volume_m3']>0
    assert (plus['volume_m3']-minus['volume_m3'])/(2*h)==pytest.approx(r['dV_dgap_m2'],rel=1e-7)
    # The second derivative is tiny compared with Vg at large opening; use
    # a scale-aware perturbation to avoid subtractive finite-difference noise.
    h2=max(h,abs(gap)*1e-4)
    second=(f.evaluate(gap+h2,p)['dV_dgap_m2']-f.evaluate(gap-h2,p)['dV_dgap_m2'])/(2*h2)
    assert second==pytest.approx(r['d2V_dgap2_m'],rel=1e-6,abs=1e-6)
    assert (plus['mass_anomaly_ref_m3']-minus['mass_anomaly_ref_m3'])/(2*h)==pytest.approx(r['dmass_dgap_m2'],rel=1e-7)


def test_pressure_work_is_conjugate_to_mass_and_volume():
    f=model(); r=f.evaluate(.001,1.2e6); V=r['volume_m3']; m=r['mass_anomaly_ref_m3']; h=1e-8
    dV=(f.energy_from_mass_volume(m,V+h)-f.energy_from_mass_volume(m,V-h))/(2*h)
    dm=(f.energy_from_mass_volume(m+h,V)-f.energy_from_mass_volume(m-h,V))/(2*h)
    assert dV==pytest.approx(-r['pressure_conjugate_Pa'],rel=1e-7)
    assert dm==pytest.approx(1.2e6,rel=1e-7)


def test_closed_fluid_content_keeps_mass_while_volume_changes():
    f=model(); g=f.reference_gap_m+1e-8; V=f.evaluate(g,0)['volume_m3']; p=f.fluid_bulk_Pa*(f.reference_volume_m3/V-1)
    r=f.evaluate(g,p)
    assert abs(r['mass_anomaly_ref_m3'])<1e-15
    assert r['absolute_pressure_Pa']>f.minimum_absolute_pressure_Pa


def test_cavitation_and_density_domains_reject_without_clipping():
    f=model()
    with pytest.raises(CavitationDomainError): f.evaluate(.001,-2e6)
    with pytest.raises(ValueError): f.evaluate(.001,.02*f.fluid_bulk_Pa)
    with pytest.raises(ValueError): FractureControlVolume(1,0,2e9,0,2e6,3000)


def test_physical_pressure_controls_cavitation_not_mass_potential():
    f=model()
    r=f.evaluate(5e-5,-1997500.)
    assert r['mass_potential_increment_Pa']==-1997500.
    assert r['absolute_mechanical_pressure_Pa']==pytest.approx(3406.8196022727)
    assert r['absolute_pressure_Pa']==r['absolute_mechanical_pressure_Pa']
    assert r['mechanical_pressure_increment_Pa']==r['pressure_conjugate_Pa']
    # The old pressure-like reporting would reject this physically admissible
    # state: p_ref+p=2500 Pa. No clipping or density change is involved.
    assert f.reference_absolute_pressure_Pa+r['mass_potential_increment_Pa']==2500.


def test_cavitation_boundary_in_physical_pressure_coordinates():
    f=model(); K=f.fluid_bulk_Pa
    for absolute, valid in [(2999.,False),(3001.,True),(100000.,True)]:
        Pi=absolute-f.reference_absolute_pressure_Pa
        p=2*Pi/(1+np.sqrt(1+2*Pi/K))
        if valid:
            assert f.evaluate(0.,p)['absolute_pressure_Pa']==pytest.approx(absolute,abs=1e-8)
        else:
            with pytest.raises(CavitationDomainError): f.evaluate(0.,p)


def test_density_pressure_sound_speed_derivative():
    # P(rho) is quadratic, so central differences have no truncation error;
    # a 1 kPa increment avoids subtracting almost equal 1000 kg/m3 densities.
    f=model(); p=1e6; h=1000.
    a=f.evaluate(0.,p-h); b=f.evaluate(0.,p+h)
    dpdrho=(b['absolute_pressure_Pa']-a['absolute_pressure_Pa'])/(b['density_kg_m3']-a['density_kg_m3'])
    expected=f.fluid_bulk_Pa/f.reference_density_kg_m3*(1+p/f.fluid_bulk_Pa)
    assert dpdrho==pytest.approx(expected,rel=1e-7)
