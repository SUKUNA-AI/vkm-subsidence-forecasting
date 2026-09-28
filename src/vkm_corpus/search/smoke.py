"""Russian search smoke for acceptance §59: properties, not specific documents (the corpus changes).

Q1 «оседание земной поверхности» (pages): hits, stable IDs, highlights, other word forms highlighted, stable order.
Q2 «ползучесть / ползучести каменной соли» (blocks, collapsed by page): identical rankings, one hit per page.
Q3 «закладка выработанного пространства» (pages): year / origin filters hold, a non-existent value gives 0 hits.
Q4 «маркшейдерские наблюдения» (blocks): source and source-scope filters hold; availability needs unknown_policy.
Q5 «рис. 3.1 мульда сдвижения» (figures): only figures; a figure labelled 3.1 in the top 5; visual candidates.
Q6 «расчётная схема» = «расчетная схема»; Q7 «сильвинит» = «сильвинита» ≠ «сильвин»; Q8 «creep» → «creeping».
Q9 figures+tables+formulas fused by rank (RRF); Q10 text rerank candidates are ID + passage references (≤ 24).
"""
from __future__ import annotations

import re
from typing import Any, Callable

from vkm_corpus.graph.common import SKIP, CheckResult, check
from vkm_corpus.search.query import (MAX_TEXT_CANDIDATES, RERANK_TEXT_RULE, SearchRequest, SearchRequestError,
                                     rerank_candidates, search)

_ID_RE = re.compile(r"^VKM-SRC-\d{3}:[prs]\d{4}(:[bftmc][0-9a-f]{12})?$")
_EM_RE = re.compile(r"<em>(.*?)</em>")


class _Runner:
    def __init__(self, client: Any, prefix: str, indices: dict[str, str] | None) -> None:
        self.client, self.prefix, self.indices = client, prefix, indices

    def __call__(self, query: str, kinds: tuple[str, ...] = ("PAGE",), **kw: Any):
        return search(self.client, SearchRequest(query=query, kinds=kinds, **kw), self.prefix, indices=self.indices)


def _ids(resp: Any) -> list[str]:
    return [h.id for h in resp.hits]


def _guard(results: list[CheckResult], check_id: str, title: str, fn: Callable[[], list[str]]) -> None:
    try:
        results.append(check(check_id, title, fn(), code="E_SMOKE"))
    except SearchRequestError as exc:
        results.append(check(check_id, title, [f"request error {exc.code}"], code="E_SMOKE"))


def run_smoke(client: Any, prefix: str, *, indices: dict[str, str] | None = None, t0: str = "2009-12-31",
              min_total: int = 1) -> list[CheckResult]:
    run = _Runner(client, prefix, indices)
    results: list[CheckResult] = []

    def q1() -> list[str]:
        bad = []
        first = run("оседание земной поверхности", size=20)
        if first.totals["PAGE"] < min_total:
            bad.append(f"total {first.totals['PAGE']} < {min_total}")
        bad += [w for w in first.warnings if w.startswith("ID_MISMATCH")]
        bad += [f"bad id {h.id}" for h in first.hits if not _ID_RE.match(h.id)]
        bad += [f"no highlight {h.id}" for h in first.hits if not h.highlights]
        forms = {m.lower() for h in first.hits for frag in h.highlights for m in _EM_RE.findall(frag)}
        if first.hits and not (forms - {"оседание", "земной", "поверхности"}):
            bad.append("no other word form highlighted (morphology not visible)")
        if _ids(run("оседание земной поверхности", size=20)) != _ids(first):
            bad.append("order changed on repeat")
        return bad

    def q2() -> list[str]:
        a = run("ползучесть каменной соли", kinds=("BLOCK",), size=20)
        b = run("ползучести каменной соли", kinds=("BLOCK",), size=20)
        bad = [] if _ids(a) == _ids(b) else ["rankings differ for two forms of one phrase"]
        pages = [h.page_id for h in a.hits]
        if len(pages) != len(set(pages)):
            bad.append("more than one hit per page after collapse")
        if a.hits and not all(h.best_blocks for h in a.hits):
            bad.append("collapsed hits without best_blocks")
        if not a.hits:
            bad.append("no hits")
        return bad

    def q3() -> list[str]:
        query = "закладка выработанного пространства"
        base = run(query, size=50)
        bad = [] if base.hits else ["no hits"]
        by_year = run(query, size=50, filters={"year": {"gte": 2000}})
        bad += [f"year filter broken on {h.id}" for h in by_year.hits if (h.fields.get("year") or 0) < 2000]
        by_origin = run(query, size=50, filters={"origin": ["NATIVE"]})
        bad += [f"origin filter broken on {h.id}" for h in by_origin.hits if h.fields.get("origin") != "NATIVE"]
        if by_year.totals["PAGE"] > base.totals["PAGE"] or by_origin.totals["PAGE"] > base.totals["PAGE"]:
            bad.append("filtered total exceeds unfiltered total")
        if run(query, size=5, filters={"source_id": ["VKM-SRC-000"]}).totals["PAGE"] != 0:
            bad.append("filter on a non-existent source returned hits")
        return bad

    def q4() -> list[str]:
        query = "маркшейдерские наблюдения"
        base = run(query, kinds=("BLOCK",), size=20)
        if not base.hits:
            return ["no hits"]
        bad = []
        sid = base.hits[0].source_id
        by_source = run(query, kinds=("BLOCK",), size=20, filters={"source_id": [sid]})
        bad += [f"source filter broken on {h.id}" for h in by_source.hits if h.source_id != sid]
        scopes = base.hits[0].fields.get("source_site_scope") or []
        if scopes:
            by_scope = run(query, kinds=("BLOCK",), size=20, filters={"source_scope": [scopes[0]]})
            bad += [f"source_scope filter broken on {h.id}" for h in by_scope.hits
                    if scopes[0] not in (h.fields.get("source_site_scope") or [])]
        try:
            run(query, kinds=("BLOCK",), filters={"available_until": t0})
            bad.append("available_until without unknown_policy was accepted")
        except SearchRequestError as exc:
            if exc.code != "E_UNKNOWN_POLICY_REQUIRED":
                bad.append(f"unexpected error {exc.code}")
        excl = run(query, kinds=("BLOCK",), size=50, filters={"available_until": t0, "unknown_policy": "EXCLUDE"})
        bad += [f"availability EXCLUDE broken on {h.id}" for h in excl.hits
                if not h.fields.get("available_latest_day") or h.fields["available_latest_day"] > t0]
        incl = run(query, kinds=("BLOCK",), size=50, filters={"available_until": t0, "unknown_policy": "INCLUDE"})
        bad += [f"availability INCLUDE broken on {h.id}" for h in incl.hits
                if h.fields.get("available_latest_day") and h.fields["available_latest_day"] > t0]
        if incl.totals["BLOCK"] < excl.totals["BLOCK"]:
            bad.append("INCLUDE returned fewer hits than EXCLUDE")
        if not base.availability_counts:
            bad.append("no availability counts by basis")
        return bad

    def q5() -> list[str]:
        resp = run("рис. 3.1 мульда сдвижения", kinds=("FIGURE",), size=20)
        bad = [f"non-figure {h.id}" for h in resp.hits if h.object_type != "FIGURE"]
        # every book has its own «рис. 3.1»: on a multi-source corpus a strongly matching figure with another label
        # may rank first; the check is that the label boost brings a figure labelled 3.1 into the top 5
        labelled = [h for h in resp.hits if h.fields.get("object_label") == "3.1"]
        if labelled and not any(h.fields.get("object_label") == "3.1" for h in resp.hits[:5]):
            bad.append("no figure labelled 3.1 in the top 5")
        visual = rerank_candidates(resp, mode="visual")
        bad += [f"visual candidate without image {c['candidate_id']}" for c in visual["candidates"]
                if not c.get("image_artifact_id")]
        return bad

    def q6() -> list[str]:
        a, b = run("расчётная схема", size=20), run("расчетная схема", size=20)
        return ([] if _ids(a) == _ids(b) else ["ё and е give different rankings"]) + ([] if a.hits else ["no hits"])

    def q7() -> list[str]:
        a, b = run("сильвинит", size=50), run("сильвинита", size=50)
        bad = [] if _ids(a) == _ids(b) else ["сильвинит and сильвинита give different rankings"]
        tokens = [t["token"] for t in client.indices.analyze(
            index=(indices or {}).get("pages") or f"{prefix}-pages",
            body={"analyzer": "vkm_text", "text": "сильвин сильвинит в сильвините"})["tokens"]]
        if tokens != ["сильвин", "сильвинит", "сильвинит"]:
            bad.append(f"сильвин/сильвинит analysed as {tokens}")
        return bad + ([] if a.hits else ["no hits"])

    def q8() -> list[str]:
        resp = run("creep", size=20)
        forms = {m.lower() for h in resp.hits for frag in h.highlights for m in _EM_RE.findall(frag)}
        if not resp.hits:
            return ["no hits"]
        return [] if forms else ["no highlighted form"]

    def q9() -> list[str]:
        kinds = ("FIGURE", "TABLE", "FORMULA")
        a = run("ползучести", kinds=kinds, size=20)
        b = run("ползучести", kinds=kinds, size=20)
        bad = [] if a.fusion == "RRF" else ["no rank fusion across kinds"]
        bad += [] if _ids(a) == _ids(b) else ["fused order changed on repeat"]
        bad += [f"rank gap at {h.id}" for i, h in enumerate(a.hits, 1) if h.rank != i]
        return bad

    def q10() -> list[str]:
        resp = run("ползучесть каменной соли", kinds=("BLOCK",), size=40)
        cand = rerank_candidates(resp, mode="text")
        bad = [] if len(cand["candidates"]) <= MAX_TEXT_CANDIDATES else ["more than 24 text candidates"]
        bad += [f"bad passage {c['candidate_id']}" for c in cand["candidates"]
                if c["passage"]["rule"] != RERANK_TEXT_RULE or not c["passage"]["object_ids"]]
        bad += [f"text in a candidate {c['candidate_id']}" for c in cand["candidates"] if "text" in c]
        return bad

    for cid, title, fn in (("Q1", "«оседание земной поверхности»: hits, stable IDs, highlights, morphology, order", q1),
                           ("Q2", "«ползучесть/ползучести каменной соли»: same ranking; one hit per page", q2),
                           ("Q3", "«закладка выработанного пространства»: filters hold", q3),
                           ("Q4", "«маркшейдерские наблюдения»: source, scope, availability filters", q4),
                           ("Q5", "«рис. 3.1 мульда сдвижения»: figures, label boost, visual candidates", q5),
                           ("Q6", "«расчётная» = «расчетная»", q6),
                           ("Q7", "«сильвинит» = «сильвинита» ≠ «сильвин»", q7),
                           ("Q8", "«creep» finds and highlights English forms", q8),
                           ("Q9", "figures+tables+formulas fused by rank (RRF), deterministic", q9),
                           ("Q10", "text rerank candidates: ≤ 24 ID + passage references, no text", q10)):
        _guard(results, cid, title, fn)
    return results


def skipped(reason: str) -> list[CheckResult]:
    return [CheckResult("Q*", "Russian search smoke", SKIP, details={"reason": reason})]
