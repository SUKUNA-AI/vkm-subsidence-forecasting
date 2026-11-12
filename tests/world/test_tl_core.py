import copy
import numpy as np
import pytest
from numpy.testing import assert_allclose
from vkm_world.representations.world0.tl_core import (MANDEL, TetMesh, TLModel, FiniteState, evaluate_material,
                     green, reference_body_force)
from _finite_tl_fixture import laws, cube, stretch_bc, E0, NU, DAY


@pytest.fixture(params=['Nishihara','Lubby2','AbelSOE'])
def law(request):
    return laws()[request.param]


def test_full_piola_tangent_fd(law):
    old = law.initial_state()
    F = np.array([[1.035,.018,.006],[.009,.99,.014],[.003,-.004,1.012]])
    # Establish nonzero reference memory, then differentiate a different endpoint.
    old = law.commit(evaluate_material(law, old, F, DAY).material_trial)
    F = F + np.diag([.012,-.007,.004])
    r = evaluate_material(law,old,F,2*DAY)
    fd = np.empty((3,3,3,3)); h=2e-6
    for k in range(3):
        for L in range(3):
            d=np.zeros((3,3));d[k,L]=h
            fd[:,:,k,L]=(evaluate_material(law,old,F+d,2*DAY).P-
                          evaluate_material(law,old,F-d,2*DAY).P)/(2*h)
    assert np.linalg.norm(fd-r.A)/np.linalg.norm(fd) < 2e-7
    # Geometric contribution is essential, not an optional symmetrization.
    assert np.linalg.norm(fd-r.A_material)/np.linalg.norm(fd) > .005


def test_objectivity_nonzero_memory_and_power(law):
    F=np.array([[1.04,.025,0],[.01,.98,.012],[.004,0,1.01]])
    old=law.commit(evaluate_material(law,law.initial_state(),F,DAY).material_trial)
    F=F+np.diag([.01,-.005,.003])
    theta=.73;Q=np.array([[np.cos(theta),-np.sin(theta),0],
                         [np.sin(theta),np.cos(theta),0],[0,0,1]])
    r=evaluate_material(law,old,F,2*DAY)
    q=evaluate_material(law,old,Q@F,2*DAY)
    assert_allclose(q.E,r.E,atol=2e-16)
    assert_allclose(q.P,Q@r.P,rtol=2e-10,atol=.001)
    assert_allclose(q.sigma,Q@r.sigma@Q.T,rtol=2e-10,atol=.001)
    for key in r.material_trial.state.memory:
        assert_allclose(q.material_trial.state.memory[key],r.material_trial.state.memory[key],atol=1e-12)
    Fdot=np.array([[.2,.1,0],[-.07,.06,.03],[.01,0,-.04]])
    Edot=.5*(F.T@Fdot+Fdot.T@F)
    d=.5*(Fdot@np.linalg.inv(F)+(Fdot@np.linalg.inv(F)).T)
    assert_allclose(np.sum(r.P*Fdot),np.sum(r.S*Edot),rtol=1e-14)
    assert_allclose(np.sum(r.P*Fdot),r.J*np.sum(r.sigma*d),rtol=1e-14)


def test_zero_time_preserves_all_memory(law):
    F=np.diag([1.04,.99,1.])
    old=law.commit(evaluate_material(law,law.initial_state(),F,DAY).material_trial)
    new=evaluate_material(law,old,np.diag([1.07,.98,1.01]),0).material_trial.state
    assert new.time_s == old.time_s
    for key in old.memory:
        assert np.array_equal(new.memory[key],old.memory[key])


def test_rigid_rotation_and_tet_patch(law):
    mesh=cube();model=TLModel(mesh,law);old=model.initial_state()
    Q=np.array([[0,-1,0],[1,0,0],[0,0,1.]])
    u=mesh.nodes@Q.T-mesh.nodes+np.array([.5,-.3,.1])
    force,_,r=model.assemble(old,u,0)
    assert_allclose(force,0,atol=2e-6)
    assert_allclose(r.J,1,atol=5e-16)
    fixed,vals=stretch_bc(mesh,1.12)
    step=model.solve(old,np.zeros_like(u),fixed,vals,0)
    lt=np.sqrt(1-NU*(1.12**2-1))
    exact=mesh.nodes*np.array([.12,lt-1,lt-1])
    assert_allclose(step.state.u,exact,atol=2e-11)
    assert_allclose(step.response.P[:,0,0],1.12*E0*(1.12**2-1)/2,rtol=2e-10)
    assert step.residual_relative < 1e-10


def test_assembled_tangent_and_mass(law):
    mesh=cube();model=TLModel(mesh,law);old=model.initial_state()
    F=np.array([[1.035,.012,0],[.008,.99,.01],[0,.004,1.01]])
    u=mesh.nodes@(F-np.eye(3)).T
    f,K,r=model.assemble(old,u,DAY)
    d=np.random.default_rng(314).normal(size=u.shape);d/=np.linalg.norm(d)
    h=1e-6
    fp=model.assemble(old,u+h*d,DAY)[0]
    fm=model.assemble(old,u-h*d,DAY)[0]
    assert np.linalg.norm((fp-fm).ravel()/(2*h)-K@d.ravel())/np.linalg.norm(K@d.ravel()) < 2e-7
    assert_allclose(f.sum(axis=0),0,atol=1e-7)
    # Internal torque vanishes in the CURRENT configuration.
    assert_allclose(np.cross(mesh.nodes+u,f).sum(axis=0),0,atol=1e-7)
    assert_allclose(np.sum(mesh.density/r.J*(r.J*mesh.volumes)),np.sum(mesh.density*mesh.volumes))
    b=np.array([0,0,-9.81])
    assert_allclose(reference_body_force(mesh,b).sum(axis=0),np.sum(mesh.density*mesh.volumes)*b)


def test_work_identity_and_svk_compression_limit():
    law=laws()['Nishihara'];old=law.initial_state()
    F0=np.diag([.9,1.02,.98]);F1=np.array([[.87,.02,0],[.01,1.03,0],[0,0,.99]])
    r=evaluate_material(law,old,F1,0)
    E0=green(F0)[0];dF=F1-F0
    materialwork=np.sum(r.S*(r.E-E0))
    assert_allclose(np.sum(((F0+F1)/2@r.S)*dF),materialwork,rtol=1e-14)
    endpoint=np.sum(r.P*dF)
    assert_allclose(endpoint-materialwork,.5*np.sum(r.S*(dF.T@dF)),rtol=1e-12)
    assert endpoint-materialwork < 0 # compression: no blanket BE-energy guarantee
    unstable=evaluate_material(law,old,np.diag([.5,1.,1.]),0)
    assert unstable.J > 0 and unstable.A[0,0,0,0] < 0


@pytest.mark.parametrize('stretch',[.9,1.05,1.12])
def test_reference_traction_force_control(stretch):
    model=TLModel(cube(),laws()['Nishihara']);mesh=model.mesh
    # Independent exact plane-strain nominal traction, integrated on two triangles.
    lame=E0*NU/((1+NU)*(1-2*NU));mu=E0/(2*(1+NU))
    traction=(lame+2*mu)*stretch*(stretch**2-1)/2
    ext=np.zeros_like(mesh.nodes)
    ext[[1,2,6,5],0]=traction*np.array([1/3,1/6,1/3,1/6])
    free=3*np.array([1,2,5,6]);fixed=np.setdiff1d(np.arange(24),free)
    step=model.solve(model.initial_state(),ext,fixed,np.zeros(len(fixed)),0)
    exact=np.zeros_like(mesh.nodes);exact[:,0]=(stretch-1)*mesh.nodes[:,0]
    assert_allclose(step.state.u,exact,atol=2e-11)
    assert_allclose(step.reactions[mesh.nodes[:,0]==0,0].sum(),-traction,rtol=1e-10)


def test_virtual_work_and_nonsymmetric_material_tangent():
    law=laws()['Lubby2'];model=TLModel(cube(),law);old=model.initial_state()
    # Non-coaxial prehistory is needed to expose the general algorithmic tangent.
    F0=np.array([[1.035,.035,.01],[0,.985,0],[0,0,1.]])
    u0=model.mesh.nodes@(F0-np.eye(3)).T
    response=model.assemble(old,u0,20*DAY)[2]
    old=FiniteState(u0,law.commit(response.material_trial),model.signature)
    F=np.array([[1.025,-.01,0],[.015,1.005,.02],[.005,0,.98]])
    u=model.mesh.nodes@(F-np.eye(3)).T
    f,K,r=model.assemble(old,u,10*DAY)
    assert np.linalg.norm(K-K.T)/np.linalg.norm(K) > 1e-7
    du=np.random.default_rng(78).normal(size=u.shape)
    dF=np.einsum('eai,eaj->eij',du[model.mesh.tets],model.mesh.grad)
    assert_allclose(np.sum(f*du),np.einsum('e,eij,eij',model.mesh.volumes,r.P,dF),rtol=1e-14)
    h=1e-6
    fd=(model.assemble(old,u+h*du,10*DAY)[0]-model.assemble(old,u-h*du,10*DAY)[0])/(2*h)
    assert np.linalg.norm(fd.ravel()-K@du.ravel())/np.linalg.norm(fd)<2e-7


@pytest.mark.parametrize('F',[np.diag([-1.,1,1]),np.diag([0.,1,1]),np.full((3,3),np.nan)])
def test_reject_invalid_F(F):
    law=laws()['Nishihara']
    with pytest.raises(ValueError):evaluate_material(law,law.initial_state(),F,0)


def test_invalid_mesh_gauge_and_state():
    m=cube();law=laws()['Nishihara'];model=TLModel(m,law);old=model.initial_state()
    with pytest.raises(ValueError):TetMesh(m.nodes,m.tets.astype(float)+.2,m.density)
    with pytest.raises(ValueError):TetMesh(m.nodes,m.tets[:,[1,0,2,3]],m.density)
    with pytest.raises(ValueError):TetMesh(m.nodes,m.tets,np.inf)
    with pytest.raises(ValueError):model.solve(old,np.zeros_like(old.u),np.array([0,1,2]),np.zeros(3),0)
    with pytest.raises(ValueError):model.validate_state(FiniteState(old.u+.01*m.nodes,old.material,model.signature))
    other=TLModel(TetMesh(m.nodes*2,m.tets,m.density),law)
    with pytest.raises(ValueError):other.validate_state(old)


def test_immutable_failure_rollback_and_restart(law):
    model=TLModel(cube(),law);old=model.initial_state();before=old.material.to_dict()
    fixed,vals=stretch_bc(model.mesh,1.12)
    with pytest.raises(RuntimeError):model.solve(old,np.zeros_like(old.u),fixed,vals,DAY,max_iterations=0)
    assert old.material.to_dict()==before
    assert np.array_equal(old.u,np.zeros_like(old.u))
    for a in [old.u,model.mesh.nodes,model.mesh.grad,copy.deepcopy(old).u]:
        with pytest.raises(ValueError):a.setflags(write=True)
    with pytest.raises(AttributeError):model.mesh.nodes=np.zeros_like(old.u)
    s=model.solve(old,np.zeros_like(old.u),*stretch_bc(model.mesh,1.03),DAY).state
    restored=FiniteState(s.u,type(s.material).from_dict(s.material.to_dict()),s.model_signature)
    a=model.solve(s,np.zeros_like(old.u),*stretch_bc(model.mesh,1.04),DAY)
    b=model.solve(restored,np.zeros_like(old.u),*stretch_bc(model.mesh,1.04),DAY)
    assert np.array_equal(a.state.u,b.state.u)
    assert a.state.material.to_dict()==b.state.material.to_dict()
