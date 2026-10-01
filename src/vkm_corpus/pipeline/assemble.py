"""Phase 4 — rebuild one source entirely from the stage caches (no model call) into internal objects.

Inputs: the prep summary, the visual summary, NATIVE_RAW / LAYOUT_RAW / OCR_RAW artifacts and the call cache. The
OCR tasks of every page are re-planned with ``ocr_stage.plan_page_tasks`` and re-cropped deterministically, so each
result is found by its call signature; a task without a cached result leaves the page ``OCR_REQUIRED``/``PARTIAL``
(never ``NATIVE_OK`` with empty text). The output is a ``SourceResult`` for ``extract.to_canon``.
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from vkm_corpus.artifacts.store import ArtifactStore
from vkm_corpus.extract.model import (BlockX, DocumentX, ErrorRec, FigureX, FormulaX, ModelRef, PageX, Pagination,
                                      SourceInput, SourceResult, StepRec, TableX)
from vkm_corpus.layout import ppdoclayout as ppl
from vkm_corpus.layout.regions import Region, assign, overlap_share
from vkm_corpus.ocr import normalize as onorm
from vkm_corpus.ocr.quality import call_flags
from vkm_corpus.pipeline import ocr_stage, scenario_b
from vkm_corpus.pipeline.cache import StageCache
from vkm_corpus.pipeline.config import EXTRACTOR_VERSIONS, GENERATIONS, PipelineConfig

LAYOUT_MODEL = ModelRef("LAYOUT", ppl.MODEL_ID, ppl.MODEL_REVISION)
CAPTION_RE = re.compile(r"^\s*(Рис(?:унок|\.)?|Fig(?:ure|\.)?|Табл(?:ица|\.)?|Table|Схема|График)\s*\.?\s*"
                        r"(№\s*)?([\dIVXLА-Я][\d.\-–]*[а-яa-z]?)", re.I)
ROMAN = re.compile(r"^(?=[ivxlcdm]+$)m{0,3}(cm|cd|d?c{0,3})(xc|xl|l?x{0,3})(ix|iv|v?i{0,3})$", re.I)


def _pv() -> str:
    from vkm_corpus.versions import PIPELINE_VERSION

    return PIPELINE_VERSION


def _recog_model(cfg: PipelineConfig) -> ModelRef:
    return ModelRef("RECOGNITION", cfg.model.model_id, cfg.model.model_revision)


def _cfg_hash(d: dict[str, Any]) -> str:
    from vkm_corpus.contracts.signatures import config_hash

    return config_hash(d)


def _parse_label(raw: str | None) -> tuple[str | None, int | None]:
    if not raw:
        return None, None
    s = raw.strip().strip(".-–— ")
    if re.fullmatch(r"\d{1,4}", s):
        return s, int(s)
    if ROMAN.match(s):
        vals = {"i": 1, "v": 5, "x": 10, "l": 50, "c": 100, "d": 500, "m": 1000}
        total, prev = 0, 0
        for ch in reversed(s.lower()):
            v = vals[ch]
            total = total - v if v < prev else total + v
            prev = max(prev, v)
        return s, total
    return s, None


class Assembler:
    def __init__(self, cfg: PipelineConfig, store: ArtifactStore, cache: StageCache, src: SourceInput,
                 prep: dict[str, Any], visual: dict[str, Any] | None, *, sample_pages: set[int] | None = None,
                 scenario_decision: dict[str, Any] | None = None, only_pages: set[int] | None = None):
        self.only_pages = only_pages
        self.cfg = cfg
        self.store = store
        self.cache = cache
        self.src = src
        self.prep = prep
        self.visual = visual
        self.fmt = (prep.get("inspect") or {}).get("file_format", "UNKNOWN")
        self.vis_rows = {r["page_index"]: r for r in (visual or {}).get("pages", [])}
        self.sample_pages = sample_pages or set()
        self.decision = scenario_decision or {}
        self.result = SourceResult(source=src)
        self.region_hash = _cfg_hash(cfg.stage_config("REGIONS"))
        self.ocr_hash = _cfg_hash(cfg.stage_config("OCR"))
        self.native_hash = _cfg_hash(cfg.stage_config("NATIVE_TEXT"))
        self.path = Path(cfg.resources_root) / src.canonical_path
        self._pdf = None

    # ------------------------------------------------------------------ helpers
    def err(self, code: str, stage: str, message: str, page_index: int | None = None, retryable: bool = False,
            tool: str = "vkm_corpus", exception_type: str | None = None) -> None:
        self.result.errors.append(ErrorRec(code=code, stage=stage, message=message[:500], retryable=retryable,
                                           page_index=page_index, tool=tool, exception_type=exception_type))

    def step(self, stage: str, page_index: int | None, outcome: str, status: str, sig: str, extractor: str,
             config_hash: str, **kw: Any) -> None:
        self.result.steps.append(StepRec(stage=stage, page_index=page_index, outcome=outcome, status=status,
                                         stage_signature=sig, extractor_id=extractor,
                                         extractor_version=EXTRACTOR_VERSIONS.get(extractor, "0.1.0"),
                                         config_hash=config_hash, **kw))

    def pdf(self):
        if self._pdf is None:
            from vkm_corpus.extract import pdf_native

            target = self.path
            if self.fmt == "DOCX":
                target = self.store.find(self.prep["render"]["pdf_artifact_id"])
            self._pdf = pdf_native.open_pdf(target)
        return self._pdf

    def close(self) -> None:
        if self._pdf is not None:
            self._pdf.close()
            self._pdf = None

    def ocr_result(self, crop: ocr_stage.CropOut) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
        """(call-cache entry, OCR_RAW record) of a crop, or (None, None) when not cached."""
        sig = ocr_stage.call_signature_for(self.cfg, crop.spec.task, crop.pixel_sha256)
        crop.call_signature = sig
        hit = self.cache.best_call(sig)
        if hit is None:
            return None, None
        try:
            return hit, self.store.read_json(hit["raw_artifact_id"])
        except (FileNotFoundError, RuntimeError) as exc:
            self.err("ARTIFACT_MISSING", "OCR", f"{hit['raw_artifact_id']}: {exc}", crop.spec.page_index)
            return None, None

    # ------------------------------------------------------------------ entry
    def build(self) -> SourceResult:
        r = self.result
        insp = self.prep.get("inspect") or {}
        for e in self.prep.get("errors", []):
            self.err(e["code"], e["stage"], e["message"], e.get("page_index"), e.get("retryable", False),
                     e.get("tool", "vkm_corpus"), e.get("exception_type"))
        pag = self.prep.get("pagination")
        pagination = Pagination(**pag) if pag else None
        doc_meta = self.prep.get("document") or {}
        flags = []
        if "SIGNATURE_AFTER_BOM" in insp.get("flags", []):
            flags.append("SIGNATURE_AFTER_BOM")
        if "LEADING_BYTES_BEFORE_HEADER" in insp.get("flags", []):
            flags.append("LEADING_BYTES_BEFORE_HEADER")
        r.document = DocumentX(file_format=insp.get("file_format", "UNKNOWN"),
                               format_version=insp.get("format_version"),
                               container_detail=insp.get("container_detail"), pagination=pagination,
                               document_class=self.prep.get("document_class", "UNKNOWN"),
                               is_encrypted=bool(doc_meta.get("is_encrypted")),
                               has_native_page_labels=doc_meta.get("has_native_page_labels"),
                               pagination_render_profile=(self.prep.get("render") or {}).get("profile_string"),
                               pagination_artifact_id=(self.prep.get("render") or {}).get("pdf_artifact_id"),
                               file_meta=doc_meta.get("metadata") or {},
                               file_identifiers=list(doc_meta.get("identifiers") or []),
                               file_languages=list(doc_meta.get("languages") or []),
                               native_raw_artifact_id=self.prep.get("document_raw_artifact_id"), quality_flags=flags,
                               extra={"spreads": self.prep.get("spreads"), "docx_counts": self.prep.get("docx_counts")})
        self._source_steps(pagination)
        if self.prep.get("status") in ("FAILED", "UNSUPPORTED") or pagination is None:
            r.status = "FAILED" if self.prep.get("status") != "UNSUPPORTED" else "UNSUPPORTED"
            return r
        try:
            if self.fmt in ("PDF", "DJVU"):
                self._fixed_layout()
            elif self.fmt == "EPUB":
                self._epub()
            elif self.fmt == "DOCX":
                self._docx()
        finally:
            self.close()
        self._printed_labels()
        self._captions()
        r.status = self._rollup()
        return r

    def _source_steps(self, pagination: Pagination | None) -> None:
        """INSPECT and PAGINATE steps of the source (EXECUTED in this run or REUSED_CACHED from the prep summary)."""
        outcome = "EXECUTED" if self.prep.get("run_id") == self.cache.run_id else "REUSED_CACHED"
        failed = self.prep.get("status") in ("FAILED", "UNSUPPORTED")
        sig = self.prep.get("prepare_signature") or "0" * 64
        cfg_hash = _cfg_hash({"inspect": "signature_v1"})
        self.step("INSPECT", None, outcome, "FAILED" if failed else "NATIVE_OK", sig, "vkm-pipeline", cfg_hash,
                  reason_code=next((e["code"] for e in self.prep.get("errors", []) if e.get("stage") == "INSPECT"),
                                   None))
        mismatch = pagination is not None and not pagination.agree
        self.step("PAGINATE", None, outcome if pagination is not None else "NOT_ATTEMPTED",
                  "NEEDS_REVIEW" if mismatch else "FAILED" if pagination is None else "NATIVE_OK", sig,
                  "vkm-pipeline", cfg_hash, n_objects_out=pagination.count if pagination else 0,
                  reason_code="PAGECOUNT_MISMATCH" if mismatch else None)

    def _captions(self) -> None:
        """Caption of each figure/table: nearest primary CAPTION block (or a block starting with a caption word) in
        the same column – below a figure, above a table preferred – within 120 pt (DERIVATION, flagged)."""
        by_page: dict[int, list[tuple[int, BlockX]]] = {}
        for i, b in enumerate(self.result.blocks):
            if b.bbox is not None and b.is_primary_layer:
                by_page.setdefault(b.page_index, []).append((i, b))
        for obj, prefer_below in [(f, True) for f in self.result.figures] + [(t, False) for t in self.result.tables]:
            if obj.bbox is None:
                continue
            best = None
            for i, b in by_page.get(obj.page_index, []):
                text = onorm.text_from_markdown(b.text).strip()
                is_cap = b.block_type == "CAPTION" or bool(CAPTION_RE.match(text))
                if not is_cap:
                    continue
                ox = min(obj.bbox[2], b.bbox[2]) - max(obj.bbox[0], b.bbox[0])
                if ox <= 0:
                    continue
                below = b.bbox[1] >= obj.bbox[3] - 2
                above = b.bbox[3] <= obj.bbox[1] + 2
                if not (below or above):
                    continue
                gap = b.bbox[1] - obj.bbox[3] if below else obj.bbox[1] - b.bbox[3]
                if gap > 120:
                    continue
                penalty = 0 if below == prefer_below else 40
                score = gap + penalty + (0 if b.block_type == "CAPTION" else 20)
                if best is None or score < best[0]:
                    best = (score, i, b, text)
            if best is None:
                obj.quality_flags.append("CAPTION_NOT_FOUND")
                continue
            _, i, b, text = best
            obj.caption = text
            obj.caption_block_index = i
            m = CAPTION_RE.match(text)
            label = m.group(0).strip() if m else None
            if isinstance(obj, FigureX):
                obj.figure_label = label
            else:
                obj.table_label = label
            if b.block_type != "CAPTION" or best[0] > 60:
                obj.quality_flags.append("CAPTION_ASSOCIATION_UNCERTAIN")

    # ------------------------------------------------------------------ fixed-layout pages (PDF, DjVu)
    def _page_base(self, row: dict[str, Any]) -> PageX:
        vis = self.vis_rows.get(row["page_index"], {})
        page = PageX(page_index=row["page_index"], page_kind=row.get("page_kind", "PDF_PAGE"),
                     width_pt=row.get("width_pt"), height_pt=row.get("height_pt"),
                     rotation_deg=int(row.get("rotation") or 0),
                     page_box="DJVU_IMAGE" if self.fmt == "DJVU" else "RENDER_PDF" if self.fmt == "DOCX" else "CROPBOX",
                     native_dpi=row.get("native_dpi"), page_class=row.get("page_class"),
                     class_flags=list(row.get("class_flags") or []), route=row.get("route"),
                     route_reason=row.get("route_reason"), native_raw_artifact_id=row.get("native_raw_artifact_id"),
                     plain_text_sha256=row.get("plain_text_sha256"),
                     layout_raw_artifact_id=vis.get("layout_raw_artifact_id"),
                     render_artifact_id=vis.get("preview_artifact_id"),
                     render_dpi=int(round(vis["render_dpi"] * vis["preview_width_px"] / vis["render_width_px"]))
                     if vis.get("preview_artifact_id") else None,
                     layout_render_artifact_id=vis.get("render_artifact_id"))
        if not (page.width_pt and page.height_pt):
            page.bbox_space = "NONE"
        page.extra["ink_ratio"] = vis.get("ink_ratio")
        page.extra["label"] = row.get("label")
        if vis.get("spread_2up"):
            page.is_spread = True
            page.quality_flags.append("SPREAD_2UP")
        elif self.fmt in ("PDF", "DJVU"):
            page.is_spread = False
        if page.rotation_deg:
            page.quality_flags.append("ROTATED")
        for f in page.class_flags:
            if f in ("FONT_WITHOUT_TOUNICODE", "TYPE3_FONT", "LOW_FUNCTION_WORD_RATE", "UNMAPPED_SYMBOL_GLYPHS",
                     "VECTOR_NO_TEXT", "IMAGE_NO_TEXT", "HIDDEN_TEXT_LAYER", "GARBAGE_GLYPHS", "PUA_GLYPHS",
                     "MOJIBAKE_NOT_REPAIRABLE", "REPAIRABLE_CP1251_REMAP", "EMBEDDED_TEXT_LAYER"):
                page.quality_flags.append(f)
            if f == "VISIBLE_TEXT_OVER_IMAGE":
                page.quality_flags.append("TEXT_LAYER_VISIBLE_MODE")
        if page.page_class == "BROKEN_TEXT_LAYER":
            page.quality_flags.append("BROKEN_TEXT_LAYER")
        return page

    def _native_blocks(self, page: PageX, raw: dict[str, Any], regions: list[Region], primary: bool) -> list[BlockX]:
        """Blocks of the file's own layer (PDF text or DjVu zones); typed and ordered by the layout regions."""
        route = page.route
        # H-02: text over a scanned image is someone's OCR, never NATIVE — also when the layer is too weak to be
        # used (route OCR_REQUIRED on a RASTER_SCAN page)
        embedded = route == "OCR_OPTIONAL" or self.fmt == "DJVU" or page.page_class == "RASTER_SCAN"
        origin = "EMBEDDED_OCR" if embedded else "NATIVE"
        if self.fmt == "DJVU":
            text_layer, region_origin, extractor = "DJVU_EMBEDDED_OCR_LAYER", "DJVU_TEXT_ZONE", "djvulibre-cli"
            units = [(tuple(ln["bbox"]), ln["text"], i, ln.get("para"), ln.get("region"))
                     for i, ln in enumerate(raw.get("lines", []))]
        else:
            from vkm_corpus.extract.pdf_native import block_text

            text_layer = "PDF_EMBEDDED_OCR_LAYER" if embedded else "PDF_TEXT_LAYER"
            region_origin, extractor = "PDF_TEXT_BLOCK", "pymupdf-native"
            units = [(tuple(b["bbox"]), block_text(b), b["n"], None, None) for b in raw.get("blocks", [])]
        if not units:
            return []
        text_regions = [rg for rg in regions if rg.family == "text"]
        owner = assign([u[0] for u in units], text_regions, ("text",), min_share=0.5)
        groups: list[tuple[Any, list[int]]] = []
        if self.fmt == "DJVU":
            # lines are grouped into one block per layout region; lines outside regions by their zone paragraph
            by_key: dict[Any, list[int]] = {}
            for i, (bb, _, n, para, reg) in enumerate(units):
                key = ("R", owner[i]) if owner[i] is not None else ("P", para if para is not None else f"l{n}")
                by_key.setdefault(key, []).append(i)
            groups = list(by_key.items())
        else:
            groups = [(("B", i), [i]) for i in range(len(units))]
        blocks: list[BlockX] = []
        for key, idxs in groups:
            bbs = [units[i][0] for i in idxs]
            bbox = (min(b[0] for b in bbs), min(b[1] for b in bbs), max(b[2] for b in bbs), max(b[3] for b in bbs))
            text = "\n".join(units[i][1] for i in idxs)
            rid = owner[idxs[0]]
            region = text_regions[rid] if rid is not None else None
            flags: list[str] = []
            if route == "NATIVE_REPAIR" and not embedded:
                from vkm_corpus.extract.text_repair import remap_cp1251

                text = remap_cp1251(text)
                flags.append("ENCODING_REPAIRED")
            if page.page_class == "BROKEN_TEXT_LAYER":
                flags.append("BROKEN_TEXT_LAYER")
            if embedded:
                flags.append("EMBEDDED_TEXT_LAYER")
            b = BlockX(page_index=page.page_index, bbox=bbox, origin=origin, region_origin=region_origin,
                       extractor_id=extractor, extractor_version=EXTRACTOR_VERSIONS[extractor],
                       generation=str(GENERATIONS[extractor]), raw_config_hash=self.native_hash,
                       models=[LAYOUT_MODEL] if region is not None else [],
                       raw_artifact_id=page.native_raw_artifact_id,
                       raw_artifacts=[("EMBEDDED_TEXT_LAYER" if embedded else "NATIVE_EXTRACT",
                                       page.native_raw_artifact_id)],
                       raw_locator=(f"/lines/{idxs[0]}" if self.fmt == "DJVU" else f"/blocks/n={units[idxs[0]][2]}"),
                       quality_flags=flags, text=text, block_type=region.block_type if region else "TEXT",
                       text_layer=text_layer, is_primary_layer=primary, native_order=units[idxs[0]][2])
            b.extra["order_key"] = (region.order_rank if region else self._nearest_rank(bbox, text_regions),
                                    units[idxs[0]][2])
            b.extra["layout_label"] = region.label if region else None
            if region is not None:
                b.extra["layout_det_index"] = region.det_index
            blocks.append(b)
        blocks.sort(key=lambda b: b.extra["order_key"])
        for i, b in enumerate(blocks, 1):
            b.reading_order = i
            b.extra["reading_order_method"] = "LAYOUT_REGION_ORDER" if regions else "NATIVE_STREAM"
        return blocks

    def _crops(self, specs: list[Any]) -> list[Any]:
        """Crops of a page's OCR tasks: from the OCR phase's crop manifest when cached, otherwise re-rendered."""
        from vkm_corpus.pipeline.ocr_stage import crop_settings, make_page_crops, manifest_crops

        if not specs:
            return []
        crops = manifest_crops(self.cfg, self.cache, self.fmt, self.src.sha256, specs)
        if crops is not None:
            return crops
        return make_page_crops(crop_settings(self.cfg), self.fmt, str(self.path), specs)

    def _page_steps(self, page: PageX, row: dict[str, Any], vis: dict[str, Any]) -> None:
        """NATIVE_TEXT and LAYOUT steps of a page (EXECUTED in this run or REUSED_CACHED)."""
        from vkm_corpus.contracts.signatures import stage_signature

        unit = "r" if self.fmt == "DOCX" else "p"
        this_run = self.cache.run_id
        sig = stage_signature(source_sha256=self.src.sha256, stage="NATIVE_TEXT", pipeline_version=_pv(),
                              extractor_id="djvulibre-cli" if self.fmt == "DJVU" else "pymupdf-native",
                              extractor_version="0.1.0", stage_config_hash=self.native_hash, page_unit=unit,
                              page_index=page.page_index)
        self.step("NATIVE_TEXT", page.page_index, "EXECUTED" if self.prep.get("run_id") == this_run else
                  "REUSED_CACHED", "NATIVE_OK" if row.get("route") != "FAILED" else "FAILED", sig,
                  "djvulibre-cli" if self.fmt == "DJVU" else "pymupdf-native", self.native_hash,
                  output_artifact_ids=[a for a in [page.native_raw_artifact_id] if a])
        if vis.get("layout_call_signature"):
            lsig = vis["layout_call_signature"]
            self.step("LAYOUT", page.page_index, "EXECUTED" if vis.get("layout_executed_run") == this_run else
                      "REUSED_CACHED" if vis.get("layout_raw_artifact_id") else "NOT_ATTEMPTED",
                      "NATIVE_OK" if vis.get("layout_raw_artifact_id") else "FAILED", lsig, "pp-doclayoutv3-hf",
                      _cfg_hash(self.cfg.stage_config("LAYOUT")), call_signature=lsig,
                      input_artifact_ids=[a for a in [vis.get("render_artifact_id")] if a],
                      output_artifact_ids=[a for a in [vis.get("layout_raw_artifact_id")] if a])

    @staticmethod
    def _nearest_rank(bbox: tuple[float, float, float, float], regions: list[Region]) -> float:
        if not regions:
            return 1e9
        cy = (bbox[1] + bbox[3]) / 2
        best = min(regions, key=lambda r: abs((r.bbox[1] + r.bbox[3]) / 2 - cy))
        return best.order_rank + 0.5

    def _fixed_layout(self) -> None:
        from vkm_corpus.pipeline.ocr_stage import crop_settings, make_page_crops, plan_page_tasks

        cfg = self.cfg
        recog = _recog_model(cfg)
        reocr = self.decision.get("decision") == "REOCR_SOURCE"
        for row in self.prep["pages"]:
            idx = row["page_index"]
            if self.only_pages is not None and idx not in self.only_pages:
                continue
            page = self._page_base(row)
            self.result.pages.append(page)
            if row.get("route") == "FAILED" or row.get("error"):
                e = row.get("error") or {}
                self.err(e.get("code", "NATIVE_EXTRACT_FAILED"), e.get("stage", "NATIVE_TEXT"),
                         e.get("message", "page failed"), idx, e.get("retryable", False), e.get("tool", "vkm_corpus"))
                page.page_status = "FAILED"
                page.native_text_status = "NOT_CHECKED"
                continue
            vis = self.vis_rows.get(idx, {})
            self._page_steps(page, row, vis)
            for key in ("render_error", "layout_error"):
                if vis.get(key):
                    self.err(vis[key]["code"], "RENDER" if key == "render_error" else "LAYOUT", vis[key]["message"],
                             idx, retryable=True)
            regions: list[Region] = []
            if vis.get("layout_raw_artifact_id"):
                try:
                    _, regions = ocr_stage.load_regions(self.store, vis["layout_raw_artifact_id"], page.width_pt,
                                                        page.height_pt, cfg.region_thresholds)
                    page.models.append(LAYOUT_MODEL)
                except Exception as exc:  # noqa: BLE001
                    self.err("ARTIFACT_MISSING", "LAYOUT", f"{type(exc).__name__}: {exc}", idx)
            raw = {}
            if page.native_raw_artifact_id:
                raw = self.store.read_json(page.native_raw_artifact_id)
            # --- native / embedded layer
            has_layer_text = bool(raw.get("blocks") or raw.get("lines"))
            route = page.route
            layer_primary = route in ("NATIVE", "NATIVE_REPAIR", "OCR_OPTIONAL")
            native_blocks = self._native_blocks(page, raw, regions, primary=layer_primary) if has_layer_text else []
            page.native_char_count = sum(len(b.text) for b in native_blocks) if native_blocks else 0
            if route in ("NATIVE", "NATIVE_REPAIR"):
                page.native_text_status = "PRESENT_OK"
            elif route == "OCR_OPTIONAL":
                page.native_text_status = "PRESENT_OK"
                page.embedded_layer_evidence = "DJVU_TXT" if self.fmt == "DJVU" else (
                    "HIDDEN_TEXT_LAYER" if "HIDDEN_TEXT_LAYER" in page.class_flags else "VISIBLE_OVER_IMAGE")
                doc = self.prep.get("document") or {}
                page.text_layer_producer = doc.get("producer") or doc.get("creator") if self.fmt == "PDF" else None
            elif page.page_class == "BROKEN_TEXT_LAYER":
                page.native_text_status = "PRESENT_BROKEN"
            elif route == "EMPTY" or not has_layer_text:
                page.native_text_status = "ABSENT"
            # --- OCR tasks (same plan as the OCR phase)
            specs = plan_page_tasks(cfg, self.fmt, self.src.source_id, row, vis, regions,
                                    sample_b=idx in self.sample_pages, reocr=reocr)
            crops = self._crops(specs)
            ocr_blocks: list[BlockX] = []
            text_specs = [s for s in specs if s.task == "text"]
            text_done = 0
            empty_on_ink = False
            truncated = False
            groups: dict[int, list[Any]] = {}
            for crop in crops:
                groups.setdefault(id(crop.spec), []).append(crop)
            for spec in specs:
                parts = sorted(groups.get(id(spec), []), key=lambda c: c.band_index)
                if any(c.error for c in parts):
                    self.err("RENDER_FAILED", "OCR", next(c.error for c in parts if c.error), idx, retryable=True)
                    continue
                found = []
                for crop in parts:
                    hit, rec = self.ocr_result(crop)
                    self.step("OCR", idx, "REUSED_CACHED" if hit and hit.get("run_id") != self.cache.run_id else
                              "EXECUTED" if hit else "NOT_ATTEMPTED", "OCR_OK" if rec else "OCR_REQUIRED",
                              crop.call_signature, "vkm-glm-ocr-client", self.ocr_hash,
                              call_signature=crop.call_signature, model_id=cfg.model.model_id,
                              model_revision=cfg.model.model_revision,
                              input_artifact_ids=[hit["input_artifact_id"]] if hit and hit.get("input_artifact_id")
                              else [], output_artifact_ids=[hit["raw_artifact_id"]] if hit else [],
                              n_objects_out=1 if rec else 0, attempt=int(hit["attempt"]) if hit else 1)
                    found.append((crop, hit, rec))
                if not parts or any(rec is None for _, _, rec in found):
                    continue  # the region stays pending (all bands are needed)
                contents = [(rec.get("response") or {}).get("content") for _, _, rec in found]
                qflags: list[str] = []
                for crop, hit, rec in found:
                    page.ocr_raw_artifact_ids.append(hit["raw_artifact_id"])
                    resp = rec.get("response") or {}
                    # flags are a function of the raw output: recomputed with the current rule (QUALITY_RULE)
                    cflags = call_flags(resp.get("content"), resp.get("finish_reason"), crop.ink)
                    for f in cflags:
                        if f in ("TRUNCATED", "REPETITION") and f not in qflags:
                            qflags.append(f)
                    if "EMPTY_ON_INK" in cflags:
                        empty_on_ink = True
                    if (rec.get("response") or {}).get("finish_reason") == "length":
                        truncated = True
                        self.err("OCR_TRUNCATED", "OCR", f"finish_reason=length on {spec.role} {spec.task} band "
                                 f"{crop.band_index + 1}/{crop.band_count}", idx, retryable=True, tool="glm-ocr")
                first_hit = found[0][1]
                raw_ids = [h["raw_artifact_id"] for _, h, _ in found]
                models = [LAYOUT_MODEL, recog] if spec.det_index is not None else [recog]
                region_origin = "LAYOUT_MODEL" if spec.det_index is not None else "OCR_MODEL"
                common = dict(page_index=idx, bbox=spec.bbox_pt, origin="OCR", region_origin=region_origin,
                              extractor_id="vkm-glm-ocr-client", extractor_version=EXTRACTOR_VERSIONS[
                                  "vkm-glm-ocr-client"], generation=str(GENERATIONS["vkm-glm-ocr-client"]),
                              raw_config_hash=_cfg_hash({"ocr": self.cfg.stage_config("OCR"), "task": spec.task,
                                                         "regions": self.region_hash}),
                              models=models, raw_artifact_id=raw_ids[0],
                              raw_artifacts=[("OCR_RESPONSE", a) for a in raw_ids] +
                              ([("LAYOUT_DETECTIONS", page.layout_raw_artifact_id)] if spec.det_index is not None
                               else []),
                              raw_locator=f"/detections/{spec.det_index}" if spec.det_index is not None else None,
                              quality_flags=list(qflags))
                if len(found) > 1 and any(c.band_hard_cut for c, _, _ in found):
                    common["quality_flags"].append("BBOX_APPROX")
                if spec.task == "text":
                    text_done += 1
                    from vkm_corpus.layout.regions import BLOCK_TYPE

                    primary = spec.role in ("PRIMARY", "FULL_PAGE", "REOCR")
                    b = BlockX(**common, text="\n".join(c or "" for c in contents),
                               block_type=BLOCK_TYPE.get(spec.label or "text", "TEXT"), text_layer="GLM_OCR",
                               is_primary_layer=primary)
                    b.extra.update({"order_key": (spec.order_rank if spec.order_rank is not None else 0, 0),
                                    "layout_label": spec.label, "role": spec.role, "layout_det_index": spec.det_index,
                                    "ocr_input_artifact_id": first_hit.get("input_artifact_id"),
                                    "bands": len(found)})
                    ocr_blocks.append(b)
                elif spec.task == "table":
                    self._table_from_ocr(page, spec, self._region_image(page, spec, found), contents, common, row)
                elif spec.task == "formula":
                    self._formula_from_ocr(page, spec, first_hit, contents[0], common, raw)
            # --- figures (layout image/chart regions) and native tables without OCR
            self._figures(page, regions, raw)
            # --- primary layer and statuses
            if route == "OCR_OPTIONAL" and reocr and text_specs and text_done == len(text_specs):
                for b in native_blocks:
                    b.is_primary_layer = False
                for b in ocr_blocks:
                    b.is_primary_layer = True
            elif route == "OCR_OPTIONAL":
                for b in ocr_blocks:
                    b.is_primary_layer = False
            ocr_blocks.sort(key=lambda b: b.extra["order_key"])
            for i, b in enumerate(ocr_blocks, 1):
                b.reading_order = i
                b.extra["reading_order_method"] = "LAYOUT_REGION_ORDER"
            if route == "OCR_REQUIRED" and page.page_class == "BROKEN_TEXT_LAYER":
                for b in native_blocks:
                    b.is_primary_layer = False
            self.result.blocks.extend(native_blocks)
            self.result.blocks.extend(ocr_blocks)
            self._page_status(page, route, text_specs, text_done, ocr_blocks, native_blocks, empty_on_ink, truncated,
                              specs, crops)
            if route == "OCR_OPTIONAL" and ocr_blocks and native_blocks:
                layer = "\n".join(b.text for b in sorted(native_blocks, key=lambda b: b.reading_order or 0))
                glm = "\n".join(onorm.text_from_markdown(b.text) for b in ocr_blocks)
                value, dist, ref = scenario_b.cer(layer, glm)
                page.native_ocr_cer = value
                page.extra["scenario_b"] = {"distance": dist, "ref_len": ref}
                from vkm_corpus.extract.text_repair import letter_share

                page.extra["scenario_b"]["layer_letter_share"] = letter_share(layer)

    def _page_status(self, page: PageX, route: str | None, text_specs: list[Any], text_done: int,
                     ocr_blocks: list[BlockX], native_blocks: list[BlockX], empty_on_ink: bool, truncated: bool,
                     specs: list[Any], crops: list[Any]) -> None:
        ocr_total = len(specs)
        ocr_done = sum(1 for c in crops if c.call_signature and self.cache.best_call(c.call_signature))
        if route == "EMPTY" and not text_specs:
            page.page_status = "NATIVE_OK"
            page.primary_text_origin = "NONE"
            page.quality_flags.append("EMPTY_PAGE")
            page.ocr_status = "NOT_REQUIRED"
        elif route in ("NATIVE", "NATIVE_REPAIR"):
            page.page_status = "NATIVE_OK"
            page.primary_text_origin = "NATIVE" if native_blocks else "NONE"
            page.primary_text_layer = "PDF_TEXT_LAYER" if native_blocks else "NONE"
        elif route == "OCR_OPTIONAL":
            glm_primary = any(b.is_primary_layer for b in ocr_blocks)
            page.page_status = "OCR_OK" if glm_primary else "EMBEDDED_TEXT_OK"
            page.primary_text_origin = "OCR" if glm_primary else "EMBEDDED_OCR"
            page.primary_text_layer = "GLM_OCR" if glm_primary else (
                "DJVU_EMBEDDED_OCR_LAYER" if self.fmt == "DJVU" else "PDF_EMBEDDED_OCR_LAYER")
        else:  # OCR_REQUIRED (incl. EMPTY with ink)
            page.primary_text_origin = "OCR" if ocr_blocks else "NONE"
            page.primary_text_layer = "GLM_OCR" if ocr_blocks else "NONE"
            if not text_specs:
                page.page_status = "OCR_REQUIRED"  # regions pending (layout missing): planned, not done
            elif text_done == 0:
                page.page_status = "OCR_REQUIRED"
            elif text_done < len(text_specs):
                page.page_status = "PARTIAL"
            else:
                page.page_status = "OCR_OK"
                if not any(b.text.strip() for b in ocr_blocks):
                    page.quality_flags.append("EMPTY_PAGE")
            if empty_on_ink and page.page_status == "OCR_OK":
                page.page_status = "NEEDS_REVIEW"
                self.err("OCR_EMPTY_ON_INK", "OCR", "empty recognition on a crop with ink", page.page_index)
        if truncated and page.page_status in ("OCR_OK", "EMBEDDED_TEXT_OK", "NATIVE_OK"):
            page.page_status = "PARTIAL"
            page.quality_flags.append("TRUNCATED")
        if ocr_total == 0:
            page.ocr_status = "REQUIRED" if route == "OCR_REQUIRED" else "NOT_REQUIRED"
        elif ocr_done == ocr_total:
            page.ocr_status = "DONE"
        elif ocr_done == 0:
            page.ocr_status = "REQUIRED"
        else:
            page.ocr_status = "PARTIAL"
        page.recognized_char_count = sum(len(b.text) for b in ocr_blocks) if ocr_blocks else None

    # ------------------------------------------------------------------ objects
    def _region_image(self, page: PageX, spec: Any, found: list[tuple[Any, dict[str, Any], Any]]) -> str | None:
        """Image of a whole OCR region: the model input itself for one band; for a banded region its band inputs
        stacked in order (bands tile the crop exactly), stored as TABLE_CROP."""
        if len(found) == 1:
            return found[0][1].get("input_artifact_id")
        import io

        from PIL import Image

        from vkm_corpus.artifacts.render import png_bytes

        parts = []
        for crop, hit, _ in sorted(found, key=lambda x: x[0].band_index):
            aid = hit.get("input_artifact_id")
            if not aid:
                return found[0][1].get("input_artifact_id")
            parts.append(Image.open(io.BytesIO(self.store.read_bytes(aid))).convert("L"))
        w, h = max(p.width for p in parts), sum(p.height for p in parts)
        full = Image.new("L", (w, h), 255)
        y = 0
        for p in parts:
            full.paste(p, (0, y))
            y += p.height
        rec = self.store.put_bytes(png_bytes(full), "TABLE_CROP", "image/png", source_id=self.src.source_id,
                                   page_id=f"{self.src.source_id}:p{page.page_index:04d}", image_width_px=w,
                                   image_height_px=h, image_dpi=spec.crop_dpi or None)
        return rec.artifact_id

    def _table_from_ocr(self, page: PageX, spec: Any, image_artifact_id: str | None, contents: list[str | None],
                        common: dict[str, Any], row: dict[str, Any]) -> None:
        norm = onorm.normalize_table_bands(contents)
        content = "\n".join(c or "" for c in contents)
        flags = list(common["quality_flags"])
        if not norm["structure_ok"]:
            flags.append("TABLE_STRUCTURE_UNCERTAIN")
        if any(c["row_span"] > 1 or c["col_span"] > 1 for c in norm["cells"]):
            flags.append("SPANNING_CELLS")
        method = "OCR_GLM"
        raw_artifacts = list(common["raw_artifacts"])
        audit = self.store.put_json({"schema": "vkm.table_structure_audit/1", "rule": onorm.OCR_NORMALIZE_RULE,
                                    "status": "DERIVATION", "input_artifacts": raw_artifacts,
                                    "raw_grid": norm.get("raw_grid"), "dispositions": norm.get("dispositions", []),
                                    "band_audits": norm.get("band_audits", []),
                                    "normalized_rows": norm["n_rows"], "normalized_cols": norm["n_cols"]},
                                   "VALIDATION_REPORT", source_id=self.src.source_id,
                                   page_id=f"{self.src.source_id}:p{page.page_index:04d}")
        raw_artifacts.append(("STRUCTURAL_DIAGNOSTICS", audit.artifact_id))
        if self.fmt == "PDF" and page.route in ("NATIVE", "NATIVE_REPAIR") and spec.bbox_pt:
            native = self._native_table(page, spec.bbox_pt)
            if native is not None:
                art = self.store.put_json(native, "NATIVE_RAW", source_id=self.src.source_id,
                                          page_id=f"{self.src.source_id}:p{page.page_index:04d}")
                raw_artifacts.append(("NATIVE_TABLE_FINDER", art.artifact_id))
                agree = native["n_rows"] == norm["n_rows"] and native["n_cols"] == norm["n_cols"]
                method = "BOTH_AGREE" if agree else "BOTH_DISAGREE"
                if not agree:
                    flags.append("NATIVE_OCR_DISAGREE")
        t = TableX(**{**common, "quality_flags": flags, "raw_artifacts": raw_artifacts}, raw_format=norm["raw_format"],
                   raw_output=content or "", recognition_method=method, n_rows=norm["n_rows"], n_cols=norm["n_cols"],
                   cells=norm["cells"], normalized_text=norm["normalized_text"],
                   image_artifact_id=image_artifact_id, image_dpi=spec.crop_dpi,
                   layout_score=spec.score)
        t.extra["layout_label"] = spec.label
        self.result.tables.append(t)

    def _native_table(self, page: PageX, bbox: tuple[float, float, float, float]) -> dict[str, Any] | None:
        from vkm_corpus.extract.pdf_native import derotate_bbox, rot_bbox

        try:
            pg = self.pdf()[page.page_index - 1]
            found = pg.find_tables(clip=derotate_bbox(bbox, pg))
        except Exception:  # noqa: BLE001 - the OCR table stays; the native finder is optional
            return None
        best = None
        for tb in found.tables:
            tb_bbox = rot_bbox(tb.bbox, pg.rotation_matrix)
            share = overlap_share(tb_bbox, bbox)
            if share < 0.5:
                continue
            cells = tb.extract()
            if best is None or share > best[0]:
                best = (share, tb_bbox, cells, tb.row_count, tb.col_count)
        if best is None:
            return None
        _, tb_bbox, cells, nr, nc = best
        return {"schema": "vkm.native_raw.table_finder/1", "extractor": "pymupdf-find-tables", "bbox": list(tb_bbox),
                "n_rows": nr, "n_cols": nc, "cells": cells}

    def _formula_from_ocr(self, page: PageX, spec: Any, hit: dict[str, Any], content: str | None,
                          common: dict[str, Any], raw: dict[str, Any]) -> None:
        norm = onorm.normalize_formula(content)
        flags = list(common["quality_flags"])
        if norm["latex_parse_ok"] is False:
            flags.append("FORMULA_LATEX_UNPARSEABLE")
        glyphs = None
        if self.fmt == "PDF" and raw.get("blocks") and spec.bbox_pt and page.route in ("NATIVE", "NATIVE_REPAIR"):
            from vkm_corpus.extract.pdf_native import text_in_bbox

            glyphs = text_in_bbox(raw, spec.bbox_pt) or None
        kind = "DISPLAY" if spec.label == "display_formula" else "INLINE" if spec.label == "inline_formula" else \
            "UNKNOWN"
        f = FormulaX(**{**common, "quality_flags": flags}, formula_kind=kind, equation_label=norm["equation_label"],
                     raw_format="LATEX" if content else "IMAGE_ONLY", raw_output=content if content else None,
                     normalized_latex=norm["normalized_latex"], latex_parse_ok=norm["latex_parse_ok"],
                     native_glyph_text=glyphs, recognition_method="OCR_GLM",
                     image_artifact_id=hit.get("input_artifact_id"), layout_score=spec.score)
        f.extra["layout_label"] = spec.label
        self.result.formulas.append(f)

    def _figures(self, page: PageX, regions: list[Region], raw: dict[str, Any]) -> None:
        figs = [r for r in regions if r.family == "figure" and r.label in ("image", "chart")]
        if not figs:
            return
        from vkm_corpus.artifacts.render import crop_pt, png_bytes

        raster = None
        try:
            if self.fmt == "DJVU":
                from vkm_corpus.artifacts.render import render_djvu_page

                raster = render_djvu_page(self.path, page.page_index, 200, "RGB", "fig200")
            else:
                from vkm_corpus.artifacts.render import render_pdf_page

                raster = render_pdf_page(self.pdf()[page.page_index - 1], 200, "RGB", "fig200")
        except Exception as exc:  # noqa: BLE001
            self.err("RENDER_FAILED", "FIGURES", f"{type(exc).__name__}: {exc}", page.page_index, retryable=True)
        svg_art = None
        for rg in figs:
            flags: list[str] = ["FIGURE_TYPE_LOW_CONFIDENCE"]
            img_art = None
            if raster is not None:
                try:
                    img = crop_pt(raster, rg.bbox, 2.0)
                    rec = self.store.put_bytes(png_bytes(img), "FIGURE_CROP", "image/png",
                                               source_id=self.src.source_id,
                                               page_id=f"{self.src.source_id}:p{page.page_index:04d}",
                                               image_width_px=img.width, image_height_px=img.height, image_dpi=200)
                    img_art = rec.artifact_id
                except Exception:  # noqa: BLE001
                    img_art = None
            emb = None
            vectors: list[tuple[str, str]] = []
            if self.fmt == "PDF" and raw:
                pg = self.pdf()[page.page_index - 1]
                try:
                    from vkm_corpus.extract.pdf_native import embedded_image_for

                    emb = embedded_image_for(self.pdf(), pg, raw, rg.bbox)
                except Exception:  # noqa: BLE001
                    emb = None
                rects = raw.get("path_rects") or []
                inside = [r for r in rects if overlap_share(tuple(r), rg.bbox) >= 0.5]
                if inside:
                    try:
                        from vkm_corpus.extract.pdf_native import drawings_json, page_svg

                        paths = drawings_json(pg, clip=rg.bbox)
                        prec = self.store.put_json(paths, "VECTOR_PATHS_JSON", compress=True,
                                                   source_id=self.src.source_id,
                                                   page_id=f"{self.src.source_id}:p{page.page_index:04d}")
                        vectors.append(("PATHS_JSON", prec.artifact_id))
                        if svg_art is None:
                            svg_art = self.store.put_bytes(page_svg(pg), "VECTOR_SVG", "image/svg+xml",
                                                           source_id=self.src.source_id,
                                                           page_id=f"{self.src.source_id}:p{page.page_index:04d}"
                                                           ).artifact_id
                        vectors.append(("SVG", svg_art))
                    except Exception as exc:  # noqa: BLE001
                        self.err("NATIVE_EXTRACT_FAILED", "NATIVE_VECTOR", f"{type(exc).__name__}: {exc}",
                                 page.page_index)
            emb_id, transcoded, filt = None, None, None
            if emb and not emb.get("ambiguous"):
                media = {"jpeg": "image/jpeg", "jpg": "image/jpeg", "png": "image/png", "jpx": "application/octet-stream",
                         "jb2": "application/octet-stream", "tiff": "image/tiff", "gif": "image/gif"}.get(
                    (emb.get("ext") or "").lower(), "application/octet-stream")
                erec = self.store.put_bytes(emb["bytes"], "EMBEDDED_IMAGE", media, source_id=self.src.source_id,
                                            page_id=f"{self.src.source_id}:p{page.page_index:04d}",
                                            image_width_px=emb.get("width"), image_height_px=emb.get("height"))
                emb_id, transcoded, filt = erec.artifact_id, bool(emb.get("transcoded")), emb.get("original_filter")
            layout_class = "CHART" if rg.label == "chart" else (
                "MIXED" if emb_id and vectors else "RASTER_IMAGE" if emb_id else "VECTOR_GRAPHICS" if vectors else
                "LAYOUT_REGION")
            fig = FigureX(page_index=page.page_index, bbox=rg.bbox, origin="NATIVE" if self.fmt == "PDF" and
                          page.route in ("NATIVE", "NATIVE_REPAIR") else "OCR" if False else "NATIVE",
                          region_origin="LAYOUT_MODEL", extractor_id="pp-doclayoutv3-hf",
                          extractor_version=EXTRACTOR_VERSIONS["pp-doclayoutv3-hf"],
                          generation=str(GENERATIONS["pp-doclayoutv3-hf"]), raw_config_hash=self.region_hash,
                          models=[LAYOUT_MODEL], raw_artifact_id=page.layout_raw_artifact_id,
                          raw_artifacts=[("LAYOUT_DETECTIONS", page.layout_raw_artifact_id)],
                          raw_locator=f"/detections/{rg.det_index}", quality_flags=flags,
                          layout_class=layout_class, layout_label=rg.label, layout_score=rg.score,
                          image_artifact_id=img_art, image_dpi=200 if img_art else None,
                          embedded_image_artifact_id=emb_id, embedded_image_transcoded=transcoded,
                          original_filter=filt, vector_artifacts=vectors)
            fig.extra["order_rank"] = rg.order_rank
            self.result.figures.append(fig)

    # ------------------------------------------------------------------ EPUB
    def _epub(self) -> None:
        from vkm_corpus.pipeline.ocr_stage import make_page_crops, plan_epub_tasks

        cfg = self.cfg
        recog = _recog_model(cfg)
        epub_hash = _cfg_hash({"epub": "blocks_v2_mathml"})
        source_page_list = (self.prep.get("document") or {}).get("page_list_source")
        for row in self.prep["pages"]:
            idx = row["page_index"]
            page = PageX(page_index=idx, page_kind="EPUB_SPINE_ITEM", bbox_space="NONE", page_class="REFLOWABLE",
                         route=row.get("route"), route_reason=row.get("route_reason"),
                         native_raw_artifact_id=row.get("native_raw_artifact_id"), spine_href=row.get("spine_href"),
                         rotation_deg=0)
            self.result.pages.append(page)
            if row.get("route") == "FAILED":
                self.err("NATIVE_EXTRACT_FAILED", "NATIVE_TEXT", (row.get("error") or {}).get("message", ""), idx)
                page.page_status = "FAILED"
                continue
            raw = self.store.read_json(page.native_raw_artifact_id)
            anchors = raw.get("page_anchors") or []
            if anchors:
                page.printed_page_labels = [a[0] for a in anchors]
                page.printed_page_raw = ",".join(page.printed_page_labels)
                page.printed_label_origin = "EPUB_PAGE_ANCHOR"
                page.printed_label_extractor = "epub-xhtml 0.1.0"
                if source_page_list == "CALIBRE_PAGE_ID":
                    page.quality_flags.append("EPUB_PAGE_ANCHOR_CALIBRE")
            ordinal = 0
            for b in raw.get("blocks", []):
                ordinal += 1
                blk = BlockX(page_index=idx, bbox=None, origin="NATIVE", region_origin="EPUB_ELEMENT",
                             extractor_id="epub-xhtml", extractor_version=EXTRACTOR_VERSIONS["epub-xhtml"],
                             generation=str(GENERATIONS["epub-xhtml"]), raw_config_hash=epub_hash,
                             raw_artifact_id=page.native_raw_artifact_id,
                             raw_artifacts=[("SOURCE_MARKUP", page.native_raw_artifact_id)],
                             raw_locator=b["xpath"], text=b["text"], block_type=b["block_type"],
                             text_layer="EPUB_XHTML", is_primary_layer=True, reading_order=ordinal,
                             anchor_ordinal=ordinal)
                blk.extra.update({"element_path": b["xpath"], "element_id": b.get("element_id"),
                                  "reading_order_method": "DOCUMENT_ORDER"})
                self.result.blocks.append(blk)
            for t in raw.get("tables", []):
                norm = onorm.normalize_table(t["html"], preserve_empty=True)
                tb = TableX(page_index=idx, bbox=None, origin="NATIVE", region_origin="EPUB_ELEMENT",
                            extractor_id="epub-xhtml", extractor_version=EXTRACTOR_VERSIONS["epub-xhtml"],
                            generation=str(GENERATIONS["epub-xhtml"]), raw_config_hash=epub_hash,
                            raw_artifact_id=page.native_raw_artifact_id,
                            raw_artifacts=[("SOURCE_MARKUP", page.native_raw_artifact_id)], raw_locator=t["xpath"],
                            raw_format="XHTML", raw_output=t["html"], recognition_method="EPUB_XHTML",
                            n_rows=norm["n_rows"], n_cols=norm["n_cols"], cells=norm["cells"],
                            normalized_text=norm["normalized_text"], anchor_ordinal=t["order"])
                tb.extra["element_path"] = t["xpath"]
                self.result.tables.append(tb)
            for m in raw.get("maths", []):
                formula = FormulaX(page_index=idx, bbox=None, origin="NATIVE", region_origin="EPUB_ELEMENT",
                    extractor_id="epub-xhtml", extractor_version=EXTRACTOR_VERSIONS["epub-xhtml"],
                    generation=str(GENERATIONS["epub-xhtml"]), raw_config_hash=epub_hash,
                    raw_artifact_id=page.native_raw_artifact_id,
                    raw_artifacts=[("SOURCE_MARKUP", page.native_raw_artifact_id)], raw_locator=m["xpath"],
                    formula_kind="DISPLAY" if m.get("display") else "INLINE", raw_format="MATHML",
                    raw_output=m["markup"], native_glyph_text=m.get("linear_text"),
                    normalized_latex=None, latex_parse_ok=None, recognition_method="NATIVE_MATHML",
                    anchor_ordinal=m["order"])
                formula.extra["element_path"] = m["xpath"]
                self.result.formulas.append(formula)
            specs = plan_epub_tasks(self.src.source_id, raw)
            crops = {c.spec.epub_image_order: c for c in self._crops(specs)}
            for im in raw.get("images", []):
                data = None
                try:
                    from vkm_corpus.extract.epub import read_member

                    data = read_member(self.path, im["href"])
                except Exception as exc:  # noqa: BLE001
                    self.err("NATIVE_EXTRACT_FAILED", "NATIVE_IMAGES", f"{im['href']}: {exc}", idx)
                media = im.get("media_type") or "application/octet-stream"
                emb = self.store.put_bytes(data, "EMBEDDED_IMAGE", media if media.startswith("image/") else
                                           "application/octet-stream", source_id=self.src.source_id,
                                           page_id=f"{self.src.source_id}:s{idx:04d}") if data else None
                if im.get("is_formula_candidate"):
                    crop = crops.get(im["order"])
                    hit, rec = (self.ocr_result(crop) if crop and not crop.error else (None, None))
                    content = (rec or {}).get("response", {}).get("content") if rec else None
                    norm = onorm.normalize_formula(content)
                    if crop and not crop.error:
                        self.step("OCR", idx, "REUSED_CACHED" if hit and hit.get("run_id") != self.cache.run_id else
                                  "EXECUTED" if hit else "NOT_ATTEMPTED", "OCR_OK" if rec else "OCR_REQUIRED",
                                  crop.call_signature, "vkm-glm-ocr-client", self.ocr_hash,
                                  call_signature=crop.call_signature, model_id=cfg.model.model_id,
                                  model_revision=cfg.model.model_revision,
                                  output_artifact_ids=[hit["raw_artifact_id"]] if hit else [])
                    f = FormulaX(page_index=idx, bbox=None, origin="OCR" if rec else "NATIVE",
                                 region_origin="EPUB_ELEMENT",
                                 extractor_id="vkm-glm-ocr-client" if rec else "epub-xhtml",
                                 extractor_version=EXTRACTOR_VERSIONS["vkm-glm-ocr-client" if rec else "epub-xhtml"],
                                 generation=str(GENERATIONS["vkm-glm-ocr-client" if rec else "epub-xhtml"]),
                                 raw_config_hash=self.ocr_hash if rec else epub_hash,
                                 models=[recog] if rec else [],
                                 raw_artifact_id=hit["raw_artifact_id"] if hit else page.native_raw_artifact_id,
                                 raw_artifacts=([("OCR_RESPONSE", hit["raw_artifact_id"])] if hit else []) +
                                 [("SOURCE_MARKUP", page.native_raw_artifact_id)],
                                 raw_locator=im["xpath"], formula_kind="INLINE" if im.get("inline") else "DISPLAY",
                                 equation_label=im.get("equation_label") or norm["equation_label"],
                                 raw_format="LATEX" if content else "IMAGE_ONLY", raw_output=content or None,
                                 normalized_latex=norm["normalized_latex"], latex_parse_ok=norm["latex_parse_ok"],
                                 recognition_method="EPUB_IMAGE_OCR" if rec else "NONE",
                                 image_artifact_id=emb.artifact_id if emb else None, anchor_ordinal=im["order"])
                    f.extra.update({"element_path": im["xpath"], "epub_member": im["href"]})
                    if rec:
                        page.ocr_raw_artifact_ids.append(hit["raw_artifact_id"])
                    self.result.formulas.append(f)
                else:
                    fig = FigureX(page_index=idx, bbox=None, origin="NATIVE", region_origin="EPUB_ELEMENT",
                                  extractor_id="epub-xhtml", extractor_version=EXTRACTOR_VERSIONS["epub-xhtml"],
                                  generation=str(GENERATIONS["epub-xhtml"]), raw_config_hash=epub_hash,
                                  raw_artifact_id=page.native_raw_artifact_id,
                                  raw_artifacts=[("SOURCE_MARKUP", page.native_raw_artifact_id)],
                                  raw_locator=im["xpath"], quality_flags=["FIGURE_TYPE_LOW_CONFIDENCE"],
                                  layout_class="RASTER_IMAGE", embedded_image_artifact_id=emb.artifact_id if emb else
                                  None, embedded_image_transcoded=False if emb else None,
                                  anchor_ordinal=im["order"])
                    fig.extra.update({"element_path": im["xpath"], "alt": im.get("alt")})
                    self.result.figures.append(fig)
            n_formula = sum(1 for im in raw.get("images", []) if im.get("is_formula_candidate"))
            n_done = sum(1 for f in self.result.formulas if f.page_index == idx and f.raw_output
                         and f.recognition_method == "EPUB_IMAGE_OCR")
            page.native_char_count = sum(len(b["text"]) for b in raw.get("blocks", []))
            page.native_text_status = "PRESENT_OK" if page.native_char_count else "ABSENT"
            page.page_status = "NATIVE_OK"
            page.primary_text_origin = "NATIVE" if page.native_char_count else "NONE"
            page.primary_text_layer = "EPUB_XHTML" if page.native_char_count else "NONE"
            page.ocr_status = "NOT_REQUIRED" if not n_formula else "DONE" if n_done == n_formula else \
                "REQUIRED" if n_done == 0 else "PARTIAL"
            if n_formula:
                page.models.append(recog)

    # ------------------------------------------------------------------ DOCX
    def _docx(self) -> None:
        from vkm_corpus.extract.docx import align_to_pages

        raw = self.store.read_json(self.prep["document_raw_artifact_id"])
        docx_hash = _cfg_hash({"docx": "blocks_v2_parts"})
        page_texts = []
        for row in self.prep["pages"]:
            page = self._page_base(row)
            page.page_kind = "DOCX_RENDERED_PAGE"
            page.page_status = "NATIVE_OK"
            page.native_text_status = "NOT_APPLICABLE"
            page.primary_text_origin = "NATIVE"
            page.primary_text_layer = "DOCX_XML"
            page.printed_label_origin = "NONE"
            self.result.pages.append(page)
            text = ""
            if row.get("native_raw_artifact_id"):
                nraw = self.store.read_json(row["native_raw_artifact_id"])
                from vkm_corpus.extract.pdf_native import block_text

                text = "\n".join(block_text(b) for b in nraw.get("blocks", []))
            page_texts.append(text)
        paras = raw.get("paragraphs", [])
        tables = raw.get("tables", [])
        # Auxiliary parts and text boxes do not advance the body reading cursor.
        body = [p for p in paras if p.get("container", "BODY") == "BODY"]
        body_alignment = dict(zip((p["path"] for p in body), align_to_pages([p["text"] for p in body], page_texts)))
        aligned = [body_alignment.get(p["path"], (None, 0.0)) for p in paras]
        common = dict(bbox=None, origin="NATIVE", region_origin="DOCX_ELEMENT", extractor_id="docx-xml",
                      extractor_version=EXTRACTOR_VERSIONS["docx-xml"], generation=str(GENERATIONS["docx-xml"]),
                      raw_config_hash=docx_hash, raw_artifact_id=self.prep["document_raw_artifact_id"],
                      raw_artifacts=[("SOURCE_MARKUP", self.prep["document_raw_artifact_id"])])
        ro = 0
        page_of_path: dict[str, int | None] = {}
        for p, (pg, score) in zip(paras, aligned):
            page_of_path[p["path"]] = pg
            if not p["text"].strip():
                continue
            ro += 1
            b = BlockX(page_index=pg or 0, **common, raw_locator=p["path"], text=p["text"],
                       block_type=p["block_type"], text_layer="DOCX_XML", is_primary_layer=True, reading_order=ro,
                       anchor_ordinal=p["order"])
            b.extra.update({"docx_paragraph_path": p["path"], "render_page_index": pg, "render_alignment_score": score,
                            "render_page_status": "RENDER_DEPENDENT", "style": p.get("style"),
                            "reading_order_method": "DOCUMENT_ORDER"})
            self.result.blocks.append(b)
        for t in tables:
            norm_text = t.get("text")
            pg, score = align_to_pages([norm_text or ""], page_texts)[0] if norm_text else (None, 0.0)
            tb = TableX(page_index=pg or 0, **common, raw_locator=t["path"], raw_format="DOCX_XML",
                        raw_output=t["xml"], recognition_method="DOCX_XML", n_rows=t["n_rows"], n_cols=t["n_cols"],
                        cells=t["cells"], normalized_text=norm_text, anchor_ordinal=t["order"])
            tb.extra.update({"docx_paragraph_path": t["path"], "render_page_index": pg,
                             "render_page_status": "RENDER_DEPENDENT", "render_alignment_score": score})
            if any(c["row_span"] > 1 or c["col_span"] > 1 for c in t["cells"]):
                tb.quality_flags.append("SPANNING_CELLS")
            self.result.tables.append(tb)
        for m in raw.get("maths", []):
            pg = page_of_path.get(m["paragraph_path"])
            f = FormulaX(page_index=pg or 0, **common, raw_locator=f"{m['paragraph_path']}#math{m['order']}",
                         formula_kind="DISPLAY" if m["display"] else "INLINE", raw_format="OMML",
                         raw_output=m["omml"], normalized_latex=None, latex_parse_ok=None,
                         native_glyph_text=m.get("linear_text") or None, recognition_method="NATIVE_OMML",
                         anchor_ordinal=m["order"])
            f.extra.update({"docx_paragraph_path": m["paragraph_path"], "render_page_index": pg,
                            "render_page_status": "RENDER_DEPENDENT", "math_order": m["order"]})
            self.result.formulas.append(f)
        from vkm_corpus.extract.docx import read_member

        for im in raw.get("images", []):
            pg = page_of_path.get(im["paragraph_path"])
            emb = None
            try:
                data = read_member(self.path, im["member"])
                media = im["media_type"] if im["media_type"] in ("image/png", "image/jpeg", "image/gif",
                                                                  "image/tiff") else "application/octet-stream"
                emb = self.store.put_bytes(data, "EMBEDDED_IMAGE", media, source_id=self.src.source_id)
            except Exception as exc:  # noqa: BLE001
                self.err("NATIVE_EXTRACT_FAILED", "NATIVE_IMAGES", f"{im['member']}: {exc}", None)
            fig = FigureX(page_index=pg or 0, **common, raw_locator=f"{im['paragraph_path']}#img{im['order']}",
                          quality_flags=["FIGURE_TYPE_LOW_CONFIDENCE"], layout_class="RASTER_IMAGE",
                          embedded_image_artifact_id=emb.artifact_id if emb else None,
                          embedded_image_transcoded=False if emb else None, anchor_ordinal=im["order"])
            fig.extra.update({"docx_paragraph_path": im["paragraph_path"], "render_page_index": pg,
                              "render_page_status": "RENDER_DEPENDENT", "media_member": im["member"],
                              "media_type": im["media_type"], "descr": im.get("descr")})
            self.result.figures.append(fig)
        for page in self.result.pages:
            page.native_char_count = sum(len(b.text) for b in self.result.blocks
                                         if b.extra.get("render_page_index") == page.page_index)

    # ------------------------------------------------------------------ printed labels, roll-up
    def _printed_labels(self) -> None:
        if self.fmt not in ("PDF", "DJVU"):
            return
        has_rules = bool((self.prep.get("document") or {}).get("has_native_page_labels"))
        for page in self.result.pages:
            if has_rules and page.extra.get("label"):
                raw = page.extra["label"]
                page.printed_page_raw = raw
                page.printed_page_labels = [raw]
                page.printed_label_origin = "PDF_PAGE_LABELS"
                page.printed_label_extractor = "pymupdf " + (self.prep.get("libraries") or {}).get("pymupdf", "")
                continue
            nums = [b for b in self.result.blocks if b.page_index == page.page_index and b.block_type == "PAGE_NUMBER"
                    and b.is_primary_layer]
            labels = []
            for b in sorted(nums, key=lambda x: (x.bbox or (0, 0, 0, 0))[0]):
                s, _ = _parse_label(onorm.text_from_markdown(b.text))
                if s:
                    labels.append(s)
            if labels:
                page.printed_page_labels = labels
                page.printed_page_raw = "-".join(labels) if len(labels) > 1 else labels[0]
                page.printed_label_origin = "RUNNING_HEAD_OCR" if any(b.origin == "OCR" for b in nums) else \
                    "RUNNING_HEAD_NATIVE"
                page.printed_label_extractor = "layout-number-region v1"
        # sequence status
        prev_val = None
        for page in self.result.pages:
            vals = [_parse_label(x)[1] for x in page.printed_page_labels]
            if not page.printed_page_labels:
                page.extra["printed_label_status"] = "NONE"
                prev_val = None
                continue
            if any(v is None for v in vals):
                page.extra["printed_label_status"] = "UNPARSED"
                prev_val = None
                continue
            if prev_val is not None and vals[0] == prev_val + 1:
                page.extra["printed_label_status"] = "CONSISTENT_SEQUENCE"
            else:
                page.extra["printed_label_status"] = "ISOLATED"
            prev_val = vals[-1]

    def _rollup(self) -> str:
        st = [p.page_status for p in self.result.pages]
        pag = self.result.document.pagination if self.result.document else None
        if not st:
            return "FAILED"
        if pag is not None and not pag.agree:
            return "NEEDS_REVIEW"
        if all(s in ("NATIVE_OK", "EMBEDDED_TEXT_OK", "OCR_OK") for s in st):
            return "COMPLETE"
        if all(s == "FAILED" for s in st):
            return "FAILED"
        if any(s == "NEEDS_REVIEW" for s in st) and not any(s in ("FAILED", "PARTIAL", "OCR_REQUIRED") for s in st):
            return "NEEDS_REVIEW"
        return "PARTIAL"
