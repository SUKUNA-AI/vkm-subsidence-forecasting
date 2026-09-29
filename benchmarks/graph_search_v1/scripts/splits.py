"""GRAPH_SEARCH_V1 splits (PREREGISTRATION §5): writes ``benchmarks/graph_search_v1/splits.json`` deterministically.

* retrieval_v1 — the retrieval_v0 assignment: ``train`` and ``dev`` → ``dev``, ``test`` → ``test``; the three queries
  V1 could not score (no VERIFIED label on a page of the final snapshot) are left out.
* topic_v1 — per track, topics ordered by sha256(``graph_search_v1|<topic_id>``); the first third (floor) → ``dev``,
  the rest → ``test``.

Run from the repository root: ``python benchmarks/graph_search_v1/scripts/splits.py``.
"""
from __future__ import annotations

import hashlib
import json
from collections import Counter
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
SALT = "graph_search_v1"
V1_UNCOVERED = ("T-MATH-005", "V-SMAP-001", "V-MSEIS-001")      # RESULTS_V1 §1: not scored by V1


def main() -> None:
    v0 = json.loads((REPO / "benchmarks/retrieval_v0/splits.json").read_text(encoding="utf-8"))["assignments"]
    tracks = {}
    for line in (REPO / "benchmarks/retrieval_v0/queries.jsonl").read_text(encoding="utf-8").splitlines():
        if line.strip():
            q = json.loads(line)
            tracks[q["query_id"]] = q["track"]
    v1 = {q: ("test" if s == "test" else "dev") for q, s in sorted(v0.items()) if q not in V1_UNCOVERED}
    topics = [json.loads(line) for line in
              (REPO / "benchmarks/topic_v1/topic_set_v1.jsonl").read_text(encoding="utf-8").splitlines() if line.strip()]
    topic: dict[str, str] = {}
    for track in ("PROCESS", "MODEL_FAMILY"):
        ids = sorted((t["topic_id"] for t in topics if t["track"] == track),
                     key=lambda x: hashlib.sha256(f"{SALT}|{x}".encode("utf-8")).hexdigest())
        for i, tid in enumerate(ids):
            topic[tid] = "dev" if i < len(ids) // 3 else "test"
    out = {"benchmark": "GRAPH_SEARCH_V1", "salt": SALT,
           "rules": {"retrieval_v1": "retrieval_v0 splits: train, dev -> dev; test -> test; V1-uncovered left out",
                     "topic_v1": "per track, topics by sha256(salt|topic_id); first floor(n/3) -> dev, rest -> test"},
           "counts": {"retrieval_v1": {f"{tracks[q]}:{s}": n for (q, s), n in sorted(
               Counter((q, s) for q, s in v1.items()).items())},
               "topic_v1": dict(sorted(Counter(topic.values()).items()))},
           "retrieval_v1": v1, "topic_v1": dict(sorted(topic.items()))}
    counts = Counter(f"{tracks[q]}:{s}" for q, s in v1.items())
    out["counts"]["retrieval_v1"] = dict(sorted(counts.items()))
    path = REPO / "benchmarks/graph_search_v1/splits.json"
    path.write_text(json.dumps(out, ensure_ascii=False, indent=1) + "\n", encoding="utf-8", newline="\n")
    print(json.dumps(out["counts"], ensure_ascii=False))


if __name__ == "__main__":
    main()
