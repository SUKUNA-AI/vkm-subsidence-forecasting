"""Two affine plane-strain cells, finite solid/fluid work and reference graph.

SVK here is an analytic verification fixture, not an alternative FEM backend.
All mechanical forces are nodal generalized forces in N (length DOFs in m).
"""
from dataclasses import asdict
from pathlib import Path
import hashlib,json,sys
import numpy as np
from vkm_world.representations.world0.finite_poromechanics import ReferenceFluid,cofactor,discrete_cofactor
import vkm_world.representations.world0.finite_poromechanics as fluid_module


def svk(F,lam=4e6,mu=4e6):
    F=np.asarray(F);E=.5*(F.T@F-np.eye(3));S=lam*np.trace(E)*np.eye(3)+2*mu*E
    W=.5*lam*np.trace(E)**2+mu*np.sum(E*E);P=F@S
    A=np.einsum('ik,lj->ijkl',np.eye(3),S)+lam*np.einsum('ij,kl->ijkl',F,F)+mu*np.einsum('il,kj->ijkl',F,F)+mu*np.einsum('jl,ik->ijkl',np.eye(3),F@F.T)
    return W,P,A,E,S


def svk_discrete(F0,F1):
    W0,_,_,E0,S0=svk(F0);W1,_,_,E1,S1=svk(F1)
    Fm=.5*(F0+F1);Sm=.5*(S0+S1);P=Fm@Sm
    lam=mu=4e6
    A=.5*np.einsum('ik,lj->ijkl',np.eye(3),Sm)+.5*lam*np.einsum('ij,kl->ijkl',Fm,F1)+.5*mu*np.einsum('il,kj->ijkl',Fm,F1)+.5*mu*np.einsum('jl,ik->ijkl',np.eye(3),Fm@F1.T)
    return W0,W1,P,A


class Pair:
    def __init__(self,conductance=1e-10):
        self.cells=[ReferenceFluid(1.,.3/2.2e9,1.,2e6) for _ in range(2)]
        self.B=np.array([[1.],[-1.]])
        self.g=float(conductance)
        if not np.isfinite(self.g) or self.g<0: raise ValueError('invalid reference mobility')
        self.L=self.g*self.B@self.B.T
        self.G=np.zeros((2,3,3));self.G[0,0,0]=1.;self.G[1,1,1]=1. # unit-m reference lengths
        self.reference_force=np.full(4,-2e6)
        codes={p.name:hashlib.sha256(p.read_bytes()).hexdigest() for p in [Path(__file__),Path(fluid_module.__file__)]}
        self.signature=hashlib.sha256(json.dumps(dict(cells=[asdict(c) for c in self.cells],g=self.g,lengths_m=[1,1],solid='SVK_lam_mu_4MPa',forcing='compressed_cell0_ramp_hold_release_T2000-v1',codes=codes),sort_keys=True).encode()).hexdigest()

    def F(self,q): return np.eye(3)+np.einsum('a,aij->ij',q,self.G)

    def state(self,q,p):
        mass=[];energy=0.;forces=[];Ks=[];Gs=[];Js=[];pabs=[]
        for i,c in enumerate(self.cells):
            F=self.F(q[2*i:2*i+2]);r=c.mixed(F,p[i]);W,Ps,A,_,_=svk(F)
            energy+=c.V0_m3*(W+r['W']);mass.append(r['zeta_m3']);Js.append(r['J']);pabs.append(r['absolute_pressure_Pa'])
            forces.extend(c.V0_m3*np.einsum('aij,ij->a',self.G,Ps+r['P']))
            Ks.append(c.V0_m3*np.einsum('aij,ijkl,bkl->ab',self.G,A+r['A'],self.G))
            Gs.append(np.einsum('aij,ij->a',self.G,r['dcontent_dF']))
        return dict(mass=np.array(mass),energy=float(energy),force=np.array(forces),K=Ks,G=Gs,J=Js,pabs=np.array(pabs))

    def force(self,t):
        load=float(np.interp(t,[0,500,1500,2000],[0,1,1,0]))
        return self.reference_force+load*np.array([-3e6,0,0,0])

    def residual(self,q,p,q0,p0,dt,forcing,scheme='DG'):
        old=self.state(q0,p0);new=self.state(q,p)
        residual=np.zeros(6);K=np.zeros((6,6))
        for i,c in enumerate(self.cells):
            F0=self.F(q0[2*i:2*i+2]);F1=self.F(q[2*i:2*i+2]);s=slice(2*i,2*i+2)
            if scheme=='DG':
                _,_,Ps,As=svk_discrete(F0,F1);Cb,dCb=discrete_cofactor(F0,F1)
                pb=c.reference_pressure_Pa+.5*(p0[i]+p[i]);P=Ps-c.alpha*pb*Cb;A=As-c.alpha*pb*dCb
                cross=-.5*c.alpha*c.V0_m3*np.einsum('aij,ij->a',self.G,Cb)
            elif scheme=='BE':
                _,Ps,As,_,_=svk(F1);r=c.mixed(F1,p[i]);P=Ps+r['P'];A=As+r['A']
                cross=-new['G'][i]
            else: raise ValueError('unknown time representation')
            residual[s]=c.V0_m3*np.einsum('aij,ij->a',self.G,P)-forcing[s]
            K[s,s]=c.V0_m3*np.einsum('aij,ijkl,bkl->ab',self.G,A,self.G)
            K[s,4+i]=cross
            K[4+i,s]=new['G'][i]
        weight=.5 if scheme=='DG' else 1.
        pflow=(1-weight)*old['pabs']+weight*new['pabs']
        residual[4:]=new['mass']-old['mass']+dt*self.L@pflow
        K[4:,4:]=np.diag([c.storage_m3_Pa for c in self.cells])+dt*weight*self.L
        return residual,K,new,pflow

    def advance(self,q0,p0,t,dt,scheme='DG'):
        forcing=self.force(t+.5*dt if scheme=='DG' else t+dt)
        q=q0.copy();p=p0.copy();scales=np.r_[np.full(4,1e6),np.full(2,.1)]
        unknown_scale=np.r_[np.ones(4),np.full(2,1e6)]
        for it in range(35):
            r,K,new,pflow=self.residual(q,p,q0,p0,dt,forcing,scheme);norm=max(abs(r/scales))
            if norm<1e-11: break
            direction=unknown_scale*np.linalg.solve((K*unknown_scale)/scales[:,None],-r/scales)
            factor=1.
            for _ in range(25):
                qt=q+factor*direction[:4];pt=p+factor*direction[4:]
                try:rt,_,_,_=self.residual(qt,pt,q0,p0,dt,forcing,scheme)
                except ValueError:factor*=.5;continue
                if max(abs(rt/scales))<norm: q,p=qt,pt;break
                factor*=.5
            else: raise RuntimeError('line search failed without committing state')
        else: raise RuntimeError('Newton failed without committing state')
        old=self.state(q0,p0);du=q-q0
        work=float(forcing@du);loss=float(dt*pflow@self.L@pflow)
        balance=new['energy']-old['energy']+loss-work
        ns=nf=0.
        if scheme=='BE':
            for i,c in enumerate(self.cells):
                F0=self.F(q0[2*i:2*i+2]);F1=self.F(q[2*i:2*i+2]);dF=F1-F0
                W0,*_=svk(F0);W1,P1,*_=svk(F1)
                z0=c.content(F0,p0[i]);z1=c.content(F1,p[i]);r0=c.evaluate(F0,z0);r1=c.evaluate(F1,z1)
                ns+=c.V0_m3*(np.sum(P1*dF)-(W1-W0))
                nf+=c.V0_m3*np.sum(r1['P']*dF)+r1['absolute_pressure_Pa']*(z1-z0)-(r1['energy_J']-r0['energy_J'])
        return q,p,dict(work_J=work,Darcy_J=loss,balance_J=balance,algorithmic_solid_J=ns,algorithmic_fluid_J=nf,corrected_balance_J=balance+ns+nf,mass_error_m3=float(sum(new['mass']-old['mass'])),residual=float(norm),iterations=it+1,energy_J=new['energy'],min_pressure_Pa=float(min(new['pabs'])),J=new['J'],instant_force_defect_N=float(max(abs(new['force']-self.force(t+dt)))))


def run(steps=200,conductance=1e-10,scheme='DG',restart=None):
    model=Pair(conductance);dt=2000./steps
    q=np.zeros(4);p=np.zeros(2);start=0;work=loss=0.;rows=[]
    numerical_solid=numerical_fluid=0.
    if restart is not None:
        payload=dict(restart);checksum=payload.pop('checksum',None)
        if checksum!=hashlib.sha256(json.dumps(payload,sort_keys=True,allow_nan=False).encode()).hexdigest(): raise ValueError('checkpoint corruption')
        if restart['signature']!=model.signature or restart['steps']!=steps or restart['scheme']!=scheme: raise ValueError('incompatible restart')
        q=np.array(restart['q']);p=np.array(restart['p']);start=restart['step'];work=restart['work'];loss=restart['loss']
        if q.shape!=(4,) or p.shape!=(2,) or not 0<=start<steps: raise ValueError('invalid restart state')
        if not np.array_equal(model.state(q,p)['mass'],restart['mass']): raise ValueError('checkpoint content inconsistent')
        numerical_solid=restart['numerical_solid'];numerical_fluid=restart['numerical_fluid']
    checkpoint=None;maximum=masserr=defect=0.;pmin=1e99;maxdeform=0.
    for k in range(start,steps):
        q,p,r=model.advance(q,p,k*dt,dt,scheme);work+=r['work_J'];loss+=r['Darcy_J']
        numerical_solid+=r['algorithmic_solid_J'];numerical_fluid+=r['algorithmic_fluid_J']
        maximum=max(maximum,abs(r['balance_J']));masserr=max(masserr,abs(r['mass_error_m3']));defect=max(defect,r['instant_force_defect_N']);pmin=min(pmin,r['min_pressure_Pa']);maxdeform=max(maxdeform,max(abs(q)))
        rows.append([(k+1)*dt,*q,*p,*r['J'],r['energy_J'],work,loss,r['balance_J']])
        if k+1==steps//2:
            checkpoint=dict(signature=model.signature,steps=steps,scheme=scheme,step=k+1,q=q.tolist(),p=p.tolist(),mass=model.state(q,p)['mass'].tolist(),work=work,loss=loss,numerical_solid=numerical_solid,numerical_fluid=numerical_fluid)
            checkpoint['checksum']=hashlib.sha256(json.dumps(checkpoint,sort_keys=True,allow_nan=False).encode()).hexdigest()
    result=dict(signature=model.signature,scheme=scheme,steps=steps,max_step_balance_J=maximum,final_balance_J=r['energy_J']+loss-work,algorithmic_solid_J=numerical_solid,algorithmic_fluid_J=numerical_fluid,corrected_final_balance_J=r['energy_J']+loss+numerical_solid+numerical_fluid-work,max_mass_error_m3=masserr,min_pressure_Pa=pmin,max_abs_displacement_m=maxdeform,max_instant_force_defect_N=defect,final_q=q.tolist(),final_p=p.tolist(),Darcy_J=loss,work_J=work,energy_J=r['energy_J'])
    return np.array(rows),result,checkpoint


if __name__=='__main__':
    out=Path(sys.argv[1]);out.mkdir(exist_ok=False,parents=True)
    data={};receipts={}
    for name,n,g,scheme in [('DG100',100,1e-10,'DG'),('DG200',200,1e-10,'DG'),('DG400',400,1e-10,'DG'),('DG800',800,1e-10,'DG'),('BE400',400,1e-10,'BE'),('BE800',800,1e-10,'BE'),('closed_DG200',200,0.,'DG')]:
        a,r,c=run(n,g,scheme);data[name]=a;receipts[name]=r
        (out/f'{name}_checkpoint.json').write_text(json.dumps(c,indent=2))
        if name=='DG200':
            ar,rr,_=run(n,g,scheme,json.loads(json.dumps(c)));r['restart_final_max_abs_difference']=float(max(abs(a[-1]-ar[-1])))
    np.savez_compressed(out/'histories.npz',**data)
    (out/'receipt.json').write_text(json.dumps(receipts,indent=2));print(json.dumps(receipts,indent=2))
