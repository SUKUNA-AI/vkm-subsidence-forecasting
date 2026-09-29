"""CHART_MODELS_V1 — markdown tables for RESULTS.md from results_v1.json (+ exploratory json). Numbers are printed,
not typed by hand. Usage: report_tables.py <results_v1.json> [<exploratory json>]"""
from __future__ import annotations

import json
import statistics
import sys


def f(v, nd=2, pct=False):
    if v is None:
        return "—"
    if isinstance(v, (list, tuple)):
        return f"{f(v[0], nd, pct)} (n={v[1]})"
    return (f"{100 * v:.{max(0, nd - 2)}f} %" if pct else f"{v:.{nd}f}").replace(".", ",")


def main() -> None:
    d = json.loads(open(sys.argv[1], encoding="utf-8").read())
    per, a = d["per_figure"], d["aggregate"]
    keys = list(per)
    arms1 = [("qwen", "Qwen3.5-9B"), ("granite", "Granite 4.1 4B"), ("tesseract", "Tesseract (маршрут R)")]
    print("### T1 — подписи осей\n")
    print("| Рука | Полнота подписей | Полнота с учётом оси | Точность | Название x | Название y | Единица x | Единица y | Легенда (полнота) |")
    print("|---|---|---|---|---|---|---|---|---|")
    for arm, name in arms1:
        r = a["T1"][arm]
        print(f"| {name} | {f(r['micro_recall'], 3)} | {f(r['micro_axis_recall'], 3)} | {f(r['micro_precision'], 3)} | "
              f"{f(r.get('x_title_ok'))} | {f(r.get('y_title_ok'))} | {f(r.get('x_unit_ok'))} | {f(r.get('y_unit_ok'))} | "
              f"{f(r.get('legend_recall_mean'))} |")
    for arm in ("qwen", "granite"):
        b = a["T1"][f"{arm}_vs_tesseract_recall"]
        print(f"\n{arm} − Tesseract, средняя по рисункам разность полноты: {f(b['mean_diff'], 3)}, ДИ 95 % "
              f"[{f(b['ci95'][0], 3)}; {f(b['ci95'][1], 3)}], n = {b['n']}")
    print("\nПо рисункам (полнота подписей):\n")
    print("| Рисунок | подписей | Qwen | Granite | Tesseract |")
    print("|---|---|---|---|---|")
    for k in keys:
        t = per[k]["T1"]
        print(f"| {k} | {t['qwen']['n_truth']} | {f(t['qwen']['recall'])} | {f(t['granite']['recall'])} | {f(t['tesseract']['recall'])} |")
    arms2 = [("qwen", "Qwen3.5-9B"), ("granite", "Granite 4.1 4B"), ("G", "G — маршрут R"), ("H", "H — гибрид")]
    print("\n### T2 — ряды (17 рисунков с числовой истиной)\n")
    print("| Рука | hit@1 % | **hit@2 %** | hit@5 % | vhit@2 % | медиана расстояния | медиана |Δy| на рисунках в мм | верное число рядов | подписи рядов верны | пропуски | галлюцинации |")
    print("|---|---|---|---|---|---|---|---|---|---|---|")
    for arm, name in arms2:
        r = a["T2"][arm]
        vh = statistics.mean([per[k]["T2"][arm].get("vhit@2%", 0.0) for k in keys if "T2" in per[k]])
        print(f"| {name} | {f(r['mean_hit@1%'])} | **{f(r['mean_hit@2%'])}** | {f(r['mean_hit@5%'])} | {f(vh)} | "
              f"{f(r['median_of_figure_median_rel_dist'], 4)} | {f(r['median_abs_err_mm_figs'])} | {r['series_count_ok']}/17 | "
              f"{f(r['label_acc_micro'])} | {f(r['mean_missing_share'])} | {f(r['mean_hallucinated_share'])} |")
    for pair in ("qwen_vs_G", "granite_vs_G", "H_vs_G", "qwen_vs_granite"):
        b = a["T2"][f"{pair}_hit@2%"]
        print(f"\n{pair}: разность hit@2 % {f(b['mean_diff'], 3)}, ДИ 95 % [{f(b['ci95'][0], 3)}; {f(b['ci95'][1], 3)}]")
    print("\nПо рисункам, hit@2 % (в скобках — медиана |Δy| в единицах оси y):\n")
    print("| Рисунок | рядов | точек | Qwen | Granite | G | H |")
    print("|---|---|---|---|---|---|---|")
    for k in keys:
        if "T2" not in per[k]:
            continue
        t = per[k]["T2"]
        cell = lambda arm: f"{f(t[arm].get('hit@2%', 0.0))} ({f(t[arm].get('median_abs_err'))})"  # noqa: E731
        print(f"| {k} | {t['qwen']['n_truth_series']} | {t['qwen']['n_truth_points']} | {cell('qwen')} | {cell('granite')} | "
              f"{cell('G')} | {cell('H')} |")
    print("\nR1 и R5 (описательно):\n")
    for k in keys:
        if "T2_qualitative" in per[k]:
            q = per[k]["T2_qualitative"]
            lg = lambda a: q[a].get("labels_in_printed_legend")  # noqa: E731
            print(f"- {k}: истина {q['truth_series_count']} рядов, в легенде {q.get('truth_legend_entries')}; "
                  f"Qwen {q['qwen']['n_series']} рядов (подписей из легенды {lg('qwen')}); "
                  f"Granite {q['granite']['n_series']} ({lg('granite')}); "
                  f"G {q['G']['n_series']} ({q['G']['axis_status']}, {lg('G')}); H {q['H']['n_series']} ({q['H']['axis_status']}, {lg('H')})")
    if any("qwen_think" in per[k].get("T2", {}) for k in keys):
        print("\n### E1 — Qwen с рассуждением\n")
        print("| Рисунок | hit@2 % без рассуждения | hit@2 % с рассуждением | токенов ответа | секунд |")
        print("|---|---|---|---|---|")
        for k in keys:
            t = per[k].get("T2", {})
            if "qwen_think" in t:
                print(f"| {k} | {f(t['qwen'].get('hit@2%', 0.0))} | {f(t['qwen_think'].get('hit@2%', 0.0))} | "
                      f"{t['qwen_think'].get('completion_tokens')} | {f(t['qwen_think'].get('wall_s'), 0)} |")
    print("\n### T3 — время\n")
    for arm in ("qwen", "granite"):
        w1 = [per[k]["T1"][arm]["wall_s"] for k in keys if per[k]["T1"][arm].get("wall_s") is not None]
        w2 = [per[k]["T2"][arm]["wall_s"] for k in keys if "T2" in per[k] and per[k]["T2"][arm].get("wall_s") is not None]
        pk = [per[k]["T1"][arm].get("peak_card_mib") for k in keys if per[k]["T1"][arm].get("peak_card_mib")]
        pk += [per[k]["T2"][arm].get("peak_card_mib") for k in keys if "T2" in per[k] and per[k]["T2"][arm].get("peak_card_mib")]
        if w1:
            print(f"- {arm}: T1 медиана {f(statistics.median(w1), 1)} с, максимум {f(max(w1), 1)} с; T2 медиана "
                  f"{f(statistics.median(w2), 1)} с, максимум {f(max(w2), 1)} с; пик карты {max(pk)} МиБ")
    ge = [per[k]["T1"]["tesseract"]["elapsed_s"] for k in keys]
    print(f"- маршрут R (G): медиана {f(statistics.median(ge), 2)} с, максимум {f(max(ge), 1)} с на рисунок, CPU")
    if len(sys.argv) > 2:
        x = json.loads(open(sys.argv[2], encoding="utf-8").read())["aggregate"]
        print("\n### Разведочное (не предрегистрировано)\n")
        for kk, v in x.items():
            print(f"- {kk}: {json.dumps(v, ensure_ascii=False)}")


if __name__ == "__main__":
    main()
