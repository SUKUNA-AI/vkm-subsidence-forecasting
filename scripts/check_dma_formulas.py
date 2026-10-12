#!/usr/bin/env python3
"""Field dictionary and formula check of the DMA table of SKRU-1 (dataset ``dma-skru1-losev``, 12.10.2026).

The table ``ДМА_Лосев.csv`` holds normalised indicators of polygonal zones (no units, no zone ids). This script
re-derives every relation between its columns from the numbers alone and keeps two kinds of result apart:
REPRODUCED_NUMERICALLY (the identity holds for every row within the print precision) and the status of the
definition in the sources (STATED in a named source, INFERRED, UNKNOWN). A relation reproduced by the numbers is not
thereby the authors' method; a value lost to clipping or normalisation is not restored.

Relations checked (L = exel_Load, R = the 8 TRI / Canny / SRTM140 / SRTM245 mean / range columns, D500 / D1000 = the
three Disp_*_500 / Disp_*_1000 columns):
  Rel_mera = (L + R) / 9;  Disp500_mera = D500 / 3;  Disp1000_mera = D1000 / 3;
  Disp500_Rel_mera = (L + R + D500) / 12;  Disp1000_Rel_mera = (R + D1000) / 11  (no load; the variant with the load
  is checked as an alternative and kept as such);  Disp_s_500 = max(1/51, (101·Disp_s_1000 − 50) / 51) for s = mean,
  min, max, i.e. φ_500 = min(φ_1000, 500) with φ_N = (N + 10)(1 − Disp_s_N).

Input: the original CSV (path as an argument, read as bytes; SHA-256 recorded). Output (``--out``, a new directory):
``formula_checks.csv``, ``field_dictionary.csv``, ``implied_values_summary.json``, ``receipt.json``. The original is
never written.

Usage:  python scripts/check_dma_formulas.py <ДМА_Лосев.csv> --out <new dir>
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
import sys
from pathlib import Path

RULE = "dma_formula_check_v1"
REL = ("TRI_mean", "TRI_range", "Canny_mean", "Canny_range", "SRTM140_mean", "SRTM140_range", "SRTM245_mean",
       "SRTM245_range")
STATS = ("mean", "min", "max")
TOL = 5e-9          # values are printed with 9 significant digits


def load(path: Path) -> tuple[list[str], list[dict], bytes]:
    raw = path.read_bytes()
    text = raw.decode("utf-8-sig")
    rows = list(csv.reader(io.StringIO(text, newline=""), delimiter=";"))
    hdr = rows[0]
    out = [{h: float(v) for h, v in zip(hdr, r)} for r in rows[1:]]
    return hdr, out, raw


def parts(r: dict) -> tuple[float, float, float, float]:
    L = r["exel_Load"]
    R = sum(r[c] for c in REL)
    D5 = sum(r[f"Disp_{s}_500"] for s in STATS)
    D10 = sum(r[f"Disp_{s}_1000"] for s in STATS)
    return L, R, D5, D10


CHECKS = [
    ("Rel_mera", "(L + R) / 9", lambda L, R, D5, D10, r: (L + R) / 9),
    ("Disp500_mera", "D500 / 3", lambda L, R, D5, D10, r: D5 / 3),
    ("Disp1000_mera", "D1000 / 3", lambda L, R, D5, D10, r: D10 / 3),
    ("Disp500_Rel_mera", "(L + R + D500) / 12", lambda L, R, D5, D10, r: (L + R + D5) / 12),
    ("Disp1000_Rel_mera", "(R + D1000) / 11", lambda L, R, D5, D10, r: (R + D10) / 11),
    ("Disp1000_Rel_mera", "ALTERNATIVE (L + R + D1000) / 12", lambda L, R, D5, D10, r: (L + R + D10) / 12),
    ("Disp500_Rel_mera", "ALTERNATIVE (R + D500) / 11", lambda L, R, D5, D10, r: (R + D5) / 11),
] + [(f"Disp_{s}_500", f"max(1/51, (101·Disp_{s}_1000 − 50) / 51)",
      (lambda s: lambda L, R, D5, D10, r: max(1 / 51, (101 * r[f"Disp_{s}_1000"] - 50) / 51))(s)) for s in STATS]


def check(rows: list[dict]) -> list[dict]:
    out = []
    for target, formula, f in CHECKS:
        res = [abs(r[target] - f(*parts(r), r)) for r in rows]
        n_bad = sum(e > TOL for e in res)
        out.append({"target": target, "formula": formula, "n_rows": len(rows), "max_abs_residual": f"{max(res):.3e}",
                    "n_rows_over_tolerance": n_bad, "tolerance": f"{TOL:g}",
                    "result": "REPRODUCED_NUMERICALLY" if n_bad == 0 else "NOT_REPRODUCED"})
    return out


def implied(rows: list[dict]) -> dict:
    """Values implied by the reproduced relations (INFERRED): φ_N = (N + 10)(1 − Disp_s_N), clipped at N."""
    out = {}
    for n in (500, 1000):
        for s in STATS:
            phi = [(n + 10) * (1 - r[f"Disp_{s}_{n}"]) for r in rows]
            out[f"phi_{s}_{n}"] = {"min": round(min(phi), 3), "max": round(max(phi), 3),
                                   "share_at_clip": round(sum(p >= n - 1e-6 for p in phi) / len(phi), 4)}
    load = [r["exel_Load"] for r in rows]
    out["exel_Load"] = {"min": min(load), "max": max(load), "n_distinct": len(set(load))}
    return out


# field → (meaning, unit, source and status of the definition, transformation, depends on / used by)
DICTIONARY = [
    ("xcoord", "долгота центроида полигонального участка", "градус (WGS 84, по объявлению CRS84 в GeoJSON)",
     "STATED: ВКР Филатовой (VKM-SRC-023) r0041 — таблица Centoids_zone_pol (xcoord, ycoord); привязка плана к OSM по "
     "контрольным точкам, r0040 — точность UNKNOWN", "центроид полигона (проверено: медиана расхождения 4·10⁻⁹°)",
     "ключ связи строки CSV с контуром GeoJSON"),
    ("ycoord", "широта центроида полигонального участка", "градус (WGS 84)", "как xcoord", "как xcoord",
     "ключ связи"),
    ("exel_Load", "горнотехнический коэффициент нагрузки (нормированный)", "безразмерный, 0…1",
     "STATED: ВКР табл. 1.1 r0052–r0055 (поле 8); RJES VKM-SRC-152 с. 7; статья VKM-SRC-197 PDF 6 / печ. 8 — "
     "«горнотехнический коэффициент нагрузки». Нормировка: INFERRED 1 − C/0,88 (C — степень нагружения, рис. 17 ВКР; "
     "сходится в 6 точках, субагент 12.10) — методикой не подтверждена",
     "нормировка (min–max?) — UNKNOWN; исходная величина C в таблице отсутствует",
     "Rel_mera, Disp500_Rel_mera; НЕ входит в Disp1000_Rel_mera"),
    ("TRI_mean", "индекс неровности рельефа (Terrain Ruggedness Index), среднее по участку", "безразмерный 0…1 "
     "(исходная единица и окно — UNKNOWN)", "STATED: ВКР табл. 1.1 (поле 12); RJES с. 5, 7; статья PDF 6. ЦМР, окно, "
     "формула TRI и нормировка — UNKNOWN", "зональная статистика (среднее) по полигону, затем нормировка — UNKNOWN",
     "R → Rel_mera, Disp*_Rel_mera"),
    ("TRI_range", "TRI, размах по участку", "безразмерный 0…1", "как TRI_mean (поле 13)", "размах, нормировка UNKNOWN",
     "R"),
    ("Canny_mean", "средний «градиент рельефа» (по ВКР); в RJES — линии рельефа по методам Канни и Хафа и их "
     "линейные плотности", "безразмерный 0…1", "STATED-ВКР табл. 1.1 (поле 14: средний градиент рельефа); RJES с. 5 "
     "(Canny/Hough); статья PDF 6 — «градиент рельефа». Расхождение определений ВКР и RJES — не разрешено",
     "среднее по полигону, нормировка UNKNOWN", "R"),
    ("Canny_range", "размах «градиента рельефа»", "безразмерный 0…1", "как Canny_mean (поле 15)", "размах", "R"),
    ("SRTM140_mean", "мера рельефа по косинусному преобразованию (μL1Re/μL2Re?), среднее", "безразмерный 0…1",
     "STATED: ВКР табл. 1.1 (поля 16–19: меры рельефа по косинусному преобразованию); RJES с. 7 (μL1Re, μL2Re). "
     "Смысл «140» и «245» и ЦМР (SRTM, разрешение) — UNKNOWN; соответствие 140/245 ↔ L1/L2 — не установлено",
     "среднее по полигону", "R"),
    ("SRTM140_range", "та же мера, размах", "безразмерный 0…1", "как SRTM140_mean", "размах", "R"),
    ("SRTM245_mean", "мера рельефа по косинусному преобразованию, среднее (параметр 245)", "безразмерный 0…1",
     "как SRTM140_mean", "среднее", "R"),
    ("SRTM245_range", "та же мера, размах", "безразмерный 0…1", "как SRTM140_mean", "размах", "R"),
] + [(f"Disp_{s}_{n}", f"оседание в пределах участка ({ {'mean': 'среднее', 'min': 'минимум', 'max': 'максимум'}[s] }), "
      f"нормированное с отсечкой {n} мм", "безразмерный; 1 − min(S, N)/(N + 10), S — мм (INFERRED)",
      "STATED: ВКР табл. 1.1 (поля 20–22 Disp_mean/min/max — оседание в пределах участка), r0045 — изолинии "
      "интерполированы в растр, затем зональная статистика; статья PDF 6. Суффикс 500/1000, отсечка и «+10» — "
      "UNKNOWN в источниках; соотношение воспроизведено по числам (INFERRED). Поле оседаний — DERIVATION от "
      "интерполированного растра (MON-0470), эпоха (2022 — по имени файла) и метод наблюдения UNKNOWN",
      f"S → 1 − min(S, {n})/{n + 10}; значения выше {n} мм не восстанавливаются (отсечка)",
      ("Disp500_mera, Disp500_Rel_mera" if n == 500 else "Disp1000_mera, Disp1000_Rel_mera")
      + (f"; = max(1/51, (101·Disp_{s}_1000 − 50)/51)" if n == 500 else ""))
    for n in (500, 1000) for s in STATS] + [
    ("Rel_mera", "мера ДМА «рельеф + нагрузка»", "безразмерный 0…1", "Определение «меры» (среднее нормированных "
     "показателей, SF = 1 − μ): RJES с. 4, ВКР r0056–r0058 (STATED); состав (L + 8 показателей рельефа) не совпадает "
     "ни с одним вариантом ВКР (12–19, 12–22) или RJES (1–19, 1–22)", "(L + R)/9 — REPRODUCED_NUMERICALLY", "—"),
    ("Disp500_mera", "мера оседаний (отсечка 500)", "безразмерный 0…1", "в источниках не названа (UNKNOWN)",
     "D500/3 — REPRODUCED_NUMERICALLY", "—"),
    ("Disp1000_mera", "мера оседаний (отсечка 1000)", "безразмерный 0…1", "в источниках не названа (UNKNOWN)",
     "D1000/3 — REPRODUCED_NUMERICALLY", "—"),
    ("Disp500_Rel_mera", "мера «рельеф + нагрузка + оседания 500»", "безразмерный 0…1", "UNKNOWN",
     "(L + R + D500)/12 — REPRODUCED_NUMERICALLY", "—"),
    ("Disp1000_Rel_mera", "мера «рельеф + оседания 1000» — без нагрузки", "безразмерный 0…1",
     "по составу (R + D1000) — вариант ВКР «признаки 12–22» (r0066–r0067; INFERRED). Отсутствие нагрузки "
     "несимметрично с Disp500_Rel_mera; авторская причина UNKNOWN; исходный вариант сохранён, исправление — только "
     "отдельной версией с обоснованием", "(R + D1000)/11 — REPRODUCED_NUMERICALLY; вариант с нагрузкой не сходится",
     "—"),
]
DICT_COLS = ["field", "meaning", "unit", "definition_source_and_status", "transformation", "dependents"]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("csv", type=Path)
    ap.add_argument("--out", type=Path, required=True)
    a = ap.parse_args()
    hdr, rows, raw = load(a.csv)
    expected = ["xcoord", "ycoord", "exel_Load", *REL, *(f"Disp_{s}_{n}" for n in (500, 1000) for s in STATS),
                "Rel_mera", "Disp500_mera", "Disp1000_mera", "Disp500_Rel_mera", "Disp1000_Rel_mera"]
    if hdr != expected:
        print(json.dumps({"error": "unexpected header", "header": hdr}, ensure_ascii=False))
        return 1
    if [d[0] for d in DICTIONARY] != expected:
        raise SystemExit("dictionary does not cover the header")
    a.out.mkdir(parents=True, exist_ok=False)
    files = {}

    def table(rows_, cols):
        buf = io.StringIO()
        w = csv.DictWriter(buf, fieldnames=cols, lineterminator="\n")
        w.writeheader()
        w.writerows(rows_)
        return buf.getvalue().encode("utf-8")

    checks = check(rows)
    files["formula_checks.csv"] = table(checks, list(checks[0]))
    files["field_dictionary.csv"] = table([dict(zip(DICT_COLS, d)) for d in DICTIONARY], DICT_COLS)
    files["implied_values_summary.json"] = (json.dumps(implied(rows), ensure_ascii=False, indent=1) + "\n").encode()
    receipt = {"rule": RULE, "input": {"file": a.csv.name, "sha256": hashlib.sha256(raw).hexdigest(),
                                       "size_bytes": len(raw), "rows": len(rows)},
               "code_sha256": hashlib.sha256(Path(__file__).read_bytes().replace(b"\r\n", b"\n")).hexdigest(),
               "outputs": {k: hashlib.sha256(v).hexdigest() for k, v in files.items()},
               "summary": {c["target"] + " = " + c["formula"]: c["result"] for c in checks}}
    files["receipt.json"] = (json.dumps(receipt, ensure_ascii=False, indent=1) + "\n").encode()
    for k, v in files.items():
        (a.out / k).write_bytes(v)
    print(json.dumps(receipt["summary"], ensure_ascii=False, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
