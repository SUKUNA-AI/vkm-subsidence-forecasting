"""OCR_RAW records: one immutable, content-addressed JSON per model call attempt (design §7.1, H-05).

The record keeps the request without pixels (image replaced by its hashes), the complete HTTP response body as
received, the parsed content and finish reason, timings, the model identity (id, revision, weights sha256) and the
serving engine (``/version``, image digest). Normalisation never edits a record; it reads it.

Field names avoid the leakage-guard list (no ``quote``/``ocr_text``/``page_text``/``full_text``): the recognised text is
``response.content`` and the raw HTTP body is ``response.body``.
"""
from __future__ import annotations

from typing import Any

from vkm_corpus.ocr.client import CLIENT_ID, CLIENT_VERSION, OcrResponse, request_body
from vkm_corpus.ocr.prompts import ModelIdentity

RAW_SCHEMA = "vkm.ocr_raw/1"


def build_record(resp: OcrResponse, *, call_signature: str, attempt: int, model: ModelIdentity,
                 backend: dict[str, Any], run_id: str | None, source_id: str | None, page_id: str | None,
                 input_ref: dict[str, Any], region_ref: dict[str, Any] | None) -> dict[str, Any]:
    req = resp.request
    return {
        "schema": RAW_SCHEMA,
        "call_signature": call_signature,
        "attempt": attempt,
        "processing_run_id": run_id,
        "source_id": source_id,
        "page_id": page_id,
        "task": req.task,
        "prompt": req.prompt,
        "input": {"png_sha256": req.png_sha256, "pixel_sha256": req.pixel_sha256, "png_bytes": len(req.png),
                  "width": req.width, "height": req.height, "mode": req.mode, **input_ref},
        "region": region_ref,
        "model": model.as_dict(),
        "backend": backend,
        "client": {"id": CLIENT_ID, "version": CLIENT_VERSION},
        "sampling": req.sampling.as_dict(),
        "request": request_body(req, model.served_model_name, redact_image=True),
        "http": {"endpoint": "/v1/chat/completions", "status": resp.http_status},
        "status": resp.status,
        "response": {"body": resp.body_text, "content": resp.content, "finish_reason": resp.finish_reason,
                     "usage": resp.usage},
        "error": resp.error,
        "tries": resp.tries,
        "timing": {"queued_at": resp.queued_at, "started_at": resp.started_at, "finished_at": resp.finished_at,
                   "latency_ms": resp.latency_ms},
    }


def content_of(record: dict[str, Any]) -> str | None:
    return (record.get("response") or {}).get("content")


def finish_reason_of(record: dict[str, Any]) -> str | None:
    return (record.get("response") or {}).get("finish_reason")
