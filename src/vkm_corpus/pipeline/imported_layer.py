"""Stage ``IMPORTED_LAYER``: a recognition layer produced outside the pipeline, imported page by page.

The OCR v2 run of 06.10.2026 (own page images, PaddleX doc parser = PP-DocLayoutV3 layout + PaddleOCR-VL-1.6 on a
local vLLM server) left, per page of a frozen page set: the raw PaddleX result of every image part (a page or the
halves ``a``/``b`` of a split spread), a postprocessed page record (the chosen answer of every block, normalisation
log, guard flags, verdict) and a per-page layer choice (rule ``CHOICE_V1``: ``NEW`` or ``OLD``).

``import_layer`` (CLI ``vkm-corpus run import-layer``) takes only pages chosen ``NEW``. For each it checks the source
version (register sha256 = OCR-run manifest = prep summary) and the page geometry (prepared raster vs. canon page size)
and stores one immutable ``OCR_RAW`` record (raw part JSON, preparation record, postprocess record, choice row; no
images — only their sha256; machine paths replaced by logical names). A page entry of the stage cache is keyed by
``stage_signature(source_sha256, IMPORTED_LAYER, page, cfg_hash(provenance, geometry rule, CHOICE_V1))``; a source
entry lists the pages and the refusals. Nothing is called, nothing old is overwritten.

The Assembler (``SourceImport`` → ``page_objects``) maps a record onto the canonical page with the geometry rule
``ocrv2_geometry_v1`` (deskew undone exactly as PIL ``rotate(-a, expand=True)`` did it, part offset, raster → page
points, clip; spread halves deduplicated in their overlap band by IoU, reading order ``a`` then ``b``) and makes the
imported blocks/tables/formulas the page's primary layer (``PADDLEOCR_VL``, origin OCR, region LAYOUT_MODEL, review
status AUTO_EXTRACTED_UNREVIEWED). The previous layers stay in the canon as secondary objects.
"""
from __future__ import annotations

import hashlib
import json
import math
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Iterator

from vkm_corpus.extract.model import ModelRef

STAGE = "IMPORTED_LAYER"
EXTRACTOR_ID = "paddleocr-vl-import"
TEXT_LAYER = "PADDLEOCR_VL"
RECOGNITION_METHOD = "OCR_PADDLEOCR_VL"
GEOMETRY_RULE = "ocrv2_geometry_v1"
CHOICE_RULE = "CHOICE_V1"
RECORD_SCHEMA = "vkm.ocr_raw.paddleocr_vl_page/1"
PROVENANCE_SCHEMA = "vkm.imported_layer_provenance/1"
REVIEW_STATUS = "AUTO_EXTRACTED_UNREVIEWED"
ACCEPTED_VERDICTS = ("OK", "OK_WITH_FLAGS")

# Model identities of the OCR v2 run (pins: infra/models/model_pins.json, keys below)
LAYOUT_MODEL = ModelRef("LAYOUT", "PaddlePaddle/PP-DocLayoutV3", "241f8bdfc77a7c7bee915a5057aaee58c235a8d3")
RECOGNITION_MODEL = ModelRef("RECOGNITION", "PaddlePaddle/PaddleOCR-VL-1.6", "c5630abae1d940eafe0697512a0325494b02ab42")
PIN_KEYS = {"LAYOUT": "pp-doclayout-v3-paddle", "RECOGNITION": "paddleocr-vl-1.6"}
PINS_RELPATH = "infra/models/model_pins.json"
SERVER_IMAGE_DIGEST = "sha256:073bd0cf36400d1f7fcb0bd028289a1ccbf2820772ecb023a4cc13219104399c"

# geometry tolerances
SIZE_TOL_PT = 1.0            # canon page size vs. prepared page size (pt, after the 0.2 % relative allowance)
SIZE_TOL_REL = 0.002
ASPECT_TOL = 0.01            # raster aspect vs. canon page aspect
RASTER_COVERAGE_APPROX = 0.005  # an embedded raster covering the page within this share maps exactly enough
OVERLAP_IOU = 0.5            # spread halves: same object detected in both halves

# PaddleX labels → canonical block types (pipeline layout labels first; OCR v2 titles of tables/charts are captions)
_EXTRA_BLOCK_TYPES = {"table_title": "CAPTION", "chart_title": "CAPTION", "abstract_title": "HEADING"}
# OCR v2 guard codes → canonical quality flags (dataset scope is applied by to_canon)
_FLAG_MAP = {
    "TRUNCATED": "TRUNCATED", "PIPELINE_TRUNCATED": "TRUNCATED", "PIPELINE_TRUNCATED_PUNCT": "TRUNCATED",
    "LOOP_NGRAM": "REPETITION", "CHAR_RUN": "REPETITION", "ESCAPE_RUN": "REPETITION", "LATEX_LOOP": "REPETITION",
    "TABLE_ROW_LOOP": "REPETITION", "TABLE_CELL_LOOP": "REPETITION",
    "TABLE_UNPARSABLE": "TABLE_STRUCTURE_UNCERTAIN", "TABLE_RAGGED_OTSL": "TABLE_STRUCTURE_UNCERTAIN",
    "TABLE_RAGGED_OTSL_MINOR": "TABLE_STRUCTURE_UNCERTAIN", "TABLE_OTSL_INCOMPLETE": "TABLE_STRUCTURE_UNCERTAIN",
    "TABLE_GRID_INCONSISTENT": "TABLE_STRUCTURE_UNCERTAIN", "TABLE_PADDED_TAIL": "TABLE_STRUCTURE_UNCERTAIN",
    "TABLE_MOSTLY_EMPTY": "EMPTY_CELLS_RATIO", "TABLE_LOW_NUMERIC_PARSE": "NUMERIC_PARSE_FAILURES",
    "LATEX_INVALID": "FORMULA_LATEX_UNPARSEABLE", "EMPTY_FORMULA": "FORMULA_LATEX_UNPARSEABLE",
    "FOREIGN_SCRIPT": "LANGUAGE_UNCERTAIN", "GREEK_WORDS": "LANGUAGE_UNCERTAIN", "MIRRORED_LATIN": "LANGUAGE_UNCERTAIN",
}
_MACHINE_PATH = re.compile(r"^(?:[A-Za-z]:[\\/]|/home/|/mnt/|/Users/|/root/|/tmp/|~/)")


class ImportRefused(ValueError):
    """A page (or source) cannot be imported; ``code`` is recorded, the old layer stays primary."""

    def __init__(self, code: str, detail: str = ""):
        super().__init__(f"{code}: {detail}" if detail else code)
        self.code = code
        self.detail = detail


# ================================================================================================= geometry
def pil_rotate_matrix(width: int, height: int, angle: float) -> tuple[list[float], tuple[int, int]]:
    """Affine matrix of PIL ``Image.rotate(angle, expand=True)`` (about the centre, no translate) and the output size.

    The matrix maps a point of the OUTPUT image onto the INPUT image — exactly the matrix PIL passes to its affine
    transform (PIL 12 ``Image.rotate``), so undoing a deskew needs no inversion. Fast paths (0/90/180/270°) are not
    used by a deskew of 0.3–5°."""
    w, h = float(width), float(height)
    a = -math.radians(angle % 360.0)
    m = [round(math.cos(a), 15), round(math.sin(a), 15), 0.0, round(-math.sin(a), 15), round(math.cos(a), 15), 0.0]

    def tf(x: float, y: float) -> tuple[float, float]:
        return m[0] * x + m[1] * y + m[2], m[3] * x + m[4] * y + m[5]

    cx, cy = w / 2.0, h / 2.0
    m[2], m[5] = tf(-cx, -cy)
    m[2] += cx
    m[5] += cy
    xs, ys = zip(*(tf(x, y) for x, y in ((0, 0), (w, 0), (w, h), (0, h))))
    nw = math.ceil(max(xs)) - math.floor(min(xs))
    nh = math.ceil(max(ys)) - math.floor(min(ys))
    m[2], m[5] = tf(-(nw - w) / 2.0, -(nh - h) / 2.0)
    return m, (int(nw), int(nh))


def _apply(m: list[float], x: float, y: float) -> tuple[float, float]:
    return m[0] * x + m[1] * y + m[2], m[3] * x + m[4] * y + m[5]


def undo_deskew(bbox: Iterable[float], deskew: dict[str, Any] | None) -> tuple[tuple[float, float, float, float], bool]:
    """Box in the deskewed part image → envelope of its four mapped corners in the part image before the deskew.
    Returns ``(box, approximate)``; a box is approximate when a rotation was undone (the envelope is larger)."""
    x0, y0, x1, y1 = (float(v) for v in bbox)
    if not deskew or not deskew.get("applied"):
        return (x0, y0, x1, y1), False
    w, h = deskew["size_before"]
    m, size = pil_rotate_matrix(int(w), int(h), -float(deskew["angle"]))   # prep did ``rotate(-angle)``
    if list(size) != [int(v) for v in deskew["size_after"]]:
        raise ImportRefused("DESKEW_SIZE_MISMATCH", f"{size} != {deskew['size_after']}")
    pts = [_apply(m, x, y) for x, y in ((x0, y0), (x1, y0), (x1, y1), (x0, y1))]
    return (min(p[0] for p in pts), min(p[1] for p in pts), max(p[0] for p in pts), max(p[1] for p in pts)), True


def to_page_px(bbox: tuple[float, float, float, float], box_in_page: Iterable[float]) -> tuple[float, ...]:
    ox, oy = float(list(box_in_page)[0]), float(list(box_in_page)[1])
    return bbox[0] + ox, bbox[1] + oy, bbox[2] + ox, bbox[3] + oy


def to_page_pt(bbox_px: tuple[float, ...], page_size_px: Iterable[float], width_pt: float, height_pt: float
               ) -> tuple[tuple[float, float, float, float], bool]:
    """Raster pixels of the upright page → PAGE_PT_TL points, clipped to the page; ``clipped`` when it was outside."""
    pw, ph = (float(v) for v in page_size_px)
    sx, sy = float(width_pt) / pw, float(height_pt) / ph
    raw = (bbox_px[0] * sx, bbox_px[1] * sy, bbox_px[2] * sx, bbox_px[3] * sy)
    out = (min(max(raw[0], 0.0), width_pt), min(max(raw[1], 0.0), height_pt),
           min(max(raw[2], 0.0), width_pt), min(max(raw[3], 0.0), height_pt))
    out = tuple(round(v, 3) for v in out)
    clipped = any(abs(a - b) > 0.01 for a, b in zip(raw, out))
    return out, clipped  # type: ignore[return-value]


def iou(a: tuple[float, ...], b: tuple[float, ...]) -> float:
    ix = max(0.0, min(a[2], b[2]) - max(a[0], b[0]))
    iy = max(0.0, min(a[3], b[3]) - max(a[1], b[1]))
    inter = ix * iy
    ua = (a[2] - a[0]) * (a[3] - a[1]) + (b[2] - b[0]) * (b[3] - b[1]) - inter
    return inter / ua if ua > 0 else 0.0


def check_page_geometry(prep: dict[str, Any], width_pt: float, height_pt: float, rotation_deg: int,
                        fmt: str) -> dict[str, Any]:
    """The prepared upright raster must be the canonical page: same aspect, and (PDF) the size pdfium reported equals
    the canon size (or its transpose for a /Rotate 90/270 page, whose render is upright like the canon's displayed
    page). Returns the geometry record stored with the page (a function of the OCR run and the canon size only)."""
    pw, ph = (float(v) for v in prep["page_size_px"])
    if not (width_pt and height_pt and pw and ph):
        raise ImportRefused("PAGE_SIZE_UNKNOWN")
    aspect = (pw / ph) / (float(width_pt) / float(height_pt))
    out: dict[str, Any] = {"aspect_ratio": round(aspect, 5), "method": prep.get("method"),
                           "page_size_px": [int(pw), int(ph)], "width_pt": width_pt, "height_pt": height_pt,
                           "rotation_deg": int(rotation_deg or 0)}
    if abs(aspect - 1.0) > ASPECT_TOL:
        raise ImportRefused("PAGE_ASPECT_MISMATCH", f"{aspect:.4f}")

    def near(a: float, b: float) -> bool:
        return abs(a - b) <= max(SIZE_TOL_PT, SIZE_TOL_REL * max(abs(a), abs(b)))

    if fmt == "PDF":
        psz = prep.get("page_size_pt")
        if not psz:
            raise ImportRefused("PAGE_SIZE_UNKNOWN", "page_size_pt")
        same = near(psz[0], width_pt) and near(psz[1], height_pt)
        swapped = near(psz[0], height_pt) and near(psz[1], width_pt)
        if not (same or (swapped and int(rotation_deg or 0) in (90, 270))):
            raise ImportRefused("PAGE_SIZE_MISMATCH", f"{psz} vs {[width_pt, height_pt]}")
        out["prep_page_size_pt"] = [float(psz[0]), float(psz[1])]
        out["prep_size_basis"] = "SAME" if same else "TRANSPOSED_FOR_ROTATE"
        approx = False
        if prep.get("method") == "pdfimages_original_raster":
            rows = [r for r in prep.get("pdfimages_rows") or [] if r.get("type") == "image"]
            if len(rows) == 1 and rows[0].get("xppi") and rows[0].get("yppi"):
                cw = rows[0]["w"] / rows[0]["xppi"] * 72.0
                ch = rows[0]["h"] / rows[0]["yppi"] * 72.0
                dev = max(abs(cw - width_pt) / width_pt, abs(ch - height_pt) / height_pt)
                out["raster_coverage_deviation"] = round(dev, 5)
                approx = dev > RASTER_COVERAGE_APPROX
            else:
                approx = True
        out["bbox_approx"] = approx
    elif fmt == "DJVU":
        out["bbox_approx"] = False
    else:
        raise ImportRefused("FORMAT_NOT_IMPORTABLE", fmt)
    return out


def check_djvu_grid(prep: dict[str, Any], canon_px: tuple[int, int]) -> dict[str, Any]:
    """DjVu: the prepared raster is the page's INFO pixel grid, possibly uniformly downscaled (never upscaled)."""
    pw, ph = (float(v) for v in prep["page_size_px"])
    s = pw / float(canon_px[0])
    if abs(ph / float(canon_px[1]) - s) > 0.002 or s > 1.0001:
        raise ImportRefused("PAGE_SIZE_MISMATCH", f"raster {int(pw)}x{int(ph)} vs INFO {list(canon_px)}")
    return {"raster_scale_vs_info": round(s, 6)}


# ================================================================================================= page objects
@dataclass
class ImportedObject:
    kind: str                                   # BLOCK | TABLE | FORMULA
    label: str
    locator: str                                # JSON pointer into the OCR_RAW record
    bbox: tuple[float, float, float, float]
    order: int                                  # reading order on the page (1..n, part a then b)
    part: str
    block_id: Any
    flags: list[str] = field(default_factory=list)
    text: str = ""
    block_type: str = "TEXT"
    html: str | None = None
    latex: str | None = None
    number: str | None = None
    formula_kind: str | None = None
    layout_score: float | None = None
    source: str = "original"                    # original | retry:<strategy>


@dataclass
class Suppressed:
    kind: str                                   # TEXT | TABLE | FORMULA | FIGURE (accounting candidate kind)
    label: str
    locator: str
    reason: str
    duplicate_of: str | None = None


@dataclass
class PageImport:
    objects: list[ImportedObject]
    suppressed: list[Suppressed]
    page_flags: list[str]
    geometry: dict[str, Any]


def _block_type(label: str) -> str:
    from vkm_corpus.layout.regions import BLOCK_TYPE

    return BLOCK_TYPE.get(label) or _EXTRA_BLOCK_TYPES.get(label) or "TEXT"


def _flags(item: dict[str, Any]) -> list[str]:
    out: list[str] = []
    for f in item.get("flags") or []:
        code = _FLAG_MAP.get(str(f.get("code") if isinstance(f, dict) else f))
        if code and code not in out:
            out.append(code)
    return out


def _candidate_kind(label: str, kind: str | None) -> str:
    if label == "table" or kind == "table":
        return "TABLE"
    if label in ("display_formula", "formula", "inline_formula") or kind in ("formula", "inline_formula"):
        return "FORMULA"
    if kind == "figure" or label in ("image", "chart", "seal", "header_image", "footer_image"):
        return "FIGURE"
    return "TEXT"


def _layout_scores(res: dict[str, Any]) -> dict[tuple[int, ...], float]:
    out = {}
    for b in ((res.get("layout_det_res") or {}).get("boxes") or []):
        coord = b.get("coordinate")
        if coord is not None and b.get("score") is not None:
            out[tuple(int(round(float(v))) for v in coord)] = float(b["score"])
    return out


def page_objects(record: dict[str, Any], width_pt: float, height_pt: float) -> PageImport:
    """Objects of one imported page in PAGE_PT_TL of the canonical page (rule ``ocrv2_geometry_v1``)."""
    if record.get("schema") != RECORD_SCHEMA:
        raise ImportRefused("RECORD_SCHEMA_UNKNOWN", str(record.get("schema")))
    prep, pp = record["prep"], record["postprocess"]
    page_px = prep["page_size_px"]
    geom = dict(record.get("geometry") or {})
    approx_page = bool(geom.get("bbox_approx"))
    parts = record["parts"]
    tables = {(t.get("part") or "", t.get("block_id")): t for t in pp.get("tables") or []}
    by_part: dict[str, list[dict[str, Any]]] = {}
    for it in pp.get("blocks") or []:
        by_part.setdefault(it.get("part") or "", []).append(it)
    prep_parts = {(q.get("part") or ""): q for q in prep.get("parts") or []}
    staged: list[tuple[int, ImportedObject | Suppressed, tuple[float, ...] | None]] = []  # (part_rank, obj, page_px)
    deskewed = False
    for k, part in enumerate(parts):
        name = part.get("part") or ""
        res = (part.get("raw") or {}).get("res") or {}
        raw_blocks = res.get("parsing_res_list") or []
        items = sorted(by_part.get(name, []), key=lambda it: int(it["order"]))
        if [int(it["order"]) for it in items] != list(range(len(raw_blocks))):
            raise ImportRefused("POSTPROCESS_RAW_MISMATCH", f"part {name!r}")
        q = prep_parts.get(name)
        if q is None:
            raise ImportRefused("PREP_PART_MISSING", name)
        if q.get("deskew", {}).get("applied"):
            deskewed = True
        scores = _layout_scores(res)
        heads: dict[Any, str] = {}
        for i, (rb, it) in enumerate(zip(raw_blocks, items)):
            loc = f"/parts/{k}/raw/res/parsing_res_list/{i}"
            label = it.get("label") or rb.get("block_label") or "text"
            if rb.get("block_id", i) != it.get("block_id") or label != (rb.get("block_label") or "text"):
                raise ImportRefused("POSTPROCESS_RAW_MISMATCH", loc)
            kind = it.get("kind")
            gid = it.get("group_id", it.get("block_id"))
            if kind == "merged_follower":
                staged.append((k, Suppressed(_candidate_kind(label, None), label, loc, "MERGED_GROUP_FOLLOWER",
                                             heads.get(gid)), None))
                continue
            heads.setdefault(gid, loc)
            if kind == "figure":
                staged.append((k, Suppressed("FIGURE", label, loc, "FIGURE_FROM_PIPELINE_LAYOUT"), None))
                continue
            if kind == "formula_number" and it.get("attached"):
                staged.append((k, Suppressed("TEXT", label, loc, "FORMULA_NUMBER_ATTACHED"), None))
                continue
            if not it.get("kept", True):
                staged.append((k, Suppressed(_candidate_kind(label, kind), label, loc,
                                             "DROPPED_MARGINAL_BLOCK" if it.get("dropped_flags") else
                                             "NOT_KEPT_BY_POSTPROCESS"), None))
                continue
            box, approx = undo_deskew(rb.get("block_bbox") or it.get("bbox"), q.get("deskew"))
            px = to_page_px(box, q["box_in_page"])
            flags = _flags(it)
            if approx or approx_page:
                flags.append("BBOX_APPROX")
            obj = ImportedObject(kind="BLOCK", label=label, locator=loc, bbox=(0.0, 0.0, 0.0, 0.0), order=0,
                                 part=name, block_id=it.get("block_id"), flags=flags,
                                 source=str(it.get("source") or "original"),
                                 layout_score=scores.get(tuple(int(v) for v in (rb.get("block_bbox") or []))))
            if kind == "table":
                t = tables.get((name, it.get("block_id")))
                obj.kind, obj.html = "TABLE", (t or {}).get("html") or ""
                obj.text = it.get("text") or ""
            elif kind in ("formula", "inline_formula"):
                obj.kind = "FORMULA"
                obj.formula_kind = "DISPLAY" if kind == "formula" else "INLINE"
                obj.latex = it.get("latex") or None
                obj.number = it.get("number") if kind == "formula" else None
            else:
                obj.text = it.get("text") or ""
                obj.block_type = "FORMULA_NUMBER" if kind == "formula_number" else _block_type(label)
            staged.append((k, obj, px))
    # split spreads: one object detected in both halves (inside the overlap band) is kept from part a
    if len(parts) == 2:
        boxes = [q["box_in_page"] for q in (prep_parts.get(p.get("part") or "") for p in parts)]
        band = (float(boxes[1][0]), float(boxes[0][2]))      # [x of b's left edge, x of a's right edge]
        a_objs = [(o, px) for r, o, px in staged if r == 0 and isinstance(o, ImportedObject)]
        for idx, (r, o, px) in enumerate(staged):
            if r != 1 or not isinstance(o, ImportedObject) or px is None:
                continue
            if px[2] < band[0] or px[0] > band[1]:
                continue
            for ao, apx in a_objs:
                if ao.kind == o.kind and apx[2] >= band[0] and apx[0] <= band[1] and iou(apx, px) >= OVERLAP_IOU:
                    staged[idx] = (r, Suppressed({"BLOCK": "TEXT"}.get(o.kind, o.kind), o.label, o.locator,
                                                 "SPREAD_OVERLAP_DUPLICATE", ao.locator), None)
                    break
    objects: list[ImportedObject] = []
    suppressed: list[Suppressed] = []
    clipped = 0
    for _, o, px in staged:
        if isinstance(o, Suppressed):
            suppressed.append(o)
            continue
        o.bbox, was_clipped = to_page_pt(px, page_px, width_pt, height_pt)  # type: ignore[arg-type]
        if was_clipped:
            clipped += 1
            if "BBOX_APPROX" not in o.flags:
                o.flags.append("BBOX_APPROX")
        o.order = len(objects) + 1
        objects.append(o)
    geom.update({"rule": GEOMETRY_RULE, "clipped_objects": clipped, "deskew_undone": deskewed,
                 "parts": len(parts)})
    return PageImport(objects, suppressed, ["SKEW_CORRECTED"] if deskewed else [], geom)


# ================================================================================================= inputs
def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _jsonl(path: Path) -> Iterator[dict[str, Any]]:
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if line:
                yield json.loads(line)


def _part_stem(page_id: str, part: str) -> str:
    sid, rest = page_id.split(":")
    return f"{sid}_{rest}" + (f"_{part}" if part else "")


def sanitize_paths(obj: Any, *, img_root_name: str = "OCRV2_IMG", pointer: str = "") -> tuple[Any, list[str]]:
    """Copy of a JSON value with absolute machine paths replaced by logical names (``OCRV2_IMG:png/<file>``)."""
    changed: list[str] = []

    def walk(v: Any, ptr: str) -> Any:
        if isinstance(v, dict):
            return {k: walk(x, f"{ptr}/{k}") for k, x in v.items()}
        if isinstance(v, list):
            return [walk(x, f"{ptr}/{i}") for i, x in enumerate(v)]
        if isinstance(v, str) and _MACHINE_PATH.match(v):
            changed.append(ptr)
            m = re.search(r"/(png|empty|retry_crops)/(.+)$", v.replace("\\", "/"))
            return f"{img_root_name}:{m.group(1)}/{m.group(2)}" if m else "<machine-path>"
        return v

    return walk(obj, pointer), changed


@dataclass
class OcrRunInputs:
    """Files of one OCR v2 run (``--ocr-root`` + ``--run-tag``) and the kit manifest of its page set."""

    ocr_root: Path
    run_tag: str
    kit_manifest: Path

    @property
    def pages(self) -> Path:
        return self.ocr_root / "pp" / self.run_tag / "pages.jsonl"

    @property
    def postprocess_receipt(self) -> Path:
        return self.ocr_root / "pp" / self.run_tag / "postprocess_receipt.json"

    @property
    def parts_dir(self) -> Path:
        return self.ocr_root / "runs" / self.run_tag / "parts"

    @property
    def receipts_dir(self) -> Path:
        return self.ocr_root / "runs" / self.run_tag / "receipts"

    @property
    def prep_manifest(self) -> Path:
        return self.ocr_root / "img" / "prep_manifest.jsonl"

    @property
    def prep_receipt(self) -> Path:
        return self.ocr_root / "img" / "prep_receipt.json"

    @property
    def choice(self) -> Path:
        return self.ocr_root / "choice" / "layer_choice.jsonl"

    @property
    def choice_receipt(self) -> Path:
        return self.ocr_root / "choice" / "layer_choice_receipt.json"


def load_pins(repo_root: Path) -> dict[str, dict[str, Any]]:
    path = Path(repo_root) / PINS_RELPATH
    data = json.loads(path.read_text(encoding="utf-8"))
    return {m["key"]: m for m in data.get("models", [])}


def build_provenance(inputs: OcrRunInputs, pins: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """Deterministic provenance of the import (no machine paths, no import time); its hash keys the stage cache."""
    receipts = [json.loads(p.read_text(encoding="utf-8")) for p in sorted(inputs.receipts_dir.glob("*.json"))]
    receipts = [r for r in receipts if r.get("run_tag") == inputs.run_tag]
    if not receipts or any(r.get("status") != "COMPLETED" for r in receipts):
        raise ImportRefused("RUN_RECEIPTS_INCOMPLETE")
    same = ("config_sha256", "pipeline", "predict_kwargs", "code_sha256")
    for key in same:
        if len({json.dumps(r.get(key), sort_keys=True) for r in receipts}) != 1:
            raise ImportRefused("RUN_RECEIPTS_INCONSISTENT", key)
    versions = {json.dumps({k: r["versions"].get(k) for k in ("paddleocr", "paddlex", "paddle", "python")},
                           sort_keys=True) for r in receipts}
    if len(versions) != 1:
        raise ImportRefused("RUN_RECEIPTS_INCONSISTENT", "versions")
    r0 = receipts[0]
    init = {k: v for k, v in (r0.get("pipeline") or {}).get("init_kwargs", {}).items() if "url" not in k}
    served = (((r0.get("server") or {}).get("models") or {}).get("data") or [{}])[0]
    model_entries = []
    for ref in (LAYOUT_MODEL, RECOGNITION_MODEL):
        pin = pins.get(PIN_KEYS[ref.role])
        if pin is None or pin.get("repo_id") != ref.model_id or pin.get("revision") != ref.model_revision:
            raise ImportRefused("MODEL_PIN_MISMATCH", PIN_KEYS[ref.role])
        entry = {"role": ref.role, "model_id": ref.model_id, "model_revision": ref.model_revision,
                 "pin": f"{PINS_RELPATH}#{PIN_KEYS[ref.role]}", "weights_sha256": pin.get("weights_sha256")}
        if ref.role == "RECOGNITION":
            if SERVER_IMAGE_DIGEST.split(":")[1] not in json.dumps(pin):
                raise ImportRefused("MODEL_PIN_MISMATCH", "server image digest")
            entry["served_by"] = {"engine": "vllm-server (PaddleOCR genai image)", "image_digest": SERVER_IMAGE_DIGEST,
                                  "variant": r0.get("server_variant") or (r0.get("config") or {}).get("server_variant"),
                                  "served_model": served.get("id"), "max_model_len": served.get("max_model_len")}
        else:
            entry["served_by"] = {"engine": "PaddleX doc parser (in the OCR v2 client)"}
        model_entries.append(entry)
    prep_r = json.loads(inputs.prep_receipt.read_text(encoding="utf-8"))
    pp_r = json.loads(inputs.postprocess_receipt.read_text(encoding="utf-8"))
    ch_r = json.loads(inputs.choice_receipt.read_text(encoding="utf-8"))
    if ch_r.get("rule") != CHOICE_RULE:
        raise ImportRefused("CHOICE_RULE_UNKNOWN", str(ch_r.get("rule")))
    pages_sha = _sha256_file(inputs.pages)
    if (ch_r.get("inputs") or {}).get("pages_sha256") != pages_sha:
        raise ImportRefused("CHOICE_NOT_BOUND_TO_PAGES", "layer choice was computed on another pages.jsonl")
    if pp_r.get("status") != REVIEW_STATUS:
        raise ImportRefused("POSTPROCESS_STATUS_UNKNOWN")
    cfg = r0.get("config") or {}
    return {
        "schema": PROVENANCE_SCHEMA, "extractor_id": EXTRACTOR_ID, "status": REVIEW_STATUS, "origin": "OCR",
        "region_origin": "LAYOUT_MODEL", "text_layer": TEXT_LAYER, "recognition_method": RECOGNITION_METHOD,
        "geometry_rule": GEOMETRY_RULE, "record_schema": RECORD_SCHEMA, "choice_rule": CHOICE_RULE,
        "models": model_entries,
        "software": json.loads(next(iter(versions))),
        "run": {"run_tag": inputs.run_tag, "config_sha256": r0.get("config_sha256"),
                "init_kwargs": init, "predict_kwargs": r0.get("predict_kwargs"),
                "paddlex_config_overrides": (r0.get("pipeline") or {}).get("paddlex_config_overrides"),
                "predict": cfg.get("predict"), "retry": cfg.get("retry"), "max_attempts": (cfg.get("run") or {}).get(
                    "max_attempts"),
                "shards": sorted([{"shard": r.get("shard"), "units_todo": r.get("units_todo"), "done": r.get("done"),
                                   "failed": r.get("failed"), "finished_at": r.get("finished_at"),
                                   "retries": r.get("retries"), "vlm_records_unmatched": r.get("vlm_records_unmatched")}
                                  for r in receipts], key=lambda x: str(x["shard"])),
                "code_sha256": r0.get("code_sha256")},
        "image_preparation": {"config": prep_r.get("config"), "n_pages": prep_r.get("n_pages"),
                              "methods": prep_r.get("methods"), "deskew_applied": prep_r.get("deskew_applied"),
                              "prep_manifest_sha256": _sha256_file(inputs.prep_manifest)},
        "postprocess": {"code_sha256": pp_r.get("code_sha256"), "render_ctx": pp_r.get("render_ctx"),
                        "verdicts": pp_r.get("verdicts"),
                        "versions": {k: (pp_r.get("versions") or {}).get(k)
                                     for k in ("pymorphy3", "rapidfuzz", "pylatexenc")},
                        "pages_sha256": pages_sha},
        "choice": {"rule": ch_r.get("rule"), "params": ch_r.get("params"), "inputs": ch_r.get("inputs"),
                   "summary": {"pages": (ch_r.get("summary") or {}).get("pages"),
                               "choice": (ch_r.get("summary") or {}).get("choice")},
                   "code_sha256": ch_r.get("code_sha256"), "choice_jsonl_sha256": _sha256_file(inputs.choice)},
        "kit_manifest_sha256": _sha256_file(inputs.kit_manifest),
    }


def import_config_hash(provenance: dict[str, Any]) -> str:
    from vkm_corpus.contracts.signatures import config_hash
    from vkm_corpus.pipeline.config import EXTRACTOR_VERSIONS

    return config_hash({"provenance": provenance, "geometry_rule": GEOMETRY_RULE, "choice_rule": CHOICE_RULE,
                        "record_schema": RECORD_SCHEMA, "extractor": [EXTRACTOR_ID, EXTRACTOR_VERSIONS[EXTRACTOR_ID]]})


def page_signature(source_sha256: str, page_index: int, cfg_hash: str) -> str:
    from vkm_corpus.contracts.signatures import stage_signature
    from vkm_corpus.pipeline.config import EXTRACTOR_VERSIONS, GENERATIONS
    from vkm_corpus.versions import PIPELINE_VERSION

    return stage_signature(source_sha256=source_sha256, stage=STAGE, pipeline_version=PIPELINE_VERSION,
                           extractor_id=EXTRACTOR_ID, extractor_version=EXTRACTOR_VERSIONS[EXTRACTOR_ID],
                           stage_config_hash=cfg_hash, page_unit="p" if page_index is not None else None,
                           page_index=page_index, extraction_generation=GENERATIONS[EXTRACTOR_ID],
                           models=[LAYOUT_MODEL, RECOGNITION_MODEL])


# ================================================================================================= import
def build_record(page_id: str, src_sha: str, choice: dict[str, Any], prep: dict[str, Any], pp: dict[str, Any],
                 parts_dir: Path, geometry: dict[str, Any], cfg_hash: str) -> dict[str, Any]:
    """The immutable OCR_RAW record of one page (raw PaddleX part JSON verbatim except sanitised paths)."""
    parts = []
    sanitized: list[str] = []
    if [q.get("part_id") for q in prep.get("parts") or []] != list(pp.get("parts") or []):
        raise ImportRefused("PARTS_MISMATCH", "prep parts != postprocess parts")
    for q in prep.get("parts") or []:
        if q.get("status") == "EMPTY_PAGE":
            continue        # a blank half of a spread was not OCRed: nothing to import (kept in /prep)
        if q.get("status") != "OK":
            raise ImportRefused("PART_NOT_OCRED", q.get("part_id", ""))
        k = len(parts)
        path = parts_dir / f"{_part_stem(page_id, q.get('part') or '')}.json"
        if not path.is_file():
            raise ImportRefused("PART_JSON_MISSING", path.name)
        raw = json.loads(path.read_text(encoding="utf-8"))
        img = raw.get("image") or {}
        if raw.get("status") != "OK" or raw.get("part_id") != q.get("part_id") or \
                img.get("png_sha256") != q.get("png_sha256") or \
                [img.get("width"), img.get("height")] != [q.get("width"), q.get("height")]:
            raise ImportRefused("PART_JSON_NOT_BOUND", q.get("part_id", ""))
        res = raw.get("res") or {}
        if [res.get("width"), res.get("height")] != [q.get("width"), q.get("height")]:
            raise ImportRefused("PART_SIZE_MISMATCH", q.get("part_id", ""))
        clean, changed = sanitize_paths(raw, pointer=f"/parts/{k}/raw")
        sanitized += changed
        parts.append({"part": q.get("part") or "", "part_id": q.get("part_id"), "raw": clean,
                      "raw_json_sha256": _sha256_file(path)})
    prep_clean, changed = sanitize_paths(prep, pointer="/prep")
    pp_clean, changed2 = sanitize_paths(pp, pointer="/postprocess")
    sanitized += changed + changed2
    return {"schema": RECORD_SCHEMA, "page_id": page_id, "source_id": page_id.split(":")[0],
            "page_index": int(page_id.split(":")[1][1:]), "source_sha256": src_sha, "status": REVIEW_STATUS,
            "import_config_hash": cfg_hash, "choice": choice, "prep": prep_clean, "postprocess": pp_clean,
            "parts": parts, "geometry": geometry, "sanitized_pointers": sorted(sanitized)}


def import_layer(cfg: Any, store: Any, cache: Any, sources: list[Any], inputs: OcrRunInputs, *,
                 repo_root: Path, run_id: str, dry_run: bool = False, log: Any = None) -> dict[str, Any]:
    """Import every NEW page of the selected sources; returns the receipt (counts, refusals, timings)."""
    from vkm_corpus.pipeline.commit import load_state

    t0 = time.time()
    provenance = build_provenance(inputs, load_pins(repo_root))
    cfg_hash = import_config_hash(provenance)
    prov_art = None if dry_run else store.put_json(provenance, "RUN_CONFIG").artifact_id
    wanted = {s.source_id: s for s in sources if s.lifecycle == "ACTIVE"}
    choice = {r["page_id"]: r for r in _jsonl(inputs.choice) if r["page_id"].split(":")[0] in wanted}
    new_ids = {pid for pid, r in choice.items() if r.get("choice") == "NEW"}
    manifest = {r["page_id"]: r for r in _jsonl(inputs.kit_manifest) if r["page_id"] in new_ids}
    prep_rows = {r["page_id"]: r for r in _jsonl(inputs.prep_manifest) if r["page_id"] in new_ids}
    pp_rows = {r["page_id"]: r for r in _jsonl(inputs.pages) if r["page_id"] in new_ids}
    receipt: dict[str, Any] = {"schema": "vkm.import_layer_receipt/1", "run_id": run_id, "stage": STAGE,
                               "extractor_id": EXTRACTOR_ID, "config_hash": cfg_hash,
                               "provenance_artifact_id": prov_art, "geometry_rule": GEOMETRY_RULE,
                               "choice_rule": CHOICE_RULE, "dry_run": dry_run, "sources": {}, "totals": {}}
    totals = {"sources": 0, "pages_new": 0, "pages_imported": 0, "pages_refused": 0, "pages_cached": 0,
              "objects": 0, "suppressed": 0}
    for sid, src in sorted(wanted.items()):
        pages = sorted((pid for pid in new_ids if pid.startswith(sid + ":")), key=lambda p: int(p.split(":")[1][1:]))
        if not pages:
            continue
        totals["sources"] += 1
        totals["pages_new"] += len(pages)
        srec: dict[str, Any] = {"pages_new": len(pages), "imported": 0, "cached": 0, "refused": []}
        receipt["sources"][sid] = srec
        prep_summary, _ = load_state(cfg, src)
        canon_rows = {r["page_index"]: r for r in (prep_summary or {}).get("pages", [])}
        fmt = ((prep_summary or {}).get("inspect") or {}).get("file_format") or \
            (manifest.get(pages[0]) or {}).get("format")
        man_sha = {manifest[p].get("source_sha256") for p in pages if p in manifest}
        problem = None
        if not src.sha256 or man_sha != {src.sha256}:
            problem = ImportRefused("SOURCE_SHA256_MISMATCH", "register vs OCR-run manifest")
        elif prep_summary is not None and prep_summary.get("source_sha256") != src.sha256:
            problem = ImportRefused("SOURCE_SHA256_MISMATCH", "register vs prep summary")
        page_sigs: dict[str, str] = {}
        raw_ids: list[str] = []
        for pid in pages:
            idx = int(pid.split(":")[1][1:])
            try:
                if problem is not None:
                    raise problem
                m, q, pp, ch = manifest.get(pid), prep_rows.get(pid), pp_rows.get(pid), choice[pid]
                if m is None or q is None or pp is None:
                    raise ImportRefused("RUN_INPUT_MISSING", "manifest/prep/postprocess row")
                if pp.get("verdict") not in ACCEPTED_VERDICTS or ch.get("verdict") != pp.get("verdict"):
                    raise ImportRefused("VERDICT_NOT_IMPORTABLE", str(pp.get("verdict")))
                if q.get("status") != "OK" or q.get("source_id") != sid or int(q.get("page_index") or 0) != idx:
                    raise ImportRefused("PREP_ROW_NOT_OK")
                width, height, rot = m.get("width_pt"), m.get("height_pt"), int(m.get("rotation_deg") or 0)
                row = canon_rows.get(idx)
                if row is not None and (abs(float(row.get("width_pt") or 0) - float(width or 0)) > 0.01 or
                                        abs(float(row.get("height_pt") or 0) - float(height or 0)) > 0.01):
                    raise ImportRefused("CANON_PAGE_SIZE_CHANGED", "prep summary vs OCR-run manifest")
                geometry = check_page_geometry(q, float(width), float(height), rot, str(fmt))
                checks: dict[str, Any] = {"prep_summary": row is not None}
                if fmt == "DJVU" and row is not None and row.get("width_px") and row.get("height_px"):
                    checks["djvu_info_grid"] = check_djvu_grid(q, (row["width_px"], row["height_px"]))
                rec = build_record(pid, src.sha256, {k: ch.get(k) for k in ("choice", "reason", "old_layer",
                                                                            "verdict", "new", "old")},
                                   q, pp, inputs.parts_dir, geometry, cfg_hash)
                rec["choice"]["rule"] = CHOICE_RULE
                objs = page_objects(rec, float(width), float(height))       # refuses on any inconsistency
                sig = page_signature(src.sha256, idx, cfg_hash)
                outputs = {"page_id": pid, "source_sha256": src.sha256, "config_hash": cfg_hash,
                           "geometry_rule": GEOMETRY_RULE, "width_pt": float(width), "height_pt": float(height),
                           "choice": rec["choice"]["choice"], "choice_reason": rec["choice"]["reason"],
                           "verdict": pp.get("verdict"), "parts": [p["part_id"] for p in rec["parts"]],
                           "counts": {"objects": len(objs.objects), "suppressed": len(objs.suppressed),
                                      "tables": sum(o.kind == "TABLE" for o in objs.objects),
                                      "formulas": sum(o.kind == "FORMULA" for o in objs.objects)},
                           "provenance_artifact_id": prov_art, "checks": checks}
                totals["objects"] += len(objs.objects)
                totals["suppressed"] += len(objs.suppressed)
                if dry_run:
                    srec["imported"] += 1
                    continue
                art = store.put_json(rec, "OCR_RAW", compress=True, source_id=sid, page_id=pid,
                                     producer_signature=sig)
                outputs["raw_artifact_id"] = art.artifact_id
                hit = cache.get_stage(sig)
                if hit and hit.get("status") == "OK" and (hit.get("outputs") or {}).get("raw_artifact_id") == \
                        art.artifact_id:
                    srec["cached"] += 1
                else:
                    cache.add_stage(stage_signature=sig, stage=STAGE, source_id=sid, page_index=idx, outputs=outputs)
                    srec["imported"] += 1
                page_sigs[str(idx)] = sig
                raw_ids.append(art.artifact_id)
            except ImportRefused as exc:
                srec["refused"].append({"page_index": idx, "code": exc.code, "detail": exc.detail[:200]})
            except (OSError, KeyError, TypeError, ValueError) as exc:
                srec["refused"].append({"page_index": idx, "code": "IMPORT_INPUT_INVALID",
                                        "detail": f"{type(exc).__name__}"})
        totals["pages_imported"] += srec["imported"]
        totals["pages_cached"] += srec["cached"]
        totals["pages_refused"] += len(srec["refused"])
        if dry_run:
            continue
        source_outputs = {"source_id": sid, "source_sha256": src.sha256, "config_hash": cfg_hash,
                          "geometry_rule": GEOMETRY_RULE, "choice_rule": CHOICE_RULE,
                          "extractor_id": EXTRACTOR_ID, "provenance_artifact_id": prov_art,
                          "pages": page_sigs, "raw_artifact_ids": sorted(raw_ids),
                          "refused": srec["refused"]}
        ssig = page_signature(src.sha256, None, cfg_hash)  # type: ignore[arg-type]
        hit = cache.get_stage(ssig)
        if not (hit and hit.get("status") == "OK" and hit.get("outputs") == source_outputs):
            cache.add_stage(stage_signature=ssig, stage=STAGE, source_id=sid, page_index=None, outputs=source_outputs)
        if log is not None:
            log.info("imported layer", extra={"vkm": {"source_id": sid, "stage": STAGE,
                                                      "status": f"{srec['imported']}+{srec['cached']}/{len(pages)}"}})
    receipt["totals"] = totals
    receipt["wall_s"] = round(time.time() - t0, 1)
    receipt["provenance"] = {"models": provenance["models"], "software": provenance["software"]}
    return receipt


# ================================================================================================= assembly side
@dataclass
class SourceImport:
    """The import of one source as the Assembler sees it (latest source entry of the stage cache)."""

    config_hash: str
    provenance_artifact_id: str | None
    pages: dict[int, dict[str, Any]]            # page index → page entry outputs (+ signature)
    refused: list[dict[str, Any]]
    stale: list[tuple[int | None, str]]         # (page index, reason) of entries that cannot be used
    entry_signature: str

    def digest(self) -> dict[str, Any]:
        return {"config_hash": self.config_hash, "geometry_rule": GEOMETRY_RULE,
                "pages": sorted((i, e["signature"], e.get("raw_artifact_id")) for i, e in self.pages.items()),
                "refused": sorted((r["page_index"], r["code"]) for r in self.refused),
                "stale": sorted((i or 0, r) for i, r in self.stale)}


def load_source_import(cache: Any, source_id: str, source_sha256: str) -> SourceImport | None:
    """Latest source entry of the stage (by creation time) and its usable page entries; ``None`` without an import."""
    stages = getattr(cache, "stages", None) or {}      # a cache stand-in without stages has no import
    cands = [r for r in stages.values() if r.get("stage") == STAGE and r.get("source_id") == source_id
             and r.get("page_index") is None and r.get("status") == "OK"]
    if not cands:
        return None
    entry = max(cands, key=lambda r: r.get("created_at", ""))
    out = entry.get("outputs") or {}
    imp = SourceImport(config_hash=out.get("config_hash", ""), provenance_artifact_id=out.get("provenance_artifact_id"),
                       pages={}, refused=list(out.get("refused") or []), stale=[],
                       entry_signature=entry["stage_signature"])
    if out.get("source_sha256") != source_sha256 or out.get("geometry_rule") != GEOMETRY_RULE:
        imp.stale.append((None, "SOURCE_VERSION_OR_RULE_CHANGED"))
        return imp
    for key, sig in sorted((out.get("pages") or {}).items(), key=lambda kv: int(kv[0])):
        idx = int(key)
        hit = cache.get_stage(sig)
        expected = page_signature(source_sha256, idx, imp.config_hash)
        if hit is None or hit.get("status") != "OK" or sig != expected:
            imp.stale.append((idx, "PAGE_ENTRY_MISSING_OR_STALE"))
            continue
        imp.pages[idx] = {**(hit.get("outputs") or {}), "signature": sig}
    return imp


def run_models() -> list[dict[str, Any]]:
    """Run-record model entries for runs that assemble imported pages (validator D02b)."""
    return [{"role": "LAYOUT", "model_id": LAYOUT_MODEL.model_id, "model_revision": LAYOUT_MODEL.model_revision,
             "backend": "paddlex (imported OCR v2 layer, no call in this run)"},
            {"role": "RECOGNITION", "model_id": RECOGNITION_MODEL.model_id,
             "model_revision": RECOGNITION_MODEL.model_revision,
             "backend": "vllm-server (imported OCR v2 layer, no call in this run)"}]


def imported_run_models(cfg: Any, source_ids: list[str]) -> list[dict[str, Any]]:
    """``run_models()`` when the staging cache holds an import of one of ``source_ids`` (else nothing): a run
    declares the imported models only when it may assemble imported pages."""
    root = Path(cfg.data_root) / "cache" / "stages"
    if not root.is_dir():
        return []
    wanted = {f'"source_id":"{sid}"' for sid in source_ids}
    needle = f'"stage":"{STAGE}"'
    for path in root.rglob("*.jsonl"):
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                if needle in line and (not wanted or any(w in line for w in wanted)):
                    return run_models()
    return []
