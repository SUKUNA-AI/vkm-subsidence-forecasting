"""Failure analysis of TOPIC_BENCHMARK_V1: why the evidence pages were missed (pre-registered causes), worst
queries, and exploratory (post-hoc, not pre-registered) gains of simple remedies. IDs and counts only.

usage (WSL, canonical DuckDB copy of the snapshot, read only; PRIVATE clone for page-level evidence methods):
    VKM_RESOURCES_ROOT=<PRIVATE clone> PYTHONPATH=src python benchmarks/topic_v1/scripts/failure_analysis.py \
        --runs <runs.jsonl> --canon-db <db>

Deviation from the pre-registration (reported, both counts kept): the pre-registered NO_TEXT rule also used the
flags VECTOR_NO_TEXT / IMAGE_NO_TEXT. They mark a page whose *file text layer* is missing (the page is then
OCR-ed, ``extract/classify.py``), not a page without text, so the main table counts NO_TEXT by the actual text
(char_count < 100, status PARTIAL / OCR_REQUIRED, EMPTY_PAGE); ``by_cause_preregistered_rule`` keeps the literal rule.
Evidence methods of model-registry targets are taken per page (the registry vn_ids located on that page, PRIVATE
``quote_check_v2.csv``: methods only), falling back to the model-level list of the set.
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path

import duckdb

from vkm_corpus.retrieval_lab import topic_bench as TB

sys.path.insert(0, str(Path(__file__).resolve().parent))
from score import load_runs, ranking_of  # noqa: E402

BENCH = Path(__file__).resolve().parents[1]
ROOT = BENCH.parents[1]
K = 50
NO_TEXT_FLAGS_PREREG = {"EMPTY_PAGE", "IMAGE_NO_TEXT", "VECTOR_NO_TEXT"}
NO_TEXT_FLAGS = {"EMPTY_PAGE"}
BAD_TEXT_FLAGS = {"GARBAGE_GLYPHS", "BROKEN_TEXT_LAYER", "LOW_FUNCTION_WORD_RATE", "MOJIBAKE_NOT_REPAIRABLE",
                  "TRUNCATED", "FONT_WITHOUT_TOUNICODE", "UNMAPPED_SYMBOL_GLYPHS", "PUA_GLYPHS"}
VISUAL_METHODS = {"VISUAL_READ", "GRAPH_DIGITIZED_APPROX"}
CAUSES = ("NO_TEXT", "VISUAL_EVIDENCE", "LANGUAGE", "OCR_QUALITY", "TERMINOLOGY", "SECTION_MISSING", "DILUTION",
          "OTHER")


def stems(text: str) -> set[str]:
    words = re.findall(r"[a-zа-я]+", (text or "").lower().replace("ё", "е"))
    return {w[:5] for w in words if len(w) >= 5}


def cyr_share(text: str) -> float:
    letters = re.findall(r"[A-Za-zА-Яа-яЁё]", text or "")
    return sum(1 for c in letters if re.match(r"[А-Яа-яЁё]", c)) / len(letters) if letters else 0.0


def rrf(rankings: list[list[str]], k: int = TB.RRF_K, top: int = K) -> list[str]:
    score: dict[str, float] = defaultdict(float)
    first: dict[str, int] = {}
    n = 0
    for r in rankings:
        for i, p in enumerate(r, 1):
            score[p] += 1.0 / (k + i)
            if p not in first:
                first[p] = n
                n += 1
    return sorted(score, key=lambda p: (-score[p], first[p]))[:top]


def recall_of(pages: list[str], topic: TB.Topic, aliases: dict[str, frozenset[str]] | None = None) -> float:
    got: set[str] = set()
    for p in pages:
        got |= (aliases or {}).get(p, frozenset((p,)))
    return sum(1 for t in topic.targets if t.pages & got) / len(topic.targets)


def mean(xs) -> float | None:
    xs = list(xs)
    return round(sum(xs) / len(xs), 4) if xs else None


def page_methods(raw_targets: dict, topics: list[TB.Topic]) -> dict[str, set[str]]:
    """Evidence extraction methods per target: page-level for processes (the set), per page for registry targets
    when the PRIVATE quote check is available, else the model-level list of the set."""
    out = {tid: {k.split(":", 1)[1] for k in x.get("kinds", []) if ":" in k} for tid, x in raw_targets.items()}
    priv = os.environ.get("VKM_RESOURCES_ROOT")
    qc = Path(priv) / "11_evidence_vnext" / "merged" / "quote_check_v2.csv" if priv else None
    if not qc or not qc.exists():
        return out
    vn = {r["vn_id"]: (r["source_id"], int(r["pdf_page"]), r["extraction_method"])
          for r in csv.DictReader(open(qc, encoding="utf-8"))}
    model_vns = {m["model_id"]: [v.strip() for v in m["vn_ids"].split(";") if v.strip()] for m in
                 csv.DictReader(open(ROOT / "catalogues" / "mathematics" / "MATHEMATICAL_MODEL_REGISTRY.csv",
                                     encoding="utf-8"))}
    for topic in topics:
        if topic.track != "MODEL_FAMILY":
            continue
        for t in topic.targets:
            idxs = {TB.split_page_id(p)[2] for p in t.pages if TB.split_page_id(p)[0] == t.source_id}
            found = {vn[v][2] for m in raw_targets[t.target_id].get("basis", []) for v in model_vns.get(m, [])
                     if v in vn and vn[v][0] == t.source_id and vn[v][1] in idxs}
            if found:
                out[t.target_id] = found
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--runs", action="append", required=True)
    ap.add_argument("--canon-db", required=True)
    ap.add_argument("--out", default=str(BENCH / "failure_analysis_v1.json"))
    args = ap.parse_args()
    topics = TB.load_set(BENCH / "topic_set_v1.jsonl")
    raw_targets = {x["target_id"]: x for line in (BENCH / "topic_set_v1.jsonl").read_text(encoding="utf-8").splitlines()
                   if line.strip() for x in json.loads(line)["targets"]}
    methods = page_methods(raw_targets, topics)
    runs, outlines, _meta, _files = load_runs(args.runs)
    index = TB.SectionIndex.from_outlines(outlines)
    systems = [s for s in TB.SYSTEMS if any(k[1] == s for k in runs)]
    rk: dict[tuple[str, str], TB.Ranking] = {k: ranking_of(v, index) for k, v in runs.items()}

    # ------------------------------------------------------------ page facts of every target page (primary)
    con = duckdb.connect(args.canon_db, read_only=True)
    pages = sorted({t.page_id for tp in topics for t in tp.targets})
    facts: dict[str, dict] = {}
    for pid, chars, status, flags, text in con.execute(
            "SELECT page_id, char_count, page_status, quality_flags, normalized_text FROM canonical.pages "
            "WHERE page_id IN (SELECT unnest(?::VARCHAR[]))", [pages]).fetchall():
        facts[pid] = {"chars": chars or 0, "status": status, "flags": set(flags or []), "cyr": cyr_share(text),
                      "stems": stems(text)}
    objs = Counter()
    for table in ("figures", "tables"):
        for pid, n in con.execute(f"SELECT page_id, count(*) FROM canonical.{table} WHERE page_id IN "
                                  "(SELECT unnest(?::VARCHAR[])) GROUP BY 1", [pages]).fetchall():
            objs[pid] += n

    def cause(topic: TB.Topic, t: TB.Target, system: str, rule: str = "corrected") -> str:
        f = facts.get(t.page_id, {"chars": 0, "status": None, "flags": set(), "cyr": 0.0, "stems": set()})
        no_text_flags = NO_TEXT_FLAGS_PREREG if rule == "preregistered" else NO_TEXT_FLAGS
        if f["chars"] < 100 or f["status"] in ("PARTIAL", "OCR_REQUIRED") or f["flags"] & no_text_flags:
            return "NO_TEXT"
        if methods.get(t.target_id, set()) & VISUAL_METHODS or (objs[t.page_id] and f["chars"] < 800):
            return "VISUAL_EVIDENCE"
        if f["cyr"] < 0.5:
            return "LANGUAGE"
        if f["flags"] & BAD_TEXT_FLAGS:
            return "OCR_QUALITY"
        q = set().union(*(stems(x.text) for x in topic.queries))
        if q and len(q & f["stems"]) / len(q) < 0.25:
            return "TERMINOLOGY"
        if system == "nav" and not index.deepest(t.page_id):
            return "SECTION_MISSING"
        for x in topic.queries:
            r = rk.get((x.query_id, system))
            if r and any(TB.split_page_id(p)[0] == t.source_id for p in r.pages[:K]):
                return "DILUTION"
        return "OTHER"

    out: dict = {"benchmark": "TOPIC_BENCHMARK_V1", "k": K, "causes": list(CAUSES),
                 "deviation": ("NO_TEXT counted by the actual page text; the pre-registered flag rule "
                               "(VECTOR_NO_TEXT / IMAGE_NO_TEXT = file text layer missing, page OCR-ed) is kept in "
                               "by_cause_preregistered_rule"),
                 "registry_methods": "page-level" if os.environ.get("VKM_RESOURCES_ROOT") else "model-level",
                 "systems": {}}
    n_targets = sum(len(tp.targets) for tp in topics)
    for system in systems:
        missed, missed_pre, per_query = [], Counter(), Counter()
        by_track = defaultdict(Counter)
        zero_topics = []
        for topic in topics:
            found = set()
            for x in topic.queries:
                r = rk.get((x.query_id, system))
                if r is None:
                    continue
                for tid, v in TB.target_ranks(topic.targets, r).items():
                    if v is not None and v <= K:
                        found.add(tid)
                    else:
                        per_query[cause(topic, next(t for t in topic.targets if t.target_id == tid), system)] += 1
            if not found:
                zero_topics.append(topic.topic_id)
            for t in topic.targets:
                if t.target_id not in found:
                    c = cause(topic, t, system)
                    missed.append((t.target_id, c))
                    missed_pre[cause(topic, t, system, "preregistered")] += 1
                    by_track[topic.track][c] += 1
        rows = []
        for topic in topics:
            for x in topic.queries:
                r = rk.get((x.query_id, system))
                if r is not None:
                    m = TB.query_metrics(topic, r)
                    rows.append((m["page_recall@50"], m["mrr@50"], x.query_id))
        out["systems"][system] = {
            "n_targets": n_targets, "n_missed_all_variants": len(missed),
            "missed_share": round(len(missed) / n_targets, 4),
            "by_cause": {c: n for c, n in Counter(c for _t, c in missed).most_common()},
            "by_cause_preregistered_rule": dict(missed_pre.most_common()),
            "by_cause_and_track": {tr: dict(v.most_common()) for tr, v in by_track.items()},
            "per_query_target_misses_by_cause": dict(per_query.most_common()),
            "queries_with_zero_page_recall@50": sum(1 for r50, _m, _q in rows if r50 == 0),
            "worst_queries": [qid for _r, _m, qid in sorted(rows)[:10]],
            "topics_with_no_target_found": zero_topics,
            "missed_target_ids": sorted(t for t, _c in missed),
        }

    # ------------------------------------------------------------ descriptive diagnostics
    diag: dict = {}
    if "hybrid_late" in systems:
        sr50, conc, dist = [], [], Counter()
        for tp in topics:
            for q in tp.queries:
                r = rk[(q.query_id, "hybrid_late")]
                got = {TB.split_page_id(p)[0] for al in r.aliases[:K] for p in al}
                sr50.append(len(tp.sources & got) / len(tp.sources))
                per_src = Counter(TB.split_page_id(p)[0] for p in r.pages[:K])
                conc.append(max(per_src.values()) if per_src else 0)
                for t in tp.targets:
                    if any(al & t.pages for al in r.aliases[:K]):
                        continue
                    same = [TB.split_page_id(p)[2] for p in r.pages[:K] if TB.split_page_id(p)[0] == t.source_id]
                    if same:
                        d = min(abs(i - TB.split_page_id(t.page_id)[2]) for i in same)
                        dist["<=2" if d <= 2 else "3-5" if d <= 5 else "6-20" if d <= 20 else ">20"] += 1
        diag["hybrid_late_source_recall@50"] = mean(sr50)
        diag["hybrid_late_max_pages_of_one_source_in_top50_mean"] = mean(conc)
        diag["hybrid_late_query_misses_with_same_source_retrieved_by_distance"] = dict(dist)
    if "nav" in systems:
        nav = [v for k, v in runs.items() if k[1] == "nav"]
        diag["nav_queries_with_empty_section_search_404"] = sum(1 for v in nav if not (v.get("nav") or {}).get("sections"))
        diag["nav_queries_without_concept_match"] = sum(1 for v in nav if not ((v.get("nav") or {}).get("term") or {})
                                                        .get("term_id"))
        diag["nav_levels_of_top5_section_search_results"] = dict(sorted(Counter(
            s.get("level") for v in nav for s in ((v.get("nav") or {}).get("sections") or [])[:5]).items()))
        diag["nav_pages_in_top10_nodes_mean"] = mean(len(set().union(*rk[k].nodes[:10])) if rk[k].nodes else 0
                                                     for k in rk if k[1] == "nav")
    out["diagnostics"] = diag

    # ------------------------------------------------------------ exploratory gains (post hoc)
    gains: dict = {"note": "post-hoc, exploratory (not pre-registered): simple remedies measured on the same runs, "
                           "same 50-page budget"}
    if "hybrid_late" in systems:
        base, multi20, multi50, fused20, fused50, nb = [], [], [], [], [], []
        better = worse = 0
        for topic in topics:
            rl = [rk.get((x.query_id, "hybrid_late")) for x in topic.queries]
            rl = [r for r in rl if r is not None]
            aliases = {p: a for r in rl for p, a in zip(r.pages, r.aliases)}
            single = [recall_of(r.pages[:K], topic, aliases) for r in rl]
            base += single
            merged = rrf([r.pages for r in rl])
            multi20.append(recall_of(merged[:20], topic, aliases))
            multi50.append(recall_of(merged, topic, aliases))
            better += multi50[-1] > sum(single) / len(single) + 1e-9
            worse += multi50[-1] < sum(single) / len(single) - 1e-9
            for x in topic.queries:
                h, n = rk.get((x.query_id, "hybrid_late")), rk.get((x.query_id, "nav"))
                if h is None:
                    continue
                if n is not None:
                    f = rrf([h.pages, n.pages])
                    fused20.append(recall_of(f[:20], topic, aliases))
                    fused50.append(recall_of(f, topic, aliases))
                expanded: list[str] = []
                for p in h.pages[:17]:
                    src, pre, i = TB.split_page_id(p)
                    for j in (i, i + 1, i - 1):
                        q = f"{src}:{pre}{j:04d}"
                        if j >= 1 and q not in expanded:
                            expanded.append(q)
                nb.append(recall_of(expanded[:K], topic, aliases))
        gains["hybrid_late_page_recall@50"] = mean(base)
        gains["three_formulations_rrf_page_recall@20"] = mean(multi20)
        gains["three_formulations_rrf_page_recall@50"] = mean(multi50)
        gains["three_formulations_topics_better_worse_than_single_mean"] = [better, worse]
        if fused50:
            gains["rrf_hybrid_late_nav_page_recall@20"] = mean(fused20)
            gains["rrf_hybrid_late_nav_page_recall@50"] = mean(fused50)
        gains["neighbour_pages_top17_pm1_page_recall@50"] = mean(nb)
    out["exploratory_gains"] = gains
    Path(args.out).write_bytes((json.dumps(out, ensure_ascii=False, indent=1) + "\n").encode("utf-8"))
    for s, v in out["systems"].items():
        print(s, v["n_missed_all_variants"], v["missed_share"], v["by_cause"], "| prereg:", v["by_cause_preregistered_rule"])
        print("  zero-recall queries", v["queries_with_zero_page_recall@50"], "topics with nothing found",
              len(v["topics_with_no_target_found"]), "worst", v["worst_queries"])
    print(json.dumps(diag, ensure_ascii=False))
    print(json.dumps(gains, ensure_ascii=False))


if __name__ == "__main__":
    main()
