"""Phase 2 — render + preview + layout (one process holds the GPU).

For every fixed-layout page of a prepared source (PDF, DjVu, DOCX render pages):

* 200-dpi RGB render (``PAGE_RENDER``; not stored, reproducible: its id is the sha256 of the uncompressed PPM/PGM
  encoding, so it is encoder-independent) with a recipe (renderer, version, profile, page);
* 1024-px JPEG preview (``PAGE_PREVIEW``, stored; visual rerank and API page image);
* ink share of the render (EMPTY pages with ink go to OCR);
* PP-DocLayoutV3 raw detections (``LAYOUT_RAW``), keyed by a call signature over the render pixels – a page whose
  signature is cached is never sent to the model again (H-03). 2-up spreads are detected per half.

The per-source visual summary (JSON in the staging cache) lists these ids per page; later phases read only it.
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any, Iterator

from vkm_corpus.artifacts.render import Raster, ink_ratio, preview_jpeg, render_djvu_page, render_pdf_page
from vkm_corpus.artifacts.store import ArtifactStore, canonical_json_bytes, sha256_hex
from vkm_corpus.layout import ppdoclayout as ppl
from vkm_corpus.pipeline.cache import StageCache
from vkm_corpus.pipeline.config import PipelineConfig
from vkm_corpus.versions import PIPELINE_VERSION

VISUAL_SCHEMA = "vkm.visual/1"


def pixel_sha(image: Any) -> str:
    from vkm_corpus.contracts.signatures import pixel_sha256

    return pixel_sha256(image.mode, image.width, image.height, image.tobytes())


def pnm_bytes_sha(image: Any) -> tuple[str, int, str]:
    """(sha256, size, media type) of the canonical uncompressed PPM/PGM encoding of an image."""
    import hashlib

    mode = image.mode
    magic = b"P5" if mode == "L" else b"P6"
    img = image if mode in ("L", "RGB") else image.convert("RGB")
    header = magic + f"\n{img.width} {img.height}\n255\n".encode("ascii")
    data = img.tobytes()
    h = hashlib.sha256(header)
    h.update(data)
    return h.hexdigest(), len(header) + len(data), "image/x-portable-graymap" if mode == "L" else \
        "image/x-portable-pixmap"


def layout_call_signature(cfg: PipelineConfig, pixel_sha256: str, spread: bool) -> str:
    from vkm_corpus.contracts.signatures import call_signature, config_hash

    prompt = ("layout-2up:" if spread else "layout:") + config_hash(cfg.stage_config("LAYOUT"))
    return call_signature(model_id=ppl.MODEL_ID, model_revision=ppl.MODEL_REVISION, weights_sha256=ppl.WEIGHTS_SHA256,
                          prompt=prompt, sampling={}, input_pixel_sha256=pixel_sha256)


def visual_signature(cfg: PipelineConfig, prep: dict[str, Any]) -> str:
    from vkm_corpus.contracts.signatures import config_hash, stage_signature

    conf = {"render": cfg.stage_config("RENDER"), "layout": cfg.stage_config("LAYOUT") if cfg.use_gpu_layout else None,
            "prepare_signature": prep["prepare_signature"], "renderers": _renderer_versions()}
    return stage_signature(source_sha256=prep["source_sha256"], stage="VISUAL", pipeline_version=PIPELINE_VERSION,
                           extractor_id="vkm-pipeline", extractor_version="0.1.0", stage_config_hash=config_hash(conf))


def _renderer_versions() -> dict[str, str]:
    from vkm_corpus.pipeline.prepare import library_versions

    v = library_versions()
    try:
        import PIL

        v["pillow"] = PIL.__version__
    except Exception:  # noqa: BLE001
        pass
    return v


def visual_path(data_root: Path, source_id: str, signature: str) -> Path:
    return Path(data_root) / "cache" / "visual" / source_id / f"{signature}.json"


def load_visual(data_root: Path, source_id: str, signature: str) -> dict[str, Any] | None:
    p = visual_path(data_root, source_id, signature)
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else None


# ---------------------------------------------------------------------------------------------------- rendering
class PageRenderer:
    """Renders pages of one prepared source at a given dpi (PDF via PyMuPDF, DjVu via ddjvu, DOCX via its render)."""

    def __init__(self, cfg: PipelineConfig, store: ArtifactStore, prep: dict[str, Any]):
        self.cfg = cfg
        self.prep = prep
        self.fmt = prep["inspect"]["file_format"]
        self._doc = None
        if self.fmt == "DJVU":
            self.path = Path(cfg.resources_root) / _canonical_path(cfg, prep)
        elif self.fmt == "DOCX":
            pdf = store.find(prep["render"]["pdf_artifact_id"])
            if pdf is None:
                raise FileNotFoundError("ARTIFACT_MISSING: DOCX render")
            self.path = pdf
        else:
            self.path = Path(cfg.resources_root) / _canonical_path(cfg, prep)

    def _pdf(self):
        if self._doc is None:
            from vkm_corpus.extract import pdf_native

            self._doc = pdf_native.open_pdf(self.path)
        return self._doc

    def render(self, page_index: int, dpi: float, mode: str, profile: str) -> Raster:
        if self.fmt == "DJVU":
            return render_djvu_page(self.path, page_index, dpi, mode, profile)
        return render_pdf_page(self._pdf()[page_index - 1], dpi, mode, profile)

    def recipe(self, page_index: int, dpi: float, mode: str, profile: str) -> dict[str, Any]:
        v = _renderer_versions()
        renderer = "ddjvu" if self.fmt == "DJVU" else "pymupdf"
        return {"renderer": renderer, "renderer_version": v.get("djvulibre" if renderer == "ddjvu" else "pymupdf"),
                "profile": profile, "dpi": dpi, "mode": mode, "source_id": self.prep["source_id"],
                "source_sha256": self.prep["source_sha256"], "page_index": page_index,
                "input": "DOCX_RENDERED_PDF " + self.prep["render"]["pdf_artifact_id"] if self.fmt == "DOCX" else
                "SOURCE_FILE"}

    def close(self) -> None:
        if self._doc is not None:
            self._doc.close()
            self._doc = None


def _canonical_path(cfg: PipelineConfig, prep: dict[str, Any]) -> str:
    from vkm_corpus.pipeline.sources import load_sources

    if "canonical_path" in prep:
        return prep["canonical_path"]
    for s in load_sources(cfg.resources_root):
        if s.source_id == prep["source_id"]:
            prep["canonical_path"] = s.canonical_path
            return s.canonical_path
    raise KeyError(prep["source_id"])


def split_spread(image: Any) -> tuple[Any, Any]:
    w = image.width
    return image.crop((0, 0, w // 2, image.height)), image.crop((w // 2, 0, w, image.height))


# ---------------------------------------------------------------------------------------------------- phase
def visual_pages(prep: dict[str, Any]) -> Iterator[dict[str, Any]]:
    if prep["inspect"]["file_format"] not in ("PDF", "DJVU", "DOCX"):
        return
    for p in prep["pages"]:
        if p.get("route") != "FAILED":
            yield p


def run_visual(cfg: PipelineConfig, store: ArtifactStore, cache: StageCache, prep: dict[str, Any],
               layout_model: Any | None, *, log: Any = None, max_layout_calls: int | None = None,
               counters: dict[str, int] | None = None, only_pages: set[int] | None = None) -> dict[str, Any]:
    """Render/preview/layout of one source; returns the visual summary (cached by ``visual_signature``)."""
    sig = visual_signature(cfg, prep)
    cached = load_visual(cfg.data_root, prep["source_id"], sig)
    if cached is not None and cached.get("complete"):
        return cached
    counters = counters if counters is not None else {}
    prev = {p["page_index"]: p for p in (cached or {}).get("pages", [])}
    out_pages: list[dict[str, Any]] = []
    renderer = PageRenderer(cfg, store, prep)
    use_layout = cfg.use_gpu_layout and prep["inspect"]["file_format"] in ("PDF", "DJVU")
    dpi = cfg.layout_render_dpi
    complete = True
    pending: list[tuple[dict[str, Any], Any, str, bool, dict[str, Any]]] = []

    def flush() -> None:
        nonlocal complete
        if not pending:
            return
        images: list[Any] = []
        spans: list[tuple[int, int]] = []
        for _, img, _, spread, _ in pending:
            start = len(images)
            images.extend(split_spread(img) if spread else [img])
            spans.append((start, len(images)))
        t0 = time.perf_counter()
        try:
            dets = layout_model.detect(images)
        except Exception as exc:  # noqa: BLE001 - every page of the batch records the failure
            for row, _, _, _, _ in pending:
                row["layout_error"] = {"code": "LAYOUT_FAILED", "message": f"{type(exc).__name__}: {exc}"[:300]}
            complete = False
            pending.clear()
            return
        ms = int((time.perf_counter() - t0) * 1000 / max(1, len(pending)))
        for (row, img, csig, spread, render_info), (a, b) in zip(pending, spans):
            if spread:
                left, right = dets[a], dets[b - 1]
                half_w = img.width // 2
                merged = []
                for half, d, off in (("L", left, 0), ("R", right, half_w)):
                    for x in d["detections"]:
                        bx = x["box_px"]
                        merged.append({**x, "half": half, "box_px": [bx[0] + off, bx[1], bx[2] + off, bx[3]]})
                det = {"n_queries": left["n_queries"], "n_classes": left["n_classes"], "detections": merged}
            else:
                det = dets[a]
            rec = ppl.raw_record(det, render=render_info, config=cfg.layout, runtime=layout_model.runtime,
                                 id2label=layout_model.id2label, run_id=cache.run_id, source_id=prep["source_id"],
                                 page_id=row["page_id"])
            rec["spread_2up"] = spread
            art = store.put_json(rec, "LAYOUT_RAW", source_id=prep["source_id"], page_id=row["page_id"],
                                 producer_signature=csig)
            cache.add_call(call_signature=csig, attempt=cache.next_attempt(csig), status="OK",
                           raw_artifact_id=art.artifact_id, input_artifact_id=render_info["render_artifact_id"],
                           kind="LAYOUT", source_id=prep["source_id"], page_id=row["page_id"],
                           extra={"ms_per_page": ms})
            row["layout_raw_artifact_id"] = art.artifact_id
            row["layout_executed_run"] = cache.run_id
            counters["layout_calls"] = counters.get("layout_calls", 0) + 1
        pending.clear()

    try:
        for p in visual_pages(prep):
            idx = p["page_index"]
            unit = "r" if prep["inspect"]["file_format"] == "DOCX" else "p"
            page_id = f"{prep['source_id']}:{unit}{idx:04d}"
            old = prev.get(idx)
            if old and old.get("preview_artifact_id") and (not use_layout or old.get("layout_raw_artifact_id")):
                out_pages.append(old)
                continue
            if only_pages is not None and idx not in only_pages:
                out_pages.append(old or {"page_index": idx, "page_id": page_id, "pending": True})
                complete = False
                continue
            row: dict[str, Any] = {"page_index": idx, "page_id": page_id}
            try:
                t0 = time.perf_counter()
                r = renderer.render(idx, dpi, "RGB", "r200c")
                psha = pixel_sha(r.image)
                rsha, rsize, rmedia = pnm_bytes_sha(r.image)
                rrec = store.register_reproducible(rsha, rsize, "PAGE_RENDER", rmedia,
                                                   renderer.recipe(idx, dpi, "RGB", "r200c"),
                                                   source_id=prep["source_id"], page_id=page_id,
                                                   image_width_px=r.width, image_height_px=r.height, image_dpi=dpi,
                                                   pixel_sha256=psha)
                jpg, pw, ph = preview_jpeg(r.image, cfg.preview_long_side, cfg.preview_quality)
                prec = store.put_bytes(jpg, "PAGE_PREVIEW", "image/jpeg", source_id=prep["source_id"],
                                       page_id=page_id, image_width_px=pw, image_height_px=ph,
                                       image_dpi=round(dpi * pw / r.width, 3),
                                       recipe={"from": rrec.artifact_id, "long_side": cfg.preview_long_side,
                                               "format": "JPEG", "quality": cfg.preview_quality})
                row.update({"render_artifact_id": rrec.artifact_id, "render_pixel_sha256": psha,
                            "render_width_px": r.width, "render_height_px": r.height, "render_dpi": dpi,
                            "preview_artifact_id": prec.artifact_id, "preview_width_px": pw, "preview_height_px": ph,
                            "ink_ratio": round(ink_ratio(r.image), 5), "render_ms": int((time.perf_counter() - t0) * 1000)})
                if use_layout:
                    spread = bool(prep.get("spreads")) and r.width / max(1, r.height) >= cfg.spread_aspect
                    csig = layout_call_signature(cfg, psha, spread)
                    row["layout_call_signature"] = csig
                    row["spread_2up"] = spread
                    hit = cache.best_call(csig)
                    if hit is not None:
                        row["layout_raw_artifact_id"] = hit["raw_artifact_id"]
                        counters["layout_cached"] = counters.get("layout_cached", 0) + 1
                    elif layout_model is None:
                        complete = False
                        row["layout_error"] = {"code": "MODEL_UNAVAILABLE", "message": "layout model not loaded"}
                    elif max_layout_calls is not None and counters.get("layout_calls", 0) + len(pending) >= \
                            max_layout_calls:
                        complete = False
                        row["layout_error"] = {"code": "MODEL_CALL_BUDGET_EXHAUSTED", "message": "layout budget"}
                    else:
                        render_info = {"render_artifact_id": rrec.artifact_id, "pixel_sha256": psha,
                                       "width": r.width, "height": r.height, "dpi": dpi, "mode": "RGB",
                                       "profile": "r200c"}
                        pending.append((row, r.image, csig, spread, render_info))
                        if len(pending) >= max(1, cfg.layout.batch_size):
                            flush()
            except Exception as exc:  # noqa: BLE001
                complete = False
                row["render_error"] = {"code": "RENDER_FAILED", "message": f"{type(exc).__name__}: {exc}"[:300]}
            out_pages.append(row)
            if log is not None and idx % 50 == 0:
                log.info("visual progress", extra={"vkm": {"source_id": prep["source_id"], "page_id": page_id}})
        flush()
    finally:
        renderer.close()
    for row in out_pages:
        if use_layout and not row.get("layout_raw_artifact_id"):
            complete = False
    summary = {"schema": VISUAL_SCHEMA, "source_id": prep["source_id"], "visual_signature": sig,
               "prepare_signature": prep["prepare_signature"], "complete": complete, "use_layout": use_layout,
               "pages": sorted(out_pages, key=lambda x: x["page_index"])}
    p = visual_path(cfg.data_root, prep["source_id"], sig)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_bytes(canonical_json_bytes(summary))
    os.replace(tmp, p)
    return summary
