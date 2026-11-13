import json,sys
import numpy as np,pytest
from vkm_world.representations.world0.wedge_adapter import *
from vkm_world.representations.world0.constitutive import Lubby2
from vkm_world.representations.world0.multiseam import ElasticMesh

def mesh(two=True, fraction=1.):
 x=np.array([[0.,0,z] for z in [0,1,2] for _ in [0]])
 x=np.array([[a,b,z] for z in [0.,1.,2.] for a,b in [(0.,0.),(1.,0.),(0.,1.)]])
 cells=np.array([[0,1,2,3,4,5],[3,4,5,6,7,8]])
 if not two:x=x[:6];cells=cells[:1]
 return WedgeMesh(x,cells,2400.,fraction)
def build(two=True,mixed=True,fraction=1.):
 w=mesh(two,fraction);K=1.25e9;G=576923076.9230769;elastic=ReferenceElastic(K,G)
 if mixed and two:
  lb=ReferentialLubby(Lubby2(K,G,.7*G,.7*G*63115200.,G*473364000.,mK=-2e-8,mM=-3e-8,mG=0.))
  law=GroupedLaw((Group(np.arange(6),elastic),Group(np.arange(6,9),lb),Group(np.arange(9,12),DeviatoricMaxwell(K,G,G*473364000.))),(2,6))
 else:law=GroupedLaw((Group(np.arange(len(w.cells)*6),elastic),),(len(w.cells),6))
 return WedgeModel(w,law)
def affine(m,F):return m.mesh.nodes@(F-np.eye(3)).T
def saved(s):return (s.u.tobytes(),json.dumps(s.material.to_dict(),sort_keys=True),s.model_signature)
def loadedF():return np.array([[1.01,.006,.003],[.002,.997,.004],[.001,.002,1.005]])

@pytest.mark.parametrize('two',[False,True])
def test_constantF_mass_and_partition(two):
 m=build(two,False);w=m.mesh;F=loadedF();got=w.deformation_gradient(affine(m,F))
 np.testing.assert_allclose(got,np.broadcast_to(F,got.shape),rtol=0,atol=3e-13)
 np.testing.assert_allclose(w.grad.sum(axis=2),0,atol=3e-13)
 assert abs(w.weights.sum()-(1. if two else .5))<3e-13
 a=np.array([.1,-.2,-9.80665]);f=w.body_force(a);expected=w.weights.sum()*2400*a
 np.testing.assert_allclose(f.sum(axis=0),expected,rtol=3e-13,atol=1e-10)

@pytest.mark.parametrize('mixed',[False,True])
def test_full_assembled_tangent_and_nonsymmetry(mixed):
 m=build(True,mixed);old=m.initial_state()
 if mixed:
  preF=np.array([[1.01,.012,0.],[0.,.996,.004],[0.,0.,1.002]]);preu=affine(m,preF);pre=m.assemble(old,preu,1e7)[2];old=FiniteState(preu,m.law.commit(pre.material_trial),m.signature)
 u=affine(m,loadedF());f,K,r=m.assemble(old,u,1e6);fd=np.empty_like(K);h=1e-6
 for j in range(u.size):
  d=np.zeros_like(u);d.flat[j]=h;fd[:,j]=((m.assemble(old,u+d,1e6)[0]-m.assemble(old,u-d,1e6)[0])/(2*h)).ravel()
 print('FD_relative',mixed,float(np.linalg.norm(fd-K)/np.linalg.norm(fd)),'nonsym_relative',float(np.linalg.norm(K-K.T)/np.linalg.norm(K)))
 assert np.linalg.norm(fd-K)/np.linalg.norm(fd)<2e-7
 if mixed:assert np.linalg.norm(K-K.T)/np.linalg.norm(K)>1e-7

@pytest.mark.parametrize('mixed',[False,True])
def test_rotation_force_moment_and_matrix(mixed):
 m=build(True,mixed);old=m.initial_state();u=affine(m,loadedF());f,K,r=m.assemble(old,u,1e6);Q,_=np.linalg.qr(np.random.default_rng(771).normal(size=(3,3)));Q[:,0]*=np.linalg.det(Q)
 v=(m.mesh.nodes+u)@Q.T-m.mesh.nodes;ff,KK,rr=m.assemble(old,v,1e6)
 np.testing.assert_allclose(rr.P,np.einsum('ij,egjk->egik',Q,r.P),rtol=3e-13,atol=1e-6)
 T=np.kron(np.eye(len(u)),Q);assert np.linalg.norm(KK-T@K@T.T)/np.linalg.norm(K)<3e-12
 scale=max(1,np.linalg.norm(f));assert np.linalg.norm(f.sum(axis=0))/scale<3e-12
 assert np.linalg.norm(np.cross(m.mesh.nodes+u,f).sum(axis=0))/scale<3e-12
 # Shared reference triangular face carries opposite tractions under constant P.
 if not mixed:
  Pface=r.P[0,0];a=m.mesh.nodes[[3,4,5]];area=.5*np.cross(a[1]-a[0],a[2]-a[0]);out=Pface@area;other=Pface@(-area)
  assert np.array_equal(out,-other)

@pytest.mark.parametrize('two',[False,True])
def test_linear_limit_matches_existing_WEDGE_operator(two):
 m=build(two,False);old=m.initial_state();_,K,_=m.assemble(old,old.u,0)
 Kb=m.law.groups[0].law.K;G=m.law.groups[0].law.G;E=9*Kb*G/(3*Kb+G);nu=(3*Kb-2*G)/(2*(3*Kb+G))
 legacy=ElasticMesh(m.mesh.nodes,m.mesh.cells,E,nu,np.array([0,1,2]));ref=legacy.matrix.toarray()
 assert np.linalg.norm(K-ref)/np.linalg.norm(ref)<3e-13

def test_existing_Newton_reactions_energy_restart_and_rollback():
 m=build();old=m.initial_state();fixed=np.arange(9);vals=np.zeros(9);force=m.mesh.body_force([0,0,-9.80665]);force[-1,0]+=4000.
 init=m.solve(old,force,fixed,vals,0,rtol=1e-10);m.validate_state(init.state)
 assert all(np.array_equal(init.state.material.memory[k],old.material.memory[k]) for k in old.material.memory)
 s=m.solve(init.state,force,fixed,vals,1e6,rtol=1e-10)
 assert s.residual_relative<=1e-10
 total=force+s.reactions;scale=max(1,np.linalg.norm(force));assert np.linalg.norm(total.sum(axis=0))/scale<3e-12
 assert np.linalg.norm(np.cross(m.mesh.nodes+s.state.u,total).sum(axis=0))/scale<3e-12
 audit=m.work_audit(init.state,s,force);print('WORK_AUDIT',audit);den=max(1,abs(audit['external_work_J']),abs(audit['stored_energy_change_J']),audit['physical_branch_dissipation_J'])
 assert abs(audit['algebraic_residual_J'])/den<1e-10 and audit['physical_branch_dissipation_J']>0
 assert abs(audit['equilibrium_work_residual_J'])/den<1e-10
 restored=m.restore(s.state.u,json.loads(json.dumps(s.state.material.to_dict())),s.state.model_signature)
 r1=m.solve(s.state,force,fixed,vals,1e6);r2=m.solve(restored,force,fixed,vals,1e6);assert saved(r1.state)==saved(r2.state)
 before=saved(s.state)
 with pytest.raises(RuntimeError):m.solve(s.state,force*2,fixed,vals,1e6,max_iterations=0)
 assert saved(s.state)==before

@pytest.mark.parametrize('kind',['elastic','maxwell','lubby'])
def test_LH_compression_rejects_all_groups_and_keepsold(kind):
 w=mesh(False);K=1.25e9;G=576923076.9230769
 law={'elastic':lambda:ReferenceElastic(K,G),'maxwell':lambda:DeviatoricMaxwell(K,G,G*1e7),'lubby':lambda:ReferentialLubby(Lubby2(K,G,.7*G,.7*G*63115200,G*473364000,mK=-2e-8,mM=-3e-8))}[kind]()
 m=WedgeModel(w,GroupedLaw((Group(np.arange(6),law),),(1,6)));s=m.initial_state();before=saved(s)
 with pytest.raises(ValueError):m.assemble(s,affine(m,np.diag([.5,1,1.])),0)
 assert before==saved(s)

def test_fraction_single_weight_and_no_zero_void():
 m=build(True,False,fraction=np.array([.4,.8]));full=build(True,False);s=m.initial_state();u=affine(m,loadedF());f,K,r=m.assemble(s,u,0)
 assert abs(m.mesh.measures.sum()-.6)<1e-13
 assert abs(m.mesh.body_force([0,0,-1]).sum(axis=0)[2]+1440.)<1e-10
 assert m.signature!=full.signature
 with pytest.raises(ValueError):m.assemble(full.initial_state(),u,0)
 with pytest.raises(ValueError):mesh(True,0.)

def test_strict_inputs_groupcoverage_and_immutable():
 m=build();w=m.mesh;old=m.initial_state()
 with pytest.raises(ValueError):WedgeMesh(w.nodes.astype(complex),w.cells,2400.)
 with pytest.raises(ValueError):WedgeMesh(w.nodes,w.cells.astype(float),2400.)
 with pytest.raises(ValueError):GroupedLaw((Group(np.arange(6),ReferenceElastic(1e9,4e8)),),(2,6))
 with pytest.raises(ValueError):GroupedLaw((Group(np.arange(6),ReferenceElastic(1e9,4e8)),Group(np.arange(6),ReferenceElastic(1e9,4e8))),(2,6))
 with pytest.raises(ValueError):m.assemble(old,old.u,True)
 with pytest.raises(ValueError):m.assemble(old,affine(m,np.diag([-1.,1,1])),0.)
 with pytest.raises(ValueError):w.nodes.setflags(write=True)
 with pytest.raises(ValueError):w.cells.setflags(write=True)


def test_one_nonsmooth_group_rejects_whole_trial_transaction():
 from vkm_world.representations.world0.finite_lubby import deformation_from_green,UnsupportedClassicalTangent
 m=build();old=m.initial_state();u=affine(m,loadedF());r=m.assemble(old,u,1e7)[2];loaded=FiniteState(u,m.law.commit(r.material_trial),m.signature)
 g=m.law.groups[1];part=m.law._extract(loaded.material,1)
 # Construct the rejected q=0 target from the unchanged original material law;
 # this is an input oracle, never an admitted grouped trial.
 target=g.law.inner.trial_stress(g.law._underlying(part),np.zeros_like(part.stress),1e6)
 F=deformation_from_green(target.state.strain[0]-g.law.reference_strain)
 before=saved(loaded)
 with pytest.raises(UnsupportedClassicalTangent):m.assemble(loaded,affine(m,F),1e6)
 assert before==saved(loaded)


@pytest.mark.parametrize('bad',['complex','booltime','vectortime','floatshape','foreignmodel','missingmemory'])
def test_restart_rejects_corrupt_payload_before_coercion(bad):
 import copy
 m=build();s=m.initial_state();payload=copy.deepcopy(s.material.to_dict());sig=s.model_signature
 if bad=='complex':payload['stress'][0][0][0]=1j
 elif bad=='booltime':payload['time_s']=True
 elif bad=='vectortime':payload['time_s']=[0.]
 elif bad=='floatshape':payload['memory']['1/kelvin']['shape']=[3.,6.]
 elif bad=='foreignmodel':sig='foreign'
 elif bad=='missingmemory':del payload['memory']['1/kelvin']
 before=saved(s)
 with pytest.raises(ValueError):m.restore(s.u,payload,sig)
 assert saved(s)==before

def test_moduli_bool_rejected():
 with pytest.raises(ValueError):ReferenceElastic(True,4e8)


@pytest.mark.parametrize('kind',['elastic','maxwell','lubby'])
def test_all_group_forged_stress_rejected_without_inventing_energy(kind):
 K=1.25e9;G=5e8
 law={'elastic':lambda:ReferenceElastic(K,G),'maxwell':lambda:DeviatoricMaxwell(K,G,1e17),'lubby':lambda:ReferentialLubby(Lubby2(K,G,.7*G,.7*G*63115200,G*473364000,mK=-2e-8,mM=-3e-8))}[kind]()
 m=WedgeModel(mesh(False),GroupedLaw((Group(np.arange(6),law),),(1,6)));s=m.initial_state();payload=s.material.to_dict();payload['stress'][0][0][0]=1e6;payload['stress'][0][0][1]=-1e6
 before=saved(s)
 with pytest.raises(ValueError):m.restore(s.u,payload,s.model_signature)
 assert before==saved(s)

@pytest.mark.parametrize('name',['1/kelvin','1/maxwell','2/creep'])
def test_traceless_forged_memory_breaking_closure_rejected(name):
 m=build();s=m.initial_state();payload=s.material.to_dict();payload['memory'][name]['values'][0][0]=.001;payload['memory'][name]['values'][0][1]=-.001
 with pytest.raises(ValueError):m.law.restore(payload)


def test_loaded_mixed_state_json_parity_after_closure_guard():
 m=build();old=m.initial_state();u=affine(m,loadedF());tr=m.assemble(old,u,1e7)[2].material_trial;s=FiniteState(u,m.law.commit(tr),m.signature)
 restored=m.restore(u,json.loads(json.dumps(s.material.to_dict())),m.signature)
 assert saved(restored)==saved(s)
 f1,K1,r1=m.assemble(s,u,1e6);f2,K2,r2=m.assemble(restored,u,1e6)
 assert np.array_equal(f1,f2) and np.array_equal(K1,K2)
 assert np.array_equal(m.law.energy(s.material),m.law.energy(restored.material))
