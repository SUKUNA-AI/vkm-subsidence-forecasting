"""Print the markdown tables of OGS_QUICK_CHECKS_RU.md from the committed receipts (no hand transcription)."""
from __future__ import annotations

import json

import oqc_common as C
import oqc_toy as T


def rec(step):
    return json.load(open(C.RECEIPTS / f"{step}.json", encoding="utf-8"))


def e(x, n=2):
    return f"{x:.{n}e}".replace("e-0", "e-").replace("e+0", "e+")


def mm(x):
    if x is None:
        return "—"
    v = x * 1e3
    return f"{v:.1f}" if abs(v) < 100 else f"{v:.0f}"


def ladder():
    s1, s2, s3, s4, s5, s6 = (rec(k) for k in ("S1", "S2", "S3", "S4", "S5", "S6"))
    print("| Шаг | Что проверяется | Эталон | Результат (ошибка) | Итог |")
    print("|---|---|---|---|---|")
    for b in s1["benchmarks"]:
        worst = []
        for v in b["vtkdiff_results"]:
            worst.append(f"{v['field']}: abs ≤ {e(max(v['abs_max_norm']))}, rel ≤ {e(max(v['rel_max_norm']))} "
                         f"(допуск {v['abs_tol']:g}/{v['rel_tol']:g})")
        print(f"| S1 | эталонная задача OGS `{b['name']}` ({b['description']}) | эталонный .vtu из поставки OGS, "
              f"`ogs -r` + vtkdiff | {'; '.join(worst)} | {'PASS' if b['pass'] else 'FAIL'} |")
    print(f"| S2 | один элемент QUAD8, плоская деформация, упругость | закон Гука (σzz = νσyy, εyy = (1−ν²)σ/E) | "
          f"макс. отн. ошибка {e(s2['worst_rel_err'])} | {'PASS' if s2['pass'] else 'FAIL'} |")
    c = s3["checks"]
    print(f"| S3 | слоистый столб 500 м под собственным весом (слои toy) | σv(z) = ∫ρg dz; σx = ν/(1−ν)·σv; осадка ∫σv/M dz | "
          f"σyy: {e(c['max_abs_err_sigma_yy_over_sigma_v_bottom'])}·σv(низ); σxx: "
          f"{e(c['max_abs_err_sigma_xx_vs_K0_over_sigma_v_bottom_(non-interface nodes)'])}·σv(низ); осадка "
          f"{c['surface_settlement_ogs_m']:.4f} м против {c['surface_settlement_analytic_m']:.4f} м | "
          f"{'PASS' if s3['pass'] else 'FAIL'} |")
    worst_u = max(cc["max_abs_u_over_gravity_settlement"] for cc in s4["cases"])
    print(f"| S4 | начальное напряжение σh = λσv как начальное условие; λ = 0,45 / 0,6 / 0,71 / 1,0 (столб) и 0,45 "
          f"(всё toy-сечение без камер) | равновесие: смещения 0, σ = σ0 | max|u| ≤ {e(worst_u)} от осадки S3; "
          f"σ − σ0 ≤ 1,3e-14·σv | {'PASS' if s4['pass'] else 'FAIL'} |")
    for v in s5["variants"]:
        pr = v["profiles"]
        print(f"| S5 | круглое отверстие a = 1 м в пластине 20a, σx = −5, σy = −10 МПа; вариант «{v['variant']}» | "
              f"решение Кирша | σθθ на контуре: {100 * pr['x_axis_theta0']['wall_sigma_tt_rel_err']:.2f} % (θ = 0°), "
              f"{100 * pr['y_axis_theta90']['wall_sigma_tt_rel_err']:.2f} % (90°); u_r на контуре: "
              f"{100 * v['worst_wall_u_r_err'] if 'worst_wall_u_r_err' in v else 100 * v['worst_wall_u_r_rel_err']:.1f} %; профиль σθθ до 5a: "
              f"≤ {100 * v['worst_profile_sigma_tt_err_over_p']:.1f} % от p | {'PASS' if v['pass'] else 'FAIL'} |")
    for cc in s6["cases"]:
        if "3D" in cc["case"]:
            print(f"| S6 | ползучесть одного элемента, 15 МПа, 10 лет: {cc['case']} | ε = σ/E + A·exp(−Q/RT)(σ/σ0)ⁿ·t | "
                  f"макс. отн. ошибка ε_zz {e(cc['max_rel_err_eps_zz'])} | {'PASS' if cc['pass'] else 'FAIL'} |")
        else:
            print(f"| S6 | то же, плоская деформация (переход σzz → σyy/2) | ОДУ одной точки (Radau) и "
                  f"установившаяся скорость (√3/2)ⁿ⁺¹A(σ/σ0)ⁿ | ε_yy(t): ≤ {100 * cc['max_rel_err_eps_yy_vs_ode']:.2f} %; "
                  f"скорость: {e(cc['steady_rate_rel_err'])} | {'PASS' if cc['pass'] else 'FAIL'} |")


def params():
    print("| Величина | Значение | Основание | Источник | Статус |")
    print("|---|---|---|---|---|")
    for r in T.parameter_table():
        print(f"| {r['name']} | {r['value']} | {r['basis']} | {r['source']} | {r['status']} |")


def s7():
    r = rec("S7")
    ts = r["time_series_base"]
    print("S7 time series:", json.dumps({k: [round(v, 5) for v in vals] for k, vals in ts.items()}))
    print("S7 checks:", json.dumps(r["checks"], indent=1))
    print("S7 numerical sensitivity:", json.dumps(r["numerical_sensitivity"], indent=1))
    print("S7 trough metrics 50 yr:", r["trough_metrics_base"].get("50yr"))
    print("S7 trough metrics 0+:", r["trough_metrics_base"].get("0+ (1 s)"))
    print("S7 runs:", {k: (v["run"]["exit_code"], v["run"]["wall_time_s"]) for k, v in r["runs"].items()})
    print("S7 mesh:", r["mesh_counts"])


def s8():
    r = rec("S8")
    order = ["IS-SC-B/0.6", "IS-SC-B/0.8", "IS-SC-A", "IS-SC-D", "IS-SC-E", "IS-P-21-0.71", "NORMATIVE-0.45",
             "IS-SC-A/OB0.7", "IS-SC-C"]
    bfru = {"none": "нет", "soft": "мягкая", "stiff": "жёсткая"}
    print("| Сценарий начального поля | Ползучесть | Закладка | s₀₊, мм | s(10 лет), мм | s(50 лет), мм | x50, м | "
          "перегиб, м | наклон max, мм/м | ε целика, % | фон (без выемки) 50 лет, мм | Статус |")
    print("|---|---|---|---|---|---|---|---|---|---|---|---|")
    rows = sorted(r["table"], key=lambda t: (order.index(t["scenario"]), t["creep_factor"],
                                              ["none", "soft", "stiff"].index(t["backfill"])))
    for t in rows:
        if t.get("status") not in ("OK", "OUTSIDE_VALIDITY"):
            print(f"| {t['scenario']} | ×{t['creep_factor']:g} | {bfru[t['backfill']]} | — | — | — | — | — | — | — | — | "
                  f"{t.get('status')} |")
            continue
        bg = t["background_s_max_series_m"][-1]
        up = t["background_uplift_max_series_m"][-1]
        bgs = f"{mm(bg)}" if bg > abs(up) else f"подъём {mm(up)}"
        print(f"| {t['scenario']} | ×{t['creep_factor']:g} | {bfru[t['backfill']]} | {mm(t['s_max_m@0+'])} | "
              f"{mm(t['s_max_m@10yr'])} | {mm(t['s_max_m@50yr'])} | {t['x50_m@50yr']:.0f} | "
              f"{t['x_inflection_m@50yr']:.0f} | {t['max_tilt_mm_per_m@50yr']:.2f} | "
              f"{100 * t['pillar1_strain@50yr']:.2f} | {bgs} | {t['status']} |")
    print()
    sens = r["sensitivity_ensemble"]
    print("| Отклик | Начальное поле (IS-SC) | Ползучесть | Закладка | Ранжирование |")
    print("|---|---|---|---|---|")
    for k, v in sens.items():
        if not isinstance(v, dict) or "effects" not in v:
            continue
        ef = v["effects"]
        print(f"| {k} | {100 * ef['scenario']['share_of_total_SS']:.0f} % | {100 * ef['creep_factor']['share_of_total_SS']:.0f} % | "
              f"{100 * ef['backfill']['share_of_total_SS']:.0f} % | {' > '.join(v['ranking'])} |")
    print()
    for k in ("s_max_m@50yr (log10)", "s_max_m@10yr (log10)", "s_max_m@0+ (log10)"):
        if k in sens:
            print(k, json.dumps({f: {lv: round(10 ** m * 1e3, 2) for lv, m in d["level_means"].items()}
                                 for f, d in sens[k]["effects"].items()}, ensure_ascii=False))
    print("runs:", r["n_runs"])


if __name__ == "__main__":
    import sys

    todo = sys.argv[1:] or ["ladder", "params", "S7", "S8"]
    for t in todo:
        {"ladder": ladder, "params": params, "S7": s7, "S8": s8}[t]()
        print()
