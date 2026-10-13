"""Synthetic search answers of production shape for the size tests of the brief view (``api.brief``).

Twenty hits over the objects of the API's synthetic canon (pages, a figure, a table, a formula — repeated: the size
of an answer does not depend on distinct ids), each with three index highlights of ~180 characters (E's fragment size)
and the stage trace a CORE hybrid search writes (BM25, dense, RRF, translation legs, late interaction with its unit,
dense unit); the run record carries the stage records of a late + graph + translation run. Text is synthetic.
"""
from __future__ import annotations

from typing import Any

from vkm_corpus.api.fixtures import FakeHybrid, FakeSearch, SyntheticCanon

N_HITS = 20
_WORDS = ("синтетический фрагмент текста страницы о деформации массива над выработками и наблюдениях за смещением "
          "реперов профильной линии в годы отработки панелей с закладкой выработанного пространства").split()


def _highlight(i: int, j: int) -> str:
    words = [_WORDS[(i * 7 + j * 3 + k) % len(_WORDS)] for k in range(24)]
    words[5] = f"<em>{words[5]}</em>"
    words[11] = f"<em>{words[11]}</em>"
    return " ".join(words)[:180]


def _pool(canon: SyntheticCanon) -> list[tuple[str, str, str, int]]:
    """(id, object type, source, page index) of the synthetic canon's searchable objects."""
    return [("VKM-SRC-001:p0001", "PAGE", "VKM-SRC-001", 1), ("VKM-SRC-001:p0002", "PAGE", "VKM-SRC-001", 2),
            ("VKM-SRC-002:p0001", "PAGE", "VKM-SRC-002", 1), (canon.ids["figure"], "FIGURE", "VKM-SRC-001", 2),
            ("VKM-SRC-001:p0003", "PAGE", "VKM-SRC-001", 3), (canon.ids["table"], "TABLE", "VKM-SRC-001", 2),
            (canon.ids["formula"], "FORMULA", "VKM-SRC-001", 3)]


def hybrid_hits(canon: SyntheticCanon, n: int = N_HITS) -> list[dict[str, Any]]:
    pool = _pool(canon)
    hits = []
    for i in range(n):
        oid, otype, sid, index = pool[i % len(pool)]
        trace = {"bm25_rank": i + 1, "bm25_score": round(14.2 - 0.37 * i, 4), "dense_rank": i + 2,
                 "dense_score": round(0.82 - 0.004 * i, 6), "fused_rank": i + 1,
                 "rrf_score": round(0.0328 - 0.0004 * i, 8), "rrf_k": 60, "late_rank": i + 1, "rerank_rank": None,
                 "expansion_ranks": {"bm25~x1": i + 3, "dense~x1": i + 1},
                 "late_score": round(21.53 - 0.31 * i, 6), "late_status": "SCORED", "final_rank": i + 1,
                 "late_unit": {"unit_id": f"u1-{i:016x}", "units": 7, "tokens": 412},
                 "dense_unit": {"unit_id": f"u1-{i + 100:016x}", "unit_kind": "BLOCK_GROUP",
                                "object_ids": [canon.ids["block"], canon.ids["block2"]]}}
        hits.append({"id": oid, "object_type": otype, "index": "vkm-pages-m1-b-test", "build_id": "b-test",
                     "score": trace["late_score"], "source_id": sid, "page_id": f"{sid}:p{index:04d}",
                     "page_index": index, "highlights": [_highlight(i, j) for j in range(3)],
                     "best_blocks": [{"id": canon.ids["block"], "reading_order": 1}] if otype == "PAGE" else [],
                     "duplicates": ["VKM-SRC-002:p0001"] if oid == "VKM-SRC-001:p0001" else [], "trace": trace})
    return hits


def search_hits(canon: SyntheticCanon, n: int = N_HITS) -> list[dict[str, Any]]:
    pool = _pool(canon)
    return [{"id": oid, "object_type": otype, "index": "vkm-pages-m1-b-test", "build_id": "b-test",
             "score": round(14.2 - 0.37 * i, 4), "source_id": sid, "page_id": f"{sid}:p{index:04d}",
             "page_index": index, "highlights": [_highlight(i, j) for j in range(3)],
             "best_blocks": [{"id": canon.ids["block"], "reading_order": 1}] if otype == "PAGE" else []}
            for i, (oid, otype, sid, index) in ((i, pool[i % len(pool)]) for i in range(n))]


class RichHybrid(FakeHybrid):
    """FakeHybrid whose run record has the stage records of a CORE run (late, graph, routes, expansion)."""

    def search(self, request: dict[str, Any]) -> dict[str, Any]:
        out = super().search(request)
        out.update({"late": True, "late_candidates": 100, "route": "default"})
        out["stages"].update({
            "bm25": {"engine": "opensearch", "indices": "per-kind aliases of E"},
            "late": {"engine": "rx580-retrieval", "model_key": "mlateon-test", "query_signature": "q" * 16,
                     "query_tokens": 9, "store": {"snapshot_id": self.built_from, "build_id": "tok-test"},
                     "candidates": 100, "scored": 98, "unscored": 2, "unscored_ids": ["VKM-SRC-001:p0009"],
                     "ordering": "per kind by MaxSim inside the kind's RRF positions (H-44); unscored after scored",
                     "page_score": "max MaxSim over the units of the page except BIB_ENTRY (CP-42)",
                     "page_exclude_kinds": ["BIB_ENTRY"], "timings_ms": {"total": 410.2, "load": 12.5}},
            "bib_route": {"status": "NOT_APPLIED", "cues": [], "weak_cues": []},
            "visual_route": {"status": "DISABLED", "cues": []},
            "expansion": {"texts": ["synthetic surface subsidence"], "legs": ["bm25:PAGE~x1", "dense:PAGE~x1"],
                          "fusion": "extra RRF legs of the expansions; the late stage scores the original query"},
            "graph": {"requested": ["collapse"],
                      "status": {"collapse": "APPLIED", "cohesion": "OFF", "concepts": "OFF", "cites": "OFF",
                                 "topics": "OFF"},
                      "fusion": {"collapse": "last, over every kind"}, "review_status": "AUTO_EXTRACTED_UNREVIEWED"},
            "rerank": "NOT_RUN here (rerank_text over rerank_candidate through its configured backend)"})
        out["timings_ms"] = {"vectors_meta": 1.2, "embed": 35.1, "bm25_page": 48.3, "dense_page": 22.4,
                             "late": 410.2, "graph_collapse": 3.1, "total": 640.7}
        return out


def rich_hybrid(canon: SyntheticCanon, n: int = N_HITS) -> RichHybrid:
    return RichHybrid(hybrid_hits(canon, n))


def rich_search(canon: SyntheticCanon, n: int = N_HITS) -> FakeSearch:
    return FakeSearch(search_hits(canon, n))
