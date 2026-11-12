import numpy as np
from scipy.sparse import csr_matrix
from scipy.linalg import expm, solve
from vkm_world.representations.world0.event_gauss4 import step, A2, C2, ramp
from vkm_world.representations.world0 import event_state_io as state_io

def test_independent_fullblock_and_expm():
    rng=np.random.default_rng(34);n=7;M=np.linspace(2.,5.,n);dm=np.linspace(.1,.3,n);q=rng.normal(size=(n,n));K=q.T@q+np.eye(n)*4;q=rng.normal(size=(n,n));dK=q.T@q*.2
    q=rng.normal(size=(n,n));C=q.T@q*.15;B=rng.normal(size=(n,3));S=np.array([.5,.8,1.2]);A=K+(B/S)@B.T;r0=rng.normal(size=n);rel=rng.normal(size=n);x=rng.normal(size=n)*.2;v=rng.normal(size=n)*.1;h=.017;t=.31
    xn,vn,sx,sv,_,diag=step(t,h,x,v,M,dm,csr_matrix(A),csr_matrix(dK),csr_matrix(C),r0,rel)
    # Independent first-order state-stage direct equations (not acceleration elimination).
    Js=[];forces=[]
    for c in C2:
        a,_=ramp(t+c*h);mass=M-a*dm;kk=A+(1-a)*dK;Js.append(np.block([[np.zeros((n,n)),np.eye(n)],[-kk/mass[:,None],-C/mass[:,None]]]));forces.append(np.r_[np.zeros(n),(r0+a*rel)/mass])
    direct=np.block([[np.eye(2*n)*(i==j)-h*A2[i,j]*Js[j] for j in range(2)] for i in range(2)])
    rhs=np.concatenate([np.r_[x,v]+h*sum(A2[i,j]*forces[j] for j in range(2)) for i in range(2)]);states=solve(direct,rhs).reshape(2,2*n)
    err=float(np.linalg.norm(np.c_[sx,sv]-states)/np.linalg.norm(states));assert err<1e-10
    m=M-dm;J=np.block([[np.zeros((n,n)),np.eye(n)],[-A/m[:,None],-C/m[:,None]]]);force=np.r_[np.zeros(n),(r0+rel)/m];aug=np.zeros((2*n+1,2*n+1));aug[:2*n,:2*n]=J;aug[:2*n,-1]=force
    ref=(expm(aug)@np.r_[x,v,1.])[:2*n];errors=[];energy=[];masserr=[]
    for dt in (.05,.025,.0125):
        u=x.copy();w=v.copy();rad=0.;E0=.5*w@(m*w)+.5*u@A@u-(r0+rel)@u
        for k in range(round(1/dt)):
            u,w,us,vs,_,_=step(k*dt,dt,u,w,M,dm,csr_matrix(A),csr_matrix(dK),csr_matrix(C),r0,rel,constant_post=True);rad+=dt*sum(vv@C@vv for vv in vs)/2
        errors.append(float(np.linalg.norm(np.r_[u,w]-ref)/np.linalg.norm(ref)));energy.append(float(abs(.5*w@(m*w)+.5*u@A@u-(r0+rel)@u-E0+rad)));p=-B.T@u/S;masserr.append(float(np.max(abs(S*p+B.T@u))))
    assert max(energy)<1e-10 and max(masserr)<1e-12 and min(np.array(errors[:-1])/errors[1:])>15


def test_stage_restart_corruption(tmp_path):
    out=tmp_path
    x=np.arange(7,dtype=float)*.1;v=-x;flux=np.arange(4,dtype=float)*11;warm=np.arange(14,dtype=float)*.03;identity='synthetic-nonzero-warm-exact'
    state_io.save_stage(out/'state.npz',x,v,flux,12,identity,warm);a,b,c,k,w=state_io.load_stage(out/'state.npz',identity,(7,),20)
    assert np.array_equal(a,x) and np.array_equal(b,v) and np.array_equal(c,flux) and np.array_equal(w,warm) and k==12
    original=dict(np.load(out/'state.npz'));checks={}
    for key,change in [('warm',lambda q:q+1e-8),('x',lambda q:q+1e-8),('step',lambda q:np.array(.5)),('v',lambda q:q*np.nan),('flux',lambda q:q[:2])]:
        altered={k:v.copy() for k,v in original.items()};altered[key]=change(altered[key]);path=out/(key+'.npz');np.savez_compressed(path,**altered)
        try:state_io.load_stage(path,identity,(7,),20);checks[key]=False
        except ValueError:checks[key]=True
    assert all(checks.values())
