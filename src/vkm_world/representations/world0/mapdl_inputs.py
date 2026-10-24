"""APDL decks for the identification runs of World-0 kernels and pairs (solver as adapter; Ansys MAPDL 2026 R1).

Two families of decks, both with structured meshes generated here (node rows are contiguous, so results are read
row-by-row with *VGET):

1. ``LayeredSlotModel`` — flat layered column (plane strain or axisymmetric) with an open slot in the mined seam
   under the panel. N load steps, one per cell: a unit support DEFICIT (equal and opposite tractions pulling roof
   and floor of cell j towards each other). Output: roof/floor/surface displacements → compliance C and surface
   operator S (``influence`` turns them into K = C⁻¹, G = S·C⁻¹). Linear elasticity; layer interfaces optionally
   frictionless without separation (CONTA172/TARGE169, MPC) — the bounds of the layer–layer contact port.
2. ``PillarCellModel`` — periodic unit cell half-pillar + half-chamber (+ optional backfill with an initial gap)
   between roof and floor slabs, hydrostatic initial stress (INISTATE EPEL, λ = 1), instantaneous excavation (EKILL)
   and Norton creep (TB,CREEP TBOPT 10) in years. Output: pillar shortening, chamber convergence, mean pillar and
   fill stresses by time step.

Units: Pa, m, years (creep). Compression + in the Python API; MAPDL itself uses tension +.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field

import numpy as np

from . import laws


@dataclass(frozen=True)
class Material:
    name: str
    E: float
    nu: float
    rho: float = 2200.0
    eta_MPa_yr: float | None = None   # Norton viscosity (VKM-SRC-045 form, p0 = 1 MPa); None = elastic
    n: float = 5.0

    def apdl(self, mat_id: int) -> list[str]:
        out = [f"MP,EX,{mat_id},{self.E:.6e}", f"MP,PRXY,{mat_id},{self.nu}", f"MP,DENS,{mat_id},{self.rho:.2f}"]
        if self.eta_MPa_yr is not None:
            c1 = laws.mapdl_norton_c1(self.eta_MPa_yr * 1.0e6, self.n)   # η in Pa·yr → C1 for σ in Pa, t in yr
            out += [f"TB,CREEP,{mat_id},,,10", f"TBDATA,1,{c1:.10e},{self.n},0"]
        return out


@dataclass(frozen=True)
class Layer:
    name: str
    thickness: float
    material: Material
    rows: int


def graded_axis(x0: float, x_in: float, dx_in: float, x_end: float, dx_max: float, growth: float = 1.15) -> np.ndarray:
    """Nodes: uniform dx_in on [x0, x_in], then geometric growth to dx_max up to x_end (x_in must be a multiple)."""
    n_in = int(round((x_in - x0) / dx_in))
    xs = list(x0 + dx_in * np.arange(n_in + 1))
    dx = dx_in
    while xs[-1] < x_end - 1e-9:
        dx = min(dx * growth, dx_max)
        xs.append(min(xs[-1] + dx, x_end))
    if x_end - xs[-2] < 0.3 * dx and len(xs) > n_in + 2:
        xs.pop(-2)
    return np.asarray(xs)


@dataclass
class LayeredSlotModel:
    """Flat layers bottom→top; the seam layer ``seam`` is open for |x| ≤ panel/2 (or r ≤ panel/2 if axisym)."""

    layers: list[Layer]                       # bottom → top
    seam: str
    panel: float
    n_cells: int
    half_width: float
    dx_in: float = 2.5
    dx_max: float = 20.0
    axisym: bool = False
    frictionless_interfaces: tuple[str, ...] = ()   # names "lower|upper" of interfaces to slide without separation
    unit_traction: float = 1.0e6
    x_panel: np.ndarray | None = None          # explicit node positions inside the panel (overrides dx_in there)
    pillar_intervals: tuple[tuple[float, float], ...] = ()   # FINE mode: seam material kept on these x intervals
    sigma0: float = 0.0                        # FINE mode: in-situ stress removed from the chamber faces (λ = 1)
    probe_layers: tuple[str, ...] = ()         # also write u_x, u_y on the top row of these layers (VZT probes)
    abutment_wall_loads: bool = True           # FINE mode: also unload the two panel-end faces (horizontal σ0)
    x: np.ndarray = field(init=False)

    def __post_init__(self):
        if self.axisym:
            if self.x_panel is not None:
                raise ValueError("explicit panel nodes are implemented for plane strain only")
            a = self.panel / self.n_cells
            if abs(round(a / self.dx_in) * self.dx_in - a) > 1e-9:
                raise ValueError("cell width must be a multiple of dx_in")
            self.x = graded_axis(0.0, self.panel, self.dx_in, self.half_width, self.dx_max)
            self.cell_edges = np.linspace(0.0, self.panel, self.n_cells + 1)
        else:
            half = self.panel / 2
            if self.x_panel is None:
                a = self.panel / self.n_cells
                if abs(round(a / self.dx_in) * self.dx_in - a) > 1e-9:
                    raise ValueError("cell width must be a multiple of dx_in")
                right = graded_axis(0.0, half, self.dx_in, self.half_width, self.dx_max)
                self.x = np.concatenate([-right[:0:-1], right])
            else:
                xp = np.asarray(self.x_panel, float)
                if abs(xp[0] + half) > 1e-9 or abs(xp[-1] - half) > 1e-9:
                    raise ValueError("x_panel must span [-panel/2, panel/2]")
                dx_edge = float(xp[-1] - xp[-2])
                outer = graded_axis(half, half + dx_edge, dx_edge, self.half_width, self.dx_max)
                self.x = np.concatenate([-outer[::-1], xp[1:-1], outer])
            self.cell_edges = np.linspace(-half, half, self.n_cells + 1)
        for e in self.cell_edges:
            if np.min(np.abs(self.x - e)) > 1e-6:
                raise ValueError("cell edges must coincide with nodes")
        names = [L.name for L in self.layers]
        if self.seam not in names:
            raise ValueError("seam layer missing")
        for itf in self.frictionless_interfaces:
            lo, up = itf.split("|")
            if names.index(up) != names.index(lo) + 1:
                raise ValueError(f"interface {itf} is not between adjacent layers")
        self._build()

    # ---------------------------------------------------------------------------------------------- mesh
    def _build(self):
        nx = len(self.x)
        rows_y: list[float] = []
        row_layer_top: dict[str, int] = {}
        row_layer_bot: dict[str, int] = {}
        y = -sum(L.thickness for L in self.layers)        # top surface at y = 0
        rows_y.append(y)
        prev = None
        for L in self.layers:
            if prev is not None and f"{prev}|{L.name}" in self.frictionless_interfaces:
                rows_y.append(y)                          # duplicate row: upper copy of the interface
            row_layer_bot[L.name] = len(rows_y) - 1
            ys = y + L.thickness * np.arange(1, L.rows + 1) / L.rows
            rows_y.extend(ys.tolist())
            y = float(ys[-1])
            row_layer_top[L.name] = len(rows_y) - 1
            prev = L.name
        self.rows_y = np.asarray(rows_y)
        self.row_bot, self.row_top = row_layer_bot, row_layer_top
        self.nx = nx
        self.nodes = np.array([(xx, yy) for yy in self.rows_y for xx in self.x])   # node id = row*nx + i + 1
        xc = 0.5 * (self.x[:-1] + self.x[1:])
        half = self.panel / 2
        in_slot = (xc <= self.panel + 1e-9) if self.axisym else (np.abs(xc) <= half + 1e-9)
        elems, mats = [], []
        keep = np.zeros_like(in_slot)
        for (pa, pb) in self.pillar_intervals:
            keep |= (xc > pa) & (xc < pb)
        self.in_slot = in_slot & ~keep
        for li, L in enumerate(self.layers):
            r0, r1 = self.row_bot[L.name], self.row_top[L.name]
            for r in range(r0, r1):
                for i in range(nx - 1):
                    if L.name == self.seam and self.in_slot[i]:
                        continue
                    n1 = r * nx + i + 1
                    elems.append((n1, n1 + 1, n1 + 1 + nx, n1 + nx))
                    mats.append(li + 1)
        self.elems = np.asarray(elems, int)
        self.elem_mat = np.asarray(mats, int)
        seam_i = [L.name for L in self.layers].index(self.seam)
        self.roof_row = self.row_bot[self.layers[seam_i + 1].name] if f"{self.seam}|{self.layers[seam_i + 1].name}" \
            in self.frictionless_interfaces else self.row_top[self.seam]
        self.floor_row = self.row_bot[self.seam]
        if f"{self.layers[seam_i - 1].name}|{self.seam}" in self.frictionless_interfaces:
            self.floor_row = self.row_top[self.layers[seam_i - 1].name]
        self.top_row = len(self.rows_y) - 1
        self.depth_roof = -self.rows_y[self.row_top[self.seam]]

    def row_nodes(self, row: int) -> np.ndarray:
        return row * self.nx + np.arange(self.nx) + 1

    # --------------------------------------------------------------------------------------------- loads
    def cell_forces(self, j: int) -> list[tuple[int, float]]:
        """Consistent nodal FY for a unit deficit on cell j: roof pulled down, floor pulled up."""
        a, b = self.cell_edges[j], self.cell_edges[j + 1]
        t = self.unit_traction
        f: dict[int, float] = {}
        for row, sgn in ((self.roof_row, -1.0), (self.floor_row, +1.0)):
            ids = self.row_nodes(row)
            for i in range(self.nx - 1):
                xa, xb = self.x[i], self.x[i + 1]
                if xa < a - 1e-9 or xb > b + 1e-9:
                    continue
                L = xb - xa
                if self.axisym:
                    fa = 2 * math.pi * t * L * (2 * xa + xb) / 6
                    fb = 2 * math.pi * t * L * (xa + 2 * xb) / 6
                else:
                    fa = fb = t * L / 2
                f[ids[i]] = f.get(ids[i], 0.0) + sgn * fa
                f[ids[i + 1]] = f.get(ids[i + 1], 0.0) + sgn * fb
        return sorted(f.items())

    def excavation_forces(self) -> list[tuple[int, str, float]]:
        """FINE mode: remove the in-situ stress σ0 (λ = 1) from the chamber faces: roof/floor spans of open
        elements pulled together, pillar walls pulled into the chambers (consistent nodal forces)."""
        t = self.sigma0
        out: dict[tuple[int, str], float] = {}
        roof = self.row_nodes(self.roof_row)
        floor = self.row_nodes(self.floor_row)
        for i in range(self.nx - 1):
            if not self.in_slot[i]:
                continue
            L = self.x[i + 1] - self.x[i]
            for ids, sgn in ((roof, -1.0), (floor, +1.0)):
                for q in (i, i + 1):
                    out[(int(ids[q]), "FY")] = out.get((int(ids[q]), "FY"), 0.0) + sgn * t * L / 2
        rows = list(range(self.row_bot[self.seam], self.row_top[self.seam] + 1))
        half = self.panel / 2
        for i in range(1, self.nx - 1):
            left_open, right_open = bool(self.in_slot[i - 1]), bool(self.in_slot[i])
            if left_open == right_open:
                continue
            if not self.abutment_wall_loads and abs(abs(self.x[i]) - half) < 1e-6:
                continue
            sgn = -1.0 if left_open else +1.0          # chamber on the left → wall pulled towards −x
            for k, r in enumerate(rows):
                trib = 0.0
                if k > 0:
                    trib += (self.rows_y[r] - self.rows_y[rows[k - 1]]) / 2
                if k + 1 < len(rows):
                    trib += (self.rows_y[rows[k + 1]] - self.rows_y[r]) / 2
                nid = int(r * self.nx + i + 1)
                out[(nid, "FX")] = out.get((nid, "FX"), 0.0) + sgn * t * trib
        return [(n, c, v) for (n, c), v in sorted(out.items())]

    # ---------------------------------------------------------------------------------------------- deck
    def deck(self, title: str) -> str:
        L: list[str] = ["/BATCH", f"/TITLE,{title}", "/NERR,500,100000", "/PREP7",
                        "ET,1,PLANE182", "KEYOPT,1,1,3", f"KEYOPT,1,3,{1 if self.axisym else 2}"]
        for k, lay in enumerate(self.layers, 1):
            L += lay.material.apdl(k)
        for nid, (xx, yy) in enumerate(self.nodes, 1):
            L.append(f"N,{nid},{xx:.6f},{yy:.6f}")
        L.append("TYPE,1")
        cur = None
        for (n1, n2, n3, n4), m in zip(self.elems, self.elem_mat):
            if m != cur:
                L.append(f"MAT,{m}")
                cur = m
            L.append(f"E,{n1},{n2},{n3},{n4}")
        if self.frictionless_interfaces:
            L += ["ET,10,CONTA172", "KEYOPT,10,2,2", "KEYOPT,10,12,4", "KEYOPT,10,4,2", "ET,11,TARGE169", "R,10"]
            names = [lay.name for lay in self.layers]
            for itf in self.frictionless_interfaces:
                lo, up = itf.split("|")
                rlo, rup = self.row_top[lo], self.row_bot[up]
                L += ["REAL,10",
                      f"ESEL,S,MAT,,{names.index(lo) + 1}", f"NSEL,S,NODE,,{rlo * self.nx + 1},{(rlo + 1) * self.nx}",
                      "TYPE,11", "ESURF",
                      f"ESEL,S,MAT,,{names.index(up) + 1}", f"NSEL,S,NODE,,{rup * self.nx + 1},{(rup + 1) * self.nx}",
                      "TYPE,10", "ESURF", "ALLSEL"]
        x0, x1 = self.x[0], self.x[-1]
        ybot = self.rows_y[0]
        if self.pillar_intervals and self.frictionless_interfaces:
            # a pillar between two frictionless faces has a free horizontal rigid-body mode: hold one node per pillar
            for (pa, pb) in self.pillar_intervals:
                i = int(np.argmin(np.abs(self.x - 0.5 * (pa + pb))))
                L.append(f"D,{self.row_bot[self.seam] * self.nx + i + 1},UX,0")
        L += ["ALLSEL", "ESEL,S,TYPE,,1", "NSLE,S", "NSEL,INVE", "D,ALL,ALL,0", "ALLSEL",
              f"NSEL,S,LOC,X,{x0 - 1e-3},{x0 + 1e-3}", f"NSEL,A,LOC,X,{x1 - 1e-3},{x1 + 1e-3}", "D,ALL,UX,0",
              f"NSEL,S,LOC,Y,{ybot - 1e-3},{ybot + 1e-3}", "D,ALL,UX,0", "D,ALL,UY,0", "ALLSEL",
              "FINISH", "/SOLU", "ANTYPE,STATIC", "OUTRES,ALL,LAST"]
        if self.pillar_intervals:
            for nid, comp, val in self.excavation_forces():
                L.append(f"F,{nid},{comp},{val:.8e}")
            L.append("SOLVE")
            n_ls = 1
        else:
            for j in range(self.n_cells):
                L.append("FDELE,ALL,ALL")
                for nid, fy in self.cell_forces(j):
                    L.append(f"F,{nid},FY,{fy:.8e}")
                L.append("SOLVE")
            n_ls = self.n_cells
        L += ["FINISH", "/POST1", f"*DIM,UR,ARRAY,{self.nx}", f"*DIM,UF,ARRAY,{self.nx}",
              f"*DIM,UT,ARRAY,{self.nx}", f"*DIM,XT,ARRAY,{self.nx}"]
        rr, rf, rt = self.roof_row * self.nx + 1, self.floor_row * self.nx + 1, self.top_row * self.nx + 1
        for j in range(1, n_ls + 1):
            L += [f"SET,{j},LAST", f"*VGET,UR(1),NODE,{rr},U,Y", f"*VGET,UF(1),NODE,{rf},U,Y",
                  f"*VGET,UT(1),NODE,{rt},U,Y", f"*VGET,XT(1),NODE,{rt},U,X",
                  f"*CFOPEN,ls{j:03d},txt", "*VWRITE,UR(1),UF(1),UT(1),XT(1)", "(4E20.11)", "*CFCLOS"]
            for q, name in enumerate(self.probe_layers):
                rp = self.row_top[name] * self.nx + 1
                L += [f"*VGET,UR(1),NODE,{rp},U,X", f"*VGET,UF(1),NODE,{rp},U,Y",
                      f"*CFOPEN,pr{j:03d}_{q},txt", "*VWRITE,UR(1),UF(1)", "(2E20.11)", "*CFCLOS"]
        L += ["FINISH", "/EXIT,NOSAVE"]
        return "\n".join(L) + "\n"

    # ------------------------------------------------------------------------------------------ results
    def _cell_mean(self, values: np.ndarray, j: int) -> float:
        a, b = self.cell_edges[j], self.cell_edges[j + 1]
        m = (self.x >= a - 1e-9) & (self.x <= b + 1e-9)
        xs, vs = self.x[m], values[m]
        w = xs if self.axisym else np.ones_like(xs)
        return float(np.trapezoid(vs * w, xs) / np.trapezoid(w, xs))

    def assemble(self, per_ls: list[np.ndarray]) -> dict:
        """per_ls[j] = array (nx, 4) of roof uy, floor uy, top uy, top ux. Returns C, S, Sx (per unit traction)."""
        n = self.n_cells
        C = np.zeros((n, n))
        S = np.zeros((self.nx, n))
        Sx = np.zeros((self.nx, n))
        for j, arr in enumerate(per_ls):
            conv = arr[:, 1] - arr[:, 0]            # floor up (+) minus roof down (−) → convergence +
            for i in range(n):
                C[i, j] = self._cell_mean(conv, i) / self.unit_traction
            S[:, j] = -arr[:, 2] / self.unit_traction
            Sx[:, j] = arr[:, 3] / self.unit_traction
        return {"C": C, "S": S, "Sx": Sx, "x": self.x.copy(), "cell_edges": self.cell_edges.copy()}


# ---------------------------------------------------------------------------------------- pillar unit cell
@dataclass
class PillarCellModel:
    """Periodic unit cell: x ∈ [0, a/2] from the pillar centre to the chamber centre (symmetry u_x = 0 on both).

    Rows: floor slab (bottom, u_y = 0) | seam (pillar for x < w/2, chamber for x ≥ w/2) | roof slab (top pressure σ0).
    Initial hydrostatic stress σ0 in all solids (INISTATE EPEL; λ = 1); LS1 equilibrium; LS2 kills the chamber
    (instantaneous mining at t = 0); then creep steps. Optional backfill: own elements in the chamber from the floor
    to ``fill_height`` (shared floor nodes, zero initial stress), contact with the roof (initial gap) and frictionless
    contact with the pillar wall. ``roof_contact`` = 'bonded' | 'frictionless' at the pillar–roof and pillar–floor
    faces (no separation, MPC). Load history: list of (t_end [yr], σ_top [Pa], n_substeps).
    """

    a: float
    w: float
    h: float
    t_roof: float
    t_floor: float
    pillar: Material
    roof: Material
    floor: Material
    sigma0: float
    history: list[tuple[float, float, int]]
    dx: float = 0.25
    dy_seam: float = 0.5
    roof_contact: str = "bonded"
    fill: Material | None = None
    fill_height: float | None = None
    crplimit: float = 0.2
    contact_fkn: float | None = None      # normal penalty factor of the fill contacts (None = MAPDL default)
    max_substep_factor: int = 50

    def __post_init__(self):
        if self.roof_contact not in ("bonded", "frictionless"):
            raise ValueError("roof_contact must be bonded or frictionless")
        half = self.a / 2
        xp = self.w / 2
        nxp = max(2, int(round(xp / self.dx)))
        nxc = max(2, int(round((half - xp) / self.dx)))
        self.x = np.concatenate([np.linspace(0, xp, nxp + 1), np.linspace(xp, half, nxc + 1)[1:]])
        self.i_wall = nxp
        ny_seam = max(4, int(round(self.h / self.dy_seam)))
        y_seam = np.linspace(0.0, self.h, ny_seam + 1)
        y_floor = np.unique(np.concatenate([np.linspace(-self.t_floor, -2.0, 14), np.linspace(-2.0, 0.0, 5)]))
        y_roof = np.unique(np.concatenate([np.linspace(self.h, self.h + 2.0, 5),
                                           np.linspace(self.h + 2.0, self.h + self.t_roof, 14)]))
        self.y_floor, self.y_seam, self.y_roof = y_floor, y_seam, y_roof
        self._build()

    def _build(self):
        nx = len(self.x)
        fric = self.roof_contact == "frictionless"
        rows: list[tuple[float, str]] = [(y, "F") for y in self.y_floor]           # floor rows incl. y = 0
        if fric:
            rows.append((0.0, "Sbot"))                                            # seam bottom copy
        rows += [(y, "S") for y in self.y_seam[1:-1]]
        if fric:
            rows.append((self.h, "Stop"))
        rows += [(y, "R") for y in self.y_roof]                                    # roof rows incl. y = h
        self.rows = rows
        self.nx = nx
        nodes = [(xx, y) for y, _ in rows for xx in self.x]
        ridx = {}
        for k, (y, tag) in enumerate(rows):
            ridx.setdefault(tag, []).append(k)
        self.r_floor_top = ridx["F"][-1]
        self.r_roof_bot = ridx["R"][0]
        self.r_seam_bot = ridx["Sbot"][0] if fric else self.r_floor_top
        self.r_seam_top = ridx["Stop"][0] if fric else self.r_roof_bot
        self.r_top = len(rows) - 1
        seam_rows = [self.r_seam_bot] + ridx.get("S", []) + [self.r_seam_top]
        elems, mats = [], []

        def quad(r0, r1, i):
            n1 = r0 * nx + i + 1
            n4 = r1 * nx + i + 1
            return (n1, n1 + 1, n4 + 1, n4)

        for k in range(len(ridx["F"]) - 1):
            for i in range(nx - 1):
                elems.append(quad(ridx["F"][k], ridx["F"][k + 1], i))
                mats.append(3)
        for k in range(len(seam_rows) - 1):
            for i in range(nx - 1):
                elems.append(quad(seam_rows[k], seam_rows[k + 1], i))
                mats.append(1 if i < self.i_wall else 4)         # 4 = ore of the chamber (killed at LS2)
        for k in range(len(ridx["R"]) - 1):
            for i in range(nx - 1):
                elems.append(quad(ridx["R"][k], ridx["R"][k + 1], i))
                mats.append(2)
        # with frictionless pillar contacts the chamber ore must stay connected to the slabs: share slab nodes
        if fric:
            for e, m in enumerate(mats):
                if m == 4:
                    n = list(elems[e])
                    for q in range(4):
                        row, col = divmod(n[q] - 1, nx)
                        if row == self.r_seam_bot:
                            n[q] = self.r_floor_top * nx + col + 1
                        elif row == self.r_seam_top:
                            n[q] = self.r_roof_bot * nx + col + 1
                    elems[e] = tuple(n)
        self.nodes = np.asarray(nodes)
        self.n_base = len(nodes)
        # backfill: own nodes in the chamber, bottom row shared with the floor top
        self.fill_elems = []
        if self.fill is not None:
            if self.fill_height is None or not 0 < self.fill_height < self.h:
                raise ValueError("fill_height must be inside (0, h)")
            nyf = max(3, int(round(self.fill_height / self.dy_seam)))
            yf = np.linspace(0.0, self.fill_height, nyf + 1)
            xs = self.x[self.i_wall:]
            nxf = len(xs)
            extra = []
            idmap = {}
            for kk, y in enumerate(yf[1:], 1):
                for ii, xx in enumerate(xs):
                    idmap[(kk, ii)] = self.n_base + len(extra) + 1
                    extra.append((xx, y))
            for ii in range(nxf):
                idmap[(0, ii)] = self.r_floor_top * nx + self.i_wall + ii + 1
            for kk in range(nyf):
                for ii in range(nxf - 1):
                    self.fill_elems.append((idmap[(kk, ii)], idmap[(kk, ii + 1)], idmap[(kk + 1, ii + 1)],
                                            idmap[(kk + 1, ii)]))
            self.fill_top_first = idmap[(nyf, 0)]
            self.fill_top_last = idmap[(nyf, nxf - 1)]
            self.fill_wall_nodes = [idmap[(kk, 0)] for kk in range(1, nyf + 1)]
            self.nodes = np.vstack([self.nodes, np.asarray(extra)])
        self.elems = elems
        self.elem_mat = mats

    def deck(self, title: str) -> str:
        nx = self.nx
        Lines = ["/BATCH", f"/TITLE,{title}", "/NERR,500,100000", "/PREP7", "ET,1,PLANE182", "KEYOPT,1,1,0",
                 "KEYOPT,1,3,2"]
        Lines += self.pillar.apdl(1) + self.roof.apdl(2) + self.floor.apdl(3) + self.pillar.apdl(4)
        if self.fill is not None:
            Lines += self.fill.apdl(5)
        for nid, (xx, yy) in enumerate(self.nodes, 1):
            Lines.append(f"N,{nid},{xx:.6f},{yy:.6f}")
        Lines.append("TYPE,1")
        cur = None
        for (n1, n2, n3, n4), m in zip(self.elems, self.elem_mat):
            if m != cur:
                Lines.append(f"MAT,{m}")
                cur = m
            Lines.append(f"E,{n1},{n2},{n3},{n4}")
        n_solid = len(self.elems)
        if self.fill is not None:
            Lines.append("MAT,5")
            for (n1, n2, n3, n4) in self.fill_elems:
                Lines.append(f"E,{n1},{n2},{n3},{n4}")
        # contacts
        if self.roof_contact == "frictionless":
            Lines += ["ET,10,CONTA172", "KEYOPT,10,2,2", "KEYOPT,10,12,4", "KEYOPT,10,4,2", "ET,11,TARGE169", "R,10"]
            for slab_row, seam_row, slab_mat in ((self.r_roof_bot, self.r_seam_top, 2),
                                                 (self.r_floor_top, self.r_seam_bot, 3)):
                Lines += ["REAL,10",
                          f"ESEL,S,MAT,,{slab_mat}", f"NSEL,S,NODE,,{slab_row * nx + 1},{slab_row * nx + self.i_wall + 1}",
                          "TYPE,11", "ESURF",
                          "ESEL,S,MAT,,1", f"NSEL,S,NODE,,{seam_row * nx + 1},{seam_row * nx + self.i_wall + 1}",
                          "TYPE,10", "ESURF", "ALLSEL"]
        if self.fill is not None:
            Lines += ["ET,12,CONTA172", "KEYOPT,12,2,0", "KEYOPT,12,12,0", "KEYOPT,12,9,0",
                      "ET,13,TARGE169", "R,12", f"RMODIF,12,6,{-2.0 * self.h:.3f}"]
            if self.contact_fkn is not None:
                Lines += [f"RMODIF,12,3,{self.contact_fkn}", "KEYOPT,12,10,2"]
            Lines += [
                      # fill top ↔ roof: the initial geometric gap is kept (KEYOPT(9) = 0), pinball 2h (absolute)
                      "REAL,12", "ESEL,S,MAT,,2",
                      f"NSEL,S,NODE,,{self.r_roof_bot * nx + self.i_wall + 1},{(self.r_roof_bot + 1) * nx}",
                      "TYPE,13", "ESURF", "ESEL,S,MAT,,5", f"NSEL,S,NODE,,{self.fill_top_first},{self.fill_top_last}",
                      "TYPE,12", "ESURF", "ALLSEL",
                      "ET,14,CONTA172", "KEYOPT,14,12,0", "KEYOPT,14,9,0", "ET,15,TARGE169", "R,14",
                      "REAL,14", "ESEL,S,MAT,,1",
                      f"NSEL,S,LOC,X,{self.x[self.i_wall] - 1e-4},{self.x[self.i_wall] + 1e-4}",
                      f"NSEL,R,LOC,Y,{-1e-4},{self.fill_height + 1e-4}", "TYPE,15", "ESURF",
                      "ESEL,S,MAT,,5", "NSEL,NONE"]
            for nid in self.fill_wall_nodes:
                Lines.append(f"NSEL,A,NODE,,{nid}")
            Lines += [f"NSEL,A,NODE,,{self.r_floor_top * nx + self.i_wall + 1}", "TYPE,14", "ESURF", "ALLSEL"]
        half = self.x[-1]
        ybot = self.y_floor[0]
        ytop = self.y_roof[-1]
        Lines += ["ALLSEL", "ESEL,S,ENAME,,182", "NSLE,S", "NSEL,INVE", "D,ALL,ALL,0", "ALLSEL",
                  f"NSEL,S,LOC,X,{-1e-4},{1e-4}", f"NSEL,A,LOC,X,{half - 1e-4},{half + 1e-4}", "D,ALL,UX,0",
                  f"NSEL,S,LOC,Y,{ybot - 1e-4},{ybot + 1e-4}", "D,ALL,UY,0", "ALLSEL",
                  f"NSEL,S,LOC,Y,{ytop - 1e-4},{ytop + 1e-4}", "ESLN,S", f"SF,ALL,PRES,{self.sigma0:.6e}", "ALLSEL",
                  "FINISH", "/SOLU", "ANTYPE,STATIC", "NLGEOM,OFF", "KBC,1", "OUTRES,ALL,ALL"]
        # initial hydrostatic elastic strain of the solids (not of the fill)
        Lines += ["INISTATE,SET,DTYP,EPEL"]
        mats = {1: self.pillar, 2: self.roof, 3: self.floor, 4: self.pillar}
        for eid, m in enumerate(self.elem_mat, 1):
            mat = mats[m]
            e0 = -self.sigma0 * (1 - 2 * mat.nu) / mat.E
            Lines.append(f"INISTATE,DEFINE,{eid},,,,{e0:.8e},{e0:.8e},{e0:.8e},0")
        Lines += ["RATE,OFF", "TIME,1e-7", "NSUBST,1", "SOLVE",
                  "ESEL,S,MAT,,4", "EKILL,ALL", "ALLSEL"]
        Lines += ["TIME,2e-7", "NSUBST,1", "SOLVE", "RATE,ON", "AUTOTS,ON",
                  f"CUTCONTROL,CRPLIMIT,{self.crplimit},1", "NEQIT,40"]
        prev_s = self.sigma0
        for t_end, s_top, nsub in self.history:
            if s_top != prev_s:
                Lines += [f"NSEL,S,LOC,Y,{ytop - 1e-4},{ytop + 1e-4}", "ESLN,S", f"SF,ALL,PRES,{s_top:.6e}", "ALLSEL"]
                prev_s = s_top
            Lines += [f"TIME,{t_end}", f"NSUBST,{nsub},{self.max_substep_factor * nsub},{max(1, nsub // 4)}",
                      "SOLVE"]
        Lines += ["FINISH", "/POST1", "*GET,NSETS,ACTIVE,0,SET,NSET", f"*DIM,RES,ARRAY,NSETS,8"]
        rt = self.r_seam_top * nx
        rb = self.r_seam_bot * nx
        rr = self.r_roof_bot * nx
        rf = self.r_floor_top * nx
        Lines += [
            "*DO,K,1,NSETS", "SET,,,,,,,K", "*GET,TT,ACTIVE,0,SET,TIME", "RES(K,1)=TT",
            # pillar shortening: mean over the pillar top minus bottom faces (trapezoid weights)
            "SUM1=0", "SUMW=0",
        ]
        for i in range(self.i_wall + 1):
            wgt = (self.x[min(i + 1, self.i_wall)] - self.x[max(i - 1, 0)]) / 2
            Lines.append(f"SUM1=SUM1+({wgt:.6f})*(UY({rb + i + 1})-UY({rt + i + 1}))")
            Lines.append(f"SUMW=SUMW+({wgt:.6f})")
        # mean convergence over the whole cell (roof bottom row minus floor top row, trapezoid weights)
        Lines += ["SUM3=0", "SUMW3=0"]
        for i in range(nx):
            wgt = (self.x[min(i + 1, nx - 1)] - self.x[max(i - 1, 0)]) / 2
            Lines.append(f"SUM3=SUM3+({wgt:.6f})*(UY({rf + i + 1})-UY({rr + i + 1}))")
            Lines.append(f"SUMW3=SUMW3+({wgt:.6f})")
        Lines += ["RES(K,8)=SUM3/SUMW3"]
        Lines += ["RES(K,2)=SUM1/SUMW",
                  f"RES(K,3)=UY({rf + nx})-UY({rr + nx})"]            # chamber-centre convergence
        # mean vertical stress in the pillar (element SY averaged over pillar elements, compression +)
        Lines += ["ESEL,S,MAT,,1", "ETABLE,SYP,S,Y", "ETABLE,VOLP,VOLU", "SMULT,SYV,SYP,VOLP", "SSUM",
                  "*GET,SSY,SSUM,,ITEM,SYV", "*GET,SVOL,SSUM,,ITEM,VOLP", "RES(K,4)=-SSY/SVOL", "ALLSEL"]
        if self.fill is not None:
            Lines += ["ESEL,S,MAT,,5", "ETABLE,SYF,S,Y", "ETABLE,VOLF,VOLU", "SMULT,SYFV,SYF,VOLF", "SSUM",
                      "*GET,FSY,SSUM,,ITEM,SYFV", "*GET,FVOL,SSUM,,ITEM,VOLF", "RES(K,5)=-FSY/FVOL",
                      "ETABLE,SXF,S,X", "SMULT,SXFV,SXF,VOLF", "SSUM", "*GET,FSX,SSUM,,ITEM,SXFV",
                      "RES(K,7)=-FSX/FVOL", "ALLSEL"]
        else:
            Lines += ["RES(K,5)=0", "RES(K,7)=0"]
        if self.pillar.eta_MPa_yr is not None:
            Lines += ["ESEL,S,MAT,,1", "ETABLE,CRP,NL,CREQ", "SMULT,CRV,CRP,VOLP", "SSUM",
                      "*GET,SCR,SSUM,,ITEM,CRV", "RES(K,6)=SCR/SVOL", "ALLSEL"]
        else:
            Lines += ["RES(K,6)=0"]
        Lines += ["ETABLE,ERAS", "*ENDDO",
                  "*CFOPEN,history,txt",
                  "*VWRITE,RES(1,1),RES(1,2),RES(1,3),RES(1,4),RES(1,5),RES(1,6),RES(1,7),RES(1,8)",
                  "(8E20.11)", "*CFCLOS", "FINISH", "/EXIT,NOSAVE"]
        return "\n".join(Lines) + "\n"

    @staticmethod
    def parse_history(text: str) -> dict:
        arr = np.array([[float(v) for v in line.split()] for line in text.strip().splitlines()])
        return {"t": arr[:, 0], "pillar_shortening": arr[:, 1], "chamber_convergence": arr[:, 2],
                "sigma_pillar": arr[:, 3], "sigma_fill": arr[:, 4], "eps_cr_pillar": arr[:, 5],
                "sigma_fill_x": arr[:, 6], "cell_convergence": arr[:, 7]}
