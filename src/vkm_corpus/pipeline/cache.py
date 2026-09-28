"""Stage and model-call caches of the producer (staging) root.

Append-only JSON-lines index files under ``<data_root>/cache/`` point at content-addressed artifacts:

* ``calls/`` – one line per model-call attempt: ``call_signature``, ``attempt``, ``status``, the raw artifact id
  (``OCR_RAW`` / ``LAYOUT_RAW``) and the input artifact; the newest successful attempt of a signature is the cached
  result (H-05: the raw blob is content-addressed, the index row carries the signature and the attempt number);
* ``stages/`` – one line per stage output keyed by ``stage_signature`` (native extraction, DOCX render, commits).

Each process appends to its own file (``<kind>/run=<RUN>/<pid>.jsonl``), so there is no cross-process locking; a line
is flushed and fsynced before the call returns. The index is an accelerator of the staging host: the canonical
``artifacts`` rows (``producer_signature``) and ``processing_steps`` can rebuild it (``pull-index``).
"""
from __future__ import annotations

import json
import os
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


@dataclass
class CallEntry:
    call_signature: str
    attempt: int
    status: str                 # OK | ERROR
    raw_artifact_id: str | None
    input_artifact_id: str | None
    record: dict[str, Any]


class JsonlAppender:
    def __init__(self, path: Path):
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        self._fh = open(path, "a", encoding="utf-8", newline="\n")
        self._lock = threading.Lock()

    def append(self, obj: dict[str, Any]) -> None:
        line = json.dumps(obj, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"
        with self._lock:
            self._fh.write(line)
            self._fh.flush()
            os.fsync(self._fh.fileno())

    def close(self) -> None:
        self._fh.close()


def _iter_lines(root: Path) -> Iterator[dict[str, Any]]:
    if not root.exists():
        return
    for f in sorted(root.rglob("*.jsonl")):
        with open(f, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    yield json.loads(line)
                except json.JSONDecodeError:
                    continue  # a torn last line of a crashed writer


class StageCache:
    """Index of cached stage outputs and model calls of one data root."""

    def __init__(self, data_root: Path, run_id: str, *, writer_name: str | None = None):
        self.root = Path(data_root) / "cache"
        self.run_id = run_id
        name = writer_name or f"{os.getpid()}"
        self._calls_w = None
        self._stages_w = None
        self._name = name
        self.calls: dict[str, list[dict[str, Any]]] = {}
        self.stages: dict[str, dict[str, Any]] = {}
        self.reload()

    # ------------------------------------------------------------------ loading
    def reload(self) -> None:
        self.calls = {}
        for rec in _iter_lines(self.root / "calls"):
            self.calls.setdefault(rec["call_signature"], []).append(rec)
        self.stages = {}
        for rec in _iter_lines(self.root / "stages"):
            self.stages[rec["stage_signature"]] = rec

    def _writer(self, kind: str) -> JsonlAppender:
        attr = "_calls_w" if kind == "calls" else "_stages_w"
        w = getattr(self, attr)
        if w is None:
            w = JsonlAppender(self.root / kind / f"run={self.run_id}" / f"{self._name}.jsonl")
            setattr(self, attr, w)
        return w

    def close(self) -> None:
        for w in (self._calls_w, self._stages_w):
            if w is not None:
                w.close()

    # ------------------------------------------------------------------ model calls
    def best_call(self, call_signature: str) -> dict[str, Any] | None:
        """Newest successful attempt of a call signature, or None."""
        ok = [r for r in self.calls.get(call_signature, []) if r.get("status") == "OK"]
        return max(ok, key=lambda r: (int(r.get("attempt", 0)), r.get("created_at", ""))) if ok else None

    def next_attempt(self, call_signature: str) -> int:
        return max((int(r.get("attempt", 0)) for r in self.calls.get(call_signature, [])), default=0) + 1

    def add_call(self, *, call_signature: str, attempt: int, status: str, raw_artifact_id: str | None,
                 input_artifact_id: str | None, kind: str, source_id: str | None, page_id: str | None,
                 extra: dict[str, Any] | None = None) -> dict[str, Any]:
        rec = {"call_signature": call_signature, "attempt": attempt, "status": status,
               "raw_artifact_id": raw_artifact_id, "input_artifact_id": input_artifact_id, "kind": kind,
               "source_id": source_id, "page_id": page_id, "run_id": self.run_id, "created_at": _now(),
               **(extra or {})}
        self._writer("calls").append(rec)
        self.calls.setdefault(call_signature, []).append(rec)
        return rec

    # ------------------------------------------------------------------ stages
    def get_stage(self, stage_signature: str) -> dict[str, Any] | None:
        return self.stages.get(stage_signature)

    def add_stage(self, *, stage_signature: str, stage: str, source_id: str | None, page_index: int | None,
                  outputs: dict[str, Any], status: str = "OK") -> dict[str, Any]:
        rec = {"stage_signature": stage_signature, "stage": stage, "source_id": source_id, "page_index": page_index,
               "outputs": outputs, "status": status, "run_id": self.run_id, "created_at": _now()}
        self._writer("stages").append(rec)
        self.stages[stage_signature] = rec
        return rec

    def latest_stage(self, stage: str, source_id: str) -> dict[str, Any] | None:
        cands = [r for r in self.stages.values() if r.get("stage") == stage and r.get("source_id") == source_id
                 and r.get("status") == "OK"]
        return max(cands, key=lambda r: r.get("created_at", "")) if cands else None
