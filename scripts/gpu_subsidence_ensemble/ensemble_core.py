"""One batch of ensemble members on the GPU: mesh from profiles, materials, LS1, excavation, backfill, creep, outputs.

Effect of excavation = u(with excavation, t) − u(reference without excavation, t), as in the MAPDL/OGS step-7
drivers (the reference run is the intact slice from the same LS1 state with the same creep law). Subsidence s = −u_z
(+ down); all values are MODEL results.
"""
from __future__ import annotations

import time

import numpy as np
import torch

from vkm_world.representations.gpu_plane_strain import (Materials, MeshSpec, PlaneStrainCreepSolver,
                                                        build_slice_batch, column_vertical_stress, stress_from_lambda)
from vkm_world.representations.gpu_plane_strain.slice_mesh import ZONE_CHAMBER, ZONE_CHAMBER_LOW, ZONE_PILLAR

from world_inputs import G_ACC, Tables, element_fields, lambda_field

CHAMBER_W = 10.2      # m, 2 passes × 5.1 m (VKM-SRC-096 p0005 табл. 2), as in the step-7 meshes
PANEL_CENTRE = 1459.4025824751734  # m, centre of the 600 m reference panel (G_solver step 10)
PANEL_LEN = 600.0
NU_FILL = 0.3         # MODEL_CHOICE (Poisson ratio of the backfill; UNKNOWN in the corpus)
SOLVER_KW: dict = {}  # solver options (precision of the factorisation, reuse policy)


def surface_metrics(x: np.ndarray, s: np.ndarray, ux: np.ndarray) -> dict:
    """The same definitions as the step-7 post-processing (post_creep.surface_metrics)."""
    i = int(np.argmax(s))
    tilt = np.gradient(s, x) * 1e3
    eps = np.gradient(ux, x) * 1e3
    over = np.where(s >= 0.010)[0]
    width = float(x[over[-1]] - x[over[0]]) if len(over) else float("nan")
    return {"s_max_mm": float(s[i] * 1e3), "x_at_max_m": float(x[i]), "width_10mm_m": width,
            "tilt_max_mm_per_m": float(np.abs(tilt).max()), "ux_max_mm": float(np.abs(ux).max() * 1e3),
            "eps_h_max_mm_per_m": float(np.abs(eps).max())}


def run_batch(tab: Tables, prof: dict, P: dict, spec: MeshSpec, steps, out_times, device="cuda", with_ref=True,
              log=print) -> dict:
    """P: dict of 1-D arrays (one value per member): E_factor, eta_sylv, eta_salt, eta_carn [MPa·yr], L_axis [m],
    d_roof [m], h_seam [m], fill_on (0/1), fill_frac, E_fill [Pa], k0 (0/1), geo (index of realization, −1 = v6_2)."""
    t_start = time.time()
    dt = torch.float64
    B = len(P["E_factor"])
    T = lambda k: torch.as_tensor(np.asarray(P[k], dtype=float), dtype=dt, device=device)
    chain = torch.as_tensor(prof["chain"], dtype=dt, device=device)
    geo = np.asarray(P["geo"], int)
    C = np.where(geo[:, None, None] < 0, prof["v62"][None], prof["realizations"][np.maximum(geo, 0)])
    contacts = torch.as_tensor(C, dtype=dt, device=device)
    surface = torch.as_tensor(prof["dsm"], dtype=dt, device=device)
    a = torch.full((B,), CHAMBER_W, dtype=dt, device=device)
    b = T("L_axis") - CHAMBER_W
    n_ch = int(np.floor((PANEL_LEN + (np.asarray(P["L_axis"]) - CHAMBER_W)) / np.asarray(P["L_axis"]) + 1e-9)[0])
    ff = torch.where(T("fill_on") > 0, T("fill_frac"), torch.full((B,), 0.5, dtype=dt, device=device))
    mesh, info = build_slice_batch(chain, surface, contacts, float(prof["base_z"]), T("d_roof"), T("h_seam"), ff, a, b,
                                   n_ch, PANEL_CENTRE, spec)
    zone = mesh.zone
    eta = torch.stack([T("eta_sylv"), T("eta_salt"), T("eta_carn")], dim=1)
    norm = torch.as_tensor(np.asarray(P.get("norm", np.zeros(B)), int) > 0, device=device)
    E, nu, rho, A = element_fields(tab, zone, T("E_factor"), eta, norm_moduli=norm)
    lam = lambda_field(nu, torch.as_tensor(np.asarray(P["k0"]) > 0, device=device))
    sig0 = stress_from_lambda(column_vertical_stress(mesh, rho * G_ACC), lam)
    mat = Materials(E, nu, rho, A, torch.ones_like(E))
    S = PlaneStrainCreepSolver(mesh, mat, sig0, n=tab.n, p0=tab.p0, **SOLVER_KW)
    st1 = S.initial_state()
    exc = mat.clone()
    exc.alive = ((zone != ZONE_CHAMBER) & (zone != ZONE_CHAMBER_LOW)).to(dt)
    st2 = S.elastic_stage(st1, exc, "excavation")
    fill_on = T("fill_on") > 0
    if bool(fill_on.any()):
        fill = (zone == ZONE_CHAMBER_LOW) & fill_on[:, None, None]
        exc = S.place_fill(st2, exc, fill, T("E_fill"), NU_FILL, 0.0)
    xs = mesh.xs
    nz = mesh.nz
    # chamber nearest to the panel centre: roof / floor node rows of the seam
    rows = np.cumsum((0,) + tuple(spec.rows))
    j_roof, j_floor = int(rows[6]), int(rows[8])
    role = info["role"].cpu().numpy()
    chamber_cols = np.where(role == 2)[0]
    k_mid = n_ch // 2  # as the step-7 post-processing: chamber n//2 (0-based), node at its middle
    centre_el = int(chamber_cols[k_mid * spec.chamber_elems] + spec.chamber_elems // 2)
    pil = (zone == ZONE_PILLAR)

    def grab(t, st):
        u = st.u
        return {"surf": u[:, :, 0, :].detach().clone(),
                "conv": (u[:, centre_el, j_floor, 1] - u[:, centre_el, j_roof, 1]).clone()}

    tE = time.time()
    resE = S.run_creep(st2, exc, steps, out_times, grab)
    tE = time.time() - tE
    stE = resE["final"]
    resR = None
    tR = 0.0
    if with_ref:
        tR = time.time()
        resR = S.run_creep(st1, mat, steps, out_times, grab)
        tR = time.time() - tR
    out_t = sorted(resE["outputs"])
    xs_np = xs.cpu().numpy()
    surf_E = torch.stack([resE["outputs"][t]["surf"] for t in out_t], 1).cpu().numpy()
    conv_E = torch.stack([resE["outputs"][t]["conv"] for t in out_t], 1).cpu().numpy()
    if resR is not None:
        surf_R = torch.stack([resR["outputs"][t]["surf"] for t in out_t], 1).cpu().numpy()
        conv_R = torch.stack([resR["outputs"][t]["conv"] for t in out_t], 1).cpu().numpy()
        u1 = st1.u[:, :, 0, :].cpu().numpy()
        background = np.abs(-(surf_R[..., 1] - u1[:, None, :, 1])).max(axis=2) * 1e3
    else:
        surf_R = np.repeat(st1.u[:, None, :, 0, :].cpu().numpy(), len(out_t), axis=1)
        conv_R = np.zeros_like(conv_E)
        background = np.full((B, len(out_t)), np.nan)
    d = surf_E - surf_R
    s = -d[..., 1]
    ux = d[..., 0]
    eq_pil = (stE.eps_eq.mean(-1) * pil).sum((1, 2)) / pil.sum((1, 2)).clamp(min=1)
    out = {"t": np.asarray(out_t), "xs": xs_np, "s": s.astype(np.float32), "ux": ux.astype(np.float32),
           "conv_central_m": (conv_E - conv_R), "background_mm": background,
           "pillar_creep_eq_mean_final": eq_pil.cpu().numpy(), "n_chambers": n_ch, "pillar_elems": info["pillar_elems"],
           "adjust_max_m": info["adjust_max_m"].cpu().numpy(), "nx": mesh.nx, "nz": mesh.nz,
           "stats": dict(S.stats), "newton_E": stE.stats.get("newton", []), "failed": S.failed.cpu().numpy(),
           "time_s": {"total": time.time() - t_start, "exc": tE, "ref": tR},
           "seam_roof_depth_centre_m": None}
    # seam roof depth below the surface at the panel centre (per member)
    I = info["interfaces"].cpu().numpy()
    ic = np.argmin(np.abs(xs_np - PANEL_CENTRE), axis=1)
    out["seam_roof_depth_centre_m"] = I[np.arange(B), ic, 0] - I[np.arange(B), ic, 6]
    return out
