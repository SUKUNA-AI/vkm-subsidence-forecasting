"""TOPIC_BENCHMARK_V1 — delta pool T3 (protocol: ``benchmarks/topic_v1/POOL_LABELING_T3.md``).

T1 (``LLM_AGENT_T1``) labelled the first 10 pages of the pool systems on ``snap-20260928T160616Z-5d669f09``, T2
(``LLM_AGENT_T2``) the delta on ``snap-20260929T175107Z-574daaac``. Since 07.10.2026 CORE serves the OCR v2 snapshot
``SNAPSHOT``: the PADDLEOCR_VL text layer (7 057 pages of 76 sources) changes their page texts and with them the
lexical and dense rankings. T3 labels the topic × page pairs that the systems bring now and that carry no T1 or T2 judgment.

Everything else is the T2 code (``pool_t2.py``) with the T3 constants: the same systems (re-run ``bm25``,
``hybrid_late``, ``hybrid_nolate``, ``nav`` + experiment variant ``human3``), depth 10, unit, scale, reason codes,
duplicate groups, blind packets, ingest checks and cross-check. A pair is "judged" when its page or a duplicate alias
carries a T1 or T2 label of the topic (T2 ``t1_alias_links`` count as aliases of their T1 unit). Label source
``LLM_AGENT_T3``; the T1 and T2 files are never written.

Environment (outside git): as pool_t2.py, with ``T3_RUNS`` / ``T3_EXPERIMENTS`` (default: the re-run and the experiment
of 07.10 under ``work/``) and ``CANON_DB`` = canonical DuckDB of ``SNAPSHOT``.

usage: PYTHONPATH=src python benchmarks/topic_v1/scripts/pool_t3.py {pool,packets,status,ingest,xscore} …
"""
from __future__ import annotations

import importlib.util
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import pool_t1 as PT  # noqa: E402  (also puts <repo>/src on sys.path)
import score_t1 as ST  # noqa: E402

from vkm_corpus.retrieval_lab import bench as B  # noqa: E402
from vkm_corpus.retrieval_lab import topic_bench as TB  # noqa: E402


def _private_t2():
    """A private copy of pool_t2: the T3 constants below never reach ``pool_t2`` / ``score_t2`` users."""
    spec = importlib.util.spec_from_file_location("topic_v1_pool_t2_for_t3",
                                                  Path(__file__).resolve().parent / "pool_t2.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


P2 = _private_t2()
REPO, BENCH = PT.REPO, PT.BENCH
T2_TSV, T2_GROUPS = P2.OUT_TSV, P2.OUT_GROUPS                     # read only
SNAPSHOT = "snap-20261007T103222Z-2d71e9e8"                        # the OCR v2 snapshot with VKM-SRC-053 / -230
# sources recommitted by the OCR v2 campaign (92 published 06.10 + 053 and 230 on 07.10); 76 of them have pages
# whose primary text layer is PADDLEOCR_VL (7 057 pages in snapshot 2d71e9e8)
OCR_V2_SOURCES = frozenset(f"VKM-SRC-{n:03d}" for n in (
    3, 11, 12, 14, 18, 20, 25, 28, 29, 37, 42, 43, 44, 45, 46, 47, 48, 49, 50, 51, 52, 53, 54, 59, 60, 61, 62, 63, 64,
    69, 82, 89, 97, 98, 99, 103, 117, 123, 124, 140, 141, 142, 144, 146, 148, 149, 150, 151, 177, 185, 190, 192, 193,
    198, 201, 202, 203, 204, 205, 207, 208, 209, 210, 212, 213, 216, 218, 219, 220, 221, 223, 226, 227, 228, 229, 230,
    231, 232, 233, 234, 235, 237, 238, 240, 243, 244, 245, 246, 248, 250, 251, 256, 266, 267))
DEFAULT_RUNS = REPO / "work" / "topic_v1_rerun_20261007" / "runs_v1_20261007.jsonl"
DEFAULT_EXPERIMENTS = REPO / "work" / "topic_v1_experiments_20261007" / "experiments_v1.jsonl"

# the T2 code with the T3 constants (module globals are read at call time)
P2.LABEL_SOURCE = "LLM_AGENT_T3"
P2.OUT_TSV = BENCH / "qrels_topic_v1_pooled_t3.tsv"
P2.OUT_GROUPS = BENCH / "qrels_topic_v1_pooled_t3_groups.json"
P2.SNAPSHOT = SNAPSHOT
P2.NEW_SOURCES = OCR_V2_SOURCES          # "new" in the T2 summaries = a source recommitted by OCR v2
P2.CID_PREFIX = "e"                      # T3 unit ids e001…; T1 c001…, T2 d001…
P2.POOL_FILE = "pool_t3.json"
P2.XCHECK_NAME = "xc-002"
P2.XCHECK_SEED = 20261007
P2.CONTROL_RUNS_FILE = "score_20261007.json"


def run_paths() -> tuple[Path, Path]:
    return (Path(os.environ.get("T3_RUNS") or DEFAULT_RUNS).expanduser(),
            Path(os.environ.get("T3_EXPERIMENTS") or DEFAULT_EXPERIMENTS).expanduser())


def t2_labels() -> tuple[list[dict[str, str]], list[dict]]:
    """T2 rows and the groups T2 adds: its own unit groups and its ``t1_alias_links`` as aliases of the T1 pages."""
    rows = B.load_pooled_qrels(T2_TSV)
    doc = json.loads(T2_GROUPS.read_text(encoding="utf-8"))
    links = [{"topic_id": x["topic_id"], "pages": list(x["t1_pages"]), "aliases": [x["page"]]}
             for x in doc["t1_alias_links"]]
    return rows, doc["groups"] + links


def judged_t1_t2(topics: list[TB.Topic], rows: list[dict[str, str]], groups: list[dict]
                 ) -> dict[str, frozenset[str]]:
    """Pages that carry a T1 or T2 judgment for each topic (``score_t1.judged_pages`` over T1 ∪ T2 rows with the
    aliases of their units), without the catalogue target pages (the V1 rule of ``collect_pool``)."""
    rows2, groups2 = t2_labels()
    p = ST.judged_pages(topics, rows + rows2, groups + groups2)
    v = ST.judged_pages(topics, None, groups)
    return {tid: p[tid] - v[tid] for tid in p}


P2.run_paths = run_paths
P2.t1_judged = judged_t1_t2              # pool() and ingest() exclude / refuse pairs judged in T1 or T2


def main() -> None:
    if SNAPSHOT == "snap-PENDING":
        raise SystemExit("SNAPSHOT is not set: the T3 snapshot id is fixed in POOL_LABELING_T3.md before the pool")
    if len(sys.argv) > 1 and sys.argv[1] == "ingest":
        protected = {p.resolve() for p in (PT.OUT_TSV, PT.OUT_GROUPS, T2_TSV, T2_GROUPS)}
        if {P2.OUT_TSV.resolve(), P2.OUT_GROUPS.resolve()} & protected:
            raise SystemExit("the T1 and T2 files are never written")
    P2.main()


if __name__ == "__main__":
    main()
