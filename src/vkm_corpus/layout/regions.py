"""Regions from a stored LAYOUT_RAW record (rule ``REGION_RULE``; no model call, no GPU).

PP-DocLayoutV3 has 25 classes in alphabetical order; the HF config merges some names (display/inline formula →
``formula``, header/footer images → ``header``/``footer``, vertical text → ``text``). ``FINE_LABELS`` restores the
original names from the class id, so display and inline formulas stay distinct.

Rule v1: one label per query (its best class) → per-label score threshold → NMS inside a family (IoU ≥ 0.85) and
across families for near-identical boxes (IoU ≥ 0.95) → boxes clipped to the page → ordered by the model's
reading-order rank. Every region keeps the index of its raw detection (``raw_locator = /detections/<i>``).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

REGION_RULE = "regions_v1"

FINE_LABELS = ["abstract", "algorithm", "aside_text", "chart", "content", "display_formula", "doc_title",
               "figure_title", "footer", "footer_image", "footnote", "formula_number", "header", "header_image",
               "image", "inline_formula", "number", "paragraph_title", "reference", "reference_content", "seal",
               "table", "text", "vertical_text", "vision_footnote"]

FAMILY = {
    "table": "table", "display_formula": "formula", "inline_formula": "formula", "image": "figure",
    "chart": "figure", "header_image": "figure", "footer_image": "figure", "seal": "figure",
}
# block types of the contract (BlockType) for text-like labels
BLOCK_TYPE = {
    "abstract": "ABSTRACT", "algorithm": "CODE", "aside_text": "SIDE_TEXT", "text": "TEXT", "vertical_text": "TEXT",
    "content": "TABLE_OF_CONTENTS", "doc_title": "TITLE", "paragraph_title": "HEADING",
    "figure_title": "CAPTION", "header": "PAGE_HEADER", "footer": "PAGE_FOOTER", "number": "PAGE_NUMBER",
    "footnote": "FOOTNOTE", "vision_footnote": "FOOTNOTE", "reference": "REFERENCE_LIST",
    "reference_content": "REFERENCE_LIST", "formula_number": "FORMULA_NUMBER",
}
DEFAULT_THRESHOLDS: dict[str, float] = {"*": 0.30, "image": 0.50, "chart": 0.50, "table": 0.50,
                                        "display_formula": 0.50, "inline_formula": 0.50, "seal": 0.50,
                                        "header_image": 0.50, "footer_image": 0.50}


@dataclass
class Region:
    det_index: int
    query: int
    label: str
    label_id: int
    score: float
    bbox: tuple[float, float, float, float]      # PAGE_PT_TL
    order_rank: int
    family: str                                  # text | table | formula | figure
    half: str | None = None                      # L | R for 2-up spreads
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def block_type(self) -> str:
        return BLOCK_TYPE.get(self.label, "OTHER")

    @property
    def area(self) -> float:
        x0, y0, x1, y1 = self.bbox
        return max(0.0, x1 - x0) * max(0.0, y1 - y0)


def fine_label(label_id: int, merged: str) -> str:
    return FINE_LABELS[label_id] if 0 <= label_id < len(FINE_LABELS) else merged


def iou(a: tuple[float, float, float, float], b: tuple[float, float, float, float]) -> float:
    ix = max(0.0, min(a[2], b[2]) - max(a[0], b[0]))
    iy = max(0.0, min(a[3], b[3]) - max(a[1], b[1]))
    inter = ix * iy
    ua = (a[2] - a[0]) * (a[3] - a[1]) + (b[2] - b[0]) * (b[3] - b[1]) - inter
    return inter / ua if ua > 0 else 0.0


def overlap_share(inner: tuple[float, float, float, float], outer: tuple[float, float, float, float]) -> float:
    """Share of ``inner``'s area covered by ``outer``."""
    ix = max(0.0, min(inner[2], outer[2]) - max(inner[0], outer[0]))
    iy = max(0.0, min(inner[3], outer[3]) - max(inner[1], outer[1]))
    a = max(1e-9, (inner[2] - inner[0]) * (inner[3] - inner[1]))
    return ix * iy / a


def regions_from_raw(raw: dict[str, Any], page_w: float, page_h: float,
                     thresholds: dict[str, float] | None = None) -> list[Region]:
    th = {**DEFAULT_THRESHOLDS, **(thresholds or {})}
    best: dict[tuple[str | None, int], tuple[int, dict[str, Any]]] = {}
    for i, d in enumerate(raw.get("detections", [])):
        key = (d.get("half"), int(d["query"]))
        if key not in best or d["score"] > best[key][1]["score"]:
            best[key] = (i, d)
    cands: list[Region] = []
    for (half, q), (i, d) in best.items():
        label = fine_label(int(d["label_id"]), d["label"])
        if d["score"] < th.get(label, th["*"]):
            continue
        x0, y0, x1, y1 = d["box_pt"]
        x0, y0 = max(0.0, min(x0, page_w)), max(0.0, min(y0, page_h))
        x1, y1 = max(0.0, min(x1, page_w)), max(0.0, min(y1, page_h))
        if x1 - x0 < 1.0 or y1 - y0 < 1.0:
            continue
        cands.append(Region(det_index=i, query=q, label=label, label_id=int(d["label_id"]), score=float(d["score"]),
                            bbox=(round(x0, 3), round(y0, 3), round(x1, 3), round(y1, 3)),
                            order_rank=int(d.get("order_rank", 0)) + (1000 if half == "R" else 0),
                            family=FAMILY.get(label, "text"), half=half))
    cands.sort(key=lambda r: (-r.score, r.det_index))
    kept: list[Region] = []
    for r in cands:
        dup = False
        for k in kept:
            v = iou(r.bbox, k.bbox)
            if (k.family == r.family and v >= 0.85) or v >= 0.95:
                dup = True
                break
        if not dup:
            kept.append(r)
    kept.sort(key=lambda r: (r.order_rank, r.bbox[1], r.bbox[0], r.det_index))
    return kept


def assign(boxes: list[tuple[float, float, float, float]], regions: list[Region],
           families: tuple[str, ...] = ("text",), min_share: float = 0.5) -> list[int | None]:
    """Index of the region (among ``regions``) that covers each box best (share of the box area ≥ ``min_share``)."""
    out: list[int | None] = []
    for b in boxes:
        best_i, best_v = None, 0.0
        for i, r in enumerate(regions):
            if r.family not in families:
                continue
            v = overlap_share(b, r.bbox)
            if v > best_v:
                best_i, best_v = i, v
        out.append(best_i if best_v >= min_share else None)
    return out
