"""Batched symmetric block-tridiagonal factorisation for structured column meshes.

With nodes numbered column by column, the stiffness of a structured quadrilateral mesh is block tridiagonal:
column ``i`` couples only to columns ``i−1`` and ``i+1``. The block size is ``m = 2·(nz+1)``. The factorisation is the
block Cholesky (block Thomas) recursion

    S_0 = D_0,   L_i L_iᵀ = S_i,   W_i = L_i⁻¹ U_i,   S_{i+1} = D_{i+1} − W_iᵀ W_i,

which is exact (a direct solver, no iteration and no tolerance). Each recursion step is one batched dense
operation over all ensemble members, which is what makes the GPU efficient for an ensemble of meshes that
share ``nx, nz``.
"""
from __future__ import annotations

import torch

# element-local DOFs that fall into the left / right column window [2j, 2j+1, 2j+2, 2j+3]
_PL = [6, 7, 0, 1]  # TL(u,v), BL(u,v)  -> column i
_PR = [4, 5, 2, 3]  # TR(u,v), BR(u,v)  -> column i+1


def _band_index(nz: int, device) -> torch.Tensor:
    m = 2 * (nz + 1)
    j = torch.arange(nz, device=device)
    r = torch.arange(4, device=device)
    rows = 2 * j[:, None, None] + r[None, :, None]
    cols = 2 * j[:, None, None] + r[None, None, :]
    return (rows * m + cols).reshape(-1)


def assemble(ke: torch.Tensor):
    """Element matrices (B, nx, nz, 8, 8) -> dense diagonal blocks D (B, nx+1, m, m) and upper blocks U (B, nx, m, m)."""
    B, nx, nz = ke.shape[:3]
    m = 2 * (nz + 1)
    idx = _band_index(nz, ke.device)
    kLL = ke[..., _PL, :][..., _PL]
    kRR = ke[..., _PR, :][..., _PR]
    kLR = ke[..., _PL, :][..., _PR]
    tmp = ke.new_zeros(B, nx + 1, nz, 4, 4)
    tmp[:, :-1] += kLL
    tmp[:, 1:] += kRR
    D = ke.new_zeros(B, nx + 1, m * m)
    D.index_add_(2, idx, tmp.reshape(B, nx + 1, nz * 16))
    U = ke.new_zeros(B, nx, m * m)
    U.index_add_(2, idx, kLR.reshape(B, nx, nz * 16))
    return D.view(B, nx + 1, m, m), U.view(B, nx, m, m)


def apply_dirichlet(D: torch.Tensor, U: torch.Tensor, fixed: torch.Tensor):
    """Zero rows/columns of fixed DOFs (fixed: (B, nx+1, m) bool) and put 1 on their diagonal; DOFs with an
    exactly zero diagonal (nodes of removed elements only) are treated the same way. Returns the mask of
    eliminated DOFs (fixed ∪ orphan)."""
    keep = (~fixed).to(D.dtype)
    D.mul_(keep[..., :, None] * keep[..., None, :])
    U.mul_(keep[:, :-1, :, None] * keep[:, 1:, None, :])
    diag = D.diagonal(dim1=-2, dim2=-1)
    orphan = (diag == 0)
    diag.add_(orphan.to(D.dtype))
    return fixed | orphan


def factor_(D: torch.Tensor, U: torch.Tensor) -> torch.Tensor:
    """In-place block Cholesky: D[i] <- L_i, U[i] <- W_i. Returns per-member info (0 = success)."""
    nb = D.shape[1]
    info = torch.zeros(D.shape[0], dtype=torch.int32, device=D.device)
    for i in range(nb):
        if i > 0:
            W = U[:, i - 1]
            D[:, i].baddbmm_(W.transpose(-1, -2), W, beta=1.0, alpha=-1.0)
        L, inf = torch.linalg.cholesky_ex(D[:, i])
        info = torch.maximum(info, inf)
        D[:, i] = L
        if i < nb - 1:
            U[:, i] = torch.linalg.solve_triangular(L, U[:, i], upper=False)
    return info


def solve(L: torch.Tensor, W: torch.Tensor, b: torch.Tensor) -> torch.Tensor:
    """Solve K x = b with the factors of :func:`factor_`; b, x: (B, nx+1, m)."""
    nb = L.shape[1]
    y = torch.empty_like(b)
    for i in range(nb):
        r = b[:, i, :, None]
        if i > 0:
            r = r - torch.bmm(W[:, i - 1].transpose(-1, -2), y[:, i - 1, :, None])
        y[:, i] = torch.linalg.solve_triangular(L[:, i], r, upper=False)[..., 0]
    x = torch.empty_like(b)
    for i in range(nb - 1, -1, -1):
        r = y[:, i, :, None]
        if i < nb - 1:
            r = r - torch.bmm(W[:, i], x[:, i + 1, :, None])
        x[:, i] = torch.linalg.solve_triangular(L[:, i].transpose(-1, -2), r, upper=True)[..., 0]
    return x


class BlockThomas:
    """Sequential block Cholesky (reference implementation) with the same interface as CyclicReduction."""

    def __init__(self, D: torch.Tensor, U: torch.Tensor):
        self.L, self.W = D, U
        self.info = factor_(D, U)

    def solve(self, b: torch.Tensor) -> torch.Tensor:
        return solve(self.L, self.W, b)


class CyclicReduction:
    """Symmetric block cyclic reduction of the same block-tridiagonal SPD matrix (exact direct solver).

    At every level the odd blocks are eliminated at once (one batched Cholesky + two triangular solves + three
    GEMMs over all odd blocks of all members), the even blocks form a block-tridiagonal Schur complement of half
    the size. ~log2(nb) levels instead of nb sequential steps: about 2.5× the flops of :func:`factor_` but two
    orders of magnitude fewer kernel launches, which is what limits the sequential recursion on a GPU.
    Odd block o couples to its left even neighbour through Uleft = U[o−1] and to the right one through U[o];
    with L Lᵀ = D_o, X = L⁻¹ Uleftᵀ, Y = L⁻¹ U_o, the Schur complement is
    D_e[p] −= XᵀX (o = 2p+1),  D_e[p+1] −= YᵀY,  U'_p = −XᵀY.
    """

    def __init__(self, D: torch.Tensor, U: torch.Tensor):
        self.levels = []
        info = torch.zeros(D.shape[0], dtype=torch.int32, device=D.device)
        while D.shape[1] > 1:
            n = D.shape[1]
            no = n // 2
            ne = n - no
            L, inf = torch.linalg.cholesky_ex(D[:, 1::2])
            info = torch.maximum(info, inf.amax(-1) if inf.dim() > 1 else inf)
            X = torch.linalg.solve_triangular(L, U[:, 0::2][:, :no].transpose(-1, -2), upper=False)
            nr = (n - 1) // 2  # odd blocks with a right neighbour
            Y = torch.linalg.solve_triangular(L[:, :nr], U[:, 1::2][:, :nr], upper=False)
            De = D[:, 0::2].clone()
            De[:, :no] -= X.transpose(-1, -2) @ X
            De[:, 1:nr + 1] -= Y.transpose(-1, -2) @ Y
            Un = -(X[:, :nr].transpose(-1, -2) @ Y)
            self.levels.append((L, X, Y, n, no, nr))
            D, U = De, Un
        L, inf = torch.linalg.cholesky_ex(D[:, 0])
        self.info = torch.maximum(info, inf)
        self.Lf = L

    def solve(self, b: torch.Tensor) -> torch.Tensor:
        zs = []
        for L, X, Y, n, no, nr in self.levels:
            z = torch.linalg.solve_triangular(L, b[:, 1::2, :, None], upper=False)  # (B,no,m,1)
            be = b[:, 0::2].clone()
            be[:, :no] -= (X.transpose(-1, -2) @ z)[..., 0]
            be[:, 1:nr + 1] -= (Y.transpose(-1, -2) @ z[:, :nr])[..., 0]
            zs.append(z)
            b = be
        x = torch.cholesky_solve(b[:, 0, :, None], self.Lf)[:, None, :, 0]
        for (L, X, Y, n, no, nr), z in zip(reversed(self.levels), reversed(zs)):
            r = z - X @ x[:, :no, :, None]
            r[:, :nr] -= Y @ x[:, 1:nr + 1, :, None]
            xo = torch.linalg.solve_triangular(L.transpose(-1, -2), r, upper=True)[..., 0]
            full = x.new_empty(x.shape[0], n, x.shape[2])
            full[:, 0::2] = x
            full[:, 1::2] = xo
            x = full
        return x


def matvec(D: torch.Tensor, U: torch.Tensor, x: torch.Tensor) -> torch.Tensor:
    """K x for the assembled (unfactored) blocks; used by tests."""
    y = torch.einsum("bimn,bin->bim", D, x)
    y[:, :-1] += torch.einsum("bimn,bin->bim", U, x[:, 1:])
    y[:, 1:] += torch.einsum("binm,bin->bim", U, x[:, :-1])
    return y
