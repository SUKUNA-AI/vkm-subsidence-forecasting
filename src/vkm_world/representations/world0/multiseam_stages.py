"""Joint tensor Maxwell trajectory with explicit layered elasticity and staged KrII support."""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np
from scipy.sparse import coo_matrix
from scipy.sparse.linalg import splu

from .multiseam import _quadrature, elastic_matrix, quadrature_data


def deviator(strain):
    """Engineering strain Voigt order is xx, yy, zz, xy, yz, xz."""
    result = strain.copy()
    result[..., :3] -= result[..., :3].mean(axis=-1, keepdims=True)
    return result


def _dev_stress(strain):
    stress = deviator(strain)
    stress[..., :3] *= 2
    return stress


@dataclass(frozen=True)
class StageState:
    time_s: float
    displacement_m: np.ndarray
    creep: np.ndarray
    support: np.ndarray
    fill: np.ndarray
    fill_ref: np.ndarray
    event_cursor: int

    def __post_init__(self):
        # An immutable bytes owner prevents callers from re-enabling WRITEABLE.
        # Copying also disconnects accepted history from mutable trial arrays.
        for name in ("displacement_m", "creep", "support", "fill", "fill_ref"):
            value = np.asarray(getattr(self, name))
            if value.dtype.hasobject:
                raise ValueError("StageState arrays must not contain Python objects")
            frozen = np.frombuffer(value.tobytes(order="C"), dtype=value.dtype).reshape(value.shape)
            object.__setattr__(self, name, frozen)

    def __deepcopy__(self, memo):
        # All fields are immutable; deepcopy must not create writable ndarrays.
        memo[id(self)] = self
        return self


class TensorStages:
    """Geometrically linear mixture with solid KrII and compression-only fill.

    The KrII skeleton obeys a deviatoric Maxwell law. Other layers are elastic.
    Mining is a scenario change to the homogenized support, not a void mesh.
    Fill contributes mass and only an axial compression response after birth.
    """

    def __init__(self, mesh, *, young_pa, poisson, density, rock_cells, patch_index,
                 excavation_fraction, events, viscosity_pa_s, fill_young_pa, fill_density, fill_fraction,
                 gravity=9.80665, creep_enabled=None):
        if mesh.volumetric_average:
            raise ValueError("tensor stages use full quadrature, not an averaged volumetric tangent")
        material = elastic_matrix(np.asarray(young_pa, dtype=float), np.asarray(poisson, dtype=float))
        if material.shape != mesh.D.shape or not np.array_equal(material, mesh.D):
            raise ValueError("material field differs from the assembled ElasticMesh stiffness")
        self.mesh = mesh
        self.rock = np.flatnonzero(rock_cells)
        self.patch_index = np.asarray(patch_index, dtype=int)[self.rock]
        self.excavation = np.asarray(excavation_fraction, dtype=float)
        self.events = sorted(events, key=lambda item: item[0])
        if (not len(self.rock) or not np.isfinite(self.excavation).all()
                or np.any((self.excavation < 0) | (self.excavation >= 1))):
            raise ValueError("invalid active KrII support fractions")
        if np.any((self.patch_index < 0) | (self.patch_index >= len(self.excavation))):
            raise ValueError("active KrII cells lack a parent patch")
        seen = {}
        for t, patch, kind in self.events:
            if not np.isfinite(t) or t <= 0 or patch < 0 or patch >= len(self.excavation):
                raise ValueError("invalid event time or parent")
            prior = seen.get(patch)
            if ((kind == "excavate" and prior is None and self.excavation[patch] > 0)
                    or (kind == "backfill" and prior is not None and prior[1] == "excavate" and t > prior[0])):
                seen[patch] = (t, kind)
            else:
                raise ValueError("invalid event transition")
        self.bulk = np.asarray(young_pa, float) / (3 * (1 - 2 * np.asarray(poisson, float)))
        self.shear = np.asarray(young_pa, float) / (2 * (1 + np.asarray(poisson, float)))
        self.density = np.asarray(density, float)
        if any(a.shape != (len(mesh.cells),) or not np.isfinite(a).all() or np.any(a <= 0)
               for a in (self.bulk, self.shear, self.density)):
            raise ValueError("invalid bulk, shear or density field")
        constants = [fill_young_pa, fill_density, fill_fraction, gravity]
        if not all(np.isfinite(x) and x > 0 for x in constants) or fill_fraction > 1:
            raise ValueError("invalid scenario constants")
        viscosity = np.asarray(viscosity_pa_s, dtype=float)
        if viscosity.ndim == 0:
            viscosity = np.full(len(self.rock), float(viscosity))
        enabled = np.ones(len(self.rock), dtype=bool) if creep_enabled is None else np.asarray(creep_enabled)
        if enabled.shape != (len(self.rock),) or enabled.dtype.kind != "b":
            raise ValueError("creep_enabled must be one boolean per support cell")
        if (viscosity.shape != (len(self.rock),) or np.isinf(viscosity).any()
                or not np.isfinite(viscosity[enabled]).all()
                or np.any(viscosity[np.isfinite(viscosity)] <= 0)):
            raise ValueError("viscosity must be positive and finite on creep-enabled cells")
        self.eta = viscosity.copy()
        self.creep_enabled = enabled.copy()
        self.creep_enabled.flags.writeable = False
        self.fill_young = fill_young_pa
        self.fill_density = fill_density
        self.fill_fraction = fill_fraction
        self.gravity = gravity
        coordinates = mesh.xyz[mesh.cells]
        self.mass_weights_m3 = np.zeros(mesh.cells.shape)
        for gradient, shape, weight in _quadrature(mesh.cells.shape[1]):
            det = np.linalg.det(coordinates.swapaxes(-2, -1) @ gradient)
            self.mass_weights_m3 += (det * weight)[:, None] * shape
        if not np.allclose(self.mass_weights_m3.sum(axis=1), mesh.volume_m3, rtol=1e-12, atol=1e-9):
            raise ValueError("inconsistent body-force volume")
        quad = list(quadrature_data(coordinates[self.rock]))
        self.b = np.stack([item[0] for item in quad], axis=1)
        self.weights = np.stack([item[1] for item in quad], axis=1)
        self.rock_dofs = mesh.dofs[self.rock]
        width = mesh.dofs.shape[1]
        self.bulk_basis = np.zeros((len(self.rock), width, width))
        self.shear_basis = np.zeros_like(self.bulk_basis)
        for b, w in zip(self.b.transpose(1, 0, 2, 3), self.weights.T):
            div = b[:, :3].sum(axis=1)
            dev = b.copy()
            dev[:, :3] -= div[:, None, :] / 3
            dev[:, 3:] /= np.sqrt(2)
            self.bulk_basis += div[:, :, None] * div[:, None, :] * w[:, None, None]
            self.shear_basis += (2 * w[:, None, None]) * (dev.swapaxes(-1, -2) @ dev)
        self.bz = self.b[:, :, 2, :]
        self.rock_k = np.empty_like(self.bulk_basis)
        self.base_k = mesh.ke.copy()
        self.base_k[self.rock] = 0
        self.base_matrix = mesh.assemble(self.base_k)
        self._cache = None

    def creep_ratio(self, dt_s):
        """Return zero creep rate on explicitly elastic support domains."""
        if not np.isfinite(dt_s) or dt_s < 0:
            raise ValueError("invalid constitutive time increment")
        ratio = np.zeros(len(self.rock))
        np.divide(dt_s * self.shear[self.rock], self.eta, out=ratio, where=self.creep_enabled)
        return ratio

    def element_body_force(self, support, fill):
        rho = self.density.copy()
        rho[self.rock] = rho[self.rock] * support + self.fill_density * fill
        force = np.zeros_like(self.mesh.dofs, dtype=float)
        force[:, 2::3] = -self.gravity * rho[:, None] * self.mass_weights_m3
        return force

    def _body_force(self, support, fill):
        return np.bincount(self.mesh.dofs.ravel(), self.element_body_force(support, fill).ravel(),
                           minlength=self.mesh.n_dof)

    def element_residual(self, state, diag):
        local = (self.mesh.ke @ state.displacement_m[self.mesh.dofs, None])[:, :, 0]
        total = diag["kr2_stress_pa"].copy()
        total[..., 2] += diag["fill_stress_pa"]
        local[self.rock] = np.einsum("egvi,egv,eg->ei", self.b, total, self.weights)
        return local - self.element_body_force(state.support, state.fill)

    def _assemble_rock(self, values):
        width = self.rock_dofs.shape[1]
        row = np.repeat(self.rock_dofs, width, axis=1).ravel()
        col = np.tile(self.rock_dofs, (1, width)).ravel()
        return coo_matrix((values.ravel(), (row, col)),
                          shape=(self.mesh.n_dof, self.mesh.n_dof)).tocsr()

    def _strain(self, u):
        return np.einsum("egvi,ei->egv", self.b, u[self.rock_dofs])

    def _equilibrium(self, previous, time_s, support, fill, fill_ref, event_cursor):
        mesh = self.mesh
        dt = time_s - previous.time_s
        if not np.isfinite(dt) or dt < 0:
            raise ValueError("invalid step")
        local_g = self.shear[self.rock]
        ratio = self.creep_ratio(dt)
        bulk = self.bulk[self.rock] * support
        shear = local_g * support / (1 + ratio)
        base = self._body_force(support, fill)
        creep_stress = shear[:, None, None] * _dev_stress(previous.creep)
        memory_force = np.einsum("egvi,egv,eg->ei", self.b, creep_stress, self.weights)
        rhs_rock = np.bincount(self.rock_dofs.ravel(), memory_force.ravel(), minlength=mesh.n_dof)
        rhs = base + rhs_rock
        installed = fill[:, None] > 0
        active = installed & (self._strain(previous.displacement_m)[..., 2] < fill_ref)
        fill_k = fill[:, None] * self.fill_young
        free = mesh.free_dofs
        for _ in range(24):
            key = (dt, bulk.tobytes(), shear.tobytes(), fill_k.tobytes(),
                   active.tobytes(), free.tobytes())
            if self._cache is None or key != self._cache[0]:
                self.rock_k[:] = (bulk[:, None, None] * self.bulk_basis
                                  + shear[:, None, None] * self.shear_basis)
                self.rock_k += np.einsum("egi,egj,eg->eij", self.bz, self.bz,
                                         self.weights * fill_k * active, optimize=True)
                matrix = self.base_matrix + self._assemble_rock(self.rock_k)
                free_matrix = matrix[free][:, free].tocsc()
                scale = 1 / np.sqrt(free_matrix.diagonal())
                equilibrated = free_matrix.multiply(scale[:, None]).multiply(scale[None, :]).tocsc()
                factor = splu(equilibrated, permc_spec="MMD_AT_PLUS_A", diag_pivot_thresh=0)
                self._cache = (key, matrix, scale, factor)
            _, matrix, scale, factor = self._cache
            ref_force = np.einsum("egi,eg->ei", self.bz, self.weights * fill_k * active * fill_ref)
            rhs_fill = np.bincount(self.rock_dofs.ravel(), ref_force.ravel(), minlength=mesh.n_dof)
            applied = rhs + rhs_fill
            u = np.zeros(mesh.n_dof)
            u[free] = scale * factor.solve(scale * applied[free])
            strain = self._strain(u)
            next_active = installed & (strain[..., 2] < fill_ref - 1e-13)
            if np.array_equal(active, next_active):
                break
            active = next_active
        else:
            raise RuntimeError("fill active set failed to converge; accepted state unchanged")
        residual = matrix @ u - applied
        force_scale = max(1.0, float(np.linalg.norm(base, ord=np.inf)))
        residual_relative = float(np.linalg.norm(residual[free], ord=np.inf) / force_scale)
        if not np.isfinite(residual_relative) or residual_relative > 1e-8:
            raise RuntimeError(f"joint residual {residual_relative:.4g} exceeds declared tolerance")
        creep = (previous.creep + ratio[:, None, None] * deviator(strain)) / (1 + ratio[:, None, None])
        delta_creep = creep - previous.creep
        elastic = strain - creep
        stress = (support * local_g)[:, None, None] * _dev_stress(elastic)
        pressure = bulk[:, None] * elastic[..., :3].sum(axis=-1)
        stress[..., :3] += pressure[..., None]
        fill_stress = fill_k * np.minimum(strain[..., 2] - fill_ref, 0.0)
        dissipation = float(np.einsum("egv,egv,eg->", stress, delta_creep, self.weights))
        if dissipation < -1e-7:
            raise RuntimeError("negative creep dissipation")
        fixed = mesh.fixed_dofs
        reaction = np.bincount(fixed % 3, weights=residual[fixed], minlength=3)
        body = base.reshape(-1, 3).sum(axis=0)
        balance = float(np.linalg.norm(reaction + body, ord=np.inf) / max(1.0, np.linalg.norm(body, ord=np.inf)))
        if balance > 1e-8:
            raise RuntimeError(f"global reaction mismatch {balance:.4g}")
        self.last_system = (matrix, applied)
        state = StageState(time_s, u, creep, support.copy(), fill.copy(), fill_ref.copy(), event_cursor)
        diagnostics = {"free_relative_residual": residual_relative, "global_relative_balance": balance,
                       "reaction_n": reaction, "body_force_n": body, "creep_dissipation_j": dissipation,
                       "kr2_strain": strain, "kr2_stress_pa": stress, "fill_stress_pa": fill_stress,
                       "contact_points": int(np.count_nonzero(active))}
        return state, diagnostics

    def initial(self):
        shape = self.weights.shape
        raw = StageState(0.0, np.zeros(self.mesh.n_dof), np.zeros((*shape, 6)),
                         np.ones(shape[0]), np.zeros(shape[0]), np.zeros(shape), 0)
        return self._equilibrium(raw, 0.0, raw.support, raw.fill, raw.fill_ref, 0)

    def advance(self, state, target):
        if target <= state.time_s or (state.event_cursor < len(self.events) and target > self.events[state.event_cursor][0]):
            raise ValueError("step must end before or on the next event")
        return self._equilibrium(state, target, state.support, state.fill, state.fill_ref, state.event_cursor)

    def apply_events(self, state):
        index = state.event_cursor
        if index >= len(self.events) or self.events[index][0] != state.time_s:
            raise ValueError("event time differs from accepted state")
        support, fill, refs = state.support.copy(), state.fill.copy(), state.fill_ref.copy()
        strain = self._strain(state.displacement_m)
        while index < len(self.events) and self.events[index][0] == state.time_s:
            _, patch, kind = self.events[index]
            mask = self.patch_index == patch
            if kind == "excavate":
                support[mask] = 1 - self.excavation[patch]
            else:
                fill[mask] = self.fill_fraction * self.excavation[patch]
                refs[mask] = strain[mask, :, 2]
            index += 1
        return self._equilibrium(state, state.time_s, support, fill, refs, index)
