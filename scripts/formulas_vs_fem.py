"""Сравнение численных 2D-расчётов оседаний (MAPDL/OGS, упругость и ползучесть) с формулами корпуса ВКМ.

Читает (только чтение) результаты расчётов направления G и считает по формулам модуля
``vkm_world.physics.subsidence_formulas`` те же сценарии: панель КрII 600 м, глубина ≈ 321 м до кровли (327 м до почвы),
вынимаемая мощность 5,5 м, коэффициенты извлечения 37,78 % (A) и 51 % (B, C, D), закладка (C).

Все числа на выходе — МОДЕЛЬНЫЕ (FEM) или НОРМАТИВНО-РАСЧЁТНЫЕ (формулы); наблюдения ПЛ 12 — только диагностическое
сопоставление порядка величин и формы, без подбора параметров (калибровка запрещена).

Использование (пути — аргументы, в коде их нет):
    python scripts/formulas_vs_fem.py --fem-root <G_solver> --out <каталог> \
        [--creep-json <G_creep/results_creep.json>] [--pl12-csv <.../fig_4_7b/series.csv>] \
        [--matlab-csv <формулы, посчитанные MATLAB>]
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import sys
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from vkm_world.physics import subsidence_formulas as sf  # noqa: E402

M0 = 5.5  # вынимаемая мощность в сценариях FEM (СКРУ-1: 5,10–6,30 м, SRC-252 табл. 1.2; 5,5 — MODEL_CHOICE FEM)
OMEGA = {"A": 0.3778, "B": 0.51, "C": 0.51, "D": 0.51}  # SRC-096 p0005 табл. 2 (через сценарии FEM); D — граница
B_FILL = {"гидравлическая": 0.18, "механическая солеотходами": 0.25}  # VKM-SRC-065 с. 29
A_FILL = (0.75, 1.0)  # сценарий степени заполнения для варианта C (в FEM не задана) — MODEL_CHOICE
DELTA_CUR, PSI_CUR = 55.0, 55.0   # VKM-SRC-012 с. 54 (ред. 2022, постоянные границы, полная подработка)
DELTA_92, PSI_92 = 50.0, 55.0     # VKM-SRC-037 с. 337 (ред. 1990/1992)
AVERSHIN_B = (0.15, 0.18)         # B = (0,15–0,18)·H — VKM-SRC-208 с. 67 (Авершин; уголь)
A_RATIO_92 = (0.2, 0.26)          # ξ0 = a·η0 (8.137) VKM-SRC-037 с. 338
C_SKRU1 = (0.192, 0.303)          # проектные C, СКРУ-1 ГУ-2, пласт Кр.2: VKM-SRC-252 r0024 (ПРГР 2013) — не измерения
T_YEARS = (10.0, 20.0, 40.0)      # сравнение во времени; t — годы (VKM-SRC-103 с. 26 (1.12))
BASE = 15.0                       # база разностей для модельных профилей, м (приведение к 15 м: (1.15), MM-OBS-002)
PALETTE = ["#2a78d6", "#eb6834", "#1baf7a", "#eda100", "#e87ba4", "#008300", "#6250d6", "#e34948"]
INK, INK2, GRID = "#0b0b0b", "#52514e", "#d9d8d4"


def sha256(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


# ------------------------------------------------------------------------------------------------- FEM

def load_fem(root: Path) -> dict:
    mesh = np.load(root / "step10_slice" / "mesh.npz")
    nz = int(mesh["nz"]); xs = mesh["xs"]; I = mesh["interfaces"]; p0, p1 = (float(v) for v in mesh["panel"])
    rows = np.cumsum(mesh["rows_per"])
    roof_row, floor_row = int(rows[5]), int(rows[6])
    top = np.arange(len(xs)) * (nz + 1)
    ic = int(np.argmin(abs(xs - 0.5 * (p0 + p1))))
    res = json.loads((root / "results.json").read_text(encoding="utf-8"))
    cases = {}
    for case in res["cases"]:
        U1 = np.loadtxt(root / "step6_cases" / case / "u_ls1.txt")
        U4 = np.loadtxt(root / "step6_cases" / case / "u_ls4.txt")
        d = U4 - U1
        s = -d[top, 1]
        h = d[top, 0]
        cols = np.arange(len(xs)) * (nz + 1)
        conv = -(d[cols + roof_row, 1] - d[cols + floor_row, 1])
        cases[case] = {"s": s, "u_h": h, "conv": conv}
    return {"x": xs, "panel": (p0, p1), "H_roof": float(I[ic, 0] - I[ic, 6]), "H_floor": float(I[ic, 0] - I[ic, 7]),
            "cases": cases, "results": res,
            "inputs": {"mesh.npz": sha256(root / "step10_slice" / "mesh.npz"),
                       "results.json": sha256(root / "results.json")}}


def load_creep(path: Path) -> dict:
    d = json.loads(path.read_text(encoding="utf-8"))
    out = {}
    for case, c in d["cases"].items():
        x = np.asarray(c["x"], float)
        prof = {float(t): np.asarray(v, float) / 1000.0 for t, v in c["profiles"].items()}  # мм → м
        conv = {float(r["t_yr"]): float(r["central_m"]) for r in c["convergence"]}
        out[case] = {"x": x, "profiles": prof, "conv_central": conv}
    return {"cases": out, "status": d.get("status"), "effect": d.get("effect_definition"), "sha256": sha256(path)}


def load_pl12(path: Path) -> dict:
    rows = list(csv.DictReader(path.open(encoding="utf-8")))
    out = {}
    for r in rows:
        ep = r["epoch_or_date"]
        out.setdefault(ep, []).append((float(r["x_dist_m_derived"]), float(r["y_value"]) / 1000.0))
    return {ep: np.array(sorted(v)) for ep, v in out.items()} | {"_sha256": sha256(path)}


# ---------------------------------------------------------------------------------------- shape metrics

def shape_metrics(prof, panel, H):
    """Характеристики формы мульды: уровни 50 % и 10 % максимума относительно краёв панели, крутизна краевой части,
    наибольшие наклон и кривизна на единицу максимального оседания (по наклонам и кривизнам самого профиля)."""
    p0, p1 = panel
    x, s = prof.x, prof.s
    smax = float(np.max(s))
    out = {"s_max_m": smax}
    for frac in (0.5, 0.1):
        e = sf.trough_edges(x, s, frac * smax)
        tag = f"{int(frac * 100)}pct"
        if e is None:
            out[f"offset_{tag}_m"] = None
            continue
        off = 0.5 * ((p0 - e[0]) + (e[1] - p1))  # > 0 — точка уровня за краем панели
        out[f"offset_{tag}_m"] = off
        out[f"offset_{tag}_over_H"] = off / H
        out[f"angle_{tag}_deg"] = sf.angle_from_edge(H, off) if off > 0 else 180 - sf.angle_from_edge(H, -off)
    e90, e10 = sf.trough_edges(x, s, 0.9 * smax), sf.trough_edges(x, s, 0.1 * smax)
    if e90 and e10:
        out["edge_zone_90_10_over_H"] = 0.5 * ((e90[0] - e10[0]) + (e10[1] - e90[1])) / H
    out["tilt_over_s"] = float(np.max(np.abs(prof.tilt)) / smax)        # 1/м
    out["curv_over_s"] = float(np.max(np.abs(prof.curvature)) / smax)   # 1/м²
    if np.any(np.isfinite(prof.eps_h)):
        out["eps_over_s"] = float(np.nanmax(np.abs(prof.eps_h)) / smax)  # 1/м
    return out


def knothe_equivalent_angle(x, s, panel, H):
    """Угол β, при котором гауссова кривая Кноте лучше всего повторяет НОРМИРОВАННЫЙ профиль модели.

    Подбор формы к модельному результату (не к наблюдениям) — характеристика FEM, не калибровка параметров мира."""
    p0, p1 = panel
    sn = s / np.max(s)
    best = None
    for beta in np.arange(20.0, 85.01, 0.25):
        k = sf.knothe_profile(x, p0, p1, H, 1.0, beta).s
        k = k / np.max(k)
        err = float(np.sqrt(np.mean((k - sn) ** 2)))
        if best is None or err < best[1]:
            best = (float(beta), err)
    return {"beta_eq_deg": best[0], "rms_misfit": best[1], "r_eq_m": H / math.tan(math.radians(best[0]))}


# -------------------------------------------------------------------------------------- normative runs

def normative_profiles(x, panel, H):
    """Профили формул для ω = 0,3778 и 0,51 (без закладки; η — к концу процесса сдвижения)."""
    p0, p1 = panel
    out = {}
    for tag, omega in (("w378", OMEGA["A"]), ("w510", OMEGA["B"])):
        eta_cur = sf.eta_max_current(M0, omega, 1.0, 0.9)
        eta_92 = sf.eta_max_1992(M0 * omega, omega, 1.0 - omega)  # ΣS/(a+b) = m·ω (прямоугольная камера)
        out[f"UKAZ_T4_{tag}"] = sf.typical_curve_profile(x, p0, p1, H, eta_cur, "VKM_UKAZANIYA_T4_SRC065",
                                                          DELTA_CUR, PSI_CUR, edition="current", strain_method="me")
        for mode in ("MODE2", "MODE3"):
            out[f"N1992_{mode}_{tag}"] = sf.typical_curve_profile(x, p0, p1, H, eta_92, f"VKM1992_T8_26_{mode}",
                                                                  DELTA_92, PSI_92, edition="1992", strain_method="F")
        out[f"KNOTHE55_{tag}"] = sf.knothe_profile(x, p0, p1, H, eta_cur, DELTA_CUR,
                                                    B_avershin=np.mean(AVERSHIN_B) * H)
    return out


def amplitude_table(fem, H):
    rows = []
    for case, d in fem["cases"].items():
        mod, lam, var = case.split("_")
        omega = OMEGA[var]
        eta_k09 = sf.eta_max_current(M0, omega, 1.0, 0.9)
        r = fem["results"]["cases"][case]
        i_c = int(np.argmin(abs(fem["x"] - 0.5 * sum(fem["panel"]))))
        conv_c = float(d["conv"][i_c])
        inside = (fem["x"] >= fem["panel"][0]) & (fem["x"] <= fem["panel"][1])
        vol_s = float(np.trapezoid(d["s"], fem["x"]))
        vol_c = float(np.trapezoid(np.where(inside, d["conv"], 0.0), fem["x"]))
        row = {"case": case, "moduli": mod, "lambda": lam, "variant": var, "omega": omega,
               "s_max_fem_mm": r["s_max_mm"], "seam_conv_centre_fem_mm": conv_c * 1000,
               "transfer_smax_over_conv": r["s_max_mm"] / (conv_c * 1000) if conv_c > 0 else None,
               "transfer_volume_ratio": vol_s / vol_c if vol_c > 0 else None,
               "void_closed_fraction_fem": conv_c / (M0 * omega),
               "eta_norm_k09_mm": eta_k09 * 1000,
               "fem_over_norm": r["s_max_mm"] / (eta_k09 * 1000),
               "eta_norm_skru1_C_min_mm": eta_k09 * sf.loading_factor_k(C_SKRU1[0]) / 0.9 * 1000,
               "eta_norm_skru1_C_max_mm": eta_k09 * sf.loading_factor_k(C_SKRU1[1]) / 0.9 * 1000,
               "fem_over_norm_skru1_C_min": r["s_max_mm"] / (eta_k09 * sf.loading_factor_k(C_SKRU1[0]) / 0.9 * 1000)}
        if var == "C":
            ps = [sf.backfill_factor(a, b) for a in A_FILL for b in B_FILL.values()]
            row["eta_norm_backfill_min_mm"] = min(ps) * eta_k09 * 1000
            row["eta_norm_backfill_max_mm"] = max(ps) * eta_k09 * 1000
        rows.append(row)
    return rows


# --------------------------------------------------------------------------------------------- figures

def _style(ax, xlabel=None, ylabel=None):
    ax.grid(True, color=GRID, linewidth=0.6)
    ax.set_axisbelow(True)
    for sp in ("top", "right"):
        ax.spines[sp].set_visible(False)
    for sp in ("left", "bottom"):
        ax.spines[sp].set_color(INK2)
    ax.tick_params(colors=INK2, labelsize=8)
    if xlabel:
        ax.set_xlabel(xlabel, color=INK2, fontsize=9)
    if ylabel:
        ax.set_ylabel(ylabel, color=INK2, fontsize=9)


def fig_amplitudes(path, amp, creep, pl12):
    import matplotlib.pyplot as plt
    items = []  # (label, value_m, group[, low_m])
    for tag, omega in (("A: ω = 37,8 %", OMEGA["A"]), ("B: ω = 51 %", OMEGA["B"])):
        items.append((f"Норматив, {tag}, C ≥ 0,32 (k = 0,9)", sf.eta_max_current(M0, omega, 1, 0.9), 0))
        items.append((f"Норматив, {tag}, C = 0,19–0,30 (проект СКРУ-1)",
                      sf.eta_max_current(M0, omega, 1, sf.loading_factor_k(C_SKRU1[1])), 0,
                      sf.eta_max_current(M0, omega, 1, sf.loading_factor_k(C_SKRU1[0]))))
        items.append((f"Норматив, {tag}, C = 0,19–0,30, через 40 лет (0,002·t)",
                      sf.time_fraction_ukazaniya(40.0, C_SKRU1[1]) * sf.eta_max_current(M0, omega, 1, sf.loading_factor_k(C_SKRU1[1])), 0,
                      sf.time_fraction_ukazaniya(40.0, C_SKRU1[0]) * sf.eta_max_current(M0, omega, 1, sf.loading_factor_k(C_SKRU1[0]))))
    byc = {r["case"]: r for r in amp}
    for case, lab in (("M10_K0_A", "FEM упругий, LAB×1,0, A"), ("M05_K0_A", "FEM упругий, LAB×0,5, A"),
                      ("M03_K0_B", "FEM упругий, LAB×0,3, B"), ("NORM_K0_B", "FEM упругий, модули «Указаний», B"),
                      ("M05_K0_D", "FEM упругий, граница D, LAB×0,5"), ("NORM_K0_D", "FEM упругий, граница D, модули «Указаний»")):
        items.append((lab, byc[case]["s_max_fem_mm"] / 1000, 1))
    if creep:
        for case, lab in (("M05_FIELD_A27", "FEM ползучесть FIELD, 27 м"), ("M05_LAB_A27", "FEM ползучесть LAB, 27 м"),
                          ("M05_FIELD_B20", "FEM ползучесть FIELD, 20 м"), ("M05_LAB_B20", "FEM ползучесть LAB, 20 м")):
            c = creep["cases"].get(case)
            if c:
                items.append((lab + ", 40 лет", float(np.max(c["profiles"][40.0])), 2))
    if pl12:
        items.append(("ПЛ 12, 2010 г. (наблюдение, другое место, 2 пласта)", float(np.max(pl12["2010"][:, 1])), 3))
    colors = {0: PALETTE[0], 1: PALETTE[1], 2: PALETTE[2], 3: PALETTE[6]}
    names = {0: "формула (норматив)", 1: "FEM упругий мгновенный", 2: "FEM с ползучестью", 3: "наблюдение (диагностика)"}
    fig, ax = plt.subplots(figsize=(8.6, 0.32 * len(items) + 1.9), dpi=150)
    y = np.arange(len(items))[::-1]
    seen = set()
    for yi, it in zip(y, items):
        lab, v, g = it[:3]
        if len(it) > 3:  # диапазон
            ax.plot([it[3], v], [yi, yi], color=colors[g], linewidth=2.0, zorder=2)
            ax.plot(it[3], yi, "o", color=colors[g], markersize=6, markeredgecolor="white", markeredgewidth=1.2, zorder=3)
        ax.plot(v, yi, "o", color=colors[g], markersize=7, markeredgecolor="white", markeredgewidth=1.5, zorder=3,
                label=names[g] if g not in seen else None)
        seen.add(g)
        txt = (f"{it[3] * 1000:.0f}–{v * 1000:.0f} мм" if len(it) > 3 else
               (f"{v * 1000:.0f} мм" if v >= 0.0995 else f"{v * 1000:.1f} мм"))
        ax.text(v * 1.25, yi, txt, va="center", fontsize=7.5, color=INK)
    ax.set_yticks(y, [it[0] for it in items], fontsize=7.5, color=INK)
    ax.set_xscale("log")
    ax.set_xlim(5e-4, 20)
    _style(ax, "максимальное оседание поверхности, м (логарифмическая шкала)")
    ax.legend(loc="upper center", bbox_to_anchor=(0.35, -0.09), ncol=2, fontsize=7.5, frameon=False)
    ax.set_title("Максимальное оседание: формулы, FEM и наблюдение ПЛ 12", fontsize=10, color=INK, loc="left")
    fig.tight_layout()
    fig.savefig(path)
    plt.close(fig)


def fig_shapes(path, x, panel, H, fem, norm, creep):
    import matplotlib.pyplot as plt
    p0, p1 = panel
    xc = 0.5 * (p0 + p1)
    fig, ax = plt.subplots(figsize=(8.6, 4.6), dpi=150)
    series = [
        ("Норматив «Указаний» (табл. 4, δ0 = ψ = 55°)", x, norm["UKAZ_T4_w378"].s, PALETTE[0], "-"),
        ("Норматив 1992, режим C < 0,7 (δ0 = 50°)", x, norm["N1992_MODE2_w378"].s, PALETTE[0], "--"),
        ("Кноте, β = 55°", x, norm["KNOTHE55_w378"].s, PALETTE[6], "-"),
        ("FEM упругий M05, A", x, fem["cases"]["M05_K0_A"]["s"], PALETTE[1], "-"),
        ("FEM упругий, граница D (модули «Указаний»)", x, fem["cases"]["NORM_K0_D"]["s"], PALETTE[1], "--"),
    ]
    if creep and "M05_LAB_B20" in creep["cases"]:
        c = creep["cases"]["M05_LAB_B20"]
        series.append(("FEM ползучесть LAB, 20 м, 40 лет", c["x"], c["profiles"][40.0], PALETTE[2], "-"))
    for lab, xx, s, col, ls in series:
        ax.plot((xx - xc) / H, s / np.max(s), color=col, linestyle=ls, linewidth=1.8, label=lab)
    for e in (p0, p1):
        ax.axvline((e - xc) / H, color=INK2, linewidth=0.8, linestyle=":")
    ax.text((p1 - xc) / H, 1.04, " край панели", fontsize=7.5, color=INK2)
    ax.set_xlim(-3.2, 3.2)
    ax.set_ylim(-0.02, 1.1)
    _style(ax, "расстояние от центра панели, в долях глубины H", "оседание / максимум")
    ax.legend(fontsize=7.5, frameon=False, loc="upper left")
    ax.set_title("Форма мульды (нормирована на максимум): упругая FEM шире и положе нормативной", fontsize=10,
                 color=INK, loc="left")
    fig.tight_layout()
    fig.savefig(path)
    plt.close(fig)


def fig_transfer(path, amp):
    import matplotlib.pyplot as plt
    fig, axes = plt.subplots(1, 2, figsize=(8.6, 3.6), dpi=150)
    ax = axes[0]
    conv = np.array([r["seam_conv_centre_fem_mm"] for r in amp])
    smax = np.array([r["s_max_fem_mm"] for r in amp])
    ax.loglog(conv, smax, "o", color=PALETTE[1], markersize=6, markeredgecolor="white", markeredgewidth=1.2,
              label="FEM: 34 сценария")
    xx = np.array([1, 1e4])
    ax.loglog(xx, 0.9 * xx, color=PALETTE[0], linewidth=1.8, label="норматив: η = 0,9·(m·ω)")
    for tag, om in (("m·ω, A", OMEGA["A"]), ("m·ω, B", OMEGA["B"])):
        ax.axvline(M0 * om * 1000, color=INK2, linewidth=0.8, linestyle=":")
        ax.text(M0 * om * 1000, 1.2, " " + tag, fontsize=7, color=INK2, rotation=90, va="bottom")
    _style(ax, "сближение кровли и почвы пласта в центре, мм", "максимум оседания поверхности, мм")
    ax.legend(fontsize=7.5, frameon=False, loc="upper left")
    ax.set_title("Передача на поверхность", fontsize=9.5, color=INK, loc="left")
    ax = axes[1]
    sel = [r for r in amp if r["lambda"] == "K0"]  # λ = 1 даёт то же сближение (±1 %)
    mods = ["M10", "M05", "M03", "NORM"]
    names = {"M10": "LAB×1,0", "M05": "LAB×0,5", "M03": "LAB×0,3", "NORM": "модули «Указаний»"}
    marks = {"A": ("o", PALETTE[1]), "B": ("s", PALETTE[3]), "C": ("^", PALETTE[4]), "D": ("D", PALETTE[7])}
    for var, (mk, col) in marks.items():
        xs_ = [100 * r["void_closed_fraction_fem"] for m in mods for r in sel if r["moduli"] == m and r["variant"] == var]
        ax.plot(xs_, np.arange(len(mods)), mk, color=col, markersize=7, markeredgecolor="white", markeredgewidth=1.2,
                label=f"вариант {var}")
    ax.set_yticks(np.arange(len(mods)), [names[m] for m in mods], fontsize=8, color=INK)
    ax.axvline(100, color=PALETTE[0], linewidth=1.5)
    ax.text(90, 3.35, "норматив: камеры\nзакрываются полностью", fontsize=7, color=INK2, ha="right", va="top")
    ax.set_xscale("log")
    ax.set_xlim(0.03, 200)
    ax.set_ylim(-0.6, 3.6)
    ax.legend(fontsize=7, frameon=False, loc="lower right", ncol=2)
    _style(ax, "закрытый объём камер (сближение / m·ω) в FEM, %")
    ax.set_title("Источник: насколько сходится пласт", fontsize=9.5, color=INK, loc="left")
    fig.tight_layout()
    fig.savefig(path)
    plt.close(fig)


def fig_creep(path, creep, pl12):
    """FEM с ползучестью против нормативной функции времени: η(t) = 0,002·t·η_ok(C), C = 0,19–0,30 (C < 0,32)."""
    import matplotlib.pyplot as plt
    fig, axes = plt.subplots(1, 2, figsize=(8.6, 3.9), dpi=150, sharey=True)
    tt = np.linspace(0, 40, 81)
    for ax, (geo, om, cases) in zip(axes, (("межосевое 27 м", OMEGA["A"], ("M05_FIELD_A27", "M05_LAB_A27")),
                                          ("межосевое 20 м", OMEGA["B"], ("M05_FIELD_B20", "M05_LAB_B20")))):
        lo = [sf.time_fraction_ukazaniya(t, C_SKRU1[0]) * sf.eta_max_current(M0, om, 1, sf.loading_factor_k(C_SKRU1[0]))
              for t in tt]
        hi = [sf.time_fraction_ukazaniya(t, C_SKRU1[1]) * sf.eta_max_current(M0, om, 1, sf.loading_factor_k(C_SKRU1[1]))
              for t in tt]
        ax.fill_between(tt, lo, hi, color=PALETTE[0], alpha=0.18, linewidth=0,
                        label=f"норматив: 0,002·t·η_ok, C = 0,19–0,30, ω = {om * 100:.1f} %")
        ax.plot(tt, hi, color=PALETTE[0], linewidth=1.2)
        ax.plot(tt, lo, color=PALETTE[0], linewidth=1.2)
        for case, ls, lab in ((cases[0], "-", "FEM, вязкость FIELD"), (cases[1], "--", "FEM, вязкость LAB")):
            c = creep["cases"].get(case)
            if not c:
                continue
            t = sorted(c["profiles"])
            ax.plot(t, [np.max(c["profiles"][k]) for k in t], color=PALETTE[2], linestyle=ls, linewidth=1.8, label=lab)
        ax.set_xlim(0, 40)
        _style(ax, "время, лет", "максимум оседания, м" if ax is axes[0] else None)
        ax.set_title(geo, fontsize=9.5, color=INK, loc="left")
        ax.legend(fontsize=7, frameon=False, loc="upper left")
    axes[0].set_ylim(0, 0.6)
    fig.suptitle("Ползучесть FEM и нормативная функция времени «Указаний» (t — годы)", fontsize=10, color=INK, x=0.01,
                 ha="left")
    fig.tight_layout()
    fig.savefig(path)
    plt.close(fig)


def fig_deformations(path, x, panel, H, fem, norm):
    import matplotlib.pyplot as plt
    p0, p1 = panel
    xc = 0.5 * (p0 + p1)
    fig, axes = plt.subplots(3, 1, figsize=(8.6, 7.4), dpi=150, sharex=True)
    fe = sf.profile_from_samples(x, fem["cases"]["NORM_K0_D"]["s"], fem["cases"]["NORM_K0_D"]["u_h"], base=BASE)
    fs = 1.0 / np.max(fe.s)
    nu = norm["UKAZ_T4_w510"]
    ns = 1.0 / np.max(nu.s)
    kn = norm["KNOTHE55_w510"]
    xn = (x - xc) / H
    ax = axes[0]
    ax.plot(xn, nu.tilt * ns * H, color=PALETTE[0], linewidth=1.8, label="«Указания» (1.17)")
    ax.plot(xn, kn.tilt * ns * H, color=PALETTE[6], linewidth=1.5, label="Кноте, β = 55°")
    ax.plot(xn, fe.tilt * fs * H, color=PALETTE[1], linewidth=1.8,
            label="FEM, граница D, модули «Указаний» (разности на базе 15 м)")
    _style(ax, None, "наклон · H / η")
    ax.legend(fontsize=7.5, frameon=False, loc="lower left")
    ax = axes[1]
    ax.plot(xn, nu.curvature * ns * H ** 2, color=PALETTE[0], linewidth=1.8)
    ax.plot(xn, kn.curvature * ns * H ** 2, color=PALETTE[6], linewidth=1.5)
    ax.plot(xn, fe.curvature * fs * H ** 2, color=PALETTE[1], linewidth=1.8)
    _style(ax, None, "кривизна · H² / η")
    ax = axes[2]
    ax.plot(xn, nu.eps_h * ns * H, color=PALETTE[0], linewidth=1.8, label="«Указания»: ε = m_e·K·L (1.20)")
    ax.plot(xn, kn.eps_h * ns * H, color=PALETTE[6], linewidth=1.5, label="Кноте + Авершин: ε = B·K, B = 0,165H")
    ax.plot(xn, fe.eps_h * fs * H, color=PALETTE[1], linewidth=1.8, label="FEM")
    _style(ax, "расстояние от центра панели, в долях H", "гор. деформация · H / η")
    ax.legend(fontsize=7, frameon=False, loc="upper left")
    for a in axes:
        for e in (p0, p1):
            a.axvline((e - xc) / H, color=INK2, linewidth=0.8, linestyle=":")
    axes[0].set_xlim(-3, 3)
    axes[0].set_title("Деформации на единицу максимального оседания (форма, без амплитуды)", fontsize=10,
                      color=INK, loc="left")
    fig.tight_layout()
    fig.savefig(path)
    plt.close(fig)


# ------------------------------------------------------------------------------------------------- main

def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--fem-root", type=Path, required=True)
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--creep-json", type=Path)
    ap.add_argument("--pl12-csv", type=Path)
    ap.add_argument("--matlab-csv", type=Path, help="профили формул, посчитанные MATLAB (run_formulas_vs_fem.m)")
    a = ap.parse_args()
    out = a.out
    out.mkdir(parents=True, exist_ok=True)
    fem = load_fem(a.fem_root)
    creep = load_creep(a.creep_json) if a.creep_json else None
    pl12 = load_pl12(a.pl12_csv) if a.pl12_csv else None
    x, panel = fem["x"], fem["panel"]
    H = fem["H_floor"]  # «Указания»: до почвы нижнего отработанного пласта (VKM-SRC-065 с. 31)
    norm = normative_profiles(x, panel, H)
    amp = amplitude_table(fem, H)
    (out / "scenario_for_matlab.json").write_text(json.dumps(
        {"x": [float(v) for v in x], "panel": list(panel), "H": H, "m0": M0, "omega_A": OMEGA["A"],
         "omega_B": OMEGA["B"], "delta_cur": DELTA_CUR, "psi_cur": PSI_CUR, "delta_92": DELTA_92, "psi_92": PSI_92,
         "B_avershin_over_H": float(np.mean(AVERSHIN_B))}, indent=0) + "\n", encoding="utf-8")

    shapes = {}
    for name, p in norm.items():
        shapes[f"formula:{name}"] = shape_metrics(p, panel, H) | {"maxima": p.maxima(), "meta": p.meta}
    for case, d in fem["cases"].items():
        prof = sf.profile_from_samples(x, d["s"], d["u_h"], base=BASE)
        m = shape_metrics(prof, panel, H)
        m["knothe_equivalent"] = knothe_equivalent_angle(x, d["s"], panel, H)
        mx = prof.maxima()
        m["maxima"] = mx
        flank = np.abs(prof.tilt) > 0.3 * mx["tilt_max"]
        m["avershin_B_fem_over_H"] = float(np.sum(prof.u_h[flank] * prof.tilt[flank]) /
                                           np.sum(prof.tilt[flank] ** 2)) / H
        shapes[f"fem:{case}"] = m
    if creep:
        for case, c in creep["cases"].items():
            for t in (0.0, 10.0, 40.0):
                if t in c["profiles"]:
                    m = shape_metrics(sf.profile_from_samples(c["x"], c["profiles"][t], base=BASE), panel, H)
                    m["knothe_equivalent"] = knothe_equivalent_angle(c["x"], c["profiles"][t], panel, H)
                    m["fraction_of_norm_k09"] = m["s_max_m"] / sf.eta_max_current(
                        M0, OMEGA["A"] if case.endswith("A27") else OMEGA["B"], 1, 0.9)
                    shapes[f"fem_creep:{case}:t{int(t)}"] = m

    # MATLAB ↔ Python по тем же нормативным профилям
    matlab_check = None
    if a.matlab_csv and a.matlab_csv.exists():
        rows = list(csv.DictReader(a.matlab_csv.open(encoding="utf-8")))
        worst = 0.0
        for name, p in norm.items():
            got = [r for r in rows if r["case"] == name]
            if not got:
                continue
            for key in ("s", "tilt", "curvature", "eps_h"):
                m = np.array([float(r[key]) for r in got])
                v = getattr(p, key)
                worst = max(worst, float(np.nanmax(np.abs(m - v)) / max(np.nanmax(np.abs(v)), 1e-30)))
        matlab_check = {"max_rel_diff": worst, "pass": worst < 1e-9, "matlab_csv_sha256": sha256(a.matlab_csv)}

    # таблицы
    with (out / "amplitude_comparison.csv").open("w", encoding="utf-8", newline="") as f:
        keys = sorted({k for r in amp for k in r}, key=lambda k: list(amp[0]).index(k) if k in amp[0] else 99)
        w = csv.DictWriter(f, fieldnames=keys)
        w.writeheader()
        for r in amp:
            w.writerow({k: (f"{v:.6g}" if isinstance(v, float) else v) for k, v in r.items()})
    with (out / "formula_profiles.csv").open("w", encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        w.writerow(["case", "x", "s", "tilt", "curvature", "u_h", "eps_h"])
        for name, p in norm.items():
            for j in range(len(x)):
                w.writerow([name, f"{x[j]:.4f}", f"{p.s[j]:.9e}", f"{p.tilt[j]:.9e}", f"{p.curvature[j]:.9e}",
                            f"{p.u_h[j]:.9e}", f"{p.eps_h[j]:.9e}"])
    with (out / "fem_surface_profiles.csv").open("w", encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        w.writerow(["case", "x", "s", "u_h", "seam_convergence"])
        for case, d in fem["cases"].items():
            for j in range(len(x)):
                w.writerow([case, f"{x[j]:.4f}", f"{d['s'][j]:.9e}", f"{d['u_h'][j]:.9e}", f"{d['conv'][j]:.9e}"])

    summary = {
        "schema": "vkm.formulas_vs_fem/1",
        "status": "MODEL RESULT (FEM) и НОРМАТИВНЫЙ РАСЧЁТ (формулы корпуса); не наблюдение, не прогноз, не валидация",
        "scenario": {"panel_chainage_m": list(panel), "H_roof_m": fem["H_roof"], "H_floor_m": H, "m0_m": M0,
                     "omega": OMEGA, "angles_current_deg": [DELTA_CUR, PSI_CUR], "angles_1992_deg": [DELTA_92, PSI_92],
                     "avershin_B_over_H": AVERSHIN_B, "a_ratio_1992": A_RATIO_92,
                     "backfill_scenario": {"A": A_FILL, "B": B_FILL}},
        "normative_eta_m": {f"omega={om}": {"k=0.9 (C>=0.32)": sf.eta_max_current(M0, om, 1, 0.9),
                                            "C=0.20": sf.eta_max_current(M0, om, 1, sf.loading_factor_k(0.20)),
                                            "C=0.10": sf.eta_max_current(M0, om, 1, sf.loading_factor_k(0.10))}
                            for om in sorted(set(OMEGA.values()))},
        "undermining": {"D_over_H": (panel[1] - panel[0]) / H,
                        "regime": "полная подработка: D1 > 1,4H, D2 = ∞ (2D), χ ≥ 1,4 → f0 = 1 (VKM-SRC-065 (1.9)–(1.10))"
                        if panel[1] - panel[0] >= 1.4 * H else "неполная подработка",
                        "f0": sf.incomplete_factor_f0(sf.undermining_chi(panel[1] - panel[0], 1e9, H))},
        "normative_time": {f"omega={om}": {f"C={C}": {"k": sf.loading_factor_k(C), "eta_ok_m": sf.eta_max_current(M0, om, 1, sf.loading_factor_k(C)),
                                                      **{f"eta_t{int(ty)}_m": sf.time_fraction_ukazaniya(ty, C) * sf.eta_max_current(M0, om, 1, sf.loading_factor_k(C)) for ty in T_YEARS}}
                                           for C in C_SKRU1} for om in (OMEGA["A"], OMEGA["B"])},
        "creep_vs_time": ({case: {f"s_t{int(ty)}_m": float(np.max(c["profiles"][ty])) for ty in T_YEARS if ty in c["profiles"]}
                           for case, c in creep["cases"].items()} if creep else None),
        "amplitude": amp, "shapes": shapes, "matlab_check": matlab_check,
        "inputs": {"fem": fem["inputs"], "creep_sha256": creep["sha256"] if creep else None,
                   "creep_status": creep["status"] if creep else None,
                   "pl12_sha256": pl12["_sha256"] if pl12 else None},
    }
    if pl12:
        diag = {}
        for ep, arr in pl12.items():
            if ep.startswith("_"):
                continue
            xx, ss = arr[:, 0], arr[:, 1]
            tilt = np.diff(ss) / np.diff(xx)
            diag[ep] = {"s_max_m": float(ss.max()), "tilt_max": float(np.max(np.abs(tilt))),
                        "tilt_over_s_per_m": float(np.max(np.abs(tilt)) / ss.max())}
        summary["pl12_diagnostic"] = {"status": "OBSERVATION_AS_PUBLISHED (оцифровка, вторичная публикация); только "
                                                "диагностика формы и порядка величин", "epochs": diag}
    (out / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=1, default=float) + "\n",
                                      encoding="utf-8")

    import matplotlib
    matplotlib.use("Agg")
    fig_amplitudes(out / "fig1_amplitudes.png", amp, creep, pl12)
    fig_shapes(out / "fig2_shapes.png", x, panel, H, fem, norm, creep)
    fig_transfer(out / "fig3_transfer.png", amp)
    fig_deformations(out / "fig4_deformations.png", x, panel, H, fem, norm)
    if creep:
        fig_creep(out / "fig5_creep_vs_norm.png", creep, pl12)
    print(json.dumps({"out": out.name, "cases": len(amp), "matlab_check": matlab_check}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
