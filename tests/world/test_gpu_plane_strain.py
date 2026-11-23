"""Synthetic tests of the GPU plane-strain representation (run on CPU torch; no project data).

Truths: dense linear algebra, the 1D oedometric column under self-weight (exact nodal values for linear elements with
consistent load), exact equilibrium of a column-wise geostatic stress on flat layers, the closed-form Norton stress
relaxation at fixed strain, uniform-strain patch reproduction, finite-difference tangents.
"""
from __future__ import annotations

import math

import numpy as np
import pytest

torch = pytest.importorskip("torch")

from vkm_world.representations.gpu_plane_strain import (ColumnMesh, Materials, MeshSpec, PlaneStrainCreepSolver,
                                                        Q4BBar, build_slice_batch, column_vertical_stress,
                                                        norton_return, stress_from_lambda, time_steps, von_mises)
from vkm_world.representations.gpu_plane_strain import blocktri
from vkm_world.representations.gpu_plane_strain.fem import elastic_moduli

DT = torch.float64


def _rect_mesh(B=1, nx=3, z_nodes=(0.0, -2.0, -5.0, -9.0), width=6.0, skew=0.0):
    xs = torch.linspace(0, width, nx + 1, dtype=DT)
    z = torch.tensor(z_nodes, dtype=DT)
    Z = z[None, :].repeat(nx + 1, 1) + skew * torch.sin(xs)[:, None] * (torch.arange(len(z_nodes), dtype=DT) % 2)[None, :]
    nz = len(z_nodes) - 1
    return ColumnMesh(xs[None].repeat(B, 1), Z[None].repeat(B, 1, 1), torch.zeros(B, nx, nz, dtype=torch.long))


def test_blocktri_matches_dense_solve():
    g = torch.Generator().manual_seed(1)
    B, nx, nz = 2, 5, 3
    ke = torch.randn(B, nx, nz, 8, 8, generator=g, dtype=DT)
    ke = ke @ ke.transpose(-1, -2) + 8 * torch.eye(8, dtype=DT)
    D, U = blocktri.assemble(ke)
    fixed = torch.zeros(B, nx + 1, 2 * (nz + 1), dtype=torch.bool)
    fixed[:, 0, ::2] = True
    elim = blocktri.apply_dirichlet(D, U, fixed)
    m = 2 * (nz + 1)
    n = (nx + 1) * m
    K = torch.zeros(B, n, n, dtype=DT)
    for i in range(nx + 1):
        K[:, i * m:(i + 1) * m, i * m:(i + 1) * m] = D[:, i]
        if i < nx:
            K[:, i * m:(i + 1) * m, (i + 1) * m:(i + 2) * m] = U[:, i]
            K[:, (i + 1) * m:(i + 2) * m, i * m:(i + 1) * m] = U[:, i].transpose(-1, -2)
    b = torch.randn(B, nx + 1, m, generator=g, dtype=DT).masked_fill(elim, 0.0)
    x_dense = torch.linalg.solve(K, b.reshape(B, n, 1)).reshape(B, nx + 1, m)
    L, W = D.clone(), U.clone()
    info = blocktri.factor_(L, W)
    assert int(info.max()) == 0
    x = blocktri.solve(L, W, b)
    assert torch.allclose(x, x_dense, rtol=1e-10, atol=1e-12)
    assert torch.allclose(blocktri.matvec(D, U, x), b, atol=1e-9)


@pytest.mark.parametrize("nx", [1, 2, 6, 7, 12])
def test_cyclic_reduction_matches_dense_solve(nx):
    g = torch.Generator().manual_seed(nx)
    B, nz = 3, 2
    ke = torch.randn(B, nx, nz, 8, 8, generator=g, dtype=DT)
    ke = ke @ ke.transpose(-1, -2) + 8 * torch.eye(8, dtype=DT)
    D, U = blocktri.assemble(ke)
    fixed = torch.zeros(B, nx + 1, 2 * (nz + 1), dtype=torch.bool)
    fixed[:, :, -2:] = True
    elim = blocktri.apply_dirichlet(D, U, fixed)
    b = torch.randn(B, nx + 1, 2 * (nz + 1), generator=g, dtype=DT).masked_fill(elim, 0.0)
    cr = blocktri.CyclicReduction(D.clone(), U.clone())
    assert int(cr.info.max()) == 0
    x = cr.solve(b)
    assert torch.allclose(blocktri.matvec(D, U, x), b, atol=1e-9)
    th = blocktri.BlockThomas(D.clone(), U.clone())
    assert torch.allclose(th.solve(b), x, rtol=1e-10, atol=1e-12)


def test_bbar_reproduces_uniform_strain_on_distorted_mesh():
    mesh = _rect_mesh(skew=0.3)
    fem = Q4BBar(mesh)
    exx, ezz, exz = 1e-3, -2e-3, 0.5e-3
    x = mesh.xs[:, :, None].expand_as(mesh.z)
    z = mesh.z
    u = torch.stack([exx * x + exz * z, ezz * z + exz * x], dim=-1)
    eps = fem.strain(u)
    expect = torch.tensor([exx, ezz, 0.0, math.sqrt(2) * exz], dtype=DT)
    assert torch.allclose(eps, expect.expand_as(eps), atol=1e-12)


def _column(layers=((2.0, 1900.0, 1e8, 0.3), (3.0, 2200.0, 6e9, 0.2), (4.0, 2100.0, 3e9, 0.4)), rows=4):
    z = [0.0]
    props = []
    for h, rho, E, nu in layers:
        for _ in range(rows):
            z.append(z[-1] - h / rows)
            props.append((rho, E, nu))
    mesh = _rect_mesh(nx=2, z_nodes=z, width=1.0)
    nz = mesh.nz
    rho = torch.tensor([p[0] for p in props], dtype=DT)[None, None, :].repeat(1, 2, 1)
    E = torch.tensor([p[1] for p in props], dtype=DT)[None, None, :].repeat(1, 2, 1)
    nu = torch.tensor([p[2] for p in props], dtype=DT)[None, None, :].repeat(1, 2, 1)
    return mesh, Materials(E, nu, rho, torch.zeros_like(E), torch.ones_like(E)), np.array(z), props


def test_oedometric_column_under_self_weight_matches_analytic():
    mesh, mat, z, props = _column()
    S = PlaneStrainCreepSolver(mesh, mat, torch.zeros(*mat.E.shape, 4, dtype=DT))
    st = S.initial_state()
    uz = st.u[0, 0, :, 1].numpy()
    # analytic: u(z_j) = −Σ_{elements below j} ∫ σv/M dz, σv linear in each element (exact nodal values)
    g = 9.81
    sv_top, analytic = [0.0], np.zeros(len(z))
    for k, (rho, E, nu) in enumerate(props):
        h = z[k] - z[k + 1]
        sv_top.append(sv_top[-1] + rho * g * h)
    for j in range(len(z) - 2, -1, -1):
        rho, E, nu = props[j]
        h = z[j] - z[j + 1]
        M = E * (1 - nu) / ((1 + nu) * (1 - 2 * nu))
        mean_sv = 0.5 * (sv_top[j] + sv_top[j + 1])
        analytic[j] = analytic[j + 1] - mean_sv * h / M
    assert np.allclose(uz, analytic, rtol=1e-9, atol=1e-14)
    assert abs(st.u[0, :, :, 0]).max() < 1e-14


@pytest.mark.parametrize("k0", [False, True])
def test_column_geostatic_stress_is_in_equilibrium_on_flat_layers(k0):
    mesh, mat, z, props = _column()
    sv = column_vertical_stress(mesh, mat.rho * 9.81)
    lam = mat.nu / (1 - mat.nu) if k0 else torch.ones_like(mat.nu)
    S = PlaneStrainCreepSolver(mesh, mat, stress_from_lambda(sv, lam))
    st = S.initial_state()
    assert float(st.u.abs().max()) < 1e-13


def test_norton_relaxation_at_fixed_strain_matches_closed_form():
    """All DOFs fixed: dq/dt = −3G·A·(q/p0)^n  =>  q^(1−n) = q0^(1−n) + (n−1)·3G·A·t/p0^n.
    Backward Euler is first order: the error at t = 1, 5 yr must be small and halve with the step."""
    mesh = _rect_mesh(nx=1, z_nodes=(0.0, -1.0), width=1.0)
    E, nu, n, A, p0 = 6.895e9, 0.2, 5.0, 1.0 / 7.58e7, 1e6
    mat = Materials(torch.full((1, 1, 1), E, dtype=DT), torch.full((1, 1, 1), nu, dtype=DT),
                    torch.zeros(1, 1, 1, dtype=DT), torch.full((1, 1, 1), A, dtype=DT), torch.ones(1, 1, 1, dtype=DT))
    tau = 6e6  # pure shear σxx = −σzz = τ, σ_out-of-plane = 0 stays 0 (its deviator is 0)
    sig0 = torch.tensor([tau, -tau, 0.0, 0.0], dtype=DT).reshape(1, 1, 1, 4)
    G = E / (2 * (1 + nu))
    q0 = math.sqrt(3.0) * tau
    errs = []
    for dtm in (0.01, 0.005):
        S = PlaneStrainCreepSolver(mesh, mat, sig0, n=n)
        S.fixed[:] = True
        st = S.initial_state()
        steps = [dtm] * int(round(5.0 / dtm))

        def grab(t, s):
            sig, *_ = S._stress(S.mat, s, s.u, 0.0, False, tangent=False)
            return float(von_mises(sig).mean())

        res = S.run_creep(st, S.mat, steps, [1.0, 5.0], grab)
        e = []
        for t, q in res["outputs"].items():
            exact = (q0 ** (1 - n) + (n - 1) * 3 * G * A * t / p0 ** n) ** (1 / (1 - n))
            e.append((q - exact) / exact)
        errs.append(np.array(e))
    assert np.abs(errs[1]).max() < 1.5e-3
    ratio = errs[0] / errs[1]
    assert np.all((ratio > 1.7) & (ratio < 2.3)), ratio


def test_norton_tangent_matches_finite_differences():
    g = torch.Generator().manual_seed(3)
    K = torch.tensor(4e9, dtype=DT)
    G = torch.tensor(2.5e9, dtype=DT)
    A = torch.tensor(1 / 2e7, dtype=DT)
    sig = (torch.randn(5, 4, generator=g, dtype=DT) * 8e6)
    _, _, _, Dt = norton_return(sig, K, G, A, 5.0, 0.3)
    m = torch.tensor([1.0, 1, 1, 0], dtype=DT)
    Pdev = torch.eye(4, dtype=DT) - torch.outer(m, m) / 3
    Del = K * torch.outer(m, m) + 2 * G * Pdev
    h = 1e-9
    for k in range(4):
        e = torch.zeros(4, dtype=DT)
        e[k] = h
        sp, *_ = norton_return(sig + Del @ e, K, G, A, 5.0, 0.3, need_tangent=False)
        sm, *_ = norton_return(sig - Del @ e, K, G, A, 5.0, 0.3, need_tangent=False)
        fd = (sp - sm) / (2 * h)
        assert torch.allclose(fd, Dt[..., :, k], rtol=1e-5, atol=1e3)


def test_excavation_of_chambers_in_layered_slice_is_symmetric_and_subsides():
    B = 1
    chain = torch.tensor([0.0, 1000.0], dtype=DT)
    contacts = torch.tensor([[-10.0, -40.0, -80.0, -100.0, -150.0, -220.0]], dtype=DT)[:, None, :].repeat(B, 2, 1)
    spec = MeshSpec(rows=(1, 2, 2, 2, 2, 2, 1, 1, 2, 4), chamber_elems=2, pillar_h=4.0, outer_h0=4.0, outer_hmax=40.0)
    one = lambda v: torch.full((B,), v, dtype=DT)
    mesh, info = build_slice_batch(chain, torch.zeros(2, dtype=DT), contacts, -400.0, one(8.0), one(5.0), one(0.5),
                                   one(10.0), one(15.0), 5, 500.0, spec)
    E = torch.full(mesh.zone.shape, 5e9, dtype=DT)
    nu = torch.full_like(E, 0.25)
    rho = torch.full_like(E, 2200.0)
    mat = Materials(E, nu, rho, torch.zeros_like(E), torch.ones_like(E))
    sig0 = stress_from_lambda(column_vertical_stress(mesh, rho * 9.81), torch.ones_like(E))
    S = PlaneStrainCreepSolver(mesh, mat, sig0)
    st1 = S.initial_state()
    assert float(st1.u.abs().max()) < 1e-12  # flat layers, homogeneous: geostatic field is exact
    exc = mat.clone()
    exc.alive = ((mesh.zone != 15) & (mesh.zone != 16)).to(DT)
    st2 = S.elastic_stage(st1, exc)
    s = -(st2.u - st1.u)[0, :, 0, 1].numpy()
    x = mesh.xs[0].numpy()
    assert s.max() > 0 and abs(x[np.argmax(s)] - 500.0) < 20.0
    mirrored = np.interp(1000.0 - x, x, s)
    assert np.abs(mirrored - s).max() < 1e-3 * s.max()


def test_float32_factorisation_converges_to_float64_solution():
    mesh, mat, z, props = _column()
    S64 = PlaneStrainCreepSolver(mesh, mat, torch.zeros(*mat.E.shape, 4, dtype=DT))
    S32 = PlaneStrainCreepSolver(mesh, mat, torch.zeros(*mat.E.shape, 4, dtype=DT), factor_dtype=torch.float32,
                                 newton_tol_m=1e-13)
    u64 = S64.initial_state().u
    u32 = S32.initial_state().u
    assert torch.allclose(u32, u64, rtol=1e-8, atol=1e-13)


def _creep_slice(k0: bool):
    B = 2
    chain = torch.tensor([0.0, 1000.0], dtype=DT)
    contacts = torch.tensor([[-10.0, -40.0, -80.0, -100.0, -150.0, -220.0]], dtype=DT)[:, None, :].repeat(B, 2, 1)
    spec = MeshSpec(rows=(1, 2, 2, 2, 2, 2, 1, 1, 2, 4), chamber_elems=2, pillar_h=4.0, outer_h0=4.0, outer_hmax=40.0)
    one = lambda v: torch.full((B,), v, dtype=DT)
    mesh, _ = build_slice_batch(chain, torch.zeros(2, dtype=DT), contacts, -400.0, one(8.0), one(5.0), one(0.5),
                                one(10.0), one(15.0), 5, 500.0, spec)
    E = torch.full(mesh.zone.shape, 5e9, dtype=DT)
    nu = torch.full_like(E, 0.25)
    rho = torch.full_like(E, 2200.0)
    A = torch.where(mesh.zone >= 3, torch.full_like(E, 1 / 2e7), torch.zeros_like(E))
    mat = Materials(E, nu, rho, A, torch.ones_like(E))
    lam = nu / (1 - nu) if k0 else torch.ones_like(nu)
    sig0 = stress_from_lambda(column_vertical_stress(mesh, rho * 9.81), lam)
    return mesh, mat, sig0


@pytest.mark.parametrize("k0", [False, True])
def test_reference_run_does_not_inherit_the_excavation_factorisation(k0):
    """Regression (23.10.2026): with keep_factor_between_steps the reference run must not start from the last
    factorisation of the excavation run (another matrix); results equal those of a fresh solver."""
    mesh, mat, sig0 = _creep_slice(k0)
    kw = dict(factor_dtype=torch.float32, keep_factor_between_steps=True, reuse_ratio=0.4)
    steps = time_steps(2.0, dt0=0.05, growth=1.3, dt_max=0.5)
    S = PlaneStrainCreepSolver(mesh, mat, sig0, **kw)
    st1 = S.initial_state()
    exc = mat.clone()
    exc.alive = ((mesh.zone != 15) & (mesh.zone != 16)).to(DT)
    S.run_creep(S.elastic_stage(st1, exc), exc, steps, [])
    ref_after = S.run_creep(st1, mat, steps, [])["final"].u
    F = PlaneStrainCreepSolver(mesh, mat, sig0, **kw)
    ref_fresh = F.run_creep(F.initial_state(), mat, steps, [])["final"].u
    assert torch.allclose(ref_after, ref_fresh, atol=1e-8)
