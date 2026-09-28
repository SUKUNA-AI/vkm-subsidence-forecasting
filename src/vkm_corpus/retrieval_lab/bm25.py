"""BM25 over units: a local implementation (Lucene-like BM25, analyzer mirroring ``vkm_text``) and an adapter for
experimental OpenSearch indices with agent E's analysis settings (prefix ``vkm-exp-``, CP-26).

Production indices of agent E are never touched: the adapter creates ``vkm-exp-<run>-units``, loads the units, runs
queries and deletes the index (``cleanup``). Query shape follows E's templates (best-fields multi_match on the stemmed
field + phrase boost on ``.exact``), so the lab measures the production analyzers.
"""
from __future__ import annotations

import math
from collections import Counter
from dataclasses import dataclass, field
from typing import Any, Iterable, Sequence

from vkm_corpus.retrieval_lab.textproc import analyze

EXP_PREFIX = "vkm-exp-"
K1 = 1.2
B = 0.75


@dataclass
class LocalBM25:
    """Okapi BM25 with Lucene's IDF ``ln(1 + (N − n + 0.5)/(n + 0.5))`` and length normalisation by avg doc length."""
    ids: list[str]
    k1: float = K1
    b: float = B
    doc_len: list[int] = field(default_factory=list)
    postings: dict[str, list[tuple[int, int]]] = field(default_factory=dict)
    avgdl: float = 0.0

    @classmethod
    def build(cls, ids: Sequence[str], texts: Sequence[str], *, k1: float = K1, b: float = B) -> "LocalBM25":
        idx = cls(list(ids), k1, b)
        for i, text in enumerate(texts):
            tf = Counter(analyze(text))
            idx.doc_len.append(sum(tf.values()))
            for term, n in tf.items():
                idx.postings.setdefault(term, []).append((i, n))
        idx.avgdl = (sum(idx.doc_len) / len(idx.doc_len)) if idx.doc_len else 0.0
        return idx

    def idf(self, term: str) -> float:
        n = len(self.postings.get(term, ()))
        return math.log(1.0 + (len(self.ids) - n + 0.5) / (n + 0.5))

    def search(self, query: str, k: int = 100) -> list[tuple[str, float]]:
        acc: dict[int, float] = {}
        for term in set(analyze(query)):
            plist = self.postings.get(term)
            if not plist:
                continue
            w = self.idf(term)
            for i, tf in plist:
                norm = self.k1 * (1 - self.b + self.b * self.doc_len[i] / self.avgdl) if self.avgdl else self.k1
                acc[i] = acc.get(i, 0.0) + w * tf * (self.k1 + 1) / (tf + norm)
        ranked = sorted(acc.items(), key=lambda x: (-x[1], self.ids[x[0]]))[:k]
        return [(self.ids[i], s) for i, s in ranked]


# ---------------------------------------------------------------- OpenSearch experimental index
def exp_index_name(run_tag: str) -> str:
    safe = "".join(c if c.isalnum() or c == "-" else "-" for c in run_tag.lower()).strip("-")
    return f"{EXP_PREFIX}{safe}-units"


def exp_index_body(run_tag: str) -> dict[str, Any]:
    """Unit index with E's analysis settings (``vkm-analysis/1``) and a minimal strict mapping."""
    from vkm_corpus.search.analysis import ANALYSIS_VERSION
    from vkm_corpus.search.mappings import index_settings

    text = {"type": "text", "analyzer": "vkm_text", "fields": {"exact": {"type": "text", "analyzer": "vkm_exact"}}}
    return {"settings": index_settings(building=True),
            "mappings": {"dynamic": "strict",
                         "_meta": {"owner": "retrieval_lab", "run_tag": run_tag, "analysis_version": ANALYSIS_VERSION},
                         "properties": {"unit_id": {"type": "keyword"}, "kind": {"type": "keyword"},
                                        "source_id": {"type": "keyword"}, "page_id": {"type": "keyword"},
                                        "text": text, "title": text}}}


def query_body(query: str, k: int, *, fields: Sequence[str] = ("text^1.0",), title_boost: float = 0.3,
               minimum_should_match: str | None = None) -> dict[str, Any]:
    """E-style query: best-fields match on the stemmed field, phrase boost on ``.exact``, weak title boost."""
    must: dict[str, Any] = {"multi_match": {"query": query, "type": "best_fields", "fields": list(fields),
                                            "tie_breaker": 0.2}}
    if minimum_should_match:
        must["multi_match"]["minimum_should_match"] = minimum_should_match
    return {"size": k, "_source": ["unit_id"], "track_total_hits": False,
            "query": {"bool": {"must": [must],
                               "should": [{"match_phrase": {"text.exact": {"query": query, "slop": 3, "boost": 2.0}}},
                                          {"match": {"title": {"query": query, "boost": title_boost}}}]}},
            "sort": [{"_score": "desc"}, {"unit_id": "asc"}]}


@dataclass
class OpenSearchBM25:
    client: Any
    index: str

    @classmethod
    def create(cls, client: Any, run_tag: str, docs: Iterable[dict[str, Any]], *, chunk: int = 500) -> "OpenSearchBM25":
        name = exp_index_name(run_tag)
        if not name.startswith(EXP_PREFIX):
            raise ValueError("experimental indices must use the vkm-exp- prefix")
        if client.indices.exists(index=name):
            client.indices.delete(index=name)
        client.indices.create(index=name, body=exp_index_body(run_tag))
        batch: list[dict[str, Any]] = []
        for doc in docs:
            batch += [{"create": {"_index": name, "_id": doc["unit_id"]}}, doc]
            if len(batch) >= 2 * chunk:
                cls._bulk(client, batch)
                batch = []
        if batch:
            cls._bulk(client, batch)
        client.indices.put_settings(index=name, body={"index": {"refresh_interval": "1s"}})
        client.indices.refresh(index=name)
        return cls(client, name)

    @staticmethod
    def _bulk(client: Any, lines: list[dict[str, Any]]) -> None:
        resp = client.bulk(body=lines)
        if resp.get("errors"):
            bad = [i for i in resp.get("items", []) if "error" in next(iter(i.values()))][:3]
            raise RuntimeError(f"bulk errors in experimental index: {bad}")

    def search(self, query: str, k: int = 100, **kw: Any) -> list[tuple[str, float]]:
        resp = self.client.search(index=self.index, body=query_body(query, k, **kw))
        return [(h["_source"]["unit_id"], float(h["_score"] or 0.0)) for h in resp["hits"]["hits"]]

    def cleanup(self) -> None:
        if self.index.startswith(EXP_PREFIX) and self.client.indices.exists(index=self.index):
            self.client.indices.delete(index=self.index)
