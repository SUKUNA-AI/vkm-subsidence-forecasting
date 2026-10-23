"""Figures of the v6_2 uncertainty ensemble: maps, sections, profile line 12, calibration.

Reads the outputs of ``build_geology_ensemble.py`` (``grid`` and ``cv`` stages). Every
figure carries a scale bar, a legend and the status "synthetic ensemble, not observations".
Coordinates are the local engineering frame SH1 (metres, no EPSG).
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

import numpy as np

CONTACT_RU = {"TKT": "кровля ТКТ", "SMT": "кровля СМТ", "PKS": "кровля ПКС", "SKZ": "кровля СКЗ",
              "SZ": "кровля СЗ (= кровля пласта А)", "PDKS": "кровля ПдКС"}
COLORS = {"TKT": "#8c6d31", "SMT": "#6b8e23", "PKS": "#b0558f", "SKZ": "#7a5fb0", "SZ": "#d08a2e", "PDKS": "#2f6f9f"}
STATUS = "Синтетический ансамбль (DERIVATION): не наблюдения и не полевая проверка"
FRAME = "X, Y — местная система SH1, м (EPSG нет; глобальная привязка UNKNOWN). Z — DSM + глубины (инженерное допущение)"
ZOOM = (-2900.0, 4100.0, -5300.0, -500.0)
LINE12 = ((2680.0, -960.0), (3710.0, -960.0))


def read(path):
    return json.loads(Path(path).read_text(encoding="utf-8-sig"))


def scale_bar(ax, length_m, label):
    x0, x1 = ax.get_xlim()
    y0, y1 = ax.get_ylim()
    x = x0 + 0.05 * (x1 - x0)
    y = y0 + 0.05 * (y1 - y0)
    h = 0.012 * (y1 - y0)
    from matplotlib.patches import Rectangle

    for i in range(4):
        ax.add_patch(Rectangle((x + i * length_m / 4, y), length_m / 4, h, facecolor="black" if i % 2 == 0 else "white",
                               edgecolor="black", lw=0.6, zorder=20))
    ax.text(x, y + 1.6 * h, "0", ha="center", va="bottom", fontsize=7, zorder=21)
    ax.text(x + length_m, y + 1.6 * h, label, ha="center", va="bottom", fontsize=7, zorder=21)
    ax.text(x + length_m / 2, y - 0.8 * h, "линейный масштаб", ha="center", va="top", fontsize=5.5, zorder=21)


def north_arrow(ax):
    ax.annotate("С", xy=(0.95, 0.93), xytext=(0.95, 0.83), xycoords="axes fraction", ha="center", fontsize=8,
                arrowprops=dict(arrowstyle="-|>", color="black", lw=1.0), zorder=21)


def supports_layer(ax, model_dir, legend=True):
    from shapely.geometry import MultiPoint

    qa = read(model_dir / "FULL_CONTEXT_PARTIAL" / "qa.json")
    mapped = {r["physical_id"]: r for r in read(model_dir / "mapped_boreholes.json")}
    ids = qa["physical_groups"]
    xy = np.array([[mapped[i]["x"], mapped[i]["y"]] for i in ids])
    complete = np.array([all(v is not None for v in mapped[i]["depths_m"]) for i in ids])
    hull = MultiPoint([tuple(p) for p in xy]).convex_hull
    hx, hy = hull.exterior.xy
    ax.plot(hx, hy, color="black", lw=0.9, ls="--", zorder=10, label="оболочка опор (интерполяция внутри)")
    ax.scatter(xy[complete, 0], xy[complete, 1], s=16, c="black", zorder=12, label="скважина, полная колонка")
    ax.scatter(xy[~complete, 0], xy[~complete, 1], s=16, facecolors="white", edgecolors="black", zorder=12, label="скважина, неполная колонка")
    for pid, (x, y) in zip(ids, xy):
        ax.text(x + 60, y + 60, pid.replace("BH-SOL-", "").replace("s", "с"), fontsize=5.5, zorder=13)
    return ids, xy


def plan_map(npz, model_dir, sections, field, title, label, out, cmap="viridis", levels=None, contour=None, window=None, extra=None):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from matplotlib.tri import Triangulation

    xy, tri = npz["xy"], npz["triangles"]
    t = Triangulation(xy[:, 0], xy[:, 1], tri)
    fig, ax = plt.subplots(figsize=(11.7, 8.3), layout="constrained")
    cf = ax.tricontourf(t, field, levels=levels if levels is not None else 14, cmap=cmap)
    if contour is not None:
        cs = ax.tricontour(t, contour[0], levels=contour[1], colors="black", linewidths=0.4)
        ax.clabel(cs, fontsize=5.5, fmt="%.0f")
    cb = fig.colorbar(cf, ax=ax, shrink=0.8)
    cb.set_label(label)
    supports_layer(ax, model_dir)
    for name in ("I-I", "II-II", "III-III"):
        tr = np.asarray(sections[name]["xy"])
        ax.plot(tr[:, 0], tr[:, 1], color="#c0392b", lw=1.0, zorder=11)
        ax.text(tr[0, 0], tr[0, 1], name, color="#c0392b", fontsize=7, weight="bold", zorder=13)
    ax.plot([LINE12[0][0], LINE12[1][0]], [LINE12[0][1], LINE12[1][1]], color="#7b2d8f", lw=1.6, zorder=11, label="ПЛ 12 (реконструкция ±50 м)")
    ax.plot([], [], color="#c0392b", lw=1.0, label="трассы общих разрезов")
    if extra:
        extra(ax)
    if window:
        ax.set_xlim(window[0], window[1]); ax.set_ylim(window[2], window[3])
    ax.set_aspect("equal")
    span = ax.get_xlim()[1] - ax.get_xlim()[0]
    length = 1000.0 if span < 9000 else 2000.0
    scale_bar(ax, length, "%g км" % (length / 1000))
    north_arrow(ax)
    ax.set_xlabel("X SH1, м"); ax.set_ylabel("Y SH1, м")
    ax.set_title(title, fontsize=10)
    ax.legend(loc="upper left", fontsize=6.5, framealpha=0.9)
    fig.text(0.01, 0.005, STATUS + ". " + FRAME, fontsize=6)
    for ext in ("png", "pdf"):
        fig.savefig(out.with_suffix("." + ext), dpi=200)
    plt.close(fig)


def section_figure(sec_g, sec_p, name, out, key=("SZ", "PDKS")):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    s = sec_g["sections"][name]
    sp = sec_p["sections"][name] if sec_p else None
    ch = np.asarray(s["chainage_m"])
    fig, axes = plt.subplots(2, 1, figsize=(11.7, 9.0), sharex=True, layout="constrained", gridspec_kw={"height_ratios": [1, 1.3]})
    ax = axes[0]
    ax.plot(ch, s["dsm_m"], color="black", lw=0.8, label="DSM (GLO-30, 2011–2015)")
    for c in CONTACT_RU:
        q = np.asarray(s["bands"][c])
        ax.fill_between(ch, q[1], q[3], color=COLORS[c], alpha=0.18, lw=0)
        ax.plot(ch, q[2], color=COLORS[c], lw=1.0, label=CONTACT_RU[c])
        ax.plot(ch, s["v62"][c], color=COLORS[c], lw=0.6, ls="--")
    ax.plot([], [], color="grey", ls="--", label="модель v6_2 (IDW2)")
    ax.set_ylabel("Отметка, м")
    ax.set_title("Разрез %s: медианы P50 и полосы P10–P90 всех контактов; ансамбль G, %d реализаций" % (name, sec_g["realizations"]), fontsize=10)
    ax.grid(alpha=0.25)
    ax.legend(fontsize=6.5, ncol=4, loc="upper center", bbox_to_anchor=(0.5, -0.02), frameon=False)
    bx = axes[1]
    for c in key:
        q = np.asarray(s["bands"][c])
        bx.fill_between(ch, q[0], q[4], color=COLORS[c], alpha=0.15, lw=0, label=CONTACT_RU[c] + ": P5–P95")
        bx.fill_between(ch, q[1], q[3], color=COLORS[c], alpha=0.35, lw=0, label=CONTACT_RU[c] + ": P10–P90")
        bx.plot(ch, q[2], color=COLORS[c], lw=1.2, label=CONTACT_RU[c] + ": P50")
        bx.plot(ch, s["v62"][c], color=COLORS[c], lw=0.8, ls="--")
        if sp:
            qp = np.asarray(sp["bands"][c])
            bx.plot(ch, qp[1], color=COLORS[c], lw=0.6, ls=":")
            bx.plot(ch, qp[3], color=COLORS[c], lw=0.6, ls=":")
    bx.plot([], [], color="grey", ls="--", label="модель v6_2")
    if sp:
        bx.plot([], [], color="grey", ls=":", label="P10/P90 с плановой привязкой ±90 м")
    bx.set_ylabel("Отметка, м")
    bx.set_xlabel("Расстояние вдоль трассы, м (масштабы Г и В различны)")
    bx.set_title("Ключевые контакты: кровля СЗ (пласт А) и кровля ПдКС", fontsize=9)
    bx.grid(alpha=0.25)
    bx.legend(fontsize=6.5, ncol=3, loc="upper center", bbox_to_anchor=(0.5, -0.12), frameon=False)
    fig.text(0.01, 0.003, STATUS + ". Зубчатость полос — самородок: в узлах сетки (шаг ≈100 м) независимый шум, между узлами P1-интерполяция его усредняет.\n" + FRAME, fontsize=5.5)
    for ext in ("png", "pdf"):
        fig.savefig(out.with_suffix("." + ext), dpi=200)
    plt.close(fig)


def line12_figure(sec, out):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    s = sec["sections"]["PL12"]
    kr = sec["kr2_line12"]
    ch = np.asarray(s["chainage_m"])
    fig, axes = plt.subplots(2, 1, figsize=(11.7, 8.0), sharex=True, layout="constrained", gridspec_kw={"height_ratios": [3, 1]})
    ax = axes[0]
    for c in ("SZ", "PDKS"):
        q = np.asarray(s["bands"][c])
        ax.fill_between(ch, q[1], q[3], color=COLORS[c], alpha=0.25, lw=0)
        ax.plot(ch, q[2], color=COLORS[c], lw=1.0, label=CONTACT_RU[c] + ": P50 и P10–P90")
        ax.plot(ch, s["v62"][c], color=COLORS[c], lw=0.7, ls="--")
    styles = {"offset_7.5": ("#b8322a", "КрII, сценарий «7,5 м ниже кровли СЗ»"), "offset_16": ("#6e2c00", "КрII, сценарий «16 м ниже кровли СЗ»")}
    for key, (col, lab) in styles.items():
        q = np.asarray(kr["PL12"][key]["roof_q"])
        ax.plot(ch, q[2], color=col, lw=1.3, label=lab + ": кровля P50; P10 и P90 — штрихпунктир")
        ax.plot(ch, q[1], color=col, lw=0.8, ls="-.")
        ax.plot(ch, q[3], color=col, lw=0.8, ls="-.")
        for alt in ("PL12_north50", "PL12_south50"):
            ax.plot(ch, np.asarray(kr[alt][key]["roof_q"])[2], color=col, lw=0.5, ls=":")
    ax.plot([], [], color="grey", ls=":", label="P50 при сдвиге линии на ±50 м (привязка ПЛ 12)")
    ax.plot([], [], color="grey", ls="--", label="модель v6_2")
    ax.axvspan(0, 520, color="#fff3c4", alpha=0.25, lw=0, zorder=0, label="участок листа L04 (0–520 м)")
    ax.set_ylabel("Отметка, м")
    ax.set_title("Профильная линия 12 (блок 201): разброс отметки кровли КрII — %d реализаций; вся линия — экстраполяция" % sec["realizations"], fontsize=10)
    ax.grid(alpha=0.25)
    ax.legend(fontsize=6.5, loc="lower left", ncol=2)
    bx = axes[1]
    for key, (col, lab) in styles.items():
        bx.plot(ch, kr["PL12"][key]["conflict_fraction"], color=col, label=lab)
    bx.set_ylabel("Доля реализаций,\nгде подошва КрII\nниже кровли ПдКС", fontsize=7)
    bx.set_xlabel("Расстояние от репера 45 (≈), м; линия З → В, Y = −960 SH1")
    bx.set_ylim(-0.02, 1.02); bx.grid(alpha=0.25); bx.legend(fontsize=6.5)
    bx.plot([0, 200], [0.9, 0.9], color="black", lw=2); bx.text(100, 0.8, "200 м", ha="center", fontsize=6)
    fig.text(0.01, 0.005, STATUS + ". Сценарий КрII: правила листа L04 (MODEL_CHOICE), концы диапазона не усредняются.", fontsize=6)
    for ext in ("png", "pdf"):
        fig.savefig(out.with_suffix("." + ext), dpi=200)
    plt.close(fig)


def calibration_figure(summary, out):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    fig, axes = plt.subplots(1, 2, figsize=(11.7, 5.2), layout="constrained", sharey=True)
    for ax, nominal, key in ((axes[0], 0.8, "coverage80"), (axes[1], 0.9, "coverage90")):
        labels, vals, los, his, cols = [], [], [], [], []
        for fold, fr in (("leave_one_physical_borehole_out", "LOO"), ("spatial_block", "блоки 1,5 км")):
            for pos, pr in (("INTERPOLATION", "инт."), ("EXTRAPOLATION", "экстр.")):
                for qty, qr in (("depth", "отметки"), ("thickness", "мощн.")):
                    for var, col in (("G", "#2f6f9f"), ("P90", "#d08a2e")):
                        k = "/".join((fold, pos, qty, var, "SELECTED"))
                        if k not in summary:
                            continue
                        b = summary[k]["pooled"]
                        labels.append("%s %s %s %s (n=%d)" % (fr, pr, qr, var, b["n_readings"]))
                        vals.append(b[key]); lo, hi = b[key + "_wilson95"]; los.append(max(b[key] - lo, 0.0)); his.append(max(hi - b[key], 0.0)); cols.append(col)
        y = np.arange(len(labels))
        ax.errorbar(vals, y, xerr=[los, his], fmt="none", ecolor="grey", lw=0.8)
        ax.scatter(vals, y, c=cols, s=18, zorder=3)
        ax.axvline(nominal, color="black", lw=0.8, ls="--")
        ax.set_yticks(y); ax.set_yticklabels(labels, fontsize=6)
        ax.set_xlim(0, 1.02); ax.grid(alpha=0.25)
        ax.set_title("Покрытие интервала %d%% (номинал — пунктир; усы — Wilson 95%%)" % round(nominal * 100), fontsize=9)
    fig.text(0.01, 0.005, "Проверка согласованности модели неопределённости по отбивкам корпуса (VKM-SRC-012); отбивки одной скважины зависимы. Не полевая валидация. "
             "Синий — G (устье = DSM), оранжевый — P90 (плановая привязка ±90 м).", fontsize=6)
    for ext in ("png", "pdf"):
        fig.savefig(out.with_suffix("." + ext), dpi=200)
    plt.close(fig)


def main():
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("--grid", type=Path, required=True)
    p.add_argument("--cv", type=Path, required=True)
    p.add_argument("--model-dir", type=Path, required=True)
    p.add_argument("--output", type=Path, required=True)
    args = p.parse_args()
    out = args.output
    out.mkdir(parents=True, exist_ok=True)
    g = np.load(args.grid / "ensemble_G.npz")
    p90 = np.load(args.grid / "ensemble_P90.npz")
    sec_g = read(args.grid / "sections_G.json")
    sec_p = read(args.grid / "sections_P90.json")
    sections = sec_g["sections"]
    contacts = list(g["contacts"])
    made = []
    for c in ("SZ", "PDKS"):
        q = g[c + "_q"]
        width = q[3] - q[1]
        lv = np.arange(np.floor(q[2].min() / 10) * 10, q[2].max() + 10, 10)
        for win, tag in ((None, "full"), (ZOOM, "skru1")):
            f = out / ("map_%s_p50_%s" % (c, tag))
            plan_map(g, args.model_dir, sections, q[2], "%s: медиана P50 ансамбля G (%d реализаций); изолинии через 10 м" % (CONTACT_RU[c], int(g["realizations"])),
                     "P50 отметки, м", f, cmap="terrain", contour=(q[2], lv), window=win); made.append(f)
            f = out / ("map_%s_width80_%s" % (c, tag))
            inwin = np.ones(len(width), bool) if win is None else ((g["xy"][:, 0] >= win[0]) & (g["xy"][:, 0] <= win[1]) & (g["xy"][:, 1] >= win[2]) & (g["xy"][:, 1] <= win[3]))
            top = float(np.ceil(np.percentile(width[inwin], 99.5) / 10) * 10)
            plan_map(g, args.model_dir, sections, np.minimum(width, top), "%s: ширина интервала P10–P90, м (G); 0 — в самих скважинах" % CONTACT_RU[c],
                     "P90 − P10, м", f, cmap="magma_r", levels=np.linspace(0, top, 15), window=win); made.append(f)
        wp = p90[c + "_q"][3] - p90[c + "_q"][1]
        f = out / ("map_%s_plan_component_skru1" % c)
        plan_map(g, args.model_dir, sections, wp - width, "%s: прибавка ширины P10–P90 от плановой привязки ±90 м (P90 − G), м" % CONTACT_RU[c],
                 "Δ ширины, м", f, cmap="RdBu_r", levels=np.linspace(-15, 15, 13), window=ZOOM); made.append(f)
    k = contacts.index("PKS")
    pz = g["p_zero"][k]  # interval PKS->SKZ (p_zero[j] = interval CONTACTS[j] -> CONTACTS[j+1])
    for win, tag in ((None, "full"), (ZOOM, "skru1")):
        f = out / ("map_PKS_zero_probability_%s" % tag)
        plan_map(g, args.model_dir, sections, pz, "Вероятность нулевой мощности ПКС (ПКС отсутствует), ансамбль G; у скв. 139 — наблюдение (VKM-SRC-270)",
                 "P(мощность ПКС = 0)", f, cmap="Purples", levels=[0, 0.02, 0.05, 0.1, 0.2, 0.3, 0.5, 0.7, 0.9, 1.0001], window=win); made.append(f)
    for name in ("I-I", "II-II", "III-III"):
        f = out / ("section_%s" % name.replace("-", "_"))
        section_figure(sec_g, sec_p, name, f); made.append(f)
    f = out / "line12_kr2"
    line12_figure(sec_g, f); made.append(f)
    summary = read(args.cv / "cv_summary.json")["summary"]
    f = out / "calibration_coverage"
    calibration_figure(summary, f); made.append(f)
    print(json.dumps([m.name for m in made], ensure_ascii=False))


if __name__ == "__main__":
    main()
