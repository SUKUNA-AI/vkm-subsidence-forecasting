"""TOPIC_BENCHMARK_V1 — pooled labels T1 (preregistration: ``benchmarks/topic_v1/POOL_LABELING_V1.md``).

The frozen truth of topic_v1 (886 catalogue pages in 39 sources) is extended by agent labels ``LLM_AGENT_T1`` on the pool
of the systems' first pages over the whole corpus. Label unit: topic × page; grade 0–3 against the topic definition and
a machine-readable reason code; status CANDIDATE, basis POOL_JUDGMENT. The catalogue truth wins: a pooled page that
matches a catalogue target of its topic (primary page, alternate or duplicate alias) is not labelled.

    rank     rankings of the topic_v1 systems (the raw answers of the frozen runs through the ranking code of score.py)
             and of the TERM_DICTIONARY_V1 dossier arms D0 / D1 (stage-B searches replayed through the production fusion
             code, as term_dictionary_v1/scripts/evaluate.py does) → ``$LB_WORK/rankings_t1.json`` (IDs only);
             control: every per-query metric equals results_v1.json of topic_v1 and of term_dictionary_v1
    pool     the preregistered pool (systems, depth rule, exclusions, duplicate groups) → ``$LB_WORK/pool_t1.json``
    packets  blind labelling packets ``$LB_WORK/packets/pk-NNN.md``: the topic definition, then per candidate its page
             id, source title, windows of the page text, captions (no system names, no ranks; candidates by page id)
    status   labelled / missing per packet
    ingest   ``$LB_WORK/judgments/*.txt`` (lines ``<topic_id>|<cid>|<grade>|<CODE>``) → checks →
             ``benchmarks/topic_v1/qrels_topic_v1_pooled.tsv`` (IDs, grades, provenance, reason codes; no corpus text)
    hsample  seeded review sample stratified by grade → ``H_REVIEW_SAMPLE_T1.md`` (blind: no grade, no code, no system),
             ``$LB_WORK/h_review/`` (the key, the page windows for the reviewer and the description sheet; outside git)

Environment (all outside git): ``LB_WORK`` (work dir), ``TOPIC_RUNS`` (dir with runs_v1.jsonl, runs_v1_pool.jsonl,
runs_v1_posthoc.jsonl of topic_v1), ``TD_WORK`` (TERM_DICTIONARY_V1 stage_a.json, stage_b.json, root/),
``CANON_DB`` (canonical DuckDB of the topic_v1 snapshot, read only), ``NAV_SECTIONS`` (optional sections.parquet of a
NAV build: section titles in the packets).
"""
from __future__ import annotations

import csv
import hashlib
import importlib.util
import json
import os
import random
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
BENCH = REPO / "benchmarks" / "topic_v1"
sys.path.insert(0, str(REPO / "src"))

from vkm_corpus.retrieval_lab import bench as B  # noqa: E402
from vkm_corpus.retrieval_lab import topic_bench as TB  # noqa: E402

LABEL_SOURCE = "LLM_AGENT_T1"
OUT_TSV = BENCH / "qrels_topic_v1_pooled.tsv"
SET_SHA256 = "4dbc595723cbb9ed3e59d1a71cbe84301c9554ecedc41b7f3abf654258bac2b1"
TOPIC_SYSTEMS = ("bm25", "hybrid_late", "hybrid_nolate", "nav", "hybrid_late_pool", "hybrid_late_kinds",
                 "hybrid_late_drill")
DOSSIER_ARMS = ("D0", "D1")
# the preregistered pool (POOL_LABELING_V1.md §2): system → depth over each of the topic's three queries
POOL_DEPTH = {"hybrid_late": 10, "hybrid_nolate": 10, "bm25": 10, "nav": 10, "D1": 10}
EXTENSION = ("hybrid_late", 20)        # labelled only if the depth-10 pool plus the extension stays within BUDGET
BUDGET = 6000
GRADES = (0, 1, 2, 3)
# reason codes (POOL_LABELING_V1.md §4): the grade each code may carry
CODES = {
    "KEY_QUANT": 3, "KEY_MODEL": 3, "KEY_OBS": 3, "KEY_MECH": 3,
    "SUP_DISCUSS": 2, "SUP_FIGTAB": 2, "SUP_ASPECT": 2, "SUP_MODEL": 2,
    "MENTION": 1, "LIST": 1, "NEIGHBOUR": 1, "GENERAL": 1,
    "OFF_TOPIC": 0, "NO_TEXT": 0, "FRONT_MATTER": 0,
}
SNIP_MAIN, SNIP_SECOND, SNIP_SHORT_PAGE, SNIP_CAPTION = 520, 260, 800, 110
CANDS_PER_PACKET = 60
HSAMPLE_N, HSAMPLE_SEED = 60, 20260929


def env_path(name: str) -> Path:
    v = os.environ.get(name)
    if not v:
        raise SystemExit(f"environment variable {name} is not set")
    return Path(v).expanduser()


def sha256_file(path: Path, lf: bool = False) -> str:
    data = path.read_bytes()
    return hashlib.sha256(data.replace(b"\r\n", b"\n") if lf else data).hexdigest()


def load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def load_topics() -> list[TB.Topic]:
    path = BENCH / "topic_set_v1.jsonl"
    if sha256_file(path, lf=True) != SET_SHA256:
        raise SystemExit("topic_set_v1.jsonl does not match the pre-registered sha256")
    return TB.load_set(path)


# ------------------------------------------------------------------------------------------------ rankings
def topic_v1_rankings(topics: list[TB.Topic]) -> tuple[dict, dict, TB.SectionIndex, list[dict]]:
    """Rankings of the topic_v1 systems from the frozen raw answers (score.py code) + the section index."""
    score = load_module("topic_v1_score", BENCH / "scripts" / "score.py")
    runs_dir = env_path("TOPIC_RUNS")
    paths = [str(runs_dir / n) for n in ("runs_v1.jsonl", "runs_v1_pool.jsonl", "runs_v1_posthoc.jsonl")]
    runs, outlines, _meta, files = score.load_runs(paths)
    index = TB.SectionIndex.from_outlines(outlines)
    out: dict[str, dict[str, TB.Ranking]] = defaultdict(dict)
    for (qid, system), line in runs.items():
        out[system][qid] = score.ranking_of(line, index)
    return out, runs, index, files


def dossier_rankings(topics: list[TB.Topic]) -> tuple[dict[str, dict[str, TB.Ranking]], dict]:
    """D0 / D1 of TERM_DICTIONARY_V1: the recorded formulations replayed over the stage-B searches through
    ``DossierBuilder._retrieve`` and ``_page_list`` (pages.core, then pages.rest) — the code path of evaluate.py q1."""
    td = env_path("TD_WORK")
    os.environ.setdefault("TD_WORK", str(td))
    ev = load_module("td_evaluate", REPO / "benchmarks" / "term_dictionary_v1" / "scripts" / "evaluate.py")
    from vkm_corpus.api import topic as T
    from vkm_corpus.api.canon import CanonStore

    a = json.load(open(td / "stage_a.json", encoding="utf-8"))
    b = json.load(open(td / "stage_b.json", encoding="utf-8"))
    snap = a["snapshot_id"]
    canon = CanonStore(td / "root" / "duckdb" / "vkm_corpus.duckdb", current_snapshot=lambda: snap)
    builder = ev.ReplayBuilder(canon, None, None, ev.Replay(b["dossier"]), cache={})
    tmap = {t.topic_id: t for t in topics}
    core = set(a["dossier"]["core_sources"])
    out: dict[str, dict[str, TB.Ranking]] = {arm: {} for arm in DOSSIER_ARMS}
    for qid, q in sorted(a["dossier"]["queries"].items()):
        t = tmap[q["topic_id"]]
        text = next(x.text for x in t.queries if x.query_id == qid)
        for arm in DOSSIER_ARMS:
            st = T._State(req=T.TopicRequest(query=text, translate=(arm == "D1")), stems=T.query_stems(text))
            st.core = set(core)
            st.recorded = q[arm]["formulations"]
            builder._retrieve(st)
            pages = builder._page_list(st)
            r = TB.Ranking()
            for p in pages["core"] + pages["rest"]:
                r.add_page(p["page_id"])
            out[arm][qid] = r
    info = {"snapshot_id": snap, "stage_a_sha256": sha256_file(td / "stage_a.json"),
            "stage_b_sha256": sha256_file(td / "stage_b.json")}
    return out, info


def all_rankings(topics: list[TB.Topic]) -> tuple[dict[str, dict[str, TB.Ranking]], dict]:
    tv1, _runs, _index, files = topic_v1_rankings(topics)
    dos, info = dossier_rankings(topics)
    rankings = {**tv1, **dos}
    return rankings, {"topic_v1_run_files": files, "term_dictionary": info}


def control(topics: list[TB.Topic], rankings: dict[str, dict[str, TB.Ranking]]) -> dict:
    """Recomputed per-query metrics must equal the published ones (4-decimal rounding of the published files)."""
    tmap = {q.query_id: t for t in topics for q in t.queries}
    res = json.load(open(BENCH / "results_v1.json", encoding="utf-8"))
    cols = res["per_query"]["columns"]
    diffs = Counter()
    n = 0
    for row in res["per_query"]["rows"]:
        d = dict(zip(cols, row))
        r = rankings[d["system"]].get(d["query_id"])
        m = TB.query_metrics(tmap[d["query_id"]], r)
        for c in cols[2:-1]:
            n += 1
            if abs(round(m[c], 4) - d[c]) > 1e-9:
                diffs[(d["system"], c)] += 1
    td = json.load(open(REPO / "benchmarks" / "term_dictionary_v1" / "results_v1.json", encoding="utf-8"))
    for arm, rows in td["q1_dossier"]["per_query"].items():
        for qid, ms in rows.items():
            m = TB.query_metrics(tmap[qid], rankings[arm][qid])
            for c, v in ms.items():
                n += 1
                if abs(round(m[c], 4) - v) > 1e-9:
                    diffs[(arm, c)] += 1
    return {"values_checked": n, "mismatches": {f"{s}|{c}": k for (s, c), k in sorted(diffs.items())}}


def rank() -> None:
    work = env_path("LB_WORK")
    work.mkdir(parents=True, exist_ok=True)
    topics = load_topics()
    rankings, info = all_rankings(topics)
    ctl = control(topics, rankings)
    out = {"info": info, "control": ctl,
           "rankings": {s: {q: [[p, sorted(al)] for p, al in zip(r.pages, r.aliases)] for q, r in rs.items()}
                        for s, rs in sorted(rankings.items())}}
    (work / "rankings_t1.json").write_text(json.dumps(out, ensure_ascii=False), encoding="utf-8")
    print(json.dumps({"info": info, "control": ctl}, ensure_ascii=False, indent=1))
    if ctl["mismatches"]:
        raise SystemExit("recomputed rankings do not reproduce the published per-query metrics")


def load_rankings() -> dict[str, dict[str, list[tuple[str, frozenset[str]]]]]:
    data = json.load(open(env_path("LB_WORK") / "rankings_t1.json", encoding="utf-8"))
    return {s: {q: [(p, frozenset(al)) for p, al in lst] for q, lst in rs.items()}
            for s, rs in data["rankings"].items()}


# ------------------------------------------------------------------------------------------------ pool
def canon_duplicate_groups(con) -> dict[str, frozenset[str]]:
    groups: dict[str, frozenset[str]] = {}
    for _gid, pages in con.execute("SELECT dup_group_id, list(page_id ORDER BY page_id) FROM "
                                   "main.duplicate_page_candidates GROUP BY 1").fetchall():
        for p in pages:
            groups[p] = frozenset(pages)
    return groups


def target_pages(t: TB.Topic) -> frozenset[str]:
    out: set[str] = set()
    for x in t.targets:
        out |= x.pages
    return frozenset(out)


def collect_pool(topics: list[TB.Topic], rk: dict, depths: dict[str, int], dup: dict[str, frozenset[str]]
                 ) -> dict[str, dict]:
    """Per topic: candidate pages (union over the three queries of the first ``depth`` pages of each system) that do
    not match a catalogue target; provenance = best rank per system; alias pages (API duplicates, canonical duplicate
    groups) of each candidate."""
    pool: dict[str, dict] = {}
    for t in topics:
        tp = target_pages(t)
        cands: dict[str, dict] = {}
        excluded: set[str] = set()
        for q in t.queries:
            for system, depth in depths.items():
                for rank_, (page, aliases) in enumerate(rk[system].get(q.query_id, [])[:depth], 1):
                    al = set(aliases) | set(dup.get(page, ()))
                    if al & tp:
                        excluded.add(page)
                        continue
                    c = cands.setdefault(page, {"page_id": page, "systems": {}, "variants": set(), "aliases": set()})
                    c["systems"][system] = min(rank_, c["systems"].get(system, 999))
                    c["variants"].add(q.variant)
                    c["aliases"] |= al - {page}
        pool[t.topic_id] = {"candidates": cands, "excluded_catalogue_matches": sorted(excluded)}
    return pool


def group_candidates(cands: dict[str, dict]) -> list[list[str]]:
    """Duplicate groups among a topic's candidates (union-find over alias links); one label unit per group."""
    parent = {p: p for p in cands}

    def find(x: str) -> str:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for p, c in cands.items():
        for a in c["aliases"]:
            if a in parent:
                ra, rp = find(a), find(p)
                if ra != rp:
                    parent[max(ra, rp)] = min(ra, rp)
    groups: dict[str, list[str]] = defaultdict(list)
    for p in sorted(cands):
        groups[find(p)].append(p)
    return sorted(groups.values())


def pool() -> None:
    import duckdb

    work = env_path("LB_WORK")
    topics = load_topics()
    rk = load_rankings()
    con = duckdb.connect(str(env_path("CANON_DB")), read_only=True)
    dup = canon_duplicate_groups(con)
    snapshot = con.execute("SELECT snapshot_id FROM meta.snapshot").fetchone()[0]
    pool10 = collect_pool(topics, rk, POOL_DEPTH, dup)
    n10 = sum(len(v["candidates"]) for v in pool10.values())
    ext_depths = {**POOL_DEPTH, EXTENSION[0]: EXTENSION[1]}
    pool20 = collect_pool(topics, rk, ext_depths, dup)
    n20 = sum(len(v["candidates"]) for v in pool20.values())
    extend = n20 <= BUDGET
    chosen = pool20 if extend else pool10
    depths = ext_depths if extend else dict(POOL_DEPTH)
    old = {}
    if (work / "pool_t1.json").is_file():
        old = json.load(open(work / "pool_t1.json", encoding="utf-8"))["topics"]
    topics_out = {}
    stats = Counter()
    for t in topics:
        cands = chosen[t.topic_id]["candidates"]
        prev = {p: u["cid"] for u in old.get(t.topic_id, {}).get("units", []) for p in u["pages"]}
        nxt = 1 + max((int(c[1:]) for c in prev.values()), default=0)
        units = []
        for g in group_candidates(cands):
            cid = next((prev[p] for p in g if p in prev), None)
            if cid is None:
                cid = f"c{nxt:03d}"
                nxt += 1
            systems: dict[str, int] = {}
            for p in g:
                for s, r in cands[p]["systems"].items():
                    systems[s] = min(r, systems.get(s, 999))
            units.append({"cid": cid, "pages": g, "systems": systems,
                          "variants": sorted(set().union(*(cands[p]["variants"] for p in g))),
                          "per_page": {p: cands[p]["systems"] for p in g},
                          "aliases": sorted(set().union(*(cands[p]["aliases"] for p in g)) - set(g))})
            stats["units"] += 1
            stats["pages"] += len(g)
            stats["multi_page_units"] += 1 if len(g) > 1 else 0
        units.sort(key=lambda u: u["pages"][0])
        topics_out[t.topic_id] = {"track": t.track, "units": units,
                                  "excluded_catalogue_matches": chosen[t.topic_id]["excluded_catalogue_matches"]}
        stats["excluded_catalogue_matches"] += len(chosen[t.topic_id]["excluded_catalogue_matches"])
    by_system = Counter()
    only = Counter()
    for tid, v in topics_out.items():
        for u in v["units"]:
            for s in u["systems"]:
                by_system[s] += 1
            if len(u["systems"]) == 1:
                only[next(iter(u["systems"]))] += 1
    per_topic = sorted(len(v["units"]) for v in topics_out.values())
    summary = {"canonical_snapshot_id": snapshot, "depths": depths, "budget": BUDGET,
               "pairs_depth10": n10, "pairs_depth10_plus_extension": n20, "extension_applied": extend,
               "label_units": stats["units"], "pooled_pages": stats["pages"],
               "multi_page_units": stats["multi_page_units"],
               "excluded_catalogue_matches": stats["excluded_catalogue_matches"],
               "units_by_system": dict(sorted(by_system.items())), "units_only_one_system": dict(sorted(only.items())),
               "units_per_topic": {"min": per_topic[0], "median": per_topic[len(per_topic) // 2],
                                   "max": per_topic[-1]},
               "units_by_track": dict(Counter(v["track"] for v in topics_out.values() for _u in v["units"]))}
    (work / "pool_t1.json").write_text(json.dumps({"summary": summary, "topics": topics_out}, ensure_ascii=False,
                                                  indent=0), encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=1))


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else ""
    fn = {"rank": rank, "pool": pool}.get(cmd)
    if fn is None:
        raise SystemExit(__doc__)
    fn()
