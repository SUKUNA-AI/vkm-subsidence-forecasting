"""FIGURE_READINGS_V1 — take the embedded images out of a DOCX at full resolution and map each to its caption.

For every image occurrence in ``word/document.xml`` (DrawingML ``a:blip/@r:embed`` and VML ``v:imagedata/@r:id``) the
relationship is resolved to its ``word/media/*`` part; the bytes are copied unchanged (sha256 of the original part).
Caption candidates: the nearest following paragraph that starts with "Рисунок N" / "Рис. N" (figures are captioned
below) and the nearest preceding paragraph that starts with "Таблица N" (tables are captioned above); the nearest
non-empty body paragraph before the image is kept as context. Nothing is interpreted; captions and context stay in
the git-ignored work directory.

EMF/WMF parts are rendered to PNG in a separate step (``render_vector.py``); here they are copied as they are.

    python extract_docx_images.py --docx <file.docx> --source-id VKM-SRC-023 --prefix F023 --out <work dir>
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import zipfile
from pathlib import Path, PurePosixPath

from lxml import etree

NS = {
    "w": "http://schemas.openxmlformats.org/wordprocessingml/2006/main",
    "a": "http://schemas.openxmlformats.org/drawingml/2006/main",
    "r": "http://schemas.openxmlformats.org/officeDocument/2006/relationships",
    "v": "urn:schemas-microsoft-com:vml",
    "wp": "http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing",
    "pr": "http://schemas.openxmlformats.org/package/2006/relationships",
}
R_EMBED = "{%s}embed" % NS["r"]
R_ID = "{%s}id" % NS["r"]
FIG_RE = re.compile(r"^\s*(Рисунок|Рис\.)\s*(\d+(?:\.\d+)*)", re.I)
TAB_RE = re.compile(r"^\s*Таблица\s*(\d+(?:\.\d+)*)", re.I)
MAX_FWD, MAX_BACK = 6, 6


def para_text(p) -> str:
    return "".join(t.text or "" for t in p.iter("{%s}t" % NS["w"])).strip()


def rels_of(z: zipfile.ZipFile) -> dict[str, str]:
    root = etree.fromstring(z.read("word/_rels/document.xml.rels"))
    out = {}
    for rel in root.findall("pr:Relationship", NS):
        tgt = rel.get("Target")
        if rel.get("TargetMode") == "External":
            continue
        out[rel.get("Id")] = str(PurePosixPath("word") / tgt) if not tgt.startswith("/") else tgt.lstrip("/")
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--docx", required=True)
    ap.add_argument("--source-id", required=True)
    ap.add_argument("--prefix", required=True, help="short key prefix, e.g. F023")
    ap.add_argument("--out", required=True)
    args = ap.parse_args()
    out = Path(args.out)
    (out / "images").mkdir(parents=True, exist_ok=True)
    docx = Path(args.docx)
    z = zipfile.ZipFile(docx)
    rels = rels_of(z)
    root = etree.fromstring(z.read("word/document.xml"))
    body = root.find("w:body", NS)
    paras = list(body.iter("{%s}p" % NS["w"]))
    texts = [para_text(p) for p in paras]
    in_table = [any(a.tag == "{%s}tbl" % NS["w"] for a in p.iterancestors()) for p in paras]

    occurrences = []
    for i, p in enumerate(paras):
        ids = []
        for el in p.iter():
            if el.tag == "{%s}blip" % NS["a"] and el.get(R_EMBED):
                ids.append(el.get(R_EMBED))
            elif el.tag == "{%s}imagedata" % NS["v"] and el.get(R_ID):
                ids.append(el.get(R_ID))
        # keep document order, one entry per reference (a nested fallback may repeat the same rId)
        seen = []
        for rid in ids:
            if rid not in seen:
                seen.append(rid)
        for rid in seen:
            occurrences.append((i, rid))

    records, per_para_count = [], {}
    for n, (i, rid) in enumerate(occurrences, start=1):
        part = rels.get(rid)
        if part is None or part not in z.namelist():
            records.append({"n": n, "paragraph": i, "rid": rid, "error": "relationship not resolved"})
            continue
        data = z.read(part)
        media = PurePosixPath(part).name
        ext = media.rsplit(".", 1)[-1].lower()
        per_para_count[i] = per_para_count.get(i, 0) + 1
        fig = next(((j, texts[j]) for j in range(i, min(len(paras), i + MAX_FWD + 1))
                    if FIG_RE.match(texts[j])), None)
        tab = next(((j, texts[j]) for j in range(i - 1, max(-1, i - MAX_BACK - 1), -1)
                    if TAB_RE.match(texts[j])), None)
        ctx = next((texts[j] for j in range(i - 1, max(-1, i - 12), -1)
                    if texts[j] and not FIG_RE.match(texts[j]) and not TAB_RE.match(texts[j])
                    and len(texts[j]) > 40), "")
        # chosen caption: the nearer of a figure caption below and a table caption above (a tie goes to the figure)
        chosen, kind = None, None
        cands = [(abs(c[0] - i), n, c, k) for n, (c, k) in enumerate(((fig, "figure_caption_below"),
                                                                        (tab, "table_caption_above"))) if c]
        if cands:
            _, _, chosen, kind = min(cands, key=lambda t: (t[0], t[1]))
        key = f"{args.prefix}_{n:03d}"
        fname = f"{key}_{media}"
        (out / "images" / fname).write_bytes(data)
        records.append({
            "key": key, "n": n, "source_id": args.source_id, "container": docx.name,
            "media_part": part, "media_name": media, "format": ext, "bytes": len(data),
            "sha256": hashlib.sha256(data).hexdigest(), "file": f"images/{fname}",
            "paragraph": i, "in_table_cell": in_table[i], "index_in_paragraph": per_para_count[i],
            "caption": chosen[1] if chosen else None, "caption_kind": kind,
            "caption_paragraph": chosen[0] if chosen else None,
            "figure_caption_candidate": fig[1] if fig else None,
            "table_caption_candidate": tab[1] if tab else None,
            "context_paragraph": ctx[:600],
        })
    unreferenced = sorted(set(n for n in z.namelist() if n.startswith("word/media/"))
                          - {r["media_part"] for r in records if "media_part" in r})
    old_path = out / f"manifest_{args.prefix}.json"
    if old_path.exists():      # keep the renders of an earlier run (render_vector.py) for unchanged parts
        old = {r.get("key"): r for r in json.loads(old_path.read_text(encoding="utf-8")).get("images", [])}
        for r in records:
            o = old.get(r.get("key"))
            if o and o.get("sha256") == r.get("sha256") and o.get("rendered"):
                r["rendered"] = o["rendered"]
    manifest = {"source_id": args.source_id, "container": docx.name,
                "container_sha256": hashlib.sha256(docx.read_bytes()).hexdigest(),
                "n_occurrences": len(records), "n_media_parts": len([n for n in z.namelist() if n.startswith("word/media/")]),
                "media_not_referenced_from_body": unreferenced, "images": records}
    (out / f"manifest_{args.prefix}.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"{args.prefix}: {len(records)} occurrences, {manifest['n_media_parts']} media parts, "
          f"not referenced from body: {unreferenced}")


if __name__ == "__main__":
    main()
