"""CHART_MODELS_V1 — model inputs (PNG) and truth files.

Runs in the WSL venv of agent FD (pymupdf, opencv, pyarrow). Every machine path is an argument; the outputs go to a
git-ignored work directory (``--out``): ``inputs/<key>.png``, ``truth/<key>.json``, ``qc/<key>_truth.png`` and
``manifest.json`` with sha256 of every file. Nothing here is committed except this code.

Subcommands:

* ``select``  — applies the preregistered selection rule for the extra vector-render figures (X01…) to the FD sweep
  log and prints the registry rows (figure ids only).
* ``build``   — builds inputs and truth for all figures of the registry (``figures_v1.json``).

Truth, by figure family (see PREREGISTRATION.md §2):

* S (vector showcase) and X (extra vector renders): route-A points of the published ``figure_series_v0.parquet``
  (native PDF vertices, ~0.03 pt), restricted to vertices inside the calibrated plot box (vertices outside it are
  clipped away in the drawn figure and invisible in the image); tick labels = route-A calibration labels (PDF text
  layer; S2 dates by the verified local OCR) completed by the manual transcription file where the calibration left
  printed labels out; series labels from route A, corrected by the manual file.
* R2/R3: the printed data table under the chart (manual transcription); the value row is masked in the model input.
* R4: the manual digitization of VKM-SRC-002 p.15 line 6 already in PUBLIC evidence.
* R1, R5: tick labels and legend labels only (manual transcription); no numeric truth.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import random
import shutil
from collections import defaultdict
from pathlib import Path

import numpy as np

DPI = 200            # render resolution of vector crops (the FD showcase used the same)
BOX_GROW = 0.40      # visible region: the calibrated plot box (tick-label span) grown by this share of its size on
                     # every side (drawn frames often extend beyond the outermost tick label), cut to the image
SEED = 20260929


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def load_rows(parquet: Path) -> dict:
    import pyarrow.parquet as pq

    by_fig = defaultdict(list)
    for r in pq.read_table(parquet).to_pylist():
        if r["route"] == "A_NATIVE_VECTOR":
            by_fig[r["figure_id"]].append(r)
    return by_fig


def both_axes(s: dict) -> bool:
    return "X_UNCALIBRATED" not in s["flags"] and "Y_UNCALIBRATED" not in s["flags"]


# --------------------------------------------------------------------------------------------- selection rule
def select(sweep: Path, showcase_ids: set[str], n: int = 20, per_source: int = 2) -> list[dict]:
    """Eligible: routes A and B both calibrate both axes; the routes found the same number of series and every series
    was matched with max relative disagreement ≤ 1e-3 (the truth is confirmed by two independent readers of the
    vectors); 1…12 route-A series with both axes and ≥ 3 points; not a showcase figure. Sampling: per source a seeded
    shuffle keeps ≤ ``per_source`` figures, then a seeded shuffle of the union keeps ``n``; the rest keeps its seeded
    order as the replacement list for figures whose truth fails the QC overlay."""
    log = json.loads((sweep / "figure_series" / "sweep_log.json").read_text(encoding="utf-8"))
    by_fig = load_rows(sweep / "figure_series" / "figure_series_v0.parquet")
    pool = []
    for e in log:
        fid, c = e["figure_id"], e.get("cmp") or {}
        if fid in showcase_ids or e["A"]["axis"] != "OK" or e["B"]["axis"] != "OK":
            continue
        ms = c.get("matched_series") or []
        if c.get("series_a") != c.get("series_b") or len(ms) != c.get("series_a"):
            continue
        if c.get("max_rel_diff") is None or c["max_rel_diff"] > 1e-3:
            continue
        ok = [s for s in by_fig[fid] if both_axes(s) and s["n_points"] >= 3]
        if 1 <= len(ok) <= 12:
            pool.append({"figure_id": fid, "source_id": e["source_id"], "series": len(ok)})
    rng = random.Random(SEED)
    by_src = defaultdict(list)
    for p in sorted(pool, key=lambda p: p["figure_id"]):
        by_src[p["source_id"]].append(p)
    first, rest = [], []
    for src in sorted(by_src):
        items = by_src[src][:]
        rng.shuffle(items)
        first += items[:per_source]
        rest += items[per_source:]
    rng.shuffle(first)
    rng.shuffle(rest)
    order = first + rest
    for i, p in enumerate(order):
        p["rank"] = i + 1
        p["role"] = "SELECTED" if i < n else "REPLACEMENT"
    return order


# --------------------------------------------------------------------------------------------- helpers
def render_crop(pdf: Path, page_no: int, out_png: Path) -> dict:
    import pymupdf

    doc = pymupdf.open(str(pdf))
    page = doc[page_no - 1]
    pix = page.get_pixmap(dpi=DPI, alpha=False, colorspace=pymupdf.csRGB)
    pix.save(str(out_png))
    cb = page.cropbox
    return {"cropbox_pt": [float(cb.x0), float(cb.y0), float(cb.x1), float(cb.y1)], "rotation": page.rotation,
            "px": [pix.width, pix.height], "dpi": DPI}


def to_px(x_pt, y_pt, origin) -> tuple[float, float]:
    k = DPI / 72.0
    return (x_pt - origin[0]) * k, (y_pt - origin[1]) * k


def image_size(head: bytes) -> tuple[int, int]:
    import struct

    return struct.unpack(">II", head[16:24])


def calib_labels(cal: dict, ax: str) -> list[str]:
    a = cal.get(ax) or {}
    labs = [(float(l["pos"]), l["text"]) for l in a.get("labels", [])]
    labs.sort()
    return [t for _, t in labs] + list(a.get("dropped_labels") or [])


def vector_truth(key: str, fid: str, rows: list[dict], origin, img_wh, manual: dict) -> dict:
    """Series of route A with both axes; a vertex is kept when it is drawn inside the image and inside the calibrated
    plot box grown by BOX_GROW (vertices of partly clipped paths far outside the plot are invisible); the manual file
    may narrow the visible value range (``visible_values``) or drop series (``drop_series``) after the QC overlay.
    ``origin``: page point (x, y; top-left frame of the source page) of image pixel (0, 0)."""
    ok = [s for s in rows if both_axes(s) and s["n_points"] >= 3]
    cal = json.loads(ok[0]["calibration"])
    x0, y0, x1, y1 = cal["plot_box"]
    gx, gy = BOX_GROW * (x1 - x0), BOX_GROW * (y1 - y0)
    vis = manual.get("visible_values") or {}
    drop_series = set(manual.get("drop_series") or [])
    fix = (manual.get("label_fix") or {})
    series = []
    for s in sorted(ok, key=lambda s: s["series_index"]):
        if s["series_id"] in drop_series:
            continue
        pts, dropped = [], 0
        for p in s["points"]:
            if p["x"] is None or p["y"] is None:
                continue
            px, py = to_px(p["x_drawing"], p["y_drawing"], origin)
            inside = (x0 - gx <= p["x_drawing"] <= x1 + gx and y0 - gy <= p["y_drawing"] <= y1 + gy and
                      0 <= px <= img_wh[0] and 0 <= py <= img_wh[1])
            for ax, v in (("x", p["x"]), ("y", p["y"])):
                lo, hi = (vis.get(ax) or [None, None])
                if (lo is not None and v < lo) or (hi is not None and v > hi):
                    inside = False
            if not inside:
                dropped += 1
                continue
            pts.append({"x": p["x"], "x_date": p.get("x_date"), "y": p["y"], "x_err": p["x_err"], "y_err": p["y_err"],
                        "px": px, "py": py})
        lab = s["series_label_raw"]
        lab = fix.get(lab, lab)
        series.append({"series_id": s["series_id"], "label": lab, "color": s["series_color"], "points": pts,
                       "n_outside_plot_box": dropped, "x_kind": s["x_axis_kind"], "y_kind": s["y_axis_kind"]})
    xa, ya = cal.get("x") or {}, cal.get("y") or {}
    return {
        "key": key, "figure_id": fid, "truth_kind": "ROUTE_A_VECTOR_VERTICES",
        "x_kind": xa.get("kind"), "y_kind": ya.get("kind"), "x_is_time": bool(ok[0]["x_is_time"]),
        "series": [s for s in series if len(s["points"]) >= 2],
        "series_outside_plot": [s["series_id"] for s in series if len(s["points"]) < 2],
        "ticks": {"x": manual.get("x_ticks") or calib_labels(cal, "x"), "y": manual.get("y_ticks") or calib_labels(cal, "y")},
        "ticks_origin": {"x": "MANUAL" if manual.get("x_ticks") else "ROUTE_A_CALIBRATION",
                         "y": "MANUAL" if manual.get("y_ticks") else "ROUTE_A_CALIBRATION"},
        "titles": {"x": manual.get("x_title", ok[0]["x_title_raw"]), "y": manual.get("y_title", ok[0]["y_title_raw"])},
        "units": {"x": manual.get("x_unit", ok[0]["x_unit_raw"]), "y": manual.get("y_unit", ok[0]["y_unit_raw"])},
        "legend": manual.get("legend"),
        "legend_only": manual.get("legend_only", []),
        "axis_range": axis_range(cal),
        "plot_box_px": [*to_px(x0, y0, origin), *to_px(x1, y1, origin)],
        "visible_box_px": [*to_px(x0 - gx, y0 - gy, origin), *to_px(x1 + gx, y1 + gy, origin)],
        "qc": manual.get("qc"),
        "calibration_rms": {"x": xa.get("residual_rms_drawing_units"), "y": ya.get("residual_rms_drawing_units")},
    }


def axis_range(cal: dict) -> dict:
    out = {}
    for ax in ("x", "y"):
        a = cal.get(ax) or {}
        v = [l["value"] for l in a.get("labels", []) if l.get("value") is not None]
        out[ax] = [min(v), max(v)] if v else None
    return out


def draw_overlay(png: Path, truth: dict, out: Path) -> None:
    import cv2

    img = cv2.imread(str(png))
    palette = [(255, 0, 255), (0, 160, 0), (255, 128, 0), (0, 0, 255), (0, 200, 255), (128, 0, 128), (0, 128, 128)]
    for si, s in enumerate(truth["series"]):
        col = palette[si % len(palette)]
        for p in s["points"]:
            if p.get("px") is None:
                continue
            cv2.circle(img, (int(round(p["px"])), int(round(p["py"]))), 3, col, 1)
        if s["points"]:
            p = s["points"][len(s["points"]) // 2]
            cv2.putText(img, f"T{si}", (int(p["px"]) + 4, int(p["py"]) - 4), cv2.FONT_HERSHEY_SIMPLEX, 0.45, col, 1)
    if truth.get("plot_box_px"):
        a, b, c, d = (int(round(v)) for v in truth["plot_box_px"])
        cv2.rectangle(img, (a, b), (c, d), (0, 0, 255), 1)
    cv2.imwrite(str(out), img)


# --------------------------------------------------------------------------------------------- build
def build(args) -> None:
    out = Path(args.out)
    for sub in ("inputs", "truth", "qc"):
        (out / sub).mkdir(parents=True, exist_ok=True)
    reg = json.loads(Path(args.registry).read_text(encoding="utf-8"))
    manual = json.loads(Path(args.manual).read_text(encoding="utf-8"))
    by_fig = load_rows(Path(args.sweep) / "figure_series" / "figure_series_v0.parquet")
    batches = json.loads((Path(args.sweep) / "batches" / "manifest.json").read_text(encoding="utf-8"))
    where = {p["figure_id"]: (b["batch_pdf"], p["page"]) for b in batches for p in b["pages"]}
    crop_box = {p["figure_id"]: p["crop"]["box_unrotated_pt"] for b in batches for p in b["pages"]}
    manifest = {"dpi": DPI, "box_grow": BOX_GROW, "figures": {}}
    for f in reg["figures"]:
        key, fam = f["key"], f["family"]
        m = manual.get(key, {})
        png = out / "inputs" / f"{key}.png"
        rec = {"family": fam}
        if fam == "S":
            src = Path(args.showcase) / f"{f['showcase_tag']}_a_original.png"
            shutil.copyfile(src, png)
            import pymupdf

            pg = pymupdf.open(str(Path(args.crops) / f["crop_pdf"]))[0]
            cb, mb = pg.cropbox, pg.mediabox
            if abs(mb.x0) > 1e-6 or abs(mb.y0) > 1e-6:
                raise ValueError(f"{key}: media box with an offset origin; origin mapping not verified")
            with open(png, "rb") as fh:
                wh = image_size(fh.read(24))
            truth = vector_truth(key, f["figure_id"], by_fig[f["figure_id"]], (float(cb.x0), float(cb.y0)), wh, m)
            rec["image_origin"] = "FD showcase render of the crop PDF, 200 dpi"
        elif fam == "X":
            pdf, page = where[f["figure_id"]]
            info = render_crop(Path(args.sweep) / "batches" / pdf, page, png)
            box = crop_box[f["figure_id"]]     # figure box in the frame of the source page = frame of route A
            truth = vector_truth(key, f["figure_id"], by_fig[f["figure_id"]], (box[0], box[1]), info["px"], m)
            rec["image_origin"] = f"render of the FD sweep crop ({pdf} p.{page}), {DPI} dpi"
        elif fam == "R":
            src = Path(args.showcase) / f"{f['showcase_tag']}_a_original.png"
            if f.get("mask_px"):
                import cv2

                img = cv2.imread(str(src))
                a, b, c, d = f["mask_px"]
                img[b:d + 1, a:c + 1] = 255
                cv2.imwrite(str(png), img)
                rec["image_origin"] = "FD showcase region; printed data-table value row masked white"
            else:
                shutil.copyfile(src, png)
                rec["image_origin"] = "FD showcase region"
            truth = raster_truth(key, f, m, args)
        else:
            raise ValueError(fam)
        tpath = out / "truth" / f"{key}.json"
        tpath.write_text(json.dumps(truth, ensure_ascii=False, indent=1), encoding="utf-8")
        if truth.get("series") and any(p.get("px") is not None for s in truth["series"] for p in s["points"]):
            draw_overlay(png, truth, out / "qc" / f"{key}_truth.png")
        rec.update({"input": f"inputs/{key}.png", "input_sha256": sha256(png), "truth": f"truth/{key}.json",
                    "truth_sha256": sha256(tpath), "n_series": len(truth.get("series") or []),
                    "n_points": sum(len(s["points"]) for s in truth.get("series") or [])})
        manifest["figures"][key] = rec
        print(key, rec["n_series"], rec["n_points"], rec["input_sha256"][:12])
    (out / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=1), encoding="utf-8")


def raster_truth(key: str, f: dict, m: dict, args) -> dict:
    t = {"key": key, "figure_id": None, "source_id": f["source_id"], "page": f["page"],
         "truth_kind": f["truth_kind"], "ticks": {"x": m.get("x_ticks"), "y": m.get("y_ticks")},
         "ticks_origin": {"x": "MANUAL", "y": "MANUAL"},
         "titles": {"x": m.get("x_title"), "y": m.get("y_title")}, "units": {"x": m.get("x_unit"), "y": m.get("y_unit")},
         "legend": m.get("legend"), "legend_only": [], "x_kind": m.get("x_kind"), "y_kind": "LINEAR",
         "x_is_time": False, "series_count": m.get("series_count"), "series": []}
    yt = [float(v.replace("−", "-").replace("–", "-")) for v in m.get("y_ticks") or []]
    t["axis_range"] = {"x": None, "y": [min(yt), max(yt)] if yt else None}
    if f["truth_kind"] == "PRINTED_DATA_TABLE":
        cats = m["categories"]
        t["x_categories"] = cats
        t["x_category_px"] = m.get("category_px")      # column centres of the printed table (route R mapping)
        t["axis_range"]["x"] = [0, len(cats) - 1]
        t["series"] = [{"label": m["table_series_label"], "color": None,
                        "points": [{"x": i, "x_category": c, "y": float(v)} for i, (c, v) in
                                   enumerate(zip(cats, m["table_values"]))]}]
    elif f["truth_kind"] == "PUBLIC_EVIDENCE_MANUAL_DIGITIZATION":
        import csv

        rows = [r for r in csv.DictReader(open(Path(args.evidence), encoding="utf-8"))
                if r["line"] == f["line"] and r["value_mm"]]
        t["x_positions"] = m["x_positions"]          # printed label → category position
        t["axis_range"]["x"] = [1, 31]
        for meth in ("levelling", "insar"):
            pts = [{"x": int(r["position_index"]), "y": float(r["value_mm"]),
                    "y_err": float(r["digitization_uncertainty_mm"] or 0), "status": r["digitization_status"]}
                   for r in rows if r["method"] == meth]
            t["series"].append({"label": meth, "color": m["colors"][meth], "points": pts})
    return t


def main() -> None:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("select")
    s.add_argument("--sweep", required=True)
    s.add_argument("--showcase-ids", required=True, help="comma-separated figure ids")
    b = sub.add_parser("build")
    for a in ("--registry", "--manual", "--sweep", "--showcase", "--crops", "--evidence", "--out"):
        b.add_argument(a, required=True)
    args = ap.parse_args()
    if args.cmd == "select":
        order = select(Path(args.sweep), set(args.showcase_ids.split(",")))
        print(json.dumps(order, ensure_ascii=False, indent=1))
    else:
        build(args)


if __name__ == "__main__":
    main()
