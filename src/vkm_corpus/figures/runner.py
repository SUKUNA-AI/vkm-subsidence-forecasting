"""Batch runner: candidates → single-figure pages → route B (AutoCAD, the DXF comes from ``vkm-cad``
``cad_pdf_import``) and route A (native vectors) → ``figure_series`` rows and a route comparison.

Paths are always passed in (no machine paths here). The AutoCAD step is not called from this module: the batch PDFs
it writes are imported with the ``vkm-cad`` MCP tool (one call per batch of ≤ 20 pages, console only, decision
CP-43), and the job's DXF files are handed back to :func:`digitize_route_b`.
"""
from __future__ import annotations

import json
import math
import time
from pathlib import Path as FsPath

import numpy as np

from vkm_corpus.figures import core, dxf_route, labels_ocr, pdf_route
from vkm_corpus.figures.crop import CropInfo, crop_figure_pdf
from vkm_corpus.figures.dataset import FigureContext, build_rows

QUANTUM_PT = 0.05          # coordinate precision assumed for native PDF vectors (points)
CONFIG = {"quantum_pt": QUANTUM_PT, "crop_margin_pt": 12.0, "ocr_psm": 7, "ocr_dpi": 600, "legend_max_len": 0.2,
          "plot_box_overhang": 0.5, "min_chain_frac": 0.04}


def select(cands: list[dict]) -> list[dict]:
    """Chart-like vector figures with a subsidence/trough/benchmark/convergence/levelling/displacement keyword in the
    caption or on the page; unrotated pages (the DXF frame of a rotated page is not handled in v0)."""
    return [c for c in cands if c.get("chartlike") and c.get("core_keyword") and not c.get("rotation")]


def context(c: dict) -> FigureContext:
    return FigureContext(figure_id=c["figure_id"], source_id=c["source_id"], page_id=c["page_id"],
                         work_id=c.get("work_id"), figure_label=c.get("figure_label"),
                         caption_block_id=c.get("caption_block_id"),
                         caption_text_for_hints="прогноз" if c.get("model_hint_caption") else None,
                         source_sha256=c.get("source_sha256"), publication_year=c.get("publication_year"),
                         available_from=c.get("available_latest_day"),
                         available_basis=(c.get("available_basis") or "ASSUMED_FROM_PUBLICATION")
                         if (c.get("available_latest_day") or c.get("publication_year")) else "UNKNOWN",
                         site_scope_raw=c.get("site_scope_raw"))


# ------------------------------------------------------------------------------------------------ route B
def make_batches(cands: list[dict], pdf_of, out_dir, per_pdf: int = 20, margin: float = 12.0) -> list[dict]:
    """Single-figure pages (CropBox = figure box) gathered into batch PDFs of ≤ ``per_pdf`` pages."""
    import pymupdf

    out_dir = FsPath(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    manifest = []
    failed = []
    for b in range(0, len(cands), per_pdf):
        chunk = cands[b:b + per_pdf]
        batch = pymupdf.open()
        pages = []
        for c in chunk:
            tmp = out_dir / "_one.pdf"
            try:
                info = crop_figure_pdf(pdf_of(c), c["page_index"], c["bbox"], tmp, margin=margin)
            except Exception as exc:  # noqa: BLE001 — recorded, the figure stays for route A
                failed.append({"figure_id": c["figure_id"], "error": f"{type(exc).__name__}: {exc}"[:200]})
                continue
            one = pymupdf.open(tmp)
            batch.insert_pdf(one)
            one.close()
            pages.append({"page": len(pages) + 1, "figure_id": c["figure_id"], "crop": info.to_json()})
        if not pages:
            continue
        name = out_dir / f"batch_{b // per_pdf + 1:03d}.pdf"
        batch.save(name, garbage=1)
        (out_dir / "_one.pdf").unlink(missing_ok=True)
        manifest.append({"batch_pdf": name.name, "pages": pages})
    (out_dir / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=1), encoding="utf-8")
    (out_dir / "crop_failures.json").write_text(json.dumps(failed, ensure_ascii=False, indent=1), encoding="utf-8")
    return manifest


def _crop_from_json(j: dict) -> CropInfo:
    return CropInfo(tuple(j["box_unrotated_pt"]), j["page_rotation"], j["page_height_pt"], j["chars_before"],
                    j["chars_after"], j["chars_outside_after"], j.get("mode", "cropbox"))


def digitize_route_b(c: dict, crop_json: dict, batch_pdf, page_no: int, dxf_path, source_pdf,
                     engine: labels_ocr.OcrEngine) -> dict:
    import pymupdf

    t0 = time.time()
    crop = _crop_from_json(crop_json)
    x0, y0, x1, y1 = crop.box_unrotated_pt
    region = (0.0, 0.0, (x1 - x0) / 72.0, (y1 - y0) / 72.0)
    texts, paths = dxf_route.load_dxf(dxf_path)
    tt, pp = dxf_route.clip(texts, paths, region)
    tt, refine = dxf_route.refine_texts(tt, dxf_route.pdf_words(batch_pdf, page_no - 1))
    page = pymupdf.open(str(source_pdf))[c["page_index"] - 1]
    reader = labels_ocr.make_glyph_reader(page, crop.dxf_to_page, engine, max_size=0.25, default_h=0.08)
    res = core.digitize(tt, pp, region, quantum=QUANTUM_PT / 72.0, glyph_reader=reader)
    res.update({"elapsed_s": round(time.time() - t0, 3), "text_refine": refine, "crop": crop.to_json(),
                "n_texts": len(tt), "n_paths": len(pp)})
    return res


# ------------------------------------------------------------------------------------------------ route A
def digitize_route_a(c: dict, source_pdf, engine: labels_ocr.OcrEngine, margin: float = 12.0) -> dict:
    import pymupdf

    t0 = time.time()
    page = pymupdf.open(str(source_pdf))[c["page_index"] - 1]
    bx0, by0, bx1, by1 = c["bbox"]
    region = (bx0 - margin, by0 - margin, bx1 + margin, by1 + margin)
    texts, paths = pdf_route.load_page(page, region)
    reader = labels_ocr.make_glyph_reader(page, lambda X, Y: (X, Y), engine, max_size=18.0, default_h=6.0,
                                          y_down=True)
    res = core.digitize(texts, paths, region, quantum=QUANTUM_PT, glyph_reader=reader)
    res.update({"elapsed_s": round(time.time() - t0, 3), "n_texts": len(texts), "n_paths": len(paths)})
    return res


# ------------------------------------------------------------------------------------------------ comparison
def compare(res_a: dict, res_b: dict) -> dict:
    """Route agreement on one figure: axes, series counts and — for series matched by label or colour — the median
    and maximum point distance (value units of each axis, A points nearest to B points)."""
    out = {"axis_a": res_a.get("axis_status"), "axis_b": res_b.get("axis_status"),
           "series_a": len(res_a.get("series", [])), "series_b": len(res_b.get("series", [])),
           "points_a": sum(len(s["points"]) for s in res_a.get("series", [])),
           "points_b": sum(len(s["points"]) for s in res_b.get("series", []))}
    pairs = []
    used = set()
    allp = np.array([(p["x"], p["y"]) for r in (res_a, res_b) for s in r.get("series", []) for p in s["points"]
                     if p["x"] is not None and p["y"] is not None], float)
    span = np.maximum(np.ptp(allp, 0), 1e-9) if len(allp) else np.ones(2)
    for sb in res_b.get("series", []):
        best = None
        for ia, sa in enumerate(res_a.get("series", [])):
            if ia in used:
                continue
            same = (sb.get("label_raw") and sb.get("label_raw") == sa.get("label_raw")) or \
                (sb.get("color") == sa.get("color"))
            if same and (best is None or abs(len(sa["points"]) - len(sb["points"])) < best[0]):
                best = (abs(len(sa["points"]) - len(sb["points"])), ia)
        if best is None:
            continue
        used.add(best[1])
        sa = res_a["series"][best[1]]
        pa = np.array([(p["x"], p["y"]) for p in sa["points"] if p["x"] is not None and p["y"] is not None], float)
        pb = np.array([(p["x"], p["y"]) for p in sb["points"] if p["x"] is not None and p["y"] is not None], float)
        if len(pa) == 0 or len(pb) == 0:
            continue
        d = np.sqrt((((pb[:, None, :] - pa[None, :, :]) / span) ** 2).sum(-1)).min(1)
        pairs.append({"label": sb.get("label_raw"), "n_a": len(pa), "n_b": len(pb),
                      "median_rel": float(np.median(d)), "max_rel": float(np.max(d))})
    out["matched_series"] = pairs
    out["max_rel_diff"] = max((p["max_rel"] for p in pairs), default=None)
    return out


def result_rows(c: dict, res: dict, route: str, provenance: dict) -> list[dict]:
    return build_rows(context(c), res, route, provenance, CONFIG)


def jsonable(res: dict) -> dict:
    """A digitization result without numpy arrays and axis objects (for logs)."""
    out = {k: v for k, v in res.items() if k not in ("x_axis", "y_axis", "series")}
    out["x_axis"] = res["x_axis"].to_json() if res.get("x_axis") is not None else None
    out["y_axis"] = res["y_axis"].to_json() if res.get("y_axis") is not None else None
    # a series read on its own panel's y scale (fd-0.1.5, Y_AXIS_PER_SERIES) carries that axis
    out["series"] = [{k: v for k, v in s.items() if k not in ("points", "pts", "trace_px", "y_axis")} |
                     {"n_points": len(s["points"])} |
                     ({"y_axis": s["y_axis"].to_json()} if s.get("y_axis") is not None else {})
                     for s in res.get("series", [])]
    return json.loads(json.dumps(out, default=lambda o: o.tolist() if hasattr(o, "tolist") else str(o)))


def finite(v) -> bool:
    return v is not None and not (isinstance(v, float) and (math.isnan(v) or math.isinf(v)))
