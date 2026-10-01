"""Normalisation of GLM-OCR raw outputs (never edits the raw record; rule id ``OCR_NORMALIZE_RULE``).

* text: Markdown-like output → text (heading/emphasis markers and ``<br>`` removed; inline math kept);
* formula: LaTeX delimiters ``$$…$$``, ``\\[…\\]``, ``$…$``, ``\\(…\\)`` removed, equation number from ``\\tag{}`` or a
  trailing ``(3.12)``; a structural check (balanced braces, ``\\begin``/``\\end``) sets ``latex_parse_ok``;
* table: HTML ``<table>`` (or a Markdown pipe table) → cell grid with row/col spans, header flags, plain text.

No semantics is added: no variables, no units, no numeric parsing (Formula v0, contract §15).
"""
from __future__ import annotations

import re
from typing import Any

# v3: retain raw grid and explicit dispositions; native tables preserve empty rows.
OCR_NORMALIZE_RULE = "ocr_normalize_v3"

_MD_HEADING = re.compile(r"^\s{0,3}#{1,6}\s+", re.M)
_MD_EMPH = re.compile(r"(\*\*|__)(.+?)\1", re.S)
_BR = re.compile(r"<br\s*/?>", re.I)
_TAG = re.compile(r"</?(?:sup|sub|b|i|u|strong|em|span)[^>]*>", re.I)


def text_from_markdown(content: str | None) -> str:
    if not content:
        return ""
    s = content.replace("\r\n", "\n")
    s = _BR.sub("\n", s)
    s = _MD_HEADING.sub("", s)
    s = _MD_EMPH.sub(r"\2", s)
    s = _TAG.sub("", s)
    return s.strip()


# ---------------------------------------------------------------------------------------------------- formula
_DELIMS = [
    (re.compile(r"^\s*\$\$(.*)\$\$\s*$", re.S), 1),
    (re.compile(r"^\s*\\\[(.*)\\\]\s*$", re.S), 1),
    (re.compile(r"^\s*\\\((.*)\\\)\s*$", re.S), 1),
    (re.compile(r"^\s*\$(.*)\$\s*$", re.S), 1),
]
_TAG_NUM = re.compile(r"\\tag\*?\{([^{}]+)\}")
_TRAIL_NUM = re.compile(r"(?:\\q?quad|\\,|\\;|~|\s)*\(\s*(\d+(?:[.,]\d+)*[a-zа-я]?)\s*\)\s*$", re.I)


def strip_math_delimiters(content: str) -> str:
    s = content.strip()
    for rx, g in _DELIMS:
        m = rx.match(s)
        if m:
            return m.group(g).strip()
    return s


def latex_structure_ok(latex: str) -> bool:
    depth = 0
    i = 0
    while i < len(latex):
        c = latex[i]
        if c == "\\":
            i += 2
            continue
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth < 0:
                return False
        i += 1
    if depth != 0:
        return False
    environments: list[str] = []
    for match in re.finditer(r"\\(begin|end)\{([^}]+)\}", latex):
        kind, name = match.groups()
        if kind == "begin":
            environments.append(name)
        elif not environments or environments.pop() != name:
            return False
    return not environments


def normalize_formula(content: str | None) -> dict[str, Any]:
    if not content or not content.strip():
        return {"normalized_latex": None, "equation_label": None, "latex_parse_ok": None}
    latex = strip_math_delimiters(content)
    label = None
    m = _TAG_NUM.search(latex)
    if m:
        label = f"({m.group(1).strip()})"
        latex = _TAG_NUM.sub("", latex).strip()
    else:
        m2 = _TRAIL_NUM.search(latex)
        if m2 and m2.start() > 0:
            label = f"({m2.group(1)})"
            latex = latex[: m2.start()].rstrip()
    latex = re.sub(r"\s+", " ", latex).strip()
    return {"normalized_latex": latex or None, "equation_label": label, "latex_parse_ok": latex_structure_ok(latex)}


# ---------------------------------------------------------------------------------------------------- table
def _cell_text(el: Any) -> str:
    # Nested tables are separate objects, not additional rows/text in the parent.
    parts = []
    for child in el.iter():
        if child is not el and any(a.tag == "table" for a in child.iterancestors() if a is not el
                                   and el in a.iterancestors()):
            continue
        if child.tag != "table" and child.text:
            parts.append(child.text)
        if child is not el and child.tail:
            parts.append(child.tail)
    return re.sub(r"\s+", " ", "".join(parts)).strip()


def _table_markup(html: str) -> str | None:
    """The first ``<table>…</table>``; an output cut at the token cap keeps its complete rows (closed here)."""
    start = re.search(r"<table\b", html, re.I)
    if not start:
        return None
    body = html[start.start():]
    depth = 0
    for token in re.finditer(r"</?table\b[^>]*>", body, re.I):
        depth += -1 if token.group(0).startswith("</") else 1
        if depth == 0:
            return body[:token.end()]
    last = body.lower().rfind("</tr>")
    return body[:last + 5] + "</table>" if last >= 0 else None


def parse_html_table(html: str, *, preserve_empty: bool = False) -> dict[str, Any] | None:
    from lxml import etree, html as lhtml

    markup = _table_markup(html)
    if not markup:
        return None
    try:
        root = lhtml.fragment_fromstring(markup)
    except (etree.ParserError, ValueError):
        return None
    table = root if root.tag == "table" else root.find(".//table")
    if table is None:
        return None
    occupied: set[tuple[int, int]] = set()
    cells = []
    rows = [tr for tr in table.iter("tr")
            if next((a for a in tr.iterancestors() if a.tag == "table"), None) is table]
    dispositions = []
    for nested in table.iter("table"):
        if nested is not table:
            dispositions.append({"code": "NESTED_TABLE_SEPARATE", "locator": nested.getroottree().getpath(nested)})
    for r, tr in enumerate(rows):
        c = 0
        in_head = any(a.tag == "thead" for a in tr.iterancestors())
        for td in tr:
            if td.tag not in ("td", "th"):
                continue
            while (r, c) in occupied:
                c += 1
            try:
                rs = max(1, int(td.get("rowspan", "1") or 1))
                cs = max(1, int(td.get("colspan", "1") or 1))
            except ValueError:
                rs, cs = 1, 1
            for dr in range(rs):
                for dc in range(cs):
                    occupied.add((r + dr, c + dc))
            cells.append({"row": r, "col": c, "row_span": rs, "col_span": cs,
                          "is_header": td.tag == "th" or in_head, "text": _cell_text(td)})
            c += cs
    raw_grid = {"cells": [dict(x) for x in cells], "n_rows": max(len(rows), max((r for r, _ in occupied), default=-1) + 1),
                "n_cols": max((c for _, c in occupied), default=-1) + 1}
    # OCR can loop on empty <tr>. Suppression is a recorded hypothesis, never raw data deletion.
    filled = {x["row"] + dr for x in cells if x["text"] for dr in range(x["row_span"])}
    last = raw_grid["n_rows"] - 1 if preserve_empty else max(filled, default=-1)
    dropped = sum(1 for x in cells if x["row"] > last)
    for row in range(last + 1, raw_grid["n_rows"]):
        dispositions.append({"code": "SUPPRESSED_TRAILING_EMPTY_ROW", "row": row,
                             "reason": "OCR_EMPTY_LOOP_CANDIDATE", "requires_review": True})
    for x in cells:
        if x["row"] > last:
            dispositions.append({"code": "SUPPRESSED_TRAILING_EMPTY", "row": x["row"], "col": x["col"],
                                 "reason": "OCR_EMPTY_LOOP_CANDIDATE", "requires_review": True})
        elif x["row"] + x["row_span"] - 1 > last:
            dispositions.append({"code": "CLIPPED_EMPTY_SPAN", "row": x["row"], "col": x["col"],
                                 "raw_row_span": x["row_span"], "normalized_row_span": last - x["row"] + 1})
    cells = [{**x, "row_span": min(x["row_span"], last - x["row"] + 1)} for x in cells if x["row"] <= last]
    occupied = {(r, c) for r, c in occupied if r <= last}
    n_rows = raw_grid["n_rows"] if preserve_empty else max((r for r, _ in occupied), default=-1) + 1
    n_cols = max((c for _, c in occupied), default=-1) + 1
    return {"cells": cells, "n_rows": n_rows, "n_cols": n_cols, "format": "HTML", "trailing_empty_cells": dropped,
            "raw_grid": raw_grid, "dispositions": dispositions}


def parse_markdown_table(content: str) -> dict[str, Any] | None:
    lines = [ln.strip() for ln in content.splitlines() if ln.strip().startswith("|")]
    if len(lines) < 2:
        return None
    rows = []
    for ln in lines:
        if re.fullmatch(r"\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)*\|?", ln):
            continue
        rows.append([c.strip() for c in ln.strip("|").split("|")])
    cells = [{"row": r, "col": c, "row_span": 1, "col_span": 1, "is_header": r == 0, "text": t}
             for r, row in enumerate(rows) for c, t in enumerate(row)]
    return {"cells": cells, "n_rows": len(rows), "n_cols": max((len(r) for r in rows), default=0),
            "format": "MARKDOWN"}


def table_text(cells: list[dict[str, Any]]) -> str:
    by_row: dict[int, list[tuple[int, str]]] = {}
    for c in cells:
        by_row.setdefault(c["row"], []).append((c["col"], c["text"]))
    return "\n".join(" | ".join(t for _, t in sorted(v)) for _, v in sorted(by_row.items()))


def normalize_table(content: str | None, *, preserve_empty: bool = False) -> dict[str, Any]:
    if not content or not content.strip():
        return {"cells": [], "n_rows": None, "n_cols": None, "raw_format": "TEXT", "normalized_text": None,
                "structure_ok": False}
    parsed = parse_html_table(content, preserve_empty=preserve_empty) if "<table" in content.lower() else None
    if parsed is None:
        parsed = parse_markdown_table(content)
    if parsed is None:
        return {"cells": [], "n_rows": None, "n_cols": None, "raw_format": "TEXT",
                "normalized_text": text_from_markdown(content) or None, "structure_ok": False}
    return {"cells": parsed["cells"], "n_rows": parsed["n_rows"], "n_cols": parsed["n_cols"],
            "raw_grid": parsed.get("raw_grid", {"cells": parsed["cells"], "n_rows": parsed["n_rows"],
                                                "n_cols": parsed["n_cols"]}),
            "dispositions": parsed.get("dispositions", []),
            "raw_format": parsed["format"], "normalized_text": table_text(parsed["cells"]) or None,
            "structure_ok": parsed["n_rows"] > 0 and parsed["n_cols"] > 0}


def normalize_table_bands(contents: list[str | None]) -> dict[str, Any]:
    """A table recognised in horizontal bands: grids stacked in band order (row offsets), header only from band 1."""
    if len(contents) == 1:
        return normalize_table(contents[0])
    cells: list[dict[str, Any]] = []
    offset, n_cols, ok, fmt = 0, 0, True, "HTML"
    audits = []
    for i, c in enumerate(contents):
        t = normalize_table(c)
        audits.append({"band": i, "normalized_row_offset": offset, "raw_grid": t.get("raw_grid"),
                       "dispositions": t.get("dispositions", [])})
        ok = ok and t["structure_ok"]
        if t["raw_format"] not in ("HTML", "TEXT"):
            fmt = t["raw_format"]
        for cell in t["cells"]:
            cells.append({**cell, "row": cell["row"] + offset, "is_header": bool(cell["is_header"]) and i == 0})
        offset += t["n_rows"] or 0
        n_cols = max(n_cols, t["n_cols"] or 0)
    return {"cells": cells, "n_rows": offset or None, "n_cols": n_cols or None, "raw_format": fmt,
            "normalized_text": table_text(cells) or None, "structure_ok": ok and bool(cells), "bands": len(contents),
            "band_audits": audits}
