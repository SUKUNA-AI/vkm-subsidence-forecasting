"""FIGURE_READINGS_V1 — render figure regions of corpus PDFs at a fixed resolution (default 200 dpi), offline.

Input: a JSON list of regions {key, source_id, page_index (1-based corpus page index), bbox [x0, y0, x1, y1] in PDF
points with a top-left origin (the corpus PAGE_PT_TL box) or null for the whole page, object_ids, corpus_caption}.
The PDF is found through the PRIVATE register (00_registry/SOURCE_REGISTER.csv, canonical_path); its sha256 is
checked against the register before rendering. Output: <work>/images/<key>.png and a manifest in the format of
extract_docx_images.py (so read_figures.py jobs can take it). Captions come from the corpus layer and stay local.

    python render_pdf_regions.py --regions regions.json --resources <PRIVATE clone> --out <work dir>
           [--dpi 200] [--margin 6] [--prefix C]
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
from pathlib import Path


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--regions", required=True)
    ap.add_argument("--resources", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--dpi", type=int, default=200)
    ap.add_argument("--margin", type=float, default=6.0)
    ap.add_argument("--manifest-name", default="manifest_corpus.json")
    args = ap.parse_args()
    import pypdfium2 as pdfium

    res = Path(args.resources)
    reg = {r["resource_id"]: r for r in csv.DictReader(open(res / "00_registry" / "SOURCE_REGISTER.csv", encoding="utf-8"))}
    regions = json.loads(Path(args.regions).read_text(encoding="utf-8"))
    out = Path(args.out)
    (out / "images").mkdir(parents=True, exist_ok=True)
    docs, checked, records = {}, {}, []
    scale = args.dpi / 72.0
    for rg in regions:
        sid = rg["source_id"]
        row = reg[sid]
        pdf = res / row["canonical_path"]
        if sid not in checked:
            h = hashlib.sha256(pdf.read_bytes()).hexdigest()
            checked[sid] = h
            if h != row["sha256"]:
                raise SystemExit(f"{sid}: sha256 differs from the register")
            docs[sid] = pdfium.PdfDocument(str(pdf))
        page = docs[sid][rg["page_index"] - 1]
        w, h = page.get_size()
        if rg.get("bbox"):
            x0, y0, x1, y1 = rg["bbox"]
            m = args.margin
            x0, y0, x1, y1 = max(0, x0 - m), max(0, y0 - m), min(w, x1 + m), min(h, y1 + m)
            crop = (x0, h - y1, w - x1, y0)          # left, bottom, right, top margins cut (points)
        else:
            x0, y0, x1, y1 = 0, 0, w, h
            crop = (0, 0, 0, 0)
        img = page.render(scale=scale, crop=crop, fill_color=(255, 255, 255, 255)).to_pil().convert("RGB")
        fn = f"{rg['key']}.png"
        img.save(out / "images" / fn, format="PNG")
        data = (out / "images" / fn).read_bytes()
        records.append({
            "key": rg["key"], "source_id": sid, "container": Path(row["canonical_path"]).name,
            "container_sha256": row["sha256"], "file": f"images/{fn}", "format": "png",
            "sha256": hashlib.sha256(data).hexdigest(), "bytes": len(data),
            "locator": {"page_id": f"{sid}:p{rg['page_index']:04d}", "page_index": rg["page_index"],
                        "bbox_pt_tl": [round(v, 2) for v in (x0, y0, x1, y1)], "object_ids": rg.get("object_ids", []),
                        "dpi": args.dpi, "renderer": "pypdfium2", "item": rg.get("item")},
            "caption": rg.get("corpus_caption"), "caption_kind": "corpus_caption_layer",
            "context_paragraph": rg.get("note", ""),
        })
        print(rg["key"], img.width, "x", img.height)
    man = {"source_id": "corpus", "container": "corpus PDFs", "n_occurrences": len(records), "images": records}
    (out / args.manifest_name).write_text(json.dumps(man, ensure_ascii=False, indent=1), encoding="utf-8")


if __name__ == "__main__":
    main()
