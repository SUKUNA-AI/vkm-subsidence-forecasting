"""Trial/commit Biot–Darcy coupling for the evolving generic conforming mechanical state."""
from collections import defaultdict
from dataclasses import dataclass
from functools import lru_cache
import hashlib
import json
import math
from pathlib import Path

import numpy as np
from scipy.sparse import coo_matrix, csr_matrix, diags
from scipy.sparse.linalg import LinearOperator, cg, gmres, splu


def _immutable(value):
    out = np.ascontiguousarray(value, dtype=float)
    return np.frombuffer(out.tobytes(),dtype=out.dtype).reshape(out.shape)


@dataclass(frozen=True)
class WaterState:
    pressure_pa: np.ndarray
    dilation_m3: np.ndarray
    topology_id: str

    def __post_init__(self):
        for k in ("pressure_pa", "dilation_m3"):
            object.__setattr__(self, k, _immutable(getattr(self, k)))


@dataclass(frozen=True)
class WaterTrial:
    state: WaterState
    displacement_free: np.ndarray
    mechanical_rhs_free: np.ndarray
    diagnostics: dict


class WaterModel:
    """Linearized saturated water on immutable reference cells and declared edges."""

    def __init__(self, B, storage, volume, alpha, absolute_p0, u_ref, edges,
                 well, well_t, compartments, topology_id):
        self.B = csr_matrix(B)
        self.Bt = self.B.T.tocsr()
        self.storage, self.volume, self.alpha, self.absolute_p0, self.reference_displacement_free = map(
            _immutable, (storage, volume, alpha, absolute_p0, u_ref)
        )
        self.pressure_cell_count = len(self.storage)
        self.topology_id = topology_id
        self.compartment = tuple(compartments)
        self.well, self.well_t = int(well), float(well_t)
        if self.B.shape != (len(u_ref), self.pressure_cell_count):
            raise ValueError("mechanical and pressure spaces do not agree")
        for v in (self.storage,self.volume,self.alpha,self.absolute_p0,self.reference_displacement_free,self.B.data):
            if not np.all(np.isfinite(v)):
                raise ValueError("nonfinite material or coupling")
        for v in (self.volume,self.alpha,self.absolute_p0):
            if v.shape != self.storage.shape or np.min(v) <= 0:
                raise ValueError("invalid pressure cell volume, alpha or reference pressure")
        if np.min(self.storage) <= 0 or np.max(self.alpha) > 1 or len(compartments)!=self.pressure_cell_count:
            raise ValueError("invalid Biot storage, alpha or compartments")
        if not 0 <= self.well < self.pressure_cell_count or not math.isfinite(self.well_t) or self.well_t<0:
            raise ValueError("invalid well law")
        edges = np.asarray(edges,dtype=float).reshape(-1,3)
        self.a,self.b = edges[:,:2].astype(int).T
        self.trans = _immutable(edges[:,2])
        if np.any(edges[:,:2]!=np.column_stack((self.a,self.b))) or np.any(self.a<0) or np.any(self.b<0) or (
            np.any(self.a>=self.pressure_cell_count) or np.any(self.b>=self.pressure_cell_count) or
            np.any(self.a==self.b) or np.any(self.trans<0) or not np.all(np.isfinite(self.trans))
        ):
            raise ValueError("invalid Darcy edges")
        n = self.pressure_cell_count
        a,b,t = self.a,self.b,self.trans
        self.L_internal = coo_matrix((np.r_[t,t,-t,-t],(np.r_[a,b,a,b],np.r_[a,b,b,a])),shape=(n,n)).tocsr()
        sink = np.zeros(n)
        sink[self.well] = self.well_t
        self.L = (self.L_internal+diags(sink)).tocsr()
        self.reference_dilation_m3 = _immutable(self.Bt @ self.reference_displacement_free)


    def initial(self):
        zeros=np.zeros(self.pressure_cell_count)
        return WaterState(zeros,zeros,self.topology_id)

    @lru_cache(maxsize=4)
    def _hydraulic_factor(self, dt):
        if dt==0:
            return lambda v: v/self.storage
        H=(diags(self.storage)+dt*self.L).tocsc()
        scale=1/np.sqrt(H.diagonal())
        lu=splu(H.multiply(scale[:,None]).multiply(scale[None,:]).tocsc(),permc_spec="MMD_AT_PLUS_A")
        return lambda v: scale*lu.solve(scale*v)

    def affine(self, previous, dt_s, *, well_target_pa=0., event_volume_m3=None):
        dt,target=float(dt_s),float(well_target_pa)
        if not math.isfinite(dt) or dt<0 or not math.isfinite(target) or self.absolute_p0[self.well]+target<=0:
            raise ValueError("invalid time increment or saturated well target")
        if previous.topology_id!=self.topology_id:
            raise ValueError("pressure checkpoint topology changed: conservative remap required")
        for v in (previous.pressure_pa,previous.dilation_m3):
            if v.shape!=self.storage.shape or not np.all(np.isfinite(v)):
                raise ValueError("invalid pressure checkpoint")
        if np.min(self.absolute_p0+previous.pressure_pa)<=0 or np.min(self.volume+previous.dilation_m3/self.alpha)<=0:
            raise ValueError("invalid saturated pressure/volume checkpoint")
        event=np.zeros(self.pressure_cell_count) if event_volume_m3 is None else np.asarray(event_volume_m3,dtype=float)
        if event.shape!=self.storage.shape or not np.all(np.isfinite(event)):
            raise ValueError("invalid or unmapped event water volume")
        if dt>0 and np.any(event):
            raise ValueError("instantaneous event volume must be recorded in a separate dt=0 trial")
        rhs=self.storage*previous.pressure_pa+previous.dilation_m3+event
        rhs[self.well]+=dt*self.well_t*target
        return HydraulicAffine(self,previous,dt,target,_immutable(event),_immutable(rhs),self._hydraulic_factor(dt))

    def trial(self, previous, dt_s, solve_k, k_diagonal, mechanical_rhs_free, *, well_target_pa=0., event_volume_m3=None, symmetric_tangent=True):
        """Pressure-Schur alternative for callers already holding a current-K factor."""
        a=self.affine(previous,dt_s,well_target_pa=well_target_pa,event_volume_m3=event_volume_m3)
        diag=np.asarray(k_diagonal,dtype=float)
        f=np.asarray(mechanical_rhs_free,dtype=float)
        if diag.shape!=(self.B.shape[0],) or f.shape!=diag.shape or not np.all(diag>0):
            raise ValueError("current tangent diagonal and RHS do not bind to mechanics")
        u0=solve_k(f)
        rhs=a.rhs_storage-(self.Bt @ (u0-self.reference_displacement_free))
        schur_diag=np.asarray(self.B.power(2).T@(1/diag)).ravel()
        scale=1/np.sqrt(self.storage+a.dt*self.L.diagonal()+schur_diag)
        H=(diags(self.storage)+a.dt*self.L).tocsr()
        def action(x):
            p=scale*x
            return scale*(H@p+self.Bt@solve_k(self.B@p))
        op=LinearOperator((len(rhs),len(rhs)),matvec=action,dtype=float)
        def precondition(x):
            return a.solve_H(x/scale)/scale
        pre=LinearOperator(op.shape,matvec=precondition,dtype=float)
        krylov=cg if symmetric_tangent else gmres
        p_scaled,info=krylov(op,scale*rhs,x0=previous.pressure_pa/scale,M=pre,rtol=2e-11,atol=1e-14,maxiter=160)
        if info!=0:
            raise RuntimeError(f"current tangent pressure Schur {'CG' if symmetric_tangent else 'GMRES'} failed: {info}")
        p=p_scaled*scale
        ff=f+self.B@p
        u=solve_k(ff)
        state,diag=a.accept(u,p)
        return WaterTrial(state,_immutable(u),_immutable(ff),diag)


@dataclass(frozen=True)
class HydraulicAffine:
    model: WaterModel
    previous: WaterState
    dt: float
    target_pa: float
    event_volume_m3: np.ndarray
    rhs_storage: np.ndarray
    solve_H: object

    def mechanical_update(self, v):
        m=self.model
        return m.B@self.solve_H(m.Bt@v)

    def pressure_affine_load(self):
        m=self.model
        return m.B@self.solve_H(self.rhs_storage+m.reference_dilation_m3)

    def pressure(self, u_free):
        m=self.model
        return self.solve_H(self.rhs_storage-m.Bt@(u_free-m.reference_displacement_free))

    def accept(self, u_free, p=None):
        """Validate a converged trial and return independent accepted arrays; no in-place commit."""
        m=self.model
        u=np.asarray(u_free,dtype=float)
        p=self.pressure(u) if p is None else np.asarray(p,dtype=float)
        if u.shape!=m.reference_displacement_free.shape or p.shape!=m.storage.shape or (
            not np.all(np.isfinite(u)) or not np.all(np.isfinite(p))
        ):
            raise ValueError("nonfinite or inconsistent mechanical/pressure candidate")
        old_p=self.previous.pressure_pa
        dilation=m.Bt@(u-m.reference_displacement_free)
        cell_vol=m.volume+dilation/m.alpha
        if np.min(m.absolute_p0+p)<=0 or np.min(cell_vol)<=0:
            raise RuntimeError("trial leaves saturated positive volume/pressure regime")
        delta_dilation=dilation-self.previous.dilation_m3
        dp=p-old_p
        delta_content=m.storage*dp+delta_dilation
        edge_flux=m.trans*(p[m.a]-p[m.b])
        edges=np.bincount(m.a,weights=edge_flux,minlength=len(p))-np.bincount(m.b,weights=edge_flux,minlength=len(p))
        well_in=m.well_t*(self.target_pa-p[m.well]) if self.dt>0 else 0.
        balance=delta_content+self.dt*edges-self.event_volume_m3
        balance[m.well]-=self.dt*well_in
        scale=max(float(np.max(abs(delta_content))),float(np.max(abs(self.dt*edges))),abs(self.dt*well_in),
                  float(np.max(abs(self.event_volume_m3))),1.)
        cell_error=float(np.max(abs(balance)))
        groups={}
        for name in dict.fromkeys(m.compartment):
            keep=np.asarray(m.compartment)==name
            groups[name]={"content_change_m3":float(delta_content[keep].sum()),
                          "net_intercell_outflow_m3":float(self.dt*edges[keep].sum()),
                          "well_inflow_m3":float(self.dt*well_in if keep[m.well] else 0.),
                          "event_volume_m3":float(self.event_volume_m3[keep].sum()),
                          "mass_residual_m3":float(balance[keep].sum())}
        global_error=float(abs(balance.sum()))
        total_scale=max(float(np.sum(abs(delta_content))),float(np.sum(abs(self.dt*edges))),abs(self.dt*well_in),
                        float(np.sum(abs(self.event_volume_m3))),1.)
        if cell_error>1e-7+2e-8*scale or max(global_error,*(abs(g["mass_residual_m3"]) for g in groups.values()))>1e-7+2e-8*total_scale:
            raise RuntimeError(f"hydraulic mass balance failed: {cell_error=}, {global_error=}")
        # Incremental fluid energy identity. Mechanical exchange is signed external work
        # for this subsystem, not a proof of total solid energy across changing topology.
        storage_change=.5*float((p+old_p)@(m.storage*dp))
        be_diss=.5*float(dp@(m.storage*dp))
        work_on_solid=float(p@delta_dilation)
        internal_diss=float(self.dt*(p[m.a]-p[m.b])@edge_flux)
        boundary_diss=self.dt*m.well_t*(self.target_pa-p[m.well])**2
        boundary_work=self.dt*self.target_pa*well_in
        event_work=float(p@self.event_volume_m3)
        ledger=[storage_change,be_diss,work_on_solid,internal_diss,boundary_diss,-boundary_work,-event_work]
        escale=max(1.,*(abs(v) for v in ledger))
        eerror=math.fsum(ledger)
        if min(be_diss,internal_diss,boundary_diss)<0 or abs(eerror)>1e-5+2e-8*escale:
            raise RuntimeError(f"fluid energy balance failed: {eerror=}")
        diag={"dt_s":self.dt,"cell_balance_max_m3":cell_error,"global_balance_m3":global_error,
              "compartments":groups,"mechanical_dilation_change_m3":float(delta_dilation.sum()),
              "well_volume_m3":float(self.dt*well_in),"event_volume_m3":float(self.event_volume_m3.sum()),
              "fluid_storage_change_j":storage_change,"backward_euler_dissipation_j":be_diss,
              "pressure_work_on_solid_j":work_on_solid,"internal_darcy_dissipation_j":internal_diss,
              "well_dissipation_j":boundary_diss,"well_pressure_work_j":boundary_work,"event_pressure_work_j":event_work,
              "fluid_energy_error_j":eerror,"absolute_pressure_min_pa":float(np.min(m.absolute_p0+p)),
              "pressure_increment_range_pa":[float(np.min(p)),float(np.max(p))],
              "minimum_deformed_volume_m3":float(np.min(cell_vol))}
        return WaterState(p,dilation,m.topology_id),diag
