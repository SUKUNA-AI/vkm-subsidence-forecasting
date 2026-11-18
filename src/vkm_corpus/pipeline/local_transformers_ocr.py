"""Admit a pinned offline Transformers OCR packet into an existing PDF STAGING head."""
from __future__ import annotations

import hashlib
import io
import json
from pathlib import Path

from vkm_corpus.contracts.signatures import config_hash
from vkm_corpus.extract.model import BlockX, FormulaX, ModelRef
from vkm_corpus.layout import ppdoclayout as layout_model
from vkm_corpus.layout.regions import regions_from_raw
from vkm_corpus.ocr.normalize import normalize_formula, text_from_markdown
from vkm_corpus.ocr.prompts import DEFAULT_MODEL, prompt_for
from vkm_corpus.ocr.quality import call_flags

STAGE = "LOCAL_TRANSFORMERS_OCR"
EXTRACTOR = "vkm-glm-ocr-transformers"
VERSION = "0.1.0"
SCHEMA = "vkm.ocr_raw.transformers_page/1"
RECOGNITION = ModelRef("RECOGNITION", DEFAULT_MODEL.model_id, DEFAULT_MODEL.model_revision)
LAYOUT = ModelRef("LAYOUT", layout_model.MODEL_ID, layout_model.MODEL_REVISION)


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def checked(path: Path, digest: str) -> bytes:
    value = path.read_bytes()
    if sha(value) != digest:
        raise ValueError(f"digest mismatch for {path.name}")
    return value


def _raster(data: bytes):
    from PIL import Image

    with Image.open(io.BytesIO(data)) as image:
        return image.convert("RGB")


def read_packet(cfg, source, receipt_path: Path, receipt_sha: str, execution_path: Path, selected):
    """Verify all inputs before opening any staging writer."""
    from vkm_corpus.parquet.commits import local_head, marker_by_id
    from vkm_corpus.parquet.layout import open_root
    from vkm_corpus.registry.sources import fresh_source_identity, resource_path
    from vkm_corpus.pipeline.cache import StageCache
    from vkm_corpus.pipeline.commit import load_state
    import pymupdf

    layout = open_root(cfg.data_root, "STAGING")
    packet_bytes = checked(receipt_path, receipt_sha)
    packet = json.loads(packet_bytes)
    execution_bytes = checked(execution_path, packet["meta_hashes"]["OCR_EXECUTION.json"])
    execution = json.loads(execution_bytes)
    identity = fresh_source_identity(cfg.resources_root, source.canonical_path, source.sha256, source.size_bytes)
    if (packet["scope"] != "LOCAL_PAGE_ARTIFACTS" or packet["source_id"] != source.source_id
            or packet["source_identity"] != identity or identity["file_format"] != "PDF"):
        raise ValueError("packet source differs from the registered original")
    expected = {f"{source.source_id}-p{i:04d}" for i in selected}
    if len(selected) != len(expected) or not selected or set(packet["pages"]) != expected or set(execution["pages"]) != expected:
        raise ValueError("page selection must match the packet exactly")
    run = execution["run"]
    if (execution["admission"] != "AUTO_EXTRACTED_UNREVIEWED"
            or run["source_sha256"] != source.sha256
            or run["model_revision"] != DEFAULT_MODEL.model_revision
            or run["glm_weights_sha256"] != DEFAULT_MODEL.weights_sha256
            or run["layout_weights_sha256"] != layout_model.WEIGHTS_SHA256
            or run["backend"] != "local Transformers direct bf16, no vLLM endpoint or shared service"
            or packet["model"]["actual_backend"] != run["backend"]
            or not run["transformers"] or not run["pytorch"]):
        raise ValueError("packet does not attest the pinned direct Transformers runtime")

    parent = packet["base_staging_heads"][source.source_id]
    cache = StageCache(cfg.data_root, "inspect-local-ocr", read_only=True)
    try:
        existing = source_entry(cfg, cache, source)
    finally:
        cache.close()
    current = local_head(layout, source.source_id)
    for sid, commit in packet["base_staging_heads"].items():
        observed = local_head(layout, sid)
        if sid != source.source_id and observed != commit:
            raise ValueError("other staging head differs from packet baseline")
    if (existing is not None and existing["outputs"]["packet_sha256"] != receipt_sha) or (
            current != parent and (existing is None or not current or
                                   (marker_by_id(layout, current) or {}).get("parent_commit_id") != parent)):
        raise ValueError("packet conflicts with staging head or previous local import")
    prep, _ = load_state(cfg, source)
    if prep is None:
        raise ValueError("the bound PDF PREPARE state is unavailable")
    pages = {r["page_index"]: r for r in prep["pages"]}
    packet_dir = receipt_path.parent

    def file(name):
        if not name or Path(name).name != name or "/" in name or "\\" in name:
            raise ValueError("packet file must be a leaf name")
        path = packet_dir / name
        if path.is_symlink():
            raise ValueError(f"indirect packet file {name}")
        return checked(path, packet["files"][name])

    records = []
    with pymupdf.open(resource_path(cfg.resources_root, source.canonical_path)) as pdf:
        for index in sorted(selected):
            pid = f"{source.source_id}:p{index:04d}"
            key = pid.replace(":", "-")
            row = pages[index]
            if row["route"] != "OCR_REQUIRED":
                raise ValueError(f"{pid} was not an OCR_REQUIRED native page")
            page = pdf[index - 1]
            width, height = row["width_pt"], row["height_pt"]
            pix = page.get_pixmap(dpi=200, colorspace=pymupdf.csRGB, alpha=False)
            raster = _raster(file(key + "-200dpi.png"))
            if (raster.size != (pix.width, pix.height) or raster.tobytes() != pix.samples
                    or abs(width - page.rect.width) > 0.01 or abs(height - page.rect.height) > 0.01
                    or int(row.get("rotation") or 0) != page.rotation):
                raise ValueError(f"{pid} render is not bound to the original")
            item = execution["pages"][key]
            layout_bytes = file(item["layout_file"])
            layout = json.loads(layout_bytes)
            model = layout["model"]
            if (sha(layout_bytes) != item["layout_sha256"] or layout["schema"] != layout_model.RAW_SCHEMA
                    or layout["page_id"] != pid or layout["source_id"] != source.source_id
                    or model["model_id"] != layout_model.MODEL_ID
                    or model["model_revision"] != layout_model.MODEL_REVISION
                    or model["weights_sha256"] != layout_model.WEIGHTS_SHA256
                    or layout["input"] != {"dpi": 200, "width": pix.width, "height": pix.height,
                                          "png_sha256": packet["files"][key + "-200dpi.png"]}):
                raise ValueError(f"{pid} layout is not bound to the original render and pin")
            regions = regions_from_raw(layout, width, height)
            calls = []
            seen = set()
            for raw in [item["text"], *item["typed_candidates"]]:
                task = raw["task"]
                region = raw.get("region_idx")
                if (raw["page"] != key or raw["prompt"] != prompt_for(task)
                        or raw["status"] != "EOS" or not 0 < raw["output_tokens"] < raw["max_new_tokens"]
                        or (task == "text") != (region is None) or region in seen):
                    raise ValueError(f"{pid} wrong, repeated or incomplete OCR result")
                seen.add(region)
                image_name = raw["name"] + ("-input.png" if region is None else ".png")
                image_bytes = file(image_name)
                content_bytes = file(raw["text_file"])
                image = _raster(image_bytes)
                base = raster if region is None else raster.crop(raw["clip_px"])
                if (sha(image_bytes) != raw["input_sha256"] or sha(content_bytes) != raw["text_sha256"]
                        or image.size != base.size or image.tobytes() != base.tobytes()
                        or image.size != (raw["image_width"], raw["image_height"])):
                    raise ValueError(f"{pid} OCR input pixels or output bytes do not match")
                content = content_bytes.decode("utf-8")
                if len(content) != raw["chars"] or not content.strip() or call_flags(content, "stop", 0.1):
                    raise ValueError(f"{pid} empty or guarded OCR output")
                box = [0, 0, width, height]
                region_spec = None
                if region is not None:
                    region_spec = regions[region]
                    if region < 0 or task != region_spec.family:
                        raise ValueError(f"{pid} OCR type differs from the layout")
                    x0, y0, x1, y1 = raw["clip_px"]
                    s = 200 / 72
                    bx0, by0, bx1, by1 = (p * s for p in region_spec.bbox)
                    if not (0 <= x0 <= bx0 <= bx1 <= x1 <= pix.width and 0 <= y0 <= by0 <= by1 <= y1 <= pix.height):
                        raise ValueError(f"{pid} OCR crop does not cover its layout occurrence")
                    box = list(region_spec.bbox)
                calls.append((image_bytes, {"result": raw, "content": content, "bbox_pt": box,
                                             "layout_label": region_spec.label if region_spec else None,
                                             "layout_det_index": region_spec.det_index if region_spec else None}))
            if calls[0][1]["result"]["task"] != "text" or not text_from_markdown(calls[0][1]["content"]):
                raise ValueError(f"{pid} has no usable primary OCR text")
            checked_review = packet["pages"][key]
            records.append((index, pid, width, height, layout, file(checked_review["verified_reading"]), calls))
    if fresh_source_identity(cfg.resources_root, source.canonical_path, source.sha256, source.size_bytes) != identity:
        raise ValueError("source changed while validating OCR packet")
    return packet, packet_bytes, execution_bytes, records, existing


def _stage_signature(values):
    return config_hash({"stage": STAGE, "extractor": EXTRACTOR, "version": VERSION, **values})


def source_entry(cfg, cache, source):
    entry = cache.latest_stage(STAGE, source.source_id) if hasattr(cache, "latest_stage") else None
    if entry is None:
        return None
    values = entry["outputs"]
    if cfg.profile != "exploratory" or cfg.local_ocr_packet_sha256 != values["packet_sha256"]:
        raise ValueError("local OCR cache requires its explicit exploratory packet")
    if values["source_sha256"] != source.sha256 or entry["stage_signature"] != _stage_signature(values):
        raise ValueError("local OCR stage source or signature changed")
    return entry


def stage_packet(source, store, cache, receipt_sha, decoded):
    packet, packet_bytes, execution_bytes, records, existing = decoded
    if existing is not None:
        for aid in existing["outputs"]["artifacts"]:
            store.read_bytes(aid)
        return existing
    common = {"source_id": source.source_id}
    provenance = store.put_bytes(packet_bytes, "VALIDATION_REPORT", "application/json", **common)
    report = store.put_bytes(execution_bytes, "RUN_CONFIG", "application/json", **common)
    all_ids = {provenance.artifact_id, report.artifact_id}
    outputs = {}
    for index, pid, width, height, layout, reading, crops in records:
        scope = common | {"page_id": pid}
        readback = store.put_bytes(reading, "VALIDATION_REPORT", "text/markdown", **scope)
        detections = store.put_json(layout, "LAYOUT_RAW", **scope)
        calls = []
        for image, call in crops:
            inp = store.put_bytes(image, "OCR_INPUT", "image/png", **scope)
            all_ids.add(inp.artifact_id)
            calls.append(call | {"input_artifact_id": inp.artifact_id})
        raw = store.put_json({"schema": SCHEMA, "backend": packet["model"]["actual_backend"],
                             "classification": "AUTO_EXTRACTED_UNREVIEWED", "source_sha256": source.sha256,
                             "source_id": source.source_id, "page_id": pid, "page_size_pt": [width, height],
                             "parent_commit_id": packet["base_staging_heads"][source.source_id],
                             "packet_artifact_id": provenance.artifact_id, "run_config_artifact_id": report.artifact_id,
                             "layout_artifact_id": detections.artifact_id, "visual_reading_artifact_id": readback.artifact_id,
                             "visual_reading_status": "AGENT_VISUAL_ONLY", "calls": calls}, "OCR_RAW", **scope)
        outputs[str(index)] = raw.artifact_id
        all_ids.update({raw.artifact_id, readback.artifact_id, detections.artifact_id})
    values = {"packet_sha256": receipt_sha, "source_sha256": source.sha256,
              "parent_commit_id": packet["base_staging_heads"][source.source_id],
              "pages": outputs, "artifacts": sorted(all_ids)}
    return cache.add_stage(stage_signature=_stage_signature(values), stage=STAGE, source_id=source.source_id,
                           page_index=None, outputs=values)


def assemble_page(assembler, page, entry):
    """Use the ordinary object models and accounting; local proof stays independent of archived CHOICE_V1."""
    aid = entry["outputs"]["pages"].get(str(page.page_index))
    if aid is None:
        return
    raw = assembler.store.read_json(aid)
    uid = assembler.accounting.page(page.page_index)
    if (page.page_status != "OCR_REQUIRED" or raw["schema"] != SCHEMA or raw["page_id"] != uid
            or raw["source_sha256"] != assembler.src.sha256
            or raw["page_size_pt"] != [page.width_pt, page.height_pt]):
        raise ValueError("local OCR target identity or prior layer changed")
    page.extra["primary_ocr_raw_artifact_id"] = aid
    page.ocr_raw_artifact_ids.append(aid)
    page.layout_raw_artifact_id = raw["layout_artifact_id"]
    page.primary_text_origin = "OCR"
    page.primary_text_layer = "GLM_OCR"
    page.text_layer_producer = EXTRACTOR
    page.page_status = "OCR_OK"
    page.ocr_status = "DONE"
    page.models = [RECOGNITION, LAYOUT]
    producer_hash = config_hash({"extractor": EXTRACTOR, "version": VERSION, "packet": entry["outputs"]["packet_sha256"]})
    inputs = [raw["packet_artifact_id"], raw["run_config_artifact_id"], raw["visual_reading_artifact_id"],
              raw["layout_artifact_id"]]
    layout = assembler.store.read_json(raw["layout_artifact_id"])
    layout_trace = []
    regions_from_raw(layout, page.width_pt, page.height_pt, trace=layout_trace)
    formulas = {c["layout_det_index"] for c in raw["calls"] if c["result"]["task"] == "formula"}
    tables = {c["layout_det_index"] for c in raw["calls"] if c["result"]["task"] == "table"}
    for decision in layout_trace:
        index = decision["det_index"]
        if decision["state"] == "SUPPRESSED" or index in formulas:
            continue
        decision["state"] = "SUPPRESSED"
        decision["reason"] = ("LOCAL_TABLE_PROPOSAL_NOT_ADMITTED" if index in tables
                              else "LOCAL_PACKET_HAS_NO_SEPARATE_OBJECT_FOR_REGION")
    assembler.accounting.layout(layout, raw["layout_artifact_id"], page.page_index, layout_trace)
    mapped = 0
    for i, call in enumerate(raw["calls"]):
        result = call["result"]
        task = result["task"]
        locator = f"/calls/{i}/content"
        inputs.append(call["input_artifact_id"])
        assembler.accounting.candidate(uid, task.upper(), locator, aid, input_hash=result["input_sha256"],
                                       state="SUPPRESSED" if task == "table" else None,
                                       reason="LOCAL_TABLE_PROPOSAL_NOT_ADMITTED" if task == "table" else None)
        if task == "table":  # raw proposal retained; the direct path lacks a qualified table extraction assertion
            continue
        common = dict(page_index=page.page_index, bbox=tuple(call["bbox_pt"]), origin="OCR",
                      region_origin="OCR_MODEL" if task == "text" else "LAYOUT_MODEL",
                      extractor_id=EXTRACTOR, extractor_version=VERSION, generation="1",
                      raw_config_hash=producer_hash, models=[RECOGNITION] if task == "text" else [LAYOUT, RECOGNITION],
                      raw_artifact_id=aid, raw_locator=locator, raw_artifacts=[("OCR_RESPONSE", aid)])
        if task == "text":
            content = call["content"]
            page.recognized_char_count = len(content)
            assembler.result.blocks.append(BlockX(**common, text=content, block_type="TEXT", reading_order=1,
                                                   text_layer="GLM_OCR", is_primary_layer=True))
        else:
            norm = normalize_formula(call["content"])
            common["raw_artifacts"].append(("LAYOUT_DETECTIONS", raw["layout_artifact_id"]))
            formula = FormulaX(**common, raw_output=call["content"], **norm, text_layer="GLM_OCR",
                               is_primary_layer=True,
                               formula_kind="INLINE" if call["layout_label"] == "inline_formula" else "DISPLAY",
                               image_artifact_id=call["input_artifact_id"],
                               quality_flags=[] if norm["latex_parse_ok"] else ["FORMULA_LATEX_UNPARSEABLE"])
            formula.extra["layout_det_index"] = call["layout_det_index"]
            assembler.result.formulas.append(formula)
        mapped += 1
    assembler.step("OCR", page.page_index, "REUSED_CACHED", "OCR_OK", entry["stage_signature"], EXTRACTOR, producer_hash,
                   model_id=RECOGNITION.model_id, model_revision=RECOGNITION.model_revision,
                   input_artifact_ids=inputs, output_artifact_ids=[aid], n_objects_out=mapped)
