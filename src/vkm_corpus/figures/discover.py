"""Candidate plots in the canonical DuckDB (read only): figures with native vectors, keyword hits in the caption or
on the page (subsidence, trough, profile line, benchmark, convergence, levelling, displacement…), numeric tokens
near the figure (tick labels) and path statistics from ``VECTOR_PATHS_JSON``. Output: ids, counts and keyword names
only — no corpus text."""
from __future__ import annotations

import gzip
import json
import re
import zlib
from collections import Counter
from pathlib import Path

KW = {
    "subsidence": r"оседан|осадк|опускан|subsid|settlement",
    "trough": r"мульд|trough",
    "profile_line": r"профильн\w* лини|профил[ья] |по профил|profile line",
    "benchmark": r"репер|марк[аи] |benchmark",
    "convergence": r"конвергенц|convergence",
    "time": r"во времени|врем[яени]|сутк|годы|лет |time series|по годам|динамик",
    "graph": r"график|графики|кривы|зависимост|chart|plot",
    "displacement": r"смещени|сдвижени|перемещени|displacement|деформац|deformation",
    "levelling": r"нивелир|levelling|leveling",
    "velocity": r"скорост|velocity|rate",
    "insar": r"insar|интерферометр|радар|рса|sar\b|ps-|sbas",
}
CORE = frozenset({"subsidence", "trough", "profile_line", "benchmark", "convergence", "levelling", "displacement"})
KW_RE = {k: re.compile(v, re.I) for k, v in KW.items()}
NUM_TOKEN = re.compile(r"(?<![\w.,])[-−–]?\d+(?:[.,]\d+)?(?![\w])")


def _load_json_artifact(path: Path):
    raw = path.read_bytes()
    for dec in (gzip.decompress, zlib.decompress, lambda b: b):
        try:
            return json.loads(dec(raw))
        except Exception:  # noqa: BLE001
            continue
    return None


def _vstats(doc) -> dict:
    st: Counter = Counter()
    colours = set()
    for p in doc.get("paths", []):
        st["paths"] += 1
        for it in p.get("items", []):
            st["op_" + it[0]] += 1
        col = p.get("color")
        if p.get("type") in ("s", "fs") and col:
            r, g, b = col[:3]
            if max(r, g, b) - min(r, g, b) > 0.08:
                colours.add(tuple(round(c, 2) for c in col[:3]))
                st["coloured_stroke_paths"] += 1
    st["distinct_stroke_colours"] = len(colours)
    return dict(st)


def discover(duckdb_path, artifacts_root, margin: float = 15.0) -> list[dict]:
    import duckdb

    con = duckdb.connect(str(duckdb_path), read_only=True)
    figs = con.execute("""
        select f.object_id, f.source_id, f.page_id, f.layout_class, f.caption_normalized, f.figure_label,
               f.caption_block_id, f.bbox_x0, f.bbox_y0, f.bbox_x1, f.bbox_y1, f.vector_artifacts,
               f.embedded_image_artifact_id, p.page_index, p.width_pt, p.height_pt, p.rotation_deg
        from figures f join pages p on p.page_id = f.page_id where len(f.vector_artifacts) > 0""").fetchall()
    pages = {r[2] for r in figs}
    page_text = dict(con.execute("select page_id, normalized_text from pages where page_id in (select unnest(?))",
                                 [sorted(pages)]).fetchall())
    blocks: dict[str, list] = {}
    for pid, x0, y0, x1, y1, txt in con.execute(
            "select page_id, bbox_x0, bbox_y0, bbox_x1, bbox_y1, text from blocks where is_primary_layer "
            "and bbox_space = 'PAGE_PT_TL' and page_id in (select unnest(?))", [sorted(pages)]).fetchall():
        blocks.setdefault(pid, []).append((x0, y0, x1, y1, txt or ""))
    arts = dict(con.execute("select artifact_id, storage_relpath from artifacts where artifact_kind = "
                            "'VECTOR_PATHS_JSON'").fetchall())
    meta = {sid: {"work_id": wid, "publication_year": year, "available_latest_day": str(day) if day else None,
                  "available_basis": basis}
            for sid, wid, year, day, basis in con.execute(
                "select ws.source_id, ws.work_id, wa.publication_year, wa.available_latest_day, wa.available_basis "
                "from work_sources ws left join works_availability wa using (work_id) where ws.is_primary").fetchall()}
    src = {sid: (path, scope, sha) for sid, path, scope, sha in con.execute(
        "select source_id, canonical_path, site_scope_raw, source_sha256 from sources").fetchall()}
    root = Path(artifacts_root)
    out = []
    for (fid, sid, pid, lclass, cap, flabel, capblock, x0, y0, x1, y1, vecs, emb, pindex, w, h, rot) in figs:
        cap = cap or ""
        text = page_text.get(pid) or ""
        kw_cap = sorted(k for k, rx in KW_RE.items() if rx.search(cap))
        kw_page = sorted(k for k, rx in KW_RE.items() if rx.search(text))
        nums = sum(len(NUM_TOKEN.findall(t)) for bx0, by0, bx1, by1, t in blocks.get(pid, [])
                   if x0 - margin <= (bx0 + bx1) / 2 <= x1 + margin and y0 - margin <= (by0 + by1) / 2 <= y1 + margin)
        pj = next((v["artifact_id"] for v in vecs if v["format"] == "PATHS_JSON"), None)
        vs = {}
        if pj and arts.get(pj):
            p = root / arts[pj].split("artifacts/", 1)[-1]
            if p.exists():
                doc = _load_json_artifact(p)
                vs = _vstats(doc) if doc else {}
        score = 2.0 * len(set(kw_cap) & CORE) + 1.0 * len(set(kw_cap) & {"time", "graph", "velocity", "insar"}) \
            + 0.5 * len(set(kw_page) & (CORE - {"displacement"}))
        path, scope, sha = src.get(sid, (None, None, None))
        out.append({"figure_id": fid, "source_id": sid, "page_id": pid, "page_index": pindex,
                    "layout_class": lclass, "figure_label": flabel, "caption_block_id": capblock,
                    "bbox": [round(v, 2) for v in (x0, y0, x1, y1)], "page_w": w, "page_h": h, "rotation": rot,
                    "has_embedded_raster": bool(emb), "kw_caption": kw_cap, "kw_page": kw_page,
                    "numeric_tokens_near": nums, "vector": vs,
                    "chartlike": nums >= 6 and vs.get("op_l", 0) + vs.get("op_c", 0) >= 20,
                    "core_keyword": bool((set(kw_cap) | set(kw_page)) & CORE), "score": round(score, 2),
                    "model_hint_caption": bool(re.search(r"расч[её]т|прогноз|модел|мкэ", cap, re.I)),
                    "source_path_logical": path, "source_sha256": sha, "site_scope_raw": scope,
                    **meta.get(sid, {})})
    return out
