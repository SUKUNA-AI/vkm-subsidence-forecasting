"""Gate H0 (topic_v1): the technical import of the 60 historical H relevance labels keeps every label and locator,
records lineage, promotes no status and is idempotent (benchmarks/topic_v1/h_review_import_2026-10-05/)."""
from __future__ import annotations

import hashlib
import importlib.util
import json
import shutil
from collections import Counter
from pathlib import Path

import pytest

from vkm_corpus.retrieval_lab import bench as B
from vkm_corpus.retrieval_lab import topic_bench as TB
from vkm_world.governance.leakage import scan

ROOT = Path(__file__).resolve().parents[2]
BENCH = ROOT / "benchmarks" / "topic_v1"
IMPORT = BENCH / "h_review_import_2026-10-05"
SCRIPT = BENCH / "scripts" / "import_h_review_2026_10_05.py"
SAMPLE_SHA256 = "4425711b1dc99650ea3b3e8cda3a399994bb961f7e602f95a993d35b90dcd9e4"
GENERATED = ("H_REVIEW_T1_IMPORTED.jsonl", "SOURCE_FIRST_REVIEW_QUEUE.jsonl", "IMPORT_RECEIPT.json", "SHA256SUMS")


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _sha_lf(path: Path) -> str:
    return hashlib.sha256(path.read_bytes().replace(b"\r\n", b"\n")).hexdigest()


def _jsonl(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def _load_script():
    spec = importlib.util.spec_from_file_location("topic_v1_import_h_review", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _frozen_table() -> list[tuple]:
    """The frozen sample table, parsed independently of the import script."""
    rows = []
    for line in (BENCH / "H_REVIEW_SAMPLE_T1.md").read_text(encoding="utf-8").splitlines():
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if len(cells) == 6 and cells[0].isdigit():
            rows.append((int(cells[0]), cells[1], cells[2], cells[3], cells[4], cells[5]))
    return rows


@pytest.fixture(scope="module")
def labels() -> list[dict]:
    return _jsonl(IMPORT / "H_REVIEW_T1_IMPORTED.jsonl")


@pytest.fixture(scope="module")
def queue() -> list[dict]:
    return _jsonl(IMPORT / "SOURCE_FIRST_REVIEW_QUEUE.jsonl")


@pytest.fixture(scope="module")
def receipt() -> dict:
    return json.loads((IMPORT / "IMPORT_RECEIPT.json").read_text(encoding="utf-8"))


# ---------------------------------------------------------------- identity of the frozen inputs
def test_frozen_sample_and_freeze_are_untouched(receipt):
    assert _sha_lf(BENCH / "H_REVIEW_SAMPLE_T1.md") == SAMPLE_SHA256
    freeze = json.loads((BENCH / "H_REVIEW_FREEZE_2026-09-30.json").read_text(encoding="utf-8"))
    assert freeze["sample_sha256"] == SAMPLE_SHA256
    assert freeze["notes_sha256"] == _sha_lf(BENCH / "H_REVIEW_NOTES_2026-09-30.md")
    assert freeze["blindness_status"] == "DEVIATION_ORIGINAL_TARGET_GRADES_EXPOSED_TO_SUBAGENT"
    inputs = {i["path"]: i for i in receipt["inputs"]}
    for name in ("H_REVIEW_SAMPLE_T1.md", "H_REVIEW_FREEZE_2026-09-30.json", "H_REVIEW_NOTES_2026-09-30.md",
                 "H_REVIEW_RESULT_2026-09-30.json", "topic_set_v1.jsonl"):
        assert inputs[f"benchmarks/topic_v1/{name}"]["sha256"] == _sha_lf(BENCH / name), name
    assert inputs["benchmarks/topic_v1/H_REVIEW_SAMPLE_T1.md"]["role"] == "FROZEN_HISTORICAL_SAMPLE_ONLY_SOURCE_OF_H"


def test_handoff_copies_are_unchanged_bytes():
    mod = _load_script()
    for rel, digest in mod.HANDOFF_FILES.items():
        data = (IMPORT / "handoff_2026-10-05" / Path(rel).name).read_bytes()
        assert hashlib.sha256(data).hexdigest() == digest and b"\r" not in data, rel


# ---------------------------------------------------------------- the 60 labels
def test_sixty_rows_equal_the_frozen_table(labels):
    table = _frozen_table()
    assert len(table) == len(labels) == 60
    assert [r["sample_id"] for r in labels] == list(range(1, 61))
    for (n, tid, title, pid, desc, h), r in zip(table, labels):
        assert (r["sample_id"], r["topic_id"], r["topic_title_as_supplied"], r["page_id"],
                r["description_as_supplied"]) == (n, tid, title, pid, desc)
        assert type(r["H"]) is int and str(r["H"]) == h and 0 <= r["H"] <= 3
        assert r["label_key"] == f"{tid}|{pid}"
        assert r["lineage"]["h_value"] == {"path": "benchmarks/topic_v1/H_REVIEW_SAMPLE_T1.md",
                                           "sha256": SAMPLE_SHA256, "table_row": n,
                                           "role": "FROZEN_HISTORICAL_SAMPLE_ONLY_SOURCE_OF_H"}


def test_pairs_locators_and_distribution(labels):
    pairs = [(r["topic_id"], r["page_id"]) for r in labels]
    assert len(set(pairs)) == 60
    pages = Counter(r["page_id"] for r in labels)
    assert len(pages) == 59 and [p for p, c in pages.items() if c > 1] == ["VKM-SRC-169:p0001"]
    shared = [r for r in labels if r["page_id"] == "VKM-SRC-169:p0001"]
    assert [r["sample_id"] for r in shared] == [3, 55] and shared[0]["topic_id"] != shared[1]["topic_id"]
    r43 = labels[42]
    assert r43["page_id"] == "VKM-SRC-023:r0076" and r43["page_namespace"] == "RENDERED_DOCX"
    assert Counter(r["H"] for r in labels) == {0: 14, 1: 10, 2: 9, 3: 27}
    assert Counter(r["track"] for r in labels) == {"MODEL_FAMILY": 36, "PROCESS": 24}
    tracks = {t.topic_id: t.track for t in TB.load_set(BENCH / "topic_set_v1.jsonl")}
    assert all(tracks[r["topic_id"]] == r["track"] for r in labels)


def test_no_status_is_promoted(labels):
    for r in labels:
        assert r["new_independent_grade"] is None and r["grade_changed_by_import"] is False
        assert r["current_original_review_status"] == "NOT_RUN" and r["current_review_is_blind"] is False
        assert r["scientific_admission"] == "NOT_ESTABLISHED" and r["s26_extraction_gold"] == "NOT_QUALIFIED"
        assert r["review_domain"] == "RETRIEVAL_RELEVANCE"
        assert r["label_role"] == "HISTORICAL_RETRIEVAL_RELEVANCE_LABEL"
        assert r["historical_blindness_status"] == "DEVIATION_ORIGINAL_TARGET_GRADES_EXPOSED_TO_SUBAGENT"
        assert r["rationale_basis"] == "PARAPHRASE_OF_HISTORICAL_REVIEW_NOTES_NOT_CURRENT_ORIGINAL_REVIEW"
        att = r["rationale_attribution"]
        assert att["path"] == "benchmarks/topic_v1/H_REVIEW_NOTES_2026-09-30.md" and att["reverified_now"] is False
        assert att["sample_id"] == r["sample_id"] and r["rationale_ru"].strip()
    assert sum(r["historical_root_rechecked_reported"] for r in labels) == 31
    assert sum(r["historical_review_method_reported"] == "text+image" for r in labels) == 34


# ---------------------------------------------------------------- the future review queue
def test_queue_has_59_pages_covering_exactly_the_60_pairs(labels, queue):
    assert len(queue) == len({q["page_id"] for q in queue}) == 59
    assert [q["queue_position"] for q in queue] == list(range(1, 60))
    got = {(p["sample_id"], p["topic_id"], q["page_id"]) for q in queue for p in q["topic_pairs"]}
    assert got == {(r["sample_id"], r["topic_id"], r["page_id"]) for r in labels}
    assert sum(len(q["topic_pairs"]) for q in queue) == 60
    for q in queue:
        assert q["blind_packet"] is False and q["request_status"] == "NOT_RUN"
        assert q["queue_role"] == "FUTURE_SEPARATE_SOURCE_FIRST_REVIEW"
        assert all(set(p) == {"sample_id", "topic_id"} for p in q["topic_pairs"])     # no grades in the queue
        assert not {"H", "grade", "rationale_ru"} & set(q)


# ---------------------------------------------------------------- receipt, idempotency, checksums, hygiene
def test_receipt_records_gate_h0_without_overclaiming(receipt):
    assert receipt["schema"] == "vkm.h_review_repo_import_receipt/1" and receipt["gate"] == "H0"
    assert receipt["counts"]["grade_counts"] == {"0": 14, "1": 10, "2": 9, "3": 27}
    assert receipt["counts"]["track_counts"] == {"MODEL_FAMILY": 36, "PROCESS": 24}
    assert receipt["counts"]["unique_pages"] == 59 and receipt["counts"]["unique_topic_page_pairs"] == 60
    assert receipt["grades_changed"] == receipt["new_independent_labels"] == receipt["new_original_reviews"] == 0
    assert receipt["locators_changed"] == 0 and receipt["hscore_executed"] is False
    assert receipt["pooled_qrels_opened_by_import"] is False and receipt["historical_files_written"] is False
    assert receipt["s26_extraction_gold"] == "NOT_QUALIFIED" and receipt["scientific_admission"] == "NOT_ESTABLISHED"
    assert receipt["import_context"]["is_blind"] is False
    assert receipt["gate_h0"] == {"status": "PASS", "scope": "TECHNICAL_IMPORT_ONLY", "labels_preserved": "60/60",
                                  "lineage_imported": True, "new_original_checks": "NOT_RUN"}
    outputs = {o["path"].rsplit("/", 1)[1]: o["sha256"] for o in receipt["outputs"]}
    for name, digest in outputs.items():
        assert _sha(IMPORT / name) == digest, name


def test_rebuild_is_byte_identical_to_the_committed_outputs():
    mod = _load_script()
    first, second = mod.build_outputs(), mod.build_outputs()
    assert first == second
    assert set(first) == set(GENERATED)
    for name, data in first.items():
        assert (IMPORT / name).read_bytes() == data, f"{name} is stale: run the import script `build`"
    assert mod.diff_outputs(first) == []


def test_local_checksums_are_consistent_and_frozen_index_untouched():
    sums = dict(reversed(line.split("  ", 1)) for line in
                (IMPORT / "SHA256SUMS").read_text(encoding="utf-8").splitlines() if line.strip())
    files = {p.relative_to(IMPORT).as_posix() for p in IMPORT.rglob("*") if p.is_file()}
    assert set(sums) == files - {"SHA256SUMS", "README.md"}
    for name, digest in sums.items():
        assert _sha(IMPORT / name) == digest, name
    # the topic_v1 index stays the preregistration freeze (nightly verifies it on CORE with only these files)
    frozen = dict(reversed(line.split(maxsplit=1)) for line in
                  (BENCH / "SHA256SUMS").read_text(encoding="utf-8").splitlines() if line.strip())
    assert set(frozen) == {"topic_set_v1.jsonl", "topic_queries_v1.tsv", "metrics_spec_v1.json", "page_mapping_v1.json"}
    for name, digest in frozen.items():
        assert _sha_lf(BENCH / name) == digest, name


def test_verbatim_check_is_bound_to_the_current_texts(receipt):
    check = json.loads((IMPORT / "VERBATIM_CHECK.json").read_text(encoding="utf-8"))
    assert check["status"] == "PASS" and check["excluded_text"] == []
    assert check["page_texts"]["max_shingle_run_words"] < 25 and check["private_quotes"]["max_shingle_run_words"] < 25
    assert check["page_texts"]["samples"] == 60 and check["page_texts"]["pages"] == 59
    for f in check["checked_files"]:
        assert _sha(ROOT / f["path"]) == f["sha256"], f["path"]
    assert receipt["public_text_check"]["status"] == "PASS"
    assert receipt["public_text_check"]["sha256"] == _sha(IMPORT / "VERBATIM_CHECK.json")


def test_import_files_pass_the_leakage_guard():
    files = [p for p in IMPORT.rglob("*") if p.is_file()] + [SCRIPT, Path(__file__).resolve()]
    assert scan(ROOT, files=files) == []
    forbidden = TB.FORBIDDEN_KEYS | B.FORBIDDEN_KEYS
    for p in IMPORT.rglob("*.json*"):
        docs = _jsonl(p) if p.suffix == ".jsonl" else [json.loads(p.read_text(encoding="utf-8"))]
        assert not any(TB._keys(d) & forbidden for d in docs), p.name


# ---------------------------------------------------------------- the import refuses changed inputs
def _mutate_jsonl(data: bytes, line_no: int, change) -> bytes:
    lines = data.decode("utf-8").split("\n")
    row = json.loads(lines[line_no - 1])
    change(row)
    lines[line_no - 1] = json.dumps(row, ensure_ascii=False, sort_keys=True)
    return "\n".join(lines).encode("utf-8")


def _set(key, value):
    return lambda row: row.__setitem__(key, value)


@pytest.mark.parametrize("target, line_no, change, message", [
    ("H_REVIEW_T1_ANNOTATED.jsonl", 12, _set("H", 2), "H differs"),
    ("H_REVIEW_T1_ANNOTATED.jsonl", 12, _set("H", True), "H differs"),
    ("H_REVIEW_T1_ANNOTATED.jsonl", 5, _set("new_independent_grade", 3), "new_independent_grade"),
    ("H_REVIEW_T1_ANNOTATED.jsonl", 5, _set("current_review_is_blind", True), "current_review_is_blind"),
    ("H_REVIEW_T1_ANNOTATED.jsonl", 43, _set("page_id", "VKM-SRC-023:p0076"), "page_id differs"),
    ("SOURCE_FIRST_REVIEW_REQUESTS.jsonl", 1, _set("blind_packet", True), "blind_packet"),
])
def test_changed_handoff_rows_are_rejected(tmp_path, monkeypatch, target, line_no, change, message):
    mod = _load_script()
    bench = tmp_path / "benchmarks" / "topic_v1"
    handoff = bench / "h_review_import_2026-10-05" / "handoff_2026-10-05"
    handoff.mkdir(parents=True)
    for name in ("H_REVIEW_SAMPLE_T1.md", "H_REVIEW_FREEZE_2026-09-30.json", "H_REVIEW_NOTES_2026-09-30.md",
                 "H_REVIEW_RESULT_2026-09-30.json", "topic_set_v1.jsonl"):
        shutil.copy(BENCH / name, bench / name)
    pins = dict(mod.HANDOFF_FILES)
    for rel in pins:
        data = (IMPORT / "handoff_2026-10-05" / Path(rel).name).read_bytes()
        if rel.endswith(target):
            data = _mutate_jsonl(data, line_no, change)
            pins[rel] = hashlib.sha256(data).hexdigest()     # re-pinned: the field checks themselves must refuse
        (handoff / Path(rel).name).write_bytes(data)
    monkeypatch.setattr(mod, "BENCH", bench)
    monkeypatch.setattr(mod, "OUT_DIR", bench / "h_review_import_2026-10-05")
    monkeypatch.setattr(mod, "HANDOFF_DIR", handoff)
    monkeypatch.setattr(mod, "HANDOFF_FILES", pins)
    with pytest.raises(ValueError, match=message):
        mod.build_outputs()


def test_changed_frozen_sample_or_unpinned_copy_is_rejected(tmp_path, monkeypatch):
    mod = _load_script()
    bench = tmp_path / "benchmarks" / "topic_v1"
    handoff = bench / "h_review_import_2026-10-05" / "handoff_2026-10-05"
    handoff.mkdir(parents=True)
    for name in ("H_REVIEW_FREEZE_2026-09-30.json", "H_REVIEW_NOTES_2026-09-30.md",
                 "H_REVIEW_RESULT_2026-09-30.json", "topic_set_v1.jsonl"):
        shutil.copy(BENCH / name, bench / name)
    for rel in mod.HANDOFF_FILES:
        shutil.copy(IMPORT / "handoff_2026-10-05" / Path(rel).name, handoff / Path(rel).name)
    text = (BENCH / "H_REVIEW_SAMPLE_T1.md").read_text(encoding="utf-8")
    (bench / "H_REVIEW_SAMPLE_T1.md").write_bytes(text.replace("| VKM-SRC-023:r0076 |", "| VKM-SRC-023:p0076 |")
                                                  .encode("utf-8"))
    monkeypatch.setattr(mod, "BENCH", bench)
    monkeypatch.setattr(mod, "OUT_DIR", bench / "h_review_import_2026-10-05")
    monkeypatch.setattr(mod, "HANDOFF_DIR", handoff)
    with pytest.raises(ValueError, match="frozen sample"):
        mod.build_outputs()
    shutil.copy(BENCH / "H_REVIEW_SAMPLE_T1.md", bench / "H_REVIEW_SAMPLE_T1.md")
    (handoff / "H_REVIEW_T1_ANNOTATED.md").write_bytes(b"changed\n")
    with pytest.raises(ValueError, match="handoff copy changed"):
        mod.build_outputs()
