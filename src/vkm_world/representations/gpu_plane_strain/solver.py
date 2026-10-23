"""Plane-strain geomechanics of a layered slice on the GPU: gravity, initial state, excavation (removal of chamber
elements or modulus change), optional backfill, Norton creep in time. Batched over ensemble members.

Total-strain formulation (the same as MAPDL INISTATE EPEL / OGS initial_stress):

    σ = D (ε̄(u) + ε0 − εc),

ε0 = initial elastic strain of the geostatic hypothesis (σ0 = D ε0 with the original moduli), εc = creep strain
(for a backfill element: the strain at the moment of placement, so that the fill is stress-free when placed).
Load stages follow the MAPDL drivers of the project: LS1 equilibrium of gravity + σ0 on the intact mesh (creep off),
LS2 excavation (creep off), then creep steps. Every step is solved by Newton's method with the consistent tangent
and the exact block-tridiagonal factorisation (``blocktri``), so there is no linear-solver tolerance.

This is a representation (a solver adapter): its results are MODEL results, never observations.
"""
from __future__ import annotations

from dataclasses import dataclass, field

import torch

from . import blocktri
from .creep import norton_return
from .fem import ColumnMesh, Q4BBar, elastic_D, elastic_moduli


@dataclass
class Materials:
    """Per-element material fields, each (B, nx, nz)."""

    E: torch.Tensor
    nu: torch.Tensor
    rho: torch.Tensor
    A: torch.Tensor  # Norton factor 1/ηs [1/yr]; 0 = elastic
    alive: torch.Tensor  # 1 = present, 0 = removed

    def clone(self) -> "Materials":
        return Materials(self.E.clone(), self.nu.clone(), self.rho.clone(), self.A.clone(), self.alive.clone())


@dataclass
class State:
    u: torch.Tensor  # (B, nx+1, nz+1, 2)
    eps_c: torch.Tensor  # (B, nx, nz, 4gp, 4)
    eps_eq: torch.Tensor  # (B, nx, nz, 4gp) accumulated equivalent creep strain
    t: float = 0.0
    stats: dict = field(default_factory=dict)

    def clone(self) -> "State":
        return State(self.u.clone(), self.eps_c.clone(), self.eps_eq.clone(), self.t, dict(self.stats))


def boundary_mask(mesh: ColumnMesh) -> torch.Tensor:
    """Roller sides (u_x = 0 at the first and last column), fixed base (u_x = u_z = 0 at the bottom row)."""
    B, nx, nz = mesh.batch, mesh.nx, mesh.nz
    fixed = torch.zeros(B, nx + 1, nz + 1, 2, dtype=torch.bool, device=mesh.z.device)
    fixed[:, 0, :, 0] = True
    fixed[:, -1, :, 0] = True
    fixed[:, :, -1, :] = True
    return fixed


class PlaneStrainCreepSolver:
    G_ACC = 9.81

    def __init__(self, mesh: ColumnMesh, mat: Materials, sigma0: torch.Tensor, n: float = 5.0, p0: float = 1.0e6,
                 dtype=torch.float64, newton_tol_m: float = 1e-9, max_newton: int = 40, reuse_ratio: float = 0.2,
                 factor_dtype=torch.float64, keep_factor_between_steps: bool = False, method: str = "cr",
                 strict: bool = True, extrapolate: bool = True):
        self.mesh = mesh
        self.fem = Q4BBar(mesh, dtype)
        self.dtype = dtype
        self.n = float(n)
        self.p0 = float(p0)
        self.fixed = boundary_mask(mesh)
        self.tol = newton_tol_m
        self.max_newton = max_newton
        self.reuse_ratio = reuse_ratio
        self.factor_dtype = factor_dtype
        self.keep_factor = keep_factor_between_steps
        self.method = method
        self._fac_cache = None
        self._mcache = []
        self._bbar_f = None
        self.strict = strict
        self.extrapolate = extrapolate
        self.failed = torch.zeros(mesh.batch, dtype=torch.bool, device=mesh.z.device)
        self.return_iters = 8
        self.mat = self._cast(mat)
        K, G = elastic_moduli(self.mat.E, self.mat.nu)
        # ε0 from σ0 with the ORIGINAL moduli (σ0: (B,nx,nz,4) Mandel)
        D0 = elastic_D(K, G)
        self.eps0 = torch.linalg.solve(D0, sigma0.to(dtype)[..., None])[..., 0][..., None, :].expand(
            *sigma0.shape[:-1], 4, 4).contiguous()
        self.stats = {"factorizations": 0, "solves": 0}

    def _cast(self, mat: Materials) -> Materials:
        return Materials(*(t.to(self.dtype) for t in (mat.E, mat.nu, mat.rho, mat.A, mat.alive)))

    # ------------------------------------------------------------------ pieces
    def gravity(self, mat: Materials) -> torch.Tensor:
        return self.fem.body_force(mat.rho * self.G_ACC * mat.alive)

    def _prep(self, mat: Materials):
        """Element constants of a material set (cached; a stage keeps its Materials object)."""
        for c in self._mcache:
            if c[0] is mat:
                return c
        K, G = elastic_moduli(mat.E, mat.nu)
        D = elastic_D(K, G)  # (B,nx,nz,4,4)
        Aa = (mat.A * mat.alive).reshape(-1)
        idx = torch.nonzero(Aa > 0).flatten()
        c = (mat, D, idx, K.reshape(-1)[idx][:, None], G.reshape(-1)[idx][:, None], Aa[idx][:, None])
        self._mcache = [c] + self._mcache[:3]
        return c

    def _stress(self, mat: Materials, st: State, u: torch.Tensor, dt: float, creep: bool, tangent: bool = True):
        """Stress at Gauss points; creep return mapping only in creeping (A > 0, alive) elements."""
        _, D, idx, Kc, Gc, Ac = self._prep(mat)
        eps = self.fem.strain(u) + self.eps0 - st.eps_c
        sig = torch.matmul(D[..., None, :, :], eps[..., None])[..., 0]  # (B,nx,nz,4gp,4)
        dec = torch.zeros_like(sig)
        deq = torch.zeros_like(sig[..., 0])
        Dt = D[..., None, :, :] if tangent else None
        if creep and dt > 0 and idx.numel():
            flat = sig.reshape(-1, 4, 4)
            s, dc, de, Dc = norton_return(flat[idx], Kc, Gc, Ac, self.n, dt, self.p0, iters=self.return_iters,
                                          need_tangent=tangent)
            flat[idx] = s
            dec.reshape(-1, 4, 4)[idx] = dc
            deq.reshape(-1, 4)[idx] = de
            if tangent:
                Dt = D[..., None, :, :].expand(*sig.shape[:-1], 4, 4).clone()
                Dt.reshape(-1, 4, 4, 4)[idx] = Dc
        return sig, dec, deq, Dt

    def _factor(self, mat: Materials, Dt: torch.Tensor, label: str):
        """Assemble and factorise the tangent (block cyclic reduction by default). With ``factor_dtype`` float32
        the element matrices are symmetrically scaled by the square root of the assembled diagonal and the blocks
        are assembled and factorised in float32; the solve is then an inexact Newton/refinement step whose residual
        is always evaluated in float64 (state, stresses and residuals stay float64)."""
        B, nx1, nz1 = self.mesh.batch, self.mesh.nx + 1, self.mesh.nz + 1
        m = 2 * nz1
        d = None
        low = self.factor_dtype != self.dtype
        if low:
            # element matrices directly in the factorisation precision (they only enter the factorisation)
            if self._bbar_f is None:
                self._bbar_f = self.fem.Bbar.to(self.factor_dtype)
                self._wdet_f = self.fem.wdet.to(self.factor_dtype)
            sh = self._bbar_f.shape
            DB = torch.matmul(Dt.to(self.factor_dtype), self._bbar_f) * self._wdet_f[..., None, None]
            ke = torch.matmul(self._bbar_f.reshape(*sh[:3], 16, 8).transpose(-1, -2), DB.reshape(*sh[:3], 16, 8))
            del DB
            ke = ke * mat.alive.to(self.factor_dtype)[..., None, None]
            diag = Q4BBar.scatter(torch.diagonal(ke, dim1=-2, dim2=-1)).reshape(B, nx1, m)
            elim0 = self.fixed.reshape(B, nx1, m) | (diag == 0)
            d = torch.where(elim0, torch.ones_like(diag), diag).sqrt()
            de = Q4BBar.gather(d.reshape(B, nx1, nz1, 2))  # (B,nx,nz,8)
            ke = ke / (de[..., :, None] * de[..., None, :])
            d = d.to(self.dtype)
        else:
            ke = self.fem.stiffness(Dt, mat.alive)
        Dg, Ug = blocktri.assemble(ke)
        del ke
        elim = blocktri.apply_dirichlet(Dg, Ug, self.fixed.reshape(B, nx1, m))
        F = (blocktri.CyclicReduction if self.method == "cr" else blocktri.BlockThomas)(Dg, Ug)
        del Dg, Ug
        self.stats["factorizations"] += 1
        if bool((F.info != 0).any()) and low:
            # loss of definiteness in single precision (badly conditioned tangent): redo in float64
            self.stats["fp64_fallback"] = self.stats.get("fp64_fallback", 0) + 1
            F = None
            ke = self.fem.stiffness(Dt, mat.alive)
            Dg, Ug = blocktri.assemble(ke)
            del ke
            elim = blocktri.apply_dirichlet(Dg, Ug, self.fixed.reshape(B, nx1, m))
            F = (blocktri.CyclicReduction if self.method == "cr" else blocktri.BlockThomas)(Dg, Ug)
            del Dg, Ug
            d = None
        bad = F.info != 0
        if bool(bad.any()):
            if self.strict:
                raise FloatingPointError(f"{label}: Cholesky failed for members {torch.nonzero(bad).flatten().tolist()}")
            self.failed |= bad
            self.stats.setdefault("cholesky_failed", []).append((label, torch.nonzero(bad).flatten().tolist()))
        return F, elim, d

    def _apply(self, fac, rhs: torch.Tensor) -> torch.Tensor:
        F, elim, d = fac
        rhs = rhs.masked_fill(elim, 0.0)
        if d is None:
            du = F.solve(rhs)
        else:
            du = F.solve((rhs / d).to(self.factor_dtype)).to(rhs.dtype).masked_fill(elim, 0.0) / d
        if bool(self.failed.any()):  # members marked failed are frozen (their results are discarded)
            du = torch.where(self.failed[:, None, None], torch.zeros_like(du), du)
        return torch.nan_to_num(du, nan=0.0, posinf=0.0, neginf=0.0)

    def _solve_newton(self, mat: Materials, st: State, dt: float, creep: bool, f_ext: torch.Tensor, label: str,
                      linear: bool = False, u_guess: torch.Tensor | None = None):
        """Find u with ∫B̄ᵀσ(u) = f_ext on the free DOFs (Newton, consistent tangent).

        The factorisation of the tangent is computed at the first iteration (or taken from the previous step when
        ``keep_factor_between_steps``) and reused while the corrections contract fast
        (|du_k| ≤ reuse_ratio·|du_{k−1}|), otherwise refreshed; convergence: max |du| < tol (m) over the batch.
        ``linear``: the matrix is exact from the first factorisation (elastic stages; only refinement follows).
        Non-convergence after ``max_newton`` iterations raises (``strict``) or marks the members whose last
        correction exceeds 10·tol in ``self.failed`` (their results must not be used).
        Returns (u, dεc, dε_eq, iterations, |du|)."""
        u = st.u.clone() if u_guess is None else u_guess.clone()
        B, nx1, nz1, _ = u.shape
        m = 2 * nz1
        it, dmax, prev = 0, float("inf"), None
        fac = None
        if self.keep_factor and creep and not linear and self._fac_cache is not None and self._fac_cache[0] is mat:
            fac = self._fac_cache[1]
        for it in range(1, self.max_newton + 1):
            refresh = fac is None or (not linear and prev is not None and dmax > self.reuse_ratio * prev)
            sig, dec, deq, Dt = self._stress(mat, st, u, dt, creep, tangent=refresh)
            R = self.fem.internal_force(sig, mat.alive) - f_ext
            if refresh:
                fac = None
                fac = self._factor(mat, Dt, label)
            del Dt
            du = self._apply(fac, -R.reshape(B, nx1, m))
            self.stats["solves"] += 1
            u = u + du.reshape(B, nx1, nz1, 2)
            dm = du.abs().amax(dim=(1, 2))
            prev, dmax = (dmax if it > 1 else None), float(dm.max())
            if dmax < self.tol or (linear and self.factor_dtype == u.dtype):
                break
        if dmax >= self.tol and not (linear and self.factor_dtype == u.dtype):
            if self.strict:
                raise FloatingPointError(f"{label}: Newton did not converge, |du| = {dmax:.3e} m")
            bad = dm > 10 * self.tol
            self.failed |= bad
            self.stats.setdefault("nonconverged_steps", []).append((label, int(bad.sum())))
        self._fac_cache = (mat, fac) if (self.keep_factor and creep) else None
        sig, dec, deq, _ = self._stress(mat, st, u, dt, creep, tangent=False)
        return u, dec, deq, it, dmax

    # ------------------------------------------------------------------ public stages
    def initial_state(self) -> State:
        """LS1: equilibrium of gravity and σ0 on the intact mesh (creep off)."""
        B, nx, nz = self.mesh.batch, self.mesh.nx, self.mesh.nz
        u0 = torch.zeros(B, nx + 1, nz + 1, 2, dtype=self.dtype, device=self.mesh.z.device)
        st = State(u0, torch.zeros(B, nx, nz, 4, 4, dtype=self.dtype, device=u0.device),
                   torch.zeros(B, nx, nz, 4, dtype=self.dtype, device=u0.device))
        u, *_ = self._solve_newton(self.mat, st, 0.0, False, self.gravity(self.mat), "LS1", linear=True)
        st.u = u
        return st

    def elastic_stage(self, st: State, mat: Materials, label: str = "stage") -> State:
        """A load stage with creep off (excavation by removal or modulus change, backfill placement)."""
        new = st.clone()
        u, *_ = self._solve_newton(mat, new, 0.0, False, self.gravity(mat), label, linear=True)
        new.u = u
        return new

    def place_fill(self, st: State, mat: Materials, fill: torch.Tensor, E_fill, nu_fill, A_fill=0.0) -> Materials:
        """Make elements with fill=1 alive as backfill, stress-free at the current displacement (εc := ε̄ + ε0)."""
        fill = fill.to(self.dtype)
        new = mat.clone()
        new.alive = torch.maximum(new.alive, fill)
        Ef = torch.as_tensor(E_fill, dtype=self.dtype, device=fill.device)
        nf = torch.as_tensor(nu_fill, dtype=self.dtype, device=fill.device)
        Af = torch.as_tensor(A_fill, dtype=self.dtype, device=fill.device)
        Ef = Ef.reshape(-1, *([1] * (fill.dim() - 1))) if Ef.dim() == 1 else Ef
        nf = nf.reshape(-1, *([1] * (fill.dim() - 1))) if nf.dim() == 1 else nf
        Af = Af.reshape(-1, *([1] * (fill.dim() - 1))) if Af.dim() == 1 else Af
        new.E = torch.where(fill > 0, Ef.expand_as(fill), new.E)
        new.nu = torch.where(fill > 0, nf.expand_as(fill), new.nu)
        new.A = torch.where(fill > 0, Af.expand_as(fill), new.A)
        new.rho = torch.where(fill > 0, torch.zeros_like(new.rho), new.rho)  # weight of the fill not applied
        eps = self.fem.strain(st.u)
        place = (eps + self.eps0)
        st.eps_c = torch.where(fill[..., None, None] > 0, place, st.eps_c)
        return new

    def creep_step(self, st: State, mat: Materials, dt: float, u_guess: torch.Tensor | None = None) -> State:
        u, dec, deq, it, dmax = self._solve_newton(mat, st, dt, True, self.gravity(mat), f"creep t={st.t + dt:g}",
                                                   u_guess=u_guess)
        new = State(u, st.eps_c + dec, st.eps_eq + deq, st.t + dt, dict(st.stats))
        new.stats["newton"] = list(st.stats.get("newton", [])) + [it]
        return new

    def run_creep(self, st: State, mat: Materials, steps, out_times, on_output=None) -> dict:
        """Integrate creep over ``steps`` (list of Δt, yr). ``on_output(t, state)`` is called at every out_time
        (which must coincide with step ends, tolerance 1e-9 yr)."""
        outs = {}
        self._fac_cache = None  # never start an integration from another stage's factorisation
        targets = sorted(out_times)
        if on_output is not None and any(abs(t - st.t) < 1e-9 for t in targets):
            outs[round(st.t, 9)] = on_output(st.t, st)
        u_prev, dt_prev = None, None
        for dt in steps:
            dt = float(dt)
            guess = None
            if self.extrapolate and u_prev is not None:
                guess = st.u + (dt / dt_prev) * (st.u - u_prev)  # linear predictor in time
            u_prev, dt_prev = st.u, dt
            st = self.creep_step(st, mat, dt, guess)
            hit = [t for t in targets if abs(t - st.t) < 1e-9]
            if hit and on_output is not None:
                outs[round(hit[0], 9)] = on_output(hit[0], st)
        return {"final": st, "outputs": outs}


def time_steps(t_end: float, dt0: float = 0.01, growth: float = 1.25, dt_max: float = 0.5, marks=()):
    """Geometric step sequence from dt0 growing by ``growth`` up to dt_max, ending exactly on every mark."""
    marks = sorted(set([m for m in marks if 0 < m < t_end] + [t_end]))
    steps, t, dt = [], 0.0, dt0
    for mk in marks:
        while t < mk - 1e-12:
            h = min(dt, mk - t)
            if mk - (t + h) < 0.2 * h:  # avoid a sliver step before a mark
                h = mk - t
            steps.append(h)
            t += h
            dt = min(dt * growth, dt_max)
    return steps
