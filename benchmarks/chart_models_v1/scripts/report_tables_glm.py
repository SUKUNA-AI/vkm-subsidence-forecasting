"""Markdown tables of the GLM-OCR addendum (RESULTS.md §10) from results_glm_v1.json. Usage: report_tables_glm.py <json>"""
from __future__ import annotations

import json
import sys


def f(v, nd=3):
    if v is None:
        return "—"
    if isinstance(v, (list, tuple)):
        return f"{f(v[0], 2)} (n={v[1]})"
    return f"{v:.{nd}f}".replace(".", ",")


def ci(b):
    return f"{f(b['mean_diff'])} [{f(b['ci95'][0])}; {f(b['ci95'][1])}]"


def main() -> None:
    d = json.loads(open(sys.argv[1], encoding="utf-8").read())
    a, per = d["aggregate"], d["per_figure"]
    t = a["T1"]
    n_lab = t["qwen"]["n_labels"]
    axis = {arm: sum((per[k][f"ref_{arm}"].get("axis_hits") or 0) for k in per) / n_lab
            for arm in ("qwen", "granite", "tesseract")}
    print("### T1-GLM: все 268 подписей\n")
    print("| Чтец | Вход | Полнота | Точность | Полнота с учётом оси |")
    print("|---|---|---|---|---|")
    rows = [("Qwen3.5-9B", "весь рисунок, JSON (T1)", t["qwen"], axis["qwen"]),
            ("Granite 4.1 4B", "весь рисунок, JSON (T1)", t["granite"], axis["granite"]),
            ("**GLM-OCR W**", "весь рисунок, `Text Recognition:`, числа и даты", t["glm_W"], None),
            ("GLM-OCR W, все токены", "то же, все токены", t["glm_W_all_tokens"], None),
            ("GLM-OCR IE", "весь рисунок, JSON-схема", t["glm_IE"], t["glm_IE"].get("micro_axis_recall")),
            ("Tesseract (рука G)", "весь рисунок (разреженный OCR) + кропы у осей", t["tesseract_pooled"],
             axis["tesseract"])]
    for name, inp, r, ax in rows:
        print(f"| {name} | {inp} | {f(r['micro_recall'])} | {f(r['micro_precision'])} | {f(ax) if ax is not None else '—'} |")
    print("\nНазвания, единицы, легенда:\n")
    ie, w = t["glm_IE"], t["glm_W"]
    print(f"- GLM-OCR IE (поля, точное совпадение): название x {f(ie['x_title_ok'])}, y {f(ie['y_title_ok'])}; "
          f"единица x {f(ie['x_unit_ok'])}, y {f(ie['y_unit_ok'])}; легенда {f(ie['legend_recall_mean'])} "
          f"(по отступлению G1 — {f(ie['legend_recall_mean_G1'])}); разобрано JSON {ie['parsed']} из 19")
    print(f"- GLM-OCR W (найдено в тексте): название x {f(w['x_title_found'])}, y {f(w['y_title_found'])}; "
          f"легенда {f(w['legend_found_mean'])}")
    print("\nПо рисункам (полнота подписей):\n")
    print("| Рисунок | подписей | GLM W | GLM W все | GLM IE | Tesseract | Qwen | Granite |")
    print("|---|---|---|---|---|---|---|---|")
    for k, r in per.items():
        print(f"| {k} | {r['W']['n_truth']} | {f(r['W']['recall'], 2)} | {f(r['W_all_tokens']['recall'], 2)} | "
              f"{f(r['IE']['recall'], 2)} | {f(r['ref_tesseract']['recall'], 2)} | {f(r['ref_qwen']['recall'], 2)} | "
              f"{f(r['ref_granite']['recall'], 2)} |")
    print("\n### Те же кропы, что у Tesseract\n")
    c, tc = t["glm_C"], t["tesseract_C"]
    print(f"- кропов {t['n_crops']} на {len(t['C_figures'])} рисунках; подписей истины на этих рисунках {c['n_labels']}")
    print(f"- полнота (мультимножество): GLM-OCR {f(c['micro_recall'])}, Tesseract {f(tc['micro_recall'])}; "
          f"точность: GLM-OCR {f(c['micro_precision'])}, Tesseract {f(tc['micro_precision'])}; "
          f"одинаковый ответ на кроп — {f(t['C_agreement_mean'], 2)}")
    if "G3_crops_manual" in t:
        g = t["G3_crops_manual"]
        print(f"- по каждому кропу (разведочно G3): кропов с настоящей подписью {g['label_crops']}, прочитано точно: "
              f"GLM-OCR {f(g['read_exactly']['glm'], 3)}, Tesseract {f(g['read_exactly']['tesseract'], 3)} "
              f"(только GLM прав — {g['only_glm_right']}, только Tesseract — {g['only_tesseract_right']}); "
              f"кропов без подписи {g['non_label_crops']}, из них «число» в ответе: GLM-OCR "
              f"{f(g['spurious_label_like_reading']['glm'], 2)}, Tesseract {f(g['spurious_label_like_reading']['tesseract'], 2)}")
    print("\n### Разности (парный бутстреп, ДИ 95 %)\n")
    for key, name in (("A1_glmW_vs_tesseract_pooled", "A1: GLM W − Tesseract (весь рисунок)"),
                      ("A2_glmC_vs_tesseractC", "A2: GLM C − Tesseract на тех же кропах"),
                      ("glmIE_vs_tesseract_pooled", "A3: GLM IE − Tesseract"),
                      ("B1_glmIE_vs_qwen", "B1: GLM IE − Qwen"),
                      ("B2_glmW_all_vs_qwen", "B2: GLM W (все токены) − Qwen"),
                      ("B3_glmIE_vs_granite", "B3: GLM IE − Granite")):
        print(f"- {name}: {ci(t[key])}, n = {t[key]['n']}")
    print(f"- G2 (разведочно): IE с мягким разбором JSON — полнота {f(t['glm_IE_lenient_G2']['micro_recall'])}")
    print("\n### Маршрут R с другим чтецом (вторичное S)\n")
    print("| Рука | Чтец подписей | Обе оси откалиброваны (из 19) | hit@2 % (17 рисунков) |")
    print("|---|---|---|---|")
    names = {"G": "Tesseract (как у FD)", "RC": "GLM-OCR читает кропы вместо Tesseract",
             "RIE": "подписи GLM-OCR IE на блоках (механизм H)", "RQ": "подписи Qwen T1 на блоках (механизм H)",
             "H": "Qwen T1 + число и подписи рядов (рука H основной части)"}
    for arm in ("G", "RC", "RIE", "RQ", "H"):
        r = a["route_r"].get(arm)
        if r:
            print(f"| {arm} | {names[arm]} | {r['both_axes_calibrated']} | {f(r['mean_hit@2%'])} |")
    for arm in ("RC", "RIE", "RQ"):
        b = a["route_r"].get(f"{arm}_vs_G_hit@2%")
        if b:
            print(f"\n{arm} − G, hit@2 %: {ci(b)}")
    print("\n### Время и память\n")
    for v, name in (("W", "весь рисунок, текст"), ("IE", "весь рисунок, JSON-схема"), ("C", "кропы, сумма на рисунок")):
        s = a["speed"][v]
        print(f"- {name}: медиана {f(s['median_s'], 2)} с, максимум {f(s['max_s'], 2)} с, всего {f(s['sum_s'], 1)} с; "
              f"пик всей карты {s['peak_card_mib']} МиБ")


if __name__ == "__main__":
    main()
