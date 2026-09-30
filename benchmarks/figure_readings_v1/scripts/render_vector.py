"""FIGURE_READINGS_V1 — render the EMF/WMF parts of a DOCX manifest to PNG, offline.

Step 1: the local LibreOffice container (``vkm-libreoffice:24.2``, ``--network none``) converts each EMF/WMF to PDF,
keeping the vector drawing. Docker Desktop cannot mount a non-ASCII host path, so the files are staged in an ASCII
directory (``--stage``). Step 2: pypdfium2 renders page 1 of that PDF at ``--dpi`` (the long side is capped at
``--max-side`` pixels). The PNG and its sha256 are added to the manifest record as ``rendered``; the original EMF
bytes and their sha256 stay the reference of the record.

    python render_vector.py --manifest <work>/manifest_F023.json --stage <ascii dir> [--dpi 300] [--max-side 4000]
"""
from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import subprocess
from pathlib import Path

IMAGE = "vkm-libreoffice:24.2"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--manifest", required=True)
    ap.add_argument("--stage", required=True)
    ap.add_argument("--dpi", type=int, default=300)
    ap.add_argument("--max-side", type=int, default=4000)
    args = ap.parse_args()
    import pypdfium2 as pdfium

    man_path = Path(args.manifest)
    work = man_path.parent
    man = json.loads(man_path.read_text(encoding="utf-8"))
    stage = Path(args.stage).resolve()
    stage.mkdir(parents=True, exist_ok=True)
    todo = [r for r in man["images"] if r.get("format") in ("emf", "wmf")]
    for r in todo:
        shutil.copyfile(work / r["file"], stage / Path(r["file"]).name)
    if todo:
        cmd = ["docker", "run", "--rm", "--network", "none", "-v", f"{stage}:/work", IMAGE,
               "--convert-to", "pdf", "--outdir", "/work/pdf"] + [Path(r["file"]).name for r in todo]
        proc = subprocess.run(cmd, capture_output=True, text=True)
        (work / "logs").mkdir(exist_ok=True)
        (work / "logs" / f"render_vector_{man['source_id']}.log").write_text(
            " ".join(cmd[:8]) + " ...\n" + proc.stdout + proc.stderr, encoding="utf-8")
        if proc.returncode != 0:
            raise SystemExit(f"LibreOffice exit code {proc.returncode}")
    from PIL import ImageChops, Image

    for r in todo:
        pdf = stage / "pdf" / (Path(r["file"]).stem + ".pdf")
        doc = pdfium.PdfDocument(str(pdf))
        page = doc[0]
        w_pt, h_pt = page.get_size()
        # LibreOffice places the drawing on a whole page: find the drawing's box at 72 dpi, then render only that box
        probe = page.render(scale=1.0, fill_color=(255, 255, 255, 255)).to_pil().convert("RGB")
        diff = ImageChops.difference(probe, Image.new("RGB", probe.size, (255, 255, 255))).convert("L")
        bbox = diff.point(lambda v: 255 if v > 8 else 0).getbbox() or (0, 0, probe.width, probe.height)
        pad = 4
        l, t = max(0, bbox[0] - pad), max(0, bbox[1] - pad)
        rr, b = min(probe.width, bbox[2] + pad), min(probe.height, bbox[3] + pad)
        crop_pt = (l, probe.height - b, probe.width - rr, t)  # margins cut from left, bottom, right, top (points)
        box_w, box_h = rr - l, b - t
        scale = min(args.dpi / 72.0 * 4, args.max_side / max(box_w, box_h))
        img = page.render(scale=scale, crop=crop_pt, fill_color=(255, 255, 255, 255)).to_pil().convert("RGB")
        out = work / "images" / (Path(r["file"]).stem + ".png")
        img.save(out, format="PNG")
        r["rendered"] = {"file": f"images/{out.name}", "sha256": hashlib.sha256(out.read_bytes()).hexdigest(),
                         "width": img.width, "height": img.height, "page_pt": [round(w_pt, 2), round(h_pt, 2)],
                         "drawing_box_pt": [l, t, rr, b], "scale": round(scale, 4),
                         "pdf_sha256": hashlib.sha256(pdf.read_bytes()).hexdigest(),
                         "tool": f"{IMAGE} --convert-to pdf (network none) + pypdfium2 render of the drawing box"}
        print(r["key"], r["rendered"]["width"], "x", r["rendered"]["height"], "box", [l, t, rr, b])
    man_path.write_text(json.dumps(man, ensure_ascii=False, indent=1), encoding="utf-8")


if __name__ == "__main__":
    main()
