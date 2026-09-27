"""Structured JSON-lines logging with rotation (постановка §43; no Grafana stack).

Every record carries ``ts, level, service, msg`` and, when known, the context fields ``run_id, job_id, source_id,
page_id, stage, duration_ms, status, error_code``. Context is passed with ``extra={"vkm": {...}}`` or bound once with
``bind(logger, run_id=...)``. Files rotate by size under ``$VKM_DATA_ROOT/logs/<service>.jsonl``.
"""
from __future__ import annotations

import json
import logging
import logging.handlers
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

CONTEXT_FIELDS = ("run_id", "job_id", "source_id", "page_id", "stage", "duration_ms", "status", "error_code")
_MAX_BYTES = 20 * 1024 * 1024
_BACKUPS = 10


class JsonFormatter(logging.Formatter):
    def __init__(self, service: str) -> None:
        super().__init__()
        self.service = service

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": datetime.fromtimestamp(record.created, tz=timezone.utc).isoformat(timespec="milliseconds"),
            "level": record.levelname,
            "service": self.service,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        ctx = getattr(record, "vkm", None) or {}
        for key in CONTEXT_FIELDS:
            if ctx.get(key) is not None:
                payload[key] = ctx[key]
        extra = {k: v for k, v in ctx.items() if k not in CONTEXT_FIELDS and v is not None}
        if extra:
            payload["data"] = extra
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False, default=str)


class _Bound(logging.LoggerAdapter):
    def process(self, msg, kwargs):
        ctx = dict(self.extra)
        ctx.update((kwargs.pop("extra", None) or {}).get("vkm", {}))
        kwargs["extra"] = {"vkm": ctx}
        return msg, kwargs


def bind(logger: logging.Logger | logging.LoggerAdapter, **context: Any) -> logging.LoggerAdapter:
    """Logger adapter that adds ``context`` to every record (merged with per-call ``extra={"vkm": ...}``)."""
    if isinstance(logger, _Bound):
        merged = {**logger.extra, **context}
        return _Bound(logger.logger, merged)
    return _Bound(logger, context)


def configure(service: str, log_dir: Path | None = None, level: str = "INFO") -> logging.Logger:
    """Root logger for a service: JSON to stderr and, if ``log_dir`` is given, to a rotating ``<service>.jsonl``."""
    root = logging.getLogger("vkm")
    root.setLevel(level)
    root.propagate = False
    for handler in list(root.handlers):
        root.removeHandler(handler)
    formatter = JsonFormatter(service)
    stream = logging.StreamHandler(sys.stderr)
    stream.setFormatter(formatter)
    root.addHandler(stream)
    if log_dir is not None:
        log_dir.mkdir(parents=True, exist_ok=True)
        file_handler = logging.handlers.RotatingFileHandler(
            log_dir / f"{service}.jsonl", maxBytes=_MAX_BYTES, backupCount=_BACKUPS, encoding="utf-8")
        file_handler.setFormatter(formatter)
        root.addHandler(file_handler)
    return root


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(f"vkm.{name}")
