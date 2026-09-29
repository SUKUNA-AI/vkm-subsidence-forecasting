"""FIGURE_READINGS_V1 — parse the raw answers, write per-image JSON / table CSV, compare the two table readers.

    python parse_readings.py --work <dir>

Inputs: jobs.json, raw/qwen/{CLASSIFY,EXTRACT}/<key>.json, raw/glm/TABLE/<key>.json (optional).
Outputs (all in the git-ignored work dir): parsed/<key>.json, tables/<key>_qwen.csv, tables/<key>_glm.csv,
readings.jsonl (one record per image), hashes.json, comparison_summary.json (counts and rates only).

Table comparison: both readings become grids (Qwen: header-group row, column row, data rows; GLM: the HTML table
with colspan/rowspan expanded by repetition; a header group is also repeated over its span). Rows are aligned by
dynamic programming on a tolerant row similarity, each row pair with its best column offset (-2…2) — tolerance is
used only for alignment. A cell agrees (strict rate) when both strings are equal after NFC and whitespace collapsing
only: a decimal comma never equals a point, '№' never equals 'No', a Cyrillic letter never equals a look-alike Latin
letter. The tolerant rate adds cells that differ only by look-alike letters, spacing or the decimal separator.
Every disagreement is listed; cells present in one reader only count as disagreements.
"""
from __future__ import annotations

import argparse
import csv
import difflib
import hashlib
import html
import io
import json
import re
import unicodedata
from collections import Counter
from html.parser import HTMLParser
from pathlib import Path

STATUS = "AUTO_EXTRACTED_UNREVIEWED"
ORIGIN = "VLM_EXTRACTED"
_RF = None


def _rf():
    """read_figures.py of this package (for its loop detector)."""
    global _RF
    if _RF is None:
        import importlib.util
        spec = importlib.util.spec_from_file_location("fr_read_figures", Path(__file__).with_name("read_figures.py"))
        _RF = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(_RF)
    return _RF


# ------------------------------------------------------------------------------------------------ answer parsing
def parse_json_answer(text: str | None):
    """The JSON object of a model answer (code fences and text around it tolerated); None if unparseable."""
    if not text:
        return None, "empty"
    t = text.strip()
    t = re.sub(r"^```(?:json)?\s*", "", t)
    t = re.sub(r"\s*```\s*$", "", t)
    i, k = t.find("{"), t.rfind("}")
    if i < 0 or k < i:
        return None, "no JSON object"
    body = t[i:k + 1]
    try:
        return json.loads(body), None
    except json.JSONDecodeError as e:
        fixed = re.sub(r",\s*([}\]])", r"\1", body)          # trailing commas only
        try:
            return json.loads(fixed), "repaired trailing commas"
        except json.JSONDecodeError:
            return None, f"JSON error: {e.msg} at {e.pos}"


def _strings(obj):
    """Every string inside a parsed answer (dicts, lists, grids), except the model's own description."""
    if isinstance(obj, str):
        yield obj
    elif isinstance(obj, dict):
        for k, v in obj.items():
            if k not in ("description", "what", "reason"):
                yield from _strings(v)
    elif isinstance(obj, list):
        for v in obj:
            yield from _strings(v)


def norm(s) -> str:
    """Whitespace-trimmed text as the model wrote it (canonical NFC composition only: no compatibility mapping, so
    '№' never becomes 'No' and a Roman 'II' never equals a Cyrillic 'П')."""
    s = "" if s is None else str(s)
    s = unicodedata.normalize("NFC", s)
    return re.sub(r"\s+", " ", s).strip()


# ------------------------------------------------------------------------------------------------ table grids
def qwen_grid(obj: dict) -> tuple[list[list[str]], int, dict]:
    cols = [norm(c) for c in (obj.get("columns") or [])]
    rows = [[norm(c) for c in (r if isinstance(r, list) else [r])] for r in (obj.get("rows") or [])]
    ncol = max([len(cols)] + [len(r) for r in rows] + [0])
    grid, n_header = [], 0
    groups = []
    for hg in obj.get("header_groups") or []:
        # the model often repeats every column header as a one-column "group": keep only real groups (a span of
        # more than one column, or a one-column header stacked above a different column header)
        try:
            a, b = int(hg.get("first_col", 0)), int(hg.get("last_col", 0))
        except (TypeError, ValueError, AttributeError):
            continue
        t = norm(hg.get("text"))
        if b > a or (0 <= a < len(cols) and t and t != cols[a]):
            groups.append(hg)
    if groups:
        g = [""] * ncol
        for hg in groups:
            try:
                a, b = int(hg.get("first_col", 0)), int(hg.get("last_col", 0))
            except (TypeError, ValueError):
                continue
            for c in range(max(0, a), min(ncol, b + 1)):
                g[c] = norm(hg.get("text"))
        grid.append(g)
        n_header += 1
    if cols:
        grid.append(cols)
        n_header += 1
    grid += rows
    meta = {"n_columns": ncol, "n_header_rows": n_header, "n_data_rows": len(rows),
            "unreadable_cells": sum(1 for r in rows for c in r if c.upper() == "UNREADABLE")}
    return grid, n_header, meta


class _TableHTML(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.rows, self._row, self._cell, self._span = [], None, None, (1, 1)

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == "tr":
            self._row = []
        elif tag in ("td", "th"):
            self._cell = []
            self._span = (int(a.get("colspan", 1) or 1), int(a.get("rowspan", 1) or 1))
        elif tag == "br" and self._cell is not None:
            self._cell.append(" ")

    def handle_endtag(self, tag):
        if tag in ("td", "th") and self._cell is not None and self._row is not None:
            self._row.append(("".join(self._cell), *self._span))
            self._cell = None
        elif tag == "tr" and self._row is not None:
            self.rows.append(self._row)
            self._row = None

    def handle_data(self, data):
        if self._cell is not None:
            self._cell.append(data)


def glm_grid(content: str | None) -> tuple[list[list[str]], str]:
    if not content:
        return [], "empty"
    if "<t" in content.lower():
        p = _TableHTML()
        p.feed(content)
        grid: list[list[str]] = []
        pending: dict[tuple[int, int], str] = {}
        for r, row in enumerate(p.rows):
            out, c = [], 0
            cells = list(row)
            while cells or (r, c) in pending:
                if (r, c) in pending:
                    out.append(pending.pop((r, c)))
                    c += 1
                    continue
                text, cs, rs = cells.pop(0)
                text = norm(html.unescape(text))
                for k in range(cs):
                    out.append(text)
                    for rr in range(1, rs):
                        pending[(r + rr, c + k)] = text
                c += cs
            grid.append(out)
        return grid, "html"
    lines = [l for l in content.splitlines() if l.strip().startswith("|")]
    if lines:
        grid = []
        for l in lines:
            cells = [norm(x) for x in l.strip().strip("|").split("|")]
            if all(re.fullmatch(r":?-{2,}:?", x) for x in cells if x):
                continue
            grid.append(cells)
        return grid, "markdown"
    return [[norm(l)] for l in content.splitlines() if l.strip()], "text_lines"


def _soft(v: str | None) -> str:
    """Tolerant form used only to ALIGN rows and columns (never to score strict agreement)."""
    v = norm(v).translate(_LOOK).replace(",", ".").replace(" ", "").lower()
    return v


def _row_sim(qr: list[str], gr: list[str]) -> tuple[float, int]:
    """Best column offset (-2…2) of two rows and the share of tolerantly equal non-empty cells at that offset."""
    best = (0.0, 0)
    for off in range(-2, 3):
        hits = n = 0
        for c in range(max(len(qr), len(gr))):
            a = qr[c] if 0 <= c < len(qr) else None
            b = gr[c + off] if 0 <= c + off < len(gr) else None
            if not (a or b):
                continue
            n += 1
            hits += 1 if a is not None and b is not None and _soft(a) == _soft(b) and _soft(a) != "" else 0
        s = hits / n if n else 0.0
        if s > best[0] + 1e-9 or (abs(s - best[0]) < 1e-9 and abs(off) < abs(best[1])):
            best = (s, off)
    return best


def compare(q: list[list[str]], g: list[list[str]]) -> dict:
    """Rows aligned by dynamic programming on row similarity (tolerant cell equality, gap cost 0.3), each row pair
    with its best column offset (-2…2); then every cell is scored strictly (NFC + whitespace only)."""
    n, m = len(q), len(g)
    GAP = 0.3
    sim = [[_row_sim(q[i], g[j]) for j in range(m)] for i in range(n)]
    D = [[0.0] * (m + 1) for _ in range(n + 1)]
    B = [[None] * (m + 1) for _ in range(n + 1)]
    for i in range(1, n + 1):
        D[i][0], B[i][0] = D[i - 1][0] - GAP, "up"
    for j in range(1, m + 1):
        D[0][j], B[0][j] = D[0][j - 1] - GAP, "left"
    for i in range(1, n + 1):
        for j in range(1, m + 1):
            opts = [(D[i - 1][j - 1] + sim[i - 1][j - 1][0], "diag"), (D[i - 1][j] - GAP, "up"),
                    (D[i][j - 1] - GAP, "left")]
            D[i][j], B[i][j] = max(opts, key=lambda t: t[0])
    pairs, i, j = [], n, m
    while i > 0 or j > 0:
        b = B[i][j]
        if b == "diag":
            pairs.append((i - 1, j - 1, sim[i - 1][j - 1][1])); i, j = i - 1, j - 1
        elif b == "up":
            pairs.append((i - 1, None, 0)); i -= 1
        else:
            pairs.append((None, j - 1, 0)); j -= 1
    pairs.reverse()
    agree, total, diffs, kinds = 0, 0, [], Counter()
    for qi, gi, off in pairs:
        qr = q[qi] if qi is not None else []
        gr = g[gi] if gi is not None else []
        cols = range(min(0, -off), max(len(qr), len(gr) - off))
        for c in cols:
            qv = norm(qr[c]) if 0 <= c < len(qr) else None
            gc = c + off
            gv = norm(gr[gc]) if 0 <= gc < len(gr) else None
            if (qv or "") == "" and (gv or "") == "":
                continue                       # empty in both (or padding against an empty cell)
            total += 1
            if qv is not None and gv is not None and qv == gv:
                agree += 1
            else:
                kind = diff_kind(qv, gv)
                kinds[kind] += 1
                diffs.append({"qwen_row": qi, "glm_row": gi, "col": c, "glm_col": gc, "qwen": qv, "glm": gv,
                              "kind": kind})
    tol = agree + kinds["lookalike_letters_only"] + kinds["spacing_only"] + kinds["decimal_separator_only"]
    return {"cells_compared": total, "cells_agree": agree, "agreement": round(agree / total, 4) if total else None,
            "agreement_tolerant": round(tol / total, 4) if total else None,
            "tolerant_rule": "strict matches plus cells that differ only by look-alike Latin/Cyrillic letters, spacing "
                             "or the decimal separator",
            "disagreement_kinds": dict(kinds),
            "rows_qwen": n, "rows_glm": m, "rows_paired": sum(1 for a, b, _ in pairs if a is not None and b is not None),
            "column_offsets_used": dict(Counter(o for a, b, o in pairs if a is not None and b is not None)),
            "disagreements": diffs}


_LOOK = str.maketrans({"A": "А", "B": "В", "C": "С", "E": "Е", "H": "Н", "K": "К", "M": "М", "O": "О", "P": "Р",
                       "T": "Т", "X": "Х", "Y": "У", "a": "а", "c": "с", "e": "е", "o": "о", "p": "р", "x": "х",
                       "y": "у", "l": "I"})      # Latin letters drawn like Cyrillic ones; lower-case L like capital I


def diff_kind(qv, gv) -> str:
    if qv is None or gv is None or qv == "" or gv == "":
        return "missing_in_one_reader"
    if qv.replace(" ", "") == gv.replace(" ", ""):
        return "spacing_only"
    if qv.translate(_LOOK) == gv.translate(_LOOK):
        return "lookalike_letters_only"
    if qv.replace(",", ".") == gv.replace(",", "."):
        return "decimal_separator_only"
    return "different"


def write_csv(path: Path, grid: list[list[str]]) -> None:
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\n")
    for r in grid:
        w.writerow(r)
    path.write_text(buf.getvalue(), encoding="utf-8-sig")


# ------------------------------------------------------------------------------------------------ main
def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--work", required=True)
    ap.add_argument("--keys", default="")
    args = ap.parse_args()
    work = Path(args.work)
    jobs = json.loads((work / "jobs.json").read_text(encoding="utf-8"))
    overrides = {}
    ov = work / "type_overrides.json"
    if ov.exists():
        overrides = json.loads(ov.read_text(encoding="utf-8"))
    (work / "parsed").mkdir(exist_ok=True)
    (work / "tables").mkdir(exist_ok=True)
    readings, hashes, summary = [], {}, {"tables": {}}
    for j in jobs:
        key = j["key"]
        cf = work / "raw" / "qwen" / "CLASSIFY" / f"{key}.json"
        ef = work / "raw" / "qwen" / "EXTRACT" / f"{key}.json"
        gf = work / "raw" / "glm" / "TABLE" / f"{key}.json"
        if not cf.exists():
            continue
        crec = json.loads(cf.read_text(encoding="utf-8"))
        erec = json.loads(ef.read_text(encoding="utf-8")) if ef.exists() else None
        rf = work / "raw" / "qwen" / "EXTRACT_RETRY" / f"{key}.json"
        first = None
        if erec and erec.get("finish_reason") == "length" and rf.exists():
            first = {"finish_reason": erec.get("finish_reason"),
                     "completion_tokens": (erec.get("usage") or {}).get("completion_tokens"),
                     "prompt_sha256": erec.get("prompt_sha256"), "raw_file": f"raw/qwen/EXTRACT/{key}.json",
                     "note": "greedy repetition loop up to the token cap; answer not used"}
            erec = json.loads(rf.read_text(encoding="utf-8"))
        rec = {
            "key": key, "source_id": j["source_id"], "container": j.get("container"), "locator": j["locator"],
            "image_sha256": j["original_sha256"], "image_format": j.get("original_format"),
            "sent_png_sha256": j["sent_sha256"], "sent_size": [j["width"], j["height"]],
            "rendered_from_vector": bool(j.get("rendered")),
            "caption_as_printed": j.get("caption"),
            "classification": {"type": crec.get("type"), "by": "qwen", "raw_answer": crec.get("content"),
                               "prompt_sha256": crec.get("prompt_sha256")},
            "status": STATUS, "origin": ORIGIN,
            "readers": [],
        }
        if key in overrides:
            rec["classification"]["override"] = overrides[key]
        # the image a raw answer belongs to must be the image of the job (sha256 of the PNG actually sent)
        sent_ok = all(x is None or x.get("sent_sha256") == j["sent_sha256"] for x in (crec, erec))
        rec["sent_image_hash_check"] = "PASS" if sent_ok else "MISMATCH"
        if first is not None:
            first["retry_kind"] = erec.get("retry_kind") or (
                "loop_presence_penalty" if "presence_penalty" in (erec.get("sampling") or {}) else "long_answer_larger_cap")
        if erec:
            capped = erec.get("finish_reason") == "length"
            loop = capped and _rf().is_loop(erec.get("content"))
            obj, perr = (None, ("answer ran into the token cap in a repetition loop" if loop else
                                "answer ran into the token cap (truncated)") + "; not parsed") if capped \
                else parse_json_answer(erec.get("content"))
            looped = capped
            rec["readers"].append({
                "reader": "qwen", "task": erec.get("task"), "model": erec["model"], "prompt_sha256": erec["prompt_sha256"],
                "sampling": erec["sampling"], "wall_s": erec["wall_s"], "peak_card_mib": erec["peak_card_mib"],
                "usage": erec.get("usage"), "finish_reason": erec.get("finish_reason"), "error": erec.get("error"),
                "attempt": "retry" if first else "first", "first_attempt": first,
                "reading_status": ("FAILED_REPETITION_LOOP" if loop else "TRUNCATED_AT_CAP") if looped
                else ("PARSED" if obj is not None else "UNPARSEABLE"),
                "raw_answer": erec.get("content") if not looped else None,
                "raw_file": f"raw/qwen/{'EXTRACT_RETRY' if first else 'EXTRACT'}/{key}.json",
                "parsed": obj, "parse_note": perr})
            grid, meta = None, None
            if erec.get("task") == "TABLE_SCREENSHOT" and isinstance(obj, dict):
                grid, nh, meta = qwen_grid(obj)
                write_csv(work / "tables" / f"{key}_qwen.csv", grid)
                rec["readers"][-1]["table"] = {**meta, "csv": f"tables/{key}_qwen.csv"}
        if gf.exists():            # the second reader is kept even when the first one failed
            grec = json.loads(gf.read_text(encoding="utf-8"))
            ggrid, gfmt = glm_grid(grec.get("content"))
            write_csv(work / "tables" / f"{key}_glm.csv", ggrid)
            rec["readers"].append({
                "reader": "glm-ocr", "task": "TABLE", "model": grec["model"], "prompt": grec["prompt"],
                "prompt_sha256": grec["prompt_sha256"], "sampling": grec["sampling"], "wall_s": grec["wall_s"],
                "peak_card_mib": grec["peak_card_mib"], "usage": grec.get("usage"),
                "finish_reason": grec.get("finish_reason"), "error": grec.get("error"),
                "reading_status": "TRUNCATED_AT_CAP" if grec.get("finish_reason") == "length" else
                ("PARSED" if ggrid else "EMPTY"),
                "raw_answer": grec.get("content"), "parsed": {"format": gfmt, "grid": ggrid},
                "table": {"n_rows": len(ggrid), "n_columns": max([len(r) for r in ggrid] + [0]),
                          "csv": f"tables/{key}_glm.csv"}})
            rec["sent_image_hash_check"] = rec["sent_image_hash_check"] if grec.get("sent_sha256") == j["sent_sha256"] \
                else "MISMATCH"
            if erec and grid is not None:
                cmp_ = compare(grid, ggrid)
                rec["comparison"] = cmp_
                summary["tables"][key] = {k: v for k, v in cmp_.items() if k != "disagreements"} | {
                    "n_disagreements": len(cmp_["disagreements"]), "qwen_rows": meta["n_data_rows"],
                    "qwen_columns": meta["n_columns"], "qwen_unreadable": meta["unreadable_cells"]}
        mine = []
        # a mine designation only (СКРУ-N, БКПРУ-N, «рудник …», Solikamsk-N mine, Mine-A); towns, depressions and
        # formations named after towns are areas, not mines
        mine_re = re.compile(r"(СКРУ[\s\-–_]*\d|БКПРУ[\s\-–_]*\d|БКРУ[\s\-–_]*\d|рудник\w*\s+\S+|Solikamsk-\d|"
                             r"Berezniki-\d|Mine-?\s?[A-Z]\b|\bmine\b)", re.I)
        for r in rec["readers"]:
            p = r.get("parsed")
            if isinstance(p, dict):
                for m in p.get("mine_as_printed") or []:
                    # the model sometimes dumps every cell into this field: keep only strings that name a mine
                    if isinstance(m, str) and mine_re.search(m) and norm(m) not in mine:
                        mine.append(norm(m))
        rec["mine_as_printed_by_reader"] = mine
        # mine designations anywhere in the text the readers transcribed (title block, labels, cells …), each once
        found = []
        for r in rec["readers"]:
            for s in _strings(r.get("parsed")):
                for m in re.finditer(r"(СКРУ[\s\-–_]*\d|БКПРУ[\s\-–_]*\d|БКРУ[\s\-–_]*\d|Solikamsk-\d|Berezniki-\d|"
                                     r"Mine-?\s?[A-Z]\b)", s):
                    v = re.sub(r"[\s\-–_]+", "-", m.group(0))
                    if v not in found:
                        found.append(v)
        rec["mine_designations_in_reading"] = found
        rec["mine_in_caption_as_printed"] = sorted(set(m.group(0) for m in re.finditer(
            r"(СКРУ[\s\-–_]*\d|БКПРУ[\s\-–_]*\d|БКРУ[\s\-–_]*\d|рудник\w*\s+[«\"]?[А-ЯЁA-Z][\w\-–]*|"
            r"Solikamsk-\d mine|Berezniki-\d mine|Mine-?\s?[A-Z]\b)", j.get("caption") or "")))
        (work / "parsed" / f"{key}.json").write_text(json.dumps(rec, ensure_ascii=False, indent=1), encoding="utf-8")
        readings.append(rec)
        hashes[key] = {"image_sha256": j["original_sha256"], "sent_png_sha256": j["sent_sha256"],
                       "rendered_png_sha256": (j.get("rendered") or {}).get("sha256")}
    with open(work / "readings.jsonl", "w", encoding="utf-8", newline="\n") as f:
        for r in readings:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    (work / "hashes.json").write_text(json.dumps(hashes, ensure_ascii=False, indent=1), encoding="utf-8")
    types = {}
    for r in readings:
        types[r["classification"]["type"]] = types.get(r["classification"]["type"], 0) + 1
    summary["n_images"] = len(readings)
    summary["types"] = types
    (work / "comparison_summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=1), encoding="utf-8")
    print(json.dumps({"n": len(readings), "types": types,
                      "tables": {k: (v["agreement"], v["cells_compared"]) for k, v in summary["tables"].items()}},
                     ensure_ascii=False))


if __name__ == "__main__":
    main()
