"""Actual producer-path accounting, separate from scientific admission.

Expected units are frozen from pagination/native metadata, never from the number
of outputs. Candidates precede filtering. Reports and OCR events are private,
content-addressed and source/version scoped; only their identities enter receipts.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
from collections import Counter
from typing import Any

from vkm_corpus.parquet.atomic import write_bytes, sha256_of
from vkm_evidence.contracts import canonical_bytes, record_hash
from vkm_evidence.coverage import CoverageLedger, ExpectedUnit, CandidateDisposition, ExtractionAttempt

RULE = "pipeline-object-accounting/1"
LIMIT = 500_000
MAX_REPORT_BYTES = 128 * 1024 * 1024
SHA = re.compile(r"[a-f0-9]{64}\Z")


def _path(root, name):
    root = Path(root).absolute()
    path = root / name
    if not path.resolve().is_relative_to(root.resolve()):
        raise ValueError("ACCOUNTING_PATH_ESCAPE")
    for p in (path, *path.parents):
        if p.is_symlink() or getattr(p, "is_junction", lambda: False)():
            raise ValueError("ACCOUNTING_PATH_LINK")
    return path


def _publish(root, category, value):
    root = Path(root).absolute()
    data = canonical_bytes(value)
    if len(data) > MAX_REPORT_BYTES:
        raise ValueError("ACCOUNTING_REPORT_LIMIT")
    digest = hashlib.sha256(data).hexdigest()
    path = _path(root, f"accounting/{category}/{digest}.json")
    write_bytes(_path(root, "accounting/tmp"), path, data)
    if sha256_of(path) != digest:
        raise ValueError("ACCOUNTING_PUBLICATION_HASH_MISMATCH")
    return {"path": path.relative_to(root).as_posix(), "sha256": digest}


def _read(root, reference):
    path = _path(root, reference["path"])
    if not SHA.fullmatch(reference["sha256"]) or path.stat().st_size > MAX_REPORT_BYTES:
        raise ValueError("ACCOUNTING_REFERENCE_INVALID")
    raw = path.read_bytes()
    if hashlib.sha256(raw).hexdigest() != reference["sha256"]:
        raise ValueError("ACCOUNTING_REFERENCE_CHANGED")
    return json.loads(raw)


def expected_summary(prep):
    """Read-only metadata denominator: missing/extra/duplicate indices stay visible."""
    pagination = prep.get("pagination") or {}
    count = pagination.get("count")
    known = type(count) is int and 0 <= count <= LIMIT
    unit = pagination.get("unit")
    known = known and unit in {"p", "r", "s"}
    indices = [r.get("page_index") for r in prep.get("pages", [])]
    valid = [i for i in indices if type(i) is int]
    expected = set(range(1, count + 1)) if known else set()
    return {"rule": RULE, "source_id": prep["source_id"], "source_sha256": prep["source_sha256"],
        "denominator_state": "KNOWN" if known else "UNKNOWN", "expected_count": count if known else None,
        "unit": unit, "pagination_basis": pagination.get("basis"),
        "pagination_agrees": pagination.get("check_count") in {None, count},
        "missing_indices": sorted(expected - set(valid)), "extra_indices": sorted(set(valid) - expected),
        "duplicate_indices": sorted(i for i, n in Counter(valid).items() if n > 1),
        "invalid_index_count": len(indices) - len(valid), "detector_recall": "NOT_ESTABLISHED"}


def record_ocr_event(cfg, cache, spec, source_sha256, *, phase, reason=None, crop=None,
                     outputs=(), attempt=None):
    """Durable event before/after real branches. No response/source text in events."""
    from vkm_corpus.pipeline.ocr_stage import task_key
    if not source_sha256 or not SHA.fullmatch(source_sha256):
        raise ValueError("ACCOUNTING_OCR_SOURCE_VERSION_REQUIRED")
    value = {"schema": "vkm-ocr-accounting-event/1", "rule": RULE, "run_id": cache.run_id,
        "source_id": spec.source_id, "source_sha256": source_sha256, "page_id": spec.page_id,
        "task_key": task_key(cfg, spec), "task": spec.task, "role": spec.role, "det_index": spec.det_index,
        "epub_image_order": spec.epub_image_order, "phase": phase, "reason": reason,
        "config_sha256": record_hash(cfg.stage_config("OCR")), "attempt": attempt,
        "band_index": crop.band_index if crop else None, "band_count": crop.band_count if crop else None,
        "call_signature": crop.call_signature if crop else None,
        "input_sha256": crop.png_sha256 if crop and crop.png_sha256 else source_sha256,
        "error_sha256": hashlib.sha256(crop.error.encode()).hexdigest() if crop and crop.error else None,
        "outputs": sorted(set(outputs)), "code_revision": cfg.expected_commit or "UNPINNED"}
    return _publish(Path(cfg.data_root), f"events/{spec.source_id}/{source_sha256}", value)


def ocr_events(root, source_id, source_sha256, config_sha256=None):
    root = Path(root).absolute()
    if not re.fullmatch(r"VKM-SRC-\d{3}", source_id) or not SHA.fullmatch(source_sha256):
        raise ValueError("ACCOUNTING_SOURCE_IDENTITY_INVALID")
    directory = _path(root, f"accounting/events/{source_id}/{source_sha256}")
    result = []
    if directory.exists():
        for path in sorted(directory.glob("*.json")):
            if len(result) >= LIMIT:
                raise ValueError("ACCOUNTING_EVENT_LIMIT")
            ref = {"path": path.relative_to(root).as_posix(), "sha256": path.stem}
            event = _read(root, ref)
            if event.get("source_id") != source_id or event.get("source_sha256") != source_sha256:
                raise ValueError("ACCOUNTING_EVENT_SOURCE_MISMATCH")
            if config_sha256 is None or event.get("config_sha256") == config_sha256:
                result.append((ref, event))
    return result


class SourceAccounting:
    def __init__(self, cfg, prep, store):
        self.cfg, self.prep, self.store = cfg, prep, store
        self.sid, self.source_sha = prep["source_id"], prep["source_sha256"]
        if not SHA.fullmatch(self.source_sha):
            raise ValueError("ACCOUNTING_SOURCE_HASH_REQUIRED")
        self.expected = expected_summary(prep)
        self.units, self.inspected, self.candidates = {}, set(), {}
        self.artifacts, self.gaps, self.channels = {}, [], []
        self.unit = self.expected["unit"] or "p"
        self.config_sha = record_hash({k: cfg.stage_config(k) for k in ("NATIVE_TEXT", "REGIONS", "OCR", "NORMALIZE")})
        if self.expected["denominator_state"] == "KNOWN":
            for i in range(1, self.expected["expected_count"] + 1):
                self.add_unit(f"{self.sid}:{self.unit}{i:04d}", "PAGE", f"{self.unit}:{i}")
        else:
            self.gaps.append("EXPECTED_PAGE_DENOMINATOR_UNKNOWN")

    def add_unit(self, uid, kind, locator):
        if uid not in self.units and len(self.units) >= LIMIT:
            raise ValueError("ACCOUNTING_UNIT_LIMIT")
        value = ExpectedUnit(unit_id=uid, source_id=self.sid, source_sha256=self.source_sha,
                             unit_kind=kind, locator=locator)
        if uid in self.units and self.units[uid] != value:
            raise ValueError("ACCOUNTING_UNIT_ID_COLLISION")
        self.units[uid] = value
        return uid

    def page(self, index):
        uid = f"{self.sid}:{self.unit}{index:04d}"
        if uid not in self.units:
            self.gaps.append("UNEXPECTED_PAGE_UNIT")
            self.add_unit(uid, "PAGE", f"{self.unit}:{index}")
        self.inspected.add(uid)
        return uid

    def artifact(self, aid):
        if not aid or not aid.startswith("sha256:") or not SHA.fullmatch(aid[7:]):
            raise ValueError("ACCOUNTING_RAW_ARTIFACT_ID_INVALID")
        if aid not in self.artifacts:
            path = self.store.find(aid)
            if path is None or sha256_of(path) != aid[7:]:
                raise ValueError("ACCOUNTING_RAW_ARTIFACT_MISSING_OR_CHANGED")
            self.artifacts[aid] = aid[7:]
        return aid[7:]

    def candidate(self, uid, kind, locator, aid, *, details=None, state=None, reason=None, input_hash=None):
        self.artifact(aid)
        cid = "candidate:" + record_hash([self.sid, self.source_sha, uid, aid, locator, kind])
        value = {"candidate_id": cid, "unit_id": uid, "kind": kind, "locator": locator,
            "raw_artifact_id": aid, "input_sha256": input_hash or aid[7:], "state": state,
            "reason": reason, "details": details or {}}
        if cid in self.candidates and self.candidates[cid] != value:
            raise ValueError("ACCOUNTING_CANDIDATE_ID_COLLISION")
        self.candidates[cid] = value
        if len(self.candidates) > LIMIT:
            raise ValueError("ACCOUNTING_CANDIDATE_LIMIT")
        return cid

    def native(self, raw, aid, index=None, *, docx=False):
        """Candidate inventory from already-read native structure, before assembly filters."""
        if not aid:
            return
        self.artifact(aid)
        page_uid = self.page(index) if index is not None else None
        specs = []
        if docx:
            specs += [("TEXT", p["path"], p, "EMPTY_NATIVE_PARAGRAPH" if not p.get("text", "").strip() else None)
                      for p in raw.get("paragraphs", [])]
            specs += [("TABLE", t["path"], t, None) for t in raw.get("tables", [])]
            specs += [("FORMULA", f"{m['paragraph_path']}#math{m['order']}", m, None) for m in raw.get("maths", [])]
            specs += [("FIGURE", f"{i['paragraph_path']}#img{i['order']}", i, None) for i in raw.get("images", [])]
        elif "epub" in raw.get("schema", ""):
            for key, kind in (("blocks", "TEXT"), ("tables", "TABLE"), ("maths", "FORMULA"), ("images", "FIGURE")):
                specs += [("FORMULA" if key == "images" and obj.get("is_formula_candidate") else kind,
                           obj["xpath"], obj, None) for obj in raw.get(key, [])]
        elif "djvu" in raw.get("schema", ""):
            specs += [("TEXT", f"/lines/{i}", line, None) for i, line in enumerate(raw.get("lines", []))]
        else:
            specs += [("TEXT", f"/blocks/n={b['n']}", b, None) for b in raw.get("blocks", [])]
            specs += [("FIGURE", f"/images/{i}", im, "NATIVE_IMAGE_MAPPING_UNSUPPORTED")
                      for i, im in enumerate(raw.get("images", []))]
            if raw.get("features", {}).get("n_paths", 0) or raw.get("path_rects"):
                self.channels.append({"unit_id": page_uid, "channel": "PDF_VECTOR_PRIMITIVES",
                    "raw_artifact_id": aid, "status": "PRESERVED_NOT_SEMANTIC_OBJECTS",
                    "count": raw.get("features", {}).get("n_paths"), "enumerated": raw.get("path_rects") is not None})
        for kind, locator, obj, reason in specs:
            uid = page_uid
            if docx:
                uid = self.add_unit(self.sid + ":native:" + record_hash([aid, kind, locator]), "NATIVE_OBJECT", locator)
                self.inspected.add(uid)
            details = {}
            if kind == "TABLE" and "cells" in obj:
                details = {"native_rows": obj.get("n_rows"), "native_cols": obj.get("n_cols"),
                    "native_cells": len(obj["cells"]), "native_cell_projection_sha256": record_hash([
                        {key: cell[key] for key in ("row", "col", "row_span", "col_span", "is_header", "text")}
                        for cell in obj["cells"]])}
            self.candidate(uid, kind, locator, aid, input_hash=record_hash(obj), details=details,
                state="SUPPRESSED" if reason == "EMPTY_NATIVE_PARAGRAPH" else "UNSUPPORTED" if reason else None,
                reason=reason)

    def layout(self, raw, aid, index, trace):
        uid = self.page(index)
        refs = {}
        for decision in trace:
            kind = {"table": "TABLE", "display_formula": "FORMULA", "inline_formula": "FORMULA",
                    "image": "FIGURE", "chart": "FIGURE", "seal": "FIGURE",
                    "header_image": "FIGURE", "footer_image": "FIGURE"}.get(decision["label"], "TEXT")
            refs[decision["det_index"]] = self.candidate(uid, kind, decision["raw_locator"], aid,
                details=decision, state="SUPPRESSED" if decision["state"] == "SUPPRESSED" else None,
                reason=decision.get("reason"), input_hash=record_hash(raw["detections"][decision["det_index"]]))
        for decision in trace:
            if "duplicate_of" in decision:
                self.candidates[refs[decision["det_index"]]]["duplicate_of"] = refs[decision["duplicate_of"]]
        self.channels.append({"unit_id": uid, "channel": "LAYOUT", "raw_artifact_id": aid,
            "candidate_scope": "STORED_RAW_DETECTIONS_ONLY", "n_raw_detections": len(trace),
            "n_queries": raw.get("n_queries"), "raw_floor": raw.get("config", {}).get("raw_floor"),
            "top_k": raw.get("config", {}).get("top_k"),
            "pre_raw_suppression": "NOT_ENUMERATED", "detector_recall": "NOT_ESTABLISHED"})

    def tasks(self, index, specs, aid, regions, route):
        from vkm_corpus.pipeline.ocr_stage import task_key
        uid = self.page(index)
        by_detection = {s.det_index: s for s in specs if s.det_index is not None}
        for candidate in self.candidates.values():
            if candidate["unit_id"] != uid or "det_index" not in candidate["details"]:
                continue
            det = candidate["details"]["det_index"]
            if det in by_detection:
                candidate["details"]["task_key"] = task_key(self.cfg, by_detection[det])
            elif candidate["state"] is None:
                label = candidate["details"].get("label")
                if label == "inline_formula" and not self.cfg.ocr_inline_formulas:
                    candidate["reason_if_missing"] = "INLINE_FORMULA_DISABLED"
                elif label in {"seal", "header_image", "footer_image"}:
                    candidate["reason_if_missing"] = "LAYOUT_LABEL_ROUTE_UNSUPPORTED"
                elif route == "OCR_REQUIRED" and candidate["kind"] == "TEXT":
                    candidate["reason_if_missing"] = "NESTED_TEXT_OCR_SUPPRESSED"
        for spec in specs:
            if spec.det_index is None and aid:
                key = task_key(self.cfg, spec)
                self.candidate(uid, spec.task.upper(), "planned-task:" + key, aid,
                               details={"task_key": key, "role": spec.role})

    def finish(self, result, mapped, cache, *, code_revision):
        """Cross-check real Mapper IDs/hashes, then link candidates by exact raw lineage."""
        outputs = []
        aliases = {}
        page_by_index = {p.page_index: p for p in result.pages}
        candidates_by_task = {}
        for c in self.candidates.values():
            if c["details"].get("task_key"):
                candidates_by_task.setdefault(c["details"]["task_key"], []).append(c)
        for dataset, internal in (("blocks", result.blocks), ("tables", result.tables),
                                  ("formulas", result.formulas), ("figures", result.figures)):
            rows = [r.model_dump(mode="json") if hasattr(r, "model_dump") else r for r in mapped.tables.get(dataset, [])]
            if len(rows) != len(internal):
                raise ValueError("ACCOUNTING_ASSEMBLY_MAPPER_COUNT_MISMATCH")
            for obj, row in zip(internal, rows):
                if (row["source_id"] != self.sid or row["source_sha256"] != self.source_sha
                        or not SHA.fullmatch(row["content_sha256"]) or row.get("raw_locator") != obj.raw_locator):
                    raise ValueError("ACCOUNTING_OUTPUT_SOURCE_OR_LOCATOR_MISMATCH")
                aids = {row.get("raw_artifact_id"), *(x["artifact_id"] for x in row.get("raw_artifacts", []))}
                for aid in aids - {None}:
                    self.artifact(aid)
                    for locator in (row.get("raw_locator"), *obj.extra.get("accounting_native_locators", [])):
                        aliases.setdefault((aid, locator, row["object_kind"]), []).append(row)
                det_index = obj.extra.get("layout_det_index")
                page = page_by_index.get(obj.page_index)
                if det_index is not None and page and page.layout_raw_artifact_id:
                    aliases.setdefault((page.layout_raw_artifact_id, f"/detections/{det_index}", row["object_kind"]), []).append(row)
                if obj.extra.get("accounting_task_key"):
                    for candidate in candidates_by_task.get(obj.extra["accounting_task_key"], []):
                        aliases.setdefault((candidate["raw_artifact_id"], candidate["locator"], row["object_kind"]), []).append(row)
                outputs.append(row)
        if len({r["object_id"] for r in outputs}) != len(outputs):
            raise ValueError("ACCOUNTING_DUPLICATE_OUTPUT_ID")
        attempts, dispositions = [], []
        accounted_outputs = set()
        for cid, candidate in self.candidates.items():
            mapped_kind = {"TEXT": "BLOCK", "FIGURE": "FIGURE", "TABLE": "TABLE", "FORMULA": "FORMULA"}.get(candidate["kind"])
            matches = {r["object_id"]: r for r in aliases.get((candidate["raw_artifact_id"], candidate["locator"], mapped_kind), [])}
            state, reason = candidate["state"], candidate["reason"]
            if state is None:
                state, reason = ("EXTRACTED", None) if matches else ("NEEDS_REVIEW", candidate.get(
                    "reason_if_missing", "CANDIDATE_HAS_NO_CANONICAL_OUTPUT"))
            if state != "EXTRACTED" and matches:
                raise ValueError("ACCOUNTING_SUPPRESSED_CANDIDATE_HAS_OUTPUT")
            hashes = tuple(sorted({r["content_sha256"] for r in matches.values()}))
            attempt_id = "assemble:" + record_hash([cid, self.config_sha, hashes])
            attempts.append(ExtractionAttempt(attempt_id=attempt_id, unit_id=candidate["unit_id"], candidate_id=cid,
                stage="ASSEMBLE", input_sha256=candidate["input_sha256"], config_sha256=self.config_sha,
                code_revision=code_revision, state="SUCCEEDED" if matches else "NOT_RUN", outputs=hashes,
                object_outputs={rid: r["content_sha256"] for rid, r in matches.items()}, reason=reason))
            flags = {f for r in matches.values() for f in r.get("quality_flags", [])}
            if matches and flags & {"TRUNCATED", "REPETITION", "EMPTY_ON_INK"}:
                state, reason = "NEEDS_REVIEW", "OCR_OUTPUT_QUALITY_FAILURE"
            if matches and candidate["kind"] == "FORMULA" and any(not r.get("raw_output") for r in matches.values()):
                state, reason = "NEEDS_REVIEW", "FORMULA_IMAGE_ONLY_OR_EMPTY"
            native_grid = candidate["details"].get("native_cell_projection_sha256")
            if native_grid and matches and any(record_hash(r.get("cells", [])) != native_grid or
                    r.get("n_rows") != candidate["details"]["native_rows"] or
                    r.get("n_cols") != candidate["details"]["native_cols"] for r in matches.values()):
                state, reason = "NEEDS_REVIEW", "NATIVE_TABLE_CELL_PROJECTION_CHANGED"
            dispositions.append(CandidateDisposition(candidate_id=cid, unit_id=candidate["unit_id"], kind=candidate["kind"],
                locator=candidate["locator"], state=state, reason=reason, attempts=(attempt_id,),
                output_objects=tuple(sorted(matches)), duplicate_of=candidate.get("duplicate_of")))
            accounted_outputs.update(matches)
        # Derived bibliography is not a detected original object. Unmatched main
        # objects are a mapping gap, never invented candidates made from outputs.
        unmatched = sorted({r["object_id"] for r in outputs} - accounted_outputs)
        if unmatched:
            self.gaps.append("CANONICAL_OUTPUT_WITHOUT_DETECTED_CANDIDATE")
        events = ocr_events(Path(self.cfg.data_root), self.sid, self.source_sha, record_hash(self.cfg.stage_config("OCR")))
        grouped = {}
        advanced = {(e["run_id"], e["task_key"]) for _, e in events if e["band_index"] is not None}
        order = {"PLANNED": 0, "RUNNING": 1, "RUN_STOPPED": 2, "NOT_RUN": 3, "FAILED": 4, "SUCCEEDED": 5}
        for ref, event in events:
            if event["band_index"] is None and (event["run_id"], event["task_key"]) in advanced:
                continue
            key = (event["run_id"], event["task_key"], event["band_index"], event.get("attempt"))
            previous = grouped.get(key)
            if previous is None or order[event["phase"]] > order[previous[1]["phase"]]:
                grouped[key] = ref, event
        candidate_attempts = {}
        for ref, event in grouped.values():
            uid = event["page_id"]
            if uid not in self.units:
                self.gaps.append("OCR_EVENT_UNIT_OUTSIDE_DENOMINATOR")
                continue
            phase = event["phase"]
            state = {"SUCCEEDED": "SUCCEEDED", "FAILED": "FAILED", "RUNNING": "INTERRUPTED"}.get(phase, "NOT_RUN")
            for digest in event["outputs"]:
                self.artifact("sha256:" + digest)
            matching = candidates_by_task.get(event["task_key"], [])
            cid = matching[0]["candidate_id"] if len(matching) == 1 and matching[0]["unit_id"] == uid else None
            attempt_id = "ocr:" + ref["sha256"]
            if cid:
                candidate_attempts.setdefault(cid, []).append(attempt_id)
            attempts.append(ExtractionAttempt(attempt_id=attempt_id, unit_id=uid, candidate_id=cid, stage="OCR_" + phase,
                input_sha256=event["input_sha256"], config_sha256=event["config_sha256"],
                code_revision=event["code_revision"], state=state, outputs=tuple(event["outputs"]),
                reason=event.get("reason") or ("HISTORICAL_" + phase if state != "SUCCEEDED" else None)))
        dispositions = [c.model_copy(update={"attempts": (*c.attempts, *candidate_attempts.get(c.candidate_id, []))})
                        for c in dispositions]
        for page in result.pages:
            uid = f"{self.sid}:{self.unit}{page.page_index:04d}"
            if uid not in self.units:
                continue
            page_errors = [e.code for e in result.errors if e.page_index == page.page_index]
            attempts.append(ExtractionAttempt(attempt_id="page:" + record_hash([uid, self.config_sha, page.page_status, page_errors]),
                unit_id=uid, stage="PAGE_ASSEMBLY", input_sha256=self.source_sha, config_sha256=self.config_sha,
                code_revision=code_revision, state="FAILED" if page.page_status == "FAILED" else "SUCCEEDED",
                reason=",".join(sorted(set(page_errors))) or page.page_status))
        for number, step in enumerate(result.steps):
            uid = f"{self.sid}:{self.unit}{step.page_index:04d}" if step.page_index is not None else None
            if uid not in self.units:
                continue
            state = "NOT_RUN" if step.outcome in {"NOT_ATTEMPTED", "SKIPPED_BY_POLICY"} else \
                "FAILED" if step.status == "FAILED" else "SUCCEEDED"
            output_hashes = tuple(sorted({self.artifact(aid) for aid in step.output_artifact_ids}))
            attempts.append(ExtractionAttempt(attempt_id="stage:" + record_hash([uid, number, step.stage_signature,
                step.call_signature, step.attempt, state, output_hashes]), unit_id=uid, stage=step.stage,
                input_sha256=self.source_sha, config_sha256=step.config_hash, code_revision=code_revision,
                state=state, outputs=output_hashes, reason=step.reason_code or (step.status if state != "SUCCEEDED" else None)))
        campaign = record_hash({"source_id": self.sid, "source_sha256": self.source_sha,
            "prepare_signature": self.prep.get("prepare_signature"), "config_sha256": self.config_sha, "rule": RULE})
        ledger = CoverageLedger(campaign_sha256=campaign, units=tuple(self.units.values()),
            inspected_units=tuple(sorted(self.inspected)), candidates=tuple(dispositions), attempts=tuple(attempts))
        report = ledger.report()
        structural = (self.expected["denominator_state"] == "KNOWN" and self.expected["pagination_agrees"]
            and not any(self.expected[k] for k in ("missing_indices", "extra_indices", "duplicate_indices", "invalid_index_count")))
        if not structural or self.gaps:
            report["status"] = "INCOMPLETE"
            report["unit_accounting_complete"] = False
        report["extraction_completeness"] = "NOT_ESTABLISHED"
        report["candidate_review_debt"] = sum(c.state in {"NEEDS_REVIEW", "UNSUPPORTED", "FAILED", "UNREADABLE"}
                                               for c in dispositions)
        return {"schema": "vkm-pipeline-accounting/1", "rule": RULE, "source_id": self.sid,
            "source_sha256": self.source_sha, "prepare_signature": self.prep.get("prepare_signature"),
            "config_sha256": self.config_sha, "code_revision": code_revision, "expected": self.expected,
            "ledger": ledger.model_dump(mode="json"), "report": report,
            "raw_artifacts": dict(sorted(self.artifacts.items())), "occurrences": list(self.candidates.values()),
            "channels": self.channels, "gaps": sorted(set(self.gaps)), "unmatched_output_ids": unmatched,
            "errors": [{"code": e.code, "stage": e.stage, "page_index": e.page_index} for e in result.errors],
            "attempt_history_scope": "SOURCE_SHA_PINNED_EVENTS_AND_ASSEMBLY_STEPS",
            "historical_unpinned_cache_attempts": sum(1 for entries in getattr(cache, "calls", {}).values() for r in entries
                if r.get("source_id") == self.sid and r.get("source_sha256") is None),
            "output_objects": {r["object_id"]: r["content_sha256"] for r in outputs},
            "ocr_events": [ref for ref, _ in events], "native_document_raw_only": self.prep.get("document_raw_artifact_id"),
            "scientific_admission": "NOT_ESTABLISHED", "detector_recall": "NOT_ESTABLISHED"}


def publish_report(root, report):
    """Publish before the source commit. This is not yet its completed receipt."""
    return _publish(Path(root), "reports", report)


def bind_commit(root, report_ref, *, commit_id, source_id, source_sha256):
    report = _read(root, report_ref)
    if report.get("source_id") != source_id or report.get("source_sha256") != source_sha256:
        raise ValueError("ACCOUNTING_COMMIT_SOURCE_MISMATCH")
    marker = _commit_marker(root, source_id, commit_id)
    if marker["source_sha256"] != source_sha256:
        raise ValueError("ACCOUNTING_COMMIT_SOURCE_MISMATCH")
    verify_committed_outputs(root, report, marker)
    receipt = {"schema": "vkm-pipeline-accounting-commit/1", "commit_id": commit_id,
        "source_id": source_id, "source_sha256": source_sha256, "report": report_ref,
        "commit_marker_sha256": record_hash(marker),
        "accounting_state": report["report"]["status"], "scientific_admission": "NOT_ESTABLISHED",
        "directory_durability": "FSYNC_COMPLETED" if os.name == "posix" else "NOT_QUALIFIED",
        "physical_durability": "NOT_QUALIFIED"}
    return _publish(Path(root), "commits", receipt)


def verify_committed_outputs(root, report, marker):
    """Read-only deep check, shared by local binding and receiving publication."""
    import pyarrow.parquet as pq
    source_id, source_sha256 = report["source_id"], report["source_sha256"]

    actual = {}
    for name in ("blocks", "tables", "formulas", "figures"):
        entry = marker["datasets"][name]
        path = _path(Path(root) / "canonical", entry["path"])
        if sha256_of(path) != entry["sha256"]:
            raise ValueError("ACCOUNTING_COMMITTED_PARTITION_CHANGED")
        parquet = pq.ParquetFile(path)
        if parquet.metadata.num_rows != entry["rows"]:
            raise ValueError("ACCOUNTING_COMMITTED_COUNT_MISMATCH")
        for batch in parquet.iter_batches(batch_size=1024, columns=["object_id", "source_id", "source_sha256", "content_sha256"]):
            for row in batch.to_pylist():
                if row["source_id"] != source_id or row["source_sha256"] != source_sha256 or row["object_id"] in actual:
                    raise ValueError("ACCOUNTING_COMMITTED_OBJECT_IDENTITY_MISMATCH")
                actual[row["object_id"]] = row["content_sha256"]
    if actual != report["output_objects"]:
        raise ValueError("ACCOUNTING_COMMITTED_OUTPUTS_MISMATCH")


def _commit_marker(root, source_id, commit_id):
    from vkm_corpus import ids
    if not re.fullmatch(r"VKM-SRC-\d{3}", source_id) or not re.fullmatch(r"CMT-[a-f0-9]{16}", commit_id):
        raise ValueError("ACCOUNTING_COMMIT_IDENTITY_INVALID")
    base = _path(root, "canonical/_commits")
    paths = list(base.glob(f"run=*/{source_id}__{commit_id}.json"))
    if len(paths) != 1:
        raise ValueError("ACCOUNTING_COMMIT_MARKER_MISSING")
    path = _path(base, paths[0].relative_to(base).as_posix())
    if path.stat().st_size > MAX_REPORT_BYTES:
        raise ValueError("ACCOUNTING_COMMIT_MARKER_LIMIT")
    marker = json.loads(path.read_bytes())
    if ids.commit_id(marker) != commit_id or marker.get("source_id") != source_id:
        raise ValueError("ACCOUNTING_COMMIT_MARKER_CHANGED")
    return marker


def verify_binding(root, reference, *, commit_id, source_id, require_head=False):
    receipt = _read(root, reference)
    report = _read(root, receipt["report"])
    CoverageLedger.model_validate(report["ledger"])
    marker = _commit_marker(root, source_id, commit_id)
    if (receipt["commit_id"] != commit_id or receipt["source_id"] != source_id
            or report["source_id"] != source_id or report["source_sha256"] != receipt["source_sha256"]
            or report["source_sha256"] != marker["source_sha256"]
            or receipt["accounting_state"] != report["report"]["status"]
            or receipt["commit_marker_sha256"] != record_hash(marker)):
        raise ValueError("ACCOUNTING_BINDING_MISMATCH")
    if require_head:
        from vkm_corpus.parquet.commits import chain_head
        base = _path(root, "canonical/_commits")
        markers = [json.loads(p.read_bytes()) for p in base.glob(f"run=*/{source_id}__*.json")]
        head, forks = chain_head(markers, source_id)
        if forks or head != commit_id:
            raise ValueError("ACCOUNTING_CURRENT_HEAD_MISSING_BINDING")
    return receipt["accounting_state"]
