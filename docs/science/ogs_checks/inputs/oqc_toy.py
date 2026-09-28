"""Toy plane-strain section for the OGS quick checks (S3, S4, S7, S8).

VERIFICATION TOY. Not SKRU-1, not a forecast, not calibrated, not validated.
Every number below carries a source id or an explicit status (see PARAMS).
Coordinates: x horizontal (0 = panel centre, symmetry plane), y = elevation relative to the
ground surface (y = 0 at the surface, negative downwards). Units: SI (m, Pa, s, kg/m3).
"""
from __future__ import annotations

import math

import numpy as np

import oqc_common as C

# ----------------------------------------------------------------------------- geometry
# Column read from the graphical section of borehole 128 (block 201), SRC-011 fig. 4.7a,
# EV-VN-S011-2093 (FACT, GRAPH_DIGITIZED_APPROX +-3-5 m): surface +174.40; salt mirror ~ -11.5;
# PKS ~ -20...-31; KP ~ -31...-113; SP ~ -113...-130 m abs.  One borehole != SKRU-1 (MODEL_CHOICE).
Y_SURF = 0.0
Y_SALT_MIRROR = -185.9   # 174.4 + 11.5
Y_PKS_TOP = -194.4       # 174.4 + 20  (PP salt part above: same material as PKS)
Y_KP_TOP = -205.4        # 174.4 + 31
Y_SP_TOP = -287.4        # 174.4 + 113
Y_ROOF = -294.0          # MODEL_CHOICE: KrII placed inside SP (287.4-304.4)
SEAM_H = 5.5             # KrII thickness 5.5 m (SRC-025/-037 text; CF-STR-010), within design 4.7-8.7 m
Y_FLOOR = Y_ROOF - SEAM_H
Y_SP_BOT = -304.4        # 174.4 + 130
Y_BOTTOM = -500.0        # MODEL_CHOICE (inside PdKS, mean ~300 m thick)
X_RIGHT = 1500.0         # MODEL_CHOICE (half-width ~5 H)

ROOM_W = 16.0            # DESIGN SRC-037 fig. 3.27a (KrII outside protected objects), VR-04
PILLAR_W = 11.0          # DESIGN idem
PITCH = ROOM_W + PILLAR_W  # 27 m
N_ROOMS_HALF = 7         # 13 rooms in the panel (room 0 centred on x = 0), MODEL_CHOICE
X_PANEL_EDGE = (N_ROOMS_HALF - 1) * PITCH + ROOM_W / 2.0  # 170 m

INTERFACES = [Y_SURF, Y_SALT_MIRROR, Y_PKS_TOP, Y_KP_TOP, Y_SP_TOP, Y_ROOF, Y_FLOOR, Y_SP_BOT, Y_BOTTOM]

# material ids
M_OB, M_SALT_UP, M_KP, M_SYLV, M_ROOM, M_PDKS = 0, 1, 2, 3, 4, 5

# ----------------------------------------------------------------------------- materials
# unit weights (normative, VKM-SRC-012 p.26 tab. 1.1; MR-MECH-0018...0021): rho = gamma / g
GAMMA = {"marl": 0.022e6, "rock_salt": 0.022e6, "sylvinite": 0.021e6}
RHO = {k: v / C.G for k, v in GAMMA.items()}

# Young's moduli: salts = median of LAB VKM_REGIONAL rows of mechanics_evidence_catalog
# (rock salt 34 rows -> 9.5 GPa; sylvinite 24 rows -> 10.8 GPa), Transfer LAB->MASSIF factor 1
# (ENGINEERING_ASSUMPTION; conflict MR-CONF-002). Overburden: normative marl 600 MPa (MR-MECH-0064).
E = {"marl": 600e6, "rock_salt": 9.5e9, "sylvinite": 10.8e9}
NU = {"marl": 0.3, "rock_salt": 0.3, "sylvinite": 0.2}   # normative MR-MECH-0539/0541/0542

# Creep: Norton law CL-15 (VKM-SRC rheology catalogue MR-RHEO-CL-15; NON_VKM_ANALOG Gremyachinsk,
# CALIBRATED_EFFECTIVE_MODEL): eps_rate[1/day] = k * sigma[MPa]^n  ->  OGS CreepBGRa with
# sigma0 = 1 MPa, Q = 0, A = k / 86400 [1/s].  Scenario factor f in {0.1, 1, 10} multiplies A.
CREEP_K = {"rock_salt": 1.32e-13, "sylvinite": 2.12e-13}  # MPa^-n / day
CREEP_N = {"rock_salt": 6.5, "sylvinite": 6.5}
T_REF = 293.15  # K; irrelevant because Q = 0 (temperature dependence not represented)

# Room / backfill material
E_VOID = 0.1e6   # MODEL_CHOICE numerical void (1e-5 of salt modulus), nu = 0, rho = 0
NU_VOID = 0.0
BACKFILL = {  # ENGINEERING_ASSUMPTION scenario values (no backfill modulus in the corpus: UNKNOWN)
    "none": None,
    "soft": {"E": 50e6, "nu": 0.2},
    "stiff": {"E": 500e6, "nu": 0.2},
}
T_BACKFILL = 10.0 * C.YEAR   # "traditionally 10-20 years" (SRC-037), scenario
T_END = 50.0 * C.YEAR        # MODEL_CHOICE horizon

LAMBDAS = {  # PC-03 DISCRETE_SET (status of each value kept)
    0.45: "NORMATIVE coefficient of roof-layer calc, f.(8.130) SRC-037 (not a measurement)",
    0.6: "measured BKPRU-2 ~350 m (SRC-029 p.128) -> ANALOGUE for SKRU-1",
    0.71: "Kaiser-effect mean GI UrO RAN (cited by SRC-012/-029) -> VKM_REGIONAL, ANALOGUE",
    1.0: "MODEL_CHOICE hydrostatic (SRC-037 par.6.7; Lomakin 2022)",
}
CREEP_FACTORS = [0.1, 1.0, 10.0]


def mat_lith(mid: int) -> str:
    return {M_OB: "marl", M_SALT_UP: "rock_salt", M_KP: "rock_salt", M_SYLV: "sylvinite",
            M_PDKS: "rock_salt"}[mid]


# ----------------------------------------------------------------------------- lithostatic stress
def layer_table():
    """(y_top, y_bot, gamma) from surface down (material by layer)."""
    rows = []
    for yt, yb in zip(INTERFACES[:-1], INTERFACES[1:]):
        ym = 0.5 * (yt + yb)
        rows.append((yt, yb, GAMMA[mat_lith(material_at_depth(ym))]))
    return rows


def material_at_depth(y: float) -> int:
    if y > Y_SALT_MIRROR:
        return M_OB
    if y > Y_KP_TOP:
        return M_SALT_UP
    if y > Y_SP_TOP:
        return M_KP
    if y > Y_SP_BOT:
        return M_SYLV
    return M_PDKS


def sigma_v(y):
    """Analytic sigma_yy(y) = -int_y^0 gamma dz (Pa, tension positive)."""
    y = np.asarray(y, float)
    out = np.zeros_like(y)
    for yt, yb, g in layer_table():
        overlap = np.clip(yt - np.maximum(y, yb), 0.0, yt - yb)
        out -= g * overlap
    return out


def sigma_v_expression() -> str:
    """exprtk expression (x, y) for sigma_yy, piecewise linear by layer (nested if)."""
    rows = layer_table()
    s_top = 0.0
    parts = []
    for yt, yb, g in rows:
        parts.append((yb, f"({s_top!r} + {g!r}*(y - ({yt!r})))"))
        s_top = s_top - g * (yt - yb)
    expr = parts[-1][1]
    for yb, e in reversed(parts[:-1]):
        expr = f"if(y > {yb!r}, {e}, {expr})"
    return expr


def room_condition() -> str:
    return (f"(y < {Y_ROOF!r}) and (y > {Y_FLOOR!r}) and (x < {X_PANEL_EDGE!r}) and "
            f"(abs(x - {PITCH!r}*round(x/{PITCH!r})) < {ROOM_W / 2.0!r})")


def initial_stress_expressions(lam: float, with_rooms: bool) -> list[str]:
    sv = sigma_v_expression()
    comps = [f"{lam!r}*({sv})", sv, f"{lam!r}*({sv})", "0.0"]
    if not with_rooms:
        return comps
    rc = room_condition()
    return [f"if({rc}, 0.0, {c})" if c != "0.0" else c for c in comps]


# ----------------------------------------------------------------------------- mesh
def x_lines(refine: int = 1) -> np.ndarray:
    f = int(refine)
    xs = [0.0]
    # half room 0 (0..8): 2f elements; then (pillar 4f el, room 4f el) x 6
    xs += [round(4.0 * (k + 1) / f, 9) for k in range(2 * f)]
    x = 8.0
    for k in range(1, N_ROOMS_HALF):
        for _ in range(4 * f):
            x += PILLAR_W / (4.0 * f)
            xs.append(round(x, 9))
        for _ in range(4 * f):
            x += ROOM_W / (4.0 * f)
            xs.append(round(x, 9))
    assert abs(xs[-1] - X_PANEL_EDGE) < 1e-9, xs[-1]

    # graded outwards
    def size(xx):
        cap = 20.0 if xx < 900.0 else 50.0
        return min(cap, 2.75 * 1.15 ** max(0.0, (xx - X_PANEL_EDGE) / 6.0)) / f
    tail = C.graded_segment(X_PANEL_EDGE, X_RIGHT, size)
    xs += tail[1:]
    return np.array(xs)


def y_lines(refine: int = 1) -> np.ndarray:
    f = int(refine)
    ys = [Y_BOTTOM]
    seam_mid = 0.5 * (Y_ROOF + Y_FLOOR)

    def size(yy):
        d = max(0.0, abs(yy - seam_mid) - SEAM_H / 2.0)
        return min(25.0, 1.8 + 0.12 * d) / f

    ints = sorted(INTERFACES)
    for a, b in zip(ints[:-1], ints[1:]):
        if a == Y_FLOOR and b == Y_ROOF:
            seg = list(np.linspace(a, b, 3 * f + 1))  # 3f elements over the seam
        elif b <= seam_mid:  # below seam: march from b (near seam) down to a
            seg = C.graded_segment(b, a, size)[::-1]
        else:  # above seam: march from a (near seam) up to b
            seg = C.graded_segment(a, b, size)
        ys += seg[1:]
    return np.array(ys)


def toy_mesh(with_rooms: bool = True, refine: int = 1):
    xs, ys = x_lines(refine), y_lines(refine)
    pts, conn, cent = C.quad8_rectilinear(xs, ys)
    mids = np.empty(len(conn), dtype=np.int32)
    for e, (xc, yc) in enumerate(cent):
        m = material_at_depth(yc)
        if with_rooms and Y_FLOOR < yc < Y_ROOF and xc < X_PANEL_EDGE and \
                abs(xc - PITCH * round(xc / PITCH)) < ROOM_W / 2.0:
            m = M_ROOM
        mids[e] = m
    return pts, conn, mids, xs, ys


def column_mesh(width: float = 10.0):
    xs = np.array([0.0, width / 2.0, width])
    ys = y_lines()
    pts, conn, cent = C.quad8_rectilinear(xs, ys)
    mids = np.array([material_at_depth(yc) for _, yc in cent], dtype=np.int32)
    return pts, conn, mids, xs, ys


# ----------------------------------------------------------------------------- parameter table
def parameter_table() -> list[dict]:
    """Rows for the report: value, basis, source ids, status."""
    rows = [
        dict(name="Отметка поверхности / глубины слоёв", value="0 / 185,9 / 194,4 / 205,4 / 287,4 / 304,4 м",
             basis="графическое чтение разреза скв. 128 (блок 201) ±3–5 м", source="VKM-SRC-011 рис. 4.7а; EV-VN-S011-2093",
             status="FACT (GRAPH_DIGITIZED_APPROX) → MODEL_CHOICE для toy (одна скважина ≠ СКРУ-1)"),
        dict(name="Кровля пласта КрII (toy)", value="294,0 м", basis="пласт помещён внутрь сильвинитовой пачки 287,4–304,4 м",
             source="—", status="MODEL_CHOICE"),
        dict(name="Высота камеры (мощность КрII)", value="5,5 м", basis="мощность КрII 5,5 м (текст); проект: вынимаемая 4,7–8,7 м",
             source="VKM-SRC-025/-037 (CF-STR-010: 3,1–6,1); VKM-SRC-037 рис. 3.27а", status="FACT (регион.) → MODEL_CHOICE"),
        dict(name="Камера / целик / шаг", value="16 / 11 / 27 м", basis="проектная сетка КрII вне охраняемых объектов",
             source="VKM-SRC-037 рис. 3.27а (VR-04); строки ГИС VKM-SRC-023 (UNRESOLVED)", status="DESIGN (проект ≠ факт)"),
        dict(name="Панель", value="13 камер, 340 м; одна панель, один пласт, мгновенная выемка", basis="ширина панелей 300–400 м",
             source="VKM-SRC-014, VKM-SRC-037 (MC-GE-03)", status="MODEL_CHOICE"),
        dict(name="Размер модели", value="1500 × 500 м (полуширина), плоская деформация", basis="≈5H в стороны, 200 м соли под пластом",
             source="—", status="MODEL_CHOICE"),
        dict(name="γ мергель / к. соль / сильвинит", value="0,022 / 0,022 / 0,021 МН/м³ (ρ = γ/9,81)",
             basis="нормативные удельные веса", source="VKM-SRC-012 с. 26 табл. 1.1; MR-MECH-0018/0020/0021",
             status="NORMATIVE (FACT как напечатано) → ANALOGUE для СКРУ-1 (GAP-001)"),
        dict(name="E надсолевой толщи (Q+ТКТ+СМТ)", value="600 МПа", basis="нормативный модуль мергеля",
             source="MR-MECH-0064 (VKM-SRC-012 табл. 1.1)", status="NORMATIVE → scenario (норматив ≠ измерение)"),
        dict(name="E каменной соли / сильвинита", value="9,5 / 10,8 ГПа",
             basis="медианы LAB-строк VKM_REGIONAL (34 / 24 строки); перенос LAB→MASSIF с коэффициентом 1",
             source="evidence/materials/mechanics_evidence_catalog.csv (youngs_modulus, LAB)",
             status="DERIVATION (медиана LAB) + ENGINEERING_ASSUMPTION (Transfer ×1; конфликт MR-CONF-002)"),
        dict(name="ν мергель / к. соль / сильвинит", value="0,3 / 0,3 / 0,2", basis="нормативные",
             source="MR-MECH-0542/0539/0541", status="NORMATIVE → ANALOGUE"),
        dict(name="Карналлитовая пачка (КП, 82 м)", value="свойства каменной соли", basis="сосредоточенная замена",
             source="—", status="MODEL_CHOICE (быстрая ползучесть карналлита PC-15 не представлена)"),
        dict(name="Закон ползучести", value="Нортон ε̇ = k·σ^n: к. соль k = 1,32·10⁻¹³, сильвинит 2,12·10⁻¹³ МПа⁻ⁿ·сут⁻¹, n = 6,5",
             basis="откалиброванная модель Гремячинского м-ния (1100–1300 м) → OGS CreepBGRa (σ0 = 1 МПа, Q = 0, A = k/86400)",
             source="MR-RHEO-CL-15", status="DERIVATION, NON_VKM_ANALOG → ANALOGUE с оговоркой Transfer (глубина, T, масштаб)"),
        dict(name="Множитель скорости ползучести", value="×0,1 / ×1 / ×10", basis="разброс законов каталога при 8–15 МПа: CL-10 = CL-15 ×1,5…×25",
             source="MR-RHEO-CL-10, CL-15", status="SCENARIO (маркированный диапазон)"),
        dict(name="Температура", value="не представлена (Q = 0)", basis="в законах ВКМ температурного члена нет; T опытов UNKNOWN",
             source="MECH_RHEO §5.2", status="UNKNOWN (не заменено числом)"),
        dict(name="λ = σh/σv", value="0,45 / 0,6 / 0,71 / 1,0", basis="набор гипотез PC-03, σH = σh (изотропно в плане)",
             source="PC-03; PCF-01", status="DISCRETE_SET: NORMATIVE / ANALOGUE / ANALOGUE / MODEL_CHOICE"),
        dict(name="Закладка: время", value="10 лет после выемки", basis="«традиционно 10–20 лет»",
             source="VKM-SRC-037 (MC-BF-02, CC-18)", status="SCENARIO"),
        dict(name="Закладка: модуль", value="мягкая 50 МПа / жёсткая 500 МПа, ν = 0,2; полный контакт (kзап = 1)",
             basis="модуль закладки в корпусе отсутствует; прочность гидрозакладки 3,5–4,0 МПа в 10 лет (LAB)",
             source="BF-0047 (прочность), BF-0064/0163 (зазор 0,8–1,0 м — не моделируется)",
             status="ENGINEERING_ASSUMPTION (scenario); модуль — UNKNOWN"),
        dict(name="Пустота камеры", value="E = 0,1 МПа, ν = 0, ρ = 0, σ0 = 0", basis="численная замена пустоты (10⁻⁵ от E соли)",
             source="проверено в S5b против Кирша", status="MODEL_CHOICE (численный приём)"),
    ]
    return rows
