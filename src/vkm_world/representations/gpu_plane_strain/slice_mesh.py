"""Batched structured meshes of a layered 2D slice with explicit chambers and pillars (and an optional backfill line
inside the seam), built on the GPU from contact profiles along the trace.

Layers (top -> bottom, interfaces I0..I9): ground surface, TKT, SMT, PKS, SKZ, SZ, KrII roof (= SZ − d_roof),
KrII floor (= roof − h), PDKS, model base. Zones: 0 Q, 1 ТКТ, 2 СМТ+ГГ+ПП, 3 ПКС, 4 СКЗ, 5 СЗ above KrII, 6 KrII outside
the panel, 7 СЗ below KrII, 8 ПдКС; inside the panel 14 = pillar, 15 = chamber (upper part, removed),
16 = chamber (lower part: removed and, if backfill is present, refilled). The same zone ids as the MAPDL/OGS meshes of
the project (16 is new: lower part of a chamber).

Every member of a batch shares the topology (number of columns and rows); geometry differs. Chambers of width
``a`` with pillars ``b`` are centred at the panel centre; all members of a batch must have the same chamber count.
Column positions: ``ce`` elements per chamber, ``pe`` per pillar, and graded columns outside the mined extent mapped
affinely onto each member's outer length (the mined extent differs slightly between members).
"""
from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np
import torch

from .fem import ColumnMesh

ZONE_PILLAR, ZONE_CHAMBER, ZONE_CHAMBER_LOW = 14, 15, 16


@dataclass(frozen=True)
class MeshSpec:
    rows: tuple = (1, 4, 4, 3, 4, 4, 2, 2, 3, 8)  # Q,TKT,SMT,PKS,SKZ,SZ-above,KrII-upper,KrII-lower,SZ-below,PdKS
    chamber_elems: int = 4
    pillar_h: float = 2.6       # target element width in pillars, m
    outer_h0: float = 2.6       # first outer column width at the mined extent, m
    outer_growth: float = 1.2
    outer_hmax: float = 20.0
    pdks_grading: float = 1.6
    min_thickness: float = 1.0

    @property
    def nz(self) -> int:
        return int(sum(self.rows))


def chamber_count(panel_len: float, a: float, b: float) -> int:
    return int(math.floor((panel_len + b) / (a + b) + 1e-9))


def _outer_template(length: float, h0: float, growth: float, hmax: float) -> np.ndarray:
    """Normalised positions (0..1) of graded columns from the mined edge (0) to the model side (1)."""
    pts, x, h = [0.0], 0.0, h0
    while x + h < length - 0.5 * h:
        x += h
        pts.append(x)
        h = min(h * growth, hmax)
    pts.append(length)
    return np.asarray(pts) / length


def column_layout(n_ch: int, a: torch.Tensor, b: torch.Tensor, centre: float, length: float, spec: MeshSpec,
                  pe: int, outer_ref: tuple):
    """Chainage of node columns (B, nx+1) and per-element-column role: 0 outside, 1 pillar, 2 chamber."""
    B = a.shape[0]
    dev, dt = a.device, a.dtype
    tot = n_ch * a + (n_ch - 1) * b
    s0 = centre - 0.5 * tot
    s1 = centre + 0.5 * tot
    t_ch = torch.linspace(0, 1, spec.chamber_elems + 1, device=dev, dtype=dt)
    t_p = torch.linspace(0, 1, pe + 1, device=dev, dtype=dt)
    cols = []
    role = []
    wl, el = outer_ref
    tw = torch.as_tensor(wl, device=dev, dtype=dt)  # normalised from the mined edge outwards (0..1)
    # west: from 0 to s0 (template measured from s0 towards 0)
    west = s0[:, None] - tw.flip(0)[None, :] * s0[:, None]
    cols.append(west[:, :-1])
    role += [0] * (len(wl) - 1)
    x = s0
    for k in range(n_ch):
        cols.append(x[:, None] + a[:, None] * t_ch[None, :-1])
        role += [2] * spec.chamber_elems
        x = x + a
        if k < n_ch - 1:
            cols.append(x[:, None] + b[:, None] * t_p[None, :-1])
            role += [1] * pe
            x = x + b
    te = torch.as_tensor(el, device=dev, dtype=dt)
    east = s1[:, None] + te[None, :] * (length - s1)[:, None]
    cols.append(east)
    role += [0] * (len(el) - 1)
    xs = torch.cat(cols, dim=1)
    return xs, torch.as_tensor(role, device=dev)


def interp_batch(xq: torch.Tensor, xp: torch.Tensor, fp: torch.Tensor) -> torch.Tensor:
    """Piecewise-linear interpolation: xq (B, Q), xp (P,) increasing, fp (B, P, C) -> (B, Q, C)."""
    idx = torch.searchsorted(xp, xq.clamp(xp[0], xp[-1]).contiguous(), right=True).clamp(1, len(xp) - 1)
    x0, x1 = xp[idx - 1], xp[idx]
    w = ((xq - x0) / (x1 - x0)).clamp(0, 1)
    B, Q = xq.shape
    C = fp.shape[-1]
    i0 = (idx - 1)[..., None].expand(B, Q, C)
    i1 = idx[..., None].expand(B, Q, C)
    f0 = torch.gather(fp, 1, i0)
    f1 = torch.gather(fp, 1, i1)
    return f0 + (f1 - f0) * w[..., None]


def build_slice_batch(chain: torch.Tensor, surface: torch.Tensor, contacts: torch.Tensor, base_z: float,
                      d_roof: torch.Tensor, h_seam: torch.Tensor, fill_frac: torch.Tensor, a: torch.Tensor,
                      b: torch.Tensor, n_ch: int, centre: float, spec: MeshSpec = MeshSpec()):
    """Build one batch of meshes.

    chain: (P,) chainage of the profile samples; surface: (B, P) or (P,) ground elevation; contacts: (B, P, 6)
    elevations of TKT, SMT, PKS, SKZ, SZ, PDKS; d_roof, h_seam, fill_frac, a, b: (B,) [m, -, m];
    fill_frac = height of the lower chamber part / seam thickness (the backfill line; 0.5 if no fill is used).
    Returns (ColumnMesh, info dict with per-member interface arrays and adjustments).
    """
    dev, dt = contacts.device, contacts.dtype
    B = contacts.shape[0]
    length = float(chain[-1] - chain[0])
    pe = max(1, int(round(float(b.mean()) / spec.pillar_h)))
    # outer templates for the nominal extent of this batch
    tot_nom = n_ch * float(a.mean()) + (n_ch - 1) * float(b.mean())
    wl = _outer_template(centre - 0.5 * tot_nom, spec.outer_h0, spec.outer_growth, spec.outer_hmax)
    el = _outer_template(length - (centre + 0.5 * tot_nom), spec.outer_h0, spec.outer_growth, spec.outer_hmax)
    xs, role = column_layout(n_ch, a, b, centre, length, spec, pe, (wl, el))
    if surface.dim() == 1:
        surface = surface[None].expand(B, -1)
    prof = torch.cat([surface[..., None], contacts], dim=-1)  # (B, P, 7)
    S = interp_batch(xs, chain.to(dt), prof)  # (B, nx+1, 7)
    mt = spec.min_thickness

    def seam(sz):
        roof = sz - d_roof[:, None]
        return roof, roof - (1.0 - fill_frac[:, None]) * h_seam[:, None], roof - h_seam[:, None]

    roof, fill_line, floor = seam(S[..., 5])
    raw = torch.stack([S[..., 0], S[..., 1], S[..., 2], S[..., 3], S[..., 4], S[..., 5], roof, fill_line, floor,
                       S[..., 6], torch.full_like(roof, base_z)], dim=-1)  # 11 interfaces, 10 layers
    # the same rules as the project's slice adapters: contacts keep >= mt below the one above; the seam is placed
    # by its own geometry below SZ; PDKS is lowered locally to stay >= mt below the seam floor; base below PDKS
    top = [S[..., 0]]
    for k in range(1, 6):
        top.append(torch.minimum(S[..., k], top[-1] - mt))
    roof, fill_line, floor = seam(top[5])
    roof = torch.minimum(roof, top[5] - mt)
    pdks = torch.minimum(S[..., 6], floor - mt)
    base = torch.minimum(torch.full_like(roof, base_z), pdks - mt)
    I = torch.stack(top + [roof, fill_line, floor, pdks, base], dim=-1)
    rows = spec.rows
    zz = [I[..., 0:1]]
    for k, nr in enumerate(rows):
        top, bot = I[..., k], I[..., k + 1]
        t = torch.linspace(0, 1, nr + 1, device=dev, dtype=dt)[1:]
        if k == len(rows) - 1:
            t = t ** spec.pdks_grading
        zz.append(top[..., None] + (bot - top)[..., None] * t)
    Z = torch.cat(zz, dim=-1)  # (B, nx+1, nz+1)
    layer_zone = [0, 1, 2, 3, 4, 5, 6, 6, 7, 8]
    lz = torch.as_tensor(np.repeat(layer_zone, rows), device=dev)
    lay = torch.as_tensor(np.repeat(np.arange(len(rows)), rows), device=dev)
    zone = lz[None, None, :].expand(B, len(role), -1).clone()
    seam_up = (lay == 6)[None, None, :]
    seam_lo = (lay == 7)[None, None, :]
    in_ch = (role == 2)[None, :, None]
    in_p = (role == 1)[None, :, None]
    zone = torch.where(seam_up & in_ch, ZONE_CHAMBER, zone)
    zone = torch.where(seam_lo & in_ch, ZONE_CHAMBER_LOW, zone)
    zone = torch.where((seam_up | seam_lo) & in_p, ZONE_PILLAR, zone)
    mesh = ColumnMesh(xs, Z, zone)
    info = {"interfaces": I, "interfaces_raw": raw, "role": role, "pillar_elems": pe, "n_chambers": n_ch,
            "extent": torch.stack([centre - 0.5 * (n_ch * a + (n_ch - 1) * b), centre + 0.5 * (n_ch * a + (n_ch - 1) * b)], -1),
            "adjust_max_m": (raw - I).abs().amax(dim=(1, 2))}
    return mesh, info


def mesh_from_npz(nodes: np.ndarray, zone: np.ndarray, xs: np.ndarray, nz: int, device="cpu", dtype=torch.float64):
    """ColumnMesh (batch 1) from a project slice mesh (``mesh.npz`` of slice_adapter / slice_chambers)."""
    nx = len(xs) - 1
    Z = torch.as_tensor(nodes[:, 1].reshape(nx + 1, nz + 1), dtype=dtype, device=device)[None]
    return ColumnMesh(torch.as_tensor(xs, dtype=dtype, device=device)[None], Z,
                      torch.as_tensor(zone.reshape(nx, nz), device=device)[None])
