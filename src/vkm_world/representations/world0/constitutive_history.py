"""Grouped tensor-material successor to multiseam_stages on its existing FE carrier.

SI, small strains; engineering Voigt xx, yy, zz, xy, yz, xz at the FE boundary.
Material trials use Mandel tensors. All groups share one equilibrium and pressure
solve; no material memory is committed before both balances and regime checks.
The finite-duration mechanics of excavation are deliberately not inferred here:
instantaneous C4 updates retain an OPEN event-energy recipient.
"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
import time
import numpy as np
from scipy.sparse import coo_matrix
from scipy.sparse.linalg import splu

from .multiseam import _quadrature, quadrature_data
from .constitutive import (ConstitutiveState, strain_to_mandel,
    stress_to_engineering, tangent_to_engineering)
from .hydromechanics import WaterState


def digest(value):
    return hashlib.sha256(np.ascontiguousarray(value).tobytes()).hexdigest()


def frozen(value):
    array = np.ascontiguousarray(value)
    return np.frombuffer(array.tobytes(),dtype=array.dtype).reshape(array.shape)


@dataclass(frozen=True)
class MaterialGroup:
    identifier: str
    cells: np.ndarray
    model: object
    qualifier: str

    def __post_init__(self):
        cells = np.asarray(self.cells)
        if cells.ndim != 1 or cells.dtype.kind not in 'iu' or not len(cells):
            raise ValueError('material group requires nonempty integer cell ids')
        if len(np.unique(cells)) != len(cells) or not self.identifier or not self.qualifier:
            raise ValueError('duplicate cells or absent material provenance/qualifier')
        object.__setattr__(self, 'cells', frozen(cells))


@dataclass(frozen=True)
class Cohort:
    identifier: str
    birth_s: float
    cells: np.ndarray  # indices into the changing domain, not whole mesh ids
    reference_strain: np.ndarray
    stiffness_pa: np.ndarray
    mass_kg: np.ndarray
    capacity_reference_fraction: np.ndarray

    def __post_init__(self):
        for name in ('cells', 'reference_strain', 'stiffness_pa', 'mass_kg', 'capacity_reference_fraction'):
            object.__setattr__(self, name, frozen(getattr(self, name)))


@dataclass(frozen=True)
class HistoryState:
    time_s: float
    displacement_m: np.ndarray
    materials: tuple
    support: np.ndarray
    cohorts: tuple
    event_cursor: int
    water: object
    identity: str

    def __post_init__(self):
        for name in ('displacement_m', 'support'):
            object.__setattr__(self, name, frozen(getattr(self, name)))


class StepFailure(RuntimeError):
    """Rejected trial retains the last accepted state and the actual candidate."""
    def __init__(self, reason, previous, displacement=None, diagnostics=None,candidate_state=None):
        super().__init__(reason)
        self.previous = previous
        self.displacement = None if displacement is None else frozen(displacement)
        self.diagnostics = diagnostics or {}
        self.candidate_state = candidate_state


class ConstitutiveHistory:
    """Existing conforming elasticity plus grouped constitutive residual/tangent.

    Events are source rows with time_s, layer_index, patch_index, action,
    increment_volume_fraction, layer_patch_reference_volume_m3 and event_id.
    Body force, birth aperture/mass, unilateral axial fill and hydraulic Schur
    equations are preserved from the existing source-driven history integrator.
    A supplied linear_solver(K, rhs, affine, old_u) may reuse native backends.
    """
    def __init__(self, mesh, groups, *, density, gravity, support_cells,
                 layer_id, patch_id, event_rows=(), event_metadata=None,
                 water=None, world_identity, linear_solver=None,
                 force_atol_n=.02, force_rtol=2e-10, strain_limit=.05,
                 maximum_iterations=30):
        if mesh.volumetric_average:
            raise ValueError('full quadrature required by material memory')
        self.mesh, self.groups, self.water = mesh, tuple(groups), water
        self.linear_solver = linear_solver
        self.free = mesh.free_dofs
        self.density = frozen(np.asarray(density, float))
        self.layer_id, self.patch_id = frozen(np.asarray(layer_id)), frozen(np.asarray(patch_id))
        n = len(mesh.cells)
        if any(x.shape != (n,) for x in (self.density, self.layer_id, self.patch_id)):
            raise ValueError('density/layer/patch arrays must cover every cell')
        if not np.isfinite(self.density).all() or np.min(self.density) <= 0 or not np.isfinite(gravity) or gravity <= 0:
            raise ValueError('positive density and gravity required')
        self.gravity = gravity
        support_cells = np.asarray(support_cells)
        if support_cells.dtype.kind == 'b':
            if support_cells.shape != (n,): raise ValueError('support mask shape')
            support_cells = np.flatnonzero(support_cells)
        if support_cells.ndim != 1 or support_cells.dtype.kind not in 'iu':
            raise ValueError('integer support ids required')
        grouped = np.concatenate([g.cells for g in self.groups]) if self.groups else np.empty(0, int)
        if len(np.unique(grouped)) != len(grouped): raise ValueError('overlapping constitutive groups')
        self.domain = np.union1d(grouped, support_cells)
        if not len(self.domain) or self.domain.min() < 0 or self.domain.max() >= n:
            raise ValueError('changing cells outside mesh')
        if len(set(g.identifier for g in self.groups)) != len(self.groups):
            raise ValueError('duplicate material group identifiers')
        self.group_local = tuple(np.searchsorted(self.domain, g.cells) for g in self.groups)
        self.event_allowed = np.isin(self.domain, support_cells)
        self.dofs = mesh.dofs[self.domain]
        self.volume = mesh.volume_m3[self.domain]
        quad = list(quadrature_data(mesh.xyz[mesh.cells[self.domain]]))
        self.b = np.stack([q[0] for q in quad], axis=1)
        self.weights = np.stack([q[1] for q in quad], axis=1)
        self.bz = self.b[:, :, 2]
        self.ng = self.weights.shape[1]
        self.base_matrix = mesh.matrix - self.assemble(mesh.ke[self.domain])
        self.mass_weights = np.empty(mesh.cells.shape, float)
        for start in range(0, n, 2048):
            take = slice(start, start+2048)
            xyz = mesh.xyz[mesh.cells[take]]
            local = np.zeros(mesh.cells[take].shape)
            for grad, shape, weight in _quadrature(mesh.cells.shape[1]):
                det = np.linalg.det(xyz.swapaxes(-2, -1) @ grad)
                local += det[:, None]*weight*shape
            self.mass_weights[take] = local
        np.testing.assert_allclose(self.mass_weights.sum(axis=1), mesh.volume_m3, rtol=1e-12, atol=1e-7)
        self.event_metadata = event_metadata or {}
        self.by_time = {}
        seen = set()
        for row in event_rows:
            key = (row['event_id'], row['layer_index'], row['patch_index'], row['action'])
            if key in seen or row['action'] not in ('excavate', 'backfill'):
                raise ValueError('duplicate/unknown source event')
            if not np.isfinite(row['time_s']) or row['time_s'] <= 0 or not 0 <= row['increment_volume_fraction'] <= 1:
                raise ValueError('invalid source event increment/time')
            seen.add(key)
            self.by_time.setdefault(float(row['time_s']), []).append(dict(row))
        self.event_times = sorted(self.by_time)
        self.force_atol_n, self.force_rtol = force_atol_n, force_rtol
        self.strain_limit, self.maximum_iterations = strain_limit, maximum_iterations
        if not np.isfinite([force_atol_n,force_rtol,strain_limit]).all() or min(force_atol_n,force_rtol,strain_limit)<=0 or type(maximum_iterations) is not int or maximum_iterations<1:
            raise ValueError('positive finite tolerances and iteration limit required')
        self._initial_bindings=self._current_bindings()
        payload = dict(world=world_identity, xyz=digest(mesh.xyz), cells=digest(mesh.cells),
            integrator_sha256=hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
            elastic=digest(mesh.D), density=digest(self.density), gravity=gravity,
            groups=[dict(id=g.identifier,cells=digest(g.cells),model=g.model.signature,qualifier=g.qualifier) for g in self.groups],
            domain=digest(self.domain), event_allowed=digest(self.event_allowed), fixed=digest(mesh.fixed_dofs),
            events=event_rows, event_metadata=event_metadata,
            water=None if water is None else water.topology_id,
            numerical=dict(force_atol_n=force_atol_n,force_rtol=force_rtol,strain_limit=strain_limit),
            actual_bindings=self._initial_bindings)
        self._identity_json=json.dumps(payload,sort_keys=True)
        self.identity = hashlib.sha256(self._identity_json.encode()).hexdigest()
        self._factor_cache = None
        self.last_blocks = None
        self.last_diagnostics = None

    @property
    def identity_payload(self):
        return json.loads(self._identity_json)

    def _current_bindings(self):
        def sparse_binding(a):return [digest(a.data),digest(a.indices),digest(a.indptr),a.shape]
        values=dict(xyz=digest(self.mesh.xyz),cells=digest(self.mesh.cells),elastic=digest(self.mesh.D),
            matrix=sparse_binding(self.mesh.matrix),fixed=digest(self.mesh.fixed_dofs),free=digest(self.free),
            mesh_dofs=digest(self.mesh.dofs),mesh_volume=digest(self.mesh.volume_m3),mesh_ke=digest(self.mesh.ke),
            mesh_free=digest(self.mesh.free_dofs),n_dof=self.mesh.n_dof,ng=self.ng,
            b=digest(self.b),weights=digest(self.weights),bz=digest(self.bz),dofs=digest(self.dofs),
            base_matrix=sparse_binding(self.base_matrix),mass_weights=digest(self.mass_weights),volume=digest(self.volume),
            group_local=[digest(ids) for ids in self.group_local],
            domain=digest(self.domain),event_allowed=digest(self.event_allowed),density=digest(self.density),
            layer=digest(self.layer_id),patch=digest(self.patch_id),gravity=self.gravity,
            groups=[(g.identifier,digest(g.cells),g.model.signature,g.qualifier) for g in self.groups],
            events=hashlib.sha256(json.dumps([self.by_time,self.event_metadata,self.event_times],sort_keys=True).encode()).hexdigest(),
            numerical=[self.force_atol_n,self.force_rtol,self.strain_limit,self.maximum_iterations])
        if self.water is not None:
            w=self.water
            values['water']=dict(B=sparse_binding(w.B),Bt=sparse_binding(w.Bt),L=sparse_binding(w.L),
                L_internal=sparse_binding(w.L_internal),storage=digest(w.storage),
                volume=digest(w.volume),alpha=digest(w.alpha),p0=digest(w.absolute_p0),
                reference=digest(w.reference_displacement_free),reference_dilation=digest(w.reference_dilation_m3),
                edge_a=digest(w.a),edge_b=digest(w.b),edge_trans=digest(w.trans),compartment=list(w.compartment),
                well=[w.well,w.well_t],topology=w.topology_id)
        return values

    def assemble(self, local):
        width = self.dofs.shape[1]
        row = np.repeat(self.dofs, width, axis=1).ravel()
        col = np.tile(self.dofs, (1, width)).ravel()
        return coo_matrix((local.ravel(), (row, col)), shape=(self.mesh.n_dof, self.mesh.n_dof)).tocsr()

    def strain(self, u):
        return np.einsum('egvi,ei->egv', self.b, u[self.dofs], optimize=True)

    def scatter(self, local):
        return np.bincount(self.dofs.ravel(), local.ravel(), minlength=self.mesh.n_dof)

    def body_force(self, support, cohorts):
        mass_density = self.density.copy()
        mass_density[self.domain] *= support
        for c in cohorts:
            np.add.at(mass_density, self.domain[c.cells], c.mass_kg/self.volume[c.cells])
        local = np.zeros(self.mesh.dofs.shape)
        local[:, 2::3] = -self.gravity*mass_density[:, None]*self.mass_weights
        return np.bincount(self.mesh.dofs.ravel(), local.ravel(), minlength=self.mesh.n_dof)

    def response(self, previous, u, dt, support, cohorts, *, tangent=True):
        strain = self.strain(u)
        stress = np.einsum('eij,egj->egi', self.mesh.D[self.domain], strain, optimize=True)
        if tangent:
            constitutive = np.broadcast_to(self.mesh.D[self.domain, None], strain.shape[:-1]+(6,6)).copy()
        trials = []
        for group, local, old in zip(self.groups, self.group_local, previous.materials, strict=True):
            mandel = strain_to_mandel(strain[local])
            trial = group.model.instantaneous(old, mandel) if dt == 0 else group.model.trial(old, mandel, dt)
            stress[local] = stress_to_engineering(trial.stress)
            if tangent: constitutive[local] = tangent_to_engineering(trial.tangent)
            trials.append(trial)
        stress *= support[:, None, None]
        if tangent: constitutive *= support[:, None, None, None]
        fill_stress = np.zeros(self.weights.shape)
        fill_tangent = np.zeros(self.weights.shape)
        active = []
        for c in cohorts:
            gap = strain[c.cells, :, 2]-c.reference_strain
            enabled = gap < 0
            active.append(enabled)
            np.add.at(fill_stress, c.cells, c.stiffness_pa*np.minimum(gap, 0.))
            np.add.at(fill_tangent, c.cells, c.stiffness_pa*enabled)
        total = stress.copy()
        total[..., 2] += fill_stress
        force = self.base_matrix@u + self.scatter(np.einsum('egvi,egv,eg->ei',self.b,total,self.weights,optimize=True))
        matrix = None
        if tangent:
            local_k = np.einsum('egvi,egvw,egwj,eg->eij', self.b, constitutive, self.b, self.weights, optimize=True)
            local_k += np.einsum('egi,egj,eg->eij',self.bz,self.bz,self.weights*fill_tangent,optimize=True)
            matrix = self.base_matrix + self.assemble(local_k)
        return dict(strain=strain, stress=stress, fill_stress=fill_stress, trials=tuple(trials),
                    force=force, matrix=matrix, active=active)

    def _linear(self, matrix, rhs, fluid, previous):
        if self.linear_solver is not None:
            return self.linear_solver(matrix, rhs, fluid, previous.displacement_m[self.free])
        key = (digest(matrix.data),digest(matrix.indices),digest(matrix.indptr))
        if self._factor_cache is None or self._factor_cache[0] != key:
            # The obsolete sparse factor is several GiB on the wholemine mesh.
            # Release it before allocating its replacement, not after splu.
            self._factor_cache = None
            if np.min(matrix.diagonal()) <= 0: raise ValueError('nonpositive tangent diagonal')
            scale = 1/np.sqrt(matrix.diagonal())
            lu = splu(matrix.multiply(scale[:,None]).multiply(scale[None,:]).tocsc(),
                      permc_spec='MMD_AT_PLUS_A',diag_pivot_thresh=0)
            self._factor_cache = (key,scale,lu)
        _,scale,lu = self._factor_cache
        solve = lambda v: scale*lu.solve(scale*v)
        if fluid is None: return solve(rhs), None
        skew=matrix-matrix.T
        symmetric=np.max(np.abs(skew.data),initial=0)<=1e-12*max(1.,np.max(np.abs(matrix.data)))
        trial = self.water.trial(previous.water,fluid.dt,solve,matrix.diagonal(),rhs,
                                 well_target_pa=fluid.target_pa,symmetric_tangent=symmetric)
        return trial.displacement_free,trial.state.pressure_pa

    def initial(self, seed_displacement=None):
        """Own instantaneous gravity state, explicit virgin material memory hypothesis."""
        u = np.zeros(self.mesh.n_dof) if seed_displacement is None else np.asarray(seed_displacement,float)
        materials = tuple(g.model.initial_state((len(g.cells),self.ng)) for g in self.groups)
        old = HistoryState(0.,u,materials,np.ones(len(self.domain)),(),0,
                           None if self.water is None else self.water.initial(),self.identity)
        return self.step(old,0.)

    def _check_state(self, state):
        if self._current_bindings()!=self._initial_bindings:
            raise ValueError('physical operator/input changed; new representation and replay required')
        if state.identity != self.identity or len(state.materials) != len(self.groups):
            raise ValueError('state is bound to a different physical world/representation')
        if state.support.shape != self.volume.shape or np.any((state.support < 0)|(state.support > 1)):
            raise ValueError('invalid support fractions')
        for g,s in zip(self.groups,state.materials,strict=True):
            if s.signature != g.model.signature or s.time_s != state.time_s:
                raise ValueError('material identity/time differs from joint state')
        self._check_calendar_state(state)

    def _check_calendar_state(self,state):
        cursor=state.event_cursor
        if not np.isfinite(state.time_s) or state.time_s<0 or not isinstance(cursor,(int,np.integer)) or isinstance(cursor,bool):
            raise ValueError('invalid source calendar time/cursor')
        left=int(np.searchsorted(self.event_times,state.time_s,side='left'))
        right=int(np.searchsorted(self.event_times,state.time_s,side='right'))
        if cursor not in (left,right):raise ValueError('event cursor skips or reorders source calendar')
        expected=np.ones(len(self.domain));expected_fill=set()
        for t in self.event_times[:cursor]:
            for row in self.by_time[t]:
                if row['action']=='backfill':expected_fill.add(row['event_id'])
                else:
                    selected=(self.layer_id[self.domain]==row['layer_index'])&(self.patch_id[self.domain]==row['patch_index'])&self.event_allowed
                    expected[selected]-=row['increment_volume_fraction']
        if not np.allclose(state.support,expected,rtol=0,atol=1e-12):
            raise ValueError('support does not correspond to committed source calendar')
        if {c.identifier for c in state.cohorts}!=expected_fill or len(state.cohorts)!=len(expected_fill):
            raise ValueError('fill births do not correspond to committed source calendar')
        capacity=np.zeros(len(self.domain))
        for c in state.cohorts:np.add.at(capacity,c.cells,c.capacity_reference_fraction)
        if np.any(capacity>1-state.support+1e-12):raise ValueError('backfill exceeds excavated reference capacity')

    def step(self, previous, time_s, *, support=None, cohorts=None, cursor=None):
        self._check_state(previous)
        dt = float(time_s-previous.time_s)
        if not np.isfinite(dt) or dt < 0: raise ValueError('nonnegative history increment required')
        support = previous.support if support is None else np.asarray(support,float)
        if support.shape != previous.support.shape or not np.isfinite(support).all() or np.any((support<0)|(support>1)):
            raise ValueError('support outside reference capacity')
        cohorts = previous.cohorts if cohorts is None else tuple(cohorts)
        cursor = previous.event_cursor if cursor is None else cursor
        fluid = None if self.water is None else self.water.affine(previous.water,dt,well_target_pa=0.)
        body = self.body_force(support,cohorts)
        u = previous.displacement_m.copy()
        start = time.perf_counter()
        candidate_state=None
        try:
            for iteration in range(self.maximum_iterations):
                response = self.response(previous,u,dt,support,cohorts)
                matrix = response['matrix']
                applied = body-response['force']+matrix@u
                free_matrix = matrix[self.free][:,self.free].tocsc()
                self.last_blocks = (free_matrix,applied[self.free],fluid,previous.displacement_m[self.free])
                uf,p = self._linear(*self.last_blocks[:3],previous)
                candidate = np.zeros(self.mesh.n_dof)
                candidate[self.free] = uf
                # Residual uses newly evaluated material stress, never K_tangent*u.
                checked = self.response(previous,candidate,dt,support,cohorts,tangent=False)
                pressure_force = np.zeros(self.mesh.n_dof)
                if p is not None: pressure_force[self.free] = self.water.B@p
                residual = checked['force']-body-pressure_force
                error = float(np.max(np.abs(residual[self.free])))
                scale = max(1.,float(np.max(np.abs(body))),float(np.max(np.abs(checked['force']))),float(np.max(np.abs(pressure_force))))
                u = candidate
                if error <= self.force_atol_n+self.force_rtol*scale:
                    response = checked
                    break
            else:
                candidate_water=None if self.water is None else WaterState(p,
                    self.water.Bt@(u[self.free]-self.water.reference_displacement_free),self.water.topology_id)
                candidate_state=HistoryState(time_s,u,tuple(t.state for t in checked['trials']),support,cohorts,cursor,candidate_water,self.identity)
                raise StepFailure('joint material/contact Newton did not converge',previous,u,
                    dict(force_error_n=error,scale_n=scale),candidate_state=candidate_state)
            candidate_water=None if self.water is None else WaterState(p,
                self.water.Bt@(u[self.free]-self.water.reference_displacement_free),self.water.topology_id)
            candidate_state=HistoryState(time_s,u,tuple(t.state for t in response['trials']),support,cohorts,cursor,candidate_water,self.identity)
            maximum = 0.
            for first in range(0,len(self.mesh.cells),2048):
                take = slice(first,first+2048)
                local = u[self.mesh.dofs[take],None]
                for b,_ in quadrature_data(self.mesh.xyz[self.mesh.cells[take]]):
                    maximum = max(maximum,float(np.max(np.abs(b@local))))
            if maximum > self.strain_limit:
                raise StepFailure('small-strain applicability exceeded',previous,u,
                    dict(maximum_strain=maximum,limit=self.strain_limit),candidate_state=candidate_state)
            reaction = residual[self.mesh.fixed_dofs].reshape(-1,3).sum(axis=0)
            external = (body+pressure_force).reshape(-1,3).sum(axis=0)
            balance = float(np.max(np.abs(reaction+external))/max(1.,np.max(np.abs(external))))
            if balance > 1e-8: raise StepFailure('global reaction balance failed',previous,u,dict(balance=balance),candidate_state=candidate_state)
            water,hydraulic = (None,{}) if fluid is None else fluid.accept(u[self.free],p)
            materials = tuple(g.model.commit(t) for g,t in zip(self.groups,response['trials'],strict=True))
            state = HistoryState(time_s,u,materials,support,cohorts,cursor,water,self.identity)
            self._check_calendar_state(state)
            energy = {}
            for group,local,trial in zip(self.groups,self.group_local,response['trials'],strict=True):
                energy[group.identifier] = {k: float(np.sum(v*self.weights[local]*support[local,None]))
                    for k,v in trial.diagnostics.items() if isinstance(v,np.ndarray) and v.shape == self.weights[local].shape
                    and ('energy' in k or 'work' in k or 'dissipation' in k or 'quadrature_excess' == k)}
            diagnostic = dict(time_s=time_s,dt_s=dt,force_error_n=error,force_scale_n=scale,relative_force_error=error/scale,
                global_reaction_balance_relative=balance,maximum_quadrature_engineering_strain=maximum,
                nonlinear_iterations=iteration+1,hydraulic=hydraulic,material_energy_j=energy,
                cohort_count=len(cohorts),event_cursor=cursor,support_min=float(support.min()),
                elapsed_seconds=time.perf_counter()-start,fill_mass_kg=float(sum(c.mass_kg.sum() for c in cohorts)),
                maximum_memory_by_group={g.identifier:{k:float(np.max(np.abs(v),initial=0)) for k,v in s.memory.items()}
                    for g,s in zip(self.groups,materials,strict=True)})
            self.last_diagnostics = diagnostic
            return state,diagnostic
        except StepFailure:
            raise
        except Exception as exc:
            raise StepFailure(str(exc),previous,u,dict(time_s=time_s,dt_s=dt),candidate_state=candidate_state) from exc

    def advance(self,state,target_s):
        cursor = state.event_cursor
        if target_s <= state.time_s or (cursor<len(self.event_times) and target_s>self.event_times[cursor]):
            raise ValueError('step must stop at or before next unapplied exact event')
        return self.step(state,target_s)

    def apply_events(self,state):
        if state.event_cursor>=len(self.event_times) or self.event_times[state.event_cursor]!=state.time_s:
            raise ValueError('not an unapplied exact event')
        support = state.support.copy()
        cohorts = list(state.cohorts)
        fill = {}
        removed_mass = 0.
        for row in self.by_time[state.time_s]:
            ids = np.flatnonzero((self.layer_id[self.domain]==row['layer_index']) &
                                (self.patch_id[self.domain]==row['patch_index']) & self.event_allowed)
            if not len(ids): raise ValueError('source event lacks allowed support cells')
            if not np.isclose(self.volume[ids].sum(),row['layer_patch_reference_volume_m3'],rtol=1e-12,atol=1e-5):
                raise ValueError('source reference volume differs from actual carrier')
            fraction = row['increment_volume_fraction']
            if row['action']=='excavate':
                support[ids] -= fraction
                removed_mass += fraction*float(self.volume[ids]@self.density[self.domain[ids]])
            else: fill.setdefault(row['event_id'],[]).append((ids,fraction))
        if np.any(support<0) or np.any(support>state.support): raise ValueError('invalid cumulative excavation')
        strain = self.strain(state.displacement_m)
        for identifier,rows in fill.items():
            if any(c.identifier==identifier for c in cohorts): raise ValueError('duplicate fill birth')
            meta = self.event_metadata[identifier]
            ids = np.concatenate([r[0] for r in rows])
            capacity = np.concatenate([np.full(len(i),f*meta['fill_fraction']) for i,f in rows])
            ref = strain[ids,:,2].copy()
            stretch = 1+ref
            if np.min(stretch)<=0: raise ValueError('nonpositive fill birth aperture')
            mass = meta['fill_density_kg_m3']*capacity*np.sum(self.weights[ids]*stretch,axis=1)
            modulus=meta['fill_young_pa']
            if not np.isfinite([modulus,meta['fill_density_kg_m3'],meta['fill_fraction']]).all() or modulus<=0 or meta['fill_density_kg_m3']<=0 or not 0<meta['fill_fraction']<=1:
                raise ValueError('explicit positive birth material parameters required')
            stiffness = modulus*capacity[:,None]/stretch
            cohorts.append(Cohort(identifier,state.time_s,ids,ref,stiffness,mass,capacity))
        installed_capacity=np.zeros(len(self.domain))
        for cohort in cohorts:np.add.at(installed_capacity,cohort.cells,cohort.capacity_reference_fraction)
        if np.any(installed_capacity>1-support+1e-12):raise ValueError('backfill exceeds excavated reference capacity')
        updated,diag = self.step(state,state.time_s,support=support,cohorts=cohorts,cursor=state.event_cursor+1)
        for old,new in zip(state.materials,updated.materials,strict=True):
            for key in old.memory: np.testing.assert_array_equal(old.memory[key],new.memory[key])
        diag.update(event_removed_rock_mass_kg=removed_mass,
            event_added_fill_mass_kg=float(sum(c.mass_kg.sum() for c in updated.cohorts)-sum(c.mass_kg.sum() for c in state.cohorts)),
            event_ids=sorted({r['event_id'] for r in self.by_time[state.time_s]}),
            event_energy_status='OPEN_INSTANTANEOUS_RELEASE_RECIPIENT',
            remaining_material_memory_preserved=True)
        expected = -sum(r.get('body_mass_change_kg') or 0 for r in self.by_time[state.time_s])
        if not np.isclose(removed_mass,expected,rtol=1e-10,atol=1e-3):
            raise StepFailure('source rock mass and actual carrier disagree',state,updated.displacement_m,diag,candidate_state=updated)
        return updated,diag

    def checkpoint(self,path,state,*,accepted=True):
        self._check_state(state)
        path = Path(path)
        arrays = dict(time_s=state.time_s,displacement_m=state.displacement_m,support=state.support,
            event_cursor=state.event_cursor,identity=self.identity,domain_cells=self.domain)
        metadata = dict(schema='vkm.grouped-history-state/1',identity=self.identity,materials=[],cohorts=[])
        for i,(group,s) in enumerate(zip(self.groups,state.materials,strict=True)):
            arrays[f'm{i}_strain']=s.strain; arrays[f'm{i}_stress']=s.stress
            for k,v in s.memory.items(): arrays[f'm{i}_memory_{k}']=v
            metadata['materials'].append(dict(id=group.identifier,signature=s.signature,keys=list(s.memory)))
        for i,c in enumerate(state.cohorts):
            metadata['cohorts'].append(dict(identifier=c.identifier,birth_s=c.birth_s))
            for k in ('cells','reference_strain','stiffness_pa','mass_kg','capacity_reference_fraction'):
                arrays[f'c{i}_{k}']=getattr(c,k)
        if state.water is not None:
            arrays.update(pressure_increment_pa=state.water.pressure_pa,dilation_increment_m3=state.water.dilation_m3)
            metadata['water_topology_id']=state.water.topology_id
        arrays['metadata_json']=json.dumps(metadata,sort_keys=True)
        np.savez_compressed(path,**arrays)
        sidecar=dict(schema='vkm.grouped-history-binding/1',identity=self.identity,accepted=bool(accepted),
            sha256=hashlib.sha256(path.read_bytes()).hexdigest(),dependencies=self.identity_payload)
        Path(str(path)+'.binding.json').write_text(json.dumps(sidecar,ensure_ascii=False,indent=2),encoding='utf-8')

    def reopen(self,path):
        path=Path(path)
        side=json.loads(Path(str(path)+'.binding.json').read_text(encoding='utf-8'))
        if side.get('accepted') is False:raise ValueError('rejected trial cannot be restored as accepted memory')
        if side['identity']!=self.identity or side['sha256']!=hashlib.sha256(path.read_bytes()).hexdigest():
            raise ValueError('checkpoint hash/dependency mismatch')
        with np.load(path,allow_pickle=False) as d:
            meta=json.loads(str(d['metadata_json']))
            if str(d['identity'])!=self.identity or meta['identity']!=self.identity:
                raise ValueError('checkpoint identity mismatch')
            materials=[]
            for i,m in enumerate(meta['materials']):
                materials.append(ConstitutiveState(float(d['time_s']),d[f'm{i}_strain'],d[f'm{i}_stress'],
                    {k:d[f'm{i}_memory_{k}'] for k in m['keys']},m['signature']))
            cohorts=[]
            for i,c in enumerate(meta['cohorts']):
                cohorts.append(Cohort(c['identifier'],c['birth_s'],*(d[f'c{i}_{k}'] for k in
                    ('cells','reference_strain','stiffness_pa','mass_kg','capacity_reference_fraction'))))
            water=None if self.water is None else WaterState(d['pressure_increment_pa'],d['dilation_increment_m3'],meta['water_topology_id'])
            result=HistoryState(float(d['time_s']),d['displacement_m'],tuple(materials),d['support'],tuple(cohorts),
                                int(d['event_cursor']),water,self.identity)
        self._check_state(result)
        return result

    def fields(self,state):
        """All intervals, including explicitly elastic NOT_ACTIVE creep domains."""
        self._check_state(state)
        strain=np.empty((len(self.mesh.cells),self.ng,6))
        for first in range(0,len(self.mesh.cells),2048):
            take=slice(first,first+2048)
            local=state.displacement_m[self.mesh.dofs[take],None]
            for q,(b,_) in enumerate(quadrature_data(self.mesh.xyz[self.mesh.cells[take]])):
                strain[take,q]=(b@local)[...,0]
        stress=np.einsum('eij,egj->egi',self.mesh.D,strain,optimize=True)
        law_id=np.full(len(self.mesh.cells),-1,dtype=int)
        for i,(g,s) in enumerate(zip(self.groups,state.materials,strict=True)):
            stress[g.cells]=stress_to_engineering(s.stress); law_id[g.cells]=i
        support=np.ones(len(self.mesh.cells)); support[self.domain]=state.support
        mixture=stress*support[:,None,None]
        fill=np.zeros((len(self.mesh.cells),self.ng))
        fill_present=np.zeros(len(self.mesh.cells),dtype=bool)
        fill_contact_active=np.zeros((len(self.mesh.cells),self.ng),dtype=bool)
        for c in state.cohorts:
            np.add.at(fill,self.domain[c.cells],c.stiffness_pa*np.minimum(strain[self.domain[c.cells],:,2]-c.reference_strain,0))
            fill_present[self.domain[c.cells]]=True
            np.logical_or.at(fill_contact_active,self.domain[c.cells],strain[self.domain[c.cells],:,2]<c.reference_strain)
        result=dict(node_xyz_m=self.mesh.xyz,connectivity=self.mesh.cells,displacement_m=state.displacement_m.reshape(-1,3),
            strain_engineering=strain,reference_skeleton_stress_engineering_pa=stress,
            mixture_skeleton_stress_engineering_pa=mixture,fill_axial_stress_pa=fill,
            fill_present=fill_present,fill_contact_active=fill_contact_active,
            layer_id=self.layer_id,patch_id=self.patch_id,law_group_id=law_id,
            rheology_assigned=law_id>=0,creep_active=(law_id>=0)&(support>0),support_fraction=support,cell_volume_m3=self.mesh.volume_m3,
            field_convention='engineering Voigt xx yy zz xy yz xz; tensile positive; nominal reference-model skeleton stress; absolute historical effective stress unresolved',
            inactive_process_status='NOT_ACTIVE_NOT_ZERO_OBSERVATION',time_s=state.time_s,identity=self.identity)
        if self.water is not None:
            p=state.water.pressure_pa
            result.update(pressure_increment_pa=p,pressure_absolute_pa=self.water.absolute_p0+p,
                dilation_increment_m3=state.water.dilation_m3,storage_content_increment_m3=self.water.storage*p,
                darcy_edge_flux_m3_s=self.water.trans*(p[self.water.a]-p[self.water.b]),
                darcy_edge_from=self.water.a,darcy_edge_to=self.water.b)
        return result
