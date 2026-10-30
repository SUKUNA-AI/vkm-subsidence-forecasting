"""Figures of the World-0 microkernel study (23.10.2026). Static PNGs for the report; model results only.

Usage: python scripts/world0/make_figures.py --work work/world0_20261023 --out work/world0_20261023/figures
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
import numpy as np  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parent))
import identification_setup  # noqa: E402,F401

from vkm_world.representations.world0 import influence as I  # noqa: E402
from vkm_world.representations.world0 import laws  # noqa: E402

SERIES = ["#2a78d6", "#eb6834", "#1baf7a", "#eda100", "#e87ba4", "#008300", "#6250d6", "#e34948"]
INK, MUTED, GRID = "#1f1f1e", "#6b6a64", "#e4e2dc"
plt.rcParams.update({"font.size": 9, "axes.edgecolor": MUTED, "axes.labelcolor": INK, "xtick.color": MUTED,
                     "ytick.color": MUTED, "axes.grid": True, "grid.color": GRID, "grid.linewidth": 0.6,
                     "lines.linewidth": 1.6, "legend.frameon": False, "axes.spines.top": False,
                     "axes.spines.right": False, "savefig.dpi": 150, "figure.dpi": 100})
STAMP = "МОДЕЛЬ (не наблюдение): World-0, 23.10.2026"


def stamp(fig):
    fig.text(0.995, 0.005, STAMP, ha="right", va="bottom", fontsize=7, color=MUTED)


def fig_k_sign(work: Path, out: Path):
    comp = work / "compliance"
    cases = [("PS_M05_B", "M05, контакты сцеплены"), ("PS_NORM_B", "NORM"), ("PS_C044_B", "C044 (SRC-044 табл. 28)"),
             ("PS_M05_F", "M05, проскальзывание"), ("PS_M05_HOMO", "однородная соль (эталон)")]
    fig, ax = plt.subplots(1, 2, figsize=(10, 3.8))
    lags = np.arange(0, 7)
    w = 0.16
    for q, (c, lab) in enumerate(cases):
        d = np.load(comp / c / "compliance.npz")
        K, _ = I.stiffness_from_compliance(d["C"], areas=np.diff(d["cell_edges"]))
        prof = I.lag_profile(K)[:7]
        ax[0].bar(lags + (q - 2) * w, prof, width=w * 0.9, color=SERIES[q], label=lab)
    ax[0].axhline(0, color=MUTED, lw=0.8)
    ax[0].set_xlabel("расстояние между ячейками, число ячеек по 20 м")
    ax[0].set_ylabel("K_ij / K_ii")
    ax[0].set_ylim(-0.6, 0.15)
    ax[0].set_title("Знаковая структура K (σ = σ⁰ − K·v)", fontsize=9, color=INK)
    ax[0].legend(fontsize=7, loc="lower right")
    d = np.load(comp / "PS_M05_B" / "compliance.npz")
    C = d["C"]
    for q, m in enumerate((1, 2, 3, 5)):
        n = (C.shape[0] // m) * m
        P = np.zeros((C.shape[0], n // m))
        for J in range(n // m):
            P[J * m:(J + 1) * m, J] = 1.0
        Kc, _ = I.stiffness_from_compliance(P[:n].T @ C[:n, :n] @ P[:n] / m)
        prof = I.lag_profile(Kc)[:5]
        ax[1].plot(np.arange(5) * 20 * m, prof, "o-", color=SERIES[q], ms=4, label=f"ячейка {20 * m} м")
    ax[1].axhline(0, color=MUTED, lw=0.8)
    ax[1].set_xlabel("расстояние между ячейками, м")
    ax[1].set_ylabel("K_ij / K_ii")
    ax[1].set_title("M05: укрупнение ячеек не убирает знак «+» через одну", fontsize=9, color=INK)
    ax[1].legend(fontsize=7)
    stamp(fig)
    fig.tight_layout()
    fig.savefig(out / "fig_mk1_K_sign_structure.png")
    plt.close(fig)


def fig_g(work: Path, out: Path):
    comp = work / "compliance"
    fig, ax = plt.subplots(1, 2, figsize=(10, 3.8))
    for q, (c, lab) in enumerate([("PS_M05_B", "M05 сцеплены"), ("PS_M05_F", "M05 проскальзывание"),
                                  ("PS_M05_F1", "проскальзывание ПКС|СМТ"), ("PS_NORM_B", "NORM"),
                                  ("PS_C044_B", "C044"), ("PS_M05_HOMO", "однородная")]):
        d = np.load(comp / c / "compliance.npz")
        G = I.surface_operator(d["S"], d["C"], areas=np.diff(d["cell_edges"]))
        j = G.shape[1] // 2
        ax[0].plot(d["x"], G[:, j] * 20.0, color=SERIES[q], label=lab)      # dimensionless G, scalar display factor 20
    ax[0].set_xlim(-1200, 1200)
    ax[0].set_xlabel("x, м (центр ячейки в 0)")
    ax[0].set_ylabel("20 × оседание / конвергенция (безразмерно)")
    ax[0].set_title("G: поверхность от единичной конвергенции одной ячейки", fontsize=9, color=INK)
    ax[0].legend(fontsize=7)
    d = np.load(comp / "AX_M05_B" / "compliance.npz")
    G = I.surface_operator(d["S"], d["C"], areas=np.pi * np.diff(d["cell_edges"] ** 2),
                           symmetrize=False, reciprocity_tol=1e-3)
    r = d["x"]
    area = np.pi * d["cell_edges"][1] ** 2
    g = G[:, 0] / area
    ax[1].plot(r, g * 1e6, color=SERIES[0], label="осесимметрия (диск 20 м), M05")
    f = np.exp(-np.pi * r ** 2 / 333.5 ** 2) / 333.5 ** 2
    ax[1].plot(r, f * 1e6, "--", color=SERIES[1], label="Кнотхе 3D, r = 333 м (подгонка)")
    ax[1].set_xlim(0, 1200)
    ax[1].set_xlabel("r, м")
    ax[1].set_ylabel("оседание на 1 м³ конвергенции, 10⁻⁶ 1/м²")
    ax[1].set_title("Осесимметричный отклик: tgβ ≈ 0,96\nОбъём мульды ≈ объёму конвергенции", fontsize=9, color=INK)
    ax[1].legend(fontsize=7)
    stamp(fig)
    fig.tight_layout()
    fig.savefig(out / "fig_mk2_G_surface.png")
    plt.close(fig)


def fig_fine_coarse(work: Path, out: Path):
    an = work / "analysis"
    fig, ax = plt.subplots(1, 2, figsize=(10, 3.8))
    for k, (f, lab) in enumerate([("PS_M05_FINE27N_unit_cell_CELL_A27_B_EL", "сцеплены"),
                                  ("PS_M05_FINE27FN_unit_cell_CELL_A27_F_EL", "проскальзывание")]):
        p = an / f"fine_vs_coarse_{f}.npz"
        if not p.exists():
            continue
        d = np.load(p)
        ax[k].plot(d["x"], d["s_fine"] * 1e3, color=SERIES[0], label="тонкое: 22 целика и камеры")
        ax[k].plot(d["x"], d["s_coarse"] * 1e3, "--", color=SERIES[1], label="грубое: 22 ячейки MK-MINED-CELL")
        ax[k].set_xlim(-1000, 1000)
        ax[k].set_xlabel("x, м")
        ax[k].set_ylabel("оседание, мм (упруго, M05)")
        ax[k].set_title(f"Слияние целик+камера, контакты пласта: {lab}", fontsize=9, color=INK)
        ax[k].legend(fontsize=7)
    stamp(fig)
    fig.tight_layout()
    fig.savefig(out / "fig_mk3_fine_vs_coarse.png")
    plt.close(fig)


def _hist(work, case):
    p = work / "cells" / case / "history.npz"
    return dict(np.load(p)) if p.exists() else None


def fig_cells(work: Path, out: Path):
    fig, ax = plt.subplots(1, 2, figsize=(10, 3.8))
    cases = [("CELL_A27_B_FIELD", "A27, FIELD, сцеплены"), ("CELL_A27_B_FIELD_ELSLAB", "A27, FIELD, кровля упругая"),
             ("CELL_A27_F_FIELD", "A27, FIELD, проскальзывание"), ("CELL_A27_B_LAB", "A27, LAB"),
             ("CELL_B20_B_FIELD", "B20, FIELD"), ("CELL_B20_B_LAB", "B20, LAB")]
    for q, (c, lab) in enumerate(cases):
        d = _hist(work, c)
        if d is None:
            continue
        k = d["t"] > 1e-3
        ax[0].plot(d["t"][k], d["pillar_shortening"][k] * 1e3, color=SERIES[q], label=lab)
    ax[0].set_yscale("log")
    ax[0].set_xlabel("время после выемки, лет")
    ax[0].set_ylabel("укорочение целика, мм")
    ax[0].set_title("Пара целик–кровля: торцевой контакт и ползучесть кровли", fontsize=9, color=INK)
    ax[0].legend(fontsize=7)
    A = _hist(work, "CELL_A27_B_FIELD")
    B = _hist(work, "CELL_A27_B_FIELD_HIST")
    if A is not None and B is not None:
        for q, (key, lab) in enumerate([("cell_convergence", "конвергенция ячейки"),
                                        ("pillar_shortening", "укорочение целика")]):
            iB = int(np.argmin(np.abs(B["t"] - 2.001)))
            vB0 = B[key][iB]
            kA = A["t"] > 1e-3
            ts = float(np.interp(vB0, A[key][kA], A["t"][kA]))
            tt = np.linspace(0, 40 - max(ts, 2.001), 60)
            ax[1].plot(tt, (np.interp(ts + tt, A["t"][kA], A[key][kA]) - vB0) * 1e3, color=SERIES[2 * q],
                       label=f"{lab}: история A (σ0)")
            ax[1].plot(tt, (np.interp(2.001 + tt, B["t"], B[key]) - vB0) * 1e3, "--", color=SERIES[2 * q + 1],
                       label=f"{lab}: история B (1,5σ0 → σ0)")
        ax[1].set_xlabel("время после совпадения редуцированного состояния, лет")
        ax[1].set_ylabel("приращение, мм")
        ax[1].set_title("Тест замыкания: одно и то же v, разная история", fontsize=9, color=INK)
        ax[1].legend(fontsize=7)
    stamp(fig)
    fig.tight_layout()
    fig.savefig(out / "fig_mk4_cells_closure.png")
    plt.close(fig)


def fig_backfill(work: Path, out: Path):
    fig, ax = plt.subplots(1, 2, figsize=(10, 3.8))
    cases = [("CELL_B20_B_LAB", "без закладки"), ("CELL_B20_BF_LAB_E200G165", "E 0,2 ГПа, зазор 0,165 м"),
             ("CELL_B20_BF_LAB_E200G550", "E 0,2 ГПа, зазор 0,55 м"), ("CELL_B20_BF_LAB_E50G165", "E 0,05 ГПа, зазор 0,165 м")]
    for q, (c, lab) in enumerate(cases):
        d = _hist(work, c)
        if d is None:
            continue
        k = d["t"] > 1e-3
        ax[0].plot(d["t"][k], d["chamber_convergence"][k] * 1e3, color=SERIES[q], label=lab)
        ax[1].plot(d["t"][k], d["sigma_pillar"][k] / 1e6, color=SERIES[q], label=f"целик: {lab}")
        if "BF" in c:
            ax[1].plot(d["t"][k], d["sigma_fill"][k] / 1e6, ":", color=SERIES[q], label=f"закладка: {lab}")
    ax[0].set_xlabel("время, лет")
    ax[0].set_ylabel("конвергенция в центре камеры, мм")
    ax[0].set_title("Пара целик–камера–закладка (B20, LAB)", fontsize=9, color=INK)
    ax[0].legend(fontsize=7)
    ax[1].set_xlabel("время, лет")
    ax[1].set_ylabel("среднее вертикальное напряжение, МПа")
    ax[1].set_title("Перераспределение нагрузки целик → закладка", fontsize=9, color=INK)
    ax[1].legend(fontsize=6)
    stamp(fig)
    fig.tight_layout()
    fig.savefig(out / "fig_mk5_backfill.png")
    plt.close(fig)


def fig_checks(work: Path, out: Path):
    an = json.loads((work / "analysis" / "analytic_pairs.json").read_text(encoding="utf-8"))
    fig, ax = plt.subplots(1, 3, figsize=(12, 3.5))
    kc = work / "kernel_checks"
    s1 = np.loadtxt(kc / "KC1" / "series.txt")
    ref = laws.plane_strain_norton_element(s1[:, 0], 10e6, 6.895e9, 0.2, 7.58e13, 5)
    ax[0].plot(s1[:, 0], -s1[:, 1] * 1e3, color=SERIES[0], label="MAPDL, 1 элемент")
    ax[0].plot(s1[:, 0], -ref * 1e3, "--", color=SERIES[1], label="полуаналитика (W0-L-02a)")
    ax[0].set_xlabel("лет")
    ax[0].set_ylabel("ε_yy, ‰")
    ax[0].set_title("KC1: целик в плоской деформации", fontsize=9, color=INK)
    ax[0].legend(fontsize=7)
    h = an["H1_hereditary"]["delta_0.18"]
    for q, w in enumerate(("from_1h", "from_1month")):
        e = h[w]["max_rel_err_by_modes"]
        ns = [int(k) for k in e]
        ax[1].semilogy(ns, [e[str(n)] for n in ns], "o-", color=SERIES[q], ms=4,
                       label={"from_1h": "разрешение 1 ч", "from_1month": "разрешение 1 мес"}[w])
    ax[1].axhline(0.01, color=MUTED, lw=0.8, ls=":")
    ax[1].axhline(0.05, color=MUTED, lw=0.8, ls=":")
    ax[1].set_xlabel("число мод памяти (Прони)")
    ax[1].set_ylabel("макс. отн. ошибка J(t), 40 лет")
    ax[1].set_title("Наследственный целик: размерность памяти", fontsize=9, color=INK)
    ax[1].legend(fontsize=7)
    a = an["A1_aquifer"]["U_exact_vs_1mode"]
    ax[2].plot(a["Tv"], a["exact"], "o-", color=SERIES[0], ms=4, label="точно (ряд Терцаги)")
    ax[2].plot(a["Tv"], a["one_mode"], "s--", color=SERIES[1], ms=4, label="одна мода (одно среднее p)")
    ax[2].set_xscale("log")
    ax[2].set_xlabel("T_v = c_v t / H²")
    ax[2].set_ylabel("степень консолидации U")
    ax[2].set_title("Водоносная ТКТ: одной переменной мало", fontsize=9, color=INK)
    ax[2].legend(fontsize=7)
    stamp(fig)
    fig.tight_layout()
    fig.savefig(out / "fig_mk6_kernel_checks.png")
    plt.close(fig)


def fig_gravity_vzt(work: Path, out: Path):
    an = work / "analysis"
    g = np.load(an / "gravity_profiles.npz")
    fig, ax = plt.subplots(1, 2, figsize=(10, 3.8))
    ax[0].plot(g["x"], g["g_panel"] / 1e-8, color=SERIES[0], label="панель A27, 22 камеры (2D)")
    ax[0].plot(g["x"], g["g_chamber"] / 1e-8 * 10, color=SERIES[1], label="одна камера ×10")
    ax[0].set_xlim(-1200, 1200)
    ax[0].set_xlabel("x, м")
    ax[0].set_ylabel("Δg_z, мкГал")
    ax[0].set_title("Гравитация как зонд: аномалия пустот (не драйвер механики)", fontsize=9, color=INK)
    ax[0].legend(fontsize=7)
    comp = work / "compliance"
    d = np.load(comp / "PS_M05_B" / "compliance.npz")
    p = np.load(comp / "PS_M05_B" / "probes.npz")
    C, x = d["C"], d["x"]
    Ci = np.linalg.inv(0.5 * (C + C.T))
    for q, name in enumerate(p["layers"]):
        Ux = p["P"][:, q, :, 0].T / float(d["unit_traction"])
        eps = np.gradient(Ux @ Ci @ np.ones(C.shape[0]), x)
        ax[1].plot(x, eps * 1e3, color=SERIES[q], label=f"кровля {name} ({p['depth_top'][q]:.0f} м)")
    ax[1].axhline(2.0, color=MUTED, ls=":", lw=0.8)
    ax[1].set_xlim(-800, 800)
    ax[1].set_xlabel("x, м")
    ax[1].set_ylabel("ε_x на 1 м равномерной конвергенции, ‰")
    ax[1].set_title("Пласт → ВЗТ: деформация слоёв ВЗТ (норматив 2 ‰ пунктиром)", fontsize=9, color=INK)
    ax[1].legend(fontsize=7)
    stamp(fig)
    fig.tight_layout()
    fig.savefig(out / "fig_mk7_gravity_vzt.png")
    plt.close(fig)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--work", type=Path, required=True)
    ap.add_argument("--out", type=Path, required=True)
    a = ap.parse_args()
    a.out.mkdir(parents=True, exist_ok=True)
    for f in (fig_k_sign, fig_g, fig_fine_coarse, fig_cells, fig_backfill, fig_checks, fig_gravity_vzt):
        f(a.work, a.out)
        print("ok", f.__name__)


if __name__ == "__main__":
    main()
