import numpy as np
import pytest
from vkm_world.representations.world0.finite_poromechanics import ReferenceFluid,cofactor,discrete_cofactor
from _finite_hm_fixture import Pair,svk,svk_discrete


def model(V=1.): return ReferenceFluid(V,1e-9*V,.7,2e6,initial_fluid_reference_volume_m3=.4*V)


def test_objectivity_energy_pressure_piola_and_material_tangent():
    rng=np.random.default_rng(2026);m=model()
    for _ in range(20):
        F=np.eye(3)+.08*rng.normal(size=(3,3));Q,_=np.linalg.qr(rng.normal(size=(3,3)))
        if np.linalg.det(Q)<0:Q[:,0]*=-1
        z=m.content(F,2e5);a=m.evaluate(F,z);b=m.evaluate(Q@F,z)
        assert b['energy_J']==pytest.approx(a['energy_J'],rel=2e-12,abs=1e-8)
        assert b['absolute_pressure_Pa']==pytest.approx(a['absolute_pressure_Pa'],rel=2e-12)
        assert np.allclose(b['P'],Q@a['P'],rtol=2e-12,atol=1e-7)
        assert np.allclose(a['P']@F.T/a['J'],-m.alpha*a['absolute_pressure_Pa']*np.eye(3),rtol=1e-12,atol=1e-7)
        transformed=np.einsum('ia,ajbl,kb->ijkl',Q,a['A'],Q)
        assert np.allclose(b['A'],transformed,rtol=2e-12,atol=1e-5)


def test_free_energy_derivatives_and_cross_reciprocity():
    m=model();F=np.array([[1.04,.12,0],[.01,.97,.03],[0,0,1.02]])
    z=m.content(F,3e5);r=m.evaluate(F,z);h=1e-6
    for i in range(3):
        for j in range(3):
            d=np.zeros((3,3));d[i,j]=h
            plus=m.evaluate(F+d,z);minus=m.evaluate(F-d,z)
            grad=(plus['energy_J']-minus['energy_J'])/(2*h)
            assert grad==pytest.approx(m.V0_m3*r['P'][i,j],rel=2e-7,abs=.01)
            fd=(plus['P']-minus['P'])/(2*h)
            assert np.allclose(fd,r['A'][:,:,i,j],rtol=2e-7,atol=.05)
    hz=1e-7
    gradz=(m.evaluate(F,z+hz)['energy_J']-m.evaluate(F,z-hz)['energy_J'])/(2*hz)
    assert gradz==pytest.approx(r['absolute_pressure_Pa'],rel=1e-9)
    assert np.allclose(m.V0_m3*r['dP_dzeta'],r['dp_dF'],rtol=1e-15)
    assert np.allclose(r['A'],r['A'].transpose(2,3,0,1),rtol=1e-14)


def test_mixed_pressure_tangent_holds_pressure_not_mass():
    m=model();F=np.array([[1.1,.2,0],[0,.94,0],[0,0,1.]])
    r=m.mixed(F,1e5);h=1e-6
    for i in range(3):
        for j in range(3):
            d=np.zeros((3,3));d[i,j]=h
            fd=(m.mixed(F+d,1e5)['P']-m.mixed(F-d,1e5)['P'])/(2*h)
            assert np.allclose(fd,r['A'][:,:,i,j],rtol=1e-8,atol=.001)
    assert np.allclose((m.mixed(F,1e5+1)['P']-m.mixed(F,1e5-1)['P'])/2,r['dP_dp'],rtol=1e-8,atol=1e-9)


def test_small_strain_content_and_existing_H_sign_on_tetrahedron():
    V=1/6;m=model(V);grad=np.array([[-1,-1,-1],[1,0,0],[0,1,0],[0,0,1.]])
    u=np.array([[.2,-.1,.3],[.4,.2,-.1],[-.3,.1,.2],[.1,-.2,.3]])
    H=(m.alpha*V*grad).reshape(-1)
    errors=[]
    for eps in [1e-2,1e-3,1e-4]:
        F=np.eye(3)+(eps*u).T@grad
        exact=m.content(F,0);linear=H@(eps*u).reshape(-1)
        errors.append(abs(exact-linear))
    assert errors[0]/errors[1]>95 and errors[1]/errors[2]>95
    # Mechanical pressure load derivative is -H, content derivative +H.T.
    Pdp=m.mixed(np.eye(3),0)['dP_dp']
    fdp=V*np.einsum('ij,aj->ai',Pdp,grad).reshape(-1)
    assert np.allclose(fdp,-H)


def test_absolute_pressure_and_reference_equilibrium_are_explicit():
    pair=Pair();r=pair.state(np.zeros(4),np.zeros(2))
    assert np.array_equal(r['force'],pair.reference_force)
    assert np.array_equal(r['pabs'],np.full(2,2e6))
    assert r['energy']==0 and np.array_equal(r['mass'],np.zeros(2))


def test_discrete_cofactor_and_svk_work_for_general_3D_deformations():
    rng=np.random.default_rng(31)
    for _ in range(20):
        F0=np.eye(3)+.15*rng.normal(size=(3,3));F1=np.eye(3)+.15*rng.normal(size=(3,3))
        C,_=discrete_cofactor(F0,F1)
        assert np.sum(C*(F1-F0))==pytest.approx(np.linalg.det(F1)-np.linalg.det(F0),abs=1e-14)
        W0,W1,P,_=svk_discrete(F0,F1)
        assert np.sum(P*(F1-F0))==pytest.approx(W1-W0,rel=1e-12,abs=1e-8)


@pytest.mark.parametrize('scheme',['DG','BE'])
def test_full_mixed_tangent_independent_finite_difference(scheme):
    pair=Pair();q0=np.array([-.02,.01,.01,-.01]);p0=np.array([2e5,-1e5])
    q=np.array([-.03,.015,.02,-.015]);p=np.array([3e5,-2e5]);force=pair.force(100)
    _,K,_,_=pair.residual(q,p,q0,p0,3.,force,scheme)
    x=np.r_[q,p];sc=np.r_[np.ones(4),np.full(2,1e6)]
    fd=np.empty_like(K)
    for j in range(6):
        h=1e-6*sc[j];plus=x.copy();minus=x.copy();plus[j]+=h;minus[j]-=h
        rp=pair.residual(plus[:4],plus[4:],q0,p0,3.,force,scheme)[0]
        rm=pair.residual(minus[:4],minus[4:],q0,p0,3.,force,scheme)[0]
        fd[:,j]=(rp-rm)/(2*h)
    scale_rows=np.r_[np.full(4,1e6),np.full(2,.1)]
    assert np.max(abs((K-fd)*sc/scale_rows[:,None]))<1e-7


def test_closed_content_and_full_coupled_first_law_one_step():
    for scheme in ['DG','BE']:
        pair=Pair(0.);q0=np.zeros(4);p0=np.zeros(2)
        q,p,r=pair.advance(q0,p0,0.,10.,scheme)
        assert np.max(abs(pair.state(q,p)['mass']))<1e-11
        assert abs(r['corrected_balance_J'])<1e-4
        assert r['Darcy_J']==0
        assert np.array_equal(q0,np.zeros(4)) and np.array_equal(p0,np.zeros(2))


def test_reference_volume_used_once():
    F=np.array([[1.03,.1,0],[0,.99,0],[0,0,1.]])
    a=model(1);b=model(3);ra=a.mixed(F,2e5);rb=b.mixed(F,2e5)
    assert rb['energy_J']==pytest.approx(3*ra['energy_J'])
    assert rb['W']==pytest.approx(ra['W'])
    assert np.allclose(rb['P'],ra['P']) and np.allclose(rb['A'],ra['A'])
    assert rb['zeta_m3']==pytest.approx(3*ra['zeta_m3'])


def test_old_linear_B_fails_finite_constant_pressure_work_control():
    m=model();F0=np.eye(3);F1=np.diag([.8,1.15,1.]);p=1e6
    z0=m.content(F0,p);z1=m.content(F1,p);a=m.evaluate(F0,z0);b=m.evaluate(F1,z1)
    C,_=discrete_cofactor(F0,F1);dF=F1-F0
    fluid_mass_work=a['absolute_pressure_Pa']*(z1-z0)
    true_mechanical_work=-m.alpha*m.V0_m3*a['absolute_pressure_Pa']*np.sum(C*dF)
    old_linear_mechanical_work=-m.alpha*m.V0_m3*a['absolute_pressure_Pa']*np.trace(dF)
    assert abs(true_mechanical_work+fluid_mass_work-(b['energy_J']-a['energy_J']))<1e-8
    assert abs(old_linear_mechanical_work+fluid_mass_work-(b['energy_J']-a['energy_J']))>60000


def test_negative_inputs_and_no_pressure_clipping():
    m=model()
    with pytest.raises(ValueError):cofactor(np.diag([-1,1,1]))
    with pytest.raises(ValueError):cofactor(np.diag([0,1,1]))
    with pytest.raises(ValueError):m.mixed(np.eye(3),-2e6)
    with pytest.raises(ValueError):m.evaluate(np.eye(3),-1.)
    with pytest.raises(ValueError):ReferenceFluid(1,-1,.7,2e6)
    with pytest.raises(ValueError):ReferenceFluid(1,1e-9,1.1,2e6)
    with pytest.raises(ValueError):Pair(-1)
    # Endpoints AND midpoint are positive, but det is negative for 1/5<s<1/3.
    with pytest.raises(ValueError,match='path crosses'):
        discrete_cofactor(np.eye(3),np.diag([-4.,-2.,1.]))


def test_restart_changes_and_corruption_are_rejected():
    import copy
    from _finite_hm_fixture import run
    _,_,c=run(20)
    bad=copy.deepcopy(c);bad['p'][0]+=1
    with pytest.raises(ValueError,match='corruption'):run(20,restart=bad)
    with pytest.raises(ValueError,match='incompatible'):run(40,restart=c)
    with pytest.raises(ValueError,match='incompatible'):run(20,conductance=0.,restart=c)


def test_geometric_pore_and_solid_volume_domain():
    # Reviewer counterexample: initial phi=.8 is allowed, but compression
    # J=.3 with alpha=.5 predicts pore=.45 > total=.3: reject, never clamp.
    m=ReferenceFluid(1.,1e-9,.5,2e6,initial_fluid_reference_volume_m3=.8)
    for J in [.3,.6]:
        with pytest.raises(ValueError,match='solid volume'):
            m.mixed(np.diag([J,1.,1.]),0.)
    r=m.mixed(np.diag([.61,1.,1.]),0.)
    assert r['pore_capacity_m3']==pytest.approx(.605)
    assert r['solid_volume_m3']==pytest.approx(.005)
    assert 0<r['porosity']<1
    with pytest.raises(ValueError,match='initial geometric'):
        ReferenceFluid(1.,1e-9,.5,2e6,initial_fluid_reference_volume_m3=1.)
    with pytest.raises(ValueError,match='initial geometric'):
        ReferenceFluid(1.,1e-9,.5,2e6,reference_J=.5,initial_fluid_reference_volume_m3=.6)
    # Alpha=1 preserves a finite incompressible solid inventory.
    a=ReferenceFluid(1.,1e-9,1.,2e6)
    for J in [.71,1.,1.5]:
        r=a.mixed(np.diag([J,1.,1.]),0.)
        assert r['solid_volume_m3']==pytest.approx(.7)
        assert 0<r['porosity']<1
    with pytest.raises(ValueError,match='pore capacity'):
        a.evaluate(np.diag([.6,1.,1.]),0.)
