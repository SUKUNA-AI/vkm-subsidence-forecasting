"""Self-test of the CHART_MODELS_V1 metrics (run before any model answer exists): the truth fed back as a prediction
must score perfectly, an empty prediction must score zero, a shifted copy must lose exactly the shifted points, and
the JSON repair must recover a cut answer. Usage: selftest_metrics.py <work dir> <registry>."""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import evaluate as E  # noqa: E402


def main() -> None:
    work, reg = Path(sys.argv[1]), json.loads(Path(sys.argv[2]).read_text(encoding="utf-8"))
    bad = 0
    for key in reg["evaluation_set"]:
        t = json.loads((work / "truth" / f"{key}.json").read_text(encoding="utf-8"))
        tk = E.eval_ticks(t, t["ticks"]["x"] or [], t["ticks"]["y"] or [])
        ok_t = tk["recall"] == 1.0 and tk["precision"] == 1.0
        if key in E.VALUE_KEYS_SKIP:
            print(key, "ticks", ok_t)
            bad += not ok_t
            continue
        pred = [{"label": s["label"], "color": None, "x": s["x"], "y": s["y"]} for s in E.truth_series(t)]
        r = E.eval_values(t, pred)
        r0 = E.eval_values(t, [])
        xr, yr = E.ranges(t)
        # a copy shifted by 3 % of the y range: labelled series stay with their truth (labels decide first); for
        # unlabelled crowded curves the distance matching may pair a shifted copy with a neighbour, so the check
        # is only that nothing is within 1 % unless another truth curve is that close (reported, not asserted)
        shifted = [{"label": p["label"], "color": None, "x": p["x"], "y": [v + 0.03 * yr for v in p["y"]]} for p in pred]
        r3 = E.eval_values(t, shifted)
        labelled = all(p["label"] not in (None, "") for p in pred)
        # the vertical error is ill-conditioned on near-vertical truth segments (X32, X33): ≥ 0.97 there
        ok = (ok_t and r["hit@1%"] == 1.0 and r["vhit@1%"] >= 0.97 and r["missing_share"] == 0
              and r["hallucinated_share"] == 0 and r["series_count_ok"] and (r["label_acc"] in (None, 1.0))
              and r0["hit@5%"] == 0 and r0["missing_share"] == 1.0 and r3["hit@5%"] == 1.0
              and r3["vhit@5%"] >= 0.97 and (r3["vhit@2%"] == 0 or not labelled))
        print(key, "ok" if ok else "FAIL", {k: r[k] for k in ("hit@1%", "vhit@1%", "missing_share",
                                                              "hallucinated_share", "series_count_ok", "label_acc",
                                                              "n_matched")},
              "shift3%: 2D", round(r3["hit@2%"], 3), "vert", round(r3["vhit@2%"], 3), "empty:", r0["hit@5%"])
        bad += not ok
    cut = '{"series": [{"label": "a", "points": [[1, 2], [2, 3], [3, 4'
    rep = E.parse_json_answer(cut)
    ok_cut = rep is not None and rep["series"][0]["points"] == [[1, 2], [2, 3]]
    print("json repair of a cut answer:", ok_cut, rep)
    bad += not ok_cut
    print("FAILED" if bad else "ALL OK", bad)
    sys.exit(1 if bad else 0)


if __name__ == "__main__":
    main()
