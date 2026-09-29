"""Self-test of the GLM-OCR addendum parsers and metrics (no model answers needed): the truth written as a GLM-OCR
"Text Recognition:" answer (one line per axis, LaTeX/markdown noise added) and as an IE JSON answer (number literals
unquoted) must score recall 1; an empty answer 0. Usage: selftest_glm.py <work dir> <registry>."""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import evaluate as E  # noqa: E402
import evaluate_glm as G  # noqa: E402


def as_literal(s: str) -> str:
    """unquoted JSON literal when the label is a plain JSON number, else a quoted string"""
    try:
        json.loads(s)
        return s if s.lstrip("-").replace(".", "", 1).isdigit() and not s.startswith("0") or s in ("0",) or \
            (s.startswith("0.") or s.startswith("-0.")) else json.dumps(s, ensure_ascii=False)
    except Exception:
        return json.dumps(s, ensure_ascii=False)


def main() -> None:
    work, reg = Path(sys.argv[1]), json.loads(Path(sys.argv[2]).read_text(encoding="utf-8"))
    bad = 0
    for key in reg["evaluation_set"]:
        t = json.loads((work / "truth" / f"{key}.json").read_text(encoding="utf-8"))
        tx, ty = t["ticks"]["x"] or [], t["ticks"]["y"] or []
        # W: one line per axis, with markdown pipes and a LaTeX minus on the first y label
        yl = list(ty)
        if yl and yl[0].startswith("-"):
            yl[0] = "$" + yl[0] + "$"
        text = "| " + " | ".join(tx) + " |\n" + "\n".join(yl) + "\n" + (t["titles"].get("y") or "")
        toks = G.tokens(text)
        r_all = G.ticks_pooled(t, toks)
        r_lab = G.ticks_pooled(t, G.labelish(toks))
        # IE: numbers unquoted where they are JSON numbers
        ie = ('```json\n{"x_axis": {"title": "", "unit": "", "tick_labels": [' + ", ".join(as_literal(v) for v in tx)
              + ']}, "y_axis": {"title": "", "unit": "", "tick_labels": [' + ", ".join(as_literal(v) for v in ty)
              + ']}, "legend": []}\n```')
        obj = G.parse_ie(ie)
        r_ie = E.eval_ticks(t, G.ie_labels(obj, "x"), G.ie_labels(obj, "y"))
        r0 = G.ticks_pooled(t, G.labelish(G.tokens("")))
        n_labelish = sum(E.is_labelish(v) for v in tx + ty)
        # a printed label with an inner space ("R 5.2" on R2) is two tokens of a free-text answer: it cannot match
        n_spaced = sum(" " in v.strip() for v in tx + ty)
        ok = (r_all["hits"] == len(tx) + len(ty) - n_spaced and r_ie["recall"] == 1.0
              and r_ie["axis_hits"] == len(tx) + len(ty) and r0["hits"] == 0 and r_lab["hits"] == n_labelish)
        print(key, "ok" if ok else "FAIL", "all-token recall", r_all["recall"], "labelish hits", r_lab["hits"], "/",
              n_labelish, "IE recall", r_ie["recall"], "axis", r_ie["axis_hits"])
        bad += not ok
    print("FAILED" if bad else "ALL OK", bad)
    sys.exit(1 if bad else 0)


if __name__ == "__main__":
    main()
