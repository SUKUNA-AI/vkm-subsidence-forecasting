"""Figures of the OGS quick checks, drawn from the committed receipts only (receipts/*.json).

python make_figures.py  ->  figures/*.png (small PNGs for the report)
Palette: reference categorical slots (validated, adjacent pairs, light surface); ink for text and
reference/analytic curves; sequential blue ramp for ordered times. MODEL results of a verification toy.
"""
from __future__ import annotations

import json

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
import numpy as np  # noqa: E402
from matplotlib.colors import LogNorm, LinearSegmentedColormap  # noqa: E402

import oqc_common as C  # noqa: E402

SURF = "#fcfcfb"
INK = "#0b0b0b"
INK2 = "#52514e"
MUTED = "#898781"
GRID = "#e1e0d9"
AXIS = "#c3c2b7"
CAT = ["#2a78d6", "#eb6834", "#1baf7a", "#eda100", "#e87ba4", "#008300", "#4a3aa7", "#e34948"]
SEQ = ["#86b6ef", "#6da7ec", "#5598e7", "#3987e5", "#2a78d6", "#256abf", "#1c5cab", "#184f95", "#104281", "#0d366b"]
MARKERS = ["o", "s", "^", "D", "v", "P"]

plt.rcParams.update({
    "figure.facecolor": SURF, "axes.facecolor": SURF, "savefig.facecolor": SURF,
    "axes.edgecolor": AXIS, "axes.labelcolor": INK2, "xtick.color": MUTED, "ytick.color": MUTED,
    "text.color": INK, "axes.grid": True, "grid.color": GRID, "grid.linewidth": 0.6,
    "axes.spines.top": False, "axes.spines.right": False, "font.size": 8.5, "axes.titlesize": 9,
    "axes.titleweight": "bold", "legend.frameon": False, "lines.linewidth": 1.8,
    "font.family": "DejaVu Sans",
})
DPI = 110


def rec(step):
    return json.load(open(C.RECEIPTS / f"{step}.json", encoding="utf-8"))


def save(fig, name):
    C.FIGURES.mkdir(parents=True, exist_ok=True)
    fig.savefig(C.FIGURES / name, dpi=DPI, bbox_inches="tight")
    plt.close(fig)
    print("wrote", name)


# ----------------------------------------------------------------------------- S5
def fig_s5():
    r = rec("S5")
    v = [x for x in r["variants"] if x["variant"] == "deactivated_subdomain"][0]
    p = r["test_values"]["p_Pa"]
    fig, ax = plt.subplots(1, 2, figsize=(7.4, 3.0))
    for k, (key, lab) in enumerate((("x_axis_theta0", "θ = 0° (бок)"), ("y_axis_theta90", "θ = 90° (кровля)"))):
        pr = v["profiles"][key]
        rr = np.array(pr["_r"])
        inner = rr > 1.0 + 1e-9
        ax[0].plot(rr, np.array(pr["_stt_an"]) / p, color=INK, lw=1.0, ls="--", zorder=1)
        ax[0].plot(rr[inner], np.array(pr["_stt"])[inner] / p, ls="none", marker=MARKERS[k], ms=5,
                   mfc=CAT[k], mec=SURF, mew=1.0, label=f"OGS, {lab}", zorder=3)
        ax[1].plot(rr, np.array(pr["_ur_an"]) * 1e3, color=INK, lw=1.0, ls="--", zorder=1)
        ax[1].plot(rr, np.array(pr["_ur"]) * 1e3, ls="none", marker=MARKERS[k], ms=5, mfc=CAT[k], mec=SURF,
                   mew=1.0, label=f"OGS, {lab}", zorder=3)
    ax[0].plot([], [], color=INK, lw=1.0, ls="--", label="Кирш (аналитика)")
    ax[0].set(xlabel="r / a", ylabel="σθθ / p (растяжение +)", title="Кольцевое напряжение")
    ax[1].set(xlabel="r / a", ylabel="u_r, мм (наружу +)", title="Радиальное смещение от выемки")
    ax[0].legend(loc="lower right", fontsize=7.5)
    fig.suptitle("S5. Отверстие в упругой пластине (деактивация подобласти) против решения Кирша; "
                 "TEST-значения", fontsize=8.5, color=INK2, y=1.02)
    fig.tight_layout()
    save(fig, "fig_S5_kirsch.png")


# ----------------------------------------------------------------------------- S6
def fig_s6():
    r = rec("S6")
    cs = {c["case"]: c for c in r["cases"]}
    fig, ax = plt.subplots(1, 2, figsize=(7.4, 3.0))
    yr = C.YEAR
    c0 = cs["3D_native_CreepBGRa_Q0"]
    t = np.array(c0["_t"]) / yr
    ax[0].plot(t, -np.array(c0["_ezz_an"]) * 100, color=INK, lw=1.0, ls="--", label="замкнутая форма")
    ax[0].plot(t[::3], -np.array(c0["_ezz"])[::3] * 100, ls="none", marker="o", ms=5, mfc=CAT[0], mec=SURF,
               label="OGS CreepBGRa (native)")
    cm = cs["3D_MFront_PowerLawLinearCreep_Q0"]
    ax[0].plot(np.array(cm["_t"])[1::3] / yr, -np.array(cm["_ezz"])[1::3] * 100, ls="none", marker="s", ms=4.5,
               mfc=CAT[1], mec=SURF, label="OGS + MFront PowerLawLinearCreep")
    ax[0].set(xlabel="время, лет", ylabel="осевое укорочение −ε_zz, %", title="3D, одноосное сжатие 15 МПа")
    ax[0].legend(loc="upper left", fontsize=7.5)
    c2 = cs["2D_plane_strain_native_CreepBGRa_Q0"]
    t2 = np.array(c2["_t"]) / yr
    ax[1].plot(t2, -np.array(c2["_eyy_ode"]) * 100, color=INK, lw=1.0, ls="--", label="ОДУ (Radau, эталон)")
    ax[1].plot(t2[::2], -np.array(c2["_eyy"])[::2] * 100, ls="none", marker="o", ms=5, mfc=CAT[0], mec=SURF,
               label="OGS CreepBGRa, плоская деформация")
    ax[1].set(xlabel="время, лет", ylabel="−ε_yy, %", title="2D, плоская деформация (переход σzz)")
    ax[1].legend(loc="upper left", fontsize=7.5)
    fig.suptitle("S6. Ползучесть одного элемента при постоянном напряжении (закон CL-15 сильвинита → BGRa)",
                 fontsize=8.5, color=INK2, y=1.02)
    fig.tight_layout()
    save(fig, "fig_S6_creep.png")


# ----------------------------------------------------------------------------- S7
def fig_s7():
    r = rec("S7")
    prof = r["_profiles_base"]
    fig, ax = plt.subplots(1, 2, figsize=(7.6, 3.1), gridspec_kw={"width_ratios": [1.35, 1]})
    keys = list(prof)
    cols = [SEQ[int(round(i * (len(SEQ) - 1) / max(1, len(keys) - 1)))] for i in range(len(keys))]
    for k, c in zip(keys, cols):
        x = np.array(prof[k]["x_m"])
        s = np.array(prof[k]["s_m"]) * 1e3
        sel = x <= 900
        ax[0].plot(x[sel], s[sel], color=c, lw=1.8)
        ax[0].annotate(k, (x[0], s[0]), xytext=(4, 0), textcoords="offset points", fontsize=7, color=INK2,
                       va="center")
    ax[0].axvline(170.0, color=MUTED, lw=0.8, ls=":")
    ax[0].text(174, ax[0].get_ylim()[1] * 0.95 if ax[0].get_ylim()[1] > 0 else 1, "край панели", fontsize=7,
               color=MUTED, va="top")
    ax[0].invert_yaxis()
    ax[0].set(xlabel="x от центра панели, м", ylabel="оседание, мм", title="Профили мульды (половина, симметрия)")
    ts = r["time_series_base"]
    t = np.array(ts["t_yr"])
    t[0] = 0.0
    ax[1].plot(t, np.array(ts["s_max_m"]) * 1e3, color=CAT[0], marker="o", ms=4, label="макс. оседание поверхности")
    ax[1].plot(t, np.array(ts["conv_room0_m"]) * 1e3, color=CAT[1], marker="s", ms=4, label="конвергенция камеры 0")
    ax[1].set(xlabel="время после выемки, лет", ylabel="мм", title="Развитие во времени")
    ax[1].legend(loc="upper left", fontsize=7.5)
    fig.suptitle("S7. Toy-сечение: λ = 1 (IS-SC-A), ползучесть CL-15 ×1, без закладки — МОДЕЛЬНЫЙ результат, "
                 "не СКРУ-1", fontsize=8.5, color=INK2, y=1.02)
    fig.tight_layout()
    save(fig, "fig_S7_base.png")


# ----------------------------------------------------------------------------- S8
SC_ORDER = ["IS-SC-B/0.6", "IS-SC-B/0.8", "IS-SC-A", "IS-SC-D", "IS-SC-E", "IS-P-21-0.71", "NORMATIVE-0.45"]
SC_SHORT = {"IS-SC-B/0.6": "B 0,6", "IS-SC-B/0.8": "B 0,8", "IS-SC-A": "A 1,0", "IS-SC-D": "D 1,3/0,8",
            "IS-SC-E": "E 2,8/1,9", "IS-P-21-0.71": "0,71 (внутри B)", "NORMATIVE-0.45": "0,45 норматив"}
BF_RU = {"none": "нет", "soft": "мягкая", "stiff": "жёсткая"}


def fig_s8_matrix():
    r = rec("S8")
    rows = [s for s in SC_ORDER if any(t["scenario"] == s for t in r["table"])]
    colsp = [(cf, bf) for cf in (0.1, 1.0, 10.0) for bf in ("none", "soft", "stiff")]
    M = np.full((len(rows), len(colsp)), np.nan)
    flag = np.zeros_like(M, dtype=bool)
    for t in r["table"]:
        if t["scenario"] in rows and t.get("status") in ("OK", "OUTSIDE_VALIDITY"):
            i = rows.index(t["scenario"])
            j = colsp.index((t["creep_factor"], t["backfill"]))
            M[i, j] = t["s_max_m@50yr"] * 1e3
            flag[i, j] = t["status"] == "OUTSIDE_VALIDITY"
    cmap = LinearSegmentedColormap.from_list("seqblue", ["#cde2fb"] + SEQ)
    fig, ax = plt.subplots(figsize=(7.6, 0.42 * len(rows) + 1.3))
    vmin, vmax = np.nanmin(M), np.nanmax(M)
    im = ax.imshow(M, cmap=cmap, norm=LogNorm(vmin=vmin, vmax=vmax), aspect="auto")
    for i in range(M.shape[0]):
        for j in range(M.shape[1]):
            if np.isfinite(M[i, j]):
                val = M[i, j]
                txt = f"{val:.0f}" if val >= 10 else f"{val:.1f}"
                if flag[i, j]:
                    txt += "*"
                lum = np.log(val / vmin) / max(1e-9, np.log(vmax / vmin))
                ax.text(j, i, txt, ha="center", va="center", fontsize=7.5, color="#ffffff" if lum > 0.55 else INK)
            else:
                ax.text(j, i, "—", ha="center", va="center", fontsize=7.5, color=MUTED)
    ax.set_xticks(range(len(colsp)), [f"×{cf:g}\n{BF_RU[bf]}" for cf, bf in colsp], fontsize=7.5)
    ax.set_yticks(range(len(rows)), [SC_SHORT[s] for s in rows], fontsize=7.5)
    ax.grid(False)
    for s in ("left", "bottom"):
        ax.spines[s].set_visible(False)
    n_ens = sum(1 for s in rows if s.startswith("IS-SC"))
    ax.axhline(n_ens - 0.5, color=SURF, lw=3)
    ax.set_xlabel("множитель скорости ползучести (к CL-15) и закладка (через 10 лет)", color=INK2)
    cb = fig.colorbar(im, ax=ax, fraction=0.03, pad=0.02)
    cb.set_label("мм, лог. шкала", color=INK2)
    cb.outline.set_visible(False)
    ax.set_title("S8. Оседание от выемки через 50 лет, мм (центр мульды); * — вне области применимости",
                 loc="left", color=INK)
    fig.tight_layout()
    save(fig, "fig_S8_matrix.png")


def fig_s8_effects():
    r = rec("S8")
    sens = r["sensitivity_ensemble"]
    resp = [("s_max_m@50yr (log10)", "макс. оседание, 50 лет (log)"), ("s_max_m@10yr (log10)", "макс. оседание, 10 лет (log)"),
            ("s_max_m@0+ (log10)", "упругое при выемке (log)"), ("x50_m@50yr", "полуширина x50, 50 лет"),
            ("max_tilt_mm_per_m@50yr (log10)", "макс. наклон, 50 лет (log)")]
    resp = [(k, lab) for k, lab in resp if k in sens]
    facs = [("scenario", "начальное поле (IS-SC)"), ("creep_factor", "скорость ползучести"), ("backfill", "закладка")]
    fig, ax = plt.subplots(figsize=(7.4, 0.45 * len(resp) + 1.2))
    y = np.arange(len(resp))
    h = 0.26
    for k, (fk, flab) in enumerate(facs):
        vals = [sens[rk]["effects"][fk]["share_of_total_SS"] * 100 for rk, _ in resp]
        ax.barh(y + (k - 1) * h, vals, height=h - 0.04, color=CAT[k], label=flab)
        for yy, vv in zip(y + (k - 1) * h, vals):
            ax.text(vv + 0.8, yy, f"{vv:.0f}", va="center", fontsize=7, color=INK2)
    ax.set_yticks(y, [lab for _, lab in resp])
    ax.invert_yaxis()
    ax.set_xlim(0, 105)
    ax.set_xlabel("доля суммы квадратов главного эффекта, %", color=INK2)
    ax.legend(loc="lower right", fontsize=7.5)
    ax.set_title("S8. Чувствительность (ансамбль A, B 0,6/0,8, D, E × 3 × 3; главные эффекты)", loc="left")
    ax.grid(axis="y", visible=False)
    fig.tight_layout()
    save(fig, "fig_S8_sensitivity.png")


def fig_s8_time():
    r = rec("S8")
    ens = ["IS-SC-B/0.6", "IS-SC-B/0.8", "IS-SC-A", "IS-SC-D", "IS-SC-E"]
    fig, ax = plt.subplots(1, 3, figsize=(7.8, 2.8), sharey=True)
    for j, bf in enumerate(("none", "soft", "stiff")):
        for k, sc in enumerate(ens):
            rows = [t for t in r["table"] if t["scenario"] == sc and t["backfill"] == bf and t["creep_factor"] == 1.0
                    and "s_max_series_m" in t]
            if not rows:
                continue
            t = np.array(rows[0]["t_yr"])
            t[0] = 0.0
            ax[j].plot(t, np.array(rows[0]["s_max_series_m"]) * 1e3, color=CAT[k], marker=MARKERS[k], ms=3.5,
                       lw=1.6, label=SC_SHORT[sc])
        ax[j].axvline(10.0, color=MUTED, lw=0.8, ls=":")
        ax[j].set(title=f"закладка: {BF_RU[bf]}", xlabel="лет")
    ax[0].set_ylabel("макс. оседание от выемки, мм")
    ax[0].legend(loc="upper left", fontsize=7)
    fig.suptitle("S8. Развитие оседания во времени при ползучести CL-15 ×1 (пунктир — закладка через 10 лет)",
                 fontsize=8.5, color=INK2, y=1.03)
    fig.tight_layout()
    save(fig, "fig_S8_time.png")


if __name__ == "__main__":
    import sys

    todo = sys.argv[1:] or ["S5", "S6", "S7", "S8"]
    if "S5" in todo:
        fig_s5()
    if "S6" in todo:
        fig_s6()
    if "S7" in todo:
        fig_s7()
    if "S8" in todo:
        fig_s8_matrix()
        fig_s8_effects()
        fig_s8_time()
