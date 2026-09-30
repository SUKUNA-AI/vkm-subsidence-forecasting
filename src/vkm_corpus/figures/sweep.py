"""Sweep driver of the prototype: ``python -m vkm_corpus.figures.sweep <step> …`` (all machine paths are arguments).

Steps:

* ``discover`` — candidate plots from a read-only DuckDB copy of the canon and the ``VECTOR_PATHS_JSON`` artifacts;
* ``prepare`` — single-figure pages (CropBox = figure box) in batch PDFs of ≤ 20 pages and ``manifest.json``;
* (outside this module) each batch PDF is imported with ``vkm-cad`` ``cad_pdf_import`` (console only, CP-43); a
  ``jobs.json`` maps the batch file name to ``{"job_id": …, "out_dir": <the job's out directory>}``;
* ``digitize`` — routes A and B for every selected candidate → ``figure_series_v0.{jsonl,parquet}``,
  ``sweep_log.json`` (per figure: axis status, series, seconds, route comparison) and ``summary.json``;
* ``raster`` — route R over the raster figures of listed pages (``--pages-json``: ``{"VKM-SRC-…": [page, …]}``) →
  ``figure_series_raster_v0.{jsonl,parquet}`` and ``raster_log.json``;
* ``publish`` — copy the datasets next to the NAV build outputs with a ``manifest.json`` (WORKSTATION only).
"""
from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import time
from pathlib import Path

from vkm_corpus.figures import DIGITIZER_VERSION, REVIEW_STATUS, STATUS, dataset, discover, labels_ocr, runner


def cmd_discover(a) -> dict:
    cands = discover.discover(a.db, a.artifacts)
    Path(a.out).write_text(json.dumps(cands, ensure_ascii=False), encoding="utf-8")
    sel = runner.select(cands)
    return {"vector_figures": len(cands), "chartlike": sum(bool(c["chartlike"]) for c in cands),
            "selected": len(sel), "sources": len({c["source_id"] for c in sel}),
            "pages": len({c["page_id"] for c in sel})}


def cmd_prepare(a) -> dict:
    cands = runner.select(json.loads(Path(a.cands).read_text(encoding="utf-8")))
    root = Path(a.private_root)
    man = runner.make_batches(cands, lambda c: root / c["source_path_logical"], a.out_dir, per_pdf=a.per_pdf)
    return {"batches": len(man), "pages": sum(len(m["pages"]) for m in man)}


def cmd_digitize(a) -> dict:
    cands = {c["figure_id"]: c for c in runner.select(json.loads(Path(a.cands).read_text(encoding="utf-8")))}
    root = Path(a.private_root)
    eng = labels_ocr.OcrEngine()
    eng.available()
    out = Path(a.out_dir)
    out.mkdir(parents=True, exist_ok=True)
    manifest = json.loads((Path(a.batches_dir) / "manifest.json").read_text(encoding="utf-8")) if a.batches_dir else []
    jobs = json.loads(Path(a.jobs).read_text(encoding="utf-8")) if a.jobs else {}
    page_of = {p["figure_id"]: (m["batch_pdf"], p["page"], p["crop"]) for m in manifest for p in m["pages"]}
    rows_a, rows_b, log = [], [], []
    t_all = time.time()
    for fid, c in cands.items():
        src = root / c["source_path_logical"]
        rec = {"figure_id": fid, "source_id": c["source_id"]}
        ra = None
        try:
            ra = runner.digitize_route_a(c, src, eng)
            prov = {"route": "A", "source_sha256": c.get("source_sha256"), "vector_source": "PyMuPDF get_drawings"}
            rows_a += runner.result_rows(c, ra, "A_NATIVE_VECTOR", prov)
            rec["A"] = {"axis": ra["axis_status"], "series": len(ra["series"]), "s": ra["elapsed_s"]}
        except Exception as e:  # noqa: BLE001 — recorded per figure
            rec["A"] = {"error": f"{type(e).__name__}: {e}"[:200]}
        if fid in page_of and page_of[fid][0] in jobs:
            batch_pdf, page_no, crop = page_of[fid]
            job = jobs[batch_pdf]
            dxf = Path(job["out_dir"]) / f"{Path(batch_pdf).stem}_p{page_no:03d}.dxf"
            try:
                rb = runner.digitize_route_b(c, crop, Path(a.batches_dir) / batch_pdf, page_no, dxf, src, eng)
                prov = {"route": "B", "source_sha256": c.get("source_sha256"), "cad_job": job["job_id"],
                        "batch_pdf": batch_pdf, "batch_page": page_no,
                        "dxf_sha256": hashlib.sha256(dxf.read_bytes()).hexdigest(), "crop": crop,
                        "text_refine": rb.get("text_refine")}
                rows_b += runner.result_rows(c, rb, "B_AUTOCAD_PDFIMPORT", prov)
                rec["B"] = {"axis": rb["axis_status"], "series": len(rb["series"]), "s": rb["elapsed_s"]}
                if ra is not None:
                    rec["cmp"] = runner.compare(ra, rb)
            except Exception as e:  # noqa: BLE001
                rec["B"] = {"error": f"{type(e).__name__}: {e}"[:200]}
        log.append(rec)
    rows = rows_a + rows_b
    sha_jsonl = dataset.write_jsonl(rows, out / "figure_series_v0.jsonl")
    dataset.write_parquet(rows, out / "figure_series_v0.parquet")
    (out / "sweep_log.json").write_text(json.dumps(log, ensure_ascii=False, indent=1, default=str), encoding="utf-8")
    summary = {"figures": len(cands), "elapsed_s": round(time.time() - t_all, 1), "jsonl_sha256": sha_jsonl,
               "A": dataset.summarize(rows_a) if rows_a else {}, "B": dataset.summarize(rows_b) if rows_b else {},
               "ocr": eng.provenance()}
    (out / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=1), encoding="utf-8")
    return summary


def cmd_raster(a) -> dict:
    import duckdb

    from vkm_corpus.figures import raster as R
    from vkm_corpus.figures.raster_digitize import digitize_raster

    root = Path(a.private_root)
    out = Path(a.out_dir)
    out.mkdir(parents=True, exist_ok=True)
    pages = json.loads(Path(a.pages_json).read_text(encoding="utf-8"))
    pids = [f"{s}:p{p:04d}" for s, ps in pages.items() for p in ps]
    con = duckdb.connect(a.db, read_only=True)
    figs = con.execute("""
        select f.object_id, f.source_id, f.page_id, p.page_index, f.bbox_x0, f.bbox_y0, f.bbox_x1, f.bbox_y1,
               f.layout_class, len(f.vector_artifacts) nv, f.figure_label, f.caption_block_id, s.canonical_path,
               s.site_scope_raw, s.source_sha256
        from figures f join pages p using (page_id) join sources s on s.source_id = f.source_id
        where f.page_id in (select unnest(?)) and f.layout_class in ('CHART', 'RASTER_IMAGE', 'MIXED')
          and (f.bbox_x1 - f.bbox_x0) > 100 and (f.bbox_y1 - f.bbox_y0) > 60""", [pids]).fetchall()
    meta = {sid: (wid, year, str(day) if day else None, basis) for sid, wid, year, day, basis in con.execute(
        "select ws.source_id, ws.work_id, wa.publication_year, wa.available_latest_day, wa.available_basis "
        "from work_sources ws left join works_availability wa using (work_id) where ws.is_primary").fetchall()}
    eng = labels_ocr.OcrEngine()
    rows, log = [], []
    t_all = time.time()
    for (fid, sid, pid, pidx, x0, y0, x1, y1, lclass, nv, flabel, capb, path, scope, sha) in figs:
        if nv and lclass != "RASTER_IMAGE":
            continue                      # vector figures belong to routes A/B
        t0 = time.time()
        try:
            fig = R.load_region(root / path, pidx, (x0, y0, x1, y1), dpi=300)
            res = digitize_raster(fig, eng, sample_markers=True)
        except Exception as e:  # noqa: BLE001
            log.append({"figure_id": fid, "error": f"{type(e).__name__}: {e}"[:200]})
            continue
        wid, year, day, basis = meta.get(sid, (None, None, None, None))
        c = {"figure_id": fid, "source_id": sid, "page_id": pid, "work_id": wid, "figure_label": flabel,
             "caption_block_id": capb, "source_sha256": sha, "publication_year": year, "available_latest_day": day,
             "available_basis": basis, "site_scope_raw": scope}
        res["x_title_raw"], res["y_title_raw"] = None, None
        prov = {"route": "R", "source_sha256": sha, "render_dpi": 300, "native_px_per_pt": fig.native_px_per_pt,
                "ocr": eng.provenance(), "qc": res["qc"]}
        rows += runner.result_rows(c, res, "R_RASTER", prov)
        log.append({"figure_id": fid, "axis": res["axis_status"], "series": len(res["series"]),
                    "points": sum(len(s["points"]) for s in res["series"]), "qc": res["qc"],
                    "x": res["x_axis"].kind if res["x_axis"] is not None else None,
                    "y": res["y_axis"].kind if res["y_axis"] is not None else None,
                    "s": round(time.time() - t0, 2)})
    sha = dataset.write_jsonl(rows, out / "figure_series_raster_v0.jsonl") if rows else None
    if rows:
        dataset.write_parquet(rows, out / "figure_series_raster_v0.parquet")
    (out / "raster_log.json").write_text(json.dumps(log, ensure_ascii=False, indent=1, default=str), encoding="utf-8")
    both = [r for r in rows if "X_UNCALIBRATED" not in r["flags"] and "Y_UNCALIBRATED" not in r["flags"]]
    return {"figures": len(log), "axis_ok": sum(1 for r in log if r.get("axis") == "OK"),
            "series": len(rows), "series_both_axes": len(both), "points": sum(r["n_points"] for r in rows),
            "jsonl_sha256": sha, "elapsed_s": round(time.time() - t_all, 1), "ocr_calls": eng.calls,
            "errors": sum(1 for r in log if "error" in r)}


def cmd_publish(a) -> dict:
    src, dst = Path(a.src), Path(a.dst)
    dst.mkdir(parents=True, exist_ok=True)
    files = {}
    for name in ("figure_series_v0.parquet", "figure_series_raster_v0.parquet", "figure_series_v0.jsonl",
                 "figure_series_raster_v0.jsonl", "summary.json"):
        p = src / name
        if p.exists():
            shutil.copyfile(p, dst / name)
            files[name] = {"bytes": p.stat().st_size, "sha256": hashlib.sha256(p.read_bytes()).hexdigest()}
    manifest = {
        "part": "figure_series", "schema": dataset.SCHEMA, "snapshot": a.snapshot,
        "generator": f"vkm_corpus.figures (agent FD prototype, {DIGITIZER_VERSION})", "files": files,
        "status": STATUS, "review_status": REVIEW_STATUS,
        "rules": ["derived navigation data, never evidence; promotion only through the evidence review",
                  "every value carries a per-point half-width error in value units",
                  "availability = publication date of the work (year-only: last day of the year); UNKNOWN stays "
                  "UNKNOWN",
                  "series_nature UNCLASSIFIED: plotted model results and observations are not told apart "
                  "automatically"],
    }
    (dst / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=1), encoding="utf-8")
    return files


def main(argv=None) -> None:
    p = argparse.ArgumentParser(prog="python -m vkm_corpus.figures.sweep")
    sp = p.add_subparsers(dest="cmd", required=True)
    d = sp.add_parser("discover")
    d.add_argument("--db", required=True)
    d.add_argument("--artifacts", required=True)
    d.add_argument("--out", required=True)
    r = sp.add_parser("prepare")
    r.add_argument("--cands", required=True)
    r.add_argument("--private-root", required=True)
    r.add_argument("--out-dir", required=True)
    r.add_argument("--per-pdf", type=int, default=20)
    g = sp.add_parser("digitize")
    g.add_argument("--cands", required=True)
    g.add_argument("--private-root", required=True)
    g.add_argument("--batches-dir")
    g.add_argument("--jobs")
    g.add_argument("--out-dir", required=True)
    s = sp.add_parser("raster")
    s.add_argument("--db", required=True)
    s.add_argument("--private-root", required=True)
    s.add_argument("--pages-json", required=True)
    s.add_argument("--out-dir", required=True)
    u = sp.add_parser("publish")
    u.add_argument("--src", required=True)
    u.add_argument("--dst", required=True)
    u.add_argument("--snapshot", required=True)
    a = p.parse_args(argv)
    fn = {"discover": cmd_discover, "prepare": cmd_prepare, "digitize": cmd_digitize, "raster": cmd_raster,
          "publish": cmd_publish}[a.cmd]
    print(json.dumps(fn(a), ensure_ascii=False, default=str))


if __name__ == "__main__":
    main()
