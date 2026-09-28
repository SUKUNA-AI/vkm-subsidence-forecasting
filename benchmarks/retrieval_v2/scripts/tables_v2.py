"""Markdown tables for ``RESULTS_V2.md`` from ``$V2_WORK/out/results_v2_full.json`` and ``serving_v2.json`` (IDs and
numbers only; prints to stdout)."""
from __future__ import annotations

import json
import os
from pathlib import Path

WORK = Path(os.environ["V2_WORK"])
REPO = Path(__file__).resolve().parents[3]
CFG = json.loads((REPO / "benchmarks/retrieval_v2/configs/models_v2.json").read_text(encoding="utf-8"))
BASE = "E:nano-rx580"
LIC = {"CC-BY-NC-4.0": "CC BY-NC 4.0", "Apache-2.0": "Apache-2.0", "MIT": "MIT"}


def f3(x):
    return "—" if x is None or x != x else f"{x:.3f}"


def f5(x):
    return "—" if x is None or x != x else f"{x:.5f}"


def fp(p):
    if p is None:
        return "—"
    if isinstance(p, float):
        return "<0.001" if p < 0.001 else f"{p:.3f}"
    return str(p)


def dl(d):
    return "—" if d is None else f"{d:+.3f}"


def sys_row(res, label, track, name):
    return (res["sets"][label][track]["systems"].get(name) or {})


def pair(res, label, track, a, b=BASE, m="ndcg@10"):
    return ((res["sets"][label][track]["pairs"].get(f"{a} ~ {b}") or {}).get(m)) or {}


def main() -> None:
    res = json.load(open(WORK / "out" / "results_v2_full.json", encoding="utf-8"))
    serv = {}
    if (WORK / "out" / "serving_v2.json").is_file():
        serv = json.load(open(WORK / "out" / "serving_v2.json", encoding="utf-8"))["feasibility"]
    dec = res["decision"]
    models = res["models"]
    keys = ["nano-rx580"] + [m["key"] for m in CFG["text"]]
    lic = {m["key"]: m["license"] for m in CFG["text"] + CFG["visual"]}
    lic["nano-rx580"] = "CC-BY-NC-4.0"

    print("### Текстовый трек: E:m (m в dense-канале схемы сервиса) и B:m (только dense)\n")
    print("| Модель | Лицензия | E nDCG@10 V | Δ к E (V), p, p Холма | E nDCG@10 P | Δ к E (P), p | E R@50 V / P | "
          "B nDCG@10 V / P | неоценено в top-10 E: V / P | ед./с на 5070 Ti |")
    print("|---|---|---|---|---|---|---|---|---|---|")
    for k in keys:
        e = f"E:{k}"
        v, p = sys_row(res, "verified", "text", e), sys_row(res, "verified+pooled", "text", e)
        if "overall" not in v:
            print(f"| {k} | {LIC.get(lic.get(k), lic.get(k))} | NOT_RUN | | | | | | | |")
            continue
        bv, bp = sys_row(res, "verified", "text", f"B:{k}"), sys_row(res, "verified+pooled", "text", f"B:{k}")
        dv, dp = pair(res, "verified", "text", e), pair(res, "verified+pooled", "text", e)
        hp = (dec["dense"].get(k) or {}).get("holm_p")
        name = "nano (развёрнута, Q8_0 RX580) = E" if k == "nano-rx580" else k
        ups = (models.get(k) or {}).get("units_per_s")
        bstat = " (PARTIAL_POOL)" if bp.get("status") == "PARTIAL_POOL" else ""
        print(f"| {name} | {LIC.get(lic.get(k), lic.get(k))} | {f3(v['overall']['ndcg@10'])} | "
              + (f"{dl(dv.get('delta'))}, {fp(dv.get('p_value'))}, {fp(hp) if hp is not None else '—'}" if dv else "—")
              + f" | {f3(p['overall']['ndcg@10'])} | "
              + (f"{dl(dp.get('delta'))}, {fp(dp.get('p_value'))}" if dp else "—")
              + f" | {f3(v['overall']['recall@50'])} / {f3(p['overall']['recall@50'])}"
              + f" | {f3(bv.get('overall', {}).get('ndcg@10'))} / {f3(bp.get('overall', {}).get('ndcg@10'))}{bstat}"
              + f" | {1 - v['overall']['judged@10']:.2f} / {1 - p['overall']['judged@10']:.2f}"
              + f" | {ups if ups else '—'} |")
    print("\n### Визуальный трек: E:m\n")
    print("| Модель | E nDCG@10 V | Δ (V), p | E nDCG@10 P | Δ (P), p | B nDCG@10 V / P |")
    print("|---|---|---|---|---|---|")
    for k in keys:
        e = f"E:{k}"
        v, p = sys_row(res, "verified", "visual", e), sys_row(res, "verified+pooled", "visual", e)
        if "overall" not in v:
            continue
        dv, dp = pair(res, "verified", "visual", e), pair(res, "verified+pooled", "visual", e)
        bv, bp = sys_row(res, "verified", "visual", f"B:{k}"), sys_row(res, "verified+pooled", "visual", f"B:{k}")
        print(f"| {k} | {f3(v['overall']['ndcg@10'])} | " + (f"{dl(dv.get('delta'))}, {fp(dv.get('p_value'))}" if dv
                                                              else "—")
              + f" | {f3(p['overall']['ndcg@10'])} | " + (f"{dl(dp.get('delta'))}, {fp(dp.get('p_value'))}" if dp
                                                           else "—")
              + f" | {f3(bv.get('overall', {}).get('ndcg@10'))} / {f3(bp.get('overall', {}).get('ndcg@10'))} |")
    print("\n### Визуальный канал (изображения страниц)\n")
    print("| Система | трек | nDCG@10 V | Δ к E (V), p | nDCG@10 P | Δ к E (P), p | R@50 V / P | неоценено top-10 V / P |")
    print("|---|---|---|---|---|---|---|---|")
    for m in CFG["visual"]:
        k = m["key"]
        for kind in ("VIS", "E+VIS", "E3"):
            name = f"{kind}:{k}"
            for track in ("visual", "text"):
                v, p = sys_row(res, "verified", track, name), sys_row(res, "verified+pooled", track, name)
                if "overall" not in v:
                    continue
                dv, dp = pair(res, "verified", track, name), pair(res, "verified+pooled", track, name)
                st = " (PARTIAL_POOL)" if p.get("status") == "PARTIAL_POOL" else ""
                print(f"| {name} | {track} | {f3(v['overall']['ndcg@10'])} | "
                      + (f"{dl(dv.get('delta'))}, {fp(dv.get('p_value'))}" if dv else "—")
                      + f" | {f3(p['overall']['ndcg@10'])}{st} | "
                      + (f"{dl(dp.get('delta'))}, {fp(dp.get('p_value'))}" if dp else "—")
                      + f" | {f3(v['overall']['recall@50'])} / {f3(p['overall']['recall@50'])}"
                      + f" | {1 - v['overall']['judged@10']:.2f} / {1 - p['overall']['judged@10']:.2f} |")
    print("\n### Правило решения (PREREGISTRATION §6)\n")
    print("| Кандидат | Лицензия | 1. V текст (Холм) | 2. P текст ≥ 0 | 3. визуальный трек | 4. обслуживание | итог |")
    print("|---|---|---|---|---|---|---|")
    for k, d in dec["dense"].items():
        c = d["criteria"]
        yn = lambda x: "да" if x else ("нет" if x is not None else "—")  # noqa: E731
        print(f"| {k} | {LIC.get(d['license'], d['license'])} | {yn(c['a_sig_V_text'])} ({dl(d['delta_V_text'])}, "
              f"p Холма {fp(d['holm_p'])}) | {yn(c['b_P_text_not_negative'])} ({dl(d['delta_P_text'])}) | "
              f"{yn(c['c_visual_guard'])} | {yn(c['d_serving'])} | "
              f"{'РЕКОМЕНДОВАНА' if d['recommended_candidate'] else 'нет'} |")
    for k, d in dec["visual"].items():
        print(f"| E+VIS:{k} | {LIC.get(d['license'], d['license'])} | визуальный V: {dl(d['delta_V_visual'])}, "
              f"p Холма {fp(d['holm_p'])} | P визуальный {dl(d['delta_P_visual'])} | текст P {dl(d['delta_P_text'])} | "
              f"{d['serving']} | {'РЕКОМЕНДОВАН (' + d['scope'] + ')' if d['recommended'] else 'нет'} |")
    print(f"\nВыбор dense: {dec['dense_choice']}; визуальный канал: {dec['visual_choice']}\n")
    if serv:
        print("### Обслуживание\n")
        print("| Модель | GGUF Q8_0 / гейт K | VRAM RX580, MiB | запрос p50 / p95, мс | cos Q8_0↔bf16 (ср. / мин) | "
              "Δ nDCG@10 E~q8 − E (V / P) | перекодирование 205 457 ед., мин |")
        print("|---|---|---|---|---|---|---|")
        for k, f in serv.items():
            q = f.get("q8_delta_ndcg10") or {}
            print(f"| {k} | {f.get('gate', f.get('text_tower', '—'))} | {f.get('rx580_vram_mib', '—')} | "
                  f"{f.get('rx580_query_ms_p50', '—')} / {f.get('rx580_query_ms_p95', '—')} | "
                  f"{f5(f.get('q8_cos_mean'))} / {f5(f.get('q8_cos_min'))} | "
                  f"{dl(q.get('verified'))} / {dl(q.get('verified+pooled'))} | "
                  f"{f.get('reencode_current_snapshot_min_5070ti', f.get('reencode_pages_min_5070ti', '—'))} |")
    print("\n### Пропускная способность (RTX 5070 Ti, bf16)\n")
    print("| Модель | единиц | с | ед./с | токенов/с | стр./с | пик VRAM, MiB |")
    print("|---|---|---|---|---|---|---|")
    for k, m in models.items():
        print(f"| {k} | {m.get('n_units', m.get('n_pages', '—'))} | {m.get('encode_s', '—')} | "
              f"{m.get('units_per_s', '—')} | {m.get('tokens_per_s', '—')} | {m.get('pages_per_s', '—')} | "
              f"{m.get('peak_vram_mib', '—')} |")
    print("\n### Пул LLM_AGENT_V2\n")
    print(json.dumps(res.get("pool", {}), ensure_ascii=False))
    print(json.dumps(res.get("pooled_labels", {})))


if __name__ == "__main__":
    main()
