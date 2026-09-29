"""FIGURE_READINGS_V1 — the reviewer's page REVIEW_RU.md (local work dir only; it holds the extracted values).

    python make_review.py --work <dir> [--notes review_notes.json]

Order: summary (from the notes file, written by the reading agent after looking at the results), the thesis tables
(image link, Qwen transcription, GLM-OCR transcription, every disagreement, mine as printed), thesis maps and
sections, the rest of the thesis, the user's screenshots, corpus figures, the user's new files, what was not read.
Everything shown is AUTO_EXTRACTED_UNREVIEWED / VLM_EXTRACTED.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

TYPE_RU = {"TABLE_SCREENSHOT": "таблица (скриншот)", "MAP": "карта / план", "GEOLOGICAL_SECTION": "геологический разрез",
           "CHART": "график", "SCHEME_DRAWING": "схема / чертёж", "PHOTO": "фото", "OTHER": "прочее", None: "не определён"}


def esc(s) -> str:
    s = "" if s is None else str(s)
    return s.replace("|", "\\|").replace("\n", " ").strip()


def md_table(grid: list[list[str]], mark: dict[tuple[int, int], str] | None = None, header_rows: int = 1) -> str:
    if not grid:
        return "_(пусто)_\n"
    n = max(len(r) for r in grid)
    lines = []
    for i, r in enumerate(grid):
        cells = []
        for c in range(n):
            v = esc(r[c] if c < len(r) else "")
            if mark and (i, c) in mark:
                v = f"**{v or '∅'}** ≠ {esc(mark[(i, c)]) or '∅'}"
            cells.append(v)
        lines.append("| " + " | ".join(cells) + " |")
        if i == 0:
            lines.append("|" + "---|" * n)
    return "\n".join(lines) + "\n"


def mine_line(r) -> str:
    return (f"- Рудник, как напечатано: обозначения рудников в прочитанном тексте — "
            f"{listing(r.get('mine_designations_in_reading'))}; поле «рудник» ответа модели — "
            f"{listing(r.get('mine_as_printed_by_reader'))}; в подписи — {listing(r.get('mine_in_caption_as_printed'))}")


def qwen_reader(rec):
    return next((r for r in rec["readers"] if r["reader"] == "qwen"), None)


def glm_reader(rec):
    return next((r for r in rec["readers"] if r["reader"] == "glm-ocr"), None)


def listing(values, limit=60) -> str:
    vals = [esc(v) for v in (values or []) if esc(v)]
    if not vals:
        return "—"
    more = f" … (+{len(vals) - limit})" if len(vals) > limit else ""
    return "; ".join(vals[:limit]) + more


def describe(rec, suppress_text: bool = False) -> list[str]:
    q = qwen_reader(rec)
    out = []
    if not q:
        return ["_нет ответа модели_"]
    if suppress_text:
        p = q.get("parsed") or {}
        return [f"- Тип: {TYPE_RU.get(q.get('task'))}; статус чтения: {q.get('reading_status')}",
                f"- Описание модели: {esc(p.get('what') or p.get('description') or '')}",
                "- Текст документа в обзор не переносится (конфиденциальный файл пользователя, в нём есть фамилии и "
                "подписи); полное чтение — в `parsed/` и `raw/` этой папки."]
    if q.get("reading_status") != "PARSED":
        out.append(f"_Чтение не удалось: {q.get('reading_status')} ({esc(q.get('parse_note'))})._")
        return out
    p = q["parsed"] or {}
    t = q.get("task")
    if p.get("title"):
        out.append(f"- Заголовок на рисунке: {esc(p.get('title'))}")
    if t in ("MAP", "GEOLOGICAL_SECTION"):
        out.append(f"- Территория / объекты, как напечатано: {listing(p.get('area_as_printed'))}")
    if t == "MAP":
        leg = p.get("legend") or []
        if leg:
            out.append("- Легенда: " + "; ".join(f"{esc(l.get('symbol'))} → {esc(l.get('label'))}" for l in leg
                                                 if isinstance(l, dict)))
        cb = p.get("colour_bar") or {}
        if isinstance(cb, dict) and cb.get("present"):
            out.append(f"- Цветовая шкала: {esc(cb.get('min'))} … {esc(cb.get('max'))} {esc(cb.get('unit'))}; "
                       f"деления: {listing(cb.get('ticks'))}; подпись шкалы: {esc(cb.get('title')) or '—'}")
        out.append(f"- Масштаб: {esc(p.get('scale_bar')) or '—'}; север: {esc(p.get('north_arrow'))}")
        g = p.get("grid_labels") or {}
        if isinstance(g, dict):
            out.append(f"- Сетка координат ({esc(g.get('kind'))}): X — {listing(g.get('x'))}; Y — {listing(g.get('y'))}")
        feats = p.get("features") or []
        if feats:
            out.append("- Подписанные объекты: " + "; ".join(
                f"{esc(f.get('label'))}{(' — ' + esc(f.get('what'))) if f.get('what') else ''}" for f in feats
                if isinstance(f, dict))[:1500])
    if t == "GEOLOGICAL_SECTION":
        out.append(f"- Стратиграфические подписи: {listing(p.get('stratigraphic_labels'))}")
        out.append(f"- Глубины / отметки ({esc(p.get('depth_unit'))}): {listing(p.get('depth_ticks'))}")
        out.append(f"- Скважины: {listing(p.get('borehole_labels'))}")
        leg = p.get("legend") or []
        if leg:
            out.append("- Легенда: " + "; ".join(f"{esc(l.get('symbol'))} → {esc(l.get('label'))}" for l in leg
                                                 if isinstance(l, dict)))
        out.append(f"- Масштабы: гориз. {esc(p.get('horizontal_scale')) or '—'}, верт. {esc(p.get('vertical_scale')) or '—'}")
    if t == "CHART":
        for pn in p.get("panels") or []:
            if not isinstance(pn, dict):
                continue
            x, y = pn.get("x_axis") or {}, pn.get("y_axis") or {}
            out.append(f"- Панель {esc(pn.get('panel')) or '—'} ({esc(pn.get('chart_type'))}): "
                       f"X «{esc(x.get('title'))}» [{esc(x.get('unit'))}] {listing(x.get('ticks'), 25)}; "
                       f"Y «{esc(y.get('title'))}» [{esc(y.get('unit'))}] {listing(y.get('ticks'), 25)}; "
                       f"легенда: {listing(pn.get('legend'), 20)}; серии: {listing(pn.get('series_names'), 20)}; "
                       f"значения-подписи: {listing(pn.get('printed_value_labels'), 40)}")
    if t == "SCHEME_DRAWING":
        out.append(f"- Надписи: {listing(p.get('labels'), 80)}")
        dims = p.get("dimensions") or []
        if dims:
            out.append("- Размеры: " + "; ".join(f"{esc(d.get('text'))} {esc(d.get('unit'))}".strip() for d in dims
                                                 if isinstance(d, dict)))
        if p.get("title_block"):
            out.append(f"- Штамп: {listing(p.get('title_block'))}")
    if t in ("PHOTO", "OTHER"):
        out.append(f"- Текст на изображении: {listing(p.get('printed_text'))}")
    other = p.get("other_text")
    if other:
        out.append(f"- Прочий текст: {listing(other, 40)}")
    desc = p.get("description") or p.get("what")
    if desc:
        out.append(f"- Описание модели: {esc(desc)}")
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--work", required=True)
    ap.add_argument("--notes", default="")
    a = ap.parse_args()
    work = Path(a.work)
    recs = [json.loads(l) for l in (work / "readings.jsonl").read_text(encoding="utf-8").splitlines() if l.strip()]
    notes = json.loads(Path(a.notes).read_text(encoding="utf-8")) if a.notes else {}
    remarks = notes.get("remarks", {})
    L = ["# Чтение сложных рисунков: ВКР Филатовой, корпус и новые файлы (29.09.2026)", ""]
    L += ["Все записи ниже — **машинное чтение** (модель Qwen3.5-9B; таблицы — ещё и GLM-OCR). Статус каждой записи "
          "`AUTO_EXTRACTED_UNREVIEWED`, происхождение `VLM_EXTRACTED`. Это не проверенные данные и не наблюдения: "
          "каждое число нужно сверить с картинкой. UNREADABLE и пустые места оставлены как есть. Рудник указан только "
          "так, как он напечатан на рисунке или в подписи.", ""]
    if notes.get("summary"):
        L += ["## Коротко", ""] + notes["summary"] + [""]
    F = [r for r in recs if r["key"].startswith("F023_")]
    tables = [r for r in F if (qwen_reader(r) or {}).get("task") == "TABLE_SCREENSHOT"]
    L += ["## 1. Таблицы ВКР Филатовой (VKM-SRC-023)", ""]
    for r in tables:
        q, g = qwen_reader(r), glm_reader(r)
        cap = esc(r.get("caption_as_printed")) or "подпись не найдена"
        L += [f"### {r['key']} — {cap}", ""]
        L += [f"Файл: `{r['locator'].get('media_part')}`; картинка: [{r['key']}](sent/{r['key']}.png)", ""]
        L += [f"![{r['key']}](sent/{r['key']}.png)", ""]
        L += [mine_line(r)]
        if q and q.get("reading_status") == "PARSED" and q.get("table"):
            t = q["table"]
            L += [f"- Qwen: {t['n_data_rows']} строк × {t['n_columns']} столбцов, строк заголовка {t['n_header_rows']}, "
                  f"UNREADABLE: {t['unreadable_cells']}"]
        elif q:
            L += [f"- Qwen: чтение не удалось ({q.get('reading_status')})"]
        cmpd = r.get("comparison")
        if g and cmpd:
            kinds = cmpd.get("disagreement_kinds", {})
            L += [f"- GLM-OCR: {g['table']['n_rows']} строк (с заголовком) × {g['table']['n_columns']} столбцов",
                  (f"- **Совпадение Qwen–GLM по ячейкам (строго): {cmpd['agreement']:.1%}** "
                   f"({cmpd['cells_agree']} из {cmpd['cells_compared']}); без учёта похожих букв, пробелов и "
                   f"десятичного разделителя: {cmpd['agreement_tolerant']:.1%}; расхождений: {len(cmpd['disagreements'])} "
                   f"(только похожие буквы: {kinds.get('lookalike_letters_only', 0)}, только запятая/точка: "
                   f"{kinds.get('decimal_separator_only', 0)}, только пробелы: {kinds.get('spacing_only', 0)}, "
                   f"ячейка есть у одного чтеца: {kinds.get('missing_in_one_reader', 0)}, "
                   f"разное содержание: {kinds.get('different', 0)})")
                  if cmpd.get("agreement") is not None else "- Совпадение: нет сопоставимых ячеек"]
        elif g is None:
            L += ["- GLM-OCR: не читал"]
        if r["key"] in remarks:
            L += [f"- Замечание проверяющего агента (не исправление записи): {remarks[r['key']]}"]
        L += [""]
        if q and q.get("reading_status") == "PARSED":
            import csv
            qgrid = list(csv.reader((work / q["table"]["csv"]).read_text(encoding="utf-8-sig").splitlines())) \
                if q.get("table") else []
            marks = {}
            if cmpd:
                for d in cmpd["disagreements"]:
                    if d["qwen_row"] is not None and d["qwen"] is not None:
                        marks[(d["qwen_row"], d["col"])] = d["glm"] if d["glm"] is not None else "(нет у GLM)"
            L += ["**Qwen** (жирным — ячейки, где GLM прочитал иначе; после ≠ — чтение GLM):", "", md_table(qgrid, marks)]
        if g:
            L += ["<details><summary>Чтение GLM-OCR целиком</summary>", "", md_table(g["parsed"]["grid"]), "</details>", ""]
        if cmpd and cmpd["disagreements"]:
            KR = {"lookalike_letters_only": "похожие буквы", "decimal_separator_only": "запятая/точка",
                  "spacing_only": "пробелы", "missing_in_one_reader": "есть у одного", "different": "разное"}
            L += ["<details><summary>Все расхождения списком</summary>", "",
                  "| строка Qwen | строка GLM | столбец | Qwen | GLM | вид |", "|---|---|---|---|---|---|"]
            for d in cmpd["disagreements"]:
                L += [f"| {d['qwen_row']} | {d['glm_row']} | {d['col']} | {esc(d['qwen']) or '∅'} | "
                      f"{esc(d['glm']) or '∅'} | {KR.get(d.get('kind'), d.get('kind'))} |"]
            L += ["", "</details>", ""]
    maps = [r for r in F if (qwen_reader(r) or {}).get("task") in ("MAP", "GEOLOGICAL_SECTION")]
    L += ["## 2. Карты и разрезы ВКР", ""]
    for r in maps:
        q = qwen_reader(r)
        L += [f"### {r['key']} — {esc(r.get('caption_as_printed')) or 'подпись не найдена'}", "",
              f"Тип: {TYPE_RU.get(q.get('task'))}; картинка: [{r['key']}](sent/{r['key']}.png)"
              + ("; повторный запрос после зацикливания" if q.get("attempt") == "retry" else ""), ""]
        L += describe(r)
        L += [mine_line(r)]
        if r["key"] in remarks:
            L += [f"- Замечание проверяющего агента: {remarks[r['key']]}"]
        L += [""]
    rest = [r for r in F if r not in tables and r not in maps]
    L += ["## 3. Остальные рисунки ВКР (схемы, графики, фото, формулы)", ""]
    for r in rest:
        q = qwen_reader(r)
        L += [f"### {r['key']} — {esc(r.get('caption_as_printed')) or 'подпись не найдена'}", "",
              f"Тип: {TYPE_RU.get((q or {}).get('task'))}; картинка: [{r['key']}](sent/{r['key']}.png)", ""]
        L += describe(r)
        L += [mine_line(r)]
        if r["key"] in remarks:
            L += [f"- Замечание проверяющего агента: {remarks[r['key']]}"]
        L += [""]
    if notes.get("screenshots"):
        L += ["## 4. Скриншоты пользователя: откуда они", "", "| скриншот | найден в | ключ |", "|---|---|---|"]
        for s in notes["screenshots"]:
            L += [f"| {esc(s['what'])} | {esc(s['where'])} | {esc(s.get('key', '—'))} |"]
        L += [""]
    groups = [("## 5. Рисунки корпуса (места из поиска данных 29.09)", "C"),
              ("## 6. Новые файлы пользователя (план 2013 — только локально; курсовой проект)", ("P13_", "KR_"))]
    for title, pref in groups:
        sel = [r for r in recs if r["key"].startswith(pref)]
        if not sel:
            continue
        L += [title, ""]
        for r in sel:
            q = qwen_reader(r)
            loc = r["locator"]
            where = loc.get("page_id") or loc.get("media_part")
            L += [f"### {r['key']} — {r['source_id']} {esc(where)}", "",
                  f"Подпись (слой корпуса / документ): {esc(r.get('caption_as_printed')) or '—'}; "
                  f"тип: {TYPE_RU.get((q or {}).get('task'))}; картинка: [{r['key']}](sent/{r['key']}.png)", ""]
            L += describe(r, suppress_text=r["key"] in notes.get("suppress_text", []))
            if (q or {}).get("task") == "TABLE_SCREENSHOT" and q.get("reading_status") == "PARSED" and q.get("table"):
                import csv
                qgrid = list(csv.reader((work / q["table"]["csv"]).read_text(encoding="utf-8-sig").splitlines()))
                marks = {}
                for d in (r.get("comparison") or {}).get("disagreements", []):
                    if d["qwen_row"] is not None and d["qwen"] is not None:
                        marks[(d["qwen_row"], d["col"])] = d["glm"] if d["glm"] is not None else "(нет у GLM)"
                L += ["", "**Qwen** (жирным — где GLM прочитал иначе; после ≠ — чтение GLM):", "", md_table(qgrid, marks)]
            if (q or {}).get("task") == "TABLE_SCREENSHOT" and r.get("comparison"):
                c = r["comparison"]
                L += [f"- Совпадение Qwen–GLM: строго {c['agreement']:.1%} ({c['cells_agree']} из {c['cells_compared']}), "
                      f"без учёта похожих букв / пробелов / десятичного разделителя {c['agreement_tolerant']:.1%}"
                      if c.get("agreement") is not None else "- Совпадение: —",
                      f"- Таблицы: `tables/{r['key']}_qwen.csv`, `tables/{r['key']}_glm.csv`"]
            L += [mine_line(r)]
            if r["key"] in remarks:
                L += [f"- Замечание проверяющего агента: {remarks[r['key']]}"]
            L += [""]
    if notes.get("not_read"):
        L += ["## Что не удалось прочитать", ""] + [f"- {x}" for x in notes["not_read"]] + [""]
    (work / "REVIEW_RU.md").write_text("\n".join(L) + "\n", encoding="utf-8")
    print("REVIEW_RU.md:", len(L), "lines")


if __name__ == "__main__":
    main()
