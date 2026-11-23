"""Bilinear quadrilaterals (Q4) with the B-bar method on a structured column mesh, plane strain, batched in torch.

Mesh convention (the same as the MAPDL/OGS slice meshes of the project): nodes ``(i, j)`` with column ``i = 0..nx``
(chainage ``x``) and row ``j = 0..nz`` from the top (ground surface) to the bottom; elevation ``z`` points up.
Element ``(i, j)`` has corners BL = (i, j+1), BR = (i+1, j+1), TR = (i+1, j), TL = (i, j) (counter-clockwise in x–z).
Every tensor carries a leading batch axis ``B`` (ensemble members that share ``nx, nz``).

Strains and stresses are 4-vectors in Mandel notation ``(xx, zz, yy_out_of_plane, sqrt(2)·xz)``, so the Euclidean
inner product is the tensor one. B-bar: the in-plane volumetric part of B is replaced by its element average
(see :class:`Q4BBar`); this reproduces MAPDL PLANE182 KEYOPT(1)=0 and OGS ``use_b_bar`` in plane strain.
"""
from __future__ import annotations

import math
from dataclasses import dataclass

import torch

SQ2 = math.sqrt(2.0)
_G = 1.0 / math.sqrt(3.0)
# natural coordinates of the element corners BL, BR, TR, TL and of the 2x2 Gauss points (unit weights)
_XI_N = (-1.0, 1.0, 1.0, -1.0)
_ETA_N = (-1.0, -1.0, 1.0, 1.0)
_GP = ((-_G, -_G), (_G, -_G), (_G, _G), (-_G, _G))
MANDEL_M = (1.0, 1.0, 1.0, 0.0)


def _shape_tables(dtype, device):
    N = torch.empty(4, 4, dtype=dtype, device=device)
    dxi = torch.empty(4, 4, dtype=dtype, device=device)
    deta = torch.empty(4, 4, dtype=dtype, device=device)
    for g, (xi, eta) in enumerate(_GP):
        for a in range(4):
            N[g, a] = 0.25 * (1 + xi * _XI_N[a]) * (1 + eta * _ETA_N[a])
            dxi[g, a] = 0.25 * _XI_N[a] * (1 + eta * _ETA_N[a])
            deta[g, a] = 0.25 * _ETA_N[a] * (1 + xi * _XI_N[a])
    return N, dxi, deta


@dataclass
class ColumnMesh:
    """Structured column mesh, batched. ``xs``: (B, nx+1) chainage of node columns; ``z``: (B, nx+1, nz+1)
    node elevations (row 0 = surface); ``zone``: (B, nx, nz) integer zone id of every element."""

    xs: torch.Tensor
    z: torch.Tensor
    zone: torch.Tensor

    @property
    def batch(self) -> int:
        return int(self.z.shape[0])

    @property
    def nx(self) -> int:
        return int(self.z.shape[1]) - 1

    @property
    def nz(self) -> int:
        return int(self.z.shape[2]) - 1

    def element_corners(self):
        """(X, Z) of the 4 corners BL, BR, TR, TL: each (B, nx, nz, 4)."""
        B, nx, nz = self.batch, self.nx, self.nz
        x = self.xs[:, :, None].expand(B, nx + 1, nz + 1)
        z = self.z
        X = torch.stack([x[:, :-1, 1:], x[:, 1:, 1:], x[:, 1:, :-1], x[:, :-1, :-1]], dim=-1)
        Z = torch.stack([z[:, :-1, 1:], z[:, 1:, 1:], z[:, 1:, :-1], z[:, :-1, :-1]], dim=-1)
        return X, Z


class Q4BBar:
    """Geometry operators of a batched structured mesh: B-bar matrices, Gauss weights, gather/scatter."""

    def __init__(self, mesh: ColumnMesh, dtype=torch.float64):
        self.mesh = mesh
        self.dtype = dtype
        dev = mesh.z.device
        self.device = dev
        N, dxi, deta = _shape_tables(dtype, dev)
        self.N = N
        X, Z = mesh.element_corners()
        X = X.to(dtype)
        Z = Z.to(dtype)
        # Jacobian per Gauss point: (B, nx, nz, 4gp)
        x_xi = torch.einsum("ga,...a->...g", dxi, X)
        z_xi = torch.einsum("ga,...a->...g", dxi, Z)
        x_eta = torch.einsum("ga,...a->...g", deta, X)
        z_eta = torch.einsum("ga,...a->...g", deta, Z)
        det = x_xi * z_eta - z_xi * x_eta
        if not bool((det > 0).all()):
            raise ValueError("non-positive Jacobian: element corners not counter-clockwise or degenerate")
        inv = 1.0 / det
        # dN/dx, dN/dz: (B, nx, nz, 4gp, 4nodes)
        Nx = (z_eta[..., None] * dxi - z_xi[..., None] * deta) * inv[..., None]
        Nz = (-x_eta[..., None] * dxi + x_xi[..., None] * deta) * inv[..., None]
        self.wdet = det  # unit Gauss weights
        self.area = det.sum(-1)
        # volumetric rows and their element averages
        bx_bar = (Nx * det[..., None]).sum(-2) / self.area[..., None]  # (B,nx,nz,4)
        bz_bar = (Nz * det[..., None]).sum(-2) / self.area[..., None]
        sh = Nx.shape[:-1]
        Bm = torch.zeros(*sh, 4, 8, dtype=dtype, device=dev)
        Bm[..., 0, 0::2] = Nx
        Bm[..., 1, 1::2] = Nz
        Bm[..., 3, 0::2] = Nz / SQ2
        Bm[..., 3, 1::2] = Nx / SQ2
        # in-plane B-bar: ε̄ = ε + ½(ε̄_v − ε_v)(1, 1, 0, 0) with ε_v = ε_xx + ε_zz (ε_out-of-plane stays 0 at every
        # Gauss point). This is the variant of MAPDL PLANE182 KEYOPT(1)=0 and OGS use_b_bar in plane strain:
        # equilibrium state of the 70 500-node slice agrees with both to 3e-7 m (the ⅓-variant of Hughes differs
        # by 2e-5 m and gives a 0.4 % softer excavation response) — ladder receipt GPU-L2.
        dvol_x = 0.5 * (bx_bar[..., None, :] - Nx)  # (B,nx,nz,4gp,4)
        dvol_z = 0.5 * (bz_bar[..., None, :] - Nz)
        for r in range(2):
            Bm[..., r, 0::2] += dvol_x
            Bm[..., r, 1::2] += dvol_z
        self.Bbar = Bm  # (B, nx, nz, 4gp, 4, 8)
        self.Nw = N[None, None, None] * det[..., None]  # (B,nx,nz,4gp,4): ∫N_a over the element via Gauss

    # ---------------------------------------------------------------- gather / scatter of nodal fields
    @staticmethod
    def gather(u: torch.Tensor) -> torch.Tensor:
        """u (B, nx+1, nz+1, 2) -> element vectors (B, nx, nz, 8) in corner order BL, BR, TR, TL."""
        return torch.cat([u[:, :-1, 1:], u[:, 1:, 1:], u[:, 1:, :-1], u[:, :-1, :-1]], dim=-1)

    @staticmethod
    def scatter(fe: torch.Tensor) -> torch.Tensor:
        """element vectors (B, nx, nz, 8) -> nodal sums (B, nx+1, nz+1, 2)."""
        B, nx, nz, _ = fe.shape
        F = fe.new_zeros(B, nx + 1, nz + 1, 2)
        F[:, :-1, 1:] += fe[..., 0:2]
        F[:, 1:, 1:] += fe[..., 2:4]
        F[:, 1:, :-1] += fe[..., 4:6]
        F[:, :-1, :-1] += fe[..., 6:8]
        return F

    # ---------------------------------------------------------------- operators
    def strain(self, u: torch.Tensor) -> torch.Tensor:
        """B-bar strain at Gauss points: (B, nx, nz, 4gp, 4)."""
        ue = self.gather(u)
        sh = self.Bbar.shape
        Bf = self.Bbar.reshape(*sh[:3], 16, 8)
        return torch.matmul(Bf, ue[..., None]).reshape(*sh[:3], 4, 4)

    def internal_force(self, sig: torch.Tensor, alive: torch.Tensor | None = None) -> torch.Tensor:
        """∫ B̄ᵀσ dA, sig (B,nx,nz,4gp,4); alive (B,nx,nz) multiplies element contributions."""
        sh = self.Bbar.shape
        sw = (sig * self.wdet[..., None]).reshape(*sh[:3], 16, 1)
        fe = torch.matmul(self.Bbar.reshape(*sh[:3], 16, 8).transpose(-1, -2), sw)[..., 0]
        if alive is not None:
            fe = fe * alive[..., None]
        return self.scatter(fe)

    def stiffness(self, Dt: torch.Tensor, alive: torch.Tensor | None = None) -> torch.Tensor:
        """Element matrices ∫ B̄ᵀ D B̄ dA: Dt (B,nx,nz,4gp,4,4) or (B,nx,nz,1,4,4) -> (B,nx,nz,8,8)."""
        sh = self.Bbar.shape
        DB = torch.matmul(Dt, self.Bbar) * self.wdet[..., None, None]  # (B,nx,nz,4gp,4,8)
        ke = torch.matmul(self.Bbar.reshape(*sh[:3], 16, 8).transpose(-1, -2), DB.reshape(*sh[:3], 16, 8))
        if alive is not None:
            ke = ke * alive[..., None, None]
        return ke

    def body_force(self, rho_g: torch.Tensor) -> torch.Tensor:
        """Consistent nodal load of a vertical body force −ρg per element (rho_g: (B,nx,nz), N/m³)."""
        fa = -(self.Nw.sum(-2)) * rho_g[..., None]  # (B,nx,nz,4): ∫N_a dA · (−ρg)
        fe = torch.zeros(*fa.shape[:-1], 8, dtype=fa.dtype, device=fa.device)
        fe[..., 1::2] = fa
        return self.scatter(fe)


def elastic_moduli(E: torch.Tensor, nu: torch.Tensor):
    """Bulk and shear modulus (plane strain uses the 3D values with ε_out-of-plane = 0)."""
    K = E / (3.0 * (1.0 - 2.0 * nu))
    G = E / (2.0 * (1.0 + nu))
    return K, G


def elastic_D(K: torch.Tensor, G: torch.Tensor) -> torch.Tensor:
    """Isotropic elasticity in Mandel 4-notation: D = K m⊗m + 2G P_dev; shape (..., 4, 4)."""
    m = torch.tensor(MANDEL_M, dtype=K.dtype, device=K.device)
    I = torch.eye(4, dtype=K.dtype, device=K.device)
    Pdev = I - torch.outer(m, m) / 3.0
    return K[..., None, None] * torch.outer(m, m) + 2.0 * G[..., None, None] * Pdev


def stress_from_lambda(sv: torch.Tensor, lam: torch.Tensor) -> torch.Tensor:
    """Initial stress (σx = σ_out = λσv, σz = σv, τ = 0) as Mandel 4-vectors."""
    return torch.stack([lam * sv, sv, lam * sv, torch.zeros_like(sv)], dim=-1)


def column_vertical_stress(mesh: ColumnMesh, rho_g: torch.Tensor, alive: torch.Tensor | None = None) -> torch.Tensor:
    """σv at element centres from the weight of the column above (as the project's MAPDL drivers: per column,
    element weights ρ g (z_top − z_bottom) with z_top/z_bottom = mean of the element's upper/lower corners)."""
    z = mesh.z
    top = 0.5 * (z[:, :-1, :-1] + z[:, 1:, :-1])
    bot = 0.5 * (z[:, :-1, 1:] + z[:, 1:, 1:])
    w = rho_g * (top - bot)
    if alive is not None:
        w = w * alive
    above = torch.cumsum(w, dim=-1) - w
    return -(above + 0.5 * w)
