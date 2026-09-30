"""FIGURE_READINGS_V1 — the public receipt: counts, types, model and prompt hashes, agreement rates, timing, GPU peak.

    python make_receipt.py --work <dir> --out docs/corpus_platform/receipts/figure_readings_v1.json --events events.json
           --neutral "<local id of file A>=INTAKE-A,<local id of file B>=INTAKE-B"

No extracted values, no captions or quotes, no machine paths, host names or IP addresses go into the receipt; the
user's intake documents appear only under neutral ids (INTAKE-A confidential document, INTAKE-B course project);
the mapping to their local ids is an argument, never part of the code.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import statistics
from collections import Counter, defaultdict
from pathlib import Path

HERE = Path(__file__).resolve().parent
BENCH = HERE.parent
NEUTRAL: dict[str, str] = {}      # local source ids of the user's files -> neutral ids (--neutral, never in code)


def group_of(key: str) -> str:
    return "thesis_VKM-SRC-023" if key.startswith("F023_") else ("corpus" if key.startswith("C") else "intake")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--work", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--events", default="")
    ap.add_argument("--neutral", default="", help="LOCALID=INTAKE-A,LOCALID2=INTAKE-B (the user's files)")
    a = ap.parse_args()
    for pair in [p for p in a.neutral.split(",") if "=" in p]:
        k, v = pair.split("=", 1)
        NEUTRAL[k.strip()] = v.strip()
    work = Path(a.work)
    recs = [json.loads(l) for l in (work / "readings.jsonl").read_text(encoding="utf-8").splitlines() if l.strip()]
    P = json.loads((BENCH / "prompts_v1.json").read_text(encoding="utf-8"))
    ev = json.loads(Path(a.events).read_text(encoding="utf-8")) if a.events else {}

    by_group = defaultdict(lambda: {"images": 0, "types": Counter(), "reading_status": Counter(), "retried": 0,
                                    "sources": Counter()})
    walls = defaultdict(list)
    peaks = defaultdict(int)
    tables = {}
    for r in recs:
        g = by_group[group_of(r["key"])]
        g["images"] += 1
        g["types"][r["classification"]["type"] or "UNPARSED"] += 1
        g["sources"][NEUTRAL.get(r["source_id"], r["source_id"])] += 1
        for rd in r["readers"]:
            walls[(rd["reader"], rd.get("task"))].append(rd["wall_s"])
            peaks[rd["reader"]] = max(peaks[rd["reader"]], rd.get("peak_card_mib") or 0)
            if rd["reader"] == "qwen":
                g["reading_status"][rd.get("reading_status")] += 1
                g["retried"] += 1 if rd.get("attempt") == "retry" else 0
        if r.get("comparison"):
            c = r["comparison"]
            q = next(rd for rd in r["readers"] if rd["reader"] == "qwen")
            gl = next(rd for rd in r["readers"] if rd["reader"] == "glm-ocr")
            tables[r["key"] if not r["key"].startswith(("P13_", "KR_")) else NEUTRAL.get(r["source_id"], "INTAKE") + ":" + r["key"].split("_")[1]] = {
                "source": NEUTRAL.get(r["source_id"], r["source_id"]),
                "qwen_rows": q["table"]["n_data_rows"], "qwen_columns": q["table"]["n_columns"],
                "qwen_header_rows": q["table"]["n_header_rows"], "qwen_unreadable_cells": q["table"]["unreadable_cells"],
                "glm_rows_incl_header": gl["table"]["n_rows"], "glm_columns": gl["table"]["n_columns"],
                "cells_compared": c["cells_compared"], "cells_agree": c["cells_agree"], "agreement": c["agreement"],
                "agreement_tolerant": c.get("agreement_tolerant"), "disagreement_kinds": c.get("disagreement_kinds"),
                "rows_paired": c["rows_paired"], "column_offsets_used": c.get("column_offsets_used"),
                "disagreements": len(c["disagreements"])}
    cls_walls = []
    for f in (work / "raw" / "qwen" / "CLASSIFY").glob("*.json"):
        cls_walls.append(json.loads(f.read_text(encoding="utf-8"))["wall_s"])
    retry_recs = [json.loads(f.read_text(encoding="utf-8")) for f in (work / "raw" / "qwen" / "EXTRACT_RETRY").glob("*.json")] \
        if (work / "raw" / "qwen" / "EXTRACT_RETRY").exists() else []
    retry_walls = [r["wall_s"] for r in retry_recs]
    retry_kinds = Counter((r.get("retry_kind") or ("loop_presence_penalty" if "presence_penalty" in r.get("sampling", {})
                                                   else "long_answer_larger_cap"),
                           "ok" if r.get("finish_reason") == "stop" else "capped_again") for r in retry_recs)
    loops_first = sum(1 for f in (work / "raw" / "qwen" / "EXTRACT").glob("*.json")
                      if json.loads(f.read_text(encoding="utf-8")).get("finish_reason") == "length")
    hash_check = Counter(r.get("sent_image_hash_check", "NOT_CHECKED") for r in recs)
    for f in (work / "raw").rglob("*.json"):          # every request, incl. classification and discarded answers
        try:
            rr = json.loads(f.read_text(encoding="utf-8"))
        except (ValueError, OSError):
            continue
        who = "glm-ocr" if f.parts[-3] == "glm" or rr.get("reader") == "glm-ocr" else "qwen"
        peaks[who] = max(peaks[who], rr.get("peak_card_mib") or 0)
    derived = sorted(r["key"] if not r["key"].startswith(("P13_", "KR_")) else
                     NEUTRAL.get(r["source_id"], "INTAKE") + ":" + r["key"].split("_")[1]
                     for r in recs if r["key"].endswith(("T", "a", "b", "R")) and r["key"][-1] in "TabR"
                     and not r["key"].startswith("C"))
    tot_cells = sum(t["cells_compared"] for t in tables.values())
    tot_agree = sum(t["cells_agree"] for t in tables.values())
    tol = lambda t: round((t["agreement_tolerant"] or 0) * t["cells_compared"])
    tot_tol = sum(tol(t) for t in tables.values())
    thesis_tables = {k: v for k, v in tables.items() if k.startswith("F023_")}
    th_cells = sum(t["cells_compared"] for t in thesis_tables.values())
    th_agree = sum(t["cells_agree"] for t in thesis_tables.values())
    th_tol = sum(tol(t) for t in thesis_tables.values())
    prompt_hashes = {k: hashlib.sha256((P[k] + ("" if k == "CLASSIFY" else "\n" + P["COMMON_SUFFIX"])).encode("utf-8")).hexdigest()
                     for k in ["CLASSIFY", "TABLE_SCREENSHOT", "MAP", "GEOLOGICAL_SECTION", "CHART", "SCHEME_DRAWING",
                               "PHOTO", "OTHER"]}
    prompt_hashes["GLM_TABLE"] = hashlib.sha256(P["decoding"]["glm_table"]["prompt"].encode("utf-8")).hexdigest()
    receipt = {
        "receipt": "figure_readings_v1",
        "agent": "QV",
        "date": "2026-09-29",
        "task": "structured, reviewable VLM readings of complex figures: the thesis of Filatova (VKM-SRC-023, 64 image "
                "occurrences), the figures of the corpus pages flagged by the data hunt, and the images of two user "
                "intake documents",
        "status": "AUTO_EXTRACTED_UNREVIEWED",
        "origin": "VLM_EXTRACTED",
        "status_note": "model readings are never VERIFIED and never observations; mine attribution only as printed in "
                       "the image or caption; UNKNOWN/UNREADABLE kept; images, raw answers, parsed JSON, CSV and the "
                       "review page stay in the git-ignored work directory; nothing written to evidence, the canon, "
                       "PRIVATE, CORE or EDGE",
        "code": {"package": "benchmarks/figure_readings_v1/ (prompts_v1.json; scripts: extract_docx_images, "
                            "render_vector, render_pdf_regions, read_figures, parse_readings, make_review, make_receipt, "
                            "gpu_lock)",
                 "client": "Qwen requests through benchmarks/chart_models_v1/scripts/run_models.py (request, "
                           "VramSampler); GLM-OCR requests through vkm_corpus.ocr.client.request_body with the "
                           "pipeline's DEFAULT_SAMPLING and table prompt, grey PNG as run_glm.grey_png"},
        "inputs": {
            "thesis": {"source_id": "VKM-SRC-023", "register_sha256_checked": True,
                       "media_parts": 65, "image_occurrences_in_body": 64,
                       "not_referenced_from_body": 1,
                       "formats": {"png": 41, "jpeg": 19, "emf": 4, "gif": 1},
                       "emf_rendering": "vkm-libreoffice:24.2 (--network none) EMF -> PDF, pypdfium2 render of the "
                                        "drawing box (long side 3200 px)",
                       "caption_mapping": "DrawingML/VML relationship -> media part; caption = the nearer of the first "
                                          "figure-caption paragraph below and the last table-caption paragraph above "
                                          "(each searched up to 6 paragraphs; a tie goes to the figure caption)"},
            "corpus": {"regions": sum(1 for r in recs if group_of(r["key"]) == "corpus"),
                       "sources": sorted({r["source_id"] for r in recs if group_of(r["key"]) == "corpus"}),
                       "render": "pypdfium2 at 200 dpi from the PRIVATE PDFs (sha256 checked against the register), "
                                 "boxes = corpus FIGURE/TABLE bboxes + 6 pt; two slides as whole pages"},
            "intake": {"INTAKE-A": "confidential enterprise document (2 images; readings local only)",
                       "INTAKE-B": "course project DOCX (8 images)"},
            "derived_images_read_in_addition": derived,
        },
        "models": {
            "qwen": {"model_id": "Qwen/Qwen3.5-9B", "weights_repo": "unsloth/Qwen3.5-9B-GGUF",
                     "revision": "3885219b6810b007914f3a7950a8d1b469d598a5",
                     "files_sha256": {"Qwen3.5-9B-Q8_0.gguf": "809626574d0cb43d4becfa56169980da2bb448f2299270f7be443cb89d0a6ae4",
                                      "mmproj-F16.gguf": "f70dc3509053962b0d0d3ee8a7eacebf5d60aa560cad78254ae8698516ae029f"},
                     "server": "llama.cpp b11243-fc07d781e, ghcr.io/ggml-org/llama.cpp@sha256:1c568d229561bbd4577698f1f38d3ed9bf3f5ab343e04f7a508e11ef63f4ced1",
                     "serving": "benchmarks/chart_models_v1/serving/compose.yml profile qwen, own compose project, loopback port",
                     "sampling": "temperature 0, top_p 1, seed 0, non-thinking, cache_prompt false; one retry of an "
                                 "answer that hit the cap: a loop with presence_penalty "
                                 f"{P['decoding']['RETRY_PRESENCE_PENALTY']}, a long answer with max_tokens "
                                 f"{P['decoding']['LONG_RETRY_MAX_TOKENS']}",
                     "prompt_rule": "extraction prompt = class prompt + newline + COMMON_SUFFIX (prompts_v1.json); the "
                                    "hashes below equal the hashes recorded with every request",
                     "max_tokens": P["decoding"]["max_tokens"]},
            "glm_ocr": {"model_id": "zai-org/GLM-OCR", "revision": "2e85a62840ccac27daa451df36c736c4636b8628",
                        "weights_sha256": "a16eb0de98d199293371c560f95f83130d2a2c9612449df16839f08ff9498815",
                        "server": "vLLM v0.30.0, vllm/vllm-openai@sha256:8a69ffad015f138d7170c4ddc429e230a3bc1c1719f67e14324749df200a4b90",
                        "serving": "compose profile glm (internal network, socat proxy on loopback), own compose project",
                        "sampling": "pipeline DEFAULT_SAMPLING (temperature 0, top_p 1e-5, top_k 1, repetition_penalty 1.1, seed 0), max_tokens 6144 (pipeline table cap)"},
        },
        "prompts_file_sha256": hashlib.sha256((BENCH / "prompts_v1.json").read_bytes()).hexdigest(),
        "prompt_sha256": prompt_hashes,
        "counts": {k: {"images": v["images"], "types": dict(v["types"]), "qwen_reading_status": dict(v["reading_status"]),
                       "qwen_retries_used": v["retried"], "by_source": dict(v["sources"])} for k, v in by_group.items()},
        "sent_image_hash_check": dict(hash_check),
        "answers_at_cap": {"first_pass_answers_at_cap": loops_first, "retries": len(retry_walls),
                           "retry_outcomes": {f"{k[0]}:{k[1]}": v for k, v in retry_kinds.items()},
                           "procedure": "an answer that reaches max_tokens is asked once more: a greedy loop (a few "
                                        "lines repeating, or a runaway count 1, 2, 3 …) with presence_penalty, a long "
                                        "answer with a larger cap; a retry capped again is FAILED_REPETITION_LOOP or "
                                        "TRUNCATED_AT_CAP and is not parsed"},
        "tables": {"compared": len(tables), "cells_compared": tot_cells, "cells_agree": tot_agree,
                   "agreement_all": round(tot_agree / tot_cells, 4) if tot_cells else None,
                   "agreement_all_tolerant": round(tot_tol / tot_cells, 4) if tot_cells else None,
                   "thesis_cells_compared": th_cells, "thesis_cells_agree": th_agree,
                   "agreement_thesis": round(th_agree / th_cells, 4) if th_cells else None,
                   "agreement_thesis_tolerant": round(th_tol / th_cells, 4) if th_cells else None,
                   "rule": "strict: cells equal after NFC and whitespace collapsing only; tolerant: also equal when "
                           "they differ only by look-alike Latin/Cyrillic letters, spacing or the decimal separator; "
                           "rows aligned by dynamic programming on tolerant row similarity (gap 0.3), a column offset "
                           "-2…2 per row pair; one-reader cells count as disagreements",
                   "per_table": tables},
        "timing": {
            "qwen_classify_s": {"n": len(cls_walls), "sum": round(sum(cls_walls), 1),
                                "median": round(statistics.median(cls_walls), 2) if cls_walls else None},
            "per_reader_task_s": {f"{k[0]}:{k[1]}": {"n": len(v), "sum": round(sum(v), 1),
                                                   "median": round(statistics.median(v), 2)} for k, v in walls.items()},
            "qwen_retries_s": {"n": len(retry_walls), "sum": round(sum(retry_walls), 1)},
            **ev.get("timing", {}),
        },
        "gpu": {"card": "RTX 5070 Ti 16303 MiB", "desktop_baseline_mib": ev.get("baseline_mib"),
                "peak_card_mib": dict(peaks), "lock": "work/gpu.lock (owner QV) created before the first server, "
                                                      "deleted after the last server stopped",
                **ev.get("gpu", {})},
        "deviations": ev.get("deviations", []),
        "not_done": ev.get("not_done", []),
    }
    Path(a.out).write_text(json.dumps(receipt, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    print("receipt written:", len(tables), "tables,", sum(v["images"] for v in by_group.values()), "images")


if __name__ == "__main__":
    main()
