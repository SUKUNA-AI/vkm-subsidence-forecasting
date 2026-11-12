"""Small conservative extensions of World-0; SI units and explicit scenario inputs.

No parameter in this module is a calibrated SKRU-1 value. Trial functions never
mutate accepted state. Import this beside existing world0.kernels/laws.
"""
from __future__ import annotations
from dataclasses import dataclass
import math
import numpy as np
from scipy.linalg import solve
from scipy.optimize import brentq
from vkm_world.representations.world0.kernels import BackfillKernel
from vkm_world.representations.world0.ports import mechanical_port_basis


def _finite(name, value):
    value = np.asarray(value, dtype=float)
    if not np.isfinite(value).all():
        raise ValueError(f'{name} must be finite')
    return value


def _positive(name, value, zero=False):
    a = _finite(name, value)
    if np.any(a < 0 if zero else a <= 0):
        raise ValueError(f'{name} must be {"nonnegative" if zero else "positive"}')
    return a


@dataclass(frozen=True)
class ContactState:
    plastic_slip: tuple[float, float] = (0.0, 0.0)
    elastic_slip: tuple[float, float] = (0.0, 0.0)
    dissipation_J_m2: float = 0.0


def contact_trial(gap_m, slip_m, state, kn, kt, friction, *, normal=(0,0,1), tangent=(1,0,0), opposite=(0,0,-1)):
    """Dry penalty normal contact and 2D Coulomb radial return.

    gap>0 opens; p=kn*(-gap)+. tau is resisting generalized force, hence
    input work per area is -p*dgap+tau.dslip. Actual face tractions oppose.
    This compliant interface permits elastic overlap p/kn; it does NOT claim
    exact rigid Signorini complementarity at finite kn. Dilatancy is zero.
    kn,kt [Pa/m]. Lost tangent spring energy upon opening is explicit heat.
    """
    _positive('kn/kt',[kn,kt]); _positive('friction',friction,True)
    gap=float(_finite('gap',gap_m)); s=_finite('slip',slip_m)
    if s.shape != (2,): raise ValueError('two tangent components required')
    basis=mechanical_port_basis(normal,tangent,opposite)
    sp=_finite('plastic_slip',state.plastic_slip)
    se=_finite('elastic_slip',state.elastic_slip)
    if sp.shape != (2,) or se.shape != (2,): raise ValueError('bad contact state')
    _positive('accepted dissipation',state.dissipation_J_m2,True)
    p=kn*max(-gap,0.0)
    jac=np.zeros((3,3)); jac[0,0]=kn if gap<0 else 0
    # Jacobian is for generalized displacement [closure=-gap, slip1, slip2].
    if gap >= 0:
        released=.5*kt*float(se@se)
        new=ContactState(tuple(s), (0.,0.), state.dissipation_J_m2+released)
        return dict(state=new,p=0.,tau=np.zeros(2),traction=np.zeros(3),opposite_traction=np.zeros(3),
                    tangent=jac,stored_J_m2=0.,friction_J_m2=0.,opening_heat_J_m2=released,status='OPEN')
    trial=kt*(s-sp); r=float(np.linalg.norm(trial)); cap=friction*p
    if r <= cap:
        tau=trial; dsp=np.zeros(2); jac[1:,1:]=kt*np.eye(2); status='STICK'
    else:
        direction=trial/r; tau=cap*direction; dsp=(trial-tau)/kt
        jac[1:,1:]=cap*kt/r*(np.eye(2)-np.outer(direction,direction))
        jac[1:,0]=friction*kn*direction; status='SLIP'
    friction_work=cap*float(np.linalg.norm(dsp))
    new=ContactState(tuple(sp+dsp),tuple(tau/kt),state.dissipation_J_m2+friction_work)
    traction=basis.T@np.r_[-p,-tau]
    return dict(state=new,p=p,tau=tau,traction=traction,opposite_traction=-traction,tangent=jac,
                stored_J_m2=.5*p*p/kn+.5*float(tau@tau)/kt,friction_J_m2=friction_work,
                opening_heat_J_m2=0.,status=status)


@dataclass(frozen=True)
class BirthBackfillState:
    birth_time_s: float
    birth_convergence_m: float
    eps_max: float = 0.0


def birth_backfill_trial(time_s, convergence_m, state, h_fill, gap_top, E0, eps_limit, E_unload):
    """Existing hyperbolic BackfillKernel with an explicit stress-free birth.

    E_unload must dominate the largest attained loading tangent: this avoids
    negative residual strain/unphysical hysteresis. Hydraulics and lateral
    contact are separate ports; their activation is not inferred from roof.
    """
    _positive('h/E/limit',[h_fill,E0,eps_limit,E_unload]); _positive('gap/max',[gap_top,state.eps_max],True)
    _finite('time and reference',[time_s,convergence_m,state.birth_time_s,state.birth_convergence_m])
    if state.eps_max>=eps_limit: raise ValueError('invalid accepted compaction')
    if time_s < state.birth_time_s:
        return dict(state=state,stress_Pa=0.,tangent_Pa_m=0.,age_s=None,active=False,stored_J_m3=0.,dissipated_J_m3=0.)
    v=convergence_m-state.birth_convergence_m
    eps=max(v-gap_top,0.)/h_fill
    if eps>=eps_limit: raise ValueError('limiting compaction exceeded')
    em=max(eps,state.eps_max)
    if E_unload < E0/(1-em/eps_limit)**2:
        raise ValueError('unloading modulus below attained loading tangent')
    kernel=BackfillKernel(h_fill,gap_top,E0,eps_limit,E_unload)
    stress,newmax=kernel.stress(v,state.eps_max)
    tangent=0. if stress==0 else (E0/(1-eps/eps_limit)**2 if eps>=state.eps_max else E_unload)/h_fill
    sigma_max=E0*newmax/(1-newmax/eps_limit)
    envelope_work=E0*(-eps_limit*newmax-eps_limit**2*math.log1p(-newmax/eps_limit))
    # The unloading spring stores .5 sigmaВІ/Eu. The rest of envelope work is
    # irreversible compaction. The Eu>=attained tangent guard makes it grow.
    dissipation=envelope_work-.5*sigma_max**2/E_unload
    return dict(state=BirthBackfillState(state.birth_time_s,state.birth_convergence_m,newmax),
                stress_Pa=stress,tangent_Pa_m=tangent,age_s=time_s-state.birth_time_s,active=True,
                stored_J_m3=.5*stress**2/E_unload,dissipated_J_m3=max(0.,dissipation))


@dataclass(frozen=True)
class CohesiveState:
    maximum_opening_m: float = 0.0
    damage: float = 0.0
    dissipation_J_m2: float = 0.0


def cohesive_damage_trial(opening_m, state, stiffness_Pa_m, strength_Pa, fracture_J_m2):
    """Bilinear mode-I cohesive law, separate MODEL_CHOICE, not SRC047 law.

    Area under traction-opening is Gc. For crack-band h, pass k=E/h and w=h*eps;
    thus dissipated energy per crack area is independent of element width h.
    Compression uses the independent contact port and does not heal damage.
    """
    k,ft,Gc=map(float,_positive('cohesive parameters',[stiffness_Pa_m,strength_Pa,fracture_J_m2]))
    w=float(_finite('opening',opening_m)); _positive('history',state.maximum_opening_m,True)
    if not 0<=state.damage<=1 or state.dissipation_J_m2<0: raise ValueError('bad damage state')
    w0=ft/k; wf=2*Gc/ft
    if wf<=w0: raise ValueError('Gc too small for resolved elastic opening')
    wm=max(w,state.maximum_opening_m,0.)
    if wm<=w0: D=0.; dissipated=0.
    elif wm<wf:
        envelope=ft*(wf-wm)/(wf-w0); D=1-envelope/(k*wm)
        work=.5*ft*w0+ft*((wm-w0)-.5*(wm-w0)**2/(wf-w0))
        dissipated=work-.5*envelope*wm
    else: D=1.; dissipated=Gc
    if D+1e-14<state.damage or dissipated+1e-12<state.dissipation_J_m2:
        raise ValueError('state is inconsistent with cohesive parameters')
    traction=(1-D)*k*max(w,0.)
    tangent=(1-D)*k if w>0 else 0.
    if w>state.maximum_opening_m and w0<w<wf: tangent=-ft/(wf-w0)
    return dict(state=CohesiveState(wm,D,dissipated),traction_Pa=traction,tangent_Pa_m=tangent,
                stored_J_m2=.5*(1-D)*k*max(w,0.)**2,
                increment_dissipation_J_m2=dissipated-state.dissipation_J_m2)


def incidence(n, edges):
    """Internal oriented volume flow: + at donor, - at receiver."""
    B=np.zeros((n,len(edges)))
    for e,(i,j) in enumerate(edges):
        if i==j or min(i,j)<0 or max(i,j)>=n: raise ValueError('orphan/self edge')
        B[i,e]=1.; B[j,e]=-1.
    return B


def _check_incidence(B):
    B=_finite('incidence',B)
    if B.ndim!=2 or np.any(np.sum(B,axis=0)!=0) or np.any(np.sum(np.abs(B),axis=0)!=2):
        raise ValueError('internal edge must pair opposite unit endpoints')
    if not np.isin(B,[-1,0,1]).all(): raise ValueError('non-unit incidence')
    return B


def _spd(name, A):
    A=_finite(name,A)
    if A.ndim!=2 or A.shape[0]!=A.shape[1] or not np.allclose(A,A.T,rtol=1e-12,atol=0):
        raise ValueError(f'{name} must be symmetric square')
    try: np.linalg.cholesky(A)
    except np.linalg.LinAlgError as exc: raise ValueError(f'{name} must be positive definite') from exc
    return A


def conservative_hm_step(K,H,S,B,conductance,u0,p0,force,dt,source=None,reservoir_g=None,reservoir_p=None):
    """Implicit Biot step: K u-H p=f; m=S p+H.T u [m3].

    u uses the signed dilation map H, p [Pa], K [N/m], H [m2], S [m3/Pa].
    g [m3/(Pa s)], source [m3/s], reservoir conductance optional. No density
    compressibility is silently double counted: S is fixed-strain storage;
    skeleton compliance is already H.T inv(K) H. rho_ref*m is fluid mass anomaly.
    Returns physical Darcy loss and backward-Euler numerical damping separately.
    """
    K=_spd('K',K); S=_spd('fixed-strain storage',S); H=_finite('H',H); B=_check_incidence(B)
    u0=_finite('u0',u0); p0=_finite('p0',p0); f=_finite('force',force)
    g=_positive('conductance',conductance,True); _positive('dt',dt)
    nu,n=H.shape
    if K.shape!=(nu,nu) or S.shape!=(n,n) or B.shape[0]!=n or g.shape!=(B.shape[1],) or u0.shape!=(nu,) or p0.shape!=(n,) or f.shape!=(nu,):
        raise ValueError('incompatible HM shapes')
    source=np.zeros(n) if source is None else _finite('source',source)
    gb=np.zeros(n) if reservoir_g is None else _positive('reservoir g',reservoir_g,True)
    pb=np.zeros(n) if reservoir_p is None else _finite('reservoir p',reservoir_p)
    if any(a.shape!=(n,) for a in [source,gb,pb]): raise ValueError('incompatible boundary shapes')
    L=(B*g)@B.T; KH=solve(K,H,assume_a='pos'); Kf=solve(K,f,assume_a='pos')
    C=S+H.T@KH
    rhs=S@p0+H.T@u0-H.T@Kf+dt*(source+gb*pb)
    p=solve(C+dt*(L+np.diag(gb)),rhs,assume_a='pos'); u=Kf+KH@p
    q=g*(B.T@p); boundary_in=gb*(pb-p)
    du=u-u0; dp=p-p0; dm=S@dp+H.T@du
    residual=dm+dt*(B@q-source-boundary_in)
    E0=.5*(u0@K@u0+p0@S@p0); E1=.5*(u@K@u+p@S@p)
    physical=dt*(p@L@p+np.sum(gb*(p-pb)**2))
    numerical=.5*(du@K@du+dp@S@dp)
    work=f@du+dt*(p@source+pb@boundary_in)
    return dict(u=u,p=p,q=q,m=S@p+H.T@u,mass_residual_m3=residual,
                boundary_in_m3_s=boundary_in,energy_J=E1,energy_residual_J=E1-E0+physical+numerical-work,
                darcy_dissipation_J=physical,numerical_damping_J=numerical,external_work_J=work,
                force_residual_N=K@u-H@p-f)


def directional_conductance(permeability, normal, area, length, viscosity):
    """TPFA coefficient A n.k.n/(mu L), for aligned orthogonal cells only."""
    k=_spd('permeability',permeability); n=_finite('normal',normal)
    if k.shape!=(3,3) or n.shape!=(3,) or not np.isclose(n@n,1.,atol=1e-12): raise ValueError('unit normal required')
    _positive('geometry/mu',[area,length,viscosity])
    return float(area*(n@k@n)/(viscosity*length))


def transport_step(volumes, concentration, B, diffusion_g, flow, dt):
    """Conservative implicit upwind advection + symmetric compartment exchange.

    Concentration [mol/m3], volume [m3], diffusive/exchange conductance [m3/s].
    Multirate immobile zones are explicit compartments, g_k=a_k V_k; never
    include the same volume both as a hidden compartment and a resolved cell.
    No-flow closed domain; external carriers require explicit boundary balance.
    """
    V=_positive('volumes',volumes); c=_positive('concentration',concentration,True)
    B=_check_incidence(B); g=_positive('diffusion',diffusion_g,True); q=_finite('flow',flow); _positive('dt',dt)
    if c.shape!=V.shape or B.shape!=(len(V),len(g)) or q.shape!=g.shape: raise ValueError('transport shapes')
    A=(B*g)@B.T
    for e,Q in enumerate(q):
        if Q==0: continue
        donor=int(np.flatnonzero(B[:,e]==np.sign(Q))[0])
        A[:,donor]+=B[:,e]*Q
    new=solve(np.diag(V)+dt*A,V*c)
    if np.min(new)<-1e-12*max(1.,np.max(c)): raise RuntimeError('negative concentration')
    return dict(concentration=new,mass_residual_mol=float(V@(new-c)),operator=A)


def callahan_pressure_solution_component(eps_v,phi0,r1star,r3,r4,Qs_R,T,ns,sigma_effective_MPa):
    """Only SRC192 eq2.36вЂ“37 scalar equivalent rate, NOT full 31-parameter law.

    eps_v=log(V/V_ref)<0. r1star [K/(MPa s)] includes water/grain factors;
    Qs_R [K], effective stress [MPa]. Source's zero-strain singularity rejected;
    preconsolidation must be supplied explicitly. Piecewise Gamma reproduced.
    phi0>.15 is required: the source's -15% switching rule otherwise cannot
    reach its large-strain Gamma branch before full density. No silent extension.
    """
    _finite('Callahan inputs',[eps_v,phi0,r1star,r3,r4,Qs_R,T,ns,sigma_effective_MPa])
    _positive('Callahan constants',[r4,T,ns]); _positive('rate/Qs/stress',[r1star,Qs_R,sigma_effective_MPa],True)
    if not .15<phi0<1 or not math.log(1-phi0)<=eps_v<0: raise ValueError('requires phi0>.15, density within initial-to-solid interval and nonzero prestrain')
    ev=math.exp(eps_v)
    Gamma=1. if ev-1>-.15 else max(0.,(ev+phi0-1)/(phi0*ev))**ns
    return r1star*math.exp(-eps_v+r3*eps_v-Qs_R/T)/(abs(math.expm1(eps_v))**r4*T)*Gamma*sigma_effective_MPa


def dissolution_step(solid_mol,Na_mol,Cl_mol,water_kg,pore_m3,bulk_m3,dt,rate_mol_s,csat_mol_m3,
                     solid_molar_m3,aqueous_partial_molar_m3,rho_water=1000.):
    """NaCl reaction with saturated pore filling by an explicit water reservoir.

    MODEL_CHOICE ideal concentration activity. Positive dn dissolves, negative
    precipitates. Solid volume change creates pore space. Water inflow fills
    the difference between solid and aqueous partial molar volumes. Component
    and total-mass balances include that flux; this is not a closed water box.
    Only an electroneutral NaCl solution is supported. Its initial liquid
    volume water/rho + vbar_NaCl*nNaCl must equal pore volume. Other ions or
    unequal Na/Cl require their own partial molar volumes and charge balance.
    No local VKM activity or kinetic calibration is claimed.
    """
    _positive('inventories',[solid_mol,Na_mol,Cl_mol,water_kg],True)
    _positive('reaction parameters',[pore_m3,bulk_m3,dt,csat_mol_m3,solid_molar_m3,rho_water])
    _positive('rate/partial volume',[rate_mol_s,aqueous_partial_molar_m3],True)
    if pore_m3>=bulk_m3 or aqueous_partial_molar_m3>solid_molar_m3: raise ValueError('unsupported pore/partial-volume regime')
    if solid_mol*solid_molar_m3>bulk_m3-pore_m3+1e-12*bulk_m3:
        raise ValueError('reactive solid volume exceeds available solid skeleton')
    if not math.isclose(Na_mol,Cl_mol,rel_tol=1e-12,abs_tol=1e-14):
        raise ValueError('this NaCl-only model requires electroneutral equal Na/Cl')
    initial_liquid=water_kg/rho_water+aqueous_partial_molar_m3*Na_mol
    if not math.isclose(initial_liquid,pore_m3,rel_tol=1e-10,abs_tol=1e-15):
        raise ValueError('initial liquid partial volumes must fill the pore volume')
    lo=max(-min(Na_mol,Cl_mol),-pore_m3/solid_molar_m3*.999999)
    hi=min(solid_mol,(bulk_m3-pore_m3)/solid_molar_m3*.999999)
    def residual(dn):
        V=pore_m3+solid_molar_m3*dn
        # Ion activity product reduced to sqrt(cNa*cCl)/csat, ideal MODEL_CHOICE.
        sat=math.sqrt(max(0.,Na_mol+dn)*max(0.,Cl_mol+dn))/(V*csat_mol_m3)
        return dn-dt*rate_mol_s*(1-sat)
    if residual(lo)>=0: dn=lo
    elif residual(hi)<=0: dn=hi
    else: dn=brentq(residual,lo,hi,xtol=1e-13)
    water_in=rho_water*(solid_molar_m3-aqueous_partial_molar_m3)*dn
    if water_kg+water_in<0: raise ValueError('insufficient water for precipitation')
    V=pore_m3+solid_molar_m3*dn
    return dict(solid_mol=solid_mol-dn,Na_mol=Na_mol+dn,Cl_mol=Cl_mol+dn,
                water_kg=water_kg+water_in,water_in_kg=water_in,pore_m3=V,
                porosity=V/bulk_m3,reaction_mol=dn,
                permeability_ratio=(V/pore_m3)**3*((bulk_m3-pore_m3)/(bulk_m3-V))**2)
