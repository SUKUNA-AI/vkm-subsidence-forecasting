"""Gate H0: idempotent technical import of the 60 historical H relevance labels (owner handoff of 05.10.2026).

What this does: binds the frozen review sample ``H_REVIEW_SAMPLE_T1.md`` (the only source of every H value), its freeze
record and the historical review sidecar to the owner's handoff annotations, and writes import artifacts with lineage.
What this does NOT do: change any H, rename any locator, run ``hscore``, open the pooled qrels, review any page again or
create a new independent, blind or human label. H is topical retrieval relevance only (not numeric validity,
applicability, scientific admission or S26 extraction gold).

    intake <handoff dir>   copy the handoff annotation files (unchanged bytes, verified against the package manifest and
                           the pinned hashes below) into ``h_review_import_2026-10-05/handoff_2026-10-05/``
    build                  write the import artifacts (rewrites a file only when its bytes change; a second run is a no-op)
    check                  exit 1 if any artifact differs from what ``build`` would write (nothing is written)
    verbatim-check [--pages <dir>] [--private-root <dir>]
                           compare every text of the import with the historical page texts of the 30.09 review
                           (git-ignored ``work/h_review_2026-09-30/``) and with the PRIVATE quote columns
                           (``$VKM_RESOURCES_ROOT``) using the leakage guard's shingles; writes ``VERBATIM_CHECK.json``
                           (hashes and counts only, no text), then run ``build`` again

usage: python benchmarks/topic_v1/scripts/import_h_review_2026_10_05.py {intake <dir>|build|check|verbatim-check}
"""
from __future__ import annotations

import csv
import hashlib
import json
import os
import re
import sys
from collections import Counter
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
BENCH = REPO / "benchmarks" / "topic_v1"
OUT_DIR = BENCH / "h_review_import_2026-10-05"
HANDOFF_DIR = OUT_DIR / "handoff_2026-10-05"
REL_BENCH = "benchmarks/topic_v1"
REL_OUT = f"{REL_BENCH}/h_review_import_2026-10-05"
REL_HANDOFF = f"{REL_OUT}/handoff_2026-10-05"

IMPORT_ID = "H0-IMPORT-2026-10-05"
PREPARED_DATE = "2026-10-05"
SNAPSHOT_ID = "snap-20260929T175107Z-574daaac"
SAMPLE_SHA256 = "4425711b1dc99650ea3b3e8cda3a399994bb961f7e602f95a993d35b90dcd9e4"
TOPIC_SET_SHA256 = "4dbc595723cbb9ed3e59d1a71cbe84301c9554ecedc41b7f3abf654258bac2b1"
HANDOFF_PACKAGE = "VKM_CLAUDE_HANDOFF_2026-10-05"
# SHA-256 of the package's MANIFEST.sha256 (13 files; the manifest itself is not copied: only the lines of the files below)
HANDOFF_MANIFEST_SHA256 = "2b39772b372540e79872ab16227f4234043d156299d6512c5bcbfa6e1c6994ad"
# the handoff files imported here (path inside the package → pinned SHA-256, equal to its MANIFEST.sha256 line)
HANDOFF_FILES = {
    "annotations/H_REVIEW_T1_ANNOTATED.jsonl": "6a5ba6a97e84bcbfd6d6b3fad65ad209bfcd17b67a11f669ea27610912905313",
    "annotations/SOURCE_FIRST_REVIEW_REQUESTS.jsonl": "e54e8ddf2307163bdd83130dc1ccdc4ec16a95e766119ab31eab69e11259dcad",
    "annotations/H_REVIEW_IMPORT_RECEIPT.json": "84eec81aeacc5b80da0247c10366a30dca2116592038d081062ef39e3d09da50",
    "annotations/H_REVIEW_T1_ANNOTATED.md": "bc90d8c955f4341c26a7d050353f1bde455266632706a00b00b81f8aeb4d02b9",
}
# a package file not copied: byte-identical to the repository's frozen sample (checked at intake)
HANDOFF_ORIGINAL_SAMPLE = "annotations/H_REVIEW_SAMPLE_T1.original.md"

GRADE_MEANING = {3: "DIRECT_EVIDENCE_FOR_TOPIC", 2: "SUBSTANTIAL_PARTIAL", 1: "MENTION_OR_ADJACENT", 0: "NOT_RELEVANT"}
EXPECTED_GRADES = {0: 14, 1: 10, 2: 9, 3: 27}
EXPECTED_TRACKS = {"MODEL_FAMILY": 36, "PROCESS": 24}
H3_MEANING = ("H=3 is topical retrieval relevance only: not numeric validity, applicability, scientific admission or "
              "extraction qualification")
_PAGE = re.compile(r"^(VKM-SRC-[0-9]{3}):([pr])[0-9]{4}$")
_TOPIC = re.compile(r"^(PC-[0-9]{2}|MM-[A-Z]+)$")
_GRADE = re.compile(r"^[0-3]$")
# fields that must carry exactly these values in every handoff annotation row
ANNOTATION_CONSTANTS = {
    "schema": "vkm.h_review_import_annotation/1",
    "operation": "IMPORT_EXISTING_FROZEN_LABEL",
    "corpus_snapshot": SNAPSHOT_ID,
    "input_sample_sha256": SAMPLE_SHA256,
    "current_original_review_status": "NOT_RUN",
    "current_review_is_blind": False,
    "new_independent_grade": None,
    "scientific_admission": "NOT_ESTABLISHED",
    "review_domain": "RETRIEVAL_RELEVANCE",
    "extraction_gold_eligibility": "NOT_QUALIFIED_BY_THIS_REVIEW",
    "historical_blindness_status": "DEVIATION_ORIGINAL_TARGET_GRADES_EXPOSED_TO_SUBAGENT",
    "rationale_basis": "PARAPHRASE_OF_HISTORICAL_REVIEW_NOTES_NOT_CURRENT_ORIGINAL_REVIEW",
    "scope_scale_affect_relevance_score": False,
    "source_original_revalidation_required_for_new_acceptance": True,
}
REQUEST_CONSTANTS = {
    "schema": "vkm.h_review_source_request/1",
    "blind_packet": False,
    "request_status": "NOT_RUN",
    "purpose": "OPTIONAL_SOURCE_REVIEW_OF_EXISTING_HISTORICAL_SAMPLE",
    "page_id_mapping": "PRESERVE_EXACT_NAMESPACE",
    "snapshot_id": SNAPSHOT_ID,
}
OUTPUT_LABELS = "H_REVIEW_T1_IMPORTED.jsonl"
OUTPUT_QUEUE = "SOURCE_FIRST_REVIEW_QUEUE.jsonl"
OUTPUT_RECEIPT = "IMPORT_RECEIPT.json"
OUTPUT_VERBATIM = "VERBATIM_CHECK.json"
OUTPUT_SUMS = "SHA256SUMS"


def require(cond: bool, message: str) -> None:
    if not cond:
        raise ValueError(message)


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _reject_constant(value: str):
    raise ValueError(f"non-finite JSON constant {value}")


def read_bytes(path: Path) -> bytes:
    data = path.read_bytes()
    require(b"\r" not in data, f"{path.name}: CR bytes (expected LF-only text)")
    return data


def dumps_line(obj) -> str:
    return json.dumps(obj, ensure_ascii=False, sort_keys=True, allow_nan=False)


def dumps_doc(obj) -> bytes:
    return (json.dumps(obj, ensure_ascii=False, sort_keys=True, indent=1, allow_nan=False) + "\n").encode("utf-8")


def table_rows(text: str, ncells: int) -> list[list[str]]:
    """Cells of the markdown table rows with ``ncells`` cells whose first cell is a number."""
    out = []
    for line in text.splitlines():
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if len(cells) == ncells and cells[0].isdigit():
            out.append(cells)
    return out


# ------------------------------------------------------------------------------------------------ inputs
def load_inputs() -> dict:
    """Read and cross-check every input; raises ValueError on the first inconsistency."""
    sample_b = read_bytes(BENCH / "H_REVIEW_SAMPLE_T1.md")
    require(sha256(sample_b) == SAMPLE_SHA256, "H_REVIEW_SAMPLE_T1.md differs from the frozen sample")
    freeze_b = read_bytes(BENCH / "H_REVIEW_FREEZE_2026-09-30.json")
    freeze = json.loads(freeze_b, parse_constant=_reject_constant)
    notes_b = read_bytes(BENCH / "H_REVIEW_NOTES_2026-09-30.md")
    require(freeze["schema"] == "vkm.h_review_freeze/1", "unexpected freeze schema")
    require(freeze["sample_sha256"] == SAMPLE_SHA256, "freeze does not bind the sample")
    require(freeze["notes_sha256"] == sha256(notes_b), "freeze does not bind the notes")
    require(freeze["snapshot_id"] == SNAPSHOT_ID, "freeze snapshot differs")
    require(freeze["grades_changed_after_root_review"] == 0, "freeze reports changed grades")
    require(freeze["pooled_labels_opened_before_freeze"] is False, "freeze reports opened pool")
    topic_b = (BENCH / "topic_set_v1.jsonl").read_bytes()
    require(sha256(topic_b.replace(b"\r\n", b"\n")) == TOPIC_SET_SHA256, "topic_set_v1.jsonl differs from the frozen set")
    topics = {}
    for line in topic_b.decode("utf-8").splitlines():
        if line.strip():
            t = json.loads(line, parse_constant=_reject_constant)
            topics[t["topic_id"]] = {"track": t["track"], "title": t["title"]}
    result_b = read_bytes(BENCH / "H_REVIEW_RESULT_2026-09-30.json")      # hashed only: hscore is not re-executed

    handoff = {}
    for rel in HANDOFF_FILES:
        path = HANDOFF_DIR / Path(rel).name
        require(path.is_file(), f"handoff copy missing: {path.name} (run `intake <handoff dir>`)")
        data = read_bytes(path)
        require(sha256(data) == HANDOFF_FILES[rel], f"handoff copy changed: {path.name}")
        handoff[rel] = data

    # the frozen sample: the only source of H
    sample = []
    for cells in table_rows(sample_b.decode("utf-8"), 6):
        n, tid, title, pid, desc, h = cells
        require(_GRADE.match(h) is not None, f"sample row {n}: H {h!r} is not a strict integer 0..3")
        require(_TOPIC.match(tid) is not None and _PAGE.match(pid) is not None, f"sample row {n}: bad id")
        sample.append({"sample_id": int(n), "topic_id": tid, "topic_title_as_supplied": title, "page_id": pid,
                       "description_as_supplied": desc, "H": int(h)})
    require([r["sample_id"] for r in sample] == list(range(1, 61)), "sample ids are not 1..60 in order")
    require(len({(r["topic_id"], r["page_id"]) for r in sample}) == 60, "duplicate topic/page pair in the sample")

    # the historical sidecar: own grades before root review, method and own rationale per row
    notes = {}
    for cells in table_rows(notes_b.decode("utf-8"), 6):
        if _TOPIC.match(cells[1]) and _PAGE.match(cells[2]):
            n = int(cells[0])
            require(n not in notes, f"notes row {n} repeated")
            notes[n] = {"topic_id": cells[1], "page_id": cells[2], "H": cells[3], "method": cells[4],
                        "rationale": cells[5]}
    require(sorted(notes) == list(range(1, 61)), "notes table does not cover samples 1..60")
    for r in sample:
        nr = notes[r["sample_id"]]
        require((nr["topic_id"], nr["page_id"], nr["H"]) == (r["topic_id"], r["page_id"], str(r["H"])),
                f"notes row {r['sample_id']} differs from the frozen sample")
        require(nr["method"] in ("text", "text+image"), f"notes row {r['sample_id']}: method {nr['method']!r}")

    return {"sample": sample, "sample_b": sample_b, "freeze": freeze, "freeze_b": freeze_b, "notes": notes,
            "notes_b": notes_b, "topics": topics, "topic_b": topic_b, "result_b": result_b, "handoff": handoff}


def check_annotations(inp: dict) -> list[dict]:
    """The handoff rows, verified field by field against the frozen sample, the notes, the freeze and the topic set."""
    data = inp["handoff"]["annotations/H_REVIEW_T1_ANNOTATED.jsonl"]
    lines = data.decode("utf-8").split("\n")
    require(lines[-1] == "" and all(lines[:-1]), "annotation JSONL: blank line or missing final newline")
    rows = []
    rechecked = set(inp["freeze"]["root_rechecked_sample_ids"])
    require(len(rechecked) == 31, "freeze: expected 31 root-rechecked rows")
    md_rationale = {int(c[0]): c[5] for c in
                    table_rows(inp["handoff"]["annotations/H_REVIEW_T1_ANNOTATED.md"].decode("utf-8"), 6)}
    for k, line in enumerate(lines[:-1], 1):
        a = json.loads(line, parse_constant=_reject_constant)
        s = inp["sample"][k - 1]
        n = s["sample_id"]
        require(a["sample_id"] == n, f"annotation line {k}: sample order changed")
        for f in ("topic_id", "topic_title_as_supplied", "page_id", "description_as_supplied"):
            require(a[f] == s[f], f"annotation {n}: {f} differs from the frozen sample")
        require(type(a["H"]) is int and a["H"] == s["H"], f"annotation {n}: H differs from the frozen sample")
        for f, v in ANNOTATION_CONSTANTS.items():
            require(f in a and a[f] == v and type(a[f]) is type(v), f"annotation {n}: {f} = {a.get(f)!r}")
        m = _PAGE.match(s["page_id"])
        require(a["source_id"] == m.group(1), f"annotation {n}: source_id")
        require(a["page_namespace"] == ("PAGE" if m.group(2) == "p" else "RENDERED_DOCX"), f"annotation {n}: namespace")
        topic = inp["topics"].get(s["topic_id"])
        require(topic is not None and a["track"] == topic["track"], f"annotation {n}: track differs from topic_set_v1")
        title = s["topic_title_as_supplied"]
        truncated = title.endswith("…")
        require(a["title_truncated_in_input"] is truncated, f"annotation {n}: truncation flag")
        require(topic["title"] == title or (truncated and topic["title"].startswith(title[:-1])),
                f"annotation {n}: title differs from topic_set_v1")
        require(a["grade_meaning"] == GRADE_MEANING[s["H"]], f"annotation {n}: grade meaning")
        require(a["historical_review_method_reported"] == inp["notes"][n]["method"], f"annotation {n}: method")
        require(a["historical_root_rechecked_reported"] is (n in rechecked), f"annotation {n}: root recheck flag")
        ref = a["historical_review_reference"]
        require(ref["path"] == f"{REL_BENCH}/H_REVIEW_NOTES_2026-09-30.md" and ref["sample_id"] == n,
                f"annotation {n}: historical review reference")
        require(a["freeze_reference"]["path"] == f"{REL_BENCH}/H_REVIEW_FREEZE_2026-09-30.json",
                f"annotation {n}: freeze reference")
        require(ref["commit"] == a["freeze_reference"]["commit"], f"annotation {n}: reference commits differ")
        require(isinstance(a["rationale_ru"], str) and a["rationale_ru"].strip(), f"annotation {n}: empty rationale")
        require(md_rationale.get(n) == a["rationale_ru"], f"annotation {n}: rationale differs between JSONL and MD")
        rows.append({"annotation": a, "line": k, "line_sha256": sha256(line.encode("utf-8"))})
    require(len(rows) == 60, "expected 60 annotation rows")
    return rows


def check_requests(inp: dict) -> list[dict]:
    data = inp["handoff"]["annotations/SOURCE_FIRST_REVIEW_REQUESTS.jsonl"]
    lines = data.decode("utf-8").split("\n")
    require(lines[-1] == "" and all(lines[:-1]), "request JSONL: blank line or missing final newline")
    out = []
    for k, line in enumerate(lines[:-1], 1):
        r = json.loads(line, parse_constant=_reject_constant)
        for f, v in REQUEST_CONSTANTS.items():
            require(f in r and r[f] == v and type(r[f]) is type(v), f"request line {k}: {f} = {r.get(f)!r}")
        require(set(r) <= set(REQUEST_CONSTANTS) | {"page_id", "source_id", "topic_pairs", "instructions_ru",
                                                     "required_material"}, f"request line {k}: unexpected field")
        require(all(set(p) == {"sample_id", "topic_id"} for p in r["topic_pairs"]),
                f"request line {k}: topic pairs carry more than ids (no grades in the queue)")
        out.append({"request": r, "line": k, "line_sha256": sha256(line.encode("utf-8"))})
    # the queue derived from the frozen sample: one request per page, pairs in sample order, pages in id order
    by_page: dict[str, list[dict]] = {}
    for s in inp["sample"]:
        by_page.setdefault(s["page_id"], []).append({"sample_id": s["sample_id"], "topic_id": s["topic_id"]})
    derived = [(pid, _PAGE.match(pid).group(1), pairs) for pid, pairs in sorted(by_page.items())]
    got = [(x["request"]["page_id"], x["request"]["source_id"], x["request"]["topic_pairs"]) for x in out]
    require(got == derived, "request queue differs from the grouping of the frozen sample")
    require(len(out) == 59, "expected 59 page requests")
    return out


# ------------------------------------------------------------------------------------------------ outputs
def input_records(inp: dict) -> list[dict]:
    recs = [
        ("FROZEN_HISTORICAL_SAMPLE_ONLY_SOURCE_OF_H", f"{REL_BENCH}/H_REVIEW_SAMPLE_T1.md", inp["sample_b"]),
        ("FREEZE_RECORD", f"{REL_BENCH}/H_REVIEW_FREEZE_2026-09-30.json", inp["freeze_b"]),
        ("HISTORICAL_REVIEW_SIDECAR_RATIONALE_ATTRIBUTION", f"{REL_BENCH}/H_REVIEW_NOTES_2026-09-30.md",
         inp["notes_b"]),
        ("FROZEN_TOPIC_SET_TRACK_AND_TITLE_LOOKUP", f"{REL_BENCH}/topic_set_v1.jsonl",
         inp["topic_b"].replace(b"\r\n", b"\n")),
        ("HISTORICAL_AGREEMENT_RESULT_HASHED_ONLY_NOT_RECOMPUTED", f"{REL_BENCH}/H_REVIEW_RESULT_2026-09-30.json",
         inp["result_b"]),
    ]
    roles = {
        "annotations/H_REVIEW_T1_ANNOTATED.jsonl": "OWNER_HANDOFF_ANNOTATION_TRANSPORT",
        "annotations/SOURCE_FIRST_REVIEW_REQUESTS.jsonl": "OWNER_HANDOFF_FUTURE_REVIEW_REQUESTS",
        "annotations/H_REVIEW_IMPORT_RECEIPT.json": "OWNER_HANDOFF_RECEIPT",
        "annotations/H_REVIEW_T1_ANNOTATED.md": "OWNER_HANDOFF_HUMAN_READABLE_TABLE",
    }
    for rel, data in inp["handoff"].items():
        recs.append((roles[rel], f"{REL_HANDOFF}/{Path(rel).name}", data))
    return [{"role": role, "path": path, "sha256": sha256(data), "bytes": len(data)} for role, path, data in recs]


def build_labels(inp: dict, ann: list[dict]) -> bytes:
    freeze_sha = sha256(inp["freeze_b"])
    notes_sha = sha256(inp["notes_b"])
    ann_sha = HANDOFF_FILES["annotations/H_REVIEW_T1_ANNOTATED.jsonl"]
    lines = []
    for s, x in zip(inp["sample"], ann):
        a = x["annotation"]
        n = s["sample_id"]
        row = {
            "schema": "vkm.h_review_imported_label/1",
            "import_id": IMPORT_ID,
            "label_key": f"{s['topic_id']}|{s['page_id']}",
            "sample_id": n,
            "topic_id": s["topic_id"],
            "topic_title_as_supplied": s["topic_title_as_supplied"],
            "title_truncated_in_input": a["title_truncated_in_input"],
            "track": a["track"],
            "page_id": s["page_id"],
            "source_id": a["source_id"],
            "page_namespace": a["page_namespace"],
            "description_as_supplied": s["description_as_supplied"],
            "H": s["H"],
            "grade_meaning": GRADE_MEANING[s["H"]],
            "label_role": "HISTORICAL_RETRIEVAL_RELEVANCE_LABEL",
            "review_domain": "RETRIEVAL_RELEVANCE",
            "operation": "IMPORT_EXISTING_FROZEN_LABEL",
            "grade_changed_by_import": False,
            "new_independent_grade": None,
            "current_original_review_status": "NOT_RUN",
            "current_review_is_blind": False,
            "historical_blindness_status": inp["freeze"]["blindness_status"],
            "historical_review_method_reported": a["historical_review_method_reported"],
            "historical_root_rechecked_reported": a["historical_root_rechecked_reported"],
            "scientific_admission": "NOT_ESTABLISHED",
            "s26_extraction_gold": "NOT_QUALIFIED",
            "extraction_gold_eligibility": a["extraction_gold_eligibility"],
            "scope_scale_affect_relevance_score": False,
            "source_original_revalidation_required_for_new_acceptance": True,
            "rationale_ru": a["rationale_ru"],
            "rationale_basis": a["rationale_basis"],
            "rationale_attribution": {
                "path": f"{REL_BENCH}/H_REVIEW_NOTES_2026-09-30.md", "sha256": notes_sha, "sample_id": n,
                "historical_cell_sha256": sha256(inp["notes"][n]["rationale"].encode("utf-8")),
                "reverified_now": False},
            "lineage": {
                "h_value": {"path": f"{REL_BENCH}/H_REVIEW_SAMPLE_T1.md", "sha256": SAMPLE_SHA256, "table_row": n,
                            "role": "FROZEN_HISTORICAL_SAMPLE_ONLY_SOURCE_OF_H"},
                "freeze": {"path": f"{REL_BENCH}/H_REVIEW_FREEZE_2026-09-30.json", "sha256": freeze_sha,
                           "frozen_at_utc": inp["freeze"]["frozen_at_utc"], "role": "FREEZE_RECORD"},
                "annotation": {"path": f"{REL_HANDOFF}/H_REVIEW_T1_ANNOTATED.jsonl", "sha256": ann_sha,
                               "line": x["line"], "line_sha256": x["line_sha256"], "schema": a["schema"],
                               "package": HANDOFF_PACKAGE, "role": "OWNER_HANDOFF_ANNOTATION_TRANSPORT"},
                "upstream_freeze_reference": a["freeze_reference"],
                "corpus_snapshot": SNAPSHOT_ID,
            },
        }
        lines.append(dumps_line(row))
    return ("\n".join(lines) + "\n").encode("utf-8")


def build_queue(req: list[dict]) -> bytes:
    req_sha = HANDOFF_FILES["annotations/SOURCE_FIRST_REVIEW_REQUESTS.jsonl"]
    lines = []
    for k, x in enumerate(req, 1):
        r = x["request"]
        row = {
            "schema": "vkm.h_review_source_request_queued/1",
            "import_id": IMPORT_ID,
            "queue_position": k,
            "page_id": r["page_id"],
            "source_id": r["source_id"],
            "topic_pairs": r["topic_pairs"],
            "queue_role": "FUTURE_SEPARATE_SOURCE_FIRST_REVIEW",
            "blind_packet": False,
            "not_blind_because": "historical labels, rationales and target grades are already exposed in this lineage; "
                                 "a new review needs a separate clean context and a definitions-only export",
            "request_status": "NOT_RUN",
            "purpose": r["purpose"],
            "page_id_mapping": r["page_id_mapping"],
            "required_material": r["required_material"],
            "instructions_ru": r["instructions_ru"],
            "snapshot_id": r["snapshot_id"],
            "lineage": {"path": f"{REL_HANDOFF}/SOURCE_FIRST_REVIEW_REQUESTS.jsonl", "sha256": req_sha,
                        "line": x["line"], "line_sha256": x["line_sha256"], "schema": r["schema"],
                        "package": HANDOFF_PACKAGE, "role": "OWNER_HANDOFF_FUTURE_REVIEW_REQUESTS"},
        }
        lines.append(dumps_line(row))
    return ("\n".join(lines) + "\n").encode("utf-8")


def verbatim_status(labels_b: bytes, queue_b: bytes) -> dict:
    """The committed verbatim check, accepted only when it is bound to exactly these outputs and handoff copies."""
    path = OUT_DIR / OUTPUT_VERBATIM
    if not path.is_file():
        return {"status": "NOT_RUN", "path": f"{REL_OUT}/{OUTPUT_VERBATIM}"}
    data = read_bytes(path)
    v = json.loads(data, parse_constant=_reject_constant)
    want = {f"{REL_OUT}/{OUTPUT_LABELS}": sha256(labels_b), f"{REL_OUT}/{OUTPUT_QUEUE}": sha256(queue_b)}
    want |= {f"{REL_HANDOFF}/{Path(rel).name}": digest for rel, digest in HANDOFF_FILES.items()}
    bound = {f["path"]: f["sha256"] for f in v.get("checked_files", [])}
    status = v.get("status") if bound == want else "STALE_REBIND_REQUIRED"
    return {"status": status, "path": f"{REL_OUT}/{OUTPUT_VERBATIM}", "sha256": sha256(data)}


def build_receipt(inp: dict, ann: list[dict], req: list[dict], outputs: dict[str, bytes], verbatim: dict) -> bytes:
    sample = inp["sample"]
    grades = Counter(s["H"] for s in sample)
    tracks = Counter(x["annotation"]["track"] for x in ann)
    require(dict(grades) == EXPECTED_GRADES, f"grade distribution {dict(grades)}")
    require(dict(tracks) == EXPECTED_TRACKS, f"track distribution {dict(tracks)}")
    pages = Counter(s["page_id"] for s in sample)
    shared = [{"page_id": p, "sample_ids": [s["sample_id"] for s in sample if s["page_id"] == p],
               "topic_ids": [s["topic_id"] for s in sample if s["page_id"] == p]}
              for p, c in sorted(pages.items()) if c > 1]
    up = json.loads(inp["handoff"]["annotations/H_REVIEW_IMPORT_RECEIPT.json"], parse_constant=_reject_constant)
    require(up["schema"] == "vkm.h_review_import_receipt/1", "handoff receipt schema")
    require(up["input_sha256"] == SAMPLE_SHA256 and up["frozen_hash_match"] is True, "handoff receipt identity")
    require(up["grades_changed"] == 0 and up["new_independent_labels"] == 0, "handoff receipt overclaims")
    require(up["hscore_executed"] is False and up["s26_extraction_gold"] == "NOT_QUALIFIED", "handoff receipt flags")
    require({int(k): v for k, v in up["grade_counts"].items()} == dict(grades), "handoff receipt grade counts")
    require(up["track_counts"] == dict(tracks), "handoff receipt track counts")
    require(up["row_count"] == 60 and up["unique_pages"] == 59 and up["unique_topic_page_pairs"] == 60,
            "handoff receipt counts")
    images = sum(1 for x in ann if x["annotation"]["historical_review_method_reported"] == "text+image")
    rechecked = sum(1 for x in ann if x["annotation"]["historical_root_rechecked_reported"])
    require(images == up["historical_image_review_rows_reported"] == 34, "image-review rows")
    require(rechecked == up["historical_root_rechecked_rows_reported"] == 31, "root-rechecked rows")
    receipt = {
        "schema": "vkm.h_review_repo_import_receipt/1",
        "import_id": IMPORT_ID,
        "gate": "H0",
        "prepared_date": PREPARED_DATE,
        "operation": "IDEMPOTENT_TECHNICAL_IMPORT_OF_HISTORICAL_LABELS_WITH_LINEAGE",
        "output_domain": "HISTORICAL_RETRIEVAL_RELEVANCE_LABELS",
        "snapshot_id": SNAPSHOT_ID,
        "inputs": input_records(inp),
        "handoff": {
            "package": HANDOFF_PACKAGE,
            "package_manifest_sha256": HANDOFF_MANIFEST_SHA256,
            "imported_files": [{"package_path": rel, "path": f"{REL_HANDOFF}/{Path(rel).name}", "sha256": digest}
                               for rel, digest in sorted(HANDOFF_FILES.items())],
            "not_imported_byte_identical_to_repository": {
                "package_path": HANDOFF_ORIGINAL_SAMPLE, "sha256": SAMPLE_SHA256,
                "repository_path": f"{REL_BENCH}/H_REVIEW_SAMPLE_T1.md"},
            "package_delivery_validation": {
                "tool": "validate_delivery.py of the package (stdlib, package files only)", "status": "PASS",
                "scope": "HANDOFF_FILE_INTEGRITY_AND_LABEL_IMPORT_ONLY",
                "run": "by the owner before delivery and again by the import executor on 2026-10-05"},
            "upstream_receipt_schema": up["schema"],
            "upstream_operation": up["operation"],
            "upstream_freeze_reference": up["freeze_reference"],
        },
        "outputs": [{"path": f"{REL_OUT}/{name}", "sha256": sha256(data),
                     "rows": data.count(b"\n")} for name, data in sorted(outputs.items())],
        "counts": {
            "rows": len(sample),
            "unique_topic_page_pairs": len({(s["topic_id"], s["page_id"]) for s in sample}),
            "unique_pages": len(pages),
            "grade_counts": {str(k): grades[k] for k in sorted(grades)},
            "track_counts": dict(sorted(tracks.items())),
            "shared_page_different_topics": shared,
            "rendered_locators_preserved": sorted(s["page_id"] for s in sample if ":r" in s["page_id"]),
            "historical_image_review_rows_reported": images,
            "historical_root_rechecked_rows_reported": rechecked,
            "request_queue_items": len(req),
            "request_queue_pairs_covered": sum(len(x["request"]["topic_pairs"]) for x in req),
        },
        "grades_changed": 0,
        "locators_changed": 0,
        "new_independent_labels": 0,
        "new_original_reviews": 0,
        "hscore_executed": False,
        "pooled_qrels_opened_by_import": False,
        "historical_files_written": False,
        "historical_blindness_status": inp["freeze"]["blindness_status"],
        "import_context": {
            "prior_H_and_rationales_exposed": True,
            "historical_agreement_result_visible": True,
            "is_blind": False,
            "note": "the import is mechanical; no grade is assigned or adjudicated in this context",
        },
        "rationale_policy": "rationale_ru is the owner's paraphrase of the historical sidecar, attributed to "
                            "H_REVIEW_NOTES_2026-09-30.md and not re-verified against originals now",
        "h3_meaning": H3_MEANING,
        "scientific_admission": "NOT_ESTABLISHED",
        "s26_extraction_gold": "NOT_QUALIFIED",
        "public_text_check": verbatim,
        "checks": [
            "frozen sample SHA-256 equals the freeze and the pinned value",
            "freeze binds the historical notes; snapshot, grades_changed_after_root_review=0, pool not opened",
            "sample ids 1..60 complete and ordered; 60 unique topic/page pairs; strict integer H 0..3",
            "notes table rows 1..60 equal the sample (topic, page, H); method text or text+image",
            "every annotation field taken from the sample is unchanged (ids, titles, descriptions, H)",
            "annotation constants: NOT_RUN, not blind, new grade null, NOT_ESTABLISHED, RETRIEVAL_RELEVANCE",
            "track and title agree with the frozen topic_set_v1.jsonl; locators and namespaces unchanged",
            "root-recheck flags equal the freeze list; method flags equal the notes",
            "rationale text identical between the handoff JSONL and MD tables",
            "59 page requests equal the grouping of the frozen sample, cover exactly the 60 pairs, not blind",
            "handoff receipt counts and flags agree with the recomputed values",
        ],
        "gate_h0": {
            "status": "PASS",
            "scope": "TECHNICAL_IMPORT_ONLY",
            "labels_preserved": "60/60",
            "lineage_imported": True,
            "new_original_checks": "NOT_RUN",
        },
        "script": f"{REL_BENCH}/scripts/import_h_review_2026_10_05.py",
        "idempotency": "build rewrites a file only when its bytes change; `check` regenerates in memory and compares",
    }
    return dumps_doc(receipt)


def build_outputs() -> dict[str, bytes]:
    """All generated files of the import directory (name → bytes), computed in memory."""
    inp = load_inputs()
    ann = check_annotations(inp)
    req = check_requests(inp)
    labels_b = build_labels(inp, ann)
    queue_b = build_queue(req)
    verbatim = verbatim_status(labels_b, queue_b)
    outputs = {OUTPUT_LABELS: labels_b, OUTPUT_QUEUE: queue_b}
    receipt_b = build_receipt(inp, ann, req, outputs, verbatim)
    files = {**outputs, OUTPUT_RECEIPT: receipt_b}
    sums = {name: sha256(data) for name, data in files.items()}
    if verbatim["status"] != "NOT_RUN":
        sums[OUTPUT_VERBATIM] = verbatim["sha256"]
    sums |= {f"handoff_2026-10-05/{Path(rel).name}": digest for rel, digest in HANDOFF_FILES.items()}
    files[OUTPUT_SUMS] = "".join(f"{d}  {n}\n" for n, d in sorted(sums.items())).encode("utf-8")
    return files


def diff_outputs(files: dict[str, bytes]) -> list[str]:
    return [name for name, data in files.items()
            if not (OUT_DIR / name).is_file() or (OUT_DIR / name).read_bytes() != data]


# ------------------------------------------------------------------------------------------------ commands
def cmd_intake(src: Path) -> None:
    manifest = {}
    require(sha256((src / "MANIFEST.sha256").read_bytes()) == HANDOFF_MANIFEST_SHA256, "package manifest changed")
    for line in (src / "MANIFEST.sha256").read_text(encoding="utf-8").splitlines():
        if line.strip():
            digest, rel = line.split("  ", 1)
            manifest[rel] = digest
    original = (src / HANDOFF_ORIGINAL_SAMPLE).read_bytes()
    require(manifest.get(HANDOFF_ORIGINAL_SAMPLE) == sha256(original) == SAMPLE_SHA256, "package sample changed")
    require(original == (BENCH / "H_REVIEW_SAMPLE_T1.md").read_bytes(), "package sample differs from the repository")
    HANDOFF_DIR.mkdir(parents=True, exist_ok=True)
    for rel, digest in HANDOFF_FILES.items():
        data = (src / rel).read_bytes()
        require(manifest.get(rel) == sha256(data) == digest, f"{rel}: hash differs from manifest/pin")
        dst = HANDOFF_DIR / Path(rel).name
        if dst.is_file():
            require(dst.read_bytes() == data, f"{dst.name}: an existing copy differs; refusing to overwrite")
            continue
        dst.write_bytes(data)
    print(json.dumps({"intake": "OK", "files": sorted(HANDOFF_FILES)}, ensure_ascii=False))


def cmd_build() -> None:
    files = build_outputs()
    changed = diff_outputs(files)
    for name in changed:
        (OUT_DIR / name).write_bytes(files[name])
    print(json.dumps({"build": "OK", "written": changed, "unchanged": sorted(set(files) - set(changed))},
                     ensure_ascii=False))


def cmd_check() -> None:
    changed = diff_outputs(build_outputs())
    print(json.dumps({"check": "OK" if not changed else "DIFFERS", "differs": changed}, ensure_ascii=False))
    if changed:
        raise SystemExit(1)


def _texts(obj) -> list[str]:
    if isinstance(obj, str):
        return [obj]
    if isinstance(obj, dict):
        return [t for v in obj.values() for t in _texts(v)]
    if isinstance(obj, list):
        return [t for v in obj for t in _texts(v)]
    return []


def _longest_common_run(a: list[str], b: list[str]) -> int:
    best = 0
    for n in range(1, len(a) + 1):
        grams = {" ".join(b[i:i + n]) for i in range(len(b) - n + 1)}
        if any(" ".join(a[i:i + n]) in grams for i in range(len(a) - n + 1)):
            best = n
        else:
            break
    return best


def cmd_verbatim_check(argv: list[str]) -> None:
    sys.path.insert(0, str(REPO / "src"))
    from vkm_world.governance import leakage as lk  # noqa: PLC0415

    args = dict(zip(argv[::2], argv[1::2]))
    pages_dir = Path(args.get("--pages", REPO / "work" / "h_review_2026-09-30"))
    private = args.get("--private-root") or os.environ.get("VKM_RESOURCES_ROOT")
    files = build_outputs()
    inp = load_inputs()
    want = {s["sample_id"]: s["page_id"] for s in inp["sample"]}
    texts, text_hashes = {}, {}
    entries = json.loads((pages_dir / "pages_pre_root_review.json").read_text(encoding="utf-8"))["pages"]
    extra = json.loads((pages_dir / "page_47_after_permission.json").read_text(encoding="utf-8"))
    for e in entries + [{**extra, "page_id": want.get(extra["sample_id"])}]:
        resp = e["response"]
        rec = resp["item"]["record"]
        n = e["sample_id"]
        require(rec["page_id"] == want[n] == e["page_id"], f"page text {n}: page id differs")
        require(resp["meta"]["canonical_snapshot_id"] == SNAPSHOT_ID, f"page text {n}: snapshot differs")
        require(rec["text_window"]["truncated"] is False, f"page text {n}: truncated")
        require(sha256(rec["normalized_text"].encode("utf-8")) == rec["text_sha256"], f"page text {n}: hash")
        texts[n] = rec["normalized_text"]
        text_hashes[rec["page_id"]] = rec["text_sha256"]
    require(sorted(texts) == list(range(1, 61)), "page texts do not cover the 60 samples")
    page_shingles = lk.quote_shingles(texts.values())
    quote_shingles, n_csv, n_quotes, csv_digest = set(), 0, 0, hashlib.sha256()
    if private:
        csv.field_size_limit(1 << 30)
        root = Path(private)
        for f in sorted(root.rglob("*.csv")):
            try:
                with open(f, encoding="utf-8", newline="") as fh:
                    rd = csv.reader(fh)
                    header = next(rd, [])
                    qi = [i for i, h in enumerate(header) if h.strip().lower() in lk.FORBIDDEN_COLUMNS]
                    if not qi:
                        continue
                    quotes = [row[i] for row in rd for i in qi if i < len(row)]
            except (UnicodeDecodeError, csv.Error):
                continue
            n_csv += 1
            n_quotes += len(quotes)
            quote_shingles |= lk.quote_shingles(quotes)
            csv_digest.update(f"{f.relative_to(root).as_posix()}\0{sha256(f.read_bytes())}\n".encode("utf-8"))
    checked = {f"{REL_OUT}/{OUTPUT_LABELS}": files[OUTPUT_LABELS], f"{REL_OUT}/{OUTPUT_QUEUE}": files[OUTPUT_QUEUE]}
    checked |= {f"{REL_HANDOFF}/{Path(rel).name}": inp["handoff"][rel] for rel in HANDOFF_FILES}
    runs_pages, runs_quotes, n_texts = 0, 0, 0
    for path, data in checked.items():
        text = data.decode("utf-8")
        units = ([t for line in text.splitlines() if line.strip() for t in _texts(json.loads(line))]
                 if path.endswith(".jsonl") else _texts(json.loads(text)) if path.endswith(".json")
                 else [text] + [c for line in text.splitlines() for c in line.split("|")])
        for t in units:
            w = lk.words(t)
            n_texts += 1
            runs_pages = max(runs_pages, lk.longest_shared_run(w, page_shingles)[0])
            runs_quotes = max(runs_quotes, lk.longest_shared_run(w, quote_shingles)[0])
    labels = [json.loads(line) for line in files[OUTPUT_LABELS].decode("utf-8").splitlines()]
    own_rationale = max(_longest_common_run(lk.words(r["rationale_ru"]), lk.words(texts[r["sample_id"]]))
                        for r in labels)
    own_description = max(_longest_common_run(lk.words(r["description_as_supplied"]), lk.words(texts[r["sample_id"]]))
                          for r in labels)
    limit = lk.VERBATIM_LIMIT_WORDS
    ok = runs_pages < limit and runs_quotes < limit
    result = {
        "schema": "vkm.h_review_import_verbatim_check/1",
        "import_id": IMPORT_ID,
        "method": f"leakage guard words() and {lk.SHINGLE_WORDS}-word shingles; a PUBLIC text may not repeat "
                  f"{limit} consecutive words of a source text; plus the longest run of consecutive words shared by "
                  "each rationale/description and its own page (any length)",
        "page_texts": {
            "location": "git-ignored work/h_review_2026-09-30/ (raw get_page answers of the 30.09 review, snapshot "
                        f"{SNAPSHOT_ID}); texts are not copied",
            "pages": len(text_hashes), "samples": len(texts),
            "text_sha256_by_page": dict(sorted(text_hashes.items())),
            "max_shingle_run_words": runs_pages,
        },
        "private_quotes": ({"location": "$VKM_RESOURCES_ROOT CSV columns " + ", ".join(sorted(lk.FORBIDDEN_COLUMNS)),
                            "csv_files": n_csv, "quotes": n_quotes, "shingles": len(quote_shingles),
                            "inputs_digest": csv_digest.hexdigest(), "max_shingle_run_words": runs_quotes}
                           if private else {"status": "NOT_RUN", "reason": "VKM_RESOURCES_ROOT not set"}),
        "own_page_longest_common_run_words": {"rationale_ru": own_rationale, "description_as_supplied": own_description},
        "texts_checked": n_texts,
        "checked_files": [{"path": p, "sha256": sha256(d)} for p, d in sorted(checked.items())],
        "excluded_text": [],
        "status": ("PASS" if ok and private else "PASS_PAGES_ONLY" if ok else "FAIL"),
    }
    (OUT_DIR / OUTPUT_VERBATIM).write_bytes(dumps_doc(result))
    print(json.dumps({k: result[k] for k in ("status", "own_page_longest_common_run_words", "texts_checked")} |
                     {"page_run": runs_pages, "quote_run": runs_quotes}, ensure_ascii=False))
    if not ok:
        raise SystemExit(1)


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else ""
    if cmd == "intake" and len(sys.argv) == 3:
        cmd_intake(Path(sys.argv[2]))
    elif cmd == "build":
        cmd_build()
    elif cmd == "check":
        cmd_check()
    elif cmd == "verbatim-check":
        cmd_verbatim_check(sys.argv[2:])
    else:
        raise SystemExit(__doc__)
