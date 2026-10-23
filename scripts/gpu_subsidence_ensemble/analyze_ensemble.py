"""Analysis of the GPU ensemble: distributions, Sobol' indices by groups, per-parameter first-order indices,
representation error (from the ladder receipts) and a diagnostic look at the published PL12 levelling.

No calibration, no culling of worlds: the PL12 comparison only counts how many runs reach the order of the observed
values (different place, two mined seams, unknown time zero — see the report). All numbers are MODEL results.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import sys
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parents[1] / "src"))

import ensemble_design as ED  # noqa: E402

GROUP_RU = {"moduli": "модули (LAB × k)", "creep_viscosity": "вязкость ползучести", "extraction": "степень извлечения",
            "backfill": "закладка", "seam_depth": "положение КрII под СЗ", "seam_thickness": "мощность КрII",
            "geology": "геология (реализации v6_2)", "initial_stress": "начальное поле λ"}
PARAM_RU = {"E_factor": "k модулей", "eta_sylv": "ηs сильвинита", "eta_salt": "ηs каменной соли",
            "eta_carn": "ηs карналлита", "L_axis": "межосевое", "fill_u": "закладка (есть/степень)",
            "E_fill": "модуль закладки", "d_roof": "КрII под СЗ", "h_seam": "мощность КрII", "geo_u": "реализация геологии",
            "lam_u": "λ (1 / K0)"}


def sha(p) -> str:
    return hashlib.sha256(Path(p).read_bytes()).hexdigest()


def load(out: Path, tags):
    D = dict(np.load(out / "design.npz", allow_pickle=False))
    n = len(D["run_id"])
    T = ED.OUT_TIMES
    M = {k: np.full((n, len(T)), np.nan) for k in
         ("s_max_mm", "x_at_max_m", "width_10mm_m", "tilt_max_mm_per_m", "ux_max_mm", "eps_h_max_mm_per_m", "conv_central_m",
          "background_mm")}
    extra = {"failed": np.zeros(n, bool), "seen": np.zeros(n, bool), "seam_roof_depth_centre_m": np.full(n, np.nan),
             "pillar_creep_eq_mean_final": np.full(n, np.nan)}
    prof = {}
    gpu = {"time_s": 0.0, "batches": 0, "peak_gb": 0.0, "factorizations": 0, "solves": 0}
    grid = None
    for tag in tags:
        for f in sorted((out / "batches" / tag).glob("b*.npz")):
            b = np.load(f)
            ids = b["run_id"]
            grid = b["grid"]
            for k in M:
                M[k][ids] = b[k]
            extra["failed"][ids] |= b["failed"].astype(bool)
            extra["seen"][ids] = True
            extra["seam_roof_depth_centre_m"][ids] = b["seam_roof_depth_centre_m"]
            extra["pillar_creep_eq_mean_final"][ids] = b["pillar_creep_eq_mean_final"]
            for j, t in enumerate(T):
                if t in (0.0, 10.0, 20.0, 40.0):
                    prof.setdefault(t, {})
                    for r, i in enumerate(ids):
                        prof[t][int(i)] = b["s_grid_m"][r, j]
            gpu["time_s"] += float(b["gpu_time_s"])
            gpu["batches"] += 1
            gpu["peak_gb"] = max(gpu["peak_gb"], float(b["peak_mem_gb"]))
            gpu["factorizations"] += int(b["factorizations"])
            gpu["solves"] += int(b["solves"])
    return D, M, extra, prof, grid, gpu


def sobol_groups(D, Y, ok, n_boot=500, seed=1):
    """Saltelli 2010 first-order and Jansen total indices by groups with bootstrap 90 % intervals."""
    N = int((D["block"] == "A").sum())
    g = len(ED.GROUPS)
    blk, grp = D["block"], D["group"]
    iA = np.where(blk == "A")[0]
    iB = np.where(blk == "B")[0]
    iAB = [np.where((blk == "AB") & (grp == k))[0] for k in range(g)]
    valid = ok[iA] & ok[iB] & np.all([ok[i] for i in iAB], axis=0) & np.isfinite(Y[iA]) & np.isfinite(Y[iB])
    for i in iAB:
        valid &= np.isfinite(Y[i])
    rows = np.where(valid)[0]

    def est(sel):
        fA, fB = Y[iA[sel]], Y[iB[sel]]
        V = np.var(np.concatenate([fA, fB]), ddof=1)
        S, ST = [], []
        for k in range(g):
            fABk = Y[iAB[k][sel]]
            S.append(np.mean(fB * (fABk - fA)) / V)
            ST.append(0.5 * np.mean((fA - fABk) ** 2) / V)
        return np.array(S), np.array(ST)

    S, ST = est(rows)
    rng = np.random.default_rng(seed)
    bs = [est(rng.choice(rows, len(rows))) for _ in range(n_boot)]
    Sb = np.array([b[0] for b in bs])
    STb = np.array([b[1] for b in bs])
    res = {}
    for k, (name, _) in enumerate(ED.GROUPS):
        res[name] = {"S1": float(S[k]), "S1_q05": float(np.quantile(Sb[:, k], 0.05)), "S1_q95": float(np.quantile(Sb[:, k], 0.95)),
                     "ST": float(ST[k]), "ST_q05": float(np.quantile(STb[:, k], 0.05)), "ST_q95": float(np.quantile(STb[:, k], 0.95))}
    return res, int(len(rows)), N


def first_order_by_bins(x, y, bins=24):
    ok = np.isfinite(x) & np.isfinite(y)
    x, y = x[ok], y[ok]
    if len(np.unique(x)) <= bins:
        keys = np.unique(x)
        groups = [y[x == k] for k in keys]
    else:
        edges = np.quantile(x, np.linspace(0, 1, bins + 1))
        lab = np.clip(np.searchsorted(edges, x, side="right") - 1, 0, bins - 1)
        groups = [y[lab == k] for k in range(bins)]
    m = y.mean()
    vb = sum(len(gk) * (gk.mean() - m) ** 2 for gk in groups if len(gk)) / len(y)
    return float(vb / y.var())


def pl12(private_root: Path | None):
    if private_root is None:
        return None
    p = private_root / "11_evidence_vnext/canonical/WORLD_OBSERVATIONS/digitized/VKM-SRC-011/fig_4_7b/series.csv"
    if not p.exists():
        return None
    obs = {}
    for r in csv.DictReader(open(p, encoding="utf-8")):
        obs[int(r["epoch_or_date"])] = max(obs.get(int(r["epoch_or_date"]), 0.0), float(r["y_value"]))
    return dict(sorted(obs.items())), sha(p)


def q(a, ps=(0.05, 0.25, 0.5, 0.75, 0.95)):
    a = a[np.isfinite(a)]
    return {f"q{int(100 * p):02d}": float(np.quantile(a, p)) for p in ps} | {"mean": float(a.mean()), "min": float(a.min()),
                                                                              "max": float(a.max()), "n": int(len(a))}


def figures(D, M, ok, prof, grid, sob, obs, rep, figdir: Path):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    T = np.asarray(ED.OUT_TIMES)
    plt.rcParams.update({"font.size": 9})
    s = M["s_max_mm"]
    # 1 fan chart of s_max(t)
    fig, ax = plt.subplots(1, 2, figsize=(11, 4.2))
    qs = np.nanquantile(s[ok], [0.05, 0.25, 0.5, 0.75, 0.95], axis=0)
    for a_, logy in zip(ax, (False, True)):
        a_.fill_between(T, qs[0], qs[4], color="#9ecae1", label="5–95 %")
        a_.fill_between(T, qs[1], qs[3], color="#4292c6", label="25–75 %")
        a_.plot(T, qs[2], color="#08306b", lw=2, label="медиана")
        a_.set_xlabel("время после выемки, лет")
        a_.set_ylabel("максимум оседания, мм (МОДЕЛЬ)")
        a_.grid(alpha=0.3)
        if logy:
            a_.set_yscale("log")
    ax[0].legend(loc="upper left")
    ax[0].set_title("Ансамбль: рост максимума оседания во времени")
    ax[1].set_title("то же, логарифмическая шкала")
    fig.tight_layout()
    fig.savefig(figdir / "fig_e1_smax_fan.png", dpi=150)
    plt.close(fig)
    # 2 histograms at 10/20/40
    fig, ax = plt.subplots(1, 3, figsize=(12, 3.6), sharey=True)
    for a_, t in zip(ax, (10.0, 20.0, 40.0)):
        j = ED.OUT_TIMES.index(t)
        v = s[ok, j]
        a_.hist(np.log10(v[v > 0]), bins=60, color="#4292c6")
        a_.set_xlabel("lg(максимум оседания, мм)")
        a_.set_title(f"{t:g} лет: медиана {np.median(v):.0f} мм, 5–95 % {np.quantile(v, .05):.0f}–{np.quantile(v, .95):.0f} мм")
        a_.grid(alpha=0.3)
    ax[0].set_ylabel("число прогонов")
    fig.tight_layout()
    fig.savefig(figdir / "fig_e2_smax_hist.png", dpi=150)
    plt.close(fig)
    # 3 Sobol bars
    names = [g for g, _ in ED.GROUPS]
    fig, ax = plt.subplots(1, 3, figsize=(13, 4.2), sharey=True)
    for a_, t in zip(ax, ("10", "20", "40")):
        r = sob[f"log10_s_max_t{t}"]["indices"]
        y = np.arange(len(names))
        S1 = [r[n]["S1"] for n in names]
        ST = [r[n]["ST"] for n in names]
        e1 = [[r[n]["S1"] - r[n]["S1_q05"] for n in names], [r[n]["S1_q95"] - r[n]["S1"] for n in names]]
        eT = [[r[n]["ST"] - r[n]["ST_q05"] for n in names], [r[n]["ST_q95"] - r[n]["ST"] for n in names]]
        a_.barh(y + 0.2, ST, 0.4, xerr=eT, color="#fdae6b", label="полный ST")
        a_.barh(y - 0.2, S1, 0.4, xerr=e1, color="#3182bd", label="первого порядка S1")
        a_.set_yticks(y, [GROUP_RU[n] for n in names])
        a_.set_title(f"lg s_max, {t} лет")
        a_.axvline(0, color="k", lw=0.5)
        a_.grid(alpha=0.3, axis="x")
    ax[0].invert_yaxis()
    ax[0].legend(loc="lower right")
    fig.suptitle("Индексы Соболя по элементам неопределённости (90 % бутстреп)")
    fig.tight_layout()
    fig.savefig(figdir / "fig_e3_sobol.png", dpi=150)
    plt.close(fig)
    # 4 scatter vs key parameters
    keys = ["eta_sylv", "L_axis", "E_factor", "d_roof", "h_seam", "E_fill"]
    j40 = ED.OUT_TIMES.index(40.0)
    fig, ax = plt.subplots(2, 4, figsize=(14, 6.4))
    ax = ax.ravel()
    for a_, k in zip(ax, keys):
        a_.scatter(D[k][ok], s[ok, j40], s=1, alpha=0.25, color="#08519c")
        a_.set_yscale("log")
        if k in ("eta_sylv", "E_fill"):
            a_.set_xscale("log")
        a_.set_xlabel(PARAM_RU[k])
        a_.grid(alpha=0.3)
    a_ = ax[6]
    dep = np.asarray(M.get("seam_depth", np.full(len(ok), np.nan)))
    a_.scatter(D["seam_roof_depth_centre_m"][ok] if "seam_roof_depth_centre_m" in D else dep[ok], s[ok, j40], s=1, alpha=0.25, color="#08519c")
    a_.set_yscale("log")
    a_.set_xlabel("глубина кровли КрII в центре панели, м")
    a_.grid(alpha=0.3)
    a_ = ax[7]
    for k0, c in ((0, "#3182bd"), (1, "#e6550d")):
        for fo, mk in ((0, "o"), (1, "x")):
            sel = ok & (D["k0"] == k0) & (D["fill_on"] == fo)
            a_.hist(np.log10(s[sel, j40]), bins=50, histtype="step", color=c, ls="-" if fo == 0 else "--",
                    label=f"λ={'K0' if k0 else '1'}, {'закладка' if fo else 'без закладки'}")
    a_.legend(fontsize=7)
    a_.set_xlabel("lg s_max(40 лет), мм")
    for a_ in ax[::4]:
        a_.set_ylabel("s_max через 40 лет, мм")
    fig.suptitle("Максимум оседания через 40 лет против параметров (МОДЕЛЬ)")
    fig.tight_layout()
    fig.savefig(figdir / "fig_e4_scatter.png", dpi=150)
    plt.close(fig)
    # 5 profile bands
    fig, ax = plt.subplots(figsize=(10, 4.2))
    for t, c in ((10.0, "#9ecae1"), (20.0, "#4292c6"), (40.0, "#08306b")):
        P_ = np.array([prof[t][i] for i in sorted(prof[t]) if ok[i]]) * 1e3
        qq = np.quantile(P_, [0.05, 0.5, 0.95], axis=0)
        ax.fill_between(grid, qq[0], qq[2], color=c, alpha=0.35)
        ax.plot(grid, qq[1], color=c, lw=2, label=f"{t:g} лет: медиана и 5–95 %")
    ax.axvspan(1159.4, 1759.4, color="0.9", zorder=0, label="условная панель 600 м")
    ax.invert_yaxis()
    ax.set_xlabel("пикетаж по трассе 74–75–23–30, м")
    ax.set_ylabel("оседание, мм (МОДЕЛЬ)")
    ax.legend()
    ax.grid(alpha=0.3)
    fig.tight_layout()
    fig.savefig(figdir / "fig_e5_profiles.png", dpi=150)
    plt.close(fig)
    # 6 PL12 diagnostic
    if obs:
        fig, ax = plt.subplots(figsize=(7.5, 4.2))
        for t, c in ((5.0, "#c6dbef"), (10.0, "#9ecae1"), (15.0, "#6baed6"), (20.0, "#2171b5"), (40.0, "#08306b")):
            j = ED.OUT_TIMES.index(t)
            v = np.sort(s[ok, j])
            ax.plot(v, np.linspace(0, 1, len(v)), color=c, label=f"модель, {t:g} лет")
        for yr, val in obs.items():
            ax.axvline(val, color="#e6550d", lw=1, ls="--")
            ax.text(val, 0.02 + 0.06 * (yr % 5), str(yr), color="#e6550d", fontsize=7)
        ax.set_xscale("log")
        ax.set_xlabel("максимум оседания, мм")
        ax.set_ylabel("доля прогонов не выше")
        ax.set_title("Диагностика: ПЛ 12 (наблюдение, другое место) и ансамбль (модель)")
        ax.legend(fontsize=7, loc="center right")
        ax.grid(alpha=0.3)
        fig.tight_layout()
        fig.savefig(figdir / "fig_e6_PL12_diagnostic.png", dpi=150)
        plt.close(fig)
    # 7 representation error
    if rep:
        fig, ax = plt.subplots(figsize=(8, 4))
        for tag, r in rep.items():
            for key, ls in (("vs_MAPDL_fine", "-"), ("vs_OGS", ":"), ("vs_gpu_reference_mesh_converged", "--"), ("vs_MAPDL_main", "-.")):
                if key in r:
                    tt = [x["t_yr"] for x in r[key]]
                    ax.plot(tt, [x["rel_diff_pct"] for x in r[key]], ls=ls, marker=".", label=f"{tag} {key[3:]}")
        ax.axhline(0, color="k", lw=0.5)
        ax.set_xlabel("время, лет")
        ax.set_ylabel("(GPU ансамбль − эталон) / эталон, %")
        ax.set_title("Ошибка представления ансамблевой конфигурации GPU")
        ax.legend(fontsize=6, ncol=2)
        ax.grid(alpha=0.3)
        fig.tight_layout()
        fig.savefig(figdir / "fig_e7_representation_error.png", dpi=150)
        plt.close(fig)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--tags", nargs="*", default=["main"])
    ap.add_argument("--gate", type=Path, required=True)
    ap.add_argument("--private-root", type=Path)
    ap.add_argument("--boot", type=int, default=500)
    a = ap.parse_args()
    D, M, extra, prof, grid, gpu = load(a.out, a.tags)
    D["seam_roof_depth_centre_m"] = extra["seam_roof_depth_centre_m"]
    ok = extra["seen"] & ~extra["failed"] & np.isfinite(M["s_max_mm"]).all(1)
    T = ED.OUT_TIMES
    res = {"schema": "vkm.gpu_ensemble.analysis/1", "status": "MODEL results of synthetic worlds (not observations, not a forecast)",
           "runs_total": int(len(ok)), "runs_seen": int(extra["seen"].sum()), "runs_failed": int(extra["failed"].sum()),
           "runs_ok": int(ok.sum()), "gpu": gpu, "times_yr": T}
    s = M["s_max_mm"]
    j0, j10, j20, j40 = (T.index(t) for t in (0.0, 10.0, 20.0, 40.0))
    res["distributions"] = {
        **{f"s_max_mm_t{t:g}": q(s[ok, T.index(t)]) for t in T},
        "creep_part_mm_t40": q(s[ok, j40] - s[ok, j0]),
        "ratio_s40_to_s0": q(s[ok, j40] / s[ok, j0]),
        "rate_mm_per_yr_35_40": q((s[ok, j40] - s[ok, T.index(35.0)]) / 5.0),
        "width_10mm_m_t40": q(np.nan_to_num(M["width_10mm_m"][ok, j40], nan=0.0)),
        "share_s40_below_10mm": float(np.mean(s[ok, j40] < 10.0)),
        "tilt_max_mm_per_m_t40": q(M["tilt_max_mm_per_m"][ok, j40]),
        "eps_h_max_mm_per_m_t40": q(M["eps_h_max_mm_per_m"][ok, j40]),
        "ux_max_mm_t40": q(M["ux_max_mm"][ok, j40]),
        "conv_central_m_t40": q(M["conv_central_m"][ok, j40]),
        "background_REF_mm_t40_by_lambda": {"L1": q(M["background_mm"][ok & (D["k0"] == 0), j40]),
                                            "K0": q(M["background_mm"][ok & (D["k0"] == 1), j40])},
        "pillar_creep_eq_mean_t40": q(extra["pillar_creep_eq_mean_final"][ok]),
        "seam_roof_depth_centre_m": q(extra["seam_roof_depth_centre_m"][ok]),
    }
    res["by_lambda_t40"] = {lab: q(s[ok & (D["k0"] == k), j40]) for lab, k in (("L1", 0), ("K0", 1))}
    res["by_fill_t40"] = {lab: q(s[ok & (D["fill_on"] == k), j40]) for lab, k in (("no_fill", 0), ("fill", 1))}
    sob = {}
    outputs = {f"s_max_t{t:g}": s[:, T.index(t)] for t in (10.0, 20.0, 40.0)}
    outputs |= {f"log10_s_max_t{t:g}": np.log10(np.maximum(s[:, T.index(t)], 1e-6)) for t in (0.0, 10.0, 20.0, 40.0)}
    outputs["creep_part_t40"] = s[:, j40] - s[:, j0]
    outputs["log10_tilt_t40"] = np.log10(np.maximum(M["tilt_max_mm_per_m"][:, j40], 1e-9))
    outputs["width_10mm_t40"] = np.nan_to_num(M["width_10mm_m"][:, j40], nan=0.0)
    for name, Y in outputs.items():
        r, nvalid, N = sobol_groups(D, Y, ok, n_boot=a.boot)
        sob[name] = {"indices": r, "N_valid": nvalid, "N": N}
    res["sobol_groups"] = sob
    AB = np.isin(D["block"], ["A", "B"]) & ok
    res["first_order_by_parameter_given_data"] = {
        name: {p[0]: first_order_by_bins(D[p[0]][AB], Y[AB]) for p in ED.param_list()}
        for name, Y in outputs.items() if name in ("log10_s_max_t10", "log10_s_max_t40", "s_max_t40")}
    gate = json.loads(a.gate.read_text(encoding="utf-8"))
    rep = gate["steps"].get("L5_representation", {}).get("results", {})
    res["representation_error"] = {k: v for k, v in rep.items() if not k.startswith("batch_")}
    po = pl12(a.private_root)
    obs = None
    if po:
        obs, h = po
        # diagnostic only: years since the probable zero of the published series (1989, not stated in the source)
        diag = {}
        for yr, val in obs.items():
            dt = yr - 1989
            tj = min(T, key=lambda t: abs(t - dt))
            v = s[ok, T.index(tj)]
            diag[str(yr)] = {"observed_max_mm": val, "model_time_yr": tj,
                             "share_runs_ge_obs": float(np.mean(v >= val)),
                             "share_runs_ge_obs_div3": float(np.mean(v >= val / 3)),
                             "share_runs_ge_obs_div10": float(np.mean(v >= val / 10)),
                             "share_runs_within_factor2": float(np.mean((v >= val / 2) & (v <= 2 * val))),
                             "model_median_mm": float(np.median(v)), "model_q95_mm": float(np.quantile(v, 0.95))}
        res["diagnostic_PL12"] = {"status": "DIAGNOSTIC ONLY: different place (block 201, two seams), unknown time zero "
                                            "(1989 assumed only for this look), no calibration, no culling",
                                  "source": "VKM-SRC-011 рис. 4.7б (вторичная публикация), оцифровка PRIVATE, AUTO_EXTRACTED_UNREVIEWED",
                                  "series_sha256": h, "by_epoch": diag}
    figdir = a.out / "figures"
    figdir.mkdir(exist_ok=True)
    figures(D, M, ok, prof, grid, sob, obs, res["representation_error"], figdir)
    (a.out / "analysis.json").write_text(json.dumps(res, ensure_ascii=False, indent=1), encoding="utf-8")
    # compact table of all runs
    with open(a.out / "runs_table.csv", "w", encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        pn = [p[0] for p in ED.param_list()]
        w.writerow(["run_id", "block", "group"] + pn + ["fill_on", "fill_frac", "geo", "k0", "n_ch", "seam_roof_depth_m", "ok"]
                   + [f"s_max_mm_t{t:g}" for t in T] + ["width_10mm_m_t40", "tilt_max_mm_per_m_t40", "eps_h_max_mm_per_m_t40",
                                                        "conv_central_m_t40", "background_mm_t40"])
        for i in range(len(ok)):
            w.writerow([int(D["run_id"][i]), str(D["block"][i]), int(D["group"][i])] + [f"{D[p][i]:.6g}" for p in pn]
                       + [int(D["fill_on"][i]), f"{D['fill_frac'][i]:.3f}", int(D["geo"][i]), int(D["k0"][i]), int(D["n_ch"][i]),
                          f"{extra['seam_roof_depth_centre_m'][i]:.2f}", int(ok[i])]
                       + [f"{v:.4f}" for v in s[i]] + [f"{M['width_10mm_m'][i, j40]:.1f}", f"{M['tilt_max_mm_per_m'][i, j40]:.4f}",
                                                      f"{M['eps_h_max_mm_per_m'][i, j40]:.4f}", f"{M['conv_central_m'][i, j40]:.4f}",
                                                      f"{M['background_mm'][i, j40]:.5f}"])
    print(json.dumps({k: res[k] for k in ("runs_total", "runs_ok", "runs_failed", "gpu")}, ensure_ascii=False))


if __name__ == "__main__":
    main()
