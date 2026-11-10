"""Small-strain vector mechanics on conforming HEX8 or WEDGE6 terrain layers.

Engineering-strain Voigt order is xx, yy, zz, xy, yz, xz. The three shear
entries are engineering shear strain; stress entries are physical shear stress.
All coordinates are m, displacements are m, forces N, and stress is Pa.
"""
from __future__ import annotations

import math

import numpy as np
from scipy.sparse import coo_matrix
from scipy.sparse.linalg import splu


VOIGT = ("xx", "yy", "zz", "xy", "yz", "xz")


def elastic_matrix(young_pa, poisson):
    """Three-dimensional, isotropic elasticity in engineering-strain Voigt convention."""
    e, nu = np.broadcast_arrays(np.asarray(young_pa, dtype=float), np.asarray(poisson, dtype=float))
    if np.any(~np.isfinite(e)) or np.any(e <= 0) or np.any(~np.isfinite(nu)) or np.any((nu <= -1) | (nu >= .5)):
        raise ValueError("invalid isotropic elastic material")
    mu = e / (2 * (1 + nu))
    lam = mu * 2 * nu / (1 - 2 * nu)
    d = np.zeros((*e.shape, 6, 6))
    d[..., :3, :3] = lam[..., None, None]
    diagonal = np.arange(6)
    d[..., diagonal, diagonal] += mu[..., None] * np.array([2, 2, 2, 1, 1, 1])
    return d


def _quadrature(node_count):
    g = 1 / math.sqrt(3)
    if node_count == 8:
        signs = np.array([
            (-1, -1, -1), (1, -1, -1), (1, 1, -1), (-1, 1, -1),
            (-1, -1, 1), (1, -1, 1), (1, 1, 1), (-1, 1, 1),
        ])
        for r in (-g, g):
            for s in (-g, g):
                for t in (-g, g):
                    value = 1 + signs * [r, s, t]
                    grad = signs * value[:, [1, 2, 0]] * value[:, [2, 0, 1]] / 8
                    yield grad, value.prod(axis=1) / 8, 1.0
    elif node_count == 6:
        triangle_grad = np.array([[-1, -1], [1, 0], [0, 1]])
        for r, s in ((1/6, 1/6), (2/3, 1/6), (1/6, 2/3)):
            triangle_shape = np.array([1 - r - s, r, s])
            for t in ((1 - g)/2, (1 + g)/2):
                grad = np.empty((6, 3))
                grad[:3, :2] = (1 - t) * triangle_grad
                grad[3:, :2] = t * triangle_grad
                grad[:3, 2], grad[3:, 2] = -triangle_shape, triangle_shape
                yield grad, np.r_[(1 - t) * triangle_shape, t * triangle_shape], 1/12
    else:
        raise ValueError("only HEX8 and WEDGE6 are supported")


def strain_matrix(coordinates, shape_gradient):
    """Return B and signed Jacobian determinant at one quadrature point for every cell."""
    jacobian = coordinates.swapaxes(-2, -1) @ shape_gradient
    det = np.linalg.det(jacobian)
    if np.any(~np.isfinite(det)) or np.any(det <= 0):
        raise ValueError("nonpositive or nonfinite cell Jacobian")
    gradients = shape_gradient @ np.linalg.inv(jacobian)
    b = np.zeros((len(coordinates), 6, 3 * coordinates.shape[1]))
    gx, gy, gz = np.moveaxis(gradients, -1, 0)
    b[:, 0, 0::3] = gx
    b[:, 1, 1::3] = gy
    b[:, 2, 2::3] = gz
    b[:, 3, 0::3], b[:, 3, 1::3] = gy, gx
    b[:, 4, 1::3], b[:, 4, 2::3] = gz, gy
    b[:, 5, 0::3], b[:, 5, 2::3] = gz, gx
    return b, det


def quadrature_data(coordinates):
    """Yield strain operator and positive weighted measure for HEX8 or WEDGE6."""
    for gradient, _, weight in _quadrature(coordinates.shape[1]):
        b, det = strain_matrix(coordinates, gradient)
        yield b, det * weight


def element_data(coordinates, material, *, volumetric_average=False):
    """Cell stiffness, volume, average strain operator, and unit ezz equivalent forces."""
    count, node_count, _ = coordinates.shape
    width = 3 * node_count
    k = np.zeros((count, width, width))
    integrated_b = np.zeros((count, 6, width))
    volume = np.zeros(count)
    quadrature = list(quadrature_data(coordinates))
    for b, measure in quadrature:
        integrated_b += b * measure[:, None, None]
        volume += measure
    average_b = integrated_b / volume[:, None, None]
    average_trace = average_b[:, :3].sum(axis=1)
    for b, measure in quadrature:
        if volumetric_average:
            # Keep deviatoric strain at every integration point, average dilatation.
            b[:, :3] += (average_trace - b[:, :3].sum(axis=1))[:, None, :] / 3
        k += (b.swapaxes(-2, -1) @ (material @ b)) * measure[:, None, None]
    unit_force = (integrated_b.swapaxes(-2, -1) @ material[:, :, 2, None])[:, :, 0]
    return (k + k.swapaxes(-2, -1)) / 2, volume, average_b, unit_force


class ElasticMesh:
    """Conforming mesh with homogeneous clamps and reusable elastic factorization."""

    def __init__(self, xyz_m, connectivity, young_pa, poisson, fixed_nodes, *, volumetric_average=False):
        self.xyz = np.asarray(xyz_m, dtype=float)
        self.cells = np.asarray(connectivity, dtype=np.int32)
        if self.xyz.ndim != 2 or self.xyz.shape[1] != 3 or np.any(~np.isfinite(self.xyz)):
            raise ValueError("invalid node coordinates")
        if self.cells.ndim != 2 or self.cells.shape[1] not in {6, 8} or not len(self.cells):
            raise ValueError("empty or unsupported connectivity")
        if self.cells.min() < 0 or self.cells.max() >= len(self.xyz):
            raise ValueError("cell vertex is outside mesh")
        if np.unique(self.cells).size != len(self.xyz):
            raise ValueError("mesh contains nodes outside the cell domain")
        self.n_dof = len(self.xyz) * 3
        self.volumetric_average = volumetric_average
        self.dofs = (self.cells[:, :, None] * 3 + np.arange(3, dtype=np.int32)).reshape(len(self.cells), -1)
        self.fixed_dofs = self.node_dofs(fixed_nodes)
        self.free_dofs = np.setdiff1d(np.arange(self.n_dof), self.fixed_dofs)
        if not len(self.free_dofs) or not len(self.fixed_dofs):
            raise ValueError("mesh requires both fixed and free degrees of freedom")
        self.D = elastic_matrix(np.broadcast_to(young_pa, (len(self.cells),)), np.broadcast_to(poisson, (len(self.cells),)))
        width = self.dofs.shape[1]
        self.ke = np.empty((len(self.cells), width, width))
        self.volume_m3 = np.empty(len(self.cells))
        self.average_b = np.empty((len(self.cells), 6, width))
        self.unit_force = np.empty((len(self.cells), width))
        for first in range(0, len(self.cells), 2048):
            take = slice(first, first + 2048)
            self.ke[take], self.volume_m3[take], self.average_b[take], self.unit_force[take] = element_data(
                self.xyz[self.cells[take]], self.D[take], volumetric_average=volumetric_average,
            )
        self.matrix = self.assemble(self.ke)
        self._factor = None

    def node_dofs(self, nodes):
        nodes = np.asarray(nodes, dtype=np.int64)
        if len(nodes) and (nodes.min() < 0 or nodes.max() >= len(self.xyz)):
            raise ValueError("fixed node is outside the mesh")
        return np.unique((nodes[:, None] * 3 + np.arange(3)).ravel())

    def assemble(self, elements):
        width = self.dofs.shape[1]
        values = elements.reshape(-1)
        row = np.repeat(self.dofs, width, axis=1).ravel()
        column = np.tile(self.dofs, (1, width)).ravel()
        out = coo_matrix((values, (row, column)), shape=(self.n_dof, self.n_dof)).tocsr()
        out.eliminate_zeros()
        return out

    def factor(self, extra_fixed_dofs=()):
        free = np.setdiff1d(self.free_dofs, np.asarray(extra_fixed_dofs, dtype=np.int64))
        # Equilibrate the physical stiffness without changing the reported SI matrix.
        k = self.matrix[free][:, free].tocsc()
        diagonal = k.diagonal()
        if np.any(diagonal <= 0):
            raise ValueError("nonpositive free stiffness diagonal")
        scale = 1 / np.sqrt(diagonal)
        scaled = k.multiply(scale[:, None]).multiply(scale[None, :]).tocsc()
        return free, scale, splu(scaled, permc_spec="MMD_AT_PLUS_A", diag_pivot_thresh=0)

    def eigenstrain_rhs(self, ezz):
        ezz = np.asarray(ezz, dtype=float)
        if ezz.shape != self.volume_m3.shape or np.any(~np.isfinite(ezz)):
            raise ValueError("eigenstrain must be finite with one ezz per cell")
        return np.bincount(
            self.dofs.ravel(), weights=(self.unit_force * ezz[:, None]).ravel(), minlength=self.n_dof,
        )

    def body_force_rhs(self, density_kg_m3, acceleration_m_s2):
        """Integrate body force consistently on the same element shape functions."""
        density = np.broadcast_to(np.asarray(density_kg_m3, dtype=float), (len(self.cells),))
        gravity = np.asarray(acceleration_m_s2, dtype=float)
        if (gravity.shape != (3,) or np.any(~np.isfinite(gravity))
                or np.any(~np.isfinite(density)) or np.any(density <= 0)):
            raise ValueError("invalid acceleration or density for body force")
        mass = np.zeros(self.cells.shape)
        for first in range(0, len(self.cells), 2048):
            take = slice(first, first + 2048)
            coords = self.xyz[self.cells[take]]
            for gradient, shape, weight in _quadrature(coords.shape[1]):
                det = np.linalg.det(coords.swapaxes(-2, -1) @ gradient)
                mass[take] += (det * weight * density[take])[:, None] * shape
        nodal_mass = np.bincount(self.cells.ravel(), weights=mass.ravel(), minlength=len(self.xyz))
        return (nodal_mass[:, None] * gravity).ravel()

    def solve(self, rhs, *, factorization=None):
        rhs = np.asarray(rhs, dtype=float)
        if rhs.shape[0] != self.n_dof or np.any(~np.isfinite(rhs)):
            raise ValueError("invalid global nodal force")
        if factorization is None:
            if self._factor is None:
                self._factor = self.factor()
            factorization = self._factor
        free, scale, lu = factorization
        if rhs.ndim == 2:
            scale = scale[:, None]
        u = np.zeros_like(rhs)
        u[free] = scale * lu.solve(scale * rhs[free])
        return u

    def fields(self, u, ezz):
        strain = (self.average_b @ u[self.dofs, None])[:, :, 0]
        elastic = strain.copy()
        elastic[:, 2] -= ezz
        stress = (self.D @ elastic[:, :, None])[:, :, 0]
        return strain, stress

    def element_forces(self, u, ezz):
        """Integrated residual of each element; nodal values are conjugate to displacement."""
        return (self.ke @ u[self.dofs, None])[:, :, 0] - self.unit_force * ezz[:, None]

    def interface_actions(self, element_forces, lower_cell_mask, interface_nodes, u):
        """Actions on each side of an interior shared trace, including tangent components."""
        upper = ~lower_cell_mask
        size = self.n_dof
        lower_force = np.bincount(self.dofs[lower_cell_mask].ravel(),
                                  weights=element_forces[lower_cell_mask].ravel(), minlength=size)
        upper_force = np.bincount(self.dofs[upper].ravel(),
                                  weights=element_forces[upper].ravel(), minlength=size)
        dofs = (3 * np.asarray(interface_nodes)[:, None] + np.arange(3))
        f_lower, f_upper = lower_force[dofs], upper_force[dofs]
        return f_lower, f_upper, float(np.sum(u[dofs] * (f_lower + f_upper)))


def port_force(local_traction_pa, basis_xyz, area_m2):
    """Convert the three local tractions to their work-conjugate global resultant."""
    basis = np.asarray(basis_xyz, dtype=float)
    traction = np.asarray(local_traction_pa, dtype=float)
    if (basis.shape != (3, 3) or traction.shape != (3,)
            or not np.allclose(basis.T @ basis, np.eye(3), atol=1e-12) or np.linalg.det(basis) <= 0):
        raise ValueError("port needs a three-vector and right-handed orthonormal basis")
    if not math.isfinite(area_m2) or area_m2 <= 0 or not np.all(np.isfinite(traction)):
        raise ValueError("port traction and area must be finite, with positive area")
    return area_m2 * (basis @ traction)
