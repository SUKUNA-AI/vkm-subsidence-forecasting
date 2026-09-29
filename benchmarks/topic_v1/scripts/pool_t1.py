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
    packets  blind labelling packets ``$LB_WORK/packets/pk-NNN.md`` (pages in page-id order; per page: source, section,
             text windows for its candidate topics, captions; then its candidate units; a legend of the packet's topics
             on top; no system names, no ranks) + ``$LB_WORK/topics_handbook.md`` (all topic definitions)
    status   judged / missing per packet
    ingest   ``$LB_WORK/judgments/*.txt`` (lines ``<topic_id>|<cid>|<grade>|<CODE>``) → checks →
             ``qrels_topic_v1_pooled.tsv`` (IDs, grades, provenance, reason codes; no corpus text) and
             ``qrels_topic_v1_pooled_groups.json`` (units with several pages or duplicate aliases)
    hsample  seeded review sample stratified by grade: ``--draw`` → ``$LB_WORK/h_review/`` (sample, key, the whole text of
             the sampled pages for the reviewer and a description sheet to fill); ``--write`` → ``H_REVIEW_SAMPLE_T1.md``
             (blind)
    hscore   agreement of a filled review file with the agent grades: ``hscore <filled H_REVIEW_SAMPLE_T1.md>``

Environment (all outside git): ``LB_WORK`` (work dir), ``TOPIC_RUNS`` (dir with runs_v1.jsonl, runs_v1_pool.jsonl,
runs_v1_posthoc.jsonl of topic_v1), ``TD_WORK`` (TERM_DICTIONARY_V1 stage_a.json, stage_b.json, root/),
``CANON_DB`` (canonical DuckDB of the topic_v1 snapshot, read only), ``J_V1`` (retrieval_v1 work dir: pages.json with
source titles), ``NAV_SECTIONS`` (optional sections.parquet of a NAV build: section titles in the packets).
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
OUT_GROUPS = BENCH / "qrels_topic_v1_pooled_groups.json"
H_SAMPLE_MD = BENCH / "H_REVIEW_SAMPLE_T1.md"
SET_SHA256 = "4dbc595723cbb9ed3e59d1a71cbe84301c9554ecedc41b7f3abf654258bac2b1"
DOSSIER_ARMS = ("D0", "D1")
# the preregistered pool (POOL_LABELING_V1.md §3): system → depth over each of the topic's three queries
POOL_DEPTH = {"hybrid_late": 10, "hybrid_nolate": 10, "bm25": 10, "nav": 10, "D1": 10}
EXTENSION = ("hybrid_late", 20)        # labelled only if the depth-10 pool plus the extension stays within BUDGET
BUDGET = 6000
GRADES = (0, 1, 2, 3)
# reason codes (POOL_LABELING_V1.md §4) and the one grade each code carries
CODES = {
    "KEY_QUANT": 3, "KEY_MODEL": 3, "KEY_OBS": 3, "KEY_MECH": 3,
    "SUP_DISCUSS": 2, "SUP_FIGTAB": 2, "SUP_ASPECT": 2, "SUP_MODEL": 2,
    "MENTION": 1, "LIST": 1, "NEIGHBOUR": 1, "GENERAL": 1,
    "OFF_TOPIC": 0, "NO_TEXT": 0, "FRONT_MATTER": 0,
}
# packets (POOL_LABELING_V1.md §5)
WINDOW_CHARS, PAGE_BUDGET, FULL_PAGE_CHARS, HEAD_CHARS, CHUNK_CHARS = 400, 1000, 1100, 100, 200
CAPTION_CHARS, MAX_CAPTIONS, PAGES_PER_PACKET, UNITS_PER_PACKET = 110, 2, 60, 130
FORMULA_PREVIEWS, FORMULA_CHARS = 3, 90
# review sample (POOL_LABELING_V1.md §8)
HSAMPLE_PER_GRADE, HSAMPLE_SEED = 15, 20260929
SWEEP_SOURCES = frozenset(TB.SWEEP_SOURCES)
_POOLED_FROM = re.compile(r"^[A-Za-z0-9_]+@[0-9]{1,3}(,[A-Za-z0-9_]+@[0-9]{1,3})*$")


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


def write_lf(path: Path, text: str) -> None:
    path.write_bytes(text.encode("utf-8"))


# ------------------------------------------------------------------------------------------------ rankings
def topic_v1_rankings() -> tuple[dict[str, dict[str, TB.Ranking]], TB.SectionIndex, list[dict]]:
    """Rankings of the topic_v1 systems from the frozen raw answers (score.py code) + the section index."""
    score = load_module("topic_v1_score", BENCH / "scripts" / "score.py")
    runs_dir = env_path("TOPIC_RUNS")
    paths = [str(runs_dir / n) for n in ("runs_v1.jsonl", "runs_v1_pool.jsonl", "runs_v1_posthoc.jsonl")]
    runs, outlines, _meta, files = score.load_runs(paths)
    index = TB.SectionIndex.from_outlines(outlines)
    out: dict[str, dict[str, TB.Ranking]] = defaultdict(dict)
    for (qid, system), line in runs.items():
        out[system][qid] = score.ranking_of(line, index)
    return dict(out), index, files


def dossier_rankings(topics: list[TB.Topic]) -> tuple[dict[str, dict[str, TB.Ranking]], dict]:
    """D0 / D1 of TERM_DICTIONARY_V1: the recorded formulations replayed over the stage-B searches through
    ``DossierBuilder._retrieve`` and ``_page_list`` (pages.core, then pages.rest) — the code path of evaluate.py q1."""
    td = env_path("TD_WORK")
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


def all_rankings(topics: list[TB.Topic]) -> tuple[dict[str, dict[str, TB.Ranking]], TB.SectionIndex, dict]:
    tv1, index, files = topic_v1_rankings()
    dos, info = dossier_rankings(topics)
    return {**tv1, **dos}, index, {"topic_v1_run_files": files, "term_dictionary": info}


def control(topics: list[TB.Topic], rankings: dict[str, dict[str, TB.Ranking]]) -> dict:
    """Recomputed per-query metrics must equal the published ones (4-decimal rounding of the published files)."""
    tmap = {q.query_id: t for t in topics for q in t.queries}
    res = json.load(open(BENCH / "results_v1.json", encoding="utf-8"))
    cols = res["per_query"]["columns"]
    diffs: Counter = Counter()
    n = 0
    for row in res["per_query"]["rows"]:
        d = dict(zip(cols, row))
        m = TB.query_metrics(tmap[d["query_id"]], rankings[d["system"]][d["query_id"]])
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
    rankings, _index, info = all_rankings(topics)
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
    pool_: dict[str, dict] = {}
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
        pool_[t.topic_id] = {"candidates": cands, "excluded_catalogue_matches": sorted(excluded)}
    return pool_


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
    stats: Counter = Counter()
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
    by_system: Counter = Counter()
    only: Counter = Counter()
    for v in topics_out.values():
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


def load_pool() -> dict:
    return json.load(open(env_path("LB_WORK") / "pool_t1.json", encoding="utf-8"))


# ------------------------------------------------------------------------------------------------ packets
def short(text: str | None, limit: int) -> str:
    s = re.sub(r"\s+", " ", text or "").strip()
    return s if len(s) <= limit else s[: limit - 1].rstrip() + "…"


def topic_definitions(topics: list[TB.Topic]) -> dict[str, dict]:
    """Title, queries and the catalogue row of every topic (process row / model names of the family)."""
    cat = REPO / "catalogues"
    matrix = {r["process_id"]: r for r in csv.DictReader(
        open(cat / "physics" / "physics_coverage_and_execution_matrix.csv", encoding="utf-8"))}
    models: dict[str, list[dict]] = defaultdict(list)
    for r in csv.DictReader(open(cat / "mathematics" / "MATHEMATICAL_MODEL_REGISTRY.csv", encoding="utf-8")):
        models["MM-" + r["model_id"].split("-")[1]].append(r)
    out = {}
    for t in topics:
        d = {"topic_id": t.topic_id, "track": t.track, "group": t.group, "title": t.title,
             "queries": [q.text for q in t.queries]}
        if t.track == "PROCESS":
            r = matrix.get(t.topic_id, {})
            d.update(role=r.get("causal_role"), path=r.get("causal_path_to_subsidence"),
                     variables=r.get("required_variables"), parameters=r.get("required_parameters"),
                     equations=r.get("governing_equations"))
        else:
            d["models"] = [m["name_ru"] for m in models.get(t.topic_id, [])]
        out[t.topic_id] = d
    return out


def translation_texts() -> dict[str, list[str]]:
    """Per topic: the translation formulations of D1 (English words help to find windows on English pages)."""
    td = env_path("TD_WORK")
    a = json.load(open(td / "stage_a.json", encoding="utf-8"))
    out: dict[str, list[str]] = defaultdict(list)
    for q in a["dossier"]["queries"].values():
        for f in q["D1"]["formulations"]:
            if f["kind"] == "translation" and f["text"] not in out[q["topic_id"]]:
                out[q["topic_id"]].append(f["text"])
    return out


def topic_stems(d: dict, extra: list[str]) -> frozenset[str]:
    from vkm_corpus.retrieval_lab.textproc import analyze

    return frozenset(s for s in analyze(" ".join([d["title"], *d["queries"], *extra])) if len(s) >= 3)


def chunks_of(text: str) -> list[str]:
    from vkm_corpus.retrieval_lab.textproc import pack_sentences

    text = re.sub(r"\s+", " ", text or "").strip()
    return pack_sentences(text, CHUNK_CHARS) if text else []


def best_run(chunks: list[str], hits: list[set[str]], limit: int, weight: dict[str, float] | None = None
             ) -> tuple[float, int, int]:
    """(score, i, j): the contiguous run chunks[i:j] of at most ``limit`` characters whose *distinct* topic stems weigh
    most (weight = the stem's idf over the pooled pages, so that a word repeated in every chunk does not outweigh the
    passage with the topic's specific words; ties: the earliest run)."""
    w = weight or {}
    best = (-1.0, 0, 0)
    for i in range(len(chunks)):
        length, j = 0, i
        seen: set[str] = set()
        while j < len(chunks) and length + len(chunks[j]) + 1 <= limit:
            length += len(chunks[j]) + 1
            seen |= hits[j]
            j += 1
        sc = sum(w.get(s, 1.0) for s in seen)
        if j > i and sc > best[0] + 1e-12:
            best = (sc, i, j)
    return best


def page_windows(text: str, stem_sets: list[frozenset[str]], weight: dict[str, float] | None = None) -> str:
    """The text shown for a page: the whole page if short, else the union of the best window of every candidate topic
    (document order, ≤ PAGE_BUDGET characters) and the page head if it is not inside a window."""
    from vkm_corpus.retrieval_lab.textproc import analyze

    flat = re.sub(r"\s+", " ", text or "").strip()
    if len(flat) <= FULL_PAGE_CHARS:
        return flat
    chunks = chunks_of(flat)
    cstems = [set(analyze(c)) for c in chunks]
    runs = []
    for stems in stem_sets:
        sc, i, j = best_run(chunks, [stems & cs for cs in cstems], WINDOW_CHARS, weight)
        runs.append((sc, i, j))
    chosen: set[int] = set()
    used = 0
    for sc, i, j in sorted(runs, key=lambda r: (-r[0], r[1])):
        for k in range(i, j):
            if k in chosen:
                continue
            if used + len(chunks[k]) + 1 > PAGE_BUDGET:
                break
            chosen.add(k)
            used += len(chunks[k]) + 1
    parts, prev = [], None
    for k in sorted(chosen):
        if prev is not None and k != prev + 1:
            parts.append("…")
        parts.append(chunks[k])
        prev = k
    body = " ".join(parts)
    if 0 not in chosen:
        body = "[начало] " + short(chunks[0], HEAD_CHARS) + " … " + body
    if chunks and max(chosen, default=0) < len(chunks) - 1:
        body += " …"
    return body


def page_data(con, pages: list[str]) -> dict[str, dict]:
    con.execute("CREATE OR REPLACE TEMP TABLE lb_pp(page_id VARCHAR)")
    con.executemany("INSERT INTO lb_pp VALUES (?)", [[p] for p in pages])
    out: dict[str, dict] = {}
    for pid, sid, idx, text in con.execute(
            "SELECT p.page_id, p.source_id, p.page_index, p.normalized_text FROM lb_pp JOIN canonical.pages p "
            "USING (page_id)").fetchall():
        out[pid] = {"source_id": sid, "page_index": idx, "text": text or "", "captions": [], "formulas": 0}
    for pid, label, cap in con.execute(
            "SELECT f.page_id, f.figure_label, f.caption FROM lb_pp JOIN canonical.figures f USING (page_id) "
            "WHERE f.caption IS NOT NULL AND length(f.caption) > 0 ORDER BY f.page_id, f.object_id").fetchall():
        out[pid]["captions"].append(("рис.", label, cap))
    for pid, label, cap in con.execute(
            "SELECT t.page_id, t.table_label, t.caption FROM lb_pp JOIN canonical.tables t USING (page_id) "
            "WHERE t.caption IS NOT NULL AND length(t.caption) > 0 ORDER BY t.page_id, t.object_id").fetchall():
        out[pid]["captions"].append(("табл.", label, cap))
    for pid, n in con.execute("SELECT page_id, count(*) FROM lb_pp JOIN canonical.formulas USING (page_id) "
                              "GROUP BY 1").fetchall():
        out[pid]["formulas"] = n
    previews: dict[str, list[tuple[str, str]]] = defaultdict(list)
    for pid, oid, latex in con.execute(
            "SELECT f.page_id, f.object_id, coalesce(f.normalized_latex, f.native_glyph_text) FROM lb_pp "
            "JOIN canonical.formulas f USING (page_id) WHERE coalesce(f.normalized_latex, f.native_glyph_text) "
            "IS NOT NULL").fetchall():
        previews[pid].append((oid, re.sub(r"\s+", " ", latex).strip()))
    for pid, fs in previews.items():            # the longest formulas of the page (most informative), in page order
        top = sorted(sorted(fs, key=lambda x: (-len(x[1]), x[0]))[:FORMULA_PREVIEWS])
        out[pid]["formula_previews"] = [short(x[1], FORMULA_CHARS) for x in top if len(x[1]) >= 12]
    return out


def stem_idf(texts: list[str]) -> dict[str, float]:
    """idf of every stem over the pooled pages (a page = a document): the weight of a topic word in a window."""
    import math

    from vkm_corpus.retrieval_lab.textproc import analyze

    df: Counter = Counter()
    for t in texts:
        df.update(set(analyze(t or "")))
    n = max(1, len(texts))
    return {s: math.log(1 + n / c) for s, c in df.items()}


def section_titles(pages: dict[str, dict]) -> dict[str, str]:
    path = os.environ.get("NAV_SECTIONS")
    if not path or not Path(path).expanduser().is_file():
        return {}
    import duckdb

    con = duckdb.connect()
    rows = con.execute(f"SELECT source_id, level, page_start_index, page_end_index, title FROM "
                       f"read_parquet('{Path(path).expanduser()}') WHERE title IS NOT NULL").fetchall()
    by_source: dict[str, list[tuple]] = defaultdict(list)
    for sid, level, lo, hi, title in rows:
        if lo is not None and hi is not None:
            by_source[sid].append((int(level or 0), int(lo), int(hi), title))
    out = {}
    for pid, d in pages.items():
        best = None
        for level, lo, hi, title in by_source.get(d["source_id"], ()):
            if lo <= d["page_index"] <= hi and (best is None or level > best[0]):
                best = (level, title)
        if best:
            out[pid] = best[1]
    return out


def handbook(defs: dict[str, dict]) -> str:
    lines = ["# Темы TOPIC_BENCHMARK_V1 — справочник оценщика (вне git)", ""]
    for d in defs.values():
        lines.append(f"## {d['topic_id']} · {d['track']} · {d['group']} · {d['title']}")
        lines.append("запросы: " + " | ".join(d["queries"][1:]))
        if d["track"] == "PROCESS":
            lines.append("роль: " + short(d.get("role"), 300))
            lines.append("путь к оседаниям: " + short(d.get("path"), 300))
            lines.append("переменные: " + short(d.get("variables"), 200))
            lines.append("параметры: " + short(d.get("parameters"), 300))
            lines.append("уравнения: " + short(d.get("equations"), 300))
        else:
            lines.append("модели: " + "; ".join(short(m, 110) for m in d.get("models", [])))
        lines.append("")
    return "\n".join(lines) + "\n"


def legend_entry(d: dict) -> list[str]:
    head = f"- **{d['topic_id']}** · {d['group'] if d['track'] == 'PROCESS' else 'MM'} · {d['title']}"
    out = [head, "  q: " + " | ".join(d["queries"][1:])]
    if d["track"] == "PROCESS":
        out.append("  роль: " + short(d.get("role"), 150) + " · перем.: " + short(d.get("variables"), 80))
    else:
        ms = d.get("models", [])
        text, n = [], 0
        for m in ms:
            s = short(m, 70)
            if n + len(s) > 260:
                break
            text.append(s)
            n += len(s) + 2
        more = f" (+{len(ms) - len(text)})" if len(ms) > len(text) else ""
        out.append("  модели: " + "; ".join(text) + more)
    return out


def packets() -> None:
    import duckdb

    work = env_path("LB_WORK")
    topics = load_topics()
    defs = topic_definitions(topics)
    trans = translation_texts()
    stems = {tid: topic_stems(d, trans.get(tid, [])) for tid, d in defs.items()}
    data = load_pool()
    by_page: dict[str, list[tuple[str, str, list[str]]]] = defaultdict(list)
    for tid, v in data["topics"].items():
        for u in v["units"]:
            by_page[u["pages"][0]].append((tid, u["cid"], u["pages"]))
    order = {t.topic_id: i for i, t in enumerate(topics)}
    con = duckdb.connect(str(env_path("CANON_DB")), read_only=True)
    pages = page_data(con, sorted({p for lst in by_page.values() for _t, _c, ps in lst for p in ps}))
    missing = sorted(p for p in by_page if p not in pages)
    idf = stem_idf([d["text"] for d in pages.values()])
    secs = section_titles(pages)
    meta = json.load(open(env_path("J_V1") / "pages.json", encoding="utf-8"))["sources"]
    (work / "topics_handbook.md").write_text(handbook(defs), encoding="utf-8")
    pk = work / "packets"
    pk.mkdir(parents=True, exist_ok=True)
    for f in pk.glob("pk-*.md"):
        f.unlink()
    judged = read_judgments(strict=False)
    todo_pages = [p for p in sorted(by_page) if any((t, c) not in judged for t, c, _ps in by_page[p])]
    blocks: list[list[str]] = []
    cur: list[str] = []
    n_units = 0
    index = []
    for p in todo_pages:
        if cur and (len(cur) >= PAGES_PER_PACKET or n_units + len(by_page[p]) > UNITS_PER_PACKET):
            blocks.append(cur)
            cur, n_units = [], 0
        cur.append(p)
        n_units += len(by_page[p])
    if cur:
        blocks.append(cur)
    for i, blk in enumerate(blocks, 1):
        tids = sorted({t for p in blk for t, _c, _ps in by_page[p]}, key=lambda x: order[x])
        lines = [f"# pk-{i:03d} — страниц {len(blk)}, единиц {sum(len(by_page[p]) for p in blk)}", "",
                 "Строка ответа: `тема|единица|оценка|КОД` (КОД: KEY_QUANT KEY_MODEL KEY_OBS KEY_MECH → 3; "
                 "SUP_DISCUSS SUP_FIGTAB SUP_ASPECT SUP_MODEL → 2; MENTION LIST NEIGHBOUR GENERAL → 1; "
                 "OFF_TOPIC NO_TEXT FRONT_MATTER → 0)", "", "## Темы пакета"]
        for tid in tids:
            lines += legend_entry(defs[tid])
        lines += ["", "## Страницы"]
        for p in blk:
            d = pages.get(p)
            cands = sorted(by_page[p], key=lambda x: order[x[0]])
            if d is None:
                lines += ["", f"### {p} | нет в каноне снимка", "→ " + " · ".join(f"{t} {c}" for t, c, _ in cands)]
                continue
            sm = meta.get(d["source_id"], {})
            head = (f"### {p} | {short(sm.get('title'), 70)} ({sm.get('year') or '?'}) | "
                    f"{len(d['text'])} зн.")
            if secs.get(p):
                head += f" | разд.: {short(secs[p], 70)}"
            if d["formulas"]:
                head += f" | формул: {d['formulas']}"
            copies = sorted({x for _t, _c, ps in cands for x in ps if x != p})
            if copies:
                head += " | копии: " + ", ".join(copies)
            lines += ["", head]
            caps = d["captions"][:MAX_CAPTIONS]
            if caps:
                lines.append(" ".join(f"[{k} {lab or ''}] {short(c, CAPTION_CHARS)}".replace("  ", " ")
                                      for k, lab, c in caps))
            if d.get("formula_previews"):
                lines.append("[формулы] " + " ¦ ".join(d["formula_previews"]))
            text = d["text"]
            if len(re.sub(r"\s+", "", text)) < 100:
                lines.append("[почти нет текста] " + short(text, 200))
            else:
                lines.append(page_windows(text, [stems[t] for t, _c, _ps in cands], idf))
            lines.append("→ " + " · ".join(f"{t} {c}" for t, c, _ in cands))
        write_lf(pk / f"pk-{i:03d}.md", "\n".join(lines) + "\n")
        index.append({"packet": f"pk-{i:03d}", "pages": blk,
                      "units": [[t, c] for p in blk for t, c, _ps in by_page[p]]})
    (work / "packets_index.json").write_text(json.dumps(index, ensure_ascii=False), encoding="utf-8")
    print(json.dumps({"packets": len(blocks), "pages": len(todo_pages), "missing_in_canon": missing[:20],
                      "units": sum(len(x["units"]) for x in index)}, ensure_ascii=False))


# ------------------------------------------------------------------------------------------------ judgments
def judgments_dir() -> Path:
    return Path(os.environ.get("LB_JUDGMENTS") or env_path("LB_WORK") / "judgments").expanduser()


def read_judgments(strict: bool = True) -> dict[tuple[str, str], tuple[int, str]]:
    out: dict[tuple[str, str], tuple[int, str]] = {}
    d = judgments_dir()
    if not d.is_dir():
        return out
    for f in sorted(d.glob("*.txt")):
        for n, line in enumerate(f.read_text(encoding="utf-8").splitlines(), 1):
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            parts = [x.strip() for x in line.split("|")]
            if len(parts) != 4 or not parts[2].isdigit():
                if strict:
                    raise SystemExit(f"{f.name}:{n}: bad line {line[:80]!r}")
                continue
            tid, cid, grade, code = parts
            key = (tid, cid)
            if key in out and strict and out[key] != (int(grade), code):
                raise SystemExit(f"{f.name}:{n}: {tid} {cid} judged twice with different labels")
            out[key] = (int(grade), code)
    return out


def status() -> None:
    idx = json.load(open(env_path("LB_WORK") / "packets_index.json", encoding="utf-8"))
    judged = read_judgments(strict=False)
    total = done = 0
    for p in idx:
        n = len(p["units"])
        k = sum(1 for t, c in p["units"] if (t, c) in judged)
        total += n
        done += k
        if k < n:
            print(f"{p['packet']}: {k}/{n}")
    print(f"judged {done} / {total}")


def pooled_from(systems: dict[str, int]) -> str:
    return ",".join(f"{s}@{r}" for s, r in sorted(systems.items(), key=lambda x: (x[1], x[0])))


def validate_rows(rows: list[dict[str, str]], topics: list[TB.Topic]) -> list[str]:
    """Schema of the topic pooled labels: known topic, PAGE id grammar, grade 0..3, CANDIDATE / POOL_JUDGMENT /
    LLM_AGENT_T1, ``system@rank`` provenance, a known reason code of the same grade, no duplicate pair, no pair that
    matches a catalogue target of the topic (the catalogue wins)."""
    problems: list[str] = []
    tmap = {t.topic_id: t for t in topics}
    tpages = {t.topic_id: target_pages(t) for t in topics}
    seen: set[tuple[str, str]] = set()
    for r in rows:
        where = f"{r.get('query_id')} {r.get('doc_id')}"
        if r["query_id"] not in tmap:
            problems.append(f"{where}: unknown topic")
        if r["level"] != "PAGE" or not TB.PAGE_ID.match(r["doc_id"] or ""):
            problems.append(f"{where}: bad level/doc_id")
        if not r["grade"].isdigit() or int(r["grade"]) not in GRADES:
            problems.append(f"{where}: grade {r['grade']!r}")
        if (r["status"], r["basis"], r["label_source"]) != ("CANDIDATE", "POOL_JUDGMENT", LABEL_SOURCE):
            problems.append(f"{where}: status/basis/source {r['status']}/{r['basis']}/{r['label_source']}")
        if not _POOLED_FROM.match(r["pooled_from"] or ""):
            problems.append(f"{where}: pooled_from {r['pooled_from']!r}")
        code = r["rationale"]
        if code not in CODES or (r["grade"].isdigit() and CODES[code] != int(r["grade"])):
            problems.append(f"{where}: code {code!r} does not fit grade {r['grade']}")
        key = (r["query_id"], r["doc_id"])
        if key in seen:
            problems.append(f"{where}: duplicate pair")
        seen.add(key)
        if r["doc_id"] in tpages.get(r["query_id"], frozenset()):
            problems.append(f"{where}: page is a catalogue target of the topic")
    return problems


def ingest() -> None:
    topics = load_topics()
    data = load_pool()
    judg = read_judgments()
    rows, missing, bad, groups = [], [], [], []
    known = set()
    for tid, v in data["topics"].items():
        for u in v["units"]:
            key = (tid, u["cid"])
            known.add(key)
            if key not in judg:
                missing.append(key)
                continue
            g, code = judg[key]
            if g not in GRADES or CODES.get(code) != g:
                bad.append(key)
                continue
            for p in u["pages"]:
                rows.append({"query_id": tid, "level": "PAGE", "doc_id": p, "grade": str(g), "status": "CANDIDATE",
                             "basis": "POOL_JUDGMENT", "label_source": LABEL_SOURCE,
                             "pooled_from": pooled_from(u["per_page"][p]), "rationale": code})
            if len(u["pages"]) > 1 or u["aliases"]:
                groups.append({"topic_id": tid, "pages": u["pages"], "aliases": u["aliases"]})
    extra = sorted(k for k in judg if k not in known)
    print(f"judged {len(known) - len(missing) - len(bad)}; missing {len(missing)}; bad {len(bad)}; extra {len(extra)}")
    if missing:
        print("missing:", missing[:20])
    if bad:
        print("bad:", bad[:20])
    if extra:
        print("extra:", extra[:20])
    if "--partial" not in sys.argv and (missing or bad or extra):
        raise SystemExit("not all pooled units are judged correctly (use --partial for an interim file)")
    problems = validate_rows(rows, topics)
    if problems:
        raise SystemExit("\n".join(problems[:30]))
    order = {t.topic_id: i for i, t in enumerate(topics)}
    rows.sort(key=lambda r: (order[r["query_id"]], -int(r["grade"]), r["doc_id"]))
    with open(OUT_TSV, "w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=B.POOLED_COLUMNS, delimiter="\t", lineterminator="\n")
        w.writeheader()
        w.writerows(rows)
    groups.sort(key=lambda g: (order[g["topic_id"]], g["pages"][0]))
    doc = {"benchmark": "TOPIC_BENCHMARK_V1", "label_source": LABEL_SOURCE, "qrels": OUT_TSV.name,
           "qrels_sha256": sha256_file(OUT_TSV),
           "rule": "label units of the pool with more than one page (duplicates among a topic's candidates share one "
                   "grade) or with duplicate aliases (API duplicates of the page, canonical duplicate_page_candidates); "
                   "truth P makes one target per unit: its first page, the other pages and the aliases as alternates",
           "groups": groups}
    write_lf(OUT_GROUPS, json.dumps(doc, ensure_ascii=False, indent=1) + "\n")
    print("grades", dict(sorted(Counter(r["grade"] for r in rows).items())), "rows", len(rows), "groups", len(groups),
          "->", OUT_TSV.name)


# ------------------------------------------------------------------------------------------------ review sample
def origin(systems: dict[str, int]) -> str:
    s = set(systems)
    if s == {"nav"}:
        return "nav_only"
    if len(s) == 1:
        return "one_other"
    return "several"


def draw_sample(rows: list[dict[str, str]], topics: list[TB.Topic], seed: int = HSAMPLE_SEED) -> list[dict]:
    """POOL_LABELING_V1.md §8: 15 labels per grade; round robin over the strata track × origin; order inside a stratum
    by sha256(seed|topic|page); at most one label per topic within a grade while other strata have candidates; then a
    seeded shuffle. The label unit is represented by its first page."""
    track = {t.topic_id: t.track for t in topics}
    later_pages: set[tuple[str, str]] = set()
    if OUT_GROUPS.is_file():
        for g in json.load(open(OUT_GROUPS, encoding="utf-8"))["groups"]:
            later_pages |= {(g["topic_id"], p) for p in g["pages"][1:]}
    strata: dict[tuple, list[dict]] = defaultdict(list)
    for r in rows:
        if (r["query_id"], r["doc_id"]) in later_pages:
            continue
        systems = {x.split("@")[0]: int(x.split("@")[1]) for x in r["pooled_from"].split(",")}
        strata[(int(r["grade"]), track[r["query_id"]], origin(systems))].append(r)
    picked: list[dict] = []
    for g in (3, 2, 1, 0):
        keys = sorted(k for k in strata if k[0] == g)
        pools = {k: sorted(strata[k], key=lambda r: hashlib.sha256(
            f"{seed}|{r['query_id']}|{r['doc_id']}".encode()).hexdigest()) for k in keys}
        chosen: list[dict] = []
        seen_t: set[str] = set()
        while len(chosen) < HSAMPLE_PER_GRADE and any(pools.values()):
            for k in keys:
                if len(chosen) >= HSAMPLE_PER_GRADE:
                    break
                while pools[k]:
                    r = pools[k].pop(0)
                    if r["query_id"] in seen_t and any(pools[x] for x in keys if x != k):
                        continue
                    chosen.append(r)
                    seen_t.add(r["query_id"])
                    break
        picked += chosen
    random.Random(seed).shuffle(picked)
    return picked


def hsample() -> None:
    work = env_path("LB_WORK")
    hdir = work / "h_review"
    hdir.mkdir(parents=True, exist_ok=True)
    topics = load_topics()
    tmap = {t.topic_id: t for t in topics}
    rows = B.load_pooled_qrels(OUT_TSV)
    if "--draw" in sys.argv:
        import duckdb

        picked = draw_sample(rows, topics)
        key = [{"n": i, "topic_id": r["query_id"], "page_id": r["doc_id"], "grade": int(r["grade"]),
                "code": r["rationale"], "pooled_from": r["pooled_from"]} for i, r in enumerate(picked, 1)]
        (hdir / "key.json").write_text(json.dumps(key, ensure_ascii=False, indent=1), encoding="utf-8")
        defs = topic_definitions(topics)
        con = duckdb.connect(str(env_path("CANON_DB")), read_only=True)
        pages = page_data(con, [k["page_id"] for k in key])
        snip = ["# H_REVIEW_SNIPPETS_T1 — текст страниц выборки (вне git, содержит текст корпуса)", ""]
        sheet = ["# описания: `№|описание` (≤ 15 слов, нейтрально, без слов оценки)", ""]
        for k in key:
            d = defs[k["topic_id"]]
            text = re.sub(r"\s+", " ", pages.get(k["page_id"], {}).get("text", "")).strip()
            snip += [f"## {k['n']}. {k['topic_id']} → {k['page_id']}", f"Тема: {d['title']}",
                     text, ""]                  # the whole page: the reviewer is not steered by the labeller's windows
            sheet.append(f"{k['n']}|")
        (hdir / "H_REVIEW_SNIPPETS_T1.md").write_text("\n".join(snip) + "\n", encoding="utf-8")
        if not (hdir / "descriptions.txt").is_file():
            (hdir / "descriptions.txt").write_text("\n".join(sheet) + "\n", encoding="utf-8")
        print("sample", len(key), dict(Counter((k["grade"], tmap[k["topic_id"]].track) for k in key)))
        return
    key = json.load(open(hdir / "key.json", encoding="utf-8"))
    desc = {}
    for line in (hdir / "descriptions.txt").read_text(encoding="utf-8").splitlines():
        if "|" in line and line.split("|", 1)[0].strip().isdigit():
            n, text = line.split("|", 1)
            desc[int(n)] = text.strip()
    empty = [k["n"] for k in key if not desc.get(k["n"])]
    if empty:
        raise SystemExit(f"descriptions missing for {empty}")
    too_long = [n for n, t in desc.items() if len(t.split()) > 15 or "|" in t]
    if too_long:
        raise SystemExit(f"descriptions longer than 15 words or with '|': {too_long}")
    lines = [
        "# H_REVIEW_SAMPLE_T1 — слепая выборка пул-меток T1 для ревью H", "",
        "Метки `LLM_AGENT_T1` (статус CANDIDATE, основание POOL_JUDGMENT) — оценки агента LB для пар «тема × страница» "
        "пула TOPIC_BENCHMARK_V1 ([POOL_LABELING_V1.md](POOL_LABELING_V1.md)). Выборка — по правилу §8: по 15 меток "
        f"каждой оценки 3/2/1/0, страты «трек × происхождение», seed {HSAMPLE_SEED}.", "",
        "**Ревью слепое.** Здесь нет оценки агента, кода причины и системы, которая нашла страницу. Оценки агента "
        f"записаны в [qrels_topic_v1_pooled.tsv](qrels_topic_v1_pooled.tsv) (sha256 `{sha256_file(OUT_TSV)}`) — "
        "не открывайте его до своей оценки.", "",
        "- **Что оценивать:** насколько страница — свидетельство по теме (определение темы — в "
        "[topic_set_v1.jsonl](topic_set_v1.jsonl) и строке каталога).",
        "- **Шкала:** 3 — прямое свидетельство (числа, закон или формула, наблюдение, конкретный механизм; у семейства "
        "моделей — модель изложена или применена); 2 — существенно, но частично (содержательный абзац, рисунок или "
        "таблица, один аспект); 1 — упоминание, перечень или соседняя тема; 0 — не о теме.",
        "- **Место** (ВКМ, другое месторождение, лаборатория, учебник) на оценку не влияет.",
        "- **Где читать страницу:** инструмент MCP `get_page` по `page_id` или полный текст страниц выборки на "
        "WORKSTATION (`$VKM_WORK/lb_t1/h_review/H_REVIEW_SNIPPETS_T1.md`, вне git). Описание в таблице — нейтральный "
        "пересказ агента, не цитата.",
        "- **Как отвечать:** в колонке «H» поставить свою оценку 0–3. Согласие считает "
        "`python benchmarks/topic_v1/scripts/pool_t1.py hscore <этот файл>`.", "",
        "| # | тема | название темы | page_id | что на странице | H |", "|---|---|---|---|---|---|"]
    for k in key:
        title = tmap[k["topic_id"]].title
        title = title if len(title) <= 90 else title[:89] + "…"
        lines.append(f"| {k['n']} | {k['topic_id']} | {title} | {k['page_id']} | {desc[k['n']]} | |")
    stats = Counter(tmap[k["topic_id"]].track for k in key)
    lines += ["", f"Состав: {len(key)} меток; по трекам: " +
              ", ".join(f"{t} {n}" for t, n in sorted(stats.items())) + "."]
    write_lf(H_SAMPLE_MD, "\n".join(lines) + "\n")
    print("written", H_SAMPLE_MD.name, len(key))


def agreement(a: list[int], b: list[int], k: int = 4) -> dict[str, float]:
    """Exact and ±1 agreement, Cohen's kappa (unweighted, linear, quadratic) and Krippendorff's alpha (ordinal, two
    coders, no missing values) of two ordinal gradings 0..k-1."""
    n = len(a)
    obs = [[0] * k for _ in range(k)]
    for x, y in zip(a, b):
        obs[x][y] += 1
    ra = [sum(obs[i]) for i in range(k)]
    cb = [sum(obs[i][j] for i in range(k)) for j in range(k)]

    def kappa(w) -> float:
        po = sum(w(i, j) * obs[i][j] for i in range(k) for j in range(k)) / n
        pe = sum(w(i, j) * ra[i] * cb[j] for i in range(k) for j in range(k)) / (n * n)
        return (po - pe) / (1 - pe) if pe < 1 else 1.0

    res = {"n": n, "exact": sum(1 for x, y in zip(a, b) if x == y) / n,
           "within_one": sum(1 for x, y in zip(a, b) if abs(x - y) <= 1) / n,
           "kappa": kappa(lambda i, j: 1.0 if i == j else 0.0),
           "kappa_linear": kappa(lambda i, j: 1 - abs(i - j) / (k - 1)),
           "kappa_quadratic": kappa(lambda i, j: 1 - ((i - j) / (k - 1)) ** 2)}
    # Krippendorff's alpha, ordinal metric: coincidence matrix of the pairable values
    values = a + b
    nv = Counter(values)
    total = len(values)
    coinc = Counter()
    for x, y in zip(a, b):
        coinc[(x, y)] += 1
        coinc[(y, x)] += 1

    def delta(c: int, d: int) -> float:
        lo, hi = min(c, d), max(c, d)
        s = sum(nv[g] for g in range(lo, hi + 1)) - (nv[c] + nv[d]) / 2
        return s * s

    do = sum(coinc[(c, d)] * delta(c, d) for c in range(k) for d in range(k))
    de = sum(nv[c] * nv[d] * delta(c, d) for c in range(k) for d in range(k)) / (total - 1)
    res["krippendorff_alpha_ordinal"] = 1 - do / de if de else 1.0
    return {m: (round(v, 4) if isinstance(v, float) else v) for m, v in res.items()}


def hscore() -> None:
    path = Path(sys.argv[2])
    rows = B.load_pooled_qrels(OUT_TSV)
    grade = {(r["query_id"], r["doc_id"]): int(r["grade"]) for r in rows}
    a, b, diff = [], [], []
    for line in path.read_text(encoding="utf-8").splitlines():
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if len(cells) != 6 or not cells[0].isdigit():
            continue
        h = cells[5]
        if not h.isdigit() or int(h) not in GRADES:
            continue
        g = grade[(cells[1], cells[3])]
        a.append(g)
        b.append(int(h))
        if g != int(h):
            diff.append({"n": int(cells[0]), "topic_id": cells[1], "page_id": cells[3], "grade_t1": g,
                         "grade_h": int(h)})
    if not a:
        raise SystemExit("no filled H grades found")
    res = agreement(a, b)
    res["h_higher"] = sum(1 for x, y in zip(a, b) if y > x)
    res["h_lower"] = sum(1 for x, y in zip(a, b) if y < x)
    res["evidence_threshold_agreement"] = round(sum(1 for x, y in zip(a, b) if (x >= 2) == (y >= 2)) / len(a), 4)
    print(json.dumps({"agreement": res, "disagreements": diff}, ensure_ascii=False, indent=1))


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else ""
    fn = {"rank": rank, "pool": pool, "packets": packets, "status": status, "ingest": ingest, "hsample": hsample,
          "hscore": hscore}.get(cmd)
    if fn is None:
        raise SystemExit(__doc__)
    fn()
